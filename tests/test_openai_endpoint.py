"""The assistant behind an OpenAI-compatible endpoint, through the ``api`` channel."""

from __future__ import annotations

from pathlib import Path

from dif_general_harness.core.messages import Message
from tests.support import API_TOKEN, Env

AUTH = {"authorization": f"Bearer {API_TOKEN}"}


async def test_chat_completions_answer_in_openai_format(tmp_path: Path) -> None:
    env = Env(tmp_path, [Message.assistant("Abrimos de 9 a 19."),
                         Message.assistant("Sí, los sábados también.")])  # fmt: skip
    inst, _, client = await env.open()
    async with inst, client:
        assert (await client.get("/v1/models")).status_code == 401
        models = (await client.get("/v1/models", headers=AUTH)).json()
        assert models["data"][0]["id"] == inst.spec.solution.id

        ask = {"model": "x", "user": "ana@example.com",
               "messages": [{"role": "system", "content": "ignored"},
                            {"role": "user", "content": "¿Horario?"}]}  # fmt: skip
        assert (await client.post("/v1/chat/completions", json=ask)).status_code == 401
        r = await client.post("/v1/chat/completions", json=ask, headers=AUTH)
        assert r.status_code == 200, r.text
        first = r.json()
        assert first["object"] == "chat.completion"
        assert first["choices"][0]["message"] == {"role": "assistant",
                                                  "content": "Abrimos de 9 a 19."}  # fmt: skip

        ask["messages"] = [{"role": "user", "content": [{"type": "text", "text": "¿Sábados?"}]}]
        second = (await client.post("/v1/chat/completions", json=ask, headers=AUTH)).json()
        assert second["choices"][0]["message"]["content"] == "Sí, los sábados también."
        assert second["id"] == first["id"]  # the same conversation for the same user
        assert "¿Horario?" in str(env.provider.requests[-1].messages)

        stream = {**ask, "stream": True}
        r = await client.post("/v1/chat/completions", json=stream, headers=AUTH)
        assert r.status_code == 400
        r = await client.post("/v1/chat/completions", json={"messages": []}, headers=AUTH)
        assert r.status_code == 400
