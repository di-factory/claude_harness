"""Command line.

- ``dif-general-harness spec validate|resolve PATH``
- ``dif-general-harness run INSTANCE [-m TEXT]``: run an agent headless; without ``-m``
  each stdin line is one user turn. Text streams to stdout, tool activity to stderr.
- ``dif-general-harness console INSTANCE``: the same agent in the TUI, with approvals.
- ``dif-general-harness build``: the constructor: match a pack, interview, write and
  validate the instance spec (``--answers FILE`` for a non-interactive build).
- ``dif-general-harness eval INSTANCE``: run the instance's eval suites.
- ``dif-general-harness keys new NAME`` / ``approve INSTANCE --target T`` /
  ``deploy INSTANCE --target T``: constructor v2; a deploy needs Jag's signed approval of
  exactly this staged solution.
- ``dif-general-harness serve INSTANCE``: the headless service (``--database-url`` for
  Postgres); the admin token is the ``admin_token`` secret.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import operator
import os
import shutil
import sys
import tempfile
from functools import partial
from pathlib import Path
from typing import Any

from .constructor import (
    EvalStore,
    RecordingApprover,
    build,
    load_answers,
    match,
    questionnaire,
    run_suites,
)
from .constructor.deploy import (
    DeployError,
    approve,
    check_approval,
    new_key,
    plan_aws,
    plan_docker,
    stage,
)
from .constructor.impact import missing as impact_of
from .constructor.interview import Question
from .core.events import ErrorEvent, TextDelta, ToolCallFinished, ToolCallStarted, TurnEnded
from .observability import UsageStore, quality
from .policy import Approver, AutoApprover
from .providers.base import ModelProvider
from .runtime import (
    AgentRuntime,
    Instance,
    InstanceError,
    PromptError,
    RoutingError,
    RuntimeOptions,
    scope_for,
)
from .runtime.routing import check_anthropic_key
from .spec import PackCatalog, ResolvedSpec, SpecError, load_instance, load_pack
from .store.db import connect
from .tenancy import FileSecrets, default_secrets_dir, load_env_file, local_backend


def _load(path: Path, packs: list[Path] | None) -> ResolvedSpec:
    target = path / "pack.json" if path.is_dir() else path
    kind = json.loads(target.read_text(encoding="utf-8")).get("kind")
    if kind == "pack":
        return load_pack(target)
    roots = packs or [target.parent.parent, target.parent]
    return load_instance(target, PackCatalog(roots=[r for r in roots if r.exists()]))


def _add_run(sub: argparse._SubParsersAction[argparse.ArgumentParser], name: str) -> None:
    helps = {
        "run": "run an instance's agent headless",
        "console": "chat with an agent (TUI)",
        "eval": "run the instance's eval suites",
        "serve": "run the headless service (channels, triggers, inbox, admin API)",
    }
    cmd = sub.add_parser(name, help=helps[name])
    cmd.add_argument("path", type=Path, help="instance JSON")
    cmd.add_argument("--packs", type=Path, action="append", help="folder containing packs")
    cmd.add_argument("--agent", help="agent name; default: the first agent")
    if name == "run":
        cmd.add_argument("-m", "--message", help="one user message; default: read stdin lines")
    if name == "eval":
        cmd.add_argument("--suite", type=Path, action="append", help="suite file (repeatable)")
    if name == "serve":
        cmd.add_argument(
            "--database-url",
            default=os.environ.get("DIF_DATABASE_URL"),
            help="postgresql://... (default: DIF_DATABASE_URL, else SQLite in the state folder)",
        )
        cmd.add_argument("--host", default="0.0.0.0")
        cmd.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
        cmd.add_argument(
            "--public-url",
            default=os.environ.get("DIF_PUBLIC_URL"),
            help="the URL providers call (for signature checks behind a load balancer)",
        )
        cmd.add_argument("--lanes", type=int, default=4, help="concurrent worker lanes")
    cmd.add_argument("--session", help="resume a session id")
    cmd.add_argument("--state", type=Path, default=Path(".dif/state"), help="state folder")
    cmd.add_argument(
        "--secrets-dir",
        type=Path,
        help="one file per secret; default: DIF_SECRET_<NAME> variables, then ~/.dif/secrets",
    )
    cmd.add_argument(
        "--workspace", action="append", default=[], metavar="NAME=DIR", help="local workspace"
    )
    if name != "serve":  # the service never auto-approves: approvals go to the inbox
        cmd.add_argument(
            "--yes", action="store_true", help="approve every 'ask' (trusted local runs only)"
        )


def _options(
    args: argparse.Namespace, provider: ModelProvider | None, approver: Approver | None
) -> RuntimeOptions | None:
    workspaces = {}
    for item in args.workspace:
        name, sep, folder = item.partition("=")
        if not sep:
            print(f"--workspace expects NAME=DIR, got {item!r}", file=sys.stderr)
            return None
        workspaces[name] = Path(folder)
    return RuntimeOptions(
        state_root=args.state,
        secrets=FileSecrets(args.secrets_dir) if args.secrets_dir else local_backend(),
        approver=AutoApprover() if getattr(args, "yes", False) else approver,
        workspaces=workspaces,
        provider=provider,
        database_url=getattr(args, "database_url", None),
    )


async def _start(
    args: argparse.Namespace, resolved: ResolvedSpec, options: RuntimeOptions
) -> tuple[Instance, AgentRuntime] | None:
    try:
        instance = await Instance.open(resolved, options)
    except (InstanceError, RoutingError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return None
    try:
        return instance, instance.agent(args.agent)
    except (KeyError, PromptError) as exc:
        await instance.close()
        print(f"cannot start: {exc}", file=sys.stderr)
        return None


async def _console(
    args: argparse.Namespace, resolved: ResolvedSpec, provider: ModelProvider | None
) -> int:
    from .console import ConsoleApp, ConsoleApprover  # textual loads only for the console

    approver = ConsoleApprover()
    options = _options(args, provider, approver)
    started = await _start(args, resolved, options) if options else None
    if started is None:
        return 2
    instance, agent = started
    async with instance:
        session = await (agent.resume(args.session) if args.session else agent.new_session())
        app = ConsoleApp(instance, agent, session)
        approver.app = app
        await app.run_async()
    return 0


async def _run(
    args: argparse.Namespace, resolved: ResolvedSpec, provider: ModelProvider | None
) -> int:
    options = _options(args, provider, None)
    started = await _start(args, resolved, options) if options else None
    if started is None:
        return 2
    instance, agent = started
    async with instance:
        for issue in instance.issues:
            print(issue, file=sys.stderr)
        if agent.missing_tools:
            print(f"note: tools not available: {', '.join(agent.missing_tools)}", file=sys.stderr)
        session = await (agent.resume(args.session) if args.session else agent.new_session())
        print(f"session {session.id} (agent {agent.name})", file=sys.stderr)
        messages = [args.message] if args.message else (ln.rstrip("\n") for ln in sys.stdin)
        code = 0
        for text in messages:
            if not text.strip():
                continue
            async for event in agent.send(session, text):
                if isinstance(event, TextDelta):
                    print(event.text, end="", flush=True)
                elif isinstance(event, ToolCallStarted):
                    print(f"\n-> {event.name} {json.dumps(event.input)}", file=sys.stderr)
                elif isinstance(event, ToolCallFinished):
                    print(f"<- {event.name}: {event.status}", file=sys.stderr)
                elif isinstance(event, ErrorEvent):
                    print(f"\nerror: {event.message}", file=sys.stderr)
                elif isinstance(event, TurnEnded):
                    print()
                    print(
                        f"[{event.reason}, {event.turns} turn(s), ${event.usage.cost_usd:.4f}]",
                        file=sys.stderr,
                    )
                    code = 0 if event.reason == "end_turn" else 1
        return code


def database_url_from_env(environ: dict[str, str] | None = None) -> str | None:
    """Postgres from parts (``DIF_DB_HOST``, ``DIF_DB_NAME``, ``DIF_DB_USER``,
    ``DIF_DB_PASSWORD``), as ECS injects them: the password comes from Secrets Manager."""
    from urllib.parse import quote

    env = dict(os.environ) if environ is None else environ
    host = env.get("DIF_DB_HOST")
    if not host:
        return None
    user = quote(env.get("DIF_DB_USER", "dif"), safe="")
    password = quote(env.get("DIF_DB_PASSWORD", ""), safe="")
    port = env.get("DIF_DB_PORT", "5432")
    name = env.get("DIF_DB_NAME", "dif")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}?sslmode=require"


async def _serve(
    args: argparse.Namespace, resolved: ResolvedSpec, provider: ModelProvider | None
) -> int:
    import uvicorn

    from .runtime import scope_for
    from .service import Headless, create_app
    from .service.config import boot_config
    from .store import connect
    from .tenancy import ConfigError

    options = _options(args, provider, None)
    if options is None:
        return 2
    url = (
        args.database_url
        or database_url_from_env()
        or f"sqlite:///{args.state.resolve() / 'dif.db'}"
    )
    db = await connect(url)
    try:
        running = await boot_config(db, scope_for(resolved.spec), resolved)
        options.database = db
        instance = await Instance.open(running, options)
    except (InstanceError, RoutingError, ConfigError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        await db.close()
        return 2
    async with instance:
        headless = await Headless.build(instance, public_url=args.public_url)
        for issue in [*instance.issues, *headless.issues]:
            print(issue, file=sys.stderr)
        admin_token = options.secrets.get("admin_token")
        if not admin_token:
            print("note: no admin_token secret; the admin API is off", file=sys.stderr)
        background = []
        fleet_url = os.environ.get("DIF_FLEET_URL")
        fleet_token = options.secrets.get("fleet_token")
        if fleet_url and fleet_token and os.environ.get("DIF_FLEET_PUBLIC_KEY"):
            from .fleet import InstanceAgent, evaluator, load_public_key

            agent = InstanceAgent(
                headless,
                control_url=fleet_url,
                token=fleet_token,
                public_key=load_public_key(os.environ["DIF_FLEET_PUBLIC_KEY"]),
                evaluate=evaluator(options),  # eval-gated offers run the pack's evals here first
            )
            background.append(agent.run)
            print(f"instance agent reporting to {fleet_url}", file=sys.stderr)
        app = create_app(
            headless, admin_token=admin_token, worker_lanes=args.lanes, background=background
        )
        config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
        await uvicorn.Server(config).serve()
    await db.close()
    return 0


def _approve_or_deploy(args: argparse.Namespace) -> int:
    import subprocess
    import tempfile

    target = args.target
    instance = args.path
    catalog = PackCatalog(roots=args.packs or [instance.parent.parent, instance.parent])
    approval_file = getattr(args, "approval", None) or instance.with_suffix(
        f".{target}.approval.json"
    )
    try:
        if args.group == "approve":
            with tempfile.TemporaryDirectory() as tmp:
                staged = stage(instance, catalog, Path(tmp) / "solution")
                record = approve(staged, Path(tmp) / "solution", target, args.key, args.by)
            approval_file.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            print(f"approved {record['instance_id']} for {target} as {args.by}: {approval_file}")
            return 0
        if not approval_file.exists():
            print(f"no approval at {approval_file}: Jag approves every deploy", file=sys.stderr)
            return 3
        approvers = json.loads(args.approvers.read_text(encoding="utf-8"))
        staged_id = load_instance(instance, catalog).spec.solution.id
        out = args.out or Path("deploy/build") / staged_id
        out.mkdir(parents=True, exist_ok=True)
        staged = stage(instance, catalog, out / "solution")
        check_approval(
            json.loads(approval_file.read_text(encoding="utf-8")),
            out / "solution",
            target,
            approvers,
        )
        plan = (
            plan_docker(staged, out, public_url=getattr(args, "public_url", None))
            if target == "docker"
            else plan_aws(staged, out)
        )
    except (DeployError, SpecError, OSError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    print(f"approved solution staged at {plan.folder / 'solution'}")
    for f in plan.files:
        print(f"wrote {f}")
    print("secrets the client stores in their own vault:")
    for name in sorted(plan.secrets):
        print(f"  - {name}: {plan.secrets[name]}")
    print("commands:")
    for command in plan.commands:
        print("  " + " ".join(command))
    if args.run:
        ecr = ""
        for command in plan.commands:
            if any("{ecr}" in part for part in command):
                if not ecr:  # the repository exists now: ask terraform where it is
                    out_cmd = [*plan.commands[0][:2], "output", "-raw", "ecr_repository"]
                    ecr = subprocess.run(
                        out_cmd, capture_output=True, text=True, check=True
                    ).stdout.strip()
                command = [part.replace("{ecr}", ecr) for part in command]
            if subprocess.run(command, check=False).returncode != 0:
                print(f"failed: {' '.join(command)}", file=sys.stderr)
                return 1
    return 0


def _default_roots() -> list[Path]:
    return [p for p in (Path("packs"), Path("docs/spec/examples")) if p.is_dir()]


def _prompt(q: Question, error: str | None) -> str:
    if error:
        print(f"  ! {error}")
    hints = [q.answered_by]
    if q.example:
        hints.append(f"e.g. {q.example}")
    if q.default is not None:
        hints.append(f"default {json.dumps(q.default, ensure_ascii=False)}")
    return input(f"[{q.group}] {q.text} ({'; '.join(hints)})\n> ")


def _build(args: argparse.Namespace) -> int:
    catalog = PackCatalog(roots=args.packs or _default_roots())
    pack_ids: list[str] = args.pack or []
    if not pack_ids:
        if not args.request:
            print("give --pack ID or --request TEXT", file=sys.stderr)
            return 2
        found = match(catalog, args.request)
        if not found:
            print("No pack fits this request: it needs a new pack (Di-Factory design work).")
            return 3
        for i, m in enumerate(found, 1):
            print(f"{i}. {m.pack_id}@{m.version} (score {m.score:.2f}): {m.description}")
        if args.answers is None:
            choice = input("Which pack? [1] ").strip() or "1"
            pack_ids = [found[int(choice) - 1].pack_id]
        elif found[0].score >= 0.5:
            pack_ids = [found[0].pack_id]
        else:
            print("No confident match; pass --pack explicitly.", file=sys.stderr)
            return 3
    try:
        answers: dict[str, Any] | None = None
        for path in args.answers or []:
            given = {k: v for k, v in load_answers(path).items() if v not in (None, "")}
            answers = {**(answers or {}), **given}  # later files win; blanks never erase
    except (OSError, ValueError) as exc:
        print(f"cannot read answers: {exc}", file=sys.stderr)
        return 2
    try:
        result = build(
            catalog,
            pack_ids,
            args.out,
            answers=answers,
            ask=None if args.answers else _prompt,
        )
    except SpecError as exc:
        for issue in exc.issues:
            print(issue, file=sys.stderr)
        return 2
    for problem in result.problems:
        print(f"open: {problem}")
    if result.resolved is not None:
        for issue in result.resolved.issues:
            print(issue)
    print(f"spec:    {result.spec_path}")
    print(f"answers: {result.answers_path}")
    print(f"summary: {result.summary_path}")
    print("OK: instance built and validated" if result.ok else "NOT READY: see above")
    return 0 if result.ok else 1


async def _eval(
    args: argparse.Namespace, resolved: ResolvedSpec, provider: ModelProvider | None
) -> int:
    """Each case runs in its own throwaway instance (never the production database); the
    results are recorded in the instance's own database, to report drift between runs."""
    template = _options(args, provider, RecordingApprover())
    if template is None:
        return 2

    async def open_instance(state: Path) -> Instance:
        options = dataclasses.replace(
            template, state_root=state, database_url=None, approver=RecordingApprover()
        )
        return await Instance.open(resolved, options)

    suites = args.suite or [Path(s) for s in resolved.spec.evals.suites]
    with tempfile.TemporaryDirectory(prefix="dif-eval-") as work:
        try:
            report = await run_suites(
                open_instance, suites, resolved.spec.evals.thresholds, work=Path(work)
            )
        except (InstanceError, RoutingError) as exc:
            print(f"cannot start: {exc}", file=sys.stderr)
            return 2
    args.state.mkdir(parents=True, exist_ok=True)
    url = getattr(args, "database_url", None) or f"sqlite:///{args.state.resolve() / 'dif.db'}"
    db = await connect(url)
    try:
        store = EvalStore(db, scope_for(resolved.spec))
        run_id = await store.record(report, resolved.version_hash)
        report.regressions = await store.regressions(report, run_id)
    finally:
        await db.close()
    for r in report.results:
        print(f"{r.status.upper():8} {r.suite} / {r.case}  ${r.cost_usd:.4f}")
        for reason in r.reasons:
            print(f"         - {reason}")
    rate = "n/a" if report.pass_rate is None else f"{report.pass_rate:.0%}"
    skipped = len(report.results) - len(report.ran)
    for name in report.regressions:
        print(f"REGRESSION {name} (passed in the previous run)")
    unmeasured = sorted(set(report.thresholds) - {"pass_rate", "unsafe_actions"})
    if unmeasured:
        print(f"not measured yet: {', '.join(unmeasured)}")
    print(
        f"pass rate {rate} over {len(report.ran)} case(s), {skipped} skipped, "
        f"unsafe actions {report.unsafe_actions}: {'OK' if report.ok else 'FAILED'}"
    )
    return 0 if report.ok else 1


