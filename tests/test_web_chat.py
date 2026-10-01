"""The web chat: a page the instance serves, public or behind an access code."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dif_general_harness.channels import web
from dif_general_harness.core.messages import Message
from tests.support import ADMIN_H, Env, calls

VISITOR = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"


def _public(spec: dict[str, Any]) -> None:
    spec["channels"]["web"] = {"type": "web", "public": True, "entry_agent": "front"}


def _private(spec: dict[str, Any]) -> None:
    spec["secrets"]["web_code"] = {"description": "the chat's access code"}
    spec["channels"]["web"] = {
        "type": "web", "credentials": {"$secret": "web_code"}, "entry_agent": "front",
    }  # fmt: skip


async def test_a_public_chat_page_answers_and_keeps_visitors_apart(tmp_path: Path) -> None:
    script = [
        Message.assistant("Abrimos de 9 a 19."),
        calls(("h1", "handoff.human", {"reason": "wants to talk to the therapist"})),
        Message.assistant("Te comunico con una persona."),
    ]
    env = Env(tmp_path, script, edit=_public)
    inst, _, client = await env.open()
    async with inst, client:
        page = await client.get("/chat")
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]
        assert "<title>ACME</title>" in page.text and '"public": true' in page.text

        r = await client.post("/channels/web", json={"contact": VISITOR, "text": "¿Horario?"})
        assert r.status_code == 200
        assert r.json()["replies"][0]["reply"] == "Abrimos de 9 a 19."

        r = await client.post("/channels/web", json={"contact": VISITOR, "text": "Quiero hablar"})
        [item] = await inst.inbox.list(kind="escalation")
        reply = await client.post(
            f"/admin/sessions/{item.session_id}/reply",
            json={"text": "Hola, soy la terapeuta.", "by": "dra"}, headers=ADMIN_H,
        )  # fmt: skip
        assert reply.status_code == 200
        other = await client.get("/channels/web/outbox", params={"contact": OTHER})
        assert other.json() == {"messages": []}  # another visitor never sees it
        mine = await client.get("/channels/web/outbox", params={"contact": VISITOR})
        assert mine.json() == {"messages": ["Hola, soy la terapeuta."]}
        again = await client.get("/channels/web/outbox", params={"contact": VISITOR})
        assert again.json() == {"messages": []}  # each reply is given once

        bad = await client.post("/channels/web", json={"contact": "me", "text": "hola"})
        assert bad.status_code == 400  # guessable ids are refused
        long = await client.post("/channels/web", json={"contact": OTHER, "text": "x" * 2001})
        assert long.status_code == 400


async def test_a_public_chat_is_rate_limited(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(web, "PER_VISITOR", (2, 60.0))
    env = Env(tmp_path, [Message.assistant("Hola."), Message.assistant("Hola.")], edit=_public)
    inst, _, client = await env.open()
    async with inst, client:
        for _ in range(2):
            ok = await client.post("/channels/web", json={"contact": VISITOR, "text": "hola"})
            assert ok.status_code == 200
        r = await client.post("/channels/web", json={"contact": VISITOR, "text": "hola"})
        assert r.status_code == 429 and len(env.provider.requests) == 2  # no model call


async def test_a_private_chat_needs_its_access_code(tmp_path: Path) -> None:
    env = Env(tmp_path, [Message.assistant("Hola.")], edit=_private,
              secrets={"web_code": "letmein-123"})  # fmt: skip
    inst, _, client = await env.open()
    async with inst, client:
        assert '"public": false' in (await client.get("/chat/web")).text
        body = {"contact": VISITOR, "text": "hola"}
        assert (await client.post("/channels/web", json=body)).status_code == 401
        outbox = await client.get("/channels/web/outbox", params={"contact": VISITOR})
        assert outbox.status_code == 401
        auth = {"authorization": "Bearer letmein-123"}
        r = await client.post("/channels/web", json=body, headers=auth)
        assert r.json()["replies"][0]["reply"] == "Hola."
        assert (await client.get("/chat/whatsapp")).status_code == 404


def test_the_page_cannot_be_broken_by_the_business_name() -> None:
    from dif_general_harness.channels.web_page import render_chat

    page = render_chat("web", "</script><b>Hi</b>", locale="es-MX", public=True)
    assert "</script><b>" not in page and "&lt;/script&gt;" in page
    assert "Escribe tu mensaje" in page and '<html lang="es">' in page
