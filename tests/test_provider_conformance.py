"""Provider conformance: every adapter turns its vendor's wire format into the same neutral
result. The real SDKs parse recorded SSE streams through a mock HTTP transport, offline."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx2
import openai
import pytest

from dif_general_harness.core.messages import (
    Message,
    Role,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolStatus,
    ToolUseBlock,
)
from dif_general_harness.providers.anthropic import AnthropicProvider
from dif_general_harness.providers.base import (
    ModelProvider,
    ModelRequest,
    ProviderMessage,
    ProviderTextDelta,
)
from dif_general_harness.providers.openai_compat import OpenAICompatibleProvider

TOOLS = [
    {
        "name": "calendar.find_slots",
        "description": "Free slots",
        "input_schema": {
            "type": "object",
            "properties": {"day": {"type": "string"}},
            "required": ["day"],
        },
    }
]


# --- wire-format builders -----------------------------------------------------------


def _sse(events: list[tuple[str | None, dict[str, Any] | str]]) -> bytes:
    out = []
    for name, data in events:
        body = data if isinstance(data, str) else json.dumps(data)
        out.append((f"event: {name}\n" if name else "") + f"data: {body}\n\n")
    return "".join(out).encode()


def anthropic_stream(
    blocks: list[dict[str, Any]], stop: str, *, model: str = "claude-opus-5-5"
) -> bytes:
    ev: list[tuple[str | None, dict[str, Any] | str]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 40,
                        "cache_creation_input_tokens": 0,
                    },
                },
            },
        ),
    ]
    for i, b in enumerate(blocks):
        if b["type"] == "text":
            ev.append(
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": i,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
            ev.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": i,
                        "delta": {"type": "text_delta", "text": b["text"]},
                    },
                )
            )
        elif b["type"] == "thinking":
            ev.append(
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": i,
                        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                    },
                )
            )
            ev.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": i,
                        "delta": {"type": "thinking_delta", "thinking": b["thinking"]},
                    },
                )
            )
            ev.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": i,
                        "delta": {"type": "signature_delta", "signature": b["signature"]},
                    },
                )
            )
        elif b["type"] == "tool_use":
            ev.append(
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": i,
                        "content_block": {
                            "type": "tool_use",
                            "id": b["id"],
                            "name": b["name"],
                            "input": {},
                        },
                    },
                )
            )
            ev.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": i,
                        "delta": {"type": "input_json_delta", "partial_json": b["json"]},
                    },
                )
            )
        ev.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    ev.append(
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 25},
            },
        )
    )
    ev.append(("message_stop", {"type": "message_stop"}))
    return _sse(ev)


def openai_stream(
    text: str | None, calls: list[tuple[str, str, str]], finish: str, *, model: str = "gpt-x"
) -> bytes:
    def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
        return {
            "id": "c1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    ev: list[tuple[str | None, dict[str, Any] | str]] = [(None, chunk({"role": "assistant"}))]
    if text:
        ev.append((None, chunk({"content": text})))
    for i, (cid, name, args) in enumerate(calls):
        half = len(args) // 2
        ev.append(
            (
                None,
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": i,
                                "id": cid,
                                "type": "function",
                                "function": {"name": name, "arguments": args[:half]},
                            }
                        ]
                    }
                ),
            )
        )
        ev.append(
            (None, chunk({"tool_calls": [{"index": i, "function": {"arguments": args[half:]}}]}))
        )
    ev.append((None, chunk({}, finish)))
    ev.append(
        (
            None,
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 25,
                    "total_tokens": 125,
                    "prompt_tokens_details": {"cached_tokens": 40},
                },
            },
        )
    )
    ev.append((None, "[DONE]"))
    return _sse(ev)


# --- harness -------------------------------------------------------------------------


@dataclass
class Wire:
    bodies: list[bytes]
    requests: list[dict[str, Any]] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)

    def transport(self) -> httpx2.MockTransport:
        def handle(req: httpx2.Request) -> httpx2.Response:
            self.requests.append(json.loads(req.content))
            self.paths.append(str(req.url))
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, content=self.bodies.pop(0)
            )

        return httpx2.MockTransport(handle)


def make_anthropic(wire: Wire, **kw: Any) -> ModelProvider:
    client = anthropic.AsyncAnthropic(
        api_key="test", http_client=anthropic.DefaultAsyncHttpxClient(transport=wire.transport())
    )
    return AnthropicProvider(kw.pop("model", "claude-opus-5-5"), client=client, **kw)


def make_openai(wire: Wire, **kw: Any) -> ModelProvider:
    client = openai.AsyncOpenAI(
        api_key="test",
        base_url="http://llm.test/v1",
        http_client=openai.DefaultAsyncHttpxClient(transport=wire.transport()),
    )
    return OpenAICompatibleProvider(kw.pop("model", "gpt-x"), client=client, **kw)


async def call(provider: ModelProvider, request: ModelRequest) -> tuple[str, ProviderMessage]:
    text, final = "", None
    async for ev in provider.stream(request):
        if isinstance(ev, ProviderTextDelta):
            text += ev.text
        else:
            final = ev
    assert final is not None
    return text, final


def req(*messages: Message) -> ModelRequest:
    return ModelRequest(system="Be brief.", messages=list(messages), tools=TOOLS)


# One scenario, two vendor encodings.
Scenario = Callable[[str], bytes]
SCENARIOS: dict[str, dict[str, Scenario]] = {
    "text": {
        "anthropic": lambda _: anthropic_stream(
            [{"type": "text", "text": "Hola Ana."}], "end_turn"
        ),
        "openai": lambda _: openai_stream("Hola Ana.", [], "stop"),
    },
    "tool_call": {
        "anthropic": lambda _: anthropic_stream(
            [
                {"type": "text", "text": "Checking."},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "calendar__find_slots",
                    "json": '{"day": "2026-10-05"}',
                },
            ],
            "tool_use",
        ),
        "openai": lambda _: openai_stream(
            "Checking.", [("t1", "calendar__find_slots", '{"day": "2026-10-05"}')], "tool_calls"
        ),
    },
    "max_tokens": {
        "anthropic": lambda _: anthropic_stream([{"type": "text", "text": "Long"}], "max_tokens"),
        "openai": lambda _: openai_stream("Long", [], "length"),
    },
    "refusal": {
        "anthropic": lambda _: anthropic_stream([], "refusal"),
        "openai": lambda _: openai_stream(None, [], "content_filter"),
    },
}
FACTORIES = {"anthropic": make_anthropic, "openai": make_openai}


@pytest.mark.parametrize("vendor", ["anthropic", "openai"])
async def test_text_turn(vendor: str) -> None:
    wire = Wire([SCENARIOS["text"][vendor](vendor)])
    text, final = await call(FACTORIES[vendor](wire), req(Message.user("hola")))
    assert text == "Hola Ana."
    assert final.message.text() == "Hola Ana."
    assert final.stop_reason == "end_turn"
    assert (final.usage.input_tokens, final.usage.output_tokens) != (0, 0)
    assert final.usage.cache_read_tokens == 40


@pytest.mark.parametrize("vendor", ["anthropic", "openai"])
async def test_tool_call_names_round_trip(vendor: str) -> None:
    wire = Wire([SCENARIOS["tool_call"][vendor](vendor)])
    _, final = await call(FACTORIES[vendor](wire), req(Message.user("slots?")))
    assert final.stop_reason == "tool_use"
    (use,) = final.message.tool_uses()
    assert use.name == "calendar.find_slots"  # decoded from the wire name
    assert use.input == {"day": "2026-10-05"}
    assert use.input_error is None
    sent = json.dumps(wire.requests[0])
    assert "calendar__find_slots" in sent and "calendar.find_slots" not in sent


@pytest.mark.parametrize("vendor", ["anthropic", "openai"])
@pytest.mark.parametrize(
    ("scenario", "stop"), [("max_tokens", "max_tokens"), ("refusal", "refusal")]
)
async def test_stop_reasons(vendor: str, scenario: str, stop: str) -> None:
    wire = Wire([SCENARIOS[scenario][vendor](vendor)])
    _, final = await call(FACTORIES[vendor](wire), req(Message.user("x")))
    assert final.stop_reason == stop


@pytest.mark.parametrize("vendor", ["anthropic", "openai"])
async def test_history_translation(vendor: str) -> None:
    """A tool-call turn plus its results is sent back in each vendor's format."""
    history = [
        Message.user("slots?"),
        Message(
            role=Role.ASSISTANT,
            content=[
                ThinkingBlock(text="plan", provider="anthropic", signature="sig-1"),
                TextBlock(text="Checking."),
                ToolUseBlock(id="t1", name="calendar.find_slots", input={"day": "d"}),
                ToolUseBlock(id="t2", name="calendar.find_slots", input={"day": "e"}),
            ],
        ),
        Message(
            role=Role.USER,
            content=[
                ToolResultBlock(tool_use_id="t1", status=ToolStatus.OK, content=["10:00"]),
                ToolResultBlock(tool_use_id="t2", status=ToolStatus.DENIED, error="needs approval"),
            ],
        ),
    ]
    wire = Wire([SCENARIOS["text"][vendor](vendor)])
    await call(FACTORIES[vendor](wire), req(*history))
    sent = wire.requests[0]
    if vendor == "anthropic":
        msgs = sent["messages"]
        assert msgs[1]["content"][0] == {
            "type": "thinking",
            "thinking": "plan",
            "signature": "sig-1",
        }
        results = msgs[2]["content"]
        assert results[0] == {"type": "tool_result", "tool_use_id": "t1", "content": '["10:00"]'}
        assert results[1]["is_error"] is True and "needs approval" in results[1]["content"]
        assert sent["system"] == "Be brief."
        assert sent["tools"][0]["eager_input_streaming"] is True
    else:
        msgs = sent["messages"]
        assert msgs[0] == {"role": "system", "content": "Be brief."}
        assert [c["id"] for c in msgs[2]["tool_calls"]] == ["t1", "t2"]
        assert msgs[3] == {"role": "tool", "tool_call_id": "t1", "content": '["10:00"]'}
        assert msgs[4]["role"] == "tool" and "needs approval" in msgs[4]["content"]
        assert "plan" not in json.dumps(msgs)  # other providers' reasoning is never sent


