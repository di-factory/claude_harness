"""Permissions and approvals (ARCHITECTURE §3.6, decision 13).

Rules (from ``policies.permissions`` in the spec):
- ``calendar.find_slots`` or globs such as ``knowledge.*``
- argument patterns: ``identity.reset_password(user_id=admin*)``; every listed argument must match
- a positional pattern matches the tool's primary argument (its first required input):
  ``coding.bash(git push --force*)``
- effects: ``effect:external`` (also ``effect:write``, ``effect:read``)

Values are split on shell operators (``;``, ``&&``, ``||``, ``|``, newlines, ``$(``, backticks)
so that chained commands cannot slip past a rule: a deny or ask rule matches when any
segment matches; an allow rule only when every segment does. Pattern rules on shell
commands are a guardrail, not a sandbox; the executor is the containment boundary.

When several rules match, the most restrictive wins: deny > ask > allow. With no match,
the guardrail profile decides by effect. ``ask`` goes to an ``Approver``: the console
prompts; headless runs use the approvals inbox (M2) and, until then, deny.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol

from ..core.loop import GateDecision
from ..core.messages import ToolUseBlock
from ..core.session import Session
from ..tools.registry import Effect, Tool, ToolRegistry

_RULE = re.compile(r"^(?P<tool>[^()\s]+)(?:\((?P<args>[^()]*)\))?$")
_SEGMENTS = re.compile(r"\s*(?:;|&&|\|\||\||\n|\$\(|`|\))\s*")
PRIMARY = "*primary*"  # the argument key of a positional pattern


class Verdict(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


_RANK = {Verdict.ALLOW: 0, Verdict.ASK: 1, Verdict.DENY: 2}
Profile = Literal["strict", "default", "fast"]
PROFILE_DEFAULTS: dict[str, dict[Effect, Verdict]] = {
    "strict": {Effect.READ: Verdict.ALLOW, Effect.WRITE: Verdict.ASK, Effect.EXTERNAL: Verdict.ASK},
    "default": {
        Effect.READ: Verdict.ALLOW,
        Effect.WRITE: Verdict.ASK,
        Effect.EXTERNAL: Verdict.ASK,
    },
    "fast": {Effect.READ: Verdict.ALLOW, Effect.WRITE: Verdict.ALLOW, Effect.EXTERNAL: Verdict.ASK},
}


@dataclass(frozen=True)
class Rule:
    text: str
    tool: str | None
    effect: Effect | None
    args: tuple[tuple[str, str], ...]

    @classmethod
    def parse(cls, text: str) -> Rule:
        if text.startswith("effect:"):
            return cls(text=text, tool=None, effect=Effect(text.removeprefix("effect:")), args=())
        m = _RULE.match(text.strip())
        if not m:
            raise ValueError(f"invalid permission rule {text!r}")
        inner = (m.group("args") or "").strip()
        if inner and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", inner):
            return cls(text=text, tool=m.group("tool"), effect=None, args=((PRIMARY, inner),))
        args: list[tuple[str, str]] = []
        for part in filter(None, (p.strip() for p in inner.split(","))):
            key, sep, pattern = part.partition("=")
            if not sep:
                raise ValueError(f"invalid argument pattern {part!r} in rule {text!r}")
            args.append((key.strip(), pattern.strip()))
        return cls(text=text, tool=m.group("tool"), effect=None, args=tuple(args))

    def matches(
        self,
        tool: str,
        effect: Effect,
        arguments: dict[str, Any],
        *,
        primary: str | None = None,
        every_segment: bool = False,
    ) -> bool:
        if self.effect is not None:
            return self.effect == effect
        if self.tool is None or not fnmatch.fnmatchcase(tool, self.tool):
            return False
        for key, pattern in self.args:
            name = primary if key == PRIMARY else key
            if name is None or name not in arguments:
                return False
            segments = [s for s in _SEGMENTS.split(str(arguments[name])) if s] or [""]
            hits = (fnmatch.fnmatchcase(s, pattern) for s in segments)
            if not (all(hits) if every_segment else any(hits)):
                return False
        return True


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    rule: str | None  # the matching rule, or None when the profile default applied


@dataclass
class PermissionPolicy:
    allow: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    profile: Profile = "default"

    def __post_init__(self) -> None:
        self._rules = (
            [(Verdict.DENY, Rule.parse(r)) for r in self.deny]
            + [(Verdict.ASK, Rule.parse(r)) for r in self.ask]
            + [(Verdict.ALLOW, Rule.parse(r)) for r in self.allow]
        )

    def decide(
        self, tool: str, effect: Effect, arguments: dict[str, Any], primary: str | None = None
    ) -> Decision:
        """``primary`` is the tool's main argument, used by positional patterns."""
        best: tuple[Verdict, str] | None = None
        for verdict, rule in self._rules:
            hit = rule.matches(
                tool, effect, arguments, primary=primary, every_segment=verdict is Verdict.ALLOW
            )
            if hit and (best is None or _RANK[verdict] > _RANK[best[0]]):
                best = (verdict, rule.text)
        if best is not None:
            return Decision(best[0], best[1])
        return Decision(PROFILE_DEFAULTS[self.profile][effect], None)


# --- approvals ---------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalRequest:
    session: Session
    tool: str
    effect: Effect
    arguments: dict[str, Any]
    rule: str | None


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    remember: bool = False  # approve this tool for the rest of the session
    reason: str = ""


class Approver(Protocol):
    async def approve(self, request: ApprovalRequest) -> ApprovalDecision: ...


class DenyApprover:
    """Headless default until the approvals inbox exists (M2): nobody is there to ask."""

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(False, reason="approval required but no approver is available")


class AutoApprover:
    """Approves everything. For tests and explicitly trusted local runs only."""

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(True)


class PolicyGate:
    """The loop's ``ToolGate``: applies the policy, asks the approver when needed."""

    def __init__(
        self, policy: PermissionPolicy, tools: ToolRegistry, approver: Approver | None = None
    ) -> None:
        self.policy = policy
        self.tools = tools
        self.approver = approver or DenyApprover()
        self._remembered: set[tuple[str, str]] = set()  # (session id, tool)

    async def check(self, session: Session, call: ToolUseBlock) -> GateDecision:
        tool = self.tools.get(call.name)
        effect = tool.effect if tool else Effect.EXTERNAL  # unknown tools get the strictest effect
        decision = self.policy.decide(call.name, effect, call.input, primary_argument(tool))
        if decision.verdict is Verdict.DENY:
            return GateDecision(False, f"denied by rule {decision.rule!r}")
        if decision.verdict is Verdict.ALLOW:
            return GateDecision(True)
        if (session.id, call.name) in self._remembered:
            return GateDecision(True)
        answer = await self.approver.approve(
            ApprovalRequest(session, call.name, effect, call.input, decision.rule)
        )
        if not answer.approved:
            return GateDecision(False, answer.reason or "not approved")
        if answer.remember:
            self._remembered.add((session.id, call.name))
        return GateDecision(True)


def primary_argument(tool: Tool | None) -> str | None:
    """The first required input of a tool, else its first input."""
    if tool is None:
        return None
    required = tool.input_schema.get("required") or []
    if required:
        return str(required[0])
    props = list(tool.input_schema.get("properties") or {})
    return props[0] if props else None
