"""The machine itself: how it is doing, what is running, and servers Leo started.

Read through `/proc` with the standard library. `psutil` would be the obvious
choice and is not worth a dependency for five numbers, so off Linux the read
actions say they are unavailable rather than guess.

`start` is deliberately not `terminal_command(background=True)`, which detaches
and forgets: "stop the dev server" and "why did it crash" both need somebody to
have written down what was started. The registry is a file because this process
gets killed and the servers do not, so the next run must still be able to find
them. `stop` touches registry entries and nothing else - "stop nginx" is a job
for `terminal_command`, behind its own gates - and a pid is checked against its
start time first, since after a reboot that number can belong to anything.

Nothing read from a process or a log goes in `speech`: a process name is
whatever its author called it, and a log line is whatever a request put there.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import config
from leo.memory import read_json, write_json
from tools.base import IS_WINDOWS, ToolResult, resolve_directory
from tools.safety import classify

log = logging.getLogger("leo.tools.system_control")

_IS_LINUX = sys.platform.startswith("linux")
_NAME = re.compile(r"[^a-z0-9._-]+")
_LISTEN = "0A"
_lock = threading.Lock()


# -- the registry ------------------------------------------------------------
def _registry_path() -> Path:
    return config.STATE_DIR / "processes.json"


def _logs_dir() -> Path:
    return config.STATE_DIR / "logs"


def _load() -> dict[str, dict[str, Any]]:
    raw = read_json(_registry_path(), {"processes": {}})
    rows = raw.get("processes") if isinstance(raw, dict) else None
    return {k: v for k, v in rows.items() if isinstance(v, dict)} if isinstance(rows, dict) else {}


def _save(rows: dict[str, dict[str, Any]]) -> None:
    write_json(_registry_path(), {"processes": rows})


def start_time(pid: int) -> str:
    """Field 22 of /proc/<pid>/stat: what makes a pid mean one particular process."""
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # The name is in brackets and may contain spaces, so count from after it.
    return stat.rpartition(")")[2].split()[19]


def is_alive(row: dict[str, Any]) -> bool:
    pid = int(row.get("pid") or 0)
    if pid <= 0 or start_time(pid) != str(row.get("started_at", "")):
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return state.rpartition(")")[2].split()[0] != "Z"


def register(name: str, pid: int, command: str, cwd: str, log_path: str, kind: str = "server") -> None:
    with _lock:
        rows = _load()
        rows[name] = {
            "pid": pid, "started_at": start_time(pid), "command": command, "cwd": cwd,
            "log": log_path, "kind": kind, "started": time.time(),
        }
        _save(rows)


def forget(name: str) -> None:
    with _lock:
        rows = _load()
        if rows.pop(name, None) is not None:
            _save(rows)


def entries(kind: str = "") -> dict[str, dict[str, Any]]:
    with _lock:
        rows = _load()
    return {k: v for k, v in rows.items() if not kind or v.get("kind") == kind}


def stop_group(row: dict[str, Any], wait_s: float = 2.0) -> bool:
    """TERM the whole process group, then KILL what ignored it."""
    if not is_alive(row):
        return False
    pid = int(row["pid"])
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        _reap(pid)
        if not is_alive(row):
            return True
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    _reap(pid)
    return True


def _reap(pid: int) -> None:
    """Collect a child of ours, so it does not linger as a zombie."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


# -- reading the machine -----------------------------------------------------
def _cpu_times() -> tuple[int, int]:
    fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
    values = [int(value) for value in fields]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return idle, sum(values)


def _cpu_percent(sample_s: float = 0.2) -> int:
    idle_a, total_a = _cpu_times()
    time.sleep(sample_s)
    idle_b, total_b = _cpu_times()
    total = total_b - total_a
    return round(100 * (total - (idle_b - idle_a)) / total) if total > 0 else 0


def _memory() -> tuple[int, int]:
    """(percent used, total GiB)."""
    info: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, _, rest = line.partition(":")
        info[key] = int(rest.split()[0]) if rest.split() else 0
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    used = round(100 * (total - available) / total) if total else 0
    return used, round(total / 1048576)


