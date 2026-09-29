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
import json
import logging
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx2
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..service.config import apply_active
from ..service.headless import Headless
from ..tenancy.config_versions import ConfigError, ConfigStore, config_hash

log = logging.getLogger(__name__)


def signed_message(data: dict[str, Any], approved_by: str) -> bytes:
    """The exact bytes the control plane signs for a config version."""
    body = {"approved_by": approved_by, "data": data}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load_public_key(raw: str) -> Ed25519PublicKey:
    """Base64 of the 32 raw key bytes."""
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(raw))


def _harness_version() -> str:
    try:
        return version("di-factory-general-harness")
    except PackageNotFoundError:
        return "unknown"


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
    ) -> None:
        if not control_url.startswith("https://") and "localhost" not in control_url:
            raise ValueError("the control plane must be reached over https")
        self.headless = headless
        self.base = control_url.rstrip("/")
        self.token = token
        self.public_key = public_key
        self.http = client or httpx2.AsyncClient(timeout=20.0)
        self.interval_s = interval_s
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
        inst = self.headless.instance
        data, approved_by = offer.get("data"), str(offer.get("approved_by") or "")
        if not isinstance(data, dict) or not approved_by:
            return "rejected: an offer needs data and approved_by"
        try:
            signature = base64.b64decode(str(offer.get("signature", "")))
            self.public_key.verify(signature, signed_message(data, approved_by))
        except (InvalidSignature, ValueError):
            await inst.audit.record(inst.scope, "fleet", "config_rejected", "signature", {})
            return "rejected: bad signature"
        store = ConfigStore(inst.db, inst.scope)
        digest = config_hash(data)
        known = next((v for v in await store.history() if v.hash == digest), None)
        try:
            if known is None:
                known = await store.propose(
                    data,
                    f"control-plane:{approved_by}",
                    "pulled by the instance agent",
                    approved=True,
                )
            await store.activate(known.version)
        except ConfigError as exc:
            await inst.audit.record(
                inst.scope, "fleet", "config_rejected", "validation", {"error": str(exc)}
            )
            return f"rejected: {exc}"
        await inst.audit.record(
            inst.scope,
            f"control-plane:{approved_by}",
            "config_activated",
            f"config/v{known.version}",
            {},
        )
        await apply_active(self.headless)
        return f"applied v{known.version}"

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            for step in (self.pull, self.heartbeat):
                try:
                    await step()
                except Exception as exc:  # the control plane being down never affects clients
                    log.warning("instance agent %s failed: %s", step.__name__, exc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval_s)
