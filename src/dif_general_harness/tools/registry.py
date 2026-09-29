"""Tool registry: JSON-schema contracts executed by the harness, never by the model.

M0 scope: registration, schema export and execution with structured
observations and timeouts. Permissions, verification and MCP/HTTP sources
arrive in M1.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, get_type_hints

from pydantic import TypeAdapter, ValidationError, create_model

from ..core.messages import ToolResultBlock, ToolStatus, ToolUseBlock

Handler = Callable[..., Awaitable[Any]]


class Effect(StrEnum):
    READ = "read"
    WRITE = "write"
    EXTERNAL = "external"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    effect: Effect = Effect.READ
    timeout_s: float = 30.0
    _validator: TypeAdapter[Any] | None = None

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def tool(
    name: str,
    *,
    effect: Effect = Effect.READ,
    timeout_s: float = 30.0,
    description: str | None = None,
) -> Callable[[Handler], Tool]:
    """Declare an async Python function as a tool; its schema comes from the type hints."""

    def wrap(fn: Handler) -> Tool:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"tool {name!r} must be an async function")
        hints = get_type_hints(fn)
        params = inspect.signature(fn).parameters
        fields: dict[str, Any] = {}
        for pname, param in params.items():
            default = ... if param.default is inspect.Parameter.empty else param.default
            fields[pname] = (hints.get(pname, Any), default)
        model = create_model(f"{name.replace('.', '_')}_input", **fields)
        return Tool(
            name=name,
            description=description or (inspect.getdoc(fn) or "").strip(),
            input_schema=model.model_json_schema(),
            handler=fn,
            effect=effect,
            timeout_s=timeout_s,
            _validator=TypeAdapter(model),
        )

    return wrap


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, t: Tool) -> None:
        if t.name in self._tools:
            raise ValueError(f"tool {t.name!r} already registered")
        self._tools[t.name] = t

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [self._tools[n].schema() for n in self.names()]

    async def execute(self, call: ToolUseBlock) -> ToolResultBlock:
        """Run one call. Always returns a structured observation; never raises."""
        t = self._tools.get(call.name)
        if t is None:
            return ToolResultBlock(
                tool_use_id=call.id, status=ToolStatus.ERROR, error=f"unknown tool {call.name!r}"
            )
        try:
            args = call.input
            if t._validator is not None:
                args = t._validator.validate_python(call.input).model_dump()
        except ValidationError as exc:
            return ToolResultBlock(
                tool_use_id=call.id,
                status=ToolStatus.ERROR,
                error=f"invalid input: {exc.error_count()} error(s): {exc.errors()[0]['msg']}",
            )
        try:
            result = await asyncio.wait_for(t.handler(**args), timeout=t.timeout_s)
        except TimeoutError:
            return ToolResultBlock(
                tool_use_id=call.id,
                status=ToolStatus.TIMEOUT,
                error=f"timed out after {t.timeout_s}s",
            )
        except Exception as exc:  # a tool failure is an observation for the model, not a crash
            return ToolResultBlock(
                tool_use_id=call.id, status=ToolStatus.ERROR, error=f"{type(exc).__name__}: {exc}"
            )
        return ToolResultBlock(tool_use_id=call.id, status=ToolStatus.OK, content=result)
