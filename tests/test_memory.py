"""Persistent memory tests. Offline, and never touching the real state file.

Every test redirects `config.MEMORY_FILE` at a temporary directory, so the
developer's own memory.json is neither read nor written by the suite.

The interesting cases here are the ungraceful ones. A voice assistant that
gets killed by a Windows update must come back knowing who it is talking to,
and a memory file half-written by a power cut must not stop it booting at all.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

import config  # noqa: E402
from ev.memory import Memory, get_memory, humanise_gap, read_json, write_json  # noqa: E402
from ev.tts import clean_for_speech  # noqa: E402
from tools import dispatch  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A memory file in a temporary directory, isolated from the real one."""
    path = tmp_path / "memory.json"
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "MEMORY_FILE", path)
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    return path


# -- the JSON store ----------------------------------------------------------
def test_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "thing.json"
    assert write_json(path, {"a": 1}) is True
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    # A temp file left behind means a crash mid-write would litter the folder.
    assert [p.name for p in tmp_path.iterdir()] == ["thing.json"]


def test_rewriting_keeps_exactly_one_file(tmp_path):
    path = tmp_path / "thing.json"
    write_json(path, {"a": 1})
    write_json(path, {"a": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2}
    assert len(list(tmp_path.iterdir())) == 1


def test_a_corrupt_file_is_set_aside_rather_than_raising(tmp_path):
    path = tmp_path / "memory.json"
    path.write_text('{"preferences": {"coffee"', encoding="utf-8")

    assert read_json(path, {"fresh": True}) == {"fresh": True}
    # The damaged file is kept for forensics, but out of the way.
    assert (tmp_path / "memory.json.corrupt").exists()


def test_a_missing_file_reads_as_the_default(tmp_path):
    assert read_json(tmp_path / "nope.json", {"x": 1}) == {"x": 1}


def test_json_that_is_not_an_object_reads_as_the_default(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_json(path, {"x": 1}) == {"x": 1}


# -- durations ---------------------------------------------------------------
@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "under a minute"),
        (59, "under a minute"),
        (60, "1 minute"),
        (135, "2 minutes"),
        (3600, "1 hour"),
        (8100, "2 hours and 15 minutes"),
        (7200, "2 hours"),
        (90000, "1 day and 1 hour"),
        (2 * 86400, "2 days"),
        (30 * 86400, "30 days"),
    ],
)
def test_gaps_are_phrased_the_way_a_person_would_say_them(seconds, expected):
    assert humanise_gap(seconds) == expected


def test_a_negative_gap_never_produces_nonsense():
    # A restored backup or a clock change can put the last timestamp ahead.
    assert humanise_gap(-500) == "under a minute"


# -- session lifecycle -------------------------------------------------------
def test_first_run_has_nothing_to_report(state):
    report = Memory().begin_session()
    assert report.first_run is True
    assert report.greeting == ""
    assert "first session" in report.context


