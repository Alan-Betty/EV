"""Batch file tests. Nothing here touches a real user directory.

Same containment rule as `test_file_manager.py`, and it matters more here:
these actions act on a whole folder at once, so a root that leaked would leak
by the hundred. Every source and every destination still resolves through
`_check`, and the tests below prove it by planting a symlink out of the
sandbox and watching it get refused.
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
from ev.tts import clean_for_speech  # noqa: E402
from tools import dispatch  # noqa: E402
from tools.file_manager import file_manager  # noqa: E402


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A fake home with the usual user folders, isolated from the real one."""
    home = tmp_path / "home"
    dirs = {
        name: home / name.title()
        for name in ("desktop", "downloads", "documents", "pictures")
    }
    dirs["home"] = home
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "FILE_ROOTS", [home])
    monkeypatch.setattr(config, "USER_DIRS", dirs)
    monkeypatch.setattr(config, "FILE_DEFAULT_DIR", dirs["documents"])
    monkeypatch.setattr(config, "FILE_CONFIRM_DELETE", True)
    return dirs


def _seed(folder: Path, *names: str) -> None:
    for name in names:
        (folder / name).write_text(f"contents of {name}", encoding="utf-8")


# -- batch copy --------------------------------------------------------------
def test_copying_a_whole_pattern_between_user_folders(sandbox):
    _seed(sandbox["downloads"], "march invoice.pdf", "april invoice.pdf", "cat.jpg")

    result = file_manager(
        action="batch_copy",
        path="Downloads",
        destination="Documents",
        pattern="invoice",
    )

    assert result.ok is True
    assert sorted(p.name for p in sandbox["documents"].iterdir()) == [
        "april invoice.pdf",
        "march invoice.pdf",
    ]
    # A copy leaves the originals where they were.
    assert (sandbox["downloads"] / "march invoice.pdf").exists()
    # The unmatched file is untouched.
    assert (sandbox["downloads"] / "cat.jpg").exists()


def test_a_glob_pattern_works_as_well_as_a_name_fragment(sandbox):
    _seed(sandbox["downloads"], "a.pdf", "b.pdf", "c.txt")

    result = file_manager(
        action="batch_copy", path="Downloads", destination="Desktop", pattern="*.pdf"
    )

    assert result.ok is True
    assert sorted(p.name for p in sandbox["desktop"].iterdir()) == ["a.pdf", "b.pdf"]


def test_no_pattern_means_everything_in_the_folder(sandbox):
    _seed(sandbox["downloads"], "a.pdf", "b.txt")

    result = file_manager(action="batch_copy", path="Downloads", destination="Desktop")

    assert result.ok is True
    assert len(list(sandbox["desktop"].iterdir())) == 2


def test_a_missing_destination_folder_is_created(sandbox):
    _seed(sandbox["downloads"], "a.pdf")

    result = file_manager(
        action="batch_copy", path="Downloads", destination="Documents/Invoices"
    )

    assert result.ok is True
    assert (sandbox["documents"] / "Invoices" / "a.pdf").exists()


def test_batch_copy_never_overwrites(sandbox):
    _seed(sandbox["downloads"], "notes.txt")
    (sandbox["documents"] / "notes.txt").write_text("the original", encoding="utf-8")

    file_manager(action="batch_copy", path="Downloads", destination="Documents")

    assert (sandbox["documents"] / "notes.txt").read_text(encoding="utf-8") == "the original"
    assert (sandbox["documents"] / "notes (2).txt").exists()


def test_batch_copy_does_not_recurse(sandbox):
    """"The PDFs in Downloads" is not "every PDF under my home folder"."""
    _seed(sandbox["downloads"], "top.pdf")
    nested = sandbox["downloads"] / "old"
    nested.mkdir()
    _seed(nested, "buried.pdf")

    file_manager(
        action="batch_copy", path="Downloads", destination="Desktop", pattern="*.pdf"
    )

    assert [p.name for p in sandbox["desktop"].iterdir()] == ["top.pdf"]


