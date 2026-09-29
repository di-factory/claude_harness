"""Build: answers + pack(s) -> an instance spec, validated, with a plain-language summary.

Outputs in the chosen folder:
- ``<instance-id>.json``: the instance spec (the client's 15-20%);
- ``<instance-id>.answers.yaml``: the answers, so an adjustment is "edit, rebuild";
- ``<instance-id>.summary.md``: what was built, the secrets the client must store in
  their own vault (names and purpose only, never values), what M1 cannot run yet, custom
  code that needs review, and the validation result. This is what Jag reads to approve.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..spec.errors import Issue
from ..spec.loader import PackCatalog, ResolvedSpec, load_instance
from ..spec.schema import SolutionSpec, Variable
from .interview import Ask, Question, interview, questions


@dataclass
class BuildResult:
    instance_id: str
    spec_path: Path
    answers_path: Path
    summary_path: Path
    resolved: ResolvedSpec | None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and self.resolved is not None and self.resolved.ok


def pack_questions(catalog: PackCatalog, pack_ids: list[str]) -> list[Question]:
    variables: dict[str, Variable] = {}
    for pid in pack_ids:
        data = catalog.find(pid).data
        for name, raw in data.get("variables", {}).items():
            variables.setdefault(name, Variable.model_validate(raw))
    return questions(variables)


def _extends(catalog: PackCatalog, pack_id: str) -> str:
    version = catalog.find(pack_id).data["solution"]["version"]
    major, minor, _ = version.split(".", 2)
    return f"{pack_id}@^{major}.{minor}"


def build(
    catalog: PackCatalog,
    pack_ids: list[str],
    out_dir: Path,
    *,
    answers: dict[str, Any] | None = None,
    ask: Ask | None = None,
) -> BuildResult:
    first = catalog.find(pack_ids[0]).data["solution"]
    qs = pack_questions(catalog, pack_ids)
    got, problems = interview(qs, answers, ask)

    tenant_id = str(got.get("tenant.id", "tenant"))
    instance_id = str(got.get("solution.id") or f"{tenant_id}-{first['id']}")[:63].rstrip("-")
    tenant_name = str(got.get("tenant.name", tenant_id))
    spec: dict[str, Any] = {
        "spec_version": "1",
        "kind": "instance",
        "solution": {
            "id": instance_id,
            "version": "1.0.0",
            "name": f"{tenant_name} - {first.get('name') or first['id']}",
            "lob": first["lob"],
            "locale": got.get("solution.locale", "en"),
        },
        "extends": [_extends(catalog, p) for p in pack_ids],
        "tenant": {
            "id": tenant_id,
            "name": tenant_name,
            "timezone": got.get("tenant.timezone", "UTC"),
        },
        "values": {k.removeprefix("values."): v for k, v in got.items() if k.startswith("values.")},
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    spec_path = out_dir / f"{instance_id}.json"
    answers_path = out_dir / f"{instance_id}.answers.yaml"
    summary_path = out_dir / f"{instance_id}.summary.md"
    spec_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    answers_path.write_text(
        yaml.safe_dump(_nested(got), allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    resolved: ResolvedSpec | None = None
    if not problems:
        resolved = load_instance(spec_path, catalog)
    summary_path.write_text(summary(spec, qs, got, problems, resolved), encoding="utf-8")
    return BuildResult(instance_id, spec_path, answers_path, summary_path, resolved, problems)


def _nested(flat: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in flat.items():
        section, _, name = key.partition(".")
        out.setdefault(section, {})[name] = value
    return out


def _fmt(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text.replace("|", "\\|")


def summary(
    spec: dict[str, Any],
    qs: list[Question],
    got: dict[str, Any],
    problems: list[str],
    resolved: ResolvedSpec | None,
) -> str:
    sol, tenant = spec["solution"], spec["tenant"]
    lines = [
        f"# {sol['name']}",
        "",
        f"- Instance: `{sol['id']}` for tenant `{tenant['id']}` ({tenant['timezone']})",
        f"- Packs: {', '.join(f'`{e}`' for e in spec['extends'])}",
        f"- Language: {sol['locale']}",
    ]
    if resolved is not None:
        lines.append(f"- Config version: `{resolved.version_hash}`")
    lines += ["", "## Answers", "", "| Question | Answer | By |", "|---|---|---|"]
    for q in qs:
        if q.key in got:
            lines.append(f"| {_fmt(q.text)} | {_fmt(got[q.key])} | {q.answered_by} |")

    if problems:
        lines += ["", "## Open answers", ""] + [f"- {p}" for p in problems]
    if resolved is not None:
        lines += _resolved_sections(resolved.spec, resolved.issues)
    return "\n".join(lines) + "\n"


def _resolved_sections(spec: SolutionSpec, issues: list[Issue]) -> list[str]:
    lines = ["", "## Secrets the client stores in their own vault", ""]
    lines += [f"- `{name}`: {decl.description}" for name, decl in spec.secrets.items()] or [
        "- none"
    ]
    lines += ["", "Di-Factory never sees these values; the deploy connects them by name."]

    custom = list(spec.tools.python) + list(spec.extensions)
    if custom:
        lines += ["", "## Custom code to review", ""] + [f"- `{c}`" for c in custom]

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity != "error"]
    lines += ["", "## Validation", ""]
    lines.append("- Passed." if not errors else f"- **{len(errors)} error(s)**; not deployable.")
    lines += [f"- {i.severity}: `{i.path}` {i.message}" for i in errors + warnings]
    lines += ["", "## Evals", ""]
    lines += [f"- `{s}`" for s in spec.evals.suites] or ["- none declared"]
    return lines
