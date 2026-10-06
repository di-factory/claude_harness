"""Research graphs: a run picks its own work from the graph (a query, routed by each node's
state), every return passes a script gate before it lands, nodes land before edges, aliases
keep one entity one node, and the next run skips what is settled."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from dif_general_harness.core.messages import Message
from dif_general_harness.graph.store import GraphStore
from dif_general_harness.providers.base import ModelRequest
from dif_general_harness.service.admin_client import Admin
from dif_general_harness.workflows.records import RunRecords
from tests.support import ADMIN_H, Env

ANSWERS: dict[str, Any] = {}  # label -> a return (or a list: one per look, or raw text)
ASKED: list[str] = []


def _spec(root: Path) -> Any:
    def edit(spec: dict[str, Any]) -> None:
        aliases = root / "packs" / "desk" / "aliases.csv"
        aliases.write_text("canonical,alias\nAlphabet Inc,Google\n")
        spec["graphs"] = {"market": {
            "primary": "company", "node_types": ["company", "vendor"],
            "edge_types": ["supplies", "shared_vendor"], "verified_sources": 2,
            "aliases": "aliases.csv",
        }}  # fmt: skip
        spec["workflows"]["scan"] = {
            "steps": [{"id": "scan", "type": "foreach", "graph": "market", "agent": "ops",
                       "seed": "{{input.companies}}"}],
            "stop": {"when": "counts.passes_without_new >= 2", "max_agents": 30},
        }  # fmt: skip
        spec["agents"]["front"]["tools"].append("graph.*")

    return edit


def _ret(label: str, *sites: str, edges: list[dict[str, Any]] | None = None,
         **fields: Any) -> dict[str, Any]:  # fmt: skip
    return {"label": label, "type": "company", "confidence": 0.8, "fields": fields,
            "sources": [{"url": f"https://{s}/{label.split()[0].lower()}", "date": "2026-09-01"}
                        for s in sites], "candidate_edges": edges or []}  # fmt: skip


def researcher(request: ModelRequest) -> Message:
    task = request.messages[-1].text()
    found = re.search(r"Research: (.+?) \(", task)
    label = found.group(1) if found else "?"
    ASKED.append(f"{label}: {task.split(chr(10))[2][:40]}")
    answer = ANSWERS[label]
    if isinstance(answer, list):  # two looks at a contradiction: primary, then secondary
        answer = answer[0] if "primary sources" in task else answer[1]
    return Message.assistant(answer if isinstance(answer, str) else json.dumps(answer))


async def _scan(headless: Any, companies: list[str] | None = None) -> None:
    await headless.engine.start("scan", {"companies": companies or []})
    await headless.worker().drain()


async def test_a_graph_grows_from_its_own_state_and_skips_what_is_settled(tmp_path: Path) -> None:
    ANSWERS.clear()
    ASKED.clear()
    ANSWERS.update({
        "Acme Inc": _ret("Acme Inc", "acme.com", "reuters.com", edges=[
            {"target": "Globex", "relation": "supplies", "evidence": "10-K p.14: Globex"},
            {"target": "Initech", "relation": "supplies", "evidence": "a rumour",
             "confidence": 0.3},
        ]),
        "Globex": _ret("Globex", "globex.com", "ft.com", edges=[
            {"target": "Acme", "relation": "shared_vendor", "evidence": "both use Umbrella"}]),
        "Alphabet Inc": _ret("Alphabet Inc", "abc.xyz", hq="Mountain View"),
        "Bad Co": "Bad Co is a company I could not research.",
    })  # fmt: skip
    env = Env(tmp_path, [researcher] * 40, edit=_spec(tmp_path))
    inst, headless, client = await env.open()
    async with inst, client:
        store = GraphStore(inst.db, inst.scope, "market", inst.spec.graphs["market"])
        await _scan(headless, ["Acme Inc", "Google", "Bad Co"])

        states = {n.label: store.state(n) for n in await store.nodes()}
        assert states == {
            "Acme Inc": "fresh",
            "Alphabet Inc": "thin",
            "Bad Co": "needs_human",
            "Globex": "fresh",
        }  # "Google" is Alphabet; Initech was dropped
        assert ASKED[:3] == ["Acme Inc: Full research: identify it, pull primary",
                             "Alphabet Inc: Full research: identify it, pull primary",
                             "Bad Co: Full research: identify it, pull primary"]  # fmt: skip
        edges = {(e["from"], e["type"], e["to"]): e["evidence"]
                 for e in (await store.search("Acme"))[0]["edges"]}  # fmt: skip
        assert edges == {
            ("Acme Inc", "supplies", "Globex"): "10-K p.14: Globex",
            ("Globex", "shared_vendor", "Acme Inc"): "both use Umbrella",
        }
        [record] = await RunRecords(inst.db, inst.scope).recent()
        assert record["counts"]["verified"] == 2 and record["counts"]["edges_dropped"] == 1
        assert record["counts"]["escalated"] == 1 and record["counts"]["passes"] == 2
        assert record["alias_collisions"] == [{"label": "Acme", "existing": "Acme Inc"}]
        assert record["diff"]["nodes_added"] == ["Acme Inc", "Alphabet Inc", "Bad Co", "Globex"]
        assert record["failures"][0]["gate"] == "schema"  # malformed: a person, no retry
        assert [i.kind for i in await inst.inbox.list()] == ["review", "report"]

        # next run: the thin node gets a sources-only look; settled ones are skipped
        ASKED.clear()
        ANSWERS["Alphabet Inc"] = _ret("Alphabet Inc", "sec.gov", hq="Palo Alto")
        await _scan(headless)
        assert ASKED == ["Alphabet Inc: Sources only: find independent sources ("]
        alphabet = await store.node("Google")
        assert alphabet is not None and store.state(alphabet) == "contradicted"
        conflict = alphabet.fields["_conflicts"][0]
        assert conflict["values"] == ["Mountain View", "Palo Alto"]  # both kept, never averaged

        # a contradiction gets two independent looks; agreeing, they settle it
        ASKED.clear()
        settled = _ret("Alphabet Inc", "abc.xyz", hq="Mountain View")
        ANSWERS["Alphabet Inc"] = [settled, settled]
        await _scan(headless)
        assert [a.split(": ")[1][:14] for a in ASKED] == ["Start from pri", "Start from ind"]
        alphabet = await store.node("Alphabet Inc")
        assert alphabet is not None and store.state(alphabet) == "fresh"
        assert alphabet.fields == {"hq": "Mountain View"}

        tool = inst.tools.get("graph.query")
        assert tool is not None
        found = await tool.handler(text="Globex")
        assert "<untrusted_content" in found and "10-K p.14: Globex" in found
        client.headers.update(ADMIN_H)
        page = await Admin(client).graph("market")
        assert "## needs_human (1)" in page and "Acme Inc --supplies--> Globex" in page


async def test_an_event_scopes_the_same_launch_to_one_entity(tmp_path: Path) -> None:
    ANSWERS.clear()
    ASKED.clear()
    ANSWERS.update({"Acme Inc": _ret("Acme Inc", "acme.com"),
                    "Globex": _ret("Globex", "globex.com")})  # fmt: skip

    def scoped(spec: dict[str, Any]) -> None:
        _spec(tmp_path)(spec)
        spec["workflows"]["scan"]["steps"][0]["only"] = "{{input.entity}}"

    env = Env(tmp_path, [researcher] * 10, edit=scoped)
    inst, headless, client = await env.open()
    async with inst, client:
        store = GraphStore(inst.db, inst.scope, "market", inst.spec.graphs["market"])
        for label in ("Acme Inc", "Globex"):
            await store.ensure(label)
        await headless.engine.start("scan", {"entity": "Globex"})
        await headless.worker().drain()
        assert [a.split(":")[0] for a in ASKED] == ["Globex"]
