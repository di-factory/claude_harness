"""Anthropic adapter (official ``anthropic`` SDK). SDK types never leave this module.

Behaviour that matters for correctness:
- Thinking blocks (with signatures) are echoed back unchanged to Anthropic, never to others.
- Stop reasons are surfaced so the loop never runs tools from a truncated or refused turn.
- Server-side refusal fallbacks (``fallbacks: "default"``) are on by default for models
  that support them on the direct Claude API. Blocks the declined attempt produced before
  the last ``fallback`` marker (thinking, tool calls) are dropped here, so they are neither
  executed nor echoed back; its text is kept.
- Tool names are encoded (dots are not allowed on the wire) and decoded on the way back.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from ..core.messages import (
    MediaBlock,
    Message,
    Role,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolStatus,
    ToolUseBlock,
    Usage,
)
from .base import (
    ContextOverflow,
    ModelRequest,
    ProviderEvent,
    ProviderMessage,
    ProviderTextDelta,
    StopReason,
    is_overflow,
    tool_name_map,
    wire_name,
)

PROVIDER = "anthropic"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept `fallbacks: "default"` on the direct Claude API.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_STOP: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
    "pause_turn": "pause_turn",
}


def to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        blocks: list[dict[str, Any]] = []
        for b in msg.content:
            if isinstance(b, TextBlock):
                if b.text:  # the API rejects empty text blocks
                    blocks.append({"type": "text", "text": b.text})
            elif isinstance(b, ThinkingBlock):
                if b.provider != PROVIDER:
                    continue  # another provider's reasoning cannot be replayed here
                if b.redacted_data is not None:
                    blocks.append({"type": "redacted_thinking", "data": b.redacted_data})
                else:
                    blocks.append(
                        {"type": "thinking", "thinking": b.text, "signature": b.signature or ""}
                    )
            elif isinstance(b, ToolUseBlock):
                blocks.append(
                    {"type": "tool_use", "id": b.id, "name": wire_name(b.name), "input": b.input}
                )
            elif isinstance(b, ToolResultBlock):
                blocks.append(_tool_result(b))
            elif isinstance(b, MediaBlock):
                kind = "document" if b.media_type == "application/pdf" else "image"
                source = {"type": "base64", "media_type": b.media_type, "data": b.data}
                blocks.append({"type": kind, "source": source})
        if blocks:
            out.append({"role": msg.role.value, "content": blocks})
    return out


def _tool_result(b: ToolResultBlock) -> dict[str, Any]:
    if b.status is ToolStatus.OK:
        body = b.content if isinstance(b.content, str) else json.dumps(b.content, default=str)
        return {"type": "tool_result", "tool_use_id": b.tool_use_id, "content": body}
    text = b.error_text()
    return {"type": "tool_result", "tool_use_id": b.tool_use_id, "content": text, "is_error": True}


def to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": wire_name(t["name"]),
            "description": t.get("description", ""),
            "input_schema": t["input_schema"],
            # stream large tool inputs as generated; inputs are validated before execution
            "eager_input_streaming": True,
        }
        for t in tools
    ]


def from_anthropic_content(content: list[Any], names: dict[str, str]) -> list[Any]:
    last_fallback = max(
        (i for i, b in enumerate(content) if getattr(b, "type", None) == "fallback"), default=-1
    )
    blocks: list[Any] = []
    for i, b in enumerate(content):
        kind = getattr(b, "type", None)
        declined = i < last_fallback  # produced by an attempt that was then declined
        if kind == "text":
            blocks.append(TextBlock(text=b.text))
        elif kind == "thinking" and not declined:
            blocks.append(
                ThinkingBlock(text=b.thinking or "", provider=PROVIDER, signature=b.signature)
            )
        elif kind == "redacted_thinking" and not declined:
            blocks.append(ThinkingBlock(provider=PROVIDER, redacted_data=b.data))
        elif kind == "tool_use" and not declined:
            name = names.get(b.name, b.name)
            if isinstance(b.input, dict):
                blocks.append(ToolUseBlock(id=b.id, name=name, input=b.input))
            else:
                blocks.append(
                    ToolUseBlock(id=b.id, name=name, input_error="tool input was not an object")
                )
    return blocks


def _usage(u: Any) -> Usage:
    return Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )


WORKSPACE_HINT = (
    "this Anthropic key is not tied to a workspace. Create a key inside a workspace at"
    " console.anthropic.com (API Keys), or set models.providers.anthropic.workspace_id"
)


class ProviderSetupError(RuntimeError):
    """The provider refused the credentials or settings; the message says how to fix them."""


class AnthropicProvider:
    """Streams one Messages API call per loop turn."""

    name = PROVIDER

    def __init__(
        self,
        model: str,
        *,
        client: anthropic.AsyncAnthropic | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        effort: str | None = None,
        max_tokens: int = 64000,
        fallbacks: bool = True,
        prompt_cache: bool = True,
        max_json_retries: int = 2,
        workspace_id: str | None = None,
    ) -> None:
        self.model = model
        headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        self.client = client or anthropic.AsyncAnthropic(
            api_key=api_key, base_url=base_url, default_headers=headers
        )
        self.effort = effort
        self.max_tokens = max_tokens
        # server-side fallbacks exist only on the direct Claude API
        self.fallbacks = fallbacks and model in FALLBACK_MODELS and base_url is None
        self.prompt_cache = prompt_cache
        self.max_json_retries = max_json_retries

    def _params(self, request: ModelRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens or self.max_tokens,
            "messages": to_anthropic_messages(request.messages),
        }
        if request.system:
            params["system"] = request.system
        if request.tools:
            params["tools"] = to_anthropic_tools(request.tools)
        if self.effort:
            params["output_config"] = {"effort": self.effort}
        if self.prompt_cache:
            params["cache_control"] = {"type": "ephemeral"}
        return params

    async def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]:
        params = self._params(request)
        names = tool_name_map(request.tools)
        attempt = 0
        while True:
            try:
                if self.fallbacks:
                    ctx: Any = self.client.beta.messages.stream(
                        betas=[FALLBACK_BETA], fallbacks="default", **params
                    )
                else:
                    ctx = self.client.messages.stream(**params)
                async with ctx as stream:
                    async for event in stream:
                        if getattr(event, "type", None) == "text":
                            yield ProviderTextDelta(event.text)
                    final = await stream.get_final_message()
                break
            except ValueError:
                # tool-input JSON the SDK could not parse at all: re-issue the turn (bounded)
                attempt += 1
                if attempt > self.max_json_retries:
                    raise
            except anthropic.BadRequestError as exc:
                if "workspace" in str(exc).lower():
                    raise ProviderSetupError(WORKSPACE_HINT) from exc
                if is_overflow(str(exc)):
                    raise ContextOverflow(str(exc)) from exc
                raise
        details = getattr(final, "stop_details", None)
        yield ProviderMessage(
            message=Message(
                role=Role.ASSISTANT, content=from_anthropic_content(list(final.content), names)
            ),
            usage=_usage(final.usage),
            stop_reason=_STOP.get(str(final.stop_reason), "other"),
            model=final.model,
            refusal_category=getattr(details, "category", None) if details else None,
        )
