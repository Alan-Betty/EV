"""macOS windows and accessibility through `osascript` (JavaScript for Automation).

Best effort, and labelled as such: this backend is written against System
Events' documented scripting dictionary but has not run on hardware in this
project's test loop. It needs no package - `osascript` ships with every Mac
- and the one thing it does need is the Accessibility permission for
whatever runs E.V. (Terminal, iTerm, VS Code) under System Settings ->
Privacy & Security -> Accessibility. Without it every call fails with error
-1719 / -25211, which `permission_hint` turns into a sentence a person can
act on.

Each call is one `osascript -l JavaScript` process that prints JSON, so a
call costs ~100-200ms. Window refs are (process name, window index, title):
System Events has no stable window id, and the title is kept so a window
that moved in the z-order is not mistaken for its neighbour.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any

from tools.desktop.model import Element, Tree, WindowInfo
from tools.desktop.system import IS_MAC

log = logging.getLogger("ev.tools.desktop.macos")


class MacError(RuntimeError):
    pass


def available() -> bool:
    return IS_MAC and bool(shutil.which("osascript"))


def permission_hint(message: str) -> str:
    if "-1719" in message or "-25211" in message or "assistive access" in message.lower():
        return (
            "macOS has not given E.V. accessibility access. Allow the app running E.V. under "
            "System Settings, Privacy & Security, Accessibility."
        )
    return message


def _jxa(script: str, *args: Any, timeout: float = 8.0) -> Any:
    """Run a JXA function body with `args` as JSON and return its JSON result."""
    program = (
        "function run(argv) { const args = JSON.parse(argv[0]); "
        "const se = Application('System Events'); " + script + " }"
    )
    try:
        done = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", program, json.dumps(list(args))],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise MacError("System Events did not answer in time") from exc
    if done.returncode != 0:
        raise MacError(permission_hint(done.stderr.strip() or f"osascript exited {done.returncode}"))
    output = done.stdout.strip()
    return json.loads(output) if output else None


_LIST = """
const out = [];
const procs = se.applicationProcesses.whose({backgroundOnly: false})();
for (const p of procs) {
  let wins = [];
  try { wins = p.windows(); } catch (e) { continue; }
  const front = p.frontmost();
  wins.forEach((w, i) => {
    let pos = [0, 0], size = [0, 0], mini = false, title = '';
    try { title = w.name() || ''; } catch (e) {}
    try { pos = w.position(); size = w.size(); } catch (e) {}
    try { mini = w.attributes.byName('AXMinimized').value(); } catch (e) {}
    out.push({app: p.name(), pid: p.unixId(), index: i + 1, title: title,
              x: pos[0], y: pos[1], w: size[0], h: size[1],
              focused: front && i === 0, minimized: mini});
  });
}
return JSON.stringify(out);
"""


def list_windows(limit: int = 12) -> list[WindowInfo]:
    if not available():
        return []
    try:
        raw = _jxa(_LIST)
    except MacError as exc:
        log.info("Listing macOS windows failed: %s", exc)
        return []
    found = []
    for item in raw or []:
        x, y, w, h = (int(item.get(key) or 0) for key in ("x", "y", "w", "h"))
        found.append(
            WindowInfo(
                hwnd=(item["app"], int(item["index"]), item.get("title", "")),
                title=item.get("title") or item["app"], class_name="window",
                left=x, top=y, right=x + w, bottom=y + h,
                focused=bool(item.get("focused")), minimized=bool(item.get("minimized")),
                pid=int(item.get("pid") or 0), app=item["app"], source="macos", geometry=w > 0,
            )
        )
    found.sort(key=lambda window: not window.focused)
    return found[: max(1, limit)]


_WINDOW = "const p = se.applicationProcesses.byName(args[0]); const w = p.windows[args[1] - 1]; "


def focus(window: WindowInfo) -> bool:
    app, index, _title = window.hwnd
    _jxa(
        "Application(args[0]).activate(); " + _WINDOW
        + "try { w.attributes.byName('AXMinimized').value = false; } catch (e) {} "
        "w.actions.byName('AXRaise').perform(); return 'true';",
        app, index,
    )
    return True


def close(window: WindowInfo) -> bool:
    app, index, _title = window.hwnd
    _jxa(
        _WINDOW + "const b = w.buttons.whose({subrole: 'AXCloseButton'})(); "
        "if (b.length) { b[0].click(); return 'true'; } return 'false';",
        app, index,
    )
    return True


def minimize(window: WindowInfo) -> bool:
    app, index, _title = window.hwnd
    _jxa(_WINDOW + "w.attributes.byName('AXMinimized').value = true; return 'true';", app, index)
    return True


def maximize(window: WindowInfo) -> bool:
    app, index, _title = window.hwnd
    _jxa(
        _WINDOW + "const b = w.buttons.whose({subrole: 'AXZoomButton'})(); "
        "if (b.length) { b[0].click(); } return 'true';",
        app, index,
    )
    return True


def exists(window: WindowInfo) -> bool:
    return any(item.app == window.app and item.title == window.title for item in list_windows(limit=100))


def quit_app(app: str) -> bool:
    """The polite quit - the app's own Quit, which asks about unsaved work."""
    _jxa("Application(args[0]).quit(); return 'true';", app)
    return True


