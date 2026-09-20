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
from tools import guard
from tools.file_manager import file_manager
from tools.guard import (
    SIDE_EFFECT_TOOLS,
    UNTRUSTED_OUTPUT,
    engage_lockdown,
    is_locked_down,
    redact,
    release_lockdown,
)
from tools.memory import manage_todo, recall_fact, remember, remember_fact
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
    "remember_fact": remember_fact,
    "recall_fact": recall_fact,
    "manage_todo": manage_todo,
    # Not in `TOOL_SPECS`, so it costs no tokens per turn, but still callable.
    # It predates the three named tools above and a model that half-recalls
    # the schema reaches for it; `dispatch` would otherwise answer a perfectly
    # sensible `remember` call with "I don't have a tool for that".
    "remember": remember,
    "chat": chat,
}

# Arguments each tool accepts, derived from the schemas so the two can never
# drift apart. `confirmed` is injected by the core loop, not by the model.
_ALLOWED_ARGS: dict[str, set[str]] = {
    spec["name"]: set(spec["parameters"].get("properties", {}))
    for spec in TOOL_SPECS
}
# `remember` has no spec of its own any more - see the registry note above -
# so its arguments are declared here instead. Written out rather than derived
# because there is nothing left to derive them from, and an empty set would
# mean every argument silently dropped and the tool running on nothing.
_ALLOWED_ARGS["remember"] = {"action", "key", "value"}
# `confirmed` is injected by the core loop after a spoken yes, so it is
# never something the model can set for itself.
#
# Every tool that can spend money, send a message, delete something or drive
# the real mouse is on this list. The computer-use tools are all here: they
# have no sandbox and no undo, so the spoken yes is the only thing standing
# between a misheard word and a purchase.
for _gated in (
    # `open_app` is gated for one case only - a binary Windows marked as
    # downloaded - but the argument still has to be declared here or the
    # spoken yes would be filtered out on its way back in.
    "open_app",
    "terminal_command",
    "file_manager",
    "backlog",
    # `manage_todo clear` wipes a list the user built by hand. Same gate as a
    # file delete, for the same reason: a misheard "clear my list" should ask.
    "manage_todo",
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

    # Lockdown still allows E.V. to look, but not to leave a file behind
    # doing it. Dropped rather than refused: the screenshot is the useful
    # half and the write is incidental to it.
    if name == "take_screenshot" and is_locked_down():
        kwargs.pop("save_as", None)

    # Before the tool, and therefore before the tool's own confirmation gate.
    # A locked-down E.V. that asked "Confirm?" and then refused the yes would
    # be worse than one that says no straight away.
    refused = guard.check(name, kwargs)
    if refused is not None:
        return refused

    # After coercion, so the token arrives as an object rather than as the
    # string "<tools.base.CancelToken object at 0x...>".
    if cancel is not None and name in CANCELLABLE:
        kwargs["cancel"] = cancel

    guard.note(name, kwargs)
    try:
        result = handler(**kwargs)
    except TypeError as exc:
        log.exception("Bad arguments for %s", name)
        result = ToolResult.failure(
            "Wrong arguments for that one.", f"TypeError: {exc}"
        )
    except Exception as exc:  # a broken tool must not kill the assistant
        log.exception("Tool %s blew up", name)
        result = ToolResult.failure(
            "That didn't work.", f"{type(exc).__name__} in {name}: {exc}"
        )

    # Two marks, both applied centrally rather than in fifteen tools.
    #
    # Redaction: a secret in a tool result is nearly always an accident - a
    # file that turned out to be a `.env`, a command that echoed a token -
    # and `detail` is replayed to the model on every subsequent turn, so one
    # leak becomes a leak in every request that follows it.
    #
    # `untrusted` marks the results that carry text written by someone who is
    # not the user. `ev.brain` fences those so the model reads them as data.
    result.speech = redact(result.speech)
    result.detail = redact(result.detail)
    if name in UNTRUSTED_OUTPUT:
        result.untrusted = True

    guard.record(name, kwargs, result)
    return result


__all__ = [
    "CANCELLABLE",
    "CancelToken",
    "SIDE_EFFECT_TOOLS",
    "UNTRUSTED_OUTPUT",
    "engage_lockdown",
    "is_locked_down",
    "redact",
    "release_lockdown",
    "REGISTRY",
    "TOOL_SPECS",
    "TOOL_NAMES",
    "ToolResult",
    "dispatch",
    "to_gemini_tools",
    "to_openai_tools",
]
