"""The memory store (ARCHITECTURE §3.9): episodic, semantic and procedural records, scoped.

Every record belongs to a tenant and instance and to one memory scope: a ``contact`` (a clinic
patient), the ``instance`` or an ``agent``. Nothing crosses scopes: a fact about one patient
is never retrieved for another.

- **episodic**: what happened, one record per conversation (updated as it goes), with the
  spec's ``episodic_ttl``;
- **semantic**: what is true, by ``key``; a new value supersedes the old one, and a
  contradiction is flagged to a person;
- **procedural**: skills (how to do something). Proposed by agents, counted each time they are
  proposed again, and active only after a person approves (``skill_promotion``).

Content is stored as the model saw it (PII tokens, redacted secrets), never raw.
"""

from __future__ import annotations

import math
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from ..core.scope import Scope
from ..store.db import Database, Row

Layer = Literal["episodic", "semantic", "procedural"]
_WORD = re.compile(r"[\wáéíóúñü<>]+", re.IGNORECASE)


@dataclass(frozen=True)
class MemoryScope:
    kind: Literal["contact", "instance", "agent"]
    key: str  # contact key, agent name, or "" for the instance


@dataclass(frozen=True)
class Memory:
    id: str
    layer: str
    key: str
    content: str
    status: str
    version: int
    successes: int
    source: str | None
    updated_at: float
    scope: MemoryScope

    @classmethod
    def from_row(cls, row: Row) -> Memory:
        return cls(
            row["id"], row["layer"], row["key"], row["content"], row["status"],
            int(row["version"]), int(row["successes"]), row["source"], float(row["updated_at"]),
            MemoryScope(row["scope_kind"], row["scope_key"]),
        )  # fmt: skip


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text) if len(w) > 2]


