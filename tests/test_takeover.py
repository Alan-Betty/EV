"""The overlay goes up whenever E.V. has the screen, and only once.

Nothing here draws anything or registers a hotkey: `conftest.py` turns both
off, and `Takeover` is replaced with a recorder where the test needs to see
what would have been drawn.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import config  # noqa: E402
from tools import overlay  # noqa: E402
from tools.base import CancelToken, ToolResult  # noqa: E402


def _record(monkeypatch) -> list:
    """Swap in a Takeover that remembers each one that was put up."""
    raised: list = []

    class Recorded(overlay.Takeover):
        def __enter__(self):
            raised.append(self)
            return super().__enter__()

    monkeypatch.setattr(overlay, "Takeover", Recorded)
    return raised


def test_a_nested_takeover_shares_the_outer_one(monkeypatch):
    """A mission calling screen_task must not stack a second frame."""
    raised = _record(monkeypatch)
    with overlay.taking_over("find a mouse") as outer:
        with overlay.taking_over("click the search box") as inner:
            assert inner is outer
    assert len(raised) == 1
    assert overlay.active() is None


def test_the_kill_switch_cancels_the_run_and_locks_down(monkeypatch):
    from tools import guard

    monkeypatch.setattr(config, "AGENT_KILL_LOCKS_DOWN", True)
    token = CancelToken()
    with overlay.taking_over("drive the screen", token) as hud:
        hud._fire()
    assert token.cancelled
    assert guard.is_locked_down()


def test_screen_task_puts_the_overlay_up(monkeypatch):
    from tools import computer_use

    raised = _record(monkeypatch)
    monkeypatch.setattr(config, "COMPUTER_USE_ENABLED", True)
    monkeypatch.setattr(config, "VISION_ENABLED", True)

    def drive(goal, max_steps, confirmed, cancel, hud):
        assert overlay.active() is hud
        return ToolResult.success("Done.", "drove it")

    monkeypatch.setattr(computer_use, "_drive_screen", drive)
    result = computer_use.screen_task(task="open notepad and type hello")
    assert result.ok
    assert [hud.goal for hud in raised] == ["open notepad and type hello"]


def test_a_visible_browser_run_puts_the_overlay_up_and_a_headless_one_does_not(
    monkeypatch,
):
    from tools import browser_automation, web_agent

    raised = _record(monkeypatch)
    monkeypatch.setattr(config, "BROWSER_AUTOMATION_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_HEADLESS", False)
    monkeypatch.setattr(
        web_agent, "web_mission",
        lambda goal, **kwargs: web_agent.WebOutcome("done", "Playing.", "played it"),
    )

    browser_automation.browser_task(task="play lofi on youtube")
    browser_automation.browser_task(task="play lofi on youtube", headless=True)
    assert len(raised) == 1
