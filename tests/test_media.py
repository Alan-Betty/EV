"""`media_control`: the music already playing, and the volume.

Offline: every command `tools.media` would run is answered by a fake that
records the argv, so nothing reaches a real session bus or mixer.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

from tools import TOOL_NAMES, dispatch, guard, media  # noqa: E402
from tools.media import MediaError  # noqa: E402
from tools.schemas import select_tools  # noqa: E402

SPOTIFY = "org.mpris.MediaPlayer2.spotify"
BRAVE = "org.mpris.MediaPlayer2.brave.instance42"


class FakeSystem:
    """Answers gdbus and wpctl the way a GNOME session would."""

    def __init__(self, statuses: dict[str, str], volume: float = 0.5, deny: bool = False) -> None:
        self.statuses = statuses
        self.volume = volume
        self.deny = deny
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        if argv[0] == "gdbus":
            dest, method = argv[argv.index("--dest") + 1], argv[argv.index("--method") + 1]
            if method.endswith("ListNames"):
                names = ", ".join(f"'{name}'" for name in self.statuses)
                return f"(['org.freedesktop.DBus', {names}],)"
            if self.deny:
                raise MediaError("GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: An AppArmor policy prevents this")
            if method.endswith("Properties.Get") and argv[-1] == "PlaybackStatus":
                return f"(<'{self.statuses[dest]}'>,)"
            if method.endswith("Properties.Get") and argv[-1] == "Metadata":
                return ("(<{'mpris:trackid': <'/x'>, 'xesam:title': <\"Don't Stop Me Now\">, "
                        "'xesam:artist': <['Queen']>}>,)")
            return "()"
        if argv[0] == "wpctl":
            if argv[1] == "set-volume":
                value = argv[-1]
                if value.endswith("%+"):
                    self.volume += int(value[:-2]) / 100
                elif value.endswith("%-"):
                    self.volume -= int(value[:-2]) / 100
                else:
                    self.volume = int(value[:-1]) / 100
            if argv[1] == "get-volume":
                return f"Volume: {self.volume:.2f}"
            return ""
        raise MediaError(f"{argv[0]} is not installed")


@pytest.fixture
def system(monkeypatch):
    fake = FakeSystem({BRAVE: "Paused", SPOTIFY: "Playing"})
    monkeypatch.setattr(media, "_run", fake)
    monkeypatch.setattr(media, "IS_WINDOWS", False)
    monkeypatch.setattr(media, "IS_MAC", False)
    monkeypatch.setattr(media.shutil, "which", lambda name: f"/usr/bin/{name}" if name in ("gdbus", "wpctl") else None)
    return fake


def _methods(fake: FakeSystem) -> list[tuple[str, str]]:
    return [(c[c.index("--dest") + 1], c[c.index("--method") + 1])
            for c in fake.calls if c[0] == "gdbus"]


def test_pause_goes_to_the_player_that_is_actually_playing(system):
    result = media.media_control("pause")
    assert result.ok and result.speech == "Paused."
    assert (SPOTIFY, "org.mpris.MediaPlayer2.Player.Pause") in _methods(system)
    assert (BRAVE, "org.mpris.MediaPlayer2.Player.Pause") not in _methods(system)


def test_play_with_nothing_playing_resumes_the_paused_one(system):
    system.statuses = {BRAVE: "Paused"}
    assert media.media_control("play").ok
    assert (BRAVE, "org.mpris.MediaPlayer2.Player.Play") in _methods(system)


def test_spoken_shorthand_is_understood(system):
    assert media.media_control("skip").speech == "Next one."
    assert media.media_control("resume").speech == "Playing."


def test_now_playing_reads_title_and_artist(system):
    result = media.media_control("now_playing")
    assert result.speech == "Don't Stop Me Now by Queen."


def test_no_player_says_so_and_points_elsewhere(system):
    system.statuses = {}
    result = media.media_control("pause")
    assert not result.ok
    assert "Nothing's playing" in result.speech
    assert "browser_task" in result.detail


def test_a_sandboxed_player_is_reported_as_sandboxed_not_idle(system):
    system.deny = True
    result = media.media_control("pause")
    assert not result.ok
    assert "sandboxed" in result.speech
    assert "AppArmor" in result.detail


def test_volume_steps_and_reports_the_new_level(system):
    result = media.media_control("volume_up")
    assert result.ok and result.speech == "Volume's at 60."
    assert media.media_control("volume_down", level=30).speech == "Volume's at 30."


def test_set_volume_is_clamped_and_needs_a_number(system):
    assert media.media_control("set_volume", level=250).speech == "Volume's at 100."
    assert not media.media_control("set_volume").ok


def test_mute_does_not_claim_a_level(system):
    result = media.media_control("mute")
    assert result.speech == "Muted."
    assert ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"] in system.calls


def test_an_unknown_action_lists_the_valid_ones(system):
    result = media.media_control("rewind_time")
    assert not result.ok and "now_playing" in result.detail


def test_it_is_registered_and_reached_through_dispatch(system):
    assert "media_control" in TOOL_NAMES
    # dispatch stringifies loose types; the level has to survive that.
    result = dispatch("media_control", {"action": "set_volume", "level": 35})
    assert result.speech == "Volume's at 35."


def test_louder_three_times_does_not_lock_e_v_down(system):
    """Identical calls back to back are what the runaway limiter reads as a loop."""
    for _ in range(4):
        assert dispatch("media_control", {"action": "volume_up"}).ok
    assert not guard.is_locked_down()


def test_volume_words_offer_media_control_not_the_screen_family():
    chosen = select_tools("turn the volume up a bit")
    assert "media_control" in chosen
    assert "screen_task" not in chosen
    assert "media_control" in select_tools("pause the music")
    assert "media_control" in select_tools("what song is this")


def test_windows_presses_the_media_keys(monkeypatch):
    pressed: list[tuple[int, int]] = []
    monkeypatch.setattr(media, "IS_WINDOWS", True)
    monkeypatch.setattr(media, "_press", lambda vk, times=1: pressed.append((vk, times)))
    assert media.media_control("toggle").ok
    assert pressed == [(0xB3, 1)]
    pressed.clear()
    assert media.media_control("set_volume", level=40).speech == "Volume's at 40."
    # Floor first, then up: 50 presses down, 20 up at two points a press.
    assert pressed == [(0xAE, 50), (0xAF, 20)]
