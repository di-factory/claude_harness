"""Versioned instance configuration (ARCHITECTURE §3.14): every change is a new version,
activated explicitly and reversible.

Lifecycle: ``pending`` (proposed) -> ``approved`` (by a named person; decision 37: Jag
approves what reaches a client) -> ``active`` (exactly one) -> ``retired``. ``rollback``
re-activates the previously active version. A version stores the fully resolved spec, so
what runs is exactly what was approved, byte for byte (``hash``).

Nothing activates without validation: the stored data is parsed and validated again, and
a version with errors is refused.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from ..core.scope import Scope
from ..spec.errors import SpecError
from ..spec.loader import ResolvedSpec, data_hash, resolved_from_data
from ..store.db import Database, Row


class ConfigError(ValueError):
    pass


def config_hash(data: dict[str, Any]) -> str:
    return data_hash(data)


@dataclass(frozen=True)
class ConfigVersion:
    version: int
    hash: str
    status: str
    created_at: float
    created_by: str
    note: str | None
    data: dict[str, Any]

    @classmethod
    def from_row(cls, row: Row) -> ConfigVersion:
        return cls(
            int(row["version"]), row["hash"], row["status"], float(row["created_at"]),
            row["created_by"], row["note"], json.loads(row["data"]),
        )  # fmt: skip


class ConfigStore:
    def __init__(self, db: Database, scope: Scope) -> None:
        self.db = db
        self.scope = scope

    def _key(self) -> tuple[str, str]:
        return self.scope.tenant_id, self.scope.instance_id

    async def propose(
        self, data: dict[str, Any], by: str, note: str = "", *, approved: bool = False
    ) -> ConfigVersion:
        resolved = self._validate(data)
        digest = config_hash(resolved.data)
        async with self.db.transaction() as conn:
            if conn.dialect == "postgres":
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(?))",
                    (f"config/{'/'.join(self._key())}",),
                )
            row = await conn.fetchone(
                "SELECT MAX(version) AS v FROM config_versions"
                " WHERE tenant_id = ? AND instance_id = ?",
                self._key(),
            )
            version = int(row["v"]) + 1 if row and row["v"] is not None else 1
            await conn.execute(
                "INSERT INTO config_versions (tenant_id, instance_id, version, hash, data, status,"
                " created_at, created_by, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*self._key(), version, digest, json.dumps(resolved.data, ensure_ascii=False),
                 "approved" if approved else "pending", time.time(), by, note),
            )  # fmt: skip
        got = await self.get(version)
        assert got is not None
        return got

    async def approve(self, version: int, by: str) -> None:
        changed = await self.db.execute(
            "UPDATE config_versions SET status = 'approved', note = COALESCE(note, '') || ?"
            " WHERE tenant_id = ? AND instance_id = ? AND version = ? AND status = 'pending'",
            (f" [approved by {by}]", *self._key(), version),
        )
        if not changed:
            raise ConfigError(f"version {version} is not pending")

    async def activate(self, version: int) -> ConfigVersion:
        target = await self.get(version)
        if target is None:
            raise ConfigError(f"no version {version}")
        if target.status not in {"approved", "retired", "active"}:
            raise ConfigError(f"version {version} is {target.status}; approve it first")
        self._validate(target.data)
        if config_hash(target.data) != target.hash:
            raise ConfigError(f"version {version} does not match its hash; refusing it")
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE config_versions SET status = 'retired'"
                " WHERE tenant_id = ? AND instance_id = ? AND status = 'active'",
                self._key(),
            )
            await conn.execute(
                "UPDATE config_versions SET status = 'active'"
                " WHERE tenant_id = ? AND instance_id = ? AND version = ?",
                (*self._key(), version),
            )
        active = await self.get(version)
        assert active is not None
        return active

    async def rollback(self) -> ConfigVersion:
        """Re-activate the most recent retired version."""
        current = await self.active()
        rows = await self.db.fetchall(
            "SELECT version FROM config_versions WHERE tenant_id = ? AND instance_id = ?"
            " AND status = 'retired' ORDER BY version DESC",
            self._key(),
        )
        for row in rows:
            if current is None or int(row["version"]) != current.version:
                return await self.activate(int(row["version"]))
        raise ConfigError("no earlier version to roll back to")

    async def active(self) -> ConfigVersion | None:
        row = await self.db.fetchone(
            "SELECT * FROM config_versions WHERE tenant_id = ? AND instance_id = ?"
            " AND status = 'active'",
            self._key(),
        )
        return ConfigVersion.from_row(row) if row else None

    async def get(self, version: int) -> ConfigVersion | None:
        row = await self.db.fetchone(
            "SELECT * FROM config_versions WHERE tenant_id = ? AND instance_id = ? AND version = ?",
            (*self._key(), version),
        )
        return ConfigVersion.from_row(row) if row else None

    async def history(self) -> list[ConfigVersion]:
        rows = await self.db.fetchall(
            "SELECT * FROM config_versions WHERE tenant_id = ? AND instance_id = ?"
            " ORDER BY version",
            self._key(),
        )
        return [ConfigVersion.from_row(r) for r in rows]

    def _validate(self, data: dict[str, Any]) -> ResolvedSpec:
        try:
            resolved = resolved_from_data(data)
        except SpecError as exc:
            raise ConfigError(f"invalid spec: {exc}") from None
        if not resolved.ok:
            errors = [i for i in resolved.issues if i.severity == "error"]
            raise ConfigError(
                "invalid spec: " + "; ".join(f"{i.path}: {i.message}" for i in errors)
            )
        if resolved.spec.kind != "instance":
            raise ConfigError("only instance specs are versioned")
        sol, tenant = resolved.spec.solution, resolved.spec.tenant
        if (
            sol.id != self.scope.instance_id
            or (tenant.id if tenant else "local") != self.scope.tenant_id
        ):
            raise ConfigError("the spec belongs to another tenant or instance")
        return resolved
