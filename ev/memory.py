"""Persistent state that survives a reboot, a crash, or a pulled power cable.

E.V. is a long-running desktop process, which means it tends to get killed
rather than closed: a laptop lid, a Windows update, a power cut. Anything worth
keeping therefore has to be on disk *before* the thing that kills it happens,
not flushed on the way out.

Three decisions follow from that:

* **Every write is atomic.** A temp file in the same directory, `fsync`, then
  `os.replace`. A half-written JSON file is the one failure mode that turns a
  memory system into a startup crash, and `os.replace` is atomic on Windows as
  well as POSIX, so what is on disk is always either the old file or the new
  one.
* **A corrupt file is a warning, not an error.** If the JSON will not parse it
  is moved aside and E.V. starts with empty state. Losing preferences is
  survivable; refusing to boot is not.
* **The clean-shutdown flag is written pessimistically.** It is cleared when a
  session starts and only set again on an orderly stop, so an unclean exit is
  detectable next time round instead of being indistinguishable from a tidy one.

This module is dependency-free and holds a few hundred bytes of state, in
keeping with the project's resident-memory budget.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config

log = logging.getLogger("ev.memory")

SCHEMA_VERSION = 1


# -- JSON store helpers ------------------------------------------------------
# Shared with `ev.backlog`, which has exactly the same durability problem.
def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """Load a JSON object, tolerating a missing or corrupted file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return dict(default)
    except OSError as exc:
        log.warning("Could not read %s: %s", path, exc)
        return dict(default)

    try:
        loaded = json.loads(raw)
    except ValueError as exc:
        # Power loss mid-write is the usual cause. Keep the evidence, but do
        # not let it stop E.V. from starting.
        log.warning("Corrupt state file %s (%s); starting fresh", path, exc)
        try:
            path.replace(path.with_name(path.name + ".corrupt"))
        except OSError:
            pass
        return dict(default)

    if not isinstance(loaded, dict):
        return dict(default)
    return loaded


