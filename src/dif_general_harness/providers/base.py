"""The provider protocol. The core never sees a vendor SDK type."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ..core.messages import Message, Usage


@dataclass(frozen=True)
class ModelRequest:
    system: str
    messages: list[Message]
    tools: list[dict[str, Any]] = field(default_factory=list)
    model_role: str = "main"
    max_tokens: int | None = None  # None: the provider's own default


@dataclass(frozen=True)
class ProviderTextDelta:
    text: str


StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "pause_turn", "other"]


@dataclass(frozen=True)
class ProviderMessage:
    """The final assistant message of one model call."""

    message: Message
    usage: Usage
    stop_reason: StopReason = "end_turn"
    model: str | None = None  # the model that actually served it (fallbacks can change it)
    refusal_category: str | None = None


def wire_name(name: str) -> str:
    """Provider APIs forbid dots in tool names: ``cal.find`` -> ``cal__find``."""
    return name.replace(".", "__")


def tool_name_map(tools: list[dict[str, Any]]) -> dict[str, str]:
    """Wire name -> registry name, for decoding tool calls back."""
    return {wire_name(t["name"]): t["name"] for t in tools}


ProviderEvent = ProviderTextDelta | ProviderMessage


class ModelProvider(Protocol):
    """Streams deltas and ends with exactly one ProviderMessage."""

    name: str

    def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]: ...
