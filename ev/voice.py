"""E.V.'s ear for one person, and for its own voice coming back.

On loudspeakers the microphone hears E.V. as clearly as it hears the user,
and a barge-in rule that only asks "is somebody talking?" answers yes to
E.V.'s own reply. It then cut itself off, kept the audio that "interrupted"
it, transcribed its own words and answered them - E.V. talking to itself in a
loop that never finished a sentence.

Two things here tell the user apart from the echo, and they fail in
different ways, which is why both exist:

* **A voiceprint.** `VoiceProfile` keeps two running models of what the
  microphone hears: the user's voice, learned from every utterance that was
  addressed to E.V., and E.V.'s own voice *as it arrives through the
  speakers*, learned from every reply that played to the end uninterrupted.
  Each is a diagonal Gaussian over twelve cepstral coefficients - the shape
  of the spectrum, with loudness factored out - so it learns a voice and a
  room, not a volume. It starts with no opinion, and an untrained profile
  never vetoes anything; it adapts slowly and forever after
  (`VOICE_ADAPT_FRAMES`), so a cold, a new microphone or a moved laptop are
  followed rather than fought.

  This is a statistical voiceprint, not a neural speaker model, and it is
  not claimed as one. It is good at the question it is asked - "the user, or
  that synthetic voice through that speaker?" - and not good enough to tell
  the user from another person in the room, which is why nothing uses it for
  that.

* **The words.** `is_self_echo` compares a transcript with what E.V. said a
  moment ago. However the audio got in, a transcript that is E.V.'s own
  sentence is E.V.'s own sentence, and dropping it is what makes the loop
  impossible rather than merely unlikely.

No new dependency: numpy is already required, and it is imported lazily so a
text-mode session never pays for it. The profile lives in
`STATE_DIR/voice.json`, written atomically like the rest of E.V.'s state.
"""

from __future__ import annotations

import difflib
import logging
import math
import re
import time
from pathlib import Path
from typing import Any

import config
from ev.memory import read_json, write_json

log = logging.getLogger("ev.voice")

_VERSION = 1
N_BANDS = 24
N_CEPS = 12
_LOW_HZ = 100.0
_HIGH_HZ = 7000.0
_FRAME_S = 0.03
# Below this a variance is noise in the estimate, not in the voice, and
# dividing by it would let one coefficient decide the whole score.
_VAR_FLOOR = 0.05

_filterbanks: dict[tuple[int, int], Any] = {}
_dct_matrix: Any = None


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------
def _mel(hz: float) -> float:
    return 2595.0 * math.log10(1.0 + hz / 700.0)


def _filterbank(rate: int, n_fft: int):
    """Triangular mel filters over the rfft bins, cached per (rate, size)."""
    import numpy as np

    key = (rate, n_fft)
    cached = _filterbanks.get(key)
    if cached is not None:
        return cached
    high = min(_HIGH_HZ, rate / 2 - 1)
    mels = np.linspace(_mel(_LOW_HZ), _mel(high), N_BANDS + 2)
    hz = 700.0 * (10 ** (mels / 2595.0) - 1.0)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / rate)
    bank = np.zeros((N_BANDS, freqs.size), dtype=np.float32)
    for band in range(N_BANDS):
        left, centre, right = hz[band], hz[band + 1], hz[band + 2]
        rising = (freqs - left) / max(centre - left, 1e-6)
        falling = (right - freqs) / max(right - centre, 1e-6)
        bank[band] = np.clip(np.minimum(rising, falling), 0.0, None)
    _filterbanks[key] = bank
    return bank


def _dct():
    """Orthonormal DCT-II rows 1..N_CEPS.

    Row 0 is skipped on purpose: it is the overall loudness, and a gain
    applied to the whole signal lands there and nowhere else - so leaving it
    out is what makes the voiceprint indifferent to how loud anyone was.
    """
    global _dct_matrix
    if _dct_matrix is None:
        import numpy as np

        n = np.arange(N_BANDS)
        rows = [
            math.sqrt(2.0 / N_BANDS) * np.cos(math.pi * k * (2 * n + 1) / (2 * N_BANDS))
            for k in range(1, N_CEPS + 1)
        ]
        _dct_matrix = np.array(rows, dtype=np.float32)
    return _dct_matrix


def _frames(pcm: bytes, rate: int):
    """Split int16 PCM into fixed frames, with each frame's RMS (0-1)."""
    import numpy as np

    size = max(64, int((int(rate) or config.SAMPLE_RATE) * _FRAME_S))
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2")
    count = samples.size // size
    frames = samples[: count * size].reshape(count, size).astype(np.float32) / 32768.0
    rms = np.sqrt(np.mean(frames * frames, axis=1)) if count else np.zeros(0)
    return frames, rms, size


