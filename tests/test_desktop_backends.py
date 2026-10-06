"""The desktop backends: which hands and eyes E.V. uses, and that they work.

Offline. Nothing here opens a session bus, a Mutter session or a real
/dev/uinput device: the session is a recorder, the uinput file descriptor
is fake and its ioctls are written down instead of performed.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

import config  # noqa: E402
from tools import computer_use  # noqa: E402
from tools.desktop import hands, keys, system, uinput  # noqa: E402
from tools.desktop.mutter import MutterHands  # noqa: E402


# ---------------------------------------------------------------------------
# Which session this is
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not system.IS_LINUX, reason="session types are a Linux question")
@pytest.mark.parametrize(
    "env, expected",
    [
        ({"XDG_SESSION_TYPE": "wayland"}, "wayland"),
        ({"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": "wayland-0"}, "x11"),
        # A Wayland session exports DISPLAY too - that is XWayland - so the
        # Wayland socket has to win when the type is not declared.
        ({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}, "wayland"),
        ({"DISPLAY": ":0"}, "x11"),
        ({}, ""),
    ],
)
def test_the_session_type_is_read_from_the_environment(monkeypatch, env, expected):
    for name in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert system.session_type() == expected


@pytest.mark.skipif(not system.IS_LINUX, reason="desktops are a Linux question")
def test_ubuntus_flavour_prefix_does_not_hide_gnome(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    assert system.desktop() == "gnome"


def test_the_planner_is_told_which_desktop_it_drives():
    line = system.prompt_line()
    assert line.startswith("This desktop is")
    assert ("cmd" in line) if system.IS_MAC else ("ctrl" in line)


# ---------------------------------------------------------------------------
# Choosing a backend
# ---------------------------------------------------------------------------
def test_a_forced_backend_is_used_as_asked(monkeypatch):
    monkeypatch.setattr(config, "INPUT_BACKEND", "uinput")
    monkeypatch.setattr(config, "CAPTURE_BACKEND", "mutter")
    assert hands.input_choice() == "uinput"
    assert hands.capture_choice() == "mutter"


def test_x11_and_other_systems_keep_pyautogui_and_mss(monkeypatch):
    monkeypatch.setattr(config, "INPUT_BACKEND", "auto")
    monkeypatch.setattr(config, "CAPTURE_BACKEND", "auto")
    monkeypatch.setattr(system, "is_wayland", lambda: False)
    assert hands.input_choice() == "pyautogui"
    assert hands.capture_choice() == "mss"


def test_gnome_wayland_prefers_mutter(monkeypatch):
    from tools.desktop import mutter

    monkeypatch.setattr(config, "INPUT_BACKEND", "auto")
    monkeypatch.setattr(config, "CAPTURE_BACKEND", "auto")
    monkeypatch.setattr(system, "is_wayland", lambda: True)
    monkeypatch.setattr(system, "desktop", lambda: "gnome")
    monkeypatch.setattr(mutter, "available", lambda: True)
    assert hands.input_choice() == "mutter"
    assert hands.capture_choice() == "mutter"


def test_other_wayland_desktops_fall_to_uinput_when_it_is_writable(monkeypatch):
    monkeypatch.setattr(config, "INPUT_BACKEND", "auto")
    monkeypatch.setattr(system, "is_wayland", lambda: True)
    monkeypatch.setattr(system, "desktop", lambda: "kde")
    monkeypatch.setattr(uinput, "writable", lambda: True)
    assert hands.input_choice() == "uinput"
    monkeypatch.setattr(uinput, "writable", lambda: False)
    assert hands.input_choice() == "pyautogui"


def test_an_unknown_backend_costs_the_capability_not_the_assistant(monkeypatch):
    monkeypatch.setattr(config, "INPUT_BACKEND", "telepathy")
    assert hands.hands() is None
    assert "telepathy" in hands.last_error()


# ---------------------------------------------------------------------------
# A black frame is not a picture
# ---------------------------------------------------------------------------
def test_a_black_frame_is_recognised_as_blank():
    from PIL import Image

    assert computer_use._is_blank(Image.new("RGB", (1920, 1080), (0, 0, 0)))
    busy = Image.new("RGB", (1920, 1080), (0, 0, 0))
    busy.paste((200, 200, 200), (100, 100, 400, 300))
    assert not computer_use._is_blank(busy)


def test_a_blank_grab_is_an_error_not_a_frame(monkeypatch):
    """Sent on, a black frame is answered confidently - "the screen is dark" -
    and every step after that is a guess."""
    from PIL import Image

    black = Image.new("RGB", (64, 36), (0, 0, 0))
    monkeypatch.setattr(computer_use, "_grab_with_imagegrab", lambda: black)
    monkeypatch.setattr(computer_use, "_grab_with_pyautogui", lambda: black)
    monkeypatch.setitem(sys.modules, "mss", None)  # import mss -> ImportError
    with pytest.raises(computer_use.CaptureError, match="black"):
        computer_use.capture_screen()


def test_a_compositor_frame_is_used_before_any_x11_grab(monkeypatch):
    import io

    from PIL import Image

    picture = Image.new("RGB", (320, 180), (10, 120, 200))
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG")
    monkeypatch.setattr(hands, "capture_png", lambda: buffer.getvalue())

    def never():
        raise AssertionError("an X11 grab should not be tried")

    monkeypatch.setattr(computer_use, "_grab_with_imagegrab", never)
    frame = computer_use.capture_screen()
    assert (frame.screen_width, frame.screen_height) == (320, 180)


def test_a_failed_compositor_capture_on_wayland_is_reported(monkeypatch):
    def broken():
        raise RuntimeError("pipewiresrc failed")

    monkeypatch.setattr(hands, "capture_png", broken)
    monkeypatch.setattr(computer_use.desktop_system, "is_wayland", lambda: True)
    with pytest.raises(computer_use.CaptureError, match="pipewiresrc"):
        computer_use.capture_screen()


# ---------------------------------------------------------------------------
# Mutter hands speak pyautogui
# ---------------------------------------------------------------------------
class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def size(self):
        return (1920, 1080)

    def move(self, x, y):
        self.calls.append(("move", x, y))

    def button(self, name, pressed):
        self.calls.append(("button", name, pressed))

    def scroll(self, steps, horizontal=False):
        self.calls.append(("scroll", steps))

    def key(self, sym, pressed):
        self.calls.append(("key", sym, pressed))


def test_mutter_clicks_where_it_was_told():
    session = FakeSession()
    MutterHands(session).click(x=960, y=540, clicks=2)
    assert session.calls[0] == ("move", 960, 540)
    presses = [call for call in session.calls if call[0] == "button"]
    assert presses == [("button", "left", True), ("button", "left", False)] * 2


def test_mutter_hotkeys_release_in_reverse_order():
    session = FakeSession()
    MutterHands(session).hotkey("ctrl", "s")
    syms = [(call[1], call[2]) for call in session.calls]
    ctrl, s = keys.keysym("ctrl"), keys.keysym("s")
    assert syms == [(ctrl, True), (s, True), (s, False), (ctrl, False)]


def test_mutter_types_characters_no_layout_has():
    """Keysyms carry any character, so an em dash is typed, not dropped."""
    session = FakeSession()
    MutterHands(session).write("a—é")
    downs = [call[1] for call in session.calls if call[2]]
    assert downs == [ord("a"), 0x01000000 + ord("—"), ord("é")]


def test_mutter_scroll_direction_matches_pyautogui():
    session = FakeSession()
    MutterHands(session).scroll(3)
    assert session.calls == [("scroll", -3)]


def test_unicode_capable_hands_type_instead_of_pasting(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(computer_use, "_clipboard_write", lambda text: pytest.fail("no paste"))
    assert computer_use._enter_text(MutterHands(session), "café " * 30) == "typed"


# ---------------------------------------------------------------------------
# uinput, against a fake device
# ---------------------------------------------------------------------------
class FakeKernel:
    def __init__(self) -> None:
        self.ioctls: list[tuple] = []
        self.writes: list[tuple[int, bytes]] = []
        self._next = 40

    def open(self) -> int:
        self._next += 1
        return self._next

    def ioctl(self, fd, request, arg=0):
        self.ioctls.append((fd, request, arg))
        return 0

    def write(self, fd, payload):
        self.writes.append((fd, payload))
        return len(payload)

    def events(self, fd: int) -> list[tuple[int, int, int]]:
        size = uinput._EVENT.size
        out = []
        for written_fd, payload in self.writes:
            if written_fd != fd:
                continue
            for offset in range(0, len(payload), size):
                _sec, _usec, kind, code, value = uinput._EVENT.unpack_from(payload, offset)
                if kind != uinput.EV_SYN:
                    out.append((kind, code, value))
        return out


def _devices(kernel: FakeKernel) -> uinput.UinputDevices:
    devices = uinput.UinputDevices(opener=kernel.open, ioctl=kernel.ioctl, writer=kernel.write)
    devices.settle_s = 0
    return devices


def test_uinput_builds_a_tablet_and_a_keyboard():
    kernel = FakeKernel()
    devices = _devices(kernel)
    devices.ensure()
    assert devices.pointer_fd != devices.keyboard_fd
    pointer = [call for call in kernel.ioctls if call[0] == devices.pointer_fd]
    assert (devices.pointer_fd, uinput.UI_SET_ABSBIT, uinput.ABS_X) in pointer
    assert (devices.pointer_fd, uinput.UI_SET_KEYBIT, uinput.BTN_LEFT) in pointer
    assert any(call[1] == uinput.UI_DEV_CREATE for call in pointer)
    keyboard = [call for call in kernel.ioctls if call[0] == devices.keyboard_fd]
    assert (devices.keyboard_fd, uinput.UI_SET_KEYBIT, keys.KEY["a"]) in keyboard
    # The keyboard must not look like a tablet, or libinput misfiles it.
    assert not any(call[1] == uinput.UI_SET_ABSBIT for call in keyboard)


def test_uinput_setup_blobs_have_the_kernels_sizes():
    assert len(uinput._setup_blob("x", 1)) == 92
    assert len(uinput._abs_blob(uinput.ABS_X, 65535)) == 28
    assert uinput._EVENT.size == 24


def test_uinput_maps_pixels_onto_the_tablet_range():
    kernel = FakeKernel()
    hand = uinput.UinputHands(_devices(kernel), (1920, 1080))
    hand.moveTo(1919, 0)
    assert kernel.events(hand.devices.pointer_fd) == [
        (uinput.EV_ABS, uinput.ABS_X, uinput.ABS_MAX),
        (uinput.EV_ABS, uinput.ABS_Y, 0),
    ]


def test_uinput_shifts_capitals_and_symbols():
    kernel = FakeKernel()
    hand = uinput.UinputHands(_devices(kernel), (1920, 1080))
    hand.write("A!")
    shift, a, one = keys.KEY["shift"], keys.KEY["a"], keys.KEY["1"]
    assert kernel.events(hand.devices.keyboard_fd) == [
        (uinput.EV_KEY, shift, 1), (uinput.EV_KEY, a, 1), (uinput.EV_KEY, a, 0), (uinput.EV_KEY, shift, 0),
        (uinput.EV_KEY, shift, 1), (uinput.EV_KEY, one, 1), (uinput.EV_KEY, one, 0), (uinput.EV_KEY, shift, 0),
    ]


def test_uinput_refuses_what_a_us_layout_cannot_type():
    """Refusing is what sends the text to the clipboard instead of producing
    the wrong characters."""
    hand = uinput.UinputHands(_devices(FakeKernel()), (1920, 1080))
    with pytest.raises(ValueError):
        hand.write("café")


def test_uinput_without_permission_says_how_to_get_it():
    def denied():
        raise PermissionError(13, "Permission denied")

    devices = uinput.UinputDevices(opener=denied, ioctl=lambda *a: 0, writer=lambda *a: 0)
    with pytest.raises(uinput.UinputUnavailable, match="input' group"):
        devices.ensure()


def test_event_struct_matches_a_64_bit_kernel():
    assert struct.calcsize("llHHi") == 24
