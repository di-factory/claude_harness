"""Events: everything the loop emits (ARCHITECTURE §3.1).

Consoles, channels, logs and stores all consume the same stream. Each event
carries the session's scope so any record can be attributed to a tenant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from .messages import Message, ToolStatus, Usage
from .scope import Scope


def _now() -> datetime:
    return datetime.now(UTC)


class _EventBase(BaseModel):
    scope: Scope
    session_id: str
    seq: int = 0
    ts: datetime = Field(default_factory=_now)


class SessionStarted(_EventBase):
    type: Literal["session_started"] = "session_started"
    agent_id: str
    contact_key: str | None = None
    config_version: str | None = None


class MessageAdded(_EventBase):
    """A message became part of the session history (the replayable record)."""

    type: Literal["message_added"] = "message_added"
    message: Message


class TextDelta(_EventBase):
    """Streaming text for live surfaces. Not needed for replay."""

    type: Literal["text_delta"] = "text_delta"
    text: str


class ToolCallStarted(_EventBase):
    type: Literal["tool_call_started"] = "tool_call_started"
    tool_use_id: str
    name: str
    input: dict[str, Any]


class ToolCallFinished(_EventBase):
    type: Literal["tool_call_finished"] = "tool_call_finished"
    tool_use_id: str
    name: str
    status: ToolStatus
    duration_ms: int


class TurnEnded(_EventBase):
    type: Literal["turn_ended"] = "turn_ended"
    reason: Literal[
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
    turns: int
    usage: Usage


class ContextCompacted(_EventBase):
    """The oldest ``replaced`` messages were summarised into ``summary`` (the session's
    history is now the summary followed by the rest). Replaying applies it the same way."""

    type: Literal["context_compacted"] = "context_compacted"
    summary: str
    replaced: int
    tokens_before: int
    tokens_after: int


class ErrorEvent(_EventBase):
    type: Literal["error"] = "error"
    message: str


Event = Annotated[
    SessionStarted
    | MessageAdded
    | TextDelta
    | ToolCallStarted
    | ToolCallFinished
    | TurnEnded
    | ContextCompacted
    | ErrorEvent,
    Field(discriminator="type"),
]

EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)
