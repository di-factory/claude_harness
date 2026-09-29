"""Memory (M3.5): scoped facts, contradictions, skills, episodes, extraction and forgetting."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from dif_general_harness.core.messages import Message, ToolResultBlock
from dif_general_harness.core.scope import Scope
from dif_general_harness.memory import MemoryScope, MemoryStore
from dif_general_harness.memory.agent import memory_block
from tests.support import ADMIN_H, ANA, Env, calls, whatsapp

BETO = "+5215599999999"


def _memory(spec: dict[str, Any], **extra: Any) -> None:
    spec["memory"] = {"layers": ["episodic", "semantic"], "scope": "contact", **extra}


def _result(env: Env, turn: int) -> ToolResultBlock:
    block = env.provider.requests[turn].messages[-1].content[0]
    assert isinstance(block, ToolResultBlock)
    return block


async def test_store_scopes_supersession_and_purge(db: Any, scope: Scope) -> None:
    store = MemoryStore(db, scope)
    ana, beto = MemoryScope("contact", ANA), MemoryScope("contact", BETO)
    first, previous = await store.remember(ana, "Preferred time", "mornings", "s1")
    assert previous is None
    assert await store.remember(ana, "preferred  time", "Mornings", "s2") == (first, None)
    await store.remember(beto, "preferred time", "evenings", "s3")

    second, previous = await store.remember(ana, "preferred time", "afternoons", "s4")
    assert previous is not None and previous.id == first
    [fact] = await store.active(ana, "semantic")
    assert (fact.id, fact.content, fact.version) == (second, "afternoons", 2)

    await store.restore(first)  # a person kept the earlier value
    assert [m.content for m in await store.active(ana, "semantic")] == ["mornings"]
    assert [m.content for m in await store.active(beto, "semantic")] == ["evenings"]  # untouched

    [(hit, score)] = await store.search(ana, "what time does she prefer?")
    assert hit.id == first and score > 0
    assert await store.search(beto, "allergies") == []

    other = MemoryStore(
        db, Scope(tenant_id="other", instance_id="other-desk")
    )  # another tenant sees nothing
    assert await other.active(ana) == [] and await other.get(first) is None

    await store.record_episode(ana, "s9", "user: hola", ttl_s=-1)  # already expired
    assert not [m for m in await store.active(ana) if m.layer == "episodic"]
    assert await store.purge_expired() == 1
    assert await store.forget(ana) == 2  # both versions of the fact
    assert await store.active(beto)


async def test_facts_follow_the_contact_and_changes_go_to_a_person(tmp_path: Path) -> None:
    script = [
        calls(("m1", "memory.write", {"key": "preferred time", "value": "mornings"})),
        Message.assistant("Anotado: por las mañanas."),
        calls(("m2", "memory.write", {"key": "preferred time", "value": "afternoons"})),
        Message.assistant("Cambiado a las tardes."),
        Message.assistant("Hola Beto."),
    ]
    env = Env(tmp_path, script, edit=_memory)
    inst, headless, client = await env.open()
    async with inst, client:
        assert {"memory.search", "memory.write"} <= set(headless.agent("front").tools.names())
        assert "memory.propose_skill" not in headless.agent("front").tools.names()

        for text, sid in (("Prefiero las mañanas", "SM1"), ("Mejor por la tarde", "SM2")):
            body, headers = whatsapp(text, sid)
            await client.post("/channels/whatsapp", content=body, headers=headers)
            await headless.worker().drain()
        assert "## What you remember\n- preferred time: mornings" in env.provider.requests[2].system
        assert "replaces the earlier value ('mornings')" in str(_result(env, 3).content)

        # the change went to a person, who keeps the earlier value
        r = await client.get("/admin/inbox?kind=memory", headers=ADMIN_H)
        [item] = r.json()
        assert item["payload"]["previous"] == "mornings" and item["payload"]["new"] == "afternoons"
        r = await client.post(
            f"/admin/inbox/{item['id']}/decision",
            json={"approved": False, "by": "maria"},
            headers=ADMIN_H,
        )
        assert r.json()["status"] == "denied"
        r = await client.get(f"/admin/contacts/{ANA}/memory", headers=ADMIN_H)
        facts = [(m["key"], m["content"]) for m in r.json() if m["layer"] == "semantic"]
        assert facts == [("preferred time", "mornings")]

        # another patient never sees Ana's facts
        body, headers = whatsapp("Hola", "SM3", sender=BETO, name="Beto")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        assert "What you remember" not in env.provider.requests[4].system

        r = await client.delete(f"/admin/contacts/{ANA}/memory", headers=ADMIN_H)
        assert r.json()["removed"] >= 2
        r = await client.get(f"/admin/contacts/{ANA}/memory", headers=ADMIN_H)
        assert r.json() == []
        assert (await client.get("/admin/audit/verify", headers=ADMIN_H)).json()["intact"]


async def test_skills_need_repeated_success_and_approval(tmp_path: Path) -> None:
    def edit(spec: dict[str, Any]) -> None:
        _memory(spec, skill_promotion={"min_successes": 2, "approval": "required"})
        spec["agents"]["ops"]["memory"] = {"scope": "agent", "layers": ["procedural"]}

    propose = ("p", "memory.propose_skill", {"name": "Weekly report", "steps": "runs, then post"})
    script = [
        calls(propose),
        Message.assistant("ok"),
        calls(propose),
        Message.assistant("ok"),
        Message.assistant("done"),
    ]
    env = Env(tmp_path, script, edit=edit)
    inst, headless, client = await env.open()
    async with inst, client:
        for _ in range(2):
            await headless.fire_agent("ops", "write the weekly report")
            await headless.worker().drain()
        assert "noted skill 'weekly report' (1/2" in str(_result(env, 1).content)
        assert "sent to a person for approval" in str(_result(env, 3).content)
        assert "skill 'weekly report'" not in env.provider.requests[3].system  # not yet approved

        [item] = await inst.inbox.list(kind="skill")
        await headless.decide(item.id, True, "jag")
        assert (await inst.inbox.get(item.id)).status == "approved"  # type: ignore[union-attr]
        await headless.fire_agent("ops", "write the weekly report")
        await headless.worker().drain()
        assert "- skill 'weekly report': runs, then post" in env.provider.requests[4].system


async def test_episodes_and_background_extraction(tmp_path: Path) -> None:
    def edit(spec: dict[str, Any]) -> None:
        _memory(spec, episodic_ttl="30d")
        spec["models"]["roles"]["memory_extraction"] = {
            "provider": "anthropic",
            "model": "claude-opus-5-5",
        }

    script = [
        Message.assistant("Tomo nota, sin penicilina."),
        Message.assistant('{"facts": [{"key": "allergy", "value": "penicillin"}, {"bad": 1}]}'),
    ]
    env = Env(tmp_path, script, edit=edit)
    inst, headless, client = await env.open()
    async with inst, client:
        body, headers = whatsapp("Soy alérgica a la penicilina", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        assert len(env.provider.requests) == 1  # extraction waits for the conversation to settle
        env.clock.now += 61
        await headless.worker().drain()
        extraction = env.provider.requests[1]
        assert extraction.model_role == "memory_extraction"
        assert "alérgica a la penicilina" in extraction.messages[0].text()

        ana = MemoryScope("contact", ANA)
        [episode] = await inst.memory.active(ana, "episodic")
        assert "penicilina" in episode.content
        assert episode.scope == ana

        # the next conversation starts with what was learned
        later = await headless.agent("front").new_session(contact_key=ANA)
        block = await memory_block(inst, "front", later)
        assert "- allergy: penicillin" in block
        assert "- earlier conversation: user: Soy alérgica" in block

        await inst.db.execute(
            "UPDATE memories SET expires_at = ? WHERE id = ?", (time.time() - 1, episode.id)
        )
        await headless.worker().drain()
        assert await inst.memory.purge_expired() == 1
