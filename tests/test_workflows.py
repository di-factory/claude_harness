"""The workflow engine and the triggers that start it (M3.3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dif_general_harness.core.messages import Message
from tests.support import ADMIN_H, ANA, Env, whatsapp

HOUR = 3600.0


def _flows(root: Path, workflows: dict[str, Any], triggers: dict[str, Any] | None = None) -> Any:
    def edit(spec: dict[str, Any]) -> None:
        prompts = root / "packs" / "desk" / "prompts"
        (prompts / "tpl_reminder.md").write_text(
            "Hi {{contact.name}}, your visit is tomorrow. Reply 1 to confirm."
        )
        (prompts / "tpl_nudge.md").write_text("Still coming tomorrow? Reply 1.")
        spec["channels"]["whatsapp"]["templates"] = {
            "reminder": {"file": "prompts/tpl_reminder.md", "provider_template_id": "HXreminder"},
            "nudge": {"file": "prompts/tpl_nudge.md", "provider_template_id": "HXnudge"},
        }
        spec["workflows"].update(workflows)
        spec["triggers"].update(triggers or {})
        spec["policies"] = {"permissions": {"allow": ["notes.*"]}}

    return edit


REMINDER = {
    "send-reminder": {
        "input": {"event": "calendar.event"},
        "steps": [
            {
                "id": "msg",
                "type": "template",
                "channel": "whatsapp",
                "template": "reminder",
                "to": "{{event.contact}}",
                "vars": {"1": "{{event.name}}"},
            },
            {"id": "wait", "type": "wait", "for": "reply", "timeout": "12h"},
            {
                "id": "handle",
                "type": "agent",
                "agent": "front",
                "input": "{{steps.wait.reply}}",
                "when": "steps.wait.replied",
            },
            {
                "id": "nudge",
                "type": "template",
                "channel": "whatsapp",
                "template": "nudge",
                "to": "{{event.contact}}",
                "when": "not steps.wait.replied",
            },
        ],
        "on_error": "escalate",
    }
}
REMIND_TRIGGER = {
    "reminder": {
        "type": "relative",
        "source": "appointments",
        "offset": "-24h",
        "workflow": "send-reminder",
        "input": {"event": "{{event}}"},
        "requires_consent": True,
    }
}


async def _open(
    tmp_path: Path, script: list[Any], workflows: dict[str, Any], triggers: Any = None
) -> Any:
    env = Env(tmp_path, script, edit=_flows(tmp_path, workflows, triggers))
    inst, headless, client = await env.open()
    return env, inst, headless, client


async def _appointment(client: Any, env: Env, **extra: Any) -> None:
    item = {
        "id": "evt-1",
        "start": env.clock.now + 30 * HOUR,
        "contact": ANA,
        "name": "Ana",
        **extra,
    }
    r = await client.post(
        "/admin/sources/appointments/items", json={"items": [item]}, headers=ADMIN_H
    )
    assert r.status_code == 200


async def test_reminder_flow_with_a_reply(tmp_path: Path) -> None:
    env, inst, headless, client = await _open(
        tmp_path,
        [Message.assistant("¡Gracias Ana! Tu cita queda confirmada.")],
        REMINDER,
        REMIND_TRIGGER,
    )
    async with inst, client:
        await inst.consent.set(inst.scope, ANA, "whatsapp", "granted", "clinic import")
        await _appointment(client, env)
        await headless.worker().drain()
        assert env.texts("twilio") == []  # 24 h before a visit 30 h away: not yet
        env.clock.now += 6 * HOUR + 1
        await headless.worker().drain()
        [template] = env.texts("twilio")
        assert template["ContentSid"] == "HXreminder" and json.loads(
            template["ContentVariables"]
        ) == {"1": "Ana"}
        [run] = (await client.get("/admin/runs", headers=ADMIN_H)).json()
        assert run["status"] == "waiting" and run["step"] == "wait"

        body, headers = whatsapp("1", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        assert env.provider.requests[0].messages[-1].text() == "1"  # the agent got the reply
        assert env.texts("twilio")[-1]["Body"] == "¡Gracias Ana! Tu cita queda confirmada."
        detail = (await client.get(f"/admin/runs/{run['id']}", headers=ADMIN_H)).json()
        assert detail["status"] == "done" and detail["steps"]["nudge"] == {"skipped": True}
        assert detail["steps"]["wait"]["reply"] == "1"


async def test_reminder_flow_times_out_and_nudges(tmp_path: Path) -> None:
    env, inst, headless, client = await _open(tmp_path, [], REMINDER, REMIND_TRIGGER)
    async with inst, client:
        await inst.consent.set(inst.scope, ANA, "whatsapp", "granted", "clinic import")
        await _appointment(client, env)
        env.clock.now += 6 * HOUR + 1
        await headless.worker().drain()
        env.clock.now += 12 * HOUR + 1
        await headless.worker().drain()
        assert [t["ContentSid"] for t in env.texts("twilio")] == ["HXreminder", "HXnudge"]
        [run] = await headless.engine.runs("send-reminder")
        assert run.status == "done" and run.steps["handle"] == {"skipped": True}
        assert run.steps["wait"]["timed_out"] is True
        assert env.provider.requests == []  # nobody answered, the agent never ran


async def test_relative_triggers_follow_moves_and_consent(tmp_path: Path) -> None:
    env, inst, headless, client = await _open(tmp_path, [], REMINDER, REMIND_TRIGGER)
    async with inst, client:
        await _appointment(client, env)  # no consent recorded yet: required, so nothing goes out
        env.clock.now += 6 * HOUR + 1
        await headless.worker().drain()
        assert env.texts("twilio") == [] and await headless.engine.runs() == []
        skipped = await inst.audit.records(inst.scope, action="trigger_skipped")
        assert [r.subject for r in skipped] == ["consent"]

        await inst.consent.set(inst.scope, ANA, "whatsapp", "granted", "clinic import")
        await _appointment(client, env, id="evt-2")  # 30 h from the new now
        await _appointment(client, env, id="evt-2", start=env.clock.now + 60 * HOUR)  # moved later
        env.clock.now += 6 * HOUR + 1
        await headless.worker().drain()
        assert env.texts("twilio") == []  # the old firing saw the move and stood down
        env.clock.now += 30 * HOUR
        await headless.worker().drain()
        assert [t["ContentSid"] for t in env.texts("twilio")] == ["HXreminder"]


PROCESS = {
    "process": {
        "steps": [
            {"id": "extract", "type": "agent", "agent": "ops", "input": "{{input}}"},
            {
                "id": "save",
                "type": "tool",
                "tool": "notes.write",
                "args": {
                    "key": "{{steps.extract.output.id}}",
                    "text": "MXN {{steps.extract.output.total}}",
                },
            },
            {
                "id": "route",
                "type": "branch",
                "cases": [
                    {"when": "steps.extract.output.total > 1000", "goto": "approve"},
                    {"when": "true", "goto": "done"},
                ],
            },
            {
                "id": "approve",
                "type": "approval",
                "summary": "Post {{steps.extract.output.id}}?",
                "on_reject": "rejected",
            },
            {
                "id": "tell",
                "type": "message",
                "channel": "staff",
                "body": "Posted {{steps.extract.output.id}}",
            },
            {"id": "done", "type": "end"},
            {"id": "rejected", "type": "end", "outcome": "rejected"},
        ]
    }
}


async def test_structured_agent_output_branch_and_approval(tmp_path: Path) -> None:
    big = Message.assistant('{"id": "inv-9", "total": 4200}')
    small = Message.assistant('```json\n{"id": "inv-1", "total": 90}\n```')
    env, inst, headless, client = await _open(tmp_path, [big, small, big], PROCESS)
    async with inst, client:
        run_id = await headless.engine.start("process", {"file": "a.xml"})
        await headless.worker().drain()
        run = await headless.engine.get(run_id)
        assert run and run.status == "waiting" and run.steps["extract"]["total"] == 4200
        notes = json.loads((tmp_path / "state" / "acme" / "acme-desk" / "notes.json").read_text())
        assert notes == {"inv-9": "MXN 4200"}
        [item] = (await client.get("/admin/inbox", headers=ADMIN_H)).json()
        assert item["title"] == "Post inv-9?"
        await client.post(
            f"/admin/inbox/{item['id']}/decision",
            json={"approved": True, "by": "jag"},
            headers=ADMIN_H,
        )
        await headless.worker().drain()
        run = await headless.engine.get(run_id)
        assert run and run.status == "done" and run.outcome == "completed"
        assert env.texts("telegram")[-1]["text"] == "Posted inv-9"

        small_run = await headless.engine.start("process", {"file": "b.xml"})
        await headless.worker().drain()
        done = await headless.engine.get(small_run)
        assert done and done.status == "done" and "approve" not in done.steps  # jumped to done

        rejected_run = await headless.engine.start("process", {"file": "c.xml"})
        await headless.worker().drain()
        [item] = (await client.get("/admin/inbox", headers=ADMIN_H)).json()
        await client.post(
            f"/admin/inbox/{item['id']}/decision",
            json={"approved": False, "note": "wrong RFC"},
            headers=ADMIN_H,
        )
        await headless.worker().drain()
        rejected = await headless.engine.get(rejected_run)
        assert rejected and rejected.outcome == "rejected" and "tell" not in rejected.steps


async def test_tool_steps_obey_the_policy(tmp_path: Path) -> None:
    flows = {
        "jot": {
            "steps": [
                {
                    "id": "w",
                    "type": "tool",
                    "tool": "notes.write",
                    "args": {"key": "k", "text": "v"},
                }
            ]
        },
        "nope": {
            "steps": [{"id": "n", "type": "tool", "tool": "notes.list"}],
            "on_error": "escalate",
        },
    }
    _, inst, headless, client = await _open(tmp_path, [], flows)
    inst.policy.ask.append("notes.write")
    inst.policy.deny.append("notes.list")
    inst.policy.__post_init__()
    async with inst, client:
        run_id = await headless.engine.start("jot", {})
        await headless.worker().drain()
        [item] = await inst.inbox.list()
        assert (
            item.payload["tool"] == "notes.write"
            and (await headless.engine.get(run_id)).status == "waiting"
        )  # type: ignore[union-attr]
        await headless.decide(item.id, True, "jag")
        await headless.worker().drain()
        assert (await headless.engine.get(run_id)).status == "done"  # type: ignore[union-attr]

        denied = await headless.engine.start("nope", {})
        await headless.worker().drain()
        run = await headless.engine.get(denied)
        assert run and run.status == "escalated" and "denied by rule" in (run.error or "")
        assert [i.kind for i in await inst.inbox.list()] == ["escalation"]


async def test_event_waits_and_event_triggers(tmp_path: Path) -> None:
    flows = {
        "pay": {
            "steps": [
                {
                    "id": "wait",
                    "type": "wait",
                    "for": "event",
                    "event": "payment.received",
                    "match": "event.order == input.order",
                    "timeout": "2d",
                },
                {"id": "done", "type": "end", "outcome": "paid"},
            ]
        }
    }
    triggers = {
        "on-assign": {
            "type": "event",
            "event": "ledger.task_assigned",
            "agent": "{{event.owner}}",
            "input": "New task: {{event.title}}",
        }
    }
    env, inst, headless, client = await _open(
        tmp_path, [Message.assistant("On it.")], flows, triggers
    )
    async with inst, client:
        a = await headless.engine.start("pay", {"order": "A-1"})
        b = await headless.engine.start("pay", {"order": "B-2"})
        await headless.worker().drain()
        r = await client.post(
            "/admin/events",
            json={"name": "payment.received", "data": {"order": "B-2"}},
            headers=ADMIN_H,
        )
        assert r.json() == {"woken": 1}
        await headless.worker().drain()
        assert (await headless.engine.get(b)).outcome == "paid"  # type: ignore[union-attr]
        assert (await headless.engine.get(a)).status == "waiting"  # type: ignore[union-attr]

        await headless.emit("ledger.task_assigned", {"owner": "ops", "title": "Renew the cert"})
        await headless.worker().drain()
        assert env.provider.requests[0].messages[0].text() == "New task: Renew the cert"
        assert (await client.post("/admin/events", json={}, headers=ADMIN_H)).status_code == 400


async def test_parallel_handoff_and_errors(tmp_path: Path) -> None:
    flows = {
        "standup": {
            "steps": [
                {
                    "id": "updates",
                    "type": "parallel",
                    "branches": [
                        {"id": "a", "type": "agent", "agent": "ops", "input": "Your update?"},
                        {"id": "b", "type": "agent", "agent": "front", "input": "Your update?"},
                    ],
                },
                {
                    "id": "brief",
                    "type": "message",
                    "channel": "staff",
                    "body": "ops: {{steps.a.text}} / front: {{steps.b.text}}",
                },
                {
                    "id": "person",
                    "type": "handoff",
                    "to": "human",
                    "reason": "review the brief",
                    "outcome": "reviewed",
                },
            ]
        },
        "broken": {
            "steps": [{"id": "x", "type": "tool", "tool": "notes.gone"}],
            "on_error": "escalate",
        },
    }
    env, inst, headless, client = await _open(
        tmp_path, [Message.assistant("all good"), Message.assistant("two visits")], flows
    )
    async with inst, client:
        run_id = await headless.engine.start("standup", {})
        await headless.worker().drain()
        run = await headless.engine.get(run_id)
        assert run and run.outcome == "reviewed"
        texts = {r.messages[0].text() for r in env.provider.requests}
        assert texts == {"Your update?"} and len(env.provider.requests) == 2
        brief = env.texts("telegram")
        assert any(
            t["text"] in {"ops: all good / front: two visits", "ops: two visits / front: all good"}
            for t in brief
        )
        assert [i.title for i in await inst.inbox.list(kind="escalation")] == ["review the brief"]

        failed = await headless.engine.start("broken", {})
        await headless.worker().drain()
        run = await headless.engine.get(failed)
        assert (
            run and run.status == "escalated" and "notes.gone is not available" in (run.error or "")
        )


async def test_timer_arms_a_delay_trigger_with_unless(tmp_path: Path) -> None:
    flows = {
        "ticket": {
            "steps": [
                {"id": "sla", "type": "timer", "trigger": "sla"},
                {"id": "done", "type": "end"},
            ]
        }
    }
    triggers = {
        "sla": {
            "type": "delay",
            "after": "4h",
            "agent": "ops",
            "input": "SLA breached: {{event.ticket}}",
            "unless": "event.status == 'solved'",
        }
    }
    env, inst, headless, client = await _open(
        tmp_path, [Message.assistant("Escalating.")], flows, triggers
    )
    async with inst, client:
        await headless.engine.start("ticket", {"ticket": "T-1", "status": "open"})
        await headless.engine.start("ticket", {"ticket": "T-2", "status": "solved"})
        await headless.worker().drain()
        assert env.provider.requests == []
        env.clock.now += 4 * HOUR + 1
        await headless.worker().drain()
        assert [r.messages[0].text() for r in env.provider.requests] == ["SLA breached: T-1"]


async def test_runs_survive_a_crash_and_respect_concurrency(tmp_path: Path) -> None:
    flows = {
        "slow": {
            "concurrency": 1,
            "steps": [
                {"id": "wait", "type": "wait", "for": "time", "duration": "1h"},
                {"id": "done", "type": "end"},
            ],
        }
    }
    env, inst, headless, client = await _open(tmp_path, [], flows)
    async with client:
        first = await headless.engine.start("slow", {})
        second = await headless.engine.start("slow", {})
        await headless.worker().drain()
        assert (await headless.engine.get(first)).status == "waiting"  # type: ignore[union-attr]
        assert (await headless.engine.get(second)).state["pc"] == 0  # type: ignore[union-attr]
    await inst.close()  # the process dies with both runs in flight

    restarted, headless2, _ = await env.open()
    async with restarted:
        env.clock.now += HOUR + 1
        await headless2.worker().drain()
        assert (await headless2.engine.get(first)).status == "done"  # type: ignore[union-attr]
        env.clock.now += 60  # the second run gets the slot and starts its own hour
        await headless2.worker().drain()
        waiting = await headless2.engine.get(second)
        assert waiting and waiting.status == "waiting" and waiting.state["waiting_step"] == "wait"
        env.clock.now += HOUR + 1
        await headless2.worker().drain()
        assert (await headless2.engine.get(second)).status == "done"  # type: ignore[union-attr]


async def test_workflow_triggered_by_a_webhook(tmp_path: Path) -> None:
    import hashlib
    import hmac

    from tests.support import HOOK

    flows = {
        "lead-flow": {
            "steps": [
                {
                    "id": "save",
                    "type": "tool",
                    "tool": "notes.write",
                    "args": {"key": "lead", "text": "{{input.lead.name}}"},
                },
                {"id": "done", "type": "end", "outcome": "saved"},
            ]
        }
    }

    def use_flow(spec: dict[str, Any]) -> None:
        _flows(tmp_path, flows)(spec)
        spec["triggers"]["lead"] = {
            "type": "webhook",
            "path": "/hooks/crm",
            "auth": {"$secret": "crm_hook"},
            "workflow": "lead-flow",
            "when": "event.lead.source == 'web'",
        }

    env = Env(tmp_path, [], edit=use_flow)
    inst, headless, client = await env.open()
    async with inst, client:
        for source in ("phone", "web"):
            body = json.dumps({"lead": {"name": f"Luis-{source}", "source": source}}).encode()
            sig = "sha256=" + hmac.new(HOOK.encode(), body, hashlib.sha256).hexdigest()
            await client.post(
                "/hooks/hooks/crm", content=body, headers={"x-hub-signature-256": sig}
            )
        await headless.worker().drain()
        [run] = await headless.engine.runs("lead-flow")
        assert run.outcome == "saved" and run.input["lead"]["name"] == "Luis-web"
        assert any(
            r.subject == "condition"
            for r in await inst.audit.records(inst.scope, action="trigger_skipped")
        )


async def test_a_waiting_run_takes_the_reply_not_the_agent(tmp_path: Path) -> None:
    flows = {
        "ask": {
            "steps": [
                {
                    "id": "q",
                    "type": "message",
                    "channel": "whatsapp",
                    "to": "{{input.contact}}",
                    "body": "¿Viene mañana?",
                },
                {"id": "wait", "type": "wait", "for": "reply"},
                {"id": "done", "type": "end", "outcome": "answered"},
            ]
        }
    }
    env, inst, headless, client = await _open(
        tmp_path, [Message.assistant("normal agent reply")], flows
    )
    async with inst, client:
        await inst.consent.set(inst.scope, ANA, "whatsapp", "granted", "x")
        run_id = await headless.engine.start("ask", {"contact": ANA})
        await headless.worker().drain()
        body, headers = whatsapp("sí", "SM1")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        assert (await headless.engine.get(run_id)).outcome == "answered"  # type: ignore[union-attr]
        assert env.provider.requests == []  # the entry agent did not answer this one
        body, headers = whatsapp("otra cosa", "SM2")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        assert env.texts("twilio")[-1]["Body"] == "normal agent reply"
