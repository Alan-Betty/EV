"""Wake-phrase matching.

No wake-word model. The utterance is already being transcribed, so detection
is a string match on the front of the transcript - which costs nothing and,
unlike a keyword spotter, keeps no weights in memory.

Two things make this harder than it looks:

* "Leo" is two letters, so speech-to-text mangles it constantly: Leo, Leo,
  Eve, Evie, AV, EB, Ivy, and - when the greeting runs into it - "heavy".
  Matching is therefore fuzzy, not exact. A missed wake word means Leo sits
  there silently while the user waits, which is the worst failure mode there is.

* The command has to be sliced off the *original* transcript, by character
  offset. Slicing by word index desynchronises the moment STT writes "Leo"
  (one word, two normalised tokens) and silently eats the next word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

import config

# Renderings of the name that Whisper is expected to produce. The list is short
# on purpose: every form here also widens the fuzzy match, and "leeo" alone
# made "lee" a wake word. Real clips decide what else belongs.
_HOMOPHONES = {"leo", "leo.", "l e o", "l.e.o", "l.e.o.", "lio", "leyo"}

# Greetings that can precede the name.
_GREETINGS = {"hey", "hi", "hello", "okay", "ok", "yo", "hay", "uh", "um", "excuse", "me"}

# A greeting run into the name, so no greeting is there to skip.
_MERGED = {"heyleo", "hayleo", "yoleo"}

# Ordinary words close enough to the name to pass a fuzzy check, measured
# against `_MIN_RATIO` and the looser standby `_SUMMON_RATIO`. "lemon" scores
# 0.75 and "lego" 0.86, so without this Leo answers to the shopping list.
# "Lee", "Leon" and "Lou" are here because they are other people.
_NEVER_A_NAME = {
    "lea", "leah", "leave", "led", "lee", "leg", "lego", "lei", "lemon", "leon", "les", "let",
    "lets", "leto", "level", "lever", "lid", "lie", "lieu", "lion", "lip", "lit", "liu", "lo",
    "lob", "log", "loo", "lot", "lou", "low", "lew", "lay", "law",
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
    length, so "video" and "neo" never pass.
    """
    if candidate in forms:
        return 1.0
    if candidate in _NEVER_A_NAME:
        return 0.0
    best = 0.0
    for form in forms:
        # Length guard: a long word should never fuzzy-match a short name.
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

    A bare wake phrase ("Leo?") matches with an empty command, which the core
    loop reads as "acknowledge, then listen for the real request".
    """
    if not config.WAKE_REQUIRED:
        return WakeMatch(True, transcript.strip(), 1.0)
    return _find(transcript)


def _find(transcript: str) -> WakeMatch:
    """The name at the front or the back of the utterance, regardless of config."""
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

    # Widest first: "L.E.O." tokenises to three words, and matching only the
    # first two of them would leave a stray "O." on the front of the command.
    for width in (3, 2, 1):
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

    # Trailing address: "open Chrome, Leo"
    if len(tokens) > 1:
        for width in (3, 2, 1):
            if len(tokens) <= width:
                continue
            candidate = " ".join(token[0] for token in tokens[-width:])
            if _similar(candidate, forms) >= _MIN_RATIO:
                cut = tokens[-width][1]
                command = transcript[:cut].strip().rstrip(",.:;!?-").strip()
                return WakeMatch(True, command, 0.8)

    return WakeMatch(False, "")


def heard_something_like_a_name(transcript: str) -> bool:
    """Did this probably address Leo without quite matching?

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


# Looser than `_MIN_RATIO`, and only ever used in standby - see `summons`.
_SUMMON_RATIO = 0.6


def summons(transcript: str) -> WakeMatch:
    """Was Leo called by name at all? The standby test, and a loose one.

    `detect` insists on the name at the front or the back, because outside
    standby a false wake means acting on somebody else's sentence. In standby
    that risk is gone: nothing is acted on except coming back, and the
    utterance has already been capped at `STANDBY_MAX_UTTERANCE_S`. What is
    left is the opposite failure - "Leo?", "Lio", "hey Leo you there" mumbled
    across a desk and ignored, so the user cannot get their assistant back
    without remembering the exact words "wake up".

    So here the name may sit anywhere, and a near-miss counts. The command is
    returned only when the strict match found one: a near-miss is a summons,
    not a reliable place to cut a sentence. `config.WAKE_REQUIRED` is ignored
    on purpose - with it off, `detect` matches everything, and a standby that
    any sound ends is not a standby.
    """
    strict = _find(transcript)
    if strict.matched:
        return strict
    tokens = _tokens(transcript)
    forms = _wake_forms()
    for index, (token, _, _) in enumerate(tokens):
        if token in _MERGED:
            return WakeMatch(True, "", 0.8)
        if _similar(token, forms) >= _SUMMON_RATIO:
            return WakeMatch(True, "", 0.6)
        # The initials transcribed apart from each other, mid-sentence.
        if index + 1 < len(tokens):
            pair = f"{token} {tokens[index + 1][0]}"
            if pair in forms:
                return WakeMatch(True, "", 0.9)
    return WakeMatch(False, "")
