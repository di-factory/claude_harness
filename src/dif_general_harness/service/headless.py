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
- ``file_scan``: polls a ``file`` trigger's folder or bucket and fires it once per new file
  (by its ``dedupe_key``); ``batch_run``: fans a ``batch`` trigger out, one firing per item.

REST/web channels answer inline (the reply is in the HTTP response); everything else is
acknowledged at once and processed by the worker.

A trigger that cannot run here (a missing source, a bad schedule) is reported as a warning
and left off, never half-run.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx2

from ..channels import ChannelAdapter, ChannelError, Envelope, Inbound, Unauthorized, build_adapter
from ..core import cel
from ..core.events import MessageAdded, TurnEnded
from ..core.messages import Message, Role, ToolStatus, ToolUseBlock
from ..feedback import ALL_AGENTS
from ..governance import is_opt_out, purge
from ..hitl import InboxApprover, InboxItem
from ..hitl.inbox import Status as InboxStatus
from ..policy import Verdict
from ..runtime import AgentRuntime, Instance, answer
from ..spec.errors import Issue
from ..spec.loader import duration_days
from ..spec.schema import Trigger
from ..tools.registry import Effect
from ..triggers import CronError, next_fire
from ..triggers import files as file_sources
from ..verify.output import review_output, wants_review
from ..workflows import Job, JobQueue, Worker
from ..workflows.engine import WorkflowEngine
from ..workflows.records import RunRecords
from ..workflows.render import render as render_value
from . import hooks
from . import review as weekly_review

