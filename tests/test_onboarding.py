"""Onboarding a client: a questionnaire in, a valid instance and its FAQ out; secrets by command."""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from dif_general_harness.cli import main
from dif_general_harness.constructor import questionnaire
from dif_general_harness.spec import PackCatalog
from dif_general_harness.tenancy import local_backend

PACK = "pyme-appointment-agent"


def _fill(path: Path, answers: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8")
    for key, value in answers.items():
        text, n = re.subn(rf"^(\s*){key}:.*$", rf"\g<1>{key}: {value}", text, count=1, flags=re.M)
        assert n, key
    path.write_text(text, encoding="utf-8")


def test_each_side_gets_its_own_questions(examples: Path) -> None:
    catalog = PackCatalog(roots=[examples])
    client = questionnaire(catalog, [PACK], "client")
    assert "business_name:" in client and "about:" in client and "main_model:" not in client
    assert "#   e.g. A family dental clinic" in client  # examples guide the answers
    assert 'timezone: "America/Mexico_City"' in client  # defaults are prefilled
    ours = questionnaire(catalog, [PACK], "difactory")
    assert "main_model:" in ours and "tpl_reminder_id:" in ours
    assert "about:" not in ours and "business_name:" not in ours


def test_from_questionnaires_to_a_running_client(
    examples: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packs = ["--packs", str(examples)]
    client, ours = tmp_path / "client.yaml", tmp_path / "difactory.yaml"
    assert (
        main(["questionnaire", "--pack", PACK, *packs, "--for", "client", "--out", str(client)])
        == 0
    )
    assert (
        main(["questionnaire", "--pack", PACK, *packs, "--for", "difactory", "--out", str(ours)])
        == 0
    )
    _fill(client, {
        "name": '"Clínica Dental Luna"', "id": "dental-luna", "locale": "es-MX",
        "business_name": '"Clínica Dental Luna"',
        "business_hours": '"mon-fri: 09:00-19:00; sat: 09:00-14:00"',
        "whatsapp_number": '"+525512345678"', "calendar_ids": '"dra-luna@example.com"',
        "ops_email": "recepcion@luna.mx",
        "about": '"Clínica dental familiar en Coyoacán."',
        "services": '"Limpieza, resinas y ortodoncia."',
        "location": '"Av. Universidad 123, Coyoacán."',
    })  # fmt: skip
    _fill(ours, {"main_model": "claude-sonnet-5-5", "fast_model": "claude-haiku-4-5",
                 "tpl_reminder_id": "HXa", "tpl_nudge_id": "HXb"})  # fmt: skip
    out = tmp_path / "clients" / "luna"
    build = ["build", "--pack", PACK, *packs, "--answers", str(client), "--answers", str(ours)]
    assert main([*build, "--out", str(out)]) == 0  # Di-Factory's blank client fields erase nothing
    assert "OK: instance built and validated" in capsys.readouterr().out
    instance = out / "dental-luna-pyme-appointment-agent.json"
    faq = (out / "dental-luna-pyme-appointment-agent.knowledge" / "clinic_faq.md").read_text()
    assert faq.startswith("# Clínica Dental Luna") and "## Where are you located?" in faq

    secrets = tmp_path / "secrets"
    assert main(["secrets", "check", str(instance), *packs, "--dir", str(secrets)]) == 1
    out = capsys.readouterr().out
    assert "✗ anthropic: not set" in out
    assert "without it: the agent cannot answer at all" in out  # what it means
    assert "how to get it: console.anthropic.com" in out
    env_file = tmp_path / ".env"
    env_file.write_text('export DIF_SECRET_ANTHROPIC="sk-ant-api03-' + "k" * 90 + '"\n')
    for name in ("anthropic", "google", "twilio"):
        args = ["secrets", "set", name, "--dir", str(secrets)]
        if name == "anthropic":
            assert main([*args, "--from-env-file", str(env_file)]) == 0
        else:
            (secrets / name).write_text("x\n")
    assert (secrets / "anthropic").stat().st_mode & 0o777 == 0o600
    assert main(["secrets", "check", str(instance), *packs, "--dir", str(secrets)]) == 0
    assert "anthropic: 103 characters, starts with sk-ant-api" in capsys.readouterr().out


def test_secrets_set_refuses_what_cannot_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["secrets", "set", "anthropic", "--dir", str(tmp_path)]
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-ant-oat01-abc\n"))
    assert main(args) == 2 and "subscription (OAuth) token" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert main(args) == 2 and "nothing was received" in capsys.readouterr().err
    twice = "sk-ant-api03-" + "k" * 90
    monkeypatch.setattr("sys.stdin", io.StringIO(twice * 2 + "\n"))
    assert main(args) == 2 and "holds 2 keys one after another" in capsys.readouterr().err
    assert not (tmp_path / "anthropic").exists()
    monkeypatch.setattr("sys.stdin", io.StringIO("ACxx:token\n"))
    assert main(["secrets", "set", "twilio", "--dir", str(tmp_path)]) == 0
    assert (tmp_path / "twilio").read_text() == "ACxx:token"


def test_local_runs_find_stored_secrets_after_a_new_login(tmp_path: Path) -> None:
    (tmp_path / "anthropic").write_text("from-file")
    backend = local_backend({"DIF_SECRETS_DIR": str(tmp_path)})
    assert backend.get("anthropic") == "from-file"
    env = {"DIF_SECRETS_DIR": str(tmp_path), "DIF_SECRET_ANTHROPIC": "from-env"}
    assert local_backend(env).get("anthropic") == "from-env"  # a variable still wins
    assert local_backend({"DIF_SECRETS_DIR": str(tmp_path)}).get("twilio") is None


def test_a_dotenv_file_in_the_project_is_read(tmp_path: Path) -> None:
    from dif_general_harness.tenancy import load_env_file

    (tmp_path / ".env").write_text(
        "# keys\nexport DIF_SECRET_ANTHROPIC='sk-ant-api03-x'\nDIF_SECRET_TWILIO=AC1:tok\n"
        "DIF_SECRET_LLM=from-file\nnot a line\n"
    )
    env = {"DIF_SECRET_LLM": "already-set"}
    loaded = load_env_file(tmp_path / ".env", env)
    assert loaded == ["DIF_SECRET_ANTHROPIC", "DIF_SECRET_TWILIO"]
    assert (
        env["DIF_SECRET_ANTHROPIC"] == "sk-ant-api03-x" and env["DIF_SECRET_LLM"] == "already-set"
    )
    assert load_env_file(tmp_path / "missing.env", env) == []
