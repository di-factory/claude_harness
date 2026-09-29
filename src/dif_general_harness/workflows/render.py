"""``{{input.x}}``, ``{{steps.a.b}}``, ``{{var.x}}``, ``{{event.x}}``, ``{{contact.x}}`` in
workflow step fields. A whole-value reference keeps its type (``"{{steps.extract.output}}"``
is the object itself); inside text, values are written as text (JSON for objects)."""

from __future__ import annotations

import json
import re
from typing import Any

_REF = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)*)\s*\}\}")


def lookup(path: str, ctx: dict[str, Any]) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def render(value: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(value, str):
        whole = _REF.fullmatch(value.strip())
        if whole:
            return lookup(whole.group(1), ctx)
        return _REF.sub(lambda m: _text(lookup(m.group(1), ctx)), value)
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    return value
