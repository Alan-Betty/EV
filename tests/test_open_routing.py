"""Regression tests for "open X and go to Y" doing nothing.

Three separate faults produced one symptom. A request like "open File
Explorer and navigate to my GitHub folder" appeared to be ignored, because:

1. No tool could open a folder at all. `file_manager` listed what was *in*
   one; nothing put it on screen. So the model bodged it into `open_app`'s
   arguments.
2. `open_app` handed those arguments straight to the process unchecked. The
   model invented `C:\\Users\\Alan\\GitHub` for a machine with no Alan on it;
   Explorer opens its default location for a path that is not there and exits
   0, so E.V. reported "Explorer, up." A false success is worse than a
   failure - neither the user nor the model on the next turn can tell.
3. "open my email" had nowhere to go, so it came back as a question.

Nothing here launches a real process: `popen_detached` and `webbrowser.open`
are replaced by recorders.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Keep the tests deterministic regardless of the developer's own .env.
os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import config  # noqa: E402
from ev.tts import clean_for_speech  # noqa: E402
from tools import app_launcher, browser, dispatch  # noqa: E402

# `tools/__init__.py` exports a *function* named `file_manager`, which
# shadows the module on the package. The module itself is only reachable
# through sys.modules.
file_manager = sys.modules["tools.file_manager"]  # noqa: E402
from tools.schemas import TOOL_SPECS  # noqa: E402


def _tree(monkeypatch, tmp_path: Path) -> Path:
    """A fake user profile the suite is free to touch."""
    (tmp_path / "Documents" / "GitHub").mkdir(parents=True)
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Documents" / "notes.txt").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(config, "FILE_ROOTS", [tmp_path])
    monkeypatch.setattr(config, "FILE_DEFAULT_DIR", tmp_path / "Documents")
    monkeypatch.setattr(
        config,
        "USER_DIRS",
        {
            "documents": tmp_path / "Documents",
            "downloads": tmp_path / "Downloads",
            "home": tmp_path,
        },
    )
    return tmp_path


def _record_launches(monkeypatch) -> list:
    launched: list = []
    monkeypatch.setattr(
        file_manager, "popen_detached", lambda args, **kw: launched.append(args)
    )
    return launched


# ---------------------------------------------------------------------------
# 1. A folder can be opened at all
# ---------------------------------------------------------------------------
def test_open_is_a_declared_action():
    spec = next(s for s in TOOL_SPECS if s["name"] == "file_manager")
    assert "open" in spec["parameters"]["properties"]["action"]["enum"]


def test_opening_a_folder_puts_it_on_screen(monkeypatch, tmp_path):
    root = _tree(monkeypatch, tmp_path)
    launched = _record_launches(monkeypatch)

    result = dispatch("file_manager", {"action": "open", "path": "github"})
    assert result.ok
    assert launched, "nothing was launched"
    assert str(root / "Documents" / "GitHub") in " ".join(launched[0])
    assert "GitHub" in result.speech


def test_spoken_filler_resolves_the_same_as_the_bare_name(monkeypatch, tmp_path):
    """"my github folder" is the same request as "github"."""
    root = _tree(monkeypatch, tmp_path)
    launched = _record_launches(monkeypatch)

    for said in ("github", "my github folder", "the GitHub directory"):
        launched.clear()
        result = dispatch("file_manager", {"action": "open", "path": said})
        assert result.ok, said
        assert str(root / "Documents" / "GitHub") in " ".join(launched[0]), said


def test_a_folder_that_is_not_there_is_an_honest_failure(monkeypatch, tmp_path):
    """The fault that made this invisible: Explorer at a bad path exits 0."""
    _tree(monkeypatch, tmp_path)
    launched = _record_launches(monkeypatch)

    result = dispatch("file_manager", {"action": "open", "path": "nowhere-at-all"})
    assert not result.ok
    assert launched == []
    # And the model is told not to guess again.
    assert "invented" in result.detail or "does not exist" in result.detail


def test_opening_a_file_reads_it_rather_than_revealing_it(monkeypatch, tmp_path):
    """"Open notes.txt" and "open Downloads" are the same word, different jobs."""
    _tree(monkeypatch, tmp_path)
    launched = _record_launches(monkeypatch)

    result = dispatch(
        "file_manager", {"action": "open", "path": "Documents/notes.txt"}
    )
    assert result.ok
    assert launched == []  # nothing put on screen
    assert "hello" in result.detail


def test_open_stays_inside_the_roots(monkeypatch, tmp_path):
    _tree(monkeypatch, tmp_path)
    launched = _record_launches(monkeypatch)

    result = dispatch("file_manager", {"action": "open", "path": "C:/Windows/System32"})
    assert not result.ok
    assert launched == []


# ---------------------------------------------------------------------------
# 2. open_app stops launching an invented path
# ---------------------------------------------------------------------------
def test_an_invented_path_is_refused_not_launched(monkeypatch, tmp_path):
    """A path that exists nowhere must not be launched anyway."""
    _tree(monkeypatch, tmp_path)
    launched: list = []
    monkeypatch.setattr(
        app_launcher, "popen_detached", lambda args, **kw: launched.append(args)
    )

    result = dispatch(
        "open_app",
        {"app": "explorer", "arguments": r"C:\Users\Someone\NoSuchFolderAnywhere"},
    )
    assert not result.ok
    assert launched == []
    # The detail has to tell the model what to do instead, or it retries the
    # same guess with different spelling forever.
    assert "file_manager" in result.detail
    assert "{" not in result.speech
    assert result.speech == clean_for_speech(result.speech)


def test_a_wrong_absolute_path_is_repaired_from_its_last_part(monkeypatch, tmp_path):
    """The model is usually wrong about the prefix and right about the folder."""
    root = _tree(monkeypatch, tmp_path)
    launched: list = []
    monkeypatch.setattr(
        app_launcher, "popen_detached", lambda args, **kw: launched.append(args)
    )
    monkeypatch.setattr(app_launcher, "resolve_executable", lambda name: "explorer.exe")

    result = dispatch(
        "open_app", {"app": "explorer", "arguments": r"C:\Users\Someone\GitHub"}
    )
    assert result.ok
    assert str(root / "Documents" / "GitHub") in " ".join(launched[0])


def test_switches_are_not_mistaken_for_paths(monkeypatch, tmp_path):
    """`-f` and `/select,` look pathish and are not places on disk."""
    _tree(monkeypatch, tmp_path)
    launched: list = []
    monkeypatch.setattr(
        app_launcher, "popen_detached", lambda args, **kw: launched.append(args)
    )
    monkeypatch.setattr(app_launcher, "resolve_executable", lambda name: "thing.exe")

    result = dispatch("open_app", {"app": "thing", "arguments": "--profile=work -f"})
    assert result.ok
    assert launched[0] == ["thing.exe", "--profile=work", "-f"]


def test_an_app_with_no_arguments_is_untouched(monkeypatch):
    launched: list = []
    monkeypatch.setattr(
        app_launcher, "popen_detached", lambda args, **kw: launched.append(args)
    )
    monkeypatch.setattr(app_launcher, "resolve_executable", lambda name: "notepad.exe")

    result = dispatch("open_app", {"app": "notepad"})
    assert result.ok
    assert launched[0] == ["notepad.exe"]


# ---------------------------------------------------------------------------
# 3. "open my email" goes somewhere
# ---------------------------------------------------------------------------
def _record_browser(monkeypatch) -> list:
    opened: list = []
    monkeypatch.setattr(
        browser.webbrowser, "open", lambda url, **kw: (opened.append(url), True)[1]
    )
    return opened


def test_mail_and_calendar_are_destinations_not_searches():
    for engine in ("mail", "gmail", "outlook", "calendar", "drive"):
        assert engine in config.SEARCH_ENGINES
        assert "{q}" not in config.SEARCH_ENGINES[engine]
    # And the ordinary ones still are searches.
    for engine in ("google", "youtube", "amazon"):
        assert "{q}" in config.SEARCH_ENGINES[engine]


def test_opening_mail_with_no_query_opens_the_inbox(monkeypatch):
    """This used to answer "Search for what, exactly?"."""
    opened = _record_browser(monkeypatch)
    result = dispatch("web_search", {"engine": "mail"})
    assert result.ok
    assert opened == [config.SEARCH_ENGINES["mail"]]
    assert "mail" in result.speech.lower()


def test_a_query_against_a_destination_still_opens_the_destination(monkeypatch):
    """There is no search URL for an inbox; a built one would 404."""
    opened = _record_browser(monkeypatch)
    result = dispatch("web_search", {"engine": "gmail", "query": "the invoice"})
    assert result.ok
    assert opened == [config.SEARCH_ENGINES["gmail"]]


def test_an_ordinary_search_is_unchanged(monkeypatch):
    opened = _record_browser(monkeypatch)
    result = dispatch("web_search", {"engine": "google", "query": "wireless mouse"})
    assert result.ok
    assert "google.com/search?q=" in opened[0]


def test_a_search_with_nothing_to_search_for_still_asks(monkeypatch):
    _record_browser(monkeypatch)
    result = dispatch("web_search", {"engine": "google"})
    assert not result.ok


def test_destination_engines_are_offered_to_the_model():
    spec = next(s for s in TOOL_SPECS if s["name"] == "web_search")
    engines = spec["parameters"]["properties"]["engine"]["enum"]
    for engine in ("mail", "calendar", "drive"):
        assert engine in engines
