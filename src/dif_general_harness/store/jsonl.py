"""Append-only JSONL event log, one file per session, namespaced by tenant and instance.

Layout: ``<root>/<tenant_id>/<instance_id>/sessions/<session_id>.jsonl``.
Streaming ``text_delta`` events are not persisted: they are for live surfaces,
and the final ``message_added`` event already holds the full text.
With a ``Redactor``, every event is redacted before it touches the disk, except the
fields that must survive verbatim for a resumed session to stay valid: ids that pair
tool calls with results, and provider thinking signatures (opaque, echoed back as is).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.events import EVENT_ADAPTER, Event, TextDelta
from ..core.scope import Scope
from ..core.session import Session
from ..policy.redact import Redactor

_EVENT_KEEP = {"scope", "session_id", "seq", "ts", "type", "tool_use_id", "name", "agent_id"}
_BLOCK_KEEP = {
    "tool_use": {"type", "id", "name"},
    "tool_result": {"type", "tool_use_id", "status"},
    "thinking": {"type", "provider", "signature", "redacted_data"},
}


def _scrub(event: dict[str, Any], redactor: Redactor) -> dict[str, Any]:
    out = {k: v if k in _EVENT_KEEP else redactor.redact_obj(v) for k, v in event.items()}
    message = event.get("message")
    if isinstance(message, dict):
        blocks = []
        for block in message.get("content", []):
            keep = _BLOCK_KEEP.get(block.get("type"), {"type"})
            blocks.append({k: v if k in keep else redactor.redact_obj(v) for k, v in block.items()})
        out["message"] = {"role": message["role"], "content": blocks}
    return out


class JsonlSessionStore:
    def __init__(self, root: Path | str, redactor: Redactor | None = None) -> None:
        self.root = Path(root)
        self.redactor = redactor

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
            if self.redactor is None:
                line = event.model_dump_json()
            else:
                line = json.dumps(_scrub(event.model_dump(mode="json"), self.redactor))
            fh.write(line + "\n")
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
