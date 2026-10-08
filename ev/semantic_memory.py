"""Long-term notes, recalled by meaning rather than by key.

`ev.memory` holds short named facts ("coffee is black") and puts every one in
the prompt. This holds free-form notes ("the spare key is under the blue
pot") and puts only the few that match the current utterance there.

Retrieval is local and dependency-free: BM25 over light stems, plus
character-trigram matching so a misheard word still lands. Gemini embeddings
(one httpx POST, cached per note) are an optional extra, fetched only inside
tool calls - never on the per-turn path, which must cost no network.

Failsafes, same rules as `ev.memory`:
* Atomic writes (`write_json`: temp, fsync, os.replace).
* Bad JSON or bad shape -> file moved to `.corrupt`, start empty. Bad rows
  are dropped one by one, not the whole file.
* Over `SEMANTIC_MAX_NOTES`: near notes merge, then least-used notes go to an
  append-only archive. Pruned, never silently lost.
* Delete archives first; wipe writes a `.bak` first.
* Network never runs under the lock: the per-turn read must not wait on it.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import config
from ev.memory import read_json, write_json

log = logging.getLogger("ev.semantic_memory")

SCHEMA_VERSION = 1

# -- text -----------------------------------------------------------------
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be but by do does for from had has have he her his i if "
    "in into is it its me my of on or our she so that the their them then there "
    "these they this to was we were what when where which who why will with you "
    "your about remember note recall know tell did can could would should please "
    "just".split()
)
_SUFFIXES = ("ingly", "edly", "ing", "ers", "ies", "ied", "ed", "es", "er", "ly", "s")


def stem(word: str) -> str:
    """Crude suffix strip. Enough for 'keys'/'key', 'parked'/'park'."""
    for suffix in _SUFFIXES:
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            base = word[: -len(suffix)]
            return base + "y" if suffix in ("ies", "ied") else base
    return word


def tokens(text: str) -> list[str]:
    # Single letters are possessives and contractions ("where's"); single
    # digits stay ("level 3").
    return [
        stem(w)
        for w in _WORD.findall((text or "").lower())
        if w not in _STOP and (len(w) > 1 or w.isdigit())
    ]


def _grams(word: str) -> frozenset[str]:
    padded = f" {word} "
    return frozenset(padded[i : i + 3] for i in range(len(padded) - 2))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _clean(text: Any) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat[: max(40, config.SEMANTIC_MAX_CHARS)]


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm:
        return []
    return [round(v / norm, 5) for v in vector]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


# -- rows -----------------------------------------------------------------
@dataclass
class Note:
    id: str
    text: str
    created: float
    updated: float
    hits: int = 0
    vec: list[float] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.id,
            "text": self.text,
            "created": self.created,
            "updated": self.updated,
            "hits": self.hits,
        }
        if self.vec:
            row["vec"] = self.vec
        return row


def validate_row(row: Any, dim: int) -> Note | None:
    """A trusted Note from an untrusted row, or None. Never raises."""
    if not isinstance(row, dict):
        return None
    text = _clean(row.get("text", ""))
    if not text:
        return None
    try:
        created = float(row.get("created") or time.time())
        updated = float(row.get("updated") or created)
        hits = max(0, int(row.get("hits") or 0))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(created) and math.isfinite(updated)):
        return None
    ident = str(row.get("id") or "").strip()[:32] or uuid.uuid4().hex[:8]
    vec: list[float] = []
    raw = row.get("vec")
    if isinstance(raw, list) and len(raw) == dim:
        try:
            vec = [float(v) for v in raw]
        except (TypeError, ValueError):
            vec = []
    return Note(ident, text, created, updated, hits, vec)


@dataclass
class Hit:
    note: Note
    score: float


# -- embeddings -------------------------------------------------------------
Embedder = Callable[[list[str], str], "list[list[float]] | None"]


class GeminiEmbedder:
    """Gemini `batchEmbedContents` over plain httpx. None on any failure.

    A failure starts a cooldown, so a dead key or a 429 costs one request,
    not one per search.
    """

    def __init__(self) -> None:
        self._client: Any = None
        self._cool_until = 0.0
        self._lock = threading.Lock()

    def __call__(self, texts: list[str], task: str) -> list[list[float]] | None:
        if not texts or time.monotonic() < self._cool_until:
            return None
        import httpx

        model = config.SEMANTIC_EMBED_MODEL
        body = {
            "requests": [
                {
                    "model": f"models/{model}",
                    "content": {"parts": [{"text": t}]},
                    "taskType": task,
                    "outputDimensionality": config.SEMANTIC_EMBED_DIM,
                }
                for t in texts[:64]
            ]
        }
        url = f"{config.GEMINI_BASE_URL}/models/{model}:batchEmbedContents"
        try:
            with self._lock:
                if self._client is None:
                    self._client = httpx.Client(timeout=config.SEMANTIC_EMBED_TIMEOUT_S)
                response = self._client.post(
                    url, json=body, headers={"x-goog-api-key": config.GEMINI_API_KEY}
                )
        except Exception as exc:
            return self._fail(f"embed request failed: {type(exc).__name__}", 30.0)
        if response.status_code != 200:
            wait = 60.0
            if response.status_code == 429:
                from ev.brain import _gemini_retry_after

                wait = _gemini_retry_after(response.text) or 60.0
            return self._fail(f"embed HTTP {response.status_code}", wait)
        try:
            rows = response.json().get("embeddings") or []
            vectors = [_normalise([float(v) for v in r.get("values", [])]) for r in rows]
        except Exception as exc:
            return self._fail(f"embed reply unreadable: {exc}", 60.0)
        if len(vectors) != len(body["requests"]) or not all(vectors):
            return self._fail("embed reply short", 60.0)
        return vectors

    def _fail(self, why: str, cool: float) -> None:
        log.info("Semantic memory: %s; lexical only for %.0fs", why, cool)
        self._cool_until = time.monotonic() + max(5.0, cool)
        return None


def _default_embedder() -> Embedder | None:
    mode = config.SEMANTIC_EMBEDDINGS
    if mode in ("auto", "gemini") and config.GEMINI_API_KEY:
        return GeminiEmbedder()
    return None


# -- the store --------------------------------------------------------------
class SemanticMemory:
    """Notes on disk, searched by meaning. A disabled instance is inert."""

    def __init__(
        self,
        path: Path | None = None,
        enabled: bool | None = None,
        embedder: Embedder | None | bool = True,
    ) -> None:
        self.path = Path(path) if path is not None else config.SEMANTIC_FILE
        self.enabled = config.SEMANTIC_ENABLED if enabled is None else enabled
        # True = pick from config; None/False = lexical only; callable = use it.
        if embedder is True:
            self.embedder: Embedder | None = _default_embedder()
        else:
            self.embedder = embedder or None
        self.dim = config.SEMANTIC_EMBED_DIM
        self._lock = threading.RLock()
        self._notes: list[Note] = []
        self._index: list[tuple[list[str], set[str], list[frozenset[str]]]] = []
        self.dropped_rows = 0
        if self.enabled:
            self.load()

    @property
    def archive_path(self) -> Path:
        return self.path.with_name(self.path.stem + ".archive.jsonl")

    # -- storage ---------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            loaded = read_json(self.path, {"version": SCHEMA_VERSION, "notes": []})
            rows = loaded.get("notes", [])
            if not isinstance(rows, list):
                # Parses, wrong shape. Keep the evidence, boot anyway.
                log.warning("Semantic memory %s has no note list; starting fresh", self.path)
                self._quarantine()
                rows = []
            stale_vectors = loaded.get("embed_model") not in (None, config.SEMANTIC_EMBED_MODEL)
            notes: list[Note] = []
            seen: set[str] = set()
            self.dropped_rows = 0
            for row in rows:
                note = validate_row(row, self.dim)
                if note is None or note.id in seen:
                    self.dropped_rows += 1
                    continue
                if stale_vectors:
                    # Another model's vectors are not comparable. Re-embed lazily.
                    note.vec = []
                seen.add(note.id)
                notes.append(note)
            if self.dropped_rows:
                log.warning("Semantic memory: dropped %d bad row(s)", self.dropped_rows)
            self._notes = notes
            self._reindex()

    def _quarantine(self) -> None:
        try:
            self.path.replace(self.path.with_name(self.path.name + ".corrupt"))
        except OSError:
            pass

    def save(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            rows = [n.to_row() for n in self._notes]
            # A row that would not load back must not be written.
            for row in rows:
                if validate_row(row, self.dim) is None:
                    log.warning("Semantic memory: refusing to save invalid row %r", row)
                    return False
            return write_json(
                self.path,
                {
                    "version": SCHEMA_VERSION,
                    "embed_model": config.SEMANTIC_EMBED_MODEL,
                    "notes": rows,
                },
            )

    def _reindex(self) -> None:
        index = []
        for note in self._notes:
            toks = tokens(note.text)
            uniq = set(toks)
            index.append((toks, uniq, [_grams(t) for t in uniq]))
        self._index = index

    # -- reads -------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._notes)

    @property
    def notes(self) -> list[Note]:
        with self._lock:
            return list(self._notes)

    def get(self, note_id: str) -> Note | None:
        with self._lock:
            return next((n for n in self._notes if n.id == note_id), None)

    def _lexical(self, query: str) -> list[float]:
        """0-1 per note: IDF-weighted query coverage (exact or fuzzy) x BM25 shape.

        Coverage is absolute, so a threshold means the same thing whatever
        else is stored; BM25 only orders notes that cover equally.
        """
        q = list(dict.fromkeys(tokens(query)))
        n = len(self._notes)
        if not q or not n:
            return [0.0] * n
        df = {t: sum(1 for _toks, uniq, _g in self._index if t in uniq) for t in q}
        idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q}
        total = sum(idf.values()) or 1.0
        q_grams = {t: _grams(t) for t in q}
        avg_len = (sum(len(i[0]) for i in self._index) / n) or 1.0

        coverage: list[float] = []
        bm25: list[float] = []
        for toks, uniq, grams in self._index:
            got = 0.0
            raw = 0.0
            for t in q:
                if t in uniq:
                    got += idf[t]
                    tf = toks.count(t)
                    raw += idf[t] * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(toks) / avg_len))
                elif len(t) >= 4:
                    best = max((_jaccard(q_grams[t], g) for g in grams), default=0.0)
                    if best >= 0.4:
                        got += idf[t] * best
            coverage.append(got / total)
            bm25.append(raw)
        top = max(bm25) or 1.0
        return [c * (0.75 + 0.25 * b / top) for c, b in zip(coverage, bm25)]

    def search(
        self,
        query: str,
        limit: int = 5,
        min_score: float = 0.0,
        use_embeddings: bool = True,
    ) -> list[Hit]:
        """Best notes for `query`. Network only when `use_embeddings` and an
        embedder is set; any failure there falls back to lexical."""
        if not self.enabled or not (query or "").strip():
            return []
        if use_embeddings and self.embedder is not None:
            self._backfill()
        with self._lock:
            if not self._notes:
                return []
            scores = self._lexical(query)
            notes = list(self._notes)
        if use_embeddings and self.embedder is not None:
            qv = self.embedder([query], "RETRIEVAL_QUERY")
            if qv:
                scores = self._blend(qv[0], notes, scores)
        floor = max(min_score, 1e-6)
        hits = [Hit(n, s) for n, s in zip(notes, scores) if s >= floor]
        hits.sort(key=lambda h: (h.score, h.note.updated), reverse=True)
        return hits[: max(1, limit)]

    @staticmethod
    def _blend(query_vec: list[float], notes: list[Note], lexical: list[float]) -> list[float]:
        """Lexical score lifted by embedding similarity, where it is real.

        Measured on gemini-embedding-001 at 256 dims: unrelated pairs sit at
        cosine 0.58-0.71, true matches 0.76-0.83. So a floor, not zero, is
        the origin; and with three or more notes a match must also stand
        clear of the query's median, or "close the window" would recall
        whichever note happened to be nearest the noise.
        """
        cosines = [_cosine(query_vec, n.vec) if n.vec else None for n in notes]
        known = sorted(c for c in cosines if c is not None)
        median = known[len(known) // 2] if len(known) >= 3 else None
        floor = config.SEMANTIC_EMBED_FLOOR
        out = []
        for cos, lex in zip(cosines, lexical):
            if cos is None or (median is not None and cos - median < 0.04):
                out.append(lex)
                continue
            sem = min(1.0, max(0.0, (cos - floor) / 0.08))
            out.append(max(lex, 0.4 * lex + 0.6 * sem))
        return out

    def _backfill(self, batch: int = 32) -> None:
        """Embed notes that have no vector yet. Best effort, outside the lock."""
        with self._lock:
            missing = [(n.id, n.text) for n in self._notes if not n.vec][:batch]
        if not missing or self.embedder is None:
            return
        vectors = self.embedder([text for _id, text in missing], "RETRIEVAL_DOCUMENT")
        if not vectors:
            return
        with self._lock:
            by_id = {n.id: n for n in self._notes}
            for (ident, text), vec in zip(missing, vectors):
                note = by_id.get(ident)
                # Skipped if the note changed while the request was out.
                if note is not None and note.text == text and len(vec) == self.dim:
                    note.vec = vec
            self.save()

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Symmetric lexical similarity of two texts, 0-1."""
        ta, tb = set(tokens(a)), set(tokens(b))
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / math.sqrt(len(ta) * len(tb))

    # -- writes ------------------------------------------------------------
    def _embed(self, text: str) -> list[float]:
        if self.embedder is None:
            return []
        vectors = self.embedder([text], "RETRIEVAL_DOCUMENT")
        if vectors and len(vectors[0]) == self.dim:
            return vectors[0]
        return []

    def add(self, text: str) -> tuple[Note | None, str]:
        """Store a note. Returns (note, 'added' | 'updated'); ('', None) = not saved.

        A near-duplicate of an existing note replaces it: "key is under the
        pot" then "key is under the blue pot" is one fact, updated.
        """
        clean = _clean(text)
        if not self.enabled or not clean:
            return None, ""
        vec = self._embed(clean)
        now = time.time()
        with self._lock:
            twin = max(self._notes, key=lambda n: self.similarity(n.text, clean), default=None)
            if twin is not None and self.similarity(twin.text, clean) >= config.SEMANTIC_MERGE_SIM:
                twin.text, twin.updated, twin.vec = clean, now, vec
                note, verb = twin, "updated"
            else:
                note = Note(uuid.uuid4().hex[:8], clean, now, now, vec=vec)
                self._notes.append(note)
                verb = "added"
            self._prune(keep=note)
            self._reindex()
            if not self.save():
                return None, ""
            return note, verb

    def update(self, note_id: str, text: str) -> Note | None:
        clean = _clean(text)
        if not clean:
            return None
        vec = self._embed(clean)
        with self._lock:
            note = self.get(note_id)
            if note is None:
                return None
            note.text, note.updated, note.vec = clean, time.time(), vec
            self._reindex()
            return note if self.save() else None

    def delete(self, ids: list[str]) -> list[Note]:
        """Drop notes by id. Archived first, so a wrong delete is recoverable."""
        wanted = {str(i).strip() for i in ids if str(i).strip()}
        with self._lock:
            gone = [n for n in self._notes if n.id in wanted]
            if not gone:
                return []
            self._archive(gone, "deleted")
            self._notes = [n for n in self._notes if n.id not in wanted]
            self._reindex()
            self.save()
            return gone

    def wipe(self) -> int:
        """Every note gone. A `.bak` of the file is written first."""
        with self._lock:
            count = len(self._notes)
            if not count:
                return 0
            write_json(
                self.path.with_name(self.path.name + ".bak"),
                {"version": SCHEMA_VERSION, "notes": [n.to_row() for n in self._notes]},
            )
            self._notes = []
            self._reindex()
            self.save()
            return count

    def touch(self, hits: list[Hit]) -> None:
        """Count an explicit recall, so used notes outlive unused ones."""
        if not hits:
            return
        with self._lock:
            for hit in hits:
                hit.note.hits += 1
            self.save()

    # -- pruning -----------------------------------------------------------
    @staticmethod
    def _retention(note: Note, now: float) -> float:
        age_days = max(0.0, now - note.updated) / 86400.0
        return (1.0 + math.log1p(note.hits)) * math.exp(-age_days / 120.0)

    def _prune(self, keep: Note | None = None) -> None:
        """Keep the store under its cap. Caller holds the lock.

        Pass 1 consolidates: the weakest notes fold into their nearest
        neighbour (texts joined, clipped), so a topic shrinks instead of
        vanishing. Pass 2 archives whatever is still over. `keep` - the note
        just written - is never the one folded away or evicted.
        """
        cap = max(10, config.SEMANTIC_MAX_NOTES)
        if len(self._notes) <= cap:
            return
        now = time.time()
        folded: list[Note] = []
        for note in sorted(self._notes, key=lambda n: self._retention(n, now)):
            if len(self._notes) - len(folded) <= cap:
                break
            if note is keep or note in folded:
                continue
            others = [o for o in self._notes if o is not note and o not in folded]
            best = max(others, key=lambda o: self.similarity(o.text, note.text), default=None)
            if best is None or self.similarity(best.text, note.text) < 0.35:
                continue
            best.text = _clean(f"{best.text}; {note.text}")
            best.updated = max(best.updated, note.updated)
            best.hits += note.hits
            best.vec = []
            folded.append(note)
        if folded:
            self._archive(folded, "merged")
            self._notes = [n for n in self._notes if n not in folded]
        over = len(self._notes) - cap
        if over > 0:
            pool = [n for n in self._notes if n is not keep]
            evicted = sorted(pool, key=lambda n: self._retention(n, now))[:over]
            self._archive(evicted, "pruned")
            self._notes = [n for n in self._notes if n not in evicted]

    def _archive(self, notes: list[Note], why: str) -> None:
        """Append to the archive. Rotated at ~512KB; never read on boot."""
        try:
            path = self.archive_path
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 512 * 1024:
                path.replace(path.with_name(path.name + ".1"))
            with path.open("a", encoding="utf-8") as handle:
                for note in notes:
                    row = {k: v for k, v in note.to_row().items() if k != "vec"}
                    handle.write(json.dumps({**row, "why": why, "at": time.time()}) + "\n")
        except OSError as exc:
            log.warning("Semantic memory archive write failed: %s", exc)

    # -- model context -----------------------------------------------------
    def context(self) -> str:
        """One line for the standing session context, or ''."""
        if not self.enabled or not self._notes:
            return ""
        return f"You keep {len(self._notes)} long-term notes; recall_fact searches them."

    def relevant(self, utterance: str) -> str:
        """Notes worth showing for this utterance, or ''. Lexical: no network."""
        if not self.enabled or not self._notes:
            return ""
        hits = self.search(
            utterance,
            limit=config.SEMANTIC_CONTEXT_ITEMS,
            min_score=config.SEMANTIC_CONTEXT_MIN_SCORE,
            use_embeddings=False,
        )
        lines: list[str] = []
        budget = max(80, config.SEMANTIC_CONTEXT_CHARS)
        for hit in hits:
            line = f"- {hit.note.text}"
            if len(line) > budget:
                break
            budget -= len(line)
            lines.append(line)
        return "\n".join(lines)


_store: SemanticMemory | None = None
_store_lock = threading.Lock()


def get_semantic_memory() -> SemanticMemory:
    """Process-wide store. Rebuilt when `config.SEMANTIC_FILE` changes."""
    global _store
    with _store_lock:
        if _store is None or _store.path != config.SEMANTIC_FILE:
            _store = SemanticMemory()
        return _store


__all__ = [
    "GeminiEmbedder",
    "Hit",
    "Note",
    "SemanticMemory",
    "get_semantic_memory",
    "stem",
    "tokens",
    "validate_row",
]
