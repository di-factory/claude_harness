"""The M4 gate (operations, v1.0): the PRD acceptance tests that need M4, end to end, offline.

- **Deploy:** one command turns an approved instance into a complete AWS deploy (sizing
  profile, a content-addressed image, the module's own tests), and rollback restores the
  previous config exactly, through the control plane.
- **Audit and cost:** spend sums by tenant and by vendor at list prices, models without a
  price are named, and the telemetry the client may export agrees with the ledger.
- **Fleet:** a change rolls out instance by instance, gated by each instance's evals, and the
  fleet view shows every instance on it.
- **Guardrails that M4 adds:** model calls never leave the allowed regions, and shell
  commands run in a confined container.

(The M1 to M3 gates cover the rest of the PRD list: spec, model swap, tool contract,
permissions, headless, durability, escalation, PII, consent, isolation, memory, knowledge.)
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dif_general_harness.cli import main
from dif_general_harness.constructor.deploy import solution_hash
from dif_general_harness.control import ControlPlane, create_control_app
from dif_general_harness.core.messages import Message, Usage
from dif_general_harness.fleet import InstanceAgent, evaluator, load_public_key
from dif_general_harness.observability.otel import OtlpExporter
from dif_general_harness.policy.budgets import DEFAULT_PRICES, cost_usd
from dif_general_harness.providers import FakeProvider
from dif_general_harness.providers.base import ProviderMessage
from dif_general_harness.runtime import RuntimeOptions
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.store.db import connect
from dif_general_harness.tenancy import EnvSecrets
from dif_general_harness.tools.packs import ContainerExecutor
from tests.support import ADMIN_H, Env, whatsapp

OPUS = "claude-opus-5-5"


def test_one_command_deploy_of_an_approved_instance(
    examples: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    instance = examples / "instances" / "clinica-sonrisa.json"
    packs = ["--packs", str(examples)]
    assert main(["keys", "new", "jag", "--out", str(tmp_path / "keys")]) == 0
    public = capsys.readouterr().out.split('"jag": "')[1].split('"')[0]
    approvers = tmp_path / "approvers.json"
    approvers.write_text(json.dumps({"jag": public}))
    key = str(tmp_path / "keys" / "jag.key")
    assert (
        main(["approve", str(instance), *packs, "--target", "aws", "--key", key, "--by", "jag"])
        == 0
    )
    out = tmp_path / "release"
    assert (
        main(
            [
                "deploy",
                str(instance),
                *packs,
                "--target",
                "aws",
                "--approvers",
                str(approvers),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    tfvars = json.loads((out / "terraform.tfvars.json").read_text())
    digest = solution_hash(out / "solution")[:12]
    assert tfvars["image_tag"] == f"1.0.0-{digest}" and tfvars["size"] == "small"
    assert tfvars["region"] == "mx-central-1"  # the data region holds


async def test_rollback_restores_the_previous_config(tmp_path: Path) -> None:
    db = await connect(f"sqlite:///{tmp_path / 'control.db'}")
    plane = ControlPlane(db, Ed25519PrivateKey.generate())
    inst, headless, client = await Env(tmp_path / "a", []).open()
    try:
        agent = InstanceAgent(
            headless,
            control_url="https://control.test",
            token=await plane.register("acme", "acme-desk", "jag"),
            public_key=load_public_key(plane.public_key()),
            client=_control_client(plane),
        )
        original = headless.instance.resolved
        changed = copy.deepcopy(original.data)
        changed["values"]["business"] = "ACME Dental 2"
        # the running config is a registered version (a deploy registers it on boot)
        from dif_general_harness.tenancy.config_versions import ConfigStore

        store = ConfigStore(inst.db, inst.scope)
        first = await store.propose(original.data, "deploy", "deployed", approved=True)
        await store.activate(first.version)
        await plane.offer_config("acme", "acme-desk", changed, "jag", gate="none")
        assert (await agent.pull()).startswith("applied v")
        assert headless.instance.resolved.data["values"]["business"] == "ACME Dental 2"
        await plane.offer_rollback("acme", "acme-desk", original.version_hash, "jag")
        assert (await agent.pull()) == f"applied rollback to v{first.version}"
        assert headless.instance.resolved.version_hash == original.version_hash
    finally:
        await client.aclose()
        await inst.close()
        await db.close()


def _control_client(plane: ControlPlane) -> httpx2.AsyncClient:
    app = create_control_app(plane, admin_token="control-admin-token-0123")
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="https://control.test"
    )


def _two_vendors(spec: dict[str, Any]) -> None:
    spec["models"]["roles"]["memory_extraction"] = {
        "provider": "openai-compatible",
        "model": "local-7b",
    }
    spec["models"]["providers"]["openai-compatible"] = {"base_url": "http://llm.internal/v1"}
    spec["memory"] = {"layers": ["semantic"], "scope": "contact"}


async def test_costs_sum_by_tenant_and_vendor(tmp_path: Path) -> None:
    spans: list[dict[str, Any]] = []

    def collector(request: httpx2.Request) -> httpx2.Response | None:
        if request.url.host != "otel.test":
            return None
        for rs in json.loads(request.content)["resourceSpans"]:
            for ss in rs["scopeSpans"]:
                spans.extend(ss["spans"])
        return httpx2.Response(200)

    script = [
        ProviderMessage(
            message=Message.assistant("¡Hola! ¿En qué le ayudo?"),
            usage=Usage(input_tokens=1200, output_tokens=60),
            stop_reason="end_turn",
            model=OPUS,
        ),
        ProviderMessage(
            message=Message.assistant('{"facts": []}'),
            usage=Usage(input_tokens=800, output_tokens=10),
            stop_reason="end_turn",
            model="local-7b",
        ),
    ]
    env = Env(tmp_path, script, edit=_two_vendors, routes=collector)
    env.telemetry = OtlpExporter("http://otel.test", client=env.outbound())
    inst, headless, client = await env.open()
    async with inst, client:
        body, headers = whatsapp("Hola", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        env.clock.now += 61  # the memory extraction job, on the second vendor
        await headless.worker().drain()
        report = (await client.get("/admin/costs?by=vendor", headers=ADMIN_H)).json()

    expected = round(cost_usd(Usage(input_tokens=1200, output_tokens=60), DEFAULT_PRICES[OPUS]), 6)
    by_vendor = {r["vendor"]: r for r in report["rows"]}
    assert by_vendor["anthropic"]["usd"] == expected and by_vendor["anthropic"]["calls"] == 1
    assert by_vendor["openai-compatible"]["calls"] == 1  # a self-hosted model: no list price
    assert report["unpriced"] == ["local-7b"] and report["total"]["usd"] == expected
    assert report["tenant"] == "acme"
    traced = sum(
        float(next(iter(a["value"].values())))
        for s in spans
        for a in s["attributes"]
        if a["key"] == "dif.cost_usd" and s["name"].startswith("chat ")
    )
    assert round(traced, 6) == expected  # what the client's telemetry shows agrees


async def test_rollouts_are_gated_and_reach_every_instance(tmp_path: Path) -> None:
    db = await connect(f"sqlite:///{tmp_path / 'control.db'}")
    plane = ControlPlane(db, Ed25519PrivateKey.generate())
    control = _control_client(plane)
    opened = {
        "acme": await Env(tmp_path / "a", []).open(tenant="acme", instance_id="acme-desk"),
        "beta": await Env(tmp_path / "b", []).open(tenant="beta", instance_id="beta-desk"),
    }
    try:
        agents = {}
        for tenant, (inst, headless, _) in opened.items():
            options = RuntimeOptions(
                state_root=tmp_path / f"gate-{tenant}",
                secrets=EnvSecrets({}),
                provider=FakeProvider([]),
            )
            agents[tenant] = InstanceAgent(
                headless,
                control_url="https://control.test",
                token=await plane.register(tenant, inst.scope.instance_id, "jag"),
                public_key=load_public_key(plane.public_key()),
                client=control,
                evaluate=evaluator(options),
            )
        steps = []
        for tenant, (inst, _, _) in opened.items():
            data = copy.deepcopy(inst.resolved.data)
            data.setdefault("policies", {})["budgets"] = {"per_run": {"usd": 0.1}}  # fleet-wide
            steps.append({"tenant": tenant, "instance": inst.scope.instance_id, "data": data})
        rollout = await plane.start_rollout("tighter budgets", "jag", steps)
        for tenant in ("acme", "beta"):
            assert (await agents[tenant].pull()).startswith("applied v")
            await agents[tenant].heartbeat()
        assert (await plane.rollout(rollout["id"]))["status"] == "completed"
        fleet = {f["tenant"]: f for f in await plane.fleet()}
        for tenant, (_, headless, _) in opened.items():
            running = headless.instance.resolved
            assert running.spec.policies.budgets["per_run"]["usd"] == 0.1
            assert fleet[tenant]["running_config"] == running.version_hash
            assert fleet[tenant]["evals"]["passed"] >= 1  # the gate ran the pack's evals
    finally:
        for inst, _, client in opened.values():
            await client.aclose()
            await inst.close()
        await control.aclose()
        await db.close()


def test_regions_and_the_sandbox_hold(examples: Path, tmp_path: Path) -> None:
    instance = examples / "instances" / "clinica-sonrisa.json"
    data = json.loads(instance.read_text())
    data["governance"]["regions"] = {"models": ["mx"]}  # only Mexican processing allowed
    instance.write_text(json.dumps(data))
    resolved = load_instance(instance, PackCatalog(roots=[examples]))
    assert "model_region_violation" in {i.code for i in resolved.issues}
    argv = " ".join(ContainerExecutor(image="sandbox:1").argv("rm -rf /", tmp_path, "x"))
    assert "--network none" in argv and "--read-only" in argv and "--cap-drop ALL" in argv
