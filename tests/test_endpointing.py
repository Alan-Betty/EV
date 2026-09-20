"""When E.V. decides the user has finished talking.

This is the largest fixed cost in the whole listen -> answer -> speak path
and the only one that is entirely E.V.'s to spend: the user has stopped, the
network is idle, and nothing is happening. It used to be a flat second.

It is now two waits, and the split is about *when* people pause rather than
how long for. The pause that must not be cut into is the one right at the
start - "E.V." and then a beat while they decide what they want. A pause a
second into a sentence is much rarer, and by then there is a real utterance
in the buffer. So the tests below are really one test asked twice: a short
burst keeps the generous wait, and a sentence ends promptly.

`Microphone` is driven through its own frame queue rather than a real device,
which is what `listen` reads from anyway - the reader thread is the only part
that ever touches PortAudio.
"""

import os
import queue
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ["EV_TTS_ENABLED"] = "false"

import config  # noqa: E402
from ev.audio import Microphone  # noqa: E402


def _frames(mic: Microphone, speech_ms: int, silence_ms: int) -> None:
    """Queue `speech_ms` of loud audio followed by `silence_ms` of quiet."""
    loud = struct.pack("<h", 8000) * mic.frame_samples
    quiet = struct.pack("<h", 2) * mic.frame_samples
    for _ in range(speech_ms // config.FRAME_MS):
        mic._queue.put((loud, 0.30))
    for _ in range(silence_ms // config.FRAME_MS):
        mic._queue.put((quiet, 0.0001))


def _microphone() -> Microphone:
    mic = Microphone.__new__(Microphone)
    mic.sample_rate = config.SAMPLE_RATE
    mic.frame_samples = int(mic.sample_rate * config.FRAME_MS / 1000)
    mic.frame_bytes = mic.frame_samples * 2
    mic.noise_floor = 0.01
    mic._queue = queue.Queue()
    mic._stream = object()  # listen() only checks that a device is open
    # `listen` flushes the queue first so a command starts from live audio,
    # which would throw away everything a test queued. This is the same
    # one-shot suppression a barge-in uses to keep the audio that triggered it.
    mic._hold = True
    mic._reading = True
    mic._recent_speech = 0
    mic._recent_peak = 0.0
    mic._muted = False
    return mic


def _frames_consumed(mic: Microphone, speech_ms: int, silence_ms: int) -> int:
    """How much of the queued silence `listen` used before returning."""
    _frames(mic, speech_ms, silence_ms)
    queued = silence_ms // config.FRAME_MS
    utterance = mic.listen(max_wait_s=5.0)
    assert utterance is not None, "speech that loud should always trigger"
    return queued - mic._queue.qsize()


def test_a_whole_sentence_ends_on_the_short_wait():
    """The common case, and the one the user notices every single time."""
    mic = _microphone()
    used = _frames_consumed(mic, speech_ms=1500, silence_ms=2000)
    used_ms = used * config.FRAME_MS
    assert used_ms < config.SILENCE_HANG_LONG_MS, (
        f"waited {used_ms}ms after a full sentence; the short wait is "
        f"{config.SILENCE_HANG_MS}ms"
    )
    assert used_ms >= config.SILENCE_HANG_MS - config.FRAME_MS


def test_a_single_word_keeps_the_generous_wait():
    """"E.V." followed by a beat while they decide what they want.

    Cutting in here costs the whole utterance, so this is the case the long
    wait exists for.
    """
    mic = _microphone()
    used = _frames_consumed(mic, speech_ms=300, silence_ms=2000)
    used_ms = used * config.FRAME_MS
    assert used_ms >= config.SILENCE_HANG_LONG_MS - config.FRAME_MS, (
        f"only waited {used_ms}ms after a single word"
    )


def test_a_pause_inside_a_sentence_does_not_end_it():
    """Silence shorter than the wait is part of the utterance, not the end."""
    mic = _microphone()
    _frames(mic, speech_ms=800, silence_ms=300)
    _frames(mic, speech_ms=800, silence_ms=1500)
    utterance = mic.listen(max_wait_s=5.0)
    assert utterance is not None
    # Both halves and the gap between them, so nothing was cut in two.
    assert utterance.duration_s > 1.6, utterance.duration_s


def test_the_old_flat_behaviour_is_still_reachable(monkeypatch):
    """Setting both waits the same restores exactly what was there before."""
    monkeypatch.setattr(config, "SILENCE_HANG_MS", 1000)
    monkeypatch.setattr(config, "SILENCE_HANG_LONG_MS", 1000)
    mic = _microphone()
    used = _frames_consumed(mic, speech_ms=1500, silence_ms=2000)
    assert used * config.FRAME_MS >= 1000 - config.FRAME_MS


def test_the_short_wait_is_never_longer_than_the_long_one(monkeypatch):
    """A misconfigured pair must not make E.V. wait less when it is unsure."""
    monkeypatch.setattr(config, "SILENCE_HANG_MS", 900)
    monkeypatch.setattr(config, "SILENCE_HANG_LONG_MS", 300)
    mic = _microphone()
    used = _frames_consumed(mic, speech_ms=300, silence_ms=2000)
    assert used * config.FRAME_MS >= 900 - config.FRAME_MS
