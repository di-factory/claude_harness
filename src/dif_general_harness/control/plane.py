"""The control plane (ARCHITECTURE §3.22), Di-Factory's side of fleet operations.

It never reaches into a client's cloud: instance agents call it (outbound only), report
aggregates, and pull **signed offers**. What it keeps:

- **instances:** registered with a token (stored hashed), and their last heartbeat;
- **offers:** a config version (``data``) or a rollback (to a hash the instance ran), signed
  with the control plane's Ed25519 key and naming who approved it (decision 37). An offer
  may be **gated by evals**: the instance runs the offered config's eval suites and only
  activates it when they pass. The instance reports the outcome (``applied`` / ``rejected``);
- **rollouts:** one change (a pack upgrade) taken instance by instance. The next instance is
  offered its step only after the previous one applied it; a rejection halts the rollout
  and rolls back every instance already upgraded;
- an **audit record** of every change, in its own hash chain.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..core.scope import Scope
from ..fleet.instance_agent import signed_message
from ..governance import AuditLog
from ..spec.errors import SpecError
from ..spec.loader import data_hash, resolved_from_data
from ..store.db import Database

CONTROL_SCOPE = Scope(tenant_id="di-factory", instance_id="control-plane")
GATES = ("evals", "none")


class ControlError(ValueError):
    pass


def offer_errors(data: dict[str, Any]) -> list[str]:
    """What is wrong with an offered config, as far as the control plane can tell. File
    references point into the instance's container, so the instance checks those itself
    (and refuses an offer whose files it does not have)."""
    try:
        resolved = resolved_from_data(data, "offer")
    except SpecError as exc:
        return [f"the config does not parse: {exc}"]
    return [
        f"{i.path}: {i.message}"
        for i in resolved.issues
        if i.severity == "error" and i.code != "missing_file"
    ]


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Offer:
    id: str
    tenant_id: str
    instance_id: str
    kind: str  # config | rollback
    body: dict[str, Any]
    hash: str
    approved_by: str
    signature: str
    gate: str
    status: str  # offered | applied | rejected | superseded
    rollout_id: str | None
    previous_hash: str | None
    result: dict[str, Any] | None

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> Offer:
        return cls(
            r["id"],
            r["tenant_id"],
            r["instance_id"],
            r["kind"],
            json.loads(r["body"]),
            r["hash"],
            r["approved_by"],
            r["signature"],
            r["gate"],
            r["status"],
            r["rollout_id"],
            r["previous_hash"],
            json.loads(r["result"]) if r["result"] else None,
        )

    def wire(self) -> dict[str, Any]:
        """What the instance agent receives."""
        out: dict[str, Any] = {
            "offer": self.id,
            "approved_by": self.approved_by,
            "signature": self.signature,
            "gate": self.gate,
        }
        if self.kind == "rollback":
            out["rollback_to"] = self.body["rollback_to"]
        else:
            out["data"] = self.body
        return out

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant": self.tenant_id,
            "instance": self.instance_id,
            "kind": self.kind,
            "hash": self.hash,
            "approved_by": self.approved_by,
            "gate": self.gate,
            "status": self.status,
            "rollout": self.rollout_id,
            "result": self.result,
        }


class ControlPlane:
    def __init__(self, db: Database, key: Ed25519PrivateKey, *, stale_after_s: float = 300) -> None:
        self.db = db
        self.key = key
        self.audit = AuditLog(db)
        self.stale_after_s = stale_after_s

    def public_key(self) -> str:
        from cryptography.hazmat.primitives import serialization

        raw = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return base64.b64encode(raw).decode()

    async def _record(self, actor: str, action: str, subject: str, data: dict[str, Any]) -> None:
        await self.audit.record(CONTROL_SCOPE, actor, action, subject, data)

    # --- instances -----------------------------------------------------------------------

    async def register(self, tenant: str, instance: str, by: str) -> str:
        """Register (or re-key) an instance; returns its token, shown once."""
        token = secrets.token_urlsafe(32)
        await self.db.execute(
            "INSERT INTO fleet_instances (tenant_id, instance_id, token_hash, registered_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (tenant_id, instance_id)"
            " DO UPDATE SET token_hash = excluded.token_hash",
            (tenant, instance, token_hash(token), time.time()),
        )
        await self._record(by, "instance_registered", f"{tenant}/{instance}", {})
        return token

    async def authenticate(self, tenant: str, instance: str, token: str) -> bool:
        row = await self.db.fetchone(
            "SELECT token_hash FROM fleet_instances WHERE tenant_id = ? AND instance_id = ?",
            (tenant, instance),
        )
        return row is not None and secrets.compare_digest(row["token_hash"], token_hash(token))

    async def heartbeat(self, tenant: str, instance: str, report: dict[str, Any]) -> None:
        await self.db.execute(
            "UPDATE fleet_instances SET last_seen = ?, report = ?"
            " WHERE tenant_id = ? AND instance_id = ?",
            (time.time(), json.dumps(report, default=str), tenant, instance),
        )

    async def fleet(self) -> list[dict[str, Any]]:
        now = time.time()
        rows = await self.db.fetchall(
            "SELECT tenant_id, instance_id, last_seen, report FROM fleet_instances"
            " ORDER BY tenant_id, instance_id"
        )
        out = []
        for r in rows:
            report = json.loads(r["report"]) if r["report"] else {}
            pending = await self.pending(r["tenant_id"], r["instance_id"])
            seen = r["last_seen"]
            out.append(
                {
                    "tenant": r["tenant_id"],
                    "instance": r["instance_id"],
                    "last_seen": seen,
                    "healthy": bool(seen)
                    and now - float(seen) < self.stale_after_s
                    and report.get("audit_intact", False),
                    "running_config": report.get("running_config"),
                    "harness_version": report.get("harness_version"),
                    "spend_today_usd": (report.get("spend_today") or {}).get("tenant", 0.0),
                    "inbox_open": report.get("inbox_open", {}),
                    "escalation_rate": (report.get("metrics") or {}).get("escalation_rate"),
                    "evals": report.get("evals"),
                    "issues": report.get("issues", []),
                    "pending_offer": pending.public() if pending else None,
                }
            )
        return out

    async def running(self, tenant: str, instance: str) -> str | None:
        row = await self.db.fetchone(
            "SELECT report FROM fleet_instances WHERE tenant_id = ? AND instance_id = ?",
            (tenant, instance),
        )
        if row is None or not row["report"]:
            return None
        running = json.loads(row["report"]).get("running_config")
        return str(running) if running else None

    # --- offers ----------------------------------------------------------------------------

    def _sign(self, body: dict[str, Any], approved_by: str, gate: str) -> str:
        extra = {"gate": gate} if gate != "none" else {}
        message = signed_message(body, approved_by, **extra)
        return base64.b64encode(self.key.sign(message)).decode()

    async def _known(self, tenant: str, instance: str) -> None:
        row = await self.db.fetchone(
            "SELECT 1 AS x FROM fleet_instances WHERE tenant_id = ? AND instance_id = ?",
            (tenant, instance),
        )
        if row is None:
            raise ControlError(f"instance {tenant}/{instance} is not registered")

    async def offer_config(
        self,
        tenant: str,
        instance: str,
        data: dict[str, Any],
        approved_by: str,
        *,
        gate: str = "evals",
        note: str = "",
        rollout: str | None = None,
    ) -> Offer:
        """Sign a config version for one instance. It must validate here first."""
        await self._known(tenant, instance)
        if gate not in GATES:
            raise ControlError(f"gate must be one of {GATES}")
        if not approved_by:
            raise ControlError("an offer names who approved it")
        if errors := offer_errors(data):
            raise ControlError(f"the config does not validate: {'; '.join(errors)}")
        spec = resolved_from_data(data, "offer").spec
        target = (spec.tenant.id if spec.tenant else "", spec.solution.id)
        if target != (tenant, instance):
            raise ControlError(
                f"the config is for {target[0]}/{target[1]}, not {tenant}/{instance}"
            )
        return await self._offer(
            tenant, instance, "config", data, data_hash(data), approved_by, gate, note, rollout
        )

    async def offer_rollback(
        self,
        tenant: str,
        instance: str,
        to_hash: str,
        approved_by: str,
        *,
        note: str = "",
        rollout: str | None = None,
    ) -> Offer:
        await self._known(tenant, instance)
        body = {"rollback_to": to_hash}
        return await self._offer(
            tenant, instance, "rollback", body, to_hash, approved_by, "none", note, rollout
        )

    async def _offer(
        self,
        tenant: str,
        instance: str,
        kind: str,
        body: dict[str, Any],
        digest: str,
        approved_by: str,
        gate: str,
        note: str,
        rollout: str | None,
    ) -> Offer:
        await self.db.execute(
            "UPDATE fleet_offers SET status = 'superseded', decided_at = ?"
            " WHERE tenant_id = ? AND instance_id = ? AND status = 'offered'",
            (time.time(), tenant, instance),
        )
        offer_id = uuid.uuid4().hex[:12]
        await self.db.execute(
            "INSERT INTO fleet_offers (id, tenant_id, instance_id, kind, body, hash, approved_by,"
            " signature, gate, status, note, rollout_id, previous_hash, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'offered', ?, ?, ?, ?)",
            (
                offer_id,
                tenant,
                instance,
                kind,
                json.dumps(body, ensure_ascii=False),
                digest,
                approved_by,
                self._sign(body, approved_by, gate),
                gate,
                note,
                rollout,
                await self.running(tenant, instance),
                time.time(),
            ),
        )
        await self._record(
            approved_by,
            f"offer_{kind}",
            f"{tenant}/{instance}",
            {"offer": offer_id, "hash": digest, "gate": gate, "rollout": rollout},
        )
        found = await self.get_offer(offer_id)
        assert found is not None
        return found

    async def get_offer(self, offer_id: str) -> Offer | None:
        row = await self.db.fetchone("SELECT * FROM fleet_offers WHERE id = ?", (offer_id,))
        return Offer.from_row(row) if row else None

    async def pending(self, tenant: str, instance: str) -> Offer | None:
        row = await self.db.fetchone(
            "SELECT * FROM fleet_offers WHERE tenant_id = ? AND instance_id = ?"
            " AND status = 'offered' ORDER BY created_at DESC LIMIT 1",
            (tenant, instance),
        )
        return Offer.from_row(row) if row else None

    async def offers(self, tenant: str, instance: str) -> list[Offer]:
        rows = await self.db.fetchall(
            "SELECT * FROM fleet_offers WHERE tenant_id = ? AND instance_id = ?"
            " ORDER BY created_at",
            (tenant, instance),
        )
        return [Offer.from_row(r) for r in rows]

    async def result(
        self, tenant: str, instance: str, offer_id: str, status: str, detail: dict[str, Any]
    ) -> Offer:
        """The instance reports what happened to an offer; rollouts move on (or back)."""
        offer = await self.get_offer(offer_id)
        if offer is None or (offer.tenant_id, offer.instance_id) != (tenant, instance):
            raise ControlError(f"no offer {offer_id} for {tenant}/{instance}")
        if status not in ("applied", "rejected"):
            raise ControlError("status must be applied or rejected")
        if offer.status != "offered":
            return offer  # already decided (a retried report)
        before = detail.get("running_before") or offer.previous_hash
        await self.db.execute(
            "UPDATE fleet_offers SET status = ?, result = ?, decided_at = ?, previous_hash = ?"
            " WHERE id = ?",
            (status, json.dumps(detail, default=str), time.time(), before, offer_id),
        )
        await self._record(
            f"instance:{tenant}/{instance}",
            f"offer_{status}",
            offer_id,
            {"reason": detail.get("reason"), "evals": detail.get("evals")},
        )
        if offer.rollout_id:
            await self._advance(offer.rollout_id, offer_id, status, detail)
        updated = await self.get_offer(offer_id)
        assert updated is not None
        return updated

    # --- rollouts --------------------------------------------------------------------------

    async def start_rollout(
        self, name: str, approved_by: str, steps: list[dict[str, Any]], gate: str = "evals"
    ) -> dict[str, Any]:
        """One change taken instance by instance. Every step is validated before the first
        is offered."""
        if not steps:
            raise ControlError("a rollout needs at least one step")
        for step in steps:
            await self._known(str(step["tenant"]), str(step["instance"]))
            if errors := offer_errors(step["data"]):
                where = f"{step['tenant']}/{step['instance']}"
                raise ControlError(f"step {where} does not validate: {'; '.join(errors)}")
        rollout_id = uuid.uuid4().hex[:12]
        plan = [
            {
                "tenant": s["tenant"],
                "instance": s["instance"],
                "data": s["data"],
                "offer": None,
                "status": "waiting",
            }
            for s in steps
        ]
        await self.db.execute(
            "INSERT INTO fleet_rollouts (id, name, approved_by, status, gate, steps, position,"
            " created_at) VALUES (?, ?, ?, 'running', ?, ?, 0, ?)",
            (rollout_id, name, approved_by, gate, json.dumps(plan), time.time()),
        )
        await self._record(
            approved_by,
            "rollout_started",
            rollout_id,
            {"name": name, "instances": len(plan), "gate": gate},
        )
        await self._offer_step(rollout_id)
        return await self.rollout(rollout_id)

    async def _load(self, rollout_id: str) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM fleet_rollouts WHERE id = ?", (rollout_id,))
        if row is None:
            raise ControlError(f"no rollout {rollout_id}")
        return {**row, "steps": json.loads(row["steps"])}

    async def _save(self, r: dict[str, Any]) -> None:
        await self.db.execute(
            "UPDATE fleet_rollouts SET status = ?, steps = ?, position = ?, error = ?,"
            " finished_at = ? WHERE id = ?",
            (
                r["status"],
                json.dumps(r["steps"]),
                r["position"],
                r.get("error"),
                r.get("finished_at"),
                r["id"],
            ),
        )

    async def _offer_step(self, rollout_id: str) -> None:
        r = await self._load(rollout_id)
        step = r["steps"][r["position"]]
        offer = await self.offer_config(
            step["tenant"],
            step["instance"],
            step["data"],
            r["approved_by"],
            gate=r["gate"],
            rollout=rollout_id,
            note=f"rollout {r['name']}",
        )
        step["offer"], step["status"] = offer.id, "offered"
        step["previous_hash"] = offer.previous_hash
        await self._save(r)

    async def _advance(
        self, rollout_id: str, offer_id: str, status: str, detail: dict[str, Any]
    ) -> None:
        r = await self._load(rollout_id)
        if r["status"] != "running":
            return
        step = next((s for s in r["steps"] if s["offer"] == offer_id), None)
        if step is None:
            return  # a rollback offer of this rollout
        step["status"] = status
        if detail.get("running_before"):
            step["previous_hash"] = detail["running_before"]  # the rollback target
        if status == "rejected":
            r["error"] = f"{step['tenant']}/{step['instance']}: {detail.get('reason', 'rejected')}"
            await self._save(r)
            await self._roll_back(r, r["approved_by"], automatic=True)
            return
        if r["position"] + 1 < len(r["steps"]):
            r["position"] += 1
            await self._save(r)
            await self._offer_step(rollout_id)
        else:
            r["status"], r["finished_at"] = "completed", time.time()
            await self._save(r)
            await self._record("control-plane", "rollout_completed", rollout_id, {})

    async def rollback(self, rollout_id: str, approved_by: str) -> dict[str, Any]:
        r = await self._load(rollout_id)
        if r["status"] in ("rolled_back",):
            return await self.rollout(rollout_id)
        await self._roll_back(r, approved_by, automatic=False)
        return await self.rollout(rollout_id)

    async def _roll_back(self, r: dict[str, Any], approved_by: str, *, automatic: bool) -> None:
        for step in r["steps"]:
            if step["status"] == "offered" and step["offer"]:
                await self.db.execute(
                    "UPDATE fleet_offers SET status = 'superseded', decided_at = ? WHERE id = ?"
                    " AND status = 'offered'",
                    (time.time(), step["offer"]),
                )
                step["status"] = "cancelled"
            elif step["status"] == "applied" and step.get("previous_hash"):
                back = await self.offer_rollback(
                    step["tenant"],
                    step["instance"],
                    step["previous_hash"],
                    approved_by,
                    note=f"rollback of rollout {r['name']}",
                    rollout=r["id"],
                )
                step["status"], step["rollback_offer"] = "rolling_back", back.id
        r["status"], r["finished_at"] = "rolled_back", time.time()
        await self._save(r)
        await self._record(
            approved_by,
            "rollout_rolled_back",
            r["id"],
            {"automatic": automatic, "error": r.get("error")},
        )

    async def rollout(self, rollout_id: str) -> dict[str, Any]:
        r = await self._load(rollout_id)
        return {
            "id": r["id"],
            "name": r["name"],
            "approved_by": r["approved_by"],
            "status": r["status"],
            "gate": r["gate"],
            "position": r["position"],
            "error": r["error"],
            "steps": [{k: v for k, v in s.items() if k != "data"} for s in r["steps"]],
        }
