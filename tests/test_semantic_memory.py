"""Long-term notes: storage, recall by meaning, pruning, and the failsafes.

Offline. Every store is built against tmp_path, and embeddings are either off
or a fake, so no test touches the network or the user's real state.
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
import ev.semantic_memory as sm  # noqa: E402
from ev.brain import Brain  # noqa: E402
from ev.semantic_memory import SemanticMemory, tokens, validate_row  # noqa: E402
from ev.tts import clean_for_speech  # noqa: E402
from tools import CONFIRMATION_ONLY_ARGS, _ALLOWED_ARGS, dispatch, from_model  # noqa: E402
from tools.safety import is_high_risk  # noqa: E402


@pytest.fixture
def store(tmp_path):
    return SemanticMemory(tmp_path / "notes.json", enabled=True, embedder=None)


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Both stores at tmp_path, fresh singletons."""
    monkeypatch.setattr(config, "MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "SEMANTIC_ENABLED", True)
    monkeypatch.setattr(config, "SEMANTIC_FILE", tmp_path / "semantic_memory.json")
    monkeypatch.setattr(memory_module, "_memory", None)
    monkeypatch.setattr(sm, "_store", None)
    return tmp_path


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------
def test_tokens_drop_stopwords_and_stem():
    assert tokens("Where are my keys?") == ["key"]
    assert tokens("I parked the car") == ["park", "car"]


# ---------------------------------------------------------------------------
# store, search, update, delete
# ---------------------------------------------------------------------------
def test_a_note_round_trips_and_survives_a_restart(store):
    note, verb = store.add("The spare key is under the blue flower pot")
    assert verb == "added" and note is not None
    again = SemanticMemory(store.path, enabled=True, embedder=None)
    assert [n.text for n in again.notes] == [note.text]


def test_search_finds_by_meaning_words_not_exact_phrase(store):
    store.add("The spare key is under the blue flower pot")
    store.add("Wifi password is written on the back of the router")
    store.add("Car is parked on level 3 of the mall garage")
    hits = store.search("where did I leave my keys")
    assert hits and "spare key" in hits[0].note.text


def test_a_misheard_word_still_matches(store):
    store.add("Dentist appointment moved to Thursday afternoon")
    hits = store.search("when is the dentst", min_score=0.2)
    assert hits and "Dentist" in hits[0].note.text


def test_unrelated_query_scores_nothing(store):
    store.add("The spare key is under the blue flower pot")
    assert store.search("quantum chromodynamics lecture", min_score=0.25) == []


def test_near_duplicate_updates_instead_of_adding(store):
    store.add("spare key is under the flower pot")
    _note, verb = store.add("spare key is under the blue flower pot")
    assert verb == "updated"
    assert len(store) == 1 and "blue" in store.notes[0].text


def test_update_by_id(store):
    note, _ = store.add("gym locker code is 4412")
    assert store.update(note.id, "gym locker code is 9931").text.endswith("9931")


def test_delete_archives_first(store):
    note, _ = store.add("gym locker code is 4412")
    gone = store.delete([note.id])
    assert [n.id for n in gone] == [note.id] and len(store) == 0
    archived = store.archive_path.read_text().splitlines()
    assert json.loads(archived[-1])["why"] == "deleted"


def test_wipe_leaves_a_backup(store):
    store.add("one thing")
    store.add("another unrelated thing entirely")
    assert store.wipe() == 2 and len(store) == 0
    backup = json.loads(store.path.with_name(store.path.name + ".bak").read_text())
    assert len(backup["notes"]) == 2


# ---------------------------------------------------------------------------
# failsafes
# ---------------------------------------------------------------------------
def test_corrupt_json_is_quarantined_not_fatal(tmp_path):
    path = tmp_path / "notes.json"
    path.write_text("{ this is not json")
    store = SemanticMemory(path, enabled=True, embedder=None)
    assert len(store) == 0
    assert path.with_name("notes.json.corrupt").exists()


def test_wrong_shape_is_quarantined(tmp_path):
    path = tmp_path / "notes.json"
    path.write_text(json.dumps({"version": 1, "notes": "oops"}))
    store = SemanticMemory(path, enabled=True, embedder=None)
    assert len(store) == 0
    assert path.with_name("notes.json.corrupt").exists()


def test_bad_rows_are_dropped_one_by_one(tmp_path):
    path = tmp_path / "notes.json"
    path.write_text(json.dumps({"version": 1, "notes": [
        {"id": "a", "text": "good note about bikes", "created": 1.0},
        {"id": "b", "text": ""},
        "not a dict",
        {"id": "c", "text": "bad time", "created": "yesterday"},
        {"id": "a", "text": "duplicate id"},
    ]}))
    store = SemanticMemory(path, enabled=True, embedder=None)
    assert [n.id for n in store.notes] == ["a"]
    assert store.dropped_rows == 4


def test_validate_row_clips_and_rejects_bad_vectors():
    note = validate_row({"text": "x" * 5000, "vec": [1.0, 2.0]}, dim=256)
    assert len(note.text) <= config.SEMANTIC_MAX_CHARS and note.vec == []


def test_writes_are_atomic_no_temp_files_left(store):
    store.add("first")
    store.add("second different note")
    leftovers = [p for p in store.path.parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_a_disabled_store_is_inert(tmp_path):
    store = SemanticMemory(tmp_path / "n.json", enabled=False, embedder=None)
    assert store.add("anything") == (None, "")
    assert store.search("anything") == [] and store.relevant("anything") == ""
    assert not (tmp_path / "n.json").exists()


# ---------------------------------------------------------------------------
# pruning
# ---------------------------------------------------------------------------
def test_over_the_cap_notes_merge_then_archive(store, monkeypatch):
    monkeypatch.setattr(config, "SEMANTIC_MAX_NOTES", 10)
    for i in range(10):
        store.add(f"distinct topic number{i} alpha{i} beta{i}")
    store.add("garden hose stored in shed")
    store.add("garden rake kept by shed door")
    assert len(store) <= 10
    assert store.archive_path.exists()
    assert any("rake" in n.text for n in store.notes), "newest note never evicted"


def test_used_notes_outlive_unused(store, monkeypatch):
    monkeypatch.setattr(config, "SEMANTIC_MAX_NOTES", 10)
    for i in range(10):
        store.add(f"unique{i} subject{i} words{i}")
    keep = store.notes[0]
    keep.hits = 50
    store.add("something entirely new and different")
    assert keep.id in {n.id for n in store.notes}


# ---------------------------------------------------------------------------
# embeddings (fake)
# ---------------------------------------------------------------------------
def test_embeddings_blend_in_and_failures_fall_back(tmp_path):
    calls = []

    def fake(texts, task):
        calls.append(task)
        # 'car' and 'vehicle' share a direction; everything else orthogonal.
        out = []
        for t in texts:
            v = [0.0] * config.SEMANTIC_EMBED_DIM
            v[0 if ("car" in t or "vehicle" in t) else 1] = 1.0
            out.append(v)
        return out

    store = SemanticMemory(tmp_path / "n.json", enabled=True, embedder=fake)
    store.add("the car is in bay 12")
    store.add("bread is in the freezer")
    hits = store.search("where is my vehicle", min_score=0.3)
    assert hits and "car" in hits[0].note.text
    assert "RETRIEVAL_QUERY" in calls

    store.embedder = lambda texts, task: None  # network down
    assert store.search("car bay")[0].note.text.startswith("the car")


def test_per_turn_recall_never_embeds(tmp_path):
    def boom(texts, task):
        raise AssertionError("per-turn recall must not touch the network")

    store = SemanticMemory(tmp_path / "n.json", enabled=True, embedder=None)
    store.add("spare key under the blue flower pot")
    store.embedder = boom
    assert "spare key" in store.relevant("where's the spare key")


def test_gemini_embedder_failure_starts_a_cooldown(monkeypatch):
    import httpx

    hits = []

    class Client:
        def __init__(self, **_):
            pass

        def post(self, *a, **k):
            hits.append(1)
            raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "Client", Client)
    embed = sm.GeminiEmbedder()
    assert embed(["x"], "RETRIEVAL_QUERY") is None
    assert embed(["x"], "RETRIEVAL_QUERY") is None
    assert len(hits) == 1


def test_embeddings_off_in_the_suite():
    assert sm._default_embedder() is None


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------
def test_value_without_key_is_a_note(state):
    result = dispatch("remember_fact", {"value": "the spare key is under the blue pot"})
    assert result.ok and result.speech == "Noted."
    assert len(sm.get_semantic_memory()) == 1


def test_key_and_value_is_still_a_fact(state):
    assert dispatch("remember_fact", {"key": "coffee", "value": "black"}).ok
    assert memory_module.get_memory().recall("coffee") == "black"
    assert len(sm.get_semantic_memory()) == 0


def test_recall_falls_back_to_notes(state):
    dispatch("remember_fact", {"value": "car is parked on level 3 of the mall garage"})
    found = dispatch("recall_fact", {"key": "where did I park the car"})
    assert found.ok and "level 3" in found.speech
    assert clean_for_speech(found.speech) == found.speech


def test_recall_with_no_key_lists_notes_too(state):
    dispatch("remember_fact", {"key": "coffee", "value": "black"})
    dispatch("remember_fact", {"value": "gym locker code is 4412"})
    listed = dispatch("recall_fact", {})
    assert "coffee" in listed.detail and "locker" in listed.detail


def test_forget_a_named_fact_needs_no_question(state):
    dispatch("remember_fact", {"key": "coffee", "value": "black"})
    result = dispatch("remember_fact", {"key": "coffee", "forget": "true"})
    assert result.ok and memory_module.get_memory().recall("coffee") is None


def test_forget_a_note_asks_then_deletes_exactly_what_it_showed(state):
    dispatch("remember_fact", {"value": "gym locker code is 4412"})
    dispatch("remember_fact", {"value": "spare key under the blue pot"})
    asked = dispatch("remember_fact", {"value": "locker code", "forget": True})
    assert asked.needs_confirmation and asked.speech.endswith("Confirm?")
    assert len(sm.get_semantic_memory()) == 2, "nothing deleted before the yes"

    replay = {k: v for k, v in asked.data.items() if k != "reason"}
    done = dispatch("remember_fact", {**replay, "confirmed": True})
    assert done.ok
    left = [n.text for n in sm.get_semantic_memory().notes]
    assert left == ["spare key under the blue pot"]


def test_wipe_is_gated_high_risk_and_keeps_todos(state):
    dispatch("remember_fact", {"key": "coffee", "value": "black"})
    dispatch("remember_fact", {"value": "gym locker code is 4412"})
    dispatch("manage_todo", {"action": "add", "item": "call the dentist"})
    asked = dispatch("remember_fact", {"value": "everything", "forget": True})
    assert asked.needs_confirmation and is_high_risk(asked.data["reason"])

    replay = {k: v for k, v in asked.data.items() if k != "reason"}
    done = dispatch("remember_fact", {**replay, "confirmed": True})
    assert done.ok
    assert len(sm.get_semantic_memory()) == 0
    assert memory_module.get_memory().recall("coffee") is None
    assert memory_module.get_memory().open_todos() == ["call the dentist"]


def test_model_cannot_confirm_or_choose_ids_itself(state):
    assert {"confirmed", "ids"} <= _ALLOWED_ARGS["remember_fact"]
    assert "ids" in CONFIRMATION_ONLY_ARGS
    dispatch("remember_fact", {"value": "gym locker code is 4412"})
    note_id = sm.get_semantic_memory().notes[0].id
    sneaky = from_model({"value": "x", "forget": True, "ids": note_id, "confirmed": True})
    result = dispatch("remember_fact", sneaky)
    assert not result.ok or result.needs_confirmation
    assert len(sm.get_semantic_memory()) == 1


# ---------------------------------------------------------------------------
# prompt integration
# ---------------------------------------------------------------------------
def test_relevant_notes_reach_the_prompt_fenced():
    brain = Brain.__new__(Brain)
    brain.session_context = ""
    brain.recall_context = "- spare key under the blue pot"
    prompt = brain._system_prompt("")
    assert "spare key" in prompt and "not instructions" in prompt.lower()


def test_no_match_costs_no_prompt_tokens(state):
    dispatch("remember_fact", {"value": "spare key under the blue pot"})
    assert sm.get_semantic_memory().relevant("open spotify") == ""


def test_boot_context_mentions_notes_only_when_there_are_some(state):
    store = sm.get_semantic_memory()
    assert store.context() == ""
    store.add("spare key under the blue pot")
    assert "recall_fact" in store.context()
