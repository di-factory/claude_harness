"""Small hardening (decision 92): a per-run tool-call budget, results capped with a way to
get the rest, warnings for agents with too many or look-alike tools, and failures classified
in run records for the weekly review."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dif_general_harness.core.loop import LoopConfig, run
from dif_general_harness.core.messages import Message, ToolUseBlock
from dif_general_harness.core.scope import Scope
from dif_general_harness.core.session import Session
from dif_general_harness.providers import FakeProvider
from dif_general_harness.tools.registry import MAX_RESULT_CHARS, ToolRegistry, tool
from dif_general_harness.workflows.records import RunRecords
from tests.support import Env, calls

SCOPE = Scope(tenant_id="acme", instance_id="desk")


@tool("crm.find")
async def find(name: str) -> Any:
    return {"rows": ["x" * 100] * 1000} if name == "all" else f"found {name}"


async def test_a_run_stops_when_its_tool_calls_are_spent() -> None:
    session = Session(scope=SCOPE, agent_id="a")
    second = calls(("2", "crm.find", {"name": "b"}), ("3", "crm.find", {"name": "c"}))
    provider = FakeProvider([calls(("1", "crm.find", {"name": "a"})), second])
    events = [e async for e in run(session, "go", provider, ToolRegistry([find]),
                                   LoopConfig(max_tool_calls=2))]  # fmt: skip
    assert events[-1].reason == "budget"  # type: ignore[attr-defined]
    last = session.messages[-1].content
    assert all("tool-call budget" in (b.error or "") for b in last)  # 2 + 1 > 2: none ran


async def test_a_large_result_is_cut_with_a_way_to_get_the_rest() -> None:
    result = await ToolRegistry([find]).execute(ToolUseBlock(id="1", name="crm.find",
                                                             input={"name": "all"}))  # fmt: skip
    assert isinstance(result.content, str) and len(result.content) < MAX_RESULT_CHARS + 200
    assert "[truncated: " in result.content and "a filter, a page" in result.content
    small = await ToolRegistry([find]).execute(ToolUseBlock(id="2", name="crm.find",
                                                            input={"name": "Ana"}))  # fmt: skip
    assert small.content == "found Ana"


async def test_look_alike_tools_are_flagged_when_the_instance_starts(tmp_path: Path) -> None:
    def twins(spec: dict[str, Any]) -> None:
        spec["tools"]["http"] = {"crm": {"base_url": "https://crm.example.com", "operations": {
            "find_customer": {"method": "GET", "path": "/c", "effect": "read"},
            "lookup_customer": {"method": "GET", "path": "/c", "effect": "read"}}}}  # fmt: skip
        spec["agents"]["front"]["tools"].append("crm.*")

    env = Env(tmp_path, [Message.assistant("ok")], edit=twins)
    inst, _, client = await env.open()
    async with inst, client:
        found = [i for i in inst.issues if i.code == "overlapping_tools"]
    assert found and "crm.find_customer and crm.lookup_customer" in found[0].message


async def test_failures_are_classified_for_the_weekly_review(tmp_path: Path) -> None:
    from dif_general_harness.service.review import signals

    env = Env(tmp_path, [])
    inst, _, client = await env.open()
    async with inst, client:
        records = RunRecords(inst.db, inst.scope)
        await records.append("workflow", "w", started=0, stop_reason="cap_agents", failures=[
            {"item": "a", "gate": "schema", "reason": "not JSON"},
            {"item": "b", "gate": "agent", "reason": "tool crm.book denied by rule"}])  # fmt: skip
        [record] = await records.recent()
        assert [f["class"] for f in record["failures"]] == ["contract", "authorization"]
        assert record["stop_class"] == "budget"
        found = await signals(inst)
        assert found["failure_classes"] == {"contract": 1, "authorization": 1, "budget": 1}
