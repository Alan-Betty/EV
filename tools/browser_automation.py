"""`browser_task` - structured web work through the DOM, not the pixels.

`screen_task` can drive a browser by looking at it, and for a one-off click
that is fine. For anything with a form in it, it is the wrong tool: a CSS
selector cannot miss by three pixels, does not care whether the window is in
the foreground, and does not need a vision call per step. Filling a search
box, applying two filters and reading back the first result is four DOM
operations and zero screenshots.

Three constraints shape this module.

**Playwright is imported lazily and torn down eagerly.** It is by far the
heaviest thing E.V. can reach for, so the browser is launched when a task
starts and closed in a `finally` when it ends. Between tasks the resident
cost is one unused import path. A session that never runs a web task never
loads it at all, and a machine without it gets a spoken install hint instead
of an ImportError.

**The steps are a flat, spoken-sized DSL.** The model writes one action per
line - `goto amazon.com`, `fill #search = wireless mouse`, `click Add to
cart` - because a nested JSON plan is something an LLM gets wrong at exactly
the moment it matters. A bare string with no selector syntax is matched by
its visible text, which is how a person would describe it out loud.

**Every step is classified before it runs.** The same `classify_gui` that
gates a real click gates a DOM click, because "Place order" is a purchase
whether the pointer moved or not. A risky step stops the run and asks.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config
from tools.base import CancelToken, ToolResult, was_cancelled
from tools.safety import classify_gui

log = logging.getLogger("ev.tools.browser_automation")


@dataclass
class Step:
    """One parsed line of the step DSL."""

    verb: str
    target: str = ""
    value: str = ""

    def describe(self) -> str:
        if self.value:
            return f"{self.verb} {self.target} = {self.value}".strip()
        return f"{self.verb} {self.target}".strip()


_VERBS = {
    "goto": "goto",
    "go": "goto",
    "open": "goto",
    "navigate": "goto",
    "visit": "goto",
    "click": "click",
    "tap": "click",
    "press_button": "click",
    "fill": "fill",
    "type": "fill",
    "enter": "fill",
    "set": "fill",
    "select": "select",
    "choose": "select",
    "check": "check",
    "tick": "check",
    "press": "press",
    "key": "press",
    "wait": "wait",
    "sleep": "wait",
    "scroll": "scroll",
    "read": "read",
    "text": "read",
    "get": "read",
}

# A selector the model wrote deliberately, rather than a phrase to match by
# sight. Anything else is turned into a text match, which is how a person
# describes a button out loud.
# What counts as a selector the model wrote on purpose, rather than a phrase
# to match by sight. The tag-qualified forms matter as much as the rest and
# were missing: `div.s-main-slot`, `h2 a`, `span[data-price]` and
# `li.product > a` are all ordinary CSS that a planner writes constantly, and
# each of them used to be turned into a search for that literal *text*, which
# never matches and costs a whole round to a timeout.
_EXPLICIT_SELECTOR = re.compile(
    r"""^(?:
        (?:css|text|xpath|role|id)=   # an engine the model named itself
      | [#.][a-zA-Z_-]                # #id or .class
      | //                            # xpath
      | \[                            # [attribute]
      | [a-zA-Z][\w-]*[.#\[:]          # tag.class, tag#id, tag[attr], tag:nth
      | [a-zA-Z][\w-]*\s*[>~+]\s*      # tag > child, tag ~ sibling
      | [a-zA-Z][\w-]*\s+[.#\[]        # "div .price", "ul [role]"
    )""",
    re.VERBOSE,
)

# The one shape the pattern above cannot judge: bare tag names, alone or
# nested. "h2 a" is a selector and "Sign in" is a button, and nothing about
# the characters tells them apart - only knowing which words are HTML tags
# does. Kept short on purpose: every name here is a word no button says.
_TAGS = frozenset(
    """a article aside body button div em footer form h1 h2 h3 h4 h5 h6 header
    img input label li main nav ol option p section select span strong table
    tbody td textarea th thead tr ul""".split()
)


def looks_like_selector(target: str) -> bool:
    """True when this is CSS the model meant, not words it read on screen."""
    text = (target or "").strip()
    if not text:
        return False
    if _EXPLICIT_SELECTOR.match(text):
        return True
    parts = text.split()
    return bool(parts) and all(part.lower() in _TAGS for part in parts)


def _as_selector(target: str) -> str:
    text = target.strip().strip('"').strip("'")
    if not text:
        return text
    if looks_like_selector(text):
        # Bare tag names are lowercased: "read Body" means the whole page,
        # and a tag name is not a thing anyone types with a capital on
        # purpose.
        parts = text.split()
        return text.lower() if all(p.lower() in _TAGS for p in parts) else text
    # Playwright's text engine matches on the accessible visible text, which
    # is the thing the user would have named.
    return f"text={text}"


def parse_steps(raw: str) -> list[Step]:
    """Turn the step DSL into a list of steps.

    Lines are separated by newlines or semicolons; `verb target = value` is
    the whole grammar. Unparseable lines are dropped with a log line rather
    than aborting the run - one malformed step out of six should cost that
    step, not the task.
    """
    steps: list[Step] = []
    for chunk in re.split(r"[\n;]+", raw or ""):
        line = chunk.strip().lstrip("-*0123456789. ").strip()
        if not line:
            continue

        head, _, rest = line.partition(" ")
        verb = _VERBS.get(head.strip().lower())
        if verb is None:
            # A bare URL on its own line is a navigation, which the model
            # writes more often than the explicit form.
            if re.match(r"^(https?://|www\.)", line, re.IGNORECASE):
                steps.append(Step("goto", line))
                continue
            log.debug("Dropping unparseable browser step: %r", line)
            continue

        # Only the verbs that take a value are split on `=`. A URL is full
        # of them - `goto amazon.in/s?k=mouse` was being cut down to
        # `amazon.in/s?k`, which loads a different page rather than failing.
        if verb in {"fill", "select"}:
            target, _, value = rest.partition("=")
        else:
            target, value = rest, ""
        steps.append(Step(verb, target.strip(), value.strip()))
    return steps


def _normalise_url(url: str) -> str:
    """Turn what the model wrote into something `page.goto` will accept.

    A bare name like "gmail" is looked up in `SEARCH_ENGINES` first, which is
    the same table `web_search` uses for destinations - so there is one list
    of where "my mail" means, not two that drift apart. Only entries that are
    places rather than searches qualify; a search template with a `{q}` hole
    in it is not somewhere you can navigate to.
    """
    text = url.strip().strip('"').strip("'")
    if not text:
        return text

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text) and "." not in text:
        destination = config.SEARCH_ENGINES.get(text.lower(), "")
        if destination and "{q}" not in destination:
            return destination

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        text = "https://" + text
    return text


def _visible(page: Any, selector: str) -> str:
    """Narrow an ambiguous text match to the copies a person could see.

    `text=Search` on Amazon resolves to four elements, and Playwright takes
    the first - a label inside a collapsed menu that is outside the viewport
    however far it scrolls. The click then retries for the full step timeout
    and the errand dies on a button nobody could have pressed. Only an
    ambiguous text match is narrowed: a unique one, or a selector the model
    wrote deliberately, is left exactly as written.
    """
    if not selector.startswith("text="):
        return selector
    try:
        if page.locator(selector).count() > 1:
            return f"{selector} >> visible=true"
    except Exception:  # a fake page, or one mid-navigation
        pass
    return selector


def _run_step(page: Any, step: Step, gathered: list[str]) -> str:
    """Execute one step against a live page. Returns a one-line note."""
    timeout_ms = int(config.BROWSER_STEP_TIMEOUT_S * 1000)

    if step.verb == "goto":
        url = _normalise_url(step.target or step.value)
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        return f"opened {url}"

    if step.verb == "click":
        selector = _visible(page, _as_selector(step.target))
        page.click(selector, timeout=timeout_ms)
        return f"clicked {step.target}"

    if step.verb == "fill":
        selector = _visible(page, _as_selector(step.target))
        page.fill(selector, step.value, timeout=timeout_ms)
        return f"filled {step.target}"

    if step.verb == "select":
        selector = _as_selector(step.target)
        page.select_option(selector, step.value, timeout=timeout_ms)
        return f"selected {step.value} in {step.target}"

    if step.verb == "check":
        selector = _visible(page, _as_selector(step.target))
        page.check(selector, timeout=timeout_ms)
        return f"ticked {step.target}"

    if step.verb == "press":
        key = (step.target or step.value or "Enter").strip()
        page.keyboard.press(key)
        return f"pressed {key}"

    if step.verb == "wait":
        seconds = _seconds(step.target or step.value)
        if seconds is None:
            page.wait_for_selector(_as_selector(step.target), timeout=timeout_ms)
            return f"waited for {step.target}"
        page.wait_for_timeout(int(min(seconds, config.BROWSER_STEP_TIMEOUT_S) * 1000))
        return f"waited {seconds:g}s"

    if step.verb == "scroll":
        amount = _seconds(step.target or step.value)
        pixels = int((amount if amount is not None else 6) * 100)
        page.mouse.wheel(0, pixels)
        return f"scrolled {pixels}px"

    if step.verb == "read":
        selector = _as_selector(step.target) if step.target else "body"
        # `all_inner_texts` rather than `inner_text`, because the interesting
        # reads are lists: an inbox is thirty rows and a results page is
        # twenty cards. `inner_text` returns the first match and nothing
        # else, which is how "summarise the important mail" became a summary
        # of one message.
        locator = page.locator(selector)
        locator.first.wait_for(timeout=timeout_ms, state="attached")
        chunks = locator.all_inner_texts()[: max(1, config.BROWSER_READ_ITEMS)]
        joined = " | ".join(" ".join(chunk.split()) for chunk in chunks if chunk.strip())
        gathered.append(joined[: config.BROWSER_READ_CHARS])
        return f"read {len(chunks)} item(s) from {step.target or 'the page'}"

    return f"skipped unknown step {step.verb}"


def _seconds(raw: str) -> float | None:
    try:
        return float(re.sub(r"[^0-9.\-]", "", raw or "") or "x")
    except ValueError:
        return None


def _open_browser(playwright: Any, headless: bool) -> tuple[Any, Any]:
    """Start a browser and return (closeable, page).

    A persistent profile is the default, and it is the difference between
    "open Gmail and summarise the important mail" working and stopping at a
    sign-in page. Playwright's plain `launch` gives a blank browser with no
    cookies, so every task starts logged out of everything the user is
    logged in to; `launch_persistent_context` keeps a profile directory, so
    they sign in once, in a window they can watch, and it holds after that.

    The profile is E.V.'s own rather than the user's real Chrome one. Chrome
    locks its profile while it is running, so borrowing it would fail
    whenever the user had a browser open - which is always - and automating
    a live signed-in profile is a much bigger thing to hand a voice command
    than automating a browser kept for the purpose.
    """
    engine = getattr(playwright, config.BROWSER_ENGINE, None) or playwright.chromium

    if config.BROWSER_PERSIST_PROFILE:
        profile = Path(config.BROWSER_PROFILE_DIR)
        try:
            profile.mkdir(parents=True, exist_ok=True)
            context = engine.launch_persistent_context(
                str(profile), headless=headless
            )
            # A persistent context opens with one page already; using it
            # rather than adding a second avoids leaving a blank tab behind.
            page = context.pages[0] if context.pages else context.new_page()
            return context, page
        except Exception as exc:
            # A locked or unwritable profile directory should cost the
            # logins, not the errand.
            log.warning(
                "Could not open the browser profile at %s (%s); "
                "falling back to a fresh browser",
                profile,
                exc,
            )

    browser = engine.launch(headless=headless)
    return browser, browser.new_page()


def _announce(label: str, cancel: CancelToken, headless: bool) -> Any:
    """The overlay for a visible run; nothing at all for a headless one.

    A browser clicking and typing on its own is E.V. in control of the
    screen as much as a moving pointer is, so it gets the same frame and the
    same kill switch. A headless run has nothing on screen to explain.
    """
    if headless:
        return nullcontext(None)
    from tools.overlay import taking_over

    return taking_over(label, cancel)


def _goal_run(
    task: str, url: str, notes: str, approved: str, confirmed: bool,
    cancel: CancelToken | None, headless: bool = False,
) -> ToolResult:
    """A goal with no script: let the DOM planner work the steps out.

    The model used to have to write the script itself, which meant inventing
    CSS selectors for a page it had never seen - and a call carrying only a
    goal was refused outright with "I need to know what to do in the
    browser". The planner in `tools.web_agent` reads the real page and acts on
    numbered elements instead, so a goal is now the easy case rather than the
    failing one. Confirmation is scoped exactly as `agent_task` scopes it.
    """
    from tools import web_agent  # web_agent imports this module

    verdict = classify_gui(task)
    risky = verdict.needs_confirmation and config.COMPUTER_CONFIRM_RISKY
    if risky and not confirmed:
        return ToolResult.confirm(
            f"That one {verdict.reason}: {task.rstrip('.')}. Confirm?",
            f"Awaiting confirmation for browser_task '{task}': {verdict.reason}.",
            task=task,
            url=url,
            notes=notes,
        )
    allowed = web_agent.allow(
        verdict.reason if verdict.needs_confirmation else "",
        approved if confirmed else "",
    )
    history = [line for line in (notes or "").split("; ") if line.strip()]
    token = cancel if cancel is not None else CancelToken()
    with _announce(task, token, headless) as hud:
        outcome = web_agent.web_mission(
            task,
            start=url,
            history=history,
            allowed=allowed,
            cancel=token,
            killed=(lambda: hud.killed) if hud is not None else None,
            note=hud.note if hud is not None else None,
        )
    if outcome.needs_confirmation:
        return ToolResult.confirm(
            outcome.speech,
            f"browser_task '{task}' {outcome.detail}. Confirming carries on "
            "from where it got to.",
            task=task,
            url=url,
            notes="; ".join(outcome.history[-8:]),
            approved=web_agent.allow(allowed, outcome.reason),
        )
    return web_agent.result_for(outcome, task)


def _run_plan(
    page: Any,
    plan: list[Step],
    label: str,
    cancel: CancelToken | None,
    done: list[str],
    gathered: list[str],
) -> ToolResult | None:
    """Run every step on `page`; None when all of them ran."""
    deadline = time.monotonic() + config.BROWSER_TASK_TIMEOUT_S
    for index, step in enumerate(plan, start=1):
        if was_cancelled(cancel):
            return ToolResult.stopped(
                "Stopped.",
                f"browser_task '{label}' cancelled before step "
                f"{index}. Done: {'; '.join(done) or 'nothing'}.",
            )
        if time.monotonic() > deadline:
            return ToolResult.success(
                "Ran out of time in there.",
                f"browser_task '{label}' hit the "
                f"{config.BROWSER_TASK_TIMEOUT_S:.0f}s ceiling after "
                f"{len(done)} step(s): {'; '.join(done) or 'none'}.",
            )
        try:
            done.append(_run_step(page, step, gathered))
        except Exception as exc:
            # A selector that does not match is the normal failure here, and
            # the model can often fix it on the next turn - so name the step
            # rather than dumping a stack trace.
            reason = type(exc).__name__
            log.info("Browser step %r failed: %s", step.describe(), exc)
            try:
                where = str(page.url or "")
            except Exception:
                where = ""
            return ToolResult.failure(
                f"Got stuck on the page at step {index}.",
                f"browser_task '{label}' failed at step "
                f"'{step.describe()}' ({reason}). Done: "
                f"{'; '.join(done) or 'nothing'}.",
                stuck=step.describe(),
                at_url=where,
                done=list(done),
            )
    return None


def browser_task(
    task: str = "",
    url: str = "",
    steps: str = "",
    headless: bool = False,
    notes: str = "",
    approved: str = "",
    confirmed: bool = False,
    cancel: CancelToken | None = None,
    **_: object,
) -> ToolResult:
    """Run a web errand in a real browser: a script if given one, else a goal.

    The scripted run is all-or-nothing about confirmation: the whole task is
    held if any step reads as risky, rather than stopping half way through a
    checkout to ask. That is the opposite of `screen_task`, and deliberately
    so - a DOM script is known in full before the first step runs, whereas a
    vision loop only discovers its next move by looking.
    """
    if not config.BROWSER_AUTOMATION_ENABLED:
        return ToolResult.failure(
            "Browser automation is switched off.",
            "EV_BROWSER_AUTOMATION_ENABLED is false; no browser was launched.",
        )

    want_headless = bool(headless) or config.BROWSER_HEADLESS
    plan = parse_steps(steps)
    if not plan and task.strip():
        return _goal_run(
            task.strip(), url.strip(), notes, approved, confirmed, cancel,
            want_headless,
        )
    if url.strip():
        plan.insert(0, Step("goto", url.strip()))
    if not plan:
        return ToolResult.failure(
            "I need to know what to do in the browser.",
            "browser_task needs a task, a url or at least one parseable step. "
            "Steps look like: goto amazon.com / fill #twotabsearchtextbox = "
            "wireless mouse / press Enter / read .s-result-item",
        )

    if len(plan) > config.BROWSER_MAX_STEPS:
        return ToolResult.failure(
            "That's a longer errand than I'll run in one go.",
            f"browser_task was given {len(plan)} steps; the ceiling is "
            f"{config.BROWSER_MAX_STEPS}. Split it up.",
        )

    # Every step is inspected before the browser even starts, so a purchase
    # buried at step five is asked about at step zero.
    if not confirmed and config.COMPUTER_CONFIRM_RISKY:
        for step in plan:
            verdict = classify_gui(f"{task} {step.describe()}")
            if verdict.needs_confirmation:
                return ToolResult.confirm(
                    f"That one {verdict.reason} - {step.target or step.verb}. Confirm?",
                    f"Awaiting confirmation for browser_task '{task or url}': "
                    f"step '{step.describe()}' {verdict.reason}.",
                    task=task,
                    url=url,
                    steps=steps,
                    headless=headless,
                )

    label = task or url
    token = cancel if cancel is not None else CancelToken()
    with _announce(label, token, want_headless):
        result = _scripted(plan, label, token, want_headless)
        # A script is the model's guess at a page it has never seen, and on
        # a real shop the guess is usually wrong somewhere: "click Search"
        # meets four elements with that word on them. When there is a goal
        # behind the script, the page is handed to the planner - which reads
        # it rather than guessing - carrying on from where the script got
        # to, instead of ending the errand on step three.
        if result.data.get("stuck") and task.strip():
            return _take_over_from_script(
                result, task.strip(), approved, confirmed, token, want_headless
            )
        return result


def _take_over_from_script(
    stuck: ToolResult,
    task: str,
    approved: str,
    confirmed: bool,
    cancel: CancelToken,
    headless: bool,
) -> ToolResult:
    """Carry a failed script on as a planned run, from the page it stopped on."""
    from tools import web_agent  # web_agent imports this module

    log.info("Script stuck at %r; handing the page to the planner", stuck.data["stuck"])
    done = [line for line in stuck.data.get("done", []) if line]
    done.append(f"could not {stuck.data['stuck']} - that step failed")
    # A kept browser is still sitting on the page the script reached, so
    # the planner starts there. A throwaway one has gone, so it is sent back.
    start = "" if web_agent.keeping() and not headless else stuck.data.get("at_url", "")
    result = _goal_run(
        task, start, "; ".join(done), approved, confirmed, cancel, headless
    )
    if result.needs_confirmation:
        return result
    result.detail = (
        f"The scripted steps got stuck at '{stuck.data['stuck']}', so the page "
        f"was handed to the planner. {result.detail}"
    )
    return result


def _scripted(
    plan: list[Step], label: str, cancel: CancelToken, want_headless: bool
) -> ToolResult:
    """Run a script in a browser: the kept one if there is one, else a fresh one."""
    done: list[str] = []
    gathered: list[str] = []

    from tools import web_agent  # web_agent imports this module

    if web_agent.keeping() and not want_headless:
        # The kept browser: the page stays on screen afterwards, and a
        # browser already open from the last errand is reused rather than
        # cold-started. A failed script closes it unless the planner is
        # about to take over from the step it stuck on.
        try:
            # A stuck step keeps the browser too: the planner is about to
            # carry on from exactly that page.
            failure = web_agent._KEEPER.run(
                lambda session: _run_plan(
                    session.current_page(), plan, label, cancel, done, gathered
                ),
                keep=lambda outcome: outcome is None or bool(outcome.data.get("stuck")),
            )
        except Exception as exc:
            log.warning("Playwright run failed: %s", exc)
            return ToolResult.failure(
                "The browser wouldn't cooperate.",
                f"browser_task '{label}' failed to run: {type(exc).__name__}: {exc}",
            )
        return failure or _report(label, done, gathered)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ToolResult.failure(
            "I don't have a browser driver installed.",
            "Playwright is not installed. Run: pip install playwright && "
            "python -m playwright install chromium",
        )

    browser = None
    try:
        # The sync API refuses to run inside an asyncio loop, which is
        # exactly why `dispatch` puts tools on a worker thread. There is no
        # loop on this thread, so this is the supported way to use it.
        with sync_playwright() as playwright:
            browser, page = _open_browser(playwright, want_headless)
            failure = _run_plan(page, plan, label, cancel, done, gathered)
            if failure is not None:
                return failure
    except Exception as exc:
        log.warning("Playwright run failed: %s", exc)
        return ToolResult.failure(
            "The browser wouldn't cooperate.",
            f"browser_task '{label}' failed to run: "
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        # Eager teardown when nothing is being kept: Playwright's resident
        # cost is only acceptable because it does not outlive the task.
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
    return _report(label, done, gathered)


def _report(label: str, done: list[str], gathered: list[str]) -> ToolResult:
    """A finished script as the result the model reads next turn."""
    read_back = " ".join(gathered).strip()
    detail = f"browser_task '{label}' completed {len(done)} step(s): {'; '.join(done)}."
    if read_back:
        # The page text goes in `detail`, never in `speech`. It is raw markup
        # text - navigation labels, timestamps, "1 of 47" - and reading the
        # first 180 characters of that aloud was the assistant's worst
        # possible answer to "what's in my inbox". The model gets the whole
        # lot on the next turn and says something a person would say.
        detail += (
            f" Text read from the page, for you to summarise or quote when "
            f"the user asks: {read_back}"
        )
    return ToolResult.success("That's done.", detail, page_text=read_back)


__all__ = ["Step", "browser_task", "parse_steps"]
