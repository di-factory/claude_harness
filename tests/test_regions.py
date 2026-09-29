"""The provider-region policy (M4.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from dif_general_harness.providers import FakeProvider
from dif_general_harness.runtime import Instance, InstanceError, RuntimeOptions
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.spec.regions import allowed_model_regions, provider_region
from dif_general_harness.tenancy import EnvSecrets


def _clinic(examples: Path):  # type: ignore[no-untyped-def]
    return load_instance(
        examples / "instances" / "clinica-sonrisa.json", PackCatalog(roots=[examples])
    )


def test_policy_is_the_intersection_and_unknown_is_not_allowed(examples: Path) -> None:
    resolved = _clinic(examples)
    assert resolved.ok
    spec = resolved.spec
    assert allowed_model_regions(spec) == {"us", "mx"}
    assert spec.models is not None
    spec.models.allowed_regions = ["mx", "eu"]
    assert allowed_model_regions(spec) == {"mx"}
    assert provider_region("anthropic", {}) == "us"
    assert provider_region("anthropic", {"region": "eu"}) == "eu"
    assert provider_region("openai-compatible", {"base_url": "x"}) is None


async def test_the_runtime_refuses_to_route_outside_the_policy(
    examples: Path, tmp_path: Path
) -> None:
    resolved = _clinic(examples)
    resolved.spec.governance.regions["models"] = ["mx"]  # as if validation were bypassed
    options = RuntimeOptions(
        state_root=tmp_path, secrets=EnvSecrets({"DIF_SECRET_ANTHROPIC": "sk-test"})
    )
    with pytest.raises(InstanceError) as info:
        await Instance.open(resolved, options)
    assert {i.code for i in info.value.issues} == {"model_region_violation"}  # every role
    # a test provider replaces routing entirely, so nothing is sent anywhere
    fake = RuntimeOptions(state_root=tmp_path / "b", provider=FakeProvider([]))
    async with await Instance.open(_clinic(examples), fake):
        pass
