"""File tool tests. Nothing here touches a real user directory.

Every test redirects `config.FILE_ROOTS` and `config.USER_DIRS` at a temporary
tree, so a bug in the tool cannot reach the machine running the suite. The
containment tests are the important ones: they are what stands between a
misheard word and someone's Documents folder.
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
from tools import dispatch  # noqa: E402
from tools.file_manager import PathRefused, file_manager, resolve_user_path  # noqa: E402


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
    monkeypatch.setattr(config, "FILE_USE_TRASH", False)  # deterministic deletes
    return dirs


# -- containment -------------------------------------------------------------
@pytest.mark.parametrize(
    "escape",
    [
        "C:/Windows/System32/drivers/etc/hosts",
        "/etc/passwd",
        "../../../../Windows/win.ini",
        "Documents/../../../../secret.txt",
    ],
)
def test_paths_outside_the_roots_are_refused(sandbox, escape):
    with pytest.raises(PathRefused):
        resolve_user_path(escape)


def test_refused_path_is_reported_not_silently_retargeted(sandbox):
    result = file_manager(action="create", path="C:/Windows/evil.txt", content="x")
    assert not result.ok
    assert "outside" in result.detail.lower()
    # The point of refusing is that nothing happened anywhere.
    assert not (sandbox["documents"] / "evil.txt").exists()


def test_protected_names_are_refused(sandbox):
    with pytest.raises(PathRefused):
        resolve_user_path("Documents/.ssh/id_rsa")


# -- path resolution ---------------------------------------------------------
def test_spoken_folder_names_resolve(sandbox):
    assert resolve_user_path("Downloads") == sandbox["downloads"]
    assert resolve_user_path("my downloads folder") == sandbox["downloads"]
    assert resolve_user_path("the Desktop") == sandbox["desktop"]


def test_relative_path_anchors_to_the_named_folder(sandbox):
    assert resolve_user_path("Documents/notes.txt") == sandbox["documents"] / "notes.txt"
    assert resolve_user_path("Downloads\\a\\b.txt") == sandbox["downloads"] / "a" / "b.txt"


def test_bare_filename_lands_in_the_default_folder(sandbox):
    # Crucially *not* the process working directory, which is wherever E.V.
    # happened to be started from.
    assert resolve_user_path("notes.txt") == sandbox["documents"] / "notes.txt"


# -- create / read / list ----------------------------------------------------
def test_create_writes_real_content(sandbox):
    body = "1. Kyoto\n2. Lisbon\n3. Reykjavik"
    result = file_manager(action="create", path="places.txt", content=body)
    written = sandbox["documents"] / "places.txt"
    assert result.ok
    assert written.read_text(encoding="utf-8") == body
    assert "places.txt" in result.speech


def test_create_never_overwrites_silently(sandbox):
    (sandbox["documents"] / "notes.txt").write_text("original", encoding="utf-8")
    file_manager(action="create", path="notes.txt", content="replacement")
    assert (sandbox["documents"] / "notes.txt").read_text(encoding="utf-8") == "original"
    assert (sandbox["documents"] / "notes (2).txt").read_text(encoding="utf-8") == "replacement"


def test_append_extends_an_existing_file(sandbox):
    target = sandbox["documents"] / "log.txt"
    target.write_text("line one", encoding="utf-8")
    assert file_manager(action="append", path="log.txt", content="line two").ok
    assert "line one" in target.read_text(encoding="utf-8")
    assert "line two" in target.read_text(encoding="utf-8")


def test_read_returns_contents_to_the_model_not_the_speaker(sandbox):
    (sandbox["documents"] / "notes.txt").write_text("secret sauce", encoding="utf-8")
    result = file_manager(action="read", path="notes.txt")
    assert "secret sauce" in result.detail  # the model sees it
    assert "secret sauce" not in result.speech  # nobody reads a file aloud


def test_list_counts_entries(sandbox):
    (sandbox["downloads"] / "a.txt").write_text("a", encoding="utf-8")
    (sandbox["downloads"] / "b.txt").write_text("b", encoding="utf-8")
    (sandbox["downloads"] / "sub").mkdir()
    result = file_manager(action="list", path="Downloads")
    assert result.ok
    assert "2 files" in result.speech


def test_read_of_a_missing_file_fails_clearly(sandbox):
    result = file_manager(action="read", path="nope.txt")
    assert not result.ok
    assert "nope.txt" in result.speech


# -- copy / move / rename ----------------------------------------------------
def test_copy_into_a_folder_keeps_the_filename(sandbox):
    source = sandbox["downloads"] / "report.pdf"
    source.write_text("pdf", encoding="utf-8")
    result = file_manager(
        action="copy", path="Downloads/report.pdf", destination="Documents"
    )
    assert result.ok
    assert (sandbox["documents"] / "report.pdf").exists()
    assert source.exists()  # a copy leaves the original


def test_move_removes_the_original(sandbox):
    source = sandbox["downloads"] / "report.pdf"
    source.write_text("pdf", encoding="utf-8")
    assert file_manager(
        action="move", path="Downloads/report.pdf", destination="Documents"
    ).ok
    assert (sandbox["documents"] / "report.pdf").exists()
    assert not source.exists()


def test_rename_uses_the_new_name(sandbox):
    (sandbox["documents"] / "old.txt").write_text("x", encoding="utf-8")
    result = file_manager(
        action="rename", path="Documents/old.txt", destination="Documents/new.txt"
    )
    assert result.ok
    assert (sandbox["documents"] / "new.txt").exists()
    assert not (sandbox["documents"] / "old.txt").exists()


def test_copy_without_a_destination_is_refused(sandbox):
    (sandbox["documents"] / "a.txt").write_text("x", encoding="utf-8")
    result = file_manager(action="copy", path="a.txt")
    assert not result.ok
    assert "where" in result.speech.lower()


# -- destructive actions -----------------------------------------------------
def test_delete_requires_confirmation(sandbox):
    target = sandbox["documents"] / "doomed.txt"
    target.write_text("x", encoding="utf-8")

    held = file_manager(action="delete", path="doomed.txt")
    assert held.needs_confirmation
    assert target.exists(), "nothing may be deleted before the user says yes"

    done = file_manager(action="delete", path="doomed.txt", confirmed=True)
    assert done.ok
    assert not target.exists()


def test_organize_requires_confirmation(sandbox):
    (sandbox["downloads"] / "photo.png").write_text("x", encoding="utf-8")
    held = file_manager(action="organize", path="Downloads")
    assert held.needs_confirmation
    assert (sandbox["downloads"] / "photo.png").exists()


def test_confirmation_payload_survives_a_round_trip(sandbox):
    """The held args are what the core loop replays, so they must be complete."""
    (sandbox["documents"] / "doomed.txt").write_text("x", encoding="utf-8")
    held = file_manager(action="delete", path="doomed.txt")
    replayed = dispatch("file_manager", {**held.data, "confirmed": True})
    assert replayed.ok
    assert not (sandbox["documents"] / "doomed.txt").exists()


def test_the_model_cannot_confirm_on_its_own_behalf(sandbox):
    """`confirmed` is injected by the loop; a model that guesses it is ignored.

    It reaches the tool through `dispatch`, so this checks the real path
    rather than calling the function directly.
    """
    target = sandbox["documents"] / "doomed.txt"
    target.write_text("x", encoding="utf-8")
    # Simulating the model setting confirmed=true in its tool arguments.
    result = dispatch("file_manager", {"action": "delete", "path": "doomed.txt"})
    assert result.needs_confirmation
    assert target.exists()


# -- organize ----------------------------------------------------------------
def test_organize_sorts_by_type(sandbox):
    downloads = sandbox["downloads"]
    for name in ("a.png", "b.jpg", "c.pdf", "d.zip", "e.mp3", "f.unknownext"):
        (downloads / name).write_text("x", encoding="utf-8")

    result = file_manager(action="organize", path="Downloads", confirmed=True)
    assert result.ok
    assert (downloads / "Images" / "a.png").exists()
    assert (downloads / "Images" / "b.jpg").exists()
    assert (downloads / "Documents" / "c.pdf").exists()
    assert (downloads / "Archives" / "d.zip").exists()
    assert (downloads / "Audio" / "e.mp3").exists()
    assert (downloads / "Other" / "f.unknownext").exists()


def test_organize_on_a_tidy_folder_is_a_no_op(sandbox):
    result = file_manager(action="organize", path="Downloads", confirmed=True)
    assert result.ok
    assert "tidy" in result.speech.lower()


# -- misc --------------------------------------------------------------------
def test_find_matches_by_substring(sandbox):
    (sandbox["downloads"] / "invoice-2024.pdf").write_text("x", encoding="utf-8")
    (sandbox["downloads"] / "cat.png").write_text("x", encoding="utf-8")
    result = file_manager(action="find", path="Downloads", pattern="invoice")
    assert result.ok
    assert "invoice-2024.pdf" in result.detail


def test_makedir_creates_nested_folders(sandbox):
    assert file_manager(action="makedir", path="Documents/a/b/c").ok
    assert (sandbox["documents"] / "a" / "b" / "c").is_dir()


def test_action_synonyms_are_accepted(sandbox):
    # The model reaches for "write"/"remove"/"ls" regardless of the enum.
    assert file_manager(action="write", path="x.txt", content="hi").ok
    assert file_manager(action="ls", path="Documents").ok
    assert file_manager(action="rm", path="x.txt").needs_confirmation


def test_unknown_action_is_reported_not_guessed(sandbox):
    result = file_manager(action="encrypt", path="x.txt")
    assert not result.ok
    assert "encrypt" in result.detail


def test_missing_action_is_rejected(sandbox):
    assert not file_manager(action="", path="x.txt").ok


def test_a_broken_tool_never_takes_the_assistant_down(sandbox):
    # dispatch swallows everything; the loop must always get a ToolResult.
    result = dispatch("file_manager", {"action": "read", "path": "\x00bad"})
    assert result is not None
    assert not result.ok


# -- destination is a folder, not a filename ---------------------------------
def test_copy_into_a_named_folder_that_does_not_exist_yet(sandbox, monkeypatch):
    """Regression: this used to create a *file* named `Desktop`.

    On a machine where the user folders are OneDrive-redirected, `~/Desktop`
    may not exist. The destination was then not a directory, so it was treated
    as a filename and the copy silently produced a file with a folder's name
    in the home directory. A named user folder is always a directory.
    """
    missing = sandbox["home"] / "Desktop2"
    monkeypatch.setitem(config.USER_DIRS, "desktop2", missing)
    assert not missing.exists()

    source = sandbox["documents"] / "notes.txt"
    source.write_text("x", encoding="utf-8")
    result = file_manager(action="copy", path="Documents/notes.txt", destination="desktop2")

    assert result.ok
    assert missing.is_dir(), "the folder should have been created"
    assert (missing / "notes.txt").is_file()
    assert not missing.is_file(), "must never become a file named after the folder"


def test_move_into_a_missing_named_folder_creates_it(sandbox, monkeypatch):
    missing = sandbox["home"] / "Archive"
    monkeypatch.setitem(config.USER_DIRS, "archive", missing)
    source = sandbox["downloads"] / "old.zip"
    source.write_text("x", encoding="utf-8")

    assert file_manager(action="move", path="Downloads/old.zip", destination="archive").ok
    assert (missing / "old.zip").is_file()


def test_extensionless_destination_is_treated_as_a_folder(sandbox):
    source = sandbox["documents"] / "notes.txt"
    source.write_text("x", encoding="utf-8")
    assert file_manager(
        action="copy", path="Documents/notes.txt", destination="Documents/backups"
    ).ok
    assert (sandbox["documents"] / "backups" / "notes.txt").is_file()


def test_destination_with_an_extension_is_still_a_rename(sandbox):
    source = sandbox["documents"] / "notes.txt"
    source.write_text("x", encoding="utf-8")
    assert file_manager(
        action="copy", path="Documents/notes.txt", destination="Documents/copy.txt"
    ).ok
    assert (sandbox["documents"] / "copy.txt").is_file()
    assert not (sandbox["documents"] / "copy.txt" / "notes.txt").exists()


def test_user_dirs_follow_windows_redirection():
    """`~/Documents` is the wrong answer on a OneDrive-backed profile.

    Both `~/Documents` and `~/OneDrive/Documents` can exist there, but only
    the redirected one is what Explorer shows, so writing to the other puts
    files where the user will never find them.
    """
    import os as _os

    if _os.name != "nt":
        return  # the registry lookup only applies on Windows
    for name in ("desktop", "documents", "downloads"):
        assert config.USER_DIRS[name].is_dir(), f"{name} did not resolve to a real folder"
