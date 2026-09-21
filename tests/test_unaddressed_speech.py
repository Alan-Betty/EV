"""What E.V. does with words that were not meant for it.

Once the conversation window has expired the wake phrase is required again,
and everything said without it is not E.V.'s business. That has to *look* like
it is not E.V.'s business too. Echoing the transcript first and checking the
wake phrase afterwards put every sentence spoken near the microphone on screen
under a "you >" prompt - so a conversation with somebody else in the room was
written into E.V.'s terminal, appeared to have been heard and understood, and
then got no reply. Silence is the right answer to something that was not said
to you, and silence has to be visible as silence.

The recogniser is a separate question and is answered in the module docstring
of `ev.wake`: detecting "E.V." in an utterance means having the words of that
utterance, and E.V. runs no local model by design. What this file pins is that
an unaddressed transcript goes no further - it is not drawn, not acted on, and
does not steer the decoding prompt for whatever is said next.

Offline: no microphone, no network, no audio device.
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
from ev.session import Session  # noqa: E402
from ev.stt import Transcript  # noqa: E402


class RecordingUI:
    """Captures what would have been drawn, drawing nothing."""

    def __init__(self) -> None:
        self.echoed: list[str] = []
        self.notes: list[str] = []

    def user(self, text: str, engaged: bool = False) -> None:
        self.echoed.append(str(text))

    def note(self, text: str) -> None:
        self.notes.append(text)

    def __getattr__(self, _name):
        def _draw(*_args, **_kwargs):
            return None

        return _draw


class FakeTranscriber:
    """Records which transcripts were allowed to steer the decoding prompt."""

    def __init__(self) -> None:
        self.noted: list[str] = []

    def set_hints(self, words) -> None:
        pass

    def note_transcript(self, text) -> None:
        self.noted.append(str(text))


class FakeSpeaker:
    def __init__(self) -> None:
        self.enabled = False
        self.speaking = False
        self.said: list[str] = []

    async def say(self, text: str) -> str:
        self.said.append(text)
        return text

    def stop(self) -> None:
        self.speaking = False


def _assistant(monkeypatch, heard: str, engaged: bool = False) -> ev_core.EV:
    """An `EV` whose microphone yields exactly one utterance."""
    assistant = ev_core.EV.__new__(ev_core.EV)
    assistant.ui = RecordingUI()
    assistant.speaker = FakeSpeaker()
    assistant.transcriber = FakeTranscriber()
    assistant.session = Session()
    assistant.mic = object()  # never touched; `_next_utterance` is stubbed
    assistant.text_mode = False
    assistant._running = True
    assistant._queued_utterance = None
    assistant.routed: list[str] = []

    if engaged:
        assistant.session.engage()

    async def _fake_utterance(wait_s=None):
        return Transcript(heard)

    async def _fake_route(transcript, command):
        assistant.routed.append(command)
        # The real `_route` feeds the decoding prompt once it knows the words
        # were addressed here; keep that half so the assertion is meaningful.
        assistant.transcriber.note_transcript(transcript)

    assistant._next_utterance = _fake_utterance
    assistant._route = _fake_route
    return assistant


@pytest.fixture(autouse=True)
def wake_required(monkeypatch):
    monkeypatch.setattr(config, "WAKE_REQUIRED", True)


# ---------------------------------------------------------------------------
# idle: the wake phrase is required
# ---------------------------------------------------------------------------
def test_an_unaddressed_utterance_is_not_echoed(monkeypatch):
    """The whole complaint: words meant for somebody else appeared on screen."""
    assistant = _assistant(monkeypatch, "so then I told him it was fine")

    asyncio.run(assistant._tick())

    assert assistant.ui.echoed == []
    assert assistant.routed == []


def test_an_unaddressed_utterance_does_not_steer_the_next_transcription():
    """A conversation across the room must not bias the next command.

    Whisper reads the decoding prompt as text immediately preceding the audio,
    so feeding it everything the room says makes the recogniser expect more of
    the same - at the cost of the command that actually follows.
    """
    assistant = _assistant(None, "so then I told him it was fine")

    asyncio.run(assistant._tick())

    assert assistant.transcriber.noted == []


def test_an_addressed_utterance_is_echoed_and_routed():
    assistant = _assistant(None, "E.V. open notepad")

    asyncio.run(assistant._tick())

    assert assistant.ui.echoed == ["E.V. open notepad"]
    assert assistant.routed == ["open notepad"]
    assert assistant.transcriber.noted == ["E.V. open notepad"]


def test_the_wake_phrase_is_stripped_before_the_command_is_routed():
    assistant = _assistant(None, "hey E.V., what's my python version")

    asyncio.run(assistant._tick())

    assert assistant.routed
    assert "what's my python version" in assistant.routed[0].lower()


# ---------------------------------------------------------------------------
# engaged: the name is optional inside the window
# ---------------------------------------------------------------------------
def test_inside_the_conversation_window_no_wake_phrase_is_needed():
    """This is the whole point of the engaged state, and it still holds."""
    assistant = _assistant(None, "and open notepad too", engaged=True)

    asyncio.run(assistant._tick())

    assert assistant.ui.echoed == ["and open notepad too"]
    assert assistant.routed == ["and open notepad too"]


def test_once_the_window_expires_the_name_is_required_again(monkeypatch):
    """After `CONVERSATION_WINDOW_S` of quiet, an unaddressed sentence is
    ignored again - drawn nowhere, routed nowhere."""
    monkeypatch.setattr(config, "CONVERSATION_WINDOW_S", 0.0)
    assistant = _assistant(None, "so then I told him it was fine", engaged=True)

    assert assistant.session.engaged is False, "window should have lapsed"

    asyncio.run(assistant._tick())

    assert assistant.ui.echoed == []
    assert assistant.routed == []


# ---------------------------------------------------------------------------
# the near-miss hint still works
# ---------------------------------------------------------------------------
def test_a_mangled_name_still_counts_as_being_addressed():
    """Wake detection is fuzzy on purpose: STT turns "E.V." into Evie, AV,
    heavy. Those are matches, not near misses, so they are echoed and routed
    like any other command - the fuzziness is what makes the wake phrase
    usable at all, and tightening it here would break waking up."""
    assistant = _assistant(None, "evie open notepad")

    asyncio.run(assistant._tick())

    assert assistant.ui.echoed == ["evie open notepad"]
    assert assistant.routed == ["open notepad"]


def test_ordinary_speech_that_merely_rhymes_is_still_ignored():
    """The other side of that trade: fuzzy must not mean indiscriminate."""
    assistant = _assistant(None, "everything is fine, leave it")

    asyncio.run(assistant._tick())

    assert assistant.ui.echoed == []
    assert assistant.routed == []


def test_an_empty_transcript_costs_nothing():
    assistant = _assistant(None, "")

    asyncio.run(assistant._tick())

    assert assistant.ui.echoed == []
    assert assistant.routed == []
    assert assistant.transcriber.noted == []


# ---------------------------------------------------------------------------
# And attention has to look like attention
# ---------------------------------------------------------------------------
class SpinnerUI(RecordingUI):
    """A console that records which spinners were run, and for how long."""

    def __init__(self) -> None:
        super().__init__()
        self.live: str = ""
        self.started: list[str] = []
        self.transient: list[str] = []

    def begin_status(self, label: str) -> None:
        if self.live != label:
            self.started.append(label)
        self.live = label

    def end_status(self) -> None:
        self.live = ""

    def status(self, label: str):
        self.transient.append(label)
        import contextlib as _contextlib

        return _contextlib.nullcontext()


class OneUtteranceMic:
    """A microphone that hands over one clip and then only silence."""

    def __init__(self, clips: int = 1) -> None:
        self.left = clips
        self.listens = 0

    def listen(self, wait_s=None, stop=None):
        self.listens += 1
        if self.left <= 0:
            return None
        self.left -= 1
        return type("Clip", (), {"wav": b"RIFF", "duration_s": 1.0})()


class EchoTranscriber(FakeTranscriber):
    def __init__(self, text: str = "what time is it") -> None:
        super().__init__()
        self.text = text

    async def transcribe(self, _wav):
        return Transcript(self.text)


def _listener(engaged: bool, clips: int = 1) -> ev_core.EV:
    assistant = ev_core.EV.__new__(ev_core.EV)
    assistant.ui = SpinnerUI()
    assistant.speaker = FakeSpeaker()
    assistant.transcriber = EchoTranscriber()
    assistant.session = Session()
    assistant.mic = OneUtteranceMic(clips)
    assistant.text_mode = False
    assistant._running = True
    assistant._clock = None
    if engaged:
        assistant.session.engage()
    return assistant


def test_the_listening_spinner_runs_only_inside_the_conversation_window():
    """Animated while idle, it claims an attention E.V. is not paying.

    Outside the window nothing is acted on without the wake phrase, so a
    spinner through that says "I am listening to you" about a room E.V. is
    only filtering. Inside the window it is the honest signal that the next
    sentence needs no name in front of it.
    """
    engaged = _listener(engaged=True)
    asyncio.run(engaged._next_utterance(wait_s=2.0))
    assert engaged.ui.started == ["Listening..."]

    idle = _listener(engaged=False)
    asyncio.run(idle._next_utterance(wait_s=None))
    assert idle.ui.started == []


def test_transcribing_an_unaddressed_utterance_is_not_announced():
    """It is still transcribed - the wake phrase is in the words - but a
    spinner about it is the same claim of attention, made about somebody
    else's conversation."""
    idle = _listener(engaged=False)
    asyncio.run(idle._next_utterance(wait_s=None))
    assert idle.ui.transient == []

    engaged = _listener(engaged=True)
    asyncio.run(engaged._next_utterance(wait_s=2.0))
    assert engaged.ui.transient == ["Transcribing..."]


def test_the_spinner_is_down_before_anything_is_printed():
    """`rich` allows one live display at a time, and a panel drawn under a
    running spinner is a panel fighting it for the cursor."""
    assistant = _listener(engaged=True)
    asyncio.run(assistant._next_utterance(wait_s=2.0))
    assert assistant.ui.live == ""


def test_a_quiet_window_does_not_restart_the_spinner_every_poll():
    """The window is several trips round the loop: E.V. polls on a short
    timeout so it can expire. Rebuilt each time, the spinner is a flicker."""
    assistant = _listener(engaged=True, clips=0)
    for _ in range(3):
        asyncio.run(assistant._next_utterance(wait_s=2.0))
    assert assistant.ui.started == ["Listening..."]
    assert assistant.mic.listens == 3
