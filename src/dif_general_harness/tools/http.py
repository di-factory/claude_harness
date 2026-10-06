"""HTTP connectors: tools declared in the spec (``tools.http``), no code needed.

Each operation becomes a tool named ``<connector>.<operation>``. Inputs are a short form,
``{"user_id": "string", "note": "string?"}`` (``?`` marks an optional field), or a full JSON
schema. Path placeholders (``/users/{user_id}``, also in a query string) are filled from the
input and URL-encoded; remaining inputs go to the query string for GET and DELETE and to a
JSON body otherwise.

The connector receives its config after ``$secret`` references are resolved. Plain HTTP is
refused except for localhost, so credentials never travel unencrypted.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx2

from ..spec.schema import HttpConnector, HttpOperation
from .registry import IDEMPOTENCY_KEY, Effect, Tool, ToolFailure, schema_check

MAX_RESPONSE_CHARS = 20_000
_SHORT_TYPES = {"string", "number", "integer", "boolean", "object", "array"}
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConnectorError(ValueError):
    """A connector that cannot be built from its spec."""


class HttpStatusError(ToolFailure):
    """An error status from the client's API, with what it means for a retry."""


def _status_failure(status: int, text: str, reads: bool) -> HttpStatusError:
    message = f"HTTP {status}: {text[:500]}"
    if status in (401, 403):
        return HttpStatusError(message, reason="not_authorized", side_effects="none",
                               hint="a person must fix this connector's access; tell the contact"
                               " it cannot be done right now")  # fmt: skip
    if status == 429:
        return HttpStatusError(message, reason="rate_limited", retryable=True, side_effects="none",
                               hint="wait a moment and try once more")  # fmt: skip
    if status == 409:
        return HttpStatusError(
            message,
            reason="conflict",
            side_effects="none",
            hint="read the current state first; it may already be done",
        )
    if status < 500:
        return HttpStatusError(message, reason="rejected", side_effects="none",
                               hint="the request was refused as sent; check the arguments"
                               " against the error")  # fmt: skip
    return HttpStatusError(
        message, reason="server_error", retryable=reads, side_effects="none" if reads else "unknown"
    )


def input_schema(short: dict[str, Any], path: str) -> dict[str, Any]:
    """Expand the short input form into a JSON schema; a full schema passes through."""
    if short.get("type") == "object" and isinstance(short.get("properties"), dict):
        schema = dict(short)
    else:
        props: dict[str, Any] = {}
        required: list[str] = []
        for key, kind in short.items():
            if isinstance(kind, dict):
                props[key] = kind
                required.append(key)
                continue
            base = str(kind).removesuffix("?")
            if base not in _SHORT_TYPES:
                raise ConnectorError(f"input {key!r}: unknown type {kind!r}")
            props[key] = {"type": base}
            if not str(kind).endswith("?"):
                required.append(key)
        schema = {"type": "object", "properties": props, "required": required}
    schema.setdefault("additionalProperties", False)
    placeholders = set(_PLACEHOLDER.findall(path))
    missing = placeholders - set(schema.get("properties", {}))
    if missing:
        raise ConnectorError(f"path {path!r} uses {sorted(missing)} that are not in the input")
    schema["required"] = sorted(set(schema.get("required", [])) | placeholders)
    return schema


def _auth_headers(auth: dict[str, Any]) -> dict[str, str]:
    kind = auth.get("type", "none") if auth else "none"
    if kind == "none":
        return {}
    if kind == "bearer":
        return {"Authorization": f"Bearer {_str(auth, 'token')}"}
    if kind == "header":
        return {_str(auth, "name"): _str(auth, "value")}
    if kind == "basic":
        pair = f"{_str(auth, 'username')}:{_str(auth, 'password')}".encode()
        return {"Authorization": "Basic " + base64.b64encode(pair).decode()}
    raise ConnectorError(f"auth type {kind!r} is not supported yet")


def _str(auth: dict[str, Any], key: str) -> str:
    value = auth.get(key)
    if not isinstance(value, str) or not value:
        raise ConnectorError(f"auth.{key} must be a resolved, non-empty string")
    return value


def _check_base_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS:
        return
    raise ConnectorError(f"base_url {url!r} must be https (plain http only for localhost)")


def http_tools(
    name: str,
    connector: HttpConnector | dict[str, Any],
    *,
    client: httpx2.AsyncClient | None = None,
    timeout_s: float = 30.0,
) -> list[Tool]:
    """Build the tools of one connector. ``client`` is injectable for tests."""
    spec = connector if isinstance(connector, HttpConnector) else HttpConnector(**connector)
    _check_base_url(spec.base_url)
    headers = _auth_headers(spec.auth)
    http = client or httpx2.AsyncClient(timeout=timeout_s)
    base = spec.base_url.rstrip("/")
    return [
        _operation_tool(f"{name}.{op_name}", op, base, headers, http, timeout_s)
        for op_name, op in spec.operations.items()
    ]


def _operation_tool(
    tool_name: str,
    op: HttpOperation,
    base: str,
    headers: dict[str, str],
    http: httpx2.AsyncClient,
    timeout_s: float,
) -> Tool:
    schema = input_schema(op.input, op.path)
    method = op.method.upper()
    placeholders = set(_PLACEHOLDER.findall(op.path))
    reads = op.effect == "read"

    async def handler(**args: Any) -> Any:
        path = _PLACEHOLDER.sub(lambda m: quote(str(args[m.group(1)]), safe=""), op.path)
        rest = {k: v for k, v in args.items() if k not in placeholders}
        in_query = method in {"GET", "DELETE", "HEAD"}
        url = httpx2.URL(base + path)
        if in_query and rest:  # merge, so a query already in the path template is kept
            url = url.copy_merge_params({k: _query_value(v) for k, v in rest.items()})
        sent = dict(headers)
        key = IDEMPOTENCY_KEY.get()
        if key and method not in {"GET", "HEAD"}:
            sent["Idempotency-Key"] = key  # the same intent, retried, is one operation
        try:
            response = await http.request(method, url, headers=sent,
                                          json=None if in_query else rest)  # fmt: skip
        except httpx2.ConnectError as exc:
            raise ToolFailure(f"could not reach the service: {exc}", reason="unreachable",
                              retryable=True, side_effects="none") from None  # fmt: skip
        except httpx2.TransportError as exc:  # sent, but the answer was lost
            raise ToolFailure(f"the connection failed: {type(exc).__name__}: {exc}",
                              reason="connection_lost", retryable=reads,
                              side_effects="none" if reads else "unknown") from None  # fmt: skip
        return _observation(response, reads)

    return Tool(
        name=tool_name,
        description=f"{method} {op.path}",
        input_schema=schema,
        handler=handler,
        effect=Effect(op.effect),
        timeout_s=timeout_s,
        check_input=schema_check(schema),
        verify=op.verify,
        source="http",
    )


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else json.dumps(value)


def _observation(response: httpx2.Response, reads: bool = True) -> Any:
    text = response.text
    if response.status_code >= 400:
        raise _status_failure(response.status_code, text, reads)
    if "json" in response.headers.get("content-type", ""):
        try:
            data = response.json()
        except ValueError:
            pass
        else:
            if len(text) <= MAX_RESPONSE_CHARS:
                return data
    if len(text) > MAX_RESPONSE_CHARS:
        return text[:MAX_RESPONSE_CHARS] + f"\n[truncated {len(text) - MAX_RESPONSE_CHARS} chars]"
    return text
