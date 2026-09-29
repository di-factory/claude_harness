"""Command line.

- ``dif-general-harness spec validate|resolve PATH``
- ``dif-general-harness run INSTANCE [-m TEXT]``: run an agent headless; without ``-m``
  each stdin line is one user turn. Text streams to stdout, tool activity to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .core.events import ErrorEvent, TextDelta, ToolCallFinished, ToolCallStarted, TurnEnded
from .policy import AutoApprover
from .providers.base import ModelProvider
from .runtime import Instance, InstanceError, PromptError, RoutingError, RuntimeOptions
from .spec import PackCatalog, ResolvedSpec, SpecError, load_instance, load_pack
from .tenancy import EnvSecrets, FileSecrets


def _load(path: Path, packs: list[Path] | None) -> ResolvedSpec:
    target = path / "pack.json" if path.is_dir() else path
    kind = json.loads(target.read_text(encoding="utf-8")).get("kind")
    if kind == "pack":
        return load_pack(target)
    roots = packs or [target.parent.parent, target.parent]
    return load_instance(target, PackCatalog(roots=[r for r in roots if r.exists()]))


def _add_run(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    cmd = sub.add_parser("run", help="run an instance's agent headless")
    cmd.add_argument("path", type=Path, help="instance JSON")
    cmd.add_argument("--packs", type=Path, action="append", help="folder containing packs")
    cmd.add_argument("--agent", help="agent name; default: the first agent")
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


async def _run(
    args: argparse.Namespace, resolved: ResolvedSpec, provider: ModelProvider | None
) -> int:
    workspaces = {}
    for item in args.workspace:
        name, sep, folder = item.partition("=")
        if not sep:
            print(f"--workspace expects NAME=DIR, got {item!r}", file=sys.stderr)
            return 2
        workspaces[name] = Path(folder)
    options = RuntimeOptions(
        state_root=args.state,
        secrets=FileSecrets(args.secrets_dir) if args.secrets_dir else EnvSecrets(),
        approver=AutoApprover() if args.yes else None,
        workspaces=workspaces,
        provider=provider,
    )
    try:
        instance = await Instance.open(resolved, options)
    except (InstanceError, RoutingError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2
    async with instance:
        for issue in instance.issues:
            print(issue, file=sys.stderr)
        try:
            agent = instance.agent(args.agent)
        except (KeyError, PromptError) as exc:
            print(f"cannot start: {exc}", file=sys.stderr)
            return 2
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
    _add_run(sub)
    args = parser.parse_args(argv)

    try:
        resolved = _load(args.path, args.packs)
    except SpecError as exc:
        for issue in exc.issues:
            print(issue, file=sys.stderr)
        return 2

    if args.group == "run":
        return asyncio.run(_run(args, resolved, provider))
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
