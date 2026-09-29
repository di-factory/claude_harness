"""Append-only JSONL store: persistence, resume, crash tolerance and tenant isolation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from dif_general_harness.core.events import TextDelta
from dif_general_harness.core.loop import run
from dif_general_harness.core.messages import Message, Role
from dif_general_harness.core.scope import Scope
from dif_general_harness.core.session import Session
from dif_general_harness.providers import FakeProvider
from dif_general_harness.store import JsonlSessionStore
from dif_general_harness.tools import ToolRegistry


async def _turn(session: Session, store: JsonlSessionStore, text: str, reply: str) -> None:
    async for ev in run(session, text, FakeProvider([Message.assistant(reply)]), ToolRegistry()):
        await store.append(ev)


async def test_resume_after_restart(tmp_path: Path, scope: Scope) -> None:
    store = JsonlSessionStore(tmp_path)
    session = Session(scope=scope, agent_id="receptionist", contact_key="+52555")
    await store.append(session.started_event())
    await _turn(session, store, "hola", "¡Hola! ¿En qué te ayudo?")

    # simulate a restart: rebuild purely from the log, then keep going
    resumed = await store.load(scope, session.id)
    assert resumed.messages == session.messages
    assert resumed.contact_key == "+52555"
    # the last event (turn_ended) is persisted, so numbering continues exactly where it stopped
    assert resumed.next_seq == session.next_seq
    await _turn(resumed, store, "1", "Confirmada.")

    final = await store.load(scope, session.id)
    assert [m.role for m in final.messages] == [Role.USER, Role.ASSISTANT] * 2
    seqs = [e.seq for e in await store.read(scope, session.id)]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))


async def test_text_deltas_are_not_persisted(tmp_path: Path, scope: Scope) -> None:
    store = JsonlSessionStore(tmp_path)
    session = Session(scope=scope, agent_id="a")
    await store.append(session.started_event())
    await store.append(TextDelta(scope=scope, session_id=session.id, text="partial"))
    assert len(await store.read(scope, session.id)) == 1


async def test_partial_last_line_is_tolerated(tmp_path: Path, scope: Scope) -> None:
    store = JsonlSessionStore(tmp_path)
    session = Session(scope=scope, agent_id="a")
    await store.append(session.started_event())
    await store.append(session.add_message(Message.user("hola")))
    path = tmp_path / scope.tenant_id / scope.instance_id / "sessions" / f"{session.id}.jsonl"
    with path.open("a") as fh:
        fh.write('{"type": "message_added", "scope": {"tena')  # crash mid-write
    assert len((await store.load(scope, session.id)).messages) == 1


async def test_corruption_in_the_middle_is_an_error(tmp_path: Path, scope: Scope) -> None:
    store = JsonlSessionStore(tmp_path)
    session = Session(scope=scope, agent_id="a")
    await store.append(session.started_event())
    path = tmp_path / scope.tenant_id / scope.instance_id / "sessions" / f"{session.id}.jsonl"
    with path.open("a") as fh:
        fh.write("garbage\n")
    await store.append(session.add_message(Message.user("hola")))
    with pytest.raises(ValueError, match="corrupted event on line 2"):
        await store.read(scope, session.id)


async def test_tenants_are_isolated(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    a = Session(scope=Scope(tenant_id="tenant-a", instance_id="x"), agent_id="a")
    b = Session(scope=Scope(tenant_id="tenant-b", instance_id="x"), agent_id="a")
    await store.append(a.started_event())
    await store.append(b.started_event())
    assert await store.list_sessions(a.scope) == [a.id]
    assert await store.list_sessions(b.scope) == [b.id]
    with pytest.raises(FileNotFoundError):
        await store.read(b.scope, a.id)


def test_replay_rejects_mixed_tenants(scope: Scope) -> None:
    a = Session(scope=scope, agent_id="a")
    other = Scope(tenant_id="other", instance_id="appointments")
    foreign = Session(id=a.id, scope=other, agent_id="a").add_message(Message.user("x"))
    with pytest.raises(ValueError, match="different sessions or tenants"):
        Session.from_events([a.started_event(), foreign])


@pytest.mark.parametrize("bad", ["", "UPPER", "../escape", "a/b", "-lead"])
def test_scope_ids_are_safe_path_segments(bad: str) -> None:
    with pytest.raises(ValidationError):
        Scope(tenant_id=bad, instance_id="x")


async def test_session_ids_are_safe_path_segments(tmp_path: Path, scope: Scope) -> None:
    with pytest.raises(ValueError):
        await JsonlSessionStore(tmp_path).read(scope, "../../etc/passwd")
