"""The three named memory tools, and the to-do list underneath them.

Offline, and never touching the real state file: every test redirects
`config.MEMORY_FILE` at a temporary directory, so the developer's own
memory.json is neither read nor written by the suite.

Two properties matter more than the individual verbs:

* the to-do list and the backlog are **separate stores**, so a phrase aimed at
  one cannot empty the other, and
* everything these tools say out loud survives `clean_for_speech` unchanged,
  because it reaches the speaker on the same path as any other reply.
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
import ev.memory as memory_module  # noqa: E402
from ev.memory import Memory, get_memory  # noqa: E402
from ev.tts import clean_for_speech  # noqa: E402
from tools import _ALLOWED_ARGS, REGISTRY, dispatch  # noqa: E402
from tools.schemas import TOOL_NAMES, TOOL_SPECS  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Point the whole memory system at a temp file and start empty."""
    path = tmp_path / "memory.json"
    monkeypatch.setattr(config, "MEMORY_FILE", path)
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(memory_module, "_memory", None)
    return path


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
def test_the_three_tools_are_declared_and_registered():
    for name in ("remember_fact", "recall_fact", "manage_todo"):
        assert name in TOOL_NAMES, f"{name} is not in TOOL_SPECS"
        assert name in REGISTRY, f"{name} has no implementation"


def test_allowed_args_track_the_schema():
    """`dispatch` filters to the declared properties, so they must agree."""
    for name in ("remember_fact", "recall_fact", "manage_todo"):
        spec = next(s for s in TOOL_SPECS if s["name"] == name)
        declared = set(spec["parameters"].get("properties", {}))
        assert declared <= _ALLOWED_ARGS[name]


def test_clearing_the_list_is_gated_like_a_delete():
    """`confirmed` is injected by the core loop, never set by the model."""
    assert "confirmed" in _ALLOWED_ARGS["manage_todo"]
    spec = next(s for s in TOOL_SPECS if s["name"] == "manage_todo")
    assert "confirmed" not in spec["parameters"]["properties"], (
        "the model must not be able to confirm its own destructive call"
    )


def test_the_schema_budget_still_leaves_room_for_two_turns():
    """Groq's free tier meters 8000 tokens a minute, and two turns have to
    fit, or the second half of a compound request fails by construction
    rather than by accident.

    This measures the *whole* schema, which an ordinary turn no longer sends:
    `select_tools` filters it per utterance and takes about 70% off. The
    ceiling still matters because the full schema is what the tool-failure
    ladder puts back on - so a turn that stumbles costs this much, and two of
    those in a minute is the worst case that must still fit. It is also the
    cost of running with `EV_TOOL_SUBSET_ENABLED=false`.
    """
    schema = len(json.dumps([
        {"type": "function", "function": {
            "name": s["name"],
            "description": s["description"],
            "parameters": s["parameters"],
        }} for s in TOOL_SPECS
    ])) // 4
    prompt = len(config.SYSTEM_PROMPT) // 4
    assert 2 * (schema + prompt) < 8000, (
        f"floor is {schema + prompt} tokens; two turns would be "
        f"{2 * (schema + prompt)}"
    )


# ---------------------------------------------------------------------------
# facts
# ---------------------------------------------------------------------------
def test_a_fact_round_trips_through_dispatch(state):
    stored = dispatch("remember_fact", {"key": "coffee", "value": "black"})
    assert stored.ok

    found = dispatch("recall_fact", {"key": "coffee"})
    assert found.ok
    assert "black" in found.detail


def test_a_fact_survives_a_restart(state):
    dispatch("remember_fact", {"key": "name", "value": "Alan"})
    # A fresh store reading the same file is what a reboot looks like.
    assert Memory(state).recall("name") == "Alan"


def test_recall_without_a_key_lists_everything(state):
    dispatch("remember_fact", {"key": "coffee", "value": "black"})
    dispatch("remember_fact", {"key": "editor", "value": "VS Code"})
    listed = dispatch("recall_fact", {})
    assert listed.ok
    assert "coffee" in listed.detail and "editor" in listed.detail


