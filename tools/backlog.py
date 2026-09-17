"""`backlog` - read back, tick off, or retry what was left unfinished.

The store itself lives in [ev/backlog.py](../ev/backlog.py); this is only the
face it shows the model. Two rules keep it honest:

* **Speech stays short, detail carries the list.** "Three things open" is what
  gets said; the numbered listing goes to `detail` for the model. Reading a
  backlog aloud item by item is exactly the kind of thing that makes an
  assistant tiresome.
* **Retrying is never pre-approved.** `run` re-dispatches the stored tool
  *without* `confirmed`, so a delete that was held for a spoken yes last
  session is held for a spoken yes again. A backlog entry is a reminder, not a
  signed permission slip.
"""

from __future__ import annotations

import logging

import config
from ev.backlog import get_backlog
from tools.base import ToolResult

log = logging.getLogger("ev.tools.backlog")


def _summarise(store) -> ToolResult:
    pending = store.pending()
    if not pending:
        return ToolResult.success("Nothing outstanding.", "The backlog is empty.")
    first = pending[0].text
    speech = (
        f"One thing open: {first}."
        if len(pending) == 1
        else f"{len(pending)} open. First one's {first}."
    )
    return ToolResult.success(speech, f"Backlog ({len(pending)} pending):\n{store.listing()}")


def backlog(
    action: str = "",
    text: str = "",
    item: str = "",
    confirmed: bool = False,
    **_: object,
) -> ToolResult:
    """Single entry point for the backlog. Never raises."""
    store = get_backlog()
    verb = (action or "").strip().lower() or "list"
    verb = {
        "show": "list",
        "read": "list",
        "check": "list",
        "remaining": "list",
        "new": "add",
        "create": "add",
        "remember": "add",
        "note": "add",
        "complete": "done",
        "finish": "done",
        "tick": "done",
        "remove": "drop",
        "delete": "drop",
        "clear_all": "clear",
        "empty": "clear",
        "execute": "run",
        "retry": "run",
        "do": "run",
    }.get(verb, verb)

    if not store.enabled:
        return ToolResult.failure(
            "My backlog's switched off.",
            "Backlog disabled; set EV_BACKLOG_ENABLED=true to use it.",
        )

    if verb == "list":
        return _summarise(store)

    if verb == "add":
        entry = store.add(text or item, kind="reminder")
        if entry is None:
            return ToolResult.failure(
                "You didn't say what to add.", "'add' needs a text argument."
            )
        return ToolResult.success(
            f"Added. {len(store.pending())} open.",
            f"Added backlog item {entry.describe()}",
        )

    if verb in {"done", "drop"}:
        reference = item or text
        if not reference:
            return ToolResult.failure(
                "Which one?", f"'{verb}' needs an item reference (a number or some text)."
            )
        entry = store.complete(reference) if verb == "done" else store.drop(reference)
        if entry is None:
            return ToolResult.failure(
                "I don't have that one.",
                f"No backlog item matched '{reference}'.\n{store.listing()}",
            )
        store.prune()
        remaining = len(store.pending())
        tail = "Nothing left." if not remaining else f"{remaining} to go."
        return ToolResult.success(
            f"Cleared {entry.text}. {tail}", f"Removed backlog item {entry.describe()}"
        )

    if verb == "clear":
        pending = len(store.pending())
        if not pending:
            return ToolResult.success("Already empty.", "The backlog was empty.")
        # Wiping the list is data loss, so it goes through the same spoken
        # confirmation as a delete. `confirmed` only ever arrives from the
        # core loop, never from the model.
        if config.BACKLOG_CONFIRM_CLEAR and not confirmed:
            return ToolResult.confirm(
                f"That wipes all {pending} backlog items. Sure?",
                f"Awaiting confirmation to clear the backlog.\n{store.listing()}",
                action="clear",
            )
        removed = store.clear()
        return ToolResult.success(
            "Backlog's clear.", f"Cleared {removed} backlog items."
        )

    if verb == "run":
        reference = item or text
        if not reference:
            return ToolResult.failure(
                "Which one?", f"'run' needs an item reference.\n{store.listing()}"
            )
        entry = store.get(reference)
        if entry is None:
            return ToolResult.failure(
                "I don't have that one.",
                f"No backlog item matched '{reference}'.\n{store.listing()}",
            )
        if not entry.runnable:
            return ToolResult.failure(
                f"That one's a note, not a command: {entry.text}",
                f"Backlog item {entry.id} has no stored tool call to replay.",
            )

        # Imported here, not at module scope: `tools/__init__` imports this
        # module, so a top-level import would be circular.
        from tools import dispatch

        # No `confirmed` flag. Whatever gate held this command last session
        # holds it again now.
        log.info("Replaying backlog item %s: %s %s", entry.id, entry.tool, entry.args)
        result = dispatch(entry.tool, dict(entry.args))
        if result.ok:
            store.complete(entry.id)
            store.prune()
        return result

    return ToolResult.failure(
        "I don't know that backlog operation.",
        f"Unknown action '{action}'. Valid: list, add, done, drop, clear, run.",
    )
