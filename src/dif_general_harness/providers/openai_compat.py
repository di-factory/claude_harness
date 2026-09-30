"""OpenAI-compatible adapter (``openai`` SDK, Chat Completions) for OpenAI, vLLM, Ollama and
gateways. Chat Completions is used because it is what compatible servers implement.

SDK types never leave this module. Reasoning blocks from other providers are not sent;
tool results become ``tool`` role messages; malformed tool-call JSON is marked with
``input_error`` so the loop reports it instead of executing the call.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import openai

from ..core.messages import (
    MediaBlock,
    Message,
    Role,
    TextBlock,
    ToolResultBlock,
    ToolStatus,
    ToolUseBlock,
    Usage,
)
from .base import (
    Embeddings,
    ModelRequest,
    ProviderEvent,
    ProviderMessage,
    ProviderTextDelta,
    StopReason,
    tool_name_map,
    wire_name,
)

PROVIDER = "openai-compatible"
_STOP: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    for msg in messages:
        text = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
        if msg.role is Role.ASSISTANT:
            calls = [
                {
                    "id": b.id,
                    "type": "function",
                    "function": {"name": wire_name(b.name), "arguments": json.dumps(b.input)},
                }
                for b in msg.content
                if isinstance(b, ToolUseBlock)
            ]
            entry: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
            continue
        # user turn: tool results first (they must directly follow the assistant tool calls)
        for b in msg.content:
            if isinstance(b, ToolResultBlock):
                out.append({"role": "tool", "tool_call_id": b.tool_use_id, "content": _result(b)})
        media = [b for b in msg.content if isinstance(b, MediaBlock)]
        if media:
            parts: list[dict[str, Any]] = [_media_part(b) for b in media]
            if text:
                parts.append({"type": "text", "text": text})
            out.append({"role": "user", "content": parts})
        elif text:
            out.append({"role": "user", "content": text})
    return out


def _media_part(b: MediaBlock) -> dict[str, Any]:
    url = f"data:{b.media_type};base64,{b.data}"
    if b.media_type == "application/pdf":
        return {"type": "file", "file": {"filename": "document.pdf", "file_data": url}}
    return {"type": "image_url", "image_url": {"url": url}}


def _result(b: ToolResultBlock) -> str:
    if b.status is ToolStatus.OK:
        return b.content if isinstance(b.content, str) else json.dumps(b.content, default=str)
    return f"[{b.status.value}] {b.error or ''}".strip()


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": wire_name(t["name"]),
                "description": t.get("description", ""),
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


class OpenAICompatibleProvider:
    name = PROVIDER

    def __init__(
        self,
        model: str,
        *,
        client: openai.AsyncOpenAI | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        effort: str | None = None,
        max_tokens: int = 16000,
    ) -> None:
        self.model = model
        self.client = client or openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.effort = effort
        self.max_tokens = max_tokens

    async def embed(self, texts: list[str], *, model_role: str = "embedding") -> Embeddings:
        response = await self.client.embeddings.create(model=self.model, input=texts)
        ordered = sorted(response.data, key=lambda d: d.index)
        usage = getattr(response, "usage", None)
        return Embeddings(
            [list(d.embedding) for d in ordered],
            str(getattr(response, "model", None) or self.model),
            int(getattr(usage, "prompt_tokens", 0) or 0),
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]:
        names = tool_name_map(request.tools)
        params: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(request.system, request.messages),
            "max_completion_tokens": request.max_tokens or self.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            params["tools"] = to_openai_tools(request.tools)
        if self.effort:
            params["reasoning_effort"] = self.effort

        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish: str | None = None
        usage = Usage()
        served_by: str | None = None
        stream = await self.client.chat.completions.create(**params)
        async for chunk in stream:
            served_by = getattr(chunk, "model", None) or served_by
            if getattr(chunk, "usage", None):
                u = chunk.usage
                details = getattr(u, "prompt_tokens_details", None)
                cached = getattr(details, "cached_tokens", None) or 0
                usage = Usage(
                    input_tokens=(u.prompt_tokens or 0) - cached,
                    output_tokens=u.completion_tokens or 0,
                    cache_read_tokens=cached,
                )
            for choice in chunk.choices or []:
                delta = choice.delta
                if delta.content:
                    text_parts.append(delta.content)
                    yield ProviderTextDelta(delta.content)
                for tc in delta.tool_calls or []:
                    slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["arguments"] += tc.function.arguments
                if choice.finish_reason:
                    finish = choice.finish_reason

        blocks: list[Any] = []
        if text_parts:
            blocks.append(TextBlock(text="".join(text_parts)))
        for index in sorted(calls):
            slot = calls[index]
            name = names.get(slot["name"], slot["name"])
            call_id = slot["id"] or f"call_{index}"
            try:
                args = json.loads(slot["arguments"] or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments are not a JSON object")
                blocks.append(ToolUseBlock(id=call_id, name=name, input=args))
            except ValueError as exc:
                blocks.append(
                    ToolUseBlock(
                        id=call_id, name=name, input_error=f"invalid tool arguments: {exc}"
                    )
                )
        yield ProviderMessage(
            message=Message(role=Role.ASSISTANT, content=blocks),
            usage=usage,
            stop_reason=_STOP.get(finish or "stop", "other"),
            model=served_by,
        )