def _status() -> ToolResult:
    cpu = _cpu_percent()
    used, total = _memory()
    disk = shutil.disk_usage(Path.home())
    disk_used = round(100 * disk.used / disk.total) if disk.total else 0
    free_gb = round(disk.free / 1073741824)
    load = os.getloadavg()[0]
    uptime_s = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    return ToolResult.success(
        f"CPU {cpu} percent, memory {used}, disk {disk_used}, with {free_gb} gigabytes free.",
        f"cpu={cpu}% load1={load:.2f} memory={used}% of {total}GiB "
        f"disk={disk_used}% free={free_gb}GiB uptime={uptime_s / 3600:.1f}h",
    )


def _read_processes() -> list[tuple[int, int, str]]:
    """(rss in MiB, pid, name) for every process we are allowed to read."""
    page = os.sysconf("SC_PAGE_SIZE")
    found: list[tuple[int, int, str]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            resident = int((entry / "statm").read_text(encoding="utf-8").split()[1])
            name = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except (OSError, IndexError, ValueError):
            continue
        found.append((resident * page // 1048576, int(entry.name), name))
    return found


def _processes(query: str) -> ToolResult:
    rows = sorted(_read_processes(), reverse=True)
    wanted = query.strip().lower()
    if wanted:
        rows = [row for row in rows if wanted in row[2].lower()]
        if not rows:
            return ToolResult.success(f"Nothing called {query} is running.", f"No process matches {query!r}.")
    shown = rows[:12]
    listing = "\n".join(f"{mib:>6} MiB  pid {pid:<7} {name}" for mib, pid, name in shown)
    speech = f"{len(rows)} running." if wanted else f"{len(rows)} processes running."
    return ToolResult.success(speech, f"{len(rows)} processes by memory:\n{listing}")


def _socket_owners() -> dict[str, tuple[int, str]]:
    """Socket inode -> (pid, name), for the processes we may look inside."""
    owners: dict[str, tuple[int, str]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fds = list((entry / "fd").iterdir())
            name = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:["):
                owners[target[8:-1]] = (int(entry.name), name)
    return owners


def _ports(query: str) -> ToolResult:
    owners = _socket_owners()
    seen: dict[int, tuple[int, str]] = {}
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10 or parts[3] != _LISTEN:
                continue
            port = int(parts[1].rpartition(":")[2], 16)
            seen.setdefault(port, owners.get(parts[9], (0, "")))
    wanted = query.strip()
    if wanted.isdigit():
        port = int(wanted)
        if port not in seen:
            return ToolResult.success(f"Nothing is listening on {port}.", f"Port {port} is free.")
        pid, name = seen[port]
        return ToolResult.success(f"Something is listening on {port}.", f"Port {port}: {name or 'unknown'} pid {pid}")
    listing = "\n".join(
        f"{port:>6}  {seen[port][1] or 'not ours to see'}  pid {seen[port][0]}" for port in sorted(seen)
    )
    return ToolResult.success(f"{len(seen)} ports are listening.", f"Listening TCP ports:\n{listing}")


# -- servers -----------------------------------------------------------------
def _safe_name(name: str, command: str) -> str:
    raw = name or (command.split()[0].rsplit("/", 1)[-1] if command.split() else "")
    return _NAME.sub("-", raw.strip().lower()).strip("-")[:40]


def _start(name: str, command: str, directory: str, confirmed: bool) -> ToolResult:
    text = command.strip()
    if not text:
        return ToolResult.failure("Start what?", "start needs a command.")
    if not config.ALLOW_SHELL:
        return ToolResult.failure(
            "Shell access is switched off.",
            "LEO_ALLOW_SHELL is false; refuse and tell the user to enable it.",
        )
    verdict = classify(text)
    if verdict.is_blocked:
        return ToolResult.failure(
            "Not running that one.", f"Refused ({verdict.reason}). Do not retry."
        )
    label = _safe_name(name, text)
    if not label:
        return ToolResult.failure("Start what?", "That command has no usable name.")
    if verdict.needs_confirmation and not confirmed:
        return ToolResult.confirm(
            f"Start that? It {verdict.reason}. Confirm?",
            f"Waiting on a yes before starting: {text}",
            action="start", name=label, command=text, directory=directory, reason=verdict.reason,
        )
    known = entries().get(label)
    if known and is_alive(known):
        return ToolResult.failure(
            f"{label} is already running.", f"{label} is pid {known['pid']}. Stop it first."
        )
    cwd = resolve_directory(directory, config.DEFAULT_PROJECT_DIR)
    logs = _logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{label}.log"
    bash = shutil.which("bash")
    if bash is None:
        return ToolResult.failure("There's no shell to start it in.", "bash is not on PATH.")
    try:
        with log_path.open("ab") as sink:
            process = subprocess.Popen(
                [bash, "-lc", text], cwd=str(cwd) if cwd else None, stdin=subprocess.DEVNULL,
                stdout=sink, stderr=subprocess.STDOUT, start_new_session=True,
            )
    except OSError as exc:
        return ToolResult.failure(f"Couldn't start {label}.", f"Failed to start {text}: {exc}")
    register(label, process.pid, text, str(cwd or ""), str(log_path))
    # A server that dies at once should be reported now, not found out later.
    time.sleep(config.SYSTEM_START_SETTLE_S)
    if process.poll() is not None:
        forget(label)
        return ToolResult.failure(
            f"{label} died straight away.",
            f"Exit {process.returncode}. Last output:\n{_tail(log_path, 20)}",
        )
    return ToolResult.success(f"{label} is up.", f"Started {text} as pid {process.pid}. Log: {log_path}")


def _stop(name: str) -> ToolResult:
    label = _safe_name(name, "")
    row = entries().get(label)
    if row is None:
        return ToolResult.failure(
            f"I didn't start anything called {name}.",
            f"Only processes started by system_control can be stopped here. Known: "
            f"{', '.join(entries()) or 'none'}.",
        )
    stopped = stop_group(row)
    forget(label)
    if not stopped:
        return ToolResult.success(f"{label} had already stopped.", f"{label} was not running.")
    return ToolResult.success(f"{label} is stopped.", f"Stopped {label}, pid {row.get('pid')}.")


def _tail(path: Path, lines: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 64 * 1024))
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-max(1, lines):])


def _logs(name: str) -> ToolResult:
    label = _safe_name(name, "")
    row = entries().get(label)
    if row is None:
        return ToolResult.failure(f"I have no log for {name}.", f"Known: {', '.join(entries()) or 'none'}.")
    text = _tail(Path(str(row.get("log", ""))), config.SYSTEM_LOG_LINES)
    state = "running" if is_alive(row) else "stopped"
    return ToolResult.success(f"Got the log for {label}. It's {state}.", f"{label} ({state}):\n{text}")


def _list() -> ToolResult:
    rows = entries()
    if not rows:
        return ToolResult.success("I haven't started anything.", "Nothing in the registry.")
    listing = "\n".join(
        f"{name}: {'running' if is_alive(row) else 'stopped'}, {row.get('kind')}, pid {row.get('pid')}, {row.get('command')}"
        for name, row in rows.items()
    )
    running = sum(1 for row in rows.values() if is_alive(row))
    return ToolResult.success(f"{running} of {len(rows)} still running.", listing)


def system_control(
    action: str = "",
    name: str = "",
    command: str = "",
    directory: str = "",
    confirmed: bool = False,
    **_: object,
) -> ToolResult:
    verb = (action or "status").strip().lower()
    if IS_WINDOWS or not _IS_LINUX:
        return ToolResult.failure(
            "System control only works on Linux so far.",
            f"{verb} reads /proc and uses POSIX process groups. Use terminal_command here.",
        )
    if verb == "status":
        return _status()
    if verb == "processes":
        return _processes(name)
    if verb == "ports":
        return _ports(name)
    if verb == "list":
        return _list()
    if verb == "logs":
        return _logs(name)
    if verb == "start":
        return _start(name, command, directory, confirmed)
    if verb == "stop":
        return _stop(name)
    return ToolResult.failure("I can't do that to the system.", f"Unknown action {verb!r}.")
