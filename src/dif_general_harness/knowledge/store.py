"""Knowledge corpora: ingestion, sync and retrieval (ARCHITECTURE §3.19).

Documents and their chunks live in the instance's database, scoped to tenant, instance and
corpus. A document is identified by its ``uri``; putting it again with the same content is a
no-op, with new content its chunks are replaced (and its ``version`` goes up), and deleting
it removes its chunks from the index at once.

- **Sources:** ``file`` sources (a file or a folder, read recursively) are synced here, on
  open and on the corpus's ``sync.schedule``. With ``sync.on_delete: propagate`` (the default)
  a file that disappeared is removed from the index. Other sources (Drive, S3, SharePoint,
  Notion) are fed through the admin API (``PUT /admin/knowledge/{corpus}/documents``) by
  whatever syncs them; they are reported as not synced here.
- **Retrieval:** keyword scoring over the corpus. ``score`` is the share of the query's
  information (idf-weighted terms, stop words removed, accents folded) that a chunk
  covers, from 0 to 1, so ``retrieval.min_score`` means the same thing for every corpus;
  BM25 orders chunks with the same coverage. Nothing above ``min_score`` means "not found".
  ``mode: hybrid`` adds embeddings when an embedding model is configured (production
  Postgres with pgvector); until then it is keyword scoring.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.scope import Scope
from ..store.db import Database
from .chunk import chunk, format_of, title_of

_STOP = """a about after all also am an and any are as at be been but by can could did do does
    for from had has have how i if in into is it its me my no not of on or our please should
    so than that the their them then there these they this to us was we were what when where
    which who why will with would you your al como con cual cuales cuando de del donde el en
    es esta estan este esto hay la las le les lo los me mi mis mas no nos o para pero por
    puede pueden puedo que quien se ser si sin son su sus te tiene tienen tu tus un una unas
    uno unos y ya much many get tell know want need like cuanto cuanta cuantos cuantas
    quiero necesito saber"""
STOP_WORDS = frozenset(_STOP.split())
_TERM = re.compile(r"[a-z0-9]+")
K1, B = 1.2, 0.75


def terms(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(c for c in folded if not unicodedata.combining(c))
    out = []
    for word in _TERM.findall(plain):
        if word in STOP_WORDS or len(word) < 2:
            continue
        if len(word) > 4 and word.endswith("es"):
            word = word[:-2]
        elif len(word) > 3 and word.endswith("s"):
            word = word[:-1]
        out.append(word[:7])  # a crude stem: "payments" and "payment" meet
    return out


@dataclass(frozen=True)
class Hit:
    id: str
    corpus: str
    uri: str
    title: str
    section: str
    text: str
    score: float


@dataclass
class SyncReport:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    skipped: list[str] = field(default_factory=list)  # files in a format not read here
    unavailable: list[str] = field(default_factory=list)  # sources not synced here


@dataclass
class _Index:
    stamp: tuple[Any, ...]
    rows: list[dict[str, Any]]
    tfs: list[Counter[str]]
    lengths: list[int]
    df: Counter[str]


class KnowledgeBase:
    def __init__(self, db: Database, scope: Scope, corpora: dict[str, dict[str, Any]]) -> None:
        self.db = db
        self.scope = scope
        self.corpora = corpora
        self._indexes: dict[str, _Index] = {}

    def retrieval(self, corpus: str) -> dict[str, Any]:
        return dict(self.corpora[corpus].get("retrieval") or {})

    def _check(self, corpus: str) -> None:
        if corpus not in self.corpora:
            raise KeyError(f"unknown corpus {corpus!r}; defined: {sorted(self.corpora)}")

    # --- documents ---------------------------------------------------------------------

    async def put(
        self, corpus: str, uri: str, text: str, *, fmt: str = "markdown",
        title: str | None = None, origin: str = "api",
    ) -> tuple[str, str]:  # fmt: skip
        """Add or replace a document. Returns (doc id, "added" | "updated" | "unchanged")."""
        self._check(corpus)
        mode = str(self.corpora[corpus].get("chunking") or "layout")
        digest = hashlib.sha256(f"{mode}\0{fmt}\0{text}".encode()).hexdigest()
        title = title or title_of(text, fmt, uri.rsplit("/", 1)[-1])
        scope = (self.scope.tenant_id, self.scope.instance_id, corpus)
        row = await self.db.fetchone(
            "SELECT id, hash, version FROM knowledge_docs"
            " WHERE tenant_id = ? AND instance_id = ? AND corpus = ? AND uri = ?",
            (*scope, uri),
        )
        if row is not None and row["hash"] == digest:
            return str(row["id"]), "unchanged"
        chunks = chunk(text, fmt, mode)
        now = time.time()
        async with self.db.transaction() as conn:
            if row is None:
                doc_id = uuid.uuid4().hex[:12]
                await conn.execute(
                    "INSERT INTO knowledge_docs (id, tenant_id, instance_id, corpus, uri, origin,"
                    " title, hash, version, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (doc_id, *scope, uri, origin, title, digest, now),
                )
            else:
                doc_id = str(row["id"])
                await conn.execute("DELETE FROM knowledge_chunks WHERE doc_id = ?", (doc_id,))
                await conn.execute(
                    "UPDATE knowledge_docs SET title = ?, hash = ?, version = ?, updated_at = ?,"
                    " origin = ? WHERE id = ?",
                    (title, digest, int(row["version"]) + 1, now, origin, doc_id),
                )
            for n, c in enumerate(chunks):
                await conn.execute(
                    "INSERT INTO knowledge_chunks (id, doc_id, tenant_id, instance_id, corpus, ord,"
                    " section, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex[:10], doc_id, *scope, n, c.section, c.text),
                )
        self._indexes.pop(corpus, None)
        return doc_id, "added" if row is None else "updated"

    async def delete(self, corpus: str, uri: str) -> bool:
        self._check(corpus)
        row = await self.db.fetchone(
            "SELECT id FROM knowledge_docs"
            " WHERE tenant_id = ? AND instance_id = ? AND corpus = ? AND uri = ?",
            (self.scope.tenant_id, self.scope.instance_id, corpus, uri),
        )
        if row is None:
            return False
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM knowledge_chunks WHERE doc_id = ?", (row["id"],))
            await conn.execute("DELETE FROM knowledge_docs WHERE id = ?", (row["id"],))
        self._indexes.pop(corpus, None)
        return True

    async def documents(self, corpus: str) -> list[dict[str, Any]]:
        self._check(corpus)
        rows = await self.db.fetchall(
            "SELECT d.id, d.uri, d.origin, d.title, d.version, d.updated_at,"
            " (SELECT COUNT(*) FROM knowledge_chunks c WHERE c.doc_id = d.id) AS chunks"
            " FROM knowledge_docs d WHERE d.tenant_id = ? AND d.instance_id = ? AND d.corpus = ?"
            " ORDER BY d.uri",
            (self.scope.tenant_id, self.scope.instance_id, corpus),
        )
        return [dict(r) for r in rows]

    async def chunks(self, ids: list[str]) -> dict[str, Hit]:
        """Chunks by id (for citations), with their document's title."""
        if not ids:
            return {}
        marks = ", ".join("?" for _ in ids)
        rows = await self.db.fetchall(
            "SELECT c.id, c.corpus, c.section, c.text, d.uri, d.title FROM knowledge_chunks c"
            f" JOIN knowledge_docs d ON d.id = c.doc_id WHERE c.id IN ({marks})"
            " AND c.tenant_id = ? AND c.instance_id = ?",
            (*ids, self.scope.tenant_id, self.scope.instance_id),
        )
        return {
            r["id"]: Hit(r["id"], r["corpus"], r["uri"], r["title"], r["section"], r["text"], 1.0)
            for r in rows
        }

    # --- sync --------------------------------------------------------------------------

    async def sync(self, corpus: str) -> SyncReport:
        """Bring the corpus in line with its file sources."""
        self._check(corpus)
        spec = self.corpora[corpus]
        report = SyncReport()
        files: list[Path] = []
        complete = True  # every file source was listed: safe to propagate deletions
        sources = spec.get("sources") or []
        for src in sources if isinstance(sources, list) else [sources]:
            path = _file_path(src)
            if path is None:
                report.unavailable.append(_describe(src))
                continue
            if path.is_dir():
                files += sorted(p for p in path.rglob("*") if p.is_file())
            elif path.is_file():
                files.append(path)
            else:
                complete = False
                report.unavailable.append(f"{path} (not found)")
        seen: set[str] = set()
        for path in files:
            fmt = format_of(path)
            if fmt is None:
                report.skipped.append(str(path))
                continue
            uri = f"file:{path.resolve()}"
            seen.add(uri)
            text = path.read_text(encoding="utf-8", errors="replace")
            _, outcome = await self.put(corpus, uri, text, fmt=fmt, origin="file")
            setattr(report, outcome, getattr(report, outcome) + 1)
        on_delete = str((spec.get("sync") or {}).get("on_delete") or "propagate")
        if complete and on_delete == "propagate":
            for doc in await self.documents(corpus):
                if doc["origin"] == "file" and doc["uri"] not in seen:
                    await self.delete(corpus, doc["uri"])
                    report.removed += 1
        return report

    # --- retrieval ---------------------------------------------------------------------

    async def _index(self, corpus: str) -> _Index:
        scope = (self.scope.tenant_id, self.scope.instance_id, corpus)
        stamp_row = await self.db.fetchone(
            "SELECT COUNT(*) AS n, MAX(updated_at) AS t, SUM(version) AS v FROM knowledge_docs"
            " WHERE tenant_id = ? AND instance_id = ? AND corpus = ?",
            scope,
        )
        stamp = tuple(stamp_row.values()) if stamp_row else ()
        cached = self._indexes.get(corpus)
        if cached is not None and cached.stamp == stamp:
            return cached
        rows = await self.db.fetchall(
            "SELECT c.id, c.section, c.text, d.uri, d.title FROM knowledge_chunks c"
            " JOIN knowledge_docs d ON d.id = c.doc_id"
            " WHERE c.tenant_id = ? AND c.instance_id = ? AND c.corpus = ? ORDER BY d.uri, c.ord",
            scope,
        )
        tfs = [Counter(terms(f"{r['title']} {r['section']} {r['text']}")) for r in rows]
        df: Counter[str] = Counter()
        for tf in tfs:
            df.update(tf.keys())
        index = _Index(stamp, [dict(r) for r in rows], tfs, [sum(t.values()) for t in tfs], df)
        self._indexes[corpus] = index
        return index

    async def search(
        self, corpus: str, query: str, top_k: int | None = None, min_score: float | None = None
    ) -> list[Hit]:
        """Chunks scoring at least ``min_score``, best first (empty: not found)."""
        self._check(corpus)
        settings = self.retrieval(corpus)
        top_k = int(top_k or settings.get("top_k") or 5)
        floor = float(min_score if min_score is not None else settings.get("min_score") or 0.0)
        index = await self._index(corpus)
        wanted = set(terms(query))
        n = len(index.rows)
        if not wanted or not n:
            return []
        avgdl = sum(index.lengths) / n or 1.0

        def idf(term: str) -> float:
            return math.log(1 + (n - index.df[term] + 0.5) / (index.df[term] + 0.5))

        total = sum(idf(t) for t in wanted)
        scored = []
        for row, tf, length in zip(index.rows, index.tfs, index.lengths, strict=True):
            matched = [t for t in wanted if tf[t]]
            if not matched:
                continue
            coverage = sum(idf(t) for t in matched) / total
            bm25 = sum(
                idf(t) * tf[t] * (K1 + 1) / (tf[t] + K1 * (1 - B + B * length / avgdl))
                for t in matched
            )
            if coverage >= floor:
                scored.append((coverage, bm25, row))
        scored.sort(key=lambda s: (-s[0], -s[1]))
        return [
            Hit(r["id"], corpus, r["uri"], r["title"], r["section"], r["text"], round(cov, 3))
            for cov, _, r in scored[:top_k]
        ]


def _file_path(src: Any) -> Path | None:
    if isinstance(src, dict) and src.get("type") == "file" and src.get("path"):
        return Path(str(src["path"]))
    if isinstance(src, str):
        raw = src.removeprefix("file://")
        if "://" not in raw and Path(raw).is_absolute():
            return Path(raw)
    return None


def _describe(src: Any) -> str:
    if isinstance(src, dict):
        kind = src.get("type", "?")
        where = next((str(v) for k, v in src.items() if k not in ("type", "auth")), "")
        return f"{kind}:{where}" if where else str(kind)
    return str(src)