def _lifecycle(args: argparse.Namespace) -> int:
    from .constructor.lifecycle import adjust, parse_value, upgrade

    catalog = PackCatalog(roots=args.packs or [args.path.resolve().parent.parent])
    try:
        if args.group == "adjust":
            values: dict[str, Any] = {}
            for item in args.set:
                name, sep, raw = item.partition("=")
                if not sep or not name:
                    print(f"--set expects NAME=VALUE, got {item!r}", file=sys.stderr)
                    return 2
                values[name] = parse_value(raw) if raw else None
            plan = adjust(args.path, catalog, values)
        else:
            plan = upgrade(args.path, catalog, args.pack, args.to)
    except (SpecError, ValueError) as exc:
        print(f"cannot {args.group}: {exc}", file=sys.stderr)
        return 2
    for change in plan.changes:
        print(change)
    if not plan.changes:
        print("no change")
    for name in plan.missing:
        print(f"needs a value: {name} (adjust --set {name}=...)")
    for error in plan.errors():
        print(f"error: {error}")
    print(f"config {plan.before.version_hash[:12]} -> {plan.after.version_hash[:12]}")
    if not plan.ok:
        print("NOT WRITTEN: the result does not validate")
        return 1
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    plan.write()
    print(f"wrote {args.path}; next: evals, then approve + deploy (or fleet offer)")
    return 0