async def test_anthropic_thinking_is_kept_for_replay() -> None:
    wire = Wire(
        [
            anthropic_stream(
                [
                    {"type": "thinking", "thinking": "hmm", "signature": "s9"},
                    {"type": "text", "text": "ok"},
                ],
                "end_turn",
            )
        ]
    )
    _, final = await call(make_anthropic(wire), req(Message.user("x")))
    thinking = final.message.content[0]
    assert isinstance(thinking, ThinkingBlock)
    assert (thinking.provider, thinking.signature, thinking.text) == ("anthropic", "s9", "hmm")


async def test_anthropic_fallbacks_on_by_default_for_supported_models() -> None:
    wire = Wire(
        [
            anthropic_stream([{"type": "text", "text": "hi"}], "end_turn"),
            anthropic_stream([{"type": "text", "text": "hi"}], "end_turn"),
        ]
    )
    await call(make_anthropic(wire), req(Message.user("x")))
    assert wire.requests[0]["fallbacks"] == "default"
    await call(make_anthropic(wire, model="claude-haiku-4-5"), req(Message.user("x")))
    assert "fallbacks" not in wire.requests[1]


async def test_anthropic_effort_and_cache() -> None:
    wire = Wire([anthropic_stream([{"type": "text", "text": "hi"}], "end_turn")])
    await call(make_anthropic(wire, effort="low"), req(Message.user("x")))
    assert wire.requests[0]["output_config"] == {"effort": "low"}
    assert wire.requests[0]["cache_control"] == {"type": "ephemeral"}


