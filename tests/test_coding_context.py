"""The coding pack reads the workspace's own instructions (CLAUDE.md, AGENTS.md) every turn."""

from __future__ import annotations

from pathlib import Path

from dif_general_harness.core.messages import Message
from dif_general_harness.providers import FakeProvider
from dif_general_harness.runtime import Instance, RuntimeOptions
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.tenancy.secrets import EnvSecrets


async def test_the_repository_instructions_reach_the_agent(
    tmp_path: Path, helper_solution: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("Run `make check` before saying a change is done.")
    provider = FakeProvider([Message.assistant("ok"), Message.assistant("ok")])
    resolved = load_instance(helper_solution, PackCatalog(roots=[tmp_path / "packs"]))
    options = RuntimeOptions(state_root=tmp_path / "state", secrets=EnvSecrets({}),
                             workspaces={"repo": repo}, provider=provider)  # fmt: skip
    async with await Instance.open(resolved, options) as inst:
        agent = inst.agent()
        session = await agent.new_session()
        async for _ in agent.send(session, "hola"):
            pass
        (repo / "AGENTS.md").write_text("Tests live in tests/.")  # read again every turn
        async for _ in agent.send(session, "otra"):
            pass
    first, second = (r.system for r in provider.requests)
    assert "## The repository's own instructions" in first
    assert "Run `make check`" in first and "### CLAUDE.md" in first
    assert "Tests live in tests/." not in first and "### AGENTS.md" in second
    assert "make check" not in agent.system  # the agent's own prompt is unchanged
