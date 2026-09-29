"""The feedback loop (ARCHITECTURE §3.13): candidate constraints and the approved ones.

Signals from running the solution become **candidate constraints**: a check that failed twice,
a person denying an action with a reason, a run over budget, an escalation a person resolved
with a lesson, a thumbs-down with a comment. A candidate goes to the inbox (kind
``constraint``); a person approves it (possibly rewording it) or rejects it. Approved
constraints are **pinned** in the agent's system prompt, never cut, until a person retires
them. Every learned instruction is approved by a person.

The same signal seen again does not file a second item: it raises the candidate's
``occurrences``. A rejected candidate stays rejected (the same signal is not proposed again).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..core.scope import Scope
from ..store.db import Database, Row

ALL_AGENTS = "*"  # a constraint for every agent of the instance


@dataclass(frozen=True)
class Constraint:
    id: str
    agent: str
    text: str
    status: str  # candidate | active | rejected | retired
    source: str
    evidence: dict[str, Any]
    occurrences: int
    created_at: float
    decided_by: str | None

    @classmethod
    def from_row(cls, row: Row) -> Constraint:
        return cls(
            row["id"], row["agent"], row["text"], row["status"], row["source"],
            json.loads(row["evidence"]), int(row["occurrences"]), float(row["created_at"]),
            row["decided_by"],
        )  # fmt: skip

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "agent": self.agent, "text": self.text, "status": self.status,
            "source": self.source, "evidence": self.evidence, "occurrences": self.occurrences,
            "created_at": self.created_at, "decided_by": self.decided_by,
        }  # fmt: skip


def fingerprint(agent: str, key: str) -> str:
    normal = " ".join(key.lower().split())
    return hashlib.sha256(f"{agent}\0{normal}".encode()).hexdigest()[:24]


class ConstraintStore:
    def __init__(self, db: Database, scope: Scope) -> None:
        self.db = db
        self.scope = scope

    async def propose(
        self, agent: str, text: str, source: str, evidence: dict[str, Any],
        key: str | None = None,
    ) -> tuple[Constraint, bool]:  # fmt: skip
        """File a candidate. Returns (the constraint, True if it is new)."""
        fp = fingerprint(agent, key or text)
        row = await self.db.fetchone(
            "SELECT * FROM constraints WHERE tenant_id = ? AND instance_id = ? AND fingerprint = ?"
            " AND status <> 'retired' ORDER BY created_at DESC LIMIT 1",
            (self.scope.tenant_id, self.scope.instance_id, fp),
        )
        if row is not None:
            await self.db.execute(
                "UPDATE constraints SET occurrences = occurrences + 1 WHERE id = ?", (row["id"],)
            )
            found = await self.get(str(row["id"]))
            assert found is not None
            return found, False
        cid = uuid.uuid4().hex[:12]
        await self.db.execute(
            "INSERT INTO constraints (id, tenant_id, instance_id, agent, text, fingerprint, status,"
            " source, evidence, occurrences, created_at) VALUES (?, ?, ?, ?, ?, ?, 'candidate',"
            " ?, ?, 1, ?)",
            (cid, self.scope.tenant_id, self.scope.instance_id, agent, text[:1000], fp, source,
             json.dumps(evidence, ensure_ascii=False, default=str), time.time()),
        )  # fmt: skip
        created = await self.get(cid)
        assert created is not None
        return created, True

    async def add(self, agent: str, text: str, by: str) -> Constraint:
        """A rule a person writes directly: active at once."""
        constraint, _ = await self.propose(agent, text, "operator", {"by": by})
        await self.decide(constraint.id, "active", by)
        found = await self.get(constraint.id)
        assert found is not None
        return found

    async def get(self, cid: str) -> Constraint | None:
        row = await self.db.fetchone(
            "SELECT * FROM constraints WHERE id = ? AND tenant_id = ? AND instance_id = ?",
            (cid, self.scope.tenant_id, self.scope.instance_id),
        )
        return Constraint.from_row(row) if row else None

    async def decide(self, cid: str, status: str, by: str, text: str | None = None) -> bool:
        sql = "UPDATE constraints SET status = ?, decided_at = ?, decided_by = ?"
        params: list[Any] = [status, time.time(), by]
        if text and text.strip():
            sql += ", text = ?"
            params.append(text.strip()[:1000])
        sql += " WHERE id = ? AND tenant_id = ? AND instance_id = ?"
        params += [cid, self.scope.tenant_id, self.scope.instance_id]
        return await self.db.execute(sql, params) > 0

    async def all(self, status: str | None = None) -> list[Constraint]:
        sql = "SELECT * FROM constraints WHERE tenant_id = ? AND instance_id = ?"
        params: list[Any] = [self.scope.tenant_id, self.scope.instance_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        rows = await self.db.fetchall(sql + " ORDER BY created_at", params)
        return [Constraint.from_row(r) for r in rows]

    async def active(self, agent: str) -> list[Constraint]:
        rows = await self.db.fetchall(
            "SELECT * FROM constraints WHERE tenant_id = ? AND instance_id = ?"
            " AND status = 'active' AND agent IN (?, ?) ORDER BY decided_at, created_at",
            (self.scope.tenant_id, self.scope.instance_id, agent, ALL_AGENTS),
        )
        return [Constraint.from_row(r) for r in rows]


def pinned_block(constraints: list[Constraint]) -> str:
    """The approved rules, for the system prompt (never cut)."""
    if not constraints:
        return ""
    lines = "\n".join(f"- {c.text}" for c in constraints)
    return f"\n\n## Rules approved from review (always follow)\n{lines}"