async def test_openai_malformed_arguments_are_marked_not_run() -> None:
    wire = Wire([openai_stream(None, [("t1", "calendar__find_slots", '{"day": ')], "tool_calls")])
    _, final = await call(make_openai(wire), req(Message.user("x")))
    (use,) = final.message.tool_uses()
    assert use.input_error is not None and "invalid tool arguments" in use.input_error


async def test_anthropic_mid_stream_fallback_drops_declined_blocks() -> None:
    """Blocks the declined attempt produced before the fallback marker must never run or be
    echoed back; its text is kept; the served-by model is reported."""
    ev: list[tuple[str | None, dict[str, Any] | str]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "m",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-opus-5-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "old"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": "Partial "},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {
                    "type": "tool_use",
                    "id": "declined",
                    "name": "calendar__find_slots",
                    "input": {},
                },
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"day": "x"}'},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 2}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 3,
                "content_block": {
                    "type": "fallback",
                    "from": {"model": "claude-opus-5-5"},
                    "to": {"model": "claude-opus-4-8"},
                },
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 3}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 4,
                "content_block": {
                    "type": "tool_use",
                    "id": "kept",
                    "name": "calendar__find_slots",
                    "input": {},
                },
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 4,
                "delta": {"type": "input_json_delta", "partial_json": '{"day": "y"}'},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 4}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 9},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    wire = Wire([_sse(ev)])
    _, final = await call(make_anthropic(wire), req(Message.user("x")))
    assert [u.id for u in final.message.tool_uses()] == ["kept"]
    assert not any(isinstance(b, ThinkingBlock) for b in final.message.content)
    assert final.message.text() == "Partial "
    assert "beta=true" in wire.paths[0]
