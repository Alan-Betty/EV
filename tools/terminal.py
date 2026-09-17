"""`terminal_command` - run shell commands, gated by the safety classifier."""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time

import config
from tools.base import (
    IS_WINDOWS,
    CancelToken,
    ToolResult,
    resolve_directory,
    resolve_executable,
    was_cancelled,
)
from tools.safety import Risk, classify

log = logging.getLogger("ev.tools.terminal")


def _shell_argv(command: str, shell: str) -> list[str] | None:
    if shell == "cmd":
        exe = resolve_executable("cmd")
        return [exe, "/c", command] if exe else None
    if shell == "bash":
        exe = resolve_executable("bash")
        return [exe, "-lc", command] if exe else None

    # Default: PowerShell. Prefer pwsh 7 when present, fall back to 5.1.
    exe = resolve_executable("pwsh") or resolve_executable("powershell")
    if not exe:
        return None
    # -NoLogo roughly halves PowerShell 5.1's startup cost, which dominates
    # the latency of a short command.
    return [exe, "-NoProfile", "-NonInteractive", "-NoLogo", "-Command", command]


# Process bookkeeping nobody wants read aloud: PIDs, handles, job tables.
_KILL_OK = re.compile(
    r'SUCCESS:\s*(?:Sent termination signal to the process|The process)\s*"?([^"]+?)"?\s*with PID\s*\d+',
    re.IGNORECASE,
)
_PID_NOISE = re.compile(r"\s*\bwith PID\s+\d+\.?", re.IGNORECASE)
_JOB_TABLE = re.compile(r"^\s*(Id|PID|ProcessId)\s+\w+", re.IGNORECASE)
_SEPARATOR = re.compile(r"^[\s\-=_|+]+$")


def _speakable_lines(output: str) -> list[str]:
    """Drop table headers, rule lines and PID bookkeeping."""
    kept: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or _SEPARATOR.match(line) or _JOB_TABLE.match(line):
            continue
        kept.append(_PID_NOISE.sub("", line).strip())
    return kept


def _summarise(command: str, returncode: int, output: str) -> str:
    """Turn command output into something worth saying out loud.

    Raw console output is written for a screen, not an ear. Reading it back
    verbatim means E.V. announcing process IDs and table headers, so the
    common shapes get rewritten into a plain sentence instead.
    """
    if returncode != 0:
        first_error = next(
            (line.strip() for line in output.splitlines() if line.strip()),
            "no output",
        )
        return f"That failed. {_PID_NOISE.sub('', first_error)[:120]}"

    killed = _KILL_OK.search(output)
    if killed:
        name = killed.group(1).strip().rsplit(".exe", 1)[0]
        count = len(_KILL_OK.findall(output))
        if count > 1:
            return f"Closed {name}, {count} instances."
        return f"Closed {name}."

    lines = _speakable_lines(output)
    if not lines:
        return "Done."
    if len(lines) == 1:
        return lines[0][:160]
    return f"Done. {lines[0][:110]}"


