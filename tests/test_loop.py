"""The agent loop, run offline against scripted FakeProvider responses."""

from __future__ import annotations

import asyncio

import pytest

from dif_general_harness.core.events import (
    ErrorEvent,
    Event,
    MessageAdded,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnEnded,
)
from dif_general_harness.core.loop import LoopConfig, run
from dif_general_harness.core.messages import (
    Message,
    Role,
    TextBlock,
    ToolResultBlock,
    ToolStatus,
    ToolUseBlock,
)
from dif_general_harness.core.scope import Scope
from dif_general_harness.core.session import Session
from dif_general_harness.providers import FakeProvider
from dif_general_harness.tools import Effect, ToolRegistry, tool


@tool("calendar.find_slots")
async def find_slots(day: str, count: int = 2) -> list[str]:
    """Free appointment slots for a day."""
    return [f"{day} 10:00", f"{day} 12:00"][:count]


@tool("calendar.move_event", effect=Effect.EXTERNAL)
async def move_event(event_id: str, slot: str) -> dict[str, str]:
    """Move an appointment."""
    raise RuntimeError("calendar API unavailable")


@tool("slow.tool", timeout_s=0.05)
async def slow_tool() -> str:
    """Never finishes in time."""
    await asyncio.sleep(1)
    return "late"


def _registry() -> ToolRegistry:
    return ToolRegistry([find_slots, move_event, slow_tool])


def _calls(*calls: tuple[str, str, dict[str, object]]) -> Message:
    return Message(
        role=Role.ASSISTANT,
        content=[
            TextBlock(text="Let me check."),
            *[ToolUseBlock(id=i, name=n, input=a) for i, n, a in calls],
        ],
    )


async def _collect(
    session: Session, text: str, provider: FakeProvider, cfg: LoopConfig | None = None
) -> list[Event]:
    return [ev async for ev in run(session, text, provider, _registry(), cfg)]


async def test_text_only_turn(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="receptionist")
    provider = FakeProvider([Message.assistant("Your appointment is confirmed.")])
    events = await _collect(session, "1", provider)

    assert isinstance(events[-1], TurnEnded) and events[-1].reason == "end_turn"
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == (
        "Your appointment is confirmed."
    )
    assert [m.role for m in session.messages] == [Role.USER, Role.ASSISTANT]
    assert [e.seq for e in events] == list(range(len(events)))
    assert all(e.scope == scope and e.session_id == session.id for e in events)


async def test_parallel_tools_return_structured_observations(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="receptionist")
    provider = FakeProvider(
        [
            _calls(
                ("t1", "calendar.find_slots", {"day": "2026-10-05"}),
                ("t2", "calendar.move_event", {"event_id": "e1", "slot": "x"}),
                ("t3", "slow.tool", {}),
                ("t4", "nope.tool", {}),
                ("t5", "calendar.find_slots", {"count": 1}),  # missing required 'day'
            ),
            Message.assistant("Here are two free slots."),
        ]
    )
    events = await _collect(session, "Can I move my appointment?", provider)

    started = [e.name for e in events if isinstance(e, ToolCallStarted)]
    assert len(started) == 5
    status = {e.tool_use_id: e.status for e in events if isinstance(e, ToolCallFinished)}
    assert status == {
        "t1": ToolStatus.OK,
        "t2": ToolStatus.ERROR,
        "t3": ToolStatus.TIMEOUT,
        "t4": ToolStatus.ERROR,
        "t5": ToolStatus.ERROR,
    }
    results = session.messages[2].content
    assert all(isinstance(r, ToolResultBlock) for r in results)
    ok = next(r for r in results if isinstance(r, ToolResultBlock) and r.tool_use_id == "t1")
    assert ok.content == ["2026-10-05 10:00", "2026-10-05 12:00"]
    # the model saw the tool results on its second call
    assert len(provider.requests) == 2
    assert provider.requests[1].messages[-1].role is Role.USER
    assert {t["name"] for t in provider.requests[0].tools} == {
        "calendar.find_slots",
        "calendar.move_event",
        "slow.tool",
    }
    assert isinstance(events[-1], TurnEnded) and events[-1].turns == 2


