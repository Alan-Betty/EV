"""`open_app` on Linux finds what the app grid shows, not only what is on PATH.

"Find me Firefox" or "open Files" has to land on a program whatever its
binary is called - Nautilus, a snap wrapper, a flatpak. The `.desktop`
entries are what GNOME itself lists, so they are what gets indexed.

Offline: a temporary XDG tree, and launching is recorded, not done.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

import config  # noqa: E402
from tools import app_launcher  # noqa: E402
from tools.app_launcher import _scan_desktop_entries, open_app  # noqa: E402


def _entry(folder: Path, stem: str, body: str) -> Path:
    path = folder / f"{stem}.desktop"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    apps = tmp_path / "applications"
    apps.mkdir()
    _entry(apps, "firefox_firefox",
           "[Desktop Entry]\nType=Application\nName=Firefox\nGenericName=Web Browser\n"
           "Exec=firefox %u\n\n[Desktop Action new-window]\nName=New Window\nExec=firefox -new-window\n")
    _entry(apps, "org.gnome.Nautilus",
           "[Desktop Entry]\nType=Application\nName=Files\nExec=nautilus --new-window\n")
    _entry(apps, "hidden-helper",
           "[Desktop Entry]\nType=Application\nName=Helper Daemon\nNoDisplay=true\n")
    _entry(apps, "a-link", "[Desktop Entry]\nType=Link\nName=Some Website\nURL=https://x\n")
    monkeypatch.setattr(app_launcher, "_desktop_dirs", lambda: [apps])
    monkeypatch.setattr(app_launcher, "IS_LINUX", True)
    monkeypatch.setattr(app_launcher, "IS_WINDOWS", False)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(app_launcher, "_index", app_launcher._AppIndex())
    return apps


def test_visible_applications_are_indexed_by_name(xdg):
    apps = _scan_desktop_entries()
    assert apps["firefox"].endswith("firefox_firefox.desktop")
    assert apps["files"].endswith("org.gnome.Nautilus.desktop")


def test_actions_hidden_entries_and_links_are_not_programs(xdg):
    apps = _scan_desktop_entries()
    assert "new window" not in apps
    assert "helper daemon" not in apps
    assert "some website" not in apps


def test_a_generic_name_reaches_a_program_without_shadowing_one(xdg):
    apps = _scan_desktop_entries()
    assert apps["web browser"].endswith("firefox_firefox.desktop")


def test_a_program_off_path_is_launched_through_its_entry(xdg, monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(app_launcher, "resolve_executable", lambda name: None)
    monkeypatch.setattr(app_launcher, "_launch_desktop_entry", lambda path: launched.append(path) or True)
    result = open_app("files")
    assert result.ok
    assert launched and launched[0].endswith("org.gnome.Nautilus.desktop")


def test_find_me_phrasing_resolves_the_same_program(xdg, monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(app_launcher, "resolve_executable", lambda name: None)
    monkeypatch.setattr(app_launcher, "_launch_desktop_entry", lambda path: launched.append(path) or True)
    assert open_app("the Firefox app").ok
    assert launched[0].endswith("firefox_firefox.desktop")


def test_a_broken_entry_is_reported_not_claimed(xdg, monkeypatch):
    monkeypatch.setattr(app_launcher, "resolve_executable", lambda name: None)
    monkeypatch.setattr(app_launcher, "_launch_desktop_entry", lambda path: False)
    result = open_app("files")
    assert not result.ok
    assert "Can't find" in result.speech
