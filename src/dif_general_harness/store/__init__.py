"""Storage: session stores (JSONL locally, SQL in production) and the database layer."""

from typing import Protocol

from ..core.events import Event
from ..core.scope import Scope
from ..core.session import Session
from .db import Database, connect
from .jsonl import JsonlSessionStore
from .sql import SqlSessionStore


class SessionStore(Protocol):
    async def append(self, event: Event) -> None: ...
    async def read(self, scope: Scope, session_id: str) -> list[Event]: ...
    async def load(self, scope: Scope, session_id: str) -> Session: ...
    async def list_sessions(self, scope: Scope) -> list[str]: ...


__all__ = ["Database", "JsonlSessionStore", "SessionStore", "SqlSessionStore", "connect"]
