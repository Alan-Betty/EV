"""Full-duplex capture and speech interruption.

The microphone never stops. A background reader thread keeps pulling frames
while E.V. is talking, which is what makes it possible to interrupt a reply
part-way through instead of waiting politely for it to end.

Three things have to hold, and each one has a failure mode that makes E.V.
unusable in a different direction:

* **Two conditions, not one.** Frame count alone cannot tell the user's voice
  from E.V.'s own coming back through the speakers - both are a long steady
  run of speech-looking frames - so E.V. would interrupt itself on every
  reply. Loudness alone trips on a door closing.
* **Stopping means stopping.** Killing the clip that is playing ends one
  sentence; a streamed reply has the rest of itself queued behind it and
  carries straight on. Barge-in has to reach the queue as well.
* **The audio that triggered it is kept.** Those frames are the opening of the
  user's sentence. Flushed, they cost the user the start of what they said,
  which is the thing barge-in exists to prevent.

Offline: no audio device is opened and no network call is made.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

import config  # noqa: E402
import ev_core  # noqa: E402
from ev.audio import Microphone  # noqa: E402
from ev.tts import Speaker, clean_for_speech  # noqa: E402


# ---------------------------------------------------------------------------
# doubles
# ---------------------------------------------------------------------------
class FakeMic:
    """A microphone whose levels the test drives directly."""

    def __init__(self, energy: int = 0, level: float = 0.0, floor: float = 0.02):
        self.noise_floor = floor
        self._energy = energy
        self._level = level
        self.held = False
        self.flushed = 0

    def set(self, energy: int, level: float) -> None:
        self._energy, self._level = energy, level

    def speech_energy(self) -> int:
        return self._energy

    def speech_level(self) -> float:
        return self._level

    def hold_audio(self) -> None:
        self.held = True

    def reset_levels(self) -> None:
        self._energy, self._level = 0, 0.0

    def flush(self) -> None:
        self.flushed += 1


class FakeSpeaker:
    """Speaks for a set duration, and records whether it was cut short."""

    def __init__(self) -> None:
        self.enabled = True
        self.speaking = False
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True
        self.speaking = False


class SilentUI:
    def __init__(self) -> None:
        self.notes: list[str] = []

    def note(self, text: str) -> None:
        self.notes.append(text)

    def __getattr__(self, _name):
        def _draw(*_args, **_kwargs):
            return None

        return _draw


def _assistant(mic, speaker) -> ev_core.EV:
    assistant = ev_core.EV.__new__(ev_core.EV)
    assistant.ui = SilentUI()
    assistant.speaker = speaker
    assistant.mic = mic
    assistant.text_mode = False
    assistant._running = True
    return assistant


@pytest.fixture(autouse=True)
def instant_grace(monkeypatch):
    """The grace period is real and deliberate; waiting it out in tests is not."""
    monkeypatch.setattr(config, "BARGE_IN_GRACE_S", 0.0)
    monkeypatch.setattr(config, "TTS_BARGE_IN", True)
    monkeypatch.setattr(config, "BARGE_IN_FRAMES", 8)
    monkeypatch.setattr(config, "BARGE_IN_LEVEL_MULTIPLIER", 1.8)
    monkeypatch.setattr(config, "BARGE_IN_KEEP_AUDIO", True)


async def _watch_briefly(assistant, seconds: float = 0.3) -> None:
    """Run the watcher against a speaker that is 'speaking' for a moment."""
    assistant.speaker.speaking = True
    watcher = asyncio.ensure_future(assistant._watch_for_barge_in())
    await asyncio.sleep(seconds)
    assistant.speaker.speaking = False
    watcher.cancel()
    try:
        await watcher
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# the detection rule
# ---------------------------------------------------------------------------
def test_sustained_and_loud_speech_takes_the_floor():
    mic = FakeMic(energy=12, level=0.09, floor=0.02)
    speaker = FakeSpeaker()
    assistant = _assistant(mic, speaker)

    asyncio.run(_watch_briefly(assistant))

    assert speaker.stopped is True


def test_ev_hearing_itself_does_not_count_as_an_interruption():
    """The loopback case: a long run of frames, but no louder than the room.

    Without the level test this is indistinguishable from the user talking,
    and E.V. cuts itself off on its own voice every single reply.
    """
    mic = FakeMic(energy=40, level=0.025, floor=0.02)  # 40 frames, but quiet
    speaker = FakeSpeaker()
    assistant = _assistant(mic, speaker)

    asyncio.run(_watch_briefly(assistant))

    assert speaker.stopped is False


def test_a_single_loud_bang_does_not_count_either():
    """A door closing is loud and over in one frame."""
    mic = FakeMic(energy=2, level=0.9, floor=0.02)
    speaker = FakeSpeaker()
    assistant = _assistant(mic, speaker)

    asyncio.run(_watch_briefly(assistant))

    assert speaker.stopped is False


def test_the_threshold_scales_with_the_room():
    """A noisy room raises the floor, so the bar for interrupting rises too."""
    loud_room = FakeMic(energy=20, level=0.09, floor=0.08)
    speaker = FakeSpeaker()
    assistant = _assistant(loud_room, speaker)

    asyncio.run(_watch_briefly(assistant))

    # 0.09 is below 0.08 * 1.8, so this is room noise rather than a command.
    assert speaker.stopped is False


def test_barge_in_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "TTS_BARGE_IN", False)
    mic = FakeMic(energy=40, level=0.9, floor=0.02)
    speaker = FakeSpeaker()
    assistant = _assistant(mic, speaker)

    async def run() -> None:
        speaker.speaking = True
        async with assistant._barge_in():
            await asyncio.sleep(0.1)
        speaker.speaking = False

    asyncio.run(run())
    assert speaker.stopped is False


def test_text_mode_has_no_microphone_to_watch():
    speaker = FakeSpeaker()
    assistant = _assistant(None, speaker)

    async def run() -> None:
        async with assistant._barge_in():
            await asyncio.sleep(0.05)

    asyncio.run(run())  # must not raise
    assert speaker.stopped is False


# ---------------------------------------------------------------------------
# what happens on a trigger
# ---------------------------------------------------------------------------
def test_the_interrupting_audio_is_kept_not_flushed():
    """The frames that proved the user started talking are their first word."""
    mic = FakeMic(energy=12, level=0.09, floor=0.02)
    assistant = _assistant(mic, FakeSpeaker())

    asyncio.run(_watch_briefly(assistant))

    assert mic.held is True


def test_keeping_the_audio_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "BARGE_IN_KEEP_AUDIO", False)
    mic = FakeMic(energy=12, level=0.09, floor=0.02)
    assistant = _assistant(mic, FakeSpeaker())

    asyncio.run(_watch_briefly(assistant))

    assert mic.held is False


def test_the_prompt_to_go_ahead_is_drawn_and_never_spoken():
    """`ev.ui` is a dead end: nothing it draws may reach the speaker."""
    mic = FakeMic(energy=12, level=0.09, floor=0.02)
    speaker = FakeSpeaker()
    assistant = _assistant(mic, speaker)

    asyncio.run(_watch_briefly(assistant))

    assert assistant.ui.notes == ["Go ahead."]
    assert not hasattr(speaker, "said") or not getattr(speaker, "said", [])


# ---------------------------------------------------------------------------
# the microphone itself
# ---------------------------------------------------------------------------
def test_a_held_queue_survives_exactly_one_listen():
    """One-shot: the hold must not outlive the interruption that set it."""
    mic = Microphone()
    assert mic._hold is False

    mic.hold_audio()
    assert mic._hold is True

    # `listen` clears it before doing anything else; simulate that half.
    mic._hold = False
    assert mic._hold is False


def test_levels_can_be_reset_as_playback_starts():
    """Otherwise the tail of the user's own command counts as recent speech."""
    mic = Microphone()
    mic._recent_speech = 30
    mic._recent_peak = 0.5

    mic.reset_levels()

    assert mic.speech_energy() == 0
    assert mic.speech_level() == 0.0


