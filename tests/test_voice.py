"""E.V. hearing itself, and learning who it is listening to.

On loudspeakers E.V. barged in on its own reply, kept the audio, transcribed
its own words and answered them - a loop that never finished a sentence.
Three independent fixes, each pinned here:

* **Barge-in is measured against E.V.'s echo, not the room.** The reference
  is how loud E.V. was a moment ago, and the grace period is timed from the
  first sound rather than from the reply being queued.
* **A learned voiceprint** tells the user from E.V.-through-the-speakers, and
  has no vote until it has heard enough of both.
* **A transcript of E.V.'s own words is never answered,** however it got in.

Offline: no audio device is opened and no network call is made. The voices
are synthetic - two harmonic sources with different spectral shapes - which
is enough to check the model separates what it was taught to separate.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import numpy as np  # noqa: E402
import pytest  # noqa: E402

import config  # noqa: E402
import ev_core  # noqa: E402
from ev import voice  # noqa: E402
from ev.audio import Microphone, Utterance  # noqa: E402
from ev.session import Session  # noqa: E402
from ev.stt import Transcript  # noqa: E402
from ev.tts import Speaker  # noqa: E402

RATE = 16000


# ---------------------------------------------------------------------------
# synthetic voices
# ---------------------------------------------------------------------------
def _voice(
    fundamental: float, tilt: float, seconds: float = 3.0, gain: float = 0.3, seed: int = 0
) -> bytes:
    """A voiced source: harmonics of `fundamental`, rolled off by `tilt` per harmonic."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(RATE * seconds)) / RATE
    wobble = 1 + 0.03 * np.sin(2 * np.pi * 3 * t)
    signal = np.zeros_like(t)
    for k in range(1, 30):
        freq = fundamental * k
        if freq > RATE / 2 - 200:
            break
        signal += (tilt**k) * np.sin(2 * np.pi * freq * wobble * t + rng.uniform(0, 6.28))
    signal += 0.02 * rng.standard_normal(t.size)
    signal = gain * signal / np.max(np.abs(signal))
    return (signal * 32767).astype("<i2").tobytes()


def user_voice(seed: int = 0, **kw) -> bytes:
    return _voice(110.0, 0.92, seed=seed, **kw)


def echo_voice(seed: int = 0, **kw) -> bytes:
    return _voice(210.0, 0.55, seed=seed, **kw)


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VOICE_FILE", tmp_path / "voice.json")
    monkeypatch.setattr(config, "VOICE_MIN_FRAMES", 50)
    return voice.VoiceProfile()


# ---------------------------------------------------------------------------
# features and the model
# ---------------------------------------------------------------------------
def test_the_voiceprint_ignores_how_loud_anyone_was():
    """Loudness lives in the coefficient that is thrown away."""
    loud = voice.features(user_voice(gain=0.5), RATE)
    quiet = voice.features(user_voice(gain=0.1), RATE)
    assert loud.shape == quiet.shape
    # Not exact: a fifth of the gain is a fifth of the int16 resolution, and
    # the quietest bands feel it. Hundredths, against coefficients of ~2.
    assert np.abs(loud - quiet).max() < 0.05


def test_silence_is_not_learned_as_a_voice():
    silence = bytes(RATE * 2)
    assert voice.features(silence, RATE, min_level=0.01).shape[0] == 0


def test_an_untrained_profile_has_no_opinion(profile):
    """None is permission. An untrained ear must never veto an interruption."""
    assert profile.score(user_voice(), RATE, 0.01) is None
    profile.learn_user(user_voice(), RATE, 0.01)
    assert not profile.trained
    assert profile.score(user_voice(), RATE, 0.01) is None


def test_a_trained_profile_tells_the_user_from_the_echo(profile):
    for seed in range(3):
        profile.learn_user(user_voice(seed), RATE, 0.01)
        profile.learn_echo(echo_voice(seed), RATE, 0.01)
    assert profile.trained

    assert profile.score(user_voice(seed=9), RATE, 0.01) > 2.0
    assert profile.score(echo_voice(seed=9), RATE, 0.01) < -2.0


