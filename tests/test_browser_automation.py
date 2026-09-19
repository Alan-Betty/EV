"""Offline tests for `browser_task`.

No browser is ever launched. Playwright's `sync_playwright` is replaced by a
fake that records what would have been done, so the parser, the safety gate,
the ceilings and the teardown can all be exercised on a machine that has
never run `playwright install`.
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
from tools import browser_automation  # noqa: E402
from tools.base import CancelToken  # noqa: E402
from tools.browser_automation import (  # noqa: E402
    _as_selector,
    browser_task,
    parse_steps,
)


# ---------------------------------------------------------------------------
# A fake browser
# ---------------------------------------------------------------------------
class FakeKeyboard:
    def __init__(self, log):
        self._log = log

    def press(self, key):
        self._log.append(("press", key))


class FakeMouse:
    def __init__(self, log):
        self._log = log

    def wheel(self, dx, dy):
        self._log.append(("wheel", dy))


class FakePage:
    def __init__(self, log, text="First result: a wireless mouse", fail_on=""):
        self._log = log
        self._text = text
        self._fail_on = fail_on
        self.keyboard = FakeKeyboard(log)
        self.mouse = FakeMouse(log)

    def _maybe_fail(self, selector):
        if self._fail_on and self._fail_on in selector:
            raise RuntimeError(f"Timeout waiting for {selector}")

    def goto(self, url, timeout=0, wait_until=""):
        self._log.append(("goto", url))

    def click(self, selector, timeout=0):
        self._maybe_fail(selector)
        self._log.append(("click", selector))

    def fill(self, selector, value, timeout=0):
        self._maybe_fail(selector)
        self._log.append(("fill", selector, value))

    def select_option(self, selector, value, timeout=0):
        self._log.append(("select", selector, value))

    def check(self, selector, timeout=0):
        self._log.append(("check", selector))

    def wait_for_selector(self, selector, timeout=0):
        self._log.append(("wait_for", selector))

    def wait_for_timeout(self, ms):
        self._log.append(("wait_ms", ms))

    def inner_text(self, selector, timeout=0):
        self._maybe_fail(selector)
        self._log.append(("read", selector))
        return self._text

    def locator(self, selector):
        return FakeLocator(self._log, selector, self._text, self._maybe_fail)


class FakeLocator:
    """Enough of Playwright's locator for the `read` step.

    `read` uses `all_inner_texts`, so a list of strings stands in for a page
    where the selector matched several elements - an inbox, a results list -
    which is the case the step exists for.
    """

    def __init__(self, log, selector, text, maybe_fail):
        self._log = log
        self._selector = selector
        self._text = text
        self._maybe_fail = maybe_fail
        self.first = self

    def wait_for(self, timeout=0, state=""):
        self._maybe_fail(self._selector)

    def all_inner_texts(self):
        self._maybe_fail(self._selector)
        self._log.append(("read", self._selector))
        return self._text if isinstance(self._text, list) else [self._text]


class FakeBrowser:
    def __init__(self, log, page):
        self._log = log
        self._page = page
        self.closed = False

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True
        self._log.append(("close",))


class FakeEngine:
    def __init__(self, log, page):
        self._log = log
        self._page = page
        self.browser: FakeBrowser | None = None

    def launch(self, headless=False):
        self._log.append(("launch", headless))
        self.browser = FakeBrowser(self._log, self._page)
        return self.browser


class FakePlaywright:
    def __init__(self, log, page):
        self.chromium = FakeEngine(log, page)
        self.firefox = self.chromium
        self.webkit = self.chromium

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _arm(monkeypatch, page: FakePage | None = None):
    """Replace Playwright with a recorder. Returns (log, engine)."""
    log: list[tuple] = []
    page = page or FakePage(log)
    playwright = FakePlaywright(log, page)

    module = type(sys)("playwright.sync_api")
    module.sync_playwright = lambda: playwright
    monkeypatch.setitem(sys.modules, "playwright", type(sys)("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return log, playwright.chromium


# ---------------------------------------------------------------------------
# The step DSL
# ---------------------------------------------------------------------------
def test_steps_parse_one_action_per_line():
    steps = parse_steps(
        "goto amazon.co.uk\n"
        "fill #search = wireless mouse\n"
        "press Enter\n"
        "click Add to cart\n"
        "read .result"
    )
    assert [s.verb for s in steps] == ["goto", "fill", "press", "click", "read"]
    assert steps[1].target == "#search"
    assert steps[1].value == "wireless mouse"


def test_steps_accept_semicolons_and_list_bullets():
    steps = parse_steps("- goto example.com; 2. click Sign in")
    assert [s.verb for s in steps] == ["goto", "click"]


def test_verb_synonyms_collapse():
    assert [s.verb for s in parse_steps("open a.com\ntype #q = hi\ntap Go")] == [
        "goto",
        "fill",
        "click",
    ]


def test_a_bare_url_is_a_navigation():
    steps = parse_steps("https://example.com/page")
    assert steps[0].verb == "goto"
    assert steps[0].target == "https://example.com/page"


def test_unparseable_lines_are_dropped_not_fatal():
    """One bad step out of three costs that step, not the errand."""
    steps = parse_steps("goto a.com\nsomething the model made up\nclick Go")
    assert [s.verb for s in steps] == ["goto", "click"]


def test_bare_text_becomes_a_text_selector_and_css_is_left_alone():
    assert browser_automation._as_selector("Add to cart") == "text=Add to cart"
    assert browser_automation._as_selector("#search") == "#search"
    assert browser_automation._as_selector(".result-item") == ".result-item"
    assert browser_automation._as_selector("css=div > a") == "css=div > a"
    assert browser_automation._as_selector("//div[@id='x']") == "//div[@id='x']"


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
def test_a_whole_workflow_runs_in_order(monkeypatch):
    log, engine = _arm(monkeypatch)
    result = browser_task(
        task="find a mouse",
        url="amazon.co.uk",
        steps="fill #search = wireless mouse\npress Enter\nread .result",
    )
    assert result.ok
    assert ("goto", "https://amazon.co.uk") in log
    assert ("fill", "#search", "wireless mouse") in log
    assert ("press", "Enter") in log
    assert ("read", ".result") in log
    # Torn down: Playwright's cost is only acceptable because it does not
    # outlive the task that needed it.
    assert engine.browser.closed


def test_what_was_read_goes_to_the_model_and_not_to_the_speaker(monkeypatch):
    """Page text is evidence for the next turn, not something to read aloud.

    The raw inner text of a page is navigation labels, timestamps and "1 of
    47". Speaking the first 180 characters of it was the worst available
    answer to "what's in my inbox"; the model gets the whole lot in `detail`
    and says something a person would say.
    """
    log, _ = _arm(monkeypatch, FakePage([], text="Logitech M720, thirty pounds"))
    result = browser_task(url="example.com", steps="read .price")
    assert result.ok
    assert "Logitech" in result.detail
    assert "Logitech" not in result.speech
    assert "browser_task" in result.detail
    # The machine-flavoured half stays out of the speaker's mouth.
    assert "browser_task" not in result.speech


def test_read_returns_every_matching_row_not_just_the_first(monkeypatch):
    """An inbox is thirty rows, and a summary of one of them is not a summary."""
    subjects = [f"Message {n}: something about invoices" for n in range(1, 13)]
    log, _ = _arm(monkeypatch, FakePage([], text=subjects))
    result = browser_task(url="mail.example.com", steps="read tr.row")
    assert result.ok
    for subject in subjects:
        assert subject in result.detail


def test_read_is_capped_so_a_long_page_cannot_flood_the_next_turn(monkeypatch):
    monkeypatch.setattr(config, "BROWSER_READ_ITEMS", 4)
    rows = [f"row {n} " + "x" * 50 for n in range(20)]
    log, _ = _arm(monkeypatch, FakePage([], text=rows))
    result = browser_task(url="example.com", steps="read li")
    assert result.ok
    assert "row 3" in result.detail
    assert "row 4" not in result.detail


def test_a_bare_destination_name_resolves_through_the_shared_table(monkeypatch):
    """"gmail" is somewhere to go, and `web_search` already knows where.

    Resolving it here through the same `SEARCH_ENGINES` table means there is
    one list of what "my mail" means rather than two that drift apart.
    """
    # No page of our own: `_arm`'s default one records into the log it hands
    # back, which is what this test needs to read.
    log, _ = _arm(monkeypatch)
    result = browser_task(url="gmail", steps="read body")
    assert result.ok
    visited = [entry[1] for entry in log if entry[0] == "goto"]
    assert visited and visited[0] == config.SEARCH_ENGINES["gmail"]
    assert "https://gmail" not in visited[0]


def test_a_missing_selector_fails_with_the_step_named(monkeypatch):
    log = []
    page = FakePage(log, fail_on="Add to cart")
    _arm(monkeypatch, page)
    result = browser_task(url="example.com", steps="click Add to cart")
    assert not result.ok
    assert "Add to cart" in result.detail
    # No stack trace reaches the speaker.
    assert result.speech == clean_for_speech(result.speech)
    assert "Traceback" not in result.speech


def test_no_steps_is_an_honest_failure(monkeypatch):
    _arm(monkeypatch)
    result = browser_task(task="do something web-ish")
    assert not result.ok
    assert "goto" in result.detail  # the error teaches the grammar


def test_too_many_steps_is_refused_before_launching(monkeypatch):
    log, _ = _arm(monkeypatch)
    steps = "\n".join(f"click Item {n}" for n in range(config.BROWSER_MAX_STEPS + 3))
    result = browser_task(url="example.com", steps=steps)
    assert not result.ok
    assert log == []  # the browser never started


def test_headless_is_honoured(monkeypatch):
    log, _ = _arm(monkeypatch)
    browser_task(url="example.com", steps="read body", headless=True)
    assert ("launch", True) in log


def test_disabled_automation_launches_nothing(monkeypatch):
    log, _ = _arm(monkeypatch)
    monkeypatch.setattr(config, "BROWSER_AUTOMATION_ENABLED", False)
    result = browser_task(url="example.com", steps="read body")
    assert not result.ok
    assert log == []


def test_a_missing_playwright_says_how_to_install_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    monkeypatch.delitem(sys.modules, "playwright", raising=False)

    import builtins

    real_import = builtins.__import__

    def no_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_playwright)
    result = browser_task(url="example.com", steps="read body")
    assert not result.ok
    assert "pip install playwright" in result.detail
    assert "pip install" not in result.speech  # not read aloud


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------
def test_a_purchase_step_is_held_before_the_browser_opens(monkeypatch):
    """A buy buried at step five is asked about at step zero."""
    log, _ = _arm(monkeypatch)
    result = browser_task(
        task="get me a mouse",
        url="amazon.co.uk",
        steps=(
            "fill #search = wireless mouse\n"
            "press Enter\n"
            "click Add to cart\n"
            "click Proceed to checkout\n"
            "click Place order"
        ),
    )
    assert result.needs_confirmation
    assert log == []  # nothing launched, nothing navigated
    # The held call is echoed back verbatim so the yes replays exactly it.
    assert result.data["url"] == "amazon.co.uk"
    assert "Place order" in result.data["steps"]


def test_confirming_lets_the_purchase_run(monkeypatch):
    log, _ = _arm(monkeypatch)
    result = browser_task(
        task="get me a mouse",
        url="amazon.co.uk",
        steps="click Place order",
        confirmed=True,
    )
    assert result.ok
    assert ("click", "text=Place order") in log


def test_an_ordinary_shopping_errand_is_not_gated(monkeypatch):
    """Adding to a cart is not buying, and gating it makes this unusable."""
    log, _ = _arm(monkeypatch)
    result = browser_task(
        task="find a mouse",
        url="amazon.co.uk",
        steps="fill #search = wireless mouse\npress Enter\nclick Add to cart",
    )
    assert result.ok
    assert ("click", "text=Add to cart") in log


def test_the_task_text_alone_can_trigger_the_gate(monkeypatch):
    log, _ = _arm(monkeypatch)
    result = browser_task(task="buy the cheapest one", url="shop.com", steps="click Go")
    assert result.needs_confirmation
    assert log == []


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------
def test_a_cancel_stops_between_steps(monkeypatch):
    log, engine = _arm(monkeypatch)
    token = CancelToken()
    token.cancel()
    result = browser_task(url="example.com", steps="read body", cancel=token)
    # Cancelled is a success carrying `cancelled`: what ran, ran.
    assert result.ok
    assert result.cancelled
    assert ("goto", "https://example.com") not in log
    # Still torn down. A cancelled task must not leak a browser process.
    assert engine.browser.closed


# ---------------------------------------------------------------------------
# Speech purity
# ---------------------------------------------------------------------------
def test_browser_speech_is_safe_to_read_aloud(monkeypatch):
    _arm(monkeypatch)
    results = [
        browser_task(url="example.com", steps="read body"),
        browser_task(task="do something web-ish"),
        browser_task(task="buy it all", url="shop.com", steps="click Go"),
    ]
    for result in results:
        assert result.speech
        assert result.speech == clean_for_speech(result.speech)
        for banned in ("Spoke:", "E.V.:", "**", "{", "}", "browser_task"):
            assert banned not in result.speech


def test_a_structural_tag_is_a_selector_not_a_phrase_to_look_for():
    """"read body" means the whole page. Turning it into a search for the
    visible word "body" is how "summarise my inbox" timed out on a perfectly
    good Gmail tab."""
    assert _as_selector("body") == "body"
    assert _as_selector("main") == "main"
    assert _as_selector("Body") == "body"


def test_a_phrase_is_still_matched_by_its_visible_text():
    """Which is how a person describes a button out loud."""
    assert _as_selector("Add to cart") == "text=Add to cart"
    assert _as_selector("#search") == "#search"
    assert _as_selector(".price") == ".price"
