"""The agent loop (ARCHITECTURE §3.2): call the model, run tools, feed results back.

The loop owns control flow only. Policy lives behind two small interfaces:
- ``ToolGate`` decides, per tool call, whether it may run (permissions, approvals).
- ``Meter`` prices each model call and stops the run when a budget is exhausted.

Cheap caps stop a run that is going nowhere (``stuck``): the same tool call with the same input
again after ``max_repeats`` times, or ``max_tool_errors`` turns in a row whose tool calls all
failed; and one reply's wall-clock limit (``timeout``), checked between model calls.

Invariants:
- Every tool call in the history gets exactly one result, so the next request is valid.
- Tools never run from a refused turn or from a turn cut off by ``max_tokens``.
- Every event is stamped with the session's scope and a sequence number.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

from ..providers.base import (
    ContextOverflow,
    ModelProvider,
    ModelRequest,
    ProviderMessage,
    ProviderTextDelta,
)
from ..tools.registry import UNKNOWN_HINT, Effect, ToolRegistry
from .events import (
    ErrorEvent,
    Event,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnEnded,
)
from .messages import (
    Message,
    Role,
    TextBlock,
    ToolResultBlock,
    ToolStatus,
    ToolUseBlock,
    Usage,
)
from .session import Session


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str = ""


class ToolGate(Protocol):
    async def check(self, session: Session, call: ToolUseBlock) -> GateDecision: ...


class Meter(Protocol):
    def charge(self, usage: Usage, model: str | None) -> Usage:
        """Return the usage with its cost filled in, and record it."""
        ...

    def exceeded(self, turns: int) -> str | None:
        """A human-readable reason when a budget is exhausted, else None."""
        ...


class AllowAll:
    async def check(self, session: Session, call: ToolUseBlock) -> GateDecision:
        return GateDecision(True)


class NoBudget:
    def charge(self, usage: Usage, model: str | None) -> Usage:
        return usage

    def exceeded(self, turns: int) -> str | None:
        return None


EndReason = Literal[
    "end_turn",
    "max_turns",
    "max_tokens",
    "refusal",
    "budget",
    "error",
    "stuck",
    "timeout",
    "overflow",
]
FAILED = {ToolStatus.ERROR, ToolStatus.TIMEOUT}


@dataclass(frozen=True)
class LoopConfig:
    system: str = ""
    model_role: str = "main"
    max_turns: int = 12
    max_repeats: int = 3  # the same call, same input, more times than this: stuck
    max_tool_errors: int = 3  # this many turns in a row with every tool call failed: stuck
    max_tool_calls: int | None = None  # tool calls one run may execute (budgets.tool_calls)
    max_seconds: float | None = None  # one reply's wall-clock limit


async def run(
    session: Session,
    user_input: str | None,
    provider: ModelProvider,
    tools: ToolRegistry,
    config: LoopConfig | None = None,
    *,
    gate: ToolGate | None = None,
    meter: Meter | None = None,
) -> AsyncIterator[Event]:
    """Run one user turn to completion, yielding every event. ``user_input`` None continues
    the history as it is (after compaction made room for a turn that did not fit). A history
    the model's context cannot hold ends the run as ``overflow``, for the caller to compact."""
    cfg = config or LoopConfig()
    gate = gate or AllowAll()
    meter = meter or NoBudget()
    repaired = _interrupted(session, tools)
    if repaired or user_input is not None:  # results first: they must follow their calls
        text = [TextBlock(text=user_input)] if user_input is not None else []
        yield session.add_message(Message(role=Role.USER, content=[*repaired, *text]))
    usage = Usage()
    turns = 0
    started = time.monotonic()
    calls_seen: dict[str, int] = {}
    executed = 0
    failed_turns = 0

    def end(reason: EndReason) -> TurnEnded:
        return session.stamp(
            TurnEnded(
                scope=session.scope, session_id=session.id, reason=reason, turns=turns, usage=usage
            )
        )

    def error(message: str) -> ErrorEvent:
        return session.stamp(
            ErrorEvent(scope=session.scope, session_id=session.id, message=message)
        )

    while True:
        if turns >= cfg.max_turns:
            yield end("max_turns")
            return
        if (why := meter.exceeded(turns)) is not None:
            yield error(f"budget exhausted: {why}")
            yield end("budget")
            return
        if cfg.max_seconds is not None and time.monotonic() - started > cfg.max_seconds:
            yield error(f"no answer within {cfg.max_seconds:g} s")
            yield end("timeout")
            return
        turns += 1

        request = ModelRequest(
            system=cfg.system,
            messages=list(session.messages),
            tools=tools.schemas(),
            model_role=cfg.model_role,
        )
        final: ProviderMessage | None = None
        try:
            async for pev in provider.stream(request):
                if isinstance(pev, ProviderTextDelta):
                    yield session.stamp(
                        TextDelta(scope=session.scope, session_id=session.id, text=pev.text)
                    )
                else:
                    final = pev
        except ContextOverflow:
            yield end("overflow")
            return
        except Exception as exc:
            yield error(f"provider error: {type(exc).__name__}: {exc}")
            yield end("error")
            return
        if final is None:
            yield error("provider ended without a final message")
            yield end("error")
            return

        usage = usage + meter.charge(final.usage, final.model)
        if final.message.content:  # an empty assistant turn would make the next request invalid
            yield session.add_message(final.message)
        calls = final.message.tool_uses()

        if final.stop_reason == "refusal":
            if calls:  # keep the history valid: every tool call gets a result
                yield session.add_message(_not_run(calls, "the model's turn was refused"))
            category = f" ({final.refusal_category})" if final.refusal_category else ""
            yield error(f"the model declined the request{category}")
            yield end("refusal")
            return
        if final.stop_reason == "pause_turn":
            continue  # server-side work paused; resend the history to resume
        if final.stop_reason == "max_tokens":
            if calls:
                yield session.add_message(_not_run(calls, "tool input was cut off by max_tokens"))
                yield error("tool call truncated by max_tokens; not executed")
            yield end("max_tokens")
            return
        if not calls:
            yield end("end_turn")
            return

        repeated = None
        for call in calls:
            key = call.name + json.dumps(call.input, sort_keys=True, default=str)
            calls_seen[key] = calls_seen.get(key, 0) + 1
            if calls_seen[key] > cfg.max_repeats:
                repeated = call
        if repeated is not None:
            times = calls_seen[repeated.name + json.dumps(repeated.input, sort_keys=True,
                                                          default=str)]  # fmt: skip
            yield session.add_message(_not_run(calls, "the same call was repeated"))
            yield error(f"stuck: {repeated.name} called {times} times with the same input")
            yield end("stuck")
            return
        if cfg.max_tool_calls is not None and executed + len(calls) > cfg.max_tool_calls:
            yield session.add_message(_not_run(calls, "the run's tool-call budget is spent"))
            yield error(f"budget exhausted: {cfg.max_tool_calls} tool calls")
            yield end("budget")
            return
        executed += len(calls)
        for call in calls:
            yield session.stamp(
                ToolCallStarted(
                    scope=session.scope,
                    session_id=session.id,
                    tool_use_id=call.id,
                    name=call.name,
                    input=call.input,
                )
            )
        results = await asyncio.gather(*(_run_one(session, gate, tools, c) for c in calls))
        for call, (result, ms) in zip(calls, results, strict=True):
            yield session.stamp(
                ToolCallFinished(
                    scope=session.scope,
                    session_id=session.id,
                    tool_use_id=call.id,
                    name=call.name,
                    status=result.status,
                    duration_ms=ms,
                )
            )
        yield session.add_message(Message(role=Role.USER, content=[r for r, _ in results]))
        failed_turns = failed_turns + 1 if all(r.status in FAILED for r, _ in results) else 0
        if failed_turns >= cfg.max_tool_errors:
            yield error(f"stuck: every tool call failed {failed_turns} turns in a row")
            yield end("stuck")
            return


