"""The rename must not cost anyone their settings."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["LEO_TTS_ENABLED"] = "false"

import config  # noqa: E402


def test_the_new_name_is_read(monkeypatch):
    monkeypatch.setenv("LEO_SOMETHING", " new ")
    assert config._env("LEO_SOMETHING") == "new"


def test_the_old_name_still_works_and_is_noted(monkeypatch):
    monkeypatch.delenv("LEO_ONLY_OLD", raising=False)
    monkeypatch.setenv("EV_ONLY_OLD", "old")
    monkeypatch.setattr(config, "LEGACY_ENV", [])
    assert config._env("LEO_ONLY_OLD") == "old"
    assert config.LEGACY_ENV == ["EV_ONLY_OLD"]


def test_the_new_name_wins_and_says_nothing(monkeypatch):
    monkeypatch.setenv("LEO_BOTH", "new")
    monkeypatch.setenv("EV_BOTH", "old")
    monkeypatch.setattr(config, "LEGACY_ENV", [])
    assert config._env("LEO_BOTH") == "new"
    assert config.LEGACY_ENV == []


def test_an_empty_new_value_is_respected(monkeypatch):
    monkeypatch.setenv("LEO_EMPTY", "")
    monkeypatch.setenv("EV_EMPTY", "old")
    assert config._env("LEO_EMPTY", "default") == ""


def test_the_typed_readers_follow(monkeypatch):
    monkeypatch.delenv("LEO_NUM", raising=False)
    monkeypatch.setenv("EV_NUM", "7")
    assert config._env_int("LEO_NUM", 1) == 7


def test_an_old_env_file_boots_the_real_process(tmp_path):
    """The whole path: a real interpreter, nothing but the old name set."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EV_", "LEO_"))}
    env["EV_STATE_DIR"] = str(tmp_path / "state")
    # A copy, because `config` loads the `.env` beside it, and the developer's
    # own LEO_STATE_DIR would win.
    (tmp_path / "config.py").write_bytes((ROOT / "config.py").read_bytes())
    done = subprocess.run(
        [sys.executable, "-c", "import config; print(config.STATE_DIR, config.LEGACY_ENV)"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert str(tmp_path / "state") in done.stdout
    assert "EV_STATE_DIR" in done.stdout


def test_existing_state_loads_under_the_same_names():
    """Names on disk did not change, so nothing needs migrating."""
    assert config.MEMORY_FILE.name == "memory.json"
    assert config.BACKLOG_FILE.name == "backlog.json"
    assert config.SEMANTIC_FILE.name == "semantic_memory.json"


def test_the_extension_directory_matches_its_uuid():
    """GNOME refuses an extension whose folder and uuid disagree."""
    from tools.desktop import gnome

    assert gnome.SOURCE.is_dir()
    assert json.loads((gnome.SOURCE / "metadata.json").read_text())["uuid"] == gnome.UUID == gnome.SOURCE.name


def test_the_extension_and_its_client_agree_on_the_bus_name():
    from tools.desktop import gnome

    script = (gnome.SOURCE / "extension.js").read_text(encoding="utf-8")
    assert gnome.NAME in script and gnome.PATH in script


def test_an_old_copy_of_the_extension_is_still_reached(monkeypatch):
    from tools.desktop import gnome

    monkeypatch.setattr(gnome, "_owned", lambda name: name == gnome.LEGACY_NAME)
    assert gnome.available()
    assert gnome._address() == (gnome.LEGACY_NAME, gnome.LEGACY_PATH)
    monkeypatch.setattr(gnome, "_owned", lambda name: True)
    assert gnome._address() == (gnome.NAME, gnome.PATH)


def test_no_old_name_left_in_what_the_user_sees():
    for path in [ROOT / "leo_core.py", *(ROOT / "leo").rglob("*.py"), *(ROOT / "tools").rglob("*.py")]:
        assert "E.V." not in path.read_text(encoding="utf-8"), path
    assert "Everyday Virtual" not in config.SYSTEM_PROMPT
    assert "Electronic Visor" not in config.SYSTEM_PROMPT
    assert "billionaire" not in config.SYSTEM_PROMPT


def test_the_template_does_not_hand_out_the_old_wake_word():
    """A rename of the keys left `LEO_WAKE_PHRASES=ev,...` in new `.env` files."""
    line = next(
        line for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if "LEO_WAKE_PHRASES=" in line
    )
    assert "leo" in line and " ev" not in line and "=ev" not in line
