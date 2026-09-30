"""Sampled verification (G4): a share of tool calls, and reviews of the agents' answers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dif_general_harness.core.messages import Message, ToolUseBlock
from dif_general_harness.verify.checks import sampled
from tests.support import ADMIN_H, Env, whatsapp

VERIFIER = {"provider": "anthropic", "model": "claude-haiku-4-5"}


def _reviewed(mode: str = "always", **extra: Any) -> Any:
    def edit(spec: dict[str, Any]) -> None:
        spec["models"]["roles"]["verifier"] = VERIFIER
        spec["policies"] = {
            "verification": {
                "verifier": {"model_role": "verifier", "applies_to": ["output"], "mode": mode,
                             **extra}
            }
        }  # fmt: skip

    return edit


async def _say(client: Any, headless: Any, text: str, sid: str) -> None:
    body, headers = whatsapp(text, sid)
    await client.post("/channels/whatsapp", content=body, headers=headers)
    await headless.worker().drain()


async def test_answers_are_reviewed_after_they_are_sent(tmp_path: Path) -> None:
    script = [
        Message.assistant("Claro, le hacemos 50% de descuento en su limpieza."),
        Message.assistant(
            '{"pass": false, "reason": "It promised a discount the instructions do not allow.",'
            ' "rule": "Never promise discounts; offer to ask the clinic."}'
        ),
        Message.assistant("Abrimos de 9 a 18 h."),
        Message.assistant('{"pass": true, "reason": "Correct hours."}'),
        Message.assistant("Con gusto."),
        Message.assistant("I think it is fine"),  # an unreadable review says nothing
    ]
    env = Env(tmp_path, script, edit=_reviewed())
    inst, headless, client = await env.open()
    async with inst, client:
        await _say(client, headless, "¿Me hacen descuento?", "SM1")
        assert env.texts("twilio")[-1]["Body"].startswith("Claro, le hacemos 50%")  # sent first
        review = env.provider.requests[1]
        assert review.model_role == "verifier" and review.tools == []
        question = review.messages[0].text()
        assert "You answer for ACME Dental" in question  # judged against the instructions
        assert question.rstrip().endswith("Claro, le hacemos 50% de descuento en su limpieza.")

        [item] = await inst.inbox.list(kind="review")
        assert "discount" in item.payload["reason"] and item.session_id
        [proposed] = await inst.inbox.list(kind="constraint")
        assert proposed.payload["text"] == "Never promise discounts; offer to ask the clinic."
        assert proposed.payload["source"] == "output_review"

        await _say(client, headless, "¿Qué horario tienen?", "SM2")
        await _say(client, headless, "Gracias", "SM3")
        assert len(await inst.inbox.list(kind="review")) == 1
        metrics = (await client.get("/admin/metrics", headers=ADMIN_H)).json()
        assert metrics["answer_reviews"] == {"passed": 1, "failed": 1}
        assert metrics["answer_review_pass_rate"] == 0.5
        spent = await inst.db.fetchall("SELECT role, calls FROM usage WHERE role = 'verifier'")
        assert sum(r["calls"] for r in spent) == 3  # reviews are charged


async def test_only_the_sample_is_reviewed(tmp_path: Path) -> None:
    script = [Message.assistant(f"Respuesta {i}.") for i in range(60)]
    env = Env(tmp_path, script, edit=_reviewed("sampled", sample_rate=0.25))
    inst, headless, client = await env.open()
    async with inst, client:
        for i in range(20):
            await _say(client, headless, f"Pregunta {i}", f"SM{i}")
        roles = [r.model_role for r in env.provider.requests]
        reviews = roles.count("verifier")
        assert 1 <= reviews <= 12 and roles.count("main") == 20  # about a quarter


def test_sampling_is_deterministic_and_proportional() -> None:
    cfg = {"sample_rate": 0.3}
    picked = sum(sampled(cfg, "session", str(i)) for i in range(2000))
    assert 500 <= picked <= 700
    assert sampled(cfg, "s", "7") == sampled(cfg, "s", "7")  # a retry decides the same way
    assert not any(sampled({"sample_rate": 0}, "s", str(i)) for i in range(100))
    assert all(sampled({"sample_rate": 1}, "s", str(i)) for i in range(100))


async def test_sampled_tool_calls_get_the_verifier(tmp_path: Path) -> None:
    def edit(spec: dict[str, Any]) -> None:
        spec["models"]["roles"]["verifier"] = VERIFIER
        spec["policies"] = {
            "verification": {
                "verifier": {"model_role": "verifier", "applies_to": ["notes.write"],
                             "mode": "sampled", "sample_rate": 0.5}
            }
        }  # fmt: skip

    inst, _, client = await Env(tmp_path, [], edit=edit).open()
    async with inst, client:
        tool = inst.tools.get("notes.write")
        other = inst.tools.get("notes.read")
        checked = 0
        for i in range(200):
            call = ToolUseBlock(id=f"c{i}", name="notes.write", input={"key": "k", "text": "t"})
            checked += "__verifier__" in inst.verifier.checks_for(tool, call, "s1")
            read = ToolUseBlock(id=f"r{i}", name="notes.read", input={"key": "k"})
            assert inst.verifier.checks_for(other, read, "s1") == []  # not in applies_to
        assert 70 <= checked <= 130
