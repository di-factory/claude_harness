"""Verification: maker != checker (ARCHITECTURE §3.10, decision 14).

Before a tool call with side effects commits, its checks run:
- ``tool``: call a read tool (with the call's matching arguments, or ``args`` templates such
  as ``"{{args.slot}}"``) and compare the result with ``expect``;
- ``condition``: a CEL expression over ``args``, ``contact`` (the contact's attributes),
  ``var`` and ``tool``;
- ``command``: run a command in the agent's workspace; it must exit 0;
- ``verifier``: the verifier model role judges the call against ``criteria`` and must answer
  ``{"pass": true|false, "reason": "..."}``; anything else counts as a failure.
``citations`` checks apply to answers, not tool calls (the knowledge module runs them).

The spec's ``policies.verification.verifier`` adds the verifier agent to calls matching
``applies_to``: every such call (``mode: always``) or only calls that already have a
``verify`` check (``critical_only``).

A failure goes back to the maker as the denial reason, so it can correct itself. The second
failure for the same tool in one conversation sets ``verification.failed_twice`` for the
escalation rules; after that the call is refused without checking again.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core import cel
from ..core.messages import Message, ToolStatus, ToolUseBlock
from ..core.session import Session
from ..policy.permissions import Rule
from ..providers.base import ModelRequest, ProviderMessage
from ..tools.registry import Effect, Tool

if TYPE_CHECKING:
    from ..runtime.instance import Instance

MAX_FAILURES = 2
_ARG_REF = re.compile(r"\{\{\s*args\.([A-Za-z0-9_]+)\s*\}\}")
VERIFIER_PROMPT = """You verify one action an AI agent wants to take, before it takes effect.
You are not the agent. Judge only whether the action satisfies every criterion, given the
conversation. Answer with JSON only: {"pass": true, "reason": "..."} or
{"pass": false, "reason": "what is wrong, in one sentence the agent can act on"}."""
DEFAULT_CRITERIA = [
    "the action is what the contact asked for, and the contact confirmed it",
    "every argument is consistent with the conversation (no invented values)",
    "the action breaks no rule stated in the agent's instructions",
]


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reason: str = ""
    check: str = ""


@dataclass
class Verifier:
    instance: Instance
    failures: dict[tuple[str, str], int] = field(default_factory=dict)
    on_failed_twice: Callable[[Session, ToolUseBlock, str], Awaitable[None]] | None = None

    # --- which checks apply ----------------------------------------------------------

    def checks_for(self, tool: Tool | None, call: ToolUseBlock) -> list[str]:
        names = [tool.verify] if tool is not None and tool.verify else []
        cfg = self.instance.spec.policies.verification.verifier or {}
        if cfg:
            mode = cfg.get("mode", "critical_only")
            applies = [Rule.parse(r) for r in cfg.get("applies_to", ["effect:external"])]
            effect = tool.effect if tool else Effect.EXTERNAL
            matches = any(r.matches(call.name, effect, call.input) for r in applies)
            if matches and (mode == "always" or names):
                names.append("__verifier__")
        return names

    def unrunnable(self, check_name: str) -> str | None:
        """Why a check cannot run in this instance (so the runtime falls back to asking)."""
        checks = self.instance.spec.policies.verification.checks
        check = checks.get(check_name)
        if check is None:
            return f"unknown check {check_name!r}"
        extra = check.model_extra or {}
        if check.type == "tool" and self.instance.tools.get(str(extra.get("tool"))) is None:
            return f"check tool {extra.get('tool')!r} is not available"
        if check.type == "command" and self.instance.workspace is None:
            return "command checks need a workspace"
        if check.type == "citations":
            return "citations checks apply to answers, not tool calls"
        if check.type == "condition" and cel.check(str(extra.get("expr", ""))):
            return "invalid condition"
        return None

    # --- running them ----------------------------------------------------------------

    async def verify(self, session: Session, call: ToolUseBlock, tool: Tool | None) -> Verdict:
        names = self.checks_for(tool, call)
        if not names:
            return Verdict(True)
        key = (session.id, call.name)
        if self.failures.get(key, 0) >= MAX_FAILURES:
            return Verdict(False, "verification already failed twice; a person was asked", "")
        for name in names:
            verdict = await self._run(name, session, call, tool)
            await self.instance.audit.record(
                self.instance.scope, "verifier", "verification", call.name,
                {"check": name, "passed": verdict.passed, "reason": verdict.reason,
                 "session": session.id},
            )  # fmt: skip
            if not verdict.passed:
                self.failures[key] = self.failures.get(key, 0) + 1
                if self.failures[key] >= MAX_FAILURES and self.on_failed_twice is not None:
                    await self.on_failed_twice(session, call, verdict.reason)
                return verdict
        return Verdict(True)

    async def _run(
        self, name: str, session: Session, call: ToolUseBlock, tool: Tool | None
    ) -> Verdict:
        if name == "__verifier__":
            cfg = self.instance.spec.policies.verification.verifier or {}
            criteria = cfg.get("criteria") or DEFAULT_CRITERIA
            return await self._verifier(
                session, call, list(criteria), cfg.get("model_role", "verifier"), name
            )
        check = self.instance.spec.policies.verification.checks[name]
        extra = check.model_extra or {}
        try:
            if check.type == "tool":
                return await self._tool_check(name, extra, call)
            if check.type == "condition":
                ok = cel.holds(str(extra.get("expr", "")), await self._context(session, call))
                return Verdict(ok, "" if ok else f"condition not met: {extra.get('expr')}", name)
            if check.type == "command":
                return await self._command_check(name, extra)
            if check.type == "verifier":
                criteria = list(extra.get("criteria") or DEFAULT_CRITERIA)
                role = (self.instance.spec.policies.verification.verifier or {}).get(
                    "model_role", "verifier"
                )
                return await self._verifier(session, call, criteria, role, name)
        except Exception as exc:  # a check that cannot run never lets the call through
            return Verdict(False, f"check {name} could not run: {type(exc).__name__}: {exc}", name)
        return Verdict(False, f"check {name} ({check.type}) cannot verify a tool call", name)

    async def _context(self, session: Session, call: ToolUseBlock) -> dict[str, Any]:
        inst = self.instance
        attributes = (
            await inst.contacts.get(inst.scope, session.contact_key) if session.contact_key else {}
        )
        return {
            "args": call.input,
            "tool": call.name,
            "contact": {"key": session.contact_key, **attributes},
            "var": inst.spec.values,
        }

    async def _tool_check(self, name: str, extra: dict[str, Any], call: ToolUseBlock) -> Verdict:
        tool_name = str(extra.get("tool"))
        check_tool = self.instance.tools.get(tool_name)
        if check_tool is None:
            return Verdict(False, f"check tool {tool_name} is not available", name)
        if extra.get("args"):
            args = {
                k: _ARG_REF.sub(lambda m: str(call.input.get(m.group(1), "")), v)
                if isinstance(v, str) else v
                for k, v in dict(extra["args"]).items()
            }  # fmt: skip
            for k, v in dict(extra["args"]).items():  # a whole-value reference keeps its type
                m = _ARG_REF.fullmatch(v) if isinstance(v, str) else None
                if m:
                    args[k] = call.input.get(m.group(1))
        else:
            wanted = set((check_tool.input_schema.get("properties") or {}).keys())
            args = {k: v for k, v in call.input.items() if k in wanted}
        result = await self.instance.tools.execute(
            ToolUseBlock(id=f"verify_{call.id}", name=tool_name, input=args)
        )
        if result.status is not ToolStatus.OK:
            return Verdict(False, f"{tool_name} failed: {result.error}", name)
        ok = _matches(result.content, extra.get("expect"), extra.get("expr"))
        reason = (
            "" if ok else f"{tool_name} returned {json.dumps(result.content, default=str)[:300]}"
        )
        return Verdict(ok, reason, name)

    async def _command_check(self, name: str, extra: dict[str, Any]) -> Verdict:
        from ..tools.packs.coding import SubprocessExecutor

        ws = self.instance.workspace
        if ws is None:
            return Verdict(False, "command checks need a workspace", name)
        runner = self.instance.executor or self.instance.options.executor or SubprocessExecutor()
        result = await runner.run(
            str(extra.get("run", "")), ws.root, float(extra.get("timeout_s", 600))
        )
        expected = int(extra.get("expect_exit", 0))
        ok = not result.timed_out and result.exit_code == expected
        tail = result.output[-500:]
        return Verdict(ok, "" if ok else f"command exited {result.exit_code}: {tail}", name)

    async def _verifier(
        self, session: Session, call: ToolUseBlock, criteria: list[str], role: str, name: str
    ) -> Verdict:
        inst = self.instance
        assert inst.provider is not None
        transcript = "\n".join(f"{m.role}: {m.text()}" for m in session.messages[-12:] if m.text())
        question = (
            "Criteria:\n" + "\n".join(f"- {c}" for c in criteria)
            + f"\n\nConversation (most recent last):\n{transcript}\n\n"
            + f"Action: {call.name} with arguments {json.dumps(call.input, ensure_ascii=False)}"
        )  # fmt: skip
        request = ModelRequest(
            system=VERIFIER_PROMPT, messages=[Message.user(question)], tools=[], model_role=role
        )
        final: ProviderMessage | None = None
        async for event in inst.provider.stream(request):
            if isinstance(event, ProviderMessage):
                final = event
        if final is None:
            return Verdict(False, "the verifier gave no answer", name)
        await _charge(inst, final)
        text = final.message.text().strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        try:
            answer = json.loads(match.group(0) if match else text)
        except ValueError:
            return Verdict(False, "the verifier's answer was not valid JSON", name)
        if not isinstance(answer, dict) or not isinstance(answer.get("pass"), bool):
            return Verdict(False, "the verifier's answer had no pass/fail", name)
        return Verdict(answer["pass"], str(answer.get("reason", "")), name)


async def _charge(inst: Instance, final: ProviderMessage) -> None:
    await inst.charge("verifier", "verifier", final)


def _matches(result: Any, expect: Any, expr: Any) -> bool:
    """``expect``: the result equals it, its ``status``/``result`` equals it, it names a truthy
    field, or (for text) it appears in it. ``expr``: CEL over ``result``. Both must hold."""
    if expr and not cel.holds(str(expr), {"result": result}):
        return False
    if expect is None:
        return bool(result) or result == 0
    if result == expect:
        return True
    if isinstance(result, dict):
        for key in ("status", "result", "outcome"):
            if result.get(key) == expect:
                return True
        return isinstance(expect, str) and bool(result.get(expect))
    return isinstance(result, str) and isinstance(expect, str) and expect in result
