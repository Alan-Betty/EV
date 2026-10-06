"""`app_control`: switching, closing and operating applications.

Offline. The window list, the close and the accessibility tree are all
fakes with the real shapes, so nothing here touches a real desktop - which
matters more than usual, because the thing under test closes programs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

from tools.app_control import verb_of  # noqa: E402
from tools import dispatch, from_model, guard  # noqa: E402
from tools import window as windows  # noqa: E402
from tools.base import ToolResult  # noqa: E402
from tools.desktop import a11y  # noqa: E402
from tools.desktop.model import Element, Tree, WindowInfo  # noqa: E402
from tools.safety import HIGH_RISK_REASONS  # noqa: E402
from tools.schemas import select_tools  # noqa: E402

EDITOR = WindowInfo(
    101, "notes.txt - Text Editor", "gnome-text-editor", 0, 0, 800, 600,
    focused=True, pid=4242, app="gnome-text-editor", source="fake",
)
SPOTIFY = WindowInfo(202, "Spotify Premium", "spotify", 0, 0, 800, 600, pid=77, app="Spotify", source="fake")


@pytest.fixture
def desktop(monkeypatch):
    """Two open windows and a record of everything done to them."""
    calls: dict[str, list] = {"focus": [], "close": [], "kill": [], "act": [], "launch": []}
    open_now = [EDITOR, SPOTIFY]

    def find(needle: str, limit: int = 40):
        needle = needle.lower()
        return next(
            (w for w in open_now if needle in w.title.lower() or needle in w.app.lower()), None
        )

    monkeypatch.setattr(windows, "list_windows", lambda limit=12: list(open_now))
    monkeypatch.setattr(windows, "find", find)
    monkeypatch.setattr(windows, "foreground", lambda: next((w for w in open_now if w.focused), None))
    monkeypatch.setattr(windows, "app_windows", lambda app: [w for w in open_now if w is find(app)])
    monkeypatch.setattr(windows, "focus", lambda w: calls["focus"].append(w.title) or True)
    monkeypatch.setattr(windows, "exists", lambda w: w in open_now)
    monkeypatch.setattr(windows, "kill_pid", lambda pid: calls["kill"].append(pid) or True)
    monkeypatch.setattr(windows, "describe_windows", lambda *a, **k: "- Text Editor\n- Spotify")
    from tools import computer_use

    monkeypatch.setattr(computer_use, "screen_size", lambda passive=False: (1920, 1080))

    def launch(app: str = "", **_):
        calls["launch"].append(app)
        return ToolResult.success(f"{app}'s up.", f"Launched {app}.")

    from tools import app_launcher

    monkeypatch.setattr(app_launcher, "open_app", launch)
    monkeypatch.setattr(a11y, "available", lambda: True)

    def act(element, action="press", text=""):
        calls["act"].append((element.label, action, text))
        return True

    monkeypatch.setattr(a11y, "act", act)
    return calls, open_now


def _closes(monkeypatch, open_now, status="closed", buttons=(), text=""):
    """`close_and_verify` that removes the window, or leaves it asking."""

    def close(window, wait_s=None):
        if status == "closed":
            open_now.remove(window)
        return windows.CloseOutcome(status, window, buttons=tuple(buttons), text=text)

    monkeypatch.setattr(windows, "close_and_verify", close)


def _tree(*elements: Element, sandboxed: bool = False, error: str = "") -> Tree:
    return Tree(window=EDITOR.title, app=EDITOR.app, elements=list(elements), sandboxed=sandboxed, error=error)


# ---------------------------------------------------------------------------
# Window verbs
# ---------------------------------------------------------------------------
def test_list_names_what_is_open(desktop):
    result = dispatch("app_control", {"action": "list"})
    assert result.ok
    assert "Text Editor" in result.speech and "Spotify" in result.speech
    # Window titles are whatever a web page or a document called itself.
    assert result.untrusted


def test_spoken_synonyms_reach_the_right_verb():
    assert verb_of("switch to") == "focus"
    assert verb_of("minimise") == "minimize"
    assert verb_of("force quit") == "kill"
    assert verb_of("exit") == "quit"


def test_focus_brings_a_window_forward(desktop):
    calls, _ = desktop
    result = dispatch("app_control", {"action": "focus", "app": "spotify"})
    assert result.ok
    assert calls["focus"] == [SPOTIFY.title]


def test_switching_to_something_closed_opens_it(desktop):
    calls, _ = desktop
    result = dispatch("app_control", {"action": "switch", "app": "calculator"})
    assert result.ok
    assert calls["launch"] == ["calculator"]
    assert "was not open" in result.detail


def test_a_clean_close_is_verified_and_reported(desktop, monkeypatch):
    calls, open_now = desktop
    _closes(monkeypatch, open_now)
    result = dispatch("app_control", {"action": "close", "app": "spotify"})
    assert result.ok
    assert SPOTIFY not in open_now


def test_a_save_dialog_stops_the_close_and_asks_the_user(desktop, monkeypatch):
    calls, open_now = desktop
    _closes(monkeypatch, open_now, "dialog", buttons=("Cancel", "Discard", "Save"), text="Save changes?")
    result = dispatch("app_control", {"action": "close", "app": "text editor"})
    assert not result.ok
    assert "unsaved changes" in result.speech
    assert "Discard" in result.detail and "Save" in result.detail
    # The question belongs to the user: nothing was pressed on their behalf.
    assert calls["act"] == []


def test_closing_without_saving_needs_a_strict_yes(desktop):
    result = dispatch("app_control", {"action": "close", "discard": True})
    assert result.needs_confirmation
    assert result.data["reason"] in HIGH_RISK_REASONS
    # Replayed after the yes, "the focused window" might be E.V.'s own
    # terminal - so the window is pinned by its title.
    assert result.data["app"] == EDITOR.title


def test_the_model_cannot_confirm_its_own_discard(desktop, monkeypatch):
    calls, open_now = desktop
    _closes(monkeypatch, open_now, "dialog", buttons=("Don't Save",))
    result = dispatch("app_control", from_model({"action": "close", "app": "editor", "discard": True, "confirmed": True}))
    assert result.needs_confirmation
    assert calls["act"] == []


def test_a_confirmed_discard_presses_dont_save(desktop, monkeypatch):
    calls, open_now = desktop
    _closes(monkeypatch, open_now, "dialog", buttons=("Cancel", "Don't Save", "Save"))
    dialog = _tree(
        Element(1, "dialog", "Save changes?"),
        Element(2, "push button", "Cancel", depth=1),
        Element(3, "push button", "Don't Save", depth=1),
        Element(4, "push button", "Save", depth=1),
    )

    def act(element, action="press", text=""):
        calls["act"].append(element.label)
        open_now.remove(EDITOR)
        return True

    monkeypatch.setattr(a11y, "read", lambda window: dialog)
    monkeypatch.setattr(a11y, "act", act)
    result = dispatch("app_control", {"action": "close", "app": "editor", "discard": True, "confirmed": True})
    assert result.ok, result.detail
    assert calls["act"] == ["Don't Save"]


def test_kill_is_held_for_confirmation_then_ends_the_process(desktop):
    calls, _ = desktop
    held = dispatch("app_control", {"action": "kill", "app": "spotify"})
    assert held.needs_confirmation
    assert held.data["reason"] in HIGH_RISK_REASONS
    assert calls["kill"] == []
    done = dispatch("app_control", {**held.data, "confirmed": True})
    assert done.ok
    assert calls["kill"] == [SPOTIFY.pid]


def test_an_unknown_app_is_reported_not_guessed(desktop):
    result = dispatch("app_control", {"action": "close", "app": "photoshop"})
    assert not result.ok
    assert "list" in result.detail


# ---------------------------------------------------------------------------
# Inside the window
# ---------------------------------------------------------------------------
def test_read_lists_numbered_elements(desktop, monkeypatch):
    tree = _tree(Element(1, "push button", "Open"), Element(2, "text", "Search", value="foo"))
    monkeypatch.setattr(a11y, "read", lambda window: tree)
    result = dispatch("app_control", {"action": "read", "app": "editor"})
    assert result.ok
    assert '1 push button "Open"' in result.detail
    assert result.untrusted


def test_a_sandboxed_app_is_reported_never_read_as_empty(desktop, monkeypatch):
    monkeypatch.setattr(a11y, "read", lambda window: _tree(sandboxed=True, error="AppArmor refused"))
    result = dispatch("app_control", {"action": "read", "app": "editor"})
    assert not result.ok
    assert "won't let me" in result.speech
    assert "screen_task" in result.detail


def test_press_by_ref_uses_the_accessibility_action(desktop, monkeypatch):
    calls, _ = desktop
    tree = _tree(Element(1, "push button", "Open"), Element(2, "push button", "Bold"))
    monkeypatch.setattr(a11y, "read", lambda window: tree)
    result = dispatch("app_control", {"action": "press", "app": "editor", "target": "2"})
    assert result.ok
    assert calls["act"] == [("Bold", "press", "")]


def test_pressing_send_asks_first(desktop, monkeypatch):
    calls, _ = desktop
    monkeypatch.setattr(a11y, "read", lambda window: _tree(Element(1, "push button", "Send")))
    result = dispatch("app_control", {"action": "press", "app": "editor", "target": "Send"})
    assert result.needs_confirmation
    assert calls["act"] == []


def test_set_text_meets_the_blocked_command_list(desktop, monkeypatch):
    calls, _ = desktop
    field = Element(1, "terminal", "Terminal", actions=("set text",))
    monkeypatch.setattr(a11y, "read", lambda window: _tree(field))
    result = dispatch("app_control", {"action": "set_text", "app": "editor", "text": "rm -rf /"})
    assert not result.ok
    assert calls["act"] == []


def test_set_text_fills_the_focused_field(desktop, monkeypatch):
    calls, _ = desktop
    tree = _tree(
        Element(1, "entry", "Name", actions=("set text",)),
        Element(2, "entry", "Note", states=frozenset({"focused"}), actions=("set text",)),
    )
    monkeypatch.setattr(a11y, "read", lambda window: tree)
    result = dispatch("app_control", {"action": "set_text", "app": "editor", "text": "milk"})
    assert result.ok
    assert calls["act"] == [("Note", "set", "milk")]


# ---------------------------------------------------------------------------
# Guards and routing
# ---------------------------------------------------------------------------
def test_lockdown_still_answers_what_is_open(desktop, monkeypatch):
    calls, open_now = desktop
    _closes(monkeypatch, open_now)
    guard.engage_lockdown("test")
    assert dispatch("app_control", {"action": "list"}).ok
    refused = dispatch("app_control", {"action": "close", "app": "spotify"})
    assert not refused.ok
    assert SPOTIFY in open_now


def test_closing_a_program_does_not_pull_in_the_screen_family():
    chosen = select_tools("close spotify")
    assert "app_control" in chosen
    assert "screen_task" not in chosen
    assert "app_control" in select_tools("what apps are open")
    assert "app_control" in select_tools("switch to firefox")
