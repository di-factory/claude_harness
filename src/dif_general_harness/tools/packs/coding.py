"""The ``coding`` tool pack: files and shell inside one workspace.

Guardrails:
- Every path resolves inside the workspace root; ``..`` and symlinks cannot escape it.
- Secret files (``.env*``, keys, credentials, ``.ssh``/``.aws``) are never read, written or
  listed, even inside the workspace.
- Every write and edit is checkpointed, so ``Workspace.undo`` can roll it back.
  Shell commands are not checkpointed; run them in a disposable workspace.
- ``coding.bash`` runs through an ``Executor``. M1 ships ``SubprocessExecutor`` (a clean
  environment, the workspace as cwd, a timeout that kills the process group); the container
  executor (network deny-by-default, resource limits) is M4. Its default effect is
  ``external``; a spec can lower it with ``tools.overrides``.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import os
import re
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..registry import Effect, Tool, tool

SECRET_FILES = [
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "credentials",
    "credentials.*",
    "*.credentials",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "*.tfstate",
    "*.tfstate.*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
]
SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".docker", ".kube"}
_EXAMPLES = (".example", ".sample", ".template")  # .env.example is documentation, not a secret
MAX_OUTPUT = 30_000
MAX_MATCHES = 200


class WorkspaceError(ValueError):
    pass


def is_secret_path(relative: Path) -> bool:
    if any(part in SECRET_DIRS for part in relative.parts[:-1]):
        return True
    name = relative.name.lower()
    if name.endswith(_EXAMPLES):
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_FILES)


@dataclass
class Checkpoint:
    tool: str
    path: Path
    before: bytes | None  # None: the file did not exist


@dataclass
class Workspace:
    root: Path
    checkpoints: list[Checkpoint] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        if not self.root.is_dir():
            raise WorkspaceError(f"workspace {self.root} does not exist")

    def resolve(self, path: str) -> Path:
        full = (self.root / path).resolve()
        if not full.is_relative_to(self.root):
            raise WorkspaceError(f"{path!r} is outside the workspace")
        if full != self.root and is_secret_path(full.relative_to(self.root)):
            raise WorkspaceError(f"{path!r} looks like a secret file; access is denied")
        return full

    def rel(self, full: Path) -> str:
        return full.relative_to(self.root).as_posix()

    def visible(self, full: Path) -> bool:
        try:
            resolved = full.resolve()
        except OSError:
            return False
        return resolved.is_relative_to(self.root) and not is_secret_path(
            resolved.relative_to(self.root)
        )

    def write(self, tool_name: str, full: Path, data: bytes) -> None:
        before = full.read_bytes() if full.exists() else None
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
        self.checkpoints.append(Checkpoint(tool_name, full, before))

    def undo(self) -> Checkpoint | None:
        """Roll back the last write or edit. Returns it, or None when there is nothing to undo."""
        if not self.checkpoints:
            return None
        cp = self.checkpoints.pop()
        if cp.before is None:
            cp.path.unlink(missing_ok=True)
        else:
            cp.path.write_bytes(cp.before)
        return cp


# --- executor ----------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    exit_code: int | None
    output: str
    timed_out: bool = False


class Executor(Protocol):
    async def run(self, command: str, cwd: Path, timeout_s: float) -> CommandResult: ...


class SubprocessExecutor:
    """Runs ``bash -c`` with a minimal environment: no harness secrets reach the shell."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            **(env or {}),
        }

    async def run(self, command: str, cwd: Path, timeout_s: float) -> CommandResult:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            cwd=cwd,
            env={**self.env, "HOME": str(cwd)},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # its own process group, so a timeout kills children too
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            return CommandResult(None, "", timed_out=True)
        return CommandResult(proc.returncode, out.decode("utf-8", errors="replace"))


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return f"{text[:half]}\n[... {len(text) - MAX_OUTPUT} chars omitted ...]\n{text[-half:]}"


# --- tools -------------------------------------------------------------------------


def coding_tools(
    workspace: Workspace, executor: Executor | None = None, *, bash_timeout_s: float = 120.0
) -> list[Tool]:
    ws = workspace
    runner = executor or SubprocessExecutor()

    @tool("coding.read")
    async def read(path: str, offset: int = 1, limit: int = 2000) -> str:
        """Read a text file from the workspace, with line numbers. offset is 1-based."""
        full = ws.resolve(path)
        lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(offset, 1)
        chunk = lines[start - 1 : start - 1 + limit]
        body = "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(chunk, start))
        if start - 1 + limit < len(lines):
            body += f"\n[{len(lines) - (start - 1 + limit)} more lines; use offset]"
        return _clip(body)

    @tool("coding.write", effect=Effect.WRITE)
    async def write(path: str, content: str) -> str:
        """Create or overwrite a file in the workspace."""
        full = ws.resolve(path)
        ws.write("coding.write", full, content.encode("utf-8"))
        return f"wrote {ws.rel(full)} ({len(content)} chars)"

    @tool("coding.edit", effect=Effect.WRITE)
    async def edit(path: str, old: str, new: str, replace_all: bool = False) -> str:
        """Replace exact text in a file. old must match once unless replace_all is true."""
        full = ws.resolve(path)
        text = full.read_text(encoding="utf-8")
        count = text.count(old) if old else 0
        if count == 0:
            raise WorkspaceError("old text not found")
        if count > 1 and not replace_all:
            raise WorkspaceError(f"old text matches {count} times; add context or use replace_all")
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        ws.write("coding.edit", full, updated.encode("utf-8"))
        return f"edited {ws.rel(full)} ({count if replace_all else 1} replacement(s))"

    @tool("coding.glob")
    async def glob(pattern: str) -> list[str]:
        """List workspace files matching a glob such as src/**/*.py."""
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise WorkspaceError("glob patterns are relative to the workspace")
        hits = sorted(ws.rel(p) for p in ws.root.glob(pattern) if p.is_file() and ws.visible(p))
        return hits[:MAX_MATCHES] + (
            [f"[{len(hits) - MAX_MATCHES} more]"] if len(hits) > MAX_MATCHES else []
        )

    @tool("coding.grep")
    async def grep(pattern: str, glob: str = "**/*", ignore_case: bool = False) -> list[str]:
        """Search workspace files for a regular expression. Returns path:line: text."""
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        if Path(glob).is_absolute() or ".." in Path(glob).parts:
            raise WorkspaceError("glob patterns are relative to the workspace")
        out: list[str] = []
        for p in sorted(ws.root.glob(glob)):
            if not p.is_file() or not ws.visible(p) or ".git" in p.relative_to(ws.root).parts:
                continue
            data = p.read_bytes()
            if b"\0" in data[:4096]:
                continue  # binary
            for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{ws.rel(p)}:{n}: {line[:300]}")
                    if len(out) >= MAX_MATCHES:
                        return [*out, "[more matches omitted]"]
        return out

    @tool("coding.bash", effect=Effect.EXTERNAL, timeout_s=bash_timeout_s + 5)
    async def bash(command: str) -> dict[str, object]:
        """Run a shell command in the workspace root. Output is stdout and stderr combined."""
        result = await runner.run(command, ws.root, bash_timeout_s)
        if result.timed_out:
            raise TimeoutError(f"command timed out after {bash_timeout_s}s")
        return {"exit_code": result.exit_code, "output": _clip(result.output)}

    return [read, write, edit, glob, grep, bash]