async def _fleet(args: argparse.Namespace) -> int:
    from .constructor.lifecycle import ControlClient, FleetError, container_data, write_token

    token = os.environ.get("DIF_CONTROL_ADMIN_TOKEN", "")
    if not args.control or not token:
        print("set --control (or DIF_CONTROL_URL) and DIF_CONTROL_ADMIN_TOKEN", file=sys.stderr)
        return 2
    client = ControlClient(args.control, token)

    def load(path: Path) -> tuple[ResolvedSpec, PackCatalog]:
        catalog = PackCatalog(roots=args.packs or [path.resolve().parent.parent])
        return load_instance(path, catalog), catalog

    def target(resolved: ResolvedSpec) -> tuple[str, str]:
        scope = scope_for(resolved.spec)
        return scope.tenant_id, scope.instance_id

    try:
        if args.command == "register":
            resolved, _ = load(args.path)
            tenant, instance = target(resolved)
            path = write_token(
                args.out, tenant, instance, await client.register(tenant, instance, args.by)
            )
            key = await client.public_key()
            print(f"registered {tenant}/{instance}")
            print(f"token written to {path}: store it as the secret 'fleet_token' in the")
            print("client's vault, then delete the file")
            print(f"deploy with fleet_url={args.control} fleet_public_key={key}")
        elif args.command == "offer":
            resolved, catalog = load(args.path)
            tenant, instance = target(resolved)
            data = container_data(args.path, catalog)
            made = await client.offer(tenant, instance, data, args.approved_by, args.gate)
            print(f"offer {made['id']} to {tenant}/{instance}: {made['status']} (gate {args.gate})")
        elif args.command == "rollout":
            steps = []
            for path in args.paths:
                resolved, catalog = load(path)
                tenant, instance = target(resolved)
                steps.append(
                    {"tenant": tenant, "instance": instance, "data": container_data(path, catalog)}
                )
            made = await client.rollout(args.name, args.approved_by, steps, args.gate)
            print(f"rollout {made['id']} ({made['status']}): {len(steps)} instance(s), one by one")
        elif args.command == "rollback":
            if args.rollout:
                made = await client.rollout_rollback(args.rollout, args.approved_by)
                print(f"rollout {made['id']}: {made['status']}")
            elif args.instance and args.to:
                tenant, _, instance = args.instance.partition("/")
                made = await client.rollback_instance(tenant, instance, args.to, args.approved_by)
                print(f"rollback offer {made['id']} to {args.instance}")
            else:
                print("give --rollout ID, or --instance TENANT/INSTANCE --to HASH", file=sys.stderr)
                return 2
        else:
            for row in await client.fleet():
                state = "ok" if row["healthy"] else "STALE"
                pending = row["pending_offer"]
                print(
                    f"{state:6} {row['tenant']}/{row['instance']}  config "
                    f"{str(row['running_config'])[:12]}  spend ${row['spend_today_usd']:.2f}"
                    + (f"  offer {pending['id']} {pending['status']}" if pending else "")
                )
    except (FleetError, SpecError, DeployError) as exc:
        print(f"fleet {args.command}: {exc}", file=sys.stderr)
        return 1
    return 0


