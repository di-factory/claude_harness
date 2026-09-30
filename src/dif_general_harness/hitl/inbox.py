"""The human-in-the-loop inbox (ARCHITECTURE §3.17).

One place for everything waiting on a person: approval requests, escalations and budget
stops. Items live in the instance's database, so they survive restarts, and every decision
is audited.

Headless approvals are **deferred**, not blocking: when a tool call needs approval, the
``InboxApprover`` files an item and the call returns ``denied`` with "waiting for approval
<id>", so the agent can tell the contact. When a person approves, the service executes
the stored call (re-checking the deny rules first) and runs a follow-up turn so the agent
reports the outcome. Nothing holds a model call open for hours.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from ..core.scope import Scope
from ..policy import ApprovalDecision, ApprovalRequest
from ..store.db import Database, Row

Kind = Literal["approval", "escalation", "budget", "memory", "skill", "constraint", "review"]
Status = Literal["open", "approved", "denied", "resolved", "expired"]


@dataclass(frozen=True)
class InboxItem:
    id: str
    kind: str
    status: str
    session_id: str | None
    title: str
    payload: dict[str, Any]
    created_at: float
    decided_by: str | None
    note: str | None

    @classmethod
    def from_row(cls, row: Row) -> InboxItem:
        return cls(
            row["id"], row["kind"], row["status"], row["session_id"], row["title"],
            json.loads(row["payload"]), float(row["created_at"]), row["decided_by"], row["note"],
        )  # fmt: skip


class Inbox:
    def __init__(self, db: Database, scope: Scope) -> None:
        self.db = db
        self.scope = scope

    async def create(
        self, kind: Kind, title: str, payload: dict[str, Any], session_id: str | None = None
    ) -> str:
        item_id = uuid.uuid4().hex[:12]
        await self.db.execute(
            "INSERT INTO inbox (id, tenant_id, instance_id, kind, status, session_id, title,"
            " payload, created_at) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)",
            (item_id, self.scope.tenant_id, self.scope.instance_id, kind, session_id, title,
             json.dumps(payload, ensure_ascii=False, default=str), time.time()),
        )  # fmt: skip
        return item_id

    async def get(self, item_id: str) -> InboxItem | None:
        row = await self.db.fetchone(
            "SELECT * FROM inbox WHERE id = ? AND tenant_id = ? AND instance_id = ?",
            (item_id, self.scope.tenant_id, self.scope.instance_id),
        )
        return InboxItem.from_row(row) if row else None

    async def list(self, status: str | None = "open", kind: str | None = None) -> list[InboxItem]:
        sql = "SELECT * FROM inbox WHERE tenant_id = ? AND instance_id = ?"
        params: list[Any] = [self.scope.tenant_id, self.scope.instance_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        rows = await self.db.fetchall(sql + " ORDER BY created_at", params)
        return [InboxItem.from_row(r) for r in rows]

    async def decide(self, item_id: str, status: Status, by: str, note: str = "") -> bool:
        """Close an open item. False when it was already decided (first decision wins)."""
        changed = await self.db.execute(
            "UPDATE inbox SET status = ?, decided_at = ?, decided_by = ?, note = ?"
            " WHERE id = ? AND tenant_id = ? AND instance_id = ? AND status = 'open'",
            (status, time.time(), by, note, item_id, self.scope.tenant_id, self.scope.instance_id),
        )
        return changed > 0


Notify = Callable[[str, str], Awaitable[None]]  # (item id, one-line summary)


class InboxApprover:
    """The headless ``Approver``: files the request and defers the call."""

    def __init__(self, inbox: Inbox, notify: Notify | None = None) -> None:
        self.inbox = inbox
        self.notify = notify

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        payload = {
            "tool": request.tool,
            "effect": str(request.effect),
            "arguments": request.arguments,
            "rule": request.rule,
            "agent": request.session.agent_id,
        }
        item_id = await self.inbox.create(
            "approval", f"Approve {request.tool}?", payload, request.session.id
        )
        if self.notify is not None:
            await self.notify(item_id, f"Approval needed: {request.tool} ({request.effect})")
        return ApprovalDecision(
            False,
            reason=f"waiting for approval {item_id}: a person must approve this action; tell"
            " the contact it is pending and that they will be updated",
        )
