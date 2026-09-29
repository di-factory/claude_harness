"""M2 gate: the PRD's template-level acceptance tests that M2 can prove, end to end, offline.

- Headless: a scheduled trigger and an inbound channel message each run a turn and reply
  through the channel.
- Durability: a worker killed mid-job; after a restart the job is resumed and done once.
- Escalation: an escalated conversation arrives in the inbox with transcript and tool trace
  (plan and verification verdict come with M3's verification).
- PII: planted names, phones and CURP never appear in model inputs; replies show them only
  where allowed.
- Consent: an opted-out contact receives no triggered messages.
- Isolation: tenant A's data never appears for tenant B, even in one shared database.
- Audit and cost: every side effect has an audit record; costs sum by tenant and vendor.
- Deploy: rollback restores the previous config (tests/test_config.py); the one-command AWS
  deploy is generated and validated (terraform validate, a real image build) but applying it
  needs a client account, so it is not part of this offline gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.core.messages import Message, Usage
from dif_general_harness.providers.base import ProviderMessage
from dif_general_harness.service import Headless
from tests.support import ADMIN_H, ANA, Env, calls, whatsapp

CURP = "GOMA850312HDFRRN09"


def _deliver(spec: dict[str, Any]) -> None:
    """A daily brief to the staff channel and a reminder to a patient, both scheduled."""
    spec["triggers"]["morning"]["channel"] = "staff"
    spec["triggers"]["remind-ana"] = {
        "type": "schedule",
        "cron": "0 10 * * *",
        "agent": "ops",
        "input": "Write Ana's reminder.",
        "channel": "whatsapp",
        "to": ANA,
    }


def _wire(provider: Any) -> str:
    return json.dumps(
        [[m.model_dump(mode="json") for m in r.messages] + [r.system] for r in provider.requests],
        ensure_ascii=False,
    )


async def _fire_all(headless: Headless, env: Env) -> None:
    rows = await headless.instance.db.fetchall(
        "SELECT run_at FROM jobs WHERE kind = 'trigger' AND status = 'queued'"
    )
    env.clock.now = max(r["run_at"] for r in rows)
    await headless.worker().drain()


async def test_headless_trigger_and_channel_reply_through_the_channel(tmp_path: Path) -> None:
    replies = ["Brief: 2 tickets open.", "Recordatorio: cita mañana.", "¡Hola! ¿En qué te ayudo?"]
    env = Env(tmp_path, [Message.assistant(r) for r in replies], edit=_deliver)
    inst, headless, client = await env.open()
    async with inst, client:
        await inst.consent.set(inst.scope, ANA, "whatsapp", "granted", "clinic import")
        await headless.start()
        await _fire_all(headless, env)
        staff = env.texts("telegram")
        assert (
            staff and staff[0]["chat_id"] == "999" and staff[0]["text"] == "Brief: 2 tickets open."
        )
        reminder = env.texts("twilio")
        assert (
            reminder[0]["To"] == f"whatsapp:{ANA}"
            and reminder[0]["Body"] == "Recordatorio: cita mañana."
        )

        body, headers = whatsapp("Hola", "SM1")
        assert (
            await client.post("/channels/whatsapp", content=body, headers=headers)
        ).status_code == 200
        await headless.worker().drain()
        assert env.texts("twilio")[-1]["Body"] == "¡Hola! ¿En qué te ayudo?"


async def test_durability_after_a_crash(tmp_path: Path) -> None:
    env = Env(tmp_path, [Message.assistant("Respuesta única.")])
    inst, headless, client = await env.open()
    async with client:
        body, headers = whatsapp("Hola", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        job = await headless.queue.claim()  # a worker takes the message...
        assert job is not None and job.kind == "inbound"
    await inst.close()  # ...and the process dies before finishing it

    env.clock.now += 301  # past the lease
    restarted, headless2, _ = await env.open()  # a new process on the same database
    async with restarted:
        assert await headless2.worker().drain() == 1
        assert [t["Body"] for t in env.texts("twilio")] == ["Respuesta única."]
        assert await headless2.queue.counts(restarted.scope) == {"done": 1}


async def test_escalation_reaches_the_inbox_with_context(tmp_path: Path) -> None:
    script = [
        calls(("n1", "notes.list", {})),
        calls(("h1", "handoff.human", {"reason": "the patient reports severe pain"})),
        Message.assistant("Te comunico con una persona."),
    ]
    env = Env(tmp_path, script)
    inst, headless, client = await env.open()
    async with inst, client:
        body, headers = whatsapp("Me duele mucho la muela", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        [item] = (await client.get("/admin/inbox?kind=escalation", headers=ADMIN_H)).json()
        payload = item["payload"]
        assert payload["reason"] == "the patient reports severe pain"
        assert payload["transcript"][0] == {"role": "user", "text": "Me duele mucho la muela"}
        assert [t["tool"] for t in payload["tool_trace"]] == ["notes.list", "handoff.human"]
        assert payload["tool_trace"][0]["status"] == "ok"
        assert any("Escalation" in t["text"] for t in env.texts("telegram"))


async def test_pii_never_reaches_the_model(tmp_path: Path) -> None:
    def curp_too(spec: dict[str, Any]) -> None:
        spec["governance"]["pii"]["classes"] += ["curp", "email"]

    reply = "Gracias, Ana. Registré tu CURP {curp} y tu tel {phone}."
    env = Env(tmp_path, [], edit=curp_too)
    inst, headless, client = await env.open()
    async with inst, client:
        text = f"Soy Ana Gómez, mi cel 55 1234 5678, CURP {CURP}, correo ana@example.mx"
        safe = await inst.pii.tokenize(text, ["Ana"])
        curp_token = next(w for w in safe.replace(",", " ").split() if w.startswith("<CURP_"))
        phone_token = next(w for w in safe.replace(",", " ").split() if w.startswith("<PHONE_"))
        name_token = next(w for w in safe.replace(",", " ").split() if w.startswith("<NAME_"))
        env.provider._script = [
            ProviderMessage(
                message=Message.assistant(
                    reply.format(curp=curp_token, phone=phone_token).replace("Ana", name_token)
                ),
                usage=Usage(input_tokens=10, output_tokens=10),
                stop_reason="end_turn",
                model="claude-opus-5-5",
            )
        ]
        body, headers = whatsapp(text, "SM1", name="Ana Gómez")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()

        wire = _wire(env.provider)
        for planted in ["Ana", "Gómez", "1234 5678", "5512345678", CURP, "ana@example.mx"]:
            assert planted not in wire
        sent = env.texts("twilio")[-1]["Body"]
        assert "Ana" in sent  # names are allowed in replies (reveal_in_output)
        assert CURP not in sent and "[curp]" in sent and "[phone]" in sent


async def test_opted_out_contacts_get_no_triggered_messages(tmp_path: Path) -> None:
    env = Env(
        tmp_path, [Message.assistant("Brief."), Message.assistant("Recordatorio.")], edit=_deliver
    )
    inst, headless, client = await env.open()
    async with inst, client:
        await inst.consent.set(inst.scope, ANA, "whatsapp", "revoked", "keyword BAJA")
        await headless.start()
        await _fire_all(headless, env)
        assert env.texts("twilio") == []  # the reminder was suppressed
        assert env.texts("telegram")[0]["text"] == "Brief."  # staff channels are exempt
        suppressed = await inst.audit.records(inst.scope, action="message_suppressed")
        assert len(suppressed) == 1

        # consent required and never granted: also nothing
        await inst.consent.set(inst.scope, ANA, "whatsapp", "granted", "x")
        assert await headless.message("whatsapp", "+5215599999999", "hola") is False


async def test_tenants_are_isolated_in_one_database(tmp_path: Path) -> None:
    env = Env(tmp_path, [Message.assistant("Hola A."), Message.assistant("Hola B.")])
    a, headless_a, client_a = await env.open()
    b, headless_b, client_b = await env.open(
        tenant="bravo", instance_id="bravo-desk", database=a.db
    )
    async with a, b, client_a, client_b:
        for client, headless, sid in (
            (client_a, headless_a, "SM-A"),
            (client_b, headless_b, "SM-B"),
        ):
            body, headers = whatsapp(f"Hola, soy Ana, CURP {CURP}", sid)
            await client.post("/channels/whatsapp", content=body, headers=headers)
            await headless.worker().drain()
        await a.inbox.create("escalation", "only for A", {}, None)

        sessions_a = await a.store.list_sessions(a.scope)
        sessions_b = await b.store.list_sessions(b.scope)
        assert len(sessions_a) == len(sessions_b) == 1 and set(sessions_a).isdisjoint(sessions_b)
        with pytest.raises(FileNotFoundError):
            await b.store.load(b.scope, sessions_a[0])
        assert (
            await client_b.get(f"/admin/sessions/{sessions_a[0]}", headers=ADMIN_H)
        ).status_code == 404
        assert (await client_b.get("/admin/inbox", headers=ADMIN_H)).json() == []
        assert len((await client_a.get("/admin/inbox", headers=ADMIN_H)).json()) == 1
        token_a = await a.pii.vault.token_for(a.scope, "curp", CURP)
        assert await b.pii.vault.value_of(b.scope, token_a) is None
        assert len(await a.audit.records(a.scope)) == len(await b.audit.records(b.scope))
        assert await a.audit.verify(a.scope) is None and await b.audit.verify(b.scope) is None


async def test_every_side_effect_is_audited_and_costs_add_up(tmp_path: Path) -> None:
    def step(message: Message, usage: Usage, model: str) -> ProviderMessage:
        stop = "tool_use" if message.tool_uses() else "end_turn"
        return ProviderMessage(message=message, usage=usage, stop_reason=stop, model=model)

    write = calls(("w1", "notes.write", {"key": "k", "text": "v"}))
    priced = [
        step(write, Usage(input_tokens=100_000), "claude-opus-5-5"),
        step(
            Message.assistant("Pendiente de aprobación."),
            Usage(output_tokens=10_000),
            "claude-haiku-4-5",
        ),
        step(Message.assistant("Listo."), Usage(output_tokens=10_000), "claude-haiku-4-5"),
    ]
    env = Env(tmp_path, priced)
    inst, headless, client = await env.open()
    async with inst, client:
        body, headers = whatsapp("anota algo", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        [item] = await inst.inbox.list()
        await client.post(
            f"/admin/inbox/{item.id}/decision",
            json={"approved": True, "by": "jag"},
            headers=ADMIN_H,
        )
        await headless.worker().drain()

        actions = [(r.action, r.subject) for r in await inst.audit.records(inst.scope)]
        assert ("tool_call", "notes.write") in actions  # attempted (denied, pending approval)
        assert ("inbox_approved", f"inbox/{item.id}") in actions
        assert ("approved_action", "notes.write") in actions  # the side effect itself
        assert [a for a, _ in actions].count("message_out") == 2  # both replies sent
        assert await inst.audit.verify(inst.scope) is None

        spend = (await client.get("/admin/spend", headers=ADMIN_H)).json()
        opus, haiku = 0.4, 2 * 10_000 * 5 / 1_000_000
        assert spend["model:claude-opus-5-5"] == pytest.approx(opus)
        assert spend["model:claude-haiku-4-5"] == pytest.approx(haiku)
        assert spend["tenant"] == pytest.approx(opus + haiku)
        assert spend["agent:front"] == pytest.approx(opus + haiku)
