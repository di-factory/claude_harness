"""The headless service (M2.3): channels, triggers, inbox, escalation, admin API."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from dif_general_harness.channels.telegram import webhook_secret
from dif_general_harness.core.messages import Message
from dif_general_harness.service import create_app
from dif_general_harness.service.headless import FALLBACK, render_event
from tests.support import (
    ADMIN_H,
    ANA,
    API_TOKEN,
    HOOK,
    TG_TOKEN,
    Env,
    calls,
    whatsapp,
)

# --- channels ----------------------------------------------------------------------


async def test_gateway_message_is_verified_queued_answered_and_deduplicated(tmp_path: Path) -> None:
    env = Env(tmp_path, [Message.assistant("Hola Ana, ¿en qué te ayudo?")])
    inst, headless, client = await env.open()
    async with inst, client:
        body, headers = whatsapp("Hola, soy Ana", "SM001")
        r = await client.post("/channels/whatsapp", content=body, headers=headers)
        assert r.status_code == 200 and r.text == "<Response/>"
        again = await client.post("/channels/whatsapp", content=body, headers=headers)
        assert again.status_code == 200  # a gateway retry of the same message
        assert await headless.worker().drain() == 1  # answered once

        [out] = env.texts("twilio")
        assert out["To"] == f"whatsapp:{ANA}" and out["From"] == "whatsapp:+15550001111"
        assert out["Body"] == "Hola Ana, ¿en qué te ayudo?"  # names may show (reveal_in_output)
        # the model never saw the name or the phone
        wire = json.dumps(
            [[m.model_dump(mode="json") for m in req.messages] for req in env.provider.requests]
        )
        assert "Ana" not in wire and "5512345678" not in wire

        bad = await client.post(
            "/channels/whatsapp", content=body, headers={**headers, "x-twilio-signature": "forged"}
        )
        assert bad.status_code == 401
        assert (await client.post("/channels/nope", content=b"")).status_code == 400
        audit = await inst.audit.records(inst.scope, action="message_out")
        assert len(audit) == 1


async def test_conversation_continues_within_the_window(tmp_path: Path) -> None:
    env = Env(tmp_path, [Message.assistant("¡Hola!"), Message.assistant("Claro.")])
    inst, headless, client = await env.open()
    async with inst, client:
        for i, text in enumerate(["Hola", "¿Me ayudas?"]):
            body, headers = whatsapp(text, f"SM{i}")
            await client.post("/channels/whatsapp", content=body, headers=headers)
            await headless.worker().drain()
        sessions = await inst.store.list_sessions(inst.scope)
        assert len(sessions) == 1
        assert len(env.provider.requests[1].messages) == 3  # the second turn sees the first


async def test_opt_out_revokes_consent(tmp_path: Path) -> None:
    env = Env(tmp_path, [Message.assistant("Listo, ya no recibirás recordatorios.")])
    inst, headless, client = await env.open()
    async with inst, client:
        await inst.consent.set(inst.scope, ANA, "whatsapp", "granted", "clinic import")
        body, headers = whatsapp("BAJA", "SM9")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        assert await inst.consent.get(inst.scope, ANA, "whatsapp") == "revoked"
        assert not await inst.consent.may_contact(inst.scope, ANA, "whatsapp", required=True)
        assert [r.action for r in await inst.audit.records(inst.scope, action="consent_revoked")]


async def test_api_channel_answers_inline(tmp_path: Path) -> None:
    env = Env(tmp_path, [Message.assistant("Your appointment is on Monday.")])
    inst, _, client = await env.open()
    async with inst, client:
        payload = {"contact": "ana@example.com", "text": "When is my appointment?"}
        r = await client.post(
            "/channels/api", json=payload, headers={"authorization": f"Bearer {API_TOKEN}"}
        )
        assert r.status_code == 200
        assert r.json()["replies"][0]["reply"] == "Your appointment is on Monday."
        denied = await client.post(
            "/channels/api", json=payload, headers={"authorization": "Bearer nope"}
        )
        assert denied.status_code == 401
        assert (await client.post("/channels/api", json=payload)).status_code == 401


async def test_telegram_parse_and_secret(tmp_path: Path) -> None:
    env = Env(tmp_path, [])
    inst, headless, _ = await env.open()
    async with inst:
        from dif_general_harness.channels import Inbound, Unauthorized

        update = {
            "message": {
                "message_id": 5,
                "chat": {"id": 999},
                "from": {"first_name": "Jag"},
                "text": "hola",
            }
        }
        good = Inbound(
            "u",
            {"x-telegram-bot-api-secret-token": webhook_secret(TG_TOKEN)},
            json.dumps(update).encode(),
        )
        [envl] = headless.adapters["staff"].parse(good)
        assert (envl.contact_key, envl.text, envl.names, envl.message_id) == (
            "999",
            "hola",
            ["Jag"],
            "999:5",
        )
        with pytest.raises(Unauthorized):
            headless.adapters["staff"].parse(Inbound("u", {}, good.body))


async def test_unsupported_parts_are_reported(tmp_path: Path) -> None:
    env = Env(tmp_path, [])
    inst, headless, _ = await env.open()
    async with inst:
        codes = {(i.code, i.path) for i in headless.issues}
        assert ("channel_unavailable", "channels.mail") in codes
        assert ("trigger_unavailable", "triggers.report") in codes
        assert set(headless.triggers) == {"morning", "lead"}


# --- approvals and escalation ------------------------------------------------------


async def test_approval_is_deferred_then_executed(tmp_path: Path) -> None:
    script = [
        calls(("w1", "notes.write", {"key": "cita", "text": "mover a lunes"})),
        Message.assistant("Tu solicitud está pendiente de aprobación."),
        Message.assistant("¡Listo! Ya quedó registrado."),
    ]
    env = Env(tmp_path, script)
    inst, headless, client = await env.open()
    async with inst, client:
        body, headers = whatsapp("Anota que muevo mi cita a lunes", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()

        # the call was deferred, the staff channel was told, the contact got the pending note
        result = env.provider.requests[1].messages[-1].content[0]
        assert result.status == "denied" and "waiting for approval" in (result.error or "")
        [staff] = env.texts("telegram")
        assert staff["chat_id"] == "999" and "Approval needed: notes.write" in staff["text"]
        assert env.texts("twilio")[-1]["Body"] == "Tu solicitud está pendiente de aprobación."

        r = await client.get("/admin/inbox", headers=ADMIN_H)
        [item] = r.json()
        assert item["kind"] == "approval" and item["payload"]["tool"] == "notes.write"

        r = await client.post(
            f"/admin/inbox/{item['id']}/decision",
            json={"approved": True, "by": "jag"},
            headers=ADMIN_H,
        )
        assert r.json() == {"id": item["id"], "status": "approved"}
        again = await client.post(
            f"/admin/inbox/{item['id']}/decision", json={"approved": False}, headers=ADMIN_H
        )
        assert again.status_code == 409  # the first decision wins
        await headless.worker().drain()

        assert (tmp_path / "state" / "acme" / "acme-desk" / "notes.json").exists()
        assert "granted" in env.provider.requests[2].messages[-1].text()
        assert env.texts("twilio")[-1]["Body"] == "¡Listo! Ya quedó registrado."
        actions = [r.action for r in await inst.audit.records(inst.scope)]
        assert "inbox_approved" in actions and "approved_action" in actions


async def test_approval_times_out(tmp_path: Path) -> None:
    script = [
        calls(("w1", "notes.write", {"key": "k", "text": "t"})),
        Message.assistant("Pendiente."),
        Message.assistant("No fue aprobado, lo siento."),
    ]
    env = Env(tmp_path, script)
    inst, headless, client = await env.open()
    async with inst, client:
        body, headers = whatsapp("anota algo", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        env.clock.now += 2 * 3600 + 1
        await headless.worker().drain()
        [item] = await inst.inbox.list(status=None)
        assert item.status == "denied" and item.decided_by == "system:timeout"
        assert not (tmp_path / "state" / "acme" / "acme-desk" / "notes.json").exists()
        assert env.texts("twilio")[-1]["Body"] == "No fue aprobado, lo siento."


async def test_escalation_holds_the_conversation_for_a_person(tmp_path: Path) -> None:
    script = [
        calls(("h1", "handoff.human", {"reason": "patient asks for medical advice"})),
        Message.assistant("Te comunico con una persona del equipo."),
        Message.assistant("De nada, Ana."),
    ]
    env = Env(tmp_path, script)
    inst, headless, client = await env.open()
    async with inst, client:
        body, headers = whatsapp("¿Qué medicina tomo?", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        [item] = await inst.inbox.list(kind="escalation")
        assert "medical advice" in item.title
        before = len(env.provider.requests)

        body, headers = whatsapp("¿Sigues ahí?", "SM2")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        assert len(env.provider.requests) == before  # held: the agent does not answer
        assert any("New message in an escalated" in t["text"] for t in env.texts("telegram"))

        view = (await client.get(f"/admin/sessions/{item.session_id}", headers=ADMIN_H)).json()
        assert view["state"] == "escalated" and view["messages"][-1]["text"] == "¿Sigues ahí?"
        r = await client.post(
            f"/admin/sessions/{item.session_id}/reply",
            json={"text": "Hola Ana, soy la Dra. López.", "by": "dra"},
            headers=ADMIN_H,
        )
        assert (
            r.status_code == 200
            and env.texts("twilio")[-1]["Body"] == "Hola Ana, soy la Dra. López."
        )

        await client.post(
            f"/admin/inbox/{item.id}/decision",
            json={"approved": True, "by": "dra"},
            headers=ADMIN_H,
        )
        body, headers = whatsapp("Gracias", "SM3")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        assert env.texts("twilio")[-1]["Body"] == "De nada, Ana."  # the agent is back


async def test_failed_turn_sends_a_fallback_and_files_an_item(tmp_path: Path) -> None:
    env = Env(tmp_path, [RuntimeError("provider down")])
    inst, headless, client = await env.open()
    async with inst, client:
        body, headers = whatsapp("hola", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        assert env.texts("twilio")[-1]["Body"] == FALLBACK
        [item] = await inst.inbox.list(kind="escalation")
        assert "error" in item.title


# --- triggers ----------------------------------------------------------------------


async def test_schedule_trigger_runs_and_reschedules(tmp_path: Path) -> None:
    env = Env(tmp_path, [Message.assistant("All good."), Message.assistant("All good again.")])
    inst, headless, _ = await env.open()
    async with inst:
        await headless.start()
        await headless.start()  # a restart seeds nothing twice
        rows = await inst.db.fetchall("SELECT run_at FROM jobs WHERE kind = 'trigger'")
        assert len(rows) == 1
        first = rows[0]["run_at"]
        env.clock.now = first
        assert await headless.worker().drain() >= 1
        assert env.provider.requests[0].messages[0].text() == "Morning check."
        pending = await inst.db.fetchall(
            "SELECT run_at FROM jobs WHERE kind = 'trigger' AND status = 'queued'"
        )
        assert [r["run_at"] - first for r in pending] == [86400]
        assert [r.actor for r in await inst.audit.records(inst.scope, action="trigger_run")] == [
            "trigger:morning"
        ]


async def test_webhook_trigger(tmp_path: Path) -> None:
    env = Env(tmp_path, [Message.assistant("Lead logged.")])
    inst, headless, client = await env.open()
    async with inst, client:
        body = json.dumps({"lead": {"name": "Luis", "source": "web"}}).encode()
        sig = "sha256=" + hmac.new(HOOK.encode(), body, hashlib.sha256).hexdigest()
        headers = {
            "x-hub-signature-256": sig,
            "x-delivery-id": "d1",
            "content-type": "application/json",
        }
        r = await client.post("/hooks/hooks/crm", content=body, headers=headers)
        assert r.status_code == 202 and r.json() == {"queued": True, "duplicate": False}
        dup = await client.post("/hooks/hooks/crm", content=body, headers=headers)
        assert dup.json()["duplicate"] is True
        forged = await client.post(
            "/hooks/hooks/crm", content=body, headers={**headers, "x-hub-signature-256": "sha256=0"}
        )
        assert forged.status_code == 401
        assert (await client.post("/hooks/nope", content=body)).status_code == 404
        await headless.worker().drain()
        # a name the detectors cannot know (no profile, no introduction) passes as text
        assert env.provider.requests[0].messages[0].text() == "New lead Luis from web"


def test_render_event() -> None:
    event = {"lead": {"name": "Luis", "tags": ["a"]}}
    assert (
        render_event("{{event.lead.name}} {{event.lead.tags}} {{event.nope}}", event)
        == 'Luis ["a"] '
    )


# --- admin ---------------------------------------------------------------------------


async def test_admin_api(tmp_path: Path) -> None:
    env = Env(tmp_path, [Message.assistant("Hi.")])
    inst, headless, client = await env.open()
    async with inst, client:
        assert (await client.get("/admin/inbox")).status_code == 401
        assert (
            await client.get("/admin/inbox", headers={"authorization": "Bearer x"})
        ).status_code == 401
        health = (await client.get("/healthz")).json()
        assert health["tenant"] == "acme" and health["config_version"] == inst.resolved.version_hash
        assert (await client.get("/readyz")).json() == {"status": "ready"}

        r = await client.post(
            "/admin/consent",
            json={"contact": ANA, "channel": "whatsapp", "status": "granted"},
            headers=ADMIN_H,
        )
        assert r.json() == {"status": "granted"}
        assert await inst.consent.get(inst.scope, ANA, "whatsapp") == "granted"
        assert (
            await client.post(
                "/admin/consent",
                json={"contact": ANA, "channel": "whatsapp", "status": "maybe"},
                headers=ADMIN_H,
            )
        ).status_code == 422

        body, headers = whatsapp("hola", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        assert (await client.get("/admin/jobs", headers=ADMIN_H)).json() == {"queued": 1}
        await headless.worker().drain()
        assert (await client.get("/admin/audit/verify", headers=ADMIN_H)).json() == {
            "intact": True,
            "first_broken_seq": None,
        }
        assert (
            "tenant" not in (await client.get("/admin/spend", headers=ADMIN_H)).json()
        )  # fake model: unpriced

        no_admin = create_app(headless, run_worker=False)
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=no_admin), base_url="http://x"
        ) as c:
            assert (await c.get("/admin/inbox", headers=ADMIN_H)).status_code == 403


# --- config versions and the instance agent (M2.4) ---------------------------------


async def test_hot_reload_and_rollback_through_the_admin_api(tmp_path: Path) -> None:
    from dif_general_harness.service.config import boot_config
    from dif_general_harness.tenancy import ConfigStore

    env = Env(tmp_path, [])
    inst, headless, client = await env.open()
    async with client:
        await boot_config(inst.db, inst.scope, inst.resolved)
        store = ConfigStore(inst.db, inst.scope)
        data = json.loads(json.dumps(inst.resolved.data))
        data["values"]["business"] = "ACME Dental Norte"
        v2 = await store.propose(data, "jag", approved=True)
        first = headless.instance

        r = await client.post(f"/admin/config/{v2.version}/activate", headers=ADMIN_H)
        assert r.status_code == 200 and r.json()["active"] == 2
        assert headless.instance is not first  # swapped without a restart
        assert "ACME Dental Norte" in headless.agent("front").system
        health = (await client.get("/healthz")).json()
        assert health["config_version"] == v2.hash

        r = await client.post("/admin/config/rollback", headers=ADMIN_H)
        assert r.json()["active"] == 1 and "ACME Dental." in headless.agent("front").system
        listing = (await client.get("/admin/config", headers=ADMIN_H)).json()
        assert [v["status"] for v in listing["versions"]] == ["active", "retired"]
        assert (await client.post("/admin/config/9/activate", headers=ADMIN_H)).status_code == 409
        actions = [r.action for r in await headless.instance.audit.records(headless.instance.scope)]
        assert (
            "config_activated" in actions
            and "config_rollback" in actions
            and "config_applied" in actions
        )
    await headless.instance.close()


async def test_instance_agent(tmp_path: Path) -> None:
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from dif_general_harness.fleet import InstanceAgent, load_public_key, signed_message
    from dif_general_harness.service.config import boot_config

    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    env = Env(tmp_path, [Message.assistant("hola")])
    inst, headless, client = await env.open()
    offers: list[dict[str, Any] | None] = []
    beats: list[dict[str, Any]] = []

    def control(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == "Bearer fleet-token"
        assert request.url.path.startswith("/v1/instances/acme/acme-desk/")
        if request.url.path.endswith("/heartbeat"):
            beats.append(json.loads(request.content))
            return httpx2.Response(204)
        offer = offers.pop(0) if offers else None
        return httpx2.Response(204) if offer is None else httpx2.Response(200, json=offer)

    agent = InstanceAgent(
        headless,
        control_url="https://control.di-factory.biz",
        token="fleet-token",
        public_key=load_public_key(public),
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(control)),
    )
    async with client:
        await boot_config(inst.db, inst.scope, inst.resolved)
        body, headers = whatsapp("Hola, soy Ana", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()

        report = await agent.heartbeat()
        assert report["tenant"] == "acme" and report["active_version"] == 1
        assert report["audit_intact"] is True and report["jobs"] == {"done": 1}
        assert "channel_unavailable" in report["issues"]
        sent = json.dumps(beats)
        assert ANA not in sent and "5512345678" not in sent and "Ana" not in sent  # aggregates only

        assert await agent.pull() == "current"

        data = json.loads(json.dumps(headless.instance.resolved.data))
        data["values"]["business"] = "ACME Dental Sur"
        good = {
            "data": data,
            "approved_by": "jag",
            "signature": base64.b64encode(key.sign(signed_message(data, "jag"))).decode(),
        }
        forged = {**good, "approved_by": "mallory"}  # the signature covers the approver too
        unsigned = {"data": data, "approved_by": "jag", "signature": ""}
        nobody = {"data": data, "signature": good["signature"]}
        offers += [forged, unsigned, nobody]
        assert await agent.pull() == "rejected: bad signature"
        assert await agent.pull() == "rejected: bad signature"
        assert await agent.pull() == "rejected: an offer needs data and approved_by"
        assert "ACME Dental Sur" not in headless.agent("front").system

        broken = json.loads(json.dumps(data))
        broken["agents"]["front"]["model_role"] = "nope"
        offers.append(
            {
                "data": broken,
                "approved_by": "jag",
                "signature": base64.b64encode(key.sign(signed_message(broken, "jag"))).decode(),
            }
        )
        assert (await agent.pull()).startswith("rejected: invalid spec")

        offers.append(good)
        assert await agent.pull() == "applied v2"
        assert "ACME Dental Sur" in headless.agent("front").system
        await agent.heartbeat()
        assert beats[-1]["active_version"] == 2 and beats[-1]["last_pull"] == "applied v2"
        rejected = await headless.instance.audit.records(
            headless.instance.scope, action="config_rejected"
        )
        assert len(rejected) == 3

    with pytest.raises(ValueError, match="https"):
        InstanceAgent(
            headless,
            control_url="http://control.example",
            token="t",
            public_key=load_public_key(public),
        )
    await headless.instance.close()
