"""Run records: one per finished run (a workflow, a batch, a scheduled agent), append only.

A record answers "why did it stop, what failed and why, what did it change?" weeks later,
and is what the weekly review reads (``service/review.py``). It never holds message texts:
counts, ids, gate failures (gate and reason) and the change a run made (``diff``).

    {"kind": "workflow", "name": "market-scan", "started": ..., "ended": ...,
     "stop_reason": "completed | condition | cap_agents | cap_minutes | failed | ...",
     "counts": {"agents": 12, "verified": 9, "retried": 2, "escalated": 1, ...},
     "failures": [{"item": "Acme", "gate": "schema | verifier | threshold", "reason": "..."}],
     "alias_collisions": [...], "diff": {"nodes_added": [...], "edges_added": [...]}}

There is no update or delete: a run's record is written once, when it ends.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from ..core.scope import Scope
from ..store.db import Database

DAY = 86400.0
REASON_CHARS = 300


# what kind of failure, in the handbook's terms (decision 92): what the weekly review counts
STOP_CLASSES = {
    "budget": "budget", "max_turns": "budget", "timeout": "budget", "cap_agents": "budget",
    "cap_minutes": "budget", "stuck": "selection", "needs_human": "verification",
    "failed": "environment", "error": "environment", "refusal": "authorization",
    "overflow": "context", "max_tokens": "contract",
}  # fmt: skip
GATE_CLASSES = {"schema": "contract", "verifier": "verification", "threshold": "verification"}


def failure_class(failure: dict[str, Any]) -> str:
    gate = str(failure.get("gate") or "")
    if gate in GATE_CLASSES:
        return GATE_CLASSES[gate]
    reason = str(failure.get("reason") or "").lower()
    for word, kind in (
        ("denied", "authorization"),
        ("not allowed", "authorization"),
        ("injection", "provenance"),
        ("suspicious", "provenance"),
        ("unknown tool", "selection"),
        ("invalid input", "contract"),
        ("outcome unknown", "recovery"),
        ("may already have run", "recovery"),
        ("budget", "budget"),
        ("timed out", "budget"),
        ("stuck", "selection"),
    ):
        if word in reason:
            return kind
    return "environment"


class RunRecords:
    def __init__(self, db: Database, scope: Scope) -> None:
        self.db, self.scope = db, scope

    async def append(
        self,
        kind: str,
        name: str,
        *,
        started: float,
        stop_reason: str,
        counts: dict[str, int] | None = None,
        failures: list[dict[str, Any]] | None = None,
        ended: float | None = None,
        **extra: Any,
    ) -> str:
        record_id = uuid.uuid4().hex[:16]
        body = {
            "counts": dict(counts or {}),
            "failures": [{**{k: (str(v)[:REASON_CHARS] if k == "reason" else v)
                             for k, v in f.items()}, "class": failure_class(f)}
                         for f in (failures or [])][:200],
            "stop_class": STOP_CLASSES.get(stop_reason),
            **{k: v for k, v in extra.items() if v not in (None, [], {})},
        }  # fmt: skip
        await self.db.execute(
            "INSERT INTO run_records (id, tenant_id, instance_id, kind, name, started, ended,"
            " stop_reason, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record_id, self.scope.tenant_id, self.scope.instance_id, kind, name, started,
             ended if ended is not None else time.time(), stop_reason,
             json.dumps(body, ensure_ascii=False, default=str)),
        )  # fmt: skip
        return record_id

    async def recent(self, days: float = 7.0, *, now: float | None = None,
                     limit: int = 500) -> list[dict[str, Any]]:  # fmt: skip
        """The records of the last ``days``, newest first."""
        since = (now if now is not None else time.time()) - days * DAY
        rows = await self.db.fetchall(
            "SELECT * FROM run_records WHERE tenant_id = ? AND instance_id = ? AND ended >= ?"
            " ORDER BY ended DESC LIMIT ?",
            (self.scope.tenant_id, self.scope.instance_id, since, limit),
        )
        return [{"id": r["id"], "kind": r["kind"], "name": r["name"], "started": r["started"],
                 "ended": r["ended"], "stop_reason": r["stop_reason"], **json.loads(r["body"])}
                for r in rows]  # fmt: skip
