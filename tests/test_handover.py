"""Handing a client their solution: the documents their own Claude starts from."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dif_general_harness.cli import main
from dif_general_harness.constructor import build
from dif_general_harness.constructor.handover import SKILLS, build_handover
from dif_general_harness.spec import PackCatalog
from tests.test_constructor import CLINIC, _flat

URL = "https://3-148-79-116.sslip.io"


@pytest.fixture
def client(examples: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DIF_SECRETS_DIR", raising=False)
    (tmp_path / "home").mkdir()
    built = build(PackCatalog(roots=[examples]), ["pyme-appointment-agent"],
                  tmp_path / "clients", answers=_flat(CLINIC))  # fmt: skip
    assert built.ok
    return built.spec_path


def test_the_documents_a_clients_claude_starts_from(
    examples: Path, tmp_path: Path, client: Path
) -> None:
    deploy = tmp_path / "harness" / "deploy" / "build"
    secrets = deploy / "clinica-sonrisa-pyme-appointment-agent" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "anthropic").write_text("sk-ant-api03-x")
    (secrets / "admin_token").write_text("t" * 48)
    out = tmp_path / "home" / "sonrisa"
    result = build_handover(
        client, PackCatalog(roots=[examples]), out, url=URL, owner="Roberta", lang="es",
        support="soporte@di-factory.mx", harness=tmp_path / "harness",
    )  # fmt: skip
    files = {p.relative_to(out).as_posix() for p in result.files}
    assert {"CLAUDE.md", "GUIA.md", "HANDOVER.md", "negocio", ".claude/settings.json"} <= files
    assert {f"docs/{d}.md" for d in ("negocio", "solucion", "operacion", "pendientes",
                                     "cambios")} <= files  # fmt: skip
    assert all(f".claude/skills/{s}/SKILL.md" in files for s in SKILLS)

    claude = (out / "CLAUDE.md").read_text()
    assert "You help **Roberta**" in claude and "in **Spanish**" in claude
    assert f"{URL}/chat" in claude and "./negocio inbox" in claude
    assert "Never send anything to a customer without Roberta's explicit OK" in claude
    assert "Never read, print or move secrets" in claude and "soporte@di-factory.mx" in claude

    business = (out / "docs" / "negocio.md").read_text()
    assert business.startswith("# Clínica Sonrisa") and "## What is the business about?" in business
    pending = (out / "docs" / "pendientes.md").read_text()
    assert "## twilio" in pending and "customers cannot reach the agent there" in pending
    assert "## anthropic" not in pending  # present in the deployed secrets: not missing
    solution = (out / "docs" / "solucion.md").read_text()
    assert "web chat (public)" in solution and "Escalates to a person when" in solution
    assert "Spend limit: $5" in solution
    assert "Personal data in conversations is tokenized" in solution

    guide = (out / "GUIA.md").read_text()
    assert guide.startswith("# Guía rápida") and "cd ~/sonrisa" in guide
    wrapper = out / "negocio"
    assert os.access(wrapper, os.X_OK)
    assert f'--project "{(tmp_path / "harness").resolve()}" dif-general-harness admin' in (
        wrapper.read_text()
    )
    settings = json.loads((out / ".claude" / "settings.json").read_text())["permissions"]
    assert "Read(~/.dif/**)" in settings["deny"] and "Bash(docker:*)" in settings["deny"]
    assert "Bash(./negocio reply:*)" in settings["ask"]  # sending to a customer: always asked
    assert "Bash(./negocio status)" in settings["allow"]
    skill = (out / ".claude" / "skills" / "responder" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: responder\n") and "Only after a clear yes" in skill
    assert "Bash(./negocio faq gaps:*)" in settings["allow"]  # reading the list: free
    assert "Bash(./negocio faq done:*)" in settings["ask"]  # marking it: with the owner's OK
    gaps = (out / ".claude" / "skills" / "preguntas" / "SKILL.md").read_text()
    assert "./negocio faq gaps" in gaps and "never invent it" in gaps
    assert "## Questions the FAQ did not answer" in (out / "docs" / "operacion.md").read_text()

    checks = dict((what.split(" (")[0], ok) for ok, what in result.checks)
    assert checks["Jag's approver key is not on this server"] is True
    assert checks["the admin token is in place"] is True
    assert "- [x] Jag's approver key is not on this server" in (out / "HANDOVER.md").read_text()


def test_a_signing_key_left_on_the_server_blocks_the_handover(
    examples: Path, tmp_path: Path, client: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keys = tmp_path / "home" / ".dif" / "keys"
    keys.mkdir(parents=True)
    (keys / "jag.key").write_text("secret")
    code = main(["handover", str(client), "--packs", str(examples), "--url", URL,
                 "--owner", "Roberta", "--lang", "es"])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 1 and "✗ Jag's approver key is not on this server" in out
    assert (tmp_path / "home" / "clinica-sonrisa" / "CLAUDE.md").exists()  # default: ~/<tenant>
