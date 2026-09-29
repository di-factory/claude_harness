"""Persisted spend per tenant, agent and model, per UTC day (ARCHITECTURE §3.8).

Keys: ``tenant``, ``agent:<name>``, ``model:<id>``. Daily budgets read these totals before
each run, so a limit holds across restarts and across workers; cost reports sum them by
vendor.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..core.scope import Scope
from ..store.db import Database


def today() -> str:
    return datetime.now(UTC).date().isoformat()


class SpendStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, scope: Scope, key: str, usd: float, day: str | None = None) -> None:
        if usd <= 0:
            return
        await self.db.execute(
            "INSERT INTO spend (tenant_id, instance_id, key, day, usd) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (tenant_id, instance_id, key, day)"
            " DO UPDATE SET usd = spend.usd + excluded.usd",
            (scope.tenant_id, scope.instance_id, key, day or today(), usd),
        )

    async def day(self, scope: Scope, day: str | None = None) -> dict[str, float]:
        rows = await self.db.fetchall(
            "SELECT key, usd FROM spend WHERE tenant_id = ? AND instance_id = ? AND day = ?",
            (scope.tenant_id, scope.instance_id, day or today()),
        )
        return {r["key"]: float(r["usd"]) for r in rows}
