"""GNOME on Wayland: eyes and hands through Mutter's own remote-desktop API.

On a GNOME Wayland session nothing E.V. used to rely on works. An X11 grab
of the screen comes back the right size and entirely black - measured, every
channel `(0, 0)` - and XTest input reaches XWayland windows but not Files,
Settings or the Text Editor. The obvious replacement, the xdg Screenshot
portal, refuses outright for a process with no window of its own: "Only the
focused app is allowed to show a system access dialog", and E.V. has no
focused window to ask from.

`org.gnome.Mutter.ScreenCast` and `org.gnome.Mutter.RemoteDesktop` are what
GNOME Remote Desktop itself is built on, and they answer a session process
directly. One remote-desktop session, with a screencast of the primary
monitor linked to it, gives both halves at once:

* **Frames** arrive over PipeWire. `gst-launch-1.0 pipewiresrc` takes one
  buffer and hands back a PNG - ~0.27s a frame, measured, plus ~0.14s once
  to set the session up.
* **Input** is injected by the compositor itself, so it reaches every
  window, native or XWayland. Pointer positions are given *relative to the
  stream*, which means the coordinate a model reads off a frame is the
  coordinate the click lands on - there is no second mapping to get wrong.
  Keys are sent as keysyms and Mutter finds them in the active layout,
  pressing Shift where the layout needs it.

GNOME shows its own "screen is being shared" indicator while a session is
open. That is not a side effect to hide - it is the compositor saying, in
its own chrome, that something is driving the desktop. The session is
closed after `CAPTURE_SESSION_IDLE_S` without use, so the indicator means
what it says.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from typing import Any

from tools.desktop import bus, keys

log = logging.getLogger("ev.tools.desktop.mutter")

_RD = "org.gnome.Mutter.RemoteDesktop"
_SC = "org.gnome.Mutter.ScreenCast"

BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
_BUTTONS = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}


class MutterUnavailable(RuntimeError):
    """This is not a GNOME session, or Mutter would not start one."""


def available() -> bool:
    """Whether Mutter's remote-desktop service is on the session bus at all."""
    try:
        (owner,) = bus.call(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", "s", (_RD,), timeout=1.0,
        )
        return bool(owner)
    except Exception:
        return False