async def _control_serve(args: argparse.Namespace) -> int:
    """The control plane: its admin token comes from DIF_CONTROL_ADMIN_TOKEN, never argv."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from .control import ControlPlane, create_control_app

    admin_token = os.environ.get("DIF_CONTROL_ADMIN_TOKEN", "")
    if len(admin_token) < 16:
        print("set DIF_CONTROL_ADMIN_TOKEN (at least 16 characters)", file=sys.stderr)
        return 2
    key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        print(
            "the control plane signs with an Ed25519 key (dif-general-harness keys new)",
            file=sys.stderr,
        )
        return 2
    if args.database_url.startswith("sqlite:///"):
        Path(args.database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    db = await connect(args.database_url)
    plane = ControlPlane(db, key)
    print(f"control plane public key (DIF_FLEET_PUBLIC_KEY): {plane.public_key()}", file=sys.stderr)
    import uvicorn

    app = create_control_app(plane, admin_token=admin_token)
    try:
        await uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port)).serve()
    finally:
        await db.close()
    return 0


async def _costs(args: argparse.Namespace, resolved: ResolvedSpec) -> int:
    url = args.database_url or f"sqlite:///{args.state.resolve() / 'dif.db'}"
    if url.startswith("sqlite") and not (args.state / "dif.db").exists():
        print(f"no database at {args.state / 'dif.db'}: nothing has run yet", file=sys.stderr)
        return 2
    db = await connect(url)
    try:
        scope = scope_for(resolved.spec)
        try:
            report = await UsageStore(db).report(
                scope, since=args.since, until=args.until, by=args.by.split(",")
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        metrics = await quality(db, scope, "30d")
    finally:
        await db.close()
    if args.json:
        print(json.dumps({"costs": report, "metrics": metrics}, indent=2))
        return 0
    dims = report["by"]
    print(f"{scope.tenant_id}/{scope.instance_id}  {report['since']} .. {report['until']}")
    for row in report["rows"]:
        label = " / ".join(str(row[d]) for d in dims)
        tokens = row["input_tokens"] + row["output_tokens"]
        print(f"  {label:48} {row['calls']:6} calls {tokens:10} tokens  ${row['usd']:.4f}")
    total = report["total"]
    print(f"  {'total':48} {total.get('calls', 0):6} calls {'':17}  ${total.get('usd', 0.0):.4f}")
    if report["unpriced"]:
        print(f"  unpriced (no list price; add one): {', '.join(report['unpriced'])}")
    rate = metrics["tool_success_rate"]
    print(
        f"quality (30d): tool success {'n/a' if rate is None else f'{rate:.0%}'}, "
        f"escalations {metrics['escalations']}/{metrics['conversations']}, "
        f"cost per resolved {metrics['cost_per_resolved_usd']}"
    )
    return 0


def main(argv: list[str] | None = None, *, provider: ModelProvider | None = None) -> int:
    """``provider`` replaces the spec's models (tests and offline demos)."""
    load_env_file(Path(".env"))  # e.g. DIF_SECRET_ANTHROPIC=...; never committed (.gitignore)
    parser = argparse.ArgumentParser(prog="dif-general-harness")
    sub = parser.add_subparsers(dest="group", required=True)
    spec = sub.add_parser("spec", help="work with solution specs")
    spec_sub = spec.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("validate", "validate a pack or instance"),
        ("resolve", "print the resolved spec as JSON"),
    ):
        cmd = spec_sub.add_parser(name, help=help_text)
        cmd.add_argument("path", type=Path, help="pack folder, pack.json or instance JSON")
        cmd.add_argument(
            "--packs",
            type=Path,
            action="append",
            help="folder containing packs (repeatable); default: next to the instance",
        )
    copy = spec_sub.add_parser(
        "copy", help="copy an instance with the files it references (a new client from an example)"
    )
    copy.add_argument("path", type=Path, help="instance JSON to copy")
    copy.add_argument("dest", type=Path, help="new folder; the copy is <dest>/instance.json")
    copy.add_argument("--id", help="new solution id (default: keep the original's)")
    _add_run(sub, "run")
    _add_run(sub, "console")
    _add_run(sub, "eval")
    _add_run(sub, "serve")
    for name, text in (
        ("adjust", "constructor: change an instance's values (validated, diffed)"),
        ("upgrade", "constructor: move an instance to a newer pack version"),
    ):
        life = sub.add_parser(name, help=text)
        life.add_argument("path", type=Path, help="instance JSON")
        life.add_argument("--packs", type=Path, action="append", help="folder containing packs")
        life.add_argument("--dry-run", action="store_true", help="show the change; write nothing")
        if name == "adjust":
            life.add_argument(
                "--set",
                action="append",
                default=[],
                metavar="NAME=VALUE",
                help="a value (JSON or text); NAME= removes it (repeatable)",
            )
        else:
            life.add_argument("--pack", required=True, help="the pack to move (its id)")
            life.add_argument("--to", required=True, help="the pack version, e.g. 1.1.0")
    fleet = sub.add_parser("fleet", help="operate instances through the control plane")
    fleet.add_argument("--control", default=os.environ.get("DIF_CONTROL_URL", ""))
    fleet.add_argument("--packs", type=Path, action="append", help="folder containing packs")
    fleet_sub = fleet.add_subparsers(dest="command", required=True)
    f_register = fleet_sub.add_parser("register", help="register an instance; writes its token")
    f_register.add_argument("path", type=Path, help="instance JSON")
    f_register.add_argument("--by", required=True)
    f_register.add_argument("--out", type=Path, default=Path.home() / ".dif" / "fleet")
    f_offer = fleet_sub.add_parser("offer", help="offer an instance its current spec (signed)")
    f_offer.add_argument("path", type=Path, help="instance JSON")
    f_offer.add_argument("--approved-by", required=True)
    f_offer.add_argument("--gate", choices=["evals", "none"], default="evals")
    f_rollout = fleet_sub.add_parser("rollout", help="take instances' specs out one by one")
    f_rollout.add_argument("paths", type=Path, nargs="+", help="instance JSON files, in order")
    f_rollout.add_argument("--name", required=True)
    f_rollout.add_argument("--approved-by", required=True)
    f_rollout.add_argument("--gate", choices=["evals", "none"], default="evals")
    f_back = fleet_sub.add_parser("rollback", help="roll a rollout (or one instance) back")
    f_back.add_argument("--rollout", help="rollout id")
    f_back.add_argument("--instance", help="TENANT/INSTANCE (with --to)")
    f_back.add_argument("--to", help="the config hash to return to")
    f_back.add_argument("--approved-by", required=True)
    fleet_sub.add_parser("status", help="the fleet view")
    control = sub.add_parser("control", help="the Di-Factory control plane (fleet operations)")
    control_sub = control.add_subparsers(dest="command", required=True)
    control_serve = control_sub.add_parser("serve", help="run the control plane API")
    control_serve.add_argument("--key", type=Path, required=True, help="Ed25519 signing key (PEM)")
    control_serve.add_argument(
        "--database-url",
        default=os.environ.get("DIF_CONTROL_DATABASE_URL", "sqlite:///.dif/control.db"),
    )
    control_serve.add_argument("--host", default="0.0.0.0")
    control_serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8090")))
    costs_cmd = sub.add_parser("costs", help="model spend and quality metrics of an instance")
    costs_cmd.add_argument("path", type=Path, help="instance JSON")
    costs_cmd.add_argument("--packs", type=Path, action="append", help="folder containing packs")
    costs_cmd.add_argument("--state", type=Path, default=Path(".dif/state"), help="state folder")
    costs_cmd.add_argument("--database-url", default=os.environ.get("DIF_DATABASE_URL"))
    costs_cmd.add_argument("--since", help="first day (YYYY-MM-DD); default: 30 days ago")
    costs_cmd.add_argument("--until", help="last day (YYYY-MM-DD); default: today")
    costs_cmd.add_argument("--by", default="vendor", help="day,agent,role,vendor,model")
    costs_cmd.add_argument("--json", action="store_true", help="print JSON")
    build_cmd = sub.add_parser("build", help="constructor: interview and build an instance")
    build_cmd.add_argument("--request", help="what the client needs, in plain words")
    build_cmd.add_argument("--pack", action="append", help="pack id (skip matching)")
    build_cmd.add_argument(
        "--answers", type=Path, action="append",
        help="answers file (YAML/JSON), repeatable (client's and Di-Factory's): no prompts",
    )  # fmt: skip
    build_cmd.add_argument("--out", type=Path, default=Path("instances"), help="output folder")
    build_cmd.add_argument("--packs", type=Path, action="append", help="folder containing packs")

    setup = sub.add_parser(
        "setup", help="guided setup: key, questionnaire, build, test, and optionally online"
    )
    setup.add_argument("--packs", type=Path, action="append", help="folder containing packs")
    setup.add_argument("--out", type=Path, default=Path("clients"), help="where clients go")
    setup.add_argument(
        "--public-url", default=os.environ.get("DIF_PUBLIC_URL"),
        help="https address to serve it at (setup.sh finds it from the server's public IP)",
    )  # fmt: skip

    form = sub.add_parser(
        "questionnaire", help="constructor: a fill-in questionnaire for a client (build --answers)"
    )
    form.add_argument("--pack", action="append", required=True, help="pack id (repeatable)")
    form.add_argument("--packs", type=Path, action="append", help="folder containing packs")
    form.add_argument(
        "--for", dest="audience", choices=["client", "difactory", "all"], default="all",
        help="client: the business owner's questions; difactory: models and ids; all (default)",
    )  # fmt: skip
    form.add_argument("--out", type=Path, help="write it here (default: print it)")

    secrets_cmd = sub.add_parser("secrets", help="store and check secrets for local runs")
    secrets_sub = secrets_cmd.add_subparsers(dest="command", required=True)
    s_set = secrets_sub.add_parser("set", help="store one secret as a file (asks for the value)")
    s_set.add_argument("name", help="the secret's name, e.g. anthropic")
    s_set.add_argument("--dir", type=Path, help="default: DIF_SECRETS_DIR or ~/.dif/secrets")
    s_set.add_argument(
        "--from-env-file", type=Path, metavar="FILE",
        help="take it from a .env file (DIF_SECRET_<NAME>=... or <NAME>=...)",
    )  # fmt: skip
    s_check = secrets_sub.add_parser(
        "check", help="which secrets an instance needs, and which are set"
    )
    s_check.add_argument("path", type=Path, help="instance JSON")
    s_check.add_argument("--packs", type=Path, action="append", help="folder containing packs")
    s_check.add_argument("--dir", type=Path, help="default: DIF_SECRETS_DIR or ~/.dif/secrets")

    keys = sub.add_parser("keys", help="approver keys (Ed25519)")
    keys_sub = keys.add_subparsers(dest="command", required=True)
    new = keys_sub.add_parser("new", help="create an approver key")
    new.add_argument("name", help="who approves with it, e.g. jag")
    new.add_argument("--out", type=Path, default=Path.home() / ".dif" / "keys")

    for name, text in (
        ("approve", "sign a staged solution for one deploy target (Jag)"),
        ("deploy", "stage, check the approval and prepare (or run) the deploy"),
    ):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("path", type=Path, help="instance JSON")
        cmd.add_argument("--packs", type=Path, action="append", help="folder containing packs")
        cmd.add_argument("--target", choices=["docker", "aws"], required=True)
        if name == "approve":
            cmd.add_argument("--key", type=Path, required=True, help="the approver's private key")
            cmd.add_argument(
                "--by", required=True, help="the approver's name (as in approvers.json)"
            )
        else:
            cmd.add_argument(
                "--approval", type=Path, help="default: <instance>.<target>.approval.json"
            )
            cmd.add_argument("--approvers", type=Path, default=Path(".dif/approvers.json"))
            cmd.add_argument("--out", type=Path, help="default: deploy/build/<instance-id>")
            cmd.add_argument("--run", action="store_true", help="run the deploy commands too")
            cmd.add_argument(
                "--public-url",
                help="docker: the HTTPS address a proxy serves; the app then listens on 127.0.0.1",
            )
    args = parser.parse_args(argv)

    if args.group == "build":
        return _build(args)
    if args.group == "setup":
        from .constructor.setup import run_setup

        return run_setup(args, run=lambda argv: main(argv, provider=provider))
    if args.group == "questionnaire":
        text = questionnaire(PackCatalog(roots=args.packs or _default_roots()), args.pack,
                             args.audience)  # fmt: skip
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text, encoding="utf-8")
            print(f"wrote {args.out}: fill it in, then run build --answers {args.out}")
        else:
            print(text, end="")
        return 0
    if args.group == "secrets":
        return _secrets(args)
    if args.group == "keys":
        path, public = new_key(args.out, args.name)
        print(f"private key: {path} (keep it secret; only {args.name} uses it)")
        print(f'add to approvers.json: "{args.name}": "{public}"')
        return 0
    if args.group == "control":
        return asyncio.run(_control_serve(args))
    if args.group in {"adjust", "upgrade"}:
        return _lifecycle(args)
    if args.group == "fleet":
        return asyncio.run(_fleet(args))
    if args.group in {"approve", "deploy"}:
        return _approve_or_deploy(args)
    if args.group == "spec" and args.command == "copy":
        return _copy_instance(args.path, args.dest, args.id)

    try:
        resolved = _load(args.path, args.packs)
    except SpecError as exc:
        for issue in exc.issues:
            print(issue, file=sys.stderr)
        return 2

    if args.group == "run":
        return asyncio.run(_run(args, resolved, provider))
    if args.group == "console":
        return asyncio.run(_console(args, resolved, provider))
    if args.group == "eval":
        return asyncio.run(_eval(args, resolved, provider))
    if args.group == "serve":
        return asyncio.run(_serve(args, resolved, provider))
    if args.group == "costs":
        return asyncio.run(_costs(args, resolved))
    if args.command == "resolve":
        print(json.dumps(resolved.data, indent=2, ensure_ascii=False))
        return 0 if resolved.ok else 1

    for issue in resolved.issues:
        print(issue)
    errors = sum(i.severity == "error" for i in resolved.issues)
    warnings = len(resolved.issues) - errors
    status = "OK" if resolved.ok else "FAILED"
    print(
        f"{status}: {resolved.spec.kind} {resolved.spec.solution.id} "
        f"({errors} errors, {warnings} warnings, config {resolved.version_hash})"
    )
    return 0 if resolved.ok else 1


