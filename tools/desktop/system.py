"""Which desktop E.V. is running on, asked once and answered in one place.

Every OS check used to be a private copy - `IS_LINUX` in the launcher,
`IS_MAC` in media, `IS_WINDOWS` in the overlay - and none of them asked the
question that decides whether E.V. can see or touch anything on Linux:
*which display server*. On GNOME under Wayland an X11 screen grab returns a
perfectly sized, perfectly black frame, and XTest input reaches XWayland
windows only. Nothing failed; E.V. was simply blind, and nothing said so.

Named `system` rather than `platform` so it can never be mistaken for the
standard library module of that name.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"


def os_name() -> str:
    """"windows", "linux", "macos" or the raw platform string."""
    if IS_WINDOWS:
        return "windows"
    if IS_MAC:
        return "macos"
    if IS_LINUX:
        return "linux"
    return sys.platform


def session_type() -> str:
    """"wayland", "x11", or "" when there is no Linux display session.

    `XDG_SESSION_TYPE` is the authority when it is set. Without it the
    socket variables decide, and `WAYLAND_DISPLAY` wins over `DISPLAY`
    because a Wayland session exports both - `DISPLAY` is XWayland.
    Read on every call rather than cached: tests set the environment, and
    the lookup costs nothing.
    """
    if not IS_LINUX:
        return ""
    declared = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if declared in {"wayland", "x11"}:
        return declared
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return ""


def is_wayland() -> bool:
    return session_type() == "wayland"


def has_xwayland() -> bool:
    """True when X11 clients can still connect - XWayland, or plain X."""
    return IS_LINUX and bool(os.environ.get("DISPLAY"))


def desktop() -> str:
    """"gnome", "kde", ... in lower case, or "" when unknown.

    `XDG_CURRENT_DESKTOP` is a colon list ("ubuntu:GNOME"); the first entry
    that names a real desktop wins, so Ubuntu's flavour prefix is skipped.
    """
    if not IS_LINUX:
        return ""
    raw = os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("DESKTOP_SESSION", "")
    parts = [part.strip().lower() for part in raw.split(":") if part.strip()]
    for known in ("gnome", "kde", "xfce", "sway", "hyprland", "cinnamon", "mate", "lxqt"):
        if known in parts:
            return known
    return parts[-1] if parts else ""


@lru_cache(maxsize=1)
def confinement_label() -> str:
    """The AppArmor label this process runs under, or "".

    Run from VS Code's snapped terminal, E.V. inherits `snap.code.code`,
    and a strictly confined snap on the other end (Brave, Firefox) then
    refuses D-Bus calls from it - MPRIS and AT-SPI alike. That is not
    "nothing is playing" or "this app has no buttons", and the label is
    what lets E.V. say which it is.
    """
    if not IS_LINUX:
        return ""
    try:
        with open("/proc/self/attr/current", encoding="utf-8", errors="replace") as handle:
            label = handle.read().strip().strip("\x00")
    except OSError:
        return ""
    return "" if label in {"", "unconfined"} else label


def snap_confined() -> bool:
    return confinement_label().startswith("snap.")


def describe() -> str:
    """One line for logs and `--check`, e.g. "linux wayland gnome (snap.code.code)"."""
    parts = [os_name(), session_type(), desktop()]
    text = " ".join(part for part in parts if part)
    label = confinement_label()
    return f"{text} ({label})" if label else text


def prompt_line() -> str:
    """One sentence telling a planner which desktop it is driving.

    Built at call time and appended to the step prompts rather than stored
    in `SYSTEM_PROMPT`, so it costs nothing on an ordinary turn. It matters
    because the shortcuts differ: ctrl on Windows and Linux, cmd on a Mac,
    and the key that opens the launcher is called something different on
    each.
    """
    if IS_WINDOWS:
        return "This desktop is Windows: shortcuts use ctrl, and the win key opens Start."
    if IS_MAC:
        return "This desktop is macOS: shortcuts use cmd, not ctrl, and cmd+space opens Spotlight."
    flavour = desktop().upper() if desktop() in {"gnome", "kde"} else (desktop() or "Linux")
    session = session_type() or "a"
    return (
        f"This desktop is Linux ({flavour}, {session} session): shortcuts use ctrl, "
        "and the super key opens the app launcher."
    )