def test_the_profile_adapts_to_a_voice_that_changes(profile, monkeypatch):
    """Slowly and forever: an old voice fades out, it is not frozen in."""
    monkeypatch.setattr(config, "VOICE_ADAPT_FRAMES", 200)
    for seed in range(4):
        profile.learn_user(user_voice(seed), RATE, 0.01)
    before = profile.user.mean.copy()
    changed = _voice(140.0, 0.8, seed=5)
    target = voice.features(changed, RATE, 0.01).mean(axis=0)

    for _ in range(8):
        profile.learn_user(changed, RATE, 0.01)

    moved = np.linalg.norm(profile.user.mean - target)
    assert moved < 0.25 * np.linalg.norm(before - target)


def test_the_profile_survives_a_restart(profile):
    for seed in range(2):
        profile.learn_user(user_voice(seed), RATE, 0.01)
        profile.learn_echo(echo_voice(seed), RATE, 0.01)
    profile.note_echo_level(0.08)
    assert profile.save()

    again = voice.VoiceProfile()
    assert again.user.frames == profile.user.frames
    assert again.utterances == 2
    assert again.echo_level == pytest.approx(0.08)
    assert np.allclose(again.user.mean, profile.user.mean, atol=1e-3)


def test_a_damaged_profile_costs_what_was_learned_not_the_session(profile):
    profile.path.write_text('{"version": 1, "user": {"frames": 999, "mean": "x"}}')
    again = voice.VoiceProfile()
    assert again.user.frames == 0
    assert not again.trained


def test_the_profile_moves_with_the_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VOICE_FILE", tmp_path / "a.json")
    first = voice.get_voice_profile()
    monkeypatch.setattr(config, "VOICE_FILE", tmp_path / "b.json")
    assert voice.get_voice_profile() is not first


# ---------------------------------------------------------------------------
# the words
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "heard",
    [
        "is up and running",
        "Okay, Chrome's up and running. Anything else",  # punctuation differs
        "chrome's up and running now",
    ],
)
def test_e_v_s_own_sentence_is_recognised(heard):
    said = "Okay, Chrome's up and running now. Anything else?"
    assert voice.is_self_echo(heard, said)


@pytest.mark.parametrize(
    "heard",
    [
        "yes",  # the answer to "Confirm?"
        "open notepad",  # too short to judge, whatever was said
        "no wait open the downloads folder instead",
        "what's the weather like tomorrow",
    ],
)
def test_the_user_answering_is_not_mistaken_for_an_echo(heard):
    said = "Want me to open notepad? Confirm?"
    assert not voice.is_self_echo(heard, said)


# ---------------------------------------------------------------------------
# the speaker remembers what it said
# ---------------------------------------------------------------------------
class _NullPlayer:
    def play(self, path, block, timeout, aborted):
        pass

    def stop(self):
        pass


def test_the_speaker_knows_what_it_said_and_when_it_was_audible():
    speaker = Speaker()
    player = _NullPlayer()
    seen = {}

    def play(path, block, timeout, aborted):
        seen["audible"] = speaker.audible_since

    player.play = play
    speaker._player = player

    asyncio.run(speaker._play("unused.mp3", "Chrome is up."))

    assert seen["audible"] is not None
    assert speaker.audible_since is None
    assert "Chrome is up." in speaker.said_since(5.0)
    assert speaker.said_since(-1.0) == ""


# ---------------------------------------------------------------------------
# barge-in
# ---------------------------------------------------------------------------
class FakeMic:
    def __init__(self, level=0.0, energy=20, floor=0.02, pcm=b""):
        self.noise_floor = floor
        self.sample_rate = RATE
        self.level = level
        self.energy = energy
        self.pcm = pcm
        self.held = False

    def speech_energy(self):
        return self.energy

    def speech_level(self):
        return self.level

    def hold_audio(self):
        self.held = True

    def recent_audio(self, frames):
        return self.pcm

    def audio_between(self, start, end):
        return self.pcm


