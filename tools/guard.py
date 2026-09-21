"""The last line between a wrong decision and a bad afternoon.

Everything else in `tools/` assumes the model is trying to help and only has
to be stopped from doing something *destructive*: `safety.classify` reads a
command, `file_manager._check` reads a path, and both answer a question about
one call in isolation. This module assumes less than that. It answers three
questions the per-call checks structurally cannot:

* **"Is E.V. allowed to act at all right now?"** - lockdown. "Stop" cancels
  the tool that is running and says nothing about the next one. When E.V. is
  looping, or has been told something by a web page that it should not have
  believed, cancelling one iteration is not what the user wants. Lockdown
  takes every side effect away at once and leaves talking, remembering-back
  and looking intact, so E.V. can explain itself while holding still.

* **"Is this the tenth one of these in ten seconds?"** - the runaway
  limiter. A loop is not a bug in any single call: each one is individually
  reasonable, which is exactly why no single-call check can see it. Only
  something counting across calls can.

* **"What did it actually do?"** - the audit log. Every other guard here is
  preventive, and a preventive guard that works leaves no trace. This is the
  one that can be read afterwards.

Redaction runs through all of it. Secrets reach the model by accident rather
than by attack - a file read that turned out to be a `.env`, a command that
echoed a token - and once one is in the history it rides in every subsequent
request and lands in this log too.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import config
from tools.base import ToolResult

log = logging.getLogger("ev.tools.guard")


# Tools that change something outside E.V.'s own head: they start a process,
# move a file, move the pointer, write to disk, or send something. These are
# what lockdown withholds and what the runaway limiter counts.
#
# `remember_fact`, `manage_todo` and `backlog` are on the list even though
# they only write small JSON files of E.V.'s own. A stored fact is read back
# into the system prompt on every later turn, so a poisoned one outlives the
# turn that wrote it - which makes it the most durable side effect here, not
# the mildest.
SIDE_EFFECT_TOOLS: frozenset[str] = frozenset(
    {
        "open_app",
        "web_search",
        "dev_workflow",
        "terminal_command",
        "file_manager",
        "mouse_action",
        "keyboard_action",
        "screen_task",
        "browser_task",
        # An autonomous mission is the largest side effect there is: it
        # drives the screen across applications for minutes at a time. It
        # counts once towards the limiter, because its own sub-tools are
        # called directly rather than through `dispatch` - and it checks
        # `is_locked_down` between rounds itself, so the phrase still stops
        # a run that is already under way.
        "agent_task",
        "backlog",
        "remember_fact",
        "manage_todo",
        "remember",
    }
)

# Tools whose `detail` carries text E.V. did not write and the user did not
# say: a web page, a file, whatever the screen happened to show. It is data
# about the world, and the model must read it as data - see
# `ev.brain.Brain.remember`.
UNTRUSTED_OUTPUT: frozenset[str] = frozenset(
    {
        "agent_task",
        "browser_task",
        "file_manager",
        "take_screenshot",
        "screen_task",
        "web_search",
    }
)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
# Deliberately about *shapes*, not about names. A pattern list keyed on the
# word "password" misses the token that was printed on its own, which is the
# usual way one escapes.
_SECRETS: tuple[tuple[re.Pattern[str], str], ...] = (
    # A named assignment, whatever the value looks like. First, because it
    # swallows the whole value: run it after the shape patterns and
    # `KEY=gsk_...` comes out as `KEY=[redacted groq key]` with the word
    # "key" still attached, which is noise pretending to be information.
    # Quotes are left off the replacement so the shape of the line survives
    # for the model to understand, while the value does not.
    (
        re.compile(
            r"(?i)\b([a-z0-9_]*(?:api[_-]?key|secret|token|password|passwd|pwd)"
            r"[a-z0-9_]*)\s*[=:]\s*[\"']?([^\s\"'\n]{6,})[\"']?"
        ),
        r"\1=[redacted]",
    ),
    (re.compile(r"\bgsk_[A-Za-z0-9]{20,}"), "[redacted groq key]"),
    (re.compile(r"\bsk-[A-Za-z0-9._\-]{20,}"), "[redacted api key]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "[redacted google key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[redacted github token]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "[redacted slack token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted aws key id]"),
    (
        re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE),
        "Bearer [redacted]",
    ),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[redacted private key]",
    ),
)


def redact(text: str) -> str:
    """Mask anything key-shaped. Never raises, never returns None."""
    if not text or not config.REDACT_SECRETS:
        return text or ""
    cleaned = text
    for pattern, replacement in _SECRETS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


# ---------------------------------------------------------------------------
# Lockdown
# ---------------------------------------------------------------------------
class _State:
    """Guard state, shared between the event loop and the tool threads.

    A `threading.Lock` rather than an asyncio one: lockdown is engaged from
    the loop (a spoken phrase) and read from a worker thread (a dispatch),
    so the two sides are genuinely different threads.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.locked = False
        self.reason = ""
        self.since = 0.0
        # (timestamp, signature) for every side-effecting call.
        self.recent: deque[tuple[float, str]] = deque(maxlen=512)


