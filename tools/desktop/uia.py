"""Windows accessibility: UI Automation through `comtypes`.

UI Automation is what Narrator reads, and every framework Windows ships -
Win32, WinForms, WPF, UWP/WinUI, and Chromium and Electron through their own
providers - publishes it. A button arrives as a button with a name and an
Invoke pattern, so pressing it needs no coordinates at all.

`comtypes` is the one dependency here, optional and imported on first use
only; without it this tier reports itself unavailable and the vision loop
does the job as it always has. The tree is fetched with a single
`CacheRequest` over the whole window subtree, which is one cross-process
round trip rather than one per property per element - the difference
between ~100ms and several seconds on a busy window.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import config
from tools.desktop.model import Element, Tree, WindowInfo
from tools.desktop.system import IS_WINDOWS

log = logging.getLogger("ev.tools.desktop.uia")

# Property ids (UIAutomationClient.h).
_NAME = 30005
_CONTROL_TYPE = 30003
_AUTOMATION_ID = 30011
_RECT = 30001
_ENABLED = 30010
_OFFSCREEN = 30022
_FOCUSED = 30008
_HAS_INVOKE = 30031
_HAS_TOGGLE = 30041
_HAS_VALUE = 30043
_HAS_SELECTION_ITEM = 30036
_HAS_EXPAND = 30028
_HAS_RANGE = 30033
_VALUE = 30045
_TOGGLE_STATE = 30086
_EXPAND_STATE = 30070
_IS_SELECTED = 30079
_IS_MODAL = 30077

# Pattern ids.
_INVOKE = 10000
_VALUE_PATTERN = 10002
_RANGE_PATTERN = 10003
_EXPAND_PATTERN = 10005
_SELECTION_ITEM = 10010
_TOGGLE = 10015
_LEGACY = 10018

_TREE_SCOPE_SUBTREE = 7

# Control types, named the way the AT-SPI side names them so the planner
# reads one vocabulary on every OS.
_ROLES = {
    50000: "push button", 50002: "check box", 50003: "combo box", 50004: "text",
    50005: "link", 50007: "list item", 50008: "list", 50009: "menu", 50010: "menu bar",
    50011: "menu item", 50013: "radio button", 50015: "slider", 50016: "spin button",
    50018: "page tab list", 50019: "page tab", 50020: "label", 50021: "tool bar",
    50023: "tree", 50024: "tree item", 50026: "grouping", 50028: "table",
    50029: "table cell", 50030: "document", 50031: "push button", 50032: "dialog",
    50033: "panel", 50034: "header", 50035: "header item", 50036: "table",
    50037: "title bar", 50038: "separator", 50025: "custom", 50006: "image",
    50012: "progress bar", 50014: "scroll bar", 50017: "status bar", 50022: "tool tip",
}
_STRUCTURE = frozenset({"panel", "grouping", "custom", "separator", "scroll bar", "title bar", "image"})


class UiaUnavailable(RuntimeError):
    pass


_lock = threading.Lock()
_api: Any = None


def _automation() -> Any:
    """(module, IUIAutomation) - created once, on first use."""
    global _api
    with _lock:
        if _api is not None:
            return _api
        if not IS_WINDOWS:
            raise UiaUnavailable("UI Automation is Windows only")
        try:
            import comtypes.client
        except ImportError as exc:
            raise UiaUnavailable("comtypes is not installed (pip install comtypes)") from exc
        try:
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as module

            automation = comtypes.client.CreateObject(module.CUIAutomation, interface=module.IUIAutomation)
        except Exception as exc:
            raise UiaUnavailable(f"UI Automation would not start: {exc}") from exc
        _api = (module, automation)
        return _api


def available() -> bool:
    try:
        _automation()
        return True
    except UiaUnavailable:
        return False


def _cache_request(automation: Any) -> Any:
    request = automation.CreateCacheRequest()
    for prop in (
        _NAME, _CONTROL_TYPE, _AUTOMATION_ID, _RECT, _ENABLED, _OFFSCREEN, _FOCUSED, _HAS_INVOKE,
        _HAS_TOGGLE, _HAS_VALUE, _HAS_SELECTION_ITEM, _HAS_EXPAND, _HAS_RANGE, _VALUE,
        _TOGGLE_STATE, _EXPAND_STATE, _IS_SELECTED, _IS_MODAL,
    ):
        request.AddProperty(prop)
    request.TreeScope = _TREE_SCOPE_SUBTREE
    request.TreeFilter = automation.ControlViewCondition
    return request


def _cached(element: Any, prop: int) -> Any:
    try:
        return element.GetCachedPropertyValue(prop)
    except Exception:
        return None


def _children(element: Any) -> list[Any]:
    try:
        array = element.GetCachedChildren()
    except Exception:
        return []
    if array is None:
        return []
    return [array.GetElement(index) for index in range(array.Length)]


def _bounds(rect: Any) -> tuple[int, int, int, int] | None:
    """UIA hands BoundingRectangle back as (left, top, width, height)."""
    try:
        if rect is None or len(rect) != 4:
            return None
        left, top, width, height = (int(item) for item in rect)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return (left, top, left + width, top + height)


def element_from(raw: Any, depth: int, ref: int) -> Element | None:
    """One cached UIA element as an `Element`, or None if it is structure."""
    role = _ROLES.get(int(_cached(raw, _CONTROL_TYPE) or 0), "unknown")
    if role in _STRUCTURE:
        return None
    name = str(_cached(raw, _NAME) or "")
    actions: list[str] = []
    if _cached(raw, _HAS_INVOKE):
        actions.append("invoke")
    if _cached(raw, _HAS_TOGGLE):
        actions.append("toggle")
    if _cached(raw, _HAS_SELECTION_ITEM):
        actions.append("select")
    if _cached(raw, _HAS_EXPAND):
        actions.append("expand")
    value = ""
    if _cached(raw, _HAS_VALUE):
        value = str(_cached(raw, _VALUE) or "")
        actions.append("set text")
    states: set[str] = set()
    toggle = _cached(raw, _TOGGLE_STATE)
    if _cached(raw, _HAS_TOGGLE) and toggle is not None:
        states.add("checked" if int(toggle) == 1 else "unchecked")
    expand = _cached(raw, _EXPAND_STATE)
    if _cached(raw, _HAS_EXPAND) and expand is not None and int(expand) in (0, 1):
        states.add("expanded" if int(expand) == 1 else "collapsed")
    if _cached(raw, _IS_SELECTED):
        states.add("selected")
    if _cached(raw, _FOCUSED):
        states.add("focused")
    if not _cached(raw, _ENABLED):
        states.add("disabled")
    if _cached(raw, _IS_MODAL):
        states.add("modal")
    if not (name or value or actions):
        return None
    return Element(
        ref=ref, role=role, name=name, value=value, states=frozenset(states),
        actions=tuple(actions), bounds=_bounds(_cached(raw, _RECT)), depth=depth, handle=raw,
    )


def read_tree(window: WindowInfo, max_nodes: int = 0, max_depth: int = 0) -> Tree:
    tree = Tree(window=window.title, app=window.app, source="uia")
    try:
        _module, automation = _automation()
        root = automation.ElementFromHandle(window.hwnd)
        built = root.BuildUpdatedCache(_cache_request(automation))
    except UiaUnavailable as exc:
        tree.error = str(exc)
        return tree
    except Exception as exc:
        tree.error = f"UI Automation could not read the window: {exc}"
        return tree
    max_nodes = max_nodes or int(getattr(config, "A11Y_MAX_NODES", 250))
    max_depth = max_depth or int(getattr(config, "A11Y_MAX_DEPTH", 40))

    def walk(raw: Any, depth: int) -> None:
        if tree.truncated or depth > max_depth:
            return
        if len(tree.elements) >= max_nodes:
            tree.truncated = True
            return
        if depth > 0 and _cached(raw, _OFFSCREEN):
            return
        if depth > 0:
            element = element_from(raw, depth, len(tree.elements) + 1)
            if element is not None:
                tree.elements.append(element)
        for child in _children(raw):
            walk(child, depth + 1)

    walk(built, 0)
    return tree


def _pattern(element: Element, pattern_id: int, interface_name: str) -> Any:
    module, _automation_obj = _automation()
    raw = element.handle.GetCurrentPattern(pattern_id)
    if raw is None:
        return None
    return raw.QueryInterface(getattr(module, interface_name))


def do_action(element: Element, wanted: str = "") -> bool:
    order = ([wanted] if wanted else []) + ["invoke", "toggle", "select", "expand"]
    for action in order:
        if action not in element.actions:
            continue
        if action == "invoke":
            _pattern(element, _INVOKE, "IUIAutomationInvokePattern").Invoke()
        elif action == "toggle":
            _pattern(element, _TOGGLE, "IUIAutomationTogglePattern").Toggle()
        elif action == "select":
            _pattern(element, _SELECTION_ITEM, "IUIAutomationSelectionItemPattern").Select()
        elif action == "expand":
            pattern = _pattern(element, _EXPAND_PATTERN, "IUIAutomationExpandCollapsePattern")
            if "expanded" in element.states:
                pattern.Collapse()
            else:
                pattern.Expand()
        return True
    # The catch-all every control implements, even when nothing else fits.
    legacy = _pattern(element, _LEGACY, "IUIAutomationLegacyIAccessiblePattern")
    if legacy is None:
        return False
    legacy.DoDefaultAction()
    return True


def set_text(element: Element, text: str) -> bool:
    pattern = _pattern(element, _VALUE_PATTERN, "IUIAutomationValuePattern")
    if pattern is None:
        return False
    pattern.SetValue(text)
    return True


def set_value(element: Element, value: float) -> bool:
    pattern = _pattern(element, _RANGE_PATTERN, "IUIAutomationRangeValuePattern")
    if pattern is None:
        return False
    pattern.SetValue(float(value))
    return True


def grab_focus(element: Element) -> bool:
    try:
        element.handle.SetFocus()
        return True
    except Exception:
        return False
