"""Win32 window focus helpers.

Keystroke automation is only safe once we know *which* window will receive
the keys. These helpers find a window by title and bring it to the
foreground, so `dev_workflow` can refuse to type when the target never
appeared instead of spraying text into whatever was focused.

Pure `ctypes` against user32, so there is no extra dependency and no
measurable memory cost.
"""

from __future__ import annotations

import logging
import time

from tools.base import IS_WINDOWS

log = logging.getLogger("ev.tools.window")

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _ENUM_PROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    _user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    _SW_RESTORE = 9


def _window_title(hwnd) -> str:  # type: ignore[no-untyped-def]
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def find_window(title_contains: str):  # type: ignore[no-untyped-def]
    """Return the HWND of the first visible window whose title matches."""
    if not IS_WINDOWS:
        return None

    needle = title_contains.lower()
    match: list = []

    def _callback(hwnd, _lparam):  # type: ignore[no-untyped-def]
        if not _user32.IsWindowVisible(hwnd):
            return True
        if needle in _window_title(hwnd).lower():
            match.append(hwnd)
            return False  # stop enumerating
        return True

    _user32.EnumWindows(_ENUM_PROC(_callback), 0)
    return match[0] if match else None


def focus_window(hwnd) -> bool:  # type: ignore[no-untyped-def]
    """Bring a window to the foreground and confirm it actually got there.

    Windows refuses `SetForegroundWindow` from a process that does not own the
    current foreground window, so we temporarily attach to that window's input
    thread, which is the supported way around the restriction.
    """
    if not IS_WINDOWS or hwnd is None:
        return False

    _user32.ShowWindow(hwnd, _SW_RESTORE)

    current = _user32.GetForegroundWindow()
    target_thread = _user32.GetWindowThreadProcessId(hwnd, None)
    current_thread = _user32.GetWindowThreadProcessId(current, None) if current else 0
    this_thread = _kernel32.GetCurrentThreadId()

    attached = []
    for thread in {current_thread, this_thread}:
        if thread and thread != target_thread:
            if _user32.AttachThreadInput(thread, target_thread, True):
                attached.append(thread)
    try:
        _user32.SetForegroundWindow(hwnd)
    finally:
        for thread in attached:
            _user32.AttachThreadInput(thread, target_thread, False)

    time.sleep(0.25)
    return _user32.GetForegroundWindow() == hwnd


def wait_for_window(title_contains: str, timeout: float = 10.0, poll: float = 0.4):  # type: ignore[no-untyped-def]
    """Poll until a matching window shows up, or give up and return None."""
    if not IS_WINDOWS:
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hwnd = find_window(title_contains)
        if hwnd is not None:
            return hwnd
        time.sleep(poll)
    return None


def focus_by_title(title_contains: str, timeout: float = 10.0) -> bool:
    """Wait for a window and focus it. False if it never appeared or refused."""
    hwnd = wait_for_window(title_contains, timeout=timeout)
    if hwnd is None:
        log.warning("No window matching %r appeared within %.1fs", title_contains, timeout)
        return False
    if not focus_window(hwnd):
        log.warning("Window %r would not take focus", title_contains)
        return False
    return True
