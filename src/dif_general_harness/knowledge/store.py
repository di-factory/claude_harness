"""Knowledge corpora: ingestion, sync and retrieval (ARCHITECTURE §3.19).

Documents and their chunks live in the instance's database, scoped to tenant, instance and
corpus. A document is identified by its ``uri``; putting it again with the same content is a
no-op, with new content its chunks are replaced (and its ``version`` goes up), and deleting
it removes its chunks from the index at once.

- **Sources:** ``file`` sources (a file or a folder, read recursively), S3 prefixes, Google
  Drive folders and web pages (``sources.py``) are synced here, on open and on the corpus's
  ``sync.schedule``; an entry is re-read only when its version changed. With
  ``sync.on_delete: propagate`` (the default) what disappeared from a source that was
  listed completely is removed from the index. Anything else (SharePoint, Notion...) is fed
  through the admin API (``PUT /admin/knowledge/{corpus}/documents``).
- **Retrieval:** keyword scoring over the corpus. ``score`` is the share of the query's
  information (idf-weighted terms, stop words removed, accents folded) that a chunk
  covers, from 0 to 1, so ``retrieval.min_score`` means the same thing for every corpus;
  BM25 orders chunks with the same coverage. Nothing above ``min_score`` means "not found".
- **Hybrid:** with ``mode: hybrid`` and an ``embedding`` model role, every chunk also gets a
  vector. A search ranks by both lists (reciprocal rank fusion), so a passage that says the
  same thing in other words is found too; it qualifies by keyword coverage (``min_score``)
  or by cosine similarity (``min_similarity``, default 0.5), and its ``score`` is the
  higher of the two. Vectors are compared in-process (exact, fine to about 100k chunks per
  corpus); a failed embedding call falls back to keyword scoring, never to "not found".
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
import time
import unicodedata
import uuid
from array import array
from base64 import b64decode, b64encode
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.scope import Scope
from ..store.db import Database
from .chunk import chunk, format_of, title_of
from .sources import normalize

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
ID_LETTERS = "abcdefghjkmnpqrstuvwxyz"  # chunk ids: letters only, so no PII detector (phone,
# CURP, RFC, account) ever mistakes a citation marker for personal data and tokenizes it


def chunk_id() -> str:
    return "".join(secrets.choice(ID_LETTERS) for _ in range(10))


K1, B = 1.2, 0.75
RRF_K = 60
EMBED_BATCH = 64
DEFAULT_MIN_SIMILARITY = 0.5
# texts -> (vectors, model); a source spec -> a remote source (both set by the instance)
Embedder = Callable[[list[str]], Awaitable[tuple[list[list[float]], str]]]
SourceBuilder = Callable[[str, dict[str, Any]], Any]


def pack(vector: list[float]) -> str:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return b64encode(array("f", [x / norm for x in vector]).tobytes()).decode()


def unpack(text: str) -> array[float]:
    out = array("f")
    out.frombytes(b64decode(text))
    return out


def cosine(a: array[float], b: array[float]) -> float:
    """Of unit vectors: their dot product."""
    return sum(x * y for x, y in zip(a, b, strict=False))


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
    embedded: int = 0  # chunks that got a vector
    embedding_error: str = ""


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
        self.embedder: Embedder | None = None
        self.sources: SourceBuilder | None = None
        self._vectors: dict[str, dict[str, array[float]]] = {}

    def retrieval(self, corpus: str) -> dict[str, Any]:
        return dict(self.corpora[corpus].get("retrieval") or {})

    def hybrid(self, corpus: str) -> bool:
        return self.embedder is not None and self.retrieval(corpus).get("mode") == "hybrid"

    def _check(self, corpus: str) -> None:
        if corpus not in self.corpora:
            raise KeyError(f"unknown corpus {corpus!r}; defined: {sorted(self.corpora)}")

    # --- documents ---------------------------------------------------------------------

    async def put(
        self, corpus: str, uri: str, text: str, *, fmt: str = "markdown",
        title: str | None = None, origin: str = "api", source_version: str | None = None,
        embed: bool = True,
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
            if source_version is not None:
                await self.db.execute(
                    "UPDATE knowledge_docs SET source_version = ? WHERE id = ?",
                    (source_version, row["id"]),
                )
            return str(row["id"]), "unchanged"
        chunks = chunk(text, fmt, mode)
        now = time.time()
        async with self.db.transaction() as conn:
            if row is None:
                doc_id = uuid.uuid4().hex[:12]
                await conn.execute(
                    "INSERT INTO knowledge_docs (id, tenant_id, instance_id, corpus, uri, origin,"
                    " title, hash, version, updated_at, source_version)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (doc_id, *scope, uri, origin, title, digest, now, source_version),
                )
            else:
                doc_id = str(row["id"])
                await conn.execute("DELETE FROM knowledge_chunks WHERE doc_id = ?", (doc_id,))
                await conn.execute("DELETE FROM knowledge_vectors WHERE doc_id = ?", (doc_id,))
                await conn.execute(
                    "UPDATE knowledge_docs SET title = ?, hash = ?, version = ?, updated_at = ?,"
                    " origin = ?, source_version = ? WHERE id = ?",
                    (title, digest, int(row["version"]) + 1, now, origin, source_version, doc_id),
                )
            for n, c in enumerate(chunks):
                await conn.execute(
                    "INSERT INTO knowledge_chunks (id, doc_id, tenant_id, instance_id, corpus, ord,"
                    " section, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (chunk_id(), doc_id, *scope, n, c.section, c.text),
                )
        self._indexes.pop(corpus, None)
        self._vectors.pop(corpus, None)
        if embed and self.hybrid(corpus):
            await self.embed_missing(corpus)
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
            await conn.execute("DELETE FROM knowledge_vectors WHERE doc_id = ?", (row["id"],))
            await conn.execute("DELETE FROM knowledge_docs WHERE id = ?", (row["id"],))
        self._indexes.pop(corpus, None)
        self._vectors.pop(corpus, None)
        return True

    # --- vectors -----------------------------------------------------------------------

    async def embed_missing(self, corpus: str) -> int:
        """Give every chunk of the corpus a vector from the current embedding model (chunks
        without one, and all of them after a model change). Returns how many were embedded;
        raises on a failed embedding call (the chunks stay keyword-searchable)."""
        if self.embedder is None:
            return 0
        scope = (self.scope.tenant_id, self.scope.instance_id, corpus)
        rows = await self.db.fetchall(
            "SELECT c.id, c.doc_id, c.section, c.text, d.title FROM knowledge_chunks c"
            " JOIN knowledge_docs d ON d.id = c.doc_id"
            " LEFT JOIN knowledge_vectors v ON v.chunk_id = c.id"
            " WHERE c.tenant_id = ? AND c.instance_id = ? AND c.corpus = ? AND v.chunk_id IS NULL"
            " ORDER BY c.doc_id, c.ord",
            scope,
        )
        done = 0
        for start in range(0, len(rows), EMBED_BATCH):
            batch = rows[start : start + EMBED_BATCH]
            texts = [f"{r['title']}\n{r['section']}\n{r['text']}".strip() for r in batch]
            vectors, model = await self.embedder(texts)
            if len(vectors) != len(batch):
                raise ValueError("the embedding model returned the wrong number of vectors")
            async with self.db.transaction() as conn:
                await conn.execute(  # a new model: the old vectors are not comparable
                    "DELETE FROM knowledge_vectors WHERE tenant_id = ? AND instance_id = ?"
                    " AND corpus = ? AND model <> ?",
                    (*scope, model),
                )
                for r, vector in zip(batch, vectors, strict=True):
                    await conn.execute(
                        "INSERT INTO knowledge_vectors (chunk_id, doc_id, tenant_id, instance_id,"
                        " corpus, model, vector) VALUES (?, ?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT DO NOTHING",
                        (r["id"], r["doc_id"], *scope, model, pack(vector)),
                    )
            done += len(batch)
        if done:
            self._vectors.pop(corpus, None)
        return done

    async def _vector_index(self, corpus: str) -> dict[str, array[float]]:
        cached = self._vectors.get(corpus)
        if cached is not None:
            return cached
        rows = await self.db.fetchall(
            "SELECT chunk_id, vector FROM knowledge_vectors"
            " WHERE tenant_id = ? AND instance_id = ? AND corpus = ?",
            (self.scope.tenant_id, self.scope.instance_id, corpus),
        )
        index = {str(r["chunk_id"]): unpack(str(r["vector"])) for r in rows}
        self._vectors[corpus] = index
        return index

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
        """Bring the corpus in line with its sources."""
        self._check(corpus)
        spec = self.corpora[corpus]
        report = SyncReport()
        files: list[Path] = []
        complete = True  # every file source was listed: safe to propagate deletions
        listed: dict[str, set[str]] = {}  # remote origin -> uris it listed (complete)
        sources = spec.get("sources") or []
        for raw in sources if isinstance(sources, list) else [sources]:
            src = normalize(raw)
            path = _file_path(src)
            if path is None:
                if src is not None and self.sources is not None:
                    await self._sync_remote(corpus, src, report, listed)
                else:
                    report.unavailable.append(_describe(raw))
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
                text = _document_text(path)  # PDF, DOCX, XLSX: their text
                if text is None:
                    report.skipped.append(str(path))
                    continue
                fmt = "text"
            else:
                text = path.read_text(encoding="utf-8", errors="replace")
            uri = f"file:{path.resolve()}"
            seen.add(uri)
            _, outcome = await self.put(corpus, uri, text, fmt=fmt, origin="file", embed=False)
            setattr(report, outcome, getattr(report, outcome) + 1)
        on_delete = str((spec.get("sync") or {}).get("on_delete") or "propagate")
        if on_delete == "propagate":
            for doc in await self.documents(corpus):
                gone = (doc["origin"] == "file" and complete and doc["uri"] not in seen) or (
                    doc["origin"] in listed and doc["uri"] not in listed[doc["origin"]]
                )
                if gone:
                    await self.delete(corpus, doc["uri"])
                    report.removed += 1
        if self.hybrid(corpus):
            try:
                report.embedded = await self.embed_missing(corpus)
            except Exception as exc:  # keyword search still works; the next sync retries
                report.embedding_error = f"{type(exc).__name__}: {exc}"
        return report

    async def _sync_remote(
        self, corpus: str, src: dict[str, Any], report: SyncReport, listed: dict[str, set[str]]
    ) -> None:
        assert self.sources is not None
        try:
            source = self.sources(corpus, src)
            entries = await source.entries()
        except Exception as exc:  # a source that is down removes nothing
            report.unavailable.append(f"{_describe(src)} ({exc})")
            return
        origin = f"sync:{source.label}"
        uris: set[str] = set()
        known = {d["uri"]: d for d in await self._versions(corpus)}
        for entry in entries:
            uris.add(entry.uri)
            doc = known.get(entry.uri)
            if doc is not None and doc["source_version"] == entry.version and entry.version:
                report.unchanged += 1
                continue
            try:
                text = await source.read(entry)
            except Exception as exc:
                report.skipped.append(f"{entry.uri} ({exc})")  # kept as indexed
                continue
            if text is None:
                report.skipped.append(entry.uri)
                continue
            _, outcome = await self.put(
                corpus, entry.uri, text.text, fmt=text.fmt, title=entry.title, origin=origin,
                source_version=entry.version, embed=False,
            )  # fmt: skip
            setattr(report, outcome, getattr(report, outcome) + 1)
        listed[origin] = listed.get(origin, set()) | uris

    async def _versions(self, corpus: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT uri, origin, source_version FROM knowledge_docs"
            " WHERE tenant_id = ? AND instance_id = ? AND corpus = ?",
            (self.scope.tenant_id, self.scope.instance_id, corpus),
        )
        return [dict(r) for r in rows]

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
            scored.append((coverage, bm25, row))
        scored.sort(key=lambda s: (-s[0], -s[1]))
        if self.hybrid(corpus):
            fused = await self._fuse(corpus, query, index, scored, floor, settings)
            if fused is not None:
                return fused[:top_k]
        return [
            Hit(r["id"], corpus, r["uri"], r["title"], r["section"], r["text"], round(cov, 3))
            for cov, _, r in scored
            if cov >= floor
        ][:top_k]

    async def _fuse(
        self, corpus: str, query: str, index: _Index,
        keyword: list[tuple[float, float, dict[str, Any]]], floor: float,
        settings: dict[str, Any],
    ) -> list[Hit] | None:  # fmt: skip
        """Keyword and vector rankings fused (RRF); None when the query cannot be embedded."""
        assert self.embedder is not None
        try:
            [query_vector], _ = await self.embedder([query])
        except Exception:
            return None  # the embedding model is down: keyword scoring still answers
        q = unpack(pack(query_vector))
        vectors = await self._vector_index(corpus)
        rows = {r["id"]: r for r in index.rows}
        similar = sorted(
            ((cosine(q, v), cid) for cid, v in vectors.items() if cid in rows), reverse=True
        )
        min_sim = float(settings.get("min_similarity") or DEFAULT_MIN_SIMILARITY)
        coverage = {r["id"]: cov for cov, _, r in keyword}
        similarity = {cid: sim for sim, cid in similar}
        rrf: dict[str, float] = {}
        for rank, (_, _, r) in enumerate(keyword):
            rrf[r["id"]] = rrf.get(r["id"], 0.0) + 1 / (RRF_K + rank + 1)
        for rank, (_, cid) in enumerate(similar):
            rrf[cid] = rrf.get(cid, 0.0) + 1 / (RRF_K + rank + 1)
        hits = []
        for cid in sorted(rrf, key=lambda c: -rrf[c]):
            cov, sim = coverage.get(cid, 0.0), similarity.get(cid, 0.0)
            if cov < floor and sim < min_sim:
                continue
            r = rows[cid]
            score = round(max(cov, sim), 3)
            hits.append(Hit(cid, corpus, r["uri"], r["title"], r["section"], r["text"], score))
        return hits


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


def _document_text(path: Path) -> str | None:
    """The text of a PDF (text layer), DOCX or XLSX; None for anything else, for scans and
    for unreadable files (reported as skipped, never indexed half-read)."""
    from ..documents.extract import DocumentError, extract
    from ..documents.extract import format_of as document_format

    if document_format(path.name) not in ("pdf", "docx", "xlsx"):
        return None
    try:
        found = extract(path.read_bytes(), path.name)
    except DocumentError:
        return None
    if found.needs_ocr or not found.text.strip():
        return None
    return found.text