def test_nothing_matching_is_reported_rather_than_faked(sandbox):
    _seed(sandbox["downloads"], "cat.jpg")

    result = file_manager(
        action="batch_copy", path="Downloads", destination="Documents", pattern="invoice"
    )

    assert result.ok is False
    assert "invoice" in result.speech
    assert list(sandbox["documents"].iterdir()) == []


def test_a_batch_copy_with_no_destination_is_refused(sandbox):
    _seed(sandbox["downloads"], "a.pdf")
    result = file_manager(action="batch_copy", path="Downloads")
    assert result.ok is False
    assert "destination" in result.detail


def test_copying_a_folder_onto_itself_is_refused(sandbox):
    _seed(sandbox["downloads"], "a.pdf")
    result = file_manager(
        action="batch_copy", path="Downloads", destination="Downloads"
    )
    assert result.ok is False
    assert "same folder" in result.speech


def test_a_file_as_the_destination_is_refused(sandbox):
    _seed(sandbox["downloads"], "a.pdf")
    _seed(sandbox["documents"], "target.txt")

    result = file_manager(
        action="batch_copy", path="Downloads", destination="Documents/target.txt"
    )

    assert result.ok is False
    assert "isn't a folder" in result.speech


def test_a_source_that_is_not_a_folder_is_refused(sandbox):
    _seed(sandbox["documents"], "single.txt")
    result = file_manager(
        action="batch_copy", path="Documents/single.txt", destination="Desktop"
    )
    assert result.ok is False
    assert "isn't a folder" in result.speech


# -- batch move --------------------------------------------------------------
def test_batch_move_asks_before_it_moves_anything(sandbox):
    _seed(sandbox["desktop"], "shot1.png", "shot2.png")

    held = file_manager(
        action="batch_move", path="Desktop", destination="Pictures", pattern="*.png"
    )

    assert held.needs_confirmation is True
    assert "Desktop" in held.speech
    # Nothing has moved yet.
    assert len(list(sandbox["desktop"].iterdir())) == 2
    assert list(sandbox["pictures"].iterdir()) == []


def test_a_confirmed_batch_move_carries_every_argument_through(sandbox):
    _seed(sandbox["desktop"], "shot1.png", "shot2.png", "keep.txt")

    held = file_manager(
        action="batch_move", path="Desktop", destination="Pictures", pattern="*.png"
    )
    # This is what the core loop replays: the held data plus the spoken yes.
    result = dispatch("file_manager", {**held.data, "confirmed": True})

    assert result.ok is True
    assert sorted(p.name for p in sandbox["pictures"].iterdir()) == [
        "shot1.png",
        "shot2.png",
    ]
    assert [p.name for p in sandbox["desktop"].iterdir()] == ["keep.txt"]


# -- batch rename ------------------------------------------------------------
def test_batch_rename_numbers_the_matches_and_keeps_extensions(sandbox):
    _seed(sandbox["pictures"], "IMG_001.jpg", "IMG_002.jpg", "IMG_003.png")

    held = file_manager(
        action="batch_rename", path="Pictures", pattern="IMG", new_name="holiday"
    )
    assert held.needs_confirmation is True

    result = dispatch("file_manager", {**held.data, "confirmed": True})

    assert result.ok is True
    assert sorted(p.name for p in sandbox["pictures"].iterdir()) == [
        "holiday 1.jpg",
        "holiday 2.jpg",
        "holiday 3.png",
    ]


def test_a_single_match_is_not_numbered(sandbox):
    _seed(sandbox["pictures"], "IMG_001.jpg")

    result = file_manager(
        action="batch_rename",
        path="Pictures",
        pattern="IMG",
        new_name="holiday",
        confirmed=True,
    )

    assert result.ok is True
    assert [p.name for p in sandbox["pictures"].iterdir()] == ["holiday.jpg"]


def test_an_extension_in_the_new_name_is_dropped(sandbox):
    """Each file keeps its own extension; "holiday.jpg" means "holiday"."""
    _seed(sandbox["pictures"], "a.png", "b.png")

    file_manager(
        action="batch_rename",
        path="Pictures",
        pattern="*.png",
        new_name="holiday.jpg",
        confirmed=True,
    )

    assert sorted(p.name for p in sandbox["pictures"].iterdir()) == [
        "holiday 1.png",
        "holiday 2.png",
    ]


