"""Constructor v2 (ARCHITECTURE §0.4 steps 6-7): approve, then deploy.

**Staging.** A deployable solution is a folder: ``instance.json``, the files it references
(e.g. Spanish template overrides) and the packs it extends. ``stage`` builds it
deterministically, and ``solution_hash`` hashes every file in it (paths and contents).

**Approval (decision 37: only Jag approves deployments).** ``approve`` signs, with the
approver's Ed25519 key, exactly one staged solution for one target. ``check_approval``
refuses a deploy when the signature does not verify against a trusted approver key, or when
the solution changed in any byte since it was approved (a prompt, a pack file, a value).

**Deploy targets.**
- ``docker``: the staged solution, a ``docker-compose.yml`` (the instance plus Postgres) and a
  ``secrets/`` folder for the client to fill in (one file per secret).
- ``aws``: the staged solution and ``terraform.tfvars.json`` for ``deploy/terraform/aws``
  (ECS Fargate, RDS Postgres, Secrets Manager, one region), plus the exact commands.

Secret values never pass through here: the client stores them in their own vault.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ..spec.loader import PackCatalog, ResolvedSpec, load_instance

REPO_ROOT = Path(__file__).resolve().parents[3]
TARGETS = ("docker", "aws")
ECR = "{ecr}"  # filled from `terraform output ecr_repository` once the repository exists
SERVICE_SECRETS = {"admin_token": "Bearer token for the admin API (inbox, config, consent)"}


class DeployError(RuntimeError):
    pass


# --- staging -----------------------------------------------------------------------


def _references(value: Any, base: Path) -> set[Path]:
    """Files an instance points at, relative to its folder."""
    found: set[Path] = set()
    if isinstance(value, str) and value and "{{" not in value and len(value) < 300:
        candidate = (base / value).resolve()
        if candidate.is_file() and candidate.is_relative_to(base.resolve()):
            found.add(candidate)
    elif isinstance(value, dict):
        for v in value.values():
            found |= _references(v, base)
    elif isinstance(value, list):
        for v in value:
            found |= _references(v, base)
    return found


def stage(instance_file: Path, catalog: PackCatalog, out: Path) -> ResolvedSpec:
    """Build ``out`` as a self-contained solution folder and check it resolves the same."""
    resolved = load_instance(instance_file, catalog)
    if not resolved.ok:
        errors = "; ".join(
            f"{i.path}: {i.message}" for i in resolved.issues if i.severity == "error"
        )
        raise DeployError(f"the instance does not validate: {errors}")
    if out.exists():
        shutil.rmtree(out)
    (out / "packs").mkdir(parents=True)
    base = instance_file.parent
    shutil.copyfile(instance_file, out / "instance.json")
    raw = json.loads(instance_file.read_text(encoding="utf-8"))
    for ref in sorted(_references(raw, base)):
        target = out / ref.relative_to(base.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ref, target)
    for pack_json in resolved.layers[:-1]:
        pack_dir = pack_json.parent
        shutil.copytree(
            pack_dir,
            out / "packs" / pack_dir.name,
            ignore=shutil.ignore_patterns("__pycache__", ".*"),
        )
    staged = load_instance(out / "instance.json", PackCatalog(roots=[out / "packs"]))
    if not staged.ok:
        raise DeployError("the staged solution does not validate on its own")
    return staged


def solution_hash(folder: Path) -> str:
    """Every file's path and content: any change, however small, changes the hash."""
    digest = hashlib.sha256()
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        digest.update(path.relative_to(folder).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


# --- keys and approvals ------------------------------------------------------------


def new_key(out_dir: Path, name: str) -> tuple[Path, str]:
    """A new approver key: the private key file (0600) and the public key (base64)."""
    key = Ed25519PrivateKey.generate()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.key"
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return path, base64.b64encode(public).decode()


def _approval_message(record: dict[str, Any]) -> bytes:
    body = {k: v for k, v in record.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def approve(
    staged: ResolvedSpec, folder: Path, target: str, key_file: Path, approved_by: str
) -> dict[str, Any]:
    if target not in TARGETS:
        raise DeployError(f"unknown target {target!r}")
    key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise DeployError("approver keys are Ed25519")
    spec = staged.spec
    record: dict[str, Any] = {
        "instance_id": spec.solution.id,
        "tenant_id": spec.tenant.id if spec.tenant else "local",
        "target": target,
        "solution_hash": solution_hash(folder),
        "approved_by": approved_by,
        "approved_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    record["signature"] = base64.b64encode(key.sign(_approval_message(record))).decode()
    return record


def check_approval(
    record: dict[str, Any], folder: Path, target: str, approvers: dict[str, str]
) -> None:
    """Raise ``DeployError`` unless ``record`` approves exactly this solution for ``target``."""
    who = str(record.get("approved_by", ""))
    if who not in approvers:
        raise DeployError(f"{who or 'nobody'} is not a trusted approver")
    try:
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(approvers[who]))
        public.verify(base64.b64decode(str(record.get("signature", ""))), _approval_message(record))
    except (InvalidSignature, ValueError):
        raise DeployError("the approval signature does not verify") from None
    if record.get("target") != target:
        raise DeployError(f"approved for {record.get('target')!r}, not {target!r}")
    if record.get("solution_hash") != solution_hash(folder):
        raise DeployError("the solution changed since it was approved; approve it again")


# --- targets -----------------------------------------------------------------------


@dataclass
class DeployPlan:
    target: str
    folder: Path
    files: list[Path]
    commands: list[list[str]]
    secrets: dict[str, str]


def required_secrets(staged: ResolvedSpec) -> dict[str, str]:
    out = {name: decl.description for name, decl in staged.spec.secrets.items()}
    return {**out, **SERVICE_SECRETS}


def plan_docker(staged: ResolvedSpec, out: Path, *, image: str | None = None) -> DeployPlan:
    sol = staged.spec.solution
    image = image or f"dif/{sol.id}:{sol.version}"
    secrets = required_secrets(staged)
    (out / "secrets").mkdir(exist_ok=True)
    readme = ["One file per secret, named exactly as below, holding only the value.", ""]
    readme += [f"- {name}: {desc}" for name, desc in sorted(secrets.items())]
    (out / "secrets" / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")
    compose = {
        "services": {
            "instance": {
                "image": image,
                "build": {
                    "context": str(REPO_ROOT),
                    "dockerfile": "deploy/docker/Dockerfile",
                    "args": {"SOLUTION": os.path.relpath(out / "solution", REPO_ROOT)},
                },
                "environment": {
                    "DIF_SECRETS_BACKEND": "file",
                    "DIF_SECRETS_DIR": "/run/dif-secrets",
                    "DIF_DATABASE_URL": "postgresql://dif:dif@db:5432/dif",
                },
                "volumes": ["./secrets:/run/dif-secrets:ro"],
                "ports": ["8080:8080"],
                "depends_on": {"db": {"condition": "service_healthy"}},
                "restart": "unless-stopped",
            },
            "db": {
                "image": "postgres:16",
                "environment": {
                    "POSTGRES_USER": "dif",
                    "POSTGRES_PASSWORD": "dif",
                    "POSTGRES_DB": "dif",
                },
                "volumes": ["pgdata:/var/lib/postgresql/data"],
                "healthcheck": {
                    "test": ["CMD", "pg_isready", "-U", "dif"],
                    "interval": "5s",
                    "retries": 10,
                },
                "restart": "unless-stopped",
            },
        },
        "volumes": {"pgdata": {}},
    }
    compose_file = out / "docker-compose.yml"
    compose_file.write_text(json.dumps(compose, indent=2) + "\n", encoding="utf-8")  # JSON is YAML
    return DeployPlan(
        "docker",
        out,
        [compose_file, out / "secrets" / "README.md"],
        [["docker", "compose", "-f", str(compose_file), "up", "-d", "--build"]],
        secrets,
    )


def plan_aws(staged: ResolvedSpec, out: Path) -> DeployPlan:
    spec = staged.spec
    deploy = spec.deploy
    if deploy is None or deploy.target != "aws" or not deploy.region:
        raise DeployError("deploy.target must be 'aws' with a region in the spec")
    allowed = spec.governance.regions.get("data")
    if allowed and not deploy.region.startswith(f"{allowed}-"):
        raise DeployError(f"region {deploy.region} is outside the allowed data region {allowed!r}")
    name = spec.solution.id
    secrets = required_secrets(staged)
    tfvars = {
        "name": name,
        "region": deploy.region,
        "image_tag": spec.solution.version,
        "secret_names": sorted(secrets),
        "tenant_id": spec.tenant.id if spec.tenant else "local",
        "size": deploy.profile or "small",
    }
    tf_file = out / "terraform.tfvars.json"
    tf_file.write_text(json.dumps(tfvars, indent=2) + "\n", encoding="utf-8")
    tf = ["terraform", f"-chdir={REPO_ROOT / 'deploy' / 'terraform' / 'aws'}"]
    solution = os.path.relpath(out / "solution", REPO_ROOT)
    image = f"{ECR}:{spec.solution.version}"
    login = (
        f"aws ecr get-login-password --region {deploy.region}"
        f" | docker login --username AWS --password-stdin {ECR.split('/')[0]}"
    )
    commands = [
        [*tf, "init"],
        [*tf, "apply", f"-var-file={tf_file}", "-target=aws_ecr_repository.instance"],
        ["sh", "-c", login],
        ["docker", "build", "-f", "deploy/docker/Dockerfile", "--build-arg",
         f"SOLUTION={solution}", "-t", image, "."],
        ["docker", "push", image],
        [*tf, "apply", f"-var-file={tf_file}"],
    ]  # fmt: skip
    return DeployPlan("aws", out, [tf_file], commands, secrets)
