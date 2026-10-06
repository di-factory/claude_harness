"""Tool registry: JSON-schema contracts executed by the harness, never by the model.

Every tool, whatever its source (Python, HTTP connector, MCP server), is a JSON-schema
contract plus an async handler. Inputs are validated before the handler runs, because
streamed tool inputs can arrive truncated. Execution always ends in a structured
observation: ``ok | denied | error | timeout``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, get_type_hints

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError, create_model

from ..core.messages import SideEffects, ToolResultBlock, ToolStatus, ToolUseBlock

Handler = Callable[..., Awaitable[Any]]
InputCheck = Callable[[dict[str, Any]], dict[str, Any]]


IDEMPOTENCY_KEY: ContextVar[str | None] = ContextVar("dif_idempotency_key", default=None)
"""The key of the side-effecting call being run (``runtime/intents.py``): a handler that
calls an outside API sends it, so a retried intent is one operation there."""


class InputError(ValueError):
    """Tool input that does not match the tool's schema."""


class ToolFailure(Exception):
    """A tool's failure with what the model needs to recover: raise it from a handler to say
    precisely whether it can be retried and whether anything changed (an HTTP 4xx changed
    nothing; a lost response may have). Any other exception is reported as a failure whose
    side effects are unknown, unless the tool only reads."""

    def __init__(
        self, message: str, *, reason: str = "failed", retryable: bool = False,
        side_effects: SideEffects = "unknown", hint: str | None = None,
    ) -> None:  # fmt: skip
        super().__init__(message)
        self.reason, self.retryable, self.side_effects, self.hint = (
            reason,
            retryable,
            side_effects,
            hint,
        )


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

    def replace(self, t: Tool) -> None:
        """Put ``t`` in place of the tool of that name (evals and replays: a canned result
        instead of the real call); registers it when there is none."""
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
            return _failed(call, call.input_error, "invalid_input", True, "none",
                           "send the call again with complete, valid arguments")  # fmt: skip
        t = self._tools.get(call.name)
        if t is None:
            names = ", ".join(self.names()[:30])
            return _failed(call, f"unknown tool {call.name!r}", "unknown_tool", False, "none",
                           f"use one of the tools you have: {names}")  # fmt: skip
        try:
            args = t.check_input(call.input) if t.check_input else call.input
        except InputError as exc:
            return _failed(call, f"invalid input: {exc}", "invalid_input", True, "none",
                           "fix the arguments named in the error and call it again")  # fmt: skip
        reads = t.effect is Effect.READ
        try:
            result = await asyncio.wait_for(t.handler(**args), timeout=t.timeout_s)
        except TimeoutError:
            return ToolResultBlock(
                tool_use_id=call.id, status=ToolStatus.TIMEOUT,
                error=f"timed out after {t.timeout_s}s", reason="timeout", retryable=reads,
                side_effects="none" if reads else "unknown",
                hint=None if reads else UNKNOWN_HINT,
            )  # fmt: skip
        except ToolFailure as exc:
            return _failed(call, str(exc), exc.reason, exc.retryable,
                           "none" if reads else exc.side_effects,
                           exc.hint or (UNKNOWN_HINT if exc.side_effects == "unknown" and not reads
                                        else None))  # fmt: skip
        except Exception as exc:  # a tool failure is an observation for the model, not a crash
            return _failed(call, f"{type(exc).__name__}: {exc}", "failed", reads,
                           "none" if reads else "unknown",
                           None if reads else UNKNOWN_HINT)  # fmt: skip
        return ToolResultBlock(tool_use_id=call.id, status=ToolStatus.OK, content=_capped(result))


MAX_RESULT_CHARS = 40_000  # what one tool result may put into the model's context


def _capped(result: Any) -> Any:
    """A result that fits the context budget; a cut one says how to get the rest."""
    text = result if isinstance(result, str) else None
    if text is None:
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return result
        if len(text) <= MAX_RESULT_CHARS:
            return result
    if len(text) <= MAX_RESULT_CHARS:
        return result
    left = len(text) - MAX_RESULT_CHARS
    return (text[:MAX_RESULT_CHARS] + f"\n[truncated: {left} more characters. Ask for less:"
            " a filter, a page, a narrower query or fewer fields]")  # fmt: skip


UNKNOWN_HINT = (
    "it may or may not have taken effect: check with a read tool (or ask the person) before"
    " trying it again"
)


def _failed(call: ToolUseBlock, error: str, reason: str, retryable: bool,
            side_effects: SideEffects, hint: str | None) -> ToolResultBlock:  # fmt: skip
    return ToolResultBlock(
        tool_use_id=call.id, status=ToolStatus.ERROR, error=error, reason=reason,
        retryable=retryable, side_effects=side_effects, hint=hint,
    )  # fmt: skip


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
