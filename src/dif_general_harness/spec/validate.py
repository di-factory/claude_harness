"""Cross-reference and safety validation (SOLUTION_SPEC §6).

Runs on a schema-valid spec. Returns issues instead of raising, so a builder
(or the constructor agent) sees every problem at once.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..core import cel
from ..policy.permissions import Rule
from ..tools import python as python_tools
from ..triggers.cron import Cron, CronError
from .errors import Issue
from .regions import region_violations
from .schema import SolutionSpec, Step
from .skills import discover as discover_skills

BUILTIN_NAMESPACES = {"ledger", "runs", "knowledge", "memory"}
# Namespaces each built-in tool pack provides (tools/packs/); "web" arrives with a search provider.
PACK_NAMESPACES: dict[str, set[str]] = {
    "general": {"http", "web", "notes"},
    "coding": {"coding"},
    "documents": {"documents"},
    "connectors/google-calendar": {"calendar"},
}
EMBEDDING_PROVIDERS = {"openai", "openai-compatible"}
OPERATOR_PURPOSES = {"hitl", "founder", "outbound"}
AWS_REGION_PREFIX = {"mx": "mx-", "us": "us-", "eu": "eu-"}

_VAR_TEMPLATE = re.compile(r"\{\{\s*var\.([A-Za-z0-9_]+)\s*\}\}")
_VAR_CEL = re.compile(r"\bvar\.([A-Za-z0-9_]+)")

_MOVED = (
    " (paths are relative to the spec file that names them; to start a client from an"
    " example, use `dif-general-harness spec copy`, which copies the files too)"
)
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_LOGO = re.compile(r"data:image/(png|jpeg|webp|gif|svg\+xml);base64,[A-Za-z0-9+/=]+$")
MAX_LOGO_CHARS = 400_000
_DIALABLE = re.compile(r"^(\+[1-9]\d{6,14}|sip:[^\s@]+@[^\s]+)$")
_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _secrets_used(obj: Any) -> Iterator[str]:
    if isinstance(obj, dict):
        if set(obj) == {"$secret"} and isinstance(obj["$secret"], str):
            yield obj["$secret"]
        for v in obj.values():
            yield from _secrets_used(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _secrets_used(v)


def _rule_base(rule: str) -> str:
    return rule.split("(", 1)[0]


def _covered(rules: list[str], tool: str, *, unconditional: bool = False) -> bool:
    """Does any rule match the tool? With ``unconditional``, rules scoped to argument
    patterns (``tool(arg=...)``) don't count: they guard only some calls."""
    bases = [_rule_base(r) for r in rules if not (unconditional and "(" in r)]
    if tool.endswith(".*"):
        prefix = tool[:-1]
        return any(b == tool or b.startswith(prefix) for b in bases)
    return any(fnmatch.fnmatch(tool, b) for b in bases)


_CONDITION_KEYS = {"when", "unless", "expr"}


