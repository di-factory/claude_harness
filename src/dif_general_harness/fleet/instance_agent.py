"""The instance agent (ARCHITECTURE §3.22): Di-Factory's view into a client deployment,
on the client's terms.

- **Outbound only.** It calls the control plane; nothing calls in. The client revokes it by
  removing ``DIF_FLEET_URL`` or the ``fleet_token`` secret, and the instance keeps running.
- **Aggregates only.** Heartbeats carry health, versions, job and inbox counts, spend
  totals and whether the audit chain is intact; never messages, contacts or PII.
- **Signed, approved config only.** A pulled version must be signed by the control plane's
  Ed25519 key and name who approved it (decision 37). It then goes through the normal
  versioned-config path (validation, activation, audit) like any other change.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import json
import logging
import tempfile
import time
from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import httpx2
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..constructor.evals import EvalReport, EvalStore, RecordingApprover, run_suites
from ..observability import quality
from ..runtime import Instance, RuntimeOptions
from ..service.config import apply_active
from ..service.headless import Headless
from ..spec.loader import ResolvedSpec, resolved_from_data
from ..tenancy.config_versions import ConfigError, ConfigStore, config_hash

log = logging.getLogger(__name__)
Evaluate = Callable[[ResolvedSpec], Awaitable[EvalReport]]


def signed_message(data: dict[str, Any], approved_by: str, **extra: Any) -> bytes:
    """The exact bytes the control plane signs for an offer: the data (a config version, or
    ``{"rollback_to": hash}``), who approved it and, when set, how it is gated."""
    body = {"approved_by": approved_by, "data": data, **extra}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load_public_key(raw: str) -> Ed25519PublicKey:
    """Base64 of the 32 raw key bytes."""
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(raw))


def _harness_version() -> str:
    try:
        return version("di-factory-general-harness")
    except PackageNotFoundError:
        return "unknown"


def evaluator(options: RuntimeOptions) -> Evaluate:
    """Evals for eval-gated offers: every case in a throwaway instance with this instance's
    secrets and models (never its database), as ``dif-general-harness eval`` does."""

    async def evaluate(resolved: ResolvedSpec) -> EvalReport:
        async def open_instance(state: Path) -> Instance:
            case = dataclasses.replace(
                options, state_root=state, database=None, database_url=None,
                approver=RecordingApprover(), telemetry=None,
            )  # fmt: skip
            return await Instance.open(resolved, case)

        suites = [Path(s) for s in resolved.spec.evals.suites]
        with tempfile.TemporaryDirectory(prefix="dif-gate-") as work:
            return await run_suites(
                open_instance, suites, resolved.spec.evals.thresholds, work=Path(work)
            )

    return evaluate


class InstanceAgent:
    def __init__(
        self,
        headless: Headless,
        *,
        control_url: str,
        token: str,
        public_key: Ed25519PublicKey,
        client: httpx2.AsyncClient | None = None,
        interval_s: float = 60.0,
        evaluate: Evaluate | None = None,
    ) -> None:
        if not control_url.startswith("https://") and "localhost" not in control_url:
            raise ValueError("the control plane must be reached over https")
        self.headless = headless
        self.base = control_url.rstrip("/")
        self.token = token
        self.public_key = public_key
        self.http = client or httpx2.AsyncClient(timeout=20.0)
        self.interval_s = interval_s
        self.evaluate = evaluate  # runs an offered config's evals (eval-gated offers)
        self.started = time.monotonic()
        self.last_pull: str = "never"

    @property
    def _path(self) -> str:
        scope = self.headless.instance.scope
        return f"{self.base}/v1/instances/{scope.tenant_id}/{scope.instance_id}"

    @property
    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}"}

    async def report(self) -> dict[str, Any]:
        inst = self.headless.instance
        scope = inst.scope
        active = await ConfigStore(inst.db, scope).active()
        inbox: dict[str, int] = {}
        for item in await inst.inbox.list("open"):
            inbox[item.kind] = inbox.get(item.kind, 0) + 1
        return {
            "tenant": scope.tenant_id,
            "instance": scope.instance_id,
            "harness_version": _harness_version(),
            "running_config": inst.resolved.version_hash,
            "active_version": active.version if active else None,
            "uptime_s": round(time.monotonic() - self.started),
            "jobs": await self.headless.queue.counts(scope),
            "inbox_open": inbox,
            "spend_today": await inst.spend.day(scope),
            "audit_intact": await inst.audit.verify(scope) is None,
            "issues": sorted({i.code for i in [*inst.issues, *self.headless.issues]}),
            "last_pull": self.last_pull,
            "metrics": await quality(inst.db, scope, "1d"),
            "evals": next(iter(await EvalStore(inst.db, scope).history(1)), None),
        }

    async def heartbeat(self) -> dict[str, Any]:
        body = await self.report()
        response = await self.http.post(f"{self._path}/heartbeat", json=body, headers=self._headers)
        if response.status_code >= 400:
            raise RuntimeError(f"heartbeat refused: HTTP {response.status_code}")
        return body

    async def pull(self) -> str:
        """Fetch and apply an approved config version. Returns what happened."""
        inst = self.headless.instance
        running = inst.resolved.version_hash
        response = await self.http.get(
            f"{self._path}/config", params={"running": running}, headers=self._headers
        )
        if response.status_code == 204:
            self.last_pull = "current"
            return self.last_pull
        if response.status_code >= 400:
            raise RuntimeError(f"config pull refused: HTTP {response.status_code}")
        self.last_pull = await self._apply(response.json())
        return self.last_pull

    async def _apply(self, offer: dict[str, Any]) -> str:
        running_before = self.headless.instance.resolved.version_hash
        outcome, detail = await self._decide(offer)
        if offer.get("offer"):  # tell the control plane (it gates rollouts on this)
            status = "applied" if outcome.startswith("applied") else "rejected"
            with contextlib.suppress(Exception):
                await self.http.post(
                    f"{self._path}/results",
                    json={
                        "offer": offer["offer"],
                        "status": status,
                        "reason": outcome,
                        "evals": detail,
                        "running_before": running_before,
                    },
                    headers=self._headers,
                )
        return outcome

    async def _decide(self, offer: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        inst = self.headless.instance
        approved_by = str(offer.get("approved_by") or "")
        gate = str(offer.get("gate") or "none")
        rollback_to = offer.get("rollback_to")
        data = {"rollback_to": rollback_to} if rollback_to else offer.get("data")
        if not isinstance(data, dict) or not approved_by:
            return "rejected: an offer needs data and approved_by", None
        try:
            signature = base64.b64decode(str(offer.get("signature", "")))
            extra = {"gate": gate} if gate != "none" else {}
            self.public_key.verify(signature, signed_message(data, approved_by, **extra))
        except (InvalidSignature, ValueError):
            await inst.audit.record(inst.scope, "fleet", "config_rejected", "signature", {})
            return "rejected: bad signature", None
        store = ConfigStore(inst.db, inst.scope)
        history = await store.history()
        if rollback_to:
            target = next((v for v in reversed(history) if v.hash == rollback_to), None)
            if target is None:
                return f"rejected: this instance never ran {str(rollback_to)[:12]}", None
            await store.activate(target.version)
            await inst.audit.record(
                inst.scope,
                f"control-plane:{approved_by}",
                "config_rolled_back",
                f"config/v{target.version}",
                {},
            )
            await apply_active(self.headless)
            return f"applied rollback to v{target.version}", None
        digest = config_hash(data)
        known = next((v for v in history if v.hash == digest), None)
        evals: dict[str, Any] | None = None
        try:
            if known is None:
                known = await store.propose(
                    data,
                    f"control-plane:{approved_by}",
                    "pulled by the instance agent",
                    approved=True,
                )
            if gate == "evals":
                passed, evals = await self._gate(data)
                if not passed:
                    await inst.audit.record(
                        inst.scope, "fleet", "config_rejected", "evals", {"evals": evals}
                    )
                    return f"rejected: evals did not pass ({evals.get('summary')})", evals
            await store.activate(known.version)
        except ConfigError as exc:
            await inst.audit.record(
                inst.scope, "fleet", "config_rejected", "validation", {"error": str(exc)}
            )
            return f"rejected: {exc}", evals
        await inst.audit.record(
            inst.scope,
            f"control-plane:{approved_by}",
            "config_activated",
            f"config/v{known.version}",
            {"evals": evals} if evals else {},
        )
        await apply_active(self.headless)
        return f"applied v{known.version}", evals

    async def _gate(self, data: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        """Run the offered config's eval suites here, with this instance's models and keys,
        before it may activate. A gate that runs no case does not pass."""
        if self.evaluate is None:
            return False, {"summary": "evals are not available on this instance"}
        resolved = resolved_from_data(data, "offer")
        report = await self.evaluate(resolved)
        inst = self.headless.instance
        await EvalStore(inst.db, inst.scope).record(report, resolved.version_hash)
        ran = len(report.ran)
        rate = report.pass_rate
        summary = (
            f"pass rate {'n/a' if rate is None else f'{rate:.0%}'} over {ran} case(s),"
            f" unsafe actions {report.unsafe_actions}"
        )
        failed = [f"{r.suite}/{r.case}" for r in report.results if r.status == "failed"]
        detail = {
            "summary": summary,
            "pass_rate": rate,
            "ran": ran,
            "unsafe_actions": report.unsafe_actions,
            "failed": failed,
        }
        return report.ok and ran > 0, detail

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            for step in (self.pull, self.heartbeat):
                try:
                    await step()
                except Exception as exc:  # the control plane being down never affects clients
                    log.warning("instance agent %s failed: %s", step.__name__, exc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval_s)
