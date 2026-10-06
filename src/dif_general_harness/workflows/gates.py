"""Gates on what an agent returns, outside the agent's control, cheapest first.

An agent re-reading its own work approves it, so a workflow ``agent`` step (or a ``foreach``)
can name a ``gate``; its checks run in this order, each only when the previous passed:

1. ``schema``: the return is JSON that satisfies a JSON Schema (the step's ``schema``, inline
   or a file, else the agent's ``output_schema``), plus the step's own deterministic
   ``rules`` (the graph's checks for a research return). Free: no model is called.
2. ``verifier``: the ``verifier`` model role, with only the task and the return (never the
   agent's conversation or reasoning), judges it against ``criteria``.
3. ``threshold``: a number in the return (``field``, default ``confidence``) under ``min``.

What happens next (``run_gated``): a malformed return (schema) is never retried; any other
failure is retried once, in the same conversation, with the reason appended ("Your return
was rejected: ..."), so the retry is a correction, not the same attempt again; a second
failure is ``needs_human``. Every failure is counted for the run record.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from ..core.messages import Message
from ..providers.base import ModelRequest, ProviderMessage

if TYPE_CHECKING:
    from ..runtime.instance import Instance

GATE_PROMPT = """You check one return from a research or processing agent before it is
accepted. You see only the task it was given and what it returned: not how it worked.
Judge it against the criteria. Thin or missing evidence, a different entity than the task
named, and relationships or facts its own evidence lines do not support are failures.
Answer with JSON only: {"pass": true|false, "reason": "<one sentence; on a failure, what
to fix>"}."""
DEFAULT_CRITERIA = [
    "Every stated fact is supported by a source or evidence line in the return.",
    "It is about exactly the entity or item the task named.",
]

Rules = Callable[[dict[str, Any]], Awaitable[str | None]]  # deterministic: a reason or None


@dataclass
class Gate:
    schema: dict[str, Any] | None = None
    criteria: list[str] = field(default_factory=list)
    verify: bool = False
    threshold_field: str = "confidence"
    threshold: float | None = None
    retries: int = 1
    rules: Rules | None = None

    @classmethod
    def from_step(cls, raw: Any, agent_schema: str | None = None) -> Gate | None:
        if not raw and not agent_schema:
            return None
        cfg = raw if isinstance(raw, dict) else {}
        schema = cfg.get("schema", agent_schema)
        if isinstance(schema, str):
            schema = json.loads(Path(schema).read_text(encoding="utf-8"))
        verify = cfg.get("verify", False)
        criteria = list(verify) if isinstance(verify, list) else []
        threshold = cfg.get("threshold")
        if isinstance(threshold, dict):
            t_field, t_min = str(threshold.get("field", "confidence")), threshold.get("min")
        else:
            t_field, t_min = "confidence", threshold
        return cls(
            schema=schema if isinstance(schema, dict) else None,
            criteria=criteria or (DEFAULT_CRITERIA if verify else []),
            verify=bool(verify),
            threshold_field=t_field,
            threshold=float(t_min) if t_min is not None else None,
            retries=int(cfg.get("retries", 1)),
        )


@dataclass
class Failure:
    gate: str  # schema | verifier | threshold | agent
    reason: str


@dataclass
class Gated:
    """The outcome of a gated piece of work."""

    output: dict[str, Any] | None
    passed: bool
    failures: list[Failure] = field(default_factory=list)
    retried: bool = False

    @property
    def needs_human(self) -> bool:
        return not self.passed


def parse(text: str) -> Any:
    """The JSON an agent returned (alone, or in a code fence), else None."""
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


async def check_schema(gate: Gate, output: Any) -> str | None:
    if not isinstance(output, dict):
        return "the return is not a JSON object"
    if gate.schema is not None:
        errors = sorted(Draft202012Validator(gate.schema).iter_errors(output), key=str)
        if errors:
            where = "/".join(str(p) for p in errors[0].absolute_path) or "the return"
            return f"{where}: {errors[0].message}"[:300]
    if gate.rules is not None:
        return await gate.rules(output)
    return None


def check_threshold(gate: Gate, output: dict[str, Any]) -> str | None:
    if gate.threshold is None:
        return None
    value = output.get(gate.threshold_field)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return f"no numeric {gate.threshold_field}"
    if value < gate.threshold:
        return f"{gate.threshold_field} {value} is under {gate.threshold}"
    return None


async def check_verifier(
    inst: Instance, gate: Gate, task: str, output: dict[str, Any]
) -> str | None:
    """The verifier model, fresh: the task and the return only. A reason on a failure."""
    roles = inst.spec.models.roles if inst.spec.models else {}
    if not gate.verify or "verifier" not in roles or inst.provider is None:
        return None
    question = (
        "Criteria:\n" + "\n".join(f"- {c}" for c in gate.criteria)
        + f"\n\nTask:\n{task[:6000]}\n\nReturn:\n"
        + json.dumps(output, ensure_ascii=False, default=str)[:12000]
    )  # fmt: skip
    request = ModelRequest(
        system=GATE_PROMPT, messages=[Message.user(question)], tools=[], model_role="verifier"
    )
    final: ProviderMessage | None = None
    async for event in inst.provider.stream(request):
        if isinstance(event, ProviderMessage):
            final = event
    if final is None:
        return "the verifier gave no answer"
    await inst.charge("gate", "verifier", final)
    found = re.search(r"\{.*\}", final.message.text(), re.DOTALL)
    try:
        answer = json.loads(found.group(0)) if found else None
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or not isinstance(answer.get("pass"), bool):
        return "the verifier's answer could not be read"
    return None if answer["pass"] else str(answer.get("reason") or "rejected")[:300]


Attempt = Callable[[str], Awaitable[str]]  # a message to the agent -> its final text


async def run_gated(inst: Instance, gate: Gate | None, task: str, attempt: Attempt) -> Gated:
    """Ask, gate, and on a failure other than a malformed return retry once with the
    reason; a second failure needs a person."""
    message = task
    failures: list[Failure] = []
    for round_ in range(1 + max(0, gate.retries if gate else 0)):
        text = await attempt(message)
        output = parse(text)
        if gate is None:
            return Gated(output if isinstance(output, dict) else {"text": text}, True)
        problem = await check_schema(gate, output)
        if problem is not None:
            failures.append(Failure("schema", problem))
            return Gated(None, False, failures, retried=round_ > 0)  # malformed: no retry
        assert isinstance(output, dict)
        found: Failure | None = None
        if (why := await check_verifier(inst, gate, task, output)) is not None:
            found = Failure("verifier", why)
        elif (why := check_threshold(gate, output)) is not None:
            found = Failure("threshold", why)
        if found is None:
            return Gated(output, True, failures, retried=round_ > 0)
        failures.append(found)
        message = (
            f"Your return was rejected by the {found.gate} check: {found.reason}\n"
            "Fix exactly that and return the whole corrected JSON, nothing else."
        )
    return Gated(None, False, failures, retried=True)
