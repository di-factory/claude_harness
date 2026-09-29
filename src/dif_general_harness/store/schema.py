"""Tables for the headless runtime. Portable DDL (SQLite and Postgres), keyed by tenant.

Migrations are ordered and idempotent; ``schema_version`` records the last one applied.
Add a new migration at the end, never edit an applied one.
"""

from __future__ import annotations

from .db import Database

MIGRATIONS: list[list[str]] = [
    [
        """CREATE TABLE IF NOT EXISTS events (
            tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL, session_id TEXT NOT NULL,
            seq BIGINT NOT NULL, type TEXT NOT NULL, ts DOUBLE PRECISION NOT NULL,
            data TEXT NOT NULL,
            PRIMARY KEY (tenant_id, instance_id, session_id, seq))""",
        """CREATE TABLE IF NOT EXISTS sessions (
            tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL, session_id TEXT NOT NULL,
            agent_id TEXT NOT NULL, channel TEXT, contact_key TEXT,
            state TEXT NOT NULL DEFAULT 'active', created_at DOUBLE PRECISION NOT NULL,
            last_active DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (tenant_id, instance_id, session_id))""",
        """CREATE INDEX IF NOT EXISTS sessions_by_contact
            ON sessions (tenant_id, instance_id, channel, contact_key, last_active)""",
        """CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
            run_at DOUBLE PRECISION NOT NULL, locked_until DOUBLE PRECISION,
            dedupe_key TEXT, last_error TEXT,
            created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS jobs_dedupe
            ON jobs (tenant_id, instance_id, dedupe_key)""",
        "CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (status, run_at)",
        """CREATE TABLE IF NOT EXISTS inbox (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            kind TEXT NOT NULL, status TEXT NOT NULL, session_id TEXT, title TEXT NOT NULL,
            payload TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
            decided_at DOUBLE PRECISION, decided_by TEXT, note TEXT)""",
        "CREATE INDEX IF NOT EXISTS inbox_open ON inbox (tenant_id, instance_id, status)",
        """CREATE TABLE IF NOT EXISTS consent (
            tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL, contact_key TEXT NOT NULL,
            channel TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (tenant_id, instance_id, contact_key, channel))""",
        """CREATE TABLE IF NOT EXISTS audit (
            tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL, seq BIGINT NOT NULL,
            ts DOUBLE PRECISION NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
            subject TEXT NOT NULL, data TEXT NOT NULL, prev_hash TEXT NOT NULL,
            hash TEXT NOT NULL,
            PRIMARY KEY (tenant_id, instance_id, seq))""",
        """CREATE TABLE IF NOT EXISTS pii_tokens (
            tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL, token TEXT NOT NULL,
            kind TEXT NOT NULL, value TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (tenant_id, instance_id, token))""",
        """CREATE UNIQUE INDEX IF NOT EXISTS pii_by_value
            ON pii_tokens (tenant_id, instance_id, kind, value)""",
        """CREATE TABLE IF NOT EXISTS config_versions (
            tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL, version INTEGER NOT NULL,
            hash TEXT NOT NULL, data TEXT NOT NULL, status TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL, created_by TEXT NOT NULL, note TEXT,
            PRIMARY KEY (tenant_id, instance_id, version))""",
        """CREATE TABLE IF NOT EXISTS spend (
            tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL, key TEXT NOT NULL,
            day TEXT NOT NULL, usd DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (tenant_id, instance_id, key, day))""",
    ],
    [  # M3: contact attributes for conditions
        """CREATE TABLE IF NOT EXISTS contacts (
            tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL, contact_key TEXT NOT NULL,
            attrs TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (tenant_id, instance_id, contact_key))""",
    ],
]


async def migrate(db: Database) -> int:
    """Apply pending migrations; returns the schema version."""
    await db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    async with db.transaction() as conn:
        if conn.dialect == "postgres":  # one migrator at a time across containers
            await conn.execute("SELECT pg_advisory_xact_lock(4217001)")
        row = await conn.fetchone("SELECT MAX(version) AS v FROM schema_version")
        current = int(row["v"]) if row and row["v"] is not None else 0
        for number, statements in enumerate(MIGRATIONS[current:], start=current + 1):
            for statement in statements:
                await conn.execute(statement)
            await conn.execute("INSERT INTO schema_version (version) VALUES (?)", (number,))
    return len(MIGRATIONS)
