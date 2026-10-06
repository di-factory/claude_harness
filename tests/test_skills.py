"""Skills in packs: listed by name and description in every agent's prompt, read in full
with ``skills.load`` only when a request needs one."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dif_general_harness.core.messages import Message
from tests.support import API_TOKEN, Env, calls

REFUND = """---
name: refund
description: A customer asks for their money back.
---
Never promise a refund; hand the conversation to a person.
"""


def _skills(root: Path) -> Any:
    def edit(spec: dict[str, Any]) -> None:
        folder = root / "skills" / "refund"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(REFUND)
        (folder / "wording.md").write_text("Lo sentimos mucho.")
        spec["skills"] = [str(root / "skills")]

    return edit


async def test_an_agent_reads_a_skill_when_a_request_needs_it(tmp_path: Path) -> None:
    script = [
        calls(("s1", "skills.load", {"name": "refund"}),
              ("s2", "skills.load", {"name": "refund", "file": "wording.md"}),
              ("s3", "skills.load", {"name": "refund", "file": "../../secrets"})),
        Message.assistant("Lo sentimos mucho; una persona le escribirá."),
    ]  # fmt: skip
    env = Env(tmp_path, script, edit=_skills(tmp_path))
    inst, _, client = await env.open()
    async with inst, client:
        r = await client.post("/channels/api", json={"contact": "a@x.com", "text": "Reembolso"},
                              headers={"authorization": f"Bearer {API_TOKEN}"})  # fmt: skip
        assert r.status_code == 200, r.text
    first = env.provider.requests[0]
    assert "- refund: A customer asks for their money back." in first.system
    assert "Never promise a refund" not in first.system  # only the index, until it is loaded
    results = env.provider.requests[1].messages[-1].content
    assert "Never promise a refund" in str(results[0].content)
    assert "Lo sentimos mucho." in str(results[1].content)
    assert results[2].status == "error"  # nothing outside the skill's folder
