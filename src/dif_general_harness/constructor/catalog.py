"""Matching a request against the pack catalog.

v1 is lexical: a request ("appointment reminders for a dental clinic on WhatsApp") is scored
against each pack's id, name, line of business, description and agent descriptions. The
constructor never invents capability: when nothing scores, the answer is "no pack fits",
which is pack-design work for Di-Factory, not something to improvise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..spec.loader import Layer, PackCatalog

_WORD = re.compile(r"[a-záéíóúñ0-9]+")
_STOP = {
    "a", "an", "and", "the", "for", "of", "to", "in", "on", "with", "by", "from", "that",
    "our", "their", "its", "is", "are", "be", "we", "need", "needs", "want", "client",
    "solution", "agent", "agents", "de", "la", "el", "los", "las", "para", "con", "en", "y",
}  # fmt: skip


@dataclass(frozen=True)
class PackMatch:
    pack_id: str
    version: str
    score: float
    name: str
    lob: str
    description: str
    layer: Layer


def _words(text: str) -> set[str]:
    words = {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}
    return words | {w[:-1] for w in words if w.endswith("s") and len(w) > 4}


def _text(data: dict[str, Any]) -> str:
    sol = data.get("solution", {})
    parts = [
        sol.get("id", ""),
        sol.get("name") or "",
        sol.get("lob", ""),
        sol.get("description") or "",
    ]
    parts += [a.get("description") or "" for a in data.get("agents", {}).values()]
    parts += [c.get("type", "") for c in data.get("channels", {}).values()]
    return " ".join(p.replace("-", " ") for p in parts)


def match(catalog: PackCatalog, request: str, *, limit: int = 3) -> list[PackMatch]:
    """Packs ranked by how much of the request they cover; empty when nothing fits."""
    wanted = _words(request)
    if not wanted:
        return []
    out: list[PackMatch] = []
    for layer in catalog.latest():
        have = _words(_text(layer.data))
        score = len(wanted & have) / len(wanted)
        if score > 0:
            sol = layer.data["solution"]
            out.append(
                PackMatch(
                    sol["id"],
                    sol["version"],
                    round(score, 3),
                    sol.get("name") or sol["id"],
                    sol.get("lob", ""),
                    sol.get("description") or "",
                    layer,
                )
            )
    return sorted(out, key=lambda m: (-m.score, m.pack_id))[:limit]
