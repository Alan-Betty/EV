"""E.V.'s tool registry.

Every capability the LLM can invoke lives here. `dispatch` is the single
entry point: it validates the tool name, filters the arguments down to what
the schema actually declares, and never lets an exception from a tool take
the assistant down.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from tools.app_launcher import open_app
from tools.backlog import backlog
from tools.base import CancelToken, ToolResult
from tools.browser import web_search
from tools.browser_automation import browser_task
from tools.computer_use import (
    keyboard_action,
    mouse_action,
    screen_task,
    take_screenshot,
)
from tools.dev_tools import dev_workflow
from tools.file_manager import file_manager
from tools.memory import remember
from tools.schemas import (
    TOOL_NAMES,
    TOOL_SPECS,
    normalise_arguments,
    to_gemini_tools,
    to_openai_tools,
)
from tools.terminal import terminal_command

log = logging.getLogger("ev.tools")


def chat(reply: str = "", **_: object) -> ToolResult:
    """Conversational fallback: say something, touch nothing.

    `detail` deliberately equals `speech`. It used to be f"Spoke: {text}",
    which the core loop stored as an assistant turn - so the model read its
    own past replies as being prefixed with a label, learned the pattern, and
    started emitting "Spoke:" in text that went straight to the speaker. Chat
    has no side effect to report, so there is nothing to add here.
    """
    text = (reply or "").strip()
    if not text:
        return ToolResult.success("I've got nothing on that one.")
    return ToolResult.success(text)


REGISTRY: dict[str, Callable[..., ToolResult]] = {
    "open_app": open_app,
    "web_search": web_search,
    "dev_workflow": dev_workflow,
    "terminal_command": terminal_command,
    "file_manager": file_manager,
    "take_screenshot": take_screenshot,
    "mouse_action": mouse_action,
    "keyboard_action": keyboard_action,
    "screen_task": screen_task,
    "browser_task": browser_task,
    "backlog": backlog,
    "remember": remember,
    "chat": chat,
}

# Arguments each tool accepts, derived from the schemas so the two can never
# drift apart. `confirmed` is injected by the core loop, not by the model.
_ALLOWED_ARGS: dict[str, set[str]] = {
    spec["name"]: set(spec["parameters"].get("properties", {}))
    for spec in TOOL_SPECS
}
# `confirmed` is injected by the core loop after a spoken yes, so it is
# never something the model can set for itself.
#
# Every tool that can spend money, send a message, delete something or drive
# the real mouse is on this list. The computer-use tools are all here: they
# have no sandbox and no undo, so the spoken yes is the only thing standing
# between a misheard word and a purchase.
for _gated in (
    "terminal_command",
    "file_manager",
    "backlog",
    "mouse_action",
    "keyboard_action",
    "screen_task",
    "browser_task",
):
    _ALLOWED_ARGS[_gated].add("confirmed")

# Tools that can be stopped part-way through, at a point where stopping is
# safe. Everything else runs to completion, and `ev_core` says so rather than
# pretending otherwise - a "stopped it" that did not stop anything is worse
# than an honest "can't".
#
# `cancel` is deliberately absent from `_ALLOWED_ARGS`: it is attached after
# the model's arguments have been filtered, so the model can neither set it
# nor clear it.
#
# The two autonomous loops are the clearest case for this. `screen_task`
# checks between steps and `browser_task` between page actions - both are
# points where the work is coherent and stopping leaves nothing half-written.
CANCELLABLE: frozenset[str] = frozenset(
    {"terminal_command", "file_manager", "screen_task", "browser_task"}
)


def dispatch(
    name: str,
    arguments: dict[str, Any] | None = None,
    cancel: "CancelToken | None" = None,
) -> ToolResult:
    """Run a tool by name. Always returns a ToolResult, never raises.

    `cancel` is a cooperative stop signal from the core loop. It is attached
    after argument filtering and type coercion, so it cannot be spoofed by the
    model and cannot be mangled into a string on the way through.
    """
    handler = REGISTRY.get(name)
    if handler is None:
        log.warning("Model asked for unknown tool %r", name)
        return ToolResult.failure(
            "I don't have a tool for that.",
            f"Unknown tool '{name}'. Valid tools: {', '.join(sorted(TOOL_NAMES))}.",
        )

    raw = arguments or {}
    if not isinstance(raw, dict):
        return ToolResult.failure(
            "Those arguments made no sense.", f"Expected an object, got {type(raw).__name__}."
        )

    # A near-miss name is renamed onto the real property before filtering,
    # so a call the model got almost right is not silently emptied. It is
    # still only ever a rename onto something the schema declares.
    raw = normalise_arguments(name, raw)

    allowed = _ALLOWED_ARGS.get(name, set())
    kwargs = {key: value for key, value in raw.items() if key in allowed}
    dropped = set(raw) - allowed
    if dropped:
        log.debug("Dropped unknown args for %s: %s", name, ", ".join(sorted(dropped)))

    # Coerce the loose types an LLM tends to emit into what the tools expect.
    for key, value in list(kwargs.items()):
        if isinstance(value, bool) or value is None:
            continue
        if key in {"start_claude", "background", "confirmed", "recursive", "headless"}:
            kwargs[key] = str(value).strip().lower() in {"true", "1", "yes"}
        elif not isinstance(value, str):
            kwargs[key] = str(value)
    kwargs = {key: ("" if value is None else value) for key, value in kwargs.items()}

    # After coercion, so the token arrives as an object rather than as the
    # string "<tools.base.CancelToken object at 0x...>".
    if cancel is not None and name in CANCELLABLE:
        kwargs["cancel"] = cancel

    try:
        return handler(**kwargs)
    except TypeError as exc:
        log.exception("Bad arguments for %s", name)
        return ToolResult.failure("Wrong arguments for that one.", f"TypeError: {exc}")
    except Exception as exc:  # a broken tool must not kill the assistant
        log.exception("Tool %s blew up", name)
        return ToolResult.failure(
            "That didn't work.", f"{type(exc).__name__} in {name}: {exc}"
        )


__all__ = [
    "CANCELLABLE",
    "CancelToken",
    "REGISTRY",
    "TOOL_SPECS",
    "TOOL_NAMES",
    "ToolResult",
    "dispatch",
    "to_gemini_tools",
    "to_openai_tools",
]
