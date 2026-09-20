"""Going back for the second half of a compound request.

"Open my mail and give me a summary of the important things" is two jobs in
one sentence. The model answers it with a single tool call - it opens the
mail, and the summary never becomes a call at all. E.V. said "Opening your
mail.", fell silent on the only part the user was waiting for, and went back
to listening. From where they were standing that reads as being ignored.

Two halves to pin down. The splitter has to find that trailing question
*without* finding one in "open Chrome and search for X", where the single
call was correct and a second turn would search twice. And the core loop has
to take the extra turn only when the first half genuinely finished.

Everything here is offline: no network, no microphone, no audio device.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

import config  # noqa: E402
import ev_core  # noqa: E402
from ev.brain import ToolCall  # noqa: E402
from ev.session import split_followup  # noqa: E402
from tools.base import ToolResult  # noqa: E402


# ---------------------------------------------------------------------------
# The splitter
# ---------------------------------------------------------------------------
def test_a_trailing_question_is_found():
    for said, expected in (
        (
            "open my mail and give me a summary of the important things",
            "give me a summary of the important things",
        ),
        ("open my mail and tell me what is important", "tell me what is important"),
        ("open my email and summarise the unread ones", "summarise the unread ones"),
        ("check my calendar and what is on today", "what is on today"),
        (
            "take a screenshot and tell me what is on it",
            "tell me what is on it",
        ),
        (
            "open the drive and then tell me what is in there",
            "tell me what is in there",
        ),
    ):
        assert split_followup(said) == expected, said


def test_an_action_followed_by_an_action_is_left_alone():
    """One call was correct; a second turn would do the thing twice."""
    for said in (
        "open chrome and search for a gaming mouse",
        "open notepad and type hello world",
        "open spotify and play something",
        "open vs code and start claude",
        "copy the invoices to documents and rename them",
    ):
        assert split_followup(said) == "", said


def test_a_single_request_is_never_split():
    for said in (
        "open my downloads folder",
        "what is on my screen",
        "show me my desktop",
        "summarise this page",
        "",
        "and",
    ):
        assert split_followup(said) == "", said


def test_a_trailing_fragment_is_not_a_question():
    """"and go" is not a second job."""
    assert split_followup("open chrome and go") == ""


# ---------------------------------------------------------------------------
# The core loop
# ---------------------------------------------------------------------------
class FakeSpeaker:
    def __init__(self) -> None:
        self.enabled = False  # keeps the streaming path out of the way
        self.said: list[str] = []
        self.speaking = False

    async def say(self, text: str) -> str:
        self.said.append(text)
        return text

    def stop(self) -> None:
        self.speaking = False


class SilentUI:
    """Every `ev.ui` call is a `-> None` draw, so a no-op stands in for all.

    `status` is the one exception: it is used as a context manager, so it
    needs to be one.
    """

    def __init__(self) -> None:
        self.notes: list[str] = []

    def note(self, text: str) -> None:
        self.notes.append(text)

    @contextlib.contextmanager
    def status(self, _label: str):
        yield

    def __getattr__(self, _name):
        def _draw(*_args, **_kwargs):
            return None

        return _draw


class _NullMemory:
    """Stands in for the persisted session clock, which `say` ticks."""

    def touch(self) -> None:
        pass


class ScriptedBrain:
    """Hands back a prepared tool call per turn and records what it was asked."""

    def __init__(self, calls: list[ToolCall]) -> None:
        self.calls = list(calls)
        self.asked: list[tuple[str, str]] = []
        self.session_context = ""

    async def decide(self, transcript, extra_context="", on_sentence=None):
        self.asked.append((transcript, extra_context))
        return self.calls.pop(0) if self.calls else ToolCall("chat", {"reply": "Done."})

    def remember(self, user, assistant, observation="", untrusted=False) -> None:
        pass


def _assistant(monkeypatch, brain: ScriptedBrain, results: list[ToolResult]):
    """An `EV` with no network, no microphone and no audio device."""
    assistant = ev_core.EV.__new__(ev_core.EV)
    assistant.ui = SilentUI()
    assistant.speaker = FakeSpeaker()
    assistant.brain = brain
    assistant.mic = None
    assistant.text_mode = True
    assistant._running = True
    assistant._queued_utterance = None
    assistant._cancel_refused = None
    assistant._pending_followup = ""
    assistant.session = ev_core.Session()
    # `say` touches the persisted session clock. A stub keeps the suite off
    # the developer's real state file.
    assistant.memory = _NullMemory()

    dispatched: list[str] = []
    queue = list(results)

    async def fake_run_tool(call, arguments=None):
        dispatched.append(call.name)
        return queue.pop(0) if queue else ToolResult.success("Done.")

    monkeypatch.setattr(assistant, "_run_tool", fake_run_tool)
    monkeypatch.setattr(assistant, "_log_backlog", lambda *a, **k: None)
    return assistant, dispatched


@pytest.fixture(autouse=True)
def instant_chain(monkeypatch):
    monkeypatch.setattr(config, "CHAIN_ENABLED", True)
    monkeypatch.setattr(config, "CHAIN_SETTLE_S", 0.0)
    monkeypatch.setattr(config, "ACK_ENABLED", False)


def test_the_second_half_gets_its_own_turn(monkeypatch):
    """The bug: the summary was never asked for at all."""
    brain = ScriptedBrain(
        [
            ToolCall("web_search", {"engine": "mail"}),
            ToolCall("take_screenshot", {"question": "summarise the inbox"}),
        ]
    )
    assistant, dispatched = _assistant(
        monkeypatch,
        brain,
        [
            ToolResult.success("Opening your mail."),
            ToolResult.success("Three job alerts and a design brief."),
        ],
    )

    asyncio.run(
        assistant.handle("open my mail and give me a summary of the important things")
    )

    assert dispatched == ["web_search", "take_screenshot"]
    assert assistant.speaker.said == [
        "Opening your mail.",
        "Three job alerts and a design brief.",
    ]
    # The second turn asks only the remaining half, and is told the first one
    # already ran - without that the model reopens the mail.
    assert brain.asked[1][0] == "give me a summary of the important things"
    assert "already been carried out" in brain.asked[1][1]


def test_a_single_request_takes_exactly_one_turn(monkeypatch):
    brain = ScriptedBrain([ToolCall("open_app", {"app": "notepad"})])
    assistant, dispatched = _assistant(
        monkeypatch, brain, [ToolResult.success("Notepad's up.")]
    )

    asyncio.run(assistant.handle("open notepad"))

    assert dispatched == ["open_app"]
    assert len(brain.asked) == 1


def test_a_follow_up_cannot_spawn_another(monkeypatch):
    """Depth one, always. Otherwise this is a loop with no ceiling."""
    brain = ScriptedBrain(
        [
            ToolCall("web_search", {"engine": "mail"}),
            ToolCall("take_screenshot", {"question": "read it"}),
            ToolCall("take_screenshot", {"question": "again"}),
        ]
    )
    assistant, dispatched = _assistant(
        monkeypatch,
        brain,
        [
            ToolResult.success("Opening your mail."),
            # A second compound-looking answer must not chain again.
            ToolResult.success("open the drive and tell me what is in it"),
        ],
    )

    asyncio.run(assistant.handle("open my mail and tell me what is important"))
    assert dispatched == ["web_search", "take_screenshot"]


def test_a_failed_first_half_stops_the_chain(monkeypatch):
    """No point summarising an inbox that never opened."""
    brain = ScriptedBrain([ToolCall("web_search", {"engine": "mail"})])
    assistant, dispatched = _assistant(
        monkeypatch, brain, [ToolResult.failure("Couldn't get a browser open.")]
    )

    asyncio.run(assistant.handle("open my mail and tell me what is important"))
    assert dispatched == ["web_search"]


def test_a_cancelled_first_half_stops_the_chain(monkeypatch):
    brain = ScriptedBrain([ToolCall("file_manager", {"action": "organize"})])
    assistant, dispatched = _assistant(
        monkeypatch, brain, [ToolResult.stopped("Stopped.")]
    )

    asyncio.run(assistant.handle("tidy my downloads and tell me what moved"))
    assert dispatched == ["file_manager"]


def test_chaining_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "CHAIN_ENABLED", False)
    brain = ScriptedBrain([ToolCall("web_search", {"engine": "mail"})])
    assistant, dispatched = _assistant(
        monkeypatch, brain, [ToolResult.success("Opening your mail.")]
    )

    asyncio.run(assistant.handle("open my mail and tell me what is important"))
    assert dispatched == ["web_search"]


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------
def test_a_confirmation_holds_the_question_until_after_the_yes(monkeypatch):
    """"Tidy my desktop and tell me what moved" still answers after a yes."""
    brain = ScriptedBrain(
        [
            ToolCall("file_manager", {"action": "organize", "path": "desktop"}),
            ToolCall("chat", {"reply": "Twelve files into four folders."}),
        ]
    )
    assistant, dispatched = _assistant(
        monkeypatch,
        brain,
        [
            ToolResult.confirm("That'll reorganise Desktop. Sure?", action="organize"),
            ToolResult.success("Sorted."),
            ToolResult.success("Twelve files into four folders."),
        ],
    )

    async def scenario():
        await assistant.handle("tidy my desktop and tell me what moved")
        # Held, not lost, while the question is outstanding.
        assert assistant._pending_followup == "tell me what moved"
        await assistant._resolve_pending("yes")

    asyncio.run(scenario())
    assert dispatched == ["file_manager", "file_manager", "chat"]
    assert assistant._pending_followup == ""


def test_a_declined_confirmation_drops_the_question(monkeypatch):
    """Saying no ends the whole request, not just its first half."""
    brain = ScriptedBrain(
        [ToolCall("file_manager", {"action": "organize", "path": "desktop"})]
    )
    assistant, dispatched = _assistant(
        monkeypatch,
        brain,
        [ToolResult.confirm("That'll reorganise Desktop. Sure?", action="organize")],
    )

    async def scenario():
        await assistant.handle("tidy my desktop and tell me what moved")
        await assistant._resolve_pending("no")

    asyncio.run(scenario())
    assert dispatched == ["file_manager"]
    assert assistant._pending_followup == ""
