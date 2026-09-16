"""Wake-phrase matching.

No wake-word model. The utterance is already being transcribed, so detection
is a string match on the front of the transcript - which costs nothing and,
unlike a keyword spotter, keeps no weights in memory.

Two things make this harder than it looks:

* "E.V." is two letters, so speech-to-text mangles it constantly: EV, E.V.,
  Eve, Evie, AV, EB, Ivy, and - when the greeting runs into it - "heavy".
  Matching is therefore fuzzy, not exact. A missed wake word means E.V. sits
  there silently while the user waits, which is the worst failure mode there is.

* The command has to be sliced off the *original* transcript, by character
  offset. Slicing by word index desynchronises the moment STT writes "E.V."
  (one word, two normalised tokens) and silently eats the next word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

import config

# Renderings of "E.V." seen in real Whisper output. Cheaper and far more
# predictable than relying on fuzzy matching alone.
_HOMOPHONES = {
    "ev", "e v", "ev.", "e.v", "evee", "eevee", "evie", "evy", "evi",
    "eve", "eva", "av", "a v", "ab", "eb", "e b", "ivy", "ivey",
    "envy", "everie", "heavy", "hevy", "ebe", "ebb", "evan",
}

# Greetings that can precede the name. "a" is deliberately absent: STT writes
# "A.V." for the name, and treating that leading "a" as a greeting throws the
# match away.
_GREETINGS = {"hey", "hi", "hello", "okay", "ok", "yo", "hay", "uh", "um", "excuse", "me"}

# "heavy" is "hey EV" run together, so these need no greeting in front.
_MERGED = {"heavy", "hevy", "hayev", "heyev"}

# Ordinary English words close enough to the name to pass a fuzzy check.
# "every" scores 0.91 against "everie" and "even" scores 0.86 against "eve",
# so without this list E.V. answers to "every single time" and "even so".
_NEVER_A_NAME = {
    "every", "even", "ever", "evening", "everything", "everyone", "eventually",
    "already", "above", "about", "away", "over", "very", "level", "seven",
    "given", "eleven", "email", "edit", "end", "and", "add", "app",
}

_TOKEN = re.compile(r"[\w']+")
_MIN_RATIO = 0.74


@dataclass
class WakeMatch:
    matched: bool
    command: str  # the transcript with the wake phrase removed
    confidence: float = 0.0


def _tokens(text: str) -> list[tuple[str, int, int]]:
    """Normalised words with their character spans in the original string."""
    return [
        (match.group(0).lower(), match.start(), match.end())
        for match in _TOKEN.finditer(text)
    ]


def _wake_forms() -> set[str]:
    """Every accepted spelling of the name, greetings stripped, as one string."""
    forms = set(_HOMOPHONES)
    for phrase in config.WAKE_PHRASES:
        words = [word for word in _TOKEN.findall(phrase.lower())]
        while words and words[0] in _GREETINGS:
            words = words[1:]
        if words:
            forms.add(" ".join(words))
    return {form for form in forms if form}


def _similar(candidate: str, forms: set[str]) -> float:
    """Best fuzzy score of `candidate` against any accepted form.

    Known spellings match exactly. Fuzzy matching beyond that is deliberately
    tight: it must agree on the first letter and stay within two characters of
    length. Without the first-letter rule, "never" scores 0.75 against "eve"
    and E.V. wakes up every time someone says "never mind".
    """
    if candidate in forms:
        return 1.0
    if candidate in _NEVER_A_NAME:
        return 0.0
    best = 0.0
    for form in forms:
        # Length guard: "open" should never fuzzy-match "ev".
        if abs(len(form) - len(candidate)) > 2:
            continue
        if candidate[:1] != form[:1]:
            continue
        ratio = SequenceMatcher(None, candidate, form).ratio()
        if ratio > best:
            best = ratio
    return best


def detect(transcript: str) -> WakeMatch:
    """Check for the wake phrase and return whatever command followed it.

    A bare wake phrase ("E.V.?") matches with an empty command, which the core
    loop reads as "acknowledge, then listen for the real request".
    """
    if not config.WAKE_REQUIRED:
        return WakeMatch(True, transcript.strip(), 1.0)

    tokens = _tokens(transcript)
    if not tokens:
        return WakeMatch(False, "")

    forms = _wake_forms()

    # Skip a leading greeting, but remember where it started in case the
    # greeting itself turns out to be part of a merged form like "heavy".
    start = 0
    while start < len(tokens) and tokens[start][0] in _GREETINGS:
        start += 1
        if start >= 3:  # "excuse me" is two words; nothing sane is longer
            break

    # Try one-token then two-token candidates ("ev" and "e v" both occur).
    for width in (2, 1):
        end = start + width
        if end > len(tokens):
            continue
        candidate = " ".join(token[0] for token in tokens[start:end])
        score = _similar(candidate, forms)
        if score >= _MIN_RATIO:
            # Slice the ORIGINAL text by character offset so nothing is lost.
            cut = tokens[end - 1][2]
            command = transcript[cut:].strip().lstrip(",.:;!?-").strip()
            return WakeMatch(True, command, score)

    # A merged greeting-plus-name ("heavy, open Chrome") has no greeting to skip.
    if tokens[0][0] in _MERGED:
        cut = tokens[0][2]
        command = transcript[cut:].strip().lstrip(",.:;!?-").strip()
        return WakeMatch(True, command, 0.8)

    # Trailing address: "open Chrome, E.V."
    if len(tokens) > 1:
        for width in (2, 1):
            if len(tokens) <= width:
                continue
            candidate = " ".join(token[0] for token in tokens[-width:])
            if _similar(candidate, forms) >= _MIN_RATIO:
                cut = tokens[-width][1]
                command = transcript[:cut].strip().rstrip(",.:;!?-").strip()
                return WakeMatch(True, command, 0.8)

    return WakeMatch(False, "")


def heard_something_like_a_name(transcript: str) -> bool:
    """Did this probably address E.V. without quite matching?

    Used only to tell the user "I think you said my name but I wasn't sure"
    instead of ignoring them, which is what makes a voice assistant feel broken.
    """
    tokens = _tokens(transcript)
    if not tokens:
        return False
    forms = _wake_forms()
    for token, _, _ in tokens[:3]:
        if _similar(token, forms) >= 0.6:
            return True
    return False
