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
from .interview import Ask, Question, business_questions, interview, questions

CARRIED = ("models", "secrets", "channels")


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
    corpora: dict[str, Any] = {}
    for pid in pack_ids:
        data = catalog.find(pid).data
        for name, raw in data.get("variables", {}).items():
            variables.setdefault(name, Variable.model_validate(raw))
        for name, cfg in ((data.get("knowledge") or {}).get("corpora") or {}).items():
            corpora.setdefault(name, cfg)
    return questions(variables) + business_questions(corpora)


def business_faq(qs: list[Question], got: dict[str, Any], tenant_name: str) -> dict[str, str]:
    """The client's FAQ per corpus (Markdown), from the business answers."""
    out: dict[str, list[str]] = {}
    for q in qs:
        if not q.key.startswith("knowledge.") or not got.get(q.key):
            continue
        corpus = q.key.split(".")[1]
        lines = out.setdefault(corpus, [f"# {tenant_name}", ""])
        lines += [f"## {q.heading or q.text}", str(got[q.key]).strip(), ""]
    return {corpus: "\n".join(lines) for corpus, lines in out.items()}


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
    answers = dict(answers or {})
    branding = answers.pop("branding", None)  # the client's look: not a pack question
    # Di-Factory's per-client choices that are not questions either, e.g. running a pack's
    # models on another provider: copied into the instance and kept for rebuilds
    carried = {k: answers.pop(k) for k in CARRIED if isinstance(answers.get(k), dict)}
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
    if isinstance(branding, dict) and branding:
        spec["branding"] = branding  # the client's look (logo, colors): kept across rebuilds
    spec.update(carried)

    out_dir.mkdir(parents=True, exist_ok=True)
    faqs = business_faq(qs, got, tenant_name)
    if faqs:
        corpora: dict[str, Any] = {}
        for corpus, text in faqs.items():
            rel = Path(f"{instance_id}.knowledge") / f"{corpus}.md"
            (out_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (out_dir / rel).write_text(text, encoding="utf-8")
            corpora[corpus] = {"sources": [{"type": "file", "path": rel.as_posix()}]}
        spec["knowledge"] = {"corpora": corpora}  # the client's own FAQ replaces the example
    spec_path = out_dir / f"{instance_id}.json"
    answers_path = out_dir / f"{instance_id}.answers.yaml"
    summary_path = out_dir / f"{instance_id}.summary.md"
    spec_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    saved = _nested(got)
    for key in ("branding", *CARRIED):
        if key in spec:
            saved[key] = spec[key]
    answers_path.write_text(
        yaml.safe_dump(saved, allow_unicode=True, sort_keys=False), encoding="utf-8"
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
        if section == "knowledge":
            corpus, _, item = name.partition(".")
            out.setdefault(section, {}).setdefault(corpus, {})[item] = value
        else:
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


AUDIENCES = {"client": {"client", "either"}, "difactory": {"difactory", "either"}}


def questionnaire(catalog: PackCatalog, pack_ids: list[str], audience: str = "all") -> str:
    """A fill-in answers file (YAML with the questions as comments) for ``build --answers``.

    ``audience`` picks the questions: ``client`` (the business owner's: their business,
    hours, numbers, FAQ), ``difactory`` (models, provider template ids) or ``all``. Both
    halves can be filled separately and merged: keys never collide.
    """
    qs = pack_questions(catalog, pack_ids)
    wanted = AUDIENCES.get(audience)
    first = catalog.find(pack_ids[0]).data["solution"]
    lines = [
        f"# Questionnaire: {first.get('name') or first['id']} ({', '.join(pack_ids)})",
        "# Write each answer after its colon. Empty means: use the default shown, or ask later.",
        "# For several lines write `key: |` and the text indented on the next lines.",
        "# Who answers: [client] the business owner, [difactory] Di-Factory, [either] both.",
        f"# Then: dif-general-harness build --pack {pack_ids[0]} --answers THIS_FILE"
        " --out clients/<name>",
        "",
    ]
    section = corpus = None
    for q in qs:
        if wanted is not None and q.answered_by not in wanted:
            continue
        head, _, rest = q.key.partition(".")
        if head != section:
            section, corpus = head, None
            lines += ["", f"{head}:"]
        indent, name = "  ", rest
        if head == "knowledge":
            corpus_name, _, name = rest.partition(".")
            if corpus_name != corpus:
                corpus = corpus_name
                lines.append(f"  {corpus_name}:")
            indent = "    "
        note = f"# [{q.answered_by}] {q.text}" + ("  (required)" if q.required else "")
        lines.append(f"{indent}{note}")
        if q.example:
            lines.append(f"{indent}#   e.g. {q.example}")
        if q.options:
            lines.append(f"{indent}#   one of: {', '.join(map(str, q.options))}")
        default = "" if q.default is None else " " + json.dumps(q.default, ensure_ascii=False)
        lines.append(f"{indent}{name}:{default}")
    return "\n".join(lines) + "\n"
