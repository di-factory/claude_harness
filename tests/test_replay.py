"""Replaying real conversations on a rebuilt client: the running instance's latest
conversations, sent again to the new version in throwaway instances, each new reply judged
against the one the customer got, and a report with the worse ones first."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from dif_general_harness.cli import replay_command
from dif_general_harness.constructor.replay import conversations
from dif_general_harness.core.messages import Message
from dif_general_harness.providers import FakeProvider
from dif_general_harness.providers.base import ModelRequest
from tests.support import ADMIN_H, API_TOKEN, TWILIO_SID, TWILIO_TOKEN, Env

ROLE = {"provider": "anthropic", "model": "claude-haiku-4-5"}


def _verifier(spec: dict[str, Any]) -> None:
    spec["models"]["roles"]["verifier"] = ROLE


async def _ask(client: Any, contact: str, text: str) -> None:
    r = await client.post("/channels/api", json={"contact": contact, "text": text},
                          headers={"authorization": f"Bearer {API_TOKEN}"})  # fmt: skip
    assert r.status_code == 200, r.text


def test_sessions_become_turns_without_automatic_notes() -> None:
    rows = [{"id": "s1", "channel": "api", "messages": [
        {"role": "user", "text": "¿Horario?"},
        {"role": "assistant", "text": "9 a 19."},
        {"role": "user", "text": "[Automatic check: your last answer cites 0 source(s)]"},
        {"role": "assistant", "text": "De lunes a sábado."},
        {"role": "user", "text": "Gracias"},
    ]}, {"id": "s2", "channel": "api",
         "messages": [{"role": "assistant", "text": "Hola"}]}]  # fmt: skip
    [convo] = conversations(rows)
    assert [(t.customer, t.before) for t in convo.turns] == [
        ("¿Horario?", "9 a 19.\n\nDe lunes a sábado."), ("Gracias", "")]  # fmt: skip


async def test_the_rebuilt_client_answers_the_real_conversations_again(tmp_path: Path) -> None:
    first = [Message.assistant("Abrimos de 9 a 19."),
             Message.assistant("No sé si hacemos limpiezas.")]  # fmt: skip
    live = Env(tmp_path, first, edit=_verifier)
    inst, _, client = await live.open()
    client.headers.update(ADMIN_H)

    def rebuilt(request: ModelRequest) -> Message:
        asked = request.messages[0].text() if request.messages else ""
        if request.model_role == "verifier":
            if "limpiezas" in asked:
                return Message.assistant('{"verdict": "better", "why": "It now answers."}')
            return Message.assistant('{"verdict": "worse", "why": "The hours changed."}')
        if "limpieza" in asked.lower():
            return Message.assistant("Sí: limpieza dental, 600 MXN.")
        return Message.assistant("Abrimos de 10 a 14.")

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name, value in {"api_token": API_TOKEN, "twilio": f"{TWILIO_SID}:{TWILIO_TOKEN}",
                        "telegram": "1:x", "crm_hook": "h"}.items():  # fmt: skip
        (secrets / name).write_text(value)
    out = tmp_path / "replay.md"
    args = argparse.Namespace(
        path=tmp_path / "acme-desk.json", limit=20, url=None, token_file=None, out=out,
        no_judge=False, state=tmp_path / "replay-state", secrets_dir=secrets, workspace=[],
        yes=False,
    )  # fmt: skip
    async with inst, client:
        await _ask(client, "ana@example.com", "¿Qué horario tienen?")
        await _ask(client, "luis@example.com", "¿Hacen limpiezas?")
        assert len((await client.get("/admin/sessions")).json()) == 2
        candidate = FakeProvider([rebuilt] * 8)
        code = await replay_command(args, inst.resolved, candidate, http=client)

    assert code == 1  # a reply got worse
    text = out.read_text()
    assert "2 customer message(s) in 2 conversation(s): 1 worse, 1 better" in text
    worse = text.index("**WORSE**: The hours changed.")
    assert worse < text.index("**BETTER**")  # the worse one comes first
    assert "> Abrimos de 9 a 19." in text and "> Abrimos de 10 a 14." in text
    assert "> Sí: limpieza dental, 600 MXN." in text
    assert live.provider.requests and len(live.sent) == 0  # nothing reached a customer
    judged = [r for r in candidate.requests if r.model_role == "verifier"]
    assert len(judged) == 2 and "BEFORE:" in judged[0].messages[0].text()


def test_the_setup_asks_before_putting_a_worse_version_online(tmp_path: Path) -> None:
    from dif_general_harness.constructor.build import BuildResult
    from dif_general_harness.constructor.setup import Setup

    result = BuildResult("x", tmp_path / "x.json", tmp_path / "a", tmp_path / "s", None)
    calls: list[list[str]] = []

    def setup_with(code: int, replies: list[str]) -> Setup:
        answers = iter(replies)
        return Setup([tmp_path], tmp_path, ask=lambda _: next(answers),
                     run=lambda argv: calls.append(argv) or code,
                     public_url="https://x.sslip.io")  # fmt: skip

    assert setup_with(1, ["n"]).compare_with_live(result) is False  # worse: stays off
    assert calls[-1][:2] == ["replay", str(tmp_path / "x.json")]  # always, never asked
    assert setup_with(1, ["y"]).compare_with_live(result) is True  # the operator decides
    assert setup_with(0, []).compare_with_live(result) is True  # nothing worse: no question
    assert setup_with(2, []).compare_with_live(result) is True  # cannot compare: goes on
    assert len(calls) == 4


async def test_an_owner_faq_edit_is_checked_against_real_conversations(tmp_path: Path) -> None:
    import pytest

    from dif_general_harness.service.admin_client import Admin, AdminError
    from tests.test_admin_cli import FAQ, _faq_spec

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "faq.md").write_text(FAQ)

    def spec(s: dict[str, Any]) -> None:
        _faq_spec(docs)(s)
        _verifier(s)

    def verdict(word: str) -> Message:
        return Message.assistant(f'{{"verdict": "{word}", "why": "Price {word}."}}')

    script = [
        Message.assistant("El precio promedio es 2,000 MXN."),  # the real conversation
        Message.assistant("No tengo precios."), verdict("worse"),  # check 1: blocked
        Message.assistant("El precio promedio es 1,800 MXN."), verdict("better"),  # check 2
    ]  # fmt: skip
    env = Env(tmp_path, script, edit=spec)
    inst, _, client = await env.open()
    async with inst, client:
        await _ask(client, "ana@example.com", "¿Cuánto cuesta?")
        client.headers.update(ADMIN_H)
        admin = Admin(client)
        with pytest.raises(AdminError) as refused:
            await admin.faq_set(FAQ.replace("## Prices\nAverage price: 2,000 MXN.\n", ""))
        message = str(refused.value)
        assert "Not applied: with this FAQ, 1 reply(ies)" in message
        assert "before: El precio promedio es 2,000 MXN." in message and "--force" in message
        assert "2,000 MXN" in await admin.faq_show()  # the live FAQ did not change

        saved = await admin.faq_set(FAQ.replace("2,000 MXN", "1,800 MXN"))
        assert saved.startswith("Checked against recent real conversations: 1 customer")
        assert "1 better" in saved and "1,800 MXN" in await admin.faq_show()
        assert len(await inst.audit.records(inst.scope, action="knowledge_check")) == 2
        assert len(env.sent) == 0  # the checks reached no customer


async def test_a_replay_never_acts_on_the_world(tmp_path: Path) -> None:
    from dif_general_harness.constructor.replay import Replayed, Turn, replay
    from dif_general_harness.tools.packs import NoteStore
    from tests.support import calls

    def allow_notes(spec: dict[str, Any]) -> None:
        _verifier(spec)
        spec.setdefault("policies", {})["permissions"] = {"allow": ["notes.*"]}

    script = [calls(("w1", "notes.write", {"key": "cita", "text": "lunes"})),
              Message.assistant("Anotado."),
              Message.assistant('{"verdict": "same", "why": "Same."}')]  # fmt: skip
    env = Env(tmp_path, script, edit=allow_notes)
    inst, _, client = await env.open()
    async with inst, client:
        pass
    convo = Replayed("s1", "api", [Turn("anota lunes", "Anotado.")])
    states: list[Path] = []

    async def open_copy(state: Path) -> Any:
        states.append(state)
        from dif_general_harness.runtime import Instance

        options = __import__("dataclasses").replace(inst.options, state_root=state)
        return await Instance.open(inst.resolved, options)

    [done] = await replay(open_copy, [convo], tmp_path / "work")
    assert done.turns[0].after == "Anotado." and not done.error
    result = env.provider.requests[1].messages[-1].content[0]
    assert "not run: this is a replayed conversation" in str(result.content)
    assert NoteStore(states[0], inst.scope).get("cita") is None  # nothing was written
