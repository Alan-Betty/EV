"""Session state and local fast-path intents.

Some commands must never wait on a network round trip. "E.V., take five"
should land the instant it is heard - going out to an LLM to be told the user
wants a pause is both slow and absurd, and it fails exactly when it matters
most, which is when E.V. is mid-task and the user wants it to stop.

So a small set of control phrases are matched locally, in microseconds, before
the brain is consulted at all. Everything else goes to the model as usual.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum

import config


class Mode(str, Enum):
    """Where E.V. is in a conversation.

    IDLE     - not being talked to; the wake phrase is required.
    ENGAGED  - mid-conversation; just talk, no wake phrase needed. Decays back
               to IDLE after a stretch of silence.
    STANDBY  - explicitly told to wait; ignores everything but "wake up".
    """

    IDLE = "idle"
    ENGAGED = "engaged"
    STANDBY = "standby"

    # Kept so existing call sites reading Mode.ACTIVE keep working.
    ACTIVE = "engaged"


class Intent(str, Enum):
    STANDBY = "standby"  # pause, stop talking, wait for me
    RESUME = "resume"  # come back
    CANCEL = "cancel"  # abandon what you are doing, stay awake
    SHUTDOWN = "shutdown"  # quit entirely
    STATUS = "status"  # are you there / what are you doing


_PUNCT = re.compile(r"[^\w\s']")


def _normalise(text: str) -> str:
    return " ".join(_PUNCT.sub(" ", text.lower()).split())


# Phrase -> intent. Matched against the whole utterance, so these stay short
# and unambiguous; anything longer is a real request and belongs to the model.
_PHRASES: dict[Intent, tuple[str, ...]] = {
    Intent.STANDBY: (
        "take five", "take 5", "take a five", "take a break", "take a breather",
        "stand by", "standby", "hold on", "hold up", "hang on", "hang tight",
        "pause", "pause for a bit", "wait", "wait a sec", "wait a second",
        "wait a minute", "give me a minute", "give me a sec", "give me a second",
        "one sec", "one second", "one moment", "just a sec", "just a second",
        "sit tight", "be quiet", "quiet", "shush", "hush", "mute", "shut up",
        "stop talking", "zip it", "cool it", "chill", "chill out", "relax",
        "go to sleep", "sleep", "power down", "idle", "brb", "be right back",
    ),
    Intent.RESUME: (
        "wake up", "you there", "you awake", "back to work", "resume",
        "continue", "carry on", "i'm back", "im back", "we're back", "were back",
        "let's go", "lets go", "ready", "you up", "rise and shine",
        "break's over", "breaks over", "time's up", "times up", "unmute",
        "start listening", "listen up", "come back",
    ),
    Intent.CANCEL: (
        "stop", "stop it", "cancel", "cancel that", "never mind", "nevermind",
        "abort", "forget it", "forget that", "belay that", "scratch that",
        "undo that", "don't", "dont", "no stop", "stop stop",
    ),
    Intent.SHUTDOWN: (
        "shut down", "shutdown", "power off", "goodbye", "good bye", "bye",
        "goodnight", "good night", "exit", "quit", "that's all", "thats all",
        "we're done", "were done", "sign off", "log off", "dismissed",
    ),
    Intent.STATUS: (
        "you there", "are you there", "status", "what are you doing",
        "you still there", "still with me", "report",
    ),
}

# RESUME and STATUS share "you there". In standby, resuming is what the user
# means, so RESUME is checked first and this ordering is deliberate.
_PRIORITY = (Intent.RESUME, Intent.STANDBY, Intent.CANCEL, Intent.SHUTDOWN, Intent.STATUS)

_LOOKUP: dict[str, Intent] = {}
for _intent in _PRIORITY:
    for _phrase in _PHRASES[_intent]:
        _LOOKUP.setdefault(_phrase, _intent)


# ---------------------------------------------------------------------------
# Compound requests
# ---------------------------------------------------------------------------
# "Open my mail and give me a summary of the important things" is two jobs,
# and the model answers it with one tool call - it opens the mail and drops
# the summary. Nothing was wrong with the call; the second half simply never
# became one. From where the user is standing that reads as E.V. ignoring
# them, because the part they were waiting for is the part that vanished.
#
# This finds that trailing half so the core loop can go back for it.
#
# The gate is deliberately narrow: only a trailing *question* counts. An
# action followed by another action ("open Chrome and search for X") is
# usually one tool call on purpose, and re-running the tail of those would
# search twice. A question has no side effect to double, so the worst case of
# a false positive here is one wasted round trip - against a silent failure,
# which is what the alternative costs.
_JOINER = re.compile(
    r"\s+(?:and\s+then|and\s+also|then|and)\s+|\s*[;,]\s*then\s+", re.IGNORECASE
)

# Openings that mean "tell me something", as opposed to "do something".
_ASK_OPENERS = (
    "give me", "gimme", "get me", "tell me", "show me what", "read me",
    "read out", "read back", "let me know", "fill me in", "catch me up",
    "summarise", "summarize", "summary", "describe", "explain", "list",
    "what", "which", "who", "when", "where", "why", "how",
    "is there", "are there", "anything", "any ",
)

# A trailing clause has to be doing something with information to qualify.
_ASK_WORDS = (
    "summary", "summarise", "summarize", "important", "gist", "rundown",
    "overview", "tell", "read", "say", "what", "which", "anything",
    "unread", "new ", "latest", "recap",
)


def split_followup(text: str) -> str:
    """Return the trailing question of a compound request, or "".

    Conservative by design. It returns something only when the utterance
    splits on a joining word *and* the trailing clause reads as a request for
    information rather than a second action.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""

    parts = [part.strip(" .,;") for part in _JOINER.split(cleaned)]
    parts = [part for part in parts if part]
    if len(parts) < 2:
        return ""

    tail = parts[-1]
    lowered = tail.lower()
    # Too short to be a request of its own: "open Chrome and go" is one job.
    if len(lowered.split()) < 2:
        return ""
    if not lowered.startswith(_ASK_OPENERS):
        return ""
    if not any(word in lowered for word in _ASK_WORDS):
        return ""
    return tail


