"""Sessions: a scoped conversation that can be rebuilt from its event log."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from pydantic import BaseModel, Field

from .events import Event, MessageAdded, SessionStarted
from .messages import Message
from .scope import Scope


class Session(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    scope: Scope
    agent_id: str
    contact_key: str | None = None
    config_version: str | None = None
    messages: list[Message] = Field(default_factory=list)
    next_seq: int = 0

    def started_event(self) -> SessionStarted:
        """The first event of every log; call once when the session is created."""
        return self.stamp(
            SessionStarted(
                scope=self.scope,
                session_id=self.id,
                agent_id=self.agent_id,
                contact_key=self.contact_key,
                config_version=self.config_version,
            )
        )

    def stamp[E: Event](self, event: E) -> E:
        """Assign the next sequence number; the loop stamps every event it emits."""
        event.seq = self.next_seq
        self.next_seq += 1
        return event

    def add_message(self, message: Message) -> MessageAdded:
        self.messages.append(message)
        return self.stamp(MessageAdded(scope=self.scope, session_id=self.id, message=message))

    @classmethod
    def from_events(cls, events: Iterable[Event]) -> Session:
        """Rebuild a session by replaying its log (resume after a crash or restart)."""
        session: Session | None = None
        last_seq = -1
        for event in events:
            if isinstance(event, SessionStarted):
                if session is not None:
                    raise ValueError("log contains more than one session_started event")
                session = cls(
                    id=event.session_id,
                    scope=event.scope,
                    agent_id=event.agent_id,
                    contact_key=event.contact_key,
                    config_version=event.config_version,
                )
            elif session is None:
                raise ValueError("log does not start with session_started")
            elif event.session_id != session.id or event.scope != session.scope:
                raise ValueError("log mixes events from different sessions or tenants")
            elif isinstance(event, MessageAdded):
                session.messages.append(event.message)
            last_seq = max(last_seq, event.seq)
        if session is None:
            raise ValueError("empty log")
        session.next_seq = last_seq + 1
        return session
