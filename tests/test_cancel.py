"""Stopping a tool that is already running.

"Stop" used to do nothing once work was underway: the tool sits on a worker
thread, the loop is blocked awaiting it, and a thread cannot be killed from
outside. Cancellation is therefore *cooperative* - the tool agrees to stop,
and only at a point where stopping is safe.

That last part is what these tests are really for. Three properties matter
more than the feature itself:

* **Never mid-write.** A cancelled sweep leaves whole files, not halves.
* **Never a lie.** A tool that cannot honour the token must not report that it
  stopped. `open_app` has already launched the program; saying otherwise would
  be worse than saying nothing.
* **Never spoofable.** `cancel` is attached after the model's arguments are
  filtered, so the model can neither set it nor clear it.

Offline: the one subprocess used is `sleep`, run through the real shell.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

import config  # noqa: E402
from ev.tts import clean_for_speech  # noqa: E402
from tools import CANCELLABLE, dispatch  # noqa: E402
from tools.base import CancelToken, ToolResult, was_cancelled  # noqa: E402
from tools.file_manager import file_manager  # noqa: E402


class CancelAfter(CancelToken):
    """Trips once `n` safe-points have gone past.

    Lets a sweep be stopped at an exact file rather than at whatever moment a
    timer happens to fire, which would make these tests flaky.
    """

    __slots__ = ("_remaining",)

    def __init__(self, n: int) -> None:
        super().__init__()
        self._remaining = n

    @property
    def cancelled(self) -> bool:
        self._remaining -= 1
        return self._remaining < 0


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A fake home, isolated from the real one."""
    home = tmp_path / "home"
    dirs = {
        name: home / name.title()
        for name in ("desktop", "downloads", "documents", "pictures")
    }
    dirs["home"] = home
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "FILE_ROOTS", [home])
    monkeypatch.setattr(config, "USER_DIRS", dirs)
    monkeypatch.setattr(config, "FILE_DEFAULT_DIR", dirs["documents"])
    monkeypatch.setattr(config, "FILE_CONFIRM_DELETE", True)
    return dirs


def _seed(folder: Path, count: int, suffix: str = ".txt") -> None:
    for index in range(count):
        (folder / f"file{index:02d}{suffix}").write_text("x", encoding="utf-8")


# -- the token ---------------------------------------------------------------
def test_a_fresh_token_means_keep_going():
    token = CancelToken()
    assert token.cancelled is False
    assert bool(token) is True
    assert was_cancelled(token) is False


def test_cancelling_is_visible_from_another_thread():
    """The setter is the event loop; the reader is a worker thread."""
    token = CancelToken()
    seen: list[bool] = []

    def worker():
        while not token.cancelled:
            time.sleep(0.005)
        seen.append(True)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    token.cancel()
    thread.join(timeout=2.0)

    assert seen == [True]
    assert bool(token) is False


def test_no_token_at_all_is_not_a_cancellation():
    assert was_cancelled(None) is False


def test_waiting_wakes_early_when_cancelled():
    """This is what makes "stop" land in milliseconds, not a poll interval."""
    token = CancelToken()
    threading.Timer(0.05, token.cancel).start()
    started = time.monotonic()
    token.wait(5.0)
    assert time.monotonic() - started < 1.0


def test_a_stopped_result_counts_as_success_but_is_marked():
    """The part that ran really did run, so this is not a failure."""
    result = ToolResult.stopped("Stopped.", "moved 3 of 10")
    assert result.ok is True
    assert result.cancelled is True
    assert ToolResult.success("Done.").cancelled is False
    assert ToolResult.failure("Nope.").cancelled is False


# -- batch file operations ---------------------------------------------------
def test_a_cancelled_copy_leaves_whole_files_behind(sandbox):
    _seed(sandbox["downloads"], 10)

    result = file_manager(
        action="batch_copy",
        path="Downloads",
        destination="Documents",
        cancel=CancelAfter(3),
    )

    assert result.cancelled is True
    assert result.ok is True
    copied = list(sandbox["documents"].iterdir())
    assert len(copied) == 3
    # Whole files, not halves: this is why the check sits between files.
    assert all(path.read_text(encoding="utf-8") == "x" for path in copied)
    # A copy leaves every source where it was.
    assert len(list(sandbox["downloads"].iterdir())) == 10


def test_a_cancelled_move_keeps_the_rest_where_they_were(sandbox):
    _seed(sandbox["desktop"], 10, ".png")

    result = file_manager(
        action="batch_move",
        path="Desktop",
        destination="Pictures",
        confirmed=True,
        cancel=CancelAfter(4),
    )

    assert result.cancelled is True
    assert len(list(sandbox["pictures"].iterdir())) == 4
    assert len(list(sandbox["desktop"].iterdir())) == 6


