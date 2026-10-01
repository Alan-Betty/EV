"""Is the user in the middle of something the face must not sit on top of?

A face in the corner is company while someone works and an intrusion while
they watch a film, present, or play something full screen. This module
answers one question - "is now a bad moment to be seen?" - with a short
reason or an empty string, and the window decides what to do about it.

No Qt and nothing resident: every probe is one cheap call the operating
system already answers, run on the face's own polling thread.

**Windows** has the question built in. `SHQueryUserNotificationState` is
what the shell itself asks before it shows a toast, and it reports a
full-screen application, a Direct3D game and presentation mode directly.

**Ubuntu on Wayland** does not. A Wayland compositor tells no client what
any other client is doing, by design, and GNOME's window introspection is
locked to its own tools. Three signals survive that, and together they cover
what "hide during videos and priority stuff" means in practice:

* **Do Not Disturb.** The user said it themselves, in the top bar.
* **A video wake lock.** Browsers and players stop the screen dimming while
  video plays - Chrome and Brave register it as "Video Wake Lock", and a
  native Wayland player asks Mutter, which registers it as "mutter". Music
  takes a *suspend* lock instead (flag 4, "Playing audio"), which is
  deliberately not read: a song is not a reason to hide.
* **A full-screen X11 window**, for anything running through XWayland, read
  from the window manager's own `_NET_WM_STATE`.

The wake lock cannot tell a full-screen video from one in a corner of a
page, and that is accepted rather than worked round: the only way to know
would be a GNOME Shell extension, and a face that ducks out while a video is
playing anywhere errs in the right direction.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from typing import Callable

log = logging.getLogger("ev.face.busy")

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

PROBE_TIMEOUT_S = 1.5

# SHQueryUserNotificationState results that mean "do not draw over this".
_WINDOWS_BUSY = {
    2: "fullscreen",     # QUNS_BUSY: a full-screen application
    3: "fullscreen",     # QUNS_RUNNING_D3D_FULL_SCREEN: a game
    4: "presentation",   # QUNS_PRESENTATION_MODE
    7: "fullscreen",     # QUNS_APP: a full-screen Store app
}

# GNOME's inhibit flag for "do not go idle" - what a playing video asks for.
_INHIBIT_IDLE = 8
_VIDEO_REASON = re.compile(r"video|fullscreen|full screen|present|movie|film|game", re.IGNORECASE)

Runner = Callable[[list[str]], "str | None"]


def run(argv: list[str]) -> str | None:
    """stdout of a short command, or None if it is missing, slow or failed."""
    if shutil.which(argv[0]) is None:
        return None
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("probe %s failed: %s", argv[0], exc)
        return None
    return done.stdout if done.returncode == 0 else None


# -- Windows -------------------------------------------------------------


def windows_reason() -> str:
    try:
        import ctypes

        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return ""
        return _WINDOWS_BUSY.get(state.value, "")
    except (AttributeError, OSError) as exc:
        log.debug("SHQueryUserNotificationState unavailable: %s", exc)
        return ""


# -- Linux ---------------------------------------------------------------


def do_not_disturb(runner: Runner = run) -> bool:
    out = runner(["gsettings", "get", "org.gnome.desktop.notifications", "show-banners"])
    return out is not None and out.strip() == "false"


def _gdbus(runner: Runner, path: str, method: str, *args: str) -> str | None:
    return runner(["gdbus", "call", "--session", "--dest", "org.gnome.SessionManager",
                   "--object-path", path, "--method", method, *args])


def video_playing(runner: Runner = run) -> bool:
    """An idle inhibitor that looks like a video, a game or a presentation.

    `IsInhibited` is asked first because it is one call and almost always
    false; the inhibitors are only listed when something is holding one.
    """
    held = _gdbus(runner, "/org/gnome/SessionManager",
                  "org.gnome.SessionManager.IsInhibited", str(_INHIBIT_IDLE))
    if held is None or "true" not in held:
        return False
    listing = _gdbus(runner, "/org/gnome/SessionManager", "org.gnome.SessionManager.GetInhibitors")
    for path in re.findall(r"/org/gnome/SessionManager/Inhibitor\d+", listing or ""):
        flags = _gdbus(runner, path, "org.gnome.SessionManager.Inhibitor.GetFlags") or ""
        match = re.search(r"uint32 (\d+)", flags)
        if not match or not int(match.group(1)) & _INHIBIT_IDLE:
            continue
        reason = _gdbus(runner, path, "org.gnome.SessionManager.Inhibitor.GetReason") or ""
        app = _gdbus(runner, path, "org.gnome.SessionManager.Inhibitor.GetAppId") or ""
        # "mutter" is a Wayland client using the idle-inhibit protocol, which
        # in practice is a native video player or a game.
        if _VIDEO_REASON.search(reason) or "'mutter'" in app:
            return True
    return False


def x11_fullscreen(runner: Runner = run) -> bool:
    """The focused X11 window is full screen. Blind to native Wayland windows."""
    active = runner(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    match = re.search(r"window id # (0x[0-9a-fA-F]+)", active or "")
    if not match or int(match.group(1), 16) == 0:
        return False
    state = runner(["xprop", "-id", match.group(1), "_NET_WM_STATE"])
    return bool(state) and "_NET_WM_STATE_FULLSCREEN" in state


# -- the question ---------------------------------------------------------


def busy_reason(*, fullscreen: bool = True, video: bool = True, dnd: bool = True,
                runner: Runner = run) -> str:
    """Why the face should stay out of sight right now, or "" if it need not."""
    if IS_WINDOWS:
        reason = windows_reason()
        if reason == "presentation" or (reason and fullscreen):
            return reason
        return ""
    if not IS_LINUX:
        return ""
    if dnd and do_not_disturb(runner):
        return "do-not-disturb"
    if fullscreen and x11_fullscreen(runner):
        return "fullscreen"
    if video and video_playing(runner):
        return "video"
    return ""
