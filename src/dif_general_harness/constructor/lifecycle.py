"""Constructor v3: the lifecycle after the first deploy (ARCHITECTURE §0.4, decision 38).

- **adjust:** change an instance's values (the client's answers); the spec is re-resolved,
  validated and diffed before anything is written.
- **upgrade:** move an instance to a newer pack version; reports the new values the pack
  needs and what changes.
- **fleet:** register an instance with the control plane, offer it a config version, roll a
  change out across instances, roll back. Offers carry the **resolved config as the running
  container sees it** (paths under ``/app/solution``), so the instance's hash matches; a
  change that needs new files (a new prompt or eval suite) needs an image release instead,
  and the instance refuses the offer (its validation finds the missing file).

Safety is unchanged: every write validates (the loader's monotonic rules still apply), no
secret value is ever read or written here, and the fleet token lands in a file only its owner
can read, for the client's vault.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2

from ..spec.loader import PackCatalog, ResolvedSpec, load_instance
from .deploy import stage

CONTAINER_ROOT = "/app/solution"


@dataclass(frozen=True)
class Change:
    path: str
    before: Any
    after: Any

    def __str__(self) -> str:
        if self.before is None:
            return f"+ {self.path} = {_short(self.after)}"
        if self.after is None:
            return f"- {self.path} (was {_short(self.before)})"
        return f"~ {self.path}: {_short(self.before)} -> {_short(self.after)}"


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= 80 else text[:77] + "..."


def diff(before: Any, after: Any, path: str = "") -> list[Change]:
    """Leaf-level differences between two resolved specs."""
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[Change] = []
        for key in sorted(set(before) | set(after), key=str):
            here = f"{path}.{key}" if path else str(key)
            if key not in before:
                out.append(Change(here, None, after[key]))
            elif key not in after:
                out.append(Change(here, before[key], None))
            else:
                out += diff(before[key], after[key], here)
        return out
    return [] if before == after else [Change(path, before, after)]


@dataclass
class Plan:
    """A proposed change to an instance file: validated and diffed, not yet written."""

    instance_file: Path
    data: dict[str, Any]  # the new instance file contents
    before: ResolvedSpec
    after: ResolvedSpec
    changes: list[Change] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # values the new pack version needs

    @property
    def ok(self) -> bool:
        return self.after.ok and not self.missing

    def errors(self) -> list[str]:
        return [f"{i.path}: {i.message}" for i in self.after.issues if i.severity == "error"]

    def write(self) -> None:
        if not self.ok:
            raise ValueError("refusing to write an instance that does not validate")
        self.instance_file.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )


def _resolve_candidate(
    instance_file: Path, data: dict[str, Any], catalog: PackCatalog
) -> ResolvedSpec:
    """Resolve new file contents next to the original, so relative references still work."""
    fd, name = tempfile.mkstemp(prefix=".candidate-", suffix=".json", dir=instance_file.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        return load_instance(Path(name), catalog)
    finally:
        Path(name).unlink(missing_ok=True)


def _plan(instance_file: Path, data: dict[str, Any], catalog: PackCatalog) -> Plan:
    before = load_instance(instance_file, catalog)
    after = _resolve_candidate(instance_file, data, catalog)
    plan = Plan(instance_file, data, before, after, diff(before.data, after.data))
    plan.missing = sorted(
        i.path.removeprefix("values.")
        for i in after.issues
        if i.severity == "error" and i.code == "missing_value"
    )
    return plan


def adjust(instance_file: Path, catalog: PackCatalog, values: dict[str, Any]) -> Plan:
    """Change instance values (``None`` removes one, falling back to the pack default)."""
    data = json.loads(instance_file.read_text(encoding="utf-8"))
    current = dict(data.get("values") or {})
    for name, value in values.items():
        if value is None:
            current.pop(name, None)
        else:
            current[name] = value
    data["values"] = current
    return _plan(instance_file, data, catalog)


def upgrade(instance_file: Path, catalog: PackCatalog, pack_id: str, version: str) -> Plan:
    """Point the instance at ``pack_id@^<major>.<minor>`` of ``version`` (which must exist)."""
    found = catalog.find(f"{pack_id}@{version}")
    major, minor, _ = str(found.data["solution"]["version"]).split(".", 2)
    data = json.loads(instance_file.read_text(encoding="utf-8"))
    extends = list(data.get("extends") or [])
    ids = [str(e).split("@", 1)[0] for e in extends]
    if pack_id not in ids:
        raise ValueError(f"the instance does not extend {pack_id}")
    extends[ids.index(pack_id)] = f"{pack_id}@^{major}.{minor}"
    data["extends"] = extends
    return _plan(instance_file, data, catalog)


def parse_value(raw: str) -> Any:
    """``--set name=value``: JSON when it parses (numbers, lists, objects), else text."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


