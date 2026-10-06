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
    [  # M3: eval results per config version (drift between runs)
        """CREATE TABLE IF NOT EXISTS eval_results (
            run_id TEXT NOT NULL, tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            config_version TEXT NOT NULL, suite TEXT NOT NULL, case_id TEXT NOT NULL,
            status TEXT NOT NULL, reasons TEXT NOT NULL, unsafe_actions INTEGER NOT NULL,
            cost_usd DOUBLE PRECISION NOT NULL, created_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (run_id, suite, case_id))""",
        """CREATE INDEX IF NOT EXISTS eval_results_by_instance
            ON eval_results (tenant_id, instance_id, created_at)""",
    ],
    [  # M4: model usage per day, agent, role, vendor and model (cost reports)
        """CREATE TABLE IF NOT EXISTS usage (
            tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL, day TEXT NOT NULL,
            agent TEXT NOT NULL, role TEXT NOT NULL, vendor TEXT NOT NULL, model TEXT NOT NULL,
            calls INTEGER NOT NULL, input_tokens BIGINT NOT NULL, output_tokens BIGINT NOT NULL,
            cache_read_tokens BIGINT NOT NULL, cache_write_tokens BIGINT NOT NULL,
            usd DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (tenant_id, instance_id, day, agent, role, vendor, model))""",
    ],
    [  # M4: the control plane (Di-Factory side): instances, signed offers, rollouts
        """CREATE TABLE IF NOT EXISTS fleet_instances (
            tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL, token_hash TEXT NOT NULL,
            registered_at DOUBLE PRECISION NOT NULL, last_seen DOUBLE PRECISION,
            report TEXT, PRIMARY KEY (tenant_id, instance_id))""",
        """CREATE TABLE IF NOT EXISTS fleet_offers (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            kind TEXT NOT NULL, body TEXT NOT NULL, hash TEXT NOT NULL, approved_by TEXT NOT NULL,
            signature TEXT NOT NULL, gate TEXT NOT NULL, status TEXT NOT NULL, note TEXT,
            rollout_id TEXT, previous_hash TEXT, result TEXT,
            created_at DOUBLE PRECISION NOT NULL, decided_at DOUBLE PRECISION)""",
        """CREATE INDEX IF NOT EXISTS fleet_offers_by_instance
            ON fleet_offers (tenant_id, instance_id, status, created_at)""",
        """CREATE TABLE IF NOT EXISTS fleet_rollouts (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, approved_by TEXT NOT NULL,
            status TEXT NOT NULL, gate TEXT NOT NULL, steps TEXT NOT NULL,
            position INTEGER NOT NULL, error TEXT, created_at DOUBLE PRECISION NOT NULL,
            finished_at DOUBLE PRECISION)""",
    ],
    [  # file and batch triggers: what each trigger has already seen (object versions, keys)
        """CREATE TABLE IF NOT EXISTS trigger_seen (
            tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL, trigger_name TEXT NOT NULL,
            seen_key TEXT NOT NULL, seen_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (tenant_id, instance_id, trigger_name, seen_key))""",
    ],
    [  # hybrid retrieval and remote knowledge sources
        "ALTER TABLE knowledge_docs ADD COLUMN source_version TEXT",
        """CREATE TABLE IF NOT EXISTS knowledge_vectors (
            chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
            instance_id TEXT NOT NULL, corpus TEXT NOT NULL, model TEXT NOT NULL,
            vector TEXT NOT NULL)""",
        """CREATE INDEX IF NOT EXISTS knowledge_vectors_by_corpus
            ON knowledge_vectors (tenant_id, instance_id, corpus)""",
        "CREATE INDEX IF NOT EXISTS knowledge_vectors_by_doc ON knowledge_vectors (doc_id)",
    ],
    [  # a document's own text, so an owner can read and edit exactly what the agent knows
        "ALTER TABLE knowledge_docs ADD COLUMN body TEXT",
    ],
    [  # questions the documents did not answer: the owner's list of what the FAQ lacks
        """CREATE TABLE IF NOT EXISTS knowledge_gaps (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            corpus TEXT NOT NULL, gap_key TEXT NOT NULL, question TEXT NOT NULL,
            asked INTEGER NOT NULL, status TEXT NOT NULL, session_id TEXT,
            first_seen DOUBLE PRECISION NOT NULL, last_seen DOUBLE PRECISION NOT NULL)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS knowledge_gaps_by_key
            ON knowledge_gaps (tenant_id, instance_id, corpus, gap_key)""",
    ],
    [  # run records (append only: why a run stopped, what failed, what it changed)
        """CREATE TABLE IF NOT EXISTS run_records (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            kind TEXT NOT NULL, name TEXT NOT NULL, started DOUBLE PRECISION NOT NULL,
            ended DOUBLE PRECISION NOT NULL, stop_reason TEXT NOT NULL, body TEXT NOT NULL)""",
        """CREATE INDEX IF NOT EXISTS run_records_by_time
            ON run_records (tenant_id, instance_id, ended)""",
    ],
    [  # research graphs: nodes with sources, edges with evidence
        """CREATE TABLE IF NOT EXISTS graph_nodes (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            graph TEXT NOT NULL, label_key TEXT NOT NULL, label TEXT NOT NULL,
            type TEXT NOT NULL, status TEXT NOT NULL, confidence DOUBLE PRECISION NOT NULL,
            sources TEXT NOT NULL, fields TEXT NOT NULL, last_checked DOUBLE PRECISION,
            created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS graph_nodes_by_label
            ON graph_nodes (tenant_id, instance_id, graph, label_key)""",
        """CREATE TABLE IF NOT EXISTS graph_edges (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            graph TEXT NOT NULL, from_id TEXT NOT NULL, to_id TEXT NOT NULL, type TEXT NOT NULL,
            evidence TEXT NOT NULL, confidence DOUBLE PRECISION NOT NULL, run_id TEXT,
            created_at DOUBLE PRECISION NOT NULL)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS graph_edges_by_pair
            ON graph_edges (tenant_id, instance_id, graph, from_id, to_id, type)""",
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
