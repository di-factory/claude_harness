from __future__ import annotations

import json
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


def write_helper_solution(root: Path) -> Path:
    """A small pack (general + coding tools) and an instance of it, for runtime tests."""
    pack = root / "packs" / "helper"
    (pack / "prompts").mkdir(parents=True)
    (pack / "prompts" / "helper.md").write_text("You help {{var.owner}} with files and notes.")
    (pack / "pack.json").write_text(
        json.dumps(
            {
                "spec_version": "1",
                "kind": "pack",
                "solution": {"id": "helper", "version": "1.0.0", "lob": "general"},
                "variables": {"owner": {"type": "string", "required": True}},
                "secrets": {"llm": {"description": "Model API key"}},
                "models": {
                    "roles": {"main": {"provider": "anthropic", "model": "claude-opus-5-5"}},
                    "providers": {"anthropic": {"api_key": {"$secret": "llm"}}},
                },
                "agents": {
                    "helper": {
                        "prompt": "prompts/helper.md",
                        "model_role": "main",
                        "tools": ["notes.*", "coding.*"],
                        "workspace": "repo",
                    }
                },
                "workspaces": {"repo": {"type": "local"}},
                "tools": {"packs": ["general", "coding"]},
                "evals": {"suites": ["evals/smoke.yaml"]},
            }
        )
    )
    (pack / "evals").mkdir()
    (pack / "evals" / "smoke.yaml").write_text("cases: []\n")
    instance = root / "instances" / "acme-helper.json"
    instance.parent.mkdir()
    instance.write_text(
        json.dumps(
            {
                "spec_version": "1",
                "kind": "instance",
                "solution": {"id": "acme-helper", "version": "1.0.0", "lob": "general"},
                "extends": ["helper@^1.0"],
                "tenant": {"id": "acme", "name": "ACME"},
                "values": {"owner": "Ana"},
            }
        )
    )
    return instance


@pytest.fixture
def helper_solution(tmp_path: Path) -> Path:
    return write_helper_solution(tmp_path)
