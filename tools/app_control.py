"""`app_control`: switch to, close, quit, minimise and operate any application.

`open_app` could start a program and nothing could do anything to it
afterwards except the vision loop - a screenshot, a guessed coordinate, and
alt+F4 if the model thought of it. Closing a window is not a vision
problem: the window manager knows exactly which windows exist and will
close one when asked. Pressing "Save" is not a vision problem either when
the application publishes an accessibility tree that says where Save is
and how to press it.

So this tool never calls a model. It resolves the application by name
against the live window list, then acts through the OS (`tools.window`) or
the accessibility tree (`tools.desktop.a11y`), and verifies the result by
looking again. In-app jobs that need judgement - "turn on dark mode in
settings" - are `screen_task`'s, which plans over the same tree.

Three rules shape the close path, all from "graceful + confirm":

* **A close is always polite.** It is the title bar's X, so an editor with
  unsaved work gets to ask its own question.
* **That question belongs to the user.** If one appears, E.V. reads it out
  - what it says and which buttons it offers - and stops. It never answers
  "Save changes?" on the user's behalf.
* **Losing work takes a strict yes.** `discard` (close without saving) and
  `kill` are held as "discards unsaved work" / "force quits a program",
  both in `safety.HIGH_RISK_REASONS`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import config
from tools import window as windows
from tools.base import ToolResult
from tools.desktop import a11y
from tools.desktop.model import Element, Tree, WindowInfo
from tools.safety import classify, classify_gui

log = logging.getLogger("ev.tools.app_control")

ACTIONS = ("list", "focus", "close", "quit", "minimize", "maximize", "kill", "read", "press", "set_text", "menu")

# Actions that only look. `guard` lets these through a lockdown, on the same
# argument that keeps take_screenshot working: an assistant that cannot say
# what is on screen while locked down is not safer, just broken.
READ_ONLY_ACTIONS = frozenset({"list", "read"})

_ALIASES = {
    "switch": "focus", "switch_to": "focus", "activate": "focus", "bring_up": "focus", "show": "focus",
    "raise": "focus", "go_to": "focus",
    "exit": "quit", "close_all": "quit",
    "minimise": "minimize", "hide": "minimize", "maximise": "maximize", "fullscreen": "maximize",
    "force_quit": "kill", "terminate": "kill", "end_task": "kill",
    "click": "press", "toggle": "press", "select": "press",
    "type": "set_text", "fill": "set_text", "write": "set_text", "set": "set_text",
    "inspect": "read", "look": "read",
    "windows": "list", "list_windows": "list", "apps": "list",
}

# The buttons a "Save changes?" dialog offers for "lose the changes", across
# the toolkits E.V. is likely to meet.
_DISCARD_BUTTONS = (
    "don't save", "dont save", "do not save", "discard", "close without saving",
    "don't save changes", "close without save", "no",
)


def verb_of(action: str) -> str:
    raw = (action or "list").strip().lower().replace("-", "_").replace(" ", "_")
    return _ALIASES.get(raw, raw)


def _name(window: WindowInfo) -> str:
    """How to say a window out loud: the app's name, not a 90-character title.

    App ids are written for machines - "gnome-text-editor",
    "org.gnome.Nautilus", "TextEditor" - and are said the way a person
    would: "Text Editor", "Nautilus".
    """
    if window.app and len(window.app) <= 40:
        app = window.app
        if "." in app and " " not in app:
            app = app.rsplit(".", 1)[-1]  # reverse-DNS id: keep the last part
        app = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", app)
        words = app.replace("_", " ").replace("-", " ").split()
        # Snap window classes repeat themselves: "firefox_firefox".
        app = " ".join(w for i, w in enumerate(words) if i == 0 or w.lower() != words[i - 1].lower())
        app = re.sub(r"(?i)^(gnome|kde|org)\s+", "", app) or app
        return app.title() if app.islower() else app
    title = window.title.split(" - ")[-1].strip() if " - " in window.title else window.title
    return title[:40] or "that window"


def _target(app: str) -> WindowInfo | None:
    if (app or "").strip():
        return windows.find(app)
    return windows.foreground()


def _not_found(app: str) -> ToolResult:
    return ToolResult.failure(
        f"I can't see {app or 'that'} open.",
        f"No open window matches {app!r}. Use app_control action 'list' to see what is open, "
        "or open_app to start it.",
    )


# ---------------------------------------------------------------------------
# Window verbs
# ---------------------------------------------------------------------------
def _list() -> ToolResult:
    found = windows.list_windows(limit=20)
    if not found:
        return ToolResult.failure(
            "I can't see any windows from here.",
            "No window backend returned anything on this desktop (see --check).",
        )
    names: list[str] = []
    for item in found:
        spoken = _name(item)
        if spoken not in names:
            names.append(spoken)
    if len(names) == 1:
        speech = f"Just {names[0]}."
    else:
        shown = names[:7]
        speech = f"{', '.join(shown[:-1])} and {shown[-1]}."
    from tools.computer_use import screen_size

    width, height = screen_size(passive=True)
    return ToolResult.success(
        speech, "Open windows, focused first:\n" + windows.describe_windows(width, height, limit=20)
    )


def _focus(app: str) -> ToolResult:
    target = windows.find(app)
    if target is None:
        # "Switch to Spotify" when Spotify is not running means "open it".
        from tools.app_launcher import open_app

        launched = open_app(app=app)
        if launched.ok:
            launched.detail = f"{app} was not open, so it was launched. {launched.detail}"
        return launched
    if target.focused and not target.minimized:
        return ToolResult.success(f"{_name(target)}'s already up.", f"'{target.title}' already has focus.")
    if windows.focus(target):
        return ToolResult.success(f"{_name(target)}.", f"Brought '{target.title}' to the front.")
    return ToolResult.failure(
        f"{_name(target)} wouldn't come forward.",
        f"Focusing '{target.title}' ({target.source}) did not take. On GNOME Wayland, installing E.V.'s "
        "Shell extension (--install-gnome-extension) makes focus reliable.",
    )


def _resize(app: str, verb: str) -> ToolResult:
    target = _target(app)
    if target is None:
        return _not_found(app)
    done = windows.minimize(target) if verb == "minimize" else windows.maximize(target)
    if done:
        return ToolResult.success("Done.", f"{verb.capitalize()}d '{target.title}'.")
    return ToolResult.failure(f"{_name(target)} wouldn't {verb}.", f"{verb} of '{target.title}' failed.")


def _press_discard(outcome: windows.CloseOutcome) -> bool:
    """Click the dialog's "Don't save" button. Only after a strict yes."""
    tree = a11y.read(outcome.dialog or outcome.window)
    for label in _DISCARD_BUTTONS:
        button = a11y.find(tree, label, frozenset({"push button", "button"}))
        if button is not None and a11y.act(button, "press"):
            return True
    return False


