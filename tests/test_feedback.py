"""The feedback loop (M3.7): signals become candidate constraints; approved ones are pinned."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dif_general_harness.core.messages import Message
from dif_general_harness.core.scope import Scope
from dif_general_harness.feedback import ConstraintStore, pinned_block
from tests.support import ADMIN_H, Env, calls, whatsapp

PINNED = "## Rules approved from review (always follow)"


def _ask_notes(spec: dict[str, Any]) -> None:
    spec["policies"] = {"permissions": {"allow": ["notes.read"], "ask": ["notes.write"]}}


async def _say(client: Any, headless: Any, text: str, sid: str) -> None:
    body, headers = whatsapp(text, sid)
    await client.post("/channels/whatsapp", content=body, headers=headers)
    await headless.worker().drain()


async def test_store_dedupes_and_pins(db: Any, scope: Scope) -> None:
    store = ConstraintStore(db, scope)
    first, new = await store.propose("front", "Never quote prices.", "rating", {}, key="prices")
    assert new and first.status == "candidate"
    again, new = await store.propose("front", "Do not quote prices!", "rating", {}, key="prices")
    assert not new and again.id == first.id and again.occurrences == 2
    await store.decide(first.id, "rejected", "maria")
    _, new = await store.propose("front", "Never quote prices.", "rating", {}, key="prices")
    assert not new  # a rejected rule is not proposed again

    everyone = await store.add("*", "Answer in the contact's language.", "maria")
    mine = await store.add("front", "Sign as the front desk.", "maria")
    await store.add("ops", "Report in English.", "maria")
    assert [c.id for c in await store.active("front")] == [everyone.id, mine.id]
    await store.decide(mine.id, "retired", "maria")
    block = pinned_block(await store.active("front"))
    assert block == f"\n\n{PINNED}\n- Answer in the contact's language."
    other = ConstraintStore(db, Scope(tenant_id="beta", instance_id="beta-desk"))
    assert await other.active("front") == []  # nothing crosses tenants


async def test_denial_reason_becomes_a_pinned_rule(tmp_path: Path) -> None:
    script = [
        calls(("n1", "notes.write", {"key": "ana", "text": "cleaning 800 MXN"})),
        Message.assistant("Queda pendiente de aprobación."),
        Message.assistant("No se guardó la nota."),  # follow-up after the denial
        Message.assistant("¿En qué más te ayudo?"),
    ]
    env = Env(tmp_path, script, edit=_ask_notes)
    inst, headless, client = await env.open()
    async with inst, client:
        await _say(client, headless, "Guarda mi presupuesto", "SM1")
        [approval] = await inst.inbox.list(kind="approval")
        r = await client.post(
            f"/admin/inbox/{approval.id}/decision",
            json={"approved": False, "by": "maria", "note": "never store prices in notes"},
            headers=ADMIN_H,
        )
        assert r.json()["status"] == "denied"
        await headless.worker().drain()

        [item] = (await client.get("/admin/inbox?kind=constraint", headers=ADMIN_H)).json()
        assert item["payload"]["source"] == "denial" and item["payload"]["agent"] == "front"
        assert "never store prices in notes" in item["payload"]["text"]
        assert PINNED not in env.provider.requests[-1].system  # not before a person approves

        r = await client.post(
            f"/admin/inbox/{item['id']}/decision",
            json={"approved": True, "by": "maria", "text": "Do not store prices in notes."},
            headers=ADMIN_H,
        )
        assert r.json()["status"] == "approved"
        await _say(client, headless, "Hola otra vez", "SM2")
        assert f"{PINNED}\n- Do not store prices in notes." in env.provider.requests[-1].system

        [rule] = (await client.get("/admin/constraints?status=active", headers=ADMIN_H)).json()
        assert rule["decided_by"] == "maria"
        r = await client.delete(f"/admin/constraints/{rule['id']}?by=maria", headers=ADMIN_H)
        assert r.json()["status"] == "retired"
        assert await inst.constraints.active("front") == []


async def test_ratings_escalations_and_budget_propose_rules(tmp_path: Path) -> None:
    def edit(spec: dict[str, Any]) -> None:
        spec["policies"] = {"budgets": {"per_run": {"turns": 1}}}

    script = [  # each run may call the model once: both runs stop on the budget
        calls(("h1", "handoff.human", {"reason": "asks about a refund"})),
        calls(("n1", "notes.read", {"key": "x"})),
    ]
    env = Env(tmp_path, script, edit=edit)
    inst, headless, client = await env.open()
    async with inst, client:
        await _say(client, headless, "Quiero un reembolso", "SM1")
        [escalation] = await inst.inbox.list(kind="escalation")
        await headless.decide(escalation.id, True, "maria", "refunds go to billing@ by email")
        session_id = escalation.session_id
        assert session_id is not None

        r = await client.post(
            f"/admin/sessions/{session_id}/feedback",
            json={"rating": "down", "comment": "Too formal; use tú", "by": "maria"},
            headers=ADMIN_H,
        )
        assert r.json()["proposed"]
        up = await client.post(
            f"/admin/sessions/{session_id}/feedback", json={"rating": "up"}, headers=ADMIN_H
        )
        assert up.json() == {"proposed": None}
        missing = await client.post(
            "/admin/sessions/nope/feedback", json={"rating": "up"}, headers=ADMIN_H
        )
        assert missing.status_code == 404

        await headless.fire_agent("front", "look up note x")
        await headless.worker().drain()
        sources = {c.source: c for c in await inst.constraints.all("candidate")}
        assert set(sources) == {"escalation", "rating", "budget"}
        assert sources["escalation"].text == (
            "When this comes up (asks about a refund): refunds go to billing@ by email"
        )
        assert sources["rating"].evidence["session"] == session_id
        assert sources["budget"].occurrences == 2  # one candidate however often it happens
        assert len(await inst.inbox.list(kind="constraint")) == 3

        r = await client.post(
            "/admin/constraints", json={"agent": "front", "text": "Use tú."}, headers=ADMIN_H
        )
        assert r.json()["status"] == "active"
        bad = await client.post(
            "/admin/constraints", json={"agent": "nobody", "text": "x"}, headers=ADMIN_H
        )
        assert bad.status_code == 404
