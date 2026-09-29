"""The provider protocol. The core never sees a vendor SDK type."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..core.messages import Message, Usage


@dataclass(frozen=True)
class ModelRequest:
    system: str
    messages: list[Message]
    tools: list[dict[str, Any]] = field(default_factory=list)
    model_role: str = "main"
    max_tokens: int = 4096


@dataclass(frozen=True)
class ProviderTextDelta:
    text: str


@dataclass(frozen=True)
class ProviderMessage:
    """The final assistant message of one model call."""

    message: Message
    usage: Usage


ProviderEvent = ProviderTextDelta | ProviderMessage


class ModelProvider(Protocol):
    """Streams deltas and ends with exactly one ProviderMessage."""

    name: str

    def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]: ...
