"""The session a tool call belongs to, for built-in tools that act on the conversation
(``handoff.human``). Set for the duration of ``AgentRuntime.send``; tool tasks inherit it."""

from __future__ import annotations

from contextvars import ContextVar

from ..core.session import Session

current_session: ContextVar[Session | None] = ContextVar("current_session", default=None)