class FakeSpeaker:
    def __init__(self, audible=True):
        self.speaking = True
        self.stopped = False
        self.audible_since = time.monotonic() if audible else None

    def stop(self):
        self.stopped = True
        self.speaking = False


class FakeVoice:
    def __init__(self, score=None, echo_level=0.0):
        self._score = score
        self.echo_level = echo_level
        self.trained = score is not None
        self.echo_learned = 0
        self.levels = []

    def score(self, pcm, rate, floor):
        return self._score

    def learn_echo(self, pcm, rate, floor):
        self.echo_learned += 1

    def note_echo_level(self, level):
        self.levels.append(level)


class SilentUI:
    def __init__(self):
        self.notes = []

    def note(self, text):
        self.notes.append(text)

    def __getattr__(self, _name):
        return lambda *a, **k: None


def _assistant(mic, speaker, voice_profile=None):
    assistant = ev_core.EV.__new__(ev_core.EV)
    assistant.ui = SilentUI()
    assistant.mic = mic
    assistant.speaker = speaker
    assistant.voice = voice_profile
    assistant.text_mode = False
    assistant._running = True
    return assistant


@pytest.fixture(autouse=True)
def barge_settings(monkeypatch):
    monkeypatch.setattr(config, "TTS_BARGE_IN", True)
    monkeypatch.setattr(config, "BARGE_IN_FRAMES", 8)
    monkeypatch.setattr(config, "BARGE_IN_LEVEL_MULTIPLIER", 1.8)
    monkeypatch.setattr(config, "BARGE_IN_GRACE_S", 0.2)
    monkeypatch.setattr(config, "BARGE_IN_ECHO_LAG_S", 0.2)
    monkeypatch.setattr(config, "BARGE_IN_ECHO_MARGIN", 1.3)
    monkeypatch.setattr(config, "BARGE_IN_ECHO_MARGIN_KNOWN", 1.05)
    monkeypatch.setattr(config, "BARGE_IN_KEEP_AUDIO", True)


async def _watch(assistant, seconds, script=None):
    state = {}
    task = asyncio.ensure_future(assistant._watch_for_barge_in(state))
    started = time.monotonic()
    while time.monotonic() - started < seconds and not task.done():
        if script is not None:
            script(time.monotonic() - started)
        await asyncio.sleep(0.02)
    assistant.speaker.speaking = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return state


def test_e_v_s_own_steady_voice_never_takes_the_floor():
    """The bug: E.V. through the speakers is far louder than the room."""
    mic = FakeMic(level=0.09)  # 4.5x the floor; the old rule fired on this
    speaker = FakeSpeaker()
    assistant = _assistant(mic, speaker)

    asyncio.run(_watch(assistant, 1.2))

    assert speaker.stopped is False
    assert mic.held is False


def test_the_user_talking_over_the_echo_still_takes_the_floor():
    mic = FakeMic(level=0.09)
    speaker = FakeSpeaker()
    assistant = _assistant(mic, speaker)

    def script(elapsed):
        if elapsed > 0.8:
            mic.level = 0.2  # the user, on top of E.V.

    state = asyncio.run(_watch(assistant, 1.5, script))

    assert speaker.stopped is True
    assert mic.held is True
    assert state.get("barged") is True


def test_nothing_is_interrupted_before_e_v_makes_a_sound():
    """Grace counts from the first sound, not from the reply being queued."""
    mic = FakeMic(level=0.3)
    speaker = FakeSpeaker(audible=False)  # still synthesising
    assistant = _assistant(mic, speaker)

    asyncio.run(_watch(assistant, 0.6))

    assert speaker.stopped is False


def test_the_echo_level_learned_last_time_guards_the_first_moments(monkeypatch):
    monkeypatch.setattr(config, "BARGE_IN_GRACE_S", 0.0)
    mic = FakeMic(level=0.09)
    speaker = FakeSpeaker()
    assistant = _assistant(mic, speaker, FakeVoice(echo_level=0.1))

    asyncio.run(_watch(assistant, 0.15))

    assert speaker.stopped is False


