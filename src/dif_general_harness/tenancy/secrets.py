"""Secret resolution (ARCHITECTURE §3.12, decision 19).

Specs never hold secret values, only references: ``{"$secret": "llm"}``. At runtime a
backend turns the name into a value, and every resolved value is registered with the
``Redactor`` so it can never reach a log, trace or memory.

Backends in M1:
- ``env``: ``DIF_SECRET_<NAME>`` (upper case, ``-`` and ``.`` become ``_``).
- ``file``: one file per secret in a directory (``<dir>/<name>``), as mounted by
  Docker/Kubernetes secrets. Trailing newlines are stripped.
- ``aws-secrets-manager``: the client's AWS Secrets Manager, ``<prefix>/<name>``.
``backend_from_env`` picks one from ``DIF_SECRETS_BACKEND``. GCP Secret Manager and
1Password follow the same protocol.
"""

from __future__ import annotations

import os
import re
import time
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
        value = self.environ.get(self.key(name))
        return value.strip() if value is not None else None  # a pasted newline is no part of it


class FileSecrets:
    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def get(self, name: str) -> str | None:
        path = self.directory / _check(name)
        return path.read_text(encoding="utf-8").strip() if path.is_file() else None


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


class AwsSecretsManager:
    """Secrets from the client's AWS Secrets Manager: ``<prefix>/<name>``.

    The instance's IAM role grants read access to that prefix only; Di-Factory never sees
    the values. Values are cached for ``ttl_s`` so rotations are picked up without a
    restart. ``boto3`` is an optional dependency (``pip install .[aws]``); tests inject a
    client.
    """

    def __init__(
        self, prefix: str, *, client: Any = None, region: str | None = None, ttl_s: float = 300.0
    ) -> None:
        if client is None:
            import boto3  # type: ignore[import-not-found]

            client = boto3.client("secretsmanager", region_name=region)
        self.client = client
        self.prefix = prefix.rstrip("/")
        self.ttl_s = ttl_s
        self._cache: dict[str, tuple[float, str | None]] = {}

    def get(self, name: str) -> str | None:
        now = time.monotonic()
        hit = self._cache.get(name)
        if hit and now - hit[0] < self.ttl_s:
            return hit[1]
        try:
            response = self.client.get_secret_value(SecretId=f"{self.prefix}/{_check(name)}")
        except Exception as exc:
            if type(exc).__name__ == "ResourceNotFoundException" or "ResourceNotFound" in str(exc):
                value = None
            else:
                raise
        else:
            value = response.get("SecretString")
        self._cache[name] = (now, value)
        return value


class ChainSecrets:
    """The first backend that has a value wins (environment first, then files)."""

    def __init__(self, backends: list[SecretBackend]) -> None:
        self.backends = backends

    def get(self, name: str) -> str | None:
        for backend in self.backends:
            value = backend.get(name)
            if value:
                return value
        return None


def default_secrets_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Where local runs keep secret files: ``DIF_SECRETS_DIR``, else ``~/.dif/secrets``."""
    env = os.environ if environ is None else environ
    return Path(env.get("DIF_SECRETS_DIR") or Path.home() / ".dif" / "secrets")


def local_backend(environ: Mapping[str, str] | None = None) -> SecretBackend:
    """For command-line runs: ``DIF_SECRET_<NAME>`` variables, then the secrets folder, so a
    key stored once with ``dif-general-harness secrets set`` survives every new login."""
    env = os.environ if environ is None else environ
    if env.get("DIF_SECRETS_BACKEND", "env") != "env":
        return backend_from_env(env)
    return ChainSecrets([EnvSecrets(env), FileSecrets(default_secrets_dir(env))])


def backend_from_env(environ: Mapping[str, str] | None = None) -> SecretBackend:
    """``DIF_SECRETS_BACKEND``: ``env`` (default), ``file`` (``DIF_SECRETS_DIR``) or
    ``aws-secrets-manager`` (``DIF_SECRETS_PREFIX``, ``AWS_REGION``)."""
    env = os.environ if environ is None else environ
    kind = env.get("DIF_SECRETS_BACKEND", "env")
    if kind == "env":
        return EnvSecrets(env)
    if kind == "file":
        return FileSecrets(env.get("DIF_SECRETS_DIR", "/run/secrets"))
    if kind == "aws-secrets-manager":
        prefix = env.get("DIF_SECRETS_PREFIX")
        if not prefix:
            raise ValueError("DIF_SECRETS_PREFIX is required for aws-secrets-manager")
        return AwsSecretsManager(prefix, region=env.get("AWS_REGION"))
    raise ValueError(f"unknown secrets backend {kind!r}")