# ---------------------------------------------------------------------------
# Accessibility: a bounded walk of one window
# ---------------------------------------------------------------------------
_TREE = """
const p = se.applicationProcesses.byName(args[0]);
const w = p.windows[args[1] - 1];
const out = [];
const max = args[2];
function walk(el, path, depth) {
  if (out.length >= max || depth > 12) return;
  let role = '', name = '', value = '', desc = '', actions = [], enabled = true;
  try { role = el.role(); } catch (e) {}
  try { name = el.name() || ''; } catch (e) {}
  try { desc = el.description() || ''; } catch (e) {}
  try { const v = el.value(); value = v === null || v === undefined ? '' : String(v); } catch (e) {}
  try { actions = el.actions().map(a => a.name()); } catch (e) {}
  try { enabled = el.enabled(); } catch (e) {}
  if (depth > 0 && (name || value || desc || actions.length))
    out.push({path: path, role: role, name: name, value: value.slice(0, 200), desc: desc,
              actions: actions, enabled: enabled, depth: depth});
  let kids = [];
  try { kids = el.uiElements(); } catch (e) {}
  kids.forEach((k, i) => walk(k, path.concat([i]), depth + 1));
}
walk(w, [], 0);
return JSON.stringify(out);
"""

_ROLE_NAMES = {
    "AXButton": "push button", "AXCheckBox": "check box", "AXRadioButton": "radio button",
    "AXTextField": "text", "AXTextArea": "text", "AXPopUpButton": "combo box",
    "AXMenuButton": "menu", "AXMenuItem": "menu item", "AXSlider": "slider",
    "AXLink": "link", "AXStaticText": "label", "AXTabGroup": "page tab list",
    "AXRow": "list item", "AXCell": "table cell",
}


def read_tree(window: WindowInfo, max_nodes: int = 250) -> Tree:
    app, index, title = window.hwnd
    tree = Tree(window=title or app, app=app, source="macos")
    try:
        raw = _jxa(_TREE, app, index, max_nodes, timeout=15.0) or []
    except MacError as exc:
        tree.error = str(exc)
        return tree
    for item in raw:
        raw_role = str(item.get("role", ""))
        role = _ROLE_NAMES.get(raw_role, raw_role.removeprefix("AX").lower())
        actions = tuple(item.get("actions") or ())
        if role == "text":
            actions = actions + ("set text",)
        tree.elements.append(
            Element(
                ref=len(tree.elements) + 1, role=role, name=item.get("name", ""),
                value=item.get("value", ""), description=item.get("desc", ""),
                states=frozenset() if item.get("enabled", True) else frozenset({"disabled"}),
                actions=actions, depth=int(item.get("depth", 0)),
                handle=(app, index, tuple(item.get("path") or ())),
            )
        )
    tree.truncated = len(tree.elements) >= max_nodes
    return tree


_ELEMENT = (
    "let el = se.applicationProcesses.byName(args[0]).windows[args[1] - 1]; "
    "for (const i of args[2]) { el = el.uiElements[i]; } "
)


def do_action(element: Element, wanted: str = "") -> bool:
    app, index, path = element.handle
    named = [item for item in element.actions if item != "set text"]
    action = wanted or ("AXPress" if "AXPress" in named else (named[0] if named else "AXPress"))
    _jxa(_ELEMENT + "el.actions.byName(args[3]).perform(); return 'true';", app, index, list(path), action)
    return True


def set_text(element: Element, text: str) -> bool:
    app, index, path = element.handle
    _jxa(_ELEMENT + "el.value = args[3]; return 'true';", app, index, list(path), text)
    return True


def menu(app: str, path: list[str]) -> bool:
    """Click a menu-bar path such as ["File", "Save As…"]."""
    _jxa(
        "const p = se.applicationProcesses.byName(args[0]); "
        "let m = p.menuBars[0].menuBarItems.byName(args[1][0]).menus[0]; "
        "for (let i = 1; i < args[1].length - 1; i++) { m = m.menuItems.byName(args[1][i]).menus[0]; } "
        "m.menuItems.byName(args[1][args[1].length - 1]).click(); return 'true';",
        app, path,
    )
    return True
