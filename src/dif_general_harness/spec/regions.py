"""The provider-region policy (ARCHITECTURE §3.18): where model calls may be processed.

A tenant allows model regions with ``governance.regions.models`` (and/or
``models.allowed_regions``; both apply). Every provider a role uses must then have a known
region in that set: ``models.providers.<name>.region``, or the default for a first-party
API below. A provider with no known region is refused while a policy is set: unknown is not
allowed. Later spec layers can only narrow the allowed regions (``spec/loader.py``).
"""

from __future__ import annotations

from typing import Any

from .schema import SolutionSpec

# Where each first-party API processes requests when the spec does not say otherwise.
DEFAULT_PROVIDER_REGIONS = {"anthropic": "us"}


def allowed_model_regions(spec: SolutionSpec) -> set[str] | None:
    """The regions model calls may go to, or None when the spec sets no policy."""
    sets = []
    governed = spec.governance.regions.get("models")
    if isinstance(governed, list):
        sets.append({str(r) for r in governed})
    if spec.models and spec.models.allowed_regions is not None:
        sets.append(set(spec.models.allowed_regions))
    if not sets:
        return None
    allowed = sets[0]
    for other in sets[1:]:
        allowed &= other
    return allowed


def provider_region(name: str, settings: dict[str, Any] | None) -> str | None:
    region = (settings or {}).get("region")
    if isinstance(region, str) and region and "{{" not in region:
        return region
    return DEFAULT_PROVIDER_REGIONS.get(name)


def region_violations(spec: SolutionSpec) -> list[tuple[str, str, str]]:
    """(code, path, message) for every role routed outside the allowed regions."""
    allowed = allowed_model_regions(spec)
    if allowed is None or spec.models is None:
        return []
    out = []
    for role, config in spec.models.roles.items():
        region = provider_region(config.provider, spec.models.providers.get(config.provider))
        if region is None:
            out.append((
                "model_region_unknown", f"models.providers.{config.provider}",
                f"provider {config.provider!r} (role {role}) has no known region; set 'region'"
                f" (allowed: {sorted(allowed)})",
            ))  # fmt: skip
        elif region not in allowed:
            out.append((
                "model_region_violation", f"models.roles.{role}",
                f"role {role} uses {config.provider} in {region!r}; allowed: {sorted(allowed)}",
            ))  # fmt: skip
    return out