def _close(app: str, discard: bool, confirmed: bool) -> ToolResult:
    target = _target(app)
    if target is None:
        return _not_found(app)
    spoken = _name(target)
    if discard and not confirmed:
        # The yes replays these arguments. "The focused window" by then may
        # be E.V.'s own terminal, so the window is pinned by its title.
        return ToolResult.confirm(
            f"Close {spoken} without saving? Confirm?",
            f"Awaiting confirmation: close '{target.title}' and discard unsaved work.",
            reason="discards unsaved work", action="close", app=app or target.title, discard=True,
        )

    outcome = windows.close_and_verify(target)
    if outcome.status == "closed":
        return ToolResult.success(f"{spoken}'s closed.", f"Closed '{target.title}'.")
    if outcome.status == "dialog":
        if discard and confirmed and _press_discard(outcome) and not windows.exists(target):
            return ToolResult.success(
                f"Closed {spoken} without saving.", f"Discarded the changes and closed '{target.title}'."
            )
        buttons = ", ".join(outcome.buttons) or "unknown buttons"
        question = f' It says: "{outcome.text}".' if outcome.text else ""
        return ToolResult.failure(
            f"{spoken} wants to know about unsaved changes first. Save, don't save, or cancel?",
            f"Closing '{target.title}' raised a dialog.{question} Buttons: {buttons}. "
            "Ask the user. To save: app_control press with target the save button's label. "
            "To lose the changes: app_control close with discard true (asks for confirmation).",
            buttons=list(outcome.buttons),
        )
    if outcome.status == "refused":
        return ToolResult.failure(
            f"{spoken} wouldn't take the close request.",
            f"The close request for '{target.title}' failed: {outcome.text or 'the desktop refused it'}.",
        )
    return ToolResult.failure(
        f"{spoken} is still open.",
        f"'{target.title}' ignored the close request and showed no dialog. It may be busy; "
        "app_control kill ends it, after the user confirms.",
    )