def _secret_value(args: argparse.Namespace) -> str:
    if args.from_env_file:
        wanted = {f"DIF_SECRET_{args.name.upper().replace('-', '_').replace('.', '_')}",
                  args.name, args.name.upper()}  # fmt: skip
        for line in args.from_env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and key.strip() in wanted:
                return str(value).strip().strip("'\"")
        raise ValueError(f"{args.from_env_file} has no {' or '.join(sorted(wanted))} line")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    import getpass

    return getpass.getpass(f"Value for {args.name} (paste it, then Enter; nothing is shown): ")


def _describe_secret(name: str, value: str) -> str:
    shown = value[:10] if value.startswith("sk-") else value[:3]
    return f"{name}: {len(value)} characters, starts with {shown}..."


def _secrets(args: argparse.Namespace) -> int:
    folder = args.dir or default_secrets_dir()
    if args.command == "set":
        try:
            value = _secret_value(args).strip()
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not value:
            print("error: nothing was received. If pasting shows nothing, paste and press Enter"
                  " anyway; or use --from-env-file, or pipe it: pbpaste | ssh ... secrets set"
                  f" {args.name}", file=sys.stderr)  # fmt: skip
            return 2
        if args.name == "anthropic":
            try:
                check_anthropic_key(value, {})
            except RoutingError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o700)
        target = folder / args.name
        target.write_text(value, encoding="utf-8")
        target.chmod(0o600)
        print(f"saved {_describe_secret(args.name, value)} in {target}")
        return 0
    try:
        resolved = _load(args.path, args.packs)
    except SpecError as exc:
        for issue in exc.issues:
            print(issue, file=sys.stderr)
        return 2
    env = local_backend() if args.dir is None else None
    files = FileSecrets(folder)
    missing = 0
    print(f"secrets for {resolved.spec.solution.id} (files in {folder}):")
    for name, decl in sorted(resolved.spec.secrets.items()):
        value = (env.get(name) if env is not None else None) or files.get(name) or ""
        if not value:
            missing += 1
            [item] = impact_of(resolved.spec, resolved.data, partial(operator.ne, name))
            print(f"  ✗ {name}: not set ({decl.description})")
            for effect in item.impact:
                print(f"      without it: {effect}")
            print(f"      how to get it: {item.how}")
            continue
        problem = ""
        if name == "anthropic":
            try:
                check_anthropic_key(value, {})
            except RoutingError as exc:
                problem = f"  ⚠ {exc}"
        print(f"  ✓ {_describe_secret(name, value)}{problem}")
    print("all set" if not missing else f"{missing} not set (the parts that use them stay off)")
    return 0 if not missing else 1


def _copy_instance(source: Path, dest: Path, new_id: str | None) -> int:
    """Copy an instance JSON and every local file or folder it names by a relative path
    (template overrides, prompts, knowledge folders), keeping their layout, so the copy
    validates exactly like the original."""
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read {source}: {exc}", file=sys.stderr)
        return 2
    if data.get("kind") != "instance":
        print(f"{source} is not an instance spec", file=sys.stderr)
        return 2
    if (dest / "instance.json").exists():
        print(f"{dest / 'instance.json'} already exists; choose another folder", file=sys.stderr)
        return 2
    base = source.parent.resolve()
    copied: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, str) and value and "{{" not in value and "://" not in value:
            rel = Path(value)
            if rel.is_absolute() or ".." in rel.parts or len(value) > 300:
                return
            found = (base / rel).resolve()
            if found.is_relative_to(base) and found.exists() and found != base:
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if found.is_dir():
                    shutil.copytree(found, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(found, target)
                copied.append(value)

    walk(data)
    if new_id:
        data.setdefault("solution", {})["id"] = new_id
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "instance.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    for name in sorted(set(copied)):
        print(f"  copied {name}")
    print(f"next: dif-general-harness spec validate {out} --packs <folder with the packs>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
