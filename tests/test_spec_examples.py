"""The seven example packs and the clinic instance are the spec's living fixtures."""

from __future__ import annotations

import pytest

from dif_general_harness.cli import main
from dif_general_harness.spec import PackCatalog, load_instance, load_pack

from .conftest import EXAMPLES, PACK_IDS


@pytest.mark.parametrize("pack_id", PACK_IDS)
def test_every_example_pack_is_valid(pack_id: str) -> None:
    resolved = load_pack(EXAMPLES / pack_id)
    errors = [i for i in resolved.issues if i.severity == "error"]
    assert errors == []
    assert resolved.spec.solution.id == pack_id
    assert resolved.spec.solution.locale == "en"
    # the only warnings allowed in the examples are eval files not written yet
    assert {i.code for i in resolved.issues} <= {"eval_missing"}


def test_clinic_instance_resolves() -> None:
    resolved = load_instance(
        EXAMPLES / "instances" / "clinica-sonrisa.json", PackCatalog(roots=[EXAMPLES])
    )
    assert resolved.ok, [str(i) for i in resolved.issues]
    data = resolved.data
    # pack variables are interpolated with the instance's values, keeping types
    assert data["triggers"]["reminder"]["offset"] == "-24h"
    assert data["tools"]["config"]["connectors/google-calendar"]["calendar_ids"] == [
        "dra-lopez@example.com",
        "dr-ramirez@example.com",
    ]
    # the instance's Spanish template overrides the pack's English one; the template id survives
    reminder = data["channels"]["whatsapp"]["templates"]["reminder"]
    assert reminder["file"].endswith("instances/clinica-sonrisa/tpl_reminder.es-MX.md")
    assert reminder["provider_template_id"] == "HXXXXXXXXXXXXXXXX1"
    # safety settings can only tighten
    assert data["governance"]["retention"]["conversations"] == "90d"
    assert data["policies"]["budgets"]["per_tenant_day"]["usd"] == 3
    assert data["solution"]["locale"] == "es-MX"
    assert resolved.spec.tenant is not None and resolved.spec.tenant.id == "clinica-sonrisa"
    assert len(resolved.version_hash) == 16


def test_version_hash_is_stable() -> None:
    catalog = PackCatalog(roots=[EXAMPLES])
    path = EXAMPLES / "instances" / "clinica-sonrisa.json"
    assert load_instance(path, catalog).version_hash == load_instance(path, catalog).version_hash


def test_cli_validate(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["spec", "validate", str(EXAMPLES / "dev-cell")]) == 0
    assert "OK: pack dev-cell" in capsys.readouterr().out
    assert main(["spec", "validate", str(EXAMPLES / "instances" / "clinica-sonrisa.json")]) == 0
