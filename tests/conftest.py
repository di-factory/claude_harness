from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.core.scope import Scope

EXAMPLES = Path(__file__).resolve().parent.parent / "docs" / "spec" / "examples"
PACK_IDS = [
    "pyme-appointment-agent",
    "service-desk-cell",
    "conversational-rag",
    "pyme-receipt-processing",
    "dev-cell",
    "opc-c-suite",
    "research-graph",
]


@pytest.fixture
def examples(tmp_path: Path) -> Path:
    """A private, mutable copy of the example packs and instances."""
    dest = tmp_path / "examples"
    shutil.copytree(EXAMPLES, dest)
    return dest


@pytest.fixture
def scope() -> Scope:
    return Scope(tenant_id="clinica-sonrisa", instance_id="appointments")


def write_helper_solution(root: Path) -> Path:
    """A small pack (general + coding tools) and an instance of it, for runtime tests."""
    pack = root / "packs" / "helper"
    (pack / "prompts").mkdir(parents=True)
    (pack / "prompts" / "helper.md").write_text("You help {{var.owner}} with files and notes.")
    (pack / "pack.json").write_text(
        json.dumps(
            {
                "spec_version": "1",
                "kind": "pack",
                "solution": {"id": "helper", "version": "1.0.0", "lob": "general"},
                "variables": {"owner": {"type": "string", "required": True}},
                "secrets": {"llm": {"description": "Model API key"}},
                "models": {
                    "roles": {"main": {"provider": "anthropic", "model": "claude-opus-5-5"}},
                    "providers": {"anthropic": {"api_key": {"$secret": "llm"}}},
                },
                "agents": {
                    "helper": {
                        "prompt": "prompts/helper.md",
                        "model_role": "main",
                        "tools": ["notes.*", "coding.*"],
                        "workspace": "repo",
                    }
                },
                "workspaces": {"repo": {"type": "local"}},
                "tools": {"packs": ["general", "coding"]},
                "evals": {"suites": ["evals/smoke.yaml"]},
            }
        )
    )
    (pack / "evals").mkdir()
    (pack / "evals" / "smoke.yaml").write_text("cases: []\n")
    instance = root / "instances" / "acme-helper.json"
    instance.parent.mkdir()
    instance.write_text(
        json.dumps(
            {
                "spec_version": "1",
                "kind": "instance",
                "solution": {"id": "acme-helper", "version": "1.0.0", "lob": "general"},
                "extends": ["helper@^1.0"],
                "tenant": {"id": "acme", "name": "ACME"},
                "values": {"owner": "Ana"},
            }
        )
    )
    return instance


@pytest.fixture
def helper_solution(tmp_path: Path) -> Path:
    return write_helper_solution(tmp_path)


# --- databases ---------------------------------------------------------------------

PG_BIN = Path("/usr/lib/postgresql/16/bin")


@pytest.fixture(scope="session")
def pg_server() -> Iterator[str | None]:
    """A throwaway local Postgres (unix socket only, no network). None when not installed."""
    if not (PG_BIN / "initdb").exists():
        yield None
        return
    root = Path(tempfile.mkdtemp(prefix="dif-pg-"))
    root.chmod(0o755)
    as_pg: list[str] = []
    if os.geteuid() == 0:  # initdb refuses to run as root
        shutil.chown(root, "postgres", "postgres")
        as_pg = ["runuser", "-u", "postgres", "--"]
    data = root / "data"
    subprocess.run(
        [*as_pg, str(PG_BIN / "initdb"), "-D", str(data), "-A", "trust", "-U", "dif"],
        check=True,
        capture_output=True,
        timeout=120,
    )
    opts = f"-k {root} -c listen_addresses='' -p 55432 -c fsync=off"
    # the server must not inherit our pipes, or waiting on them never ends
    quiet: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "timeout": 60,
    }
    subprocess.run(
        [*as_pg, str(PG_BIN / "pg_ctl"), "-D", str(data), "-o", opts, "-l", str(root / "log"),
         "-w", "start"],
        check=True,
        **quiet,
    )  # fmt: skip
    try:
        yield f"postgresql://dif@/postgres?host={root}&port=55432"
    finally:
        subprocess.run(
            [*as_pg, str(PG_BIN / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
            **quiet,
        )
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request: pytest.FixtureRequest, tmp_path: Path, pg_server: str | None) -> Any:
    """A fresh, migrated database: SQLite and (when installed) Postgres."""
    from dif_general_harness.store import connect

    if request.param == "sqlite":
        database = await connect(f"sqlite:///{tmp_path / 'dif.db'}")
        yield database
        await database.close()
        return
    if pg_server is None:
        pytest.skip("Postgres is not installed")
    import psycopg

    name = f"t_{uuid.uuid4().hex[:12]}"
    async with await psycopg.AsyncConnection.connect(pg_server, autocommit=True) as admin:
        await admin.execute(f"CREATE DATABASE {name}")
    database = await connect(pg_server.replace("/postgres?", f"/{name}?"))
    yield database
    await database.close()
