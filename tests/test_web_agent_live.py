"""Live browser tests: a real Chromium against real pages.

Every other test file in this suite is offline and must stay that way, so
these are skipped unless `EV_LIVE_BROWSER=1` is set. They exist because the
offline tests prove the *logic* and prove nothing at all about the thing the
logic depends on: that a page scan finds the search box on a page nobody
wrote for us, that an element number still points at the same element two
hundred milliseconds later, and that a real site's real markup survives the
round trip.

    EV_LIVE_BROWSER=1 python -m pytest tests/test_web_agent_live.py -q

No planner is called here and no money can be spent. The shopping flow runs
against `books.toscrape.com`, a site published for exactly this purpose: it
has a catalogue, a basket and a checkout, and none of them are real.
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
from tools.web_agent import (  # noqa: E402
    Action,
    BrowserSession,
    observe,
    run_action,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("EV_LIVE_BROWSER", "") not in {"1", "true", "yes"},
    reason="live browser tests are opt-in: set EV_LIVE_BROWSER=1",
)


@pytest.fixture(scope="module")
def browser():
    """One headless Chromium for the whole file.

    Headless and profile-less on purpose: these tests must not touch the
    profile E.V. keeps the user's real logins in, and they must run on a
    machine with nobody watching.
    """
    config.BROWSER_PERSIST_PROFILE = False
    with BrowserSession(headless=True) as session:
        yield session


def _goto(browser, url):
    run_action(browser.page, Action("goto", url), [])
    return observe(browser.page)


# ---------------------------------------------------------------------------
# The scan, against markup nobody wrote for us
# ---------------------------------------------------------------------------
def test_a_plain_page_reads_back_as_text_and_elements(browser):
    sight = _goto(browser, "https://example.com")
    assert "example" in sight.url.lower()
    assert "Example Domain" in sight.title
    assert "documentation examples" in sight.text
    # The page has exactly one link, and the scan found it by its own text.
    # Asserted loosely on purpose: this page's wording has already changed
    # once under these tests, and what is being checked is that a link
    # arrives with a usable label, not what the label happens to say.
    links = [e for e in sight.elements if e.get("tag") == "a"]
    assert links, "the scan found no link on a page with one"
    assert str(links[0].get("label", "")).strip()


def test_the_scan_finds_a_real_search_box(browser):
    """A search box has no text of its own, so the label comes from its
    placeholder or its ARIA label - which is most of why the scan assembles
    a label out of five different attributes rather than reading innerText."""
    sight = _goto(browser, "https://duckduckgo.com")
    boxes = [
        e for e in sight.elements
        if e.get("tag") in {"input", "textarea"}
        and "search" in str(e.get("label", "")).lower()
    ]
    assert boxes, f"no labelled search box among {len(sight.elements)} elements"


def test_a_catalogue_page_reads_as_mostly_links(browser):
    sight = _goto(browser, "https://books.toscrape.com")
    assert len(sight.elements) > 20
    assert "Books to Scrape" in sight.title


def test_an_element_number_still_points_at_that_element(browser):
    """The claim the whole DOM route rests on."""
    sight = _goto(browser, "https://books.toscrape.com")
    links = [
        e for e in sight.elements
        if e.get("tag") == "a" and "travel" in str(e.get("label", "")).lower()
    ]
    assert links, "expected a Travel category link"
    ref = str(links[0]["i"])

    run_action(browser.page, Action("click", ref), [])
    after = observe(browser.page)
    assert "travel" in after.url.lower()
    assert "Travel" in after.title


def test_reading_a_list_returns_every_match(browser):
    """An inbox is thirty rows; so is a catalogue page."""
    _goto(browser, "https://books.toscrape.com")
    gathered: list[str] = []
    note = run_action(browser.page, Action("read", ".product_pod h3"), gathered)
    assert "read 20 item(s)" in note
    assert gathered[0].count("|") >= 10  # many titles, not one


# ---------------------------------------------------------------------------
# A real shopping flow, on a shop that sells nothing
# ---------------------------------------------------------------------------
def test_a_search_can_be_filled_and_submitted(browser):
    """Wikipedia: fill a box, press Enter, land somewhere else."""
    sight = _goto(browser, "https://www.wikipedia.org")
    boxes = [
        e for e in sight.elements
        if e.get("tag") == "input" and "search" in str(e.get("label", "")).lower()
    ]
    assert boxes, "no search box found on wikipedia.org"

    run_action(browser.page, Action("fill", str(boxes[0]["i"]), "computer mouse"), [])
    run_action(browser.page, Action("press", "Enter"), [])
    browser.page.wait_for_load_state("domcontentloaded")
    after = observe(browser.page)
    assert "mouse" in after.url.lower() or "mouse" in after.title.lower()


def test_a_price_is_readable_from_the_page_text(browser):
    """How "under 5000" is checked with no pixels involved."""
    sight = _goto(browser, "https://books.toscrape.com")
    products = [
        e for e in sight.elements
        if e.get("tag") == "a" and "catalogue/" in str(e.get("href", ""))
        and str(e.get("label", "")).strip()
    ]
    assert products, "no product links found"
    run_action(browser.page, Action("click", str(products[0]["i"])), [])
    product = observe(browser.page)
    assert "catalogue" in product.url
    assert "£" in product.text
    assert "In stock" in product.text or "in stock" in product.text.lower()


def test_a_whole_cart_flow_works_end_to_end(browser):
    """Log in, add to cart, see the cart count change.

    `saucedemo.com` is a demo shop published for automation practice: the
    sign-in is a published username and password, the catalogue is fake and
    the cart is real. It is the closest thing to the user's actual errand
    that can be run a hundred times without spending anything.

    `books.toscrape.com` was the first choice and was wrong: its "Add to
    basket" buttons are static decoration on a mirrored site, so the flow
    passed through them and changed nothing. A test that cannot fail when
    the cart is broken is not a test of the cart.
    """
    sight = _goto(browser, "https://www.saucedemo.com")

    def ref(pred):
        found = [e for e in sight.elements if pred(e)]
        assert found, f"nothing matched on {sight.url}"
        return str(found[0]["i"])

    user = ref(lambda e: "user" in str(e.get("label", "")).lower())
    run_action(browser.page, Action("fill", user, "standard_user"), [])
    sight = observe(browser.page)
    password = ref(lambda e: str(e.get("type", "")) == "password")
    run_action(browser.page, Action("fill", password, "secret_sauce"), [])
    sight = observe(browser.page)
    run_action(
        browser.page,
        Action("click", ref(lambda e: "login" in str(e.get("label", "")).lower())),
        [],
    )

    catalogue = observe(browser.page)
    assert "inventory" in catalogue.url, catalogue.url
    assert "Backpack" in catalogue.text

    add = [
        e for e in catalogue.elements
        if "add to cart" in str(e.get("label", "")).lower()
    ]
    assert add, "no 'Add to cart' button on the catalogue"
    run_action(browser.page, Action("click", str(add[0]["i"])), [])

    after = observe(browser.page)
    # The badge is the shop's own evidence that the cart really changed -
    # exactly the kind of thing the planner is told to name under "evidence".
    badge = [e for e in after.elements if str(e.get("label", "")).strip() == "1"]
    assert badge or "Remove" in after.text, after.text[:300]


def test_scrolling_and_going_back_behave(browser):
    _goto(browser, "https://books.toscrape.com")
    run_action(browser.page, Action("scroll", "5"), [])
    run_action(browser.page, Action("goto", "https://example.com"), [])
    run_action(browser.page, Action("back"), [])
    browser.page.wait_for_load_state("domcontentloaded")
    assert "toscrape" in observe(browser.page).url


def test_a_missing_element_raises_rather_than_silently_passing(browser):
    """A click that matched nothing must be a failure the loop can see."""
    _goto(browser, "https://example.com")
    with pytest.raises(Exception):
        run_action(browser.page, Action("click", "999"), [])
