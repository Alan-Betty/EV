"""The "stand by" acknowledgement, and the state E.V. carries across restarts.

Two properties are worth pinning down here, because breaking either one is
exactly the sort of regression that only shows up on a slow machine:

* **The acknowledgement never delays the work it announces.** It is a
  background task, and the tool dispatch has to be underway before a syllable
  comes out. A test that only checked "it spoke" would pass just as happily on
  an implementation that awaited the speech first and made every command a
  second slower.
* **It is retired before the real answer.** A cancelled acknowledgement must
  not leave the speaker lock held, or E.V. goes silent for the rest of the run.

Everything runs offline against a fake speaker; no audio device is opened.
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

import pytest  # noqa: E402

import config  # noqa: E402
import ev_core  # noqa: E402
from ev.brain import ToolCall  # noqa: E402
from ev.tts import clean_for_speech  # noqa: E402


class FakeSpeaker:
    """Records what would have been said, and how long it held the floor."""

    def __init__(self, duration: float = 0.0) -> None:
        self.enabled = True
        self.said: list[str] = []
        self.speaking = False
        self.started_at = 0.0
        self.finished_at = 0.0
        self._duration = duration

    async def say(self, text: str) -> str:
        self.speaking = True
        self.started_at = time.monotonic()
        try:
            if self._duration:
                await asyncio.sleep(self._duration)
            self.said.append(text)
            return text
        finally:
            self.finished_at = time.monotonic()
            self.speaking = False

    def stop(self) -> None:
        self.speaking = False


class SilentUI:
    """Every `ev.ui` call is a `-> None` draw, so a no-op stands in for all."""

    def __init__(self) -> None:
        self.notes: list[str] = []

    def note(self, text: str) -> None:
        self.notes.append(text)

    def __getattr__(self, _name):
        def _draw(*_args, **_kwargs):
            return None

        return _draw


class FakeTranscriber:
    """Records the decoding hints `_boot` feeds it. No network."""

    def __init__(self) -> None:
        self.hints: list[str] = []
        self.recent = ""

    def set_hints(self, words: list[str]) -> None:
        self.hints = list(words)

    def note_transcript(self, text: str) -> None:
        self.recent = text


def _assistant(monkeypatch, speaker: FakeSpeaker) -> ev_core.EV:
    """An `EV` with no network, no microphone and no audio device."""
    assistant = ev_core.EV.__new__(ev_core.EV)
    assistant.ui = SilentUI()
    assistant.speaker = speaker
    assistant.transcriber = FakeTranscriber()
    assistant.mic = None
    assistant.text_mode = True
    assistant._running = True
    return assistant


@pytest.fixture(autouse=True)
def fast_ack(monkeypatch):
    monkeypatch.setattr(config, "ACK_ENABLED", True)
    monkeypatch.setattr(config, "ACK_DELAY_S", 0.01)


# -- what gets acknowledged --------------------------------------------------
def test_chat_is_never_acknowledged(monkeypatch):
    """`chat` has no side effect to wait for, and streams its own reply."""
    assistant = _assistant(monkeypatch, FakeSpeaker())
    assert assistant._start_ack(ToolCall("chat", {"reply": "Mm-hm."})) is None


def test_acknowledgement_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "ACK_ENABLED", False)
    assistant = _assistant(monkeypatch, FakeSpeaker())
    assert assistant._start_ack(ToolCall("open_app", {"app": "chrome"})) is None


def test_a_tool_call_draws_the_acknowledgement_immediately(monkeypatch):
    """The visual half lands with no delay at all - it is only a draw."""

    async def scenario():
        assistant = _assistant(monkeypatch, FakeSpeaker())
        ack = assistant._start_ack(ToolCall("open_app", {"app": "chrome"}))
        notes = list(assistant.ui.notes)
        await assistant._finish_ack(ack)
        return notes

    notes = asyncio.run(scenario())
    assert notes and notes[0] in config.ACK_PHRASES


def test_a_fast_tool_is_never_acknowledged_out_loud(monkeypatch):
    """A tool that finishes in milliseconds needs no "stand by"."""

    async def scenario():
        speaker = FakeSpeaker()
        assistant = _assistant(monkeypatch, speaker)
        ack = assistant._start_ack(ToolCall("open_app", {"app": "chrome"}))
        await asyncio.sleep(0)  # the tool came back at once
        await assistant._finish_ack(ack)
        return speaker.said

    assert asyncio.run(scenario()) == []


def test_a_slow_tool_is_acknowledged_out_loud(monkeypatch):
    async def scenario():
        speaker = FakeSpeaker()
        assistant = _assistant(monkeypatch, speaker)
        ack = assistant._start_ack(ToolCall("dev_workflow", {"directory": "api"}))
        await asyncio.sleep(0.05)  # the tool is still working
        await assistant._finish_ack(ack)
        return speaker.said

    said = asyncio.run(scenario())
    assert len(said) == 1
    assert said[0] in config.ACK_PHRASES


def test_an_acknowledgement_already_speaking_is_allowed_to_finish(monkeypatch):
    """Cutting a `to_thread` playback short would put E.V. on top of itself."""

    async def scenario():
        speaker = FakeSpeaker(duration=0.05)
        assistant = _assistant(monkeypatch, speaker)
        ack = assistant._start_ack(ToolCall("terminal_command", {"command": "git status"}))
        await asyncio.sleep(0.02)  # mid-sentence
        assert speaker.speaking is True
        await assistant._finish_ack(ack)
        return speaker.said

    assert len(asyncio.run(scenario())) == 1


def test_finishing_a_missing_acknowledgement_is_harmless(monkeypatch):
    assistant = _assistant(monkeypatch, FakeSpeaker())
    asyncio.run(assistant._finish_ack(None))


# -- the latency guarantee ---------------------------------------------------
def test_the_tool_runs_while_the_acknowledgement_is_still_being_spoken(monkeypatch):
    """The whole point: speech is concurrent with the work, never before it.

    Asserted as an overlap rather than a total elapsed time. A wall-clock
    bound would be a flake waiting for a loaded CI box, while the overlap is
    the actual property: an implementation that awaited the speech before
    dispatching would show the tool starting only after the speech ended.
    """
    marks: dict[str, float] = {}

    def slow_tool():
        marks["tool_start"] = time.monotonic()
        time.sleep(0.15)
        marks["tool_end"] = time.monotonic()

    async def scenario():
        speaker = FakeSpeaker(duration=0.05)
        assistant = _assistant(monkeypatch, speaker)
        ack = assistant._start_ack(ToolCall("open_app", {"app": "chrome"}))
        await asyncio.to_thread(slow_tool)
        await assistant._finish_ack(ack)
        return speaker

    speaker = asyncio.run(scenario())
    assert speaker.said, "a tool this slow should have been acknowledged"
    # Genuine overlap in both directions: neither one waited for the other.
    assert marks["tool_start"] < speaker.finished_at
    assert speaker.started_at < marks["tool_end"]


def test_acknowledgement_phrases_are_speakable(monkeypatch):
    """They go to the speaker on the same path as any reply, so no labels."""
    assert config.ACK_PHRASES
    for phrase in config.ACK_PHRASES:
        assert clean_for_speech(phrase) == phrase


# -- backlog logging from the loop -------------------------------------------
@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKLOG_FILE", tmp_path / "backlog.json")
    monkeypatch.setattr(config, "BACKLOG_ENABLED", True)
    monkeypatch.setattr(config, "BACKLOG_AUTOLOG", True)
    from ev.backlog import Backlog

    return Backlog()


def test_a_failed_action_lands_on_the_backlog(monkeypatch, state):
    assistant = _assistant(monkeypatch, FakeSpeaker())
    assistant.backlog = state

    assistant._log_backlog(
        "copy the invoices to Documents",
        ToolCall("file_manager", {"action": "copy", "path": "invoices"}),
        "failed",
        "Source not found",
    )

    pending = state.pending()
    assert [item.text for item in pending] == ["copy the invoices to Documents"]
    assert pending[0].tool == "file_manager"
    assert pending[0].kind == "failed"


def test_chat_is_never_backlogged(monkeypatch, state):
    assistant = _assistant(monkeypatch, FakeSpeaker())
    assistant.backlog = state
    assistant._log_backlog("how are you", ToolCall("chat", {"reply": "Fine."}), "failed")
    assert state.pending() == []


def test_autolog_can_be_switched_off(monkeypatch, state):
    monkeypatch.setattr(config, "BACKLOG_AUTOLOG", False)
    assistant = _assistant(monkeypatch, FakeSpeaker())
    assistant.backlog = state
    assistant._log_backlog("do the thing", ToolCall("open_app", {"app": "x"}), "failed")
    assert state.pending() == []


def test_a_backlogged_command_drops_its_confirmation(monkeypatch, state):
    """An abandoned confirmation must be asked about again, not assumed."""
    assistant = _assistant(monkeypatch, FakeSpeaker())
    assistant.backlog = state
    assistant._log_backlog(
        "delete the build folder",
        ToolCall("file_manager", {"action": "delete", "path": "build", "confirmed": True}),
        "interrupted",
    )
    assert "confirmed" not in state.pending()[0].args


# -- boot report -------------------------------------------------------------
def test_the_boot_report_is_assembled_once_and_reaches_the_model(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr(config, "BACKLOG_FILE", tmp_path / "backlog.json")
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "BACKLOG_ENABLED", True)

    from ev.backlog import Backlog
    from ev.brain import Brain
    from ev.memory import Memory

    assistant = _assistant(monkeypatch, FakeSpeaker())
    assistant.memory = Memory()
    assistant.backlog = Backlog()
    assistant.backlog.add("back up the photos", kind="reminder")
    assistant.brain = Brain.__new__(Brain)
    assistant._boot_report = None
    assistant.memory.remember("coffee", "black")

    first = assistant._boot()
    assert first.first_run is True
    assert "coffee is black" in assistant.brain.session_context
    assert "back up the photos" in assistant.brain.session_context

    # Idempotent: `--say` and the interactive loop both call it.
    assert assistant._boot() is first


def _stateful(monkeypatch, tmp_path, speaker, offline_seconds=0.0):
    """An `EV` wired to temporary memory and backlog files."""
    import json

    monkeypatch.setattr(config, "MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr(config, "BACKLOG_FILE", tmp_path / "backlog.json")
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "BACKLOG_ENABLED", True)

    from ev.backlog import Backlog
    from ev.brain import Brain
    from ev.memory import Memory
    from ev.session import Session

    memory = Memory()
    memory.begin_session()
    memory.end_session(clean=True)
    if offline_seconds:
        data = json.loads((tmp_path / "memory.json").read_text(encoding="utf-8"))
        data["state"]["last_active"] = time.time() - offline_seconds
        (tmp_path / "memory.json").write_text(json.dumps(data), encoding="utf-8")

    assistant = _assistant(monkeypatch, speaker)
    assistant.memory = Memory()
    assistant.backlog = Backlog()
    assistant.brain = Brain.__new__(Brain)
    assistant.session = Session()
    assistant._boot_report = None
    return assistant


def test_the_boot_report_says_how_long_we_were_down_and_what_is_open(
    monkeypatch, tmp_path
):
    speaker = FakeSpeaker()
    assistant = _stateful(monkeypatch, tmp_path, speaker, offline_seconds=8100)
    assistant.backlog.add("copy the invoices")
    assistant.backlog.add("back up the photos")

    assistant._boot()
    asyncio.run(assistant._report_state())

    assert speaker.said[0] == (
        "Welcome back. You were offline for 2 hours and 15 minutes."
    )
    assert "2 backlog items remaining from your previous session" in speaker.said[1]
    # Both went to the speaker through `EV.say`, so both are label-free.
    for spoken in speaker.said:
        assert clean_for_speech(spoken) == spoken


def test_a_quiet_restart_with_nothing_open_says_nothing(monkeypatch, tmp_path):
    speaker = FakeSpeaker()
    assistant = _stateful(monkeypatch, tmp_path, speaker)
    assistant._boot()
    asyncio.run(assistant._report_state())
    assert speaker.said == []
