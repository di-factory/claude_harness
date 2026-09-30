"""The instance runtime: a resolved solution spec turned into runnable agents.

``Instance.open`` does, in order:
1. resolve every ``$secret`` reference (values are registered for redaction);
2. build the per-role model router;
3. build the tool sources the spec asks for: built-in packs, HTTP connectors, MCP servers;
4. apply ``tools.overrides`` (effect, verify, permission) and the permission policy.

Each agent then gets its rendered system prompt, the tools matching its globs, a policy gate
and a budget meter per run. Every event of a run is persisted, redacted, in the session store.

Governance (§3.18) wraps every run: user text and tool results are PII-tokenized before the
model sees them, tools get only the classes ``reveal_to_tools`` allows, replies are
de-tokenized with ``reveal_output``, tool calls land in the audit log and spend is persisted
per tenant, agent and model. The instance's database is SQLite under the state folder
unless ``database_url`` (Postgres in production) or ``database`` is given.

What the spec asks for but this build cannot provide (unknown tool packs, Python tools
that fail to load, knowledge sources other than files, checks that cannot run here) is reported in
``issues`` rather than silently dropped; a tool whose check cannot run is asked about, so a
missing check never lets a side effect through unreviewed. File knowledge sources are synced
when the instance opens.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2

from ..core.events import Event, MessageAdded, ToolCallFinished, ToolCallStarted, TurnEnded
from ..core.loop import LoopConfig, run
from ..core.messages import Message, Role, ToolResultBlock, ToolStatus, ToolUseBlock, Usage
from ..core.scope import Scope
from ..core.session import Session
from ..feedback.store import ALL_AGENTS, ConstraintStore, pinned_block
from ..governance import AuditLog, ConsentStore, GovernedTools, PiiPolicy, Tokenizer, TokenVault
from ..governance.contacts import ContactStore
from ..hitl.inbox import Inbox
from ..knowledge.store import KnowledgeBase, SyncReport
from ..knowledge.tools import (
    check_citations,
    citation_checks,
    render_citations,
    repair_note,
    search_tool,
    ungrounded_text,
)
from ..memory.agent import memory_block, memory_tools, record_episode
from ..memory.store import Memory, MemoryStore
from ..observability.costs import UsageStore
from ..observability.otel import OtlpExporter, TracedProvider, Tracer, exporter_from_env
from ..observability.otel import _current as _current_span
from ..policy import Approver, DailySpend, Limits, PermissionPolicy, PolicyGate, Redactor, RunMeter
from ..policy.budgets import DEFAULT_PRICES, cost_usd
from ..policy.escalation import first_match
from ..policy.spend import SpendStore
from ..providers.base import ModelProvider, ProviderMessage
from ..spec.errors import Issue
from ..spec.loader import ResolvedSpec
from ..spec.regions import region_violations
from ..spec.schema import Agent, SolutionSpec
from ..store.db import Database, connect
from ..store.sql import SqlSessionStore
from ..teams import LedgerStore, handoff_tool, runs_tools, subagent_tool
from ..tenancy.secrets import EnvSecrets, SecretBackend, SecretResolver
from ..tools import python as python_tools
from ..tools import python_sandbox
from ..tools.egress import EgressProxy
from ..tools.http import ConnectorError, http_tools
from ..tools.mcp import McpToolSource
from ..tools.packs import NoteStore, Workspace, coding_tools, general_tools
from ..tools.packs.coding import (
    BUILTIN,
    ContainerExecutor,
    Executor,
    ExecutorError,
    executor_from_spec,
)
from ..tools.packs.documents import documents_tools
from ..tools.packs.google_calendar import PACK as GOOGLE_CALENDAR
from ..tools.packs.google_calendar import CalendarError, Source, calendar_tools
from ..tools.registry import Effect, Tool, ToolRegistry
from ..triggers.files import FileSource, build_source
from ..verify import Verifier
from .compaction import compact_if_needed
from .context import current_session
from .prompts import load_text, render
from .router import short_circuit
from .routing import ProviderFactory, build_router

LOCAL_TENANT = "local"
HANDOFF_TOOL = "handoff.human"
Notify = Callable[[str, str], Awaitable[None]]  # (inbox item id, one-line summary)
Emit = Callable[[str, dict[str, Any]], Awaitable[Any]]


class InstanceError(RuntimeError):
    """An instance that cannot start."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        super().__init__("; ".join(f"{i.path}: {i.message}" for i in issues))


@dataclass
class RuntimeOptions:
    state_root: Path
    secrets: SecretBackend = field(default_factory=EnvSecrets)
    approver: Approver | None = None  # None: ask means deny (headless)
    workspaces: dict[str, Path] = field(default_factory=dict)  # local dirs per workspace name
    executor: Executor | None = None
    provider_factories: dict[str, ProviderFactory] | None = None
    provider: ModelProvider | None = None  # replaces the router entirely (tests, evals)
    mcp_servers: dict[str, Any] = field(default_factory=dict)  # name -> in-process server
    http_client: httpx2.AsyncClient | None = None  # shared by HTTP connectors (tests: a mock)
    database_url: str | None = None  # default: sqlite:///<state_root>/dif.db
    database: Database | None = None  # an open database (the service shares one)
    telemetry: OtlpExporter | None = None  # default: from OTEL_EXPORTER_OTLP_ENDPOINT, if set
    s3_client: Any = None  # for file sources in S3 (tests: a fake); default: boto3's
    egress_proxy: EgressProxy | None = None  # default: from DIF_EGRESS_ADVERTISE, if set


