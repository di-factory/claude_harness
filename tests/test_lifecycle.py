"""Constructor v3 (M4.7): adjust, upgrade, and operating instances through the control plane."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx2
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dif_general_harness.cli import main
from dif_general_harness.constructor.lifecycle import (
    ControlClient,
    FleetError,
    adjust,
    container_data,
    upgrade,
    write_token,
)
from dif_general_harness.control import ControlPlane, create_control_app
from dif_general_harness.spec import PackCatalog
from dif_general_harness.store.db import connect


def _clinic(examples: Path) -> tuple[Path, PackCatalog]:
    return examples / "instances" / "clinica-sonrisa.json", PackCatalog(roots=[examples])


def test_adjust_is_validated_diffed_and_only_then_written(examples: Path) -> None:
    path, catalog = _clinic(examples)
    original = path.read_text()
    plan = adjust(path, catalog, {"reminder_hours": 48})
    assert plan.ok and plan.before.version_hash != plan.after.version_hash
    changed = {c.path: (c.before, c.after) for c in plan.changes}
    assert changed["values.reminder_hours"] == (24, 48)
    assert changed["triggers.reminder.offset"] == (
        "-24h",
        "-48h",
    )  # what it means, not just the value
    assert path.read_text() == original  # nothing written yet
    plan.write()
    assert json.loads(path.read_text())["values"]["reminder_hours"] == 48

    too_big = adjust(path, catalog, {"reminder_hours": 500})
    assert not too_big.ok and too_big.errors()
    with pytest.raises(ValueError, match="does not validate"):
        too_big.write()
    missing = adjust(path, catalog, {"business_name": None})
    assert missing.missing == ["business_name"] and not missing.ok


def test_upgrade_reports_new_values_the_pack_needs(examples: Path) -> None:
    path, _ = _clinic(examples)
    newer = examples / "pyme-appointment-agent-1.1"
    shutil.copytree(examples / "pyme-appointment-agent", newer)
    pack = json.loads((newer / "pack.json").read_text())
    pack["solution"]["version"] = "1.1.0"
    pack["variables"]["clinic_phone"] = {"type": "string", "required": True}
    (newer / "pack.json").write_text(json.dumps(pack))
    catalog = PackCatalog(roots=[examples])

    plan = upgrade(path, catalog, "pyme-appointment-agent", "1.1.0")
    assert plan.missing == ["clinic_phone"] and not plan.ok
    assert plan.data["extends"] == ["pyme-appointment-agent@^1.1"]
    with pytest.raises(ValueError, match="does not extend"):
        upgrade(path, catalog, "dev-cell", "1.0.0")

    data = json.loads(path.read_text())  # give the new value along with the new pack
    data["extends"] = plan.data["extends"]
    path.write_text(json.dumps(data))
    ready = adjust(path, catalog, {"clinic_phone": "+52 55 5555 0000"})
    assert ready.ok, ready.errors()


def test_cli_adjust_and_upgrade(examples: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path, _ = _clinic(examples)
    packs = ["--packs", str(examples)]
    assert main(["adjust", str(path), *packs, "--set", "reminder_hours=36", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert '~ triggers.reminder.offset: "-24h" -> "-36h"' in out and "dry run" in out
    assert json.loads(path.read_text())["values"]["reminder_hours"] == 24
    assert main(["adjust", str(path), *packs, "--set", "reminder_hours=0"]) == 1
    assert "NOT WRITTEN" in capsys.readouterr().out
    assert main(["adjust", str(path), *packs, "--set", "nonsense"]) == 2
    assert (
        main(["upgrade", str(path), *packs, "--pack", "pyme-appointment-agent", "--to", "9.0.0"])
        == 2
    )


def test_offers_carry_the_containers_view(examples: Path) -> None:
    path, catalog = _clinic(examples)
    data = container_data(path, catalog)
    prompt = data["agents"]["receptionist"]["prompt"]
    assert prompt == "/app/solution/packs/pyme-appointment-agent/prompts/receptionist.md"
    text = json.dumps(data)
    assert str(examples) not in text and "/tmp/" not in text  # nothing of this machine


async def test_the_constructor_drives_the_control_plane(examples: Path, tmp_path: Path) -> None:
    db = await connect(f"sqlite:///{tmp_path / 'control.db'}")
    plane = ControlPlane(db, Ed25519PrivateKey.generate())
    app = create_control_app(plane, admin_token="control-admin-token-0123")
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="https://cp.test")
    try:
        client = ControlClient("https://cp.test", "control-admin-token-0123", client=http)
        path, catalog = _clinic(examples)
        token = await client.register("clinica-sonrisa", "clinica-sonrisa-appointments", "jag")
        stored = write_token(
            tmp_path / "fleet", "clinica-sonrisa", "clinica-sonrisa-appointments", token
        )
        assert stored.stat().st_mode & 0o777 == 0o600 and stored.read_text() == token
        offer = await client.offer(
            "clinica-sonrisa",
            "clinica-sonrisa-appointments",
            container_data(path, catalog),
            "jag",
            "evals",
        )
        assert offer["status"] == "offered" and offer["gate"] == "evals"
        rollout = await client.rollout(
            "clinic 1.0.1",
            "jag",
            [
                {
                    "tenant": "clinica-sonrisa",
                    "instance": "clinica-sonrisa-appointments",
                    "data": container_data(path, catalog),
                }
            ],
            "evals",
        )
        assert rollout["status"] == "running"
        assert (await client.rollout_rollback(rollout["id"], "jag"))["status"] == "rolled_back"
        [row] = await client.fleet()
        assert row["tenant"] == "clinica-sonrisa" and row["healthy"] is False  # never reported
        with pytest.raises(FleetError, match="HTTP 400"):
            await client.offer("clinica-sonrisa", "nope", {}, "jag", "evals")
        with pytest.raises(FleetError, match="https"):
            ControlClient("http://cp.example.com", "x")
        bad = ControlClient("https://cp.test", "wrong-token-0123456789", client=http)
        with pytest.raises(FleetError, match="HTTP 401"):
            await bad.fleet()
    finally:
        await http.aclose()
        await db.close()
