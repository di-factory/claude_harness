"""``graph.query`` for agents, and the deterministic checks on a research return."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..core.untrusted import fence
from ..tools.registry import Effect, Tool, tool
from .store import GraphStore, loose

if TYPE_CHECKING:
    from ..runtime.instance import Instance


def graph_tools(instance: Instance) -> list[Tool]:
    names = sorted(instance.spec.graphs)

    @tool("graph.query", effect=Effect.READ)
    async def query(text: str, graph: str | None = None) -> str:
        """Look something up in the research graph: the matching entities with their
        sources, fields, how sure the research is, and their relationships with the
        evidence line for each. Cite the sources when you answer from it."""
        name = graph or names[0]
        if name not in instance.spec.graphs:
            raise ValueError(f"no graph {name!r}; graphs: {names}")
        store = GraphStore(instance.db, instance.scope, name, instance.spec.graphs[name])
        found = await store.search(text)
        if not found:
            return f"nothing in the {name} graph matches {text!r}"
        return fence(json.dumps(found, ensure_ascii=False, default=str), f"graph:{name}")

    return [query]


Check = Callable[[dict[str, Any]], Awaitable[str | None]]


def return_rules(store: GraphStore, asked: str, link_check: Check | None = None) -> Check:
    """The script gate for one research return (free, before any model judges it): the
    node and relation types the graph allows, the entity that was asked about, and, when
    the step asks, that every source answers."""
    spec = store.spec

    async def check(ret: dict[str, Any]) -> str | None:
        if ret.get("type") not in spec.node_types:
            return f"type {ret.get('type')!r} is not one of {spec.node_types}"
        if store.key(str(ret.get("label"))) != store.key(asked) and loose(
            store.canonical(str(ret.get("label")))
        ) != loose(store.canonical(asked)):
            return f"returned {ret.get('label')!r}, but the task was {asked!r}"
        for cand in ret.get("candidate_edges") or []:
            if cand.get("relation") not in spec.edge_types:
                return f"relation {cand.get('relation')!r} is not one of {spec.edge_types}"
        if not ret.get("sources") and not ret.get("flagged"):
            return "no sources: return it flagged when nothing could be found"
        if link_check is not None:
            return await link_check(ret)
        return None

    return check
