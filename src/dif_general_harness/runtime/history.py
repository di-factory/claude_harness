"""``history.search``: what a long conversation's compaction left out, found again.

Compaction replaces the oldest part of a conversation with a summary, and a summary loses
detail ("which date did they say?"). Nothing is deleted: every message stays in the
conversation's own event log. With a ``compaction`` role, agents get ``history.search``: the
messages of this conversation that match the words asked for, oldest first, with their place
in the conversation. Only this conversation, never another contact's.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..core.events import MessageAdded
from ..core.messages import ToolResultBlock
from ..core.untrusted import fence
from ..tools.registry import Effect, Tool, tool
from .context import current_session

if TYPE_CHECKING:
    from .instance import Instance

MAX_HITS = 6
SNIPPET = 600


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) > 2}


def history_tool(instance: Instance) -> Tool:
    @tool("history.search", effect=Effect.READ)
    async def search(query: str) -> str:
        """Find something said earlier in this conversation that is no longer in view
        (older messages are summarised): dates, amounts, names, what was agreed. Give the
        words to look for."""
        session = current_session.get()
        store = instance.store
        if session is None or not hasattr(store, "read"):
            raise ValueError("there is no conversation to search")
        wanted = _words(query)
        if not wanted:
            raise ValueError("give the words to look for")
        hits: list[tuple[int, int, str]] = []
        position = 0
        for event in await store.read(instance.scope, session.id):
            if not isinstance(event, MessageAdded):
                continue
            position += 1
            message = event.message
            text = message.text() or " ".join(
                str(b.content) for b in message.content if isinstance(b, ToolResultBlock)
            )
            score = len(wanted & _words(text))
            if score:
                hits.append((score, position, f"#{position} {message.role}: {text[:SNIPPET]}"))
        if not hits:
            return f"nothing earlier in this conversation mentions {query!r}"
        best = sorted(sorted(hits, key=lambda h: -h[0])[:MAX_HITS], key=lambda h: h[1])
        found: list[Any] = [line for _, _, line in best]
        return fence("\n".join(found), "this conversation")

    return search
