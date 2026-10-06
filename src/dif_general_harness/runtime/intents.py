"""Side-effecting calls are recorded before they run, so a retry never repeats them blindly.

A booking, a ticket, an email or a payment can commit and still look failed: the process
crashes, the answer is lost, a job is retried. Repeating it then makes a duplicate. Every call
of a tool that writes or acts outside (``effect`` other than read) goes through an intent log,
keyed by the conversation, the tool and its exact arguments:

- before it runs, the intent is recorded as ``started``; the key goes with it
  (``IDEMPOTENCY_KEY``), so HTTP connectors send it as ``Idempotency-Key`` and the client's
  API can treat a repeat as the same operation;
- afterwards it is ``done`` (with its result), ``failed`` (nothing changed) or ``unknown``
  (a timeout or a lost answer: it may have taken effect);
- the same call again in the same conversation (within a day): a ``done`` one answers with
  the earlier result instead of running again; a ``started`` or ``unknown`` one is not run:
  the model is told to check first with a read tool, and once it has (any read tool in that
  conversation), it may call it again.

Per tool, ``tools.overrides.<tool>.retry`` sets the policy: ``check`` (the default for tools
that write or act), ``safe`` (idempotent: always runs, nothing recorded) or ``never`` (a
second identical call in a conversation is never run, whatever happened to the first: a
person decides; for payments and the like). Workflow tool steps use the same log with the
run and step as the key: a step that crashed half way is not run again but escalated.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from ..core.messages import ToolResultBlock, ToolStatus, ToolUseBlock
from ..core.scope import Scope
from ..store.db import Database
from ..tools.registry import IDEMPOTENCY_KEY, UNKNOWN_HINT, Effect, Tool, ToolRegistry
from .context import current_session

DAY = 86400.0
RESULT_CHARS = 20_000
Policy = Literal["safe", "check", "never"]


@dataclass
class Intent:
    key: str
    status: str  # started | done | failed | unknown | checked
    result: Any
    updated_at: float


def intent_key(scope: Scope, where: str, tool: str, args: Any) -> str:
    raw = json.dumps([scope.tenant_id, scope.instance_id, where, tool, args], sort_keys=True,
                     default=str, ensure_ascii=False)  # fmt: skip
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class IntentLog:
    def __init__(self, db: Database, scope: Scope) -> None:
        self.db, self.scope = db, scope

    async def get(self, key: str, within: float = DAY) -> Intent | None:
        row = await self.db.fetchone(
            "SELECT * FROM tool_intents WHERE key = ? AND tenant_id = ? AND instance_id = ?",
            (key, self.scope.tenant_id, self.scope.instance_id),
        )
        if row is None or time.time() - float(row["updated_at"]) > within:
            return None
        result = json.loads(row["result"]) if row["result"] else None
        return Intent(row["key"], row["status"], result, float(row["updated_at"]))

    async def start(self, key: str, where: str, tool: str) -> None:
        now = time.time()
        await self.db.execute("DELETE FROM tool_intents WHERE key = ?", (key,))
        await self.db.execute(
            "INSERT INTO tool_intents (key, tenant_id, instance_id, session_id, tool, status,"
            " result, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'started', NULL, ?, ?)",
            (key, self.scope.tenant_id, self.scope.instance_id, where, tool, now, now),
        )

    async def finish(self, key: str, status: str, result: Any = None) -> None:
        body = json.dumps(result, ensure_ascii=False, default=str)[:RESULT_CHARS]
        await self.db.execute(
            "UPDATE tool_intents SET status = ?, result = ?, updated_at = ? WHERE key = ?",
            (status, body if result is not None else None, time.time(), key),
        )

    async def checked(self, where: str) -> None:
        """A read in the conversation: its unknown outcomes may now be tried again."""
        await self.db.execute(
            "UPDATE tool_intents SET status = 'checked', updated_at = ? WHERE tenant_id = ?"
            " AND instance_id = ? AND session_id = ? AND status IN ('started', 'unknown')",
            (time.time(), self.scope.tenant_id, self.scope.instance_id, where),
        )


def policy_for(tool: Tool, overrides: dict[str, Any]) -> Policy:
    override = overrides.get(tool.name)
    chosen = getattr(override, "retry", None)
    if chosen in ("safe", "check", "never"):
        return chosen  # type: ignore[no-any-return]
    return "safe" if tool.effect is Effect.READ else "check"


def outcome_unknown(call: ToolUseBlock) -> ToolResultBlock:
    return ToolResultBlock(
        tool_use_id=call.id, status=ToolStatus.ERROR, reason="outcome_unknown",
        error="the same call was made earlier in this conversation and its outcome is unknown"
        " (it may have taken effect); it was not run again",
        retryable=False, side_effects="unknown",
        hint=UNKNOWN_HINT + "; after checking, you may call it again if it did not happen",
    )  # fmt: skip


class IntentTools(ToolRegistry):
    """An agent's tools with the intent log in front of every side-effecting call."""

    def __init__(self, inner: ToolRegistry, log: IntentLog, overrides: dict[str, Any]) -> None:
        super().__init__()
        self._tools = inner._tools
        self.inner, self.log, self.overrides = inner, log, overrides

    async def execute(self, call: ToolUseBlock) -> ToolResultBlock:
        tool = self.get(call.name)
        session = current_session.get()
        if tool is None or call.input_error:
            return await self.inner.execute(call)
        policy = policy_for(tool, self.overrides)
        where = session.id if session is not None else f"call:{call.id}"
        if policy == "safe":
            result = await self.inner.execute(call)
            if tool.effect is Effect.READ and session is not None:
                await self.log.checked(where)
            return result
        key = intent_key(self.log.scope, where, call.name, call.input)
        prior = await self.log.get(key)
        if prior is not None:
            if prior.status in ("started", "unknown"):
                return outcome_unknown(call)
            if prior.status == "done":
                return ToolResultBlock(tool_use_id=call.id, status=ToolStatus.OK, content={
                    "already_done": True, "result": prior.result,
                    "note": "this exact call already succeeded earlier in this conversation;"
                    " it was not repeated"})  # fmt: skip
            if policy == "never":
                return ToolResultBlock(
                    tool_use_id=call.id, status=ToolStatus.DENIED, reason="not_repeated",
                    error="this call was already attempted in this conversation and is never"
                    " repeated automatically", retryable=False, side_effects="none",
                    hint="tell the contact a person will follow up, or hand over",
                )  # fmt: skip
        await self.log.start(key, where, call.name)
        token = IDEMPOTENCY_KEY.set(key)
        try:
            result = await self.inner.execute(call)
        finally:
            IDEMPOTENCY_KEY.reset(token)
        if result.status is ToolStatus.OK:
            await self.log.finish(key, "done", result.content)
        elif result.side_effects in ("unknown", "committed"):
            await self.log.finish(key, "unknown")
        else:
            await self.log.finish(key, "failed")
        return result