log = logging.getLogger(__name__)
_EVENT_REF = re.compile(r"\{\{\s*event((?:\.[A-Za-z0-9_]+)*)\s*\}\}")
STOPPED = {"error", "budget", "refusal", "max_turns", "stuck", "timeout", "overflow"}
FALLBACK = "Sorry, I can't answer right now. A person from our team will follow up."
DAY = 86400.0


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
    file_sources: dict[str, file_sources.FileSource] = field(default_factory=dict)

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
        self.file_sources = {}
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
            if why is None and trig.type == "file":
                why = self._file_source(name, trig)
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
        if trig.type == "batch" and trig.source is not None:
            tool = _batch_tool(trig.source)
            if not tool or self.instance.tools.get(tool) is None:
                return f"batch source tool {tool!r} is not available"
            if self.instance.tools.get(tool).effect is not Effect.READ:  # type: ignore[union-attr]
                return f"batch source {tool} must be a read tool"
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

    def _file_source(self, name: str, trig: Trigger) -> str | None:
        source = self.instance.file_sources.get(name)
        if source is None:
            why = self.instance.file_source_errors.get(name, "not configured")
            return f"file source: {why}"
        self.file_sources[name] = source
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
            "file_scan": self._job_file_scan,
            "output_review": self._job_output_review,
            "batch_run": lambda job: self.run_batch(job.payload["trigger"]),
            "replay_watch": self._job_replay_watch,
            "hook": self._job_hook,
            "weekly_review": self._job_weekly_review,
        }

    def worker(self, **kw: Any) -> Worker:
        return Worker(self.queue, self.handlers(), **kw)

    async def start(self) -> None:
        """Seed recurring work: the next firing of every schedule, and today's purge."""
        now = self.queue.clock()
        for name, trig in self.triggers.items():
            if trig.type == "schedule" or (trig.type == "batch" and trig.cron):
                await self._schedule_next(name, trig, now)
            if name in self.file_sources:
                await self.queue.enqueue(
                    self.scope, "file_scan", {"trigger": name},
                    dedupe_key=f"file_scan:{name}:{int(now)}",
                )  # fmt: skip
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
        if self._watching():
            await self.queue.enqueue(
                self.scope, "replay_watch", {"reason": "the nightly check", "daily": True},
                delay_s=DAY, dedupe_key=f"replay_watch:{tomorrow}",
            )  # fmt: skip
        if weekly_review.enabled(self.instance):
            week = datetime.fromtimestamp(now, UTC).strftime("%G-W%V")
            await self.queue.enqueue(
                self.scope, "weekly_review", {"weekly": True}, delay_s=7 * DAY,
                dedupe_key=f"weekly_review:{week}",
            )  # fmt: skip

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

        first_agent, start = agent_name, len(session.messages)
        texts, reason = await answer(agent.send(session, env.text, names=env.names, route=True))
        calls = _tool_calls(session, start)
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
            target_start = len(target_session.messages)
            async for event in target.send(target_session, note):
                message = event.message if isinstance(event, MessageAdded) else None
                if message is not None and message.role is Role.ASSISTANT and message.text():
                    texts.append(message.text())
            session = target_session
            agent = target
            calls += _tool_calls(session, target_start)
        reply = await agent.reply("\n\n".join(texts)) if texts else None
        if reason in STOPPED and not escalated:
            item = await inst.inbox.create(
                "budget" if reason == "budget" else "escalation",
                f"Turn ended with {reason}",
                {"reason": reason, "agent": agent_name, "contact": env.contact_key},
                session.id,
            )
            await self.notify(item, f"Turn ended with {reason} ({agent_name})")
            reply = reply or FALLBACK
        elif reply and wants_review(inst, session.id, len(session.messages)):
            await self.queue.enqueue(
                self.scope, "output_review",
                {"agent": agent.name, "session": session.id, "upto": len(session.messages)},
                dedupe_key=f"review:{session.id}:{len(session.messages)}",
            )  # fmt: skip
        if inst.spec.hooks:
            ref = hooks.contact_ref(self.scope.tenant_id, env.contact_key)
            base = {"session": session.id, "agent": agent.name, "channel": env.channel,
                    "contact_ref": ref}  # fmt: skip
            for call in calls:
                used = inst.tools.get(call["tool"])
                effect = str(used.effect) if used is not None else None
                await self.hook("tool_call", {**base, **call, "effect": effect})
            if handoff is not None:
                await self.hook("handoff", {**base, "from": first_agent, "to": agent.name})
            if escalated:
                why = next((i.payload.get("reason") for i in await inst.inbox.list(
                    "open", "escalation") if i.session_id == session.id), None)  # fmt: skip
                await self.hook("escalation", {**base, "reason": why}, private=("reason",))
            await self.hook("turn_end", {**base, "reason": reason, "reply": reply},
                            private=("reply",))  # fmt: skip
        return TurnResult(reply, reason, session.id, escalated)

    async def hook(self, event: str, data: dict[str, Any], private: tuple[str, ...] = ()) -> None:
        """Queue a signed delivery to every hook that wants ``event`` (``private`` fields go
        only to hooks with ``texts: true``)."""
        inst = self.instance
        for name, spec in inst.spec.hooks.items():
            if not hooks.wants(spec, event, data):
                continue
            sent = data if spec.texts else {k: v for k, v in data.items() if k not in private}
            await self.queue.enqueue(
                self.scope, "hook", {"hook": name, "event": event, "data": sent,
                                     "at": time.time()}, max_attempts=6,
            )  # fmt: skip

    async def _job_hook(self, job: Job) -> None:
        inst = self.instance
        name, event = str(job.payload["hook"]), str(job.payload["event"])
        spec = inst.spec.hooks.get(name)
        if spec is None:
            return  # the hook was removed since
        raw = inst.resolved.data["hooks"][name]["secret"]
        secret = str(inst.secrets.resolve(raw))
        scope = self.scope
        payload = hooks.body(event, job.payload["data"], tenant=scope.tenant_id,
                             instance=scope.instance_id, at=float(job.payload["at"]))  # fmt: skip
        stamp = str(int(time.time()))
        headers = {"content-type": "application/json", "x-dif-event": event,
                   "x-dif-delivery": job.id, "x-dif-timestamp": stamp,
                   "x-dif-signature": hooks.sign(secret, stamp, payload)}  # fmt: skip
        client = self._http
        if client is None:
            from ..tools.packs.general import guarded_client

            client = guarded_client()
        try:
            response = await client.post(spec.url, content=payload, headers=headers)
        finally:
            if client is not self._http:
                await client.aclose()
        await inst.audit.record(self.scope, "system", "hook_sent", f"hooks/{name}",
                                {"event": event, "status": response.status_code})  # fmt: skip
        if response.status_code >= 300:
            raise RuntimeError(f"hook {name}: HTTP {response.status_code}")  # retried

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
        if adapter is None or (adapter.inline_reply and not getattr(adapter, "outbound", False)):
            return  # REST/web replies travel in the HTTP response; voice can place a call
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
        kind = "batch_run" if trig.type == "batch" else "trigger"
        await self.queue.enqueue(
            self.scope, kind, {"trigger": name, "event": {"fired_at": at}},
            run_at=at, dedupe_key=f"{kind}:{name}:{int(at)}",
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

    # --- file and batch triggers ------------------------------------------------------

    async def _seen(self, trigger: str, key: str, *, mark: bool) -> bool:
        """Whether a trigger has seen ``key``; with ``mark``, records it (True if it was new
        and is now recorded, so a concurrent scan cannot claim it too)."""
        scope = self.scope
        if not mark:
            row = await self.instance.db.fetchone(
                "SELECT 1 AS hit FROM trigger_seen WHERE tenant_id = ? AND instance_id = ?"
                " AND trigger_name = ? AND seen_key = ?",
                (scope.tenant_id, scope.instance_id, trigger, key),
            )
            return row is not None
        inserted = await self.instance.db.execute(
            "INSERT INTO trigger_seen (tenant_id, instance_id, trigger_name, seen_key, seen_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (scope.tenant_id, scope.instance_id, trigger, key, self.queue.clock()),
        )
        return bool(inserted)

    async def _job_file_scan(self, job: Job) -> None:
        name = job.payload["trigger"]
        trig, source = self.triggers.get(name), self.file_sources.get(name)
        if trig is None or source is None:
            return  # the trigger was removed since this job was queued
        options = trig.source if isinstance(trig.source, dict) else {}
        if not job.payload.get("once"):
            every = duration_days(str(options.get("poll") or "1m")) * DAY
            at = max(job.run_at, self.queue.clock()) + every
            await self.queue.enqueue(
                self.scope, "file_scan", {"trigger": name}, run_at=at,
                dedupe_key=f"file_scan:{name}:{int(at)}",
            )  # fmt: skip
        await self.scan_files(name)

    async def scan_files(self, name: str) -> int:
        """Read the new files of a file trigger and fire it once per new dedupe value.
        Returns how many firings were queued."""
        trig, source = self.triggers[name], self.file_sources[name]
        options = trig.source if isinstance(trig.source, dict) else {}
        max_bytes = int(options.get("max_bytes") or file_sources.DEFAULT_MAX_BYTES)
        inline = int(options.get("inline_bytes") or file_sources.DEFAULT_INLINE_BYTES)
        audit, actor = self.instance.audit, f"trigger:{name}"
        fired = read = 0
        for ref in await source.list():
            if read >= file_sources.SCAN_LIMIT:
                break  # the rest wait for the next scan
            version = f"obj:{ref.key}@{ref.version}"
            if not file_sources.matches(ref.key, trig.match) or await self._seen(
                name, version, mark=False
            ):
                continue
            if ref.size > max_bytes:
                await audit.record(self.scope, actor, "file_skipped", ref.key,
                                   {"reason": "too large", "size": ref.size})  # fmt: skip
                await self._seen(name, version, mark=True)
                continue
            read += 1
            data = await source.read(ref.key)
            digest = hashlib.sha256(data).hexdigest()
            event = file_sources.file_event(source, ref, data, digest, inline)
            value = file_sources.dotted(event, trig.dedupe_key or "file.sha256")
            dedupe = _dedupe_value(value if value is not None else digest)
            if not await self._seen(name, f"key:{dedupe}", mark=False):
                await self.queue.enqueue(
                    self.scope, "trigger", {"trigger": name, "event": event},
                    dedupe_key=f"file:{name}:{dedupe}",
                )  # fmt: skip
                await self._seen(name, f"key:{dedupe}", mark=True)
                await audit.record(self.scope, actor, "file_received", ref.key,
                                   {"sha256": digest, "size": len(data)})  # fmt: skip
                fired += 1
            else:
                await audit.record(self.scope, actor, "file_duplicate", ref.key,
                                   {"sha256": digest})  # fmt: skip
            await self._seen(name, version, mark=True)
        return fired

    async def read_file(self, uri: str) -> bytes:
        """The bytes of a file a file trigger announced (by its ``uri``)."""
        return await self.instance.read_document(uri)

    async def run_batch(self, name: str, items: list[Any] | None = None) -> dict[str, Any]:
        """Fan a batch trigger out: one firing per item (from its source tool, or the
        ``items`` given), skipping items whose ``dedupe_key`` value was seen before."""
        trig = self.triggers.get(name)
        if trig is None or trig.type != "batch":
            raise KeyError(f"{name!r} is not an available batch trigger")
        if items is None:
            if trig.source is None:
                raise ValueError(f"batch trigger {name} has no source; push its items")
            items = await self._batch_items(name, trig)
        options = trig.source if isinstance(trig.source, dict) else {}
        limit = int(options.get("max_items") or BATCH_MAX_ITEMS)
        batch_id = uuid.uuid4().hex[:12]
        queued = skipped = 0
        for index, item in enumerate(items[:limit]):
            event = {"item": item, "batch": {"id": batch_id, "index": index,
                                             "total": min(len(items), limit)}}  # fmt: skip
            if trig.dedupe_key:
                value = file_sources.dotted(event, trig.dedupe_key)
                if value is None:
                    skipped += 1
                    continue
                key = _dedupe_value(value)
                if await self._seen(name, f"key:{key}", mark=False):
                    skipped += 1
                    continue
                job_key = f"batch:{name}:{key}"
            else:
                job_key = f"batch:{name}:{batch_id}:{index}"
            await self.queue.enqueue(
                self.scope, "trigger", {"trigger": name, "event": event}, dedupe_key=job_key
            )
            if trig.dedupe_key:
                await self._seen(name, f"key:{key}", mark=True)
            queued += 1
        summary = {"batch": batch_id, "items": len(items), "queued": queued, "skipped": skipped,
                   "truncated": max(0, len(items) - limit)}  # fmt: skip
        await self.instance.audit.record(
            self.scope, f"trigger:{name}", "batch_started", name, summary
        )
        await RunRecords(self.instance.db, self.scope).append(
            "batch", name, started=time.time(), stop_reason="queued",
            counts={"items": len(items), "queued": queued, "skipped": skipped,
                    "truncated": max(0, len(items) - limit)},
        )  # fmt: skip
        return summary

    async def _batch_items(self, name: str, trig: Trigger) -> list[Any]:
        inst = self.instance
        tool_name = _batch_tool(trig.source) or ""
        tool = inst.tools.get(tool_name)
        if tool is None or tool.effect is not Effect.READ:
            raise ValueError(f"batch source {tool_name!r} is not an available read tool")
        options = trig.source if isinstance(trig.source, dict) else {}
        ctx = {"var": inst.spec.values, "event": {}, "input": {}}
        args = render_value(options.get("args") or {}, ctx)
        decision = inst.policy.decide(tool_name, tool.effect, args, None)
        if decision.verdict is not Verdict.ALLOW:
            raise ValueError(f"batch source {tool_name} is not allowed ({decision.verdict})")
        call = ToolUseBlock(id=f"batch_{name}_{uuid.uuid4().hex[:8]}", name=tool_name, input=args)
        result = await inst.tools.execute(call)
        await inst.audit.record(
            self.scope, f"trigger:{name}", "tool_call", tool_name,
            {"status": str(result.status), "effect": str(tool.effect)},
        )  # fmt: skip
        if result.status is not ToolStatus.OK:
            raise ValueError(f"batch source {tool_name} {result.status}: {result.error}")
        content: Any = result.content
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                raise ValueError(f"batch source {tool_name} did not return JSON") from None
        path = options.get("items")
        found = file_sources.dotted(content, str(path)) if path else content
        if isinstance(found, dict) and not path and isinstance(found.get("items"), list):
            found = found["items"]
        if not isinstance(found, list):
            raise ValueError(f"batch source {tool_name} did not return a list of items")
        return found

    async def _job_output_review(self, job: Job) -> None:
        name = job.payload["agent"]
        if name not in self.instance.spec.agents:
            return
        agent = self.agent(name)
        session = await agent.resume(job.payload["session"])
        session.messages = session.messages[: int(job.payload["upto"])]  # the answer sent
        await review_output(agent, session)

    async def _job_knowledge_sync(self, job: Job) -> None:
        corpus = job.payload["corpus"]
        if corpus not in self.instance.spec.knowledge.corpora:
            return  # the corpus was removed from the spec since this job was queued
        if not job.payload.get("once"):
            await self._sync_next(corpus, max(job.run_at, self.queue.clock()))
        report = await self.instance.sync_knowledge(corpus)
        if (report.added or report.updated or report.removed) and self._watching():
            hour = int(self.queue.clock() // 3600)  # a burst of syncs: one check
            await self.queue.enqueue(
                self.scope, "replay_watch", {"reason": f"the {corpus} documents changed"},
                dedupe_key=f"replay_watch:{corpus}:{hour}",
            )  # fmt: skip

    async def fire(
        self, name: str, event: dict[str, Any], delivery_id: str | None = None
    ) -> str | None:
        """Queue a trigger firing (webhooks); duplicates of one delivery are ignored."""
        dedupe = f"trigger:{name}:{delivery_id}" if delivery_id else None
        return await self.queue.enqueue(
            self.scope, "trigger", {"trigger": name, "event": event}, dedupe_key=dedupe
        )

    def webhook(self, path: str) -> tuple[str, Trigger] | None:
        """The trigger at ``/hooks/<path>``; a spec path written as ``/hooks/x`` is found at
        ``/hooks/x`` too (not only at ``/hooks/hooks/x``)."""
        wanted = path.strip("/")
        for name, trig in self.triggers.items():
            mine = (trig.path or "").strip("/")
            if trig.type == "webhook" and wanted in (mine, mine.removeprefix("hooks/")):
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
        started = time.time()
        texts, reason = await answer(agent.send(session, text))
        await self.instance.audit.record(
            self.scope, f"trigger:{name}", "trigger_run", trig.agent or "",
            {"session": session.id, "reason": reason},
        )  # fmt: skip
        await RunRecords(self.instance.db, self.scope).append(
            "trigger", name, started=started, stop_reason=reason,
            counts={"agents": 1, "escalated": int(reason in STOPPED)}, session=session.id,
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

    def _watching(self) -> bool:
        """Replays need a judge (the verifier role); DIF_REPLAY_WATCH=off turns them off."""
        from ..constructor.replay import can_judge

        return os.environ.get("DIF_REPLAY_WATCH", "on") != "off" and can_judge(self.instance)

    async def _job_replay_watch(self, job: Job) -> None:
        """The latest real conversations answered again by the version online now (the model
        may have changed, or the documents): a reply that got worse goes to the inbox."""
        from ..constructor.replay import check_instance, counts, summary, worse_turns

        if job.payload.get("daily"):
            day = datetime.fromtimestamp(self.queue.clock() + DAY, UTC).date().isoformat()
            await self.queue.enqueue(
                self.scope, "replay_watch", dict(job.payload), delay_s=DAY,
                dedupe_key=f"replay_watch:{day}",
            )  # fmt: skip
        if not self._watching():
            return
        inst = self.instance
        reason = str(job.payload.get("reason") or "a check")
        try:
            done = await check_instance(inst, limit=10)
        except LookupError:
            return
        await inst.audit.record(self.scope, "system", "replay_watch", reason, counts(done))
        worse = worse_turns(done)
        if not worse:
            return
        title = f"Replies got worse after {reason}"
        item = await inst.inbox.create(
            "review", title, {"reason": reason, "summary": summary(done), "worse": worse[:10]}
        )
        await self.notify(item, f"{title}: {len(worse)} reply(ies); see the inbox")

    async def _job_weekly_review(self, job: Job) -> None:
        """A week of failures, turned into proposed edits for a person (never applied)."""
        if job.payload.get("weekly"):
            later = self.queue.clock() + 7 * DAY
            week = datetime.fromtimestamp(later, UTC).strftime("%G-W%V")
            await self.queue.enqueue(
                self.scope, "weekly_review", dict(job.payload), delay_s=7 * DAY,
                dedupe_key=f"weekly_review:{week}",
            )  # fmt: skip
        if weekly_review.enabled(self.instance):
            await weekly_review.review(self.instance)

    async def _job_retention(self, job: Job) -> None:
        inst = self.instance
        removed = await purge(inst.db, self.scope, inst.spec.governance.retention)
        removed["memories"] = await inst.memory.purge_expired()
        await inst.audit.record(self.scope, "system", "retention", "purge", removed)
        tomorrow = datetime.fromtimestamp(self.queue.clock() + DAY, UTC).date().isoformat()
        await self.queue.enqueue(
            self.scope, "retention", {}, delay_s=DAY, dedupe_key=f"retention:{tomorrow}"
        )


def _tool_calls(session: Any, start: int) -> list[dict[str, Any]]:
    """The tool calls a turn made (from message ``start`` on): name and outcome, no inputs."""
    from ..core.messages import ToolResultBlock

    new = session.messages[start:]
    results = {b.tool_use_id: b for m in new for b in m.content if isinstance(b, ToolResultBlock)}
    return [{"tool": call.name,
             "status": str(results[call.id].status) if call.id in results else "pending"}
            for m in new for call in m.tool_uses()]  # fmt: skip


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


BATCH_MAX_ITEMS = 1000


def _batch_tool(source: Any) -> str | None:
    if isinstance(source, str):
        return source
    if isinstance(source, dict) and isinstance(source.get("tool"), str):
        return str(source["tool"])
    return None


def _dedupe_value(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= 128 else hashlib.sha256(text.encode()).hexdigest()


def _signed_seconds(offset: str) -> float:
    text = offset.strip()
    sign = -1.0 if text.startswith("-") else 1.0
    return sign * duration_days(text.lstrip("+-")) * DAY