def terminal_command(
    command: str = "",
    shell: str = "powershell",
    working_directory: str = "",
    background: bool = False,
    confirmed: bool = False,
    cancel: CancelToken | None = None,
    **_: object,
) -> ToolResult:
    command = (command or "").strip()
    if not command:
        return ToolResult.failure("No command to run.")

    if not config.ALLOW_SHELL:
        return ToolResult.failure(
            "Shell access is switched off.",
            "EV_ALLOW_SHELL is false; refuse and tell the user to enable it.",
        )

    verdict = classify(command)
    if verdict.is_blocked:
        log.warning("Blocked command (%s): %s", verdict.reason, command)
        return ToolResult.failure(
            "Not a chance. That one's destructive.",
            f"Blocked '{command}' - {verdict.reason}. Never retry this command.",
        )

    needs_ok = config.SHELL_CONFIRM_ALL or (
        config.SHELL_CONFIRM_DESTRUCTIVE and verdict.risk is Risk.REVIEW
    )
    if needs_ok and not confirmed:
        return ToolResult.confirm(
            f"That {verdict.reason}. Confirm?",
            f"Awaiting confirmation for '{command}' ({verdict.reason}).",
            command=command,
            shell=shell,
            working_directory=working_directory,
            background=background,
            reason=verdict.reason,
        )

    cwd = resolve_directory(working_directory, config.DEFAULT_PROJECT_DIR)
    cwd_str = str(cwd) if cwd else None

    argv = _shell_argv(command, (shell or "powershell").lower())
    if argv is None:
        return ToolResult.failure(
            f"No {shell} on this machine.", f"Shell '{shell}' could not be resolved."
        )

    if background:
        try:
            kwargs = {"cwd": cwd_str}
            if IS_WINDOWS:
                # Long-running commands get their own console so the user can
                # watch and kill them.
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen(argv, **kwargs)
        except OSError as exc:
            return ToolResult.failure("Couldn't start that.", f"Popen failed: {exc}")
        return ToolResult.success(
            "Running it in a separate window.", f"Started in background: {command}"
        )

    try:
        returncode, output, outcome = _run_interruptible(argv, cwd_str, cancel)
    except OSError as exc:
        return ToolResult.failure("Couldn't run that.", f"OS error: {exc}")

    trimmed = output.strip()[: config.SHELL_OUTPUT_CHARS]

    if outcome == "cancelled":
        return ToolResult.stopped(
            "Stopped it.",
            f"$ {command}\nKilled part-way through on the user's say-so.\n"
            f"{trimmed or '(no output)'}",
            returncode=returncode,
            output=trimmed,
        )
    if outcome == "timeout":
        return ToolResult.failure(
            f"That hung past {int(config.SHELL_TIMEOUT_S)} seconds.",
            f"Timed out: {command}",
        )

    speech = _summarise(command, returncode, output)
    detail = f"$ {command}\nexit={returncode}\n{trimmed or '(no output)'}"

    return ToolResult(
        ok=returncode == 0,
        speech=speech,
        detail=detail,
        data={"returncode": returncode, "output": trimmed},
    )


def _run_interruptible(
    argv: list[str], cwd: str | None, cancel: CancelToken | None
) -> tuple[int, str, str]:
    """Run to completion, but stay interruptible while it does.

    `subprocess.run` is the obvious way to write this and the reason "stop"
    used to do nothing: it blocks the worker thread until the command is
    finished, so there is no moment at which anything can act on a cancel.
    Polling instead gives one up to `SHELL_POLL_S`.

    Output is drained on reader threads rather than through `communicate()`,
    because a pipe that fills up deadlocks a process that nobody is reading
    from - and a command being killed for taking too long is exactly the kind
    that produces a lot of output.

    Returns `(returncode, output, outcome)` where outcome is "done",
    "cancelled" or "timeout".
    """
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    chunks: dict[str, str] = {}

    def _drain(name: str, stream) -> None:
        try:
            chunks[name] = stream.read() or ""
        except (OSError, ValueError):
            chunks[name] = ""

    readers = [
        threading.Thread(target=_drain, args=(name, stream), daemon=True)
        for name, stream in (("out", process.stdout), ("err", process.stderr))
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + config.SHELL_TIMEOUT_S
    outcome = "done"
    while process.poll() is None:
        if was_cancelled(cancel):
            outcome = "cancelled"
            break
        if time.monotonic() > deadline:
            outcome = "timeout"
            break
        # Woken early by a cancel, so "stop" lands in milliseconds rather than
        # waiting out the poll interval.
        if cancel is not None:
            cancel.wait(config.SHELL_POLL_S)
        else:
            time.sleep(config.SHELL_POLL_S)

    if outcome != "done":
        _terminate(process)

    for reader in readers:
        reader.join(timeout=2.0)
    output = chunks.get("out", "") + chunks.get("err", "")
    return (process.returncode if process.returncode is not None else -1), output, outcome


def _terminate(process: subprocess.Popen) -> None:
    """Ask the process to stop, then insist.

    On Windows `terminate()` is already a hard `TerminateProcess`, but on
    POSIX it is a catchable SIGTERM, so a shell that ignores it still needs
    the second step. Either way this must not hang the loop.
    """
    try:
        process.terminate()
    except OSError as exc:
        log.debug("terminate failed: %s", exc)
        return
    try:
        process.wait(timeout=config.SHELL_KILL_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        log.info("Process ignored terminate; killing it")
    try:
        process.kill()
        process.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not kill the process: %s", exc)
