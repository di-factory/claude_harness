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
import os
import sys
import tempfile
from pathlib import Path

from .constructor import EvalStore, RecordingApprover, build, load_answers, match, run_suites
from .constructor.deploy import (
    DeployError,
    approve,
    check_approval,
    new_key,
    plan_aws,
    plan_docker,
    stage,
)
from .constructor.interview import Question
from .core.events import ErrorEvent, TextDelta, ToolCallFinished, ToolCallStarted, TurnEnded
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
from .spec import PackCatalog, ResolvedSpec, SpecError, load_instance, load_pack
from .store.db import connect
from .tenancy import FileSecrets, backend_from_env


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
    cmd.add_argument("--secrets-dir", type=Path, help="one file per secret; default: env vars")
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
        secrets=FileSecrets(args.secrets_dir) if args.secrets_dir else backend_from_env(),
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
            from .fleet import InstanceAgent, load_public_key

            agent = InstanceAgent(
                headless,
                control_url=fleet_url,
                token=fleet_token,
                public_key=load_public_key(os.environ["DIF_FLEET_PUBLIC_KEY"]),
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
        plan = plan_docker(staged, out) if target == "docker" else plan_aws(staged, out)
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
        answers = load_answers(args.answers) if args.answers else None
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


def main(argv: list[str] | None = None, *, provider: ModelProvider | None = None) -> int:
    """``provider`` replaces the spec's models (tests and offline demos)."""
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
    _add_run(sub, "run")
    _add_run(sub, "console")
    _add_run(sub, "eval")
    _add_run(sub, "serve")
    build_cmd = sub.add_parser("build", help="constructor: interview and build an instance")
    build_cmd.add_argument("--request", help="what the client needs, in plain words")
    build_cmd.add_argument("--pack", action="append", help="pack id (skip matching)")
    build_cmd.add_argument("--answers", type=Path, help="answers file (YAML/JSON): no prompts")
    build_cmd.add_argument("--out", type=Path, default=Path("instances"), help="output folder")
    build_cmd.add_argument("--packs", type=Path, action="append", help="folder containing packs")

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
    args = parser.parse_args(argv)

    if args.group == "build":
        return _build(args)
    if args.group == "keys":
        path, public = new_key(args.out, args.name)
        print(f"private key: {path} (keep it secret; only {args.name} uses it)")
        print(f'add to approvers.json: "{args.name}": "{public}"')
        return 0
    if args.group in {"approve", "deploy"}:
        return _approve_or_deploy(args)

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


if __name__ == "__main__":
    sys.exit(main())
