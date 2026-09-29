"""OpenTelemetry export (M4.2): GenAI-convention traces, no content, never in the way."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2

from dif_general_harness.core.messages import Message, Usage
from dif_general_harness.observability.otel import OtlpExporter, exporter_from_env
from dif_general_harness.providers.base import ProviderMessage
from tests.support import Env, calls, whatsapp


class Collector:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.posts: list[dict[str, Any]] = []
        self.raw: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response | None:
        if request.url.host != "collector.test":
            return None
        self.raw.append(request.content.decode())
        self.posts.append(json.loads(request.content))
        assert request.headers["x-api-key"] == "k1"
        return httpx2.Response(self.status)

    def spans(self) -> list[dict[str, Any]]:
        return [
            span
            for post in self.posts
            for rs in post["resourceSpans"]
            for ss in rs["scopeSpans"]
            for span in ss["spans"]
        ]


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    return {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}


def test_exporter_from_env() -> None:
    assert exporter_from_env({}) is None
    exporter = exporter_from_env({
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.example.com",
        "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer t, x-team = ops",
        "OTEL_SERVICE_NAME": "clinic",
    })  # fmt: skip
    assert exporter is not None and exporter.url == "https://otel.example.com/v1/traces"
    assert exporter.headers["authorization"] == "Bearer t" and exporter.headers["x-team"] == "ops"
    assert exporter.service == "clinic"


def _env(tmp_path: Path, collector: Collector) -> Env:
    script = [
        ProviderMessage(
            message=calls(
                ("n1", "notes.write", {"key": "ana", "text": "Ana Pérez +5215512345678"})
            ),
            usage=Usage(input_tokens=1000, output_tokens=40),
            stop_reason="tool_use",
            model="claude-opus-5-5",
        ),
        Message.assistant("Guardado, Ana."),
    ]
    env = Env(tmp_path, script, routes=collector)
    env.telemetry = OtlpExporter(
        "http://collector.test", headers={"x-api-key": "k1"}, client=env.outbound()
    )
    return env


async def test_a_run_is_one_trace_without_content(tmp_path: Path) -> None:
    collector = Collector()
    env = _env(tmp_path, collector)
    env.edit = lambda spec: spec.update(policies={"permissions": {"allow": ["notes.*"]}})
    inst, headless, client = await env.open()
    async with inst, client:
        body, headers = whatsapp("Guarda: Ana Pérez +5215512345678", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()

    [post] = collector.posts
    resource = {a["key"]: a["value"]["stringValue"]
                for a in post["resourceSpans"][0]["resource"]["attributes"]}  # fmt: skip
    assert resource["dif.tenant_id"] == "acme" and resource["service.name"] == "dif-general-harness"
    spans = {s["name"]: s for s in collector.spans()}
    root = spans["invoke_agent front"]
    assert "parentSpanId" not in root
    chat = spans["chat claude-opus-5-5"]
    tool = spans["execute_tool notes.write"]
    assert chat["parentSpanId"] == root["spanId"] == tool["parentSpanId"]
    assert len({s["traceId"] for s in collector.spans()}) == 1
    a = _attrs(chat)
    assert a["gen_ai.operation.name"] == "chat" and a["gen_ai.provider.name"] == "anthropic"
    assert a["gen_ai.usage.input_tokens"] == "1000" and a["dif.cost_usd"] > 0
    assert _attrs(tool)["dif.tool_status"] == "ok" and tool["status"]["code"] == 1
    assert _attrs(root)["dif.turn_reason"] == "end_turn"
    everything = collector.raw[0]
    for secret in ("Ana", "5512345678", "Guarda", "Guardado"):
        assert secret not in everything  # names, counts and costs only


async def test_a_collector_that_is_down_never_breaks_a_run(tmp_path: Path) -> None:
    collector = Collector(status=503)
    env = _env(tmp_path, collector)
    inst, headless, client = await env.open()
    async with inst, client:
        body, headers = whatsapp("Guarda mi nota", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        assert env.texts("twilio")[-1]["Body"] == "Guardado, Ana."
        assert env.telemetry.failures == 1
