"""What E.V. has learned about how *this* user sounds to the recogniser.

There is no local speech model to fine-tune - the recogniser is Whisper over
HTTP, by design - so "adapting to the user's voice" cannot mean retraining
anything. It means changing the two inputs E.V. does control, and both are
learned from the user's own addressed speech:

* **The decoding prompt.** Whisper conditions on the text it is handed as
  "what came before", which is the strongest lever an API caller has. The
  static vocabulary and the installed-program list are guesses about what
  anyone might say; this is a running count of what *this* person actually
  says to E.V. - their project names, their friends, their jargon. The words
  that recur are the ones fed back, so a name mangled on Monday is spelled
  right by Wednesday.

  A repeat after a miss is the best evidence there is. When a transcript
  was rejected or doubtful and the user says it again inside
  `STT_RETRY_WINDOW_S`, the words that are new in the clear version are the
  ones the recogniser got wrong the first time, so they are weighted three
  times over. Doubtful transcripts never teach words themselves: learning
  "obese" from a mangled "OBS" would bias the recogniser towards its own
  mistake.

* **The confidence gate.** `STT_MIN_LOGPROB` and `STT_UNCERTAIN_LOGPROB` are
  numbers measured on a clear, unaccented speaker. Someone Whisper is
  consistently less sure of - an accent, a quiet voice, a cheap microphone -
  sits permanently near the line, and E.V. answers half of what they say
  with "Didn't catch that". So the profile tracks how Whisper scores this
  user's *accepted* speech and moves the thresholds to sit relative to that.
  It only ever loosens them, by at most `STT_ADAPT_MAX_SHIFT`: a clear
  speaker gains nothing from a stricter gate, and an unbounded drift would
  end up acting on noise.

Nothing here touches audio, and nothing here is a speaker model - see
`ev.voice` for the voiceprint. Persisted like every other bit of state, in
`STATE_DIR`, atomically, and a damaged file costs what was learned and never
the session.
"""

from __future__ import annotations

import logging
import math
import re
import time
from pathlib import Path

import config
from ev.memory import read_json, write_json

log = logging.getLogger("ev.hearing")

_VERSION = 1
_WORD = re.compile(r"[A-Za-z0-9][\w'+#.-]*")
# Weight of a word that was new in the clear repeat of a misheard sentence.
_RETRY_WEIGHT = 3.0
# Every learn decays every count a little, so a project dropped months ago
# stops taking room in a prompt that has none to spare.
_DECAY = 0.99
_FORGET_BELOW = 0.3
# A word has to recur before it is worth prompt space; one mention is chatter.
# Below 2 because decay has already taken a little off the first mention by
# the time the second arrives.
_MIN_COUNT = 1.5

# Words that carry no information about this user. Spending prompt space on
# "open" or "please" biases Whisper towards what it already gets right.
_COMMON = frozenset("""
a about above after again all also am an and any are around as at away back be
because been before being below between both but by can can't cant come could
did didn't do does doesn't doing don't done down each even ever every few find
for from get gets give go goes going gone got had has have having he her here
hers him his how i i'd i'll i'm i've if in into is isn't it it's its just keep
know last let let's like look lot made make many may me might mine more most
much must my need never new next no not now of off oh ok okay on once one only
open or other our out over please put quite rather really right said same say
see she should show so some something still such sure take tell than thank
thanks that that's the their them then there there's these they thing things
think this those though through to too try turn up us use very want was way we
well were what what's when where which while who why will with won't would yeah
yes yet you you're your yours start stop close run play search find hey hi
hello uh um hmm alright actually maybe also gonna wanna lets thats whats
""".split())


def _words(text: str) -> list[tuple[str, str]]:
    """(key, as written) for every word worth remembering in `text`."""
    found: list[tuple[str, str]] = []
    for match in _WORD.finditer(str(text or "")):
        written = match.group(0).rstrip(".'-")
        if written.lower().endswith("'s"):
            written = written[:-2]
        key = written.lower()
        if not key or key in _COMMON or key.isdigit():
            continue
        # Two letters is a word only when it was written as an acronym:
        # "VS", "PC". "to" and "is" are already gone; "ah" and "mm" are not.
        if len(key) < 3 and not any(ch.isupper() or ch.isdigit() for ch in written):
            continue
        found.append((key, written))
    return found


def _better_spelling(old: str, new: str) -> str:
    """Prefer the capitalised form: "GitHub" says more than "github"."""
    if any(ch.isupper() for ch in new) and not any(ch.isupper() for ch in old):
        return new
    return old if any(ch.isupper() for ch in old) else new


