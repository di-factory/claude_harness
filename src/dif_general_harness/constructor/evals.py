"""Eval suites (decision 35): YAML, one case per document, run against a built instance.

v1 runs what M1 can observe. Conversational cases:
  turns:  ``user`` (send a message), ``expect_tool`` (called since the last user message),
          ``expect_reply_contains`` (text or list, case-insensitive)
  must_not: ``{tool: name}``: the model must never even attempt the call
Outcome cases (``expect``) run when ``setup.message`` starts them:
  ``tools_called``, ``must_not`` (tool names), ``approval_requested``

Anything else (triggers, templates, handoffs, fixtures, ledgers) needs M2 and the case is
reported as **skipped** with the reason, never as passed. Approvals are recorded and denied,
so an eval never commits a side effect that needs a person.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from ..core.events import TextDelta, ToolCallStarted, TurnEnded
from ..policy import ApprovalDecision, ApprovalRequest
from ..runtime import AgentRuntime

TURN_STEPS = {"user", "expect_tool", "expect_reply_contains"}
EXPECT_KEYS = {"tools_called", "must_not", "approval_requested"}


class RecordingApprover:
    """Denies every approval and remembers it was asked (for ``approval_requested``)."""

    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(False, reason="evals never approve side effects")


@dataclass
class CaseResult:
    suite: str
    case: str
    status: Literal["passed", "failed", "skipped"]
    reasons: list[str] = field(default_factory=list)
    unsafe_actions: int = 0
    cost_usd: float = 0.0


@dataclass
class EvalReport:
    results: list[CaseResult]
    thresholds: dict[str, float]

    @property
    def ran(self) -> list[CaseResult]:
        return [r for r in self.results if r.status != "skipped"]

    @property
    def pass_rate(self) -> float | None:
        return sum(r.status == "passed" for r in self.ran) / len(self.ran) if self.ran else None

    @property
    def unsafe_actions(self) -> int:
        return sum(r.unsafe_actions for r in self.results)

    @property
    def ok(self) -> bool:
        need = self.thresholds.get("pass_rate", 1.0)
        rate = self.pass_rate
        return (rate is None or rate >= need) and self.unsafe_actions <= self.thresholds.get(
            "unsafe_actions", 0
        )


def load_suite(path: Path) -> list[dict[str, Any]]:
    cases = [c for c in yaml.safe_load_all(path.read_text(encoding="utf-8")) if c]
    for i, case in enumerate(cases):
        if not isinstance(case, dict) or "id" not in case:
            raise ValueError(f"{path}: case {i + 1} needs an id")
    return cases


def unsupported(case: dict[str, Any]) -> list[str]:
    """Why a case cannot run in M1; empty when it can."""
    missing: list[str] = []
    if "turns" in case:
        for step in case["turns"]:
            missing += [f"turn step {k!r}" for k in step if k not in TURN_STEPS]
        for rule in case.get("must_not", []):
            missing += [f"must_not {k!r}" for k in rule if k != "tool"]
    elif "expect" in case:
        if not (case.get("setup") or {}).get("message"):
            starts = sorted(set(case.get("setup") or {}) - {"contact"}) or ["no setup.message"]
            missing.append(f"start from {', '.join(starts)}")
        missing += [f"expect {k!r}" for k in case["expect"] if k not in EXPECT_KEYS]
    else:
        missing.append("no turns or expect")
    return missing


async def run_case(
    agent: AgentRuntime, suite: str, case: dict[str, Any], approvals: RecordingApprover
) -> CaseResult:
    result = CaseResult(suite, str(case["id"]), "passed")
    if why := unsupported(case):
        result.status = "skipped"
        result.reasons = [f"needs M2: {w}" for w in why]
        return result
    contact = (case.get("setup") or {}).get("contact") or {}
    session = agent.new_session(contact_key=contact.get("first_name"))
    approvals.requests.clear()
    all_calls: list[str] = []
    turn_calls: list[str] = []
    reply = ""

    async def say(text: str) -> None:
        nonlocal reply
        turn_calls.clear()
        reply = ""
        async for event in agent.send(session, text):
            if isinstance(event, TextDelta):
                reply += event.text
            elif isinstance(event, ToolCallStarted):
                turn_calls.append(event.name)
                all_calls.append(event.name)
            elif isinstance(event, TurnEnded):
                result.cost_usd += event.usage.cost_usd

    if "turns" in case:
        for step in case["turns"]:
            if "user" in step:
                await say(str(step["user"]))
            elif "expect_tool" in step and step["expect_tool"] not in turn_calls:
                result.reasons.append(f"expected a call to {step['expect_tool']}; got {turn_calls}")
            elif "expect_reply_contains" in step:
                wanted = step["expect_reply_contains"]
                for word in [wanted] if isinstance(wanted, str) else wanted:
                    if str(word).lower() not in reply.lower():
                        result.reasons.append(f"reply lacks {word!r}")
        forbidden = [r["tool"] for r in case.get("must_not", [])]
    else:
        expect = case["expect"]
        await say(str(case["setup"]["message"]))
        for name in expect.get("tools_called", []):
            if name not in all_calls:
                result.reasons.append(f"expected a call to {name}; got {all_calls}")
        if expect.get("approval_requested") and not approvals.requests:
            result.reasons.append("expected an approval request; none was made")
        forbidden = list(expect.get("must_not", []))

    for name in forbidden:
        attempts = all_calls.count(name)
        if attempts:
            result.unsafe_actions += attempts
            result.reasons.append(f"attempted forbidden tool {name} ({attempts}x)")
    if result.reasons:
        result.status = "failed"
    return result


async def run_suites(
    agent: AgentRuntime,
    suites: list[Path],
    approvals: RecordingApprover,
    thresholds: dict[str, float] | None = None,
) -> EvalReport:
    results: list[CaseResult] = []
    for path in suites:
        if not path.is_file():
            # validation already warns about unwritten suites (eval_missing)
            results.append(CaseResult(path.name, "*", "skipped", ["suite not written yet"]))
            continue
        for case in load_suite(path):
            results.append(await run_case(agent, path.name, case, approvals))
    return EvalReport(results, thresholds or {})
