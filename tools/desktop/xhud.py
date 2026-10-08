"""X11 pieces the takeover overlay needs on Linux, over ctypes.

Tk is X11-only, so on GNOME Wayland the overlay runs through XWayland and
these apply there too.

* `shape_window` - the X Shape extension. An empty *input* region makes a
  window click-through (the Linux twin of WS_EX_TRANSPARENT); a *bounding*
  region of four strips makes the full-screen frame a frame, not a sheet.
  Tk's `-transparentcolor` is Windows-only, which is why the frame used to
  be skipped on Linux altogether.
* `X11KillGrab` - XGrabKey on the root window. Global on an X11 session; on
  Wayland it only hears keys while an XWayland window has focus, so GNOME
  Wayland prefers the Shell extension's grab (`gnome.grab_kill`).
* `parse_linux_hotkey` / `gnome_accelerator` - "ctrl+alt+q" in both dialects.
  No modifier = refused, as on Windows: a bare-letter grab breaks a keyboard.

Every entry point fails soft: False / None, one log line, never a raise.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import select
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

log = logging.getLogger("ev.tools.desktop.xhud")

# X modifier masks.
_SHIFT, _LOCK, _CONTROL, _MOD1, _MOD2, _MOD4 = 1, 2, 4, 8, 16, 64
_MODS = {
    "ctrl": (_CONTROL, "<Control>"),
    "control": (_CONTROL, "<Control>"),
    "alt": (_MOD1, "<Alt>"),
    "shift": (_SHIFT, "<Shift>"),
    "super": (_MOD4, "<Super>"),
    "win": (_MOD4, "<Super>"),
    "meta": (_MOD4, "<Super>"),
}
# Config names -> X keysym names.
_KEYS = {
    "esc": "Escape", "escape": "Escape", "space": "space", "tab": "Tab",
    "enter": "Return", "return": "Return", "backspace": "BackSpace",
    "delete": "Delete", "del": "Delete", "insert": "Insert", "home": "Home",
    "end": "End", "pageup": "Page_Up", "pagedown": "Page_Down",
    "pause": "Pause", "scrolllock": "Scroll_Lock",
}
for _n in range(1, 13):
    _KEYS[f"f{_n}"] = f"F{_n}"

_KEY_PRESS = 2
_GRAB_MODE_ASYNC = 1
_BAD_ACCESS = 10

# Shape extension.
_SHAPE_BOUNDING, _SHAPE_INPUT = 0, 2
_SHAPE_SET = 0
_UNSORTED = 0


def parse_linux_hotkey(combo: str) -> tuple[int, str, str] | None:
    """'ctrl+alt+q' -> (X modifier mask, keysym name, GNOME accelerator)."""
    parts = [p.strip().lower() for p in (combo or "").split("+") if p.strip()]
    if len(parts) < 2:
        return None
    mask = 0
    accel = ""
    for part in parts[:-1]:
        if part not in _MODS:
            return None
        bit, label = _MODS[part]
        if not mask & bit:
            mask |= bit
            accel += label
    key = parts[-1]
    if key in _KEYS:
        name = _KEYS[key]
    elif len(key) == 1 and (key.isalpha() or key.isdigit()):
        name = key
    else:
        return None
    return mask, name, accel + name


def gnome_accelerator(combo: str) -> str:
    parsed = parse_linux_hotkey(combo)
    return parsed[2] if parsed else ""


# -- libX11 / libXext ----------------------------------------------------------
class _XRectangle(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_short), ("y", ctypes.c_short),
        ("width", ctypes.c_ushort), ("height", ctypes.c_ushort),
    ]


class _XErrorEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int), ("display", ctypes.c_void_p),
        ("resourceid", ctypes.c_ulong), ("serial", ctypes.c_ulong),
        ("error_code", ctypes.c_ubyte), ("request_code", ctypes.c_ubyte),
        ("minor_code", ctypes.c_ubyte),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("pad", ctypes.c_long * 24)]


_libs: dict[str, Any] = {}
_libs_lock = threading.Lock()
# Recent X error codes. Xlib's error handler is process-wide and its default
# *exits the process* - a BadAccess from a hotkey somebody else holds would
# take E.V. down. Ours records and returns.
_errors: list[int] = []


def _record_error(_display: Any, event: Any) -> int:
    try:
        _errors.append(int(ctypes.cast(event, ctypes.POINTER(_XErrorEvent)).contents.error_code))
        del _errors[:-16]
    except Exception:
        pass
    return 0


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(_record_error)


@contextmanager
def _quiet_errors(x: Any) -> Iterator[None]:
    """Our handler for the length of our own requests, then the old one back.

    Not installed once for good: Tk installs its own when it starts, and
    Tk's answer to an error on a display it does not own is Xlib's default -
    which exits. Swapped in around each XSync, a BadAccess or BadWindow from
    here is recorded rather than fatal.
    """
    previous = x.XSetErrorHandler(ctypes.cast(_HANDLER, ctypes.c_void_p))
    try:
        yield
    finally:
        x.XSetErrorHandler(previous)


def _x11() -> Any:
    """libX11 with prototypes, or None."""
    with _libs_lock:
        if "x11" in _libs:
            return _libs["x11"]
        lib = None
        name = ctypes.util.find_library("X11")
        if name and os.environ.get("DISPLAY"):
            try:
                lib = ctypes.CDLL(name)
                lib.XOpenDisplay.restype = ctypes.c_void_p
                lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
                lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
                lib.XDefaultRootWindow.restype = ctypes.c_ulong
                lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
                lib.XStringToKeysym.restype = ctypes.c_ulong
                lib.XStringToKeysym.argtypes = [ctypes.c_char_p]
                lib.XKeysymToKeycode.restype = ctypes.c_ubyte
                lib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
                lib.XGrabKey.argtypes = [
                    ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong,
                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ]
                lib.XUngrabKey.argtypes = [
                    ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong,
                ]
                lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
                lib.XPending.argtypes = [ctypes.c_void_p]
                lib.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(_XEvent)]
                lib.XConnectionNumber.argtypes = [ctypes.c_void_p]
                lib.XSetErrorHandler.argtypes = [ctypes.c_void_p]
                lib.XSetErrorHandler.restype = ctypes.c_void_p
            except Exception as exc:
                log.debug("libX11 unusable: %s", exc)
                lib = None
        _libs["x11"] = lib
        return lib


def _xext() -> Any:
    with _libs_lock:
        if "xext" in _libs:
            return _libs["xext"]
        lib = None
        name = ctypes.util.find_library("Xext")
        if name:
            try:
                lib = ctypes.CDLL(name)
                lib.XShapeQueryExtension.argtypes = [
                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                ]
                lib.XShapeCombineRectangles.argtypes = [
                    ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    ctypes.POINTER(_XRectangle), ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ]
            except Exception as exc:
                log.debug("libXext unusable: %s", exc)
                lib = None
        _libs["xext"] = lib
        return lib


def shape_available() -> bool:
    x, ext = _x11(), _xext()
    if x is None or ext is None:
        return False
    display = x.XOpenDisplay(None)
    if not display:
        return False
    try:
        a, b = ctypes.c_int(), ctypes.c_int()
        return bool(ext.XShapeQueryExtension(display, ctypes.byref(a), ctypes.byref(b)))
    finally:
        x.XCloseDisplay(display)


def window_ids(widget: Any) -> list[int]:
    """The X windows behind a Tk toplevel: the wrapper and the widget."""
    ids: list[int] = []
    try:
        frame = widget.wm_frame()
        ids.append(int(str(frame), 16) if isinstance(frame, str) else int(frame))
    except Exception:
        pass
    try:
        ids.append(int(widget.winfo_id()))
    except Exception:
        pass
    return [i for i in dict.fromkeys(ids) if i]


def shape_window(
    windows: list[int],
    bounding: list[tuple[int, int, int, int]] | None = None,
    click_through: bool = True,
) -> bool:
    """Set the bounding region to `bounding` (x, y, w, h rects) and/or empty
    the input region. False when the Shape extension is not there."""
    x, ext = _x11(), _xext()
    if x is None or ext is None or not windows:
        return False
    display = x.XOpenDisplay(None)
    if not display:
        return False
    try:
        with _quiet_errors(x):
            return _apply_shape(x, ext, display, windows, bounding, click_through)
    except Exception as exc:
        log.debug("XShape failed: %s", exc)
        return False
    finally:
        x.XCloseDisplay(display)


def _apply_shape(
    x: Any,
    ext: Any,
    display: Any,
    windows: list[int],
    bounding: list[tuple[int, int, int, int]] | None,
    click_through: bool,
) -> bool:
    before = len(_errors)
    for window in windows:
        if bounding is not None:
            rects = (_XRectangle * max(1, len(bounding)))(
                *[_XRectangle(rx, ry, max(0, rw), max(0, rh)) for rx, ry, rw, rh in bounding]
            )
            ext.XShapeCombineRectangles(
                display, window, _SHAPE_BOUNDING, 0, 0, rects, len(bounding),
                _SHAPE_SET, _UNSORTED,
            )
        if click_through:
            ext.XShapeCombineRectangles(
                display, window, _SHAPE_INPUT, 0, 0, None, 0, _SHAPE_SET, _UNSORTED,
            )
    x.XSync(display, 0)
    if len(_errors) > before:
        log.debug("XShape: X errors %s", _errors[before:])
        return False
    return True


def frame_rects(
    width: int, height: int, band: int, arm: int, thick: int
) -> list[tuple[int, int, int, int]]:
    """Four edge strips `band` wide, plus corner brackets `thick` wide."""
    band = max(1, min(band, width // 4, height // 4))
    rects = [
        (0, 0, width, band),
        (0, height - band, width, band),
        (0, band, band, height - 2 * band),
        (width - band, band, band, height - 2 * band),
    ]
    if thick > band:
        rects += [
            (0, 0, arm, thick), (0, 0, thick, arm),
            (width - arm, 0, arm, thick), (width - thick, 0, thick, arm),
            (0, height - thick, arm, thick), (0, height - arm, thick, arm),
            (width - arm, height - thick, arm, thick), (width - thick, height - arm, thick, arm),
        ]
    return rects


# -- the kill grab -------------------------------------------------------------
class X11KillGrab:
    """XGrabKey on the root, on a thread with its own X connection."""

    def __init__(self, combo: str, on_fire: Callable[[], None]) -> None:
        self.combo = combo
        self._on_fire = on_fire
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._ok = False
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if parse_linux_hotkey(self.combo) is None or _x11() is None:
            return False
        self._thread = threading.Thread(target=self._run, name="ev-killswitch-x11", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)
        return self._ok

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _run(self) -> None:
        x = _x11()
        parsed = parse_linux_hotkey(self.combo)
        display = x.XOpenDisplay(None) if x is not None else None
        if not display or parsed is None:
            self._ready.set()
            return
        mask, name, _accel = parsed
        root = x.XDefaultRootWindow(display)
        code = x.XKeysymToKeycode(display, x.XStringToKeysym(name.encode()))
        # Caps Lock and Num Lock are modifiers to X: grab with each, or the
        # switch is dead whenever Num Lock is on.
        variants = [mask | extra for extra in (0, _LOCK, _MOD2, _LOCK | _MOD2)]
        try:
            if not code:
                log.info("Kill switch %s: no keycode for %r", self.combo, name)
                return
            before = len(_errors)
            with _quiet_errors(x):
                for mods in variants:
                    x.XGrabKey(display, code, mods, root, 0, _GRAB_MODE_ASYNC, _GRAB_MODE_ASYNC)
                x.XSync(display, 0)
            if _BAD_ACCESS in _errors[before:]:
                log.warning("Kill-switch hotkey %s is already taken", self.combo)
                return
            self._ok = True
            self._ready.set()
            fd = x.XConnectionNumber(display)
            event = _XEvent()
            while not self._stop.is_set():
                ready, _w, _e = select.select([fd], [], [], 0.2)
                if not ready:
                    continue
                while x.XPending(display):
                    x.XNextEvent(display, ctypes.byref(event))
                    if event.type == _KEY_PRESS:
                        log.warning("Kill switch pressed (%s)", self.combo)
                        try:
                            self._on_fire()
                        except Exception as exc:
                            log.debug("Kill callback raised: %s", exc)
                        return
        except Exception as exc:
            log.debug("X11 kill grab ended: %s", exc)
        finally:
            self._ready.set()
            try:
                with _quiet_errors(x):
                    if code:
                        for mods in variants:
                            x.XUngrabKey(display, code, mods, root)
                    x.XSync(display, 0)
                x.XCloseDisplay(display)
            except Exception:
                pass


__all__ = [
    "X11KillGrab",
    "frame_rects",
    "gnome_accelerator",
    "parse_linux_hotkey",
    "shape_available",
    "shape_window",
    "window_ids",
]
