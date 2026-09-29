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
    [  # M3: workflow runs and the items relative triggers watch
        """CREATE TABLE IF NOT EXISTS workflow_runs (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            workflow TEXT NOT NULL, status TEXT NOT NULL, input TEXT NOT NULL,
            state TEXT NOT NULL, outcome TEXT, error TEXT,
            wait_kind TEXT, wait_channel TEXT, wait_contact TEXT, wait_event TEXT,
            created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL)""",
        """CREATE INDEX IF NOT EXISTS runs_waiting
            ON workflow_runs (tenant_id, instance_id, status, wait_kind, wait_channel,
                              wait_contact)""",
        """CREATE TABLE IF NOT EXISTS source_items (
            tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL, source TEXT NOT NULL,
            item_id TEXT NOT NULL, start_ts DOUBLE PRECISION NOT NULL, data TEXT NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (tenant_id, instance_id, source, item_id))""",
    ],
    [  # M3: the team ledger
        """CREATE TABLE IF NOT EXISTS ledger_tasks (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            fields TEXT NOT NULL, created_by TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ledger_by_instance ON ledger_tasks (tenant_id, instance_id)",
    ],
    [  # M3: memory (episodic, semantic, procedural)
        """CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            scope_kind TEXT NOT NULL, scope_key TEXT NOT NULL, layer TEXT NOT NULL,
            key TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
            version INTEGER NOT NULL, successes INTEGER NOT NULL, source TEXT,
            created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
            expires_at DOUBLE PRECISION)""",
        """CREATE INDEX IF NOT EXISTS memories_by_scope
            ON memories (tenant_id, instance_id, scope_kind, scope_key, layer, status)""",
    ],
    [  # M3: knowledge corpora (documents and their chunks)
        """CREATE TABLE IF NOT EXISTS knowledge_docs (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            corpus TEXT NOT NULL, uri TEXT NOT NULL, origin TEXT NOT NULL, title TEXT NOT NULL,
            hash TEXT NOT NULL, version INTEGER NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
            UNIQUE (tenant_id, instance_id, corpus, uri))""",
        """CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
            instance_id TEXT NOT NULL, corpus TEXT NOT NULL, ord INTEGER NOT NULL,
            section TEXT NOT NULL, text TEXT NOT NULL)""",
        """CREATE INDEX IF NOT EXISTS knowledge_chunks_by_corpus
            ON knowledge_chunks (tenant_id, instance_id, corpus)""",
        """CREATE INDEX IF NOT EXISTS knowledge_chunks_by_doc ON knowledge_chunks (doc_id)""",
    ],
    [  # M3: the feedback loop (candidate and approved constraints)
        """CREATE TABLE IF NOT EXISTS constraints (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            agent TEXT NOT NULL, text TEXT NOT NULL, fingerprint TEXT NOT NULL,
            status TEXT NOT NULL, source TEXT NOT NULL, evidence TEXT NOT NULL,
            occurrences INTEGER NOT NULL, created_at DOUBLE PRECISION NOT NULL,
            decided_at DOUBLE PRECISION, decided_by TEXT)""",
        """CREATE INDEX IF NOT EXISTS constraints_by_agent
            ON constraints (tenant_id, instance_id, agent, status)""",
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
