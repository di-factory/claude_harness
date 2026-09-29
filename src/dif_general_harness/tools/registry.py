"""Tool registry: JSON-schema contracts executed by the harness, never by the model.

Every tool, whatever its source (Python, HTTP connector, MCP server), is a JSON-schema
contract plus an async handler. Inputs are validated before the handler runs, because
streamed tool inputs can arrive truncated. Execution always ends in a structured
observation: ``ok | denied | error | timeout``.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, get_type_hints

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError, create_model

from ..core.messages import ToolResultBlock, ToolStatus, ToolUseBlock

Handler = Callable[..., Awaitable[Any]]
InputCheck = Callable[[dict[str, Any]], dict[str, Any]]


class InputError(ValueError):
    """Tool input that does not match the tool's schema."""


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
    check_input: InputCheck | None = None
    verify: str | None = None  # a check from policies.verification that must pass first
    source: str = "python"

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
        model: type[BaseModel] = create_model(f"{name.replace('.', '_')}_input", **fields)

        def check(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return model.model_validate(args).model_dump()
            except ValidationError as exc:
                first = exc.errors()[0]
                where = ".".join(str(p) for p in first["loc"]) or "input"
                raise InputError(f"{exc.error_count()} error(s): {where}: {first['msg']}") from None

        return Tool(
            name=name,
            description=description or (inspect.getdoc(fn) or "").strip(),
            input_schema=model.model_json_schema(),
            handler=fn,
            effect=effect,
            timeout_s=timeout_s,
            check_input=check,
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
        if call.input_error:
            return ToolResultBlock(
                tool_use_id=call.id, status=ToolStatus.ERROR, error=call.input_error
            )
        t = self._tools.get(call.name)
        if t is None:
            return ToolResultBlock(
                tool_use_id=call.id, status=ToolStatus.ERROR, error=f"unknown tool {call.name!r}"
            )
        try:
            args = t.check_input(call.input) if t.check_input else call.input
        except InputError as exc:
            return ToolResultBlock(
                tool_use_id=call.id, status=ToolStatus.ERROR, error=f"invalid input: {exc}"
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


def schema_check(schema: dict[str, Any]) -> InputCheck:
    """An input check from a JSON schema (HTTP operations, MCP tools)."""
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    def check(args: dict[str, Any]) -> dict[str, Any]:
        errors = sorted(validator.iter_errors(args), key=lambda e: list(e.path))
        if errors:
            where = ".".join(str(p) for p in errors[0].path) or "input"
            raise InputError(f"{len(errors)} error(s): {where}: {errors[0].message}")
        return args

    return check
