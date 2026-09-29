"""Python tools from packs (M3.8): ``tools.python`` references load as governed tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dif_general_harness.core.messages import ToolStatus, ToolUseBlock
from dif_general_harness.policy import Verdict
from dif_general_harness.providers import FakeProvider
from dif_general_harness.runtime import Instance, RuntimeOptions
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.tools.registry import Effect
from tests.support import Env

OPS = '''
from dif_general_harness.tools.registry import Effect, tool


@tool("ops.restart", effect=Effect.EXTERNAL)
async def restart(service: str) -> str:
    """Restart a service."""
    return f"restarted {service}"


def total(values: list[int], scale: int = 1) -> int:
    """Add numbers (a plain, synchronous function)."""
    return sum(values) * scale


@tool("other.thing")
async def misplaced() -> str:
    return "x"
'''


async def test_clinic_extension_is_a_read_tool(examples: Path, tmp_path: Path) -> None:
    resolved = load_instance(
        examples / "instances" / "clinica-sonrisa.json", PackCatalog(roots=[examples])
    )
    options = RuntimeOptions(state_root=tmp_path, provider=FakeProvider([]))
    async with await Instance.open(resolved, options) as inst:
        agent = inst.agent("receptionist")
        best = agent.tools.get("slots.best_slot")
        assert best is not None and best.effect is Effect.READ and best.source == "python"
        slots = [
            {"start": "2026-10-01T09:00", "calendar_id": "dr"},
            {"start": "2026-10-01T10:00", "calendar_id": "dra"},
        ]
        call = {"slots": slots, "prefer_calendar": "dra"}
        result = await agent.tools.execute(ToolUseBlock(id="1", name="slots.best_slot", input=call))
        assert result.status is ToolStatus.OK and result.content["calendar_id"] == "dra"


def _ops(spec: dict[str, Any], pack: Path) -> None:
    (pack / "extensions").mkdir()
    (pack / "extensions" / "ops.py").write_text(OPS)
    (pack / "extensions" / "broken.py").write_text("import not_a_module\n")
    spec["tools"]["python"] = [
        "extensions.ops:restart",
        "extensions.ops:total",
        "extensions.ops:misplaced",
        "extensions.broken:anything",
        "extensions.ops:nothing",
    ]
    spec["agents"]["ops"]["tools"] = ["notes.*", "ops.*"]
    spec["policies"] = {"permissions": {"allow": ["ops.total"]}}


async def test_decorated_plain_and_broken_extensions(tmp_path: Path) -> None:
    env = Env(tmp_path, [], edit=lambda spec: _ops(spec, tmp_path / "packs" / "desk"))
    inst, headless, client = await env.open()
    async with inst, client:
        tools = headless.agent("ops").tools
        assert {"ops.restart", "ops.total"} <= set(tools.names())
        restart = tools.get("ops.restart")
        assert restart is not None and restart.effect is Effect.EXTERNAL
        assert inst.policy.decide("ops.restart", Effect.EXTERNAL, {}).verdict is not Verdict.ALLOW

        added = await tools.execute(
            ToolUseBlock(id="1", name="ops.total", input={"values": [1, 2, 3], "scale": 2})
        )
        assert added.status is ToolStatus.OK and added.content == 12
        bad = await tools.execute(ToolUseBlock(id="2", name="ops.total", input={"values": "x"}))
        assert bad.status is ToolStatus.ERROR  # checked against the type hints

        problems = {i.path: i.message for i in inst.issues if i.code == "python_tool_error"}
        assert "'other.thing' must be in the 'ops' namespace" in problems["tools.python[2]"]
        assert "broken.py failed to import: ModuleNotFoundError" in problems["tools.python[3]"]
        assert "has no function 'nothing'" in problems["tools.python[4]"]
