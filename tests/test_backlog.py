"""Backlog tests. Offline, and never touching the real backlog file.

The rule worth guarding here is the last one: a backlog entry is a reminder,
not a signed permission slip. Replaying a stored command must put it back
through whatever gate held it the first time, or "delete the build folder",
declined on Monday, would run unasked on Tuesday.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

import config  # noqa: E402
from ev.backlog import Backlog, get_backlog  # noqa: E402
from ev.tts import clean_for_speech  # noqa: E402
from tools import dispatch  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A backlog file in a temporary directory, isolated from the real one."""
    path = tmp_path / "backlog.json"
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "BACKLOG_FILE", path)
    monkeypatch.setattr(config, "BACKLOG_ENABLED", True)
    monkeypatch.setattr(config, "BACKLOG_CONFIRM_CLEAR", True)
    return Backlog()


@pytest.fixture
def files(tmp_path, monkeypatch):
    """A sandboxed home, so a replayed file command cannot reach a real one."""
    home = tmp_path / "home"
    documents = home / "Documents"
    documents.mkdir(parents=True)
    monkeypatch.setattr(config, "FILE_ROOTS", [home])
    monkeypatch.setattr(config, "USER_DIRS", {"home": home, "documents": documents})
    monkeypatch.setattr(config, "FILE_DEFAULT_DIR", documents)
    return documents


# -- storage -----------------------------------------------------------------
def test_items_survive_a_restart(store, tmp_path):
    store.add("copy the invoices to Documents", kind="interrupted")
    store.add("back up the photos", kind="reminder")

    reloaded = Backlog()
    assert [item.text for item in reloaded.pending()] == [
        "copy the invoices to Documents",
        "back up the photos",
    ]
    assert reloaded.pending()[0].kind == "interrupted"


def test_an_empty_item_is_not_stored(store):
    assert store.add("   ") is None
    assert store.pending() == []


def test_the_same_thing_failing_twice_is_one_item(store):
    first = store.add("copy the invoices")
    second = store.add("Copy The Invoices")
    # Five retries of one broken command should read back as one line, not five.
    assert first is not None and second is not None
    assert first.id == second.id
    assert len(store.pending()) == 1


def test_the_list_is_capped(store, monkeypatch):
    monkeypatch.setattr(config, "BACKLOG_MAX_ITEMS", 3)
    for index in range(6):
        store.add(f"thing {index}")
    assert len(store.pending()) == 3
    assert store.pending()[-1].text == "thing 5"


def test_a_corrupt_backlog_starts_empty_rather_than_crashing(store, tmp_path):
    store.add("something")
    (tmp_path / "backlog.json").write_text('{"items": [', encoding="utf-8")

    assert Backlog().pending() == []
    assert (tmp_path / "backlog.json.corrupt").exists()


def test_garbage_entries_are_skipped_not_fatal(store, tmp_path):
    (tmp_path / "backlog.json").write_text(
        json.dumps({"items": [{"text": "good"}, {"nope": 1}, "string", 7]}),
        encoding="utf-8",
    )
    assert [item.text for item in Backlog().pending()] == ["good"]


def test_a_disabled_backlog_writes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "backlog.json"
    monkeypatch.setattr(config, "BACKLOG_FILE", path)
    disabled = Backlog(enabled=False)
    assert disabled.add("something") is None
    assert not path.exists()


def test_the_shared_instance_follows_the_configured_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKLOG_ENABLED", True)
    monkeypatch.setattr(config, "BACKLOG_FILE", tmp_path / "one.json")
    get_backlog().add("first")

    monkeypatch.setattr(config, "BACKLOG_FILE", tmp_path / "two.json")
    assert get_backlog().pending() == []


# -- finding items -----------------------------------------------------------
def test_items_are_found_by_position_ordinal_id_or_text(store):
    first = store.add("copy the invoices")
    second = store.add("back up the photos")

    assert store.get("1").id == first.id
    assert store.get("first").id == first.id
    assert store.get("2").id == second.id
    assert store.get("last").id == second.id
    assert store.get(first.id).id == first.id
    assert store.get("photos").id == second.id
    assert store.get("9") is None
    assert store.get("nothing like this") is None
    assert store.get("") is None


def test_completing_and_dropping_take_items_off_the_list(store):
    store.add("one")
    store.add("two")

    assert store.complete("1").text == "one"
    assert [item.text for item in store.pending()] == ["two"]
    # Completed items stay in the file until pruned, but never read back.
    assert len(store.items) == 2

    assert store.prune() == 1
    assert len(store.items) == 1

    assert store.drop("two").text == "two"
    assert store.pending() == []


def test_clearing_empties_the_list(store):
    store.add("one")
    store.add("two")
    assert store.clear() == 2
    assert Backlog().pending() == []


