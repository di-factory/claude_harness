"""Python tools from packs (``tools.python``): the escape hatch for logic the spec cannot say.

A reference is ``module.path:function``, relative to the directory of the spec layer that
declares it (``extensions.slots:best_slot`` is ``extensions/slots.py`` next to the
pack.json). The loader rewrites it to ``/abs/path/extensions/slots.py:best_slot``.

The tool's namespace is the module's name (``slots``), so specs reference it as
``slots.best_slot``. The function is either decorated with ``@tool("slots.name",
effect=...)`` from ``dif_general_harness.tools.registry``, or a plain function (async or
not) with type hints, which becomes a ``read`` tool (use ``tools.overrides`` to declare
another effect). Either way it runs behind the same permissions, verification, budgets
and audit as every other tool.

Extension code ships inside the staged solution, so Jag's deploy signature covers it.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import importlib.util
import inspect
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .registry import Tool, tool

DOTTED = re.compile(r"^(?P<module>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*):(?P<func>[A-Za-z_]\w*)$")
RESOLVED = re.compile(r"^(?P<path>.+\.py):(?P<func>[A-Za-z_]\w*)$")


class PythonToolError(RuntimeError):
    pass


def absolutize(ref: str, base: Path) -> str:
    """``extensions.slots:f`` relative to ``base`` -> ``/abs/extensions/slots.py:f``."""
    match = DOTTED.match(ref)
    if not match:
        return ref
    path = (base / Path(*match["module"].split("."))).with_suffix(".py").resolve()
    return f"{path}:{match['func']}"


def split(ref: str) -> tuple[Path, str] | None:
    match = RESOLVED.match(ref)
    return (Path(match["path"]), match["func"]) if match else None


def namespace(ref: str) -> str | None:
    parts = split(ref)
    if parts is not None:
        return parts[0].stem
    match = DOTTED.match(ref)
    return match["module"].rsplit(".", 1)[-1] if match else None


def load(ref: str) -> Tool:
    parts = split(ref)
    if parts is None:
        raise PythonToolError(f"not a resolved Python tool reference: {ref!r}")
    path, func = parts
    if not path.is_file():
        raise PythonToolError(f"file not found: {path}")
    unique = hashlib.sha256(str(path).encode()).hexdigest()[:10]
    module_name = f"dif_extension_{unique}_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PythonToolError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # so type hints and dataclasses in it resolve
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise PythonToolError(
            f"{path.name} failed to import: {type(exc).__name__}: {exc}"
        ) from None
    obj: Any = getattr(module, func, None)
    ns = path.stem
    if isinstance(obj, Tool):
        if obj.name.split(".", 1)[0] != ns:
            raise PythonToolError(f"tool {obj.name!r} must be in the {ns!r} namespace")
        return replace(obj, source="python")
    if not callable(obj):
        raise PythonToolError(f"{path.name} has no function {func!r}")
    if inspect.iscoroutinefunction(obj):
        handler = obj
    else:
        sync = obj

        @functools.wraps(sync)
        async def handler(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(sync, *args, **kwargs)

    return replace(tool(f"{ns}.{func}")(handler), source="python")
