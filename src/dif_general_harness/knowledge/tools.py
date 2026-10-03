"""Knowledge in an agent's run: the ``knowledge.search_<corpus>`` tools, the ``citations``
check on answers, and how citations reach the contact.

- **Search:** returns the chunks scoring at least ``min_score`` (a small corpus with
  ``read_whole_below`` comes back whole, and the agent is told what to do when none of it
  answers), each with a source marker
  (``kb:<id>``) when the corpus has ``cite: true``. Nothing found tells the agent what the
  corpus's ``not_found`` asks for (``say_so``: say the documents do not cover it; ``handoff``:
  pass it to a person) and evaluates the escalation rules with ``knowledge.not_found``.
- **Citations check:** after an answer that used retrieved chunks, a ``citations`` check
  (``min_citations``, ``claims_must_cite``) must pass. Citing a source that was never
  retrieved fails. A failed answer gets one rewrite; if that fails too, the contact gets its
  cited paragraphs only (the unsourced ones dropped), or "not found" when none is left.
- **Rendering:** markers become ``[1]``, ``[2]`` and a "Sources" list (document and section)
  in the reply the contact sees.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..core.messages import Message, ToolResultBlock, ToolUseBlock
from ..core.session import Session
from ..tools.registry import Effect, Tool, schema_check
from .store import KnowledgeBase

if TYPE_CHECKING:
    from ..runtime.instance import Instance
    from ..spec.schema import Check

MARKER = re.compile(r"\[kb:([a-z]{10})\]")
MIN_CLAIM_WORDS = 8
NOT_FOUND = {
    "say_so": "Say plainly that the documents do not cover this. Do not guess or answer from"
    " general knowledge.",
    "handoff": "Tell the contact you will pass the question to a person, and hand off"
    " (handoff.human) if you can.",
}
UNGROUNDED = {
    "en": "I could not find a sourced answer to that in our documents.",
    "es": "No encontré una respuesta con fuente en nuestros documentos.",
}


def search_tool(instance: Instance, corpus: str) -> Tool:
    from ..runtime.context import current_session  # runtime imports this module

    kb: KnowledgeBase = instance.knowledge
    settings = kb.retrieval(corpus)
    cite = bool(settings.get("cite", True))
    not_found = str(settings.get("not_found") or "say_so")
    description = str(instance.spec.knowledge.corpora[corpus].get("description") or "")

    async def search(query: str) -> dict[str, Any]:
        hits = await kb.search(corpus, query)
        if not hits:
            session = current_session.get()
            if session is not None:
                await instance.knowledge_not_found(session, corpus, query)
            return {
                "found": False,
                "message": f"Nothing in the {corpus} documents answers this. "
                + NOT_FOUND.get(not_found, NOT_FOUND["say_so"]),
            }
        results = []
        for h in hits:
            item = {"document": h.title, "section": h.section, "text": h.text, "score": h.score}
            results.append({"source": f"kb:{h.id}", **item} if cite else item)
        out: dict[str, Any] = {"found": True, "results": results}
        if settings.get("read_whole_below"):
            out["note"] = (
                f"These may be all the {corpus} documents. Answer only from what they say;"
                " they may be in another language than the contact's, so answer in the"
                " contact's language. If none of them answers the question: "
                + NOT_FOUND.get(not_found, NOT_FOUND["say_so"])
            )
        if cite:
            out["how_to_cite"] = (
                "Answer only from these results. Put the source marker in brackets, like"
                f" [kb:{hits[0].id}], right after each statement it supports."
            )
        return out

    schema = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "what to look up"}},
        "required": ["query"],
    }
    about = f" ({description})" if description else ""
    return Tool(
        name=f"knowledge.search_{corpus}",
        description=f"Search the {corpus} documents{about}. Returns the best matching"
        " passages, or found=false when the documents do not cover it.",
        input_schema=schema,
        handler=search,
        effect=Effect.READ,
        check_input=schema_check(schema),
        source="knowledge",
    )


# --- the citations check ---------------------------------------------------------------


def _retrieved(messages: list[Message]) -> set[str]:
    """Source ids returned by knowledge searches in these messages."""
    searches = {
        b.id
        for m in messages
        for b in m.content
        if isinstance(b, ToolUseBlock) and b.name.startswith("knowledge.search_")
    }
    found: set[str] = set()
    for m in messages:
        for b in m.content:
            if isinstance(b, ToolResultBlock) and b.tool_use_id in searches:
                found.update(re.findall(r"kb:([a-z]{10})", str(b.content)))
    return found


def check_citations(
    check: Check, answer: str, session: Session, turn_start: int
) -> tuple[bool, str]:
    """(passed, reason) for an answer; answers that used no retrieved passage pass."""
    this_turn = _retrieved(session.messages[turn_start:])
    if not this_turn:
        return True, "no passages were retrieved in this turn"
    retrieved = _retrieved(session.messages)
    cited = MARKER.findall(answer)
    invented = sorted(set(cited) - retrieved)
    if invented:
        return False, f"cites sources that were not retrieved: {', '.join(invented)}"
    extra = check.model_extra or {}
    needed = int(extra.get("min_citations", 1))
    if len(set(cited)) < needed:
        return False, f"cites {len(set(cited))} source(s); at least {needed} needed"
    if extra.get("claims_must_cite"):
        for part in _parts(answer):
            if _unsourced(part):
                words = MARKER.sub("", part).split()
                return False, f"a statement has no source: {' '.join(words[:12])!r}"
    return True, f"cites {len(set(cited))} retrieved source(s)"


def _parts(answer: str) -> list[str]:
    return re.split(r"\n\s*\n|\n(?=[-*•\d])", answer)


def _unsourced(part: str) -> bool:
    """A statement long enough to be a claim, with no source marker (questions are fine)."""
    words = MARKER.sub("", part).split()
    return (len(words) >= MIN_CLAIM_WORDS and not MARKER.search(part)
            and not part.strip().endswith("?"))  # fmt: skip


def keep_cited(check: Check, answer: str, session: Session, turn_start: int) -> str | None:
    """The answer without its unsourced paragraphs, if what is left passes the check; None
    when nothing sourced is left (the contact then gets "not found")."""
    kept = [p.strip() for p in _parts(answer) if p.strip() and not _unsourced(p)]
    if not any(MARKER.search(p) for p in kept):
        return None
    pruned = "\n\n".join(kept)
    passed, _ = check_citations(check, pruned, session, turn_start)
    return pruned if passed else None


def citation_checks(instance: Instance, agent_name: str) -> list[Check]:
    """The ``citations`` checks that apply to an agent's answers: it searches a corpus with
    ``cite: true``."""
    spec = instance.spec
    agent = spec.agents[agent_name]
    cites = any(
        bool((spec.knowledge.corpora.get(c, {}).get("retrieval") or {}).get("cite", True))
        for c in agent.knowledge
    )
    if not cites:
        return []
    return [c for c in spec.policies.verification.checks.values() if c.type == "citations"]


def ungrounded_text(instance: Instance) -> str:
    locale = instance.spec.solution.locale.split("-")[0]
    return UNGROUNDED.get(locale, UNGROUNDED["en"])


def repair_note(reason: str) -> Message:
    return Message.user(
        f"[Automatic check: your last answer {reason}. Rewrite it using only the passages you"
        " retrieved, with a [kb:<id>] marker after each statement, and leave out anything they"
        " do not say (no offers or comments without a marker). If they do not support an"
        " answer, say the documents do not cover it.]"
    )


# --- rendering -------------------------------------------------------------------------


async def render_citations(kb: KnowledgeBase, text: str) -> str:
    """``[kb:<id>]`` markers to ``[1]``.. plus a Sources list; unknown markers are dropped."""
    ids = list(dict.fromkeys(MARKER.findall(text)))
    if not ids:
        return text
    known = await kb.chunks(ids)
    numbers: dict[str, int] = {}
    sources: list[str] = []
    for chunk_id in ids:
        hit = known.get(chunk_id)
        if hit is None:
            continue
        section = hit.section.removeprefix(f"{hit.title} > ")
        label = f"{hit.title} — {section}" if section and section != hit.title else hit.title
        if label in sources:  # two passages of the same section: one source
            numbers[chunk_id] = sources.index(label) + 1
        else:
            sources.append(label)
            numbers[chunk_id] = len(sources)

    def number(match: re.Match[str]) -> str:
        n = numbers.get(match.group(1))
        return f"[{n}]" if n else ""

    body = MARKER.sub(number, text)
    body = re.sub(r"(\[\d+\])(?:\1)+", r"\1", body)  # [1][1] -> [1]
    body = re.sub(r"[ \t]+([.,;:])", r"\1", body)
    if not sources:
        return body
    listing = "\n".join(f"[{n}] {label}" for n, label in enumerate(sources, start=1))
    return f"{body}\n\nSources:\n{listing}"
