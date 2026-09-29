"""The immutable audit log (ARCHITECTURE §3.18): who did what, when, to what.

Append-only and hash-chained per tenant and instance: each record's hash covers its content
and the previous record's hash, so an edited, deleted or reordered record breaks the chain
and ``verify`` finds it. Retention may drop the oldest records; the chain is then checked
from the first remaining record.

Recorded: tool calls with side effects (every call at audit level ``full``), approvals and
their decisions, escalations, consent changes, outbound messages, config changes. Data is
stored as the model saw it (tokenized, redacted), never raw PII.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from ..core.scope import Scope
from ..store.db import Database

GENESIS = "0" * 64


def _digest(
    prev: str, seq: int, ts: float, actor: str, action: str, subject: str, data: str
) -> str:
    body = json.dumps([prev, seq, round(ts, 6), actor, action, subject, data], ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    ts: float
    actor: str
    action: str
    subject: str
    data: dict[str, Any]
    hash: str


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def record(
        self,
        scope: Scope,
        actor: str,
        action: str,
        subject: str,
        data: dict[str, Any] | None = None,
    ) -> int:
        payload = json.dumps(data or {}, ensure_ascii=False, sort_keys=True, default=str)
        ts = round(time.time(), 6)
        async with self.db.transaction() as conn:
            if conn.dialect == "postgres":  # one writer per chain
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(?))",
                    (f"audit/{scope.tenant_id}/{scope.instance_id}",),
                )
            last = await conn.fetchone(
                "SELECT seq, hash FROM audit WHERE tenant_id = ? AND instance_id = ?"
                " ORDER BY seq DESC LIMIT 1",
                (scope.tenant_id, scope.instance_id),
            )
            seq = int(last["seq"]) + 1 if last else 1
            prev = str(last["hash"]) if last else GENESIS
            digest = _digest(prev, seq, ts, actor, action, subject, payload)
            await conn.execute(
                "INSERT INTO audit (tenant_id, instance_id, seq, ts, actor, action, subject, data,"
                " prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (scope.tenant_id, scope.instance_id, seq, ts, actor, action, subject, payload,
                 prev, digest),
            )  # fmt: skip
        return seq

    async def records(self, scope: Scope, *, action: str | None = None) -> list[AuditRecord]:
        sql = "SELECT * FROM audit WHERE tenant_id = ? AND instance_id = ?"
        params: list[Any] = [scope.tenant_id, scope.instance_id]
        if action:
            sql += " AND action = ?"
            params.append(action)
        rows = await self.db.fetchall(sql + " ORDER BY seq", params)
        return [
            AuditRecord(
                int(r["seq"]), float(r["ts"]), r["actor"], r["action"], r["subject"],
                json.loads(r["data"]), r["hash"],
            )
            for r in rows
        ]  # fmt: skip

    async def verify(self, scope: Scope) -> int | None:
        """None when the chain is intact, else the first seq where it breaks."""
        rows = await self.db.fetchall(
            "SELECT * FROM audit WHERE tenant_id = ? AND instance_id = ? ORDER BY seq",
            (scope.tenant_id, scope.instance_id),
        )
        prev = rows[0]["prev_hash"] if rows else GENESIS
        expected_seq = int(rows[0]["seq"]) if rows else 1
        for r in rows:
            seq = int(r["seq"])
            digest = _digest(
                prev, seq, float(r["ts"]), r["actor"], r["action"], r["subject"], r["data"]
            )
            if seq != expected_seq or r["prev_hash"] != prev or r["hash"] != digest:
                return seq
            prev, expected_seq = r["hash"], seq + 1
        return None
