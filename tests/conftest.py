"""Per-test isolation for the one piece of state that is process-global.

This file exists for a single reason and deliberately does nothing else.

`tools.guard` counts side-effecting tool calls across a rolling window and
locks E.V. down when too many arrive too fast. That counter is per process,
which is right in production and wrong in a test suite: hundreds of
dispatches land inside one second, the limiter correctly concludes something
is looping, and every test after that point fails against an assistant that
has locked itself down. The failures are real but they are about the suite,
not about the code under test.

Note what is *not* here. Every test module still bootstraps itself - `sys.path`,
`GROQ_API_KEY`, `EV_TTS_ENABLED=false` - before importing `config`, and that
is still load-bearing, because `config` reads the environment once at import.
Nothing in this file is imported at module scope for exactly that reason: an
import of `tools.guard` up here would pull `config` in before any test module
had set its environment, and quietly undo the preamble in all of them.
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_guard(monkeypatch):
    """Start every test with the runaway limiter empty and lockdown off.

    The audit log goes with it. It is the one guard that writes outside the
    process, and left on it appends a line per dispatch to the *real*
    `.cache/state/audit.jsonl` - so a test run quietly buries the record of
    what E.V. actually did under several hundred lines of what the suite
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
    guard.reset()
    yield
    guard.reset()
