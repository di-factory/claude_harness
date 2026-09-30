"""Python extensions in a container (``tools.config.python.isolation: "container"``).

In-process extensions (the default) run with the harness's privileges. Isolated ones never
run in the harness at all:

- the harness reads the function's contract **without importing the module**: the
  signature, type hints, docstring and ``@tool(...)`` decorator are parsed with ``ast``;
- each call runs in a throwaway container (the same ``ContainerExecutor`` as shell
  commands): the extension's folder mounted read-only, no network unless ``allow_hosts``
  and an egress proxy allow it, no secrets, CPU, memory and time capped;
- arguments go in as base64 JSON in an environment variable and the result comes back as
  JSON on stdout after a marker; anything else is an error for the model, never a crash.

A decorated extension imports ``dif_general_harness.tools.registry``; inside the container
a small stand-in module provides ``tool`` and ``Effect``, so the image needs only Python.
"""

from __future__ import annotations

import ast
import base64
import json
from pathlib import Path
from typing import Any

from .packs.coding import ContainerExecutor
from .python import PythonToolError, split
from .registry import Effect, Tool, schema_check

MARKER = "\x1eDIF_RESULT\x1e"
MAX_ARGS = 64 * 1024
RUNNER = r"""
import asyncio, base64, enum, importlib.util, json, os, sys, types
reg = types.ModuleType("dif_general_harness.tools.registry")
class Effect(str, enum.Enum):
    READ = "read"
    WRITE = "write"
    EXTERNAL = "external"
def tool(name, **kw):
    def wrap(fn):
        return fn
    return wrap
reg.Effect, reg.tool = Effect, tool
for n in ("dif_general_harness", "dif_general_harness.tools"):
    sys.modules[n] = types.ModuleType(n)
sys.modules["dif_general_harness.tools.registry"] = reg
sys.path.insert(0, "/workspace")
spec = importlib.util.spec_from_file_location("extension", "/workspace/" + os.environ["DIF_MODULE"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
fn = getattr(mod, os.environ["DIF_FUNC"])
out = fn(**json.loads(base64.b64decode(os.environ["DIF_ARGS"])))
if asyncio.iscoroutine(out):
    out = asyncio.run(out)
sys.stdout.write("\x1eDIF_RESULT\x1e" + json.dumps(out, default=str))
"""
_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean",
          "dict": "object", "list": "array"}  # fmt: skip


def _schema(node: ast.expr | None) -> dict[str, Any]:
    """A JSON schema for a type hint, from its syntax (unknown hints accept anything)."""
    if node is None:
        return {}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return _schema(ast.parse(node.value, mode="eval").body)  # a string annotation
        except SyntaxError:
            return {}
    if isinstance(node, ast.Name) and node.id in _TYPES:
        return {"type": _TYPES[node.id]}
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        if node.value.id in ("list", "List"):
            return {"type": "array", "items": _schema(node.slice)}
        if node.value.id in ("dict", "Dict"):
            return {"type": "object"}
        if node.value.id == "Optional":
            return {"anyOf": [_schema(node.slice), {"type": "null"}]}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return {"anyOf": [_schema(node.left), _schema(node.right)]}
    if isinstance(node, ast.Constant) and node.value is None:
        return {"type": "null"}
    return {}


def describe(ref: str) -> tuple[str, str, dict[str, Any], Effect]:
    """(tool name, description, input schema, effect) of an extension, without running it."""
    parts = split(ref)
    if parts is None:
        raise PythonToolError(f"not a resolved Python tool reference: {ref!r}")
    path, func = parts
    if not path.is_file():
        raise PythonToolError(f"file not found: {path}")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        raise PythonToolError(f"{path.name} has a syntax error: {exc}") from None
    fn = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == func), None)  # fmt: skip
    if fn is None:
        raise PythonToolError(f"{path.name} has no function {func!r}")
    ns, name, effect = path.stem, f"{path.stem}.{func}", Effect.READ
    for deco in fn.decorator_list:
        if isinstance(deco, ast.Call) and _called(deco.func) == "tool":
            if deco.args and isinstance(deco.args[0], ast.Constant):
                name = str(deco.args[0].value)
            for kw in deco.keywords:
                if kw.arg == "effect" and isinstance(kw.value, ast.Attribute):
                    effect = Effect(kw.value.attr.lower())
    if name.split(".", 1)[0] != ns:
        raise PythonToolError(f"tool {name!r} must be in the {ns!r} namespace")
    args = fn.args
    positional = [*args.posonlyargs, *args.args]
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    properties, required = {}, []
    for arg, default in [*zip(positional, defaults, strict=True),
                         *zip(args.kwonlyargs, args.kw_defaults, strict=True)]:  # fmt: skip
        properties[arg.arg] = _schema(arg.annotation)
        if default is None:
            required.append(arg.arg)
    schema: dict[str, Any] = {"type": "object", "properties": properties,
                              "additionalProperties": False}  # fmt: skip
    if required:
        schema["required"] = required
    return name, (ast.get_docstring(fn) or "").strip(), schema, effect


def _called(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else ""


def isolated(ref: str, executor: ContainerExecutor, timeout_s: float = 30.0) -> Tool:
    """A tool whose every call runs the extension in a fresh container."""
    name, description, schema, effect = describe(ref)
    path, func = split(ref)  # type: ignore[misc]

    async def handler(**kwargs: Any) -> Any:
        payload = base64.b64encode(json.dumps(kwargs, default=str).encode()).decode()
        if len(payload) > MAX_ARGS:
            raise ValueError("arguments are too large for an isolated extension")
        env = {"DIF_MODULE": path.name, "DIF_FUNC": func, "DIF_ARGS": payload,
               "DIF_RUNNER": RUNNER, "PYTHONDONTWRITEBYTECODE": "1"}  # fmt: skip
        result = await executor.run_with(
            'exec python3 -c "$DIF_RUNNER"', Path(path.parent), timeout_s,
            env=env, read_only=True,
        )  # fmt: skip
        if result.timed_out:
            raise TimeoutError(f"{name} timed out after {timeout_s}s")
        _, marker, after = result.output.rpartition(MARKER)
        if result.exit_code != 0 or not marker:
            tail = result.output.strip().splitlines()[-1:] or ["no output"]
            raise RuntimeError(f"{name} failed (exit {result.exit_code}): {tail[0][:300]}")
        return json.loads(after)

    return Tool(
        name=name,
        description=description,
        input_schema=schema,
        handler=handler,
        effect=effect,
        timeout_s=timeout_s + 30.0,  # the container's own limit comes first
        check_input=schema_check(schema),
        source="python-container",
    )
