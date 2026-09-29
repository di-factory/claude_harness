"""Each validation and merge rule is proven by planting the error it must catch."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.spec import PackCatalog, SpecError, load_instance, load_pack
from dif_general_harness.spec.loader import version_matches

Mutator = Callable[[dict[str, Any]], None]


def _edit(path: Path, mutate: Mutator) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def _codes(pack_dir: Path) -> set[str]:
    return {i.code for i in load_pack(pack_dir).issues if i.severity == "error"}


def _no_address_delivery(d: dict[str, Any]) -> None:
    d["channels"]["founder"].pop("address")
    d["triggers"]["cto-health"]["channel"] = "founder"


PACK_MUTATIONS: list[tuple[str, str, Mutator]] = [
    (
        "pyme-appointment-agent",
        "invalid_python_ref",
        lambda d: d["tools"].update(python=["extensions/slots.py"]),
    ),
    (
        "pyme-appointment-agent",
        "missing_file",
        lambda d: d["tools"].update(python=["extensions.nope:best_slot"]),
    ),
    (
        "conversational-rag",
        "invalid_sync_schedule",
        lambda d: d["knowledge"]["corpora"]["docs"]["sync"].update(schedule="every 4 hours"),
    ),
    (
        "conversational-rag",
        "invalid_not_found",
        lambda d: d["knowledge"]["corpora"]["docs"]["retrieval"].update(not_found="guess"),
    ),
    ("dev-cell", "unknown_workspace", lambda d: d["agents"]["developer"].update(workspace="nope")),
    ("opc-c-suite", "unknown_agent", lambda d: d["triggers"]["cto-health"].update(agent="cfo")),
    ("opc-c-suite", "missing_ledger", lambda d: d.pop("ledger")),
    (
        "pyme-receipt-processing",
        "missing_dedupe",
        lambda d: d["triggers"]["new-document"].pop("dedupe_key"),
    ),
    (
        "pyme-appointment-agent",
        "unknown_model_role",
        lambda d: d["agents"]["receptionist"].update(model_role="subagent"),
    ),
    (
        "pyme-appointment-agent",
        "undeclared_secret",
        lambda d: d["channels"]["whatsapp"].update(credentials={"$secret": "nope"}),
    ),
    (
        "pyme-appointment-agent",
        "undeclared_variable",
        lambda d: d["channels"]["whatsapp"].update(address="{{var.nope}}"),
    ),
    (
        "pyme-appointment-agent",
        "consent_missing",
        lambda d: d["governance"]["consent"].update(required=False),
    ),
    (
        "pyme-appointment-agent",
        "unknown_template",
        lambda d: d["workflows"]["send-reminder"]["steps"][0].update(template="nope"),
    ),
    (
        "pyme-appointment-agent",
        "unknown_check",
        lambda d: d["tools"]["overrides"]["calendar.move_event"].update(verify="nope"),
    ),
    (
        "service-desk-cell",
        "unguarded_external",
        lambda d: d["tools"]["http"]["identity"]["operations"]["reset_password"].pop("verify"),
    ),
    (
        "service-desk-cell",
        "unknown_step",
        lambda d: d["workflows"]["resolve-ticket"]["steps"][2]["cases"][0].update(goto="nope"),
    ),
    (
        "conversational-rag",
        "pii_untokenized",
        lambda d: d["governance"]["pii"].update(tokenize=False),
    ),
    ("conversational-rag", "no_evals", lambda d: d["evals"].update(suites=[])),
    (
        "conversational-rag",
        "unknown_corpus",
        lambda d: d["agents"]["assistant"].update(knowledge=["nope"]),
    ),
    (
        "conversational-rag",
        "missing_file",
        lambda d: d["agents"]["assistant"].update(prompt="prompts/nope.md"),
    ),
    (
        "opc-c-suite",
        "unknown_channel",
        lambda d: d["triggers"]["cto-health"].update(channel="nope"),
    ),
    (
        "opc-c-suite",
        "no_recipient",
        _no_address_delivery,
    ),
    (
        "service-desk-cell",
        "invalid_condition",
        lambda d: d["policies"]["escalation"]["rules"][0].update(when="ticket.priority in ['p1'"),
    ),
    (
        "pyme-receipt-processing",
        "invalid_condition",
        lambda d: d["workflows"]["process-document"]["steps"][1].update(when="size(steps) > 1"),
    ),
    (
        "dev-cell",
        "invalid_permission_rule",
        lambda d: d["policies"]["permissions"]["deny"].append("coding.bash(cmd=x, y)"),
    ),
    (
        "dev-cell",
        "unknown_tool_namespace",
        lambda d: d["agents"]["developer"]["tools"].append("jira.create_issue"),
    ),
]


@pytest.mark.parametrize(
    ("pack", "code", "mutate"), PACK_MUTATIONS, ids=[f"{p}:{c}" for p, c, _ in PACK_MUTATIONS]
)
def test_planted_pack_errors_are_caught(
    examples: Path, pack: str, code: str, mutate: Mutator
) -> None:
    assert _codes(examples / pack) == set()
    _edit(examples / pack / "pack.json", mutate)
    assert code in _codes(examples / pack)


INSTANCE_MUTATIONS: list[tuple[str, Mutator]] = [
    ("missing_value", lambda d: d["values"].pop("business_name")),
    ("unknown_value", lambda d: d["values"].update(nope=1)),
    ("region_violation", lambda d: d["deploy"].update(region="us-east-1")),
    ("safety_weakened", lambda d: d["governance"]["retention"].update(conversations="365d")),
    ("safety_weakened", lambda d: d.setdefault("governance", {}).update(pii={"tokenize": False})),
    ("safety_weakened", lambda d: d["governance"].update(consent={"required": False})),
    ("safety_weakened", lambda d: d["policies"]["budgets"]["per_tenant_day"].update(usd=50)),
    (
        "safety_weakened",
        lambda d: d["governance"].update(regions={"models": ["us", "mx", "eu"]}),
    ),
    ("safety_weakened", lambda d: d["governance"].update(regions={"data": "us"})),
    (
        "model_region_violation",
        lambda d: d["governance"].update(regions={"models": ["mx"]}),
    ),
    (
        "model_region_unknown",
        lambda d: d.update(
            models={
                "roles": {"verifier": {"provider": "openai-compatible", "model": "local-7b"}},
                "providers": {"openai-compatible": {"base_url": "http://llm.internal/v1"}},
            }
        ),
    ),
]


@pytest.mark.parametrize(
    ("code", "mutate"),
    INSTANCE_MUTATIONS,
    ids=[f"{c}:{i}" for i, (c, _) in enumerate(INSTANCE_MUTATIONS)],
)
def test_planted_instance_errors_are_caught(examples: Path, code: str, mutate: Mutator) -> None:
    path = examples / "instances" / "clinica-sonrisa.json"
    _edit(path, mutate)
    resolved = load_instance(path, PackCatalog(roots=[examples]))
    assert code in {i.code for i in resolved.issues if i.severity == "error"}


@pytest.mark.parametrize(
    "overlay",
    [
        {"governance": {"consent": None}},
        {"governance": None},
        {"policies": {"budgets": None}},
        {"policies": {"permissions": {"deny": None}}},
        {"policies": None},
    ],
)
def test_null_cannot_remove_safety_settings(examples: Path, overlay: dict[str, Any]) -> None:
    path = examples / "instances" / "clinica-sonrisa.json"
    _edit(path, lambda d: d.update(overlay))
    resolved = load_instance(path, PackCatalog(roots=[examples]))
    assert "safety_weakened" in {i.code for i in resolved.issues if i.severity == "error"}
    assert resolved.spec.governance.consent.required is True


def test_budget_raise_allowed_with_reason(examples: Path) -> None:
    path = examples / "instances" / "clinica-sonrisa.json"
    _edit(
        path,
        lambda d: d["policies"]["budgets"].update(
            per_tenant_day={"usd": 50, "override_reason": "seasonal campaign"}
        ),
    )
    resolved = load_instance(path, PackCatalog(roots=[examples]))
    assert "safety_weakened" not in {i.code for i in resolved.issues}


def test_deny_rules_are_additive(examples: Path) -> None:
    path = examples / "instances" / "clinica-sonrisa.json"
    _edit(path, lambda d: d["policies"].update(permissions={"deny": ["calendar.move_event"]}))
    resolved = load_instance(path, PackCatalog(roots=[examples]))
    assert set(resolved.spec.policies.permissions.deny) == {
        "calendar.delete_event",
        "calendar.move_event",
    }


def test_null_removes_entry(examples: Path) -> None:
    path = examples / "instances" / "clinica-sonrisa.json"
    _edit(path, lambda d: d.update(policies={"router": None}))
    resolved = load_instance(path, PackCatalog(roots=[examples]))
    assert resolved.spec.policies.router is None


def test_unknown_top_level_key_fails_loudly(examples: Path) -> None:
    _edit(examples / "dev-cell" / "pack.json", lambda d: d.update(agnets={}))
    with pytest.raises(SpecError) as exc:
        load_pack(examples / "dev-cell")
    assert exc.value.issues[0].code == "unknown_key"


def test_schema_errors_are_reported(examples: Path) -> None:
    _edit(
        examples / "dev-cell" / "pack.json",
        lambda d: d["models"]["roles"].update(boss={"provider": "x", "model": "y"}),
    )
    with pytest.raises(SpecError) as exc:
        load_pack(examples / "dev-cell")
    assert any(i.code == "schema" for i in exc.value.issues)


def test_missing_pack_version(examples: Path) -> None:
    path = examples / "instances" / "clinica-sonrisa.json"
    _edit(path, lambda d: d.update(extends=["pyme-appointment-agent@^2.0"]))
    with pytest.raises(SpecError) as exc:
        load_instance(path, PackCatalog(roots=[examples]))
    assert exc.value.issues[0].code == "pack_not_found"


@pytest.mark.parametrize(
    ("version", "rng", "ok"),
    [
        ("1.2.3", None, True),
        ("1.2.3", "^1.0", True),
        ("1.2.3", "^1.3", False),
        ("2.0.0", "^1.0", False),
        ("1.2.3", "1.2.3", True),
        ("1.2.4", "1.2.3", False),
    ],
)
def test_version_ranges(version: str, rng: str | None, ok: bool) -> None:
    assert version_matches(version, rng) is ok
