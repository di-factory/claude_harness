"""Every harness component is a hypothesis: ablations measure whether each still pays.

A skill, the verifier, the intent router, compaction, gates or memory each exists because a
model needed it once; a newer model may not, and then it only adds cost, latency and new ways
to fail. ``eval --ablate skills,verifier`` runs the same suites on the solution as it is and
on a copy without each named component (in throwaway instances, like every eval), and reports
the pass rate, pass^k and cost of each, against the baseline:

- **keeps its place**: without it, quality drops (or an unsafe action appears);
- **no measured lift**: without it, quality holds and the runs cost less: a candidate to
  remove, for a person to decide (and to re-check on the next model change);
- **no difference**: it changed nothing these suites measure.

An ablated copy is only ever evaluated: it is never stored, signed or deployed.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..spec.loader import ResolvedSpec, resolved_from_data
from .evals import EvalReport


def _no_skills(data: dict[str, Any]) -> None:
    data["skills"] = []


def _no_role(role: str) -> Callable[[dict[str, Any]], None]:
    def strip(data: dict[str, Any]) -> None:
        ((data.get("models") or {}).get("roles") or {}).pop(role, None)

    return strip


def _no_verifier(data: dict[str, Any]) -> None:
    _no_role("verifier")(data)
    verification = (data.get("policies") or {}).get("verification") or {}
    verification.pop("verifier", None)


def _no_gates(data: dict[str, Any]) -> None:
    for wf in (data.get("workflows") or {}).values():
        for step in wf.get("steps") or []:
            step.pop("gate", None)
            for branch in step.get("branches") or []:
                branch.pop("gate", None)
    for agent in (data.get("agents") or {}).values():
        agent.pop("output_schema", None)


def _no_memory(data: dict[str, Any]) -> None:
    data.pop("memory", None)
    for agent in (data.get("agents") or {}).values():
        agent.pop("memory", None)


COMPONENTS: dict[str, Callable[[dict[str, Any]], None]] = {
    "skills": _no_skills,
    "verifier": _no_verifier,
    "router": _no_role("router"),
    "compaction": _no_role("compaction"),
    "gates": _no_gates,
    "memory": _no_memory,
}


def without(resolved: ResolvedSpec, component: str) -> ResolvedSpec:
    """The solution without one component (raises ValueError when it cannot be removed)."""
    if component not in COMPONENTS:
        raise ValueError(f"unknown component {component!r}; one of {sorted(COMPONENTS)}")
    data = copy.deepcopy(resolved.data)
    COMPONENTS[component](data)
    ablated = resolved_from_data(data, f"without {component}")
    errors = [i for i in ablated.issues if i.severity == "error"]
    if errors:
        raise ValueError(f"without {component} the solution does not validate: {errors[0]}")
    return ablated


@dataclass
class Row:
    name: str
    report: EvalReport

    @property
    def rate(self) -> float:
        return self.report.pass_rate or 0.0


def verdict(base: EvalReport, ablated: EvalReport) -> str:
    rate_base, rate = base.pass_rate or 0.0, ablated.pass_rate or 0.0
    k_base, k = base.pass_k or 0.0, ablated.pass_k or 0.0
    if rate < rate_base or k < k_base or ablated.unsafe_actions > base.unsafe_actions:
        return "keeps its place"
    if ablated.cost_usd < base.cost_usd:
        return "no measured lift"
    return "no difference"


def table(base: EvalReport, rows: list[Row]) -> str:
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.0%}"

    lines = [f"{'component':<12} {'pass':>5} {'pass^k':>7} {'cost':>9}  verdict",
             f"{'(baseline)':<12} {pct(base.pass_rate):>5} {pct(base.pass_k):>7}"
             f" ${base.cost_usd:>8.4f}"]  # fmt: skip
    for row in rows:
        lines.append(f"{'-' + row.name:<12} {pct(row.report.pass_rate):>5}"
                     f" {pct(row.report.pass_k):>7} ${row.report.cost_usd:>8.4f}"
                     f"  {verdict(base, row.report)}")  # fmt: skip
    return "\n".join(lines)
