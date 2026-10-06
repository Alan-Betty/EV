"""Windows on every desktop: list, focus, close, minimise, maximise, quit.

Keystroke automation is only safe once we know *which* window will receive
the keys, and screen perception is only accurate once the model is told
what the window manager already knows - which application owns which
rectangle and which one has focus. This module answers both, on every
desktop E.V. runs on, through one set of names:

    Windows              user32, by ctypes                     tools.desktop.win32
    Linux on X11         EWMH, by ctypes against libX11         tools.desktop.x11
    GNOME on Wayland     E.V.'s Shell extension if installed    tools.desktop.gnome
                         otherwise X11 for XWayland windows,
                         plus accessibility for native ones     tools.desktop.atspi
    macOS                System Events through osascript        tools.desktop.macos

It used to be the Windows half alone, and every function returned None off
Windows - so on Ubuntu `screen_task`'s focus and wait steps always failed
and the model was never told what was open.

A native Wayland window found only through accessibility has no
rectangle: Wayland does not give one out. `describe_windows` says so
instead of printing (0, 0, 0, 0), and focusing or closing one goes through
the keyboard - GNOME's overview search, alt+F4 - because there is no other
door, and the result is verified by looking again rather than assumed.
"""

from __future__ import annotations

import importlib
import logging
import os
import signal
import time
from types import ModuleType

import config
from tools.desktop import system
from tools.desktop.model import WindowInfo

log = logging.getLogger("ev.tools.window")

__all__ = [
    "WindowInfo", "list_windows", "find_window", "find", "focus", "focus_window", "focus_by_title",
    "wait_for_window", "foreground", "foreground_title", "describe_windows", "close_window",
    "minimize", "maximize", "app_windows", "kill_pid", "CloseOutcome", "close_and_verify",
]


# ---------------------------------------------------------------------------
# Which backends answer here
# ---------------------------------------------------------------------------
def _module(name: str) -> ModuleType:
    return importlib.import_module(f"tools.desktop.{name}")


def _listing_backends() -> list[str]:
    if system.IS_WINDOWS:
        return ["win32"]
    if system.IS_MAC:
        return ["macos"]
    if not system.IS_LINUX:
        return []
    if system.is_wayland():
        if system.desktop() == "gnome" and _module("gnome").available():
            return ["gnome"]
        return ["x11", "atspi"]
    return ["x11"]


def backend_names() -> list[str]:
    """For `--check`: which backends list windows on this desktop."""
    return _listing_backends()


def _backend(window: WindowInfo) -> ModuleType:
    return _module(window.source or (_listing_backends() or ["win32"])[0])


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
def _same_window(first: WindowInfo, second: WindowInfo) -> bool:
    """Two backends describing one window - same process, same title."""
    if not first.pid or first.pid != second.pid:
        return False
    a, b = first.title.strip().lower(), second.title.strip().lower()
    return bool(a) and bool(b) and (a == b or a.startswith(b[:30]) or b.startswith(a[:30]))


def list_windows(limit: int = 12) -> list[WindowInfo]:
    """Visible top-level windows, the focused one first.

    On GNOME Wayland without the extension, two partial views are merged:
    X11 knows the XWayland windows with rectangles, accessibility knows
    every window but without them. The X11 entry wins when both describe
    the same window, because a rectangle is the more useful thing to know.
    """
    found: list[WindowInfo] = []
    backends = _listing_backends()
    for name in backends:
        try:
            batch = _module(name).list_windows(limit=max(limit, 12))
        except Exception as exc:  # a backend failing costs its windows, not the call
            log.debug("Window backend %s failed: %s", name, exc)
            continue
        for window in batch:
            if not any(_same_window(window, known) for known in found):
                found.append(window)
    if len(backends) > 1:
        # A merged list loses z-order; the focused window is the one fact
        # that must still come first.
        found.sort(key=lambda window: not window.focused)
    return found[: max(1, limit)]


def foreground() -> WindowInfo | None:
    for window in list_windows(limit=20):
        if window.focused:
            return window
    return None


def foreground_title() -> str:
    """The title of whatever currently has focus, or "".

    This is the one fact a keyboard action most needs and the one a
    screenshot answers least reliably: two editors side by side look alike,
    and only one of them is going to receive the keys.
    """
    try:
        window = foreground()
    except Exception as exc:  # pragma: no cover - best-effort
        log.debug("No foreground window: %s", exc)
        return ""
    return window.title if window else ""


