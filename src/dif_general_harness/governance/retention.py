"""Retention (ARCHITECTURE §3.18): data older than the spec allows is deleted.

``governance.retention`` maps data classes to durations (``"conversations": "180d"``,
``"audit": "5y"``). Instances can only shorten them (enforced by the spec loader). The
purge runs as a daily job; each run is itself recorded in the audit log.

Classes handled here: ``conversations`` (sessions and their events, by last activity),
``audit``, and finished ``jobs`` (30 days). Memory and documents follow with their modules.
"""

from __future__ import annotations

import time

from ..core.scope import Scope
from ..spec.loader import duration_days
from ..store.db import Database

DAY = 86400.0
JOBS_KEEP_DAYS = 30.0


async def purge(
    db: Database, scope: Scope, retention: dict[str, str], *, now: float | None = None
) -> dict[str, int]:
    now = time.time() if now is None else now
    removed: dict[str, int] = {}
    key = (scope.tenant_id, scope.instance_id)
    async with db.transaction() as conn:
        if "conversations" in retention:
            cutoff = now - duration_days(retention["conversations"]) * DAY
            removed["events"] = await conn.execute(
                "DELETE FROM events WHERE tenant_id = ? AND instance_id = ? AND session_id IN"
                " (SELECT session_id FROM sessions WHERE tenant_id = ? AND instance_id = ?"
                " AND last_active < ?)",
                (*key, *key, cutoff),
            )
            removed["sessions"] = await conn.execute(
                "DELETE FROM sessions WHERE tenant_id = ? AND instance_id = ? AND last_active < ?",
                (*key, cutoff),
            )
            removed["knowledge_gaps"] = await conn.execute(  # contacts' questions, too
                "DELETE FROM knowledge_gaps WHERE tenant_id = ? AND instance_id = ?"
                " AND last_seen < ?",
                (*key, cutoff),
            )
        if "audit" in retention:
            cutoff = now - duration_days(retention["audit"]) * DAY
            removed["audit"] = await conn.execute(
                "DELETE FROM audit WHERE tenant_id = ? AND instance_id = ? AND ts < ?",
                (*key, cutoff),
            )
        if "documents" in retention:  # what file and batch triggers remember having seen
            cutoff = now - duration_days(retention["documents"]) * DAY
            removed["trigger_seen"] = await conn.execute(
                "DELETE FROM trigger_seen WHERE tenant_id = ? AND instance_id = ? AND seen_at < ?",
                (*key, cutoff),
            )
        removed["jobs"] = await conn.execute(
            "DELETE FROM jobs WHERE tenant_id = ? AND instance_id = ?"
            " AND status IN ('done', 'cancelled') AND updated_at < ?",
            (*key, now - JOBS_KEEP_DAYS * DAY),
        )
    return removed