# -- safety ------------------------------------------------------------------
def test_a_stored_command_never_carries_its_confirmation(store):
    """The gate that held a command must hold it again on the retry."""
    item = store.add(
        "delete the build folder",
        kind="interrupted",
        tool="file_manager",
        args={"action": "delete", "path": "build", "confirmed": True},
    )
    assert "confirmed" not in item.args
    assert "confirmed" not in Backlog().pending()[0].args


def test_replaying_a_gated_command_asks_again(store, files):
    doomed = files / "notes.txt"
    doomed.write_text("keep me", encoding="utf-8")

    store.add(
        "delete notes.txt",
        tool="file_manager",
        args={"action": "delete", "path": str(doomed)},
    )
    result = dispatch("backlog", {"action": "run", "item": "1"})

    assert result.needs_confirmation is True
    assert doomed.exists()
    # Still outstanding: it was asked about, not done.
    assert len(Backlog().pending()) == 1


def test_a_replayed_command_that_succeeds_comes_off_the_list(store, files):
    store.add(
        "make a note",
        tool="file_manager",
        args={"action": "create", "path": "note.txt", "content": "hello"},
    )
    result = dispatch("backlog", {"action": "run", "item": "first"})

    assert result.ok is True
    assert (files / "note.txt").read_text(encoding="utf-8") == "hello"
    assert Backlog().pending() == []


def test_a_failed_replay_stays_on_the_list(store, files):
    store.add(
        "read the missing file",
        tool="file_manager",
        args={"action": "read", "path": "gone.txt"},
    )
    assert dispatch("backlog", {"action": "run", "item": "1"}).ok is False
    assert len(Backlog().pending()) == 1


def test_a_plain_reminder_cannot_be_run(store):
    store.add("phone the dentist", kind="reminder")
    result = dispatch("backlog", {"action": "run", "item": "1"})
    assert result.ok is False
    assert "note, not a command" in result.speech


# -- the `backlog` tool ------------------------------------------------------
def test_the_tool_reports_an_empty_list_without_fuss(store):
    result = dispatch("backlog", {"action": "list"})
    assert result.ok is True
    assert result.detail == "The backlog is empty."


def test_the_tool_adds_lists_and_ticks_off(store):
    added = dispatch("backlog", {"action": "add", "text": "back up the photos"})
    assert added.ok is True

    listed = dispatch("backlog", {"action": "list"})
    assert "back up the photos" in listed.detail
    assert "back up the photos" in listed.speech

    done = dispatch("backlog", {"action": "done", "item": "1"})
    assert done.ok is True
    assert dispatch("backlog", {"action": "list"}).detail == "The backlog is empty."


def test_adding_nothing_is_refused(store):
    result = dispatch("backlog", {"action": "add"})
    assert result.ok is False
    assert "needs a text argument" in result.detail


def test_an_item_that_does_not_exist_is_reported_not_guessed(store):
    store.add("one")
    result = dispatch("backlog", {"action": "done", "item": "nothing like this"})
    assert result.ok is False
    assert len(Backlog().pending()) == 1


def test_clearing_the_whole_list_asks_first(store):
    store.add("one")
    store.add("two")

    held = dispatch("backlog", {"action": "clear"})
    assert held.needs_confirmation is True
    assert len(Backlog().pending()) == 2

    # `confirmed` is injected by the core loop after a spoken yes.
    ran = dispatch("backlog", {"action": "clear", "confirmed": True})
    assert ran.ok is True
    assert Backlog().pending() == []


def test_the_model_cannot_set_the_confirmation_itself(store):
    """`confirmed` is allow-listed for the loop, but the schema never offers it."""
    from tools.schemas import TOOL_SPECS

    spec = next(s for s in TOOL_SPECS if s["name"] == "backlog")
    assert "confirmed" not in spec["parameters"]["properties"]


def test_an_unknown_backlog_action_fails_without_raising(store):
    result = dispatch("backlog", {"action": "incinerate"})
    assert result.ok is False
    assert "incinerate" in result.detail


# -- reporting ---------------------------------------------------------------
def test_the_boot_summary_counts_what_is_left(store):
    assert store.summary() == ""

    store.add("copy the invoices")
    assert store.summary().startswith("One thing still open")

    store.add("back up the photos")
    summary = store.summary()
    assert "2 backlog items remaining from your previous session" in summary


def test_the_model_context_lists_items_but_the_speech_does_not(store):
    store.add("copy the invoices", tool="file_manager", args={"action": "copy"})
    assert "file_manager" in store.context()
    # Tool names are machine detail and have no business being read aloud.
    assert "file_manager" not in store.summary()


def test_backlog_speech_carries_no_labels(store):
    store.add("copy the invoices")
    store.add("back up the photos")
    for spoken in (store.summary(), dispatch("backlog", {"action": "list"}).speech):
        assert clean_for_speech(spoken) == spoken
