"""Which hands and eyes drive this desktop, chosen once.

`computer_use` asks `hands()` for something with pyautogui's methods and
`capture_png()` for a frame, and never needs to know which backend
answered. The choice is the whole of the cross-platform story for input:

    Windows, macOS, X11          pyautogui + mss, exactly as before
    GNOME on Wayland             Mutter remote-desktop session (eyes + hands)
    other Wayland desktops       uinput hands; no reliable eyes yet
    forced via EV_INPUT_BACKEND  whatever was asked for, or a clear error

A backend that fails to come up costs the capability with a reason, never
the assistant: `hands()` returns None and the mouse and keyboard tools
already know how to say "I can't drive the mouse here".
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import config
from tools.desktop import system

log = logging.getLogger("ev.tools.desktop.hands")

_lock = threading.Lock()
_hands: Any = None
_hands_name = ""
_session: Any = None
_uinput: Any = None
_last_error = ""


def mutter_session() -> Any:
    """The process-wide Mutter session, created on first use."""
    global _session
    from tools.desktop.mutter import MutterSession

    with _lock:
        if _session is None:
            _session = MutterSession(idle_s=config.CAPTURE_SESSION_IDLE_S)
        return _session


def _gnome_wayland() -> bool:
    return system.is_wayland() and system.desktop() == "gnome"


def input_choice() -> str:
    """The backend `hands()` will use, by name, without starting it."""
    forced = config.INPUT_BACKEND
    if forced and forced != "auto":
        return forced
    if not system.is_wayland():
        return "pyautogui"
    if _gnome_wayland():
        from tools.desktop import mutter

        if mutter.available():
            return "mutter"
    from tools.desktop import uinput

    if uinput.writable():
        return "uinput"
    # Last resort, and a partial one: XTest through XWayland reaches only
    # XWayland windows. Better than nothing, and `--check` says so.
    return "pyautogui"


def capture_choice() -> str:
    forced = config.CAPTURE_BACKEND
    if forced and forced != "auto":
        return forced
    if _gnome_wayland():
        from tools.desktop import mutter

        if mutter.available():
            return "mutter"
    return "mss"


def _pyautogui() -> Any:
    import pyautogui

    # The corner failsafe aborts mid-drag if the pointer happens to pass
    # through 0,0, which turns a legitimate action into a half-finished one.
    # The confirmation gate is the safety mechanism here, not a corner.
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0
    return pyautogui


def _desktop_size() -> tuple[int, int]:
    """The X root size, which under XWayland spans the whole desktop."""
    try:
        size = _pyautogui().size()
        return int(size[0]), int(size[1])
    except Exception:
        return (0, 0)


def hands() -> Any:
    """Something with pyautogui's mouse and keyboard methods, or None."""
    global _hands, _hands_name, _uinput, _last_error
    with _lock:
        if _hands is not None:
            return _hands
    choice = input_choice()
    try:
        if choice == "mutter":
            from tools.desktop.mutter import MutterHands

            made: Any = MutterHands(mutter_session())
        elif choice == "uinput":
            from tools.desktop.uinput import UinputDevices, UinputHands

            _uinput = _uinput or UinputDevices()
            size = _desktop_size()
            if not size[0]:
                raise RuntimeError("the desktop size is unknown, so a click cannot be placed")
            made = UinputHands(_uinput, size)
        elif choice == "pyautogui":
            made = _pyautogui()
        else:
            raise RuntimeError(f"unknown EV_INPUT_BACKEND {choice!r}")
    except Exception as exc:  # ImportError, no display, refused device
        _last_error = f"{choice}: {exc}"
        log.warning("Input backend %s unavailable: %s", choice, exc)
        return None
    with _lock:
        _hands, _hands_name = made, choice
    log.info("Input backend: %s (%s)", choice, system.describe())
    return made


def hands_name() -> str:
    return _hands_name or input_choice()


def last_error() -> str:
    return _last_error


def capture_png() -> bytes | None:
    """A frame from a non-X11 backend, or None when mss should do it.

    Raises when the chosen backend fails, so `capture_screen` can report the
    real reason instead of falling through to a grab that will be black.
    """
    if capture_choice() != "mutter":
        return None
    return mutter_session().capture_png()


def screen_size(open_session: bool = True) -> tuple[int, int] | None:
    """The coordinate space input is given in, when a backend defines one.

    `open_session=False` is for callers that only describe the desktop -
    the window list, `--check` - and must not raise GNOME's "screen is being
    shared" indicator just to learn a size. They get the live session's size
    if there is one, and None otherwise.
    """
    if capture_choice() == "mutter" or input_choice() == "mutter":
        if not open_session:
            live = _session
            if live is None or not getattr(live, "_stream", ""):
                return None
        try:
            size = mutter_session().size()
            if size[0] > 0:
                return size
        except Exception as exc:
            log.debug("Mutter size unavailable: %s", exc)
    return None


def reset() -> None:
    """Forget the chosen backends and close anything they opened."""
    global _hands, _hands_name, _session, _uinput, _last_error
    with _lock:
        session, devices = _session, _uinput
        _hands = _session = _uinput = None
        _hands_name = _last_error = ""
    if session is not None:
        session.stop()
    if devices is not None:
        devices.close()


def report() -> dict[str, str]:
    """What `--check` prints: the session and the backend for each job."""
    info = {
        "session": system.describe() or system.os_name(),
        "input": input_choice(),
        "capture": capture_choice(),
    }
    if system.is_wayland():
        from tools.desktop import uinput

        info["uinput"] = "writable" if uinput.writable() else "not writable (join the 'input' group)"
        if info["input"] == "pyautogui":
            info["warning"] = "XTest reaches XWayland windows only"
    return info
