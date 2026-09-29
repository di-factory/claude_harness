"""The shared task ledger for agent teams (SOLUTION_SPEC §5.15).

Tasks are rows with the fields the spec declares (``"owner": "agent"``,
``"status": "enum:todo,doing,blocked,done"``, ``"due": "date"``, ``"needs_founder": "boolean"``,
anything else ``string``). Agents reach it through ``ledger.*`` tools, only the agents in
``visible_to`` (``human`` means people, through the admin API).

Assigning a task to an agent emits ``ledger.task_assigned`` (``{"task": {...}}``), which an
``event`` trigger can route to that agent; a task that matches an escalation rule
(``ledger.task.needs_founder``) goes to a person.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from ..core.scope import Scope
from ..spec.schema import Ledger
from ..store.db import Database, Row
from ..tools.registry import Effect, InputError, Tool, schema_check

Emit = Callable[[str, dict[str, Any]], Awaitable[Any]]
OnTask = Callable[[dict[str, Any]], Awaitable[None]]


def field_schema(kind: str, agents: list[str]) -> dict[str, Any]:
    if kind == "agent":
        return {"type": "string", "enum": agents}
    if kind.startswith("enum:"):
        return {"type": "string", "enum": [v.strip() for v in kind[5:].split(",") if v.strip()]}
    if kind == "boolean":
        return {"type": "boolean"}
    if kind in {"number", "integer"}:
        return {"type": kind}
    if kind == "date":
        return {"type": "string", "format": "date", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
    return {"type": "string"}


class LedgerStore:
    def __init__(self, db: Database, scope: Scope, spec: Ledger, agents: list[str]) -> None:
        self.db = db
        self.scope = scope
        self.spec = spec
        self.fields = dict(spec.fields) or {"title": "string", "owner": "agent", "status": "string"}
        self.fields.setdefault("title", "string")
        self.agents = agents
        self.emit: Emit | None = None  # set by the service
        self.on_task: OnTask | None = None  # escalation rules, set by the instance

    def schema(self, *, require_title: bool) -> dict[str, Any]:
        props = {name: field_schema(kind, self.agents) for name, kind in self.fields.items()}
        return {
            "type": "object",
            "properties": props,
            "required": ["title"] if require_title else [],
            "additionalProperties": False,
        }

    def _row(self, row: Row) -> dict[str, Any]:
        return {"id": row["id"], **json.loads(row["fields"]), "created_by": row["created_by"]}

    async def create(self, fields: dict[str, Any], by: str) -> dict[str, Any]:
        _dates(fields)
        task_id = uuid.uuid4().hex[:10]
        now = time.time()
        fields.setdefault("status", _first_status(self.fields))
        await self.db.execute(
            "INSERT INTO ledger_tasks (id, tenant_id, instance_id, fields, created_by, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                self.scope.tenant_id,
                self.scope.instance_id,
                json.dumps(fields),
                by,
                now,
                now,
            ),
        )
        task = await self.get(task_id)
        assert task is not None
        await self._after(task, previous_owner=None)
        return task

    async def update(self, task_id: str, changes: dict[str, Any], by: str) -> dict[str, Any]:
        _dates(changes)
        task = await self.get(task_id)
        if task is None:
            raise KeyError(f"no task {task_id!r}")
        fields = {k: v for k, v in task.items() if k not in {"id", "created_by"}}
        previous_owner = fields.get("owner")
        fields.update({k: v for k, v in changes.items() if v is not None})
        await self.db.execute(
            "UPDATE ledger_tasks SET fields = ?, updated_at = ? WHERE id = ? AND tenant_id = ?"
            " AND instance_id = ?",
            (
                json.dumps(fields),
                time.time(),
                task_id,
                self.scope.tenant_id,
                self.scope.instance_id,
            ),
        )
        updated = await self.get(task_id)
        assert updated is not None
        await self._after(updated, previous_owner=previous_owner)
        return updated

    async def _after(self, task: dict[str, Any], previous_owner: Any) -> None:
        owner = task.get("owner")
        if owner and owner != previous_owner and owner in self.agents and self.emit is not None:
            await self.emit("ledger.task_assigned", {"task": task})
        if self.on_task is not None:
            await self.on_task(task)

    async def get(self, task_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM ledger_tasks WHERE id = ? AND tenant_id = ? AND instance_id = ?",
            (task_id, self.scope.tenant_id, self.scope.instance_id),
        )
        return self._row(row) if row else None

    async def list_tasks(self, **filters: Any) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM ledger_tasks WHERE tenant_id = ? AND instance_id = ?"
            " ORDER BY created_at",
            (self.scope.tenant_id, self.scope.instance_id),
        )
        tasks = [self._row(r) for r in rows]
        wanted = {k: v for k, v in filters.items() if v is not None}
        return [t for t in tasks if all(t.get(k) == v for k, v in wanted.items())]

    def tools(self, agent: str) -> list[Tool]:
        """The ``ledger.*`` tools, acting as ``agent``."""
        store = self
        create_schema = self.schema(require_title=True)
        update_schema = {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                **self.schema(require_title=False)["properties"],
            },
            "required": ["id"],
            "additionalProperties": False,
        }
        list_schema = {
            "type": "object",
            "properties": {
                k: v
                for k, v in self.schema(require_title=False)["properties"].items()
                if k in {"owner", "status", "priority"}
            },
            "additionalProperties": False,
        }

        async def create_task(**fields: Any) -> dict[str, Any]:
            return await store.create(fields, f"agent:{agent}")

        async def update_task(id: str, **changes: Any) -> dict[str, Any]:
            return await store.update(id, changes, f"agent:{agent}")

        async def list_tasks(**filters: Any) -> list[dict[str, Any]]:
            return await store.list_tasks(**filters)

        async def get_task(id: str) -> dict[str, Any]:
            task = await store.get(id)
            if task is None:
                raise KeyError(f"no task {id!r}")
            return task

        def make(name: str, fn: Any, schema: dict[str, Any], effect: Effect, doc: str) -> Tool:
            return Tool(
                name=name,
                description=doc,
                input_schema=schema,
                handler=fn,
                effect=effect,
                check_input=schema_check(schema),
                source="ledger",
            )

        get_schema = {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        }
        return [
            make(
                "ledger.create_task",
                create_task,
                create_schema,
                Effect.WRITE,
                "Create a task on the team ledger. Set owner to hand it to an agent.",
            ),
            make(
                "ledger.update_task",
                update_task,
                update_schema,
                Effect.WRITE,
                "Change a task's fields (status, owner, due...). "
                "Reassigning notifies the new owner.",
            ),
            make(
                "ledger.list_tasks",
                list_tasks,
                list_schema,
                Effect.READ,
                "List ledger tasks, optionally by owner, status or priority.",
            ),
            make("ledger.get_task", get_task, get_schema, Effect.READ, "Read one ledger task."),
        ]


def _first_status(fields: dict[str, str]) -> str:
    kind = fields.get("status", "string")
    return kind[5:].split(",")[0].strip() if kind.startswith("enum:") else "todo"


def _dates(fields: dict[str, Any]) -> None:
    for key, value in fields.items():
        if key in {"due", "date"} and isinstance(value, str):
            try:
                date.fromisoformat(value)
            except ValueError:
                raise InputError(f"{key}: expected a date like 2026-10-01") from None
