"""The control plane (M4.6) with real instance agents: fleet view, signed remote config,
eval-gated rollouts instance by instance, automatic rollback, audit."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import httpx2
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dif_general_harness.control import ControlPlane, create_control_app
from dif_general_harness.fleet import InstanceAgent, evaluator, load_public_key
from dif_general_harness.providers import FakeProvider
from dif_general_harness.runtime import RuntimeOptions
from dif_general_harness.store.db import connect
from dif_general_harness.tenancy import EnvSecrets
from tests.support import Env

ADMIN = {"authorization": "Bearer control-admin-token-0123"}
FAILING_SUITE = """id: must-write-a-note
turns:
  - user: "save a note"
  - expect_tool: notes.write
"""


async def _control(tmp_path: Path) -> tuple[ControlPlane, httpx2.AsyncClient]:
    db = await connect(f"sqlite:///{tmp_path / 'control.db'}")
    plane = ControlPlane(db, Ed25519PrivateKey.generate())
    app = create_control_app(plane, admin_token="control-admin-token-0123")
    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="https://control.test"
    )
    return plane, client


def _changed(data: dict[str, Any], business: str, suite: Path | None = None) -> dict[str, Any]:
    out = copy.deepcopy(data)
    out["values"]["business"] = business
    if suite is not None:
        out["evals"]["suites"] = [str(suite)]
    return out


async def test_fleet_offers_and_eval_gated_rollouts(tmp_path: Path) -> None:
    plane, control = await _control(tmp_path)
    envs = {"acme": Env(tmp_path / "a", []), "beta": Env(tmp_path / "b", [])}
    opened = {
        "acme": await envs["acme"].open(tenant="acme", instance_id="acme-desk"),
        "beta": await envs["beta"].open(tenant="beta", instance_id="beta-desk"),
    }
    agents: dict[str, InstanceAgent] = {}
    try:
        for tenant, (inst, headless, _) in opened.items():
            r = await control.post(
                "/v1/admin/instances",
                json={"tenant": tenant, "instance": inst.scope.instance_id, "by": "jag"},
                headers=ADMIN,
            )
            options = RuntimeOptions(
                state_root=tmp_path / f"gate-{tenant}",
                secrets=EnvSecrets({}),
                provider=FakeProvider([]),
            )
            key = (await control.get("/v1/public-key")).json()["public_key"]
            agents[tenant] = InstanceAgent(
                headless,
                control_url="https://control.test",
                token=r.json()["token"],
                public_key=load_public_key(key),
                client=control,
                evaluate=evaluator(options),
            )
            await agents[tenant].heartbeat()

        fleet = (await control.get("/v1/admin/fleet", headers=ADMIN)).json()
        assert [(f["tenant"], f["healthy"]) for f in fleet] == [("acme", True), ("beta", True)]
        assert fleet[0]["running_config"] == opened["acme"][0].resolved.version_hash
        assert "metrics" not in fleet[0] and fleet[0]["escalation_rate"] is None  # aggregates

        # an instance's token opens only its own path; the admin API needs the admin token
        stolen = {"authorization": f"Bearer {agents['acme'].token}"}
        assert (
            await control.get("/v1/instances/beta/beta-desk/config", headers=stolen)
        ).status_code == 401
        assert (await control.get("/v1/admin/fleet", headers=stolen)).status_code == 401

        # a remote config change: signed, eval-gated, applied through the versioned config
        acme, acme_headless, _ = opened["acme"]
        before = acme.resolved.version_hash
        offer = await control.post(
            "/v1/admin/instances/acme/acme-desk/offers",
            json={"data": _changed(acme.resolved.data, "ACME Dental Norte"), "approved_by": "jag"},
            headers=ADMIN,
        )
        assert offer.status_code == 200, offer.text
        assert (await agents["acme"].pull()).startswith("applied v")
        assert acme_headless.instance.resolved.data["values"]["business"] == "ACME Dental Norte"
        [done] = (
            await control.get("/v1/admin/instances/acme/acme-desk/offers", headers=ADMIN)
        ).json()
        assert done["status"] == "applied" and done["result"]["evals"]["ran"] == 1
        assert await agents["acme"].pull() == "current"
        wrong = await control.post(
            "/v1/admin/instances/beta/beta-desk/offers",
            json={"data": acme.resolved.data, "approved_by": "jag"},
            headers=ADMIN,
        )
        assert wrong.status_code == 400 and "not beta/beta-desk" in wrong.text

        # a rollout: acme upgrades, beta's evals fail, so beta is not changed and acme rolls back
        failing = tmp_path / "failing.yaml"
        failing.write_text(FAILING_SUITE)
        running = {t: opened[t][1].instance.resolved for t in opened}
        rollout = (
            await control.post(
                "/v1/admin/rollouts",
                json={
                    "name": "desk 1.1",
                    "approved_by": "jag",
                    "steps": [
                        {
                            "tenant": "acme",
                            "instance": "acme-desk",
                            "data": _changed(running["acme"].data, "ACME Dental 1.1"),
                        },
                        {
                            "tenant": "beta",
                            "instance": "beta-desk",
                            "data": _changed(running["beta"].data, "BETA Dental 1.1", failing),
                        },
                    ],
                },
                headers=ADMIN,
            )
        ).json()
        assert rollout["status"] == "running" and rollout["position"] == 0
        assert await agents["beta"].pull() == "current"  # beta waits for acme
        applied_hash = opened["acme"][1].instance.resolved.version_hash
        assert (await agents["acme"].pull()).startswith("applied v")
        assert opened["acme"][1].instance.resolved.data["values"]["business"] == "ACME Dental 1.1"

        outcome = await agents["beta"].pull()
        assert outcome.startswith("rejected: evals did not pass")
        assert opened["beta"][1].instance.resolved.data["values"]["business"] == "BETA Dental"
        state = (await control.get(f"/v1/admin/rollouts/{rollout['id']}", headers=ADMIN)).json()
        assert state["status"] == "rolled_back" and "evals did not pass" in state["error"]
        assert [s["status"] for s in state["steps"]] == ["rolling_back", "rejected"]

        assert (await agents["acme"].pull()).startswith("applied rollback to v")
        back = opened["acme"][1].instance.resolved
        assert back.version_hash == applied_hash  # exactly what ran before the rollout
        assert back.data["values"]["business"] == "ACME Dental Norte"
        assert before != applied_hash

        audit = (await control.get("/v1/admin/audit", headers=ADMIN)).json()
        actions = [r["action"] for r in audit["records"]]
        assert audit["intact"] and "rollout_rolled_back" in actions and "offer_rejected" in actions
        audited = await opened["beta"][0].audit.records(
            opened["beta"][0].scope, action="config_rejected"
        )
        assert audited and audited[-1].subject == "evals"
    finally:
        for inst, _, client in opened.values():
            await client.aclose()
            await inst.close()
        await control.aclose()
        await plane.db.close()


async def test_tampered_offers_are_refused(tmp_path: Path) -> None:
    plane, control = await _control(tmp_path)
    inst, headless, client = await Env(tmp_path / "a", []).open()
    try:
        token = await plane.register("acme", "acme-desk", "jag")
        agent = InstanceAgent(
            headless,
            control_url="https://control.test",
            token=token,
            public_key=load_public_key(plane.public_key()),
            client=control,
        )
        offer = await plane.offer_config(
            "acme", "acme-desk", _changed(inst.resolved.data, "X"), "jag", gate="none"
        )
        wire = offer.wire()
        assert await agent._apply({**wire, "gate": "none", "approved_by": "mallory"}) == (
            "rejected: bad signature"
        )
        gated = await plane.offer_config(
            "acme", "acme-desk", _changed(inst.resolved.data, "Y"), "jag", gate="evals"
        )
        dropped = {**gated.wire(), "gate": "none"}  # stripping the gate breaks the signature
        assert await agent._apply(dropped) == "rejected: bad signature"
        no_evaluator = await agent._apply(gated.wire())
        assert (
            no_evaluator
            == "rejected: evals did not pass (evals are not available on this instance)"
        )
    finally:
        await client.aclose()
        await inst.close()
        await control.aclose()
        await plane.db.close()