def match_intent(text: str, mode: Mode = Mode.ACTIVE) -> Intent | None:
    """Return a control intent for this utterance, or None.

    Matching is exact against the normalised utterance. A fuzzy match here
    would be far worse than a miss: mistaking "stop the server" for `CANCEL`
    would drop a real request on the floor.
    """
    normalised = _normalise(text)
    if not normalised:
        return None

    intent = _LOOKUP.get(normalised)
    if intent is None:
        # Allow a trailing "please" or a leading "okay", nothing more.
        stripped = re.sub(r"^(okay|ok|please|now)\s+", "", normalised)
        stripped = re.sub(r"\s+(please|now|for now|for a bit|a bit)$", "", stripped)
        intent = _LOOKUP.get(stripped)

    if intent is None:
        return None

    # In standby the only thing worth acting on is coming back, plus a hard
    # shutdown. Everything else is the user talking to someone who is not E.V.
    if mode is Mode.STANDBY and intent not in {Intent.RESUME, Intent.SHUTDOWN}:
        return None
    if mode is not Mode.STANDBY and intent is Intent.RESUME:
        return Intent.STATUS  # already awake; treat it as a check-in
    return intent


# Replies are pooled so E.V. does not repeat itself word for word all day.
_RESPONSES: dict[Intent, tuple[str, ...]] = {
    # Deliberately terse. These are spoken aloud, and a control phrase that
    # takes four seconds to acknowledge defeats the point of matching it
    # locally in the first place.
    Intent.STANDBY: (
        "Standing by.",
        "Standing by.",
        "Going quiet.",
    ),
    Intent.RESUME: (
        "Back.",
        "Still here.",
        "Go ahead.",
    ),
    Intent.CANCEL: (
        "Dropped it.",
        "Stopped.",
        "Killed it.",
        "Done, nothing ran.",
    ),
    Intent.SHUTDOWN: (
        "Shutting down. Later.",
        "Powering off. Don't break anything.",
        "Signing off.",
    ),
    Intent.STATUS: (
        "Still here.",
        "Right here. Idle.",
        "Awake and waiting.",
    ),
}