DOCUMENTS_STORAGE = "documents:storage"  # the documents pack's own source, by this name


def scope_for(spec: SolutionSpec) -> Scope:
    tenant = spec.tenant.id if spec.tenant else LOCAL_TENANT
    return Scope(tenant_id=tenant, instance_id=spec.solution.id)


def _matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


class Instance:
    def __init__(self, resolved: ResolvedSpec, options: RuntimeOptions) -> None:
        self.resolved = resolved
        self.spec = resolved.spec
        self.options = options
        self.scope = scope_for(self.spec)
        self.redactor = Redactor()
        self.secrets = SecretResolver(options.secrets, self.redactor)
        self.daily = DailySpend()
        self.issues: list[Issue] = []
        self.tools = ToolRegistry()
        self.policy = PermissionPolicy()
        self.provider: ModelProvider | None = None
        self.workspace: Workspace | None = None  # the coding pack's, for undo
        self.executor: Executor | None = None  # where shell commands and command checks run
        self.egress: EgressProxy | None = None  # the builtin egress proxy, when containers use it
        self._stack = AsyncExitStack()
        self.owns_db = False  # True when this instance opened the database (closes it too)
        self.db: Database  # set in _open_database
        self.store: SqlSessionStore
        self.pii: Tokenizer
        self.consent: ConsentStore
        self.audit: AuditLog
        self.spend: SpendStore
        self.usage: UsageStore
        self.inbox: Inbox
        self.contacts: ContactStore
        self.memory: MemoryStore
        self.knowledge: KnowledgeBase
        self.constraints: ConstraintStore
        self.verifier = Verifier(self, on_failed_twice=self._verification_failed_twice)
        self.notify: Notify | None = None  # set by the service: tells a person about inbox items
        self.emit: Emit | None = None  # set by the service: internal events (ledger, workflows)
        self.ledger: LedgerStore | None = None
        self.pending_handoffs: dict[str, tuple[str, str, str]] = {}  # source session -> target
        self.sources: dict[str, tuple[Source, float]] = {}  # item feeds: (fetch, every s)
        # folders and buckets: file triggers' (by trigger name) and the documents storage
        self.file_sources: dict[str, FileSource] = {}
        self.file_source_errors: dict[str, str] = {}
        self.tracer: Tracer | None = None  # OpenTelemetry, when an OTLP endpoint is set

    # --- lifecycle -----------------------------------------------------------------

    @classmethod
    async def open(cls, resolved: ResolvedSpec, options: RuntimeOptions) -> Instance:
        if resolved.spec.kind != "instance":
            raise InstanceError(
                [Issue("error", "not_instance", "kind", "only instances run; a pack needs values")]
            )
        if not resolved.ok:
            raise InstanceError([i for i in resolved.issues if i.severity == "error"])
        inst = cls(resolved, options)
        try:
            await inst._open_database()
            await inst._build()
        except BaseException:
            await inst.close()
            raise
        return inst

    async def close(self) -> None:
        await self._stack.aclose()
        if self.owns_db:
            self.owns_db = False
            await self.db.close()

    async def __aenter__(self) -> Instance:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _open_database(self) -> None:
        opts = self.options
        if opts.database is not None:
            self.db = opts.database
        else:
            url = opts.database_url or f"sqlite:///{opts.state_root.resolve() / 'dif.db'}"
            self.db = await connect(url)
            self.owns_db = True
        self.store = SqlSessionStore(self.db, redactor=self.redactor)
        policy = PiiPolicy.from_spec(self.spec.governance.pii)
        self.pii = Tokenizer(policy, TokenVault(self.db), self.scope)
        self.consent = ConsentStore(self.db)
        self.audit = AuditLog(self.db)
        self.spend = SpendStore(self.db)
        self.usage = UsageStore(self.db)
        self.inbox = Inbox(self.db, self.scope)
        self.contacts = ContactStore(self.db)
        self.memory = MemoryStore(self.db, self.scope)
        self.knowledge = KnowledgeBase(self.db, self.scope, dict(self.spec.knowledge.corpora))
        self.constraints = ConstraintStore(self.db, self.scope)
        if self.spec.ledger is not None:
            self.ledger = LedgerStore(self.db, self.scope, self.spec.ledger, list(self.spec.agents))
            self.ledger.emit = self._emit
            self.ledger.on_task = self._ledger_rules
        if policy.tokenize and policy.undetectable:
            self._warn(
                "pii_undetectable",
                "governance.pii.classes",
                f"no detector yet for {sorted(policy.undetectable)}; keep them out of free text",
            )

    def _warn(self, code: str, path: str, message: str) -> None:
        self.issues.append(Issue("warning", code, path, message))

    async def _build(self) -> None:
        spec, data = self.spec, self.resolved.data
        missing = set(self.secrets.missing(sorted(spec.secrets)))
        if self.options.provider is not None:
            self.provider = self.options.provider
        else:
            if spec.models is None:
                raise InstanceError([Issue("error", "no_models", "models", "no model roles")])
            raw = data["models"].get("providers", {})
            if blocking := sorted(_secret_refs(raw) & missing):
                raise InstanceError(
                    [Issue("error", "missing_secret", f"secrets.{n}", "not set") for n in blocking]
                )
            if outside := region_violations(spec):  # never route outside the allowed regions
                raise InstanceError([Issue("error", c, p, m) for c, p, m in outside])
            settings = self.secrets.resolve(raw)
            self.provider = build_router(
                spec.models.roles, settings, self.options.provider_factories
            )

        exporter = self.options.telemetry or exporter_from_env(client=self.options.http_client)
        if exporter is not None:
            self.tracer = Tracer(exporter, {
                "dif.tenant_id": self.scope.tenant_id, "dif.instance_id": self.scope.instance_id,
                "dif.config_version": self.resolved.version_hash[:12],
            })  # fmt: skip
            self.provider = TracedProvider(self.provider, self.tracer, self.vendor)
        for corpus in self.spec.knowledge.corpora:
            await self.sync_knowledge(corpus)
        self._file_sources(data, missing)
        tools: list[Tool] = []
        tools += await self._packs(data, missing)
        tools += self._http(data, missing)
        tools += await self._mcp(data, missing)
        if any("human" in a.handoffs for a in spec.agents.values()):
            tools.append(self._handoff_tool())
        tools += runs_tools(self)
        for name in sorted({s for a in spec.agents.values() for s in a.subagents}):
            tools.append(subagent_tool(self, name))
        self._configure(tools)

    def _handoff_tool(self) -> Tool:
        from ..tools.registry import tool

        @tool(HANDOFF_TOOL, effect=Effect.WRITE)
        async def handoff(reason: str) -> str:
            """Hand this conversation to a person (medical advice, complaints, emergencies,
            anything outside your rules). Say why in one sentence; then tell the contact a
            person will reply."""
            session = current_session.get()
            if session is None:
                raise RuntimeError("no conversation to hand off")
            item = await self.escalate(session, reason, by=f"agent:{session.agent_id}")
            return f"handed off to a person (escalation {item}); a person will reply here"

        return handoff

    async def sync_knowledge(self, corpus: str) -> SyncReport:
        report = await self.knowledge.sync(corpus)
        where = f"knowledge.corpora.{corpus}.sources"
        self.issues[:] = [i for i in self.issues if i.path != where]
        if report.unavailable:
            self._warn(
                "knowledge_source_unavailable",
                where,
                f"not synced here: {', '.join(report.unavailable)}; push their documents with"
                f" PUT /admin/knowledge/{corpus}/documents",
            )
        if report.skipped:
            self._warn(
                "knowledge_format_unavailable",
                where,
                f"{len(report.skipped)} file(s) without readable text (images, scans,"
                f" unsupported formats): {', '.join(report.skipped[:5])}",
            )
        if report.added or report.updated or report.removed:
            await self.audit.record(
                self.scope, "system", "knowledge_sync", f"knowledge/{corpus}",
                {"added": report.added, "updated": report.updated, "removed": report.removed},
            )  # fmt: skip
        return report

    async def knowledge_not_found(self, session: Session, corpus: str, query: str) -> None:
        """Nothing in the corpus answers: audit it and apply the escalation rules."""
        await self.audit.record(
            self.scope, f"agent:{session.agent_id}", "knowledge_not_found",
            f"knowledge/{corpus}", {"session": session.id, "query": query[:300]},
        )  # fmt: skip
        escalation = self.spec.policies.escalation or {}
        contact = (
            await self.contacts.get(self.scope, session.contact_key) if session.contact_key else {}
        )
        context = {
            "knowledge": {"not_found": True, "corpus": corpus, "query": query},
            "contact": contact,
            "var": self.spec.values,
        }
        rule = first_match(list(escalation.get("rules") or []), context)
        if rule is not None and rule.get("to") == "human":
            await self.escalate(session, f"{corpus} has no answer: {query[:120]}", by="knowledge")

    async def propose_constraint(
        self, agent: str, text: str, source: str, evidence: dict[str, Any],
        *, key: str | None = None, session_id: str | None = None,
    ) -> str:  # fmt: skip
        """A candidate constraint from a signal; new ones go to a person in the inbox."""
        constraint, new = await self.constraints.propose(agent, text, source, evidence, key)
        if new:
            item = await self.inbox.create(
                "constraint", f"Proposed rule for {agent}: {text[:100]}",
                {"constraint_id": constraint.id, "agent": agent, "text": constraint.text,
                 "source": source, "evidence": evidence},
                session_id,
            )  # fmt: skip
            if self.notify is not None:
                await self.notify(item, f"A rule was proposed for {agent} ({source})")
        return constraint.id

    def vendor(self, role: str) -> str:
        """The provider that serves a model role (the router falls back to ``main``)."""
        roles = self.spec.models.roles if self.spec.models else {}
        config = roles.get(role) or roles.get("main")
        return config.provider if config else "unknown"

    async def record_usage(self, agent: str, role: str, model: str | None, usage: Usage) -> None:
        """One priced model call: budget totals (tenant, agent, model, role, vendor) and the
        usage ledger that cost reports read."""
        name, vendor, usd = model or "unknown", self.vendor(role), usage.cost_usd
        for key in ("tenant", f"agent:{agent}", f"model:{name}", f"role:{role}",
                    f"vendor:{vendor}"):  # fmt: skip
            await self.spend.add(self.scope, key, usd)
        await self.usage.add(
            self.scope, agent=agent, role=role, vendor=vendor, model=name, usage=usage
        )

    async def charge(self, agent: str, role: str, final: ProviderMessage) -> Usage:
        """Price and record a model call made outside an agent run (verifier, extraction,
        eval judge)."""
        price = DEFAULT_PRICES.get(final.model or "")
        priced = (
            final.usage.model_copy(update={"cost_usd": cost_usd(final.usage, price)})
            if price
            else final.usage
        )
        await self.record_usage(agent, role, final.model, priced)
        return priced

    async def flag_contradiction(self, previous: Memory, value: str, new_id: str) -> None:
        """A fact changed: keep the new value, and let a person restore the old one."""
        item = await self.inbox.create(
            "memory",
            f"Changed fact: {previous.key}",
            {"key": previous.key, "previous": previous.content, "new": value,
             "previous_id": previous.id, "new_id": new_id},
        )  # fmt: skip
        if self.notify is not None:
            await self.notify(item, f"A remembered fact changed: {previous.key}")

    async def consider_skill(self, skill: Memory) -> str:
        """Count a proposed skill; after min_successes it goes to a person (or activates)."""
        promotion = (self.spec.memory.skill_promotion if self.spec.memory else None) or {}
        needed = int(promotion.get("min_successes", 3))
        if skill.status == "active":
            return f"skill '{skill.key}' is already approved"
        if skill.status == "pending":
            return f"skill '{skill.key}' is waiting for a person's approval"
        if skill.successes < needed:
            return f"noted skill '{skill.key}' ({skill.successes}/{needed} before review)"
        if promotion.get("approval", "required") != "required":
            await self.memory.set_status(skill.id, "active")
            return f"skill '{skill.key}' is now active"
        await self.memory.set_status(skill.id, "pending")
        item = await self.inbox.create(
            "skill", f"Approve skill: {skill.key}", {"memory_id": skill.id, "steps": skill.content}
        )
        if self.notify is not None:
            await self.notify(item, f"A skill is ready for review: {skill.key}")
        return f"skill '{skill.key}' was sent to a person for approval"

    async def _emit(self, name: str, data: dict[str, Any]) -> None:
        if self.emit is not None:
            await self.emit(name, data)

    async def _ledger_rules(self, task: dict[str, Any]) -> None:
        escalation = self.spec.policies.escalation or {}
        rule = first_match(
            list(escalation.get("rules") or []),
            {"ledger": {"task": task}, "var": self.spec.values},
        )
        if rule is not None and rule.get("to") == "human":
            item = await self.inbox.create(
                "escalation",
                f"Task needs a person: {task.get('title', task['id'])}",
                {"task": task},
            )
            await self.audit.record(self.scope, "ledger", "escalation", f"inbox/{item}", {})
            if self.notify is not None:
                await self.notify(item, f"Task needs a person: {task.get('title', '')}")

    async def _verification_failed_twice(
        self, session: Session, call: ToolUseBlock, reason: str
    ) -> None:
        escalation = self.spec.policies.escalation or {}
        context = {
            "verification": {"failed_twice": True, "reason": reason},
            "tool": call.name,
            "var": self.spec.values,
        }
        await self.propose_constraint(
            session.agent_id or ALL_AGENTS,
            f"Before using {call.name}, make sure its check will pass: {reason}",
            "verification",
            {"tool": call.name, "reason": reason, "session": session.id},
            key=f"verification:{call.name}",
            session_id=session.id,
        )
        rule = first_match(list(escalation.get("rules") or []), context)
        if rule is not None and rule.get("to") == "human":
            await self.escalate(
                session, f"verification of {call.name} failed twice: {reason}", by="verifier"
            )

    async def escalate(self, session: Session, reason: str, *, by: str) -> str:
        """File an escalation with the recent transcript and stop the agent replying."""
        transcript = [
            {"role": str(m.role), "text": m.text()} for m in session.messages[-12:] if m.text()
        ]
        results = {
            b.tool_use_id: b
            for m in session.messages
            for b in m.content
            if isinstance(b, ToolResultBlock)
        }
        trace = [
            {"tool": call.name, "input": call.input,
             "status": str(results[call.id].status) if call.id in results else "pending",
             "error": results[call.id].error if call.id in results else None}
            for m in session.messages
            for call in m.tool_uses()
        ]  # fmt: skip
        item = await self.inbox.create(
            "escalation",
            reason[:200],
            {"reason": reason, "agent": session.agent_id, "contact": session.contact_key,
             "transcript": transcript, "tool_trace": trace[-20:]},
            session.id,
        )  # fmt: skip
        await self.store.set_state(self.scope, session.id, "escalated")
        await self.audit.record(self.scope, by, "escalation", f"inbox/{item}", {"reason": reason})
        if self.notify is not None:
            await self.notify(item, f"Escalation: {reason[:120]}")
        return item

    async def _packs(self, data: dict[str, Any], missing: set[str]) -> list[Tool]:
        out: list[Tool] = []
        for pack in self.spec.tools.packs:
            if pack == GOOGLE_CALENDAR:
                out += await self._google_calendar(data, missing)
            elif pack == "general":
                out += general_tools(NoteStore(self.options.state_root, self.scope))
            elif pack == "documents":
                out += documents_tools(self.read_document, self._transcribe)
            elif pack == "coding":
                roots = self.options.workspaces
                name = next((a.workspace for a in self.spec.agents.values() if a.workspace), None)
                root = roots.get(name or "") or roots.get("default")
                if root is None:
                    self._warn(
                        "no_workspace",
                        "tools.packs",
                        f"the coding pack needs a local directory for workspace {name!r}",
                    )
                    continue
                self.workspace = Workspace(root)
                ws_spec = self.spec.workspaces.get(name or "") or {}
                try:
                    executor, timeout = executor_from_spec(ws_spec.get("executor"))
                except ExecutorError as exc:
                    self._warn("executor_error", f"workspaces.{name}.executor", str(exc))
                    continue  # no shell rather than an unconfined one
                if isinstance(executor, ContainerExecutor):
                    await self._attach_egress(executor, f"workspaces.{name}.executor")
                self.executor = self.options.executor or executor
                out += coding_tools(self.workspace, self.executor, bash_timeout_s=timeout or 120.0)
            else:
                self._warn(
                    "unavailable_pack", "tools.packs", f"tool pack {pack!r} is not built yet"
                )
        out += await self._python_tools(data)
        return out

    async def _python_tools(self, data: dict[str, Any]) -> list[Tool]:
        """Pack extensions: in-process, or each call in a container
        (``tools.config.python.isolation: "container"``)."""
        config = ((data.get("tools") or {}).get("config") or {}).get("python") or {}
        where = "tools.config.python"
        isolation = str(config.get("isolation") or "none")
        executor: ContainerExecutor | None = None
        timeout = 30.0
        if self.spec.tools.python and isolation == "container":
            try:
                built, limit = executor_from_spec({**config, "type": "container"})
            except ExecutorError as exc:
                self._warn("executor_error", where, f"{exc}; Python extensions are off")
                return []  # never fall back to running them unconfined
            assert isinstance(built, ContainerExecutor)
            executor, timeout = built, limit or timeout
            await self._attach_egress(executor, where)
        elif isolation not in ("none", "container"):
            self._warn("executor_error", where, "isolation must be none or container")
            return []
        out: list[Tool] = []
        for i, ref in enumerate(self.spec.tools.python):
            try:
                if executor is not None:
                    out.append(python_sandbox.isolated(ref, executor, timeout))
                else:
                    out.append(python_tools.load(ref))
            except python_tools.PythonToolError as exc:
                self._warn("python_tool_error", f"tools.python[{i}]", str(exc))
        return out

    async def _attach_egress(self, executor: ContainerExecutor, where: str) -> None:
        """Give a container executor its way out: the builtin proxy (per-command credentials
        bound to ``allow_hosts``), an operator's proxy, or none (no network)."""
        if not executor.allow_hosts:
            return
        if executor.egress_proxy == BUILTIN:
            executor.proxy = await self._egress()
            if executor.proxy is None:
                executor.egress_proxy = None
                self._warn("egress_proxy_missing", where,
                           "the builtin egress proxy needs DIF_EGRESS_ADVERTISE (the address"
                           " containers reach it at); the container gets no network")  # fmt: skip
        elif not executor.egress_proxy:
            self._warn("egress_proxy_missing", where,
                       "allow_hosts needs an egress_proxy that enforces it; the container"
                       " gets no network")  # fmt: skip

    async def _egress(self) -> EgressProxy | None:
        if self.egress is not None:
            return self.egress
        proxy = self.options.egress_proxy
        if proxy is None:
            advertise = os.environ.get("DIF_EGRESS_ADVERTISE")
            if not advertise:
                return None
            host, _, port = os.environ.get("DIF_EGRESS_LISTEN", "0.0.0.0:3128").rpartition(":")
            proxy = EgressProxy(advertise, listen_host=host or "0.0.0.0", listen_port=int(port))
        proxy.on_decision = self._egress_decision
        if proxy._server is None:
            await proxy.start()
            self._stack.push_async_callback(proxy.close)
        self.egress = proxy
        return proxy

    async def _egress_decision(
        self, label: str, host: str, port: int, allowed: bool, reason: str
    ) -> None:
        await self.audit.record(
            self.scope, f"sandbox:{label}", "egress", f"{host}:{port}",
            {"allowed": allowed, "reason": reason},
        )  # fmt: skip

    def _file_sources(self, data: dict[str, Any], missing: set[str]) -> None:
        """The folders and buckets this solution reads: its file triggers' sources and the
        documents pack's ``storage``."""
        wanted: dict[str, Any] = {
            name: raw.get("source")
            for name, raw in (data.get("triggers") or {}).items()
            if isinstance(raw, dict) and raw.get("type") == "file"
        }
        storage = ((data.get("tools") or {}).get("config") or {}).get("documents") or {}
        if "documents" in self.spec.tools.packs and storage.get("storage") is not None:
            wanted[DOCUMENTS_STORAGE] = storage["storage"]
        for name, raw in wanted.items():
            if _secret_refs(raw) & missing:
                self.file_source_errors[name] = "a secret is not set"
                continue
            try:
                creds = raw.get("credentials") if isinstance(raw, dict) else None
                resolved = self.secrets.resolve(creds) if creds is not None else None
                self.file_sources[name] = build_source(
                    raw, resolved, s3_client=self.options.s3_client
                )
            except Exception as exc:  # one broken source must not stop the instance
                self.file_source_errors[name] = str(exc)
        if DOCUMENTS_STORAGE in self.file_source_errors:
            self._warn("file_source_error", "tools.config.documents.storage",
                       self.file_source_errors[DOCUMENTS_STORAGE])  # fmt: skip

    async def read_document(self, uri: str) -> bytes:
        """The bytes of a document in one of this solution's own sources; anything else is
        refused."""
        for source in self.file_sources.values():
            key = source.key(uri)
            if key:
                return await source.read(key)
        raise PermissionError(f"{uri} is not in this solution's document sources")

    async def _transcribe(self, data: bytes, media_type: str) -> str:
        from ..documents.ocr import transcribe

        return await transcribe(self, data, media_type)

    async def _google_calendar(self, data: dict[str, Any], missing: set[str]) -> list[Tool]:
        raw = (data.get("tools", {}).get("config") or {}).get(GOOGLE_CALENDAR) or {}
        where = f"tools.config.{GOOGLE_CALENDAR}"
        if _secret_refs(raw) & missing:
            self._warn("missing_secret", where, "skipped: a secret is not set")
            return []
        http = self.options.http_client
        if http is None:
            http = await self._stack.enter_async_context(httpx2.AsyncClient(timeout=30.0))
        tz = self.spec.tenant.timezone if self.spec.tenant else "UTC"
        try:
            tools, sources, every = calendar_tools(self.secrets.resolve(raw), tz, http)
        except (CalendarError, ValueError, TypeError) as exc:
            self._warn("connector_error", where, str(exc))
            return []
        for name, fetch in sources.items():
            self.sources[name] = (fetch, every)
        return tools

    def _http(self, data: dict[str, Any], missing: set[str]) -> list[Tool]:
        out: list[Tool] = []
        for name, raw in data.get("tools", {}).get("http", {}).items():
            if _secret_refs(raw) & missing:
                self._warn("missing_secret", f"tools.http.{name}", "skipped: a secret is not set")
                continue
            try:
                out += http_tools(name, self.secrets.resolve(raw), client=self.options.http_client)
            except ConnectorError as exc:
                self._warn("connector_error", f"tools.http.{name}", str(exc))
        return out

    async def _mcp(self, data: dict[str, Any], missing: set[str]) -> list[Tool]:
        out: list[Tool] = []
        for name, raw in data.get("tools", {}).get("mcp", {}).items():
            target = self.options.mcp_servers.get(name)
            if target is None and _secret_refs(raw) & missing:
                self._warn("missing_secret", f"tools.mcp.{name}", "skipped: a secret is not set")
                continue
            try:
                source = McpToolSource(name, target or self.secrets.resolve(raw))
                await self._stack.enter_async_context(source)
            except Exception as exc:  # one unreachable server must not take the instance down
                self._warn("mcp_unavailable", f"tools.mcp.{name}", f"{type(exc).__name__}: {exc}")
                continue
            out += source.tools
        return out

    def _configure(self, tools: list[Tool]) -> None:
        perms = self.spec.policies.permissions
        allow, ask, deny = list(perms.allow), list(perms.ask), list(perms.deny)
        overrides = self.spec.tools.overrides
        for t in tools:
            o = overrides.get(t.name)
            if o is not None:
                if o.effect:
                    t = dataclasses.replace(t, effect=Effect(o.effect))
                if o.verify:
                    t = dataclasses.replace(t, verify=o.verify)
            if t.name == HANDOFF_TOOL or t.source == "team":
                allow.append(t.name)  # asking a person or a teammate is always allowed
            self.tools.register(t)
        for name in self.tools.names():  # a check that cannot run here: a person reviews
            registered = self.tools.get(name)
            check = registered.verify if registered else None
            why = self.verifier.unrunnable(check) if check else None
            if why:
                ask.append(name)
                self._warn("verification_unavailable", f"tools.{name}", f"{why}; asking instead")
        allow.append("handoff.agent")  # handing over to a listed teammate is always allowed
        allow.append("memory.*")  # remembering is internal, scoped and reviewed (contradictions)
        allow.append("knowledge.*")  # reading the solution's own documents
        for name, o in overrides.items():
            if o.permission:
                {"allow": allow, "ask": ask, "deny": deny}[o.permission].append(name)
        self.policy = PermissionPolicy(
            allow=allow, ask=ask, deny=deny, profile=self.spec.policies.profile
        )

    # --- agents --------------------------------------------------------------------

    def agent(self, name: str | None = None) -> AgentRuntime:
        agents = self.spec.agents
        if name is None:
            if not agents:
                raise KeyError("the spec defines no agents")
            name = next(iter(agents))
        if name not in agents:
            raise KeyError(f"unknown agent {name!r}; defined: {sorted(agents)}")
        return AgentRuntime(self, name, agents[name])


