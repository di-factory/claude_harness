"""The weekly review: the solution's own failures, turned into proposed edits for a person.

Once a week (with a ``verifier`` role; ``DIF_REVIEW=off`` turns it off) the service reads
what went wrong in the last seven days:

- run records (``workflows/records.py``): gate failures whose reason came back 2+ times,
  runs that stopped on a cap or needed a person, alias collisions caught at a merge;
- the inbox: conversations escalated, answers that failed review, items for one contact or
  one task more than once;
- the FAQ gaps still open, and candidate constraints nobody has decided yet.

Nothing repeated: the review ends there, without a model call. Otherwise a fresh model
(no conversation, only those signals and the solution's current instructions: each agent's
prompt, the skills, the pinned constraints, a graph's aliases) proposes edits. The review
**never writes**: it files one ``proposal`` item in the inbox with each edit as a diff
against the current text and the evidence for it, and tells the staff. A person applies what
they accept the usual way (the setup or ``adjust``; the replay and Jag's signature still
apply), or adds a constraint from the item. An agent that could edit its own constraints
unreviewed would, sooner or later, remove the inconvenient one.
"""

from __future__ import annotations

import difflib
import json
import re
import time
from collections import Counter
from typing import TYPE_CHECKING, Any

from ..core.messages import Message
from ..providers.base import ModelRequest, ProviderMessage
from ..runtime.prompts import load_text
from ..workflows.records import DAY, RunRecords

if TYPE_CHECKING:
    from ..runtime.instance import Instance

WEEK = 7 * DAY
TEXT_CHARS = 8000  # of each current instruction shown to the reviewer
REVIEW_SYSTEM = """You review one week of an AI assistant's failures and propose edits to
its instructions so the same failures stop. You get the repeated failures (with counts and
examples) and the current instructions. Propose only what the evidence supports: a failure
seen once is not a pattern. Prefer the smallest edit: one rule, one step, one alias.

Targets:
- "prompt:<agent>": the agent's prompt; give its complete new text in "new_text";
- "skill:<name>": a skill's steps; give the complete new text in "new_text";
- "constraint:<agent>": one new rule for that agent ("*" for all) in "text";
- "faq": one missing answer in "text" (only facts the evidence states; else say what to ask
  the owner);
- "alias:<graph>": one line "canonical,alias" in "text".

Answer with JSON only: {"proposals": [{"target": "...", "why": "<the failure, with its
count>", "evidence": ["<short example>", ...], "new_text": "...", "text": "..."}]}; an empty
list when nothing repeated enough."""


def enabled(inst: Instance) -> bool:
    import os

    roles = inst.spec.models.roles if inst.spec.models else {}
    return os.environ.get("DIF_REVIEW", "").lower() != "off" and "verifier" in roles


async def signals(inst: Instance, *, now: float | None = None) -> dict[str, Any]:
    """What went wrong in the last week, with only what repeated."""
    now = now if now is not None else time.time()
    records = await RunRecords(inst.db, inst.scope).recent(7, now=now)
    reasons: Counter[tuple[str, str]] = Counter()
    examples: dict[tuple[str, str], list[str]] = {}
    items_failing: Counter[str] = Counter()
    collisions: list[Any] = []
    stops: Counter[str] = Counter()
    for record in records:
        if record["stop_reason"] not in ("completed", "end_turn", "queued", "condition"):
            stops[f"{record['kind']} {record['name']}: {record['stop_reason']}"] += 1
        for failure in record.get("failures") or []:
            item = str(failure.get("item") or "")
            reason = str(failure.get("reason"))
            if item:  # "No source for Acme" and "No source for Globex" are one failure
                reason = re.sub(re.escape(item), "<item>", reason, flags=re.IGNORECASE)
            key = (str(failure.get("gate")), _normal(reason))
            reasons[key] += 1
            examples.setdefault(key, []).append(str(failure.get("item") or ""))
            items_failing[str(failure.get("item") or "")] += 1
        collisions += record.get("alias_collisions") or []
    inbox = [i for i in await inst.inbox.list(None) if i.created_at >= now - WEEK]
    kinds = Counter(i.kind for i in inbox if i.kind in ("escalation", "review", "budget"))
    by_contact = Counter(
        str(i.payload.get("contact")) for i in inbox if i.kind == "escalation"
        and i.payload.get("contact")
    )  # fmt: skip
    titles = Counter(_normal(i.title) for i in inbox if i.kind in ("escalation", "review"))
    gaps = await inst.knowledge.gaps("open", limit=10) if inst.spec.knowledge.corpora else []
    pending = [c for c in await inst.constraints.all("candidate")]
    return {
        "repeated_failures": [
            {
                "gate": gate,
                "reason": reason,
                "count": count,
                "examples": examples[(gate, reason)][:5],
            }
            for (gate, reason), count in reasons.most_common()
            if count >= 2
        ],
        "items_failing_again": [item for item, n in items_failing.items() if item and n >= 2],
        "runs_not_completed": [f"{what} (x{n})" for what, n in stops.most_common(10) if n >= 2],
        "alias_collisions": collisions[:20],
        "inbox_this_week": dict(kinds),
        "repeated_inbox_titles": [f"{t} (x{n})" for t, n in titles.most_common(10) if n >= 2],
        "contacts_escalated_again": sum(1 for n in by_contact.values() if n >= 2),
        "faq_gaps": [
            {"question": g["question"], "asked": g["asked"]} for g in gaps if int(g["asked"]) >= 2
        ],
        "constraints_waiting": [c.text for c in pending if c.occurrences >= 2][:10],
    }


