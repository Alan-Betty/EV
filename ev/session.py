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
    ACTIVE = "active"
    STANDBY = "standby"


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
    if mode is Mode.ACTIVE and intent is Intent.RESUME:
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


def response_for(intent: Intent) -> str:
    return random.choice(_RESPONSES[intent])


def all_responses() -> list[str]:
    """Every stock reply, so the speech cache can be warmed with them."""
    return [phrase for pool in _RESPONSES.values() for phrase in pool]


@dataclass
class Session:
    """Tracks whether E.V. is listening, and for how long it has been idle."""

    mode: Mode = Mode.ACTIVE
    standby_since: float = 0.0
    last_reply_at: float = 0.0
    # A terminal_command held back for confirmation, if any.
    pending: dict | None = None
    history_note: str = field(default="", repr=False)

    @property
    def in_standby(self) -> bool:
        return self.mode is Mode.STANDBY

    def enter_standby(self) -> None:
        self.mode = Mode.STANDBY
        self.standby_since = time.monotonic()
        self.pending = None

    def resume(self) -> None:
        self.mode = Mode.ACTIVE
        self.standby_since = 0.0
        self.last_reply_at = time.monotonic()

    @property
    def standby_seconds(self) -> float:
        return time.monotonic() - self.standby_since if self.in_standby else 0.0

    def in_followup_window(self) -> bool:
        """True while a follow-up needs no wake phrase."""
        if self.in_standby:
            return False
        return time.monotonic() - self.last_reply_at < config.FOLLOWUP_WINDOW_S

    def mark_replied(self) -> None:
        self.last_reply_at = time.monotonic()
