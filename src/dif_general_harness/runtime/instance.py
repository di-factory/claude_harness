"""The instance runtime: a resolved solution spec turned into runnable agents.

``Instance.open`` does, in order:
1. resolve every ``$secret`` reference (values are registered for redaction);
2. build the per-role model router;
3. build the tool sources the spec asks for: built-in packs, HTTP connectors, MCP servers;
4. apply ``tools.overrides`` (effect, verify, permission) and the permission policy.

Each agent then gets its rendered system prompt, the tools matching its globs, a policy gate
and a budget meter per run. Every event of a run is persisted, redacted, in the session store.

What the spec asks for but M1 cannot provide yet (knowledge, ledger, calendar connector,
Python extensions, verification checks) is reported in ``issues`` rather than silently
dropped. Tools with a ``verify`` check are forced to ``ask`` until the verification engine
exists, so a missing check never lets a side effect through unreviewed.
"""

from __future__ import annotations

import dataclasses
import fnmatch
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2

from ..core.events import Event
from ..core.loop import LoopConfig, run
from ..core.scope import Scope
from ..core.session import Session
from ..policy import Approver, DailySpend, Limits, PermissionPolicy, PolicyGate, Redactor, RunMeter
from ..providers.base import ModelProvider
from ..spec.errors import Issue
from ..spec.loader import ResolvedSpec
from ..spec.schema import Agent, SolutionSpec
from ..store.jsonl import JsonlSessionStore
from ..tenancy.secrets import EnvSecrets, SecretBackend, SecretResolver
from ..tools.http import ConnectorError, http_tools
from ..tools.mcp import McpToolSource
from ..tools.packs import NoteStore, Workspace, coding_tools, general_tools
from ..tools.packs.coding import Executor
from ..tools.registry import Effect, Tool, ToolRegistry
from .prompts import load_text, render
from .routing import ProviderFactory, build_router

LOCAL_TENANT = "local"


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
        self.store = JsonlSessionStore(options.state_root, redactor=self.redactor)
        self.daily = DailySpend()
        self.issues: list[Issue] = []
        self.tools = ToolRegistry()
        self.policy = PermissionPolicy()
        self.provider: ModelProvider | None = None
        self.workspace: Workspace | None = None  # the coding pack's, for undo
        self._stack = AsyncExitStack()

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
            await inst._build()
        except BaseException:
            await inst.close()
            raise
        return inst

    async def close(self) -> None:
        await self._stack.aclose()

    async def __aenter__(self) -> Instance:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

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
        self._configure(tools)

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
            if t.verify:
                ask.append(t.name)  # until verification checks run, a person reviews the call
            self.tools.register(t)
        for name, o in overrides.items():
            if o.permission:
                {"allow": allow, "ask": ask, "deny": deny}[o.permission].append(name)
        self.policy = PermissionPolicy(
            allow=allow, ask=ask, deny=deny, profile=self.spec.policies.profile
        )
        if self.spec.policies.verification.checks:
            self._warn(
                "verification_pending",
                "policies.verification",
                "verification checks are not run yet; tools with verify require approval",
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
        self.tools = ToolRegistry(
            [t for n in registry.names() if _matches(n, spec.tools) and (t := registry.get(n))]
        )
        self.missing_tools = [
            p for p in spec.tools if not any(_matches(n, [p]) for n in self.tools.names())
        ]
        self.gate = PolicyGate(instance.policy, self.tools, instance.options.approver)
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

    def new_session(self, contact_key: str | None = None) -> Session:
        inst = self.instance
        session = Session(
            scope=inst.scope,
            agent_id=self.name,
            contact_key=contact_key,
            config_version=inst.resolved.version_hash,
        )
        inst.store.append(session.started_event())
        return session

    def resume(self, session_id: str) -> Session:
        return self.instance.store.load(self.instance.scope, session_id)

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

    async def send(self, session: Session, text: str) -> AsyncIterator[Event]:
        """Run one user turn; every event is persisted (redacted) and yielded."""
        inst = self.instance
        assert inst.provider is not None
        async for event in run(
            session,
            text,
            inst.provider,
            self.tools,
            self.config,
            gate=self.gate,
            meter=self.meter(),
        ):
            inst.store.append(event)
            yield event


def _secret_refs(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        if set(obj) == {"$secret"}:
            return {str(obj["$secret"])}
        return set().union(*(_secret_refs(v) for v in obj.values())) if obj else set()
    if isinstance(obj, list):
        return set().union(*(_secret_refs(v) for v in obj)) if obj else set()
    return set()
