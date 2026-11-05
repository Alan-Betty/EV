"""Coding sessions L.E.O. starts and supervises, rather than only launches.

`dev_workflow` opens an editor and types into a terminal, then has no idea what
became of it. This runs `claude -p` on a worker thread in a git worktree of its
own, reads its event stream, and reports back when it stops.

The decisions that matter, each of which is a way this could go badly:

- **A worktree, so the user's checkout is never touched.** The result sits on a
  branch of its own for them to look at. It never pushes and never merges: that
  is denied by `--disallowedTools` and, because an allowed test runner can run
  anything, by per-process git config that aims every push at a dead URL. Neither
  stops arbitrary code run by the tests themselves from doing what it likes. That
  risk is accepted and stated rather than hidden.
- **Nobody is there to approve anything**, since `-p` has no prompt. So the tools
  are a narrow allow-list, permission modes that bypass checks are never used,
  and every run has a spending limit, a wall clock and a stall check.
- **The model does not grade itself.** "Done" means L.E.O. ran a test command that
  came from config or a fixed table, not one the model picked, and counted the
  files that changed. Claude's own account of how it went stays in `detail`.
- **What gets spoken is a template** of numbers: exit state, test result, file
  count. Claude's text has read whatever is in the repo, so it never reaches
  `speech`, which is replayed as the assistant's own words with no fence.
- **It answers to the kill switch by itself.** The worker never goes through
  `dispatch`, so it polls lockdown and kills its own process group, redacts what
  it keeps, and writes its own audit lines. A session left by a process that was
  killed has nobody watching it, so `recover` stops it at the next start.

Blocking work lives on the worker thread. Results reach the event loop only
through `set_notifier`, whose callback must be thread-safe.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import config
from leo.memory import read_json, write_json
from tools.base import IS_WINDOWS, ToolResult, resolve_directory
from tools.guard import audit, is_locked_down, redact
from tools.safety import classify

log = logging.getLogger("leo.tools.dev_session")

_PUSH_PREFIXES = ("https://", "http://", "ssh://", "git://", "git@", "file://", "/", ".")
_RUNNING = "running"
_notifier: Callable[[str], None] | None = None
_lock = threading.Lock()
_live: dict[str, "_Worker"] = {}


def _registry() -> Any:
    # `tools` exports a function of this name, which shadows the module.
    return importlib.import_module("tools.system_control")


@dataclass
class Session:
    id: str
    claude_id: str
    goal: str
    repo: str
    worktree: str
    branch: str
    base_commit: str = "HEAD"
    state: str = _RUNNING
    why: str = ""
    runs: int = 0
    events: int = 0
    cost_usd: float = 0.0
    last_text: str = ""
    tests: str = "not run"
    test_output: str = ""
    files_changed: int = 0
    diff: str = ""
    started: float = field(default_factory=time.time)
    finished: float = 0.0


def _path() -> Path:
    return config.STATE_DIR / "dev_sessions.json"


def _load_all() -> dict[str, dict[str, Any]]:
    raw = read_json(_path(), {"sessions": {}})
    rows = raw.get("sessions") if isinstance(raw, dict) else None
    return {k: v for k, v in rows.items() if isinstance(v, dict)} if isinstance(rows, dict) else {}


def _store(session: Session) -> None:
    with _lock:
        rows = _load_all()
        rows[session.id] = asdict(session)
        # Only the recent ones: this is a status board, not a history.
        newest = sorted(rows, key=lambda k: float(rows[k].get("started") or 0))[-20:]
        write_json(_path(), {"sessions": {k: rows[k] for k in newest}})


def _read(session_id: str) -> Session | None:
    row = _load_all().get(session_id)
    if row is None:
        return None
    known = set(Session.__dataclass_fields__)
    try:
        return Session(**{k: v for k, v in row.items() if k in known})
    except TypeError:
        return None


def set_notifier(callback: Callable[[str], None] | None) -> None:
    """Who hears about a finished session. Called from the worker thread."""
    global _notifier
    _notifier = callback


def child_environment() -> dict[str, str]:
    """The environment a session runs in: without our keys, without push.

    It reads whatever is in the repo, so it gets none of L.E.O.'s secrets. `git
    push` is aimed at a URL that cannot exist through config that lives only in
    this process tree, because writing it to the repo's own config would break
    the user's real pushes.
    """
    drop = {"GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"}
    env = {
        key: value for key, value in os.environ.items()
        if key not in drop and not key.startswith(("EV_", "LEO_"))
    }
    pairs = [("url.leo-no-push://blocked/.pushInsteadOf", prefix) for prefix in _PUSH_PREFIXES]
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for index, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: str, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _make_worktree(directory: Path, session_id: str) -> tuple[str, str, str, str]:
    """(repo root, worktree, branch, "") or ("", "", "", why not)."""
    root = _git(str(directory), "rev-parse", "--show-toplevel")
    if root.returncode != 0:
        return "", "", "", f"{directory} is not a git repository"
    repo = root.stdout.strip()
    branch = f"leo/{session_id}"
    folder = config.STATE_DIR / "worktrees" / session_id
    folder.parent.mkdir(parents=True, exist_ok=True)
    made = _git(repo, "worktree", "add", "-b", branch, str(folder), "HEAD", timeout=60.0)
    if made.returncode != 0:
        return "", "", "", f"git worktree failed: {made.stderr.strip()[:200]}"
    return repo, str(folder), branch, ""


def _detect_test_command(worktree: str) -> str:
    """From config, or a fixed table. Never from the model."""
    if config.DEV_TEST_COMMAND:
        return config.DEV_TEST_COMMAND
    root = Path(worktree)
    if (root / "tests").is_dir() or (root / "pytest.ini").exists() or (root / "pyproject.toml").exists():
        return f"{sys.executable} -m pytest -q"
    if (root / "package.json").exists():
        return "npm test --silent"
    if (root / "Cargo.toml").exists():
        return "cargo test --quiet"
    if (root / "go.mod").exists():
        return "go test ./..."
    return ""


def _claude_argv(session: Session, prompt: str, resume: bool) -> list[str] | None:
    exe = shutil.which(config.CLAUDE_CLI)
    if exe is None:
        return None
    argv = [
        exe, "-p", prompt, "--output-format", "stream-json", "--verbose",
        "--permission-mode", "acceptEdits",
        "--allowedTools", config.DEV_ALLOWED_TOOLS,
        "--disallowedTools", config.DEV_DENIED_TOOLS,
        "--max-budget-usd", f"{config.DEV_MAX_BUDGET_USD:g}",
    ]
    argv += ["--resume", session.claude_id] if resume else ["--session-id", session.claude_id]
    return argv


class _Worker(threading.Thread):
    """One run of `claude -p`, and what happens after it stops."""

    def __init__(self, session: Session, prompt: str, resume: bool) -> None:
        super().__init__(name=f"leo-dev-{session.id}", daemon=True)
        self.session = session
        self.prompt = prompt
        self.resume = resume
        self.stop_event = threading.Event()
        self._process: subprocess.Popen[str] | None = None

    def run(self) -> None:
        session = self.session
        try:
            self._run_agent()
            if session.state == _RUNNING:
                self._grade()
                session.state = "finished"
        except Exception as exc:
            log.warning("Coding session %s broke: %s", session.id, exc)
            session.state, session.why = "failed", f"internal error: {exc}"[:200]
        finally:
            session.finished = time.time()
            session.last_text = redact(session.last_text)
            session.test_output = redact(session.test_output)
            _store(session)
            _registry().forget(f"dev-{session.id}")
            with _lock:
                _live.pop(session.id, None)
            audit(
                "dev_session_done", id=session.id, state=session.state, tests=session.tests,
                files=session.files_changed, cost=round(session.cost_usd, 3),
            )
            self._notify()

    def _run_agent(self) -> None:
        session = self.session
        argv = _claude_argv(session, self.prompt, self.resume)
        if argv is None:
            session.state, session.why = "failed", "the claude CLI is not on PATH"
            return
        process = subprocess.Popen(
            argv, cwd=session.worktree, env=child_environment(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
            start_new_session=True,
        )
        self._process = process
        _registry().register(
            f"dev-{session.id}", process.pid, "claude -p", session.worktree, "", kind="dev"
        )
        lines: queue.Queue[str | None] = queue.Queue()

        def drain() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=drain, name=f"leo-dev-out-{session.id}", daemon=True).start()
        started = last_event = time.monotonic()
        ended = False
        while not ended:
            stop = self._should_stop(started, last_event)
            if stop:
                self._kill()
                session.state, session.why = "stopped", stop
                return
            try:
                line = lines.get(timeout=config.DEV_POLL_S)
            except queue.Empty:
                continue
            if line is None:
                ended = True
                continue
            last_event = time.monotonic()
            self._read_event(line)
            _store(session)
        code = process.wait()
        if code != 0 and session.state == _RUNNING:
            session.state, session.why = "failed", f"claude exited {code}"

    def _should_stop(self, started: float, last_event: float) -> str:
        now = time.monotonic()
        if self.stop_event.is_set():
            return "stopped when asked"
        if is_locked_down():
            return "stopped by lockdown"
        if now - started > config.DEV_MAX_RUNTIME_S:
            return "ran out of time"
        if now - last_event > config.DEV_STALL_S:
            return "went quiet"
        return ""

    def _read_event(self, line: str) -> None:
        session = self.session
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        session.events += 1
        kind = event.get("type")
        if kind == "assistant":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    session.last_text = str(block.get("text", ""))[-2000:]
        elif kind == "result":
            session.runs += 1
            session.cost_usd += float(event.get("total_cost_usd") or 0.0)
            if event.get("result"):
                session.last_text = str(event["result"])[-2000:]
            if event.get("is_error"):
                session.state = "failed"
                session.why = str(event.get("subtype") or "the run reported an error")[:120]

    def _kill(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3.0)
        except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired) as exc:
            log.debug("Killing session process: %s", exc)

    def _grade(self) -> None:
        """Run the tests ourselves and count what changed. Nothing the model said."""
        session = self.session
        # What it committed on its branch, plus what it left uncommitted.
        status = _git(session.worktree, "status", "--porcelain")
        committed = _git(session.worktree, "diff", "--name-only", f"{session.base_commit}..HEAD")
        names = {line[3:].strip() for line in status.stdout.splitlines() if line.strip()}
        names |= {line.strip() for line in committed.stdout.splitlines() if line.strip()}
        session.files_changed = len(names)
        session.diff = _git(session.worktree, "diff", "--stat", session.base_commit).stdout[-1500:]
        command = _detect_test_command(session.worktree)
        if not command:
            session.tests = "none found"
            return
        verdict = classify(command)
        if verdict.is_blocked:
            session.tests = "refused"
            return
        session.tests, session.test_output = self._run_tests(command)

    def _run_tests(self, command: str) -> tuple[str, str]:
        process = subprocess.Popen(
            ["bash", "-lc", command], cwd=self.session.worktree, env=child_environment(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            errors="replace", start_new_session=True,
        )
        deadline = time.monotonic() + config.DEV_TEST_TIMEOUT_S
        while process.poll() is None:
            if self.stop_event.is_set() or is_locked_down() or time.monotonic() > deadline:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                process.wait(timeout=5.0)
                return "stopped", ""
            time.sleep(min(config.DEV_POLL_S, 0.2))
        output = process.stdout.read() if process.stdout else ""
        return ("passed" if process.returncode == 0 else "failed"), output[-3000:]

    def _notify(self) -> None:
        callback = _notifier
        if callback is None:
            return
        try:
            callback(spoken_summary(self.session))
        except Exception as exc:
            log.debug("Session notifier raised: %s", exc)


def spoken_summary(session: Session) -> str:
    """Built only from numbers and fixed words. No text from the session at all."""
    files = f"{session.files_changed} file" + ("" if session.files_changed == 1 else "s")
    if session.state == "finished":
        tests = {
            "passed": "Tests pass",
            "failed": "Tests fail",
            "none found": "No tests to run",
            "refused": "I wouldn't run its tests",
            "stopped": "Tests didn't finish",
        }.get(session.tests, "Tests didn't run")
        return f"Coding session done. {tests}, {files} changed."
    if session.state == "stopped":
        reason = {
            "stopped when asked": "as you asked",
            "stopped by lockdown": "by lockdown",
            "ran out of time": "out of time",
            "went quiet": "because it went quiet",
        }.get(session.why, "")
        return f"Coding session stopped {reason}.".replace("  ", " ").replace(" .", ".")
    return "Coding session failed."


def _start(goal: str, directory: str, confirmed: bool) -> ToolResult:
    text = goal.strip()
    if not text:
        return ToolResult.failure("Work on what?", "start needs goal.")
    if shutil.which(config.CLAUDE_CLI) is None:
        return ToolResult.failure("Claude Code isn't installed.", f"'{config.CLAUDE_CLI}' is not on PATH.")
    target = resolve_directory(directory, config.DEFAULT_PROJECT_DIR)
    if target is None:
        return ToolResult.failure(f"I can't find {directory}.", f"No directory matched {directory!r}.")
    with _lock:
        busy = [sid for sid in _live]
    if len(busy) >= config.DEV_MAX_SESSIONS:
        return ToolResult.failure(
            "A coding session is already running.", f"Running: {', '.join(busy)}. Stop it or wait."
        )
    if _git(str(target), "rev-parse", "--show-toplevel").returncode != 0:
        return ToolResult.failure(f"{target.name} isn't a git repo.", f"{target} is not a git repository.")
    if not confirmed:
        return ToolResult.confirm(
            f"Start a coding session in {target.name}, up to {config.DEV_MAX_BUDGET_USD:g} dollars? Confirm?",
            f"Waiting on a yes. Goal: {text}. It runs in its own worktree and branch and never pushes.",
            action="start", goal=text, directory=str(target), reason="runs a coding agent unattended",
        )
    session_id = uuid.uuid4().hex[:8]
    repo, worktree, branch, why = _make_worktree(target, session_id)
    if why:
        return ToolResult.failure("Couldn't set up a worktree.", why)
    base = _git(worktree, "rev-parse", "HEAD").stdout.strip() or "HEAD"
    session = Session(session_id, str(uuid.uuid4()), text, repo, worktree, branch, base)
    return _launch(session, text, resume=False, opened="started")


def _launch(session: Session, prompt: str, resume: bool, opened: str) -> ToolResult:
    session.state, session.why = _RUNNING, ""
    _store(session)
    worker = _Worker(session, prompt, resume)
    with _lock:
        _live[session.id] = worker
    worker.start()
    audit(f"dev_session_{opened}", id=session.id, branch=session.branch)
    if config.DEV_OPEN_EDITOR:
        _open_editor(session.worktree)
    return ToolResult.success(
        f"Coding session {opened}. I'll tell you when it stops.",
        f"Session {session.id} on branch {session.branch} in {session.worktree}. Goal: {prompt}",
    )


def _open_editor(worktree: str) -> None:
    exe = shutil.which(config.VSCODE_CLI)
    if exe is None:
        return
    try:
        subprocess.Popen([exe, worktree], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        log.debug("Could not open the editor: %s", exc)


def _latest() -> Session | None:
    rows = _load_all()
    if not rows:
        return None
    newest = max(rows, key=lambda k: float(rows[k].get("started") or 0))
    return _read(newest)


def _status(session_id: str) -> ToolResult:
    session = _read(session_id) if session_id else _latest()
    if session is None:
        return ToolResult.success("No coding sessions yet.", "Nothing recorded.")
    with _lock:
        worker = _live.get(session.id)
    current = worker.session if worker else session
    speech = (
        f"Still running, {current.events} steps in." if current.state == _RUNNING
        else spoken_summary(current)
    )
    return ToolResult.success(
        speech,
        f"Session {current.id}: {current.state} {current.why}\nGoal: {current.goal}\n"
        f"Branch {current.branch}, worktree {current.worktree}\nTests: {current.tests}, "
        f"files changed: {current.files_changed}, cost: ${current.cost_usd:.2f}\n"
        f"Diff:\n{current.diff}\nTest output:\n{current.test_output}\n"
        f"Claude last said:\n{current.last_text}",
    )


def _continue(session_id: str, instruction: str, confirmed: bool) -> ToolResult:
    text = instruction.strip()
    if not text:
        return ToolResult.failure("Continue with what?", "continue needs goal: what to do next.")
    session = _read(session_id) if session_id else _latest()
    if session is None:
        return ToolResult.failure("There's no session to continue.", "Nothing recorded.")
    with _lock:
        running = session.id in _live
        busy = len(_live)
    if running:
        return ToolResult.failure("It's still running.", f"Session {session.id} has not stopped yet.")
    if busy >= config.DEV_MAX_SESSIONS:
        return ToolResult.failure("A coding session is already running.", "Stop it or wait.")
    if not Path(session.worktree).is_dir():
        return ToolResult.failure("Its worktree has gone.", f"{session.worktree} no longer exists.")
    if not confirmed:
        return ToolResult.confirm(
            f"Carry on that coding session, up to {config.DEV_MAX_BUDGET_USD:g} more dollars? Confirm?",
            f"Waiting on a yes. Session {session.id}, next: {text}",
            action="continue", id=session.id, goal=text, reason="runs a coding agent unattended",
        )
    return _launch(session, text, resume=True, opened="continued")


def _stop(session_id: str) -> ToolResult:
    with _lock:
        workers = [w for sid, w in _live.items() if not session_id or sid == session_id]
    if not workers:
        return ToolResult.success("Nothing's running.", "No live coding session.")
    for worker in workers:
        worker.stop_event.set()
    return ToolResult.success("Stopping it.", f"Asked {len(workers)} session(s) to stop.")


def stop_all() -> int:
    """The kill switch's way in. Nothing here waits."""
    with _lock:
        workers = list(_live.values())
    for worker in workers:
        worker.stop_event.set()
    return len(workers)


def recover() -> int:
    """Stop sessions a killed process left behind. Nobody is supervising them."""
    if IS_WINDOWS:
        return 0
    registry = _registry()
    stopped = 0
    with _lock:
        live = {f"dev-{sid}" for sid in _live}
    for name, row in registry.entries(kind="dev").items():
        if name in live:
            continue
        if registry.stop_group(row):
            stopped += 1
        registry.forget(name)
    for session_id, row in _load_all().items():
        if row.get("state") == _RUNNING and f"dev-{session_id}" not in live:
            session = _read(session_id)
            if session is not None:
                session.state, session.why = "stopped", "L.E.O. restarted"
                _store(session)
    if stopped:
        log.warning("Stopped %d orphaned coding session(s)", stopped)
    return stopped


def dev_session(
    action: str = "",
    goal: str = "",
    directory: str = "",
    id: str = "",  # noqa: A002 - the session's id, in the schema's word
    confirmed: bool = False,
    **_: object,
) -> ToolResult:
    verb = (action or "status").strip().lower()
    if IS_WINDOWS:
        return ToolResult.failure(
            "Coding sessions only work on Linux and Mac so far.", "POSIX process groups are required."
        )
    if verb == "start":
        return _start(goal, directory, confirmed)
    if verb == "status":
        return _status(id)
    if verb == "continue":
        return _continue(id, goal, confirmed)
    if verb == "stop":
        return _stop(id)
    return ToolResult.failure("I can't do that with a coding session.", f"Unknown action {verb!r}.")
