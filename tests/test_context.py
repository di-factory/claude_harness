"""Context compaction and the intent router short-circuit (G1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dif_general_harness.core.messages import (
    Message,
    Role,
    ToolResultBlock,
    ToolStatus,
    ToolUseBlock,
)
from dif_general_harness.core.session import SUMMARY_HEADER
from dif_general_harness.runtime.compaction import estimate_tokens, split_point
from tests.support import ANA, Env, calls, whatsapp

ROLE = {"provider": "anthropic", "model": "claude-haiku-4-5"}


def _router(spec: dict[str, Any]) -> None:
    spec["models"]["roles"]["router"] = ROLE
    spec["policies"] = {"router": {"short_circuit": ["greeting", "thanks"]}}


async def _say(client: Any, headless: Any, text: str, sid: str) -> None:
    body, headers = whatsapp(text, sid)
    await client.post("/channels/whatsapp", content=body, headers=headers)
    await headless.worker().drain()


async def test_trivial_messages_never_reach_the_main_agent(tmp_path: Path) -> None:
    script = [
        Message.assistant('{"intent": "greeting", "reply": "¡Hola! ¿En qué te ayudo?"}'),
        Message.assistant('{"intent": "other", "reply": ""}'),
        Message.assistant("Sí, mañana a las 10."),  # the main agent
        Message.assistant("not json at all"),  # a confused router: the agent answers
        Message.assistant("Con gusto."),
        Message.assistant('{"intent": "booking", "reply": "Listo, reservado"}'),  # not allowed
        Message.assistant("Te ayudo a reservar."),
        Message.assistant("Informe listo."),  # a staff task: never routed
    ]
    env = Env(tmp_path, script, edit=_router)
    inst, headless, client = await env.open()
    async with inst, client:
        await _say(client, headless, "Hola", "SM1")
        assert env.texts("twilio")[-1]["Body"] == "¡Hola! ¿En qué te ayudo?"
        assert [r.model_role for r in env.provider.requests] == ["router"]
        assert "You answer for ACME Dental" in env.provider.requests[0].system  # the persona

        await _say(client, headless, "¿Tienen cita mañana?", "SM2")
        assert env.texts("twilio")[-1]["Body"] == "Sí, mañana a las 10."
        main = env.provider.requests[2]
        assert main.model_role == "main"
        texts = [m.text() for m in main.messages if m.role is Role.USER]
        assert texts == ["Hola", "¿Tienen cita mañana?"]  # the routed turn is in the history

        await _say(client, headless, "gracias", "SM3")
        assert env.texts("twilio")[-1]["Body"] == "Con gusto."
        await _say(client, headless, "reserva el lunes", "SM4")
        assert env.texts("twilio")[-1]["Body"] == "Te ayudo a reservar."  # routers never act

        await headless.fire_agent("ops", "daily report")
        await headless.worker().drain()
        assert env.provider.requests[-1].model_role == "main"
        answered = await inst.audit.records(inst.scope, action="router_answered")
        assert [r.subject for r in answered] == ["greeting"]
        spent = await inst.spend.day(inst.scope)
        assert "role:router" in spent or spent == {}  # priced when the model has a price


def test_the_tail_starts_at_a_user_message() -> None:
    use = ToolUseBlock(id="t1", name="notes.read", input={"key": "x"})
    result = ToolResultBlock(tool_use_id="t1", status=ToolStatus.OK, content="note")
    messages = [
        Message.user("a" * 4000),
        Message.assistant("b" * 4000),
        Message.user("c" * 400),
        Message(role=Role.ASSISTANT, content=[use]),
        Message(role=Role.USER, content=[result]),
        Message.assistant("d" * 400),
        Message.user("e" * 400),
        Message.assistant("f"),
    ]
    cut = split_point(messages, keep_tokens=50)
    assert messages[cut].text() == "e" * 400
    cut = split_point(messages, keep_tokens=200)
    assert messages[cut].text() == "c" * 400  # never between a tool call and its result
    assert split_point([Message.user("only")], 10) == 0


def _compaction(spec: dict[str, Any]) -> None:
    spec["models"]["roles"]["compaction"] = ROLE
    spec["agents"]["front"]["context_tokens"] = 2000


async def test_long_conversations_are_compacted_and_resume_the_same(tmp_path: Path) -> None:
    long = "Mi historial: " + "tratamiento de ortodoncia, " * 230
    script = [
        calls(("n1", "notes.write", {"key": "ana", "text": "ortodoncia"})),
        Message.assistant("Anotado."),
        Message.assistant("Entendido, " + "x" * 2500),
        Message.assistant("Ana quiere ortodoncia; se anotó en notes. Pendiente: fecha."),  # summary
        Message.assistant("¿Qué día prefieres?"),
    ]
    env = Env(tmp_path, script, edit=_compaction)
    inst, headless, client = await env.open()
    async with inst, client:
        await _say(client, headless, long, "SM1")
        await _say(client, headless, "¿Y el precio?", "SM2")
        await _say(client, headless, "Quiero una cita", "SM3")

        summary_request = env.provider.requests[3]
        assert summary_request.model_role == "compaction"
        assert "assistant called notes.write" in summary_request.messages[0].text()
        main = env.provider.requests[4]
        first = main.messages[0].text()
        assert first.startswith(SUMMARY_HEADER) and "Pendiente: fecha" in first
        assert main.messages[-1].text() == "Quiero una cita"
        assert estimate_tokens(main.messages) < 2000
        assert main.system.startswith("You answer for ACME Dental")  # the pinned part stays

        [session_id] = await inst.store.list_sessions(inst.scope)
        resumed = await inst.store.load(inst.scope, session_id)
        assert [m.text() for m in resumed.messages] == [m.text() for m in main.messages] + [
            "¿Qué día prefieres?"
        ]
        assert resumed.contact_key == ANA


async def test_no_compaction_role_means_nothing_is_cut(tmp_path: Path) -> None:
    def edit(spec: dict[str, Any]) -> None:
        spec["agents"]["front"]["context_tokens"] = 2000

    script = [Message.assistant("a" * 5000), Message.assistant("ok")]
    env = Env(tmp_path, script, edit=edit)
    inst, headless, client = await env.open()
    async with inst, client:
        await _say(client, headless, "hola " * 800, "SM1")
        await _say(client, headless, "sigue", "SM2")
        assert len(env.provider.requests[1].messages) == 3


TOO_LONG = "prompt is too long: 213512 tokens > 200000 maximum"


async def test_a_turn_too_long_for_the_model_is_compacted_and_retried(tmp_path: Path) -> None:
    from dif_general_harness.providers.base import ContextOverflow

    def edit(spec: dict[str, Any]) -> None:
        spec["models"]["roles"]["compaction"] = ROLE  # the default budget: no early compaction

    script = [
        Message.assistant("Hola Ana."),
        Message.assistant("La limpieza cuesta 600 MXN."),
        ContextOverflow(TOO_LONG),  # the provider refuses the third turn as too long
        Message.assistant("Ana preguntó el precio de la limpieza (600 MXN)."),  # the summary
        Message.assistant("¿Qué día prefieres?"),
    ]
    env = Env(tmp_path, script, edit=edit)
    inst, headless, client = await env.open()
    async with inst, client:
        await _say(client, headless, "Hola, soy Ana", "SM1")
        await _say(client, headless, "¿Cuánto cuesta la limpieza?", "SM2")
        await _say(client, headless, "Quiero una cita", "SM3")
        assert env.provider.requests[3].model_role == "compaction"
        retry = env.provider.requests[4]
        assert retry.messages[0].text().startswith(SUMMARY_HEADER)
        assert [m.text() for m in retry.messages].count("Quiero una cita") == 1  # not re-added
        assert retry.messages[-1].text() == "Quiero una cita"
        assert await inst.inbox.list() == []  # recovered: nobody needs to step in
        [session_id] = await inst.store.list_sessions(inst.scope)
        resumed = await inst.store.load(inst.scope, session_id)
        assert resumed.messages[-1].text() == "¿Qué día prefieres?"


async def test_without_compaction_a_turn_too_long_goes_to_a_person(tmp_path: Path) -> None:
    from dif_general_harness.providers.base import ContextOverflow

    env = Env(tmp_path, [ContextOverflow(TOO_LONG)])
    inst, headless, client = await env.open()
    async with inst, client:
        await _say(client, headless, "Hola", "SM1")
        [item] = await inst.inbox.list()
        assert item.title == "Turn ended with overflow"


def test_providers_say_when_the_history_is_too_long() -> None:
    from dif_general_harness.providers.base import is_overflow

    assert is_overflow(TOO_LONG)
    assert is_overflow("This model's maximum context length is 128000 tokens.")
    assert is_overflow("input length and `max_tokens` exceed context limit: 199000 + 64000")
    assert not is_overflow("Invalid API key")