def test_the_gap_between_runs_is_measured_and_spoken(state, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_MIN_GAP_S", 900.0)
    first = Memory()
    first.begin_session()
    first.end_session(clean=True)

    # Rewind the stored timestamp by two and a quarter hours.
    data = json.loads(state.read_text(encoding="utf-8"))
    data["state"]["last_active"] = time.time() - 8100
    state.write_text(json.dumps(data), encoding="utf-8")

    report = Memory().begin_session()
    assert report.first_run is False
    assert report.offline_phrase == "2 hours and 15 minutes"
    assert report.greeting == "Welcome back. You were offline for 2 hours and 15 minutes."
    assert "2 hours and 15 minutes" in report.context


def test_a_quick_restart_is_not_greeted(state, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_MIN_GAP_S", 900.0)
    first = Memory()
    first.begin_session()
    first.end_session(clean=True)

    report = Memory().begin_session()
    assert report.clean_shutdown is True
    # Being welcomed home every time you restart a process gets old fast.
    assert report.greeting == ""


def test_an_unclean_exit_is_visible_on_the_next_start(state):
    killed = Memory()
    killed.begin_session()
    # No end_session: this is the power cut.
    del killed

    report = Memory().begin_session()
    assert report.clean_shutdown is False
    assert "badly" in report.greeting
    assert "unexpectedly" in report.context


def test_session_counting_and_duration_survive_a_restart(state):
    first = Memory()
    first.begin_session()
    first.end_session(clean=True)

    second = Memory()
    report = second.begin_session()
    assert report.sessions == 1
    second.end_session(clean=True)

    stored = json.loads(state.read_text(encoding="utf-8"))["state"]
    assert stored["sessions"] == 2
    assert stored["clean_shutdown"] is True
    assert stored["total_seconds"] >= 0.0


def test_the_clean_flag_is_cleared_on_disk_immediately(state):
    memory = Memory()
    memory.begin_session()
    # Written before anything else happens, because nothing gets a chance to
    # write it once the power goes.
    stored = json.loads(state.read_text(encoding="utf-8"))
    assert stored["state"]["clean_shutdown"] is False


def test_touch_is_throttled_but_forcible(state, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_TOUCH_INTERVAL_S", 3600.0)
    memory = Memory()
    memory.begin_session()
    stamped = json.loads(state.read_text(encoding="utf-8"))["state"]["last_active"]

    memory.touch()
    assert json.loads(state.read_text(encoding="utf-8"))["state"]["last_active"] == stamped

    time.sleep(0.01)
    memory.touch(force=True)
    assert json.loads(state.read_text(encoding="utf-8"))["state"]["last_active"] > stamped


def test_a_clock_that_ran_backwards_does_not_produce_a_negative_gap(state):
    Memory().begin_session()
    data = json.loads(state.read_text(encoding="utf-8"))
    data["state"]["last_active"] = time.time() + 10_000
    state.write_text(json.dumps(data), encoding="utf-8")

    report = Memory().begin_session()
    assert report.offline_seconds == 0.0
    assert report.offline_phrase == "under a minute"


# -- facts -------------------------------------------------------------------
def test_facts_survive_a_restart(state):
    memory = Memory()
    assert memory.remember("Coffee", "black") is True
    memory.remember("editor", "VS Code")

    reloaded = Memory()
    assert reloaded.recall("coffee") == "black"
    assert reloaded.recall("COFFEE") == "black"
    assert reloaded.preferences == {"coffee": "black", "editor": "VS Code"}


def test_profile_facts_are_kept_apart_from_preferences(state):
    memory = Memory()
    memory.remember("name", "Alan", profile=True)
    memory.remember("coffee", "black")

    reloaded = Memory()
    assert reloaded.profile == {"name": "Alan"}
    assert reloaded.preferences == {"coffee": "black"}
    assert reloaded.recall("name") == "Alan"


def test_forgetting_removes_the_fact_from_disk(state):
    memory = Memory()
    memory.remember("coffee", "black")
    assert memory.forget("coffee") is True
    assert memory.forget("coffee") is False
    assert Memory().recall("coffee") is None


def test_an_empty_key_is_refused(state):
    memory = Memory()
    assert memory.remember("   ", "value") is False
    assert memory.recall("") is None


def test_stored_facts_are_capped(state, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_MAX_ENTRIES", 3)
    memory = Memory()
    for index in range(6):
        memory.remember(f"key{index}", str(index))

    # The prompt carries these on every turn, so the file cannot grow forever.
    assert len(Memory().preferences) == 3
    assert "key5" in Memory().preferences


def test_context_is_empty_until_something_is_remembered(state):
    memory = Memory()
    assert memory.context() == ""
    memory.remember("coffee", "black")
    assert "coffee is black" in memory.context()


def test_a_disabled_memory_writes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "memory.json"
    monkeypatch.setattr(config, "MEMORY_FILE", path)
    memory = Memory(enabled=False)
    memory.begin_session()
    memory.remember("coffee", "black")
    memory.end_session()
    assert not path.exists()


def test_the_shared_instance_follows_the_configured_path(tmp_path, monkeypatch):
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)

    monkeypatch.setattr(config, "MEMORY_FILE", first)
    get_memory().remember("coffee", "black")

    monkeypatch.setattr(config, "MEMORY_FILE", second)
    assert get_memory().recall("coffee") is None
    assert get_memory().path == second


# -- the `remember` tool -----------------------------------------------------
def test_the_remember_tool_round_trips_through_dispatch(state):
    stored = dispatch("remember", {"action": "set", "key": "coffee", "value": "black"})
    assert stored.ok is True

    found = dispatch("remember", {"action": "get", "key": "coffee"})
    assert found.ok is True
    assert "black" in found.speech

    listed = dispatch("remember", {"action": "list"})
    assert listed.ok is True
    assert "coffee: black" in listed.detail

    dropped = dispatch("remember", {"action": "forget", "key": "coffee"})
    assert dropped.ok is True
    assert dispatch("remember", {"action": "get", "key": "coffee"}).ok is False


def test_the_remember_tool_refuses_a_half_given_fact(state):
    result = dispatch("remember", {"action": "set", "key": "coffee"})
    assert result.ok is False
    assert "needs both" in result.detail


def test_an_unknown_memory_action_fails_without_raising(state):
    result = dispatch("remember", {"action": "obliterate"})
    assert result.ok is False
    assert "obliterate" in result.detail


# -- speech purity -----------------------------------------------------------
def test_memory_speech_carries_no_labels(state):
    """Everything spoken here goes through the same boundary as any reply."""
    Memory().begin_session()
    data = json.loads(state.read_text(encoding="utf-8"))
    data["state"]["last_active"] = time.time() - 8100
    state.write_text(json.dumps(data), encoding="utf-8")

    greeting = Memory().begin_session().greeting
    assert clean_for_speech(greeting) == greeting

    spoken = dispatch(
        "remember", {"action": "set", "key": "coffee", "value": "black"}
    ).speech
    assert clean_for_speech(spoken) == spoken
