"""Context compaction (ARCHITECTURE §3.5): long conversations stay within budget.

Before a turn, when an agent's history is estimated above its ``context_tokens`` (default
``DEFAULT_CONTEXT_TOKENS``) and the spec defines a ``compaction`` model role, the oldest part
of the history is summarised by that role and replaced by the summary:

- the **pinned block** is never touched: the system prompt, approved rules and remembered
  facts are rebuilt every turn, not part of the history;
- durable facts are **saved to memory first** (with a ``memory_extraction`` role and
  semantic memory), so nothing the contact stated is lost to the summary;
- the recent tail is kept verbatim, starting at a user message, so a tool call is never
  separated from its result;
- the summary keeps PII tokens as they are and is recorded as a ``ContextCompacted`` event,
  so a resumed session sees exactly the same history.

When the provider refuses a turn as too long for the model's context (``overflow``), the
same routine runs at once with half the history's size as the budget, and the turn is
retried once on the compacted history.

Without a ``compaction`` role nothing is cut: the model's own limit applies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.events import ContextCompacted
from ..core.messages import Message, Role, TextBlock
from ..core.session import SUMMARY_HEADER, Session
from ..providers.base import ModelRequest, ProviderMessage

if TYPE_CHECKING:
    from .instance import AgentRuntime

DEFAULT_CONTEXT_TOKENS = 60_000
KEEP_FRACTION = 0.4  # of the budget, kept verbatim at the end of the history
COMPACT_PROMPT = """You condense the earlier part of a conversation between an assistant and a
contact, so the assistant can continue it. Write a compact summary in the language of the
conversation: what the contact wants, facts and constraints they gave, decisions made, what
the assistant did (tools called and their outcomes), and what is still open. Keep names,
dates, amounts, ids and PII tokens like <PHONE_1a2b3c4d> exactly as written. No preamble."""


def estimate_tokens(messages: list[Message]) -> int:
    return sum(len(m.model_dump_json()) for m in messages) // 4


def _is_user_text(message: Message) -> bool:
    return message.role is Role.USER and any(isinstance(b, TextBlock) for b in message.content)


def split_point(messages: list[Message], keep_tokens: int) -> int:
    """Index where the verbatim tail starts: the latest user text message such that the tail
    is at least ``keep_tokens`` (or the earliest possible), never splitting a tool pair."""
    candidates = [i for i, m in enumerate(messages) if i > 0 and _is_user_text(m)]
    best = 0
    for i in candidates:
        if estimate_tokens(messages[i:]) >= keep_tokens:
            best = i
    if best == 0 and candidates:
        best = candidates[-1]
    return best


def _transcript(messages: list[Message]) -> str:
    lines = []
    for m in messages:
        text = m.text()
        if text.startswith(SUMMARY_HEADER):
            lines.append(f"(earlier summary) {text.removeprefix(SUMMARY_HEADER).strip()}")
            continue
        for use in m.tool_uses():
            lines.append(f"assistant called {use.name}")
        if text:
            lines.append(f"{m.role}: {text}")
    return "\n".join(lines)


async def compact_if_needed(
    agent: AgentRuntime, session: Session, *, force: bool = False
) -> ContextCompacted | None:
    """Compact when the history is over budget; ``force``: the model already refused it as
    too long, so compact now to half its size whatever the estimate says."""
    inst = agent.instance
    roles = inst.spec.models.roles if inst.spec.models else {}
    if "compaction" not in roles or inst.provider is None:
        return None
    budget = agent.spec.context_tokens or DEFAULT_CONTEXT_TOKENS
    before = estimate_tokens(session.messages)
    if force:
        budget = min(budget, before // 2)
    elif before <= budget:
        return None
    cut = split_point(session.messages, int(budget * KEEP_FRACTION))
    if cut <= 0:
        return None
    from ..memory.agent import extract_facts  # durable facts go to memory before the cut

    await extract_facts(inst, agent.name, session)
    request = ModelRequest(
        system=COMPACT_PROMPT,
        messages=[Message.user(_transcript(session.messages[:cut]))],
        tools=[],
        model_role="compaction",
    )
    final: ProviderMessage | None = None
    async for event in inst.provider.stream(request):
        if isinstance(event, ProviderMessage):
            final = event
    if final is None or not final.message.text().strip():
        return None  # keep the full history rather than lose it
    await inst.charge(agent.name, "compaction", final)
    summary = final.message.text().strip()
    after = estimate_tokens([Message.user(summary), *session.messages[cut:]])
    return session.compact(summary, cut, before, after)
