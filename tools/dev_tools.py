"""`dev_workflow` - open VS Code on a project, spawn a terminal, start Claude Code.

Two strategies:

* ``vscode``  - drive VS Code's integrated terminal with synthetic keystrokes.
  Nicer to work in, but it only runs when we can prove the VS Code window took
  focus; otherwise the keystrokes would land in whatever else was on screen.
* ``terminal`` - launch Windows Terminal directly in the project directory with
  ``claude`` as its command. No keystroke injection at all, so it is the
  reliable fallback and what we degrade to automatically.
"""

from __future__ import annotations

import logging
import os
import time

import config
from tools.base import (
    IS_WINDOWS,
    ToolResult,
    popen_detached,
    resolve_directory,
    resolve_executable,
)
from tools.window import focus_by_title

log = logging.getLogger("ev.tools.dev")

_VSCODE_WINDOW_HINT = "Visual Studio Code"


def _type_into_focused(lines: list[str], settle: float = 0.4) -> bool:
    """Type lines plus Enter into the focused window via pyautogui."""
    try:
        import pyautogui
    except Exception as exc:  # ImportError, or no display / DISPLAY errors
        log.warning("pyautogui unavailable: %s", exc)
        return False

    pyautogui.FAILSAFE = False
    for line in lines:
        if line:
            # `write` sends real characters, which survives terminal apps that
            # swallow clipboard paste.
            pyautogui.write(line, interval=0.01)
        pyautogui.press("enter")
        time.sleep(settle)
    return True


def _open_vscode(directory: str) -> bool:
    exe = resolve_executable(config.VSCODE_CLI)
    if not exe:
        log.warning("VS Code CLI %r not on PATH", config.VSCODE_CLI)
        return False
    try:
        # `code` on Windows is a .cmd shim, so it needs the shell to resolve.
        popen_detached(f'"{exe}" "{directory}"', shell=True)
        return True
    except OSError as exc:
        log.warning("Failed to launch VS Code: %s", exc)
        return False


def _spawn_windows_terminal(directory: str, command: str | None) -> bool:
    """Open Windows Terminal (or PowerShell) in `directory`, optionally running a command."""
    wt = resolve_executable("wt")
    if wt:
        args = [wt, "-d", directory]
        if command:
            # `-NoExit` keeps the session alive after the command so the user
            # can keep working in it.
            args += ["powershell", "-NoExit", "-Command", command]
        try:
            popen_detached(args)
            return True
        except OSError as exc:
            log.debug("Windows Terminal launch failed: %s", exc)

    powershell = resolve_executable("powershell")
    if not powershell:
        return False
    args = [powershell, "-NoExit"]
    if command:
        args += ["-Command", f"Set-Location '{directory}'; {command}"]
    else:
        args += ["-Command", f"Set-Location '{directory}'"]
    try:
        # A console app needs its own window, which DETACHED_PROCESS denies it.
        import subprocess

        subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return True
    except OSError as exc:
        log.warning("PowerShell launch failed: %s", exc)
        return False


def dev_workflow(
    directory: str = "",
    prompt: str = "",
    start_claude: bool = True,
    **_: object,
) -> ToolResult:
    target = resolve_directory(directory, config.DEFAULT_PROJECT_DIR)
    if target is None:
        return ToolResult.failure(
            f"Can't find a project called {directory}.",
            f"No directory matched '{directory}'. Ask the user for the full path.",
        )

    path = str(target)
    name = target.name or path
    claude_available = resolve_executable(config.CLAUDE_CLI) is not None

    if start_claude and not claude_available:
        log.info("Claude CLI %r not on PATH", config.CLAUDE_CLI)

    vscode_opened = _open_vscode(path)
    if not vscode_opened:
        return ToolResult.failure(
            "VS Code isn't on the path.",
            f"'{config.VSCODE_CLI}' could not be resolved. Install the VS Code "
            "shell command or set EV_VSCODE_CLI.",
        )

    if not start_claude:
        return ToolResult.success(f"VS Code is up on {name}.", f"Opened VS Code at {path}")

    if not claude_available:
        return ToolResult.success(
            f"VS Code is up on {name}, but Claude isn't installed.",
            f"Opened VS Code at {path}. '{config.CLAUDE_CLI}' is not on PATH.",
        )

    # Give VS Code time to finish painting before we try to take focus.
    time.sleep(config.VSCODE_BOOT_S)

    used_integrated = False
    if IS_WINDOWS and focus_by_title(_VSCODE_WINDOW_HINT, timeout=config.VSCODE_BOOT_S):
        try:
            import pyautogui

            pyautogui.FAILSAFE = False
            # Ctrl+` toggles the integrated terminal; Ctrl+Shift+` forces a new
            # one so we never reuse a pane that already has something running.
            pyautogui.hotkey("ctrl", "shift", "`")
            time.sleep(config.TERMINAL_SPAWN_S)

            lines = [config.CLAUDE_CLI]
            if _type_into_focused(lines, settle=0.6):
                used_integrated = True
                if prompt and prompt.strip():
                    # Claude Code needs a moment to render its prompt before it
                    # will accept input.
                    time.sleep(3.0)
                    _type_into_focused([prompt.strip()], settle=0.3)
        except Exception as exc:
            log.warning("Integrated-terminal automation failed: %s", exc)

    if not used_integrated:
        log.info("Falling back to a standalone terminal for Claude")
        command = config.CLAUDE_CLI
        if prompt and prompt.strip():
            escaped = prompt.strip().replace("'", "''")
            command = f"{config.CLAUDE_CLI} '{escaped}'"
        if not _spawn_windows_terminal(path, command):
            return ToolResult.success(
                f"VS Code is up on {name}. You'll have to start Claude yourself.",
                f"Opened VS Code at {path} but no terminal could be spawned.",
            )

    where = "VS Code" if used_integrated else "a terminal"
    detail = (
        f"Opened VS Code at {path}, started {config.CLAUDE_CLI} in {where}"
        + (f", sent prompt: {prompt.strip()[:120]}" if prompt.strip() else "")
    )
    speech = f"VS Code and Claude are running on {name}."
    if prompt.strip():
        speech = f"Claude's running on {name} with your prompt."
    return ToolResult.success(speech, detail, directory=path)
