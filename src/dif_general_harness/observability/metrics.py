"""Quality metrics per instance (ARCHITECTURE §3.8), computed from what is already stored:
the event log, the audit chain, the inbox, sessions and usage.

- **tool success rate:** tool calls that ended ``ok`` / all tool calls;
- **verify pass rate:** verification checks that passed / all checks run;
- **answer review pass rate:** sampled answers the verifier passed / all reviewed;
- **escalation rate:** escalations filed / conversations started;
- **cost per resolved request:** model spend / conversations that were neither escalated
  nor handed to a teammate.

Only counts leave this module: no messages, contacts or arguments.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from ..core.scope import Scope
from ..spec.loader import duration_days
from ..store.db import Database


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


async def quality(db: Database, scope: Scope, since: str = "7d") -> dict[str, Any]:
    start = time.time() - duration_days(since) * 86400
    first_day = datetime.fromtimestamp(start, UTC).date().isoformat()
    key = (scope.tenant_id, scope.instance_id)

    tools: dict[str, int] = {}
    rows = await db.fetchall(
        "SELECT data FROM events WHERE tenant_id = ? AND instance_id = ?"
        " AND type = 'tool_call_finished' AND ts >= ?",
        (*key, start),
    )
    for row in rows:
        status = str(json.loads(row["data"]).get("status", "unknown"))
        tools[status] = tools.get(status, 0) + 1

    checks = {"passed": 0, "failed": 0}
    rows = await db.fetchall(
        "SELECT data FROM audit WHERE tenant_id = ? AND instance_id = ?"
        " AND action = 'verification' AND ts >= ?",
        (*key, start),
    )
    for row in rows:
        checks["passed" if json.loads(row["data"]).get("passed") else "failed"] += 1

    reviews = {"passed": 0, "failed": 0}
    rows = await db.fetchall(
        "SELECT data FROM audit WHERE tenant_id = ? AND instance_id = ?"
        " AND action = 'output_review' AND ts >= ?",
        (*key, start),
    )
    for row in rows:
        reviews["passed" if json.loads(row["data"]).get("passed") else "failed"] += 1

    states: dict[str, int] = {}
    rows = await db.fetchall(
        "SELECT state, COUNT(*) AS n FROM sessions WHERE tenant_id = ? AND instance_id = ?"
        " AND created_at >= ? GROUP BY state",
        (*key, start),
    )
    for row in rows:
        states[str(row["state"])] = int(row["n"])
    conversations = sum(states.values())
    resolved = conversations - states.get("escalated", 0) - states.get("handed_off", 0)

    escalations = await db.fetchone(
        "SELECT COUNT(*) AS n FROM inbox WHERE tenant_id = ? AND instance_id = ?"
        " AND kind = 'escalation' AND created_at >= ?",
        (*key, start),
    )
    spend = await db.fetchone(
        "SELECT SUM(usd) AS usd FROM usage WHERE tenant_id = ? AND instance_id = ? AND day >= ?",
        (*key, first_day),
    )
    usd = float((spend or {}).get("usd") or 0.0)
    escalated = int((escalations or {}).get("n") or 0)
    return {
        "since": since,
        "tool_calls": tools,
        "tool_success_rate": _rate(tools.get("ok", 0), sum(tools.values())),
        "verifications": checks,
        "verify_pass_rate": _rate(checks["passed"], sum(checks.values())),
        "answer_reviews": reviews,
        "answer_review_pass_rate": _rate(reviews["passed"], sum(reviews.values())),
        "conversations": conversations,
        "escalations": escalated,
        "escalation_rate": _rate(escalated, conversations),
        "resolved": resolved,
        "spend_usd": round(usd, 6),
        "cost_per_resolved_usd": round(usd / resolved, 6) if resolved > 0 else None,
    }