def features(pcm: bytes, rate: int, min_level: float = 0.0):
    """Cepstral features of the voiced frames in a block of int16 PCM.

    Returns an (n, N_CEPS) array; n is zero when nothing was loud enough to
    be speech. Frames at or below `min_level` (RMS, 0-1) are dropped, because
    a model of a voice trained on the silence between words is a model of the
    room.
    """
    import numpy as np

    rate = int(rate) or config.SAMPLE_RATE
    frames, rms, size = _frames(pcm, rate)
    frames = frames[rms > min_level]
    if frames.shape[0] == 0:
        return np.zeros((0, N_CEPS), dtype=np.float32)
    n_fft = 1 << (size - 1).bit_length()
    spectrum = np.abs(np.fft.rfft(frames * np.hamming(size), n_fft)) ** 2
    bands = spectrum @ _filterbank(rate, n_fft).T
    return (np.log(bands + 1e-10) @ _dct().T).astype(np.float32)


# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------
class Gaussian:
    """A diagonal Gaussian that learns exactly at first and slowly after.

    The weight of each new batch is its share of everything seen so far,
    until that share falls below `1 / VOICE_ADAPT_FRAMES` - from then on it
    is an exponential average. So the first minute of speech is averaged
    properly rather than letting the first sentence dominate, and the model
    never freezes: an old voice fades out over roughly `VOICE_ADAPT_FRAMES`
    frames of the new one.
    """

    def __init__(self, mean=None, var=None, frames: int = 0) -> None:
        self.mean = mean
        self.var = var
        self.frames = int(frames)

    def update(self, x) -> None:
        import numpy as np

        n = int(x.shape[0])
        if n == 0:
            return
        batch_mean = x.mean(axis=0)
        batch_var = x.var(axis=0)
        self.frames += n
        if self.mean is None:
            self.mean, self.var = batch_mean, np.maximum(batch_var, _VAR_FLOOR)
            return
        window = max(1, config.VOICE_ADAPT_FRAMES)
        weight = min(1.0, max(n / self.frames, n / window))
        mean = (1 - weight) * self.mean + weight * batch_mean
        var = (1 - weight) * (self.var + (self.mean - mean) ** 2) + weight * (
            batch_var + (batch_mean - mean) ** 2
        )
        self.mean, self.var = mean, np.maximum(var, _VAR_FLOOR)

    def loglik(self, x):
        """Per-frame log-likelihood."""
        import numpy as np

        diff = x - self.mean
        return -0.5 * np.sum(np.log(2 * math.pi * self.var) + diff * diff / self.var, axis=1)

    def to_json(self) -> dict[str, Any]:
        if self.mean is None:
            return {"frames": self.frames}
        return {
            "frames": self.frames,
            "mean": [round(float(v), 5) for v in self.mean],
            "var": [round(float(v), 5) for v in self.var],
        }

    @classmethod
    def from_json(cls, data: Any) -> "Gaussian":
        if not isinstance(data, dict):
            return cls()
        mean, var = data.get("mean"), data.get("var")
        if not (
            isinstance(mean, list)
            and isinstance(var, list)
            and len(mean) == len(var) == N_CEPS
        ):
            # No usable statistics means nothing learned, whatever the count
            # claims - a profile that thinks it is trained with no mean to
            # compare against would crash on its first score.
            return cls()
        import numpy as np

        return cls(
            np.array(mean, dtype=np.float32),
            np.maximum(np.array(var, dtype=np.float32), _VAR_FLOOR),
            int(data.get("frames", 0) or 0),
        )