def test_a_fact_with_no_value_is_refused_rather_than_stored(state):
    result = dispatch("remember_fact", {"key": "coffee"})
    assert result.ok is False
    assert Memory(state).recall("coffee") is None


def test_an_unknown_fact_says_so(state):
    result = dispatch("recall_fact", {"key": "nothing"})
    assert result.ok is False
    assert clean_for_speech(result.speech) == result.speech


# ---------------------------------------------------------------------------
# to-do list
# ---------------------------------------------------------------------------
def test_a_todo_round_trips_and_persists(state):
    assert dispatch("manage_todo", {"action": "add", "item": "buy milk"}).ok
    assert Memory(state).open_todos() == ["buy milk"]


def test_listing_names_every_open_item(state):
    dispatch("manage_todo", {"action": "add", "item": "buy milk"})
    dispatch("manage_todo", {"action": "add", "item": "call the dentist"})
    listed = dispatch("manage_todo", {"action": "list"})
    assert "buy milk" in listed.detail
    assert "call the dentist" in listed.detail


def test_an_item_can_be_completed_by_position_or_by_words(state):
    dispatch("manage_todo", {"action": "add", "item": "buy milk"})
    dispatch("manage_todo", {"action": "add", "item": "call the dentist"})

    assert dispatch("manage_todo", {"action": "done", "item": "first"}).ok
    assert Memory(state).open_todos() == ["call the dentist"]

    assert dispatch("manage_todo", {"action": "done", "item": "dentist"}).ok
    assert Memory(state).open_todos() == []


def test_a_completed_item_is_kept_but_no_longer_open(state):
    dispatch("manage_todo", {"action": "add", "item": "buy milk"})
    dispatch("manage_todo", {"action": "done", "item": "buy milk"})
    store = Memory(state)
    assert store.open_todos() == []
    assert [row["text"] for row in store.todos] == ["buy milk"]


def test_dropping_removes_the_row_outright(state):
    dispatch("manage_todo", {"action": "add", "item": "buy milk"})
    assert dispatch("manage_todo", {"action": "drop", "item": "milk"}).ok
    assert Memory(state).todos == []


def test_adding_the_same_thing_twice_does_not_duplicate_it(state):
    dispatch("manage_todo", {"action": "add", "item": "buy milk"})
    dispatch("manage_todo", {"action": "add", "item": "Buy Milk"})
    assert Memory(state).open_todos() == ["buy milk"]


def test_completing_something_that_is_not_there_says_so(state):
    result = dispatch("manage_todo", {"action": "done", "item": "wash the car"})
    assert result.ok is False
    assert clean_for_speech(result.speech) == result.speech


def test_clearing_asks_first_and_only_then_clears(state):
    dispatch("manage_todo", {"action": "add", "item": "buy milk"})

    asked = dispatch("manage_todo", {"action": "clear"})
    assert asked.needs_confirmation is True
    assert Memory(state).open_todos() == ["buy milk"], "cleared without a yes"

    done = dispatch("manage_todo", {"action": "clear", "confirmed": True})
    assert done.ok
    assert Memory(state).todos == []


def test_the_model_cannot_confirm_its_own_clear(state):
    """`confirmed` in the model's arguments is filtered, not honoured.

    It is in `_ALLOWED_ARGS` so the *core loop* can inject it after a spoken
    yes. This checks the other half: that arriving as a string from the model
    is not enough. `dispatch` coerces it, so what matters is that a falsy
    string cannot pass for a confirmation.
    """
    dispatch("manage_todo", {"action": "add", "item": "buy milk"})
    result = dispatch("manage_todo", {"action": "clear", "confirmed": "no"})
    assert result.needs_confirmation is True
    assert Memory(state).open_todos() == ["buy milk"]