def _score(window: WindowInfo, needle: str) -> int:
    wanted = needle.strip().lower()
    title, app, cls = window.title.lower(), window.app.lower(), window.class_name.lower()
    if wanted in {title, app}:
        return 100
    if title.startswith(wanted) or app.startswith(wanted):
        return 80
    if wanted in app or wanted in cls:
        return 70
    if wanted in title:
        return 60
    words = [word for word in wanted.split() if len(word) > 2]
    if words and all(word in f"{title} {app} {cls}" for word in words):
        return 40
    return 0


def find(needle: str, limit: int = 40) -> WindowInfo | None:
    """The window the user most plausibly meant by `needle`.

    A name match on the application beats a word buried in a title - "close
    code" means VS Code, not the browser tab titled "code review" - and a
    visible window beats a minimised one.
    """
    if not (needle or "").strip():
        return None
    best: tuple[int, WindowInfo] | None = None
    for window in list_windows(limit=limit):
        score = _score(window, needle)
        if score <= 0:
            continue
        score += 5 if window.focused else 0
        score -= 3 if window.minimized else 0
        if best is None or score > best[0]:
            best = (score, window)
    return best[1] if best else None


def find_window(title_contains: str) -> WindowInfo | None:
    """The first window matching `title_contains`, or None."""
    return find(title_contains)


def app_windows(app: str) -> list[WindowInfo]:
    """Every window belonging to the application `app` matches."""
    first = find(app)
    if first is None:
        return []
    same = [w for w in list_windows(limit=60) if (first.pid and w.pid == first.pid) or w.matches(app)]
    return same or [first]


