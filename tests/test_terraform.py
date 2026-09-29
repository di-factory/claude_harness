"""The AWS module's own tests (M4.5): sizing profiles and hardening, planned against a mocked
AWS provider. Needs the terraform binary and an initialised provider; skipped otherwise, so
the suite stays offline."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "deploy" / "terraform" / "aws"
TERRAFORM = os.environ.get("TERRAFORM_BIN") or shutil.which("terraform")


@pytest.mark.skipif(
    TERRAFORM is None or not (MODULE / ".terraform" / "providers").is_dir(),
    reason="terraform (and `terraform init -backend=false`) is not available",
)
def test_module_profiles() -> None:
    assert TERRAFORM is not None
    done = subprocess.run(
        [TERRAFORM, f"-chdir={MODULE}", "test", "-no-color"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "0 failed" in done.stdout