def write_json(path: Path, data: dict[str, Any]) -> bool:
    """Write a JSON object atomically. Returns False if it could not be saved."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        )
        try:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, path)
        return True
    except OSError as exc:
        log.warning("Could not save %s: %s", path, exc)
        return False


# -- formatting --------------------------------------------------------------
def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def humanise_gap(seconds: float) -> str:
    """A spoken-English duration, such as "2 hours and 15 minutes".

    Coarse on purpose. Nobody wants to hear "2 hours, 15 minutes and 3
    seconds", and this string is read aloud.
    """
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "under a minute"
    minutes = int(seconds // 60)
    if minutes < 60:
        return _plural(minutes, "minute")
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        if minutes:
            return _plural(hours, "hour") + " and " + _plural(minutes, "minute")
        return _plural(hours, "hour")
    days, hours = divmod(hours, 24)
    if days < 7 and hours:
        return _plural(days, "day") + " and " + _plural(hours, "hour")
    return _plural(days, "day")


@dataclass
class StartupReport:
    """What the last session left behind, as seen by this one."""

    first_run: bool = False
    offline_seconds: float = 0.0
    clean_shutdown: bool = True
    sessions: int = 0
    last_session_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def offline_phrase(self) -> str:
        return humanise_gap(self.offline_seconds)

    @property
    def greeting(self) -> str:
        """A short spoken welcome, or "" when there is nothing worth saying.

        Plain speech only: no labels, no machine detail. It reaches the speaker
        by the same path as any other reply.
        """
        if self.first_run:
            return ""
        if not self.clean_shutdown:
            return f"Welcome back. Last session ended badly, {self.offline_phrase} ago."
        if self.offline_seconds < config.MEMORY_MIN_GAP_S:
            return ""
        return f"Welcome back. You were offline for {self.offline_phrase}."

    def greeting_for(self, name: str = "") -> str:
        """The spoken welcome, led by the user's name.

        Distinct from `greeting`, which stays quiet on a quick restart because
        being greeted like a homecoming every time a process restarts gets old
        fast. This one is the front door: it is said on every start, because a
        voice assistant that comes up silent is indistinguishable from one
        that did not come up at all.

        Plain speech, no labels - it reaches `Speaker.say` by the same path as
        any other reply. The gap clause is appended only when there is a gap
        worth mentioning, so a restart says "Welcome back, Alan." and nothing
        more.
        """
        who = (name or "").strip()
        opening = f"Welcome back, {who}." if who else "Welcome back."

        if self.first_run:
            return opening
        if not self.clean_shutdown:
            return f"{opening} Last session ended badly, {self.offline_phrase} ago."
        if self.offline_seconds < config.MEMORY_MIN_GAP_S:
            return opening
        return f"{opening} You were offline for {self.offline_phrase}."

    @property
    def context(self) -> str:
        """The same facts, written for the model rather than for the speaker."""
        if self.first_run:
            return "This is the first session with this user."
        lines = [f"The user was away for {self.offline_phrase} before this session."]
        if not self.clean_shutdown:
            lines.append(
                "The previous session ended unexpectedly - a crash, reboot or "
                "power loss - rather than being shut down."
            )
        lines.extend(self.notes)
        return " ".join(lines)


class Memory:
    """User preferences, profile facts, and cross-session runtime state.

    A disabled instance is inert but still answers every call, so call sites
    never need a `None` check.
    """

    def __init__(self, path: Path | None = None, enabled: bool | None = None) -> None:
        self.path = Path(path) if path is not None else config.MEMORY_FILE
        self.enabled = config.MEMORY_ENABLED if enabled is None else enabled
        self._data = self._blank()
        self._session_started = 0.0
        self._last_touch = 0.0
        if self.enabled:
            self.load()

    # -- storage ----------------------------------------------------------
    @staticmethod
    def _blank() -> dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "profile": {},
            "preferences": {},
            # A standing to-do list, distinct from `ev.backlog`. The backlog is
            # what E.V. failed to finish and files by itself; this is what the
            # user asked to be kept. Mixing them would put "remind me to call
            # the dentist" next to a cancelled file copy, and let "clear the
            # backlog" quietly delete the dentist.
            "todos": [],
            "state": {
                "last_active": 0.0,
                "clean_shutdown": True,
                "sessions": 0,
                "last_session_seconds": 0.0,
                "total_seconds": 0.0,
            },
        }

    def load(self) -> None:
        loaded = read_json(self.path, self._blank())
        merged = self._blank()
        # Merge rather than replace, so a file written by an older version is
        # still usable instead of being discarded wholesale.
        for section in ("profile", "preferences", "state"):
            value = loaded.get(section)
            if isinstance(value, dict):
                merged[section].update(value)
        # A list, not a dict, and every row is rebuilt rather than trusted.
        # This file can be hand-edited, and one malformed row must not be the
        # difference between booting and not booting.
        todos = loaded.get("todos")
        if isinstance(todos, list):
            merged["todos"] = [
                {
                    "text": str(row.get("text", "")).strip(),
                    "done": bool(row.get("done", False)),
                    "added": float(row.get("added") or 0.0),
                }
                for row in todos
                if isinstance(row, dict) and str(row.get("text", "")).strip()
            ]
        self._data = merged

    def save(self) -> bool:
        if not self.enabled:
            return False
        return write_json(self.path, self._data)

    # -- session lifecycle ------------------------------------------------
    def begin_session(self) -> StartupReport:
        """Open a session and report what the last one left behind.

        The clean-shutdown flag is cleared here and persisted immediately, so
        that if this process is killed the next start can tell.
        """
        state = self._data["state"]
        now = time.time()
        last_active = float(state.get("last_active") or 0.0)
        first_run = not last_active

        report = StartupReport(
            first_run=first_run,
            # A restored backup or a clock change can put `last_active` in the
            # future, and a negative gap would produce nonsense speech.
            offline_seconds=max(0.0, now - last_active) if last_active else 0.0,
            clean_shutdown=bool(state.get("clean_shutdown", True)),
            sessions=int(state.get("sessions") or 0),
            last_session_seconds=float(state.get("last_session_seconds") or 0.0),
        )

        self._session_started = time.monotonic()
        self._last_touch = self._session_started
        state["sessions"] = report.sessions + 1
        state["clean_shutdown"] = False
        state["last_active"] = now
        self.save()
        return report

    def touch(self, force: bool = False) -> None:
        """Record that E.V. is still alive, at most once per interval.

        Called on every exchange. Throttled because the point is to bound how
        much of a session an unclean exit can lose, not to write a file per
        sentence.
        """
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self._last_touch < config.MEMORY_TOUCH_INTERVAL_S:
            return
        self._last_touch = now
        self._data["state"]["last_active"] = time.time()
        self.save()

    def end_session(self, clean: bool = True) -> None:
        """Close the session, marking it clean so the next start stays quiet."""
        if not self.enabled:
            return
        state = self._data["state"]
        elapsed = self.session_seconds
        state["last_session_seconds"] = round(elapsed, 1)
        state["total_seconds"] = round(
            float(state.get("total_seconds") or 0.0) + elapsed, 1
        )
        state["last_active"] = time.time()
        state["clean_shutdown"] = bool(clean)
        self.save()

    @property
    def session_seconds(self) -> float:
        if not self._session_started:
            return 0.0
        return time.monotonic() - self._session_started

    # -- preferences and profile ------------------------------------------
    @property
    def preferences(self) -> dict[str, str]:
        return dict(self._data["preferences"])

    @property
    def profile(self) -> dict[str, str]:
        return dict(self._data["profile"])

    def remember(self, key: str, value: str, profile: bool = False) -> bool:
        """Store one fact. Returns False if it could not be persisted."""
        name = (key or "").strip().lower()
        if not name:
            return False
        section = self._data["profile"] if profile else self._data["preferences"]
        section[name] = (value or "").strip()
        # A runaway "remember this" loop must not grow the file without bound.
        overflow = len(section) - config.MEMORY_MAX_ENTRIES
        for stale in list(section)[:overflow] if overflow > 0 else ():
            section.pop(stale, None)
        return self.save()

    def recall(self, key: str) -> str | None:
        name = (key or "").strip().lower()
        if not name:
            return None
        if name in self._data["preferences"]:
            return self._data["preferences"][name]
        return self._data["profile"].get(name)

    def forget(self, key: str) -> bool:
        name = (key or "").strip().lower()
        dropped = self._data["preferences"].pop(name, None)
        if dropped is None:
            dropped = self._data["profile"].pop(name, None)
        if dropped is None:
            return False
        self.save()
        return True

    def clear(self) -> None:
        """Drop every remembered fact. Deliberately leaves the to-do list.

        "Forget what you know about me" is about preferences, not about the
        errands the user asked to be held. Losing those to a phrase aimed at
        something else is the kind of data loss nobody forgives.
        """
        self._data["preferences"].clear()
        self._data["profile"].clear()
        self.save()

    # -- to-do list -------------------------------------------------------
    @property
    def todos(self) -> list[dict[str, Any]]:
        """Every entry, done and not, oldest first."""
        return [dict(row) for row in self._data["todos"]]

    def open_todos(self) -> list[str]:
        return [row["text"] for row in self._data["todos"] if not row.get("done")]

    def add_todo(self, item: str) -> bool:
        """Append one errand. A duplicate is a no-op, not a second row."""
        text = (item or "").strip()
        if not text:
            return False
        for row in self._data["todos"]:
            if row["text"].lower() == text.lower() and not row.get("done"):
                return True  # already listed; holding it twice helps nobody
        self._data["todos"].append({"text": text, "done": False, "added": time.time()})

        # Bounded for the same reason the fact store is: the open items ride in
        # the system prompt, so an unbounded list is an unbounded prompt.
        # Completed rows go first - they have already served their purpose.
        overflow = len(self._data["todos"]) - config.MEMORY_MAX_TODOS
        if overflow > 0:
            done = [i for i, r in enumerate(self._data["todos"]) if r.get("done")]
            for index in sorted(done[:overflow], reverse=True):
                self._data["todos"].pop(index)
                overflow -= 1
            if overflow > 0:
                del self._data["todos"][:overflow]
        return self.save()

    def _match_todo(self, item: str) -> int:
        """Index of the row the user means, or -1.

        Speech says "the first one" far more often than it quotes an errand
        back word for word, so a 1-based position into the *open* items is the
        primary form and a substring match is the fallback.
        """
        ref = (item or "").strip().lower()
        if not ref:
            return -1
        open_rows = [i for i, r in enumerate(self._data["todos"]) if not r.get("done")]

        if ref == "last":
            return open_rows[-1] if open_rows else -1

        ordinals = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3}
        position = ordinals.get(ref)
        if position is None and ref.isdigit():
            position = int(ref)
        if position is not None:
            if 1 <= position <= len(open_rows):
                return open_rows[position - 1]
            return -1

        for index in open_rows:
            if ref in self._data["todos"][index]["text"].lower():
                return index
        for index, row in enumerate(self._data["todos"]):
            if ref in row["text"].lower():
                return index
        return -1

    def complete_todo(self, item: str) -> str:
        """Mark one errand done. Returns its text, or "" if nothing matched."""
        index = self._match_todo(item)
        if index < 0:
            return ""
        self._data["todos"][index]["done"] = True
        self.save()
        return self._data["todos"][index]["text"]

    def drop_todo(self, item: str) -> str:
        """Remove one errand outright. Returns its text, or ""."""
        index = self._match_todo(item)
        if index < 0:
            return ""
        removed = self._data["todos"].pop(index)
        self.save()
        return removed["text"]

    def clear_todos(self) -> int:
        count = len(self._data["todos"])
        self._data["todos"].clear()
        self.save()
        return count

    # -- model context ----------------------------------------------------
    def context(self) -> str:
        """What the model should know about this user, as one short block.

        Kept small on purpose: it rides in the system prompt, so it costs
        tokens on every single turn.
        """
        parts: list[str] = []
        if self._data["profile"]:
            parts.append(
                "Known about the user: "
                + "; ".join(f"{k} is {v}" for k, v in self._data["profile"].items())
                + "."
            )
        if self._data["preferences"]:
            parts.append(
                "Remembered preferences: "
                + "; ".join(f"{k} is {v}" for k, v in self._data["preferences"].items())
                + "."
            )
        open_items = self.open_todos()
        if open_items:
            # Capped separately from the store itself: the list may legitimately
            # grow long, but the prompt it rides in may not.
            shown = open_items[: config.MEMORY_CONTEXT_TODOS]
            parts.append(
                f"The user's to-do list ({len(open_items)} open): "
                + "; ".join(shown)
                + "."
            )
        return " ".join(parts)

    def todo_summary(self) -> str:
        """The spoken boot line for the to-do list, or "".

        Plain speech - it reaches the speaker by the same path as any reply,
        so it carries no labels and no machine detail.
        """
        open_items = self.open_todos()
        if not open_items:
            return ""
        if len(open_items) == 1:
            return f"One thing on your list: {open_items[0]}."
        return (
            f"You have {len(open_items)} things on your list, "
            f"starting with {open_items[0]}."
        )


_memory: Memory | None = None


def get_memory() -> Memory:
    """The process-wide store, shared by the core loop and the tools.

    Rebuilt when `config.MEMORY_FILE` changes, which is what lets a test point
    the whole system at a temporary directory with a single monkeypatch.
    """
    global _memory
    if _memory is None or _memory.path != config.MEMORY_FILE:
        _memory = Memory()
    return _memory
