"""Append-only JSONL event log, one file per session, namespaced by tenant and instance.

Layout: ``<root>/<tenant_id>/<instance_id>/sessions/<session_id>.jsonl``.
Streaming ``text_delta`` events are not persisted: they are for live surfaces,
and the final ``message_added`` event already holds the full text.
"""

from __future__ import annotations

from pathlib import Path

from ..core.events import EVENT_ADAPTER, Event, TextDelta
from ..core.scope import Scope
from ..core.session import Session


class JsonlSessionStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, scope: Scope, session_id: str) -> Path:
        if not session_id.isalnum():
            raise ValueError(f"invalid session id {session_id!r}")
        return self.root / scope.tenant_id / scope.instance_id / "sessions" / f"{session_id}.jsonl"

    def append(self, event: Event) -> None:
        if isinstance(event, TextDelta):
            return
        path = self._path(event.scope, event.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")
            fh.flush()

    def read(self, scope: Scope, session_id: str) -> list[Event]:
        path = self._path(scope, session_id)
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        events: list[Event] = []
        for i, line in enumerate(lines):
            try:
                events.append(EVENT_ADAPTER.validate_json(line))
            except ValueError:
                if i == len(lines) - 1:
                    # A crash mid-write can leave a partial last line; everything before is intact.
                    break
                raise ValueError(f"{path}: corrupted event on line {i + 1}") from None
        return events

    def load(self, scope: Scope, session_id: str) -> Session:
        return Session.from_events(self.read(scope, session_id))

    def list_sessions(self, scope: Scope) -> list[str]:
        folder = self.root / scope.tenant_id / scope.instance_id / "sessions"
        return sorted(p.stem for p in folder.glob("*.jsonl")) if folder.exists() else []
