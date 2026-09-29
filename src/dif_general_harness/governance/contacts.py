"""Contact attributes (``contact.verified``, ``contact.vip``...) that conditions read.

Set by the client's systems through the admin API (for example after an identity check),
never by the model. Tenant- and instance-scoped like everything else.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..core.scope import Scope
from ..store.db import Database


class ContactStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, scope: Scope, contact_key: str) -> dict[str, Any]:
        row = await self.db.fetchone(
            "SELECT attrs FROM contacts WHERE tenant_id = ? AND instance_id = ?"
            " AND contact_key = ?",
            (scope.tenant_id, scope.instance_id, contact_key),
        )
        return dict(json.loads(row["attrs"])) if row else {}

    async def update(self, scope: Scope, contact_key: str, attrs: dict[str, Any]) -> dict[str, Any]:
        merged = {**await self.get(scope, contact_key), **attrs}
        merged = {k: v for k, v in merged.items() if v is not None}  # null removes an attribute
        await self.db.execute(
            "INSERT INTO contacts (tenant_id, instance_id, contact_key, attrs, updated_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT (tenant_id, instance_id, contact_key)"
            " DO UPDATE SET attrs = excluded.attrs, updated_at = excluded.updated_at",
            (scope.tenant_id, scope.instance_id, contact_key, json.dumps(merged), time.time()),
        )
        return merged
