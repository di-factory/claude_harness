from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from dif_general_harness.core.scope import Scope

EXAMPLES = Path(__file__).resolve().parent.parent / "docs" / "spec" / "examples"
PACK_IDS = [
    "pyme-appointment-agent",
    "service-desk-cell",
    "conversational-rag",
    "pyme-receipt-processing",
    "dev-cell",
    "opc-c-suite",
]


@pytest.fixture
def examples(tmp_path: Path) -> Path:
    """A private, mutable copy of the example packs and instances."""
    dest = tmp_path / "examples"
    shutil.copytree(EXAMPLES, dest)
    return dest


@pytest.fixture
def scope() -> Scope:
    return Scope(tenant_id="clinica-sonrisa", instance_id="appointments")
