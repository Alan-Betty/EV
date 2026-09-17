"""Shared plumbing for tool implementations."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("ev.tools")

IS_WINDOWS = sys.platform == "win32"


class CancelToken:
    """Cooperative "drop what you are doing" signal for a running tool.

    Tools run on a worker thread, and a thread cannot be killed from outside -
    so stopping one has to be something the tool agrees to. Every cancellable
    tool checks this at a point where stopping is *safe*: between two files,
    or between two polls of a subprocess. Never mid-write.

    That restraint is the whole design. A voice assistant hearing "stop" must
    not leave a half-copied file behind, and it must not claim to have stopped
    something it could not. Tools that cannot honour it say so, and E.V.
    repeats that to the user rather than pretending.

    Backed by `threading.Event` because the setter is the event loop and the
    reader is a worker thread.
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def __bool__(self) -> bool:
        """True while the tool should keep going."""
        return not self._event.is_set()

    def wait(self, timeout: float) -> bool:
        """Sleep up to `timeout`, waking early if cancelled."""
        return self._event.wait(timeout)


def was_cancelled(token: "CancelToken | None") -> bool:
    """True when a token exists and has been tripped."""
    return token is not None and token.cancelled


@dataclass
class ToolResult:
    """Outcome of a tool invocation.

    `speech` is what E.V. says out loud. `detail` is the fuller text that goes
    back to the model on the next turn so it knows what actually happened.
    """

    ok: bool
    speech: str
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    needs_confirmation: bool = False

    @classmethod
    def success(cls, speech: str, detail: str = "", **data: Any) -> "ToolResult":
        return cls(True, speech, detail or speech, data)

    @classmethod
    def failure(cls, speech: str, detail: str = "", **data: Any) -> "ToolResult":
        return cls(False, speech, detail or speech, data)

    @classmethod
    def confirm(cls, speech: str, detail: str = "", **data: Any) -> "ToolResult":
        return cls(False, speech, detail or speech, data, needs_confirmation=True)

    @classmethod
    def stopped(cls, speech: str, detail: str = "", **data: Any) -> "ToolResult":
        """A tool that was cancelled part-way through.

        Counts as a success, because the part that ran really did run: the
        files that moved stayed moved. `cancelled` in the data is what tells
        the core loop to put the remainder on the backlog instead of treating
        the job as finished.
        """
        return cls(True, speech, detail or speech, {**data, "cancelled": True})

    @property
    def cancelled(self) -> bool:
        return bool(self.data.get("cancelled"))


def popen_detached(args: list[str] | str, cwd: str | None = None, shell: bool = False) -> subprocess.Popen:
    """Start a process that outlives E.V. and does not steal its console.

    Detaching matters here: E.V. is a long-running loop and must never end up
    waiting on a GUI app or blocking on an inherited stdout pipe.
    """
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "shell": shell,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


def _app_paths_lookup(name: str) -> str | None:
    """Resolve an executable through the Windows "App Paths" registry key.

    Most installed GUI programs - every browser among them - are never added
    to PATH. Windows registers them here instead, which is how `start chrome`
    finds Chrome. Without this, `resolve_executable("chrome")` returns None on
    a machine that plainly has Chrome installed.
    """
    if not IS_WINDOWS:
        return None

    import winreg

    exe = name if name.lower().endswith(".exe") else name + ".exe"
    subkey = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for access in (winreg.KEY_READ, winreg.KEY_READ | winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(root, subkey, 0, access) as key:
                    path = winreg.QueryValueEx(key, "")[0].strip('"')
            except (FileNotFoundError, OSError):
                continue
            if path and os.path.isfile(path):
                return path
    return None


def resolve_executable(name: str) -> str | None:
    """Find an executable on PATH, then in the Windows App Paths registry."""
    found = shutil.which(name)
    if found:
        return found
    if IS_WINDOWS:
        if not name.lower().endswith(".exe"):
            found = shutil.which(name + ".exe")
            if found:
                return found
        return _app_paths_lookup(name)
    return None


def resolve_directory(raw: str | None, default: str) -> Path | None:
    """Turn whatever the model produced into a real directory, or None.

    Accepts a full path, `~`-relative paths, environment variables, or a bare
    project name which is searched for one level deep under the default root
    and the usual code folders. Never invents a path that does not exist.
    """
    if not raw or not raw.strip():
        return Path(default).expanduser()

    text = os.path.expandvars(raw.strip().strip('"'))

    # Only treat the input as a path when it actually looks like one. A bare
    # word such as "EV" must not be resolved against the current working
    # directory, or it silently matches a subfolder of wherever E.V. was
    # started from instead of the user's project.
    looks_like_path = (
        "/" in text
        or "\\" in text
        or text.startswith(("~", "."))
        or (len(text) > 1 and text[1] == ":")
    )
    if looks_like_path:
        candidate = Path(text).expanduser()
        if candidate.is_dir():
            return candidate.resolve()

    # A bare name like "my-api": look for it in the likely roots.
    name = text.replace(" ", "-").lower()
    home = Path.home()
    roots = [
        Path(default).expanduser(),
        home / "Documents" / "GitHub",
        home / "OneDrive" / "Documents" / "GitHub",
        home / "source" / "repos",
        home / "projects",
        home / "dev",
        home / "code",
        home,
    ]
    seen: set[Path] = set()
    for root in roots:
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        try:
            for child in root.iterdir():
                if child.is_dir() and child.name.lower() == name:
                    return child.resolve()
        except (PermissionError, OSError):
            continue
    return None
