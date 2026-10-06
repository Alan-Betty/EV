"""Virtual pointer and keyboard on /dev/uinput, for Wayland desktops without Mutter.

KDE, sway and Hyprland have no equivalent of Mutter's remote-desktop API
that answers a plain session process, and XTest only reaches XWayland
windows. A uinput device is below all of that: the kernel presents it as a
real mouse and keyboard, and every compositor reads it like one. It needs
write access to `/dev/uinput`, which on Ubuntu means membership of the
`input` group; without it this backend says so and is skipped.

Two devices, not one. The pointer copies the shape of QEMU's USB tablet -
absolute X/Y, three buttons and a wheel, nothing else - because that is the
shape libinput reliably treats as "an absolute pointer" and maps across the
desktop. A relative mouse would be at the mercy of pointer acceleration, so
"move 300px" would land somewhere different on every machine. Keys live on
a second device, because a pointer that also reports a hundred keys stops
looking like a tablet to the classifier.

Pure `fcntl.ioctl` and `struct` - no `python-evdev`, nothing compiled.
"""

from __future__ import annotations

import fcntl
import logging
import os
import struct
import threading
import time
from typing import Any

from tools.desktop import keys

log = logging.getLogger("ev.tools.desktop.uinput")

DEVICE = "/dev/uinput"

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0x00, 0x01, 0x02, 0x03
SYN_REPORT = 0
REL_HWHEEL, REL_WHEEL = 0x06, 0x08
ABS_X, ABS_Y = 0x00, 0x01
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
_BUTTONS = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}

ABS_MAX = 65535
BUS_VIRTUAL = 0x06

# ioctl numbers from linux/uinput.h, precomputed for x86-64 and arm64 (the
# encoding is the same on both).
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_ABSBIT = 0x40045567
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405C5503   # _IOW('U', 3, struct uinput_setup), 92 bytes
UI_ABS_SETUP = 0x401C5504   # _IOW('U', 4, struct uinput_abs_setup), 28 bytes

_EVENT = struct.Struct("llHHi")  # struct input_event on a 64-bit kernel


class UinputUnavailable(RuntimeError):
    """No /dev/uinput, or no permission to write it."""


def writable() -> bool:
    return os.access(DEVICE, os.W_OK)


def _setup_blob(name: str, product: int) -> bytes:
    raw = name.encode("ascii", "replace")[:79]
    return struct.pack("HHHH80sI", BUS_VIRTUAL, 0x1EF5, product, 1, raw, 0)


def _abs_blob(code: int, maximum: int) -> bytes:
    # struct uinput_abs_setup { __u16 code; struct input_absinfo absinfo; }
    # input_absinfo is six __s32 and 4-byte aligned, so two bytes of padding.
    return struct.pack("Hxxiiiiii", code, 0, 0, maximum, 0, 0, 0)


class UinputDevices:
    """The tablet and the keyboard, created once and kept for the process."""

    def __init__(self, opener: Any = None, ioctl: Any = None, writer: Any = None) -> None:
        # Injectable so the test suite can record ioctls against a fake fd
        # instead of creating real devices on the developer's machine.
        self._open = opener or (lambda: os.open(DEVICE, os.O_WRONLY | os.O_NONBLOCK))
        self._ioctl = ioctl or fcntl.ioctl
        self._write = writer or os.write
        self._lock = threading.Lock()
        self.pointer_fd: int | None = None
        self.keyboard_fd: int | None = None
        self.settle_s = 0.3

    def _create(self, name: str, product: int, configure: Any) -> int:
        try:
            fd = self._open()
        except OSError as exc:
            raise UinputUnavailable(
                f"cannot open {DEVICE} ({exc.strerror}); add yourself to the 'input' group"
            ) from exc
        try:
            configure(fd)
            self._ioctl(fd, UI_DEV_SETUP, _setup_blob(name, product))
            self._ioctl(fd, UI_DEV_CREATE)
        except OSError as exc:
            os.close(fd)
            raise UinputUnavailable(f"uinput refused the device: {exc}") from exc
        return fd

    def ensure(self) -> None:
        with self._lock:
            if self.pointer_fd is not None:
                return

            def pointer(fd: int) -> None:
                self._ioctl(fd, UI_SET_EVBIT, EV_KEY)
                for button in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE):
                    self._ioctl(fd, UI_SET_KEYBIT, button)
                self._ioctl(fd, UI_SET_EVBIT, EV_REL)
                self._ioctl(fd, UI_SET_RELBIT, REL_WHEEL)
                self._ioctl(fd, UI_SET_RELBIT, REL_HWHEEL)
                self._ioctl(fd, UI_SET_EVBIT, EV_ABS)
                for axis in (ABS_X, ABS_Y):
                    self._ioctl(fd, UI_SET_ABSBIT, axis)
                    self._ioctl(fd, UI_ABS_SETUP, _abs_blob(axis, ABS_MAX))

            def keyboard(fd: int) -> None:
                self._ioctl(fd, UI_SET_EVBIT, EV_KEY)
                for code in sorted(set(keys.KEY.values())):
                    self._ioctl(fd, UI_SET_KEYBIT, code)

            self.pointer_fd = self._create("E.V. virtual tablet", 0x0001, pointer)
            self.keyboard_fd = self._create("E.V. virtual keyboard", 0x0002, keyboard)
            # The compositor needs a moment to notice a new device; events
            # sent before it has are dropped without a word.
            if self.settle_s:
                time.sleep(self.settle_s)

    def close(self) -> None:
        with self._lock:
            for fd in (self.pointer_fd, self.keyboard_fd):
                if fd is None:
                    continue
                try:
                    self._ioctl(fd, UI_DEV_DESTROY)
                except OSError:
                    pass
                try:
                    os.close(fd)
                except OSError:
                    pass
            self.pointer_fd = self.keyboard_fd = None

    def emit(self, fd: int | None, *events: tuple[int, int, int]) -> None:
        if fd is None:
            raise UinputUnavailable("the uinput device is not open")
        payload = b"".join(_EVENT.pack(0, 0, kind, code, value) for kind, code, value in events)
        payload += _EVENT.pack(0, 0, EV_SYN, SYN_REPORT, 0)
        self._write(fd, payload)


