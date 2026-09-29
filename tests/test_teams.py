"""Agent teams (M3.4): the ledger, sub-agents, handoffs and run reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.core.messages import Message, ToolResultBlock, ToolStatus
from dif_general_harness.tools.registry import InputError
from tests.support import ADMIN_H, Env, calls, whatsapp


def _team(spec: dict[str, Any]) -> None:
    spec["agents"]["front"]["handoffs"] = ["ops", "human"]
    spec["agents"]["front"]["subagents"] = ["ops"]
    spec["agents"]["front"]["tools"] = ["notes.*", "ledger.*"]
    spec["agents"]["ops"]["tools"] = ["notes.*", "ledger.*", "runs.*"]
    spec["ledger"] = {
        "backend": "builtin",
        "fields": {"title": "string", "owner": "agent", "status": "enum:todo,doing,done",
                   "due": "date", "needs_founder": "boolean"},
        "visible_to": ["ops", "human"],
    }  # fmt: skip
    spec["triggers"]["assigned"] = {
        "type": "event", "event": "ledger.task_assigned",
        "agent": "{{event.task.owner}}", "input": "New task for you: {{event.task.title}}",
    }  # fmt: skip
    spec["policies"] = {
        "permissions": {"allow": ["notes.*", "ledger.*", "runs.*"]},
        "escalation": {"rules": [{"when": "ledger.task.needs_founder", "to": "human"}],
                       "handoff_to": {"type": "channel", "channel": "staff"}},
    }  # fmt: skip


def _result(env: Env, turn: int, index: int = 0) -> ToolResultBlock:
    block = env.provider.requests[turn].messages[-1].content[index]
    assert isinstance(block, ToolResultBlock)
    return block


async def test_ledger_tools_visibility_events_and_escalation(tmp_path: Path) -> None:
    create = (
        "c1",
        "ledger.create_task",
        {"title": "Renew TLS cert", "owner": "ops", "due": "2026-10-01"},
    )
    founder = ("c2", "ledger.create_task", {"title": "Sign the lease", "needs_founder": True})
    script = [
        calls(create, founder),
        Message.assistant("Logged."),
        Message.assistant("Renewing it now."),
    ]
    env = Env(tmp_path, script, edit=_team)
    inst, headless, client = await env.open()
    async with inst, client:
        assert inst.ledger is not None
        assert "ledger.create_task" in headless.agent("ops").tools.names()
        assert not any(
            n.startswith("ledger.") for n in headless.agent("front").tools.names()
        )  # not visible

        await headless.fire_agent("ops", "log the cert renewal and the lease")
        await headless.worker().drain()
        tasks = await inst.ledger.list_tasks()
        assert [(t["title"], t["status"], t.get("owner")) for t in tasks] == [
            ("Renew TLS cert", "todo", "ops"),
            ("Sign the lease", "todo", None),
        ]
        # assigning to an agent emitted ledger.task_assigned, and the event trigger routed it
        assert env.provider.requests[-1].messages[0].text() == "New task for you: Renew TLS cert"
        # needs_founder matched the escalation rule: an inbox item and the handoff_to channel
        [item] = await inst.inbox.list(kind="escalation")
        assert item.title == "Task needs a person: Sign the lease"
        assert any("Sign the lease" in t["text"] for t in env.texts("telegram"))

        with pytest.raises(InputError, match="date"):
            await inst.ledger.update(tasks[0]["id"], {"due": "next week"}, "x")
        bad = await headless.agent("ops").tools.execute(
            calls(("c3", "ledger.create_task", {"title": "x", "owner": "nobody"})).tool_uses()[0]
        )
        assert bad.status is ToolStatus.ERROR  # owner must be an agent


async def test_subagent_answers_as_a_tool(tmp_path: Path) -> None:
    script = [
        calls(("s1", "agent.ops", {"task": "How many visits tomorrow?"})),
        Message.assistant('{"visits": 7, "first": "09:00"}'),  # the sub-agent, in its own session
        Message.assistant("Mañana hay 7 citas; la primera a las 9:00."),
    ]
    env = Env(tmp_path, script, edit=_team)
    inst, headless, client = await env.open()
    async with inst, client:
        body, headers = whatsapp("¿Cuántas citas hay mañana?", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        assert env.provider.requests[1].messages[0].text() == "How many visits tomorrow?"
        assert len(env.provider.requests[1].messages) == 1  # a fresh session: no contact history
        result = _result(env, 2)
        assert result.status is ToolStatus.OK and result.content["output"] == {
            "visits": 7,
            "first": "09:00",
        }
        assert env.texts("twilio")[-1]["Body"] == "Mañana hay 7 citas; la primera a las 9:00."


async def test_handoff_moves_the_conversation(tmp_path: Path) -> None:
    script = [
        calls(("h1", "handoff.agent", {"to": "ops", "reason": "billing question"})),
        Message.assistant("Te paso con operaciones."),
        Message.assistant("Hola, soy de operaciones. ¿Qué factura?"),  # ops, same turn
        Message.assistant("Listo, reenviada."),  # ops again, next message
    ]
    env = Env(tmp_path, script, edit=_team)
    inst, headless, client = await env.open()
    async with inst, client:
        body, headers = whatsapp("Necesito mi factura", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        sent = env.texts("twilio")[-1]["Body"]
        assert sent == "Te paso con operaciones.\n\nHola, soy de operaciones. ¿Qué factura?"
        seed = env.provider.requests[2].messages[0].text()
        assert (
            seed.startswith("[Handoff from front: billing question]")
            and "Necesito mi factura" in seed
        )

        body, headers = whatsapp("La de septiembre", "SM2")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        assert env.texts("twilio")[-1]["Body"] == "Listo, reenviada."
        last = env.provider.requests[3]
        assert last.system.startswith("You watch operations")  # ops answered, not front
        bad = await headless.agent("front").tools.execute(
            calls(("h2", "handoff.agent", {"to": "front", "reason": "x"})).tool_uses()[0]
        )
        assert bad.status is ToolStatus.ERROR  # only listed teammates


async def test_runs_summarize(tmp_path: Path) -> None:
    def flows(spec: dict[str, Any]) -> None:
        _team(spec)
        spec["workflows"]["ok"] = {"steps": [{"id": "e", "type": "end", "outcome": "posted"}]}
        spec["workflows"]["bad"] = {"steps": [{"id": "t", "type": "tool", "tool": "notes.gone"}]}

    env = Env(
        tmp_path,
        [calls(("r1", "runs.summarize", {"since": "24h"})), Message.assistant("ok")],
        edit=flows,
    )
    inst, headless, client = await env.open()
    async with inst, client:
        for name in ("ok", "ok", "bad"):
            await headless.engine.start(name, {})
        await headless.worker().drain()
        await headless.fire_agent("ops", "daily report")
        await headless.worker().drain()
        summary = _result(env, 1).content
        assert summary["counts"] == {"failed": 1, "posted": 2} and summary["total"] == 3
        assert summary["text"].startswith("all workflows, last 24h: 1 failed, 2 posted.")
        assert "notes.gone is not available" in summary["text"]
        r = await client.get("/admin/runs?status=failed", headers=ADMIN_H)
        assert len(r.json()) == 1
