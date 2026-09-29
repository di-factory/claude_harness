"""Verification of side effects before they commit (M3.2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.core.messages import Message, ToolResultBlock, ToolStatus
from dif_general_harness.runtime import Instance, RuntimeOptions
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.tenancy import EnvSecrets
from dif_general_harness.tools.packs.coding import CommandResult
from dif_general_harness.verify.checks import _matches
from tests.support import ADMIN_H, ANA, Env, calls, whatsapp

WRITE = ("w1", "notes.write", {"key": "cita", "text": "lunes"})


def _checks(**extra: Any) -> Any:
    def edit(spec: dict[str, Any]) -> None:
        spec["policies"] = {
            "permissions": {"allow": ["notes.*"]},
            "verification": {"checks": extra.pop("checks")},
            "escalation": {"rules": [{"when": "verification.failed_twice", "to": "human"}]},
            **extra,
        }
        spec.setdefault("tools", {})["overrides"] = {"notes.write": {"verify": "gate"}}

    return edit


def _result(env: Env, turn: int) -> ToolResultBlock:
    block = env.provider.requests[turn].messages[-1].content[0]
    assert isinstance(block, ToolResultBlock)
    return block


async def _say(client: Any, headless: Any, text: str, sid: str) -> None:
    body, headers = whatsapp(text, sid)
    await client.post("/channels/whatsapp", content=body, headers=headers)
    await headless.worker().drain()


async def test_condition_check_reads_contact_attributes(tmp_path: Path) -> None:
    gate = {"gate": {"type": "condition", "expr": "contact.verified == true"}}
    script = [
        calls(WRITE),
        Message.assistant("No pude."),
        calls(WRITE),
        Message.assistant("Listo."),
    ]
    env = Env(tmp_path, script, edit=_checks(checks=gate))
    inst, headless, client = await env.open()
    async with inst, client:
        await _say(client, headless, "anota lunes", "SM1")
        denied = _result(env, 1)
        assert denied.status is ToolStatus.DENIED
        assert "verification failed: condition not met" in (denied.error or "")
        notes = tmp_path / "state" / "acme" / "acme-desk" / "notes.json"
        assert not notes.exists()

        r = await client.patch(f"/admin/contacts/{ANA}", json={"verified": True}, headers=ADMIN_H)
        assert r.json() == {"verified": True}
        await _say(client, headless, "ya me verifiqué, anota lunes", "SM2")
        assert _result(env, 3).status is ToolStatus.OK and notes.exists()
        records = await inst.audit.records(inst.scope, action="verification")
        assert [r.data["passed"] for r in records] == [False, True]


async def test_tool_check_and_failed_twice_escalates(tmp_path: Path) -> None:
    gate = {"gate": {"type": "tool", "tool": "notes.list", "expr": "not ('locked' in result)"}}
    script = [calls(WRITE), calls(WRITE), calls(WRITE), Message.assistant("Te paso con alguien.")]
    env = Env(tmp_path, script, edit=_checks(checks=gate))
    inst, headless, client = await env.open()
    async with inst, client:
        from dif_general_harness.tools.packs import NoteStore

        NoteStore(tmp_path / "state", inst.scope).put("locked", "calendar frozen")
        await _say(client, headless, "anota lunes", "SM1")
        errors = [_result(env, i).error or "" for i in (1, 2, 3)]
        assert "notes.list returned" in errors[0] and "notes.list returned" in errors[1]
        assert "already failed twice" in errors[2]  # the third try is refused unchecked
        [item] = await inst.inbox.list(kind="escalation")
        assert "verification of notes.write failed twice" in item.title
        assert len(await inst.audit.records(inst.scope, action="verification")) == 2


async def test_verification_runs_before_anyone_is_asked(tmp_path: Path) -> None:
    def ask_too(spec: dict[str, Any]) -> None:
        _checks(checks={"gate": {"type": "condition", "expr": "args.text != ''"}})(spec)
        spec["policies"]["permissions"] = {"ask": ["notes.write"]}

    blank = ("w1", "notes.write", {"key": "cita", "text": ""})
    script = [
        calls(blank),
        Message.assistant("¿Qué anoto?"),
        calls(WRITE),
        Message.assistant("Pendiente."),
    ]
    env = Env(tmp_path, script, edit=ask_too)
    inst, headless, client = await env.open()
    async with inst, client:
        await _say(client, headless, "anota", "SM1")
        assert await inst.inbox.list() == []  # a failing action never reaches a person
        await _say(client, headless, "anota lunes", "SM2")
        [item] = await inst.inbox.list()
        assert item.kind == "approval" and item.payload["arguments"]["text"] == "lunes"


async def test_verifier_agent(tmp_path: Path) -> None:
    verifier = {"model_role": "main", "applies_to": ["notes.write"], "mode": "always",
                "criteria": ["the contact confirmed the day"]}  # fmt: skip
    script = [
        calls(WRITE),
        Message.assistant('{"pass": false, "reason": "the contact never confirmed monday"}'),
        Message.assistant("¿Confirmas el lunes?"),
        calls(WRITE),
        Message.assistant('Sure: {"pass": true, "reason": "confirmed"}'),
        Message.assistant("Anotado."),
        calls(WRITE),
        Message.assistant("I think it is fine."),  # not JSON: counts as a failure
        Message.assistant("No pude."),
    ]

    def edit(spec: dict[str, Any]) -> None:
        spec["policies"] = {
            "permissions": {"allow": ["notes.*"]},
            "verification": {"checks": {}, "verifier": verifier},
        }

    env = Env(tmp_path, script, edit=edit)
    inst, headless, client = await env.open()
    async with inst, client:
        await _say(client, headless, "anota lunes", "SM1")
        assert "never confirmed monday" in (_result(env, 2).error or "")
        judged = env.provider.requests[1]
        assert judged.tools == [] and "the contact confirmed the day" in judged.messages[0].text()
        assert "notes.write" in judged.messages[0].text()
        await _say(client, headless, "sí, confirmo el lunes", "SM2")
        assert _result(env, 5).status is ToolStatus.OK
        await _say(client, headless, "anota otra vez", "SM3")
        assert "not valid JSON" in (_result(env, 8).error or "")


async def test_command_check_and_unrunnable_checks(tmp_path: Path, helper_solution: Path) -> None:
    pack = tmp_path / "packs" / "helper" / "pack.json"
    data = json.loads(pack.read_text())
    data["policies"] = {
        "permissions": {"allow": ["coding.*", "notes.*"]},
        "verification": {"checks": {
            "tests-pass": {"type": "command", "workspace": "repo", "run": "make test"},
            "gone": {"type": "tool", "tool": "calendar.check_conflicts"},
        }},
    }  # fmt: skip
    data["tools"]["overrides"] = {
        "coding.write": {"verify": "tests-pass"},
        "notes.write": {"verify": "gone"},
    }
    pack.write_text(json.dumps(data))

    ran: list[str] = []

    class Exec:
        def __init__(self) -> None:
            self.exit = 1

        async def run(self, command: str, cwd: Path, timeout_s: float) -> CommandResult:
            ran.append(command)
            return CommandResult(self.exit, "1 failed")

    executor = Exec()
    repo = tmp_path / "repo"
    repo.mkdir()
    resolved = load_instance(helper_solution, PackCatalog(roots=[tmp_path / "packs"]))
    assert resolved.ok, resolved.issues
    from dif_general_harness.providers import FakeProvider

    write = ("c1", "coding.write", {"path": "a.py", "content": "x"})
    provider = FakeProvider(
        [calls(write), Message.assistant("tests fail"), calls(write), Message.assistant("ok")]
    )
    options = RuntimeOptions(
        state_root=tmp_path / "state", secrets=EnvSecrets({}), workspaces={"repo": repo},
        executor=executor, provider=provider,
    )  # fmt: skip
    async with await Instance.open(resolved, options) as inst:
        warned = {(i.code, i.path) for i in inst.issues}
        assert ("verification_unavailable", "tools.notes.write") in warned  # no calendar here
        from dif_general_harness.policy import Verdict
        from dif_general_harness.tools import Effect

        assert inst.policy.decide("notes.write", Effect.WRITE, {}).verdict is Verdict.ASK
        agent = inst.agent()
        session = await agent.new_session()
        [e async for e in agent.send(session, "write a.py")]
        assert "command exited 1: 1 failed" in (session.messages[2].content[0].error or "")  # type: ignore[union-attr]
        executor.exit = 0
        [e async for e in agent.send(session, "tests fixed, write again")]
        assert (repo / "a.py").read_text() == "x" and ran == ["make test", "make test"]


@pytest.mark.parametrize(
    ("result", "expect", "expr", "ok"),
    [
        ({"status": "no_conflicts"}, "no_conflicts", None, True),
        ({"no_conflicts": True}, "no_conflicts", None, True),
        ({"no_conflicts": False}, "no_conflicts", None, False),
        ("no_conflicts found", "no_conflicts", None, True),
        ("conflict at 10:00", "no_conflicts", None, False),
        ({"count": 0}, None, "result.count == 0", True),
        ({"count": 2}, None, "result.count == 0", False),
        ([], None, None, False),
        (0, None, None, True),
    ],
)
def test_expect_matching(result: Any, expect: Any, expr: Any, ok: bool) -> None:
    assert _matches(result, expect, expr) is ok
