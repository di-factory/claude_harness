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

What the spec asks for but M1 cannot provide yet (knowledge, ledger, calendar connector,
Python extensions, verification checks) is reported in ``issues`` rather than silently
dropped. Tools with a ``verify`` check are forced to ``ask`` until the verification engine
exists, so a missing check never lets a side effect through unreviewed.
"""

from __future__ import annotations

import dataclasses
import fnmatch
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2

from ..core.events import Event, ToolCallFinished, ToolCallStarted, TurnEnded
from ..core.loop import LoopConfig, run
from ..core.messages import ToolResultBlock, ToolUseBlock
from ..core.scope import Scope
from ..core.session import Session
from ..governance import AuditLog, ConsentStore, GovernedTools, PiiPolicy, Tokenizer, TokenVault
from ..governance.contacts import ContactStore
from ..hitl.inbox import Inbox
from ..policy import Approver, DailySpend, Limits, PermissionPolicy, PolicyGate, Redactor, RunMeter
from ..policy.escalation import first_match
from ..policy.spend import SpendStore
from ..providers.base import ModelProvider
from ..spec.errors import Issue
from ..spec.loader import ResolvedSpec
from ..spec.schema import Agent, SolutionSpec
from ..store.db import Database, connect
from ..store.sql import SqlSessionStore
from ..tenancy.secrets import EnvSecrets, SecretBackend, SecretResolver
from ..tools.http import ConnectorError, http_tools
from ..tools.mcp import McpToolSource
from ..tools.packs import NoteStore, Workspace, coding_tools, general_tools
from ..tools.packs.coding import Executor
from ..tools.registry import Effect, Tool, ToolRegistry
from ..verify import Verifier
from .context import current_session
from .prompts import load_text, render
from .routing import ProviderFactory, build_router

LOCAL_TENANT = "local"
HANDOFF_TOOL = "handoff.human"
Notify = Callable[[str, str], Awaitable[None]]  # (inbox item id, one-line summary)


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
        self._stack = AsyncExitStack()
        self.owns_db = False  # True when this instance opened the database (closes it too)
        self.db: Database  # set in _open_database
        self.store: SqlSessionStore
        self.pii: Tokenizer
        self.consent: ConsentStore
        self.audit: AuditLog
        self.spend: SpendStore
        self.inbox: Inbox
        self.contacts: ContactStore
        self.verifier = Verifier(self, on_failed_twice=self._verification_failed_twice)
        self.notify: Notify | None = None  # set by the service: tells a person about inbox items

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
        self.inbox = Inbox(self.db, self.scope)
        self.contacts = ContactStore(self.db)
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
            settings = self.secrets.resolve(raw)
            self.provider = build_router(
                spec.models.roles, settings, self.options.provider_factories
            )

        tools: list[Tool] = []
        tools += self._packs(data)
        tools += self._http(data, missing)
        tools += await self._mcp(data, missing)
        if any("human" in a.handoffs for a in spec.agents.values()):
            tools.append(self._handoff_tool())
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

    async def _verification_failed_twice(
        self, session: Session, call: ToolUseBlock, reason: str
    ) -> None:
        escalation = self.spec.policies.escalation or {}
        context = {
            "verification": {"failed_twice": True, "reason": reason},
            "tool": call.name,
            "var": self.spec.values,
        }
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

    def _packs(self, data: dict[str, Any]) -> list[Tool]:
        out: list[Tool] = []
        for pack in self.spec.tools.packs:
            if pack == "general":
                out += general_tools(NoteStore(self.options.state_root, self.scope))
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
                out += coding_tools(self.workspace, self.options.executor)
            else:
                self._warn(
                    "unavailable_pack", "tools.packs", f"tool pack {pack!r} is not built yet"
                )
        for ref in self.spec.tools.python:
            self._warn("unavailable_tool", "tools.python", f"Python tool {ref!r} is not loaded yet")
        return out

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
            if t.name == HANDOFF_TOOL:
                allow.append(t.name)  # asking a person is always allowed
            self.tools.register(t)
        for name in self.tools.names():  # a check that cannot run here: a person reviews
            registered = self.tools.get(name)
            check = registered.verify if registered else None
            why = self.verifier.unrunnable(check) if check else None
            if why:
                ask.append(name)
                self._warn("verification_unavailable", f"tools.{name}", f"{why}; asking instead")
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
        patterns = [*spec.tools, *([HANDOFF_TOOL] if "human" in spec.handoffs else [])]
        selected = ToolRegistry(
            [t for n in registry.names() if _matches(n, patterns) and (t := registry.get(n))]
        )
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
        self, session: Session, text: str, *, names: list[str] | None = None
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
        try:
            async for event in self._run(session, safe_text, meter, started, full):
                yield event
        finally:
            current_session.reset(token)

    async def _run(
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
        async for event in run(
            session, safe_text, inst.provider, self.tools, self.config, gate=self.gate, meter=meter
        ):
            await inst.store.append(event)
            if isinstance(event, ToolCallStarted):
                started[event.tool_use_id] = event
            elif isinstance(event, ToolCallFinished):
                tool = self.tools.get(event.name)
                effect = tool.effect if tool else Effect.EXTERNAL
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
                await inst.spend.add(scope, "tenant", meter.total.cost_usd)
                await inst.spend.add(scope, f"agent:{self.name}", meter.total.cost_usd)
                for model, usd in meter.by_model.items():
                    await inst.spend.add(scope, f"model:{model}", usd)
            yield event

    async def reply(self, text: str) -> str:
        """What the contact may see: tokens of ``reveal_in_output`` classes resolved, others
        masked."""
        pii = self.instance.pii
        return await pii.detokenize(text, pii.policy.reveal_in_output) if pii.active else text


def _secret_refs(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        if set(obj) == {"$secret"}:
            return {str(obj["$secret"])}
        return set().union(*(_secret_refs(v) for v in obj.values())) if obj else set()
    if isinstance(obj, list):
        return set().union(*(_secret_refs(v) for v in obj)) if obj else set()
    return set()
