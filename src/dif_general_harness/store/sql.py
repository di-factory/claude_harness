"""Session events in the database (Postgres in production, SQLite locally).

Same contract as ``JsonlSessionStore``: append-only, ``text_delta`` not persisted, events
redacted before they are written (ids and thinking signatures kept verbatim). The
``sessions`` table also tracks each session's agent, channel, contact and state, which the
channels use to find a contact's current conversation.
"""

from __future__ import annotations

import json
import time

from ..core.events import EVENT_ADAPTER, Event, SessionStarted, TextDelta
from ..core.scope import Scope
from ..core.session import Session
from ..policy.redact import Redactor
from .db import Database
from .jsonl import _scrub


class SqlSessionStore:
    def __init__(self, db: Database, redactor: Redactor | None = None) -> None:
        self.db = db
        self.redactor = redactor

    async def append(self, event: Event) -> None:
        if isinstance(event, TextDelta):
            return
        data = event.model_dump(mode="json")
        if self.redactor is not None:
            data = _scrub(data, self.redactor)
        s = event.scope
        now = time.time()
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO events (tenant_id, instance_id, session_id, seq, type, ts, data)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (s.tenant_id, s.instance_id, event.session_id, event.seq, event.type,
                 event.ts.timestamp(), json.dumps(data, ensure_ascii=False)),
            )  # fmt: skip
            if isinstance(event, SessionStarted):
                await conn.execute(
                    "INSERT INTO sessions (tenant_id, instance_id, session_id, agent_id,"
                    " contact_key, created_at, last_active) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (s.tenant_id, s.instance_id, event.session_id, event.agent_id,
                     event.contact_key, now, now),
                )  # fmt: skip
            else:
                await conn.execute(
                    "UPDATE sessions SET last_active = ?"
                    " WHERE tenant_id = ? AND instance_id = ? AND session_id = ?",
                    (now, s.tenant_id, s.instance_id, event.session_id),
                )

    async def read(self, scope: Scope, session_id: str) -> list[Event]:
        rows = await self.db.fetchall(
            "SELECT data FROM events WHERE tenant_id = ? AND instance_id = ? AND session_id = ?"
            " ORDER BY seq",
            (scope.tenant_id, scope.instance_id, session_id),
        )
        if not rows:
            raise FileNotFoundError(f"no session {session_id!r} in {scope}")
        return [EVENT_ADAPTER.validate_json(r["data"]) for r in rows]

    async def load(self, scope: Scope, session_id: str) -> Session:
        return Session.from_events(await self.read(scope, session_id))

    async def list_sessions(self, scope: Scope) -> list[str]:
        rows = await self.db.fetchall(
            "SELECT session_id FROM sessions WHERE tenant_id = ? AND instance_id = ?"
            " ORDER BY session_id",
            (scope.tenant_id, scope.instance_id),
        )
        return [r["session_id"] for r in rows]

    # --- conversations per contact ---------------------------------------------------

    async def bind(self, scope: Scope, session_id: str, channel: str, contact_key: str) -> None:
        await self.db.execute(
            "UPDATE sessions SET channel = ?, contact_key = ?"
            " WHERE tenant_id = ? AND instance_id = ? AND session_id = ?",
            (channel, contact_key, scope.tenant_id, scope.instance_id, session_id),
        )

    async def current(
        self, scope: Scope, channel: str, contact_key: str, window_s: float | None
    ) -> tuple[str, str] | None:
        """The contact's latest session on a channel, if active within the window:
        ``(session_id, state)``."""
        row = await self.db.fetchone(
            "SELECT session_id, state, last_active FROM sessions WHERE tenant_id = ?"
            " AND instance_id = ? AND channel = ? AND contact_key = ? AND state != 'handed_off'"
            " ORDER BY last_active DESC LIMIT 1",
            (scope.tenant_id, scope.instance_id, channel, contact_key),
        )
        if row is None:
            return None
        if window_s is not None and time.time() - float(row["last_active"]) > window_s:
            return None
        return str(row["session_id"]), str(row["state"])

    async def set_state(self, scope: Scope, session_id: str, state: str) -> None:
        await self.db.execute(
            "UPDATE sessions SET state = ?"
            " WHERE tenant_id = ? AND instance_id = ? AND session_id = ?",
            (state, scope.tenant_id, scope.instance_id, session_id),
        )

    async def binding(self, scope: Scope, session_id: str) -> tuple[str, str] | None:
        """``(channel, contact_key)`` of a session started from a channel."""
        row = await self.db.fetchone(
            "SELECT channel, contact_key FROM sessions WHERE tenant_id = ? AND instance_id = ?"
            " AND session_id = ?",
            (scope.tenant_id, scope.instance_id, session_id),
        )
        if row is None or not row["channel"] or not row["contact_key"]:
            return None
        return str(row["channel"]), str(row["contact_key"])

    async def state(self, scope: Scope, session_id: str) -> str | None:
        row = await self.db.fetchone(
            "SELECT state FROM sessions WHERE tenant_id = ? AND instance_id = ? AND session_id = ?",
            (scope.tenant_id, scope.instance_id, session_id),
        )
        return str(row["state"]) if row else None
