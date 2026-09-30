"""G7: the voice channel (Twilio speech): greet, answer, listen again, transfer, hang up."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

from dif_general_harness.channels.gateway import signature
from dif_general_harness.channels.voice import speakable
from dif_general_harness.core.messages import Message
from tests.support import ANA, PUBLIC, TWILIO_TOKEN, Env, calls

URL = f"{PUBLIC}/channels/phone"


def _phone(spec: dict[str, Any]) -> None:
    spec["channels"]["phone"] = {
        "type": "voice",
        "provider": "twilio",
        "credentials": {"$secret": "twilio"},
        "address": "+15550002222",
        "entry_agent": "front",
        "contact_key": "phone",
        "session_window": "30m",
        "voice": {"language": "es-MX", "voice": "Polly.Mia", "transfer_to": "+525511112222",
                  "hints": ["limpieza", "ortodoncia"]},
    }  # fmt: skip


def _call(**params: str) -> tuple[str, dict[str, str]]:
    form = {"CallSid": "CA1", "From": ANA, "To": "+15550002222", **params}
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "x-twilio-signature": signature(TWILIO_TOKEN, URL, form),
    }
    return urlencode(form), headers


async def _post(client: Any, **params: str) -> str:
    body, headers = _call(**params)
    response = await client.post("/channels/phone", content=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/xml")
    return str(response.text)


async def test_a_phone_conversation(tmp_path: Path) -> None:
    script = [
        Message.assistant("Abrimos a las **9** de la mañana [1].\n\nSources:\n[1] FAQ — Horario"),
        calls(("h1", "handoff.human", {"reason": "the caller asks for a person"})),
        Message.assistant("Claro, le comunico."),
    ]
    env = Env(tmp_path, script, edit=_phone)
    inst, _, client = await env.open()
    async with inst, client:
        greeting = await _post(client, CallStatus="ringing")
        assert '<Gather input="speech" method="POST" language="es-MX"' in greeting
        assert f'action="{URL}"' in greeting and 'hints="limpieza, ortodoncia"' in greeting
        assert "Hola, ¿en qué le puedo ayudar?" in greeting and "<Hangup/>" in greeting
        assert env.provider.requests == []  # nobody spoke yet: no model call

        answer = await _post(client, CallStatus="in-progress", SpeechResult="¿A qué hora abren?")
        assert '<Say language="es-MX" voice="Polly.Mia">Abrimos a las 9 de la mañana.</Say>' in (
            answer
        )
        assert "Sources" not in answer and "[1]" not in answer and "<Gather" in answer
        assert env.provider.requests[0].messages[-1].text() == "¿A qué hora abren?"

        transfer = await _post(client, CallStatus="in-progress",
                               SpeechResult="Quiero hablar con una persona")  # fmt: skip
        assert "Le comunico con una persona." in transfer
        assert "<Dial>+525511112222</Dial>" in transfer and "<Gather" not in transfer
        [session_id] = await inst.store.list_sessions(inst.scope)  # one call, one conversation
        [item] = await inst.inbox.list(kind="escalation")
        assert item.session_id == session_id

        silent = await _post(client, CallStatus="in-progress", SpeechResult="")
        assert "Gracias por llamar. Hasta luego." in silent and "<Hangup/>" in silent
        assert await _post(client, CallStatus="completed") == "<Response/>"

        forged = await client.post(
            "/channels/phone", content=urlencode({"From": ANA, "SpeechResult": "hola"}),
            headers={"content-type": "application/x-www-form-urlencoded",
                     "x-twilio-signature": "forged"},
        )  # fmt: skip
        assert forged.status_code == 401


async def test_outbound_calls_speak_the_message(tmp_path: Path) -> None:
    env = Env(tmp_path, [], edit=_phone)
    inst, headless, client = await env.open()
    async with inst, client:
        sent = await headless.message("phone", ANA, "Recordatorio: su cita es **mañana** [1].")
        assert sent
        [request] = [r for r in env.sent if r.url.path.endswith("/Calls.json")]
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["From"] == "+15550002222" and form["To"] == ANA
        assert (
            '<Say language="es-MX" voice="Polly.Mia">Recordatorio: su cita es mañana.</Say>'
            in (form["Twiml"])
        )


def test_replies_are_made_speakable() -> None:
    text = (
        "Aceptamos **tarjetas** [1] y efectivo [2].\nVea https://acme.mx/pagos\n\nFuentes:\n[1] FAQ"
    )
    assert speakable(text) == "Aceptamos tarjetas y efectivo. Vea"
    assert speakable("# Horario\n- lunes a viernes") == "Horario - lunes a viernes"
