"""Secret resolution (ARCHITECTURE §3.12, decision 19).

Specs never hold secret values, only references: ``{"$secret": "llm"}``. At runtime a
backend turns the name into a value, and every resolved value is registered with the
``Redactor`` so it can never reach a log, trace or memory.

Backends in M1:
- ``env``: ``DIF_SECRET_<NAME>`` (upper case, ``-`` and ``.`` become ``_``).
- ``file``: one file per secret in a directory (``<dir>/<name>``), as mounted by
  Docker/Kubernetes secrets. Trailing newlines are stripped.
Cloud vaults (AWS Secrets Manager and others) are adapters behind the same protocol (M2).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from ..policy.redact import Redactor

_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


class MissingSecret(LookupError):
    pass


class SecretBackend(Protocol):
    def get(self, name: str) -> str | None: ...


def _check(name: str) -> str:
    if not _NAME.match(name):
        raise ValueError(f"invalid secret name {name!r}")
    return name


class EnvSecrets:
    def __init__(self, environ: Mapping[str, str] | None = None, prefix: str = "DIF_SECRET_"):
        self.environ = os.environ if environ is None else environ
        self.prefix = prefix

    def key(self, name: str) -> str:
        return self.prefix + re.sub(r"[-.]", "_", _check(name)).upper()

    def get(self, name: str) -> str | None:
        return self.environ.get(self.key(name))


class FileSecrets:
    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def get(self, name: str) -> str | None:
        path = self.directory / _check(name)
        return path.read_text(encoding="utf-8").rstrip("\r\n") if path.is_file() else None


class SecretResolver:
    """Resolves ``{"$secret": name}`` references and registers every value for redaction."""

    def __init__(self, backend: SecretBackend, redactor: Redactor | None = None) -> None:
        self.backend = backend
        self.redactor = redactor or Redactor()

    def get(self, name: str) -> str:
        value = self.backend.get(name)
        if value is None or value == "":
            raise MissingSecret(f"secret {name!r} is not set")
        self.redactor.register(value)
        return value

    def resolve(self, obj: Any) -> Any:
        """Return a copy of ``obj`` with every secret reference replaced by its value."""
        if isinstance(obj, dict):
            if set(obj) == {"$secret"}:
                return self.get(str(obj["$secret"]))
            return {k: self.resolve(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.resolve(v) for v in obj]
        return obj

    def missing(self, names: list[str]) -> list[str]:
        """Names without a value; for preflight checks before a run starts."""
        return [n for n in names if not self.backend.get(n)]