# --- the fleet ---------------------------------------------------------------------------


def _rebase(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        return new + value[len(old) :] if value.startswith(old) else value
    if isinstance(value, dict):
        return {k: _rebase(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [_rebase(v, old, new) for v in value]
    return value


def container_data(
    instance_file: Path, catalog: PackCatalog, root: str = CONTAINER_ROOT
) -> dict[str, Any]:
    """The resolved config exactly as the deployed container resolves it."""
    with tempfile.TemporaryDirectory(prefix="dif-offer-") as tmp:
        folder = Path(tmp) / "solution"
        staged = stage(instance_file, catalog, folder)
        rebased: dict[str, Any] = _rebase(copy.deepcopy(staged.data), str(folder.resolve()), root)
        return rebased


class FleetError(RuntimeError):
    pass


class ControlClient:
    """The control plane's admin API, for Di-Factory operators (admin token from the env)."""

    def __init__(
        self, url: str, admin_token: str, *, client: httpx2.AsyncClient | None = None
    ) -> None:
        if not url.startswith("https://") and "localhost" not in url and ".test" not in url:
            raise FleetError("the control plane must be reached over https")
        self.url = url.rstrip("/")
        self.headers = {"authorization": f"Bearer {admin_token}"}
        self.http = client or httpx2.AsyncClient(timeout=30.0)

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        response = await self.http.request(
            method, f"{self.url}{path}", json=body, headers=self.headers
        )
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise FleetError(f"control plane: HTTP {response.status_code}: {detail}")
        return response.json()

    async def public_key(self) -> str:
        return str((await self._call("GET", "/v1/public-key"))["public_key"])

    async def register(self, tenant: str, instance: str, by: str) -> str:
        body = {"tenant": tenant, "instance": instance, "by": by}
        return str((await self._call("POST", "/v1/admin/instances", body))["token"])

    async def offer(
        self, tenant: str, instance: str, data: dict[str, Any], approved_by: str, gate: str
    ) -> dict[str, Any]:
        body = {"data": data, "approved_by": approved_by, "gate": gate}
        result: dict[str, Any] = await self._call(
            "POST", f"/v1/admin/instances/{tenant}/{instance}/offers", body
        )
        return result

    async def rollback_instance(
        self, tenant: str, instance: str, to_hash: str, approved_by: str
    ) -> dict[str, Any]:
        body = {"rollback_to": to_hash, "approved_by": approved_by}
        result: dict[str, Any] = await self._call(
            "POST", f"/v1/admin/instances/{tenant}/{instance}/offers", body
        )
        return result

    async def rollout(
        self, name: str, approved_by: str, steps: list[dict[str, Any]], gate: str
    ) -> dict[str, Any]:
        body = {"name": name, "approved_by": approved_by, "steps": steps, "gate": gate}
        result: dict[str, Any] = await self._call("POST", "/v1/admin/rollouts", body)
        return result

    async def rollout_rollback(self, rollout_id: str, approved_by: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._call(
            "POST", f"/v1/admin/rollouts/{rollout_id}/rollback", {"approved_by": approved_by}
        )
        return result

    async def fleet(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._call("GET", "/v1/admin/fleet")
        return result


def write_token(out_dir: Path, tenant: str, instance: str, token: str) -> Path:
    """The fleet token, for the client's vault (secret ``fleet_token``): owner-only."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{tenant}.{instance}.fleet_token"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token)
    return path