def _conditions(obj: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Every condition string in the spec, with where it sits."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}.{key}" if path else str(key)
            if key in _CONDITION_KEYS and isinstance(value, str):
                yield here, value
            else:
                yield from _conditions(value, here)
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from _conditions(value, f"{path}[{i}]")


def validate(spec: SolutionSpec, data: dict[str, Any], *, is_instance: bool) -> list[Issue]:
    issues: list[Issue] = []

    def err(code: str, path: str, msg: str) -> None:
        issues.append(Issue("error", code, path, msg))

    def warn(code: str, path: str, msg: str) -> None:
        issues.append(Issue("warning", code, path, msg))

    agents, channels, triggers = spec.agents, spec.channels, spec.triggers
    workflows = spec.workflows
    corpora = spec.knowledge.corpora
    checks = spec.policies.verification.checks
    roles = set(spec.models.roles) if spec.models else set()

    # --- files -----------------------------------------------------------------
    texts: list[str] = list(
        _strings({k: v for k, v in data.items() if k not in ("variables", "values")})
    )
    for name, agent in agents.items():
        for key in ("prompt", "output_schema"):
            ref = getattr(agent, key)
            if ref is None:
                continue
            f = Path(ref)
            if not f.exists():
                err("missing_file", f"agents.{name}.{key}", f"file not found: {ref}{_MOVED}")
            elif key == "prompt":
                texts.append(f.read_text(encoding="utf-8"))
    for cname, ch in channels.items():
        for tname, tpl in ch.templates.items():
            f = Path(tpl.file)
            if not f.exists():
                err(
                    "missing_file",
                    f"channels.{cname}.templates.{tname}",
                    f"file not found: {tpl.file}{_MOVED}",
                )
            else:
                texts.append(f.read_text(encoding="utf-8"))
    for i, ref in enumerate(spec.tools.python):
        parts = python_tools.split(ref)
        if parts is None:
            err("invalid_python_ref", f"tools.python[{i}]", f"not module.path:function: {ref!r}")
        elif not parts[0].is_file():
            err("missing_file", f"tools.python[{i}]", f"file not found: {parts[0]}")
    for hname, hook in spec.hooks.items():
        local = hook.url.startswith(("http://localhost", "http://127.0.0.1"))
        if not (hook.url.startswith("https://") or local or "{{" in hook.url):
            err("invalid_hook", f"hooks.{hname}.url", "a hook must be an https:// URL")
        if not (isinstance(hook.secret, dict) and set(hook.secret) == {"$secret"}):
            err("invalid_hook", f"hooks.{hname}.secret",
                'sign with a declared secret: {"$secret": "<name>"}')  # fmt: skip
    for code, spot, trouble in discover_skills(spec.skills)[1]:
        err(code, spot, trouble)
    for i, suite in enumerate(spec.evals.suites):
        if not Path(suite).exists():
            warn("eval_missing", f"evals.suites[{i}]", f"eval suite not written yet: {suite}")
    if spec.kind == "pack" and not spec.evals.suites:
        err("no_evals", "evals.suites", "every pack must declare at least one eval suite")

    # --- variables -------------------------------------------------------------
    joined = "\n".join(texts)
    template_refs = set(_VAR_TEMPLATE.findall(joined))
    for v in sorted(template_refs - set(spec.variables)):
        err("undeclared_variable", f"{{{{var.{v}}}}}", f"variable {v!r} is used but not declared")
    if not is_instance:
        used = template_refs | set(_VAR_CEL.findall(joined))
        for v in sorted(set(spec.variables) - used):
            warn("unused_variable", f"variables.{v}", f"variable {v!r} is declared but never used")

    # --- conditions (CEL subset) -------------------------------------------------
    for where, text in _conditions(data):
        if "{{" in text:
            continue  # still a template in a pack; checked again once the instance fills it
        problem = cel.check(text)
        if problem:
            err("invalid_condition", where, f"{problem}: {text!r}")

    # --- secrets ---------------------------------------------------------------
    used_secrets = set(_secrets_used(data))
    for s in sorted(used_secrets - set(spec.secrets)):
        err("undeclared_secret", "secrets", f"secret {s!r} is used but not declared")
    for s in sorted(set(spec.secrets) - used_secrets):
        warn("unused_secret", f"secrets.{s}", f"secret {s!r} is declared but never used")

    # --- tool namespaces -------------------------------------------------------
    namespaces = set(BUILTIN_NAMESPACES) | set(spec.tools.mcp) | set(spec.tools.http)
    for pack in spec.tools.packs:
        namespaces |= PACK_NAMESPACES.get(pack, {pack.rsplit("/", 1)[-1]})
    namespaces |= {ns for r in spec.tools.python if (ns := python_tools.namespace(r))}

    # --- agents ----------------------------------------------------------------
    for name, agent in agents.items():
        where = f"agents.{name}"
        if agent.model_role not in roles:
            err(
                "unknown_model_role",
                where,
                f"model role {agent.model_role!r} is not defined in models.roles",
            )
        for ref in agent.handoffs + agent.subagents:
            if ref != "human" and ref not in agents:
                err("unknown_agent", where, f"references unknown agent {ref!r}")
        for corpus in agent.knowledge:
            if corpus not in corpora:
                err("unknown_corpus", where, f"references unknown corpus {corpus!r}")
        for t in agent.tools:
            if t.split(".", 1)[0] not in namespaces:
                err("unknown_tool_namespace", where, f"tool {t!r} has no provider namespace")
            if (
                t.startswith("knowledge.search_")
                and t.removeprefix("knowledge.search_") not in corpora
            ):
                err("unknown_corpus", where, f"tool {t!r} has no matching corpus")
        if agent.workspace and agent.workspace not in spec.workspaces:
            err("unknown_workspace", where, f"unknown workspace {agent.workspace!r}")

    verifier = spec.policies.verification.verifier or {}
    if verifier.get("model_role") and verifier["model_role"] not in roles:
        err(
            "unknown_model_role",
            "policies.verification.verifier",
            "verifier model role not defined",
        )
    router = spec.policies.router or {}
    if router.get("model_role") and router["model_role"] not in roles:
        err("unknown_model_role", "policies.router", "router model role not defined")
    if spec.kind == "instance" and spec.models:
        for rname, role in spec.models.roles.items():
            if not role.model or role.model.startswith(("<", "{{")):
                err("model_not_set", f"models.roles.{rname}",
                    f"no model chosen ({role.model!r}); set the pack's model variable in this"
                    " instance's values, e.g. \"main_model\": \"claude-sonnet-5-5\"")  # fmt: skip
    embedding = spec.models.roles.get("embedding") if spec.models else None
    if embedding is not None and embedding.provider not in EMBEDDING_PROVIDERS:
        err("invalid_embedding_provider", "models.roles.embedding",
            f"{embedding.provider} has no embeddings API here; use an OpenAI-compatible"
            " endpoint (OpenAI, a self-hosted model, a gateway)")  # fmt: skip

    # --- branding ----------------------------------------------------------------------
    brand = spec.branding
    if brand is not None:
        for slot in ("primary", "accent"):
            value = getattr(brand.colors, slot, None) if brand.colors else None
            if value is not None and not _HEX_COLOR.fullmatch(value):
                err("invalid_branding", f"branding.colors.{slot}",
                    f"{value!r} is not a color like #1f5f4a")  # fmt: skip
        logo = brand.logo
        if logo is not None and (not _LOGO.match(logo) or len(logo) > MAX_LOGO_CHARS):
            err("invalid_branding", "branding.logo",
                "a logo is an image as a base64 data: URI, under 400 kB")  # fmt: skip

    # --- channels, triggers, workflows ------------------------------------------
    for cname, ch in channels.items():
        if ch.entry_agent and ch.entry_agent not in agents:
            err("unknown_agent", f"channels.{cname}", f"entry agent {ch.entry_agent!r} not defined")
        if ch.public and ch.type != "web":
            err("invalid_public", f"channels.{cname}.public",
                "only a web channel can be public (the others verify their provider)")  # fmt: skip
        if ch.voice is not None and ch.type != "voice":
            err("invalid_voice", f"channels.{cname}.voice", "voice settings need a voice channel")
        transfer = (ch.voice or {}).get("transfer_to")
        if isinstance(transfer, str) and "{{" not in transfer and not _DIALABLE.match(transfer):
            err("invalid_voice", f"channels.{cname}.voice.transfer_to",
                "transfer_to is a phone number (+525512345678) or a sip: address")  # fmt: skip
    for cname, corpus_cfg in corpora.items():
        where = f"knowledge.corpora.{cname}"
        cron = (corpus_cfg.get("sync") or {}).get("schedule")
        if isinstance(cron, str) and "{{" not in cron:
            try:
                Cron.parse(cron)
            except CronError as exc:
                err("invalid_sync_schedule", f"{where}.sync.schedule", str(exc))
        items = corpus_cfg.get("questionnaire")
        if items is not None:
            ids = [i.get("id") for i in items] if isinstance(items, list) else []
            bad = not isinstance(items, list) or any(
                not isinstance(i, dict) or not i.get("question")
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", str(i.get("id") or ""))
                for i in items
            )  # fmt: skip
            if bad or len(ids) != len(set(ids)):
                err("invalid_questionnaire", f"{where}.questionnaire",
                    "a list of {id (lowercase), question, heading?, required?, example?}"
                    " with unique ids")  # fmt: skip
        whole = (corpus_cfg.get("retrieval") or {}).get("read_whole_below")
        if whole is not None and (
            isinstance(whole, bool) or not isinstance(whole, int) or not 0 <= whole <= 200_000
        ):
            err("invalid_read_whole", f"{where}.retrieval.read_whole_below",
                "read_whole_below is a size in characters, from 0 (off) to 200000")  # fmt: skip
        not_found = (corpus_cfg.get("retrieval") or {}).get("not_found")
        if not_found is not None and not_found not in ("say_so", "handoff"):
            err("invalid_not_found", f"{where}.retrieval", "not_found must be say_so or handoff")

    for tname, trig in triggers.items():
        where = f"triggers.{tname}"
        if trig.agent is not None:
            if trig.agent not in agents and not trig.agent.startswith("{{event."):
                err("unknown_agent", where, f"targets unknown agent {trig.agent!r}")
        elif trig.workflow is None:
            err("no_target", where, "a trigger needs a workflow or an agent")
        elif trig.workflow not in workflows:
            err("unknown_workflow", where, f"targets unknown workflow {trig.workflow!r}")
        if trig.started_by and trig.started_by not in workflows:
            err("unknown_workflow", where, f"started_by unknown workflow {trig.started_by!r}")
        if trig.type == "file" and not trig.dedupe_key:
            err("missing_dedupe", where, "file triggers need a dedupe_key")
        if trig.dedupe_key and not _DOTTED.match(trig.dedupe_key):
            err("invalid_dedupe_key", where, "dedupe_key is a field path such as file.sha256")
        if trig.type == "file" and (why := _file_source_issue(trig.source)):
            err("invalid_source", where, why)
        if trig.type == "batch":
            tool = trig.source if isinstance(trig.source, str) else None
            if isinstance(trig.source, dict):
                tool = trig.source.get("tool")
            if trig.source is not None and not isinstance(tool, str):
                err("invalid_source", where, "a batch source names a read tool")
            elif tool and tool.split(".", 1)[0] not in namespaces:
                err("unknown_tool_namespace", where, f"tool {tool!r} has no provider namespace")
            if trig.cron and trig.source is None:
                err("missing_source", where, "a scheduled batch needs a source tool")
        if trig.type == "schedule" and not trig.cron:
            err("missing_cron", where, "schedule triggers need a cron expression")
        if trig.channel is not None:
            target = channels.get(trig.channel)
            if target is None:
                err("unknown_channel", where, f"delivers to unknown channel {trig.channel!r}")
            elif not trig.to and not target.address:
                err("no_recipient", where, f"channel {trig.channel!r} has no address; set 'to'")

    verifier = spec.policies.verification.verifier or {}
    if verifier:
        mode = verifier.get("mode", "critical_only")
        rate = verifier.get("sample_rate")
        where = "policies.verification.verifier"
        if mode not in ("always", "critical_only", "sampled"):
            err("invalid_verifier", where, "mode must be always, critical_only or sampled")
        elif mode == "sampled" and not (
            isinstance(rate, (int, float)) and not isinstance(rate, bool) and 0 < rate <= 1
        ):
            err("invalid_verifier", where, "sampled mode needs a sample_rate above 0, up to 1")
        elif mode == "critical_only" and "output" in (verifier.get("applies_to") or []):
            err("invalid_verifier", where, "answers have no critical checks; use sampled or always")

    isolation = spec.tools.config.get("python") or {}
    if isolation:
        where = "tools.config.python"
        mode = isolation.get("isolation", "none")
        if mode not in ("none", "container"):
            err("invalid_isolation", where, "isolation must be none or container")
        elif mode == "container" and not isolation.get("image"):
            err("invalid_isolation", where, "container isolation needs an image with python3")

    storage = (spec.tools.config.get("documents") or {}).get("storage")
    if storage is not None and (why := _file_source_issue(storage)):
        err("invalid_source", "tools.config.documents.storage", why)

    for wname, wf in workflows.items():
        ids = [s.id for s in wf.steps]
        if len(ids) != len(set(ids)):
            err("duplicate_step", f"workflows.{wname}", "step ids must be unique")
        for step in wf.steps:
            _check_step(step, f"workflows.{wname}.{step.id}", set(ids), spec, namespaces, err)

    # --- permissions and verification of side effects ----------------------------
    perms = spec.policies.permissions
    for kind, rules in (("allow", perms.allow), ("ask", perms.ask), ("deny", perms.deny)):
        for i, text in enumerate(rules):
            try:
                Rule.parse(text)
            except ValueError as exc:
                err("invalid_permission_rule", f"policies.permissions.{kind}[{i}]", str(exc))
    overrides = spec.tools.overrides
    externals = {k for k, o in overrides.items() if o.effect == "external"}
    verify_refs = {k: o.verify for k, o in overrides.items() if o.verify}
    for hname, conn in spec.tools.http.items():
        for op, o in conn.operations.items():
            full = f"{hname}.{op}"
            if o.effect == "external":
                externals.add(full)
            if o.verify:
                verify_refs[full] = o.verify
    for t, check_name in verify_refs.items():
        if check_name not in checks:
            err("unknown_check", f"tools.{t}", f"verify references unknown check {check_name!r}")
    all_rules = perms.allow + perms.ask + perms.deny
    for name, agent in agents.items():
        for t in agent.tools:
            if not (_covered(all_rules, t) or t in verify_refs or t in overrides):
                warn(
                    "uncovered_tool",
                    f"agents.{name}",
                    f"tool {t!r} falls back to the profile default",
                )
            if t in externals and not (
                _covered(perms.ask, t, unconditional=True) or t in verify_refs
            ):
                err(
                    "unguarded_external",
                    f"agents.{name}",
                    f"external tool {t!r} needs an ask rule or a verify check",
                )
    for cname, chk in checks.items():
        if (
            chk.type == "command"
            and (chk.model_extra or {}).get("workspace") not in spec.workspaces
        ):
            err(
                "unknown_workspace",
                f"policies.verification.checks.{cname}",
                "command check needs a known workspace",
            )

    # --- governance -------------------------------------------------------------
    gov = spec.governance
    if channels and not gov.pii.tokenize and not gov.pii.override_reason:
        err(
            "pii_untokenized",
            "governance.pii",
            "channels carry user data; tokenize or give override_reason",
        )
    contact_templates = [
        (wname, s.id)
        for wname, wf in workflows.items()
        for s in wf.steps
        if s.type == "template"
        and (target := channels.get(str((s.model_extra or {}).get("channel"))))
        and target.purpose not in OPERATOR_PURPOSES
    ]
    if contact_templates and not gov.consent.required:
        err(
            "consent_missing",
            "governance.consent",
            "workflows message contacts; consent must be required",
        )
    for t in gov.pii.reveal_to_tools:
        if t.split(".", 1)[0] not in namespaces:
            err("unknown_tool_namespace", "governance.pii.reveal_to_tools", f"unknown tool {t!r}")
    if (
        any(t.startswith("ledger.") for a in agents.values() for t in a.tools)
        and spec.ledger is None
    ):
        err("missing_ledger", "ledger", "agents use ledger tools but no ledger is defined")

    for code, path, message in region_violations(spec):
        err(code, path, message)

    # --- instance-only ----------------------------------------------------------
    if is_instance:
        if spec.tenant is None:
            err("missing_tenant", "tenant", "an instance needs a tenant")
        if spec.deploy is None:
            warn("missing_deploy", "deploy", "no deploy target; only local runs are possible")
        data_region = gov.regions.get("data")
        if spec.deploy and spec.deploy.target == "aws" and data_region in AWS_REGION_PREFIX:
            region = spec.deploy.region or ""
            if not region.startswith(AWS_REGION_PREFIX[data_region]):
                err(
                    "region_violation",
                    "deploy.region",
                    f"deploy region {region!r} violates governance data region {data_region!r}",
                )
        leftovers = sorted(set(_VAR_TEMPLATE.findall("\n".join(_strings(data)))))
        for v in leftovers:
            err("unresolved_variable", f"{{{{var.{v}}}}}", f"variable {v!r} has no value")

    return issues


def _file_source_issue(source: Any) -> str | None:
    src = source if isinstance(source, dict) else {}
    needs = {"folder": "path", "s3": "bucket"}.get(str(src.get("type")))
    if needs is None:
        return "a file source has type folder or s3"
    if not src.get(needs):
        return f"{src.get('type')} sources need a {needs}"
    return None


def _check_step(
    step: Step, where: str, ids: set[str], spec: SolutionSpec, namespaces: set[str], err: Any
) -> None:
    extra = step.model_extra or {}
    if step.type == "agent" and extra.get("agent") not in spec.agents:
        err("unknown_agent", where, f"unknown agent {extra.get('agent')!r}")
    elif step.type == "tool" and str(extra.get("tool", "")).split(".", 1)[0] not in namespaces:
        err(
            "unknown_tool_namespace", where, f"tool {extra.get('tool')!r} has no provider namespace"
        )
    elif step.type in ("template", "message") and extra.get("channel") not in spec.channels:
        err("unknown_channel", where, f"unknown channel {extra.get('channel')!r}")
    elif step.type == "template":
        channel = spec.channels[str(extra["channel"])]
        if extra.get("template") not in channel.templates:
            err("unknown_template", where, f"unknown template {extra.get('template')!r}")
    elif step.type == "timer" and extra.get("trigger") not in spec.triggers:
        err("unknown_trigger", where, f"unknown timer trigger {extra.get('trigger')!r}")
    elif step.type == "branch":
        for case in extra.get("cases", []):
            if case.get("goto") not in ids:
                err("unknown_step", where, f"branch goes to unknown step {case.get('goto')!r}")
    elif step.type == "parallel":
        for branch in extra.get("branches", []):
            if branch.get("type") == "agent" and branch.get("agent") not in spec.agents:
                err(
                    "unknown_agent",
                    where,
                    f"parallel branch uses unknown agent {branch.get('agent')!r}",
                )