def test_the_count_that_survived_is_reported_exactly(sandbox):
    """"Stopped" alone leaves the user guessing whether anything happened."""
    _seed(sandbox["downloads"], 10)

    result = file_manager(
        action="batch_copy",
        path="Downloads",
        destination="Documents",
        cancel=CancelAfter(3),
    )

    assert "3" in result.speech
    assert "3 of 10" in result.detail
    assert "7 left untouched" in result.detail
    assert result.data["done"] == 3
    assert result.data["remaining"] == 7


def test_a_cancelled_rename_stops_cleanly(sandbox):
    _seed(sandbox["pictures"], 6, ".jpg")

    result = file_manager(
        action="batch_rename",
        path="Pictures",
        pattern="*.jpg",
        new_name="holiday",
        confirmed=True,
        cancel=CancelAfter(2),
    )

    assert result.cancelled is True
    names = sorted(p.name for p in sandbox["pictures"].iterdir())
    assert sum(1 for name in names if name.startswith("holiday")) == 2
    assert len(names) == 6  # nothing lost, nothing duplicated


def test_a_cancelled_organize_reports_what_it_sorted(sandbox):
    for name in ("a.jpg", "b.pdf", "c.mp3", "d.zip", "e.txt", "f.png"):
        (sandbox["downloads"] / name).write_text("x", encoding="utf-8")

    result = file_manager(
        action="organize", path="Downloads", confirmed=True, cancel=CancelAfter(2)
    )

    assert result.cancelled is True
    assert "2" in result.speech
    assert result.data["remaining"] == 4


def test_an_untripped_token_changes_nothing(sandbox):
    """The common path: a token exists but nobody ever says stop."""
    _seed(sandbox["downloads"], 5)

    result = file_manager(
        action="batch_copy",
        path="Downloads",
        destination="Documents",
        cancel=CancelToken(),
    )

    assert result.cancelled is False
    assert result.ok is True
    assert len(list(sandbox["documents"].iterdir())) == 5


def test_no_token_at_all_still_works(sandbox):
    """Cancellation is optional; every tool must run without it."""
    _seed(sandbox["downloads"], 3)
    result = file_manager(
        action="batch_copy", path="Downloads", destination="Documents"
    )
    assert result.ok is True
    assert result.cancelled is False


# -- shell commands ----------------------------------------------------------
def test_a_running_command_is_killed_not_waited_out():
    """The real thing: a 20-second command stopped after one."""
    token = CancelToken()
    threading.Timer(0.8, token.cancel).start()

    started = time.monotonic()
    result = dispatch(
        "terminal_command",
        {"command": "Start-Sleep -Seconds 20", "confirmed": True},
        token,
    )
    elapsed = time.monotonic() - started

    assert result.cancelled is True
    # Generous bound; the point is that it is nowhere near twenty.
    assert elapsed < 8.0, f"cancel did not land, took {elapsed:.1f}s"


def test_an_already_cancelled_token_stops_almost_immediately():
    token = CancelToken()
    token.cancel()
    result = dispatch(
        "terminal_command", {"command": "Start-Sleep -Seconds 20", "confirmed": True}, token
    )
    assert result.cancelled is True


def test_an_uncancelled_command_still_returns_its_output():
    result = dispatch(
        "terminal_command", {"command": "Write-Output hello", "confirmed": True}
    )
    assert result.ok is True
    assert result.cancelled is False
    assert "hello" in result.detail


# -- dispatch contract -------------------------------------------------------
def test_only_tools_that_can_stop_are_offered_the_token():
    assert CANCELLABLE == frozenset({"terminal_command", "file_manager"})
    # Launching a program cannot be undone, so it is deliberately absent.
    assert "open_app" not in CANCELLABLE
    assert "dev_workflow" not in CANCELLABLE


def test_a_tool_that_cannot_cancel_ignores_the_token_without_erroring():
    """It must run normally, not blow up on an argument it never declared."""
    token = CancelToken()
    token.cancel()
    result = dispatch("chat", {"reply": "Still here."}, token)
    assert result.ok is True
    assert result.speech == "Still here."
    assert result.cancelled is False


def test_the_model_cannot_supply_its_own_cancel(sandbox):
    """`cancel` is attached after filtering, so this string is dropped."""
    _seed(sandbox["downloads"], 3)
    result = dispatch(
        "file_manager",
        {
            "action": "batch_copy",
            "path": "Downloads",
            "destination": "Documents",
            "cancel": "true",
        },
    )
    assert result.ok is True
    assert result.cancelled is False
    assert len(list(sandbox["documents"].iterdir())) == 3


def test_cancel_is_absent_from_every_schema():
    """Nothing the model can see mentions it."""
    from tools.schemas import TOOL_SPECS

    for spec in TOOL_SPECS:
        assert "cancel" not in spec["parameters"].get("properties", {})


