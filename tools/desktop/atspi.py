"""Linux accessibility (AT-SPI) over D-Bus: the inside of any application.

A screenshot shows a button; the accessibility tree *is* the button - its
role, its label, whether it is checked, and an action that presses it
without anybody having to aim. Screen readers depend on this, so GTK, Qt,
Chromium, Electron, LibreOffice and Firefox all publish it, and on GNOME
Wayland it is also the only way to see a native window at all: X sees
nothing there, and no Wayland protocol lets one client list another's
windows.

It is spoken directly over the accessibility bus with `jeepney` - no
`pyatspi`, which needs PyGObject, which is not in E.V.'s virtualenv and
should not have to be. Three things about the other end shape everything
here:

* **It may never answer.** An application that is frozen does not refuse
  an AT-SPI call, it just never replies, so every call carries a timeout
  and a whole tree read carries a deadline.
* **It may be sandboxed.** A strictly confined snap (Brave, Firefox) refuses
  calls from a confined caller - E.V. started from VS Code's snapped
  terminal inherits `snap.code.code`. That comes back as `sandboxed=True`,
  never as an app with no buttons, so the caller can say why and fall back
  to vision.
* **Positions are not global on Wayland.** `GetExtents` in screen
  coordinates is window-relative under Wayland, so elements are operated
  by *action* - press, set text, select - and never by clicking where the
  tree says they are.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import config
from tools.desktop import bus
from tools.desktop.model import Element, Tree, WindowInfo

log = logging.getLogger("ev.tools.desktop.atspi")

_ACC = "org.a11y.atspi.Accessible"
_REGISTRY = "org.a11y.atspi.Registry"
_ROOT = "/org/a11y/atspi/accessible/root"
_NULL = "/org/a11y/atspi/null"

# AtspiStateType indices that matter here.
_STATE_NAMES = {
    1: "active", 4: "checked", 5: "collapsed", 7: "editable", 8: "enabled", 10: "expanded",
    11: "focusable", 12: "focused", 15: "iconified", 16: "modal", 20: "pressed",
    22: "selectable", 23: "selected", 24: "sensitive", 25: "showing", 30: "visible",
    41: "checkable", 43: "read only",
}

# Roles worth keeping even with no name: the user can operate them.
_INTERACTIVE_ROLES = frozenset({
    "push button", "button", "toggle button", "check box", "radio button", "menu item",
    "check menu item", "radio menu item", "menu", "combo box", "text", "entry",
    "password text", "spin button", "slider", "link", "page tab", "list item",
    "tree item", "table cell", "switch", "toggle switch", "editbar",
})
# Roles that are pure structure: walked through, never shown.
_STRUCTURE_ROLES = frozenset({
    "filler", "panel", "section", "scroll pane", "viewport", "layered pane", "split pane",
    "redundant object", "unknown", "grouping", "box",
})
_READABLE_ROLES = frozenset({"label", "static", "heading", "paragraph", "caption"})
# Applications on the bus that are never what the user means by "a window".
_IGNORED_APPS = frozenset({
    "gnome-shell", "mutter-x11-frames", "xdg-desktop-portal-gtk", "xdg-desktop-portal-gnome",
    "ibus-extension-gtk3", "ibus-x11", "evolution-alarm-notify", "update-notifier",
    "gsd-media-keys", "gsd-xsettings", "ev-face", "gjs",
})
_WINDOW_ROLES = frozenset({"frame", "window", "dialog", "alert", "file chooser"})


class AtspiUnavailable(RuntimeError):
    pass


_lock = threading.Lock()
_conn: Any = None


def _timeout() -> float:
    return float(getattr(config, "A11Y_CALL_TIMEOUT_S", 1.5))


def connection() -> Any:
    """The accessibility bus - a bus of its own, found through the session bus."""
    global _conn
    with _lock:
        if _conn is not None:
            return _conn
        try:
            (address,) = bus.call("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress")
            from jeepney.io.blocking import open_dbus_connection

            _conn = open_dbus_connection(bus=str(address))
        except bus.BusUnavailable as exc:
            raise AtspiUnavailable(str(exc)) from exc
        except Exception as exc:
            raise AtspiUnavailable(f"no accessibility bus: {exc}") from exc
        return _conn


def reset() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:  # pragma: no cover
                pass
        _conn = None


def enable() -> bool:
    """Ask toolkits to publish their trees.

    Chromium, Electron and Qt only build an accessibility tree once they
    believe an assistive technology is listening, and `org.a11y.Status
    IsEnabled` is how they are told. GTK publishes regardless. An Electron
    app that was already running needs a restart before it notices.
    """
    try:
        bus.call(
            "org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Set", "ssv",
            ("org.a11y.Status", "IsEnabled", ("b", True)),
        )
        return True
    except Exception as exc:
        log.debug("Could not enable accessibility: %s", exc)
        return False


def is_enabled() -> bool:
    try:
        return bool(bus.get_property("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Status", "IsEnabled"))
    except Exception:
        return False


def available() -> bool:
    try:
        connection()
        return True
    except AtspiUnavailable:
        return False


# ---------------------------------------------------------------------------
# Raw calls
# ---------------------------------------------------------------------------
def _call(ref: tuple[str, str], interface: str, method: str, signature: str = "", body: tuple = ()) -> tuple:
    return bus.call(ref[0], ref[1], interface, method, signature, body, timeout=_timeout(), conn=connection())


def _prop(ref: tuple[str, str], interface: str, name: str) -> Any:
    return bus.get_property(ref[0], ref[1], interface, name, timeout=_timeout(), conn=connection())


def _refs(raw: Any) -> list[tuple[str, str]]:
    return [(str(name), str(path)) for name, path in raw if path != _NULL]


def children(ref: tuple[str, str]) -> list[tuple[str, str]]:
    (raw,) = _call(ref, _ACC, "GetChildren")
    return _refs(raw)


def role(ref: tuple[str, str]) -> str:
    (label,) = _call(ref, _ACC, "GetRoleName")
    return str(label)


def name(ref: tuple[str, str]) -> str:
    return str(_prop(ref, _ACC, "Name") or "")


def states(ref: tuple[str, str]) -> frozenset[str]:
    (words,) = _call(ref, _ACC, "GetState")
    found = set()
    for index, label in _STATE_NAMES.items():
        word, bit = divmod(index, 32)
        if word < len(words) and words[word] & (1 << bit):
            found.add(label)
    return frozenset(found)


def interfaces(ref: tuple[str, str]) -> frozenset[str]:
    (names,) = _call(ref, _ACC, "GetInterfaces")
    return frozenset(str(item).rsplit(".", 1)[-1] for item in names)


def pid_of(bus_name: str) -> int:
    try:
        (pid,) = bus.call(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "GetConnectionUnixProcessID", "s", (bus_name,), timeout=_timeout(), conn=connection(),
        )
        return int(pid)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Applications and their windows
# ---------------------------------------------------------------------------
def applications() -> list[tuple[tuple[str, str], str]]:
    """(ref, name) for every application registered on the bus."""
    apps = []
    for ref in children((_REGISTRY, _ROOT)):
        try:
            label = name(ref)
        except bus.BusCallError as exc:
            log.debug("Application %s did not answer: %s", ref[0], exc)
            label = ""
        apps.append((ref, label))
    return apps


def list_windows(limit: int = 12) -> list[WindowInfo]:
    """Top-level windows of every accessible application.

    No rectangles: on Wayland the tree cannot give global positions, so
    `geometry` is False and nothing downstream may treat (0,0,0,0) as a
    place to click. `hwnd` is the (bus name, path) pair.
    """
    own_pid = os.getpid()
    found: list[WindowInfo] = []
    try:
        apps = applications()
    except (AtspiUnavailable, bus.BusCallError) as exc:
        log.debug("No accessibility registry: %s", exc)
        return []
    for app_ref, app_name in apps:
        if app_name in _IGNORED_APPS:
            continue
        pid = pid_of(app_ref[0])
        if pid == own_pid:
            continue
        try:
            windows = children(app_ref)
        except bus.BusCallError as exc:
            log.debug("Skipping %s: %s", app_name or app_ref[0], exc)
            continue
        for ref in windows:
            try:
                kind = role(ref)
                if kind not in _WINDOW_ROLES:
                    continue
                flags = states(ref)
                title = name(ref)
            except bus.BusCallError:
                continue
            if "showing" not in flags and "iconified" not in flags:
                continue
            found.append(
                WindowInfo(
                    hwnd=ref, title=title or app_name, class_name=kind,
                    left=0, top=0, right=0, bottom=0,
                    focused="active" in flags, minimized="iconified" in flags,
                    pid=pid, app=app_name, source="atspi", geometry=False,
                )
            )
            if len(found) >= max(1, limit):
                break
    # The active window first, as every other backend orders them.
    found.sort(key=lambda window: not window.focused)
    return found[: max(1, limit)]


# ---------------------------------------------------------------------------
# Reading a window
# ---------------------------------------------------------------------------
def _text_of(ref: tuple[str, str], kinds: frozenset[str]) -> str:
    if "Text" not in kinds:
        return ""
    try:
        count = int(_prop(ref, "org.a11y.atspi.Text", "CharacterCount") or 0)
        if count <= 0:
            return ""
        (text,) = _call(ref, "org.a11y.atspi.Text", "GetText", "ii", (0, min(count, 300)))
        return str(text)
    except bus.BusCallError:
        return ""


def _value_of(ref: tuple[str, str], kinds: frozenset[str]) -> str:
    if "Value" not in kinds:
        return ""
    try:
        value = float(_prop(ref, "org.a11y.atspi.Value", "CurrentValue"))
    except (bus.BusCallError, TypeError, ValueError):
        return ""
    return f"{value:g}"


def _actions_of(ref: tuple[str, str], kinds: frozenset[str]) -> tuple[str, ...]:
    if "Action" not in kinds:
        return ()
    try:
        (raw,) = _call(ref, "org.a11y.atspi.Action", "GetActions")
    except bus.BusCallError:
        return ()
    return tuple(str(item[0]) for item in raw if item and item[0])


def read_tree(window: WindowInfo | tuple[str, str], max_nodes: int = 0, max_depth: int = 0) -> Tree:
    """The operable, readable part of one window, numbered for a planner.

    Depth-first in reading order, pruned to what is SHOWING - a collapsed
    menu's items or another tab's contents are not on screen and are not
    offered. Structure (panels, fillers, scroll panes) is walked through and
    never listed. Bounded by node count, depth and a wall-clock deadline,
    because a spreadsheet will happily report a million cells.
    """
    ref = window.hwnd if isinstance(window, WindowInfo) else window
    title = window.title if isinstance(window, WindowInfo) else ""
    app = window.app if isinstance(window, WindowInfo) else ""
    max_nodes = max_nodes or int(getattr(config, "A11Y_MAX_NODES", 250))
    max_depth = max_depth or int(getattr(config, "A11Y_MAX_DEPTH", 40))
    deadline = time.monotonic() + float(getattr(config, "A11Y_TREE_TIMEOUT_S", 6.0))
    tree = Tree(window=title, app=app, source="atspi")
    visited = 0

    def walk(node: tuple[str, str], depth: int) -> None:
        nonlocal visited
        if tree.truncated or depth > max_depth:
            return
        if len(tree.elements) >= max_nodes or time.monotonic() > deadline:
            tree.truncated = True
            return
        visited += 1
        flags = states(node)
        if depth > 0 and "showing" not in flags:
            return
        kind = role(node)
        kinds = interfaces(node)
        label = name(node)
        if depth > 0 and kind not in _STRUCTURE_ROLES:
            wants_text = "editable" in flags or (kind in _READABLE_ROLES and not label)
            value = _value_of(node, kinds) or (_text_of(node, kinds) if wants_text else "")
            actions = _actions_of(node, kinds)
            editable = "EditableText" in kinds and "editable" in flags
            if label or value or actions or editable or kind in _INTERACTIVE_ROLES:
                shown = set(flags & {"checked", "selected", "expanded", "focused", "pressed", "modal"})
                if "checkable" in flags and "checked" not in flags:
                    shown.add("unchecked")
                if "expanded" not in flags and kind in {"menu", "combo box"}:
                    shown.add("collapsed")
                if "sensitive" not in flags and "enabled" not in flags:
                    shown.add("disabled")
                tree.elements.append(
                    Element(
                        ref=len(tree.elements) + 1, role=kind, name=label, value=value,
                        states=frozenset(shown),
                        actions=actions + (("set text",) if editable else ()),
                        depth=depth, handle=node,
                    )
                )
        # A table's thousand rows are not worth walking when the table
        # itself is already listed and the first screenful says what it is.
        limit = 40 if kind in {"table", "tree table", "list"} and depth > 0 else 200
        for child in children(node)[:limit]:
            walk(child, depth + 1)

    try:
        walk(ref, 0)
    except bus.BusCallError as exc:
        if exc.access_denied:
            tree.sandboxed = True
            tree.error = (
                "the app is sandboxed (a snap) and refuses accessibility requests from "
                "this process; vision still works"
            )
        else:
            tree.error = str(exc)
    except AtspiUnavailable as exc:
        tree.error = str(exc)
    log.debug("AT-SPI read %s: %d elements from %d nodes", title or ref, len(tree.elements), visited)
    return tree


# ---------------------------------------------------------------------------
# Acting on an element
# ---------------------------------------------------------------------------
_PREFERRED = ("click", "press", "activate", "toggle", "jump", "open", "select")


def do_action(element: Element, wanted: str = "") -> bool:
    """Run the element's named action, or its natural default."""
    names = [item for item in element.actions if item != "set text"]
    if not names:
        return grab_focus(element)
    choice = 0
    lowered = [item.lower() for item in names]
    if wanted and wanted.lower() in lowered:
        choice = lowered.index(wanted.lower())
    else:
        for preferred in _PREFERRED:
            if preferred in lowered:
                choice = lowered.index(preferred)
                break
    (ok,) = _call(element.handle, "org.a11y.atspi.Action", "DoAction", "i", (choice,))
    return bool(ok)


def set_text(element: Element, text: str) -> bool:
    """Replace the field's contents - exact, layout-free, no clipboard."""
    (ok,) = _call(element.handle, "org.a11y.atspi.EditableText", "SetTextContents", "s", (text,))
    return bool(ok)


def set_value(element: Element, value: float) -> bool:
    try:
        bus.call(
            element.handle[0], element.handle[1], "org.freedesktop.DBus.Properties", "Set", "ssv",
            ("org.a11y.atspi.Value", "CurrentValue", ("d", float(value))),
            timeout=_timeout(), conn=connection(),
        )
        return True
    except bus.BusCallError:
        return False


def grab_focus(element: Element) -> bool:
    try:
        (ok,) = _call(element.handle, "org.a11y.atspi.Component", "GrabFocus")
        return bool(ok)
    except bus.BusCallError:
        return False


def select_child(element: Element, label: str) -> bool:
    """Pick an option in a list or combo by its visible text."""
    wanted = label.strip().lower()
    for index, child in enumerate(children(element.handle)):
        try:
            if wanted and wanted in name(child).lower():
                (ok,) = _call(element.handle, "org.a11y.atspi.Selection", "SelectChild", "i", (index,))
                return bool(ok)
        except bus.BusCallError:
            continue
    return False
