"""Cost accounting and reports (ARCHITECTURE §3.8): per tenant, vendor, role, agent and model.

Every model call is recorded once in ``usage`` (a row per day, agent, role, vendor and model,
counting calls and tokens and the USD they cost at the vendor's list price). That backs the
zero-markup cost-transparency clause: a report sums exactly what the vendors charge,
attributed to the tenant, and any model without a price is listed as ``unpriced`` rather
than silently reported as free.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..core.messages import Usage
from ..core.scope import Scope
from ..store.db import Database

DIMENSIONS = ("day", "agent", "role", "vendor", "model")


def today() -> str:
    return datetime.now(UTC).date().isoformat()


class UsageStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(
        self, scope: Scope, *, agent: str, role: str, vendor: str, model: str, usage: Usage,
        day: str | None = None,
    ) -> None:  # fmt: skip
        await self.db.execute(
            "INSERT INTO usage (tenant_id, instance_id, day, agent, role, vendor, model, calls,"
            " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, usd)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)"
            " ON CONFLICT (tenant_id, instance_id, day, agent, role, vendor, model) DO UPDATE SET"
            " calls = usage.calls + 1, input_tokens = usage.input_tokens + excluded.input_tokens,"
            " output_tokens = usage.output_tokens + excluded.output_tokens,"
            " cache_read_tokens = usage.cache_read_tokens + excluded.cache_read_tokens,"
            " cache_write_tokens = usage.cache_write_tokens + excluded.cache_write_tokens,"
            " usd = usage.usd + excluded.usd",
            (scope.tenant_id, scope.instance_id, day or today(), agent, role, vendor, model,
             usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
             usage.cache_write_tokens, usage.cost_usd),
        )  # fmt: skip

    async def report(
        self, scope: Scope, *, since: str | None = None, until: str | None = None,
        by: list[str] | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        """Totals and rows grouped by ``by`` (any of ``DIMENSIONS``) for days in
        [since, until]. Defaults: the last 30 days, grouped by vendor."""
        dims = list(by or ["vendor"])
        bad = [d for d in dims if d not in DIMENSIONS]
        if bad:
            raise ValueError(f"cannot group by {bad}; choose from {list(DIMENSIONS)}")
        end = until or today()
        start = since or (date.fromisoformat(end) - timedelta(days=29)).isoformat()
        cols = ", ".join(dims)
        sums = (
            "SUM(calls) AS calls, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens,"
            " SUM(cache_write_tokens) AS cache_write_tokens, SUM(usd) AS usd"
        )
        where = "tenant_id = ? AND instance_id = ? AND day >= ? AND day <= ?"
        params = (scope.tenant_id, scope.instance_id, start, end)
        rows = await self.db.fetchall(
            f"SELECT {cols}, {sums} FROM usage WHERE {where} GROUP BY {cols} ORDER BY {cols}",
            params,
        )
        total = await self.db.fetchone(f"SELECT {sums} FROM usage WHERE {where}", params)
        unpriced = await self.db.fetchall(
            f"SELECT DISTINCT model FROM usage WHERE {where} AND usd = 0"
            " AND input_tokens + output_tokens > 0 ORDER BY model",
            params,
        )
        return {
            "tenant": scope.tenant_id,
            "instance": scope.instance_id,
            "since": start,
            "until": end,
            "by": dims,
            "rows": [_clean(r) for r in rows],
            "total": _clean(total or {}),
            "unpriced": [r["model"] for r in unpriced],
        }


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key == "usd":
            out[key] = round(float(value or 0.0), 6)
        elif key in ("calls",) or key.endswith("_tokens"):
            out[key] = int(value or 0)
        else:
            out[key] = value
    return out
