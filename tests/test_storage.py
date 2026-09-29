"""The database layer, the SQL session store and the durable queue, on SQLite and Postgres."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from dif_general_harness.core.messages import Message
from dif_general_harness.core.scope import Scope
from dif_general_harness.core.session import Session
from dif_general_harness.policy import Redactor
from dif_general_harness.store import SqlSessionStore
from dif_general_harness.store.schema import MIGRATIONS, migrate
from dif_general_harness.workflows import Job, JobQueue, Worker

A = Scope(tenant_id="clinica-sonrisa", instance_id="citas")
B = Scope(tenant_id="otra-clinica", instance_id="citas")


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


async def test_migrations_are_idempotent(db: Any) -> None:
    assert await migrate(db) == len(MIGRATIONS)
    assert await migrate(db) == len(MIGRATIONS)
    rows = await db.fetchall("SELECT version FROM schema_version")
    assert [r["version"] for r in rows] == list(range(1, len(MIGRATIONS) + 1))


async def test_transactions_roll_back(db: Any) -> None:
    with pytest.raises(RuntimeError):
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO spend (tenant_id, instance_id, key, day, usd) VALUES (?, ?, ?, ?, ?)",
                ("t", "i", "k", "2026-09-29", 1.0),
            )
            raise RuntimeError("boom")
    assert await db.fetchall("SELECT * FROM spend") == []


# --- sessions ----------------------------------------------------------------------


async def test_sql_session_store(db: Any) -> None:
    store = SqlSessionStore(db, redactor=Redactor(["planted-secret-value"]))
    session = Session(scope=A, agent_id="receptionist", contact_key="+5215512345678")
    await store.append(session.started_event())
    await store.append(session.add_message(Message.user("hola, mi clave es planted-secret-value")))
    await store.append(session.add_message(Message.assistant("¡Hola!")))

    resumed = await store.load(A, session.id)
    assert [m.text() for m in resumed.messages] == ["hola, mi clave es [REDACTED]", "¡Hola!"]
    assert resumed.next_seq == session.next_seq
    assert await store.list_sessions(A) == [session.id]
    assert await store.list_sessions(B) == []
    with pytest.raises(FileNotFoundError):
        await store.read(B, session.id)  # another tenant never sees it

    await store.bind(A, session.id, "whatsapp", "+5215512345678")
    assert await store.current(A, "whatsapp", "+5215512345678", 3600) == (session.id, "active")
    assert await store.current(A, "whatsapp", "+5215512345678", -1) is None  # outside window
    assert await store.current(B, "whatsapp", "+5215512345678", None) is None
    await store.set_state(A, session.id, "escalated")
    assert await store.current(A, "whatsapp", "+5215512345678", None) == (session.id, "escalated")


async def test_duplicate_event_seq_is_rejected(db: Any) -> None:
    store = SqlSessionStore(db)
    session = Session(scope=A, agent_id="a")
    started = session.started_event()
    await store.append(started)
    with pytest.raises(Exception):  # noqa: B017 - driver-specific integrity error
        await store.append(started)


# --- queue -------------------------------------------------------------------------


async def test_enqueue_claim_complete(db: Any) -> None:
    clock = Clock()
    queue = JobQueue(db, clock=clock)
    first = await queue.enqueue(A, "message", {"text": "hola"})
    later = await queue.enqueue(A, "reminder", {"event": "e1"}, delay_s=3600)
    assert first and later

    job = await queue.claim()
    assert job and job.id == first and job.payload == {"text": "hola"} and job.attempts == 1
    assert await queue.claim() is None  # the reminder is not due yet
    await queue.complete(job)

    clock.now += 3600
    reminder = await queue.claim()
    assert reminder and reminder.kind == "reminder"
    assert await queue.counts(A) == {"done": 1, "running": 1}


async def test_dedupe_and_cancel(db: Any) -> None:
    clock = Clock()
    queue = JobQueue(db, clock=clock)
    assert await queue.enqueue(A, "reminder", {}, delay_s=60, dedupe_key="reminder:e1")
    assert await queue.enqueue(A, "reminder", {}, delay_s=60, dedupe_key="reminder:e1") is None
    # the same key in another tenant is a different job
    assert await queue.enqueue(B, "reminder", {}, delay_s=60, dedupe_key="reminder:e1")
    assert await queue.cancel(A, "reminder:e1")
    clock.now += 60
    job = await queue.claim()
    assert job and job.scope == B
    assert await queue.claim() is None


async def test_retries_with_backoff_then_dead(db: Any) -> None:
    clock = Clock()
    queue = JobQueue(db, clock=clock, backoff_s=10)
    await queue.enqueue(A, "send", {}, max_attempts=3)
    delays = []
    for _ in range(3):
        job = await queue.claim()
        assert job is not None
        status = await queue.fail(job, "gateway down")
        row = await queue.get(job.id)
        assert row is not None
        delays.append(row["run_at"] - clock.now)
        clock.now = row["run_at"]
    assert status == "dead" and delays[:2] == [10, 20]
    assert row["last_error"] == "gateway down"
    assert await queue.claim() is None


async def test_expired_lease_is_recovered(db: Any) -> None:
    clock = Clock()
    queue = JobQueue(db, clock=clock, lease_s=30)
    await queue.enqueue(A, "turn", {"text": "hola"})
    crashed = await queue.claim()  # the worker dies without completing
    assert crashed is not None
    assert await queue.claim() is None
    clock.now += 31
    again = await queue.claim()
    assert again and again.id == crashed.id and again.attempts == 2


async def test_concurrent_workers_never_share_a_job(db: Any) -> None:
    queue = JobQueue(db)
    for i in range(30):
        await queue.enqueue(A, "n", {"i": i})
    seen: list[int] = []

    async def handle(job: Job) -> None:
        seen.append(job.payload["i"])
        await asyncio.sleep(0)

    worker = Worker(queue, {"n": handle}, concurrency=5, poll_s=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    for _ in range(500):
        if len(seen) >= 30:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await task
    assert sorted(seen) == list(range(30))
    assert await queue.counts(A) == {"done": 30}


async def test_worker_retries_failures_and_unknown_kinds(db: Any) -> None:
    clock = Clock()
    queue = JobQueue(db, clock=clock, backoff_s=1)
    calls = 0

    async def flaky(job: Job) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("gateway timeout")

    worker = Worker(queue, {"send": flaky})
    await queue.enqueue(A, "send", {})
    await queue.enqueue(A, "mystery", {}, max_attempts=1)
    assert await worker.drain() == 2
    clock.now += 1
    assert await worker.drain() == 1
    assert calls == 2
    assert await queue.counts(A) == {"done": 1, "dead": 1}
