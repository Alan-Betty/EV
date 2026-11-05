"""The Memory Tree: who and what a note is about, and how they connect.

Notes used to be one flat pile. "Priya's birthday is in June" and "the deploy
script needs a token" sat side by side with nothing saying one concerns a
person and the other a project, and nothing able to answer "what do you know
about Priya" except a keyword search.

The tree is **logical**: a note carries tag paths such as `entities/priya` or
`topics/leo/site`, and all notes stay in the one `semantic_memory.json`. One
file per branch looks tidier and is worse in every way that was measured:
`SemanticMemory.search` embeds its query once per store, so N branches meant N
network calls per search; IDF is computed per store, so scores from two
branches are not comparable; the 200-note cap is per store, so the total would
be unbounded; and recall runs on the event loop, where fanning out across
files is a stall the user hears.

This module holds only what notes cannot: the registry (paths, kinds, the names
each answers to) and the edges between them, in `memory_tree.json`. It never
calls a model and never touches the network, because `context_for` runs on
every utterance.

Paths are built by `_slug` from whatever the model or the user said and are
never used as filesystem paths, so a hostile name is an odd tag and nothing else.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config
from leo.memory import read_json, write_json

log = logging.getLogger("leo.memory_tree")

SCHEMA_VERSION = 1
ENTITIES = "entities"
TOPICS = "topics"
_SLUG_RUN = re.compile(r"[^a-z0-9]+")
_MAX_NAME = 60
# Names that would match half of all sentences if they were aliases.
_TOO_COMMON = frozenset({
    "me", "my", "i", "you", "he", "she", "they", "we", "it", "the", "a", "an", "this", "that",
    "him", "her", "them", "us", "home", "work", "today",
})
# What `link="<relation>: <target>"` means when the relation is one of these:
# not an edge between two nodes but a change to this one.
_ALIAS_WORDS = frozenset({"alias", "aka", "nickname", "also", "also called"})
_RENAME_WORDS = frozenset({"rename", "renamed", "renamed to"})


def _slug(text: str) -> str:
    return _SLUG_RUN.sub("-", (text or "").strip().lower()).strip("-")[:_MAX_NAME]


def _clean_name(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:_MAX_NAME]


@dataclass
class Node:
    path: str
    kind: str
    name: str
    aliases: list[str]
    created: float
    updated: float

    def to_row(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "aliases": self.aliases,
            "created": self.created,
            "updated": self.updated,
        }


@dataclass
class Edge:
    source: str
    relation: str
    target: str
    origin: str
    updated: float

    def to_row(self) -> dict[str, Any]:
        return {
            "from": self.source,
            "rel": self.relation,
            "to": self.target,
            "source": self.origin,
            "updated": self.updated,
        }


class MemoryTree:
    """The registry and the edges. Every failure degrades to a flat memory."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else config.MEMORY_TREE_FILE
        self._lock = threading.RLock()
        self._nodes: dict[str, Node] = {}
        self._edges: list[Edge] = []
        self._alias_index: list[tuple[re.Pattern[str], str]] = []
        self.load()

    # -- storage ---------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            raw = read_json(self.path, {"version": SCHEMA_VERSION, "nodes": {}, "edges": []})
            nodes: dict[str, Node] = {}
            rows = raw.get("nodes") if isinstance(raw, dict) else None
            for path, row in (rows or {}).items() if isinstance(rows, dict) else ():
                node = self._valid_node(path, row)
                if node is not None:
                    nodes[path] = node
            self._nodes = nodes
            edges = raw.get("edges") if isinstance(raw, dict) else None
            self._edges = [
                edge for edge in map(self._valid_edge, edges if isinstance(edges, list) else [])
                if edge is not None
            ]
            self._reindex()

    @staticmethod
    def _valid_node(path: Any, row: Any) -> Node | None:
        if not isinstance(path, str) or not isinstance(row, dict):
            return None
        root = path.split("/", 1)[0]
        name = _clean_name(row.get("name", ""))
        if root not in (ENTITIES, TOPICS) or "/" not in path or not name:
            return None
        aliases = row.get("aliases")
        now = time.time()
        return Node(
            path, root, name,
            [_clean_name(a) for a in aliases if _clean_name(a)] if isinstance(aliases, list) else [],
            float(row.get("created") or now), float(row.get("updated") or now),
        )

    @staticmethod
    def _valid_edge(row: Any) -> Edge | None:
        if not isinstance(row, dict):
            return None
        source, relation, target = row.get("from"), _clean_name(row.get("rel", "")), row.get("to")
        if not isinstance(source, str) or not isinstance(target, str) or not relation:
            return None
        return Edge(source, relation, target, str(row.get("source", "")), float(row.get("updated") or 0))

    def save(self) -> bool:
        with self._lock:
            return write_json(
                self.path,
                {
                    "version": SCHEMA_VERSION,
                    "nodes": {path: node.to_row() for path, node in self._nodes.items()},
                    "edges": [edge.to_row() for edge in self._edges],
                },
            )

    def _reindex(self) -> None:
        index: list[tuple[str, str]] = []
        for path, node in self._nodes.items():
            for name in [node.name, *node.aliases]:
                if name.lower() not in _TOO_COMMON:
                    index.append((name.lower(), path))
        # Longest first, so "Priya Sharma" wins over "Priya".
        index.sort(key=lambda item: -len(item[0]))
        self._alias_index = [
            (re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.IGNORECASE), path)
            for name, path in index
        ]

    # -- reads -----------------------------------------------------------
    def __len__(self) -> int:
        return len(self._nodes)

    def node(self, path: str) -> Node | None:
        with self._lock:
            return self._nodes.get(path)

    @property
    def nodes(self) -> list[Node]:
        with self._lock:
            return list(self._nodes.values())

    @property
    def edges(self) -> list[Edge]:
        with self._lock:
            return list(self._edges)

    def resolve(self, text: str) -> list[str]:
        """Every node named in `text`, longest name first. Local and quick."""
        if not text:
            return []
        found: list[str] = []
        with self._lock:
            for pattern, path in self._alias_index:
                if path not in found and pattern.search(text):
                    found.append(path)
        return found

    def locate(self, about: str) -> str | None:
        """The existing node `about` refers to, however it was phrased."""
        wanted = _clean_name(about)
        if not wanted:
            return None
        with self._lock:
            if wanted in self._nodes:
                return wanted
            lowered = wanted.lower()
            for node in self._nodes.values():
                if lowered in (name.lower() for name in [node.name, *node.aliases]):
                    return node.path
            candidate = self._path_for(wanted)
            return candidate if candidate in self._nodes else None

    @staticmethod
    def _path_for(about: str) -> str:
        """`Priya` -> entities/priya. `topic: Leo/site` -> topics/leo/site."""
        text = about.strip()
        lowered = text.lower()
        for prefix in ("topic:", "topics/", "project:", "topic/"):
            if lowered.startswith(prefix):
                rest = text[len(prefix):]
                parts = [_slug(part) for part in rest.split("/")]
                joined = "/".join(part for part in parts if part)
                return f"{TOPICS}/{joined}" if joined else ""
        for prefix in ("person:", "entity:", "entities/", "entity/"):
            if lowered.startswith(prefix):
                slug = _slug(text[len(prefix):])
                return f"{ENTITIES}/{slug}" if slug else ""
        slug = _slug(text)
        return f"{ENTITIES}/{slug}" if slug else ""

    @staticmethod
    def _display(about: str) -> str:
        text = about.strip()
        for prefix in ("topic:", "project:", "person:", "entity:"):
            if text.lower().startswith(prefix):
                return text[len(prefix):].strip()
        return text.rsplit("/", 1)[-1] if text.lower().startswith(("topics/", "entities/")) else text

    def related(self, path: str) -> list[Edge]:
        with self._lock:
            return [edge for edge in self._edges if path in (edge.source, edge.target)]

    def describe_links(self, path: str) -> str:
        """One line a person could read: "Priya: sister of Sam; works at Acme"."""
        node = self.node(path)
        if node is None:
            return ""
        parts: list[str] = []
        for edge in self.related(path):
            if edge.source == path:
                other = self.node(edge.target)
                parts.append(f"{edge.relation} {other.name if other else edge.target}")
            else:
                other = self.node(edge.source)
                parts.append(f"{other.name if other else edge.source} is {edge.relation} them")
        return f"{node.name}: {'; '.join(parts)}" if parts else ""

    # -- writes ----------------------------------------------------------
    def ensure(self, about: str) -> str | None:
        """The node for `about`, creating it and any missing ancestors."""
        name = _clean_name(about)
        if not name:
            return None
        with self._lock:
            known = self.locate(name)
            if known is not None:
                return known
            path = self._path_for(name)
            if not path or path.count("/") < 1:
                return None
            kind = path.split("/", 1)[0]
            now = time.time()
            parts = path.split("/")
            for depth in range(2, len(parts)):
                ancestor = "/".join(parts[:depth])
                if ancestor not in self._nodes:
                    self._nodes[ancestor] = Node(ancestor, kind, parts[depth - 1], [], now, now)
            self._nodes[path] = Node(path, kind, self._display(name) or parts[-1], [], now, now)
            self._reindex()
            return path if self.save() else None

    def touch(self, path: str) -> None:
        """A write under a node is news to everything above it too."""
        with self._lock:
            now = time.time()
            parts = path.split("/")
            changed = False
            for depth in range(2, len(parts) + 1):
                node = self._nodes.get("/".join(parts[:depth]))
                if node is not None:
                    node.updated = now
                    changed = True
            if changed:
                self.save()

    def add_alias(self, path: str, alias: str) -> bool:
        name = _clean_name(alias)
        with self._lock:
            node = self._nodes.get(path)
            if node is None or not name or name.lower() in _TOO_COMMON:
                return False
            if name.lower() not in (known.lower() for known in [node.name, *node.aliases]):
                node.aliases.append(name)
                node.updated = time.time()
                self._reindex()
            return self.save()

    def link(self, source: str, relation: str, target_about: str, origin: str = "user") -> Edge | None:
        """Connect two nodes, creating the target. The newest statement wins."""
        rel = _clean_name(relation)
        with self._lock:
            if source not in self._nodes or not rel:
                return None
            target = self.ensure(target_about)
            if target is None or target == source:
                return None
            now = time.time()
            for edge in self._edges:
                if edge.source == source and edge.target == target and edge.relation.lower() == rel.lower():
                    edge.updated, edge.origin = now, origin
                    self.save()
                    return edge
            edge = Edge(source, rel, target, origin, now)
            self._edges.append(edge)
            return edge if self.save() else None

    def rename(self, path: str, new_about: str) -> tuple[str, str] | None:
        """Move a branch. Returns (old prefix, new prefix) for the notes to follow.

        The old name stays as an alias: people go on saying it.
        """
        with self._lock:
            node = self._nodes.get(path)
            if node is None:
                return None
            kind = node.kind
            prefix = "topic:" if kind == TOPICS else "entity:"
            target = self._path_for(prefix + self._display(new_about))
            if not target or target == path or target in self._nodes:
                return None
            moved: dict[str, Node] = {}
            for known, item in list(self._nodes.items()):
                if known == path or known.startswith(path + "/"):
                    fresh = target + known[len(path):]
                    del self._nodes[known]
                    item.path = fresh
                    moved[fresh] = item
            self._nodes.update(moved)
            renamed = self._nodes[target]
            if renamed.name.lower() not in (a.lower() for a in renamed.aliases):
                renamed.aliases.append(renamed.name)
            renamed.name = self._display(new_about) or renamed.name
            renamed.updated = time.time()
            for edge in self._edges:
                if edge.source == path or edge.source.startswith(path + "/"):
                    edge.source = target + edge.source[len(path):]
                if edge.target == path or edge.target.startswith(path + "/"):
                    edge.target = target + edge.target[len(path):]
            self._reindex()
            return (path, target) if self.save() else None

    def remove(self, path: str) -> int:
        """Drop a node, everything under it, and every edge touching them."""
        with self._lock:
            doomed = {k for k in self._nodes if k == path or k.startswith(path + "/")}
            for key in doomed:
                del self._nodes[key]
            self._edges = [
                edge for edge in self._edges if edge.source not in doomed and edge.target not in doomed
            ]
            self._reindex()
            self.save()
            return len(doomed)

    def wipe(self) -> None:
        with self._lock:
            self._nodes, self._edges = {}, []
            self._reindex()
            self.save()


