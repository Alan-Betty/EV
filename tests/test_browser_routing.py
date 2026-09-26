"""Offline tests for getting web errands to the browser, and keeping it there.

"Do it in the browser" had stopped reaching Playwright, and nothing failed
loudly. Four separate gaps did it, and each one is pinned here:

* the route planner's "web" answer was parsed with the *step* vocabulary and
  thrown away, so every errand fell to a keyword guess that says "desktop"
  for anything off its list;
* the tool selector never offered the browser tools for "go to", a URL, a
  log-in or most site names, so the model could only open a page;
* a confirmation-only argument the model supplied itself went straight
  through `dispatch`;
* the browser closed the moment an errand finished, so a video or a basket
  was gone before anyone saw it.

Nothing here launches a browser or reaches the network.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Keep the tests deterministic regardless of the developer's own .env.
os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

import config  # noqa: E402
from tools import CONFIRMATION_ONLY_ARGS, from_model, mission, web_agent  # noqa: E402
from tools.schemas import select_tools  # noqa: E402


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------
def test_a_web_answer_from_the_route_planner_survives(monkeypatch):
    monkeypatch.setattr(
        web_agent, "ask_planner",
        lambda *_a, **_k: json.dumps({"mode": "web", "url": "amazon.in", "why": "a shop"}),
    )
    assert web_agent.choose_route("find me a gaming mouse") == {
        "mode": "web", "url": "amazon.in", "why": "a shop",
    }


def test_a_fenced_web_answer_survives_too(monkeypatch):
    monkeypatch.setattr(
        web_agent, "ask_planner",
        lambda *_a, **_k: '```json\n{"mode": "web", "url": "github.com"}\n```',
    )
    assert web_agent.choose_route("star the playwright repo")["mode"] == "web"


def test_a_desktop_answer_is_still_desktop(monkeypatch):
    monkeypatch.setattr(
        web_agent, "ask_planner", lambda *_a, **_k: '{"mode": "desktop", "why": "mixer"}'
    )
    assert web_agent.choose_route("turn the volume down")["mode"] == "desktop"


def test_the_mission_takes_the_planners_web_route_and_url(monkeypatch):
    """The route that used to be discarded is the one that runs."""
    monkeypatch.setattr(
        web_agent, "ask_planner",
        lambda *_a, **_k: '{"mode": "web", "url": "github.com"}',
    )
    assert mission._route_for("open github and star the playwright repo", "") == (
        "web", "github.com",
    )


@pytest.mark.parametrize("goal", [
    "open github and star the playwright repo",
    "go to news.ycombinator.com and read the top story",
])
def test_the_offline_guess_knows_a_website(goal):
    assert web_agent.guess_route(goal) == "web"


def test_the_offline_guess_still_knows_the_desktop():
    assert web_agent.guess_route("open notepad and type hello") == "desktop"


# ---------------------------------------------------------------------------
# The selector
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("utterance", [
    "navigate to example.com",
    "go to wikipedia and tell me about black holes",
    "log into netflix",
    "search google for python tutorials and open the first result",
    "open github and star the playwright repo",
    "look up the weather on bbc.co.uk",
    "play lofi on youtube",
])
def test_a_web_errand_is_offered_the_browser(utterance):
    offered = select_tools(utterance)
    assert "browser_task" in offered
    assert "agent_task" in offered  # the family travels together


@pytest.mark.parametrize("utterance", ["open notepad", "tell me a joke", "open spotify"])
def test_an_ordinary_command_is_not_charged_for_it(utterance):
    assert "browser_task" not in select_tools(utterance)


# ---------------------------------------------------------------------------
# What only a spoken yes may carry
# ---------------------------------------------------------------------------
def test_the_model_cannot_confirm_its_own_call():
    call = {"path": "build", "action": "delete", "confirmed": True,
            "notes": "clicked Place order", "approved": "places an order"}
    assert from_model(call) == {"path": "build", "action": "delete"}
    assert {"confirmed", "notes", "approved"} <= CONFIRMATION_ONLY_ARGS


def test_approvals_accumulate_and_are_matched_exactly():
    both = web_agent.allow("adds to a basket", "places an order\nadds to a basket")
    assert both == "adds to a basket\nplaces an order"
    assert web_agent.allows(both, "places an order")
    assert not web_agent.allows(both, "sends a message")
    assert not web_agent.allows("", "")


# ---------------------------------------------------------------------------
# The kept browser
# ---------------------------------------------------------------------------
class _FakeSession:
    """Counts how often a browser is started and closed."""

    started = 0
    closed = 0

    def __init__(self, *_a, **_k):
        self.page = object()
        self.open = False

    def __enter__(self):
        type(self).started += 1
        self.open = True
        return self

    def close(self):
        if self.open:
            type(self).closed += 1
        self.open = False

    def __exit__(self, *_):
        self.close()

    def alive(self):
        return self.open

    def current_page(self):
        return self.page

    def take_dialogs(self):
        return ""


@pytest.fixture
def keeper(monkeypatch):
    class Session(_FakeSession):
        started = 0
        closed = 0

    monkeypatch.setattr(config, "BROWSER_KEEP_OPEN", True)
    monkeypatch.setattr(config, "BROWSER_HEADLESS", False)
    monkeypatch.setattr(web_agent, "BrowserSession", Session)
    fresh = web_agent._Keeper()
    monkeypatch.setattr(web_agent, "_KEEPER", fresh)
    yield fresh, Session
    fresh.release()


def test_a_finished_errand_leaves_the_browser_up_and_the_next_reuses_it(keeper):
    host, Session = keeper
    seen = []
    host.run(lambda s: seen.append(s) or "ok", keep=lambda r: r == "ok")
    host.run(lambda s: seen.append(s) or "ok", keep=lambda r: r == "ok")
    assert Session.started == 1 and Session.closed == 0
    assert seen[0] is seen[1]  # one browser, reused - no second cold start


def test_a_failed_errand_closes_it(keeper):
    host, Session = keeper
    host.run(lambda s: "bad", keep=lambda r: r == "ok")
    assert Session.closed == 1


def test_an_exception_closes_it_and_still_reaches_the_caller(keeper):
    host, Session = keeper

    def boom(_session):
        raise RuntimeError("page crashed")

    with pytest.raises(RuntimeError):
        host.run(boom, keep=lambda r: True)
    assert Session.closed == 1


def test_a_window_the_user_closed_is_replaced_not_reused(keeper):
    host, Session = keeper
    first = host.run(lambda s: s, keep=lambda r: True)
    first.open = False  # the user closed the window
    second = host.run(lambda s: s, keep=lambda r: True)
    assert second is not first
    assert Session.started == 2


def test_release_closes_the_kept_browser(keeper):
    host, Session = keeper
    host.run(lambda s: "ok", keep=lambda r: True)
    host.release()
    assert Session.closed == 1


def test_a_web_mission_that_needs_the_user_keeps_the_page_up(keeper, monkeypatch):
    """A sign-in or a captcha is exactly when the window must stay."""
    host, Session = keeper
    monkeypatch.setattr(web_agent, "observe", lambda *_a, **_k: web_agent.Observation(url="x"))
    monkeypatch.setattr(
        web_agent, "ask_planner",
        lambda *_a, **_k: '{"mode": "ask", "speech": "It wants your password."}',
    )
    outcome = web_agent.web_mission("log into netflix")
    assert outcome.status == "ask"
    assert Session.started == 1 and Session.closed == 0


# ---------------------------------------------------------------------------
# A yes to a new risk mid-errand
# ---------------------------------------------------------------------------
def test_a_yes_mid_mission_is_not_asked_again(monkeypatch):
    """The resumed run used to allow only the original errand's risk, so the
    step that had just been agreed to was held again, and again."""
    monkeypatch.setattr(config, "AGENT_OVERLAY_ENABLED", False)
    monkeypatch.setattr(config, "AGENT_HOTKEY_ENABLED", False)
    monkeypatch.setattr(mission, "_route_for", lambda goal, start: ("web", "shop.test"))
    seen = []

    def run(goal, start="", history=None, allowed="", **_):
        seen.append(allowed)
        if not web_agent.allows(allowed, "places an order"):
            return web_agent.WebOutcome(
                "confirm", "Next bit places an order: Place order. Confirm?",
                "held at round 4", ["clicked Add to basket"],
                needs_confirmation=True, reason="places an order",
            )
        return web_agent.WebOutcome("done", "Ordered.", "done", ["clicked Place order"])

    monkeypatch.setattr(web_agent, "web_mission", run)
    start = mission.agent_task(task="find a mouse on the shop")
    assert start.needs_confirmation  # taking over is always asked first
    paused = mission.agent_task(**start.data, confirmed=True)
    assert paused.needs_confirmation and "places an order" in paused.speech
    resumed = mission.agent_task(**paused.data, confirmed=True)
    assert resumed.ok and resumed.speech == "Ordered."
