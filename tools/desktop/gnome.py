"""GNOME Wayland windows through E.V.'s own Shell extension (org.ev.Windows).

Wayland deliberately gives no client a way to list, focus or close another
client's windows, so the only complete answer on GNOME is code running
inside the compositor. `ev/gnome/ev-windows@ev.local` is that code, kept to
five methods. It is optional: without it, E.V. still lists native windows
through accessibility and XWayland ones through X11, focuses through the
overview and closes with alt+F4 - it just has no rectangles for native
windows and has to drive the keyboard to do what this does in one call.

Installed with `python ev_core.py --install-gnome-extension`. GNOME on
Wayland loads new extensions only at login, so the first use needs a log
out and back in, and `--check` says so rather than leaving it a mystery.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

from tools.desktop import bus
from tools.desktop.model import WindowInfo

log = logging.getLogger("ev.tools.desktop.gnome")

NAME = "org.ev.Windows"
PATH = "/org/ev/Windows"
UUID = "ev-windows@ev.local"
SOURCE = Path(__file__).resolve().parent.parent.parent / "ev" / "gnome" / UUID


def available() -> bool:
    try:
        (owner,) = bus.call(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", "s", (NAME,), timeout=1.0,
        )
        return bool(owner)
    except Exception:
        return False


def _call(method: str, window_id: int | None = None) -> object:
    if window_id is None:
        (result,) = bus.call(NAME, PATH, NAME, method, timeout=2.0)
    else:
        (result,) = bus.call(NAME, PATH, NAME, method, "t", (int(window_id),), timeout=2.0)
    return result


def list_windows(limit: int = 12) -> list[WindowInfo]:
    try:
        raw = json.loads(str(_call("List")))
    except Exception as exc:
        log.debug("org.ev.Windows List failed: %s", exc)
        return []
    found: list[WindowInfo] = []
    # The extension returns most-recently-used first; the focused window
    # leads, matching the z-order every other backend reports.
    for item in raw[: max(1, limit)]:
        x, y = int(item.get("x", 0)), int(item.get("y", 0))
        width, height = int(item.get("width", 0)), int(item.get("height", 0))
        found.append(
            WindowInfo(
                hwnd=int(item["id"]),
                title=str(item.get("title", "")),
                class_name=str(item.get("wm_class", "")),
                left=x, top=y, right=x + width, bottom=y + height,
                focused=bool(item.get("focused")),
                minimized=bool(item.get("minimized")),
                pid=int(item.get("pid") or 0),
                app=str(item.get("app", "")),
                source="gnome",
                geometry=width > 0,
            )
        )
    return found


def focus(window: WindowInfo) -> bool:
    if not bool(_call("Activate", window.hwnd)):
        return False
    for _attempt in range(10):
        time.sleep(0.05)
        if any(item.hwnd == window.hwnd and item.focused for item in list_windows(limit=50)):
            return True
    return False


def close(window: WindowInfo) -> bool:
    return bool(_call("Close", window.hwnd))


def minimize(window: WindowInfo) -> bool:
    return bool(_call("Minimize", window.hwnd))


def maximize(window: WindowInfo) -> bool:
    return bool(_call("Maximize", window.hwnd))


def exists(window: WindowInfo) -> bool:
    return any(item.hwnd == window.hwnd for item in list_windows(limit=200))


def install() -> tuple[bool, str]:
    """Copy the extension into the user's extensions folder and enable it.

    Returns (ok, message for the user). Only ever run from the explicit
    `--install-gnome-extension` command: it changes the user's desktop.
    """
    if not SOURCE.is_dir():
        return False, f"The extension source is missing at {SOURCE}."
    target = Path.home() / ".local" / "share" / "gnome-shell" / "extensions" / UUID
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(SOURCE, target)
    tool = shutil.which("gnome-extensions")
    enabled = False
    if tool:
        done = subprocess.run([tool, "enable", UUID], capture_output=True, text=True, timeout=10)
        enabled = done.returncode == 0
    if available():
        return True, "Installed and running."
    hint = "" if enabled else f" Then run: gnome-extensions enable {UUID}"
    return True, (
        f"Installed to {target}. GNOME on Wayland loads new extensions at login, "
        f"so log out and back in.{hint}"
    )
