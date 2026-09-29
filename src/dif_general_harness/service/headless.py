"""The headless runtime (ARCHITECTURE §3.15-3.17): channels, triggers and the inbox, all
driven through the durable queue.

Job kinds:
- ``inbound``: a channel message (deduplicated by the provider's message id). Opt-out
  keywords revoke consent; an escalated conversation is held for a person; otherwise the
  entry agent answers and the reply goes out through the channel.
- ``trigger``: a schedule or webhook firing. A schedule job enqueues its next occurrence
  first (deduplicated per fire time, so restarts never double-fire), then runs its agent.
- ``approved`` / ``approval_timeout``: executes an approved tool call (re-checking deny
  rules first) and runs a follow-up turn so the agent tells the contact; applies
  ``hitl.on_timeout`` when nobody decides in time.
- ``retention``: the daily purge.

REST/web channels answer inline (the reply is in the HTTP response); everything else is
acknowledged at once and processed by the worker.

M2 scope: triggers run agents. Triggers that start a workflow, or that carry a ``when``
condition, need the M3 workflow engine and CEL; they are reported, never half-run.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx2

from ..channels import ChannelAdapter, ChannelError, Envelope, Inbound, Unauthorized, build_adapter
from ..core import cel
from ..core.events import Event, MessageAdded, TurnEnded
from ..core.messages import Message, Role, ToolUseBlock
from ..feedback import ALL_AGENTS
from ..governance import is_opt_out, purge
from ..hitl import InboxApprover, InboxItem
from ..hitl.inbox import Status as InboxStatus
from ..policy import Verdict
from ..runtime import AgentRuntime, Instance
from ..spec.errors import Issue
from ..spec.loader import duration_days
from ..spec.schema import Trigger
from ..tools.registry import Effect
from ..triggers import CronError, next_fire
from ..workflows import Job, JobQueue, Worker
from ..workflows.engine import WorkflowEngine
from ..workflows.render import render as render_value

log = logging.getLogger(__name__)
_EVENT_REF = re.compile(r"\{\{\s*event((?:\.[A-Za-z0-9_]+)*)\s*\}\}")
FALLBACK = "Sorry, I can't answer right now. A person from our team will follow up."
DAY = 86400.0


async def answer(stream: AsyncIterator[Event]) -> tuple[list[str], str]:
    """The assistant texts of a run and how it ended. When the run goes on after a turn
    ended (an answer check asked for a rewrite), only the later texts are the answer."""
    texts: list[str] = []
    reason, ended = "error", False
    async for event in stream:
        if isinstance(event, TurnEnded):
            reason, ended = event.reason, True
        elif (
            isinstance(event, MessageAdded)
            and event.message.role is Role.ASSISTANT
            and event.message.text()
        ):
            if ended:
                texts.clear()
                ended = False
            texts.append(event.message.text())
    return texts, reason


def render_event(template: Any, event: dict[str, Any]) -> str:
    """``{{event.a.b}}`` from a trigger's event payload; the whole event for ``{{event}}``."""
    if not isinstance(template, str):
        return json.dumps(template, ensure_ascii=False)

    def value(m: re.Match[str]) -> str:
        cur: Any = event
        for part in filter(None, m.group(1).split(".")):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            return ""
        return cur if isinstance(cur, str) else json.dumps(cur, ensure_ascii=False)

    return _EVENT_REF.sub(value, template)


@dataclass
class TurnResult:
    reply: str | None
    reason: str
    session_id: str
    escalated: bool = False


