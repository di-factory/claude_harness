"""One small async database layer, two drivers: SQLite (local, console, tests) and Postgres
(production, ARCHITECTURE §3.20).

Queries are written once with ``?`` placeholders; the Postgres driver rewrites them to
``%s``. Values are plain (text, integers, floats); JSON is stored as text and timestamps as
epoch seconds, so both drivers behave the same. Every table is keyed by tenant and
instance: isolation is in the schema, not in each query's goodwill.

Use ``Database.connect(url)``: ``sqlite:///path/to/file.db``, ``sqlite://:memory:`` or
``postgresql://...``. ``transaction()`` yields a connection whose statements commit
together.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Protocol

Row = dict[str, Any]
Dialect = Literal["sqlite", "postgres"]


class Conn(Protocol):
    dialect: Dialect

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int: ...
    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[Row]: ...
    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Row | None: ...


class Database(Protocol):
    dialect: Dialect

    def transaction(self) -> Any: ...  # async context manager yielding a Conn
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int: ...
    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[Row]: ...
    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Row | None: ...
    async def close(self) -> None: ...


async def connect(url: str) -> Database:
    if url.startswith("sqlite://"):
        db: Database = SqliteDatabase(url.removeprefix("sqlite://").removeprefix("/") or ":memory:")
    elif url.startswith(("postgresql://", "postgres://")):
        db = await PostgresDatabase.open(url)
    else:
        raise ValueError(f"unsupported database url {url.split(':', 1)[0]!r}")
    from .schema import migrate

    await migrate(db)
    return db


# --- SQLite ------------------------------------------------------------------------


class _SqliteConn:
    dialect: Dialect = "sqlite"

    def __init__(self, raw: sqlite3.Connection) -> None:
        self.raw = raw

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        return self.raw.execute(sql, tuple(params)).rowcount

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        return [dict(r) for r in self.raw.execute(sql, tuple(params)).fetchall()]

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        row = self.raw.execute(sql, tuple(params)).fetchone()
        return dict(row) if row is not None else None


class SqliteDatabase:
    """One connection guarded by a lock: SQLite has a single writer anyway.

    Calls run on the event loop thread; they are short local writes. The lock makes every
    ``transaction()`` atomic with respect to other coroutines.
    """

    dialect: Dialect = "sqlite"

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._raw = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._raw.row_factory = sqlite3.Row
        self._raw.execute("PRAGMA journal_mode=WAL")
        self._raw.execute("PRAGMA foreign_keys=ON")
        self._raw.execute("PRAGMA busy_timeout=5000")
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_SqliteConn]:
        async with self._lock:
            self._raw.execute("BEGIN IMMEDIATE")
            try:
                yield _SqliteConn(self._raw)
            except BaseException:
                self._raw.execute("ROLLBACK")
                raise
            self._raw.execute("COMMIT")

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        async with self.transaction() as conn:
            return await conn.execute(sql, params)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        async with self._lock:
            return await _SqliteConn(self._raw).fetchall(sql, params)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        async with self._lock:
            return await _SqliteConn(self._raw).fetchone(sql, params)

    async def close(self) -> None:
        self._raw.close()


# --- Postgres ----------------------------------------------------------------------


def _pg(sql: str) -> str:
    """``?`` placeholders to psycopg's ``%s`` (queries never contain literal ``?`` or ``%``)."""
    return sql.replace("%", "%%").replace("?", "%s")


class _PgConn:
    dialect: Dialect = "postgres"

    def __init__(self, raw: Any) -> None:
        self.raw = raw

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = await self.raw.execute(_pg(sql), tuple(params))
        return int(cur.rowcount)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        cur = await self.raw.execute(_pg(sql), tuple(params))
        return list(await cur.fetchall())

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        cur = await self.raw.execute(_pg(sql), tuple(params))
        row = await cur.fetchone()
        return dict(row) if row is not None else None


class PostgresDatabase:
    dialect: Dialect = "postgres"

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    async def open(cls, url: str, *, min_size: int = 1, max_size: int = 10) -> PostgresDatabase:
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=False,
        )
        await pool.open(wait=True, timeout=30)
        return cls(pool)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_PgConn]:
        async with self._pool.connection() as raw, raw.transaction():
            yield _PgConn(raw)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        async with self._pool.connection() as raw:
            return await _PgConn(raw).execute(sql, params)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        async with self._pool.connection() as raw:
            return await _PgConn(raw).fetchall(sql, params)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        async with self._pool.connection() as raw:
            return await _PgConn(raw).fetchone(sql, params)

    async def close(self) -> None:
        await self._pool.close()
