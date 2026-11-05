"""Per-test isolation for the one piece of state that is process-global.

This file exists for a single reason and deliberately does nothing else.

`tools.guard` counts side-effecting tool calls across a rolling window and
locks Leo down when too many arrive too fast. That counter is per process,
which is right in production and wrong in a test suite: hundreds of
dispatches land inside one second, the limiter correctly concludes something
is looping, and every test after that point fails against an assistant that
has locked itself down. The failures are real but they are about the suite,
not about the code under test.

Note what is *not* here. Every test module still bootstraps itself - `sys.path`,
`GROQ_API_KEY`, `LEO_TTS_ENABLED=false` - before importing `config`, and that
is still load-bearing, because `config` reads the environment once at import.
Nothing in this file is imported at module scope for exactly that reason: an
import of `tools.guard` up here would pull `config` in before any test module
had set its environment, and quietly undo the preamble in all of them.
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_guard(monkeypatch, tmp_path, tmp_path_factory):
    """Start every test with the runaway limiter empty and lockdown off.

    The audit log goes with it. It is the one guard that writes outside the
    process, and left on it appends a line per dispatch to the *real*
    `.cache/state/audit.jsonl` - so a test run quietly buries the record of
    what Leo actually did under several hundred lines of what the suite
    did. A test that wants to check the log on turns it back on against its
    own `tmp_path`, which is what `tests/test_guard.py` does.
    """
    import config
    from tools import guard

    monkeypatch.setattr(config, "AUDIT_ENABLED", False, raising=False)
    # The kept browser lives on a thread of its own and outlives the call
    # that opened it - so left on, a fake page from one test would be handed
    # to the next. Tests of the keeper turn it back on and release it.
    monkeypatch.setattr(config, "BROWSER_KEEP_OPEN", False, raising=False)
    # Every screen and browser tool now announces itself with the overlay,
    # so left on, any test that reaches one would start Tk on a real display
    # and register a real system-wide hotkey.
    monkeypatch.setattr(config, "AGENT_OVERLAY_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "AGENT_HOTKEY_ENABLED", False, raising=False)
    # The face is a real Qt process on a real display. A test that reaches
    # `Leo.start` must not leave one floating on the desktop of whoever ran it.
    monkeypatch.setattr(config, "FACE_ENABLED", False, raising=False)
    # Learned hearing lives in STATE_DIR like the audit log, and the suite
    # must neither read the user's real one nor teach it test sentences.
    monkeypatch.setattr(config, "STT_LEARN", False, raising=False)
    # Everything else under STATE_DIR. `get_memory()` and `get_backlog()`
    # rebuild when the path moves, so this is all it takes - and without it a
    # run writes its test facts, fake token included, into the real
    # memory.json. A separate folder rather than `tmp_path`, which many tests
    # use as a fake home and list the contents of.
    state = tmp_path_factory.mktemp("state")
    monkeypatch.setattr(config, "STATE_DIR", state, raising=False)
    monkeypatch.setattr(config, "MEMORY_FILE", state / "memory.json", raising=False)
    monkeypatch.setattr(config, "BACKLOG_FILE", state / "backlog.json", raising=False)
    monkeypatch.setattr(config, "HEARING_FILE", state / "hearing.json", raising=False)
    monkeypatch.setattr(config, "BROWSER_PROFILE_DIR", state / "browser-profile", raising=False)
    # Long-term notes: never the user's real file, never a real embedding
    # call. Tests of the store build their own against tmp_path.
    monkeypatch.setattr(config, "SEMANTIC_EMBEDDINGS", "off", raising=False)
    monkeypatch.setattr(
        config, "SEMANTIC_FILE", tmp_path / "semantic_memory.json", raising=False
    )
    monkeypatch.setattr(config, "MEMORY_TREE_FILE", state / "memory_tree.json", raising=False)
    # On a GNOME Wayland machine "auto" opens a real Mutter remote-desktop
    # session - GNOME's screen-sharing indicator, real injected input - and
    # on others a real uinput device. Tests that want a backend ask for one.
    monkeypatch.setattr(config, "INPUT_BACKEND", "pyautogui", raising=False)
    monkeypatch.setattr(config, "CAPTURE_BACKEND", "mss", raising=False)
    # The wake phrases are the user's to set in `.env`, and wake tests pin how
    # the defaults behave. An old `ev` left in there makes "even so" a summons.
    monkeypatch.setattr(config, "WAKE_PHRASES", ["leo", "l.e.o.", "l e o", "hey leo"], raising=False)
    # A real `code` would open a real window on whoever is running the tests.
    monkeypatch.setattr(config, "DEV_OPEN_EDITOR", False, raising=False)
    # A real wacli would mean a real WhatsApp account. Tests bring a fake.
    monkeypatch.setattr(config, "WACLI_BIN", "wacli-not-on-this-machine", raising=False)
    monkeypatch.setattr(config, "WHATSAPP_SYNC_ENABLED", False, raising=False)
    # The accessibility tier would read the real AT-SPI bus - and on a live
    # desktop, plan over the user's real windows. Tests of it turn it on
    # against fakes.
    monkeypatch.setattr(config, "A11Y_ENABLED", False, raising=False)
    from tools import window
    from tools.desktop import hands

    hands.reset()
    # The window inventory reads the real X server and accessibility bus.
    # A test that wants windows supplies them.
    monkeypatch.setattr(window, "_listing_backends", lambda: [])
    guard.reset()
    yield
    guard.reset()
    hands.reset()
