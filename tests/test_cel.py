"""The condition language (CEL subset, decision 33)."""

from __future__ import annotations

from typing import Any

import pytest

from dif_general_harness.core.cel import CelError, check, evaluate, holds, roots

CTX: dict[str, Any] = {
    "steps": {
        "wait": {"replied": True, "reply": "1"},
        "extract": {
            "confidence": 0.92,
            "output": {"total": 25000.0, "rfc_receptor": "ABC010101AB1"},
        },
        "validate": {"valid": True},
        "dup": {"found": False},
        "triage": {"route": "resolver"},
    },
    "var": {"auto_post_limit_mxn": 20000, "company_rfc": "ABC010101AB1", "repos": ["api", "web"],
            "pickup_label": "agent"},
    "ticket": {"priority": "p2", "status": "open", "requester_email": "ana@acme.mx"},
    "contact": {"verified": True, "email": "ana@acme.mx", "tags": ["vip"]},
    "event": {"action": "labeled", "label": {"name": "agent"}, "repository": {"name": "api"}},
    "items": [10, 20, 30],
}  # fmt: skip


@pytest.mark.parametrize(
    ("expr", "value"),
    [
        ("true", True),
        ("steps.wait.replied", True),
        ("not steps.wait.replied", False),
        ("steps.extract.output.total > var.auto_post_limit_mxn", True),
        ("steps.extract.confidence < 0.8", False),
        ("steps.validate.valid and not steps.dup.found", True),
        ("not steps.validate.valid or steps.extract.output.rfc_receptor != var.company_rfc", False),
        ("steps.triage.route == 'resolver'", True),
        ("ticket.priority in ['p1', 'p2']", True),
        ("ticket.status in ['solved', 'pending_customer']", False),
        ("ticket.status not in ['solved']", True),
        ("contact.verified == true and contact.email == ticket.requester_email", True),
        ("event.action == 'labeled' and event.label.name == var.pickup_label"
         " and event.repository.name in var.repos", True),
        ("'vip' in contact.tags", True),
        ("items[1] == 20 && items[-1] == 30", True),
        ("contact['email'] == \"ana@acme.mx\"", True),
        ("-items[0] < 0", True),
        ("(1 < 2) == true", True),
        ("!(1 == 1) || false", False),
    ],
)  # fmt: skip
def test_conditions(expr: str, value: bool) -> None:
    assert holds(expr, CTX) is value


@pytest.mark.parametrize(
    ("expr", "value"),
    [
        ("steps.nope.replied", False),  # a missing field is falsy
        ("not steps.nope.replied", True),
        ("steps.nope == 'x'", False),
        ("steps.nope != 'x'", True),  # missing equals nothing
        ("steps.nope > 1", False),
        ("steps.nope < 1", False),
        ("steps.nope in ['x']", False),
        ("items[9] == 1", False),
        ("contact.tags.x == 1", False),  # field access on a list
        ("'1' == 1", False),  # no coercion
        ("1 == 1.0", True),
        ("true == 1", False),
        ("null == null", True),
        ("items < 3", False),  # lists do not order
    ],
)
def test_missing_and_mismatched_values_never_pass_silently(expr: str, value: bool) -> None:
    assert holds(expr, CTX) is value


@pytest.mark.parametrize(
    "bad",
    [
        "size(items) > 1",  # no function calls
        "ticket.priority in ['p1'",
        "a ==",
        "a = 1",
        "a.",
        "__import__('os')",
        "a; b",
        "(" * 40 + "1" + ")" * 40,  # too deep
        "x" * 2000,
    ],
)
def test_invalid_conditions(bad: str) -> None:
    assert check(bad) is not None
    with pytest.raises(CelError):
        holds(bad, CTX)


def test_evaluate_and_roots() -> None:
    assert evaluate("[1, 'a', true]", {}) == [1, "a", True]
    assert evaluate("'it\\'s'", {}) == "it's"
    assert roots("steps.a.b == var.x and not contact.verified") == {"steps", "var", "contact"}
    assert check("steps.wait.replied") is None
