"""`web_search` - open a browser on a search results page or a direct URL."""

from __future__ import annotations

import logging
import re
import webbrowser
from urllib.parse import quote_plus

import config
from tools.base import ToolResult, popen_detached, resolve_executable

log = logging.getLogger("ev.tools.browser")

# Executable candidates per browser, in preference order.
_BROWSER_BINARIES: dict[str, list[str]] = {
    "chrome": ["chrome", "chrome.exe", "google-chrome"],
    "edge": ["msedge", "msedge.exe", "microsoft-edge"],
    "firefox": ["firefox", "firefox.exe"],
    "brave": ["brave", "brave.exe", "brave-browser"],
}

# Filler the speech-to-text layer tends to leave attached to a query.
_FILLER = re.compile(
    r"^(please\s+)?(can\s+you\s+|could\s+you\s+|go\s+ahead\s+and\s+)?"
    r"(search|look|find|google|bing|check)\s+"
    r"(for\s+|up\s+|me\s+|out\s+)*",
    re.IGNORECASE,
)


def _clean_query(query: str) -> str:
    cleaned = _FILLER.sub("", query.strip()).strip()
    return cleaned or query.strip()


def _is_destination(engine: str) -> bool:
    """True for an entry that is a place rather than a search.

    "Open my email" names somewhere to go; there is nothing to put in a query
    string. Those templates simply have no `{q}` in them, so the template is
    its own test - no second list to keep in step with the first.
    """
    template = config.SEARCH_ENGINES.get(engine.lower(), "")
    return bool(template) and "{q}" not in template


def _build_url(query: str, engine: str) -> str:
    template = config.SEARCH_ENGINES.get(
        engine.lower(), config.SEARCH_ENGINES[config.DEFAULT_SEARCH_ENGINE]
    )
    return template.format(q=quote_plus(query))


def _normalise_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    return url


def _open_in(browser: str, url: str) -> bool:
    """Open `url` in a named browser. Returns False if it is not installed."""
    for candidate in _BROWSER_BINARIES.get(browser, []):
        exe = resolve_executable(candidate)
        if not exe:
            continue
        try:
            popen_detached([exe, url])
            return True
        except OSError as exc:
            log.debug("Failed to launch %s: %s", exe, exc)
    return False


def web_search(
    query: str = "",
    engine: str = "",
    browser: str = "",
    url: str = "",
    **_: object,
) -> ToolResult:
    engine = (engine or config.DEFAULT_SEARCH_ENGINE).lower()
    browser = (browser or config.DEFAULT_BROWSER or "default").lower()

    if url and url.strip():
        target = _normalise_url(url)
        spoken = re.sub(r"^https?://(www\.)?", "", target).split("/")[0]
        speech = f"Opening {spoken}."
    elif _is_destination(engine):
        # A place, not a search. This used to fall through to "Search for
        # what, exactly?" when there was no query, so "open my email" got a
        # question back instead of an inbox - and when there *was* a query,
        # it built a search URL for a site that has no search endpoint.
        target = config.SEARCH_ENGINES[engine.lower()]
        speech = {
            "mail": "Opening your mail.",
            "gmail": "Opening Gmail.",
            "outlook": "Opening Outlook.",
            "calendar": "Opening your calendar.",
            "drive": "Opening your drive.",
        }.get(engine.lower(), f"Opening {engine}.")
    elif query and query.strip():
        cleaned = _clean_query(query)
        target = _build_url(cleaned, engine)
        # Reading the whole query back is slow and often ungrammatical once
        # filler has been stripped. Name the destination instead.
        if engine == "google":
            speech = "On it." if len(cleaned) > 28 else f"Searching for {cleaned}."
        else:
            speech = f"Checking {engine}."
    else:
        return ToolResult.failure("Search for what, exactly?")

    return _launch(target, speech, browser)


def _launch(target: str, speech: str, browser: str) -> ToolResult:
    """Open a URL in the named browser, falling back to the default one."""
    if browser != "default":
        if _open_in(browser, target):
            return ToolResult.success(speech, f"Opened {target} in {browser}")
        log.info("%s not installed, falling back to the default browser", browser)
        speech = f"{browser.capitalize()} isn't installed. {speech}"

    try:
        opened = webbrowser.open(target, new=2, autoraise=True)
    except Exception as exc:  # webbrowser raises assorted OS errors
        log.warning("webbrowser.open failed: %s", exc)
        opened = False

    if opened:
        return ToolResult.success(speech, f"Opened {target} in the default browser")
    return ToolResult.failure("Couldn't get a browser open.", f"Failed to open {target}")
