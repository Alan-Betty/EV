"""The microphone opens on a machine where `sounddevice` cannot.

On a stock Ubuntu the pip wheel for `sounddevice` installs fine and then
fails at import with "PortAudio library not found", because it loads the
*system* PortAudio and nothing installed it. The mic worked in every other
app and E.V. said "No microphone backend. Install sounddevice" - about a
package that was installed. These pin the recorder fallback and the message.

Offline: the "recorder" is this Python interpreter writing bytes to stdout.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

from ev import audio  # noqa: E402
from ev.audio import AudioError, Microphone, _CommandStream, _no_backend_message  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="recorder commands are POSIX")


def _fake_recorder(sample: bytes = b"\x10\x00") -> list[str]:
    """A process that streams PCM and then stays up, like a real recorder."""
    script = (
        "import sys, time\n"
        f"sys.stdout.buffer.write({sample!r} * 32000)\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    return [sys.executable, "-c", script]


def test_the_portaudio_case_names_the_system_package():
    message = _no_backend_message(["sounddevice: PortAudio library not found"])
    assert "libportaudio2" in message
    assert "is installed" in message


def test_every_backend_reason_is_kept_in_the_error():
    message = _no_backend_message(["sounddevice: boom", "pyaudio: no module", "command: none"])
    assert "boom" in message and "no module" in message


def test_a_recorder_stream_returns_whole_frames(monkeypatch):
    monkeypatch.setattr(_CommandStream, "_commands", staticmethod(lambda rate, device: [("fake", _fake_recorder())]))
    stream = _CommandStream.first_available(16000, 960)
    try:
        frame = stream.read()
        assert len(frame) == 960
        assert frame[:2] == b"\x10\x00"
    finally:
        stream.close()


def test_a_recorder_that_dies_at_once_is_skipped_for_the_next(monkeypatch):
    dead = [sys.executable, "-c", "raise SystemExit(3)"]
    monkeypatch.setattr(
        _CommandStream, "_commands",
        staticmethod(lambda rate, device: [("dead", dead), ("alive", _fake_recorder())]),
    )
    stream = _CommandStream.first_available(16000, 960)
    try:
        assert stream.name == "alive"
    finally:
        stream.close()


def test_no_recorder_installed_says_which_were_looked_for(monkeypatch):
    monkeypatch.setattr(
        _CommandStream, "_commands",
        staticmethod(lambda rate, device: [("pw-record", ["ev-no-such-recorder-xyz"])]),
    )
    with pytest.raises(AudioError, match="pw-record not installed"):
        _CommandStream.first_available(16000, 960)


def test_a_named_device_is_passed_to_every_recorder():
    commands = dict(_CommandStream._commands(16000, "bluez_input.headset"))
    assert "--target" in commands["pw-record"]
    assert "--device=bluez_input.headset" in commands["parec"]
    assert "-D" in commands["arecord"]
    # An index is a PortAudio idea; it is not handed to a sound-server tool.
    assert "--target" not in dict(_CommandStream._commands(16000, "3"))["pw-record"]


def test_the_microphone_falls_through_to_the_recorder(monkeypatch):
    def portaudio_missing(self):
        raise OSError("PortAudio library not found")

    def no_pyaudio(self):
        raise ImportError("No module named 'pyaudio'")

    monkeypatch.setattr(Microphone, "_open_sounddevice", portaudio_missing)
    monkeypatch.setattr(Microphone, "_open_pyaudio", no_pyaudio)
    monkeypatch.setattr(_CommandStream, "_commands", staticmethod(lambda rate, device: [("fake", _fake_recorder())]))

    mic = Microphone()
    mic.open()
    try:
        assert mic._backend == "command"
        item = mic.next_frame(timeout=3.0)
        assert item is not None
        frame, level = item
        assert len(frame) == mic.frame_bytes
        assert level > 0
    finally:
        mic.close()
    assert mic._stream is None


def test_with_nothing_at_all_the_error_says_what_to_install(monkeypatch):
    def fail(self):
        raise OSError("PortAudio library not found")

    monkeypatch.setattr(Microphone, "_open_sounddevice", fail)
    monkeypatch.setattr(Microphone, "_open_pyaudio", fail)
    monkeypatch.setattr(Microphone, "_open_command", fail)
    with pytest.raises(AudioError, match="libportaudio2"):
        Microphone().open()


def test_stock_ubuntu_has_a_way_to_play_mp3(monkeypatch):
    """GStreamer ships with Ubuntu; ffplay, mpv and mpg123 do not."""
    monkeypatch.setattr(audio, "IS_WINDOWS", False)
    monkeypatch.setattr(
        "tools.base.resolve_executable",
        lambda name: "/usr/bin/gst-play-1.0" if name == "gst-play-1.0" else None,
    )
    player = audio.build_player()
    assert player._template[0] == "gst-play-1.0"
