"""Windows: the window manager through user32, by `ctypes`.

This is the code that used to be all of `tools/window.py`, plus the verbs it
never had - close, minimise, maximise, and the process behind a window.
Pure `ctypes` against user32, dwmapi and kernel32, so there is no extra
dependency and no measurable memory cost.

Close is `WM_CLOSE`, posted rather than sent. It is the same message the
title bar's X sends, so an editor with unsaved work asks its own "Save
changes?" question instead of losing the work - and posting means a window
that is busy, or that is showing that very dialog, cannot block E.V. while
it decides.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from tools.desktop.model import WindowInfo
from tools.desktop.system import IS_WINDOWS

log = logging.getLogger("ev.tools.desktop.win32")

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    # Windows 11 keeps a pile of invisible-but-"visible" windows around -
    # suspended UWP apps, shell host surfaces, the odd ghost of a closed
    # dialog. They pass IsWindowVisible and have real titles, so without this
    # check the model is handed a list of windows that are not on the screen
    # and picks one of them. DWM knows which are cloaked; ask it.
    try:
        _dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        _dwmapi.DwmGetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ]
    except OSError:  # pragma: no cover - dwmapi is present on every supported build
        _dwmapi = None

_SW_MINIMIZE = 6
_SW_MAXIMIZE = 3
_SW_RESTORE = 9
_WM_CLOSE = 0x0010
_DWMWA_CLOAKED = 14
_PROCESS_TERMINATE = 0x0001

# Shell furniture that is technically a visible top-level window and is
# never what the user means. "Program Manager" is the desktop itself, and a
# model offered it as a window to click will happily click the wallpaper.
_SHELL_CLASSES = frozenset(
    {
        "Progman",
        "WorkerW",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "Windows.UI.Core.CoreWindow",
        "ApplicationManager_DesktopShellWindow",
        "Xaml_WindowedPopupClass",
    }
)


def available() -> bool:
    return IS_WINDOWS


def _window_title(hwnd: Any) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _class_name(hwnd: Any) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _is_cloaked(hwnd: Any) -> bool:
    if _dwmapi is None:
        return False
    cloaked = ctypes.c_int(0)
    result = _dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return result == 0 and cloaked.value != 0


def window_pid(hwnd: Any) -> int:
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _info(hwnd: Any, foreground: Any) -> WindowInfo | None:
    if not _user32.IsWindowVisible(hwnd):
        return None
    title = _window_title(hwnd)
    if not title.strip() or _is_cloaked(hwnd):
        return None
    cls = _class_name(hwnd)
    if cls in _SHELL_CLASSES:
        return None
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
        return None
    return WindowInfo(
        hwnd=int(hwnd),
        title=title,
        class_name=cls,
        left=int(rect.left),
        top=int(rect.top),
        right=int(rect.right),
        bottom=int(rect.bottom),
        focused=bool(foreground) and int(hwnd) == int(foreground),
        minimized=bool(_user32.IsIconic(hwnd)),
        pid=window_pid(hwnd),
        source="win32",
    )


def list_windows(limit: int = 12) -> list[WindowInfo]:
    """Visible top-level windows, front to back.

    `EnumWindows` walks in z-order, so the first entry is whatever is on top
    and the foreground window is usually it. That ordering is information in
    itself and is preserved rather than sorted away.
    """
    if not IS_WINDOWS:
        return []
    foreground = _user32.GetForegroundWindow()
    found: list[WindowInfo] = []

    def _callback(hwnd, _lparam):  # type: ignore[no-untyped-def]
        if len(found) >= max(1, limit):
            return False
        info = _info(hwnd, foreground)
        if info is not None:
            found.append(info)
        return True

    try:
        _user32.EnumWindows(_ENUM_PROC(_callback), 0)
    except Exception as exc:  # pragma: no cover - defensive; enumeration is cheap
        log.debug("EnumWindows failed: %s", exc)
    return found


def focus(window: WindowInfo) -> bool:
    """Bring a window to the foreground and confirm it actually got there.

    Windows refuses `SetForegroundWindow` from a process that does not own the
    current foreground window, so we temporarily attach to that window's input
    thread, which is the supported way around the restriction.
    """
    hwnd = window.hwnd
    if not IS_WINDOWS or hwnd is None:
        return False
    if _user32.IsIconic(hwnd):
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


def close(window: WindowInfo) -> bool:
    """Ask politely - the same message as the title bar's X."""
    if not IS_WINDOWS:
        return False
    return bool(_user32.PostMessageW(window.hwnd, _WM_CLOSE, 0, 0))


def minimize(window: WindowInfo) -> bool:
    if not IS_WINDOWS:
        return False
    _user32.ShowWindow(window.hwnd, _SW_MINIMIZE)
    return True


def maximize(window: WindowInfo) -> bool:
    if not IS_WINDOWS:
        return False
    _user32.ShowWindow(window.hwnd, _SW_MAXIMIZE)
    return True


def exists(window: WindowInfo) -> bool:
    return IS_WINDOWS and bool(_user32.IsWindow(window.hwnd)) and bool(_user32.IsWindowVisible(window.hwnd))


def kill_pid(pid: int) -> bool:
    """TerminateProcess. Only ever reached after a spoken, strict yes."""
    if not IS_WINDOWS or pid <= 0:
        return False
    handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(_kernel32.TerminateProcess(handle, 1))
    finally:
        _kernel32.CloseHandle(handle)
