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
_EXPLICIT_SELECTOR = re.compile(r"^(css=|text=|xpath=|role=|id=|#|\.[a-zA-Z]|//|\[)")


# Bare words that are structural elements rather than something a person
# would read off the screen. "read body" means the whole page, and turning it
# into a search for the visible word "body" is how "summarise my inbox" timed
# out on a perfectly good Gmail tab.
_STRUCTURAL_TAGS = frozenset(
    {"body", "main", "article", "table", "ul", "ol", "form", "header", "footer", "nav"}
)


def _as_selector(target: str) -> str:
    text = target.strip().strip('"').strip("'")
    if not text:
        return text
    if _EXPLICIT_SELECTOR.match(text):
        return text
    if text.lower() in _STRUCTURAL_TAGS:
        return text.lower()
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

        target, _, value = rest.partition("=")
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


def _run_step(page: Any, step: Step, gathered: list[str]) -> str:
    """Execute one step against a live page. Returns a one-line note."""
    timeout_ms = int(config.BROWSER_STEP_TIMEOUT_S * 1000)

    if step.verb == "goto":
        url = _normalise_url(step.target or step.value)
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        return f"opened {url}"

    if step.verb == "click":
        selector = _as_selector(step.target)
        page.click(selector, timeout=timeout_ms)
        return f"clicked {step.target}"

    if step.verb == "fill":
        selector = _as_selector(step.target)
        page.fill(selector, step.value, timeout=timeout_ms)
        return f"filled {step.target}"

    if step.verb == "select":
        selector = _as_selector(step.target)
        page.select_option(selector, step.value, timeout=timeout_ms)
        return f"selected {step.value} in {step.target}"

    if step.verb == "check":
        selector = _as_selector(step.target)
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


def browser_task(
    task: str = "",
    url: str = "",
    steps: str = "",
    headless: bool = False,
    confirmed: bool = False,
    cancel: CancelToken | None = None,
    **_: object,
) -> ToolResult:
    """Run a scripted web workflow in a real browser.

    The run is all-or-nothing about confirmation: the whole task is held if
    any step reads as risky, rather than stopping half way through a checkout
    to ask. That is the opposite of `screen_task`, and deliberately so - a
    DOM script is known in full before the first step runs, whereas a vision
    loop only discovers its next move by looking.
    """
    if not config.BROWSER_AUTOMATION_ENABLED:
        return ToolResult.failure(
            "Browser automation is switched off.",
            "EV_BROWSER_AUTOMATION_ENABLED is false; no browser was launched.",
        )

    plan = parse_steps(steps)
    if url.strip():
        plan.insert(0, Step("goto", url.strip()))
    if not plan:
        return ToolResult.failure(
            "I need to know what to do in the browser.",
            "browser_task needs either a url or at least one parseable step. "
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

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ToolResult.failure(
            "I don't have a browser driver installed.",
            "Playwright is not installed. Run: pip install playwright && "
            "python -m playwright install chromium",
        )

    want_headless = bool(headless) or config.BROWSER_HEADLESS
    deadline = time.monotonic() + config.BROWSER_TASK_TIMEOUT_S
    done: list[str] = []
    gathered: list[str] = []
    browser = None

    try:
        # The sync API refuses to run inside an asyncio loop, which is
        # exactly why `dispatch` puts tools on a worker thread. There is no
        # loop on this thread, so this is the supported way to use it.
        with sync_playwright() as playwright:
            browser, page = _open_browser(playwright, want_headless)

            for index, step in enumerate(plan, start=1):
                if was_cancelled(cancel):
                    return ToolResult.stopped(
                        "Stopped.",
                        f"browser_task '{task or url}' cancelled before step "
                        f"{index}. Done: {'; '.join(done) or 'nothing'}.",
                    )
                if time.monotonic() > deadline:
                    return ToolResult.success(
                        "Ran out of time in there.",
                        f"browser_task '{task or url}' hit the "
                        f"{config.BROWSER_TASK_TIMEOUT_S:.0f}s ceiling after "
                        f"{len(done)} step(s): {'; '.join(done) or 'none'}.",
                    )
                try:
                    done.append(_run_step(page, step, gathered))
                except Exception as exc:
                    # A selector that does not match is the normal failure
                    # here, and the model can often fix it on the next turn -
                    # so name the step rather than dumping a stack trace.
                    reason = type(exc).__name__
                    log.info("Browser step %r failed: %s", step.describe(), exc)
                    return ToolResult.failure(
                        f"Got stuck on the page at step {index}.",
                        f"browser_task '{task or url}' failed at step "
                        f"'{step.describe()}' ({reason}). Done: "
                        f"{'; '.join(done) or 'nothing'}.",
                    )
    except Exception as exc:
        log.warning("Playwright run failed: %s", exc)
        return ToolResult.failure(
            "The browser wouldn't cooperate.",
            f"browser_task '{task or url}' failed to run: "
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        # Eager teardown: Playwright's resident cost is only acceptable
        # because it does not outlive the task that needed it.
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    read_back = " ".join(gathered).strip()
    detail = (
        f"browser_task '{task or url}' completed {len(done)} step(s): "
        f"{'; '.join(done)}."
    )
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
    speech = "That's done."
    return ToolResult.success(speech, detail, page_text=read_back)


__all__ = ["Step", "browser_task", "parse_steps"]