def _interrupted(session: Session, tools: ToolRegistry) -> list[ToolResultBlock]:
    """Results for calls that never got one (the process stopped while they ran): a call
    that only reads may simply be made again; any other may have taken effect."""
    answered = {b.tool_use_id for m in session.messages for b in m.content
                if isinstance(b, ToolResultBlock)}  # fmt: skip
    out = []
    for message in session.messages:
        for call in message.tool_uses():
            if call.id in answered:
                continue
            tool = tools.get(call.name)
            reads = tool is not None and tool.effect is Effect.READ
            out.append(ToolResultBlock(
                tool_use_id=call.id, status=ToolStatus.ERROR, reason="interrupted",
                error="the call was interrupted before its result came back",
                retryable=reads, side_effects="none" if reads else "unknown",
                hint=None if reads else UNKNOWN_HINT,
            ))  # fmt: skip
    return out


def _not_run(calls: list[ToolUseBlock], why: str) -> Message:
    return Message(
        role=Role.USER,
        content=[
            ToolResultBlock(tool_use_id=c.id, status=ToolStatus.ERROR, error=f"not executed: {why}")
            for c in calls
        ],
    )


async def _run_one(
    session: Session, gate: ToolGate, tools: ToolRegistry, call: ToolUseBlock
) -> tuple[ToolResultBlock, int]:
    start = time.monotonic()
    try:
        decision = await gate.check(session, call)
    except Exception as exc:  # a failing gate must never let the call through
        decision = GateDecision(False, f"permission check failed: {type(exc).__name__}: {exc}")
    if decision.allowed:
        result = await tools.execute(call)
    else:
        result = ToolResultBlock(
            tool_use_id=call.id, status=ToolStatus.DENIED, error=decision.reason or "denied",
            reason="denied", retryable=False, side_effects="none",
            hint="not allowed here: do not try another way to do the same thing; tell the"
            " contact what you can do instead, or hand over to a person",
        )  # fmt: skip
    return result, int((time.monotonic() - start) * 1000)