class UinputHands:
    """pyautogui's vocabulary on top of the two uinput devices.

    Coordinates arrive in screen pixels and are scaled onto the tablet's
    0-65535 range against the desktop size. Typing assumes a US layout for
    printable ASCII; anything else is refused here so that `_enter_text`
    pastes it instead of producing the wrong characters.
    """

    name = "uinput"
    types_unicode = False

    def __init__(self, devices: UinputDevices, size: tuple[int, int]) -> None:
        self.devices = devices
        self._size = size
        self._pos = (0.0, 0.0)

    def size(self) -> tuple[int, int]:
        return self._size

    def _abs(self, value: float, extent: int) -> int:
        if extent <= 1:
            return 0
        return max(0, min(ABS_MAX, round(float(value) * ABS_MAX / (extent - 1))))

    def moveTo(self, x: float, y: float, duration: float = 0.0, **_: Any) -> None:  # noqa: N802
        self.devices.ensure()
        width, height = self._size
        self.devices.emit(
            self.devices.pointer_fd,
            (EV_ABS, ABS_X, self._abs(x, width)),
            (EV_ABS, ABS_Y, self._abs(y, height)),
        )
        self._pos = (float(x), float(y))
        if duration:
            time.sleep(min(duration, 0.2))

    def _button(self, name: str, pressed: bool) -> None:
        self.devices.ensure()
        self.devices.emit(self.devices.pointer_fd, (EV_KEY, _BUTTONS.get(name, BTN_LEFT), int(pressed)))

    def click(self, x: Any = None, y: Any = None, clicks: int = 1, button: str = "left", **_: Any) -> None:
        if x is not None and y is not None:
            self.moveTo(x, y)
        for index in range(max(1, int(clicks))):
            self._button(button, True)
            time.sleep(0.02)
            self._button(button, False)
            if index + 1 < clicks:
                time.sleep(0.06)

    def dragTo(self, x: float, y: float, duration: float = 0.3, button: str = "left", **_: Any) -> None:  # noqa: N802
        start_x, start_y = self._pos
        self._button(button, True)
        steps = 12
        for step in range(1, steps + 1):
            fraction = step / steps
            self.moveTo(start_x + (x - start_x) * fraction, start_y + (y - start_y) * fraction)
            time.sleep(max(0.01, duration / steps))
        self._button(button, False)

    def scroll(self, clicks: int, x: Any = None, y: Any = None, **_: Any) -> None:
        if x is not None and y is not None:
            self.moveTo(x, y)
        self.devices.ensure()
        # REL_WHEEL: positive is up, the same as pyautogui.
        self.devices.emit(self.devices.pointer_fd, (EV_REL, REL_WHEEL, int(clicks)))

    def _key(self, code: int, pressed: bool) -> None:
        self.devices.ensure()
        self.devices.emit(self.devices.keyboard_fd, (EV_KEY, code, int(pressed)))

    def _code(self, name: str) -> int:
        code = keys.evdev_key(name)
        if code is None:
            raise ValueError(f"unknown key {name!r}")
        return code

    def press(self, key: str, presses: int = 1, **_: Any) -> None:
        code = self._code(key)
        for _index in range(max(1, int(presses))):
            self._key(code, True)
            self._key(code, False)

    def hotkey(self, *names: str, **_: Any) -> None:
        codes = [self._code(name) for name in names]
        for code in codes:
            self._key(code, True)
        for code in reversed(codes):
            self._key(code, False)

    def write(self, text: str, interval: float = 0.0, **_: Any) -> None:
        plan = [keys.evdev_char(char) for char in text]
        if any(step is None for step in plan):
            raise ValueError("text contains characters a US layout cannot type")
        shift = keys.KEY["shift"]
        for code, shifted in plan:  # type: ignore[misc]
            if shifted:
                self._key(shift, True)
            self._key(code, True)
            self._key(code, False)
            if shifted:
                self._key(shift, False)
            if interval:
                time.sleep(interval)
