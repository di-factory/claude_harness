"""Escalation rules (``policies.escalation.rules``): ``{"when": <condition>, "to": "human"}``.

Evaluated at the points the runtime knows about (a verification that failed twice, a
budget stop...) with the facts of that moment; the first rule that holds decides.
"""

from __future__ import annotations

from typing import Any

from ..core import cel


def first_match(rules: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any] | None:
    for rule in rules:
        when = rule.get("when")
        if isinstance(when, str) and cel.check(when) is None and cel.holds(when, context):
            return rule
    return None