class VoiceProfile:
    """What the user sounds like, and what E.V. sounds like through the speakers.

    Also two loudness figures, both measured at the microphone: how loud the
    user usually is, and how loud E.V.'s own echo usually is. The second is
    what barge-in compares against before the current reply has played long
    enough to measure its own.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or config.VOICE_FILE)
        self._reset()
        self._dirty = False
        self._saved_at = time.monotonic()
        self._load()

    def _reset(self) -> None:
        self.user = Gaussian()
        self.echo = Gaussian()
        self.user_level = 0.0
        self.echo_level = 0.0
        self.utterances = 0

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        data = read_json(self.path, {})
        if data.get("version") != _VERSION:
            return
        try:
            self.user = Gaussian.from_json(data.get("user"))
            self.echo = Gaussian.from_json(data.get("echo"))
            self.user_level = float(data.get("user_level", 0.0) or 0.0)
            self.echo_level = float(data.get("echo_level", 0.0) or 0.0)
            self.utterances = int(data.get("utterances", 0) or 0)
        except (TypeError, ValueError) as exc:
            # A damaged profile costs what was learned, never the session.
            log.warning("Voice profile unreadable, starting fresh: %s", exc)
            self._reset()

    def save(self) -> bool:
        if not self._dirty:
            return True
        ok = write_json(
            self.path,
            {
                "version": _VERSION,
                "user": self.user.to_json(),
                "echo": self.echo.to_json(),
                "user_level": round(self.user_level, 5),
                "echo_level": round(self.echo_level, 5),
                "utterances": self.utterances,
                "updated": time.time(),
            },
        )
        if ok:
            self._dirty = False
            self._saved_at = time.monotonic()
        return ok

    def _maybe_save(self) -> None:
        # Throttled: a JSON write per utterance is cheap but pointless, and
        # `EV.stop()` saves whatever is left.
        if time.monotonic() - self._saved_at >= config.VOICE_SAVE_INTERVAL_S:
            self.save()

    # -- learning ---------------------------------------------------------
    @property
    def trained(self) -> bool:
        floor = config.VOICE_MIN_FRAMES
        return self.user.frames >= floor and self.echo.frames >= floor

    def learn_user(self, pcm: bytes, rate: int, min_level: float) -> int:
        """Fold one addressed utterance into the user's model."""
        x = features(pcm, rate, min_level)
        if x.shape[0] < 3:
            return 0
        self.user.update(x)
        level = _median_level(pcm, rate, min_level)
        if level > 0:
            self.user_level = _blend(self.user_level, level, self.utterances)
        self.utterances += 1
        self._dirty = True
        self._maybe_save()
        return int(x.shape[0])

    def learn_echo(self, pcm: bytes, rate: int, min_level: float) -> int:
        """Fold audio known to be E.V.'s own voice into the echo model."""
        x = features(pcm, rate, min_level)
        if x.shape[0] < 3:
            return 0
        self.echo.update(x)
        self._dirty = True
        self._maybe_save()
        return int(x.shape[0])

    def note_echo_level(self, level: float) -> None:
        """How loud E.V. was at the microphone during one whole reply."""
        if level <= 0:
            return
        # Faster than the voiceprint: a volume change is a fact about now.
        # The reply-by-reply measurement overrides this anyway once a reply
        # has played for a second.
        self.echo_level = (
            level if self.echo_level <= 0 else 0.7 * self.echo_level + 0.3 * level
        )
        self._dirty = True

    # -- judging ----------------------------------------------------------
    def score(self, pcm: bytes, rate: int, min_level: float) -> float | None:
        """Mean log-likelihood ratio per frame, user over echo.

        Positive sounds like the user, negative like E.V. through the
        speakers. None means "no opinion": the profile is not trained yet,
        or there were too few voiced frames to judge. Callers must treat None
        as permission, never as a veto - an untrained ear refusing every
        interruption would be worse than the bug it exists to fix.
        """
        if not self.trained:
            return None
        x = features(pcm, rate, min_level)
        if x.shape[0] < 3:
            return None
        ratio = self.user.loglik(x) - self.echo.loglik(x)
        return float(ratio.mean())

    def describe(self) -> str:
        user_s = self.user.frames * _FRAME_S
        echo_s = self.echo.frames * _FRAME_S
        state = "trained" if self.trained else "still learning"
        return (
            f"{state} - {user_s:.0f}s of your voice over {self.utterances} "
            f"utterances, {echo_s:.0f}s of its own echo"
        )


def _median_level(pcm: bytes, rate: int, min_level: float) -> float:
    import numpy as np

    _, rms, _ = _frames(pcm, rate)
    voiced = rms[rms > min_level]
    return float(np.median(voiced)) if voiced.size else 0.0


def _blend(old: float, new: float, seen: int) -> float:
    if old <= 0 or seen <= 0:
        return new
    weight = max(1.0 / (seen + 1), 0.05)
    return (1 - weight) * old + weight * new


_profile: VoiceProfile | None = None


def get_voice_profile() -> VoiceProfile:
    """The process-wide profile, rebuilt when `config.VOICE_FILE` moves."""
    global _profile
    if _profile is None or _profile.path != Path(config.VOICE_FILE):
        _profile = VoiceProfile()
    return _profile


# ---------------------------------------------------------------------------
# the words
# ---------------------------------------------------------------------------
_WORD = re.compile(r"[a-z0-9']+")


def _words(text: str) -> list[str]:
    return _WORD.findall(str(text).lower().replace("’", "'"))


def echo_overlap(heard: str, said: str) -> float:
    """Share of the heard words that appear, in order, in what E.V. said."""
    heard_words, said_words = _words(heard), _words(said)
    if not heard_words or not said_words:
        return 0.0
    matcher = difflib.SequenceMatcher(None, heard_words, said_words, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(heard_words)


def is_self_echo(heard: str, said: str) -> bool:
    """Is this transcript E.V.'s own recent speech, heard back?

    Short transcripts are never judged. "Yes" after "Confirm?" and "open
    notepad" after "want me to open notepad?" are the user answering, and
    the words alone cannot tell those from an echo - while three or more
    words lifted in order from E.V.'s last sentence almost never are.
    """
    if len(_words(heard)) < config.SELF_ECHO_MIN_WORDS:
        return False
    return echo_overlap(heard, said) >= config.SELF_ECHO_MATCH
