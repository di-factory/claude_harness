"""Command line.

- ``dif-general-harness spec validate|resolve PATH``
- ``dif-general-harness run INSTANCE [-m TEXT]``: run an agent headless; without ``-m``
  each stdin line is one user turn. Text streams to stdout, tool activity to stderr.
- ``dif-general-harness console INSTANCE``: the same agent in the TUI, with approvals.
- ``dif-general-harness build``: the constructor: match a pack, interview, write and
  validate the instance spec (``--answers FILE`` for a non-interactive build).
- ``dif-general-harness eval INSTANCE``: run the instance's eval suites.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .constructor import RecordingApprover, build, load_answers, match, run_suites
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
)
from .spec import PackCatalog, ResolvedSpec, SpecError, load_instance, load_pack
from .tenancy import EnvSecrets, FileSecrets


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
    }
    cmd = sub.add_parser(name, help=helps[name])
    cmd.add_argument("path", type=Path, help="instance JSON")
    cmd.add_argument("--packs", type=Path, action="append", help="folder containing packs")
    cmd.add_argument("--agent", help="agent name; default: the first agent")
    if name == "run":
        cmd.add_argument("-m", "--message", help="one user message; default: read stdin lines")
    if name == "eval":
        cmd.add_argument("--suite", type=Path, action="append", help="suite file (repeatable)")
    cmd.add_argument("--session", help="resume a session id")
    cmd.add_argument("--state", type=Path, default=Path(".dif/state"), help="state folder")
    cmd.add_argument("--secrets-dir", type=Path, help="one file per secret; default: env vars")
    cmd.add_argument(
        "--workspace", action="append", default=[], metavar="NAME=DIR", help="local workspace"
    )
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
        secrets=FileSecrets(args.secrets_dir) if args.secrets_dir else EnvSecrets(),
        approver=AutoApprover() if args.yes else approver,
        workspaces=workspaces,
        provider=provider,
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
    approvals = RecordingApprover()
    options = _options(args, provider, approvals)
    if options is not None:
        options.approver = approvals  # evals never approve, even with --yes
    started = await _start(args, resolved, options) if options else None
    if started is None:
        return 2
    instance, agent = started
    suites = args.suite or [Path(s) for s in instance.spec.evals.suites]
    async with instance:
        report = await run_suites(agent, suites, approvals, instance.spec.evals.thresholds)
    for r in report.results:
        print(f"{r.status.upper():8} {r.suite} / {r.case}  ${r.cost_usd:.4f}")
        for reason in r.reasons:
            print(f"         - {reason}")
    rate = "n/a" if report.pass_rate is None else f"{report.pass_rate:.0%}"
    skipped = len(report.results) - len(report.ran)
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
    build_cmd = sub.add_parser("build", help="constructor: interview and build an instance")
    build_cmd.add_argument("--request", help="what the client needs, in plain words")
    build_cmd.add_argument("--pack", action="append", help="pack id (skip matching)")
    build_cmd.add_argument("--answers", type=Path, help="answers file (YAML/JSON): no prompts")
    build_cmd.add_argument("--out", type=Path, default=Path("instances"), help="output folder")
    build_cmd.add_argument("--packs", type=Path, action="append", help="folder containing packs")
    args = parser.parse_args(argv)

    if args.group == "build":
        return _build(args)

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
