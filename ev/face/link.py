"""The core's end of the face: start it, tell it things, never wait for it.

`ev_core` owns one of these. The face itself is a separate process
(`python -m ev.face --stdin`) for the reason documented in CLAUDE.md - Qt
costs ~85 MB and would otherwise be resident in the core for good - so from
here it is a pipe and nothing more.

Three rules, all about the face never costing the assistant anything:

* **Never block.** Lines go into a bounded queue and a writer thread puts
  them on the pipe. A face that has frozen fills its pipe, and a write to a
  full pipe blocks; that must stall a thread nobody waits on, never the
  event loop. When the queue is full the oldest line goes - a face behind
  on its moods only needs the latest one.
* **Never raise.** PySide6 missing, no display, a crashed face: each costs
  one log line, after which every call here is a no-op.
* **Say each state once.** The listening loop polls every couple of
  seconds, and re-sending the same mood on every poll would restart its
  transition each time. Repeats of a base state are dropped here.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

log = logging.getLogger("ev.face.link")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
QUEUE_LINES = 64
# One-shot moods that may legitimately repeat back to back - two successes
# in a row are two smiles, not one.
_REPEATABLE = frozenset({"success", "error", "poke", "barge_in", "misheard", "wake"})


class FaceLink:
    """A handle on the face process. Safe to call whether or not it exists."""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue(maxsize=QUEUE_LINES)
        self._writer: threading.Thread | None = None
        self._last_event = ""
        self._shown: bool | None = None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # -- lifecycle --------------------------------------------------------

    def start(self, verbose: bool = False) -> bool:
        """Launch the face. False, with the reason logged, if it cannot run."""
        if self.alive:
            return True
        if importlib.util.find_spec("PySide6") is None:
            log.info("Face off: PySide6 is not installed (pip install PySide6-Essentials)")
            return False
        if sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        ):
            log.info("Face off: no display")
            return False
        argv = [sys.executable, "-m", "ev.face", "--stdin"] + (["-v"] if verbose else [])
        try:
            self._process = subprocess.Popen(
                argv,
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                # Qt is chatty on stderr about fonts and portals. Shown only
                # when asked for, so it never lands in the middle of a reply.
                stderr=None if verbose else subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            log.info("Face off: could not start it (%s)", exc)
            self._process = None
            return False
        self._writer = threading.Thread(target=self._write, name="ev-face-link", daemon=True)
        self._writer.start()
        log.info("Face started (pid %s)", self._process.pid)
        return True

    def close(self) -> None:
        """Ask the face to go, then make sure it has."""
        process = self._process
        if process is None:
            return
        self._put(json.dumps({"quit": True}))
        self._put(None)
        if self._writer is not None:
            self._writer.join(timeout=1.0)
        try:
            process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            process.kill()
        self._process = None

    # -- what the core says -----------------------------------------------

    def event(self, name: str) -> None:
        """A core state - 'thinking', 'speaking', 'success' - for the face to show."""
        if name == self._last_event and name not in _REPEATABLE:
            return
        self._last_event = name
        self._send({"event": name})

    def show(self, on: bool) -> None:
        """Summon or dismiss, for EV_FACE_PRESENCE=summoned. Repeats are dropped."""
        if on == self._shown:
            return
        self._shown = on
        self._send({"show": bool(on)})

    def heard(self, text: str) -> None:
        if text and text.strip():
            self._send({"heard": text.strip()})

    def said(self, text: str) -> None:
        if text and text.strip():
            self._send({"say": text.strip()})

    # -- plumbing ---------------------------------------------------------

    def _send(self, message: dict) -> None:
        if self._process is None:
            return
        if not self.alive:
            log.info("Face process has gone; carrying on without it")
            self._process = None
            return
        self._put(json.dumps(message, ensure_ascii=False))

    def _put(self, line: str | None) -> None:
        try:
            self._lines.put_nowait(line)
        except queue.Full:
            try:
                self._lines.get_nowait()
                self._lines.put_nowait(line)
            except (queue.Empty, queue.Full):
                pass

    def _write(self) -> None:
        process = self._process
        if process is None or process.stdin is None:
            return
        stdin = process.stdin
        while True:
            line = self._lines.get()
            if line is None:
                break
            try:
                stdin.write(line + "\n")
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                break
        try:
            stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
