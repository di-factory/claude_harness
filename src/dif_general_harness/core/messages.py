"""Provider-neutral conversation model (ARCHITECTURE §3.1)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ThinkingBlock(BaseModel):
    """Model reasoning. Opaque provider data (signature, redacted payload) is kept so the
    block can be sent back unchanged to the provider that produced it."""

    type: Literal["thinking"] = "thinking"
    text: str = ""
    provider: str | None = None
    signature: str | None = None
    redacted_data: str | None = None


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    # set when the provider returned arguments that could not be parsed; never executed
    input_error: str | None = None


class ToolStatus(StrEnum):
    """Every tool call ends in exactly one of these (ARCHITECTURE §3.4)."""

    OK = "ok"
    DENIED = "denied"
    ERROR = "error"
    TIMEOUT = "timeout"


SideEffects = Literal["none", "unknown", "committed"]


class ToolResultBlock(BaseModel):
    """A tool call's outcome. A failure says what the model needs to choose its next step:
    a ``reason`` code, whether trying again can work (``retryable``), whether anything
    changed in the world (``side_effects``: none, unknown or committed) and a ``hint``."""

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    status: ToolStatus
    content: Any = None
    error: str | None = None
    reason: str | None = None
    retryable: bool | None = None
    side_effects: SideEffects | None = None
    hint: str | None = None

    def error_text(self) -> str:
        """A failure as the model reads it."""
        lines = [f"[{self.status.value}] {self.error or ''}".strip()]
        facts = []
        if self.reason:
            facts.append(f"reason: {self.reason}")
        if self.retryable is not None:
            facts.append(f"retryable: {'yes' if self.retryable else 'no'}")
        if self.side_effects:
            facts.append(f"side effects: {self.side_effects}")
        if facts:
            lines.append(" · ".join(facts))
        if self.hint:
            lines.append(f"next: {self.hint}")
        return "\n".join(lines)


class MediaBlock(BaseModel):
    """An image or a PDF for a model to read (base64). Used for OCR requests; never stored
    in a session's history."""

    type: Literal["media"] = "media"
    media_type: str  # image/png, image/jpeg, image/gif, image/webp, application/pdf
    data: str


Block = Annotated[
    TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock | MediaBlock,
    Field(discriminator="type"),
]


class Message(BaseModel):
    role: Role
    content: list[Block]

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role=Role.USER, content=[TextBlock(text=text)])

    @classmethod
    def assistant(cls, text: str) -> Message:
        return cls(role=Role.ASSISTANT, content=[TextBlock(text=text)])

    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )
