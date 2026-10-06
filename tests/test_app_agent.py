"""The accessibility route: in-app jobs planned from a tree, not a frame.

Offline. The window list, the tree and the planner are fakes with the real
shapes; nothing reads a real accessibility bus or calls a real model.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

import config  # noqa: E402
from tools import app_agent, computer_use, guard  # noqa: E402
from tools import window as windows  # noqa: E402
from tools.base import CancelToken, ToolResult  # noqa: E402
from tools.desktop import a11y  # noqa: E402
from tools.desktop.model import Element, Tree, WindowInfo  # noqa: E402

MAIL = WindowInfo(9, "Inbox - Mail", "mail", 0, 0, 800, 600, focused=True, pid=5, app="Mail", source="fake")


def _tree(*extra: Element) -> Tree:
    base = [
        Element(1, "push button", "New", actions=("click",)),
        Element(2, "push button", "Bold", actions=("click",)),
        Element(3, "entry", "Subject", actions=("set text",)),
        Element(4, "push button", "Send", actions=("click",)),
        Element(5, "check box", "Dark mode", states=frozenset({"unchecked"}), actions=("toggle",)),
    ]
    return Tree(window=MAIL.title, app=MAIL.app, elements=base + list(extra))


@pytest.fixture
def app(monkeypatch):
    """One readable window, a scripted planner, and a record of what was done."""
    state = {"tree": _tree(), "replies": [], "prompts": [], "acted": []}
    monkeypatch.setattr(config, "A11Y_ENABLED", True)
    monkeypatch.setattr(config, "COMPUTER_CONFIRM_RISKY", True)
    monkeypatch.setattr(config, "COMPUTER_ACTION_PAUSE_S", 0.0)
    monkeypatch.setattr(windows, "list_windows", lambda limit=12: [MAIL])
    monkeypatch.setattr(windows, "foreground", lambda: MAIL)
    monkeypatch.setattr(windows, "exists", lambda w: True)
    monkeypatch.setattr(windows, "find", lambda needle, limit=40: MAIL)
    monkeypatch.setattr(windows, "focus", lambda w: True)
    monkeypatch.setattr(a11y, "available", lambda: True)
    monkeypatch.setattr(a11y, "read", lambda window: state["tree"])

    def act(element, action="press", text=""):
        state["acted"].append((element.label, action, text))
        return True

    monkeypatch.setattr(a11y, "act", act)

    def planner(prompt):
        state["prompts"].append(prompt)
        if not state["replies"]:
            return json.dumps({"mode": "fail", "speech": "out of script"})
        return json.dumps(state["replies"].pop(0))

    monkeypatch.setattr(app_agent, "ask_planner", planner)
    return state


def _changes(state):
    """Make every press visibly change the window, so nothing reads as a stall."""
    counter = {"n": 0}
    original = a11y.act

    def act(element, action="press", text=""):
        counter["n"] += 1
        state["tree"] = _tree(Element(90 + counter["n"], "label", f"changed {counter['n']}"))
        return original(element, action, text)

    return act


# ---------------------------------------------------------------------------
# Planning and acting
# ---------------------------------------------------------------------------
def test_actions_parse_from_strings_and_objects():
    steps = app_agent.parse_steps(
        ["press 12", "set 3 = hello there", {"action": "select", "target": "4", "value": "Large"}, "key ctrl+s"]
    )
    assert [(s.verb, s.target, s.value) for s in steps] == [
        ("press", "12", ""), ("set", "3", "hello there"), ("select", "4", "Large"), ("key", "ctrl+s", ""),
    ]


def test_a_batch_stops_after_anything_that_changes_the_window():
    steps = app_agent.parse_steps(["launch gedit", "set 3 = hello"])
    assert [s.verb for s in steps] == ["launch"]


def test_a_job_is_done_by_ref_with_no_vision(app, monkeypatch):
    monkeypatch.setattr(a11y, "act", _changes(app))
    app["replies"] = [
        {"mode": "act", "actions": ["toggle 5"]},
        {"mode": "done", "speech": "Dark mode's on.", "evidence": "Dark mode checked"},
    ]
    outcome = app_agent.run("turn on dark mode in mail")
    assert outcome.status == "done"
    assert app["acted"] == [("Dark mode", "toggle", "")]
    # The history keeps the label, because the number is gone next round.
    assert 'toggle 5 "Dark mode"' in outcome.history[0]


def test_the_window_is_fenced_as_untrusted(app):
    app["replies"] = [{"mode": "done", "speech": "ok"}]
    app_agent.run("check mail")
    assert "UNTRUSTED WINDOW CONTENT" in app["prompts"][0]
    assert '4 push button "Send"' in app["prompts"][0]


def test_pressing_send_is_held_for_confirmation(app):
    app["replies"] = [{"mode": "act", "actions": ["press 4"]}]
    outcome = app_agent.run("send the draft")
    assert outcome.status == "confirm"
    assert "Send" in outcome.speech
    assert app["acted"] == []


def test_send_is_never_pressed_twice(app, monkeypatch):
    """One message, not four: the ledger refuses the repeat and says so."""
    monkeypatch.setattr(a11y, "act", _changes(app))
    app["replies"] = [
        {"mode": "act", "actions": ["press 4"]},
        {"mode": "act", "actions": ["press 4"]},
        {"mode": "done", "speech": "Sent."},
    ]
    outcome = app_agent.run("send the draft", confirmed=True)
    assert [label for label, _, _ in app["acted"]] == ["Send"]
    assert any("refused a repeat" in line for line in outcome.history)
    assert "ALREADY DONE" in app["prompts"][1]
    assert "refused" in app["prompts"][2]


def test_a_blocked_command_is_never_set_even_when_confirmed(app):
    app["replies"] = [{"mode": "act", "actions": ["set 3 = rm -rf /"]}]
    outcome = app_agent.run("type into the terminal", confirmed=True)
    assert outcome.status == "fail"
    assert app["acted"] == []


def test_actions_that_change_nothing_hand_over_to_vision(app):
    app["replies"] = [{"mode": "act", "actions": ["press 2"]}] * 5
    outcome = app_agent.run("make it bold")
    assert outcome.status == "vision"
    assert "did not change" in outcome.detail
    assert "NOT changed" in app["prompts"][1]


def test_an_unreadable_window_hands_over_to_vision(app):
    app["tree"] = Tree(window="Game", elements=[Element(1, "filler", "")])
    app["replies"] = [{"mode": "act", "actions": ["wait 0"]}]
    outcome = app_agent.run("move the knight")
    assert outcome.status == "vision"
    assert "cannot be read" in app["prompts"][0]


def test_a_sandboxed_window_is_said_to_be_sandboxed(app):
    app["tree"] = Tree(window="Brave", sandboxed=True, error="the sandbox refused")
    app["replies"] = [{"mode": "vision", "why": "sandboxed"}]
    outcome = app_agent.run("read the page")
    assert outcome.status == "vision"
    assert "sandbox" in app["prompts"][0]


def test_the_planner_can_ask_for_vision(app):
    app["replies"] = [{"mode": "vision", "why": "it is a canvas"}]
    assert app_agent.run("draw a circle").status == "vision"


def test_lockdown_stops_the_run(app):
    guard.engage_lockdown("test")
    app["replies"] = [{"mode": "act", "actions": ["press 2"]}]
    outcome = app_agent.run("make it bold")
    assert outcome.status == "stopped"
    assert app["acted"] == []


def test_the_kill_switch_stops_the_run(app):
    token = CancelToken()
    token.cancel()
    assert app_agent.run("make it bold", cancel=token).status == "stopped"


# ---------------------------------------------------------------------------
# Routing inside screen_task
# ---------------------------------------------------------------------------
@pytest.fixture
def screen(monkeypatch):
    monkeypatch.setattr(config, "COMPUTER_USE_ENABLED", True)
    monkeypatch.setattr(config, "VISION_ENABLED", True)
    monkeypatch.setattr(config, "A11Y_ENABLED", True)
    monkeypatch.setattr(app_agent, "available", lambda: True)
    driven: list[dict] = []

    def drive(goal, max_steps, confirmed, cancel, hud, prior=None):
        driven.append({"goal": goal, "prior": prior})
        return ToolResult.success("Vision did it.", "drove it")

    monkeypatch.setattr(computer_use, "_drive_screen", drive)
    return driven


def test_a_readable_app_never_reaches_the_vision_loop(screen, monkeypatch):
    monkeypatch.setattr(
        app_agent, "run", lambda goal, **_: app_agent.AppOutcome("done", "Bold's on.", "ok", ["press 2"])
    )
    result = computer_use.screen_task(task="make it bold")
    assert result.ok and result.speech == "Bold's on."
    assert screen == []


def test_vision_carries_on_from_where_the_tree_stopped(screen, monkeypatch):
    monkeypatch.setattr(
        app_agent, "run", lambda goal, **_: app_agent.AppOutcome("vision", "", "canvas", ["launched paint"])
    )
    result = computer_use.screen_task(task="draw a circle in paint")
    assert result.speech == "Vision did it."
    assert screen == [{"goal": "draw a circle in paint", "prior": ["launched paint"]}]


def test_a_held_step_becomes_a_confirmation_that_replays_the_goal(screen, monkeypatch):
    monkeypatch.setattr(
        app_agent, "run",
        lambda goal, **_: app_agent.AppOutcome("confirm", "Next step sends: Send. Confirm?", "held", [], "sends"),
    )
    result = computer_use.screen_task(task="send the draft")
    assert result.needs_confirmation
    assert result.data["task"] == "send the draft"
    assert screen == []


# ---------------------------------------------------------------------------
# What the planner reads
# ---------------------------------------------------------------------------
def test_the_inventory_drops_lines_the_planner_cannot_use():
    """Measured on GTK 4's Files: a container carrying "view.new-folder" is
    not a control, and an icon and caption inside a list item repeat its
    name. Both used to push real controls past the character budget.
    """
    tree = Tree(window="Files", elements=[
        Element(1, "generic", "", actions=("view.new-folder", "view.select-all")),
        Element(2, "tool bar", ""),
        Element(3, "list item", "Recent Files", depth=1),
        Element(4, "image", "Recent", depth=2),
        Element(5, "label", "Recent", depth=2),
        Element(6, "button", "Back", actions=("click",)),
        Element(7, "dialog", ""),
    ])
    lines = a11y.inventory(tree).splitlines()
    assert lines[0] == "7 dialog"  # a dialog leads, labelled or not
    assert '3 list item "Recent Files"' in lines
    assert '6 button "Back"' in lines
    assert not any(line.startswith(("1 ", "2 ", "4 ", "5 ")) for line in lines)