@dataclass
class Headless:
    instance: Instance
    queue: JobQueue
    adapters: dict[str, ChannelAdapter] = field(default_factory=dict)
    triggers: dict[str, Trigger] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    _agents: dict[str, AgentRuntime] = field(default_factory=dict)
    _http: httpx2.AsyncClient | None = None
    engine: WorkflowEngine = field(init=False)
    _public_url: str | None = None

    # --- construction ----------------------------------------------------------------

    @classmethod
    async def build(
        cls,
        instance: Instance,
        *,
        http_client: httpx2.AsyncClient | None = None,
        public_url: str | None = None,
        clock: Any = None,
    ) -> Headless:
        queue = JobQueue(instance.db, scope=instance.scope, **({"clock": clock} if clock else {}))
        self = cls(instance, queue, _http=http_client, _public_url=public_url)
        self.engine = WorkflowEngine(self)
        self._wire(instance)
        return self

    def _wire(self, instance: Instance) -> None:
        """Adapters, triggers and approvals for an instance (at build and on reload)."""
        http_client, public_url = self._http, self._public_url
        self.instance = instance
        self.adapters, self.triggers, self.issues, self._agents = {}, {}, [], {}
        instance.notify = self.notify
        instance.emit = self.emit
        instance.options.approver = InboxApprover(instance.inbox, self._approval_filed)
        spec, data = instance.spec, instance.resolved.data
        for name, channel in spec.channels.items():
            if channel.enabled is False or channel.enabled == "false":
                continue
            raw = data["channels"][name].get("credentials")
            try:
                creds = instance.secrets.resolve(raw) if raw is not None else None
                base = f"{public_url.rstrip('/')}/channels/{name}" if public_url else None
                self.adapters[name] = build_adapter(
                    name, channel, creds, client=http_client, public_url=base
                )
            except Exception as exc:  # one broken channel must not stop the others
                self._warn("channel_unavailable", f"channels.{name}", f"{exc}")
        for name, trig in spec.triggers.items():
            why = self._unsupported(trig)
            if why:
                self._warn("trigger_unavailable", f"triggers.{name}", why)
            else:
                self.triggers[name] = trig

    async def reload(self, resolved: Any) -> None:
        """Swap in a new config version without a restart. The database, queue and pending
        jobs carry over; the old instance's connections close once the new one is live."""
        import dataclasses

        old = self.instance
        options = dataclasses.replace(old.options, database=old.db, approver=None)
        new = await Instance.open(resolved, options)
        new.owns_db, old.owns_db = old.owns_db, False  # the database outlives the swap
        self._wire(new)
        await self.start()
        await old.close()

    def _warn(self, code: str, path: str, message: str) -> None:
        self.issues.append(Issue("warning", code, path, message))

    def _unsupported(self, trig: Trigger) -> str | None:
        spec = self.instance.spec
        if trig.type in {"file", "batch"}:
            return f"{trig.type} triggers need a storage connector (not available yet)"
        if trig.workflow and trig.workflow not in spec.workflows:
            return f"unknown workflow {trig.workflow!r}"
        if not trig.workflow:
            if not trig.agent:
                return "the trigger needs an agent or a workflow"
            if "{{" not in trig.agent and trig.agent not in spec.agents:
                return f"unknown agent {trig.agent!r}"
        for condition in (trig.when, trig.unless):
            if condition and cel.check(condition):
                return f"invalid condition: {cel.check(condition)}"
        if trig.type == "relative" and (not trig.source or not trig.offset):
            return "relative triggers need a source and an offset"
        if trig.type == "delay" and not trig.after:
            return "delay triggers need 'after'"
        if trig.type == "event" and not trig.event:
            return "event triggers need an event name"
        if trig.type == "schedule":
            try:
                next_fire(trig.cron or "", time.time())
            except CronError as exc:
                return str(exc)
        return None

    def agent(self, name: str) -> AgentRuntime:
        if name not in self._agents:
            self._agents[name] = self.instance.agent(name)
        return self._agents[name]

    def handlers(self) -> dict[str, Any]:
        return {
            "inbound": self._job_inbound,
            "trigger": self._job_trigger,
            "approved": self._job_approved,
            "approval_timeout": self._job_approval_timeout,
            "retention": self._job_retention,
            "workflow": lambda job: self.engine.advance(job.payload["run"]),
            "workflow_timeout": lambda job: self.engine.timed_out(
                job.payload["run"], job.payload["step"]
            ),
            "relative_scan": self._job_relative_scan,
            "agent_task": self._job_agent_task,
            "memory_extract": self._job_memory_extract,
            "knowledge_sync": self._job_knowledge_sync,
            "source_sync": self._job_source_sync,
        }

    def worker(self, **kw: Any) -> Worker:
        return Worker(self.queue, self.handlers(), **kw)

    async def start(self) -> None:
        """Seed recurring work: the next firing of every schedule, and today's purge."""
        now = self.queue.clock()
        for name, trig in self.triggers.items():
            if trig.type == "schedule":
                await self._schedule_next(name, trig, now)
        if any(t.type == "relative" for t in self.triggers.values()):
            await self.scan_relative()
        for corpus in self.instance.spec.knowledge.corpora:
            await self._sync_next(corpus, now)
        watched = {str(t.source) for t in self.triggers.values() if t.type == "relative"}
        for source in sorted(watched & set(self.instance.sources)):
            await self.queue.enqueue(
                self.scope, "source_sync", {"source": source},
                dedupe_key=f"source_sync:{source}:{int(now)}",
            )  # fmt: skip
        tomorrow = datetime.fromtimestamp(now, UTC).date().isoformat()
        await self.queue.enqueue(
            self.scope, "retention", {}, delay_s=60, dedupe_key=f"retention:{tomorrow}"
        )

    @property
    def scope(self) -> Any:
        return self.instance.scope

    # --- channels --------------------------------------------------------------------

    async def receive(self, channel: str, request: Inbound) -> list[TurnResult] | None:
        """An inbound HTTP request. Inline channels return their replies; queued channels
        return None once the messages are safely queued."""
        adapter = self.adapters.get(channel)
        if adapter is None:
            raise ChannelError(f"unknown or disabled channel {channel!r}")
        envelopes = adapter.parse(request)  # raises Unauthorized
        if adapter.inline_reply:
            return [await self.handle(env) for env in envelopes]
        for env in envelopes:
            dedupe = f"in:{channel}:{env.message_id}" if env.message_id else None
            await self.queue.enqueue(
                self.scope, "inbound", _envelope_json(env), dedupe_key=dedupe, max_attempts=3
            )
        return None

    async def _job_inbound(self, job: Job) -> None:
        env = Envelope(**job.payload)
        result = await self.handle(env)
        if result.reply:
            await self.send(env.channel, env.contact_key, result.reply, result.session_id)

    async def handle(self, env: Envelope) -> TurnResult:
        inst = self.instance
        cfg = inst.spec.channels[env.channel]
        consent = inst.spec.governance.consent
        if consent.opt_out_keywords and is_opt_out(env.text, consent.opt_out_keywords):
            await inst.consent.set(self.scope, env.contact_key, env.channel, "revoked", "keyword")
            await inst.audit.record(
                self.scope, f"contact:{env.channel}", "consent_revoked", env.channel, {}
            )
        if await self.engine.reply(env.channel, env.contact_key, env.text):
            return TurnResult(None, "workflow", "")  # a workflow run was waiting for this
        agent_name = cfg.entry_agent or next(iter(inst.spec.agents))
        agent = self.agent(agent_name)
        window = duration_days(cfg.session_window) * DAY if cfg.session_window else None
        found = await inst.store.current(self.scope, env.channel, env.contact_key, window)
        if found is not None:
            loaded = await inst.store.load(self.scope, found[0])
            if loaded.agent_id in inst.spec.agents:  # the conversation may be with a teammate now
                agent_name, agent = loaded.agent_id, self.agent(loaded.agent_id)
            session = await agent.resume(found[0])
            if found[1] == "escalated":
                return await self._hold_for_person(session, env)
        else:
            session = await agent.new_session(contact_key=env.contact_key)
            await inst.store.bind(self.scope, session.id, env.channel, env.contact_key)

        texts, reason = await answer(agent.send(session, env.text, names=env.names))
        escalated = await inst.store.state(self.scope, session.id) == "escalated"
        if inst.spec.models and "memory_extraction" in inst.spec.models.roles:
            await self.queue.enqueue(
                self.scope, "memory_extract", {"agent": agent_name, "session": session.id},
                delay_s=60, dedupe_key=f"memx:{session.id}:{len(session.messages)}",
            )  # fmt: skip
        handoff = inst.pending_handoffs.pop(session.id, None)
        if handoff is not None and reason == "end_turn":  # the teammate answers in the same turn
            target_name, target_id, note = handoff
            target = self.agent(target_name)
            target_session = await target.resume(target_id)
            async for event in target.send(target_session, note):
                message = event.message if isinstance(event, MessageAdded) else None
                if message is not None and message.role is Role.ASSISTANT and message.text():
                    texts.append(message.text())
            session = target_session
            agent = target
        reply = await agent.reply("\n\n".join(texts)) if texts else None
        if reason in {"error", "budget", "refusal", "max_turns"} and not escalated:
            item = await inst.inbox.create(
                "budget" if reason == "budget" else "escalation",
                f"Turn ended with {reason}",
                {"reason": reason, "agent": agent_name, "contact": env.contact_key},
                session.id,
            )
            await self.notify(item, f"Turn ended with {reason} ({agent_name})")
            reply = reply or FALLBACK
        return TurnResult(reply, reason, session.id, escalated)

    async def _hold_for_person(self, session: Any, env: Envelope) -> TurnResult:
        """An escalated conversation: keep the message for the person, don't let the agent
        answer."""
        inst = self.instance
        safe = await inst.pii.tokenize(env.text, env.names)
        await inst.store.append(session.add_message(Message.user(safe)))
        open_items = [
            i for i in await inst.inbox.list("open", "escalation") if i.session_id == session.id
        ]
        if open_items:
            await self.notify(open_items[0].id, "New message in an escalated conversation")
        return TurnResult(None, "held", session.id, escalated=True)

    async def send(self, channel: str, contact_key: str, text: str, session_id: str | None) -> None:
        adapter = self.adapters.get(channel)
        if adapter is None or adapter.inline_reply:
            return
        message_id = await adapter.send(contact_key, text)
        await self.instance.audit.record(
            self.scope, "system", "message_out", channel,
            {"session": session_id, "provider_id": message_id, "chars": len(text)},
        )  # fmt: skip

    async def notify(self, item_id: str, summary: str) -> None:
        """Tell the people in ``hitl.notify`` (channels with an address); failures are
        logged, never raised: the item is in the inbox either way."""
        hitl = self.instance.spec.hitl
        targets = list(hitl.notify) if hitl else []
        handoff_to = (self.instance.spec.policies.escalation or {}).get("handoff_to") or {}
        if handoff_to.get("type") == "channel" and handoff_to.get("channel"):
            targets.append({"channel": handoff_to["channel"]})
        elif handoff_to.get("type") == "email" and handoff_to.get("to"):
            mail = next((n for n, a in self.adapters.items() if a.config.type == "email"), None)
            if mail is not None:
                targets.append({"channel": mail, "to": handoff_to["to"]})
        for target in targets:
            name = target.get("channel")
            adapter = self.adapters.get(str(name))
            to = target.get("to") or (adapter.config.address if adapter else None)
            if adapter is None or not to or adapter.inline_reply:
                continue
            try:
                await adapter.send(str(to), f"[inbox {item_id}] {summary}")
            except Exception as exc:
                log.warning("notify via %s failed: %s", name, exc)

    # --- triggers ----------------------------------------------------------------------

    async def _schedule_next(self, name: str, trig: Trigger, after: float) -> None:
        tz = self.instance.spec.tenant.timezone if self.instance.spec.tenant else "UTC"
        at = next_fire(trig.cron or "", after, tz)
        await self.queue.enqueue(
            self.scope, "trigger", {"trigger": name, "event": {"fired_at": at}},
            run_at=at, dedupe_key=f"trigger:{name}:{int(at)}",
        )  # fmt: skip

    async def _sync_next(self, corpus: str, after: float) -> None:
        cron = str(
            (self.instance.spec.knowledge.corpora[corpus].get("sync") or {}).get("schedule") or ""
        )
        if not cron:
            return
        tz = self.instance.spec.tenant.timezone if self.instance.spec.tenant else "UTC"
        try:
            at = next_fire(cron, after, tz)
        except CronError as exc:
            log.warning("knowledge corpus %s: bad sync schedule: %s", corpus, exc)
            return
        await self.queue.enqueue(
            self.scope, "knowledge_sync", {"corpus": corpus},
            run_at=at, dedupe_key=f"knowledge_sync:{corpus}:{int(at)}",
        )  # fmt: skip

    async def _job_source_sync(self, job: Job) -> None:
        """Pull a connector's items (upcoming calendar events) for the relative triggers
        watching them, then come back in ``sync_every``."""
        source = job.payload["source"]
        found = self.instance.sources.get(source)
        if found is None:
            return  # the connector was removed since this job was queued
        fetch, every = found
        at = max(job.run_at, self.queue.clock()) + every
        await self.queue.enqueue(
            self.scope, "source_sync", {"source": source}, run_at=at,
            dedupe_key=f"source_sync:{source}:{int(at)}",
        )  # fmt: skip
        items = [i for i in await fetch() if i.get("start")]
        await self.upsert_items(source, items, replace=True)

    async def _job_knowledge_sync(self, job: Job) -> None:
        corpus = job.payload["corpus"]
        if corpus not in self.instance.spec.knowledge.corpora:
            return  # the corpus was removed from the spec since this job was queued
        if not job.payload.get("once"):
            await self._sync_next(corpus, max(job.run_at, self.queue.clock()))
        await self.instance.sync_knowledge(corpus)

    async def fire(
        self, name: str, event: dict[str, Any], delivery_id: str | None = None
    ) -> str | None:
        """Queue a trigger firing (webhooks); duplicates of one delivery are ignored."""
        dedupe = f"trigger:{name}:{delivery_id}" if delivery_id else None
        return await self.queue.enqueue(
            self.scope, "trigger", {"trigger": name, "event": event}, dedupe_key=dedupe
        )

    def webhook(self, path: str) -> tuple[str, Trigger] | None:
        for name, trig in self.triggers.items():
            if trig.type == "webhook" and (trig.path or "").strip("/") == path.strip("/"):
                return name, trig
        return None

    def verify_webhook(self, trig_name: str, request: Inbound) -> None:
        raw = self.instance.resolved.data["triggers"][trig_name].get("auth")
        if raw is None:
            raise Unauthorized("webhook triggers must declare auth")
        secret = self.instance.secrets.resolve(raw)
        if not isinstance(secret, str):
            raise Unauthorized("webhook auth must be a shared secret")
        signed = request.headers.get("x-hub-signature-256") or request.headers.get(
            "x-signature-256", ""
        )
        if signed:
            expected = (
                "sha256=" + hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
            )
            if hmac.compare_digest(signed, expected):
                return
        bearer = request.headers.get("authorization", "")
        if bearer.startswith("Bearer ") and hmac.compare_digest(bearer[7:].strip(), secret):
            return
        raise Unauthorized("invalid webhook signature")

    async def _job_trigger(self, job: Job) -> None:
        name = job.payload["trigger"]
        trig = self.triggers.get(name)
        if trig is None:
            return  # the trigger was removed from the spec since this job was queued
        if trig.type == "schedule":
            await self._schedule_next(name, trig, max(job.run_at, self.queue.clock()))
        event = job.payload.get("event") or {}
        if trig.type == "relative" and not await self._still_due(trig, event):
            return  # the item moved or was removed since this firing was planned
        ctx = {"event": event, "var": self.instance.spec.values, "input": event}
        skip = (trig.when and not cel.holds(trig.when, ctx)) or (
            trig.unless and cel.holds(trig.unless, ctx)
        )
        if skip:
            await self.instance.audit.record(
                self.scope, f"trigger:{name}", "trigger_skipped", "condition", {}
            )
            return
        if trig.requires_consent and not await self._consented(event):
            await self.instance.audit.record(
                self.scope, f"trigger:{name}", "trigger_skipped", "consent", {}
            )
            return
        if trig.workflow:
            given = render_value(trig.input, ctx) if trig.input is not None else event
            contact = event.get("contact") if isinstance(event.get("contact"), str) else None
            payload = given if isinstance(given, dict) else {"input": given, "event": event}
            channel = self._contact_channel() if contact else None
            await self.engine.start(trig.workflow, payload, contact=contact, channel=channel)
            return
        agent_name = str(render_value(trig.agent or "", ctx))
        if agent_name not in self.instance.spec.agents:
            raise ValueError(f"trigger {name} routed to unknown agent {agent_name!r}")
        agent = self.agent(agent_name)
        text = render_event(trig.input or f"Trigger {name} fired.", event)
        session = await agent.new_session()
        texts, reason = await answer(agent.send(session, text))
        await self.instance.audit.record(
            self.scope, f"trigger:{name}", "trigger_run", trig.agent or "",
            {"session": session.id, "reason": reason},
        )  # fmt: skip
        if trig.channel and texts:
            cfg = self.instance.spec.channels[trig.channel]
            to = render_event(trig.to, event) if trig.to else cfg.address
            if to:
                reply = await agent.reply("\n\n".join(texts))
                await self.message(trig.channel, to, reply, session_id=session.id)

    async def message(
        self,
        channel: str,
        to: str,
        text: str | None,
        *,
        session_id: str | None = None,
        template: str | None = None,
        variables: dict[str, str] | None = None,
        reply: bool = False,
    ) -> bool:
        """A message the harness starts. Contacts get it only with consent (a ``reply`` to
        something they just wrote is not a new contact); operator channels (hitl, founder,
        outbound) are exempt. Returns whether it was sent."""
        inst = self.instance
        cfg = inst.spec.channels[channel]
        consent = inst.spec.governance.consent
        required = consent.required and (not consent.channels or channel in consent.channels)
        if (
            not reply
            and cfg.purpose == "contact"
            and not await inst.consent.may_contact(self.scope, to, channel, required=required)
        ):
            await inst.audit.record(
                self.scope, "system", "message_suppressed", channel, {"reason": "no consent"}
            )
            return False
        if template is None:
            await self.send(channel, to, text or "", session_id)
            return True
        adapter = self.adapters.get(channel)
        if adapter is None:
            raise ChannelError(f"channel {channel!r} is not available")
        provider_id = await adapter.send_template(to, template, variables or {})
        await inst.audit.record(
            self.scope, "system", "message_out", channel,
            {"session": session_id, "provider_id": provider_id, "template": template},
        )  # fmt: skip
        return True

    # --- workflows, events, relative and delay triggers ----------------------------------

    def _contact_channel(self) -> str | None:
        consent = self.instance.spec.governance.consent
        contact_channels = [
            n
            for n, c in self.instance.spec.channels.items()
            if c.purpose == "contact" and n in self.adapters
        ]
        preferred = [c for c in consent.channels if c in contact_channels]
        choices = preferred or contact_channels
        return choices[0] if choices else None

    async def _consented(self, event: dict[str, Any]) -> bool:
        contact = event.get("contact")
        if not isinstance(contact, str):
            return True  # nobody to contact: the messages themselves are checked when sent
        channel = self._contact_channel()
        if channel is None:
            return False
        consent = self.instance.spec.governance.consent
        return await self.instance.consent.may_contact(
            self.scope, contact, channel, required=consent.required
        )

    async def fire_agent(self, agent: str, text: str) -> str | None:
        """Give an agent a task outside any conversation (staff, schedules, tests)."""
        if agent not in self.instance.spec.agents:
            raise KeyError(f"unknown agent {agent!r}")
        return await self.queue.enqueue(self.scope, "agent_task", {"agent": agent, "text": text})

    async def _job_memory_extract(self, job: Job) -> None:
        from ..memory.agent import extract_facts

        session = await self.instance.store.load(self.scope, job.payload["session"])
        await extract_facts(self.instance, job.payload["agent"], session)

    async def _job_agent_task(self, job: Job) -> None:
        agent = self.agent(job.payload["agent"])
        session = await agent.new_session()
        reason = "error"
        async for event in agent.send(session, job.payload["text"]):
            if isinstance(event, TurnEnded):
                reason = event.reason
        await self.instance.audit.record(
            self.scope,
            "system",
            "agent_task",
            agent.name,
            {"session": session.id, "reason": reason},
        )

    async def emit(self, name: str, data: dict[str, Any]) -> int:
        """An internal event (``ledger.task_assigned``, ``escalation.resolved``...): resumes
        runs waiting for it and fires ``event`` triggers. Returns how many things it woke."""
        woken = await self.engine.emit(name, data)
        for trig_name, trig in self.triggers.items():
            if trig.type == "event" and trig.event == name:
                await self.fire(trig_name, data)
                woken += 1
        return woken

    async def arm_delay(self, trigger: str, event: dict[str, Any]) -> dict[str, Any]:
        """A workflow ``timer`` step: fire a ``delay`` trigger after its ``after``."""
        trig = self.triggers.get(trigger)
        if trig is None or trig.type != "delay" or not trig.after:
            raise ValueError(f"{trigger!r} is not an available delay trigger")
        at = self.queue.clock() + duration_days(trig.after) * DAY
        await self.queue.enqueue(
            self.scope, "trigger", {"trigger": trigger, "event": event}, run_at=at,
            dedupe_key=f"delay:{trigger}:{event.get('run', '')}",
        )  # fmt: skip
        return {"armed": trigger, "at": at}

    async def upsert_items(
        self, source: str, items: list[dict[str, Any]], *, replace: bool = False
    ) -> int:
        """Items a relative trigger watches (calendar events...): ``{id, start (epoch or
        ISO), ...}``. With ``replace``, items missing from the list are removed."""
        now = time.time()
        db = self.instance.db
        ids = []
        for item in items:
            start = _epoch(item.get("start"))
            item_id = str(item["id"])
            ids.append(item_id)
            await db.execute(
                "INSERT INTO source_items (tenant_id, instance_id, source, item_id, start_ts, data,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (tenant_id, instance_id,"
                " source, item_id) DO UPDATE SET start_ts = excluded.start_ts,"
                " data = excluded.data, updated_at = excluded.updated_at",
                (self.scope.tenant_id, self.scope.instance_id, source, item_id, start,
                 json.dumps(item, ensure_ascii=False, default=str), now),
            )  # fmt: skip
        if replace:
            rows = await db.fetchall(
                "SELECT item_id FROM source_items WHERE tenant_id = ? AND instance_id = ?"
                " AND source = ?",
                (self.scope.tenant_id, self.scope.instance_id, source),
            )
            for row in rows:
                if row["item_id"] not in ids:
                    await db.execute(
                        "DELETE FROM source_items WHERE tenant_id = ? AND instance_id = ?"
                        " AND source = ? AND item_id = ?",
                        (self.scope.tenant_id, self.scope.instance_id, source, row["item_id"]),
                    )
        await self.scan_relative()
        return len(items)

    async def scan_relative(self) -> int:
        """Plan the firing of every relative trigger for every upcoming item."""
        planned = 0
        now = self.queue.clock()
        for name, trig in self.triggers.items():
            if trig.type != "relative":
                continue
            offset = _signed_seconds(trig.offset or "0s")
            rows = await self.instance.db.fetchall(
                "SELECT item_id, start_ts, data FROM source_items WHERE tenant_id = ?"
                " AND instance_id = ? AND source = ?",
                (self.scope.tenant_id, self.scope.instance_id, trig.source),
            )
            for row in rows:
                at = float(row["start_ts"]) + offset
                if at <= now:
                    continue
                event = {**json.loads(row["data"]), "start_ts": float(row["start_ts"])}
                job = await self.queue.enqueue(
                    self.scope, "trigger", {"trigger": name, "event": event}, run_at=at,
                    dedupe_key=f"rel:{name}:{row['item_id']}:{int(float(row['start_ts']))}",
                )  # fmt: skip
                planned += job is not None
        return planned

    async def _still_due(self, trig: Trigger, event: dict[str, Any]) -> bool:
        row = await self.instance.db.fetchone(
            "SELECT start_ts FROM source_items WHERE tenant_id = ? AND instance_id = ?"
            " AND source = ? AND item_id = ?",
            (self.scope.tenant_id, self.scope.instance_id, trig.source, str(event.get("id"))),
        )
        return (
            row is not None and abs(float(row["start_ts"]) - float(event.get("start_ts", -1))) < 1
        )

    async def _job_relative_scan(self, job: Job) -> None:
        await self.scan_relative()
        bucket = int(self.queue.clock() // 600) + 1
        await self.queue.enqueue(
            self.scope, "relative_scan", {}, run_at=bucket * 600.0, dedupe_key=f"relscan:{bucket}"
        )

    # --- approvals ---------------------------------------------------------------------

    async def _approval_filed(self, item_id: str, summary: str) -> None:
        await self.notify(item_id, summary)
        hitl = self.instance.spec.hitl
        if hitl and hitl.approval_timeout:
            await self.queue.enqueue(
                self.scope, "approval_timeout", {"item": item_id},
                delay_s=duration_days(hitl.approval_timeout) * DAY,
                dedupe_key=f"approval_timeout:{item_id}",
            )  # fmt: skip

    async def decide(
        self, item_id: str, approved: bool, by: str, note: str = "", text: str | None = None
    ) -> InboxItem:
        """A person's decision on an inbox item. Approvals queue the action; ``text`` rewords
        a proposed constraint before it is pinned."""
        inst = self.instance
        item = await inst.inbox.get(item_id)
        if item is None:
            raise KeyError(f"no inbox item {item_id!r}")
        yes_no = item.kind in ("approval", "memory", "skill", "constraint")
        status: InboxStatus = ("approved" if approved else "denied") if yes_no else "resolved"
        if not await inst.inbox.decide(item_id, status, by, note):
            raise ValueError(f"inbox item {item_id} was already decided")
        await inst.audit.record(
            self.scope, by, f"inbox_{status}", f"inbox/{item_id}", {"note": note}
        )
        if item.kind == "approval" and item.payload.get("run"):
            await self.engine.decided(str(item.payload["run"]), approved, by, note)
        elif item.kind == "approval":
            await self.queue.enqueue(
                self.scope, "approved", {"item": item_id}, dedupe_key=f"approved:{item_id}"
            )
        elif item.kind == "escalation" and item.session_id:
            await inst.store.set_state(self.scope, item.session_id, "active")
        elif item.kind == "memory" and not approved:  # the person keeps the earlier fact
            await inst.memory.restore(str(item.payload["previous_id"]))
        elif item.kind == "skill":
            skill_status = "active" if approved else "rejected"
            await inst.memory.set_status(str(item.payload["memory_id"]), skill_status)
        elif item.kind == "constraint":
            await inst.constraints.decide(
                str(item.payload["constraint_id"]), "active" if approved else "rejected", by, text
            )
        await self._lesson(item, approved, by, note)
        decided = await inst.inbox.get(item_id)
        assert decided is not None
        return decided

    async def _lesson(self, item: InboxItem, approved: bool, by: str, note: str) -> None:
        """A person's reason is a candidate rule: a denied action, a resolved escalation."""
        if not note.strip() or by.startswith("system:"):
            return
        agent = str(item.payload.get("agent") or ALL_AGENTS)
        if item.kind == "approval" and not approved and not item.payload.get("run"):
            tool = str(item.payload.get("tool"))
            await self.instance.propose_constraint(
                agent,
                f'A person declined {tool} and said: "{note.strip()}". Follow that before'
                f" using {tool} again.",
                "denial",
                {"inbox": item.id, "tool": tool, "arguments": item.payload.get("arguments")},
                key=f"denial:{tool}:{note}",
                session_id=item.session_id,
            )
        elif item.kind == "escalation":
            reason = str(item.payload.get("reason") or item.title)
            await self.instance.propose_constraint(
                agent,
                f"When this comes up ({reason[:160]}): {note.strip()}",
                "escalation",
                {"inbox": item.id, "reason": reason},
                key=f"escalation:{note}",
                session_id=item.session_id,
            )

    async def feedback(self, session_id: str, rating: str, comment: str, by: str) -> str | None:
        """A rating on a conversation (thumbs up or down). A thumbs-down with a comment
        proposes a rule. Returns the proposed constraint's id, if any."""
        inst = self.instance
        try:
            session = await inst.store.load(self.scope, session_id)
        except (FileNotFoundError, ValueError):
            raise KeyError(f"no session {session_id!r}") from None
        answer = next(
            (m.text() for m in reversed(session.messages) if m.role is Role.ASSISTANT), ""
        )
        await inst.audit.record(
            self.scope, by, f"feedback_{rating}", f"session/{session_id}",
            {"comment": comment[:500]},
        )  # fmt: skip
        if rating != "down" or not comment.strip():
            return None
        return await inst.propose_constraint(
            session.agent_id or ALL_AGENTS,
            f"From feedback on an earlier answer: {comment.strip()}",
            "rating",
            {"session": session_id, "answer": answer[:500], "by": by},
            key=f"rating:{comment}",
            session_id=session_id,
        )

    async def _job_approval_timeout(self, job: Job) -> None:
        inst = self.instance
        item = await inst.inbox.get(job.payload["item"])
        if item is None or item.status != "open":
            return
        action = inst.spec.hitl.on_timeout if inst.spec.hitl else None
        if action == "approve":
            await self.decide(
                item.id, True, "system:timeout", "approved on timeout (hitl.on_timeout)"
            )
        elif action == "escalate":
            await self.decide(item.id, False, "system:timeout", "timed out; escalated")
            if item.session_id:
                session = await self.agent(item.payload.get("agent", "")).resume(item.session_id)
                await inst.escalate(session, f"approval {item.id} timed out", by="system:timeout")
        else:
            await self.decide(item.id, False, "system:timeout", "no decision in time")

    async def _job_approved(self, job: Job) -> None:
        inst = self.instance
        item = await inst.inbox.get(job.payload["item"])
        if item is None or item.session_id is None:
            return
        agent = self.agent(item.payload.get("agent", ""))
        session = await agent.resume(item.session_id)
        tool_name = item.payload["tool"]
        if item.status == "approved":
            tool = agent.tools.get(tool_name)
            effect = tool.effect if tool else Effect.EXTERNAL
            args = item.payload.get("arguments") or {}
            decision = inst.policy.decide(tool_name, effect, args, None)
            if tool is None or decision.verdict is Verdict.DENY:  # the rules changed since
                note = f"approval {item.id} could not run: {tool_name} is no longer allowed"
            else:
                call = ToolUseBlock(id=f"approved_{item.id}", name=tool_name, input=args)
                result = await agent.tools.execute(call)
                await inst.audit.record(
                    self.scope, item.decided_by or "person", "approved_action", tool_name,
                    {"inbox": item.id, "status": str(result.status), "session": session.id},
                )  # fmt: skip
                outcome = json.dumps(
                    result.content if result.content is not None else result.error,
                    ensure_ascii=False,
                )
                note = (
                    f"approval {item.id} was granted and {tool_name} ran with status "
                    f"{result.status}: {outcome[:2000]}"
                )
        else:
            note = f"approval {item.id} for {tool_name} was denied" + (
                f": {item.note}" if item.note else ""
            )
        texts, _ = await answer(
            agent.send(session, f"[System note, not from the contact] {note}. Tell the contact.")
        )
        binding = await inst.store.binding(self.scope, session.id)
        if texts and binding is not None:
            await self.send(
                binding[0], binding[1], await agent.reply("\n\n".join(texts)), session.id
            )

    # --- operators ---------------------------------------------------------------------

    async def operator_reply(self, session_id: str, text: str, by: str) -> None:
        """A person answers an escalated conversation through its channel."""
        inst = self.instance
        binding = await inst.store.binding(self.scope, session_id)
        if binding is None:
            raise KeyError(f"session {session_id} has no channel")
        session = await inst.store.load(self.scope, session_id)
        safe = await inst.pii.tokenize(text)
        await inst.store.append(session.add_message(Message.assistant(safe)))
        await self.send(binding[0], binding[1], text, session_id)
        await inst.audit.record(self.scope, by, "operator_reply", f"session/{session_id}", {})

    async def _job_retention(self, job: Job) -> None:
        inst = self.instance
        removed = await purge(inst.db, self.scope, inst.spec.governance.retention)
        removed["memories"] = await inst.memory.purge_expired()
        await inst.audit.record(self.scope, "system", "retention", "purge", removed)
        tomorrow = datetime.fromtimestamp(self.queue.clock() + DAY, UTC).date().isoformat()
        await self.queue.enqueue(
            self.scope, "retention", {}, delay_s=DAY, dedupe_key=f"retention:{tomorrow}"
        )


def _envelope_json(env: Envelope) -> dict[str, Any]:
    return {
        "channel": env.channel,
        "contact_key": env.contact_key,
        "text": env.text,
        "message_id": env.message_id,
        "names": env.names,
        "attachments": env.attachments,
    }


__all__ = ["FALLBACK", "Headless", "TurnResult", "render_event"]


def _epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _signed_seconds(offset: str) -> float:
    text = offset.strip()
    sign = -1.0 if text.startswith("-") else 1.0
    return sign * duration_days(text.lstrip("+-")) * DAY
