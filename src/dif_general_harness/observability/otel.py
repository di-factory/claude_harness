"""OpenTelemetry traces (ARCHITECTURE §3.8), opt-in, in the GenAI semantic conventions.

Set the standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` (and optionally
``OTEL_EXPORTER_OTLP_HEADERS``, ``OTEL_SERVICE_NAME``) and every agent run becomes a trace
sent over OTLP/HTTP (JSON) to Langfuse, Phoenix, Grafana Tempo or any collector:

- ``invoke_agent <agent>``: the run (``gen_ai.agent.name``, ``gen_ai.conversation.id``, the
  tenant and instance, how the turn ended and what it cost);
- ``chat <model>``: each model call (``gen_ai.provider.name``, ``gen_ai.request.model``,
  ``gen_ai.response.model``, ``gen_ai.usage.input_tokens`` / ``output_tokens``, the stop
  reason, the USD cost);
- ``execute_tool <tool>``: each tool call (``gen_ai.tool.name``, status and effect).

**No content leaves by default:** no messages, prompts, tool arguments or results; only
names, counts, timings and costs. Exporting never slows or breaks a run: spans are sent
after the run, and a collector that is down is logged and skipped.

This is a small, dependency-free exporter rather than the OpenTelemetry SDK; the wire format
is standard OTLP, so any backend reads it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import httpx2

from ..policy.budgets import DEFAULT_PRICES, cost_usd
from ..providers.base import ModelProvider, ModelRequest, ProviderEvent, ProviderMessage

log = logging.getLogger(__name__)
SCOPE_NAME = "dif_general_harness"


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_id: str | None
    start_ns: int
    end_ns: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    kind: int = 1  # INTERNAL; 3 = CLIENT for model calls

    def set(self, **attrs: Any) -> None:
        self.attributes.update({k.replace("__", "."): v for k, v in attrs.items()})


_current: ContextVar[Span | None] = ContextVar("dif_span", default=None)


def _value(v: Any) -> dict[str, Any]:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def _attrs(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _value(v)} for k, v in attrs.items() if v is not None]


class OtlpExporter:
    """Posts finished spans as OTLP/HTTP JSON to ``<endpoint>/v1/traces``."""

    def __init__(
        self,
        endpoint: str,
        *,
        headers: dict[str, str] | None = None,
        service: str = "dif-general-harness",
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        self.url = endpoint.rstrip("/") + ("" if endpoint.endswith("/v1/traces") else "/v1/traces")
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.service = service
        self.http = client or httpx2.AsyncClient(timeout=10.0)
        self.failures = 0

    def payload(self, spans: list[Span], resource: dict[str, Any]) -> dict[str, Any]:
        return {
            "resourceSpans": [
                {
                    "resource": {"attributes": _attrs({"service.name": self.service, **resource})},
                    "scopeSpans": [
                        {
                            "scope": {"name": SCOPE_NAME},
                            "spans": [
                                {
                                    "traceId": s.trace_id,
                                    "spanId": s.span_id,
                                    **({"parentSpanId": s.parent_id} if s.parent_id else {}),
                                    "name": s.name,
                                    "kind": s.kind,
                                    "startTimeUnixNano": str(s.start_ns),
                                    "endTimeUnixNano": str(s.end_ns or s.start_ns),
                                    "attributes": _attrs(s.attributes),
                                    "status": {"code": 2, "message": s.error}
                                    if s.error
                                    else {"code": 1},
                                }
                                for s in spans
                            ],
                        }
                    ],
                }
            ]
        }

    async def export(self, spans: list[Span], resource: dict[str, Any]) -> bool:
        if not spans:
            return True
        try:
            response = await self.http.post(
                self.url, json=self.payload(spans, resource), headers=self.headers
            )
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}")
        except Exception as exc:  # telemetry never breaks a run
            self.failures += 1
            log.warning("OTLP export to %s failed: %s", self.url, exc)
            return False
        return True


def exporter_from_env(
    env: dict[str, str] | None = None, client: httpx2.AsyncClient | None = None
) -> OtlpExporter | None:
    env = dict(os.environ) if env is None else env
    endpoint = env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or env.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    if not endpoint:
        return None
    headers = {}
    for pair in (env.get("OTEL_EXPORTER_OTLP_HEADERS") or "").split(","):
        name, sep, value = pair.partition("=")
        if sep and name.strip():
            headers[name.strip()] = value.strip()
    service = env.get("OTEL_SERVICE_NAME") or "dif-general-harness"
    return OtlpExporter(endpoint, headers=headers, service=service, client=client)


class Tracer:
    """Collects the spans of each run (trace) and exports them when the run's root ends."""

    def __init__(self, exporter: OtlpExporter, resource: dict[str, Any]) -> None:
        self.exporter = exporter
        self.resource = resource
        self._open: dict[str, list[Span]] = {}  # trace id -> finished spans

    def start(self, name: str, *, kind: int = 1, **attrs: Any) -> Span:
        parent = _current.get()
        trace_id = parent.trace_id if parent else secrets.token_hex(16)
        span = Span(name, trace_id, secrets.token_hex(8), parent.span_id if parent else None,
                    time.time_ns(), kind=kind)  # fmt: skip
        span.set(**attrs)
        self._open.setdefault(trace_id, [])
        return span

    def finish(self, span: Span, end_ns: int | None = None) -> None:
        span.end_ns = end_ns or time.time_ns()
        self._open.setdefault(span.trace_id, []).append(span)

    @contextlib.contextmanager
    def active(self, span: Span) -> Iterator[None]:
        token = _current.set(span)
        try:
            yield
        finally:
            _current.reset(token)

    async def flush(self, trace_id: str) -> None:
        spans = self._open.pop(trace_id, [])
        await self.exporter.export(spans, self.resource)


class TracedProvider:
    """Wraps the model router: one ``chat`` span per model call, a child of the run."""

    def __init__(self, inner: ModelProvider, tracer: Tracer, vendor: Any) -> None:
        self.inner = inner
        self.tracer = tracer
        self.vendor = vendor  # role -> provider name
        self.name = getattr(inner, "name", "provider")

    async def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]:
        span = self.tracer.start(
            f"chat {request.model_role}",
            kind=3,
            gen_ai__operation__name="chat",
            gen_ai__provider__name=self.vendor(request.model_role),
            dif__model_role=request.model_role,
        )
        try:
            async for event in self.inner.stream(request):
                if isinstance(event, ProviderMessage):
                    usage = event.usage
                    span.name = f"chat {event.model or request.model_role}"
                    span.set(
                        gen_ai__request__model=event.model,
                        gen_ai__response__model=event.model,
                        gen_ai__usage__input_tokens=usage.input_tokens,
                        gen_ai__usage__output_tokens=usage.output_tokens,
                        gen_ai__response__finish_reasons=event.stop_reason,
                    )
                    price = DEFAULT_PRICES.get(event.model or "")
                    if price is not None:
                        span.set(dif__cost_usd=round(cost_usd(usage, price), 8))
                yield event
        except Exception as exc:
            span.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.tracer.finish(span)
            if span.parent_id is None:  # a call outside any run (extraction, a judge)
                await self.tracer.flush(span.trace_id)
