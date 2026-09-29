"""The agent loop (ARCHITECTURE §3.2): call the model, run tools, feed results back.

M0 scope: streaming, parallel tool calls, structured observations, a turn cap and
usage accounting. Context management, permissions, budgets and verification wrap
this loop in later milestones without changing its shape.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ..providers.base import ModelProvider, ModelRequest, ProviderMessage, ProviderTextDelta
from ..tools.registry import ToolRegistry
from .events import ErrorEvent, Event, TextDelta, ToolCallFinished, ToolCallStarted, TurnEnded
from .messages import Message, Role, ToolResultBlock, ToolUseBlock, Usage
from .session import Session


@dataclass(frozen=True)
class LoopConfig:
    system: str = ""
    model_role: str = "main"
    max_turns: int = 12


async def run(
    session: Session,
    user_input: str,
    provider: ModelProvider,
    tools: ToolRegistry,
    config: LoopConfig | None = None,
) -> AsyncIterator[Event]:
    """Run one user turn to completion, yielding every event (stamped with the session scope)."""
    cfg = config or LoopConfig()
    yield session.add_message(Message.user(user_input))
    usage = Usage()
    turns = 0

    while True:
        if turns >= cfg.max_turns:
            yield session.stamp(
                TurnEnded(
                    scope=session.scope,
                    session_id=session.id,
                    reason="max_turns",
                    turns=turns,
                    usage=usage,
                )
            )
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
        except Exception as exc:
            yield session.stamp(
                ErrorEvent(
                    scope=session.scope,
                    session_id=session.id,
                    message=f"provider error: {type(exc).__name__}: {exc}",
                )
            )
            yield session.stamp(
                TurnEnded(
                    scope=session.scope,
                    session_id=session.id,
                    reason="error",
                    turns=turns,
                    usage=usage,
                )
            )
            return
        if final is None:
            yield session.stamp(
                ErrorEvent(
                    scope=session.scope,
                    session_id=session.id,
                    message="provider ended without a final message",
                )
            )
            yield session.stamp(
                TurnEnded(
                    scope=session.scope,
                    session_id=session.id,
                    reason="error",
                    turns=turns,
                    usage=usage,
                )
            )
            return

        usage = usage + final.usage
        yield session.add_message(final.message)
        calls = final.message.tool_uses()
        if not calls:
            yield session.stamp(
                TurnEnded(
                    scope=session.scope,
                    session_id=session.id,
                    reason="end_turn",
                    turns=turns,
                    usage=usage,
                )
            )
            return

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
        results = await asyncio.gather(*(_timed(tools, call) for call in calls))
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


async def _timed(tools: ToolRegistry, call: ToolUseBlock) -> tuple[ToolResultBlock, int]:
    start = time.monotonic()
    result = await tools.execute(call)
    return result, int((time.monotonic() - start) * 1000)
