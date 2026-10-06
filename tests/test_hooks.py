"""Hooks: signed JSON POSTs to a client's own system on what happened in a conversation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2

from dif_general_harness.core.messages import Message
from dif_general_harness.service.hooks import verify
from tests.support import API_TOKEN, Env, calls

KEY = "hook-signing-key"


def _hooks(texts: bool) -> Any:
    def edit(spec: dict[str, Any]) -> None:
        spec["secrets"]["hooks_key"] = {"description": "signs hook deliveries"}
        spec.setdefault("policies", {})["permissions"] = {"allow": ["notes.*"]}
        spec["hooks"] = {
            "crm": {"url": "https://crm.example.com/dif", "events": ["tool_call", "turn_end"],
                    "secret": {"$secret": "hooks_key"}, "tools": ["notes.*"], "texts": texts},
        }  # fmt: skip

    return edit


async def _run(tmp_path: Path, texts: bool, status: int = 200) -> tuple[Env, list[httpx2.Request]]:
    got: list[httpx2.Request] = []

    def crm(request: httpx2.Request) -> httpx2.Response | None:
        if request.url.host != "crm.example.com":
            return None
        got.append(request)
        return httpx2.Response(status if len(got) == 1 else 200)

    script = [calls(("w1", "notes.write", {"key": "cita", "text": "lunes"})),
              Message.assistant("Anotado, ana@example.com.")]  # fmt: skip
    env = Env(tmp_path, script, edit=_hooks(texts), secrets={"hooks_key": KEY}, routes=crm)
    inst, headless, client = await env.open()
    async with inst, client:
        r = await client.post("/channels/api", json={"contact": "ana@example.com",
                              "text": "anota lunes"},
                              headers={"authorization": f"Bearer {API_TOKEN}"})  # fmt: skip
        assert r.status_code == 200, r.text
        assert got == []  # never in the way of the reply: queued
        await headless.worker().drain()
        if status >= 300:  # the failed one is retried later
            env.clock.now += 3600
            await headless.worker().drain()
    return env, got


async def test_a_client_system_gets_signed_events_without_texts(tmp_path: Path) -> None:
    _, got = await _run(tmp_path, texts=False)
    events = {r.headers["x-dif-event"]: r for r in got}
    assert set(events) == {"tool_call", "turn_end"}
    for request in got:
        stamp, signature = request.headers["x-dif-timestamp"], request.headers["x-dif-signature"]
        assert verify(KEY, stamp, request.content, signature)
        assert not verify("another-key", stamp, request.content, signature)
        assert "ana@example.com" not in request.content.decode()  # no texts, no contact
    call = json.loads(events["tool_call"].content)
    assert call["tool"] == "notes.write" and call["status"] == "ok" and call["effect"] == "write"
    assert call["tenant"] == "acme" and len(call["contact_ref"]) == 16
    end = json.loads(events["turn_end"].content)
    assert end["reason"] == "end_turn" and "reply" not in end


async def test_texts_go_only_when_asked_and_failures_are_retried(tmp_path: Path) -> None:
    _, got = await _run(tmp_path, texts=True, status=503)
    ends = [json.loads(r.content) for r in got if r.headers["x-dif-event"] == "turn_end"]
    assert ends and ends[-1]["reply"] == "Anotado, ana@example.com."
    assert len(got) == 3  # the first delivery failed (503) and was sent again
