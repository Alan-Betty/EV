"""`open_app` - launch desktop applications."""

from __future__ import annotations

import logging
import random
import shlex
import subprocess
from difflib import get_close_matches

import config
from tools.base import IS_WINDOWS, ToolResult, popen_detached, resolve_executable

log = logging.getLogger("ev.tools.app")

# Windows "shell:AppsFolder" / URI targets that have no PATH executable.
_URI_PREFIXES = ("ms-settings:", "ms-clock:", "shell:", "http://", "https://")


def _candidates(app: str) -> list[str]:
    """Map a spoken app name onto launch candidates, tolerating misheard words."""
    key = " ".join(app.lower().split()).rstrip(".")
    if key in config.APP_ALIASES:
        return config.APP_ALIASES[key]

    near = get_close_matches(key, config.APP_ALIASES.keys(), n=1, cutoff=0.75)
    if near:
        return config.APP_ALIASES[near[0]]

    # Unknown app: try the name as typed. Windows `start` resolves registered
    # App Paths entries that are not on PATH, which covers most installers.
    return [key.replace(" ", "")]


def _launched(name: str) -> str:
    """A short, varied confirmation. "Opening chrome." every time reads like a
    status log rather than someone talking."""
    return random.choice(
        (f"{name}'s up.", f"{name}, up.", f"Got it, {name}'s open.", f"There's {name}.")
    )


def open_app(app: str = "", arguments: str = "", **_: object) -> ToolResult:
    if not app.strip():
        return ToolResult.failure("You didn't say which app.")

    extra: list[str] = []
    if arguments and arguments.strip():
        try:
            extra = shlex.split(arguments, posix=not IS_WINDOWS)
        except ValueError:
            extra = [arguments.strip()]

    pretty = app.strip().title()
    for candidate in _candidates(app):
        if candidate.startswith(_URI_PREFIXES):
            try:
                popen_detached(["cmd", "/c", "start", "", candidate], shell=False)
                return ToolResult.success(_launched(pretty), f"Launched URI {candidate}")
            except OSError as exc:
                log.debug("URI launch failed for %s: %s", candidate, exc)
                continue

        exe = resolve_executable(candidate)
        if exe:
            try:
                popen_detached([exe, *extra])
                return ToolResult.success(
                    _launched(pretty), f"Launched {exe} {' '.join(extra)}".strip()
                )
            except OSError as exc:
                log.debug("Direct launch failed for %s: %s", exe, exc)

        if IS_WINDOWS:
            # Fall back to the shell's own resolver, which knows about
            # registered App Paths and Store apps that PATH does not cover.
            try:
                result = subprocess.run(
                    ["cmd", "/c", "start", "", candidate, *extra],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    return ToolResult.success(
                        _launched(pretty), f"Launched {candidate} via shell"
                    )
                log.debug("shell start failed for %s: %s", candidate, result.stderr.strip())
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.debug("shell start errored for %s: %s", candidate, exc)

    return ToolResult.failure(
        f"Can't find {pretty} on this machine.",
        f"No executable resolved for '{app}'. Add it to APP_ALIASES in config.py.",
    )