def test_the_list_is_bounded(state, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_MAX_TODOS", 3)
    store = get_memory()
    for index in range(6):
        store.add_todo(f"errand {index}")
    assert len(store.todos) <= 3


def test_an_unknown_action_is_reported_in_english(state):
    result = dispatch("manage_todo", {"action": "obliterate"})
    assert result.ok is False
    assert clean_for_speech(result.speech) == result.speech


# ---------------------------------------------------------------------------
# the two stores stay separate
# ---------------------------------------------------------------------------
def test_forgetting_the_facts_leaves_the_to_do_list_alone(state):
    """"Forget what you know about me" is about preferences, not errands."""
    store = get_memory()
    store.remember("coffee", "black")
    store.add_todo("buy milk")

    store.clear()

    assert store.recall("coffee") is None
    assert store.open_todos() == ["buy milk"]


def test_the_to_do_list_is_not_the_backlog(state, tmp_path, monkeypatch):
    """Clearing E.V.'s own backlog must not touch the user's list.

    They are different stores in different files on purpose. Merged, "clear
    the backlog" would delete the dentist appointment.
    """
    import ev.backlog as backlog_module
    from ev.backlog import Backlog

    monkeypatch.setattr(config, "BACKLOG_FILE", tmp_path / "backlog.json")
    monkeypatch.setattr(backlog_module, "_backlog", None)

    store = get_memory()
    store.add_todo("call the dentist")

    shelf = Backlog()
    shelf.add("retry the file copy")
    shelf.clear()

    assert store.open_todos() == ["call the dentist"]
    assert config.MEMORY_FILE != config.BACKLOG_FILE


# ---------------------------------------------------------------------------
# durability and context
# ---------------------------------------------------------------------------
def test_a_corrupt_file_costs_the_list_but_not_the_boot(state):
    state.write_text('{"todos": [{"text": "buy mi', encoding="utf-8")
    store = Memory(state)
    assert store.open_todos() == []
    assert store.add_todo("buy milk") is True


def test_a_malformed_row_is_dropped_rather_than_crashing(state):
    state.write_text(
        json.dumps({"todos": [{"text": "buy milk"}, "not a dict", {"nope": 1}]}),
        encoding="utf-8",
    )
    assert Memory(state).open_todos() == ["buy milk"]


def test_open_items_reach_the_model_context_and_the_spoken_summary(state):
    store = get_memory()
    store.add_todo("buy milk")
    store.remember("coffee", "black")

    assert "buy milk" in store.context()
    assert "coffee" in store.context()

    summary = store.todo_summary()
    assert "buy milk" in summary
    # It is spoken, so it has to survive the speech filter untouched.
    assert clean_for_speech(summary) == summary


def test_the_context_is_capped_however_long_the_list_gets(state, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_CONTEXT_TODOS", 2)
    store = get_memory()
    for index in range(10):
        store.add_todo(f"errand number {index}")
    context = store.context()
    assert "errand number 0" in context
    assert "errand number 9" not in context
    assert "10 open" in context


def test_a_disabled_memory_answers_rather_than_raising(state, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setattr(memory_module, "_memory", None)
    for name, args in (
        ("remember_fact", {"key": "a", "value": "b"}),
        ("recall_fact", {"key": "a"}),
        ("manage_todo", {"action": "list"}),
    ):
        result = dispatch(name, args)
        assert result.ok is False
        assert clean_for_speech(result.speech) == result.speech


def test_the_old_remember_tool_still_works(state):
    """Not in `TOOL_SPECS` any more, so it costs nothing - but still callable.

    A model that half-recalls the schema reaches for it, and `dispatch` would
    otherwise answer a perfectly sensible call with "I don't have a tool for
    that".
    """
    assert "remember" not in TOOL_NAMES
    assert dispatch("remember", {"action": "set", "key": "coffee", "value": "black"}).ok
    assert dispatch("remember", {"action": "get", "key": "coffee"}).ok
