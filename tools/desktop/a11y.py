"""The accessibility tier, one entry point for every OS.

`read(window)` returns the operable part of a window as numbered elements,
whichever API answered - AT-SPI on Linux, UI Automation on Windows, System
Events on macOS. `act(element, ...)` presses, types into, toggles or picks
from one of them. `inventory(tree)` turns a tree into the lines a planner
reads, and `fingerprint(tree)` is what notices that nothing changed.

The inventory is budgeted and ordered on purpose, for the same reasons the
browser route's scan is. A dialog the app has just raised is the only thing
that matters while it is up, so it is listed first; then whatever has
focus; then the rest in reading order. Twenty identical "Close tab" buttons
collapse to one. A planner that has to read three hundred lines to find the
Save button is a planner that clicks the wrong one.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import re
import time
from typing import Any

import config
from tools.desktop import system
from tools.desktop.model import EDITABLE_ROLES, Element, Tree, WindowInfo

log = logging.getLogger("ev.tools.desktop.a11y")

_enabled_once = False


def backend_name() -> str:
    if system.IS_WINDOWS:
        return "uia"
    if system.IS_MAC:
        return "macos"
    if system.IS_LINUX:
        return "atspi"
    return ""


def _backend() -> Any:
    name = backend_name()
    if not name:
        raise RuntimeError("no accessibility API on this platform")
    return importlib.import_module(f"tools.desktop.{name}")


def available() -> bool:
    if not getattr(config, "A11Y_ENABLED", True):
        return False
    try:
        return bool(_backend().available())
    except Exception:
        return False


def _atspi_window(window: WindowInfo) -> WindowInfo | None:
    """The accessibility twin of a window another backend found.

    X11 and the GNOME extension identify windows by their own ids; AT-SPI
    by a bus name and an object path. The process id and the title are what
    the two have in common.
    """
    if window.source == "atspi":
        return window
    from tools.desktop import atspi

    candidates = [item for item in atspi.list_windows(limit=60) if window.pid and item.pid == window.pid]
    if not candidates:
        return None
    title = window.title.strip().lower()
    for item in candidates:
        if item.title.strip().lower() == title:
            return item
    for item in candidates:
        other = item.title.strip().lower()
        if title and other and (title.startswith(other[:25]) or other.startswith(title[:25])):
            return item
    focused = [item for item in candidates if item.focused]
    return (focused or candidates)[0]


def read(window: WindowInfo) -> Tree:
    """The numbered, operable contents of `window`."""
    global _enabled_once
    if not getattr(config, "A11Y_ENABLED", True):
        return Tree(window=window.title, app=window.app, error="accessibility is switched off")
    name = backend_name()
    if name != "atspi":
        try:
            return _backend().read_tree(window)
        except Exception as exc:
            return Tree(window=window.title, app=window.app, source=name, error=str(exc))

    from tools.desktop import atspi

    try:
        target = _atspi_window(window)
    except Exception as exc:
        return Tree(window=window.title, app=window.app, source="atspi", error=str(exc))
    if target is None:
        return Tree(
            window=window.title, app=window.app, source="atspi",
            error="this window does not publish an accessibility tree",
        )
    tree = atspi.read_tree(target)
    # Chromium, Electron and Qt build a tree only once they believe an
    # assistive technology is listening. Saying so once, and reading again,
    # turns an empty window into a usable one.
    if (
        not tree.elements and not tree.sandboxed and not _enabled_once
        and getattr(config, "A11Y_AUTO_ENABLE", True) and not atspi.is_enabled()
    ):
        _enabled_once = True
        if atspi.enable():
            time.sleep(0.8)
            tree = atspi.read_tree(target)
            if not tree.elements:
                tree.error = (
                    "accessibility was just switched on; an Electron or Chromium app "
                    "that was already open needs restarting before it publishes anything"
                )
    tree.window = tree.window or window.title
    tree.app = tree.app or window.app
    return tree


# ---------------------------------------------------------------------------
# Acting
# ---------------------------------------------------------------------------
def act(element: Element, action: str = "press", text: str = "") -> bool:
    """Operate one element. `action` is press | set | toggle | select | expand | focus | value."""
    backend = _backend()
    verb = (action or "press").lower()
    if verb in {"set", "set_text", "type", "fill"}:
        return bool(backend.set_text(element, text)) if hasattr(backend, "set_text") else False
    if verb == "value" and hasattr(backend, "set_value"):
        return bool(backend.set_value(element, float(text)))
    if verb == "select" and text and backend_name() == "atspi":
        from tools.desktop import atspi

        if atspi.select_child(element, text):
            return True
    if verb == "focus" and hasattr(backend, "grab_focus"):
        return bool(backend.grab_focus(element))
    wanted = {"toggle": "toggle", "select": "select", "expand": "expand"}.get(verb, "")
    return bool(backend.do_action(element, wanted))


def _clean(label: str) -> str:
    """Menu labels carry mnemonics and ellipses: "_Save As…" is "save as"."""
    text = label.replace("_", "").replace("&", "").replace("…", "").replace("...", "")
    return " ".join(text.lower().split())


def find(tree: Tree, label: str, roles: frozenset[str] | None = None) -> Element | None:
    """The best element whose label matches `label`: exact, then prefix, then contains."""
    wanted = _clean(label)
    if not wanted:
        return None
    pool = [e for e in tree.elements if roles is None or e.role in roles]
    for test in (
        lambda text: text == wanted,
        lambda text: text.startswith(wanted),
        lambda text: wanted in text,
    ):
        for element in pool:
            if test(_clean(element.label)):
                return element
    return None


_MENU_ROLES = frozenset(
    {"menu", "menu item", "check menu item", "radio menu item", "menu bar item", "push button", "toggle button"}
)


def menu(window: WindowInfo, path: list[str], settle: float = 0.35) -> tuple[bool, str]:
    """Walk a menu path such as ["File", "Save As"]. Returns (ok, what happened).

    Each step reads the window again, because a menu's items do not exist in
    the tree until the menu is open. On macOS the menu bar belongs to the
    application, not the window, and System Events walks it directly.
    """
    if not path:
        return False, "no menu path"
    if backend_name() == "macos":
        from tools.desktop import macos

        try:
            macos.menu(window.app, path)
            return True, " > ".join(path)
        except Exception as exc:
            return False, str(exc)
    done: list[str] = []
    for step in path:
        tree = read(window)
        target = find(tree, step, _MENU_ROLES)
        if target is None:
            where = " > ".join(done) or "the window"
            return False, f"no menu entry {step!r} under {where}"
        if not act(target, "press"):
            return False, f"{step!r} would not open"
        done.append(target.label)
        time.sleep(settle)
    return True, " > ".join(done)


# ---------------------------------------------------------------------------
# What the planner reads
# ---------------------------------------------------------------------------
_PRIORITY_ROLES = frozenset({"dialog", "alert", "alert dialog", "file chooser"})


def _ordered(tree: Tree) -> list[Element]:
    """Dialog subtree first, then focused elements, then reading order."""
    elements = tree.elements
    first: list[Element] = []
    start = next(
        (i for i, e in enumerate(elements) if e.role in _PRIORITY_ROLES or "modal" in e.states), None
    )
    if start is not None:
        floor = elements[start].depth
        first.append(elements[start])
        for element in elements[start + 1:]:
            if element.depth <= floor:
                break
            first.append(element)
    chosen = {id(e) for e in first}
    focused = [e for e in elements if "focused" in e.states and id(e) not in chosen]
    chosen |= {id(e) for e in focused}
    rest = [e for e in elements if id(e) not in chosen]
    return first + focused + rest


def inventory(tree: Tree, budget: int = 0) -> str:
    """Numbered lines for a planner, within `budget` characters."""
    budget = budget or int(getattr(config, "A11Y_INVENTORY_CHARS", 2400))
    if tree.sandboxed:
        return f"(no inventory: {tree.error})"
    if not tree.elements:
        return f"(no accessible elements{': ' + tree.error if tree.error else ''})"
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    owner: tuple[int, str] | None = None
    used = 0
    dropped = 0
    for element in _ordered(tree):
        key = (element.role, _clean(element.label))
        if key[1] and key in seen:
            continue
        # "2 generic" and "27 tool bar" name nothing and do nothing: a
        # line the planner reads and cannot use. GTK 4 hangs a widget's
        # whole action group off containers ("view.new-folder",
        # "slot.reload"), which is not a control anyone can press, so dotted
        # names do not count. Dialogs stay, unlabelled or not, because they
        # mark where the question starts.
        pressable = [name for name in element.actions if "." not in name]
        if (
            not key[1] and not pressable and element.role not in EDITABLE_ROLES
            and element.role not in _PRIORITY_ROLES
        ):
            continue
        # An icon and a caption inside a list item repeat the item's own
        # name ("Recent Files", then "Recent", then "Recent"). Only the
        # thing that can be pressed is worth a line.
        if pressable or element.role.endswith(("item", "cell", "tab")):
            owner = (element.depth, key[1])
        elif (
            owner and element.depth > owner[0] and key[1]
            and (key[1] in owner[1] or owner[1] in key[1])
        ):
            continue
        seen.add(key)
        line = element.describe()
        if used + len(line) + 1 > budget:
            dropped += 1
            continue
        lines.append(line)
        used += len(line) + 1
    if dropped or tree.truncated:
        more = f"+{dropped} more not shown" if dropped else "more not read"
        lines.append(f"({more}{'; the window is larger than the read limit' if tree.truncated else ''})")
    return "\n".join(lines)


def fingerprint(tree: Tree) -> str:
    """A short hash of what the window shows - equal means nothing changed."""
    digest = hashlib.sha1()
    for element in tree.elements:
        digest.update(f"{element.role}|{element.label}|{element.value}|{sorted(element.states)}\n".encode())
    return digest.hexdigest()[:16]


_REF = re.compile(r"^\s*#?(\d{1,4})\s*$")


def parse_ref(raw: Any) -> int | None:
    match = _REF.match(str(raw or ""))
    return int(match.group(1)) if match else None
