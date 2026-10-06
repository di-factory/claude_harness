"""Permissions, approvals, budgets, secrets and redaction (M1.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from dif_general_harness.core.events import ErrorEvent, Event, TurnEnded
from dif_general_harness.core.loop import run
from dif_general_harness.core.messages import (
    Message,
    Role,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolStatus,
    ToolUseBlock,
    Usage,
)
from dif_general_harness.core.scope import Scope
from dif_general_harness.core.session import Session
from dif_general_harness.policy import (
    DEFAULT_PRICES,
    ApprovalDecision,
    ApprovalRequest,
    AutoApprover,
    DailySpend,
    Limits,
    PermissionPolicy,
    PolicyGate,
    Price,
    Redactor,
    RunMeter,
    Verdict,
    cost_usd,
)
from dif_general_harness.policy.permissions import Rule
from dif_general_harness.providers import FakeProvider
from dif_general_harness.providers.base import ProviderMessage
from dif_general_harness.store.jsonl import JsonlSessionStore
from dif_general_harness.tenancy import EnvSecrets, FileSecrets, MissingSecret, SecretResolver
from dif_general_harness.tools import Effect, ToolRegistry, tool

ANTHROPIC_KEY = "sk-ant-api03-" + "Zq7xW2vR9tLm4KpN8sBy3HcF6dJg1QeU"
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


@tool("calendar.find_slots")
async def find_slots(day: str) -> list[str]:
    """Free slots."""
    return [f"{day} 10:00"]


@tool("calendar.book", effect=Effect.WRITE)
async def book(slot: str) -> str:
    """Book a slot."""
    return "booked"


@tool("identity.reset_password", effect=Effect.EXTERNAL)
async def reset_password(user_id: str) -> str:
    """Reset a password."""
    return "reset"


def _tools() -> ToolRegistry:
    return ToolRegistry([find_slots, book, reset_password])


def _call(name: str, **args: object) -> ToolUseBlock:
    return ToolUseBlock(id="t1", name=name, input=dict(args))


# --- permission rules --------------------------------------------------------------


def test_rule_parsing() -> None:
    r = Rule.parse("identity.reset_password(user_id=admin*, reason=*)")
    assert r.tool == "identity.reset_password"
    assert r.args == (("user_id", "admin*"), ("reason", "*"))
    assert Rule.parse("effect:external").effect is Effect.EXTERNAL
    assert Rule.parse("coding.bash(git push --force*)").args == (
        ("*primary*", "git push --force*"),
    )
    for bad in ["tool(x=1", "tool(x=1, y)", "effect:dangerous", "two words"]:
        with pytest.raises(ValueError):
            Rule.parse(bad)


def test_most_restrictive_rule_wins() -> None:
    policy = PermissionPolicy(
        allow=["calendar.*", "identity.*"],
        ask=["calendar.book"],
        deny=["identity.reset_password(user_id=admin*)"],
    )
    assert policy.decide("calendar.find_slots", Effect.READ, {}).verdict is Verdict.ALLOW
    decision = policy.decide("calendar.book", Effect.WRITE, {"slot": "x"})
    assert (decision.verdict, decision.rule) == (Verdict.ASK, "calendar.book")
    admin = policy.decide("identity.reset_password", Effect.EXTERNAL, {"user_id": "admin-1"})
    assert admin.verdict is Verdict.DENY
    # the argument pattern does not match, so the allow glob applies
    user = policy.decide("identity.reset_password", Effect.EXTERNAL, {"user_id": "u-42"})
    assert user.verdict is Verdict.ALLOW
    # a missing argument never matches an argument-scoped rule
    assert policy.decide("identity.reset_password", Effect.EXTERNAL, {}).verdict is Verdict.ALLOW


def test_effect_rules_and_profile_defaults() -> None:
    policy = PermissionPolicy(allow=["calendar.*"], deny=["effect:external"])
    assert policy.decide("calendar.anything", Effect.EXTERNAL, {}).verdict is Verdict.DENY
    default = PermissionPolicy()
    assert default.decide("x", Effect.READ, {}).verdict is Verdict.ALLOW
    assert default.decide("x", Effect.WRITE, {}).verdict is Verdict.ASK
    assert default.decide("x", Effect.EXTERNAL, {}).verdict is Verdict.ASK
    fast = PermissionPolicy(profile="fast")
    assert fast.decide("x", Effect.WRITE, {}).verdict is Verdict.ALLOW
    assert fast.decide("x", Effect.EXTERNAL, {}).verdict is Verdict.ASK
    assert default.decide("x", Effect.WRITE, {}).rule is None


def test_positional_patterns_and_chained_commands() -> None:
    policy = PermissionPolicy(allow=["coding.bash(ls*)"], deny=["coding.bash(git push --force*)"])

    def verdict(command: str) -> Verdict:
        return policy.decide("coding.bash", Effect.WRITE, {"command": command}, "command").verdict

    assert verdict("ls -la") is Verdict.ALLOW
    assert verdict("git push --force origin main") is Verdict.DENY
    for sneaky in [
        "echo hi && git push --force",
        "true; git push --force",
        "x $(git push --force)",
    ]:
        assert verdict(sneaky) is Verdict.DENY
    assert verdict("ls; rm -rf ~") is Verdict.ASK  # allow needs every segment to match
    assert verdict("ls | grep x") is Verdict.ASK
    # without a known primary argument a positional pattern never matches
    assert policy.decide("coding.bash", Effect.WRITE, {"command": "ls"}).verdict is Verdict.ASK


def test_wrapped_and_backgrounded_commands_cannot_hide_from_a_rule() -> None:
    policy = PermissionPolicy(allow=["coding.bash(git status*)", "coding.bash(ls*)"],
                              deny=["coding.bash(rm -rf*)"])  # fmt: skip

    def verdict(command: str) -> Verdict:
        return policy.decide("coding.bash", Effect.WRITE, {"command": command}, "command").verdict

    for sneaky in [
        "sudo rm -rf /",
        "sudo -u root rm -rf /",
        "FOO=1 BAR='a b' rm -rf /",
        "env -i rm -rf /",
        "timeout 5 rm -rf build",
        "nohup rm -rf / &",
        "ls & rm -rf /",
        "bash -c 'ls && rm -rf /'",
        'sh -c "rm -rf /"',
    ]:
        assert verdict(sneaky) is Verdict.DENY, sneaky
    assert verdict("ls 2>&1") is Verdict.ALLOW  # a redirect is not a second command
    assert verdict("git status") is Verdict.ALLOW
    assert verdict("sudo git status") is Verdict.ASK  # allow sees the command as written
    assert verdict("PATH=/tmp git status") is Verdict.ASK


# --- the gate ----------------------------------------------------------------------


class Recorder:
    def __init__(self, decision: ApprovalDecision) -> None:
        self.decision = decision
        self.requests: list[ApprovalRequest] = []

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return self.decision


async def test_gate_denies_asks_headless(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="a")
    gate = PolicyGate(PermissionPolicy(deny=["identity.*"]), _tools())
    assert (await gate.check(session, _call("calendar.find_slots", day="d"))).allowed
    denied = await gate.check(session, _call("identity.reset_password", user_id="u"))
    assert not denied.allowed and "identity.*" in denied.reason
    # write → ask → no approver headless → denied
    assert not (await gate.check(session, _call("calendar.book", slot="s"))).allowed


async def test_gate_unknown_tool_is_treated_as_external(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="a")
    gate = PolicyGate(PermissionPolicy(profile="fast"), _tools())
    assert not (await gate.check(session, _call("mystery.tool"))).allowed


async def test_gate_approvals_and_remember(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="a")
    once = Recorder(ApprovalDecision(True))
    gate = PolicyGate(PermissionPolicy(), _tools(), once)
    for _ in range(2):
        assert (await gate.check(session, _call("calendar.book", slot="s"))).allowed
    assert len(once.requests) == 2
    req = once.requests[0]
    assert (req.tool, req.effect, req.arguments) == ("calendar.book", Effect.WRITE, {"slot": "s"})

    remember = Recorder(ApprovalDecision(True, remember=True))
    gate = PolicyGate(PermissionPolicy(), _tools(), remember)
    for _ in range(3):
        assert (await gate.check(session, _call("calendar.book", slot="s"))).allowed
    assert len(remember.requests) == 1
    other = Session(scope=scope, agent_id="a")  # remembered per session only
    await gate.check(other, _call("calendar.book", slot="s"))
    assert len(remember.requests) == 2

    refused = PolicyGate(PermissionPolicy(), _tools(), Recorder(ApprovalDecision(False)))
    assert not (await refused.check(session, _call("calendar.book", slot="s"))).allowed
    auto = PolicyGate(PermissionPolicy(deny=["calendar.book"]), _tools(), AutoApprover())
    assert not (await auto.check(session, _call("calendar.book", slot="s"))).allowed


async def test_loop_with_policy_gate(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="a")
    provider = FakeProvider(
        [
            Message(
                role=Role.ASSISTANT,
                content=[
                    ToolUseBlock(id="a", name="calendar.find_slots", input={"day": "d"}),
                    ToolUseBlock(id="b", name="calendar.book", input={"slot": "s"}),
                ],
            ),
            Message.assistant("done"),
        ]
    )
    gate = PolicyGate(PermissionPolicy(), _tools())
    events = [ev async for ev in run(session, "book", provider, _tools(), gate=gate)]
    assert isinstance(events[-1], TurnEnded) and events[-1].reason == "end_turn"
    results = {
        b.tool_use_id: b.status
        for b in session.messages[2].content
        if isinstance(b, ToolResultBlock)
    }
    assert results == {"a": ToolStatus.OK, "b": ToolStatus.DENIED}


# --- budgets -----------------------------------------------------------------------


def test_pricing() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=500_000)
    assert cost_usd(usage, DEFAULT_PRICES["claude-opus-5-5"]) == pytest.approx(4.0 + 2.0 + 0.1)
    assert cost_usd(Usage(cache_write_tokens=1_000_000), Price(1, 1, 1, 7)) == 7


def test_limits_from_spec() -> None:
    limits = Limits.from_spec({"usd": 2, "tokens": 50000, "turns": 8, "wall_time": "10m"})
    assert limits == Limits(usd=2.0, tokens=50000, turns=8, wall_time_s=600.0)
    assert Limits.from_spec(None) == Limits()
    assert Limits.from_spec({"wall_time": "30s"}).wall_time_s == 30


def test_meter_limits(scope: Scope) -> None:
    meter = RunMeter("t", Limits(usd=1.0))
    priced = meter.charge(Usage(input_tokens=100_000), "claude-opus-5-5")
    assert priced.cost_usd == pytest.approx(0.4)
    assert meter.exceeded(1) is None
    meter.charge(Usage(output_tokens=40_000), "claude-opus-5-5")
    assert "limit" in (meter.exceeded(2) or "")

    tokens = RunMeter("t", Limits(tokens=10))
    tokens.charge(Usage(input_tokens=10), None)
    assert "tokens" in (tokens.exceeded(1) or "")
    assert RunMeter("t", Limits(turns=3)).exceeded(2) is None
    assert "turns" in (RunMeter("t", Limits(turns=3)).exceeded(3) or "")
    assert "took" in (RunMeter("t", Limits(wall_time_s=0)).exceeded(0) or "")


def test_meter_unpriced_models_are_reported() -> None:
    meter = RunMeter("t", Limits(usd=0.01))
    meter.charge(Usage(input_tokens=10**9), "some-local-model")
    assert meter.unpriced == {"some-local-model"}
    assert meter.total.cost_usd == 0


def test_tenant_daily_limit_spans_runs() -> None:
    daily = DailySpend()
    day = Limits(usd=1.0)
    first = RunMeter("clinic", Limits(), day, daily=daily)
    first.charge(Usage(output_tokens=60_000), "claude-opus-5-5")  # $1.20
    second = RunMeter("clinic", Limits(), day, daily=daily)
    assert "today" in (second.exceeded(0) or "")
    other = RunMeter("other-tenant", Limits(), day, daily=daily)
    assert other.exceeded(0) is None


async def test_loop_stops_on_budget(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="a")
    step = ProviderMessage(
        message=Message(
            role=Role.ASSISTANT,
            content=[ToolUseBlock(id="a", name="calendar.find_slots", input={"day": "d"})],
        ),
        usage=Usage(input_tokens=300_000),
        stop_reason="tool_use",
        model="claude-opus-5-5",
    )
    provider = FakeProvider([step, Message.assistant("never reached")])
    meter = RunMeter("t", Limits(usd=1.0))
    events: list[Event] = [ev async for ev in run(session, "hi", provider, _tools(), meter=meter)]
    assert isinstance(events[-1], TurnEnded) and events[-1].reason == "budget"
    assert events[-1].usage.cost_usd == pytest.approx(1.2)
    assert any(isinstance(e, ErrorEvent) and "budget" in e.message for e in events)
    assert len(provider.requests) == 1
    # the tool call still got its result, so the history stays valid for a resume
    assert isinstance(session.messages[-1].content[0], ToolResultBlock)


# --- secrets -----------------------------------------------------------------------


def test_env_and_file_backends(tmp_path: Path) -> None:
    env = EnvSecrets({"DIF_SECRET_CLOUD_READONLY": "v1"})
    assert env.key("cloud-readonly") == "DIF_SECRET_CLOUD_READONLY"
    assert env.get("cloud-readonly") == "v1" and env.get("llm") is None
    (tmp_path / "llm").write_text("file-value\n")
    assert FileSecrets(tmp_path).get("llm") == "file-value"
    for bad in ["../etc/passwd", "A", ""]:
        with pytest.raises(ValueError):
            FileSecrets(tmp_path).get(bad)


def test_resolver_replaces_refs_and_registers_values() -> None:
    redactor = Redactor()
    resolver = SecretResolver(EnvSecrets({"DIF_SECRET_LLM": "llm-secret-value"}), redactor)
    spec = {"providers": {"anthropic": {"api_key": {"$secret": "llm"}, "via": "direct"}}}
    resolved = resolver.resolve(spec)
    assert resolved["providers"]["anthropic"] == {"api_key": "llm-secret-value", "via": "direct"}
    assert spec["providers"]["anthropic"]["api_key"] == {"$secret": "llm"}  # input untouched
    assert redactor.redact("key=llm-secret-value") == "key=[REDACTED]"
    assert resolver.missing(["llm", "crm"]) == ["crm"]
    with pytest.raises(MissingSecret):
        resolver.resolve([{"$secret": "crm"}])


# --- redaction ---------------------------------------------------------------------


def test_redactor_patterns_and_survivors() -> None:
    r = Redactor(["hunter2-value"])
    planted = (
        f"anthropic {ANTHROPIC_KEY}, aws {AWS_KEY}, pw hunter2-value, "
        "auth Bearer abcdefghijklmnop1234, token Xk9mQ2vL7pR4tN8wZ3yB6cF1hJ5dG0sA"
    )
    out = r.redact(planted)
    for secret in [ANTHROPIC_KEY, AWS_KEY, "hunter2-value", "abcdefghijklmnop1234", "Xk9mQ2vL7p"]:
        assert secret not in out
    survivors = (
        "session 3f2a9c1e4b5d6f708192a3b4c5d6e7f8, sha256 "
        "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08, "
        "path /home/user/claude_harness/src/module.py, short Ab1"
    )
    assert r.redact(survivors) == survivors
    assert Redactor(["abc"]).redact("abc") == "abc"  # too short to register


async def test_store_redacts_but_keeps_ids_and_signatures(tmp_path: Path, scope: Scope) -> None:
    signature = "EqQBCkYIBRgCKkBx7Y2mN4pQ8rS1tU5vW9xZ3aB6cD0eF2gH4iJ7kL1mN3oP5qR8sT0uV"
    tool_id = "toolu_01XFDUDYJgAACzvnptvVoYELmQ2b"
    session = Session(scope=scope, agent_id="a")
    store = JsonlSessionStore(tmp_path, redactor=Redactor(["clinic-db-password"]))
    await store.append(session.started_event())
    await store.append(session.add_message(Message.user(f"my key is {ANTHROPIC_KEY}")))
    await store.append(
        session.add_message(
            Message(
                role=Role.ASSISTANT,
                content=[
                    ThinkingBlock(
                        text=f"user sent {AWS_KEY}", provider="anthropic", signature=signature
                    ),
                    TextBlock(text="Checking."),
                    ToolUseBlock(
                        id=tool_id, name="db.query", input={"password": "clinic-db-password"}
                    ),
                ],
            )
        )
    )
    await store.append(
        session.add_message(
            Message(
                role=Role.USER,
                content=[
                    ToolResultBlock(
                        tool_use_id=tool_id, status=ToolStatus.OK, content=ANTHROPIC_KEY
                    )
                ],
            )
        )
    )
    raw = next(tmp_path.rglob("*.jsonl")).read_text()
    for secret in [ANTHROPIC_KEY, AWS_KEY, "clinic-db-password"]:
        assert secret not in raw
    resumed = await store.load(scope, session.id)
    assistant = resumed.messages[1].content
    assert isinstance(assistant[0], ThinkingBlock) and assistant[0].signature == signature
    assert isinstance(assistant[2], ToolUseBlock) and assistant[2].id == tool_id
    assert assistant[2].input == {"password": "[REDACTED]"}
    result = resumed.messages[2].content[0]
    assert isinstance(result, ToolResultBlock) and result.tool_use_id == tool_id
    assert result.content == "[REDACTED]"


def test_agent_budgets_only_tighten() -> None:
    instance = Limits(usd=0.5, turns=20)
    assert instance.tighten(Limits(usd=2.0)) == Limits(usd=0.5, turns=20)
    assert instance.tighten(Limits(usd=0.1, tokens=1000)) == Limits(usd=0.1, tokens=1000, turns=20)


def test_agent_daily_limit() -> None:
    daily = DailySpend()
    cto = RunMeter("opc", Limits(), daily=daily, agent="cto", per_agent_day=Limits(usd=1.0))
    cto.charge(Usage(output_tokens=60_000), "claude-opus-5-5")  # $1.20
    again = RunMeter("opc", Limits(), daily=daily, agent="cto", per_agent_day=Limits(usd=1.0))
    assert "agent spent" in (again.exceeded(0) or "")
    cgo = RunMeter("opc", Limits(), daily=daily, agent="cgo", per_agent_day=Limits(usd=1.0))
    assert cgo.exceeded(0) is None
