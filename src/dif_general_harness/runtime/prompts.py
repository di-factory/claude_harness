"""Prompt rendering: ``{{var.x}}`` and ``{{solution.x}}`` in prompt files and templates.

Specs are interpolated when they load, but prompt files are read at runtime, so they are
rendered here. An unknown reference is an error: a prompt that says ``{{var.clinic}}`` to
a patient is worse than a run that does not start.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..spec.schema import SolutionSpec

_REF = re.compile(r"\{\{\s*(var|solution|tenant)\.([A-Za-z0-9_]+)\s*\}\}")


class PromptError(ValueError):
    pass


def load_text(ref: str) -> str:
    """A prompt field is a file path (made absolute by the loader) or inline text."""
    if "\n" not in ref and ref.endswith((".md", ".txt")):
        path = Path(ref)
        if not path.is_file():
            raise PromptError(f"prompt file {ref} does not exist")
        return path.read_text(encoding="utf-8")
    return ref


def render(text: str, spec: SolutionSpec) -> str:
    sources: dict[str, dict[str, Any]] = {
        "var": spec.values,
        "solution": spec.solution.model_dump(),
        "tenant": spec.tenant.model_dump() if spec.tenant else {},
    }
    missing: list[str] = []

    def sub(m: re.Match[str]) -> str:
        values = sources[m.group(1)]
        if m.group(2) not in values or values[m.group(2)] is None:
            missing.append(f"{m.group(1)}.{m.group(2)}")
            return m.group(0)
        return _text(values[m.group(2)])

    out = _REF.sub(sub, text)
    if missing:
        raise PromptError(f"prompt references values that are not set: {sorted(set(missing))}")
    return out


def _text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_text(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {_text(v)}" for k, v in value.items())
    return str(value)
