"""Constructor v2 (M2.5): staging, Jag's signed approval, deploy plans."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.cli import database_url_from_env, main
from dif_general_harness.constructor.deploy import (
    DeployError,
    approve,
    check_approval,
    new_key,
    plan_aws,
    plan_docker,
    solution_hash,
    stage,
)
from dif_general_harness.spec import PackCatalog


def _clinic(examples: Path) -> tuple[Path, PackCatalog]:
    return examples / "instances" / "clinica-sonrisa.json", PackCatalog(roots=[examples])


def test_stage_is_self_contained_and_deterministic(examples: Path, tmp_path: Path) -> None:
    instance, catalog = _clinic(examples)
    staged = stage(instance, catalog, tmp_path / "a")
    assert staged.ok and staged.spec.solution.id == "clinica-sonrisa-appointments"
    files = sorted(
        p.relative_to(tmp_path / "a").as_posix() for p in (tmp_path / "a").rglob("*") if p.is_file()
    )
    assert "instance.json" in files
    assert "clinica-sonrisa/tpl_reminder.es-MX.md" in files  # the Spanish override travels along
    assert "packs/pyme-appointment-agent/pack.json" in files
    assert not any(f.startswith("packs/dev-cell") for f in files)  # only the packs it extends
    stage(instance, catalog, tmp_path / "b")
    assert solution_hash(tmp_path / "a") == solution_hash(tmp_path / "b")

    prompt = examples / "pyme-appointment-agent" / "prompts" / "receptionist.md"
    prompt.write_text(prompt.read_text() + "\nAlways upsell whitening.\n")
    stage(instance, catalog, tmp_path / "c")
    assert solution_hash(tmp_path / "c") != solution_hash(tmp_path / "a")  # one prompt line


def test_stage_carries_extension_code(examples: Path, tmp_path: Path) -> None:
    instance, catalog = _clinic(examples)
    raw = json.loads(instance.read_text())
    raw["tools"] = {"python": ["extensions.local:hello"]}
    (instance.parent / "extensions").mkdir()
    (instance.parent / "extensions" / "local.py").write_text(
        "async def hello() -> str:\n    return 'hi'\n"
    )
    local = instance.parent / "local.json"
    local.write_text(json.dumps(raw))
    stage(local, catalog, tmp_path / "a")
    files = {p.relative_to(tmp_path / "a").as_posix() for p in (tmp_path / "a").rglob("*.py")}
    assert files == {"extensions/local.py", "packs/pyme-appointment-agent/extensions/slots.py"}
    before = solution_hash(tmp_path / "a")
    (instance.parent / "extensions" / "local.py").write_text(
        "async def hello() -> str:\n    return 'pwned'\n"
    )
    stage(local, catalog, tmp_path / "b")
    assert solution_hash(tmp_path / "b") != before  # the signature covers extension code


def test_approval_gate(examples: Path, tmp_path: Path) -> None:
    instance, catalog = _clinic(examples)
    jag_key, jag_public = new_key(tmp_path / "keys", "jag")
    assert oct(jag_key.stat().st_mode)[-3:] == "600"
    mallory_key, _ = new_key(tmp_path / "keys", "mallory")
    approvers = {"jag": jag_public}
    folder = tmp_path / "solution"
    staged = stage(instance, catalog, folder)

    record = approve(staged, folder, "aws", jag_key, "jag")
    check_approval(record, folder, "aws", approvers)  # passes

    with pytest.raises(DeployError, match=r"not aws|not 'docker'"):
        check_approval(record, folder, "docker", approvers)
    with pytest.raises(DeployError, match="not a trusted approver"):
        check_approval(
            approve(staged, folder, "aws", mallory_key, "mallory"), folder, "aws", approvers
        )
    forged = approve(staged, folder, "aws", mallory_key, "jag")  # mallory signs as jag
    with pytest.raises(DeployError, match="does not verify"):
        check_approval(forged, folder, "aws", approvers)
    edited = {**record, "target": "docker"}
    with pytest.raises(DeployError, match="does not verify"):
        check_approval(edited, folder, "docker", approvers)

    (folder / "instance.json").write_text(
        (folder / "instance.json").read_text().replace("24", "48")
    )
    with pytest.raises(DeployError, match="changed since it was approved"):
        check_approval(record, folder, "aws", approvers)


def test_docker_plan(examples: Path, tmp_path: Path) -> None:
    instance, catalog = _clinic(examples)
    staged = stage(instance, catalog, tmp_path / "solution")
    plan = plan_docker(staged, tmp_path)
    compose = json.loads((tmp_path / "docker-compose.yml").read_text())
    app = compose["services"]["instance"]
    assert app["environment"]["DIF_SECRETS_BACKEND"] == "file"
    assert app["build"]["dockerfile"] == "deploy/docker/Dockerfile"
    assert compose["services"]["db"]["image"] == "postgres:16"
    assert set(plan.secrets) == {"anthropic", "twilio", "google", "admin_token"}
    assert "- twilio:" in (tmp_path / "secrets" / "README.md").read_text()
    assert plan.commands[0][:3] == ["docker", "compose", "-f"]


def test_aws_plan_respects_the_data_region(examples: Path, tmp_path: Path) -> None:
    instance, catalog = _clinic(examples)
    staged = stage(instance, catalog, tmp_path / "solution")
    plan = plan_aws(staged, tmp_path)
    tfvars = json.loads((tmp_path / "terraform.tfvars.json").read_text())
    assert tfvars["region"] == "mx-central-1" and tfvars["tenant_id"] == "clinica-sonrisa"
    digest = solution_hash(tmp_path / "solution")[:12]
    assert tfvars["image_tag"] == f"1.0.0-{digest}"  # a config change is a new, immutable image
    assert any(part.endswith(f":1.0.0-{digest}") for cmd in plan.commands for part in cmd)
    assert "admin_token" in tfvars["secret_names"] and "twilio" in tfvars["secret_names"]
    assert any("{ecr}" in part for cmd in plan.commands for part in cmd)
    assert plan.commands[-1][-1].startswith("-var-file=")

    staged.spec.deploy.profile = "huge"  # type: ignore[union-attr]
    with pytest.raises(DeployError, match="small, medium or large"):
        plan_aws(staged, tmp_path)
    staged.spec.deploy.profile = "large"  # type: ignore[union-attr]
    staged.spec.deploy.region = "us-east-1"  # type: ignore[union-attr]
    with pytest.raises(DeployError, match="outside the allowed data region"):
        plan_aws(staged, tmp_path)
    staged.spec.deploy = None
    with pytest.raises(DeployError, match=r"deploy\.target"):
        plan_aws(staged, tmp_path)


def test_cli_approve_then_deploy(
    examples: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    instance, _ = _clinic(examples)
    packs = ["--packs", str(examples)]
    assert main(["keys", "new", "jag", "--out", str(tmp_path / "keys")]) == 0
    public = capsys.readouterr().out.split('"jag": "')[1].split('"')[0]
    approvers = tmp_path / "approvers.json"
    approvers.write_text(json.dumps({"jag": public}))
    out = tmp_path / "build"
    deploy = [
        "deploy",
        str(instance),
        *packs,
        "--target",
        "docker",
        "--approvers",
        str(approvers),
        "--out",
        str(out),
    ]

    assert main(deploy) == 3  # nothing approved yet
    assert "Jag approves every deploy" in capsys.readouterr().err

    assert (
        main(
            [
                "approve",
                str(instance),
                *packs,
                "--target",
                "docker",
                "--key",
                str(tmp_path / "keys" / "jag.key"),
                "--by",
                "jag",
            ]
        )
        == 0
    )
    assert main(deploy) == 0
    printed = capsys.readouterr().out
    assert "docker compose" in printed and "- twilio:" in printed
    assert (out / "docker-compose.yml").exists() and (out / "solution" / "instance.json").exists()

    values: dict[str, Any] = json.loads(instance.read_text())
    values["values"]["reminder_hours"] = 48  # a change after approval
    instance.write_text(json.dumps(values))
    assert main(deploy) == 3
    assert "changed since it was approved" in capsys.readouterr().err


def test_database_url_from_parts() -> None:
    assert database_url_from_env({}) is None
    url = database_url_from_env({"DIF_DB_HOST": "db.internal", "DIF_DB_PASSWORD": "p@ss/w:rd"})
    assert url == "postgresql://dif:p%40ss%2Fw%3Ard@db.internal:5432/dif?sslmode=require"
