"""Config versions, boot selection and vault adapters (M2.4)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.runtime import scope_for
from dif_general_harness.service.config import boot_config
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.tenancy import (
    AwsSecretsManager,
    ConfigError,
    ConfigStore,
    EnvSecrets,
    FileSecrets,
    backend_from_env,
    config_hash,
)


def _clinic(examples: Path) -> Any:
    resolved = load_instance(
        examples / "instances" / "clinica-sonrisa.json", PackCatalog(roots=[examples])
    )
    assert resolved.ok
    return resolved


def _variant(data: dict[str, Any], name: str) -> dict[str, Any]:
    out = copy.deepcopy(data)
    out["values"]["business_name"] = name
    return out


async def test_config_lifecycle(db: Any, examples: Path) -> None:
    resolved = _clinic(examples)
    store = ConfigStore(db, scope_for(resolved.spec))
    v1 = await store.propose(resolved.data, "constructor", "first build")
    assert (v1.version, v1.status, v1.hash) == (1, "pending", config_hash(resolved.data))
    with pytest.raises(ConfigError, match="approve it first"):
        await store.activate(1)
    await store.approve(1, "jag")
    with pytest.raises(ConfigError, match="not pending"):
        await store.approve(1, "jag")
    assert (await store.activate(1)).status == "active"

    v2 = await store.propose(
        _variant(resolved.data, "Sonrisa Norte"), "control-plane:jag", approved=True
    )
    await store.activate(v2.version)
    active = await store.active()
    assert (
        active and active.version == 2 and active.data["values"]["business_name"] == "Sonrisa Norte"
    )
    assert [v.status for v in await store.history()] == ["retired", "active"]

    back = await store.rollback()
    assert back.version == 1 and [v.status for v in await store.history()] == ["active", "retired"]
    assert (await store.rollback()).version == 2  # rolling back twice returns to v2


async def test_config_refuses_bad_versions(db: Any, examples: Path) -> None:
    resolved = _clinic(examples)
    store = ConfigStore(db, scope_for(resolved.spec))
    broken = copy.deepcopy(resolved.data)
    broken["agents"]["receptionist"]["model_role"] = "nope"
    with pytest.raises(ConfigError, match="invalid spec"):
        await store.propose(broken, "someone")
    other = copy.deepcopy(resolved.data)
    other["tenant"]["id"] = "otra-clinica"
    with pytest.raises(ConfigError, match="another tenant"):
        await store.propose(other, "someone")
    v1 = await store.propose(resolved.data, "jag", approved=True)
    await db.execute(  # someone edits the stored spec behind the store's back
        "UPDATE config_versions SET data = ? WHERE version = ?",
        (json.dumps(_variant(resolved.data, "Hacked")), v1.version),
    )
    with pytest.raises(ConfigError, match="does not match its hash"):
        await store.activate(v1.version)
    with pytest.raises(ConfigError, match="no earlier version"):
        await store.rollback()


async def test_boot_selects_the_approved_version(db: Any, examples: Path) -> None:
    deployed = _clinic(examples)
    scope = scope_for(deployed.spec)
    store = ConfigStore(db, scope)
    running = await boot_config(db, scope, deployed)
    assert running.version_hash == deployed.version_hash
    assert [(v.version, v.status, v.created_by) for v in await store.history()] == [
        (1, "active", "deploy")
    ]
    await boot_config(db, scope, deployed)  # a restart registers nothing new
    assert len(await store.history()) == 1

    pushed = await store.propose(
        _variant(deployed.data, "Sonrisa Norte"), "control-plane:jag", approved=True
    )
    await store.activate(pushed.version)
    running = await boot_config(db, scope, deployed)  # the same image restarts
    assert running.data["values"]["business_name"] == "Sonrisa Norte"

    newer = copy.deepcopy(deployed)
    newer.data = _variant(deployed.data, "Sonrisa Sur")
    running = await boot_config(db, scope, newer)  # a new image with a new approved file
    assert running.data["values"]["business_name"] == "Sonrisa Sur"
    assert (await store.active()).version == 3  # type: ignore[union-attr]


# --- vault adapters ----------------------------------------------------------------


class ResourceNotFoundException(Exception):
    pass


class FakeSecretsManager:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.calls: list[str] = []

    def get_secret_value(self, SecretId: str) -> dict[str, str]:
        self.calls.append(SecretId)
        if SecretId not in self.values:
            raise ResourceNotFoundException(SecretId)
        return {"SecretString": self.values[SecretId]}


def test_aws_secrets_manager() -> None:
    client = FakeSecretsManager({"dif/clinica/citas/llm": "sk-live"})
    vault = AwsSecretsManager("dif/clinica/citas/", client=client, ttl_s=300)
    assert vault.get("llm") == "sk-live"
    assert vault.get("llm") == "sk-live"
    assert client.calls == ["dif/clinica/citas/llm"]  # cached
    assert vault.get("twilio") is None
    with pytest.raises(ValueError):
        vault.get("../other-tenant/llm")
    expired = AwsSecretsManager("dif/clinica/citas", client=client, ttl_s=0)
    expired.get("llm")
    expired.get("llm")
    assert client.calls.count("dif/clinica/citas/llm") == 3

    class Broken:
        def get_secret_value(self, SecretId: str) -> dict[str, str]:
            raise PermissionError("AccessDenied")

    with pytest.raises(PermissionError):  # access problems surface; only 'not found' is None
        AwsSecretsManager("p", client=Broken()).get("llm")


def test_backend_from_env(tmp_path: Path) -> None:
    assert isinstance(backend_from_env({}), EnvSecrets)
    file_backend = backend_from_env(
        {"DIF_SECRETS_BACKEND": "file", "DIF_SECRETS_DIR": str(tmp_path)}
    )
    assert isinstance(file_backend, FileSecrets)
    with pytest.raises(ValueError, match="DIF_SECRETS_PREFIX"):
        backend_from_env({"DIF_SECRETS_BACKEND": "aws-secrets-manager"})
    with pytest.raises(ValueError, match="unknown"):
        backend_from_env({"DIF_SECRETS_BACKEND": "vaultish"})
