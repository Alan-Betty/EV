"""X11 windows through EWMH, by `ctypes` against libX11.

Every X11 window manager worth the name publishes the list of client
windows, which one is active, and accepts requests to activate or close one
(`_NET_ACTIVE_WINDOW`, `_NET_CLOSE_WINDOW`). That is the whole of what
`wmctrl` does, and it is a few dozen lines of `ctypes` - so nothing needs to
be installed, the same choice `win32.py` makes against user32.

On a GNOME Wayland session this still matters: Mutter is also the X window
manager for XWayland, so VS Code, Chromium, Electron apps and anything else
running through XWayland are listed, focused and closed here with real
rectangles - measured on this machine, `wmctrl -lG` saw exactly the VS Code
window and nothing native. Native Wayland windows are invisible to X; the
GNOME extension or accessibility has to find those.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import threading
import time

from tools.desktop.model import WindowInfo
from tools.desktop.system import IS_LINUX

log = logging.getLogger("ev.tools.desktop.x11")

_Window = ctypes.c_ulong
_Atom = ctypes.c_ulong


class _ClientMessageData(ctypes.Union):
    _fields_ = [("b", ctypes.c_char * 20), ("s", ctypes.c_short * 10), ("l", ctypes.c_long * 5)]


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", _Window),
        ("message_type", _Atom),
        ("format", ctypes.c_int),
        ("data", _ClientMessageData),
    ]


class _XEvent(ctypes.Union):
    # XEvent is a union padded to 24 longs; XSendEvent reads that much.
    _fields_ = [("xclient", _XClientMessageEvent), ("pad", ctypes.c_long * 24)]


_CLIENT_MESSAGE = 33
_SUBSTRUCTURE_MASK = (1 << 19) | (1 << 20)  # SubstructureNotify | SubstructureRedirect
_ANY_PROPERTY_TYPE = 0

# Windows that are on screen but are not "an application window": docks,
# desktops, notifications, the tooltips of every app that ever ran.
_SKIP_TYPES = {
    "_NET_WM_WINDOW_TYPE_DOCK", "_NET_WM_WINDOW_TYPE_DESKTOP", "_NET_WM_WINDOW_TYPE_TOOLBAR",
    "_NET_WM_WINDOW_TYPE_MENU", "_NET_WM_WINDOW_TYPE_SPLASH", "_NET_WM_WINDOW_TYPE_TOOLTIP",
    "_NET_WM_WINDOW_TYPE_NOTIFICATION", "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU",
    "_NET_WM_WINDOW_TYPE_POPUP_MENU", "_NET_WM_WINDOW_TYPE_DND",
}


class X11Unavailable(RuntimeError):
    pass


class _Display:
    """One connection to the X server, opened on first use."""

    def __init__(self) -> None:
        name = ctypes.util.find_library("X11")
        if not name:
            raise X11Unavailable("libX11 is not installed")
        lib = ctypes.CDLL(name)
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XDefaultRootWindow.restype = _Window
        lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        lib.XInternAtom.restype = _Atom
        lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        lib.XGetAtomName.restype = ctypes.c_void_p
        lib.XGetAtomName.argtypes = [ctypes.c_void_p, _Atom]
        lib.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, _Window, _Atom, ctypes.c_long, ctypes.c_long, ctypes.c_int, _Atom,
            ctypes.POINTER(_Atom), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.XFree.argtypes = [ctypes.c_void_p]
        lib.XSendEvent.argtypes = [ctypes.c_void_p, _Window, ctypes.c_int, ctypes.c_long, ctypes.POINTER(_XEvent)]
        lib.XFlush.argtypes = [ctypes.c_void_p]
        lib.XGetGeometry.argtypes = [
            ctypes.c_void_p, _Window, ctypes.POINTER(_Window), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
        ]
        lib.XTranslateCoordinates.argtypes = [
            ctypes.c_void_p, _Window, _Window, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(_Window),
        ]
        lib.XIconifyWindow.argtypes = [ctypes.c_void_p, _Window, ctypes.c_int]
        lib.XDefaultScreen.argtypes = [ctypes.c_void_p]
        # Without a handler a BadWindow - a window closed between listing it
        # and asking for its title - is fatal to the whole process.
        self._handler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda d, e: 0)
        lib.XSetErrorHandler(self._handler)

        display = lib.XOpenDisplay(None)
        if not display:
            raise X11Unavailable(f"cannot open X display {os.environ.get('DISPLAY', '')!r}")
        self.lib = lib
        self.display = display
        self.root = lib.XDefaultRootWindow(display)
        self._atoms: dict[str, int] = {}
        self.lock = threading.RLock()

    def atom(self, name: str) -> int:
        if name not in self._atoms:
            self._atoms[name] = self.lib.XInternAtom(self.display, name.encode(), 0)
        return self._atoms[name]

    def atom_name(self, atom: int) -> str:
        pointer = self.lib.XGetAtomName(self.display, atom)
        if not pointer:
            return ""
        try:
            return ctypes.string_at(pointer).decode("utf-8", "replace")
        finally:
            self.lib.XFree(pointer)

    def prop(self, window: int, name: str) -> tuple[int, list[int] | bytes] | None:
        """(format, items) for a property: ints for format 32, bytes for 8."""
        kind, fmt = _Atom(), ctypes.c_int()
        count, after = ctypes.c_ulong(), ctypes.c_ulong()
        data = ctypes.c_void_p()
        status = self.lib.XGetWindowProperty(
            self.display, window, self.atom(name), 0, 4096, 0, _ANY_PROPERTY_TYPE,
            ctypes.byref(kind), ctypes.byref(fmt), ctypes.byref(count), ctypes.byref(after), ctypes.byref(data),
        )
        if status != 0 or not data.value:
            return None
        try:
            if fmt.value == 32:
                # Format 32 is stored as C longs, whatever the server's word size.
                array = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))
                return 32, [int(array[i]) for i in range(count.value)]
            if fmt.value == 8:
                return 8, ctypes.string_at(data, count.value)
            return None
        finally:
            self.lib.XFree(data)

    def ints(self, window: int, name: str) -> list[int]:
        found = self.prop(window, name)
        return found[1] if found and found[0] == 32 else []  # type: ignore[return-value]

    def text(self, window: int, name: str) -> str:
        found = self.prop(window, name)
        if found and found[0] == 8:
            return bytes(found[1]).decode("utf-8", "replace")  # type: ignore[arg-type]
        return ""

    def client_message(self, window: int, name: str, *data: int) -> None:
        event = _XEvent()
        event.xclient.type = _CLIENT_MESSAGE
        event.xclient.send_event = 1
        event.xclient.display = self.display
        event.xclient.window = window
        event.xclient.message_type = self.atom(name)
        event.xclient.format = 32
        for index, value in enumerate(data[:5]):
            event.xclient.data.l[index] = value
        self.lib.XSendEvent(self.display, self.root, 0, _SUBSTRUCTURE_MASK, ctypes.byref(event))
        self.lib.XFlush(self.display)

    def geometry(self, window: int) -> tuple[int, int, int, int] | None:
        root, x, y = _Window(), ctypes.c_int(), ctypes.c_int()
        width, height, border, depth = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint()
        if not self.lib.XGetGeometry(
            self.display, window, ctypes.byref(root), ctypes.byref(x), ctypes.byref(y),
            ctypes.byref(width), ctypes.byref(height), ctypes.byref(border), ctypes.byref(depth),
        ):
            return None
        abs_x, abs_y, child = ctypes.c_int(), ctypes.c_int(), _Window()
        self.lib.XTranslateCoordinates(
            self.display, window, self.root, 0, 0, ctypes.byref(abs_x), ctypes.byref(abs_y), ctypes.byref(child)
        )
        left, top = abs_x.value, abs_y.value
        # Include the decorations the window manager drew around it, so the
        # rectangle matches what the user sees as "the window".
        extents = self.ints(window, "_NET_FRAME_EXTENTS")
        if len(extents) == 4:
            frame_left, frame_right, frame_top, frame_bottom = extents
            return (
                left - frame_left, top - frame_top,
                left + width.value + frame_right, top + height.value + frame_bottom,
            )
        return (left, top, left + width.value, top + height.value)


_lock = threading.Lock()
_display: _Display | None = None
_failed = ""


def _connect() -> _Display:
    global _display, _failed
    with _lock:
        if _display is not None:
            return _display
        if _failed:
            raise X11Unavailable(_failed)
        if not (IS_LINUX and os.environ.get("DISPLAY")):
            _failed = "no X display"
            raise X11Unavailable(_failed)
        try:
            _display = _Display()
        except X11Unavailable as exc:
            _failed = str(exc)
            raise
        except Exception as exc:  # pragma: no cover - a broken libX11
            _failed = f"libX11 failed: {exc}"
            raise X11Unavailable(_failed) from exc
        return _display


def available() -> bool:
    try:
        x = _connect()
    except X11Unavailable:
        return False
    with x.lock:
        return bool(x.ints(x.root, "_NET_SUPPORTED"))


def _window_info(x: _Display, window: int, active: int) -> WindowInfo | None:
    types = {x.atom_name(atom) for atom in x.ints(window, "_NET_WM_WINDOW_TYPE")}
    if types & _SKIP_TYPES:
        return None
    title = x.text(window, "_NET_WM_NAME") or x.text(window, "WM_NAME")
    if not title.strip():
        return None
    raw_class = x.text(window, "WM_CLASS").split("\x00")
    class_name = raw_class[1] if len(raw_class) > 1 and raw_class[1] else raw_class[0]
    states = {x.atom_name(atom) for atom in x.ints(window, "_NET_WM_STATE")}
    if "_NET_WM_STATE_SKIP_TASKBAR" in states and "_NET_WM_STATE_MODAL" not in states:
        return None
    rect = x.geometry(window) or (0, 0, 0, 0)
    pid = (x.ints(window, "_NET_WM_PID") or [0])[0]
    return WindowInfo(
        hwnd=int(window),
        title=title,
        class_name=class_name,
        left=rect[0], top=rect[1], right=rect[2], bottom=rect[3],
        focused=int(window) == int(active),
        minimized="_NET_WM_STATE_HIDDEN" in states,
        pid=int(pid),
        app=raw_class[0] if raw_class else "",
        source="x11",
        geometry=rect[2] > rect[0],
    )


def list_windows(limit: int = 12) -> list[WindowInfo]:
    """Client windows, front to back, as the window manager stacks them."""
    try:
        x = _connect()
    except X11Unavailable:
        return []
    with x.lock:
        stacking = x.ints(x.root, "_NET_CLIENT_LIST_STACKING") or x.ints(x.root, "_NET_CLIENT_LIST")
        active = (x.ints(x.root, "_NET_ACTIVE_WINDOW") or [0])[0]
        found: list[WindowInfo] = []
        for window in reversed(stacking):  # the list is bottom to top
            try:
                info = _window_info(x, window, active)
            except Exception as exc:  # a window that closed while we looked
                log.debug("Skipping X window %s: %s", window, exc)
                continue
            if info is not None:
                found.append(info)
            if len(found) >= max(1, limit):
                break
        return found


def focus(window: WindowInfo) -> bool:
    x = _connect()
    with x.lock:
        # Source 2 says "a pager asked", which window managers honour without
        # applying focus-stealing prevention meant for applications.
        x.client_message(int(window.hwnd), "_NET_ACTIVE_WINDOW", 2, 0, 0)
    for _attempt in range(10):
        time.sleep(0.05)
        with x.lock:
            if (x.ints(x.root, "_NET_ACTIVE_WINDOW") or [0])[0] == int(window.hwnd):
                return True
    return False


def close(window: WindowInfo) -> bool:
    """The polite close: the window manager asks the app, which may ask the user."""
    x = _connect()
    with x.lock:
        x.client_message(int(window.hwnd), "_NET_CLOSE_WINDOW", 0, 2)
    return True


def minimize(window: WindowInfo) -> bool:
    x = _connect()
    with x.lock:
        screen = x.lib.XDefaultScreen(x.display)
        ok = bool(x.lib.XIconifyWindow(x.display, int(window.hwnd), screen))
        x.lib.XFlush(x.display)
    return ok


def maximize(window: WindowInfo) -> bool:
    x = _connect()
    with x.lock:
        x.client_message(
            int(window.hwnd), "_NET_WM_STATE", 1,
            x.atom("_NET_WM_STATE_MAXIMIZED_VERT"), x.atom("_NET_WM_STATE_MAXIMIZED_HORZ"), 2,
        )
    return True


def exists(window: WindowInfo) -> bool:
    try:
        x = _connect()
    except X11Unavailable:
        return False
    with x.lock:
        return int(window.hwnd) in set(x.ints(x.root, "_NET_CLIENT_LIST"))