def worth_reviewing(found: dict[str, Any]) -> bool:
    keys = ("repeated_failures", "items_failing_again", "alias_collisions", "runs_not_completed",
            "repeated_inbox_titles", "faq_gaps", "constraints_waiting")  # fmt: skip
    return any(found.get(k) for k in keys) or bool(found.get("contacts_escalated_again"))


def instructions(inst: Instance) -> dict[str, str]:
    """The current text of everything the review may propose to change."""
    out: dict[str, str] = {}
    for name, agent in inst.spec.agents.items():
        try:
            out[f"prompt:{name}"] = load_text(agent.prompt)
        except (OSError, ValueError):
            continue
    for skill in inst.skills:
        out[f"skill:{skill.name}"] = skill.body()
    for gname, graph in inst.spec.graphs.items():
        if graph.aliases:
            from ..graph.store import read_aliases

            out[f"alias:{gname}"] = "\n".join(f"{c},{a}" for a, c in
                                              read_aliases(graph.aliases).items())  # fmt: skip
    return out


async def review(inst: Instance, *, now: float | None = None) -> dict[str, Any]:
    """Run the weekly review. Returns what it did; files at most one inbox item."""
    started = time.time()
    found = await signals(inst, now=now)
    records = RunRecords(inst.db, inst.scope)
    if not worth_reviewing(found):
        await records.append("review", "weekly", started=started, stop_reason="nothing_repeated")
        return {"proposals": [], "signals": found}
    current = instructions(inst)
    pinned = [f"{c.agent}: {c.text}" for c in await inst.constraints.all("active")]
    question = (
        "Repeated failures and signals of the last 7 days:\n"
        + json.dumps(found, ensure_ascii=False, indent=1)
        + "\n\nPinned constraints now:\n" + ("\n".join(pinned) or "(none)")
        + "\n\nCurrent instructions:\n"
        + "\n\n".join(f"=== {k}\n{v[:TEXT_CHARS]}" for k, v in current.items())
    )  # fmt: skip
    assert inst.provider is not None
    request = ModelRequest(
        system=REVIEW_SYSTEM, messages=[Message.user(question)], tools=[], model_role="verifier"
    )
    final: ProviderMessage | None = None
    async for event in inst.provider.stream(request):
        if isinstance(event, ProviderMessage):
            final = event
    if final is not None:
        await inst.charge("weekly-review", "verifier", final)
    raw = _json(final.message.text() if final else "")
    proposals = [p for p in (raw.get("proposals") or []) if isinstance(p, dict)][:20]
    shaped = [shape(p, current) for p in proposals]
    shaped = [p for p in shaped if p is not None]
    item = None
    if shaped:
        item = await inst.inbox.create(
            "proposal", f"Weekly review: {len(shaped)} proposed edit(s) to the instructions",
            {"proposals": shaped, "signals": found},
        )  # fmt: skip
        if inst.notify is not None:
            await inst.notify(item, f"The weekly review proposes {len(shaped)} edit(s)")
    await inst.audit.record(inst.scope, "review", "weekly_review", item or "-",
                            {"proposals": len(shaped)})  # fmt: skip
    await records.append("review", "weekly", started=started,
                         stop_reason="proposed" if shaped else "no_proposal",
                         counts={"proposals": len(shaped)}, item=item)  # fmt: skip
    return {"proposals": shaped, "signals": found, "item": item}


def shape(proposal: dict[str, Any], current: dict[str, str]) -> dict[str, Any] | None:
    """A proposal as a person reads it: a diff against the current text, or the new line."""
    target = str(proposal.get("target") or "")
    kind = target.split(":", 1)[0]
    if kind not in ("prompt", "skill", "constraint", "faq", "alias"):
        return None
    out: dict[str, Any] = {
        "target": target,
        "why": str(proposal.get("why") or "")[:500],
        "evidence": [str(e)[:300] for e in proposal.get("evidence") or []][:5],
    }
    if kind in ("prompt", "skill"):
        if target not in current or not str(proposal.get("new_text") or "").strip():
            return None
        new = str(proposal["new_text"])
        diff = "".join(difflib.unified_diff(
            current[target].splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"{target} (now)", tofile=f"{target} (proposed)",
        ))  # fmt: skip
        if not diff:
            return None
        out["diff"] = diff
    else:
        text = str(proposal.get("text") or "").strip()
        if not text:
            return None
        if kind == "alias" and text.count(",") != 1:
            return None
        out["text"] = text[:1000]
        out["diff"] = f"+ {text[:1000]}"
    return out


def _normal(text: str) -> str:
    """A reason without the parts that change every time (numbers, ids, quotes)."""
    text = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", text.lower()).replace("<item>", "…")
    return re.sub(r"\d+(\.\d+)?", "N", " ".join(text.split()))[:200]


def _json(text: str) -> dict[str, Any]:
    found = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        data = json.loads(found.group(0)) if found else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
