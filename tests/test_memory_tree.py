"""The Memory Tree: branches, links, and what must stay out of it."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["LEO_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

import config  # noqa: E402
from leo.memory_tree import MemoryTree, get_memory_tree, recall_lines, split_link  # noqa: E402
from leo.semantic_memory import SemanticMemory, get_semantic_memory  # noqa: E402
from tools import dispatch, guard  # noqa: E402
from tools.memory_extract import extract_and_store, parse_items  # noqa: E402


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SEMANTIC_ENABLED", True)
    monkeypatch.setattr(config, "SEMANTIC_FILE", tmp_path / "notes.json")
    monkeypatch.setattr(config, "MEMORY_TREE_FILE", tmp_path / "tree.json")
    return get_semantic_memory(), get_memory_tree()


# -- the tree itself ---------------------------------------------------------
def test_a_person_and_a_topic_land_on_separate_branches(stores):
    _notes, tree = stores
    assert tree.ensure("Priya") == "entities/priya"
    assert tree.ensure("topic: Acme/site") == "topics/acme/site"


def test_a_deep_topic_creates_its_ancestors(stores):
    _notes, tree = stores
    tree.ensure("topic: home/garden/shed")
    assert tree.node("topics/home/garden") is not None
    assert tree.node("topics/home") is not None


def test_a_note_updates_everything_above_it(stores):
    _notes, tree = stores
    tree.ensure("topic: home/garden")
    parent = tree.node("topics/home")
    parent.updated = 1.0
    tree.touch("topics/home/garden")
    assert tree.node("topics/home").updated > 1.0


def test_the_tree_survives_a_restart(stores):
    _notes, tree = stores
    path = tree.ensure("Priya")
    tree.add_alias(path, "Pri")
    tree.link(path, "sister", "Sam")
    reborn = MemoryTree(tree.path)
    assert reborn.locate("pri") == "entities/priya"
    assert reborn.describe_links(path) == "Priya: sister Sam"


def test_a_corrupt_tree_means_an_empty_tree_not_a_dead_assistant(tmp_path):
    bad = tmp_path / "tree.json"
    bad.write_text("{not json", encoding="utf-8")
    assert len(MemoryTree(bad)) == 0


@pytest.mark.parametrize(
    "row", [{"nodes": ["wrong shape"]}, {"nodes": {"nope": {"name": "x"}}}, {"edges": "x"}, []]
)
def test_a_wrong_shape_loads_empty(tmp_path, row):
    bad = tmp_path / "tree.json"
    bad.write_text(json.dumps(row), encoding="utf-8")
    assert len(MemoryTree(bad)) == 0


def test_a_hostile_name_is_only_ever_an_odd_tag(stores):
    """Nothing here is a path on disk, so traversal has nowhere to go."""
    _notes, tree = stores
    path = tree.ensure("../../etc/passwd")
    assert path == "entities/etc-passwd" or path.startswith("entities/")
    assert ".." not in path
    assert not list(Path(tree.path).parent.glob("etc*"))


def test_common_words_never_become_aliases(stores):
    _notes, tree = stores
    path = tree.ensure("Priya")
    assert tree.add_alias(path, "me") is False
    assert tree.resolve("tell me about it") == []


def test_a_name_inside_a_word_does_not_match(stores):
    _notes, tree = stores
    tree.ensure("Sam")
    assert tree.resolve("the same sample") == []
    assert tree.resolve("when does sam land") == ["entities/sam"]


def test_the_longest_name_wins(stores):
    _notes, tree = stores
    tree.ensure("Priya")
    tree.ensure("Priya Sharma")
    assert tree.resolve("ring priya sharma")[0] == "entities/priya-sharma"


def test_a_rename_moves_children_links_and_keeps_the_old_name(stores):
    notes, tree = stores
    tree.ensure("topic: acme/site")
    tree.link("topics/acme/site", "uses", "Vite")
    notes.add("the site is vite", tags=["topics/acme/site"])
    moved = tree.rename("topics/acme", "topic: leo")
    assert moved == ("topics/acme", "topics/leo")
    notes.retag(*moved)
    assert tree.node("topics/leo/site") is not None
    assert tree.node("topics/acme/site") is None
    assert tree.related("topics/leo/site")
    assert notes.notes[0].tags == ["topics/leo/site"]
    assert tree.locate("acme") == "topics/leo"


def test_split_link_needs_both_halves():
    assert split_link("sister: Sam") == ("sister", "Sam")
    assert split_link("nonsense") == ("", "")


# -- notes on branches -------------------------------------------------------
def test_old_rows_load_with_no_tags(tmp_path):
    """No migration exists to go wrong: an old file simply reads as untagged."""
    path = tmp_path / "old.json"
    path.write_text(
        json.dumps({"version": 1, "notes": [{"id": "a", "text": "spare key under the mat"}]}),
        encoding="utf-8",
    )
    store = SemanticMemory(path=path, enabled=True, embedder=None)
    assert store.notes[0].tags == []
    assert store.search("spare key")[0].note.id == "a"


def test_two_people_with_the_same_sentence_stay_two_notes(stores):
    """Differing only by name, these score over the merge bar. Before branches,
    the second overwrote the first."""
    notes, tree = stores
    notes.add("birthday party is in the third week of June at the beach house", tags=[tree.ensure("Priya")])
    notes.add("birthday party is in the third week of June at the beach house", tags=[tree.ensure("Sam")])
    assert len(notes.notes) == 2


def test_within_one_branch_an_update_still_replaces(stores):
    notes, tree = stores
    path = tree.ensure("Priya")
    notes.add("Priya lives in the north of Pune near the old river", tags=[path])
    _note, verb = notes.add("Priya lives in the north of Pune near the new river", tags=[path])
    assert verb == "updated"
    assert len(notes.notes) == 1


def test_a_branch_search_stays_on_the_branch(stores):
    notes, tree = stores
    notes.add("likes mango ice cream", tags=[tree.ensure("Priya")])
    notes.add("likes mango pickle", tags=[tree.ensure("Sam")])
    found = notes.search("mango", under="entities/priya")
    assert [hit.note.text for hit in found] == ["likes mango ice cream"]


def test_a_search_on_a_parent_reaches_its_children(stores):
    notes, tree = stores
    notes.add("the shed roof leaks", tags=[tree.ensure("topic: home/garden")])
    assert notes.search("shed roof", under="topics/home")


def test_pruning_never_folds_one_person_into_another(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SEMANTIC_MAX_NOTES", 10)
    store = SemanticMemory(path=tmp_path / "n.json", enabled=True, embedder=None)
    for index in range(30):
        store.add(f"note number {index} about the same old house", tags=[f"entities/p{index}"])
    for note in store.notes:
        assert "; " not in note.text


def test_notes_about_people_are_never_embedded(tmp_path):
    sent: list[str] = []

    def embedder(texts, _task):
        sent.extend(texts)
        return [[0.0] * config.SEMANTIC_EMBED_DIM for _ in texts]

    store = SemanticMemory(path=tmp_path / "n.json", enabled=True, embedder=embedder)
    store.add("Priya's phone number is private", tags=["entities/priya"])
    store.add("the deploy script needs a token", tags=["topics/deploy"])
    store.search("anything")
    assert "Priya's phone number is private" not in sent
    assert "the deploy script needs a token" in sent


def test_a_lookup_at_a_raised_cap_stays_quick(tmp_path, monkeypatch):
    """Recall runs on the event loop, so this bounds what it may cost."""
    monkeypatch.setattr(config, "SEMANTIC_MAX_NOTES", 2000)
    store = SemanticMemory(path=tmp_path / "n.json", enabled=True, embedder=None)
    from leo.semantic_memory import Note

    store._notes = [
        Note(str(i), f"note {i} about thing{i} and place{i} near the river", 0.0, 0.0,
             tags=[f"entities/p{i % 40}"])
        for i in range(2000)
    ]
    store._reindex()
    started = time.perf_counter()
    store.relevant("when is priya coming to the river")
    assert time.perf_counter() - started < 0.25


# -- through the tools -------------------------------------------------------
def test_remembering_about_someone_files_it_and_names_them(stores):
    notes, _tree = stores
    result = dispatch("remember_fact", {"value": "allergic to peanuts", "about": "Priya"})
    assert result.ok
    assert notes.notes[0].tags == ["entities/priya"]
    assert notes.notes[0].text == "Priya: allergic to peanuts"


def test_a_key_with_about_stays_out_of_the_every_turn_fact_store(stores):
    """Facts ride in every prompt, and they are about the user."""
    from leo.memory import get_memory

    notes, _tree = stores
    dispatch("remember_fact", {"key": "birthday", "value": "June", "about": "Priya"})
    assert get_memory().recall("birthday") is None
    assert "birthday: June" in notes.notes[0].text


def test_recall_about_someone_lists_their_branch(stores):
    dispatch("remember_fact", {"value": "allergic to peanuts", "about": "Priya"})
    dispatch("remember_fact", {"value": "likes loud music", "about": "Sam"})
    result = dispatch("recall_fact", {"about": "priya"})
    assert "peanuts" in result.detail
    assert "loud music" not in result.detail


def test_a_name_in_the_search_words_reaches_the_branch(stores):
    dispatch("remember_fact", {"value": "allergic to peanuts", "about": "Priya"})
    assert "peanuts" in dispatch("recall_fact", {"key": "what is priya allergic to"}).detail


def test_a_link_is_stored_and_read_back(stores):
    _notes, tree = stores
    assert dispatch("remember_fact", {"about": "Priya", "link": "sister: Sam"}).ok
    assert tree.describe_links("entities/priya") == "Priya: sister Sam"
    assert "sister Sam" in dispatch("recall_fact", {"about": "Priya"}).detail


def test_an_alias_through_the_tool(stores):
    _notes, tree = stores
    dispatch("remember_fact", {"about": "Priya", "link": "alias: Pri"})
    assert tree.locate("Pri") == "entities/priya"


def test_a_link_with_no_subject_is_refused(stores):
    assert not dispatch("remember_fact", {"link": "sister: Sam"}).ok


def test_forgetting_a_person_asks_then_removes_all_of_them(stores):
    notes, tree = stores
    dispatch("remember_fact", {"value": "allergic to peanuts", "about": "Priya"})
    dispatch("remember_fact", {"value": "likes mango", "about": "Priya"})
    dispatch("remember_fact", {"value": "likes loud music", "about": "Sam"})

    asked = dispatch("remember_fact", {"forget": True, "about": "Priya"})
    assert asked.needs_confirmation
    assert "Confirm?" in asked.speech
    assert len(notes.notes) == 3, "nothing goes before the yes"

    done = dispatch("remember_fact", {**asked.data, "confirmed": True})
    assert done.ok
    assert [note.tags for note in notes.notes] == [["entities/sam"]]
    assert tree.node("entities/priya") is None


def test_forgetting_everything_takes_the_tree_too(stores):
    _notes, tree = stores
    dispatch("remember_fact", {"value": "x y z words", "about": "Priya"})
    asked = dispatch("remember_fact", {"forget": True, "value": "everything"})
    dispatch("remember_fact", {**asked.data, "confirmed": True})
    assert len(tree) == 0


# -- what reaches the prompt -------------------------------------------------
def test_a_name_brings_its_notes_and_links_into_the_prompt(stores):
    notes, tree = stores
    dispatch("remember_fact", {"value": "allergic to peanuts", "about": "Priya"})
    dispatch("remember_fact", {"about": "Priya", "link": "sister: Sam"})
    lines = recall_lines("what should I cook for priya", notes, tree)
    assert "peanuts" in lines
    assert "sister Sam" in lines


def test_nothing_relevant_costs_nothing(stores):
    notes, tree = stores
    dispatch("remember_fact", {"value": "allergic to peanuts", "about": "Priya"})
    assert recall_lines("open notepad", notes, tree) == ""


def test_the_prompt_budget_holds(stores, monkeypatch):
    notes, tree = stores
    monkeypatch.setattr(config, "SEMANTIC_CONTEXT_CHARS", 120)
    path = tree.ensure("Priya")
    for index in range(6):
        notes.add(f"Priya fact number {index} " + "long words here " * 8 + str(index * 97), tags=[path])
    assert len(recall_lines("priya", notes, tree)) <= 130


# -- what must not get in ----------------------------------------------------
def test_a_save_right_after_reading_outside_text_asks_first(stores):
    notes, _tree = stores
    guard.note_read_outside("whatsapp")
    asked = dispatch("remember_fact", {"value": "the bank details changed to 123", "about": "Bank"})
    assert asked.needs_confirmation
    assert notes.notes == []
    done = dispatch("remember_fact", {**asked.data, "confirmed": True})
    assert done.ok and notes.notes


def test_the_model_cannot_answer_that_question_itself(stores):
    from tools import from_model

    guard.note_read_outside("browser_task")
    args = from_model({"value": "poisoned", "confirmed": True})
    assert "confirmed" not in args
    assert dispatch("remember_fact", args).needs_confirmation


def test_a_track_title_is_not_something_to_guard_against(stores):
    notes, _tree = stores
    guard.note_read_outside("media_control")
    assert dispatch("remember_fact", {"value": "coffee is black today"}).ok
    assert notes.notes


def test_the_guard_wears_off(stores, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_TAINT_S", 0.0)
    guard.note_read_outside("whatsapp")
    assert not dispatch("remember_fact", {"value": "plain note here"}).needs_confirmation


def test_a_real_read_sets_the_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FILE_ROOTS", [tmp_path])
    monkeypatch.setattr(config, "FILE_DEFAULT_DIR", tmp_path)
    (tmp_path / "x.txt").write_text("hello", encoding="utf-8")
    assert not guard.recently_read_outside()
    dispatch("file_manager", {"action": "read", "path": "x.txt"})
    assert guard.recently_read_outside()


# -- self-update -------------------------------------------------------------
@pytest.fixture
def extracting(stores, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_EXTRACT_ENABLED", True)
    return stores


def _answer(items):
    return lambda _prompt, _system: json.dumps({"items": items})


def test_it_is_off_by_default():
    assert os.environ.get("LEO_MEMORY_EXTRACT_ENABLED") is None
    assert config.MEMORY_EXTRACT_ENABLED is False


def test_extraction_files_notes_and_links(extracting):
    notes, tree = extracting
    count = extract_and_store(
        ["my sister Priya hates peanuts"],
        _answer([{"about": "Priya", "note": "hates peanuts", "link": "sister: me person"}]),
    )
    assert count == 1
    assert notes.notes[0].tags == ["entities/priya"]
    assert tree.related("entities/priya")


def test_extraction_is_only_shown_what_the_user_said(extracting):
    prompts: list[str] = []

    def ask(prompt, _system):
        prompts.append(prompt)
        return "{}"

    extract_and_store(["only my own words"], ask)
    assert "only my own words" in prompts[0]


def test_extraction_stops_for_lockdown(extracting):
    notes, _tree = extracting
    guard.engage_lockdown("test")
    assert extract_and_store(["x"], _answer([{"about": "P", "note": "n"}])) == 0
    assert notes.notes == []


def test_extraction_redacts(extracting):
    notes, _tree = extracting
    extract_and_store(
        ["x"], _answer([{"about": "Priya", "note": "key is gsk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}])
    )
    assert "gsk_aaaa" not in notes.notes[0].text


def test_extraction_is_capped(extracting, monkeypatch):
    notes, _tree = extracting
    monkeypatch.setattr(config, "MEMORY_EXTRACT_MAX_ITEMS", 2)
    items = [{"about": f"Person{i}", "note": f"distinct fact {i * 97}"} for i in range(9)]
    assert extract_and_store(["x"], _answer(items)) == 2


def test_a_planner_failure_costs_nothing(extracting):
    def boom(_prompt, _system):
        raise RuntimeError("no network")

    assert extract_and_store(["x"], boom) == 0


@pytest.mark.parametrize(
    "raw",
    ["not json", "[]", '{"items": "x"}', '{"items": [1, {"note": "no subject"}]}',
     '{"items": [{"about": "me", "note": "about the user"}]}'],
)
def test_a_bad_reply_stores_nothing(raw):
    assert parse_items(raw) == []
