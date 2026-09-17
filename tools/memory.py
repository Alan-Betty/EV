"""`remember` - store, recall and forget the small facts about the user.

The store itself lives in [ev/memory.py](../ev/memory.py) and is written
atomically, so what is remembered here survives a reboot or a power cut.

Everything set through this tool ends up in the system prompt on every turn,
which is why `ev.memory` caps how many entries it will hold: an assistant that
remembers four hundred things is an assistant with a four-hundred-line prompt.
Facts are short - "coffee is black", "editor is VS Code" - and the spoken
confirmation is shorter still.
"""

from __future__ import annotations

from ev.memory import get_memory
from tools.base import ToolResult


def remember(
    action: str = "",
    key: str = "",
    value: str = "",
    **_: object,
) -> ToolResult:
    """Single entry point for persistent user facts. Never raises."""
    store = get_memory()
    verb = (action or "").strip().lower() or ("set" if value else "get")
    verb = {
        "save": "set",
        "store": "set",
        "remember": "set",
        "update": "set",
        "recall": "get",
        "what": "get",
        "read": "get",
        "delete": "forget",
        "remove": "forget",
        "drop": "forget",
        "show": "list",
        "all": "list",
    }.get(verb, verb)

    if not store.enabled:
        return ToolResult.failure(
            "My memory's switched off.",
            "Memory disabled; set EV_MEMORY_ENABLED=true to use it.",
        )

    name = (key or "").strip()

    if verb == "set":
        if not name or not value.strip():
            return ToolResult.failure(
                "Remember what, exactly?",
                "'set' needs both a key and a value, e.g. key='coffee', value='black'.",
            )
        if not store.remember(name, value):
            return ToolResult.failure(
                "Couldn't hold on to that.", f"Failed to persist '{name}' to {store.path}."
            )
        return ToolResult.success(
            "Noted.", f"Remembered that {name.lower()} is {value.strip()}."
        )

    if verb == "get":
        if not name:
            return ToolResult.failure(
                "Recall what?", "'get' needs a key argument."
            )
        found = store.recall(name)
        if found is None:
            return ToolResult.failure(
                f"Nothing on {name}.", f"No stored fact for '{name}'."
            )
        return ToolResult.success(f"{name.capitalize()}: {found}.", f"{name} = {found}")

    if verb == "forget":
        if not name:
            return ToolResult.failure("Forget what?", "'forget' needs a key argument.")
        if not store.forget(name):
            return ToolResult.failure(
                f"Nothing on {name}.", f"No stored fact for '{name}'."
            )
        return ToolResult.success("Forgotten.", f"Dropped the stored fact '{name}'.")

    if verb == "list":
        facts = {**store.profile, **store.preferences}
        if not facts:
            return ToolResult.success(
                "I'm not holding anything yet.", "No stored facts."
            )
        listing = "\n".join(f"  {k}: {v}" for k, v in facts.items())
        return ToolResult.success(
            f"I've got {len(facts)} things on you.",
            f"Stored facts ({len(facts)}):\n{listing}",
        )

    return ToolResult.failure(
        "I don't know that memory operation.",
        f"Unknown action '{action}'. Valid: set, get, forget, list.",
    )
