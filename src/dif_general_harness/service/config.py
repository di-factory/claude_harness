"""Which config version runs, and switching versions while running.

``boot_config``: the deployed file is registered as an approved version the first time it is
seen (a deploy is approved by definition: decision 37) and activated; a file already in the
history leaves the active version alone, since the control plane may have pushed a newer
approved one. ``watch_config`` polls the active version so every container of an instance
converges on it after an activation or rollback.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ..core.scope import Scope
from ..spec.loader import ResolvedSpec, resolved_from_data
from ..store.db import Database
from ..tenancy.config_versions import ConfigStore, config_hash
from .headless import Headless

log = logging.getLogger(__name__)


async def boot_config(
    db: Database, scope: Scope, deployed: ResolvedSpec, by: str = "deploy"
) -> ResolvedSpec:
    store = ConfigStore(db, scope)
    digest = config_hash(deployed.data)
    history = await store.history()
    if not any(v.hash == digest for v in history):
        version = await store.propose(deployed.data, by, "deployed file", approved=True)
        await store.activate(version.version)
        return deployed
    active = await store.active()
    if active is None:
        match = max(v.version for v in history if v.hash == digest)
        await store.activate(match)
        return deployed
    return resolved_from_data(active.data, f"config v{active.version}")


async def apply_active(headless: Headless) -> bool:
    """Reload if the active version differs from what runs. Returns True on a reload."""
    inst = headless.instance
    active = await ConfigStore(inst.db, inst.scope).active()
    if active is None or active.hash == config_hash(inst.resolved.data):
        return False
    await headless.reload(resolved_from_data(active.data, f"config v{active.version}"))
    await headless.instance.audit.record(
        inst.scope, "system", "config_applied", f"config/v{active.version}", {"hash": active.hash}
    )
    return True


async def watch_config(headless: Headless, stop: asyncio.Event, every_s: float = 15.0) -> None:
    while not stop.is_set():
        try:
            await apply_active(headless)
        except Exception:  # a bad version never takes the running one down
            log.exception("config reload failed; keeping the running version")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_s)
