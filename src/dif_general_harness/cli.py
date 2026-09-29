"""Command line: ``dif-general-harness spec validate|resolve PATH``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .spec import PackCatalog, ResolvedSpec, SpecError, load_instance, load_pack


def _load(path: Path, packs: list[Path] | None) -> ResolvedSpec:
    target = path / "pack.json" if path.is_dir() else path
    kind = json.loads(target.read_text(encoding="utf-8")).get("kind")
    if kind == "pack":
        return load_pack(target)
    roots = packs or [target.parent.parent, target.parent]
    return load_instance(target, PackCatalog(roots=[r for r in roots if r.exists()]))


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)

    try:
        resolved = _load(args.path, args.packs)
    except SpecError as exc:
        for issue in exc.issues:
            print(issue, file=sys.stderr)
        return 2

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