# Words that mean "come back" when E.V. is asleep. Matched loosely, unlike
# everything above.
_RESUME_HINTS = (
    "wake", "awake", "you there", "you up", "back to work", "resume",
    "listen up", "start listening", "come back", "unmute", "rise and shine",
    "break's over", "breaks over", "time's up", "times up", "i'm back",
    "im back", "we're back", "were back", "ready when you are",
)
# Longer than this and it is a conversation happening in the room, not someone
# trying to wake E.V. up. "Wake up" is two words; nobody needs seven.
_RESUME_MAX_WORDS = 6


def is_resume_phrase(text: str) -> bool:
    """Loose match for coming out of standby.

    `match_intent` is deliberately exact, because mistaking "stop the server"
    for a cancel would drop a real request on the floor. That risk does not
    exist here: standby has exactly two exits and no real commands, so there
    is nothing a loose match could swallow. What it fixes is the opposite
    failure - "hey, wake up" and "EV, you awake?" being silently ignored,
    which leaves the user with no way back in and no clue why.
    """
    normalised = _normalise(text)
    if not normalised or len(normalised.split()) > _RESUME_MAX_WORDS:
        return False
    return any(hint in normalised for hint in _RESUME_HINTS)


def response_for(intent: Intent) -> str:
    return random.choice(_RESPONSES[intent])


def all_responses() -> list[str]:
    """Every stock reply, so the speech cache can be warmed with them."""
    return [phrase for pool in _RESPONSES.values() for phrase in pool]


@dataclass
class Session:
    """Tracks whether E.V. is being talked to, and how recently.

    The point of the ENGAGED state is continuity: once a conversation has
    started, the user should be able to keep talking without saying the name
    before every sentence. The state decays on its own, so E.V. stops
    listening in on the room a minute after the conversation ends.
    """

    mode: Mode = Mode.IDLE
    standby_since: float = 0.0
    last_exchange_at: float = 0.0
    pending: dict | None = None  # a terminal_command held for confirmation
    turns: int = 0

    # -- queries ----------------------------------------------------------
    @property
    def in_standby(self) -> bool:
        return self.mode is Mode.STANDBY

    @property
    def engaged(self) -> bool:
        """True while E.V. is mid-conversation and needs no wake phrase."""
        if self.mode is not Mode.ENGAGED:
            return False
        if self.silence_seconds > config.CONVERSATION_WINDOW_S:
            # Conversation went quiet; drop back to needing the wake phrase.
            self.mode = Mode.IDLE
            self.turns = 0
            return False
        return True

    @property
    def silence_seconds(self) -> float:
        if not self.last_exchange_at:
            return float("inf")
        return time.monotonic() - self.last_exchange_at

    @property
    def standby_seconds(self) -> float:
        return time.monotonic() - self.standby_since if self.in_standby else 0.0

    # -- transitions ------------------------------------------------------
    def engage(self) -> None:
        """Start (or extend) a conversation."""
        self.mode = Mode.ENGAGED
        self.standby_since = 0.0
        self.last_exchange_at = time.monotonic()

    def enter_standby(self) -> None:
        self.mode = Mode.STANDBY
        self.standby_since = time.monotonic()
        self.pending = None
        self.turns = 0

    def resume(self) -> None:
        self.engage()

    def go_idle(self) -> None:
        self.mode = Mode.IDLE
        self.turns = 0

    def mark_exchange(self) -> None:
        """Record that a real exchange happened, extending the conversation."""
        self.last_exchange_at = time.monotonic()
        if self.mode is not Mode.STANDBY:
            self.mode = Mode.ENGAGED
            self.turns += 1