_state = _State()


def reset() -> None:
    """Forget every guard decision. For tests and for a fresh session."""
    with _state.lock:
        _state.locked = bool(config.LOCKDOWN_ON_START)
        _state.reason = "configured to start locked down" if _state.locked else ""
        _state.since = time.monotonic() if _state.locked else 0.0
        _state.recent.clear()


reset()


def engage_lockdown(reason: str = "asked for it") -> bool:
    """Withhold every side effect until told otherwise.

    Returns False when lockdown was already on, so a caller can tell "done"
    from "already was" and say the right thing out loud.
    """
    with _state.lock:
        if _state.locked:
            return False
        _state.locked = True
        _state.reason = reason
        _state.since = time.monotonic()
    log.warning("Lockdown engaged: %s", reason)
    audit("lockdown", reason=reason)
    return True


def release_lockdown() -> bool:
    """Hand the tools back. Returns False when nothing was locked."""
    with _state.lock:
        if not _state.locked:
            return False
        _state.locked = False
        held = _state.reason
        _state.reason = ""
        _state.since = 0.0
        # The count that tripped it dies with it, or the first call after a
        # release walks straight back into the limiter it just escaped.
        _state.recent.clear()
    log.warning("Lockdown released (was: %s)", held)
    audit("lockdown_released", reason=held)
    return True


def is_locked_down() -> bool:
    with _state.lock:
        return _state.locked


def lockdown_reason() -> str:
    with _state.lock:
        return _state.reason


# ---------------------------------------------------------------------------
# The audit log
# ---------------------------------------------------------------------------
def audit_path() -> Path:
    """Where the log lives, resolved per call.

    Deliberately not cached: `STATE_DIR` is what a test - or a second
    instance - relocates to move all persisted state at once, and a path
    captured at import would ignore it.
    """
    return Path(config.AUDIT_FILE or (config.STATE_DIR / "audit.jsonl"))


def audit(event: str, **fields: Any) -> None:
    """Append one JSON object to the log. Never raises.

    A failure here must never cost the user the action. An assistant that
    refuses to work because it cannot write its own diary is worse than one
    with a gap in the diary.
    """
    if not config.AUDIT_ENABLED:
        return
    try:
        path = audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        record = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event}
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(redact(line) + "\n")
    except Exception as exc:  # pragma: no cover - diary, not flight control
        log.debug("Could not write the audit log: %s", exc)


def _rotate(path: Path) -> None:
    """Keep one previous log. Size, not date: the interesting part is recent."""
    try:
        if path.exists() and path.stat().st_size > config.AUDIT_MAX_BYTES:
            os.replace(path, path.with_suffix(path.suffix + ".1"))
    except OSError as exc:
        log.debug("Could not rotate the audit log: %s", exc)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def _signature(name: str, arguments: dict[str, Any]) -> str:
    """A stable fingerprint for "the same call again".

    `confirmed` and `cancel` are left out. `confirmed` is the difference
    between the call that asked and the call that ran, which is a legitimate
    pair rather than a repeat, and `cancel` is an object the model never
    sees.
    """
    relevant = {
        key: str(value)[:120]
        for key, value in sorted(arguments.items())
        if key not in {"confirmed", "cancel"}
    }
    return f"{name}:{json.dumps(relevant, sort_keys=True, default=str)}"