def test_a_trained_ear_refuses_its_own_voice_however_loud():
    mic = FakeMic(level=0.05)
    speaker = FakeSpeaker()
    assistant = _assistant(mic, speaker, FakeVoice(score=-6.0))

    def script(elapsed):
        if elapsed > 0.6:
            mic.level = 0.4

    asyncio.run(_watch(assistant, 1.2, script))

    assert speaker.stopped is False


def test_a_known_voice_needs_less_margin_over_the_echo():
    mic = FakeMic(level=0.1)
    speaker = FakeSpeaker()
    assistant = _assistant(mic, speaker, FakeVoice(score=5.0))

    def script(elapsed):
        if elapsed > 0.8:
            mic.level = 0.115  # 1.15x the echo: under 1.3, over 1.05

    asyncio.run(_watch(assistant, 1.5, script))

    assert speaker.stopped is True


def test_an_uninterrupted_reply_teaches_e_v_its_own_voice():
    mic = FakeMic(level=0.09, pcm=echo_voice())
    speaker = FakeSpeaker()
    ear = FakeVoice()
    assistant = _assistant(mic, speaker, ear)

    state = asyncio.run(_watch(assistant, 1.0))
    assistant._learn_echo(state)

    assert ear.echo_learned == 1
    assert ear.levels and ear.levels[0] == pytest.approx(0.09)


def test_an_interrupted_reply_teaches_nothing():
    """The audio held the user's voice too; learning it as echo would poison it."""
    ear = FakeVoice()
    assistant = _assistant(FakeMic(), FakeSpeaker(), ear)
    assistant._learn_echo({"barged": True, "heard_from": 0.0, "samples": [(0.0, 0.1)] * 20})
    assert ear.echo_learned == 0


# ---------------------------------------------------------------------------
# the loop itself
# ---------------------------------------------------------------------------
class EchoingSpeaker:
    enabled = False
    speaking = False

    def __init__(self, said):
        self._said = said

    def said_since(self, window_s):
        return self._said

    def stop(self):
        pass


def _ticking(heard, said, ear=None):
    assistant = _assistant(FakeMic(), EchoingSpeaker(said), ear)
    assistant.session = Session()
    assistant.session.engage()  # E.V. just spoke: no wake phrase needed
    assistant._queued_utterance = None
    assistant._last_utterance = Utterance(echo_voice(), 3.0, RATE)
    assistant.routed = []

    async def _fake_utterance(wait_s=None):
        return Transcript(heard)

    async def _fake_route(transcript, command):
        assistant.routed.append(command)

    assistant._next_utterance = _fake_utterance
    assistant._route = _fake_route
    return assistant


def test_e_v_never_answers_its_own_words():
    said = "Done. I've moved twelve files into Documents. Anything else?"
    ear = FakeVoice()
    assistant = _ticking("I've moved twelve files into documents", said, ear)

    asyncio.run(assistant._tick())

    assert assistant.routed == []
    assert ear.echo_learned == 1  # and learns what it sounds like


def test_the_user_is_still_answered_straight_after_e_v_speaks():
    said = "Done. I've moved twelve files into Documents. Anything else?"
    assistant = _ticking("now empty the downloads folder", said)

    asyncio.run(assistant._tick())

    assert assistant.routed == ["now empty the downloads folder"]


def test_an_addressed_utterance_teaches_e_v_the_user_s_voice(profile):
    assistant = _ticking("now empty the downloads folder", "Done.", profile)
    assistant._last_utterance = Utterance(user_voice(), 3.0, RATE)

    asyncio.run(assistant._tick())

    assert profile.utterances == 1
    assert profile.user.frames > 0
    assert assistant._last_utterance is None  # learned once, never twice


def test_the_microphone_keeps_recent_audio_for_the_ear():
    mic = Microphone()
    now = time.monotonic()
    for index in range(5):
        mic._history.append((now + index, bytes([index]) * 4))

    assert mic.recent_audio(2) == bytes([3]) * 4 + bytes([4]) * 4
    assert mic.audio_between(now + 1, now + 2) == bytes([1]) * 4 + bytes([2]) * 4
    assert mic.recent_audio(0) == b""
