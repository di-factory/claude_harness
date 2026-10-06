"""The improvement loop: gates outside the agent's control (cheapest first, a retry that
carries the reason), runs that stop on counts and caps, append-only run records, and a weekly
review that proposes edits and never applies them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dif_general_harness.core.messages import Message
from dif_general_harness.service.admin_client import Admin
from dif_general_harness.workflows.records import RunRecords
from tests.support import ADMIN_H, Env

ROLE = {"provider": "anthropic", "model": "claude-haiku-4-5"}
SCHEMA = {
    "type": "object",
    "required": ["company", "confidence"],
    "properties": {"company": {"type": "string"}, "confidence": {"type": "number"}},
}


def _flow(steps: list[dict[str, Any]], stop: dict[str, Any] | None = None,
          verifier: bool = False) -> Any:  # fmt: skip
    def edit(spec: dict[str, Any]) -> None:
        spec["workflows"]["enrich"] = {"steps": steps, **({"stop": stop} if stop else {})}
        if verifier:
            spec["models"]["roles"]["verifier"] = ROLE

    return edit


def _gated(**gate: Any) -> dict[str, Any]:
    return {"id": "look", "type": "agent", "agent": "ops", "input": "Enrich Acme.",
            "gate": {"schema": SCHEMA, **gate}}  # fmt: skip


def _ret(confidence: float) -> Message:
    return Message.assistant(json.dumps({"company": "Acme", "confidence": confidence}))


async def _start(headless: Any, client: Any) -> dict[str, Any]:
    run_id = await headless.engine.start("enrich", {})
    await headless.worker().drain()
    run: dict[str, Any] = (await client.get(f"/admin/runs/{run_id}", headers=ADMIN_H)).json()
    return run


async def test_a_low_confidence_return_is_retried_once_with_the_reason(tmp_path: Path) -> None:
    env = Env(tmp_path, [_ret(0.3), _ret(0.9)], edit=_flow([_gated(threshold=0.6)]))
    inst, headless, client = await env.open()
    async with inst, client:
        run = await _start(headless, client)
        assert run["status"] == "done" and run["steps"]["look"]["confidence"] == 0.9
        retry = env.provider.requests[1].messages[-1].text()
        assert "rejected by the threshold check: confidence 0.3 is under 0.6" in retry
        [record] = await RunRecords(inst.db, inst.scope).recent()
        assert record["stop_reason"] == "completed"
        assert record["counts"] == {"agents": 2, "retried": 1, "passed": 1}
        assert record["failures"][0]["gate"] == "threshold"


async def test_a_malformed_return_is_never_retried(tmp_path: Path) -> None:
    env = Env(tmp_path, [Message.assistant("Acme is a big company.")],
              edit=_flow([_gated(threshold=0.6)]))  # fmt: skip
    inst, headless, client = await env.open()
    async with inst, client:
        run = await _start(headless, client)
        assert run["status"] == "escalated" and run["outcome"] == "needs_human"
        assert len(env.provider.requests) == 1  # no retry: the same mistake again is waste
        [item] = await inst.inbox.list(kind="review")
        assert item.payload["failures"][0]["gate"] == "schema"
        [record] = await RunRecords(inst.db, inst.scope).recent()
        assert record["stop_reason"] == "needs_human" and record["counts"]["escalated"] == 1


async def test_the_verifier_sees_only_the_task_and_the_return(tmp_path: Path) -> None:
    def no(why: str) -> Message:
        return Message.assistant(json.dumps({"pass": False, "reason": why}))

    script = [_ret(0.9), no("No source for the revenue."), _ret(0.9), no("Still no source.")]
    env = Env(tmp_path, script, edit=_flow([_gated(verify=["Every number has a source."])],
                                           verifier=True))  # fmt: skip
    inst, headless, client = await env.open()
    async with inst, client:
        run = await _start(headless, client)
        assert run["outcome"] == "needs_human"
        judged = [r for r in env.provider.requests if r.model_role == "verifier"]
        assert len(judged) == 2
        question = judged[0].messages[0].text()
        assert "Every number has a source." in question and "Enrich Acme." in question
        assert "You watch operations." not in question  # not the agent's prompt or reasoning
        retry = env.provider.requests[2].messages[-1].text()
        assert "rejected by the verifier check: No source for the revenue." in retry
        [item] = await inst.inbox.list(kind="review")
        assert [f["reason"] for f in item.payload["failures"]] == [
            "No source for the revenue.", "Still no source."]  # fmt: skip


async def test_a_run_stops_at_its_cap_and_hands_over_what_is_left(tmp_path: Path) -> None:
    steps = [{"id": "a", "type": "agent", "agent": "ops", "input": "one"},
             {"id": "b", "type": "agent", "agent": "ops", "input": "two"},
             {"id": "c", "type": "end"}]  # fmt: skip
    env = Env(tmp_path, [Message.assistant("ok")] * 3,
              edit=_flow(steps, stop={"max_agents": 1}))  # fmt: skip
    inst, headless, client = await env.open()
    async with inst, client:
        run = await _start(headless, client)
        assert run["outcome"] == "cap_agents" and "b" not in run["steps"]
        assert len(env.provider.requests) == 1
        [item] = await inst.inbox.list(kind="review")
        assert item.payload["unfinished"] == ["b", "c"]
        [record] = await RunRecords(inst.db, inst.scope).recent()
        assert record["stop_reason"] == "cap_agents" and record["unfinished"] == ["b", "c"]


async def test_a_run_stops_when_its_counted_condition_holds(tmp_path: Path) -> None:
    steps = [{"id": "a", "type": "agent", "agent": "ops", "input": "one"},
             {"id": "b", "type": "agent", "agent": "ops", "input": "two"}]  # fmt: skip
    env = Env(tmp_path, [Message.assistant("ok")] * 2,
              edit=_flow(steps, stop={"when": "counts.agents >= 1"}))  # fmt: skip
    inst, headless, client = await env.open()
    async with inst, client:
        run = await _start(headless, client)
        assert run["status"] == "done" and run["outcome"] == "condition"
        assert len(env.provider.requests) == 1
        client.headers.update(ADMIN_H)
        text = await Admin(client).runs()
        assert "workflow enrich: condition (agents 1)" in text


async def test_the_weekly_review_proposes_edits_and_never_applies_them(tmp_path: Path) -> None:
    from dif_general_harness.service.review import review

    proposal = {"proposals": [
        {"target": "prompt:ops", "why": "no source for revenue (x3)",
         "evidence": ["Acme", "Globex"],
         "new_text": "You watch operations.\nEvery number needs its source line."},
        {"target": "constraint:ops", "why": "same",
         "text": "Never state revenue without a source."},
        {"target": "prompt:nobody", "why": "unknown target", "new_text": "x"},
    ]}  # fmt: skip
    env = Env(tmp_path, [Message.assistant(json.dumps(proposal))],
              edit=_flow([{"id": "x", "type": "end"}], verifier=True))  # fmt: skip
    inst, _, client = await env.open()
    prompt = tmp_path / "packs" / "desk" / "prompts" / "ops.md"
    async with inst, client:
        records = RunRecords(inst.db, inst.scope)
        quiet = await review(inst)  # nothing repeated: no model call, nothing filed
        assert quiet["proposals"] == [] and env.provider.requests == []
        for item in ("Acme", "Globex", "Initech"):
            await records.append(
                "workflow",
                "enrich",
                started=0,
                stop_reason="needs_human",
                failures=[
                    {
                        "item": item,
                        "gate": "verifier",
                        "reason": f"No source for the revenue of {item}.",
                    }
                ],
            )
        done = await review(inst)
        assert [p["target"] for p in done["proposals"]] == ["prompt:ops", "constraint:ops"]
        asked = env.provider.requests[0].messages[0].text()
        assert '"count": 3' in asked and "=== prompt:ops\nYou watch operations." in asked
        [item] = await inst.inbox.list(kind="proposal")
        diff = item.payload["proposals"][0]["diff"]
        assert "+Every number needs its source line." in diff
        assert prompt.read_text() == "You watch operations."  # proposed, never written
        assert await inst.constraints.all("active") == []
        client.headers.update(ADMIN_H)
        text = await Admin(client).review()
        assert "## prompt:ops: no source for revenue (x3)" in text and "Nothing was changed" in text
        assert [r["stop_reason"] for r in await records.recent() if r["kind"] == "review"] == [
            "proposed", "nothing_repeated"]  # fmt: skip


async def test_scheduled_agents_and_batches_leave_a_record(tmp_path: Path) -> None:
    def batch(spec: dict[str, Any]) -> None:
        spec["triggers"]["leads"] = {"type": "batch", "agent": "ops", "dedupe_key": "item.id",
                                     "input": "Lead {{event.item.id}}"}  # fmt: skip

    env = Env(tmp_path, [Message.assistant("ok")] * 4, edit=batch)
    inst, headless, client = await env.open()
    async with inst, client:
        await headless.queue.enqueue(inst.scope, "trigger", {"trigger": "morning", "event": {}})
        await headless.worker().drain()
        r = await client.post("/admin/triggers/leads/run", headers=ADMIN_H,
                              json={"items": [{"id": 1}, {"id": 1}, {"id": 2}]})  # fmt: skip
        assert r.status_code == 200, r.text
        await headless.worker().drain()
        records = await RunRecords(inst.db, inst.scope).recent()
        by_kind = {(r["kind"], r["name"]): r for r in records}
        assert by_kind[("trigger", "morning")]["stop_reason"] == "end_turn"
        assert by_kind[("batch", "leads")]["counts"] == {
            "items": 3, "queued": 2, "skipped": 1, "truncated": 0}  # fmt: skip
        assert len([r for r in records if r["name"] == "leads"]) == 3  # the batch + 2 runs
