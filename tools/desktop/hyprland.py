"""Hyprland: windows, eyes, typing and a kill key, all through the compositor.

Measured on Hyprland 0.56.2 configured in Lua, where the usual answers are
wrong in a way that looks like a typo: `hyprctl dispatch` takes a Lua
expression rather than a dispatcher name, and `hyprctl keyword` refuses
outright ("keyword can't work with non-legacy parsers"). So everything goes
through `_dispatch`, which tries the Lua form and falls back to the legacy one
for a Hyprland still configured the old way.

The kill key is the part that matters. Wayland lets no client grab a global
key, and the X11 grab only fires while an XWayland window has focus - which on
a machine of native windows means it mostly did not fire at all, while the
overlay named it as working. The compositor can hold a key for anyone, so it
holds this one, and the press reaches us as a file changing: `exec_cmd` needs
no socket, no protocol and no process of ours to be healthy, and polling a
stat is cheap.

Nothing here raises. A compositor that does not answer costs the capability,
and the caller falls back to something that says so.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from tools.desktop import system
from tools.desktop.model import WindowInfo
from tools.desktop.uinput import UinputHands

log = logging.getLogger("leo.tools.desktop.hyprland")

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]+$")
_KEY_NAME = re.compile(r"^[A-Za-z0-9_]+$")
# Windows that are never what the user means by "a window": our own face.
_IGNORED_CLASSES = frozenset({"leo-face", "leo.face"})
_MODIFIERS = {
    "ctrl": "CTRL", "control": "CTRL", "alt": "ALT", "shift": "SHIFT",
    "super": "SUPER", "win": "SUPER", "meta": "SUPER", "cmd": "SUPER",
}
_MODMASK = {"SHIFT": 1, "CTRL": 4, "ALT": 8, "SUPER": 64}


def _ctl(*args: str, timeout: float = 2.0) -> str | None:
    """Run hyprctl and return its output, or None when it would not run."""
    exe = shutil.which("hyprctl")
    if exe is None:
        return None
    try:
        done = subprocess.run(
            [exe, *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("hyprctl %s failed: %s", args[:1], exc)
        return None
    return done.stdout if done.returncode == 0 else None


def _query(name: str) -> Any:
    raw = _ctl("-j", name)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def available() -> bool:
    return (
        system.desktop() == "hyprland"
        and bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))
        and shutil.which("hyprctl") is not None
    )


def _accepted(output: str | None) -> bool:
    return output is not None and output.strip().lower().startswith("ok")


def _dispatch(lua: str, *legacy: str) -> bool:
    """Issue a dispatcher: the Lua form first, the legacy form otherwise."""
    if _accepted(_ctl("dispatch", lua)):
        return True
    return bool(legacy) and _accepted(_ctl("dispatch", *legacy))


# -- windows -----------------------------------------------------------------
def _window_info(item: dict[str, Any]) -> WindowInfo | None:
    address = str(item.get("address", ""))
    if not _ADDRESS.match(address) or not item.get("mapped", True):
        return None
    class_name = str(item.get("class") or item.get("initialClass") or "")
    if class_name.lower() in _IGNORED_CLASSES:
        return None
    at, size = item.get("at") or [0, 0], item.get("size") or [0, 0]
    x, y, width, height = int(at[0]), int(at[1]), int(size[0]), int(size[1])
    workspace = item.get("workspace") or {}
    # A window on another workspace still reports a rectangle, and offering
    # that to something about to click would put the click on whatever is
    # actually there.
    on_screen = bool(item.get("visible", True)) and not item.get("hidden")
    return WindowInfo(
        hwnd=address,
        title=str(item.get("title", "")),
        class_name=class_name,
        left=x, top=y, right=x + width, bottom=y + height,
        focused=item.get("focusHistoryID") == 0,
        minimized=str(workspace.get("name", "")).startswith("special:"),
        pid=int(item.get("pid") or 0),
        app=class_name.lower(),
        source="hyprland",
        geometry=on_screen and width > 0,
    )


def list_windows(limit: int = 12) -> list[WindowInfo]:
    """Mapped windows, most recently focused first."""
    raw = _query("clients")
    if not isinstance(raw, list):
        return []
    ordered = sorted(raw, key=lambda item: item.get("focusHistoryID", 9999))
    found = [info for info in map(_window_info, ordered) if info is not None]
    return found[: max(1, limit)]


def _selector(window: WindowInfo) -> str | None:
    address = str(window.hwnd)
    return f"address:{address}" if _ADDRESS.match(address) else None


def focus(window: WindowInfo) -> bool:
    target = _selector(window)
    if target is None or not _dispatch(
        f'hl.dsp.focus({{ window = "{target}" }})', "focuswindow", target
    ):
        return False
    for _attempt in range(10):
        time.sleep(0.05)
        if any(item.hwnd == window.hwnd and item.focused for item in list_windows(limit=50)):
            return True
    return False


def close(window: WindowInfo) -> bool:
    target = _selector(window)
    return target is not None and _dispatch(
        f'hl.dsp.window.close({{ window = "{target}" }})', "closewindow", target
    )


def minimize(window: WindowInfo) -> bool:
    """Hyprland has no minimise; the special workspace is its stand-in."""
    target = _selector(window)
    return target is not None and _dispatch(
        f'hl.dsp.window.move({{ workspace = "special:minimized", follow = false, window = "{target}" }})',
        "movetoworkspacesilent", f"special:minimized,{target}",
    )


def maximize(window: WindowInfo) -> bool:
    if not focus(window):
        return False
    return _dispatch('hl.dsp.window.fullscreen({ mode = "maximized" })', "fullscreen", "1")


def exists(window: WindowInfo) -> bool:
    return any(item.hwnd == window.hwnd for item in list_windows(limit=200))


# -- eyes --------------------------------------------------------------------
def size() -> tuple[int, int] | None:
    """The desktop's logical size: the space uinput's tablet maps onto."""
    monitors = _query("monitors")
    if not isinstance(monitors, list) or not monitors:
        return None
    right = bottom = 0
    for monitor in monitors:
        scale = float(monitor.get("scale") or 1.0) or 1.0
        right = max(right, int(monitor.get("x", 0)) + round(int(monitor.get("width", 0)) / scale))
        bottom = max(bottom, int(monitor.get("y", 0)) + round(int(monitor.get("height", 0)) / scale))
    return (right, bottom) if right > 0 and bottom > 0 else None


def capture_png() -> bytes:
    """One frame of the whole desktop, from the compositor. Raises on failure."""
    exe = shutil.which("grim")
    if exe is None:
        raise RuntimeError("grim is not installed")
    done = subprocess.run([exe, "-t", "png", "-"], capture_output=True, timeout=5.0, check=False)
    if done.returncode != 0 or not done.stdout:
        raise RuntimeError(done.stderr.decode("utf-8", "replace").strip() or "grim returned nothing")
    return done.stdout


# -- hands -------------------------------------------------------------------
class HyprlandHands(UinputHands):
    """uinput for the pointer and shortcuts, `wtype` for text.

    uinput presses physical key positions, so it only spells correctly on a
    US layout and cannot produce anything that layout has no key for. `wtype`
    asks the compositor for the character itself.
    """

    name = "hyprland"

    def __init__(self, devices: Any, screen: tuple[int, int]) -> None:
        super().__init__(devices, screen)
        self._wtype = shutil.which("wtype")
        self.types_unicode = self._wtype is not None

    def write(self, text: str, interval: float = 0.0, **kwargs: Any) -> None:
        if self._wtype is None:
            super().write(text, interval=interval, **kwargs)
            return
        done = subprocess.run(
            [self._wtype, "--", text], capture_output=True, timeout=15.0, check=False
        )
        if done.returncode != 0:
            raise RuntimeError(done.stderr.decode("utf-8", "replace").strip() or "wtype failed")

    typewrite = write


# -- the kill key ------------------------------------------------------------
def chord(combo: str) -> tuple[str, str, int] | None:
    """"ctrl+alt+q" -> ("CTRL + ALT + Q", "Q", modmask), or None.

    A key with no modifier is refused for the same reason as everywhere else:
    bound globally, it would swallow that letter for the whole session.
    """
    parts = [part.strip().lower() for part in (combo or "").split("+") if part.strip()]
    if len(parts) < 2:
        return None
    mods: list[str] = []
    for part in parts[:-1]:
        mod = _MODIFIERS.get(part)
        if mod is None:
            return None
        if mod not in mods:
            mods.append(mod)
    key = parts[-1]
    if key in _MODIFIERS or not _KEY_NAME.match(key):
        return None
    name = key.upper()
    mask = sum(_MODMASK[mod] for mod in mods)
    return " + ".join([*mods, name]), name, mask


def _lua_string(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _bind_present(name: str, mask: int) -> bool:
    binds = _query("binds")
    if not isinstance(binds, list):
        return False
    return any(
        str(bind.get("key", "")).upper() == name and int(bind.get("modmask", -1)) == mask
        for bind in binds
    )


class KillBind:
    """One global key, one watcher, any number of listeners.

    A single bind for the life of the process, because a background session
    needs the key as much as a takeover does and a compositor bind is not a
    thing to add and remove around every click. Listeners are removed; the
    bind stays until `shutdown`.

    `marker` records that the chord is ours. A process that was killed leaves
    its bind behind, and without the marker the next start would read that as
    somebody else's shortcut and refuse. Both files live in the runtime
    directory, so logging out clears them along with the bind.
    """

    POLL_S = 0.15
    # A reload rebuilds the bind list from the config and drops ours.
    RECHECK_S = 5.0
    DEBOUNCE_S = 0.5

    def __init__(self, runtime_dir: str | Path | None = None) -> None:
        base = Path(runtime_dir or os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
        self.flag = base / "leo-kill-switch"
        self.marker = base / "leo-kill-switch.bound"
        self._lock = threading.Lock()
        self._listeners: dict[int, Callable[[], None]] = {}
        self._next_id = 0
        self._thread: threading.Thread | None = None
        self._halt = threading.Event()
        self._chord: tuple[str, str, int] | None = None
        self._seen = 0

    def listen(self, combo: str, on_fire: Callable[[], None]) -> Callable[[], None] | None:
        """Add a listener. Returns its remover, or None if the key cannot be had."""
        with self._lock:
            if self._thread is None and not self._start(combo):
                return None
            token = self._next_id
            self._next_id += 1
            self._listeners[token] = on_fire

        def remove() -> None:
            with self._lock:
                self._listeners.pop(token, None)

        return remove

    def shutdown(self) -> None:
        """Stop watching and give the key back."""
        with self._lock:
            thread, held = self._thread, self._chord
            self._thread, self._chord = None, None
            self._listeners.clear()
            self._halt.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        if held is not None:
            self._unbind(held)
        for path in (self.marker, self.flag):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _start(self, combo: str) -> bool:
        parsed = chord(combo)
        if parsed is None:
            log.warning("Unusable kill-switch hotkey %r; not bound", combo)
            return False
        accel, name, mask = parsed
        if _bind_present(name, mask) and not self._ours(accel):
            log.warning("%s is already bound to something else; kill key not taken", accel)
            return False
        if not self._ensure_bound(parsed):
            log.warning("Hyprland would not bind %s", accel)
            return False
        self._chord = parsed
        self._seen = self._stamp()
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="leo-killswitch-hypr", daemon=True)
        self._thread.start()
        return True

    def _ours(self, accel: str) -> bool:
        try:
            return self.marker.read_text(encoding="utf-8").strip() == accel
        except OSError:
            return False

    def _ensure_bound(self, parsed: tuple[str, str, int]) -> bool:
        accel, name, mask = parsed
        if _bind_present(name, mask):
            return True
        command = f"touch {shlex.quote(str(self.flag))}"
        lua = f"hl.bind({_lua_string(accel)}, hl.dsp.exec_cmd({_lua_string(command)}))"
        if not _accepted(_ctl("eval", lua)):
            legacy = accel.replace(" + ", " ").rsplit(" ", 1)
            spec = f"{legacy[0]}, {legacy[1]}, exec, {command}"
            if not _accepted(_ctl("keyword", "bind", spec)):
                return False
        try:
            self.marker.write_text(accel, encoding="utf-8")
        except OSError as exc:
            log.debug("Kill-switch marker not written: %s", exc)
        return _bind_present(name, mask)

    def _unbind(self, parsed: tuple[str, str, int]) -> None:
        accel = parsed[0]
        if not _accepted(_ctl("eval", f"hl.unbind({_lua_string(accel)})")):
            legacy = accel.replace(" + ", " ").rsplit(" ", 1)
            _ctl("keyword", "unbind", f"{legacy[0]}, {legacy[1]}")

    def _stamp(self) -> int:
        try:
            return self.flag.stat().st_mtime_ns
        except OSError:
            return 0

    def _watch(self) -> None:
        halt, parsed = self._halt, self._chord
        last_check = time.monotonic()
        last_fire = 0.0
        while not halt.wait(self.POLL_S):
            stamp = self._stamp()
            if stamp != self._seen:
                self._seen = stamp
                now = time.monotonic()
                if now - last_fire >= self.DEBOUNCE_S:
                    last_fire = now
                    self._fire()
            if parsed and time.monotonic() - last_check >= self.RECHECK_S:
                last_check = time.monotonic()
                self._ensure_bound(parsed)

    def _fire(self) -> None:
        log.warning("Kill switch pressed (Hyprland bind)")
        with self._lock:
            listeners = list(self._listeners.values())
        for listener in listeners:
            try:
                listener()
            except Exception as exc:
                log.debug("Kill listener raised: %s", exc)


_kill_bind: KillBind | None = None


def kill_bind() -> KillBind:
    global _kill_bind
    if _kill_bind is None:
        _kill_bind = KillBind()
    return _kill_bind


__all__ = [
    "HyprlandHands", "KillBind", "available", "capture_png", "chord", "close", "exists", "focus",
    "kill_bind", "list_windows", "maximize", "minimize", "size",
]