def test_renaming_pads_the_numbers_so_they_sort(sandbox):
    _seed(sandbox["pictures"], *[f"raw{index}.jpg" for index in range(12)])

    file_manager(
        action="batch_rename",
        path="Pictures",
        pattern="raw",
        new_name="shot",
        confirmed=True,
    )

    names = sorted(p.name for p in sandbox["pictures"].iterdir())
    assert names[0] == "shot 01.jpg"
    assert names[-1] == "shot 12.jpg"


def test_a_rename_with_no_new_name_is_refused(sandbox):
    _seed(sandbox["pictures"], "a.png")
    result = file_manager(
        action="batch_rename", path="Pictures", pattern="*.png", confirmed=True
    )
    assert result.ok is False
    assert "new_name" in result.detail


def test_renaming_onto_an_existing_name_does_not_overwrite(sandbox):
    _seed(sandbox["pictures"], "a.png", "holiday.png")
    (sandbox["pictures"] / "holiday.png").write_text("do not lose me", encoding="utf-8")

    file_manager(
        action="batch_rename",
        path="Pictures",
        pattern="a.png",
        new_name="holiday",
        confirmed=True,
    )

    assert (sandbox["pictures"] / "holiday.png").read_text(encoding="utf-8") == (
        "do not lose me"
    )
    assert (sandbox["pictures"] / "holiday (2).png").exists()


# -- containment -------------------------------------------------------------
def test_a_batch_destination_outside_the_roots_is_refused(sandbox, tmp_path):
    _seed(sandbox["downloads"], "a.pdf")
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    result = file_manager(
        action="batch_copy", path="Downloads", destination=str(outside)
    )

    assert result.ok is False
    assert "outside the folders" in result.speech
    assert list(outside.iterdir()) == []


def test_a_batch_source_outside_the_roots_is_refused(sandbox, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    _seed(outside, "secret.txt")

    result = file_manager(
        action="batch_copy", path=str(outside), destination="Documents"
    )

    assert result.ok is False
    assert list(sandbox["documents"].iterdir()) == []


def test_a_symlinked_destination_cannot_smuggle_files_out(sandbox, tmp_path):
    """`_check` resolves through realpath, so a planted link resolves out."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link = sandbox["documents"] / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this machine")

    _seed(sandbox["downloads"], "a.pdf")
    result = file_manager(
        action="batch_copy", path="Downloads", destination="Documents/escape"
    )

    assert result.ok is False
    assert list(outside.iterdir()) == []


def test_protected_names_are_still_protected_in_a_batch(sandbox):
    result = file_manager(
        action="batch_copy", path="Downloads", destination="Documents/.ssh"
    )
    assert result.ok is False
    assert "outside the folders" in result.speech or "protected" in result.detail


# -- contract ----------------------------------------------------------------
def test_the_batch_actions_are_declared_in_the_schema():
    from tools.schemas import TOOL_SPECS

    spec = next(s for s in TOOL_SPECS if s["name"] == "file_manager")
    actions = spec["parameters"]["properties"]["action"]["enum"]
    assert {"batch_copy", "batch_move", "batch_rename"} <= set(actions)
    assert "new_name" in spec["parameters"]["properties"]


def test_new_name_survives_the_argument_filter(sandbox):
    """`_ALLOWED_ARGS` is derived from the schema, so this cannot drift."""
    _seed(sandbox["pictures"], "a.png")
    result = dispatch(
        "file_manager",
        {
            "action": "batch_rename",
            "path": "Pictures",
            "pattern": "*.png",
            "new_name": "holiday",
            "confirmed": True,
        },
    )
    assert result.ok is True
    assert (sandbox["pictures"] / "holiday.png").exists()


def test_batch_speech_carries_no_labels_and_no_paths(sandbox):
    _seed(sandbox["downloads"], "a.pdf", "b.pdf")
    spoken = file_manager(
        action="batch_copy", path="Downloads", destination="Documents"
    ).speech
    assert clean_for_speech(spoken) == spoken
    assert str(sandbox["documents"]) not in spoken