def _trip(reason: str, speech: str, detail: str) -> ToolResult:
    """Refuse a call, and lock down if that is what the setting says."""
    if config.GUARD_TRIP_LOCKS_DOWN:
        engage_lockdown(reason)
    return ToolResult.failure(speech, detail)


def check(name: str, arguments: dict[str, Any]) -> ToolResult | None:
    """Decide whether this call may run at all. None means "carry on".

    Runs *before* the tool, and therefore before the tool's own confirmation
    gate. That order matters: a locked-down E.V. that still asked "Confirm?"
    and then refused the yes would be worse than useless, because the user
    would have said yes to something that was never going to happen.
    """
    if config.LOCKDOWN_ENABLED and is_locked_down() and name in SIDE_EFFECT_TOOLS:
        reason = lockdown_reason()
        audit("refused", tool=name, why="locked down", args=_loggable(arguments))
        return ToolResult.failure(
            "I'm locked down. Say 'unlock' and I'll pick it back up.",
            f"Refused '{name}': E.V. is in lockdown ({reason}). Nothing ran. "
            "Do not retry any tool - tell the user E.V. is locked down and "
            "that saying 'unlock' or 'stand down' releases it.",
        )

    if not config.GUARD_ENABLED or name not in SIDE_EFFECT_TOOLS:
        return None

    now = time.monotonic()
    signature = _signature(name, arguments)
    with _state.lock:
        window = now - config.GUARD_WINDOW_S
        while _state.recent and _state.recent[0][0] < window:
            _state.recent.popleft()
        recent = list(_state.recent)

    # Identical calls back to back. Twice is a retry and retries are how a
    # flaky thing eventually works; the third is a loop, and a loop has never
    # once been the thing the user asked for.
    repeats = 0
    for _, previous in reversed(recent):
        if previous != signature:
            break
        repeats += 1
    if repeats >= config.GUARD_MAX_REPEATS:
        log.warning("Runaway: %s repeated %d times with the same arguments", name, repeats)
        audit("refused", tool=name, why="repeat loop", repeats=repeats,
              args=_loggable(arguments))
        return _trip(
            f"repeated {name} {repeats} times in a row",
            "That's the third time round on the same thing. I've stopped.",
            f"Refused '{name}': the identical call has already run {repeats} "
            "times in a row, so it is looping rather than working. Do not "
            "call it again. Say what happened and ask the user how to "
            "proceed.",
        )

    if len(recent) >= config.GUARD_MAX_ACTIONS:
        log.warning(
            "Runaway: %d actions inside %.0fs", len(recent), config.GUARD_WINDOW_S
        )
        audit("refused", tool=name, why="burst limit", count=len(recent),
              args=_loggable(arguments))
        return _trip(
            f"{len(recent)} actions in {int(config.GUARD_WINDOW_S)} seconds",
            "That's a lot of actions very fast. I've stopped until you say otherwise.",
            f"Refused '{name}': {len(recent)} side-effecting actions in the last "
            f"{int(config.GUARD_WINDOW_S)} seconds is past the limit. E.V. is now "
            "locked down. Tell the user, and do not call another tool.",
        )

    return None


def note(name: str, arguments: dict[str, Any]) -> None:
    """Record that a side-effecting call is going ahead."""
    if name not in SIDE_EFFECT_TOOLS:
        return
    with _state.lock:
        _state.recent.append((time.monotonic(), _signature(name, arguments)))


def _loggable(arguments: dict[str, Any]) -> dict[str, str]:
    """Arguments as they should appear in the log: short, and redacted."""
    return {
        key: redact(str(value))[:200]
        for key, value in arguments.items()
        if key != "cancel"
    }


def record(name: str, arguments: dict[str, Any], result: ToolResult) -> None:
    """Write what happened. `chat` is skipped - it is talk, not action."""
    if name == "chat":
        return
    audit(
        "tool",
        tool=name,
        args=_loggable(arguments),
        ok=result.ok,
        confirm=result.needs_confirmation,
        cancelled=result.cancelled,
        speech=redact(result.speech)[:200],
    )
