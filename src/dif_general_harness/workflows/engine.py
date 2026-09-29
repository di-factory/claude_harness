"""The workflow engine (ARCHITECTURE §3.16): durable, inspectable runs of spec workflows.

A run is a row: its input, the output of every finished step, where it is and what it waits
for. A ``workflow`` job advances a run through as many steps as it can, saving after every
step, until it ends or has to wait. Waits are durable:
- ``wait`` for a ``reply`` (the contact's next message on the run's channel), an ``event``
  (``emit``), or a ``time``; with ``timeout``;
- ``approval``: an inbox item; the decision resumes the run;
- a ``tool`` call the policy says to ``ask`` about: approved like an approval step.

Step types: ``agent``, ``tool``, ``template``, ``message``, ``approval``, ``wait``,
``branch`` (first case whose ``when`` holds; ``goto``), ``parallel`` (``branches`` of agent
and tool steps, run together), ``handoff`` (to ``human`` or an agent), ``timer`` (arms a
``delay`` trigger) and ``end`` (``outcome``). Any step may carry ``when``: when it does not
hold, the step is skipped. Templates in step fields read ``input``, ``steps``, ``var``,
``event`` and ``contact``.

Safety is the same as in a conversation: tool steps go through the permission policy and
verification; messages the run starts need consent; ``on_error: escalate`` files an inbox
item instead of failing silently. A step that crashed half way is run again on resume (at
least once), so side-effecting tools should be idempotent or deduplicated.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..core import cel
from ..core.messages import ToolStatus, ToolUseBlock
from ..policy import Verdict
from ..spec.loader import duration_days
from ..spec.schema import Step, Workflow
from ..store.db import Row
from ..tools.registry import Effect
from .render import render

if TYPE_CHECKING:
    from ..service.headless import Headless

DAY = 86400.0
MAX_STEPS_PER_JOB = 200  # a guard against goto loops


class WorkflowError(RuntimeError):
    pass


@dataclass
class Run:
    id: str
    workflow: str
    status: str
    input: dict[str, Any]
    state: dict[str, Any]
    outcome: str | None = None
    error: str | None = None

    @classmethod
    def from_row(cls, row: Row) -> Run:
        return cls(
            row["id"], row["workflow"], row["status"], json.loads(row["input"]),
            json.loads(row["state"]), row["outcome"], row["error"],
        )  # fmt: skip

    @property
    def steps(self) -> dict[str, Any]:
        out: dict[str, Any] = self.state.setdefault("steps", {})
        return out


class _Wait(Exception):
    """A step that has to wait: the run is saved with what it waits for."""

    def __init__(self, kind: str, *, channel: str | None = None, contact: str | None = None,
                 event: str | None = None, deadline: float | None = None) -> None:  # fmt: skip
        self.kind, self.channel, self.contact = kind, channel, contact
        self.event, self.deadline = event, deadline


class WorkflowEngine:
    def __init__(self, host: Headless) -> None:
        self.host = host

    @property
    def db(self) -> Any:
        return self.host.instance.db

    @property
    def scope(self) -> Any:
        return self.host.instance.scope

    def workflow(self, name: str) -> Workflow:
        wf = self.host.instance.spec.workflows.get(name)
        if wf is None:
            raise WorkflowError(f"unknown workflow {name!r}")
        return wf

    # --- runs --------------------------------------------------------------------------

    async def start(
        self, name: str, input: dict[str, Any], *, dedupe: str | None = None,
        contact: str | None = None, channel: str | None = None,
    ) -> str:  # fmt: skip
        self.workflow(name)
        run_id = uuid.uuid4().hex[:16]
        state: dict[str, Any] = {"pc": 0, "steps": {}}
        if contact and channel:
            state["contact"] = {"key": contact, "channel": channel}
        now = time.time()
        await self.db.execute(
            "INSERT INTO workflow_runs (id, tenant_id, instance_id, workflow, status, input, state,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)",
            (run_id, self.scope.tenant_id, self.scope.instance_id, name,
             json.dumps(input, ensure_ascii=False, default=str), json.dumps(state), now, now),
        )  # fmt: skip
        await self.host.queue.enqueue(
            self.scope, "workflow", {"run": run_id}, dedupe_key=f"wf:{run_id}:start"
        )
        await self.host.instance.audit.record(
            self.scope, "workflow", "workflow_started", name, {"run": run_id}
        )
        return run_id

    async def get(self, run_id: str) -> Run | None:
        row = await self.db.fetchone(
            "SELECT * FROM workflow_runs WHERE id = ? AND tenant_id = ? AND instance_id = ?",
            (run_id, self.scope.tenant_id, self.scope.instance_id),
        )
        return Run.from_row(row) if row else None

    async def runs(self, workflow: str | None = None, status: str | None = None) -> list[Run]:
        sql = "SELECT * FROM workflow_runs WHERE tenant_id = ? AND instance_id = ?"
        params: list[Any] = [self.scope.tenant_id, self.scope.instance_id]
        if workflow:
            sql += " AND workflow = ?"
            params.append(workflow)
        if status:
            sql += " AND status = ?"
            params.append(status)
        return [
            Run.from_row(r) for r in await self.db.fetchall(sql + " ORDER BY created_at", params)
        ]

    async def _save(self, run: Run, wait: _Wait | None = None) -> None:
        await self.db.execute(
            "UPDATE workflow_runs SET status = ?, state = ?, outcome = ?, error = ?, wait_kind = ?,"
            " wait_channel = ?, wait_contact = ?, wait_event = ?, updated_at = ? WHERE id = ?",
            (run.status, json.dumps(run.state, ensure_ascii=False, default=str), run.outcome,
             run.error, wait.kind if wait else None, wait.channel if wait else None,
             wait.contact if wait else None, wait.event if wait else None, time.time(), run.id),
        )  # fmt: skip

    # --- resuming waits ----------------------------------------------------------------

    async def waiting_for_reply(self, channel: str, contact: str) -> Run | None:
        row = await self.db.fetchone(
            "SELECT * FROM workflow_runs WHERE tenant_id = ? AND instance_id = ?"
            " AND status = 'waiting' AND wait_kind = 'reply' AND wait_channel = ?"
            " AND wait_contact = ?"
            " ORDER BY updated_at DESC LIMIT 1",
            (self.scope.tenant_id, self.scope.instance_id, channel, contact),
        )
        return Run.from_row(row) if row else None

    async def resume(self, run: Run, output: dict[str, Any]) -> None:
        """Give the waiting step its output and let the run go on."""
        step_id = run.state.get("waiting_step")
        if run.status != "waiting" or not step_id:
            return
        run.state.pop("waiting_step", None)
        run.status = "running"
        steps = self.workflow(run.workflow).steps
        ids = [s.id for s in steps]
        step = steps[ids.index(step_id)]
        if run.state.pop("approval_for", None) == step_id:
            run.steps[f"{step_id}__approval"] = output  # the tool step runs again, decided
        else:
            run.steps[step_id] = output
            run.state["pc"] = ids.index(step_id) + 1
            reject = (step.model_extra or {}).get("on_reject")
            if step.type == "approval" and not output.get("approved") and reject:
                if reject == "end":
                    run.state["pc"] = len(steps)
                    run.outcome = "rejected"
                elif reject == "escalate":
                    run.status, run.outcome = "escalated", "rejected"
                    item = await self.host.instance.inbox.create(
                        "escalation", f"{run.workflow}: {step_id} was rejected",
                        {"run": run.id, "steps": run.steps},
                    )  # fmt: skip
                    await self.host.notify(item, f"{run.workflow}: {step_id} rejected")
                    await self._save(run)
                    return
                else:
                    run.state["pc"] = ids.index(str(reject))
        await self._save(run)
        await self.host.queue.enqueue(
            self.scope, "workflow", {"run": run.id},
            dedupe_key=f"wf:{run.id}:resume:{step_id}:{run.state.get('resumes', 0)}",
        )  # fmt: skip

    async def reply(self, channel: str, contact: str, text: str) -> bool:
        """A contact's message: resume the run waiting for it. False if none waits."""
        run = await self.waiting_for_reply(channel, contact)
        if run is None:
            return False
        await self.resume(run, {"replied": True, "reply": text, "timed_out": False})
        return True

    async def emit(self, name: str, data: dict[str, Any]) -> int:
        """An internal event: resume the runs waiting for it (their ``match`` must hold)."""
        rows = await self.db.fetchall(
            "SELECT * FROM workflow_runs WHERE tenant_id = ? AND instance_id = ?"
            " AND status = 'waiting' AND wait_kind = 'event' AND wait_event = ?",
            (self.scope.tenant_id, self.scope.instance_id, name),
        )
        resumed = 0
        for row in rows:
            run = Run.from_row(row)
            match = run.state.get("wait_match")
            if match and not cel.holds(match, {**self._ctx(run), "event": data}):
                continue
            await self.resume(run, {"received": True, "event": data, "timed_out": False})
            resumed += 1
        return resumed

    async def decided(self, run_id: str, approved: bool, by: str, note: str) -> None:
        run = await self.get(run_id)
        if run is not None:
            await self.resume(run, {"approved": approved, "by": by, "note": note})

    async def timed_out(self, run_id: str, step_id: str) -> None:
        run = await self.get(run_id)
        if run is None or run.status != "waiting" or run.state.get("waiting_step") != step_id:
            return
        await self.resume(
            run, {"replied": False, "received": False, "approved": False, "timed_out": True}
        )

    # --- advancing ---------------------------------------------------------------------

    def _ctx(self, run: Run) -> dict[str, Any]:
        inst = self.host.instance
        return {
            "input": run.input,
            "event": run.input.get("event", run.input),
            "steps": run.steps,
            "var": inst.spec.values,
            "contact": run.state.get("contact", {}),
            "run": {"id": run.id, "workflow": run.workflow},
        }

    async def advance(self, run_id: str) -> None:
        run = await self.get(run_id)
        if run is None or run.status != "running":
            return
        wf = self.workflow(run.workflow)
        if wf.concurrency and not await self._slot(run, wf.concurrency):
            tries = int(run.state.get("slot_waits", 0)) + 1
            run.state["slot_waits"] = tries
            await self._save(run)
            await self.host.queue.enqueue(
                self.scope, "workflow", {"run": run.id}, delay_s=15,
                dedupe_key=f"wf:{run.id}:slot:{tries}",
            )  # fmt: skip
            return
        ids = [s.id for s in wf.steps]
        for _ in range(MAX_STEPS_PER_JOB):
            pc = int(run.state.get("pc", 0))
            if pc >= len(wf.steps):
                run.status, run.outcome = "done", run.outcome or "completed"
                break
            step = wf.steps[pc]
            ctx = self._ctx(run)
            if step.when and not cel.holds(step.when, ctx):
                run.steps[step.id] = {"skipped": True}
                run.state["pc"] = pc + 1
                await self._save(run)
                continue
            try:
                goto = await self._step(run, step, ctx)
            except _Wait as wait:
                run.status = "waiting"
                run.state["waiting_step"] = step.id
                run.state["resumes"] = int(run.state.get("resumes", 0)) + 1
                await self._save(run, wait)
                if wait.deadline is not None:
                    key = f"wf:{run.id}:timeout:{step.id}:{run.state['resumes']}"
                    await self.host.queue.enqueue(
                        self.scope, "workflow_timeout", {"run": run.id, "step": step.id},
                        run_at=wait.deadline, dedupe_key=key,
                    )  # fmt: skip
                return
            except Exception as exc:
                await self._fail(run, wf, step, exc)
                return
            if run.status != "running":
                break
            run.state["pc"] = ids.index(goto) if goto else pc + 1
            await self._save(run)
        else:
            await self._fail(
                run, wf, wf.steps[int(run.state["pc"])], WorkflowError("too many steps")
            )
            return
        await self._save(run)
        await self.host.instance.audit.record(
            self.scope, "workflow", f"workflow_{run.status}", run.workflow,
            {"run": run.id, "outcome": run.outcome},
        )  # fmt: skip
        await self.emit(
            f"workflow.{run.workflow}.{run.status}", {"run": run.id, "outcome": run.outcome}
        )

    async def _slot(self, run: Run, limit: int) -> bool:
        rows = await self.db.fetchall(
            "SELECT id FROM workflow_runs WHERE tenant_id = ? AND instance_id = ? AND workflow = ?"
            " AND status IN ('running', 'waiting') ORDER BY created_at LIMIT ?",
            (self.scope.tenant_id, self.scope.instance_id, run.workflow, limit),
        )
        return run.id in {r["id"] for r in rows}

    async def _fail(self, run: Run, wf: Workflow, step: Step, exc: Exception) -> None:
        run.error = f"step {step.id}: {type(exc).__name__}: {exc}"
        run.status = "failed"
        if wf.on_error == "escalate":
            run.status = "escalated"
            item = await self.host.instance.inbox.create(
                "escalation", f"Workflow {run.workflow} failed at {step.id}",
                {"run": run.id, "workflow": run.workflow, "error": run.error, "steps": run.steps},
            )  # fmt: skip
            await self.host.notify(
                item, f"Workflow {run.workflow} needs a person: {run.error[:120]}"
            )
        await self._save(run)
        await self.host.instance.audit.record(
            self.scope,
            "workflow",
            "workflow_failed",
            run.workflow,
            {"run": run.id, "error": run.error},
        )

    # --- steps -------------------------------------------------------------------------

    async def _step(self, run: Run, step: Step, ctx: dict[str, Any]) -> str | None:
        """Run one step; returns a step id to jump to, or None for the next step."""
        x = step.model_extra or {}
        kind = step.type
        if kind == "agent":
            run.steps[step.id] = await self._agent(
                run, str(x["agent"]), render(x.get("input"), ctx)
            )
        elif kind == "tool":
            run.steps[step.id] = await self._tool(
                run, step, str(x["tool"]), render(x.get("args") or {}, ctx)
            )
        elif kind in {"template", "message"}:
            run.steps[step.id] = await self._send(run, kind, x, ctx)
        elif kind == "approval":
            summary = str(
                render(x.get("summary") or f"Approve step {step.id} of {run.workflow}?", ctx)
            )
            item = await self.host.instance.inbox.create(
                "approval", summary[:200], {"run": run.id, "step": step.id, "summary": summary}
            )
            await self.host._approval_filed(item, f"Approval needed: {summary[:120]}")
            raise _Wait("approval")
        elif kind == "wait":
            return self._wait(run, x, ctx)
        elif kind == "branch":
            for case in x.get("cases", []):
                if cel.holds(str(case.get("when", "false")), ctx):
                    run.steps[step.id] = {"goto": case.get("goto")}
                    return str(case["goto"])
            run.steps[step.id] = {"goto": None}
        elif kind == "parallel":
            run.steps[step.id] = await self._parallel(run, x.get("branches", []), ctx)
        elif kind == "handoff":
            run.steps[step.id] = await self._handoff(run, step, x, ctx)
        elif kind == "timer":
            run.steps[step.id] = await self.host.arm_delay(
                str(x["trigger"]), {"run": run.id, **run.input}
            )
        elif kind == "end":
            run.status, run.outcome = "done", str(x.get("outcome") or "completed")
            run.steps[step.id] = {"outcome": run.outcome}
        else:
            raise WorkflowError(f"unknown step type {kind!r}")
        return None

    def _deadline(self, timeout: Any) -> float | None:
        now = self.host.queue.clock()
        return now + duration_days(str(timeout)) * DAY if timeout else None

    def _wait(self, run: Run, x: dict[str, Any], ctx: dict[str, Any]) -> str | None:
        what = x.get("for", "reply")
        deadline = self._deadline(x.get("timeout"))
        if what == "reply":
            target = run.state.get("contact") or {}
            if not target.get("key") or not target.get("channel"):
                raise WorkflowError("wait for a reply needs a contact (send them a message first)")
            raise _Wait(
                "reply", channel=target["channel"], contact=target["key"], deadline=deadline
            )
        if what == "event":
            run.state["wait_match"] = x.get("match")
            raise _Wait("event", event=str(x["event"]), deadline=deadline)
        if what == "time":
            until = self._deadline(x.get("duration")) or deadline
            raise _Wait("time", deadline=until)
        raise WorkflowError(f"cannot wait for {what!r}")

    async def _agent(self, run: Run, name: str, given: Any) -> dict[str, Any]:
        host = self.host
        agent = host.agent(name)
        text = (
            given if isinstance(given, str) else json.dumps(given, ensure_ascii=False, default=str)
        )
        target = run.state.get("contact") or {}
        session_id = run.state.setdefault("sessions", {}).get(name)
        if session_id:
            session = await agent.resume(session_id)
        else:
            session = await agent.new_session(contact_key=target.get("key"))
            if target.get("key") and target.get("channel"):
                await host.instance.store.bind(
                    host.scope, session.id, target["channel"], target["key"]
                )
            run.state["sessions"][name] = session.id
        from ..runtime import answer  # the runtime imports the workflow engine's host

        texts, reason = await answer(agent.send(session, text))
        final = texts[-1] if texts else ""
        output: dict[str, Any] = {"text": final, "reason": reason, "session": session.id}
        parsed = _json_in(final)
        if isinstance(parsed, dict):
            output = {**parsed, **output, "output": parsed}
        if reason != "end_turn":
            raise WorkflowError(f"agent {name} ended with {reason}")
        if target.get("key") and target.get("channel") and texts and parsed is None:
            replied = any(isinstance(v, dict) and v.get("replied") for v in run.steps.values())
            await host.message(
                target["channel"], target["key"], await agent.reply("\n\n".join(texts)),
                session_id=session.id, reply=replied,
            )  # fmt: skip
        return output

    async def _tool(self, run: Run, step: Step, name: str, args: dict[str, Any]) -> Any:
        inst = self.host.instance
        tool = inst.tools.get(name)
        if tool is None:
            raise WorkflowError(f"tool {name} is not available")
        approved = run.steps.get(f"{step.id}__approval")
        decision = inst.policy.decide(name, tool.effect, args, None)
        if decision.verdict is Verdict.DENY:
            raise WorkflowError(f"tool {name} denied by rule {decision.rule!r}")
        call = ToolUseBlock(id=f"wf_{run.id}_{step.id}", name=name, input=args)
        from ..core.session import Session

        system = Session(scope=inst.scope, agent_id=f"workflow:{run.workflow}")
        verdict = await inst.verifier.verify(system, call, tool)
        if not verdict.passed:
            raise WorkflowError(f"verification failed: {verdict.reason}")
        if decision.verdict is Verdict.ASK and approved is None:
            item = await inst.inbox.create(
                "approval", f"Approve {name} in workflow {run.workflow}?",
                {"run": run.id, "step": step.id, "tool": name, "arguments": args, "for_tool": True},
            )  # fmt: skip
            await self.host._approval_filed(item, f"Approval needed: {name} ({run.workflow})")
            run.state["approval_for"] = step.id
            raise _Wait("approval")
        if decision.verdict is Verdict.ASK and not (approved or {}).get("approved"):
            raise WorkflowError(f"{name} was not approved: {(approved or {}).get('note', '')}")
        result = await inst.tools.execute(call)
        await inst.audit.record(
            inst.scope, f"workflow:{run.workflow}", "tool_call", name,
            {"run": run.id, "step": step.id, "status": str(result.status),
             "effect": str(tool.effect)},
        )  # fmt: skip
        if result.status is not ToolStatus.OK:
            raise WorkflowError(f"{name} {result.status}: {result.error}")
        return result.content

    async def _send(
        self, run: Run, kind: str, x: dict[str, Any], ctx: dict[str, Any]
    ) -> dict[str, Any]:
        channel = str(x["channel"])
        to = render(x.get("to"), ctx) or (self.host.instance.spec.channels[channel].address)
        if isinstance(to, dict):
            to = to.get("key") or to.get("phone") or to.get("contact")
        if not to:
            raise WorkflowError(f"{kind} step has no recipient")
        to = str(to)
        if self.host.instance.spec.channels[channel].purpose == "contact":
            run.state["contact"] = {"key": to, "channel": channel}
        if kind == "template":
            variables = {str(k): str(v) for k, v in (render(x.get("vars") or {}, ctx)).items()}
            sent = await self.host.message(
                channel, to, None, template=str(x["template"]), variables=variables
            )
        else:
            body = render(x.get("body") or x.get("text") or "", ctx)
            sent = await self.host.message(channel, to, str(body))
        return {"sent": sent, "to": to, "channel": channel}

    async def _parallel(
        self, run: Run, branches: list[dict[str, Any]], ctx: dict[str, Any]
    ) -> dict[str, Any]:
        async def one(branch: dict[str, Any]) -> tuple[str, Any]:
            step = Step.model_validate(branch)
            if step.type == "agent":
                return step.id, await self._agent(
                    run, str(branch["agent"]), render(branch.get("input"), ctx)
                )
            if step.type == "tool":
                return step.id, await self._tool(
                    run, step, str(branch["tool"]), render(branch.get("args") or {}, ctx)
                )
            raise WorkflowError(f"parallel branches run agent and tool steps, not {step.type}")

        results = dict(await asyncio.gather(*(one(b) for b in branches)))
        run.steps.update(results)
        return results

    async def _handoff(
        self, run: Run, step: Step, x: dict[str, Any], ctx: dict[str, Any]
    ) -> dict[str, Any]:
        to = str(x.get("to", "human"))
        if to != "human":
            return await self._agent(run, to, render(x.get("input") or run.input, ctx))
        inst = self.host.instance
        reason = str(
            render(x.get("reason") or f"workflow {run.workflow} handed off at {step.id}", ctx)
        )
        sessions = list((run.state.get("sessions") or {}).values())
        if sessions:
            agent_name = next(iter(run.state["sessions"]))
            session = await self.host.agent(agent_name).resume(sessions[-1])
            item = await inst.escalate(session, reason, by=f"workflow:{run.workflow}")
        else:
            item = await inst.inbox.create(
                "escalation",
                reason[:200],
                {"run": run.id, "workflow": run.workflow, "steps": run.steps},
            )
            await self.host.notify(item, f"Escalation: {reason[:120]}")
        run.status, run.outcome = "done", str(x.get("outcome") or "handoff")
        return {"escalation": item}


def _json_in(text: str) -> Any:
    """A JSON object an agent answered with (possibly inside a code fence), else None."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").removeprefix("json").strip()
    if not stripped.startswith("{"):
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def is_side_effecting(effect: Effect) -> bool:
    return effect is not Effect.READ
