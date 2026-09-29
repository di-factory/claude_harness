"""The interview: questions come from the packs (``variables[*].ask``), never from code.

A new pack brings its own questions. Variables without an ``ask`` block are still asked
when they are required, using their description. Placeholder defaults such as
``<main-model-id>`` do not count as answers.

Besides the pack's variables, every instance needs a few facts of its own (tenant id and
name, time zone, locale, instance id); those are the ``BASE`` questions.

Answers come from a person (``ask`` callback) or from an answers file (YAML or JSON), so the
same interview runs interactively or in CI, and adjusting a live instance is "change the
answers, rebuild".
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..spec.schema import Variable

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_DURATION = re.compile(r"^\d+(\.\d+)?[smhd]$")
_YES = {"y", "yes", "true", "1", "si", "sí", "s"}
_NO = {"n", "no", "false", "0"}


class AnswerError(ValueError):
    pass


@dataclass(frozen=True)
class Question:
    key: str  # "values.<name>" or a base key such as "tenant.id"
    text: str
    kind: str
    required: bool
    default: Any = None
    answered_by: str = "either"
    group: str = "general"
    order: int = 0
    example: str | None = None
    options: list[Any] | None = None
    minimum: float | None = None
    maximum: float | None = None

    @property
    def name(self) -> str:
        return self.key.removeprefix("values.")


def _is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("<") and value.endswith(">")


BASE = [
    Question("tenant.name", "What is the client's name?", "string", True, group="client", order=1),
    Question(
        "tenant.id",
        "Short id for the client (lowercase, digits and dashes)",
        "id",
        True,
        group="client",
        order=2,
        example="clinica-sonrisa",
    ),
    Question(
        "tenant.timezone",
        "Which time zone does the client operate in?",
        "string",
        True,
        default="America/Mexico_City",
        group="client",
        order=3,
    ),
    Question(
        "solution.id",
        "Instance id (leave empty for <client id>-<pack id>)",
        "id",
        False,
        group="client",
        order=5,
    ),
    Question(
        "solution.locale",
        "Language for the client's users (English unless the client asks for another)",
        "string",
        True,
        default="en",
        group="client",
        order=4,
        example="es-MX",
    ),
]


def questions(variables: dict[str, Variable]) -> list[Question]:
    out: list[Question] = []
    for name, var in variables.items():
        default = None if _is_placeholder(var.default) else var.default
        if var.ask is None and not var.required and default is not None:
            continue  # an optional value with a real default and no question: keep the default
        ask = var.ask
        out.append(
            Question(
                key=f"values.{name}",
                text=ask.question if ask else (var.description or name.replace("_", " ")),
                kind=var.type,
                required=var.required or (var.default is not None and default is None),
                default=default,
                answered_by=ask.answered_by if ask else "either",
                group=(ask.group if ask and ask.group else "general"),
                order=(ask.order if ask and ask.order is not None else 99),
                example=ask.example if ask else None,
                options=var.options,
                minimum=var.min,
                maximum=var.max,
            )
        )
    groups: dict[str, int] = {}
    for q in sorted(out, key=lambda q: q.order):
        groups.setdefault(q.group, len(groups))
    return BASE + sorted(out, key=lambda q: (groups[q.group], q.order, q.key))


def parse(q: Question, raw: Any) -> Any:
    """Turn an answer (text from a person, or a value from a file) into a typed value."""
    text = raw.strip() if isinstance(raw, str) else None
    if text == "" or raw is None:
        if q.default is not None:
            return q.default
        if q.required:
            raise AnswerError("an answer is required")
        return None
    kind = q.kind
    if kind in {"string", "file"}:
        value: Any = str(raw).strip()
        if kind == "file" and not Path(value).exists():
            raise AnswerError(f"file {value} does not exist")
    elif kind == "id":
        value = str(raw).strip()
        if not _ID.match(value):
            raise AnswerError("use lowercase letters, digits and dashes (2-63 characters)")
    elif kind == "integer":
        try:
            value = int(str(raw).strip())
        except ValueError:
            raise AnswerError("expected a whole number") from None
    elif kind == "number":
        try:
            value = float(str(raw).strip())
        except ValueError:
            raise AnswerError("expected a number") from None
    elif kind == "boolean":
        if isinstance(raw, bool):
            value = raw
        elif str(raw).strip().lower() in _YES:
            value = True
        elif str(raw).strip().lower() in _NO:
            value = False
        else:
            raise AnswerError("answer yes or no")
    elif kind == "enum":
        value = raw if not isinstance(raw, str) else raw.strip()
        if q.options and value not in q.options:
            raise AnswerError(f"choose one of {q.options}")
    elif kind == "list":
        value = _structured(raw, list) if not isinstance(raw, list) else raw
    elif kind in {"object", "schedule"}:
        value = _structured(raw, dict) if not isinstance(raw, dict) else raw
    elif kind == "duration":
        value = str(raw).strip()
        if not _DURATION.match(value):
            raise AnswerError("expected a duration such as 30m, 24h or 7d")
    else:
        value = raw
    if isinstance(value, int | float) and not isinstance(value, bool):
        if q.minimum is not None and value < q.minimum:
            raise AnswerError(f"must be at least {q.minimum:g}")
        if q.maximum is not None and value > q.maximum:
            raise AnswerError(f"must be at most {q.maximum:g}")
    return value


def _structured(raw: Any, kind: type) -> Any:
    """Lists: JSON or comma-separated. Objects and schedules: JSON or ``key: value; ...``."""
    text = str(raw).strip()
    if text[:1] in "[{":
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise AnswerError(f"invalid JSON: {exc}") from None
        if not isinstance(value, kind):
            raise AnswerError(f"expected a {'list' if kind is list else 'mapping'}")
        return value
    if kind is list:
        return [item.strip() for item in text.split(",") if item.strip()]
    pairs = [p for p in re.split(r"[;\n]", text) if p.strip()]
    out: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition(":") if ":" in pair.split()[0] else pair.partition(" ")
        if not sep or not key.strip() or not value.strip():
            raise AnswerError(
                "use 'key: value; key: value', e.g. 'mon-fri: 09:00-19:00; sat: 09:00-14:00'"
            )
        out[key.strip()] = value.strip()
    return out


def load_answers(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    if not isinstance(data, dict):
        raise AnswerError(f"{path}: expected a mapping of answers")
    flat: dict[str, Any] = {}
    for key, value in data.items():
        if key in {"tenant", "solution", "values"} and isinstance(value, dict):
            for sub, v in value.items():
                flat[f"{key}.{sub}"] = v
        else:
            flat[str(key)] = value
    return flat


Ask = Callable[[Question, str | None], str]  # (question, previous error) -> raw answer


def interview(
    qs: list[Question], answers: dict[str, Any] | None = None, ask: Ask | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Answer every question from ``answers`` first, then ``ask``. Returns (answers, problems).

    Without ``ask`` (non-interactive), a missing or invalid answer is a problem, not a prompt.
    """
    given = dict(answers or {})
    result: dict[str, Any] = {}
    problems: list[str] = []
    for q in qs:
        if q.key in given or q.name in given:
            raw = given.get(q.key, given.get(q.name))
            try:
                value = parse(q, raw)
            except AnswerError as exc:
                if ask is None:
                    problems.append(f"{q.key}: {exc}")
                    continue
                value = _ask_until_valid(q, ask, str(exc))
        elif ask is not None:
            value = _ask_until_valid(q, ask, None)
        else:
            try:
                value = parse(q, None)
            except AnswerError as exc:
                problems.append(f"{q.key}: {exc} ({q.text})")
                continue
        if value is not None:
            result[q.key] = value
    known = {q.key for q in qs} | {q.name for q in qs}
    problems += [f"{k}: not a question of this pack" for k in given if k not in known]
    return result, problems


def _ask_until_valid(q: Question, ask: Ask, error: str | None) -> Any:
    while True:
        raw = ask(q, error)
        try:
            return parse(q, raw)
        except AnswerError as exc:
            error = str(exc)