_tree: MemoryTree | None = None
_tree_lock = threading.Lock()


def get_memory_tree() -> MemoryTree:
    """Process-wide tree. Rebuilt when `config.MEMORY_TREE_FILE` changes."""
    global _tree
    with _tree_lock:
        if _tree is None or _tree.path != config.MEMORY_TREE_FILE:
            _tree = MemoryTree()
        return _tree


def split_link(link: str) -> tuple[str, str]:
    """"sister: Priya" -> ("sister", "Priya"). No colon means no target."""
    relation, separator, target = (link or "").partition(":")
    return (relation.strip().lower(), target.strip()) if separator else ("", "")


def apply_link(tree: MemoryTree, notes: Any, path: str, link: str, origin: str = "user") -> str:
    """Carry out `link="<relation>: <target>"` on a node. Returns what happened."""
    relation, target = split_link(link)
    if not relation or not target:
        return ""
    if relation in _ALIAS_WORDS:
        return f"{target} now also means {path}." if tree.add_alias(path, target) else ""
    if relation in _RENAME_WORDS:
        moved = tree.rename(path, target)
        if moved is None:
            return ""
        count = notes.retag(*moved)
        return f"Moved {moved[0]} to {moved[1]}, with {count} notes."
    edge = tree.link(path, relation, target, origin=origin)
    return f"{path} {edge.relation} {edge.target}." if edge else ""


