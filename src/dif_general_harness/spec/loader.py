"""Load packs and instances, merge them, and produce a resolved spec.

Merge rules (SOLUTION_SPEC §4):
1. Objects deep-merge; the later layer wins per key.
2. Named maps merge by key; ``null`` removes an entry.
3. Arrays replace, except the additive ones listed in ``ADDITIVE``.
4. Safety is monotonic: tokenization can't be turned off, consent can't be dropped,
   retention can only shorten, budgets can only drop (unless ``override_reason``).
5. Every required variable must have a value.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .errors import Issue, SpecError
from .schema import TOP_LEVEL_KEYS, SolutionSpec

ADDITIVE: set[tuple[str, ...]] = {
    ("policies", "permissions", "deny"),
    ("governance", "pii", "classes"),
    ("governance", "consent", "channels"),
    ("governance", "consent", "opt_out_keywords"),
    ("governance", "compliance"),
}

_VAR_FULL = re.compile(r"^\{\{\s*var\.([A-Za-z0-9_]+)\s*\}\}$")
_VAR_ANY = re.compile(r"\{\{\s*var\.([A-Za-z0-9_]+)\s*\}\}")
_DURATION = re.compile(r"^(\d+)([mhdy])$")
_DAYS = {"m": 1 / 1440, "h": 1 / 24, "d": 1, "y": 365}


def duration_days(value: str) -> float:
    m = _DURATION.match(value)
    if not m:
        raise ValueError(f"invalid duration {value!r} (use e.g. 30m, 24h, 90d, 5y)")
    return int(m.group(1)) * _DAYS[m.group(2)]


# --- reading layers ----------------------------------------------------------------


@dataclass
class Layer:
    """One spec file, with file references made absolute relative to its own folder."""

    path: Path
    data: dict[str, Any]


def read_layer(path: Path) -> Layer:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SpecError([Issue("error", "json", str(path), f"invalid JSON: {exc}")]) from exc
    if not isinstance(data, dict):
        raise SpecError([Issue("error", "json", str(path), "a spec must be a JSON object")])
    unknown = set(data) - TOP_LEVEL_KEYS
    if unknown:
        raise SpecError(
            [Issue("error", "unknown_key", str(path), f"unknown top-level keys {sorted(unknown)}")]
        )
    _absolutize(data, path.parent)
    return Layer(path=path, data=data)


def _abs(base: Path, rel: Any) -> Any:
    if not isinstance(rel, str) or "{{" in rel or rel.startswith(("http://", "https://")):
        return rel
    return str((base / rel).resolve())


def _absolutize(data: dict[str, Any], base: Path) -> None:
    for agent in (data.get("agents") or {}).values():
        if isinstance(agent, dict):
            for key in ("prompt", "output_schema"):
                if key in agent:
                    agent[key] = _abs(base, agent[key])
    for channel in (data.get("channels") or {}).values():
        for tpl in ((channel or {}).get("templates") or {}).values():
            if isinstance(tpl, dict) and "file" in tpl:
                tpl["file"] = _abs(base, tpl["file"])
    for corpus in ((data.get("knowledge") or {}).get("corpora") or {}).values():
        sources = (corpus or {}).get("sources")
        if isinstance(sources, list):
            for src in sources:
                if isinstance(src, dict) and src.get("type") == "file" and "path" in src:
                    src["path"] = _abs(base, src["path"])
    evals = data.get("evals") or {}
    if isinstance(evals.get("suites"), list):
        evals["suites"] = [_abs(base, s) for s in evals["suites"]]


# --- pack catalog ------------------------------------------------------------------


def _version_tuple(v: str) -> tuple[int, int, int]:
    major, minor, patch = (int(x) for x in re.split(r"[-+]", v)[0].split("."))
    return major, minor, patch


def version_matches(version: str, rng: str | None) -> bool:
    """Supports exact ``1.2.3``, caret ``^1.2`` / ``^1.2.3`` and no range."""
    if not rng:
        return True
    have = _version_tuple(version)
    if rng.startswith("^"):
        parts = [int(x) for x in rng[1:].split(".")]
        want = [*parts, 0, 0][:3]
        return have[0] == want[0] and have >= (want[0], want[1], want[2])
    return have == _version_tuple(rng)


@dataclass
class PackCatalog:
    """Finds packs by id in one or more folders (each pack lives in <folder>/<pack>/pack.json)."""

    roots: list[Path]
    _index: dict[str, list[Layer]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for root in self.roots:
            for pack_file in sorted(root.glob("*/pack.json")):
                layer = read_layer(pack_file)
                sol = layer.data.get("solution") or {}
                if layer.data.get("kind") == "pack" and "id" in sol:
                    self._index.setdefault(sol["id"], []).append(layer)

    def latest(self) -> list[Layer]:
        """The newest version of every pack, sorted by id."""
        return [self.find(pack_id) for pack_id in sorted(self._index)]

    def find(self, ref: str) -> Layer:
        pack_id, _, rng = ref.partition("@")
        candidates = [
            layer
            for layer in self._index.get(pack_id, [])
            if version_matches(layer.data["solution"]["version"], rng or None)
        ]
        if not candidates:
            raise SpecError(
                [Issue("error", "pack_not_found", "extends", f"no pack matches {ref!r}")]
            )
        best = max(candidates, key=lambda la: _version_tuple(la.data["solution"]["version"]))
        return Layer(path=best.path, data=copy.deepcopy(best.data))


# --- merging -----------------------------------------------------------------------


def merge(
    base: dict[str, Any], over: dict[str, Any], issues: list[Issue], path: tuple[str, ...] = ()
) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in over.items():
        here = (*path, key)
        if value is None:
            if key in out and _protected(here):
                issues.append(
                    Issue(
                        "error",
                        "safety_weakened",
                        ".".join(here),
                        "safety settings cannot be removed with null by a later layer",
                    )
                )
                continue
            out.pop(key, None)
            continue
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = merge(current, value, issues, here)
        elif isinstance(current, list) and isinstance(value, list) and here in ADDITIVE:
            out[key] = current + [v for v in value if v not in current]
        else:
            _check_monotonic(here, current, value, over, issues)
            out[key] = copy.deepcopy(value)
    return out


_PROTECTED_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("governance",),
    ("policies", "permissions", "deny"),
    ("policies", "budgets"),
)


def _protected(here: tuple[str, ...]) -> bool:
    """Paths whose removal would weaken safety (governance, deny rules, budgets)."""
    return any(here[: len(p)] == p or p[: len(here)] == here for p in _PROTECTED_PREFIXES)


def _check_monotonic(
    here: tuple[str, ...], old: Any, new: Any, over_parent: dict[str, Any], issues: list[Issue]
) -> None:
    where = ".".join(here)
    if here == ("governance", "pii", "tokenize") and old is True and new is False:
        issues.append(
            Issue(
                "error",
                "safety_weakened",
                where,
                "PII tokenization cannot be turned off by a later layer",
            )
        )
    elif here == ("governance", "consent", "required") and old is True and new is False:
        issues.append(
            Issue(
                "error",
                "safety_weakened",
                where,
                "consent cannot be made optional by a later layer",
            )
        )
    elif len(here) == 3 and here[:2] == ("governance", "retention") and isinstance(old, str):
        try:
            if duration_days(str(new)) > duration_days(old):
                issues.append(
                    Issue(
                        "error",
                        "safety_weakened",
                        where,
                        f"retention can only shorten ({new} > {old})",
                    )
                )
        except ValueError as exc:
            issues.append(Issue("error", "invalid_duration", where, str(exc)))
    elif (
        here[:2] == ("policies", "budgets")
        and isinstance(old, (int, float))
        and isinstance(new, (int, float))
        and new > old
        and "override_reason" not in over_parent
    ):
        issues.append(
            Issue(
                "error",
                "safety_weakened",
                where,
                f"budget raised ({new} > {old}) without override_reason",
            )
        )


# --- variables ---------------------------------------------------------------------


def interpolate(value: Any, values: dict[str, Any]) -> Any:
    """Replace ``{{var.x}}`` with instance values; whole-string refs keep their type."""
    if isinstance(value, str):
        m = _VAR_FULL.match(value)
        if m and m.group(1) in values:
            return copy.deepcopy(values[m.group(1)])
        return _VAR_ANY.sub(lambda mm: str(values.get(mm.group(1), mm.group(0))), value)
    if isinstance(value, dict):
        return {k: interpolate(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, values) for v in value]
    return value


# --- public API --------------------------------------------------------------------


@dataclass
class ResolvedSpec:
    spec: SolutionSpec
    data: dict[str, Any]
    layers: list[Path]
    issues: list[Issue]

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def version_hash(self) -> str:
        blob = json.dumps(self.data, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


def _parse(data: dict[str, Any], where: str) -> tuple[SolutionSpec | None, list[Issue]]:
    try:
        return SolutionSpec.model_validate(data), []
    except ValidationError as exc:
        return None, [
            Issue("error", "schema", ".".join(str(p) for p in err["loc"]) or where, err["msg"])
            for err in exc.errors()
        ]


def load_pack(path: Path | str) -> ResolvedSpec:
    from .validate import validate  # local import: validate depends on this module's helpers

    p = Path(path)
    layer = read_layer(p / "pack.json" if p.is_dir() else p)
    if layer.data.get("kind") != "pack":
        raise SpecError([Issue("error", "kind", str(layer.path), "expected kind 'pack'")])
    spec, issues = _parse(layer.data, str(layer.path))
    if spec is None:
        raise SpecError(issues)
    issues += validate(spec, layer.data, is_instance=False)
    return ResolvedSpec(spec=spec, data=layer.data, layers=[layer.path], issues=issues)


def load_instance(path: Path | str, catalog: PackCatalog) -> ResolvedSpec:
    from .validate import validate

    inst = read_layer(Path(path))
    if inst.data.get("kind") != "instance":
        raise SpecError([Issue("error", "kind", str(inst.path), "expected kind 'instance'")])
    issues: list[Issue] = []
    packs = [catalog.find(ref) for ref in inst.data.get("extends", [])]
    if not packs:
        raise SpecError(
            [Issue("error", "extends", str(inst.path), "an instance must extend a pack")]
        )

    merged: dict[str, Any] = {}
    for layer in packs:
        merged = merge(merged, layer.data, issues)
    variables: dict[str, Any] = merged.get("variables", {})
    merged = merge(merged, inst.data, issues)
    merged["kind"] = "instance"

    values: dict[str, Any] = {k: v.get("default") for k, v in variables.items() if "default" in v}
    values.update(inst.data.get("values", {}))
    for name, decl in variables.items():
        if decl.get("required") and name not in inst.data.get("values", {}):
            issues.append(
                Issue(
                    "error",
                    "missing_value",
                    f"values.{name}",
                    f"required variable {name!r} has no value",
                )
            )
    for name in inst.data.get("values", {}):
        if name not in variables:
            issues.append(
                Issue(
                    "error",
                    "unknown_value",
                    f"values.{name}",
                    f"value for undeclared variable {name!r}",
                )
            )

    resolved = interpolate({k: v for k, v in merged.items() if k != "variables"}, values)
    resolved["variables"] = variables
    resolved["values"] = values
    spec, schema_issues = _parse(resolved, str(inst.path))
    if spec is None:
        raise SpecError(issues + schema_issues)
    issues += validate(spec, resolved, is_instance=True)
    return ResolvedSpec(
        spec=spec, data=resolved, layers=[la.path for la in packs] + [inst.path], issues=issues
    )
