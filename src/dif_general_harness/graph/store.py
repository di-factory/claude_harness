"""Research graphs: memory with a shape (nodes with sources, edges with evidence).

A spec's ``graphs`` name each graph's one primary node type, the edge types, the confidence
under which a candidate edge is dropped, how many independent sources make a node verified,
when a verified node is stale, and an aliases file (``canonical,alias`` per line) checked
before every merge, so "Google" and "Alphabet Inc" are one node.

A node's **state** decides what a run does with it (``workflows/engine.py``, ``foreach``):

    fresh         verified at N independent sources, checked within ``stale_days``: skipped
    stale         verified, but not checked for ``stale_days``: one agent, changes only
    thin          checked, fewer than N independent sources: one agent, sources only
    contradicted  two looks disagreed on a field: two agents, different starting points
    new           never researched (seeded, or discovered as an edge's target): full pass
    needs_human   failed its checks twice: a person decides; runs leave it alone

"Independent" means different web sites (two pages of one site, or two press releases, are
one source). Nodes land first; a pass's edges are drawn only after all its nodes are in, and
every edge carries its evidence line.
"""

from __future__ import annotations

import csv
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.scope import Scope
from ..store.db import Database, Row

DAY = 86400.0
MAX_NODES = 5000
MAX_SOURCES = 10
_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|llc|plc|gmbh|sa|s\.a|sa de cv"
    r"|s\.a\. de c\.v|sab de cv|ag|bv|nv|the)\b\.?"
)

RETURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["label", "type", "sources", "confidence"],
    "properties": {
        "label": {"type": "string", "minLength": 1},
        "type": {"type": "string"},
        "sources": {
            "type": "array", "maxItems": 3,
            "items": {"type": "object", "required": ["url"],
                      "properties": {"url": {"type": "string", "pattern": "^https?://"},
                                     "date": {"type": "string"}}},
        },
        "fields": {"type": "object"},
        "candidate_edges": {
            "type": "array",
            "items": {"type": "object", "required": ["target", "relation", "evidence"],
                      "properties": {"target": {"type": "string", "minLength": 1},
                                     "relation": {"type": "string"},
                                     "evidence": {"type": "string", "minLength": 3},
                                     "confidence": {"type": "number", "minimum": 0,
                                                    "maximum": 1}}},
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "flagged": {"type": "boolean"},
    },
}  # fmt: skip


def normal(label: str) -> str:
    return " ".join(re.sub(r"[^\w\s&.-]", " ", label.lower()).split())


def loose(label: str) -> str:
    """A label without punctuation and legal suffixes: what a slipped duplicate shares."""
    text = _SUFFIXES.sub(" ", normal(label))
    return re.sub(r"[^\w]+", "", text)


def read_aliases(path: str | Path) -> dict[str, str]:
    """``alias -> canonical`` (both as written), from ``canonical,alias`` lines."""
    out: dict[str, str] = {}
    with Path(path).open(encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) != 2 or not row[0].strip() or not row[1].strip():
                continue
            canonical, alias = row[0].strip(), row[1].strip()
            if canonical.lower() == "canonical" and alias.lower() == "alias":
                continue  # the header
            out[alias] = canonical
    return out