def wait_for_window(title_contains: str, timeout: float = 10.0, poll: float = 0.4) -> WindowInfo | None:
    """Poll until a matching window shows up, or give up and return None."""
    deadline = time.monotonic() + timeout
    while True:
        window = find(title_contains)
        if window is not None:
            return window
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def describe_windows(screen_width: int, screen_height: int, limit: int = 8) -> str:
    """The window list as a few lines of prompt text.

    Deliberately terse. This rides in every step of a screen task, so it is
    priced per step: a title, a focus marker and a rectangle, and nothing
    else. Titles are truncated because a browser tab name can be a paragraph.
    A window whose position the desktop will not reveal says so, rather than
    claiming the top-left corner.
    """
    windows = list_windows(limit=limit)
    if not windows:
        return ""
    lines: list[str] = []
    for index, window in enumerate(windows, start=1):
        title = window.title if len(window.title) <= 70 else window.title[:67] + "..."
        if window.app and window.app.lower() not in title.lower():
            title = f"{title} ({window.app})"
        marks = []
        if window.focused:
            marks.append("FOCUSED")
        if window.minimized:
            marks.append("minimised")
        suffix = f" [{', '.join(marks)}]" if marks else ""
        if window.geometry:
            left, top, right, bottom = window.fractions(screen_width, screen_height)
            lines.append(f"{index}. {title}{suffix} at {left},{top} to {right},{bottom}")
        else:
            lines.append(f"{index}. {title}{suffix} (position not available)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Acting on a window
# ---------------------------------------------------------------------------
def _keyboard() -> object | None:
    from tools.desktop.hands import hands

    return hands()


def _focus_through_overview(window: WindowInfo) -> bool:
    """GNOME without the extension: Super, the app's name, Enter.

    The overview's search activates an application's existing window
    rather than starting a second copy, which is exactly "switch to". It is
    the only door Wayland leaves open to a process outside the compositor,
    and it is verified afterwards rather than trusted.
    """
    gui = _keyboard()
    name = (window.app or window.title).strip()
    if gui is None or not name or system.desktop() != "gnome":
        return False
    gui.press("win")
    time.sleep(0.45)
    gui.write(name, interval=0.0)
    time.sleep(0.6)
    gui.press("enter")
    for _attempt in range(12):
        time.sleep(0.1)
        now = foreground()
        if now is not None and (now.pid == window.pid or _same_window(now, window)):
            return True
    return False


def focus(window: WindowInfo) -> bool:
    """Bring a window to the front, and confirm it actually got there."""
    try:
        if _backend(window).focus(window):
            return True
    except Exception as exc:
        log.debug("Backend focus failed for %r: %s", window.title, exc)
    if window.source == "atspi":
        return _focus_through_overview(window)
    return False


# The name the core used to call; kept so callers need not change.
focus_window = focus


def focus_by_title(title_contains: str, timeout: float = 10.0) -> bool:
    """Wait for a window and focus it. False if it never appeared or refused."""
    window = wait_for_window(title_contains, timeout=timeout)
    if window is None:
        log.warning("No window matching %r appeared within %.1fs", title_contains, timeout)
        return False
    if window.focused:
        return True
    if not focus(window):
        log.warning("Window %r would not take focus", title_contains)
        return False
    return True


def _with_keys(window: WindowInfo, *keys: str) -> bool:
    """Focus the window, then send a window-manager shortcut to it."""
    gui = _keyboard()
    if gui is None or not focus(window):
        return False
    gui.hotkey(*keys)
    return True


def close_window(window: WindowInfo) -> bool:
    """Ask the window to close, the way its own X button would."""
    if window.source != "atspi":
        return bool(_backend(window).close(window))
    return _with_keys(window, "alt", "f4")


def minimize(window: WindowInfo) -> bool:
    if window.source != "atspi":
        return bool(_backend(window).minimize(window))
    return _with_keys(window, "win", "h")


def maximize(window: WindowInfo) -> bool:
    if window.source != "atspi":
        return bool(_backend(window).maximize(window))
    return _with_keys(window, "win", "up")


def exists(window: WindowInfo) -> bool:
    try:
        if window.source != "atspi":
            return bool(_backend(window).exists(window))
    except Exception as exc:
        log.debug("exists() failed: %s", exc)
    return any(
        (item.hwnd == window.hwnd) or (item.pid == window.pid and item.title == window.title)
        for item in list_windows(limit=60)
    )


def kill_pid(pid: int) -> bool:
    """End a process outright. Only ever reached after a spoken, strict yes."""
    if pid <= 0 or pid == os.getpid():
        return False
    if system.IS_WINDOWS:
        return bool(_module("win32").kill_pid(pid))
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


# ---------------------------------------------------------------------------
# Closing, verified
# ---------------------------------------------------------------------------
class CloseOutcome:
    """What happened after a polite close: gone, held by a dialog, or ignored."""

    def __init__(
        self,
        status: str,
        window: WindowInfo,
        dialog: WindowInfo | None = None,
        buttons: tuple[str, ...] = (),
        text: str = "",
    ) -> None:
        self.status = status  # "closed" | "dialog" | "open" | "refused"
        self.window = window
        self.dialog = dialog
        self.buttons = buttons
        self.text = text


def _dialog_for(window: WindowInfo) -> WindowInfo | None:
    """A new window from the same process: the "Save changes?" question."""
    for item in list_windows(limit=40):
        if item.hwnd == window.hwnd:
            continue
        if window.pid and item.pid == window.pid:
            return item
    return None


def close_and_verify(window: WindowInfo, wait_s: float | None = None) -> CloseOutcome:
    """Close politely, then look: is it gone, or is it asking something?

    An application with unsaved work answers a close with a question, and
    that question belongs to the user. So this never answers it - it reads
    what the dialog says and which buttons it offers, and reports both.
    """
    wait_s = float(getattr(config, "APP_CLOSE_WAIT_S", 3.0)) if wait_s is None else wait_s
    try:
        if not close_window(window):
            return CloseOutcome("refused", window)
    except Exception as exc:
        log.info("Close request for %r failed: %s", window.title, exc)
        return CloseOutcome("refused", window, text=str(exc))

    deadline = time.monotonic() + max(0.2, wait_s)
    while time.monotonic() < deadline:
        time.sleep(0.25)
        if not exists(window):
            return CloseOutcome("closed", window)

    dialog = _dialog_for(window)
    buttons, text, in_window = _read_question(dialog or window, whole=dialog is not None)
    if dialog is not None or in_window:
        return CloseOutcome("dialog", window, dialog=dialog, buttons=buttons, text=text)
    return CloseOutcome("open", window)


_QUESTION_ROLES = frozenset({"dialog", "alert", "alert dialog", "file chooser"})


def _read_question(target: WindowInfo, whole: bool) -> tuple[tuple[str, ...], str, bool]:
    """(buttons, text, found) for the question a close provoked.

    A separate dialog window is read whole. Otherwise only a dialog *inside*
    the window counts - libadwaita draws "Save changes?" as a modal sheet
    over the document rather than as a window of its own - and the window's
    own toolbar must not be mistaken for the question's buttons.
    """
    try:
        from tools.desktop import a11y

        tree = a11y.read(target)
    except Exception as exc:
        log.debug("Could not read the dialog: %s", exc)
        return (), "", False
    elements = tree.elements
    if not whole:
        start = next(
            (i for i, e in enumerate(elements) if e.role in _QUESTION_ROLES or "modal" in e.states),
            None,
        )
        if start is None:
            return (), "", False
        floor = elements[start].depth
        scoped = [elements[start]]
        for element in elements[start + 1:]:
            if element.depth <= floor:
                break
            scoped.append(element)
        elements = scoped
    buttons = tuple(
        e.label for e in elements if e.role in {"push button", "button"} and e.label
    )[:6]
    text = " ".join(
        e.label for e in elements if e.role in {"label", "static", "heading", "dialog", "alert"} and e.label
    )[:240]
    return buttons, text, True