async def test_max_turns_stops_a_looping_model(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="receptionist")
    looping = [_calls((f"t{i}", "calendar.find_slots", {"day": f"d{i}"})) for i in range(5)]
    events = await _collect(session, "loop", FakeProvider(looping), LoopConfig(max_turns=3))
    end = events[-1]
    assert isinstance(end, TurnEnded) and end.reason == "max_turns" and end.turns == 3


async def test_the_same_call_a_fourth_time_stops_the_run(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="receptionist")
    looping = [_calls((f"t{i}", "calendar.find_slots", {"day": "d"})) for i in range(5)]
    events = await _collect(session, "loop", FakeProvider(looping))
    assert isinstance(events[-1], TurnEnded) and events[-1].reason == "stuck"
    assert events[-1].turns == 4  # three calls ran; the fourth identical one did not
    assert isinstance(events[-2], ErrorEvent) and "called 4 times" in events[-2].message
    assert sum(isinstance(e, ToolCallFinished) for e in events) == 3
    last = session.messages[-1].content[0]  # the history stays valid
    assert isinstance(last, ToolResultBlock) and "repeated" in (last.error or "")


async def test_turns_where_every_tool_fails_stop_the_run(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="receptionist")
    failing = [_calls((f"t{i}", "calendar.move_event", {"event_id": f"e{i}", "slot": "s"}))
               for i in range(5)]  # fmt: skip
    events = await _collect(session, "move it", FakeProvider(failing))
    assert isinstance(events[-1], TurnEnded) and events[-1].reason == "stuck" and (
        events[-1].turns == 3)  # fmt: skip
    assert "failed 3 turns in a row" in events[-2].message  # type: ignore[attr-defined]


async def test_a_reply_has_a_time_limit(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="receptionist")
    slow = [_calls(("t1", "slow.tool", {})), Message.assistant("late")]
    events = await _collect(session, "go", FakeProvider(slow), LoopConfig(max_seconds=0.01))
    assert isinstance(events[-1], TurnEnded) and events[-1].reason == "timeout"
    assert "no answer within 0.01 s" in events[-2].message  # type: ignore[attr-defined]


async def test_provider_failure_is_an_event_not_a_crash(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="receptionist")
    events = await _collect(session, "hi", FakeProvider([ConnectionError("upstream down")]))
    assert isinstance(events[-2], ErrorEvent) and "upstream down" in events[-2].message
    assert isinstance(events[-1], TurnEnded) and events[-1].reason == "error"


async def test_usage_is_accumulated(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="receptionist")
    provider = FakeProvider(
        [
            _calls(("t1", "calendar.find_slots", {"day": "d"})),
            Message.assistant("done"),
        ]
    )
    events = await _collect(session, "hi", provider)
    end = events[-1]
    assert isinstance(end, TurnEnded)
    assert end.usage.input_tokens > 0 and end.usage.output_tokens > 0


def test_tool_schema_comes_from_type_hints() -> None:
    schema = find_slots.schema()
    assert schema["description"] == "Free appointment slots for a day."
    props = schema["input_schema"]["properties"]
    assert set(props) == {"day", "count"}
    assert schema["input_schema"]["required"] == ["day"]


def test_sync_functions_are_rejected() -> None:
    with pytest.raises(TypeError):
        tool("bad.tool")(lambda: None)  # type: ignore[arg-type]


def test_history_messages_are_recorded(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="a")
    ev = session.add_message(Message.user("hola"))
    assert isinstance(ev, MessageAdded) and ev.seq == 0 and session.next_seq == 1


# --- stop reasons and the tool gate (M1) -------------------------------------------

from dif_general_harness.core.loop import GateDecision  # noqa: E402
from dif_general_harness.core.messages import Usage  # noqa: E402
from dif_general_harness.providers.base import ProviderMessage  # noqa: E402


