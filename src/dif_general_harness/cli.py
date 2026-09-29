"""Command line.

- ``dif-general-harness spec validate|resolve PATH``
- ``dif-general-harness run INSTANCE [-m TEXT]``: run an agent headless; without ``-m``
  each stdin line is one user turn. Text streams to stdout, tool activity to stderr.
- ``dif-general-harness console INSTANCE``: the same agent in the TUI, with approvals.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

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
    helps = {"run": "run an instance's agent headless", "console": "chat with an agent (TUI)"}
    cmd = sub.add_parser(name, help=helps[name])
    cmd.add_argument("path", type=Path, help="instance JSON")
    cmd.add_argument("--packs", type=Path, action="append", help="folder containing packs")
    cmd.add_argument("--agent", help="agent name; default: the first agent")
    if name == "run":
        cmd.add_argument("-m", "--message", help="one user message; default: read stdin lines")
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
        session = agent.resume(args.session) if args.session else agent.new_session()
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
        session = agent.resume(args.session) if args.session else agent.new_session()
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
    args = parser.parse_args(argv)

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