class HearingProfile:
    """Learned vocabulary and confidence statistics for one user."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or config.HEARING_FILE)
        self._reset()
        self._dirty = False
        self._saved_at = time.monotonic()
        self._doubt_at = -math.inf
        self._doubt_words: set[str] = set()
        self._load()

    def _reset(self) -> None:
        # key -> [as written, weight]
        self.words: dict[str, list] = {}
        self.scored = 0
        self.mean = 0.0
        self.var = 0.0
        self.utterances = 0

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        data = read_json(self.path, {})
        if data.get("version") != _VERSION:
            return
        try:
            words = data.get("words") or {}
            self.words = {
                str(key): [str(entry[0]), float(entry[1])]
                for key, entry in words.items()
                if isinstance(entry, (list, tuple)) and len(entry) == 2
            }
            stats = data.get("logprob") or {}
            self.scored = int(stats.get("n", 0) or 0)
            self.mean = float(stats.get("mean", 0.0) or 0.0)
            self.var = float(stats.get("var", 0.0) or 0.0)
            self.utterances = int(data.get("utterances", 0) or 0)
        except (TypeError, ValueError, AttributeError) as exc:
            log.warning("Hearing profile unreadable, starting fresh: %s", exc)
            self._reset()

    def save(self) -> bool:
        if not self._dirty:
            return True
        ok = write_json(
            self.path,
            {
                "version": _VERSION,
                "words": {
                    key: [text, round(weight, 3)]
                    for key, (text, weight) in self.words.items()
                },
                "logprob": {
                    "n": self.scored,
                    "mean": round(self.mean, 5),
                    "var": round(self.var, 6),
                },
                "utterances": self.utterances,
                "updated": time.time(),
            },
        )
        if ok:
            self._dirty = False
            self._saved_at = time.monotonic()
        return ok

    def _maybe_save(self) -> None:
        # Same throttle as the voiceprint; `EV.stop()` saves the remainder.
        if time.monotonic() - self._saved_at >= config.VOICE_SAVE_INTERVAL_S:
            self.save()

    # -- learning ---------------------------------------------------------
    def learn(self, text: str, *, rejected: bool, uncertain: bool,
              avg_logprob: float | None, now: float | None = None) -> None:
        """Fold one *addressed* transcript in. The caller has checked that."""
        now = time.monotonic() if now is None else now
        heard = _words(text)

        if rejected or uncertain:
            # Remembered as the thing a repeat would be correcting.
            self._doubt_at = now
            self._doubt_words = {key for key, _ in heard}
        if rejected:
            return

        if avg_logprob is not None:
            self._fold_score(avg_logprob)
        self.utterances += 1
        self._dirty = True

        if uncertain:
            # Acted on, but its words may be wrong; never learn from them.
            self._maybe_save()
            return

        retry = now - self._doubt_at <= config.STT_RETRY_WINDOW_S
        for entry in self.words.values():
            entry[1] *= _DECAY
        for key, written in heard:
            weight = _RETRY_WEIGHT if retry and key not in self._doubt_words else 1.0
            entry = self.words.get(key)
            if entry is None:
                self.words[key] = [written, weight]
            else:
                entry[0] = _better_spelling(entry[0], written)
                entry[1] += weight
        if retry:
            self._doubt_at = -math.inf
            self._doubt_words = set()
        self._prune()
        self._maybe_save()

    def _fold_score(self, value: float) -> None:
        """Running mean and variance: exact at first, then an EMA."""
        self.scored += 1
        weight = max(1.0 / self.scored, 1.0 / max(1, config.STT_ADAPT_WINDOW))
        delta = value - self.mean
        self.mean += weight * delta
        self.var = (1 - weight) * (self.var + weight * delta * delta)

    def _prune(self) -> None:
        for key in [k for k, (_, w) in self.words.items() if w < _FORGET_BELOW]:
            del self.words[key]
        limit = max(1, config.STT_LEARN_MAX_WORDS)
        if len(self.words) > limit:
            keep = sorted(self.words.items(), key=lambda kv: kv[1][1], reverse=True)[:limit]
            self.words = dict(keep)

    # -- reading ----------------------------------------------------------
    def vocabulary(self) -> list[str]:
        """The user's own recurring words, most used first."""
        ranked = sorted(
            (entry for entry in self.words.values() if entry[1] >= _MIN_COUNT),
            key=lambda entry: entry[1],
            reverse=True,
        )
        return [text for text, _ in ranked]

    @property
    def adapted(self) -> bool:
        return config.STT_ADAPTIVE_CONFIDENCE and self.scored >= config.STT_ADAPT_MIN

    def thresholds(self) -> tuple[float, float]:
        """(reject below, flag as uncertain below), for this user.

        Each sits a fixed number of standard deviations under the user's
        typical score, but never above the configured value - this only
        loosens - and never more than `STT_ADAPT_MAX_SHIFT` below it.
        """
        reject, doubt = config.STT_MIN_LOGPROB, config.STT_UNCERTAIN_LOGPROB
        if not self.adapted:
            return reject, doubt
        spread = math.sqrt(max(self.var, 0.0025))  # at least 0.05
        shift = abs(config.STT_ADAPT_MAX_SHIFT)
        reject = max(reject - shift, min(reject, self.mean - 3.0 * spread))
        doubt = max(doubt - shift, min(doubt, self.mean - 1.5 * spread))
        return reject, max(doubt, reject)

    def describe(self) -> str:
        reject, doubt = self.thresholds()
        gate = (
            f"gate adapted to reject < {reject:.2f}, doubt < {doubt:.2f}"
            if self.adapted
            else f"gate at defaults until {config.STT_ADAPT_MIN} scored utterances"
        )
        return (
            f"{len(self.vocabulary())} learned words from {self.utterances} "
            f"utterances; {gate}"
        )


_profile: HearingProfile | None = None


def get_hearing() -> HearingProfile:
    """The process-wide profile, rebuilt when `config.HEARING_FILE` moves."""
    global _profile
    if _profile is None or _profile.path != Path(config.HEARING_FILE):
        _profile = HearingProfile()
    return _profile