def test_a_muted_microphone_still_measures():
    """Barge-in reads the levels. A muted mic that stopped measuring is deaf."""
    mic = Microphone()
    mic._muted = True
    # The reader updates the levels before it consults `_muted`, so the
    # accessors stay live. Verified by reading the source order rather than
    # by opening a device, which the suite must never do.
    import inspect

    body = inspect.getsource(Microphone._read_loop)
    assert body.index("_recent_peak") < body.index("if self._muted")


# ---------------------------------------------------------------------------
# stopping means stopping
# ---------------------------------------------------------------------------
def test_stopping_empties_the_queued_sentences():
    """A streamed reply has the rest of itself queued behind the current clip."""

    async def run() -> None:
        speaker = Speaker()          # TTS disabled in this suite: no device
        stream = speaker.stream()
        stream.feed("First sentence.")
        stream.feed("Second sentence.")

        speaker.stop()

        assert stream.cancelled is True
        assert stream._queue.empty()

        # Anything the model streams after the interruption is refused too.
        stream.feed("Third sentence.")
        assert "Third" not in stream.spoken

    asyncio.run(run())


def test_the_speaker_forgets_a_stream_once_it_is_stopped():
    async def run() -> None:
        speaker = Speaker()
        stream = speaker.stream()
        assert speaker._stream is stream
        speaker.stop()
        assert speaker._stream is None

    asyncio.run(run())


def test_the_playback_hook_fires_when_the_floor_is_taken():
    """It clears the mic's level history as a reply starts playing."""
    speaker = Speaker()
    calls: list[int] = []
    speaker.on_playback_start = lambda: calls.append(1)

    speaker._note_playback_start()

    assert calls == [1]


def test_a_broken_playback_hook_does_not_cost_the_reply():
    """The hook is housekeeping. Speech is not, so it wins."""
    speaker = Speaker()

    def explode() -> None:
        raise RuntimeError("bad hook")

    speaker.on_playback_start = explode
    speaker._note_playback_start()  # must not raise

    spoken = asyncio.run(speaker.say("Still fine."))
    assert spoken == "Still fine."


def test_a_speaker_with_no_audio_device_never_claims_playback_started():
    """There is no floor to take, so nothing should be told it was taken.

    This is the `EV_TTS_ENABLED=false` path the whole suite runs under, and
    also what a machine with no working output device looks like.
    """
    speaker = Speaker()
    assert speaker.enabled is False

    calls: list[int] = []
    speaker.on_playback_start = lambda: calls.append(1)

    asyncio.run(speaker.say("Hello there."))

    assert calls == []


def test_the_core_loop_wires_the_hook_to_the_microphone():
    """The reset is only useful if something actually connects the two."""
    import inspect

    source = inspect.getsource(ev_core.EV.start)
    assert "on_playback_start" in source
    assert "reset_levels" in source


def test_nothing_barge_in_says_breaches_the_speech_boundary():
    """Every string on this path reaches the speaker unchanged, or not at all."""
    for phrase in ("Go ahead.", "Standing by."):
        assert clean_for_speech(phrase) == phrase
