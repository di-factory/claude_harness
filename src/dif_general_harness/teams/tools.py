"""Team tools: sub-agents, handoffs between agents, and workflow run reports.

- ``agent.<name>``: a sub-agent as a tool. It runs in a fresh session with its own tools,
  permissions and budget, and only its final answer comes back (structured, when it answers
  with JSON). Nesting is limited to ``MAX_DEPTH``.
- ``handoff.agent``: move the conversation to another agent in the caller's ``handoffs``.
  The target gets a new session seeded with the reason and the recent transcript; the
  contact's next messages go to it. (``handoff.human`` is the escalation tool.)
- ``runs.list`` / ``runs.summarize``: what workflows did recently (the daily report).
"""

from __future__ import annotations

import json
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from ..core.events import MessageAdded, TurnEnded
from ..core.messages import Role
from ..runtime.context import current_session
from ..spec.loader import duration_days
from ..tools.registry import Effect, Tool, schema_check

if TYPE_CHECKING:
    from ..runtime.instance import AgentRuntime, Instance

MAX_DEPTH = 3
_depth: ContextVar[int] = ContextVar("subagent_depth", default=0)


def _json_in(text: str) -> Any:
    stripped = text.strip().strip("`").removeprefix("json").strip()
    if not stripped.startswith("{"):
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def subagent_tool(instance: Instance, name: str) -> Tool:
    description = instance.spec.agents[name].description or f"the {name} agent"

    async def delegate(task: str) -> Any:
        depth = _depth.get()
        if depth >= MAX_DEPTH:
            raise RuntimeError("sub-agents are nested too deeply")
        token = _depth.set(depth + 1)
        try:
            agent = instance.agent(name)
            session = await agent.new_session()
            texts: list[str] = []
            reason = "error"
            async for event in agent.send(session, task):
                if isinstance(event, MessageAdded) and event.message.role is Role.ASSISTANT:
                    if event.message.text():
                        texts.append(event.message.text())
                elif isinstance(event, TurnEnded):
                    reason = event.reason
        finally:
            _depth.reset(token)
        if reason != "end_turn":
            raise RuntimeError(f"sub-agent {name} ended with {reason}")
        answer = texts[-1] if texts else ""
        parsed = _json_in(answer)
        return {"answer": answer, "output": parsed} if parsed is not None else answer

    schema = {
        "type": "object",
        "properties": {"task": {"type": "string", "description": "what to find or do"}},
        "required": ["task"],
    }
    return Tool(
        name=f"agent.{name}",
        description=f"Ask a sub-agent: {description}. Returns its final answer.",
        input_schema=schema,
        handler=delegate,
        effect=Effect.READ,  # delegating changes nothing; the sub-agent's own tools are gated
        timeout_s=600,
        check_input=schema_check(schema),
        source="team",
    )


def handoff_tool(instance: Instance, agent: AgentRuntime, targets: list[str]) -> Tool:
    async def hand_over(to: str, reason: str) -> str:
        if to not in targets:
            raise ValueError(f"{agent.name} cannot hand off to {to!r}; allowed: {targets}")
        source = current_session.get()
        if source is None:
            raise RuntimeError("no conversation to hand off")
        target = instance.agent(to)
        session = await target.new_session(contact_key=source.contact_key)
        binding = await instance.store.binding(instance.scope, source.id)
        if binding is not None:
            await instance.store.bind(instance.scope, session.id, binding[0], binding[1])
        await instance.store.set_state(instance.scope, source.id, "handed_off")
        transcript = "\n".join(f"{m.role}: {m.text()}" for m in source.messages[-10:] if m.text())
        note = (
            f"[Handoff from {agent.name}: {reason}]\nRecent conversation:\n{transcript}\n"
            "Continue with the contact from here."
        )
        instance.pending_handoffs[source.id] = (to, session.id, note)
        await instance.audit.record(
            instance.scope,
            f"agent:{agent.name}",
            "handoff",
            to,
            {"from_session": source.id, "to_session": session.id, "reason": reason},
        )
        return f"handed off to {to}; say one short line to the contact, {to} takes it from here"

    schema = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "enum": targets},
            "reason": {"type": "string"},
        },
        "required": ["to", "reason"],
    }
    return Tool(
        name="handoff.agent",
        description=f"Hand this conversation to another agent: {', '.join(targets)}.",
        input_schema=schema,
        handler=hand_over,
        effect=Effect.WRITE,
        check_input=schema_check(schema),
        source="team",
    )


def runs_tools(instance: Instance) -> list[Tool]:
    async def fetch(
        workflow: str | None, status: str | None, since: str | None
    ) -> list[dict[str, Any]]:
        sql = "SELECT id, workflow, status, outcome, error, created_at FROM workflow_runs"
        sql += " WHERE tenant_id = ? AND instance_id = ?"
        params: list[Any] = [instance.scope.tenant_id, instance.scope.instance_id]
        if workflow:
            sql += " AND workflow = ?"
            params.append(workflow)
        if status:
            sql += " AND status = ?"
            params.append(status)
        if since:
            sql += " AND created_at >= ?"
            params.append(time.time() - duration_days(since) * 86400)
        return list(await instance.db.fetchall(sql + " ORDER BY created_at DESC LIMIT 200", params))

    async def list_runs(
        workflow: str | None = None, status: str | None = None, since: str | None = None
    ) -> list[dict[str, Any]]:
        return await fetch(workflow, status, since)

    async def summarize(workflow: str | None = None, since: str = "24h") -> dict[str, Any]:
        rows = await fetch(workflow, None, since)
        counts: dict[str, int] = {}
        for row in rows:
            key = row["outcome"] or row["status"]
            counts[key] = counts.get(key, 0) + 1
        failures = [f"{r['id']}: {r['error']}" for r in rows if r["error"]][:10]
        what = workflow or "all workflows"
        parts = [f"{n} {k}" for k, n in sorted(counts.items())] or ["no runs"]
        text = f"{what}, last {since}: " + ", ".join(parts) + "."
        if failures:
            text += " Problems: " + "; ".join(failures)
        return {"text": text, "counts": counts, "total": len(rows)}

    def make(name: str, fn: Any, schema: dict[str, Any], doc: str) -> Tool:
        return Tool(
            name=name,
            description=doc,
            input_schema=schema,
            handler=fn,
            effect=Effect.READ,
            check_input=schema_check(schema),
            source="runs",
        )

    opt = {"type": "string"}
    return [
        make(
            "runs.list",
            list_runs,
            {"type": "object", "properties": {"workflow": opt, "status": opt, "since": opt}},
            "Recent workflow runs, newest first. since: e.g. 24h or 7d.",
        ),
        make(
            "runs.summarize",
            summarize,
            {"type": "object", "properties": {"workflow": opt, "since": opt}},
            "Counts of recent runs by outcome, as a sentence for a report.",
        ),
    ]
