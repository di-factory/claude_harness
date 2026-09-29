"""The durable job queue (ARCHITECTURE §3.16, §3.20): Postgres-native, SQLite locally.

Every unit of background work (a channel message to answer, a trigger firing, a reminder
due in 24 h, an approved action to execute) is a job row, so nothing is lost when the
process dies:

- ``enqueue`` with ``delay_s``/``run_at`` for delayed work and ``dedupe_key`` for work that
  must happen once (a reminder per appointment, a webhook delivery id). A duplicate is
  ignored and ``enqueue`` returns None.
- ``claim`` takes the next ready job under a lease (``FOR UPDATE SKIP LOCKED`` on Postgres,
  so several workers never take the same job). A job whose lease expired, because its
  worker crashed, is claimed again: that is the durability guarantee.
- ``fail`` retries with exponential backoff until ``max_attempts``, then parks the job as
  ``dead`` for a person to inspect. Handlers must therefore be idempotent or use dedupe.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..core.scope import Scope
from ..store.db import Database, Row

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    id: str
    scope: Scope
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    run_at: float
    dedupe_key: str | None

    @classmethod
    def from_row(cls, row: Row) -> Job:
        return cls(
            id=row["id"],
            scope=Scope(tenant_id=row["tenant_id"], instance_id=row["instance_id"]),
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            run_at=float(row["run_at"]),
            dedupe_key=row["dedupe_key"],
        )


class JobQueue:
    def __init__(
        self,
        db: Database,
        *,
        lease_s: float = 300.0,
        backoff_s: float = 5.0,
        max_backoff_s: float = 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.lease_s = lease_s
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self.clock = clock

    async def enqueue(
        self,
        scope: Scope,
        kind: str,
        payload: dict[str, Any],
        *,
        delay_s: float = 0.0,
        run_at: float | None = None,
        dedupe_key: str | None = None,
        max_attempts: int = 5,
    ) -> str | None:
        now = self.clock()
        job_id = uuid.uuid4().hex
        inserted = await self.db.execute(
            "INSERT INTO jobs (id, tenant_id, instance_id, kind, payload, status, attempts,"
            " max_attempts, run_at, dedupe_key, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (job_id, scope.tenant_id, scope.instance_id, kind, json.dumps(payload),
             max_attempts, run_at if run_at is not None else now + delay_s, dedupe_key,
             now, now),
        )  # fmt: skip
        return job_id if inserted else None

    async def claim(self) -> Job | None:
        now = self.clock()
        lock = " FOR UPDATE SKIP LOCKED" if self.db.dialect == "postgres" else ""
        async with self.db.transaction() as conn:
            row = await conn.fetchone(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1,"
                " locked_until = ?, updated_at = ? WHERE id = (SELECT id FROM jobs WHERE"
                " (status = 'queued' AND run_at <= ?) OR (status = 'running' AND locked_until < ?)"
                f" ORDER BY run_at LIMIT 1{lock}) RETURNING *",
                (now + self.lease_s, now, now, now),
            )
        return Job.from_row(row) if row else None

    async def complete(self, job: Job) -> None:
        await self.db.execute(
            "UPDATE jobs SET status = 'done', locked_until = NULL, updated_at = ? WHERE id = ?",
            (self.clock(), job.id),
        )

    async def fail(self, job: Job, error: str) -> str:
        """Retry later, or park as dead after ``max_attempts``. Returns the new status."""
        now = self.clock()
        if job.attempts >= job.max_attempts:
            status, run_at = "dead", now
        else:
            status = "queued"
            run_at = now + min(self.backoff_s * 2 ** (job.attempts - 1), self.max_backoff_s)
        await self.db.execute(
            "UPDATE jobs SET status = ?, run_at = ?, locked_until = NULL, last_error = ?,"
            " updated_at = ? WHERE id = ?",
            (status, run_at, error[:2000], now, job.id),
        )
        return status

    async def cancel(self, scope: Scope, dedupe_key: str) -> bool:
        """Cancel pending work, e.g. the reminder of an appointment that was cancelled."""
        changed = await self.db.execute(
            "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE tenant_id = ?"
            " AND instance_id = ? AND dedupe_key = ? AND status = 'queued'",
            (self.clock(), scope.tenant_id, scope.instance_id, dedupe_key),
        )
        return changed > 0

    async def counts(self, scope: Scope) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT status, COUNT(*) AS n FROM jobs WHERE tenant_id = ? AND instance_id = ?"
            " GROUP BY status",
            (scope.tenant_id, scope.instance_id),
        )
        return {r["status"]: int(r["n"]) for r in rows}

    async def get(self, job_id: str) -> Row | None:
        return await self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))


Handler = Callable[[Job], Awaitable[None]]


class Worker:
    """Runs jobs from the queue with the handler registered for their kind."""

    def __init__(
        self,
        queue: JobQueue,
        handlers: dict[str, Handler],
        *,
        concurrency: int = 4,
        poll_s: float = 1.0,
    ) -> None:
        self.queue = queue
        self.handlers = handlers
        self.concurrency = concurrency
        self.poll_s = poll_s

    async def run_one(self) -> bool:
        """Claim and run one job. Returns False when nothing was ready."""
        job = await self.queue.claim()
        if job is None:
            return False
        handler = self.handlers.get(job.kind)
        if handler is None:
            await self.queue.fail(job, f"no handler for job kind {job.kind!r}")
            return True
        try:
            await handler(job)
        except Exception as exc:  # a failing job is retried, never crashes the worker
            status = await self.queue.fail(job, f"{type(exc).__name__}: {exc}")
            log.warning("job %s (%s) failed, now %s: %s", job.id, job.kind, status, exc)
        else:
            await self.queue.complete(job)
        return True

    async def drain(self, max_jobs: int = 1000) -> int:
        """Run ready jobs until none is left (tests, batch runs)."""
        done = 0
        while done < max_jobs and await self.run_one():
            done += 1
        return done

    async def run(self, stop: asyncio.Event) -> None:
        async def lane() -> None:
            while not stop.is_set():
                try:
                    busy = await self.run_one()
                except Exception:  # database hiccup: back off, keep the worker alive
                    log.exception("worker lane error")
                    busy = False
                if not busy:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=self.poll_s)

        await asyncio.gather(*(lane() for _ in range(self.concurrency)))
