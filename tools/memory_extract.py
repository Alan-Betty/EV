"""Self-update: learn notes and links from a conversation that just ended.

Off by default (`MEMORY_EXTRACT_ENABLED`). A wrong note is quiet and it stays,
so this earns its place by being watched on real sessions first.

It reads only what the user said out loud: `Turn.user`, never `observation`.
Page text, messages and tool output are what memory poisoning would travel
through, and none of them are in the prompt. It runs on the planner's own rate
buckets, off the event loop, and answers to the guards itself because it does
not go through `dispatch`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import config
from tools.guard import audit, is_locked_down, redact

log = logging.getLogger("leo.tools.memory_extract")

_SYSTEM = """You file away lasting facts from what a user said to their assistant.
Keep only things still true next month: who people are to the user, what they
like, dates, what a project is. Skip requests, moods and anything about right now.
Only use what the user stated. If nothing qualifies, return no items.
Reply as JSON: {"items": [{"about": "Priya" or "topic: name", "note": "one short
sentence", "link": "relation: other, or empty"}]}"""

# Names that mean the user, who is not a branch of their own tree.
_NOT_A_SUBJECT = frozenset({"me", "user", "i", "myself", "the user", ""})


def parse_items(raw: str) -> list[dict[str, str]]:
    """Well-formed items from the planner's reply. Anything else is dropped."""
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    rows = loaded.get("items") if isinstance(loaded, dict) else None
    items: list[dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        about = str(row.get("about") or "").strip()
        note = str(row.get("note") or "").strip()
        link = str(row.get("link") or "").strip()
        if about.lower() in _NOT_A_SUBJECT or not (note or link):
            continue
        items.append({"about": about, "note": note, "link": link})
    return items[: max(0, config.MEMORY_EXTRACT_MAX_ITEMS)]


def extract_and_store(said: list[str], ask: Any = None) -> int:
    """Run one pass over the user's own sentences. Returns items stored."""
    if not config.MEMORY_EXTRACT_ENABLED or is_locked_down():
        return 0
    lines = [line.strip() for line in said if line and line.strip()]
    if not lines:
        return 0
    if ask is None:
        from tools.web_agent import ask_planner as ask

    from tools.memory import _remember_in_tree

    try:
        reply = ask("The user said:\n" + "\n".join(f"- {line}" for line in lines), _SYSTEM)
    except Exception as exc:
        log.debug("Memory extraction skipped: %s", exc)
        return 0
    stored = 0
    for item in parse_items(reply):
        # Re-read: a long pass can be overtaken by "stop everything".
        if is_locked_down():
            break
        note, link = redact(item["note"]), redact(item["link"])
        if _remember_in_tree("", note, item["about"], link).ok:
            stored += 1
            audit("memory_extracted", about=item["about"][:80], note=note[:160])
    return stored
