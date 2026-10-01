"""The guided setup (./setup.sh): a fresh clone to a tested, signed, ready-to-serve client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.cli import main
from dif_general_harness.constructor import pack_questions
from dif_general_harness.constructor.deploy import check_approval
from dif_general_harness.constructor.setup import Setup
from dif_general_harness.core.messages import Message
from dif_general_harness.providers import FakeProvider
from dif_general_harness.spec import PackCatalog
from tests.test_constructor import CLINIC, _flat

PACK = "pyme-appointment-agent"
KEY = "sk-ant-api03-" + "k" * 90


def _replies(examples: Path) -> list[str]:
    """What a person types, in order: the client's answers (Di-Factory's models are defaults;
    the template ids are left blank, so they become pending)."""
    flat = _flat(CLINIC)
    skip = {"values.main_model", "values.fast_model"}
    out = []
    for q in pack_questions(PackCatalog(roots=[examples]), [PACK]):
        if q.key in skip:
            continue
        if q.key in {"values.tpl_reminder_id", "values.tpl_nudge_id"}:
            out.append("")  # Di-Factory does not know them yet
            continue
        value = flat.get(q.key, "")
        out.append(value if isinstance(value, str) else json.dumps(value))
    return out


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DIF_SECRETS_DIR", raising=False)
    monkeypatch.delenv("DIF_SECRET_ANTHROPIC", raising=False)
    return tmp_path / "home"


def test_from_nothing_to_signed_and_staged(
    examples: Path, tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replies = iter([
        "dental appointments on WhatsApp", "1",  # what the client needs, which pack
        *_replies(examples),
        "y",  # serve it online
    ])  # fmt: skip
    secret_prompts = iter(["", KEY])  # a paste that did not arrive, then the key
    visible = iter([""])  # ...nor in the visible fallback: asked again
    provider = FakeProvider([Message.assistant("Somos una clínica dental familiar.")])

    def ask(prompt: str) -> str:
        if prompt == "API key: ":
            return next(visible)
        return next(replies)

    setup = Setup(
        [examples], tmp_path / "clients", ask=ask, ask_secret=lambda _: next(secret_prompts),
        run=lambda argv: main(argv, provider=provider),
        public_url="https://3-148-79-116.sslip.io", root=tmp_path / "repo",
    )  # fmt: skip
    assert setup.run_all() == 0
    out = capsys.readouterr().out

    stored = home / ".dif" / "secrets" / "anthropic"
    assert stored.read_text() == KEY and stored.stat().st_mode & 0o777 == 0o600
    assert "Somos una clínica dental familiar." in out  # the real test question went through
    assert provider.requests[0].messages[-1].text() == "¿De qué se trata este negocio?"
    assert "Pending Di-Factory settings: tpl_reminder_id, tpl_nudge_id" in out

    instance = tmp_path / "clients" / "clinica-sonrisa-pyme-appointment-agent.json"
    spec = json.loads(instance.read_text())
    assert spec["values"]["main_model"] == "claude-sonnet-5-5"  # Di-Factory's default
    assert spec["values"]["tpl_reminder_id"] == "pending"
    assert spec["knowledge"]["corpora"]["clinic_faq"]["sources"]  # the business FAQ

    folder = tmp_path / "repo" / "deploy" / "build" / "clinica-sonrisa-pyme-appointment-agent"
    compose = json.loads((folder / "docker-compose.yml").read_text())
    service = compose["services"]["instance"]
    assert service["environment"]["DIF_PUBLIC_URL"] == "https://3-148-79-116.sslip.io"
    assert service["ports"] == ["127.0.0.1:8080:8080"]  # only Caddy reaches it
    assert (folder / "secrets" / "anthropic").read_text() == KEY
    assert (folder / "secrets" / "admin_token").read_text()  # generated once, kept in ~/.dif
    assert (folder / "Caddyfile").read_text().startswith("3-148-79-116.sslip.io {")
    marker = (tmp_path / "repo" / ".dif" / "online").read_text().splitlines()
    assert marker == [str(folder), "3-148-79-116.sslip.io"]
    approvers = json.loads((tmp_path / "repo" / ".dif" / "approvers.json").read_text())
    record = json.loads(instance.with_suffix(".docker.approval.json").read_text())
    check_approval(record, folder / "solution", "docker", approvers)  # Jag's signature holds


def test_running_again_reuses_the_client(
    examples: Path, tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secrets = home / ".dif" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "anthropic").write_text(KEY)
    clients = tmp_path / "clients"
    replies = iter(["", "dental appointments on WhatsApp", "1", *_replies(examples)])
    provider = FakeProvider([Message.assistant("Hola."), Message.assistant("Hola otra vez.")])
    run = lambda argv: main(argv, provider=provider)  # noqa: E731
    first = Setup([examples], clients, ask=lambda _: next(replies), run=run)
    assert first.run_all() == 0
    assert "Found an Anthropic key" in capsys.readouterr().out

    again = iter(["", "1"])  # keep the key; reuse client 1
    second = Setup([examples], clients, ask=lambda _: next(again), run=run)
    assert second.run_all() == 0
    out = capsys.readouterr().out
    assert "clinica-sonrisa-pyme-appointment-agent" in out and "Hola otra vez." in out
    assert "No public address known" in out  # without setup.sh it never goes online


def test_subscription_tokens_are_refused(
    examples: Path, tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keys: Any = iter(["sk-ant-oat01-abc", KEY])
    setup = Setup([examples], tmp_path, ask=lambda _: "", ask_secret=lambda _: next(keys))
    assert setup.model_key()
    assert "subscription (OAuth) token" in capsys.readouterr().out
    assert (home / ".dif" / "secrets" / "anthropic").read_text() == KEY