def _scripted(message: Message, stop: str) -> ProviderMessage:
    return ProviderMessage(message=message, usage=Usage(), stop_reason=stop, model="m")  # type: ignore[arg-type]


async def test_refusal_never_runs_tools_and_keeps_history_valid(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="a")
    turn = _calls(("t1", "calendar.move_event", {"event_id": "e", "slot": "s"}))
    events = await _collect(session, "x", FakeProvider([_scripted(turn, "refusal")]))
    assert not any(isinstance(e, ToolCallStarted) for e in events)
    end = events[-1]
    assert isinstance(end, TurnEnded) and end.reason == "refusal"
    results = session.messages[-1].content
    assert [r.status for r in results if isinstance(r, ToolResultBlock)] == [ToolStatus.ERROR]


async def test_truncated_tool_call_is_not_executed(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="a")
    turn = _calls(("t1", "calendar.find_slots", {"day": "d"}))
    events = await _collect(session, "x", FakeProvider([_scripted(turn, "max_tokens")]))
    assert not any(isinstance(e, ToolCallStarted) for e in events)
    assert isinstance(events[-1], TurnEnded) and events[-1].reason == "max_tokens"
    assert any(isinstance(e, ErrorEvent) and "truncated" in e.message for e in events)


async def test_pause_turn_resumes(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="a")
    provider = FakeProvider(
        [_scripted(Message.assistant("working..."), "pause_turn"), Message.assistant("done")]
    )
    events = await _collect(session, "x", provider)
    assert isinstance(events[-1], TurnEnded) and events[-1].reason == "end_turn"
    assert len(provider.requests) == 2


async def test_empty_assistant_turn_is_not_recorded(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="a")
    empty = Message(role=Role.ASSISTANT, content=[])
    await _collect(session, "x", FakeProvider([_scripted(empty, "refusal")]))
    assert [m.role for m in session.messages] == [Role.USER]


class DenyExternal:
    async def check(self, session: Session, call: ToolUseBlock) -> GateDecision:
        if call.name == "calendar.move_event":
            return GateDecision(False, "needs front-desk approval")
        return GateDecision(True)


class BrokenGate:
    async def check(self, session: Session, call: ToolUseBlock) -> GateDecision:
        raise RuntimeError("policy store unreachable")


@pytest.mark.parametrize(
    ("gate", "expected"),
    [
        (DenyExternal(), {"t1": ToolStatus.OK, "t2": ToolStatus.DENIED}),
        (BrokenGate(), {"t1": ToolStatus.DENIED, "t2": ToolStatus.DENIED}),
    ],
)
async def test_gate_decides_each_call(
    scope: Scope, gate: object, expected: dict[str, ToolStatus]
) -> None:
    session = Session(scope=scope, agent_id="a")
    provider = FakeProvider(
        [
            _calls(
                ("t1", "calendar.find_slots", {"day": "d"}),
                ("t2", "calendar.move_event", {"event_id": "e", "slot": "s"}),
            ),
            Message.assistant("ok"),
        ]
    )
    events = [ev async for ev in run(session, "x", provider, _registry(), gate=gate)]  # type: ignore[arg-type]
    status = {e.tool_use_id: e.status for e in events if isinstance(e, ToolCallFinished)}
    assert status == expected
    denied = [
        r
        for r in session.messages[2].content
        if isinstance(r, ToolResultBlock) and r.status is ToolStatus.DENIED
    ]
    assert all(r.error for r in denied)


async def test_invalid_provider_arguments_are_never_executed(scope: Scope) -> None:
    session = Session(scope=scope, agent_id="a")
    bad = Message(
        role=Role.ASSISTANT,
        content=[
            ToolUseBlock(id="t1", name="calendar.move_event", input_error="invalid tool arguments")
        ],
    )
    provider = FakeProvider([_scripted(bad, "tool_use"), Message.assistant("sorry")])
    events = await _collect(session, "x", provider)
    status = {e.tool_use_id: e.status for e in events if isinstance(e, ToolCallFinished)}
    assert status == {"t1": ToolStatus.ERROR}