def aliases_problem(path: str | Path) -> str | None:
    """What is wrong with an aliases file (for validation), or None."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return f"cannot read it: {exc.strerror}"
    seen: dict[str, str] = {}
    for number, row in enumerate(csv.reader(lines), 1):
        if not row or not any(c.strip() for c in row):
            continue
        if len(row) != 2 or not row[0].strip() or not row[1].strip():
            return f"line {number}: expected canonical,alias"
        canonical, alias = row[0].strip(), row[1].strip()
        if number == 1 and canonical.lower() == "canonical":
            continue
        if normal(alias) in seen and seen[normal(alias)] != canonical:
            return f"line {number}: {alias!r} already names {seen[normal(alias)]!r}"
        seen[normal(alias)] = canonical
    return None


def site(url: str) -> str:
    host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    parts = host.removeprefix("www.").split(".")
    two_level = len(parts) > 2 and parts[-2] in {"com", "co", "org", "gob", "gov", "net", "ac"}
    return ".".join(parts[-3:] if two_level else parts[-2:])


@dataclass
class Node:
    id: str
    label: str
    type: str
    status: str  # new | unverified | verified | contradicted | needs_human
    confidence: float
    sources: list[dict[str, Any]]
    fields: dict[str, Any]
    last_checked: float | None
    inbound: int = 0
    outbound: int = 0

    @classmethod
    def from_row(cls, row: Row) -> Node:
        return cls(
            row["id"], row["label"], row["type"], row["status"], float(row["confidence"]),
            json.loads(row["sources"]), json.loads(row["fields"]), row["last_checked"],
        )  # fmt: skip

    def independent_sources(self) -> int:
        return len({site(str(s.get("url", ""))) for s in self.sources if s.get("url")})

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "type": self.type, "status": self.status,
                "confidence": self.confidence, "sources": self.sources, "fields": self.fields,
                "last_checked": self.last_checked, "inbound": self.inbound,
                "outbound": self.outbound}  # fmt: skip


@dataclass
class Edge:
    id: str
    from_id: str
    to_id: str
    type: str
    evidence: str
    confidence: float


@dataclass
class Landed:
    """What a merge changed (for the run record's diff)."""

    added: bool = False
    verified: bool = False
    contradicted: bool = False


@dataclass
class EdgeReport:
    added: list[dict[str, Any]] = field(default_factory=list)
    dropped: int = 0
    discovered: list[str] = field(default_factory=list)
    collisions: list[dict[str, str]] = field(default_factory=list)


class GraphStore:
    def __init__(self, db: Database, scope: Scope, name: str, spec: Any) -> None:
        self.db, self.scope, self.name, self.spec = db, scope, name, spec
        self.aliases = {normal(a): c for a, c in read_aliases(spec.aliases).items()} if (
            spec.aliases and Path(spec.aliases).is_file()) else {}  # fmt: skip

    # --- names -------------------------------------------------------------------------

    def canonical(self, label: str) -> str:
        return self.aliases.get(normal(label), label.strip())

    def key(self, label: str) -> str:
        return normal(self.canonical(label))

    # --- reading -----------------------------------------------------------------------

    async def nodes(self) -> list[Node]:
        rows = await self.db.fetchall(
            "SELECT * FROM graph_nodes WHERE tenant_id = ? AND instance_id = ? AND graph = ?"
            " ORDER BY created_at LIMIT ?",
            (self.scope.tenant_id, self.scope.instance_id, self.name, MAX_NODES),
        )
        found = {r["id"]: Node.from_row(r) for r in rows}
        for edge in await self.edges():
            if edge.to_id in found:
                found[edge.to_id].inbound += 1
            if edge.from_id in found:
                found[edge.from_id].outbound += 1
        return list(found.values())

    async def node(self, label: str) -> Node | None:
        row = await self.db.fetchone(
            "SELECT * FROM graph_nodes WHERE tenant_id = ? AND instance_id = ? AND graph = ?"
            " AND label_key = ?",
            (self.scope.tenant_id, self.scope.instance_id, self.name, self.key(label)),
        )
        return Node.from_row(row) if row else None

    async def edges(self) -> list[Edge]:
        rows = await self.db.fetchall(
            "SELECT * FROM graph_edges WHERE tenant_id = ? AND instance_id = ? AND graph = ?"
            " ORDER BY created_at",
            (self.scope.tenant_id, self.scope.instance_id, self.name),
        )
        return [Edge(r["id"], r["from_id"], r["to_id"], r["type"], r["evidence"],
                     float(r["confidence"])) for r in rows]  # fmt: skip

    def state(self, node: Node, now: float | None = None) -> str:
        if node.status in ("needs_human", "contradicted"):
            return node.status
        if node.last_checked is None:
            return "new"
        if node.independent_sources() < self.spec.verified_sources:
            return "thin"
        age = ((now or time.time()) - node.last_checked) / DAY
        return "stale" if age > self.spec.stale_days else "fresh"

    def facts(self, node: Node, now: float | None = None) -> dict[str, Any]:
        """What a launch query sees of a node."""
        age = ((now or time.time()) - node.last_checked) / DAY if node.last_checked else 1e9
        return {"label": node.label, "type": node.type, "status": node.status,
                "state": self.state(node, now), "confidence": node.confidence,
                "sources": node.independent_sources(), "age_days": age,
                "inbound": node.inbound, "outbound": node.outbound}  # fmt: skip

    # --- writing -----------------------------------------------------------------------

    async def ensure(self, label: str, type_: str | None = None) -> tuple[Node, bool]:
        """The node for ``label`` (through the aliases), created as ``new`` if missing."""
        found = await self.node(label)
        if found is not None:
            return found, False
        now = time.time()
        node = Node(uuid.uuid4().hex[:12], self.canonical(label), type_ or self.spec.primary,
                    "new", 0.0, [], {}, None)  # fmt: skip
        await self.db.execute(
            "INSERT INTO graph_nodes (id, tenant_id, instance_id, graph, label_key, label, type,"
            " status, confidence, sources, fields, last_checked, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (node.id, self.scope.tenant_id, self.scope.instance_id, self.name,
             self.key(label), node.label, node.type, node.status, 0.0, "[]", "{}", None, now,
             now),
        )  # fmt: skip
        return node, True

    async def set_status(self, node: Node, status: str) -> None:
        node.status = status
        await self._save(node)

    async def _save(self, node: Node) -> None:
        await self.db.execute(
            "UPDATE graph_nodes SET status = ?, confidence = ?, sources = ?, fields = ?,"
            " last_checked = ?, type = ?, updated_at = ? WHERE id = ?",
            (node.status, node.confidence, json.dumps(node.sources, ensure_ascii=False),
             json.dumps(node.fields, ensure_ascii=False, default=str), node.last_checked,
             node.type, time.time(), node.id),
        )  # fmt: skip

    async def land(self, node: Node, returns: list[dict[str, Any]], *, recheck: bool = False,
                   now: float | None = None) -> Landed:  # fmt: skip
        """Merge gated returns into ``node``. Conflicting values are kept, both, with dates
        (never averaged) and make the node contradicted; with ``recheck`` (two fresh looks
        at a contradicted node) values both looks agree on settle it."""
        now = now if now is not None else time.time()
        was = node.status
        out = Landed()
        known = {str(s.get("url")) for s in node.sources}
        for ret in returns:
            for src in ret.get("sources") or []:
                if str(src.get("url")) not in known and len(node.sources) < MAX_SOURCES:
                    node.sources.append({"url": str(src["url"]), "date": src.get("date")})
                    known.add(str(src.get("url")))
        earlier = [] if recheck else list(node.fields.get("_conflicts") or [])
        conflicts: list[dict[str, Any]] = earlier
        values: dict[str, list[Any]] = {}
        for ret in returns:
            for key, value in (ret.get("fields") or {}).items():
                values.setdefault(str(key), []).append(value)
        for key, seen in values.items():
            distinct = {json.dumps(v, sort_keys=True, default=str) for v in seen}
            current = node.fields.get(key)
            if len(distinct) > 1:
                conflicts.append({"field": key, "values": seen, "checked": now})
                continue
            value = seen[0]
            if (current is not None and not recheck and json.dumps(current, sort_keys=True,
                default=str) != json.dumps(value, sort_keys=True, default=str)):  # fmt: skip
                conflicts.append({"field": key, "values": [current, value], "checked": now})
                continue
            node.fields[key] = value
        if conflicts:
            node.fields["_conflicts"] = conflicts[-20:]
        else:
            node.fields.pop("_conflicts", None)
        if any(r.get("flagged") for r in returns):
            node.fields["_flagged"] = True
        confidences = [float(r["confidence"]) for r in returns if "confidence" in r]
        node.confidence = min(confidences) if confidences else node.confidence
        node.last_checked = now
        if conflicts:
            node.status = "contradicted"
        elif node.independent_sources() >= self.spec.verified_sources:
            node.status = "verified"
        else:
            node.status = "unverified"
        await self._save(node)
        out.added = was == "new"
        out.verified = node.status == "verified" and was != "verified"
        out.contradicted = node.status == "contradicted" and was != "contradicted"
        return out

    async def draw(self, origin: Node, returns: list[dict[str, Any]], run_id: str | None,
                   known: list[Node]) -> EdgeReport:  # fmt: skip
        """The candidate edges of ``returns``, after every node of the pass has landed: below
        the threshold dropped, unknown targets added as new nodes, near-duplicates of a
        node caught (and reported, so the aliases file gets the line)."""
        report = EdgeReport()
        by_loose = {loose(n.label): n for n in known}
        for ret in returns:
            default = float(ret.get("confidence", 0.0))
            for cand in ret.get("candidate_edges") or []:
                confidence = float(cand.get("confidence", default))
                if confidence < self.spec.threshold:
                    report.dropped += 1
                    continue
                label = str(cand["target"]).strip()
                target = await self.node(label)
                if target is None and (twin := by_loose.get(loose(self.canonical(label)))):
                    report.collisions.append({"label": label, "existing": twin.label})
                    target = twin
                if target is None:
                    target, _ = await self.ensure(label)
                    report.discovered.append(target.label)
                    by_loose[loose(target.label)] = target
                if target.id == origin.id:
                    continue
                edge_id = uuid.uuid4().hex[:12]
                exists = await self.db.fetchone(
                    "SELECT id FROM graph_edges WHERE tenant_id = ? AND instance_id = ?"
                    " AND graph = ? AND from_id = ? AND to_id = ? AND type = ?",
                    (self.scope.tenant_id, self.scope.instance_id, self.name, origin.id,
                     target.id, str(cand["relation"])),
                )  # fmt: skip
                if exists:
                    continue
                await self.db.execute(
                    "INSERT INTO graph_edges (id, tenant_id, instance_id, graph, from_id, to_id,"
                    " type, evidence, confidence, run_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (edge_id, self.scope.tenant_id, self.scope.instance_id, self.name,
                     origin.id, target.id, str(cand["relation"]), str(cand["evidence"])[:500],
                     confidence, run_id, time.time()),
                )  # fmt: skip
                report.added.append({"from": origin.label, "to": target.label,
                                     "type": str(cand["relation"])})  # fmt: skip
        return report

    # --- views -------------------------------------------------------------------------

    async def search(self, text: str, limit: int = 10) -> list[dict[str, Any]]:
        """Nodes whose label or fields mention ``text``, with their sources and edges."""
        words = [w for w in normal(text).split() if len(w) > 2] or [normal(text)]
        nodes = await self.nodes()
        by_id = {n.id: n for n in nodes}
        scored = []
        for node in nodes:
            hay = normal(node.label + " " + json.dumps(node.fields, ensure_ascii=False))
            score = sum(hay.count(w) for w in words) + (5 if normal(node.label) in normal(text)
                                                         else 0)  # fmt: skip
            if score:
                scored.append((score, node))
        scored.sort(key=lambda p: (-p[0], -p[1].inbound))
        edges = await self.edges()
        out = []
        for _, node in scored[:limit]:
            related = [
                {"from": by_id[e.from_id].label, "to": by_id[e.to_id].label, "type": e.type,
                 "evidence": e.evidence, "confidence": e.confidence}
                for e in edges if node.id in (e.from_id, e.to_id)
                and e.from_id in by_id and e.to_id in by_id
            ]  # fmt: skip
            out.append({**node.public(), "state": self.state(node), "edges": related[:20]})
        return out

    async def markdown(self) -> str:
        """The graph for a person: nodes by state with their sources, then the edges."""
        nodes = await self.nodes()
        by_id = {n.id: n for n in nodes}
        out = [f"# Graph: {self.name}", ""]
        order = ["contradicted", "needs_human", "thin", "new", "stale", "fresh"]
        for state in order:
            group = [n for n in nodes if self.state(n) == state]
            if not group:
                continue
            out += [f"## {state} ({len(group)})", ""]
            for n in sorted(group, key=lambda n: -n.inbound):
                srcs = ", ".join(str(s.get("url")) for s in n.sources[:3]) or "no sources yet"
                out.append(f"- **{n.label}** ({n.type}, confidence {n.confidence:.2f},"
                           f" {n.inbound} in): {srcs}")  # fmt: skip
                for c in n.fields.get("_conflicts") or []:
                    out.append(f"  - conflict on {c['field']}: {c['values']}")
            out.append("")
        edges = await self.edges()
        if edges:
            out += [f"## Edges ({len(edges)})", ""]
            for e in edges:
                if e.from_id in by_id and e.to_id in by_id:
                    out.append(f"- {by_id[e.from_id].label} --{e.type}--> {by_id[e.to_id].label}"
                               f" ({e.confidence:.2f}): {e.evidence}")  # fmt: skip
        return "\n".join(out).rstrip() + "\n"