def _quit(app: str) -> ToolResult:
    if app:
        every = windows.app_windows(app)
    else:
        front = windows.foreground()
        every = [front] if front else []
    if not every:
        return _not_found(app)
    spoken = _name(every[0])
    from tools.desktop import system

    if system.IS_MAC:
        from tools.desktop import macos

        macos.quit_app(every[0].app)
        return ToolResult.success(f"Quit {spoken}.", f"Asked {every[0].app} to quit.")
    closed = 0
    for item in every:
        outcome = windows.close_and_verify(item)
        if outcome.status == "closed":
            closed += 1
        elif outcome.status == "dialog":
            return ToolResult.failure(
                f"{spoken} wants to know about unsaved changes first. Save, don't save, or cancel?",
                f"Quitting {spoken} stopped at a dialog in '{item.title}'. Buttons: "
                f"{', '.join(outcome.buttons) or 'unknown'}. Closed {closed} window(s) before it. Ask the user.",
                buttons=list(outcome.buttons),
            )
    if closed == len(every):
        return ToolResult.success(f"{spoken}'s closed.", f"Closed all {closed} window(s) of {spoken}.")
    return ToolResult.failure(
        f"Some of {spoken} is still open.", f"Closed {closed} of {len(every)} window(s) of {spoken}."
    )


def _kill(app: str, confirmed: bool) -> ToolResult:
    target = _target(app)
    if target is None:
        return _not_found(app)
    spoken = _name(target)
    if not target.pid:
        return ToolResult.failure(
            f"I can't tell which process {spoken} is.", f"No process id is known for '{target.title}'."
        )
    if not confirmed:
        return ToolResult.confirm(
            f"Force quit {spoken}? Anything unsaved is lost. Confirm?",
            f"Awaiting confirmation: kill process {target.pid} ('{target.title}').",
            reason="force quits a program", action="kill", app=app or target.title,
        )
    if windows.kill_pid(target.pid):
        return ToolResult.success(f"{spoken}'s been force quit.", f"Ended process {target.pid} ('{target.title}').")
    return ToolResult.failure(
        f"I couldn't force quit {spoken}.", f"Ending process {target.pid} failed (permissions?)."
    )


# ---------------------------------------------------------------------------
# Inside the window
# ---------------------------------------------------------------------------
def _tree_for(app: str) -> tuple[WindowInfo | None, Tree | None, ToolResult | None]:
    target = _target(app)
    if target is None:
        return None, None, _not_found(app)
    if not a11y.available():
        return target, None, ToolResult.failure(
            "I can't read inside apps here.",
            "No accessibility API is available (Linux: pip install jeepney; Windows: pip install "
            "comtypes). Use screen_task, which can look at the screen instead.",
        )
    tree = a11y.read(target)
    if tree.sandboxed:
        return target, tree, ToolResult.failure(
            f"{_name(target)} won't let me read it.", f"{tree.error}. Use screen_task for this window."
        )
    if not tree.elements:
        reason = f" ({tree.error})" if tree.error else ""
        return target, tree, ToolResult.failure(
            f"{_name(target)} isn't telling me what's in it.",
            f"'{target.title}' published no accessible elements{reason}. Use screen_task for this window.",
        )
    return target, tree, None


def _element(tree: Tree, target: str) -> Element | None:
    ref = a11y.parse_ref(target)
    if ref is not None:
        return tree.by_ref(ref)
    return a11y.find(tree, target)


def _gate(description: str, confirmed: bool, speech: str, **data: Any) -> ToolResult | None:
    if confirmed or not config.COMPUTER_CONFIRM_RISKY:
        return None
    verdict = classify_gui(description)
    if not verdict.needs_confirmation:
        return None
    return ToolResult.confirm(
        speech, f"Awaiting confirmation: {description} ({verdict.reason}).", reason=verdict.reason, **data
    )


def _read(app: str) -> ToolResult:
    target, tree, problem = _tree_for(app)
    if problem is not None:
        return problem
    assert target is not None and tree is not None
    return ToolResult.success(
        f"I can see {len(tree.elements)} things in {_name(target)}.",
        f"Contents of '{target.title}' (numbers are refs for press and set_text, and change on every "
        f"read):\n{a11y.inventory(tree)}",
    )


def _press(app: str, wanted: str, confirmed: bool) -> ToolResult:
    if not wanted.strip():
        return ToolResult.failure("Press what?", "app_control press needs target: a ref number or a label.")
    window, tree, problem = _tree_for(app)
    if problem is not None:
        return problem
    assert window is not None and tree is not None
    element = _element(tree, wanted)
    if element is None:
        return ToolResult.failure(
            f"I can't find {wanted} in {_name(window)}.",
            f"No element matches {wanted!r}. Read the window first (action 'read') and use a ref.",
        )
    if not element.enabled:
        return ToolResult.failure(f"{element.label or 'That'} is greyed out.", f"{element.describe()} is disabled.")
    held = _gate(
        f"press {element.label} in {window.title}", confirmed,
        f"That presses {element.label or 'it'} for real. Confirm?",
        action="press", app=app or window.title, target=wanted,
    )
    if held is not None:
        return held
    try:
        ok = a11y.act(element, "press")
    except Exception as exc:
        ok = False
        log.info("Pressing %s failed: %s", element.describe(), exc)
    if ok:
        return ToolResult.success("Done.", f"Pressed {element.role} \"{element.label}\" in '{window.title}'.")
    return ToolResult.failure(
        f"{element.label or 'It'} didn't respond.",
        f"The accessibility action on {element.describe()} failed. screen_task can click it instead.",
    )