class MutterSession:
    """One remote-desktop session plus its linked screencast, opened lazily."""

    def __init__(self, idle_s: float = 30.0) -> None:
        self.idle_s = idle_s
        self._lock = threading.RLock()
        self._rd_path = ""
        self._stream = ""
        self._node = 0
        self._size = (0, 0)
        self._last_use = 0.0
        self._timer: threading.Timer | None = None

    # -- lifecycle ------------------------------------------------------
    def _start(self) -> None:
        from jeepney import MatchRule

        (rd_path,) = bus.call(_RD, "/org/gnome/Mutter/RemoteDesktop", _RD, "CreateSession")
        session_id = bus.get_property(_RD, rd_path, f"{_RD}.Session", "SessionId")
        (sc_path,) = bus.call(
            _SC, "/org/gnome/Mutter/ScreenCast", _SC, "CreateSession", "a{sv}",
            ({"remote-desktop-session-id": ("s", str(session_id))},),
        )
        # cursor-mode 0: the pointer is not drawn into the frame. The model
        # is looking for buttons, and an arrow sitting on top of one is
        # noise exactly where it matters.
        (stream,) = bus.call(
            _SC, sc_path, f"{_SC}.Session", "RecordMonitor", "sa{sv}",
            ("", {"cursor-mode": ("u", 0)}),
        )
        rule = MatchRule(
            type="signal", interface=f"{_SC}.Stream", member="PipeWireStreamAdded", path=stream,
        )
        # A linked screencast is started by starting the remote-desktop
        # session; starting it on its own is an error.
        signal = bus.call_and_wait_signal(
            rule, lambda: bus.call(_RD, rd_path, f"{_RD}.Session", "Start"), timeout=5.0,
        )
        self._rd_path = rd_path
        self._stream = stream
        self._node = int(signal.body[0])
        self._size = self._read_size(stream)
        log.info("Mutter session up: node %s, %sx%s", self._node, *self._size)

    @staticmethod
    def _read_size(stream: str) -> tuple[int, int]:
        try:
            params = bus.get_property(_SC, stream, f"{_SC}.Stream", "Parameters")
            width, height = bus.unwrap(dict(params).get("size", (0, 0)))
            return int(width), int(height)
        except Exception as exc:
            log.debug("Stream size unavailable: %s", exc)
            return (0, 0)

    def _ensure(self) -> None:
        with self._lock:
            if not self._rd_path:
                try:
                    self._start()
                except bus.BusUnavailable as exc:
                    raise MutterUnavailable(str(exc)) from exc
                except Exception as exc:
                    self._reset()
                    raise MutterUnavailable(f"Mutter would not start a session: {exc}") from exc
            self._touch()

    def _touch(self) -> None:
        self._last_use = time.monotonic()
        if self._timer is not None:
            self._timer.cancel()
        if self.idle_s > 0:
            self._timer = threading.Timer(self.idle_s, self._idle_check)
            self._timer.daemon = True
            self._timer.start()

    def _idle_check(self) -> None:
        with self._lock:
            if time.monotonic() - self._last_use >= self.idle_s - 0.05:
                self.stop()

    def _reset(self) -> None:
        self._rd_path = ""
        self._stream = ""
        self._node = 0

    def stop(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._rd_path:
                try:
                    bus.call(_RD, self._rd_path, f"{_RD}.Session", "Stop", timeout=1.0)
                except Exception as exc:  # the user may already have stopped it
                    log.debug("Stopping the Mutter session: %s", exc)
            self._reset()

    def _rd(self, method: str, signature: str = "", body: tuple = ()) -> None:
        """One input call, reopening the session once if it was closed under us.

        The user can end the share from GNOME's indicator at any time, and
        the next call then fails with an unknown object. That is a session to
        reopen, not an action to give up on.
        """
        self._ensure()
        try:
            bus.call(_RD, self._rd_path, f"{_RD}.Session", method, signature, body)
        except bus.BusCallError as exc:
            if exc.access_denied:
                raise
            log.info("Mutter session went away (%s); reopening", exc)
            with self._lock:
                self._reset()
            self._ensure()
            bus.call(_RD, self._rd_path, f"{_RD}.Session", method, signature, body)

    # -- eyes -----------------------------------------------------------
    def size(self) -> tuple[int, int]:
        self._ensure()
        return self._size

    def capture_png(self, timeout: float = 6.0) -> bytes:
        """One frame of the primary monitor as PNG bytes."""
        gst = shutil.which("gst-launch-1.0")
        if not gst:
            raise MutterUnavailable("gst-launch-1.0 is not installed (gstreamer1.0-tools)")
        self._ensure()
        args = [
            gst, "-q", "pipewiresrc", f"path={self._node}", "num-buffers=1",
            "always-copy=true", "!", "videoconvert", "!", "pngenc", "!", "fdsink",
        ]
        for attempt in range(2):
            try:
                done = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                raise MutterUnavailable("the screencast produced no frame in time") from exc
            if done.returncode == 0 and done.stdout.startswith(b"\x89PNG"):
                self._touch()
                return done.stdout
            if attempt == 0:
                # A node that vanished means the session was closed from the
                # indicator; one fresh session is worth trying.
                with self._lock:
                    self.stop()
                self._ensure()
                args[3] = f"path={self._node}"
        tail = done.stderr.decode("utf-8", "replace").strip()[-200:]
        raise MutterUnavailable(f"pipewiresrc failed: {tail or done.returncode}")

    # -- hands ----------------------------------------------------------
    def move(self, x: float, y: float) -> None:
        self._ensure()
        self._rd("NotifyPointerMotionAbsolute", "sdd", (self._stream, float(x), float(y)))

    def button(self, name: str, pressed: bool) -> None:
        self._rd("NotifyPointerButton", "ib", (_BUTTONS.get(name, BTN_LEFT), bool(pressed)))

    def scroll(self, steps: int, horizontal: bool = False) -> None:
        if steps:
            self._rd("NotifyPointerAxisDiscrete", "ui", (1 if horizontal else 0, int(steps)))

    def key(self, sym: int, pressed: bool) -> None:
        self._rd("NotifyKeyboardKeysym", "ub", (int(sym), bool(pressed)))


class MutterHands:
    """pyautogui's vocabulary, spoken to a Mutter session.

    `computer_use` drives whatever `_gui()` returns with `moveTo`, `click`,
    `dragTo`, `scroll`, `write`, `press` and `hotkey`. Matching that shape
    means the mouse and keyboard tools - and their tests, which use a
    recorder of the same shape - did not have to change at all.
    """

    name = "mutter"
    #: Keysyms carry any character, so a non-ASCII string can be typed
    #: rather than needing the clipboard.
    types_unicode = True

    def __init__(self, session: MutterSession) -> None:
        self.session = session
        self._pos = (0.0, 0.0)

    def size(self) -> tuple[int, int]:
        return self.session.size()

    def position(self) -> tuple[float, float]:
        return self._pos

    def moveTo(self, x: float, y: float, duration: float = 0.0, **_: Any) -> None:  # noqa: N802
        self.session.move(x, y)
        self._pos = (float(x), float(y))
        if duration:
            time.sleep(min(duration, 0.2))

    def click(self, x: Any = None, y: Any = None, clicks: int = 1, button: str = "left", **_: Any) -> None:
        if x is not None and y is not None:
            self.moveTo(x, y)
        for index in range(max(1, int(clicks))):
            self.session.button(button, True)
            time.sleep(0.02)
            self.session.button(button, False)
            if index + 1 < clicks:
                time.sleep(0.06)

    def dragTo(self, x: float, y: float, duration: float = 0.3, button: str = "left", **_: Any) -> None:  # noqa: N802
        start_x, start_y = self._pos
        self.session.button(button, True)
        steps = 12
        for step in range(1, steps + 1):
            fraction = step / steps
            self.session.move(start_x + (x - start_x) * fraction, start_y + (y - start_y) * fraction)
            time.sleep(max(0.01, duration / steps))
        self._pos = (float(x), float(y))
        self.session.button(button, False)

    def scroll(self, clicks: int, x: Any = None, y: Any = None, **_: Any) -> None:
        if x is not None and y is not None:
            self.moveTo(x, y)
        # pyautogui: positive is up. Mutter's discrete axis: positive is down.
        self.session.scroll(-int(clicks))

    def _tap(self, sym: int) -> None:
        self.session.key(sym, True)
        self.session.key(sym, False)

    def press(self, key: str, presses: int = 1, **_: Any) -> None:
        sym = keys.keysym(key)
        if sym is None:
            raise ValueError(f"unknown key {key!r}")
        for _index in range(max(1, int(presses))):
            self._tap(sym)

    def hotkey(self, *names: str, **_: Any) -> None:
        syms = [keys.keysym(name) for name in names]
        if any(sym is None for sym in syms):
            raise ValueError(f"unknown key in {names!r}")
        for sym in syms:
            self.session.key(sym, True)
        for sym in reversed(syms):
            self.session.key(sym, False)

    def write(self, text: str, interval: float = 0.0, **_: Any) -> None:
        for char in text:
            self._tap(keys.char_keysym(char))
            if interval:
                time.sleep(interval)
