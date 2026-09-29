"""Consent and opt-out (ARCHITECTURE §3.18), per contact and channel.

- A contact who sends an opt-out keyword (``governance.consent.opt_out_keywords``, e.g.
  ``BAJA``, ``STOP``) is revoked on that channel at once.
- Messages the contact starts are always answered (they reached out). Messages the
  harness starts (triggers, reminders, campaigns) need consent: with
  ``consent.required`` a recorded grant is needed; without it, anything but a revocation
  passes. Operator channels (``hitl``, ``founder``, ``outbound``) are exempt: consent
  protects contacts, not staff.
"""

from __future__ import annotations

import time
from typing import Literal

from ..core.scope import Scope
from ..store.db import Database

Status = Literal["granted", "revoked"]


def is_opt_out(text: str, keywords: list[str]) -> bool:
    """The whole message is a keyword, or starts with one ("STOP please")."""
    words = text.strip().strip(".!¡").split()
    if not words:
        return False
    wanted = {k.strip().lower() for k in keywords if k.strip()}
    return " ".join(words).lower() in wanted or words[0].lower().strip(".,!") in wanted


class ConsentStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, scope: Scope, contact_key: str, channel: str) -> Status | None:
        row = await self.db.fetchone(
            "SELECT status FROM consent WHERE tenant_id = ? AND instance_id = ?"
            " AND contact_key = ? AND channel = ?",
            (scope.tenant_id, scope.instance_id, contact_key, channel),
        )
        return row["status"] if row else None

    async def set(
        self, scope: Scope, contact_key: str, channel: str, status: Status, source: str
    ) -> None:
        now = time.time()
        await self.db.execute(
            "INSERT INTO consent (tenant_id, instance_id, contact_key, channel, status, source,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (tenant_id, instance_id,"
            " contact_key, channel) DO UPDATE SET status = excluded.status,"
            " source = excluded.source, updated_at = excluded.updated_at",
            (scope.tenant_id, scope.instance_id, contact_key, channel, status, source, now),
        )

    async def may_contact(
        self, scope: Scope, contact_key: str, channel: str, *, required: bool
    ) -> bool:
        """May the harness start a conversation with this contact on this channel?"""
        status = await self.get(scope, contact_key, channel)
        if status == "revoked":
            return False
        return status == "granted" or not required