def _set_text(app: str, wanted: str, text: str, confirmed: bool) -> ToolResult:
    shell = classify(text)
    if shell.is_blocked:
        # Text set into a terminal's input is a command, whichever route it took.
        return ToolResult.failure(
            "Not typing that one.", f"Refused to enter a blocked command ({shell.reason}). Do not retry."
        )
    window, tree, problem = _tree_for(app)
    if problem is not None:
        return problem
    assert window is not None and tree is not None
    if wanted.strip():
        element = _element(tree, wanted)
    else:
        editable = [e for e in tree.elements if "set text" in e.actions]
        element = next((e for e in editable if "focused" in e.states), editable[0] if editable else None)
    if element is None or "set text" not in element.actions:
        return ToolResult.failure(
            f"I can't find a text field for that in {_name(window)}.",
            f"No editable element matches {wanted!r}. Read the window and pass the field's ref.",
        )
    held = _gate(
        f"{element.label} {text}", confirmed, f"That types into {element.label or 'the field'} for real. Confirm?",
        action="set_text", app=app or window.title, target=wanted, text=text,
    )
    if held is not None:
        return held
    try:
        ok = a11y.act(element, "set", text)
    except Exception as exc:
        ok = False
        log.info("Setting text on %s failed: %s", element.describe(), exc)
    shown = text if len(text) <= 60 else text[:57] + "..."
    if ok:
        return ToolResult.success("Typed.", f"Set {element.role} \"{element.label}\" in '{window.title}' to: {shown}")
    return ToolResult.failure(
        "That field wouldn't take the text.",
        f"Setting the text of {element.describe()} failed. keyboard_action can type it after focusing the field.",
    )


def _menu(app: str, path_text: str, confirmed: bool) -> ToolResult:
    path = [part.strip() for part in re.split(r"\s*(?:->|>|/|→)\s*", path_text or "") if part.strip()]
    if not path:
        return ToolResult.failure("Which menu?", "app_control menu needs target like 'File > Save As'.")
    window = _target(app)
    if window is None:
        return _not_found(app)
    held = _gate(
        " ".join(path), confirmed, f"That picks {path[-1]} from the menu for real. Confirm?",
        action="menu", app=app or window.title, target=path_text,
    )
    if held is not None:
        return held
    ok, what = a11y.menu(window, path)
    if ok:
        return ToolResult.success("Done.", f"Chose {what} in '{window.title}'.")
    return ToolResult.failure(
        f"I couldn't find {path[-1]} in the menus.",
        f"Menu path {' > '.join(path)} failed in '{window.title}': {what}. Many apps hide menus behind a "
        "hamburger button; read the window to find it, or use screen_task.",
    )


# ---------------------------------------------------------------------------
def app_control(
    action: str = "list",
    app: str = "",
    target: str = "",
    text: str = "",
    discard: Any = False,
    confirmed: bool = False,
    **_: object,
) -> ToolResult:
    """Switch to, close, quit, minimise, maximise, kill, read or operate an app."""
    if not getattr(config, "APP_CONTROL_ENABLED", True):
        return ToolResult.failure("App control is switched off.", "EV_APP_CONTROL_ENABLED is false.")
    verb = verb_of(action)
    app = str(app or "").strip()
    discard = discard is True or str(discard).strip().lower() in {"true", "1", "yes"}
    try:
        if verb == "list":
            return _list()
        if verb == "focus":
            if not app:
                return ToolResult.failure("Switch to what?", "app_control focus needs app.")
            return _focus(app)
        if verb in {"minimize", "maximize"}:
            return _resize(app, verb)
        if verb == "close":
            return _close(app, discard, confirmed)
        if verb == "quit":
            return _quit(app)
        if verb == "kill":
            return _kill(app, confirmed)
        if verb == "read":
            return _read(app)
        if verb == "press":
            return _press(app, str(target or text or ""), confirmed)
        if verb == "set_text":
            return _set_text(app, str(target or ""), str(text or ""), confirmed)
        if verb == "menu":
            return _menu(app, str(target or text or ""), confirmed)
    except Exception as exc:  # a backend failing mid-call costs this call, not E.V.
        log.exception("app_control %s failed", verb)
        return ToolResult.failure("That didn't work.", f"app_control {verb} failed: {exc}")
    return ToolResult.failure(
        "I don't know that one.", f"Unknown app_control action {action!r}. Valid: {', '.join(ACTIONS)}."
    )