def recall_lines(utterance: str, notes: Any, tree: MemoryTree | None = None) -> str:
    """What the prompt should carry for this utterance. Local, on the loop.

    A person or topic named in the sentence gets its own notes and links first:
    "when's Priya's birthday" should not depend on the birthday note outranking
    everything else on the word "birthday". Ordinary recall fills what is left.
    """
    tree = tree or get_memory_tree()
    budget = max(80, config.SEMANTIC_CONTEXT_CHARS)
    lines: list[str] = []
    seen: set[str] = set()

    def add(line: str) -> bool:
        nonlocal budget
        if len(line) > budget:
            return False
        budget -= len(line)
        lines.append(line)
        return True

    for path in tree.resolve(utterance)[: config.MEMORY_TREE_CONTEXT_NODES]:
        links = tree.describe_links(path)
        if links:
            add(f"- {links}")
        hits = notes.search(utterance, limit=config.SEMANTIC_CONTEXT_ITEMS, use_embeddings=False, under=path)
        found = [hit.note for hit in hits] or notes.recent_under(path, config.SEMANTIC_CONTEXT_ITEMS)
        for note in found:
            if note.id not in seen and add(f"- {note.text}"):
                seen.add(note.id)
    for hit in notes.search(
        utterance,
        limit=config.SEMANTIC_CONTEXT_ITEMS,
        min_score=config.SEMANTIC_CONTEXT_MIN_SCORE,
        use_embeddings=False,
    ):
        if hit.note.id not in seen and add(f"- {hit.note.text}"):
            seen.add(hit.note.id)
    return "\n".join(lines)


__all__ = ["Edge", "MemoryTree", "Node", "apply_link", "get_memory_tree", "recall_lines", "split_link"]
