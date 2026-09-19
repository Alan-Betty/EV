"""Long-term memory: the small facts about the user, and their to-do list.

The store itself lives in [ev/memory.py](../ev/memory.py) and is written
atomically, so what is kept here survives a reboot or a power cut.

Three tools, because the model picks better from three narrow names than from
one wide one. `remember_fact` and `recall_fact` are a key-value store;
`manage_todo` is a standing list of errands.

Everything stored here ends up in the system prompt on every turn, which is
why `ev.memory` caps both the facts and the open to-dos: an assistant that
remembers four hundred things is an assistant with a four-hundred-line
prompt. Facts are short - "coffee is black", "editor is VS Code" - and the
spoken confirmation is shorter still.

`remember` is the older single entry point, kept because it still works and
because a model that half-recalls the schema reaches for it. It is not in
`TOOL_SPECS` any more, so it costs nothing per turn; see `tools/__init__.py`.

Every function here returns a `ToolResult` and never raises. `speech` is what
gets said and carries no machine detail; `detail` is what the model reads next
turn.
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


# ---------------------------------------------------------------------------
# The three named tools the model is actually shown.
# ---------------------------------------------------------------------------


def _disabled() -> ToolResult:
    return ToolResult.failure(
        "My memory's switched off.",
        "Memory disabled; set EV_MEMORY_ENABLED=true to use it.",
    )


def remember_fact(key: str = "", value: str = "", **_: object) -> ToolResult:
    """Store one durable fact about the user. Never raises."""
    store = get_memory()
    if not store.enabled:
        return _disabled()

    name = (key or "").strip()
    fact = (value or "").strip()
    if not name or not fact:
        return ToolResult.failure(
            "Remember what, exactly?",
            "remember_fact needs both key and value, e.g. key='coffee', "
            "value='black'.",
        )
    if not store.remember(name, fact):
        return ToolResult.failure(
            "Couldn't hold on to that.",
            f"Failed to persist '{name}' to {store.path}.",
        )
    return ToolResult.success("Noted.", f"Remembered that {name.lower()} is {fact}.")


def recall_fact(key: str = "", **_: object) -> ToolResult:
    """Look one fact back up, or list everything when no key is given."""
    store = get_memory()
    if not store.enabled:
        return _disabled()

    name = (key or "").strip()
    if not name:
        # A bare "what do you know about me" is a listing, not a failure.
        facts = {**store.profile, **store.preferences}
        if not facts:
            return ToolResult.success("I'm not holding anything yet.", "No stored facts.")
        listing = "\n".join(f"  {k}: {v}" for k, v in facts.items())
        return ToolResult.success(
            f"I've got {len(facts)} things on you.",
            f"Stored facts ({len(facts)}):\n{listing}",
        )

    found = store.recall(name)
    if found is None:
        return ToolResult.failure(
            f"Nothing on {name}.",
            f"No stored fact for '{name}'. Use remember_fact to set one.",
        )
    return ToolResult.success(f"{name.capitalize()}: {found}.", f"{name} = {found}")


def manage_todo(
    action: str = "",
    item: str = "",
    confirmed: bool = False,
    **_: object,
) -> ToolResult:
    """The user's standing to-do list: add, list, complete, drop, clear.

    Separate from `backlog`, which is what E.V. itself failed to finish. This
    is what the user asked to be held on to, so "clear the backlog" must never
    reach it.
    """
    store = get_memory()
    if not store.enabled:
        return _disabled()

    verb = (action or "").strip().lower() or ("add" if item else "list")
    verb = {
        "create": "add",
        "new": "add",
        "remember": "add",
        "show": "list",
        "all": "list",
        "read": "list",
        "complete": "done",
        "finish": "done",
        "finished": "done",
        "completed": "done",
        "check": "done",
        "tick": "done",
        "remove": "drop",
        "delete": "drop",
        "cancel": "drop",
    }.get(verb, verb)

    target = (item or "").strip()

    if verb == "add":
        if not target:
            return ToolResult.failure(
                "Add what to the list?", "manage_todo 'add' needs an item."
            )
        if not store.add_todo(target):
            return ToolResult.failure(
                "Couldn't hold on to that.",
                f"Failed to persist a to-do to {store.path}.",
            )
        return ToolResult.success("On the list.", f"Added to the to-do list: {target}")

    if verb == "list":
        open_items = store.open_todos()
        if not open_items:
            return ToolResult.success("Your list is empty.", "No open to-do items.")
        listing = "\n".join(f"  {i}. {t}" for i, t in enumerate(open_items, 1))
        return ToolResult.success(
            store.todo_summary(),
            f"Open to-do items ({len(open_items)}):\n{listing}",
        )

    if verb == "done":
        if not target:
            return ToolResult.failure(
                "Which one's done?", "manage_todo 'done' needs an item."
            )
        text = store.complete_todo(target)
        if not text:
            return ToolResult.failure(
                f"Nothing on your list matching {target}.",
                f"No open to-do matched '{target}'.",
            )
        remaining = len(store.open_todos())
        return ToolResult.success(
            "Ticked off.", f"Completed '{text}'. {remaining} still open."
        )

    if verb == "drop":
        if not target:
            return ToolResult.failure(
                "Drop which one?", "manage_todo 'drop' needs an item."
            )
        text = store.drop_todo(target)
        if not text:
            return ToolResult.failure(
                f"Nothing on your list matching {target}.",
                f"No to-do matched '{target}'.",
            )
        return ToolResult.success("Dropped it.", f"Removed '{text}' from the to-do list.")

    if verb == "clear":
        # Gated like a delete: the core loop injects `confirmed` only after a
        # spoken yes, so a misheard "clear my list" asks before it wipes one.
        if not confirmed:
            count = len(store.todos)
            if not count:
                return ToolResult.success("Your list is already empty.", "Nothing to clear.")
            return ToolResult.confirm(
                f"Clear all {count} items off your list?",
                f"Waiting on a yes before clearing {count} to-do items.",
            )
        count = store.clear_todos()
        return ToolResult.success("List cleared.", f"Cleared {count} to-do items.")

    return ToolResult.failure(
        "I don't know that list operation.",
        f"Unknown action '{action}'. Valid: add, list, done, drop, clear.",
    )
