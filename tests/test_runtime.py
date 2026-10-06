"""The instance runtime (M1.4): specs turned into runnable agents, offline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from dif_general_harness.core.events import Event, TurnEnded
from dif_general_harness.core.messages import (
    Message,
    Role,
    ToolResultBlock,
    ToolStatus,
    ToolUseBlock,
    Usage,
)
from dif_general_harness.policy import AutoApprover, Verdict
from dif_general_harness.providers import FakeProvider
from dif_general_harness.providers.base import ModelRequest, ProviderMessage
from dif_general_harness.runtime import (
    Instance,
    InstanceError,
    PromptError,
    RoutingError,
    RuntimeOptions,
    build_router,
    render,
)
from dif_general_harness.spec import PackCatalog, load_instance, load_pack
from dif_general_harness.spec.errors import Issue
from dif_general_harness.spec.schema import ModelRoleConfig
from dif_general_harness.tenancy import EnvSecrets
from dif_general_harness.tools import Effect

IDP_TOKEN = "idp-token-" + "Qz8rT3wL9pX2vN7m"


def _clinic(examples: Path) -> Any:
    path = examples / "instances" / "clinica-sonrisa.json"
    return load_instance(path, PackCatalog(roots=[examples]))


def _service_desk(examples: Path, **values: Any) -> Any:
    instance = {
        "spec_version": "1",
        "kind": "instance",
        "solution": {"id": "acme-it", "version": "1.0.0", "lob": "service-desk"},
        "extends": ["service-desk-cell@^1.0"],
        "tenant": {"id": "acme", "name": "ACME"},
        "values": {
            "company_name": "ACME",
            "helpdesk_mcp_url": "https://helpdesk.acme.example/mcp",
            "idp_url": "https://idp.acme.example",
            "kb_folder_id": "f1",
            "escalation_group": "it-l2",
            "ops_channel": "#it-ops",
            "main_model": "claude-opus-5-5",
            "fast_model": "claude-haiku-4-5",
            **values,
        },
    }
    path = examples / "instances" / "acme-it.json"
    path.write_text(json.dumps(instance), encoding="utf-8")
    resolved = load_instance(path, PackCatalog(roots=[examples]))
    assert resolved.ok, resolved.issues
    return resolved


def _helpdesk() -> MCPServer:
    server = MCPServer("helpdesk")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def get_ticket(ticket_id: str) -> dict[str, str]:
        """Fetch a ticket."""
        return {"id": ticket_id, "subject": "Locked out", "requester": "ana@acme.example"}

    @server.tool()
    def solve_ticket(ticket_id: str) -> str:
        """Solve a ticket."""
        return "solved"

    @server.tool()
    def delete_ticket(ticket_id: str) -> str:
        """Delete a ticket."""
        return "deleted"

    return server


def _idp() -> httpx2.AsyncClient:
    def handle(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == f"Bearer {IDP_TOKEN}"
        return httpx2.Response(200, json={"id": "u-1", "token_echo": IDP_TOKEN})

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))


def _options(tmp_path: Path, provider: Any, **kw: Any) -> RuntimeOptions:
    kw.setdefault("http_client", _idp())
    return RuntimeOptions(
        state_root=tmp_path / "state",
        secrets=EnvSecrets({"DIF_SECRET_IDP": IDP_TOKEN}),
        provider=provider,
        **kw,
    )


# --- prompts and routing -----------------------------------------------------------


def test_prompt_rendering(examples: Path) -> None:
    spec = _clinic(examples).spec
    text = render(
        "Hi from {{var.business_name}} ({{solution.locale}}), {{var.business_hours}}", spec
    )
    assert text == "Hi from Clínica Sonrisa (es-MX), mon-fri: 09:00-19:00, sat: 09:00-14:00"
    assert render("{{ tenant.timezone }}", spec) == "America/Mexico_City"
    with pytest.raises(PromptError, match=r"var\.nope"):
        render("{{var.nope}}", spec)


def test_router_builds_per_role() -> None:
    made: list[tuple[str, str, str | None]] = []

    def factory(role: ModelRoleConfig, settings: dict[str, Any]) -> FakeProvider:
        made.append((role.model, role.effort, settings.get("api_key")))
        return FakeProvider([Message.assistant(role.model)])

    roles = {
        "main": ModelRoleConfig(provider="anthropic", model="claude-opus-5-5", effort="high"),
        "router": ModelRoleConfig(provider="anthropic", model="claude-haiku-4-5", effort="low"),
    }
    router = build_router(roles, {"anthropic": {"api_key": "k"}}, {"anthropic": factory})
    assert made == [("claude-opus-5-5", "high", "k"), ("claude-haiku-4-5", "low", "k")]
    assert router.for_role("router") is router.providers["router"]
    assert router.for_role("compaction") is router.providers["main"]  # falls back to main

    def role(model: str, provider: str = "anthropic") -> dict[str, ModelRoleConfig]:
        return {"main": ModelRoleConfig(provider=provider, model=model)}

    with pytest.raises(RoutingError, match="not set"):
        build_router(role("<main-model-id>"), {})
    with pytest.raises(RoutingError, match="unknown provider"):
        build_router(role("m", "acme-llm"), {})
    with pytest.raises(RoutingError, match="not supported"):
        build_router(role("m"), {"anthropic": {"via": "bedrock"}})
    with pytest.raises(RoutingError, match="'main'"):
        build_router({"router": role("m")["main"]}, {})


async def test_router_dispatches_by_role() -> None:
    main = FakeProvider([Message.assistant("main")])
    fast = FakeProvider([Message.assistant("fast")])
    from dif_general_harness.runtime import RoleRouter

    router = RoleRouter({"main": main, "router": fast})
    events = [
        e async for e in router.stream(ModelRequest(system="", messages=[], model_role="router"))
    ]
    assert isinstance(events[-1], ProviderMessage) and events[-1].message.text() == "fast"
    assert fast.requests and not main.requests


# --- the instance ------------------------------------------------------------------


async def test_clinic_instance_runs_and_reports_gaps(examples: Path, tmp_path: Path) -> None:
    provider = FakeProvider([Message.assistant("¡Hola! ¿En qué le ayudo?")])
    async with await Instance.open(_clinic(examples), _options(tmp_path, provider)) as inst:
        assert (inst.scope.tenant_id, inst.scope.instance_id) == (
            "clinica-sonrisa",
            "clinica-sonrisa-appointments",
        )
        gaps = {(i.code, i.path) for i in inst.issues}
        assert ("missing_secret", "tools.config.connectors/google-calendar") in gaps  # no key

        agent = inst.agent()
        assert agent.name == "receptionist"
        assert "Clínica Sonrisa" in agent.system and "{{" not in agent.system
        assert "calendar.find_slots" in agent.missing_tools
        assert agent.per_run.usd == 0.2 and agent.config.max_turns == 12

        session = await agent.new_session(contact_key="+5215512345678")
        events = [e async for e in agent.send(session, "Hola")]
        assert isinstance(events[-1], TurnEnded) and events[-1].reason == "end_turn"
        assert provider.requests[0].system == agent.system
        assert provider.requests[0].model_role == "main"

        resumed = await agent.resume(session.id)
        assert [m.role for m in resumed.messages] == [Role.USER, Role.ASSISTANT]
        assert resumed.config_version == inst.resolved.version_hash
        assert await inst.store.list_sessions(inst.scope) == [session.id]
        assert (tmp_path / "state" / "dif.db").exists()


async def test_service_desk_tools_policy_and_redaction(examples: Path, tmp_path: Path) -> None:
    calls = FakeProvider(
        [
            Message(
                role=Role.ASSISTANT,
                content=[
                    ToolUseBlock(id="a", name="helpdesk.get_ticket", input={"ticket_id": "T-9"}),
                    ToolUseBlock(id="b", name="identity.reset_password", input={"user_id": "u-1"}),
                    ToolUseBlock(id="c", name="helpdesk.delete_ticket", input={"ticket_id": "T-9"}),
                    ToolUseBlock(
                        id="d", name="identity.lookup_user", input={"email": "ana@acme.example"}
                    ),
                ],
            ),
            Message.assistant(f"Done. (debug: {IDP_TOKEN})"),
        ]
    )
    options = _options(tmp_path, calls, mcp_servers={"helpdesk": _helpdesk()})
    async with await Instance.open(_service_desk(examples), options) as inst:
        names = inst.tools.names()
        assert {"helpdesk.get_ticket", "identity.lookup_user", "identity.reset_password"} <= set(
            names
        )
        solve = inst.tools.get("helpdesk.solve_ticket")
        assert solve and solve.effect is Effect.EXTERNAL and solve.verify == "resolution-verified"

        policy = inst.policy
        assert policy.decide("helpdesk.delete_ticket", Effect.EXTERNAL, {}).verdict is Verdict.DENY
        # the pack allows it explicitly; its verifier check gates it (no forced ask any more)
        assert policy.decide("helpdesk.solve_ticket", Effect.EXTERNAL, {}).verdict is Verdict.ALLOW
        assert (
            policy.decide("identity.reset_password", Effect.EXTERNAL, {"user_id": "u"}).verdict
            is Verdict.ASK
        )

        resolver = inst.agent("resolver")
        assert resolver.tools.names() == [
            "agent.kb_researcher",  # its sub-agent, as a tool
            "handoff.human",  # the agent lists 'human' among its handoffs
            "helpdesk.get_ticket",
            "helpdesk.solve_ticket",
            "history.search",  # the pack has a compaction role: what a summary cut, found again
            "identity.lookup_user",
            "identity.reset_password",
            "memory.propose_skill",  # the pack declares memory layers
            "memory.search",
            "memory.write",
        ]
        assert resolver.missing_tools == ["helpdesk.add_comment"]

        session = await resolver.new_session()
        [e async for e in resolver.send(session, "Ana is locked out, ticket T-9")]
        results = {
            b.tool_use_id: b for b in session.messages[2].content if isinstance(b, ToolResultBlock)
        }
        assert results["a"].status is ToolStatus.OK
        assert results["b"].status is ToolStatus.DENIED  # ask, and nobody to ask (headless)
        assert results["c"].status is ToolStatus.DENIED  # not in the agent's tools
        assert results["d"].status is ToolStatus.OK

        rows = await inst.db.fetchall("SELECT data FROM events")
        raw = "".join(r["data"] for r in rows)
        assert IDP_TOKEN not in raw and "[REDACTED]" in raw


async def test_approver_and_budgets(examples: Path, tmp_path: Path) -> None:
    expensive = ProviderMessage(
        message=Message(
            role=Role.ASSISTANT,
            content=[ToolUseBlock(id="a", name="helpdesk.get_ticket", input={"ticket_id": "T-1"})],
        ),
        usage=Usage(input_tokens=200_000),  # $0.80 on claude-opus-5-5, over the $0.50 run limit
        stop_reason="tool_use",
        model="claude-opus-5-5",
    )
    provider = FakeProvider([expensive, Message.assistant("never")])
    options = _options(
        tmp_path, provider, mcp_servers={"helpdesk": _helpdesk()}, approver=AutoApprover()
    )
    async with await Instance.open(_service_desk(examples), options) as inst:
        triage = inst.agent("triage")
        events: list[Event] = [e async for e in triage.send(await triage.new_session(), "T-1?")]
        assert isinstance(events[-1], TurnEnded) and events[-1].reason == "budget"
        assert len(provider.requests) == 1


async def test_open_failures(examples: Path, tmp_path: Path) -> None:
    # without a provider override the router needs the llm secret
    no_secrets = RuntimeOptions(state_root=tmp_path, secrets=EnvSecrets({}))
    with pytest.raises(InstanceError, match=r"secrets\.llm"):
        await Instance.open(_service_desk(examples), no_secrets)
    # packs have unfilled variables; only instances run
    with pytest.raises(InstanceError, match="only instances run"):
        await Instance.open(load_pack(examples / "service-desk-cell"), no_secrets)
    # placeholder model ids are refused before any call is made
    path = examples / "instances" / "clinica-sonrisa.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["values"]["main_model"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises((InstanceError, RoutingError), match="no model chosen"):
        await Instance.open(
            _clinic(examples),
            RuntimeOptions(state_root=tmp_path, secrets=EnvSecrets({"DIF_SECRET_ANTHROPIC": "k"})),
        )
    # a spec with validation errors never starts
    broken = _service_desk(examples)
    broken.issues.append(Issue("error", "planted", "agents", "planted error"))
    with pytest.raises(InstanceError, match="planted"):
        await Instance.open(broken, _options(tmp_path, FakeProvider([])))


async def test_unreachable_mcp_server_is_reported(examples: Path, tmp_path: Path) -> None:
    resolved = _service_desk(examples, helpdesk_mcp_url="http://localhost:9/mcp")
    options = _options(tmp_path, FakeProvider([]))
    options.secrets = EnvSecrets({"DIF_SECRET_IDP": IDP_TOKEN, "DIF_SECRET_HELPDESK": "hd-token"})
    async with await Instance.open(resolved, options) as inst:
        assert any(i.code == "mcp_unavailable" for i in inst.issues)
        assert not any(n.startswith("helpdesk.") for n in inst.tools.names())


def test_cli_run(examples: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from dif_general_harness.cli import main

    instance = examples / "instances" / "clinica-sonrisa.json"
    state = tmp_path / "state"
    provider = FakeProvider([Message.assistant("Con gusto.")])
    argv = ["run", str(instance), "--state", str(state), "-m", "Hola"]
    assert main(argv, provider=provider) == 0
    out, err = capsys.readouterr()
    assert out.strip() == "Con gusto."
    assert "session " in err and "[end_turn, 1 turn(s)" in err
    assert "note: tools not available: calendar.find_slots" in err

    assert main([*argv[:-2], "--agent", "nobody", "-m", "x"], provider=provider) == 2
    assert "unknown agent" in capsys.readouterr().err
