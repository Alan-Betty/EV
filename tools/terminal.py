"""`terminal_command` - run shell commands, gated by the safety classifier."""

from __future__ import annotations

import logging
import subprocess

import config
from tools.base import (
    IS_WINDOWS,
    ToolResult,
    resolve_directory,
    resolve_executable,
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


def _summarise(command: str, returncode: int, output: str) -> str:
    """Turn command output into something worth saying out loud."""
    if returncode != 0:
        first_error = next(
            (line.strip() for line in output.splitlines() if line.strip()),
            "no output",
        )
        return f"That failed. {first_error[:120]}"

    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "Done, no output."
    if len(lines) == 1:
        return lines[0][:160]
    return f"Done. {len(lines)} lines back, first one: {lines[0][:120]}"


def terminal_command(
    command: str = "",
    shell: str = "powershell",
    working_directory: str = "",
    background: bool = False,
    confirmed: bool = False,
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
        result = subprocess.run(
            argv,
            cwd=cwd_str,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.SHELL_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return ToolResult.failure(
            f"That hung past {int(config.SHELL_TIMEOUT_S)} seconds.",
            f"Timed out: {command}",
        )
    except OSError as exc:
        return ToolResult.failure("Couldn't run that.", f"OS error: {exc}")

    output = (result.stdout or "") + (result.stderr or "")
    trimmed = output.strip()[: config.SHELL_OUTPUT_CHARS]
    speech = _summarise(command, result.returncode, output)
    detail = f"$ {command}\nexit={result.returncode}\n{trimmed or '(no output)'}"

    return ToolResult(
        ok=result.returncode == 0,
        speech=speech,
        detail=detail,
        data={"returncode": result.returncode, "output": trimmed},
    )