class AgentRuntime:
    def __init__(self, instance: Instance, name: str, spec: Agent) -> None:
        self.instance = instance
        self.name = name
        self.spec = spec
        self.system = render(load_text(spec.prompt), instance.spec)
        registry = instance.tools
        patterns = [
            *spec.tools,
            *([HANDOFF_TOOL] if "human" in spec.handoffs else []),
            *(f"agent.{s}" for s in spec.subagents),
        ]
        selected = ToolRegistry(
            [t for n in registry.names() if _matches(n, patterns) and (t := registry.get(n))]
        )
        ledger = instance.ledger
        if ledger is not None and name in (ledger.spec.visible_to or list(instance.spec.agents)):
            for t in ledger.tools(name):
                if _matches(t.name, spec.tools):
                    selected.register(t)
        teammates = [h for h in spec.handoffs if h != "human" and h in instance.spec.agents]
        if teammates:
            selected.register(handoff_tool(instance, self, teammates))
        for t in memory_tools(instance, self):
            selected.register(t)
        for corpus in instance.spec.knowledge.corpora:
            name_ = f"knowledge.search_{corpus}"
            if corpus in spec.knowledge or _matches(name_, spec.tools):
                selected.register(search_tool(instance, corpus))
        self.citation_checks = citation_checks(instance, name)
        self.tools = GovernedTools(selected, instance.pii)
        self.missing_tools = [
            p for p in spec.tools if not any(_matches(n, [p]) for n in self.tools.names())
        ]
        self.gate = PolicyGate(
            instance.policy, self.tools, instance.options.approver, instance.verifier.verify
        )
        budgets = instance.spec.policies.budgets
        own = spec.budgets or {}
        self.per_run = Limits.from_spec(budgets.get("per_run")).tighten(
            Limits.from_spec(own.get("per_run"))
        )
        self.per_tenant_day = Limits.from_spec(budgets.get("per_tenant_day"))
        self.per_agent_day = Limits.from_spec(own.get("per_day"))
        self.config = LoopConfig(
            system=self.system, model_role=spec.model_role, max_turns=spec.max_turns or 12
        )

    async def new_session(self, contact_key: str | None = None) -> Session:
        inst = self.instance
        session = Session(
            scope=inst.scope,
            agent_id=self.name,
            contact_key=contact_key,
            config_version=inst.resolved.version_hash,
        )
        await inst.store.append(session.started_event())
        return session

    async def resume(self, session_id: str) -> Session:
        return await self.instance.store.load(self.instance.scope, session_id)

    def meter(self) -> RunMeter:
        inst = self.instance
        return RunMeter(
            inst.scope.tenant_id,
            self.per_run,
            self.per_tenant_day,
            daily=inst.daily,
            agent=self.name,
            per_agent_day=self.per_agent_day,
        )

    async def send(
        self, session: Session, text: str, *, names: list[str] | None = None, route: bool = False
    ) -> AsyncIterator[Event]:
        """Run one user turn; every event is persisted (redacted) and yielded.

        ``names`` are the contact's known names (from the channel profile), so the
        tokenizer can find them. Yielded text is tokenized; ``reply`` turns it into what the
        contact may see.
        """
        inst = self.instance
        assert inst.provider is not None
        scope = inst.scope
        spent = await inst.spend.day(scope)
        inst.daily.set_today(scope.tenant_id, spent.get("tenant", 0.0))
        inst.daily.set_today(f"{scope.tenant_id}/{self.name}", spent.get(f"agent:{self.name}", 0.0))
        meter = self.meter()
        full = inst.spec.governance.audit.get("level") == "full"
        started: dict[str, ToolCallStarted] = {}
        safe_text = await inst.pii.tokenize(text, names or [])
        token = current_session.set(session)
        tracer = inst.tracer
        root = (
            tracer.start(
                f"invoke_agent {self.name}",
                gen_ai__operation__name="invoke_agent",
                gen_ai__agent__name=self.name,
                gen_ai__conversation__id=session.id,
                dif__tenant_id=scope.tenant_id,
                dif__instance_id=scope.instance_id,
            )
            if tracer
            else None
        )
        span_token = _current_span.set(root) if root else None
        try:
            routed = await short_circuit(self, session, safe_text) if route else None
            if routed is not None:  # a trivial message the router answered: no main agent
                for event in routed:
                    await inst.store.append(event)
                    if isinstance(event, TurnEnded):
                        await record_episode(inst, self.name, session)
                    yield event
                return
            compacted = await compact_if_needed(self, session)
            if compacted is not None:
                await inst.store.append(compacted)
                yield compacted
            async for event in self._run(session, safe_text, meter, started, full):
                if root is not None and isinstance(event, TurnEnded):
                    root.set(dif__turn_reason=event.reason, dif__cost_usd=meter.total.cost_usd,
                             gen_ai__usage__input_tokens=meter.total.input_tokens,
                             gen_ai__usage__output_tokens=meter.total.output_tokens)  # fmt: skip
                yield event
        finally:
            current_session.reset(token)
            if tracer is not None and root is not None and span_token is not None:
                _current_span.reset(span_token)
                tracer.finish(root)
                if root.parent_id is None:  # a sub-agent's run is part of its caller's trace
                    await tracer.flush(root.trace_id)

    async def _run(
        self,
        session: Session,
        safe_text: str,
        meter: RunMeter,
        started: dict[str, ToolCallStarted],
        full: bool,
    ) -> AsyncIterator[Event]:
        """A turn, then the answer checks: a ``citations`` failure gets one rewrite, and a
        second failure replaces the answer with "not found"."""
        turn_start = len(session.messages)
        reason = ""
        async for event in self._turn(session, safe_text, meter, started, full):
            if isinstance(event, TurnEnded):
                reason = event.reason
            yield event
        if reason == "budget":
            await self.instance.propose_constraint(
                self.name,
                "Stay within the run budget: plan before calling tools, do not repeat calls,"
                " and hand off when a task needs more than one run.",
                "budget",
                {"session": session.id},
                key="budget",
                session_id=session.id,
            )
        if reason != "end_turn" or not self.citation_checks:
            return
        for attempt in (1, 2):
            failure = self._citation_failure(session, turn_start)
            if failure is None:
                return
            inst = self.instance
            await inst.audit.record(
                inst.scope, "verifier", "citations_failed", f"agent:{self.name}",
                {"session": session.id, "reason": failure, "attempt": attempt},
            )  # fmt: skip
            if attempt == 2:
                await inst.propose_constraint(
                    self.name,
                    "Answer only from the passages you retrieved, with a source marker after each"
                    " statement; when they do not cover the question, say so.",
                    "citations",
                    {"reason": failure, "session": session.id},
                    key="citations",
                    session_id=session.id,
                )
                added = session.add_message(Message.assistant(ungrounded_text(inst)))
                await inst.store.append(added)
                yield added
                return
            async for event in self._turn(
                session, repair_note(failure).text(), meter, started, full
            ):
                if isinstance(event, TurnEnded) and event.reason != "end_turn":
                    reason = event.reason
                yield event
            if reason != "end_turn":
                return

    def _citation_failure(self, session: Session, turn_start: int) -> str | None:
        answer = next(
            (m.text() for m in reversed(session.messages) if m.role is Role.ASSISTANT), ""
        )
        for check in self.citation_checks:
            passed, why = check_citations(check, answer, session, turn_start)
            if not passed:
                return why
        return None

    async def _turn(
        self,
        session: Session,
        safe_text: str,
        meter: RunMeter,
        started: dict[str, ToolCallStarted],
        full: bool,
    ) -> AsyncIterator[Event]:
        inst = self.instance
        scope = inst.scope
        assert inst.provider is not None
        tool_starts: dict[str, int] = {}
        pinned = pinned_block(await inst.constraints.active(self.name))
        remembered = await memory_block(inst, self.name, session)
        config = dataclasses.replace(self.config, system=self.system + pinned + remembered)
        async for event in run(
            session, safe_text, inst.provider, self.tools, config, gate=self.gate, meter=meter
        ):
            await inst.store.append(event)
            if isinstance(event, ToolCallStarted):
                started[event.tool_use_id] = event
                tool_starts[event.tool_use_id] = time.time_ns()
            elif isinstance(event, ToolCallFinished):
                tool = self.tools.get(event.name)
                effect = tool.effect if tool else Effect.EXTERNAL
                if inst.tracer is not None:
                    span = inst.tracer.start(
                        f"execute_tool {event.name}",
                        gen_ai__operation__name="execute_tool",
                        gen_ai__tool__name=event.name,
                        gen_ai__tool__call__id=event.tool_use_id,
                        dif__tool_status=str(event.status),
                        dif__tool_effect=str(effect),
                    )
                    span.start_ns = tool_starts.pop(event.tool_use_id, span.start_ns)
                    if event.status is not ToolStatus.OK:
                        span.error = str(event.status)
                    inst.tracer.finish(span)
                if full or effect is not Effect.READ:
                    call = started.get(event.tool_use_id)
                    await inst.audit.record(
                        scope,
                        f"agent:{self.name}",
                        "tool_call",
                        event.name,
                        {"session": session.id, "effect": str(effect), "status": str(event.status),
                         "input": inst.redactor.redact_obj(call.input if call else {})},
                    )  # fmt: skip
            elif isinstance(event, TurnEnded):
                await record_episode(inst, self.name, session)
                for model, usage in meter.pending:  # each call once, even across rewrites
                    await inst.record_usage(self.name, self.config.model_role, model, usage)
                meter.pending.clear()
            yield event

    async def reply(self, text: str) -> str:
        """What the contact may see: tokens of ``reveal_in_output`` classes resolved, others
        masked."""
        pii = self.instance.pii
        text = await render_citations(self.instance.knowledge, text)
        return await pii.detokenize(text, pii.policy.reveal_in_output) if pii.active else text


def _secret_refs(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        if set(obj) == {"$secret"}:
            return {str(obj["$secret"])}
        return set().union(*(_secret_refs(v) for v in obj.values())) if obj else set()
    if isinstance(obj, list):
        return set().union(*(_secret_refs(v) for v in obj)) if obj else set()
    return set()


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