# -- the core loop's listener ------------------------------------------------
class FakeMic:
    """Hands back one scripted utterance, then silence."""

    def __init__(self, *utterances) -> None:
        self._queue = list(utterances)
        self.calls = 0

    def listen(self, max_wait_s=None, should_stop=None):
        self.calls += 1
        if should_stop is not None and should_stop():
            return None
        if self._queue:
            return self._queue.pop(0)
        time.sleep(0.02)
        return None


class FakeUtterance:
    wav = b"RIFF"
    duration_s = 1.0


def _listening_assistant(monkeypatch, mic, heard):
    """An `EV` with just enough wired up to run `_watch_for_cancel`."""
    import ev_core
    from ev.session import Session

    from tests.test_acknowledgement import FakeSpeaker, SilentUI  # noqa: PLC0415

    class FakeTranscriber:
        async def transcribe(self, _wav):
            from ev.stt import Transcript

            return Transcript(heard, avg_logprob=-0.2)

    assistant = ev_core.EV.__new__(ev_core.EV)
    assistant.ui = SilentUI()
    assistant.speaker = FakeSpeaker()
    assistant.transcriber = FakeTranscriber()
    assistant.session = Session()
    assistant.mic = mic
    assistant.text_mode = False
    assistant._running = True
    assistant._queued_utterance = None
    assistant._cancel_refused = None
    monkeypatch.setattr(config, "CANCEL_ENABLED", True)
    monkeypatch.setattr(config, "CANCEL_LISTEN_AFTER_S", 0.0)
    return assistant


def test_saying_stop_trips_the_token(monkeypatch):
    import asyncio

    from ev.brain import ToolCall

    async def scenario():
        assistant = _listening_assistant(monkeypatch, FakeMic(FakeUtterance()), "stop")
        token = CancelToken()
        stop_watching = threading.Event()
        await assistant._watch_for_cancel(
            ToolCall("terminal_command", {"command": "sleep"}), token, stop_watching
        )
        return token, assistant

    token, assistant = asyncio.run(scenario())
    assert token.cancelled is True
    assert assistant._cancel_refused is None


def test_stopping_an_unstoppable_tool_is_refused_not_faked(monkeypatch):
    """`open_app` has already launched it. Claiming a stop would be a lie."""
    import asyncio

    from ev.brain import ToolCall

    async def scenario():
        assistant = _listening_assistant(monkeypatch, FakeMic(FakeUtterance()), "stop")
        token = CancelToken()
        await assistant._watch_for_cancel(
            ToolCall("open_app", {"app": "chrome"}), token, threading.Event()
        )
        return token, assistant

    token, assistant = asyncio.run(scenario())
    assert token.cancelled is False
    assert assistant._cancel_refused == "open_app"


def test_something_that_is_not_a_cancel_is_kept_not_dropped(monkeypatch):
    """Talking over a slow tool is usually the next command."""
    import asyncio

    from ev.brain import ToolCall

    async def scenario():
        assistant = _listening_assistant(
            monkeypatch, FakeMic(FakeUtterance()), "what's the weather"
        )
        token = CancelToken()
        await assistant._watch_for_cancel(
            ToolCall("terminal_command", {"command": "sleep"}), token, threading.Event()
        )
        return token, assistant

    token, assistant = asyncio.run(scenario())
    assert token.cancelled is False
    assert assistant._queued_utterance == "what's the weather"


def test_no_listener_without_a_microphone(monkeypatch):
    import ev_core
    from ev.brain import ToolCall

    assistant = ev_core.EV.__new__(ev_core.EV)
    assistant.mic = None
    monkeypatch.setattr(config, "CANCEL_ENABLED", True)
    assert assistant._start_cancel_watch(ToolCall("terminal_command"), CancelToken()) is None


def test_chat_is_never_watched(monkeypatch):
    """It has no side effect to stop, and it is already streaming its reply."""
    import ev_core
    from ev.brain import ToolCall

    assistant = ev_core.EV.__new__(ev_core.EV)
    assistant.mic = FakeMic()
    monkeypatch.setattr(config, "CANCEL_ENABLED", True)
    assert assistant._start_cancel_watch(ToolCall("chat", {"reply": "hi"}), CancelToken()) is None


def test_cancelling_can_be_switched_off(monkeypatch):
    import ev_core
    from ev.brain import ToolCall

    assistant = ev_core.EV.__new__(ev_core.EV)
    assistant.mic = FakeMic()
    monkeypatch.setattr(config, "CANCEL_ENABLED", False)
    assert assistant._start_cancel_watch(ToolCall("terminal_command"), CancelToken()) is None


# -- speech purity -----------------------------------------------------------
def test_cancellation_speech_carries_no_labels_or_paths(sandbox):
    _seed(sandbox["downloads"], 5)
    spoken = file_manager(
        action="batch_copy",
        path="Downloads",
        destination="Documents",
        cancel=CancelAfter(2),
    ).speech
    assert clean_for_speech(spoken) == spoken
    assert str(sandbox["documents"]) not in spoken
