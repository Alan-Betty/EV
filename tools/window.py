"""Win32 window focus and inventory helpers.

Keystroke automation is only safe once we know *which* window will receive
the keys. These helpers find a window by title and bring it to the
foreground, so `dev_workflow` can refuse to type when the target never
appeared instead of spraying text into whatever was focused.

`list_windows` is the other half, and it is what makes screen perception
accurate rather than merely plausible. A vision model looking at a JPEG is
guessing at which window owns which rectangle and which one has focus; the
window manager already knows both, exactly, for free. Handing that list to
the model alongside the frame turns "there seems to be an editor open" into
"VS Code is focused and occupies this rectangle", which is the difference
between typing into the right window and typing into the one behind it.

Pure `ctypes` against user32, so there is no extra dependency and no
measurable memory cost.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

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
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.IsIconic.argtypes = [wintypes.HWND]

    _SW_RESTORE = 9

    # Windows 11 keeps a pile of invisible-but-"visible" windows around -
    # suspended UWP apps, shell host surfaces, the odd ghost of a closed
    # dialog. They pass IsWindowVisible and have real titles, so without this
    # check the model is handed a list of windows that are not on the screen
    # and picks one of them. DWM knows which are cloaked; ask it.
    _DWMWA_CLOAKED = 14
    try:
        _dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        _dwmapi.DwmGetWindowAttribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
    except OSError:  # pragma: no cover - dwmapi is present on every supported build
        _dwmapi = None


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


# ---------------------------------------------------------------------------
# Inventory - what is actually on the screen, according to the window manager
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WindowInfo:
    """One top-level window, as the window manager sees it.

    The rectangle is in real screen pixels. `fractions` converts it to the
    0-1 coordinate space the vision tools speak, because a model that is
    told "Notepad is at 0.12,0.08 to 0.72,0.83" can click inside Notepad
    without having to estimate anything from a downscaled JPEG.
    """

    hwnd: int
    title: str
    class_name: str
    left: int
    top: int
    right: int
    bottom: int
    focused: bool = False
    minimized: bool = False

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def fractions(self, screen_width: int, screen_height: int) -> tuple[float, float, float, float]:
        if screen_width <= 0 or screen_height <= 0:
            return (0.0, 0.0, 0.0, 0.0)
        return (
            round(self.left / screen_width, 3),
            round(self.top / screen_height, 3),
            round(self.right / screen_width, 3),
            round(self.bottom / screen_height, 3),
        )


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


def _class_name(hwnd) -> str:  # type: ignore[no-untyped-def]
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _is_cloaked(hwnd) -> bool:  # type: ignore[no-untyped-def]
    if _dwmapi is None:
        return False
    cloaked = ctypes.c_int(0)
    result = _dwmapi.DwmGetWindowAttribute(
        hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return result == 0 and cloaked.value != 0


def list_windows(limit: int = 12) -> list[WindowInfo]:
    """Visible top-level windows, front to back.

    `EnumWindows` walks in z-order, so the first entry is whatever is on top
    and the foreground window is usually it. That ordering is information in
    itself and is preserved rather than sorted away.

    Zero-area and cloaked windows are dropped: they are real handles that are
    not on the screen, and offering one to a model that is about to click is
    worse than offering nothing.
    """
    if not IS_WINDOWS:
        return []

    foreground = _user32.GetForegroundWindow()
    found: list[WindowInfo] = []

    def _callback(hwnd, _lparam):  # type: ignore[no-untyped-def]
        if len(found) >= max(1, limit):
            return False
        if not _user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if not title.strip():
            return True
        if _is_cloaked(hwnd):
            return True

        cls = _class_name(hwnd)
        if cls in _SHELL_CLASSES:
            return True

        rect = wintypes.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
            return True

        found.append(
            WindowInfo(
                hwnd=int(hwnd),
                title=title,
                class_name=cls,
                left=int(rect.left),
                top=int(rect.top),
                right=int(rect.right),
                bottom=int(rect.bottom),
                focused=bool(foreground) and int(hwnd) == int(foreground),
                minimized=bool(_user32.IsIconic(hwnd)),
            )
        )
        return True

    try:
        _user32.EnumWindows(_ENUM_PROC(_callback), 0)
    except Exception as exc:  # pragma: no cover - defensive; enumeration is cheap
        log.debug("EnumWindows failed: %s", exc)
    return found


def foreground_title() -> str:
    """The title of whatever currently has focus, or "".

    This is the one fact a keyboard action most needs and the one a
    screenshot answers least reliably: two editors side by side look alike,
    and only one of them is going to receive the keys.
    """
    if not IS_WINDOWS:
        return ""
    hwnd = _user32.GetForegroundWindow()
    return _window_title(hwnd) if hwnd else ""


def describe_windows(
    screen_width: int, screen_height: int, limit: int = 8
) -> str:
    """The window list as a few lines of prompt text.

    Deliberately terse. This rides in every step of a screen task, so it is
    priced per step: a title, a focus marker and a rectangle, and nothing
    else. Titles are truncated because a browser tab name can be a paragraph.
    """
    windows = list_windows(limit=limit)
    if not windows:
        return ""

    lines: list[str] = []
    for index, window in enumerate(windows, start=1):
        title = window.title if len(window.title) <= 70 else window.title[:67] + "..."
        left, top, right, bottom = window.fractions(screen_width, screen_height)
        marks = []
        if window.focused:
            marks.append("FOCUSED")
        if window.minimized:
            marks.append("minimised")
        suffix = f" [{', '.join(marks)}]" if marks else ""
        lines.append(f"{index}. {title}{suffix} at {left},{top} to {right},{bottom}")
    return "\n".join(lines)
