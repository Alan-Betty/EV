"""The backlog: what E.V. still owes the user from last time.

A desktop assistant gets interrupted constantly. A confirmation goes
unanswered because the phone rang, a copy fails because a drive was asleep, a
reminder is given at midnight for a thing that happens on Tuesday. All of that
used to evaporate the moment the process ended.

So anything E.V. started and did not finish lands here, on disk, and gets
handed back on the next boot: "we have two backlog items remaining from your
previous session". Brand new day, same list.

Durability comes from `ev.memory`: the same atomic write, the same tolerance
for a file that was corrupted by a power cut. An item that can be retried
stores the tool and the arguments that produced it, so `run` can replay it -
deliberately *without* the `confirmed` flag, so every safety gate applies again
on the way through. A backlog entry is a reminder, never a signed permission
slip.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import config
from ev.memory import read_json, write_json

log = logging.getLogger("ev.backlog")

SCHEMA_VERSION = 1

PENDING = "pending"
DONE = "done"

# What put the item on the list. Only used for phrasing, but the phrasing is
# the whole point of a backlog you are read back at breakfast.
KINDS = ("task", "interrupted", "failed", "reminder")


@dataclass
class Item:
    """One outstanding thing. `tool`/`args` are set only if it can be retried."""

    text: str
    kind: str = "task"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:6])
    created: float = field(default_factory=time.time)
    status: str = PENDING
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    @property
    def runnable(self) -> bool:
        return bool(self.tool)

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.created)

    def describe(self) -> str:
        """One line for the model and the terminal, never for the speaker."""
        label = f"[{self.id}] {self.text}"
        if self.kind != "task":
            label += f" ({self.kind})"
        if self.tool:
            label += f" -> {self.tool}"
        return label

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Item | None":
        text = str(raw.get("text") or "").strip()
        if not text:
            return None
        args = raw.get("args")
        kind = str(raw.get("kind") or "task")
        return cls(
            text=text,
            kind=kind if kind in KINDS else "task",
            id=str(raw.get("id") or uuid.uuid4().hex[:6]),
            created=float(raw.get("created") or time.time()),
            status=DONE if raw.get("status") == DONE else PENDING,
            tool=str(raw.get("tool") or ""),
            args=dict(args) if isinstance(args, dict) else {},
            note=str(raw.get("note") or ""),
        )


class Backlog:
    """An ordered, persisted list of unfinished work.

    A disabled instance still answers every call and simply keeps nothing, so
    call sites never need a `None` check.
    """

    def __init__(self, path: Path | None = None, enabled: bool | None = None) -> None:
        self.path = Path(path) if path is not None else config.BACKLOG_FILE
        self.enabled = config.BACKLOG_ENABLED if enabled is None else enabled
        self._items: list[Item] = []
        if self.enabled:
            self.load()

    # -- storage ----------------------------------------------------------
    def load(self) -> None:
        raw = read_json(self.path, {"version": SCHEMA_VERSION, "items": []})
        entries = raw.get("items")
        items: list[Item] = []
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                item = Item.from_dict(entry)
                if item is not None:
                    items.append(item)
        self._items = items

    def save(self) -> bool:
        if not self.enabled:
            return False
        return write_json(
            self.path,
            {
                "version": SCHEMA_VERSION,
                "saved_at": time.time(),
                "items": [asdict(item) for item in self._items],
            },
        )

    # -- queries ----------------------------------------------------------
    @property
    def items(self) -> list[Item]:
        return list(self._items)

    def pending(self) -> list[Item]:
        return [item for item in self._items if item.status == PENDING]

    def __len__(self) -> int:
        return len(self.pending())

    def get(self, reference: str) -> Item | None:
        """Find an item by spoken position, id, or a chunk of its text.

        Speech gives you "the first one" far more often than a hex id, so a
        1-based index into the pending list is the primary form.
        """
        ref = (reference or "").strip().lower()
        if not ref:
            return None
        pending = self.pending()

        ordinals = {
            "first": 1, "1st": 1, "one": 1,
            "second": 2, "2nd": 2, "two": 2,
            "third": 3, "3rd": 3, "three": 3,
            "last": len(pending),
        }
        position = ordinals.get(ref)
        if position is None and ref.isdigit():
            position = int(ref)
        if position is not None and 1 <= position <= len(pending):
            return pending[position - 1]
        # A position that does not exist falls through rather than failing:
        # "two" is an ordinal, but it is also a perfectly good item name, and
        # the list is short enough that the text match cannot be ambiguous.

        for item in self._items:
            if item.id == ref:
                return item
        for item in pending:
            if ref in item.text.lower():
                return item
        return None

    # -- mutation ---------------------------------------------------------
    def add(
        self,
        text: str,
        kind: str = "task",
        tool: str = "",
        args: dict[str, Any] | None = None,
        note: str = "",
    ) -> Item | None:
        """Append one item. Returns None when there is nothing worth storing."""
        cleaned = (text or "").strip()
        if not cleaned or not self.enabled:
            return None

        # The same failing command retried five times should be one entry, not
        # five. Match on text so a backlog read aloud stays short.
        for existing in self.pending():
            if existing.text.lower() == cleaned.lower():
                existing.created = time.time()
                self.save()
                return existing

        item = Item(
            text=cleaned,
            kind=kind if kind in KINDS else "task",
            tool=tool or "",
            args=dict(args or {}),
            note=note or "",
        )
        # `confirmed` is injected by the core loop after a spoken yes. Storing
        # it would turn a backlog entry into a standing permission to run a
        # gated command, so it is dropped on the way in.
        item.args.pop("confirmed", None)
        self._items.append(item)

        overflow = len(self._items) - config.BACKLOG_MAX_ITEMS
        if overflow > 0:
            log.info("Backlog full; dropping the %d oldest item(s)", overflow)
            del self._items[:overflow]
        self.save()
        return item

    def complete(self, reference: str) -> Item | None:
        item = self.get(reference)
        if item is None:
            return None
        item.status = DONE
        self.save()
        return item

    def drop(self, reference: str) -> Item | None:
        item = self.get(reference)
        if item is None:
            return None
        self._items = [other for other in self._items if other is not item]
        self.save()
        return item

    def clear(self, done_only: bool = False) -> int:
        """Empty the list. Returns how many items went."""
        before = len(self._items)
        if done_only:
            self._items = [item for item in self._items if item.status == PENDING]
        else:
            self._items = []
        removed = before - len(self._items)
        if removed:
            self.save()
        return removed

    def prune(self) -> int:
        """Drop completed items, which nobody needs read back to them."""
        return self.clear(done_only=True)

    # -- reporting --------------------------------------------------------
    def summary(self) -> str:
        """The spoken boot report, or "" when the list is empty.

        Plain speech: it goes to the speaker on the same path as any reply, so
        it carries no labels and no machine detail.
        """
        pending = self.pending()
        if not pending:
            return ""
        if len(pending) == 1:
            return f"One thing still open from last time: {pending[0].text}."
        return (
            f"We have {len(pending)} backlog items remaining from your previous "
            f"session. First one: {pending[0].text}."
        )

    def context(self) -> str:
        """The same list, written for the model."""
        pending = self.pending()
        if not pending:
            return ""
        lines = "; ".join(item.describe() for item in pending[:10])
        return f"Outstanding backlog items ({len(pending)}): {lines}."

    def listing(self) -> str:
        """Numbered detail for the terminal and the model."""
        pending = self.pending()
        if not pending:
            return "The backlog is empty."
        return "\n".join(
            f"  {index}. {item.describe()}" for index, item in enumerate(pending, 1)
        )


_backlog: Backlog | None = None


def get_backlog() -> Backlog:
    """The process-wide backlog, shared by the core loop and the tools.

    Rebuilt when `config.BACKLOG_FILE` changes, so a test can redirect the
    whole system with a single monkeypatch.
    """
    global _backlog
    if _backlog is None or _backlog.path != config.BACKLOG_FILE:
        _backlog = Backlog()
    return _backlog
