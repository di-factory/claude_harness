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
from .gates import Failure, Gate, Rules, run_gated
from .records import RunRecords
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


class _Stop(Exception):
    """The run's stop condition held or a cap was hit."""

    def __init__(self, reason: str, unfinished: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason, self.unfinished = reason, unfinished or []


class _NeedsHuman(Exception):
    """A gated return failed twice (or was malformed): a person takes it from here."""

    def __init__(self, what: str, failures: list[Failure]) -> None:
        super().__init__(what)
        self.what, self.failures = what, failures


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
        state: dict[str, Any] = {"pc": 0, "steps": {}, "started": time.time(), "counts": {}}
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
            "counts": run.state.get("counts") or {},
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
            try:
                self._check_caps(run, wf)
            except _Stop as stop:
                await self._stopped(run, wf, stop)
                return
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
            except _Stop as stop:
                await self._stopped(run, wf, stop)
                return
            except _NeedsHuman as needs:
                await self._needs_human(run, wf, step, needs)
                return
            except Exception as exc:
                await self._fail(run, wf, step, exc)
                return
            if run.status != "running":
                break
            run.state["pc"] = ids.index(goto) if goto else pc + 1
            await self._save(run)
            if wf.stop and wf.stop.when and cel.holds(wf.stop.when, self._ctx(run)):
                run.status, run.outcome = "done", "condition"
                break
        else:
            await self._fail(
                run, wf, wf.steps[int(run.state["pc"])], WorkflowError("too many steps")
            )
            return
        await self._save(run)
        await self._record(run, run.outcome or run.status)
        await self.host.instance.audit.record(
            self.scope, "workflow", f"workflow_{run.status}", run.workflow,
            {"run": run.id, "outcome": run.outcome},
        )  # fmt: skip
        await self.emit(
            f"workflow.{run.workflow}.{run.status}", {"run": run.id, "outcome": run.outcome}
        )

    # --- counts, caps and the run record ------------------------------------------------

    def count(self, run: Run, key: str, n: int = 1) -> None:
        counts = run.state.setdefault("counts", {})
        counts[key] = int(counts.get(key, 0)) + n

    def failed(self, run: Run, item: str, failure: Failure) -> None:
        run.state.setdefault("failures", []).append(
            {"item": item, "gate": failure.gate, "reason": failure.reason[:300]}
        )

    def _check_caps(self, run: Run, wf: Workflow, unfinished: list[str] | None = None) -> None:
        stop = wf.stop
        if stop is None:
            return
        counts = run.state.get("counts") or {}
        if stop.max_agents is not None and int(counts.get("agents", 0)) >= stop.max_agents:
            raise _Stop("cap_agents", unfinished)
        started = float(run.state.get("started") or time.time())
        if stop.max_minutes is not None and time.time() - started >= stop.max_minutes * 60:
            raise _Stop("cap_minutes", unfinished)

    async def _stopped(self, run: Run, wf: Workflow, stop: _Stop) -> None:
        """A cap: record the run, give the unfinished list to a person, exit."""
        run.status, run.outcome = "done", stop.reason
        if stop.reason.startswith("cap_"):
            ids = [s.id for s in wf.steps]
            left = stop.unfinished or ids[int(run.state.get("pc", 0)) :]
            if left:
                item = await self.host.instance.inbox.create(
                    "review", f"{run.workflow} stopped at its cap ({stop.reason}): "
                    f"{len(left)} left", {"run": run.id, "unfinished": left[:200]},
                )  # fmt: skip
                await self.host.notify(item, f"{run.workflow}: {len(left)} item(s) unfinished")
            run.state["unfinished"] = left[:200]
        await self._save(run)
        await self._record(run, stop.reason)
        await self.host.instance.audit.record(
            self.scope, "workflow", "workflow_done", run.workflow,
            {"run": run.id, "outcome": run.outcome},
        )  # fmt: skip

    async def _needs_human(self, run: Run, wf: Workflow, step: Step, needs: _NeedsHuman) -> None:
        run.status, run.outcome = "escalated", "needs_human"
        self.count(run, "escalated")
        item = await self.host.instance.inbox.create(
            "review", f"{run.workflow}: {needs.what} needs a person",
            {"run": run.id, "step": step.id,
             "failures": [{"gate": f.gate, "reason": f.reason} for f in needs.failures]},
        )  # fmt: skip
        await self.host.notify(item, f"{run.workflow}: {needs.what} failed its checks twice")
        await self._save(run)
        await self._record(run, "needs_human")

    async def _record(self, run: Run, stop_reason: str) -> None:
        state = run.state
        await RunRecords(self.db, self.scope).append(
            "workflow", run.workflow, started=float(state.get("started") or time.time()),
            stop_reason=stop_reason, counts=state.get("counts"), failures=state.get("failures"),
            run=run.id, unfinished=state.get("unfinished"),
            alias_collisions=state.get("alias_collisions"), diff=state.get("diff"),
        )  # fmt: skip

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
        await self._record(run, "failed")
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
                run, str(x["agent"]), render(x.get("input"), ctx), gate=x.get("gate")
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
        elif kind == "foreach":
            run.steps[step.id] = await self._foreach(run, step, x, ctx)
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

    async def _agent(
        self, run: Run, name: str, given: Any, *, gate: Any = None, fresh: bool = False,
        label: str | None = None, rules: Rules | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        host = self.host
        agent = host.agent(name)
        wf = self.workflow(run.workflow)
        checks = Gate.from_step(gate, agent.spec.output_schema) if gate is not None or (
            agent.spec.output_schema) else None  # fmt: skip
        if checks is not None and rules is not None:
            checks.rules = rules
        text = (
            given if isinstance(given, str) else json.dumps(given, ensure_ascii=False, default=str)
        )
        target = run.state.get("contact") or {}
        session_id = None if fresh else run.state.setdefault("sessions", {}).get(name)
        if session_id:
            session = await agent.resume(session_id)
        else:
            session = await agent.new_session(contact_key=target.get("key"))
            if target.get("key") and target.get("channel"):
                await host.instance.store.bind(
                    host.scope, session.id, target["channel"], target["key"]
                )
            if not fresh:
                run.state.setdefault("sessions", {})[name] = session.id
        from ..runtime import answer  # the runtime imports the workflow engine's host

        texts: list[str] = []

        async def attempt(message: str) -> str:
            self._check_caps(run, wf)
            self.count(run, "agents")
            said, why = await answer(agent.send(session, message))
            if why != "end_turn":
                raise WorkflowError(f"agent {name} ended with {why}")
            texts[:] = said
            return said[-1] if said else ""

        what = label or name
        if checks is not None:
            gated = await run_gated(host.instance, checks, text, attempt)
            for failure in gated.failures:
                self.failed(run, what, failure)
            if gated.retried:
                self.count(run, "retried")
            if not gated.passed:
                raise _NeedsHuman(what, gated.failures)
            self.count(run, "passed")
            final = json.dumps(gated.output, ensure_ascii=False)
            output: dict[str, Any] = {**(gated.output or {}), "text": final, "reason": "end_turn",
                                      "session": session.id, "output": gated.output}  # fmt: skip
            return output
        final = await attempt(text)
        output = {"text": final, "reason": "end_turn", "session": session.id}
        parsed = _json_in(final)
        if isinstance(parsed, dict):
            output = {**parsed, **output, "output": parsed}
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
        result = await self._once(run, step, tool, call)
        await inst.audit.record(
            inst.scope, f"workflow:{run.workflow}", "tool_call", name,
            {"run": run.id, "step": step.id, "status": str(result.status),
             "effect": str(tool.effect)},
        )  # fmt: skip
        if result.status is not ToolStatus.OK:
            raise WorkflowError(f"{name} {result.status}: {result.error}")
        return result.content

    async def _once(self, run: Run, step: Step, tool: Any, call: ToolUseBlock) -> Any:
        """A side-effecting tool step through the intent log: one that crashed half way is
        not run again (a person checks), one that finished returns its recorded result."""
        from ..core.messages import ToolResultBlock
        from ..runtime.intents import IntentLog, intent_key, policy_for
        from ..tools.registry import IDEMPOTENCY_KEY

        inst = self.host.instance
        if policy_for(tool, inst.spec.tools.overrides) == "safe":
            return await inst.tools.execute(call)
        log = IntentLog(inst.db, inst.scope)
        key = intent_key(inst.scope, f"workflow:{run.id}", f"{step.id}:{call.name}", call.input)
        prior = await log.get(key, within=30 * DAY)
        if prior is not None and prior.status in ("started", "unknown"):
            raise WorkflowError(
                f"{call.name} may already have run (its outcome is unknown); a person must check"
                " before this step runs again"
            )
        if prior is not None and prior.status == "done":
            return ToolResultBlock(tool_use_id=call.id, status=ToolStatus.OK,
                                   content=prior.result)  # fmt: skip
        await log.start(key, f"workflow:{run.id}", call.name)
        token = IDEMPOTENCY_KEY.set(key)
        try:
            result = await inst.tools.execute(call)
        finally:
            IDEMPOTENCY_KEY.reset(token)
        if result.status is ToolStatus.OK:
            await log.finish(key, "done", result.content)
        else:
            unknown = result.side_effects in ("unknown", "committed")
            await log.finish(key, "unknown" if unknown else "failed")
        return result

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
                    run,
                    str(branch["agent"]),
                    render(branch.get("input"), ctx),
                    gate=branch.get("gate"),
                )
            if step.type == "tool":
                return step.id, await self._tool(
                    run, step, str(branch["tool"]), render(branch.get("args") or {}, ctx)
                )
            raise WorkflowError(f"parallel branches run agent and tool steps, not {step.type}")

        results = dict(await asyncio.gather(*(one(b) for b in branches)))
        run.steps.update(results)
        return results

    async def _foreach(
        self, run: Run, step: Step, x: dict[str, Any], ctx: dict[str, Any]
    ) -> dict[str, Any]:
        """One agent per graph node the launch query selects, routed by the node's state,
        each return gated; nodes land first, then the pass's edges; passes until nothing is
        left, the run's stop condition holds, or a cap. See ``graph/store.py``."""
        from ..graph.store import RETURN_SCHEMA, GraphStore
        from ..graph.tools import return_rules

        inst = self.host.instance
        wf = self.workflow(run.workflow)
        name = str(x["graph"])
        store = GraphStore(inst.db, inst.scope, name, inst.spec.graphs[name])
        launch = str(x.get("launch") or "state != 'fresh'")
        limit = int(x.get("limit") or 20)
        passes = int(x.get("passes") or 3)
        width = int(x.get("concurrency") or 4)
        diff = run.state.setdefault("diff", {"nodes_added": [], "nodes_verified": [],
                                             "edges_added": [], "needs_human": []})  # fmt: skip
        for label in _labels(render(x.get("seed"), ctx)):
            node, created = await store.ensure(label)
            if created:
                diff["nodes_added"].append(node.label)
        only = {store.key(lbl) for lbl in _labels(render(x.get("only"), ctx))}
        gate_cfg = {"schema": RETURN_SCHEMA, "verify": True, "threshold": store.spec.threshold,
                    **(x.get("gate") or {})}  # fmt: skip
        touched: set[str] = set()
        quiet = 0
        done = {"passes": 0, "researched": 0}
        for _ in range(passes):
            nodes = [n for n in await store.nodes() if n.id not in touched
                     and (not only or store.key(n.label) in only)]  # fmt: skip
            facts = {n.id: store.facts(n) for n in nodes}
            chosen = [
                n
                for n in nodes
                if facts[n.id]["state"] not in ("fresh", "needs_human")
                and cel.holds(launch, {**ctx, "node": facts[n.id], **facts[n.id]})
            ]
            chosen.sort(key=lambda n: (-n.inbound, n.label))  # most connected first
            chosen = chosen[:limit]
            if not chosen:
                break
            done["passes"] += 1
            self.count(run, "passes")
            slots = asyncio.Semaphore(width)
            unfinished: list[str] = []

            async def research(
                node: Any, state: str, slots: asyncio.Semaphore = slots,
                unfinished: list[str] = unfinished,
            ) -> tuple[Any, str, list[dict[str, Any]]]:  # fmt: skip
                async with slots:
                    returns: list[dict[str, Any]] = []
                    for look in ROUTES[state]:
                        try:
                            self._check_caps(run, wf)
                            task = self._research_task(node, state, look, x, ctx)
                            out = await self._agent(
                                run, str(x["agent"]), task, gate=gate_cfg, fresh=True,
                                label=node.label, rules=return_rules(store, node.label),
                            )  # fmt: skip
                            returns.append(out["output"])
                        except _Stop:
                            unfinished.append(node.label)
                            return node, "unfinished", []
                        except _NeedsHuman:
                            return node, "needs_human", []
                        except WorkflowError as exc:
                            self.failed(run, node.label, Failure("agent", str(exc)))
                            return node, "needs_human", []
                    return node, state, returns

            results = await asyncio.gather(*(research(n, facts[n.id]["state"]) for n in chosen))
            verified_now = 0
            landed: list[tuple[Any, list[dict[str, Any]]]] = []
            for node, state, returns in results:
                touched.add(node.id)
                if state == "unfinished":
                    continue
                if state == "needs_human":
                    await store.set_status(node, "needs_human")
                    diff["needs_human"].append(node.label)
                    self.count(run, "escalated")
                    continue
                result = await store.land(node, returns, recheck=state == "contradicted")
                done["researched"] += 1
                self.count(run, "researched")
                if result.verified:
                    verified_now += 1
                    diff["nodes_verified"].append(node.label)
                    self.count(run, "verified")
                if result.contradicted:
                    self.count(run, "contradicted")
                landed.append((node, returns))
            known = await store.nodes()
            for node, returns in landed:  # every node of the pass is in: now the edges
                drawn = await store.draw(node, returns, run.id, known)
                diff["edges_added"] += drawn.added
                diff["nodes_added"] += drawn.discovered
                if drawn.dropped:
                    self.count(run, "edges_dropped", drawn.dropped)
                if drawn.collisions:
                    run.state.setdefault("alias_collisions", []).extend(drawn.collisions)
                if drawn.discovered:
                    known = await store.nodes()
            quiet = 0 if verified_now else quiet + 1
            counts = run.state.setdefault("counts", {})
            counts["passes_without_new"] = quiet
            counts["graph_verified"] = sum(1 for n in known if n.status == "verified")
            await self._save(run)
            if unfinished:
                raise _Stop(self._cap_reason(run, wf), unfinished)
            if wf.stop and wf.stop.when and cel.holds(wf.stop.when, self._ctx(run)):
                break
        if diff["needs_human"]:
            item = await inst.inbox.create(
                "review", f"{run.workflow}: {len(diff['needs_human'])} item(s) failed their"
                " checks twice", {"run": run.id, "graph": name, "items": diff["needs_human"],
                                  "failures": run.state.get("failures", [])[-50:]},
            )  # fmt: skip
            await self.host.notify(item, f"{run.workflow}: some items need a person")
        if x.get("report", True) and (diff["nodes_added"] or diff["nodes_verified"]
                                      or diff["edges_added"]):  # fmt: skip
            item = await inst.inbox.create(
                "report", f"{run.workflow}: +{len(diff['nodes_added'])} node(s),"
                f" {len(diff['nodes_verified'])} verified, +{len(diff['edges_added'])} edge(s)",
                {"run": run.id, "graph": name, "diff": diff},
            )  # fmt: skip
            await self.host.notify(item, f"{run.workflow}: the {name} graph changed")
        return {**done, "graph": name}

    def _cap_reason(self, run: Run, wf: Workflow) -> str:
        try:
            self._check_caps(run, wf)
        except _Stop as stop:
            return stop.reason
        return "cap_agents"

    def _research_task(
        self, node: Any, state: str, look: str, x: dict[str, Any], ctx: dict[str, Any]
    ) -> str:
        extra = render(x.get("input"), {**ctx, "node": {"label": node.label, "type": node.type}})
        known = json.dumps({"sources": node.sources, "fields": node.fields},
                           ensure_ascii=False, default=str)  # fmt: skip
        parts = [
            f"Research: {node.label} ({node.type}).",
            ROUTE_TASKS[look].format(checked=node.last_checked or "never"),
            "" if state == "new" else f"What the graph has now: {known[:4000]}",
            str(extra or ""),
            RETURN_FORMAT,
        ]
        return "\n\n".join(p for p in parts if p)

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


# a node's state -> the looks it gets (two independent looks for a contradiction)
ROUTES: dict[str, list[str]] = {
    "new": ["full"], "thin": ["sources"], "stale": ["delta"],
    "contradicted": ["primary", "secondary"],
}  # fmt: skip
ROUTE_TASKS = {
    "full": "Full research: identify it, pull primary sources, extract the fields.",
    "sources": "Sources only: find independent sources (other sites) for what the graph has;"
    " do not research it again from scratch.",
    "delta": "Changes only: what changed since it was last checked ({checked})? Keep what"
    " still holds; return the current values with dates.",
    "primary": "Start from primary sources only (filings, the entity's own site and"
    " documents), and settle the conflicting fields with what they state.",
    "secondary": "Start from independent coverage (not the entity's own pages), and settle"
    " the conflicting fields with what it states.",
}
RETURN_FORMAT = """Return JSON only, nothing else:
{"label": "<the entity's name>", "type": "<node type>", "sources": [{"url": "...", "date":
"YYYY-MM-DD"}] (at most 3), "fields": {...}, "candidate_edges": [{"target": "<other entity>",
"relation": "<edge type>", "evidence": "<the source line that shows it>", "confidence": 0-1}],
"confidence": 0-1, "flagged": false}
Conflicting values: return both, each with its date; never average. Under 0.6 confidence:
return it with "flagged": true rather than leaving it out."""


def _labels(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        label = item.get("label") if isinstance(item, dict) else item
        if isinstance(label, str) and label.strip():
            out.append(label.strip())
    return out


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