class MemoryStore:
    def __init__(self, db: Database, scope: Scope) -> None:
        self.db = db
        self.scope = scope

    def _where(self, ms: MemoryScope) -> tuple[str, list[Any]]:
        return (
            "tenant_id = ? AND instance_id = ? AND scope_kind = ? AND scope_key = ?",
            [self.scope.tenant_id, self.scope.instance_id, ms.kind, ms.key],
        )

    async def _insert(
        self, ms: MemoryScope, layer: Layer, key: str, content: str, *, status: str,
        version: int = 1, source: str | None = None, ttl_s: float | None = None,
    ) -> str:  # fmt: skip
        now = time.time()
        mem_id = uuid.uuid4().hex[:12]
        await self.db.execute(
            "INSERT INTO memories (id, tenant_id, instance_id, scope_kind, scope_key, layer, key,"
            " content, status, version, successes, source, created_at, updated_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (mem_id, self.scope.tenant_id, self.scope.instance_id, ms.kind, ms.key, layer, key,
             content, status, version, source, now, now, now + ttl_s if ttl_s else None),
        )  # fmt: skip
        return mem_id

    async def active(self, ms: MemoryScope, layer: Layer | None = None) -> list[Memory]:
        where, params = self._where(ms)
        sql = f"SELECT * FROM memories WHERE {where} AND status = 'active'"
        sql += " AND (expires_at IS NULL OR expires_at > ?)"
        params.append(time.time())
        if layer:
            sql += " AND layer = ?"
            params.append(layer)
        rows = await self.db.fetchall(sql + " ORDER BY updated_at DESC", params)
        return [Memory.from_row(r) for r in rows]

    async def get(self, mem_id: str) -> Memory | None:
        row = await self.db.fetchone(
            "SELECT * FROM memories WHERE id = ? AND tenant_id = ? AND instance_id = ?",
            (mem_id, self.scope.tenant_id, self.scope.instance_id),
        )
        return Memory.from_row(row) if row else None

    # --- episodic ----------------------------------------------------------------------

    async def record_episode(
        self, ms: MemoryScope, session_id: str, summary: str, ttl_s: float | None
    ) -> None:
        where, params = self._where(ms)
        row = await self.db.fetchone(
            f"SELECT id FROM memories WHERE {where} AND layer = 'episodic' AND key = ?",
            [*params, session_id],
        )
        if row is None:
            await self._insert(ms, "episodic", session_id, summary, status="active",
                               source=session_id, ttl_s=ttl_s)  # fmt: skip
        else:
            now = time.time()
            await self.db.execute(
                "UPDATE memories SET content = ?, updated_at = ?, expires_at = ? WHERE id = ?",
                (summary[-4000:], now, now + ttl_s if ttl_s else None, row["id"]),
            )

    # --- semantic ----------------------------------------------------------------------

    async def remember(
        self, ms: MemoryScope, key: str, value: str, source: str | None
    ) -> tuple[str, Memory | None]:
        """Store a fact. Returns (id, the fact it contradicted, if any)."""
        key = " ".join(key.lower().split())[:120]
        where, params = self._where(ms)
        row = await self.db.fetchone(
            f"SELECT * FROM memories WHERE {where} AND layer = 'semantic' AND key = ?"
            " AND status = 'active'",
            [*params, key],
        )
        previous = Memory.from_row(row) if row else None
        if previous is not None and previous.content.strip().lower() == value.strip().lower():
            return previous.id, None
        if previous is not None:
            await self.db.execute(
                "UPDATE memories SET status = 'superseded', updated_at = ? WHERE id = ?",
                (time.time(), previous.id),
            )
        mem_id = await self._insert(
            ms, "semantic", key, value, status="active",
            version=previous.version + 1 if previous else 1, source=source,
        )  # fmt: skip
        return mem_id, previous

    async def restore(self, mem_id: str) -> None:
        """Undo a supersession: the old fact is active again, the newer one superseded."""
        old = await self.get(mem_id)
        if old is None:
            return
        where, params = self._where(old.scope)  # only this contact's (or agent's) fact
        await self.db.execute(
            f"UPDATE memories SET status = 'superseded' WHERE {where}"
            " AND layer = 'semantic' AND key = ? AND status = 'active'",
            [*params, old.key],
        )
        await self.db.execute("UPDATE memories SET status = 'active' WHERE id = ?", (mem_id,))

    # --- procedural --------------------------------------------------------------------

    async def propose_skill(
        self, ms: MemoryScope, name: str, steps: str, source: str | None
    ) -> Memory:
        name = " ".join(name.lower().split())[:120]
        where, params = self._where(ms)
        row = await self.db.fetchone(
            f"SELECT * FROM memories WHERE {where} AND layer = 'procedural' AND key = ?"
            " AND status IN ('proposed', 'pending', 'active') ORDER BY version DESC LIMIT 1",
            [*params, name],
        )
        if row is None:
            mem_id = await self._insert(
                ms, "procedural", name, steps, status="proposed", source=source
            )
        else:
            mem_id = row["id"]
            await self.db.execute(
                "UPDATE memories SET successes = successes + 1, content = ?, updated_at = ?"
                " WHERE id = ?",
                (steps if row["status"] != "active" else row["content"], time.time(), mem_id),
            )
        skill = await self.get(mem_id)
        assert skill is not None
        return skill

    async def set_status(self, mem_id: str, status: str) -> None:
        await self.db.execute(
            "UPDATE memories SET status = ?, updated_at = ? WHERE id = ? AND tenant_id = ?"
            " AND instance_id = ?",
            (status, time.time(), mem_id, self.scope.tenant_id, self.scope.instance_id),
        )

    # --- retrieval and forgetting ------------------------------------------------------

    async def search(
        self, ms: MemoryScope, query: str, limit: int = 5
    ) -> list[tuple[Memory, float]]:
        """Active memories ranked by overlap with the query (idf-weighted), best first."""
        memories = await self.active(ms)
        docs = [(m, _words(f"{m.key} {m.content}")) for m in memories]
        if not docs:
            return []
        df: Counter[str] = Counter()
        for _, words in docs:
            df.update(set(words))
        wanted = set(_words(query))
        n = len(docs)

        def idf(word: str) -> float:
            return math.log(1 + n / (1 + df[word]))

        total = sum(idf(w) for w in wanted) or 1.0
        scored = [(m, sum(idf(w) for w in wanted & set(words)) / total) for m, words in docs]
        return sorted([s for s in scored if s[1] > 0], key=lambda s: -s[1])[:limit]

    async def forget(self, ms: MemoryScope) -> int:
        """Delete everything remembered in a scope (a contact's right to be forgotten)."""
        where, params = self._where(ms)
        return await self.db.execute(f"DELETE FROM memories WHERE {where}", params)

    async def purge_expired(self) -> int:
        return await self.db.execute(
            "DELETE FROM memories WHERE tenant_id = ? AND instance_id = ?"
            " AND expires_at IS NOT NULL AND expires_at < ?",
            (self.scope.tenant_id, self.scope.instance_id, time.time()),
        )
