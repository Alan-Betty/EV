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
from tools.base import ToolResult
from tools.browser import web_search
from tools.dev_tools import dev_workflow
from tools.file_manager import file_manager
from tools.schemas import TOOL_NAMES, TOOL_SPECS, to_gemini_tools, to_openai_tools
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
for _gated in ("terminal_command", "file_manager"):
    _ALLOWED_ARGS[_gated].add("confirmed")


def dispatch(name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
    """Run a tool by name. Always returns a ToolResult, never raises."""
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

    allowed = _ALLOWED_ARGS.get(name, set())
    kwargs = {key: value for key, value in raw.items() if key in allowed}
    dropped = set(raw) - allowed
    if dropped:
        log.debug("Dropped unknown args for %s: %s", name, ", ".join(sorted(dropped)))

    # Coerce the loose types an LLM tends to emit into what the tools expect.
    for key, value in list(kwargs.items()):
        if isinstance(value, bool) or value is None:
            continue
        if key in {"start_claude", "background", "confirmed", "recursive"}:
            kwargs[key] = str(value).strip().lower() in {"true", "1", "yes"}
        elif not isinstance(value, str):
            kwargs[key] = str(value)
    kwargs = {key: ("" if value is None else value) for key, value in kwargs.items()}

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
    "REGISTRY",
    "TOOL_SPECS",
    "TOOL_NAMES",
    "ToolResult",
    "dispatch",
    "to_gemini_tools",
    "to_openai_tools",
]
