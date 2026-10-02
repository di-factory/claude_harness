"""A new client, end to end, the way a server gets one: ``./setup.sh``'s guided setup (key,
pack, questionnaire, build, signed deploy), then the staged solution served exactly as the
container serves it (its own packs and secrets folder), answering on ``/``, ``/chat`` and
the chat itself. If any step a first client goes through breaks, this fails."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx2
import pytest

from dif_general_harness.cli import main
from dif_general_harness.constructor.setup import Setup
from dif_general_harness.core.messages import Message
from dif_general_harness.providers import FakeProvider
from dif_general_harness.providers.base import ModelRequest
from dif_general_harness.runtime import Instance, RuntimeOptions
from dif_general_harness.service import Headless, create_app
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.tenancy import FileSecrets
from tests.test_setup_wizard import KEY, _replies

URL = "https://3-148-79-116.sslip.io"
VISITOR = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DIF_SECRETS_DIR", raising=False)
    monkeypatch.delenv("DIF_SECRET_ANTHROPIC", raising=False)
    return tmp_path / "home"


def _set_up(examples: Path, tmp_path: Path) -> Path:
    """The guided setup, online: returns the deploy folder setup.sh starts."""
    replies = iter(["dental appointments on WhatsApp", "1", *_replies(examples), "y"])
    setup = Setup(
        [examples], tmp_path / "clients", ask=lambda _: next(replies), ask_secret=lambda _: KEY,
        run=lambda argv: main(argv, provider=FakeProvider([Message.assistant("Hola.")])),
        public_url=URL, root=tmp_path / "repo",
    )  # fmt: skip
    assert setup.run_all() == 0
    marker = (tmp_path / "repo" / ".dif" / "online").read_text().splitlines()
    return Path(marker[0])


def test_a_new_client_is_served_like_the_container_does(
    examples: Path, tmp_path: Path, home: Path
) -> None:
    folder = _set_up(examples, tmp_path)  # the setup runs its own event loop
    asyncio.run(_serve(folder, tmp_path))


async def _serve(folder: Path, tmp_path: Path) -> None:
    solution = folder / "solution"
    resolved = load_instance(solution / "instance.json", PackCatalog(roots=[solution / "packs"]))
    assert resolved.ok, [str(i) for i in resolved.issues if i.severity == "error"]
    secrets = FileSecrets(folder / "secrets")  # what the container mounts
    assert secrets.get("anthropic") == KEY and secrets.get("admin_token")

    def model(request: ModelRequest) -> Message:
        if request.model_role == "router":  # the intent router: not a trivial message
            return Message.assistant('{"intent": "other", "reply": ""}')
        return Message.assistant("Somos una clínica dental familiar en Coyoacán.")

    provider = FakeProvider([model] * 6)
    options = RuntimeOptions(state_root=tmp_path / "state", secrets=secrets, provider=provider)
    instance = await Instance.open(resolved, options)
    headless = await Headless.build(instance, public_url=URL)
    app = create_app(headless, admin_token=secrets.get("admin_token"), run_worker=False)
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=URL)
    async with instance, client:
        health = (await client.get("/healthz")).json()
        assert health["status"] == "ok"
        assert health["instance"] == "clinica-sonrisa-pyme-appointment-agent"
        landing = await client.get("/")
        assert landing.status_code == 200 and "Clínica Sonrisa" in landing.text
        assert 'href="/chat/web"' in landing.text
        assert (await client.get("/chat")).status_code == 200
        r = await client.post("/channels/web", json={"contact": VISITOR, "text": "¿Qué hacen?"})
        assert r.status_code == 200, r.text
        assert r.json()["replies"][0]["reply"] == "Somos una clínica dental familiar en Coyoacán."
        [main_call] = [q for q in provider.requests if q.model_role != "router"]
        prompt = main_call.system
        assert "never assume what kind of business it is" in prompt  # the current pack's
        faq = main_call.tools
        assert any(t["name"].startswith("knowledge") for t in faq)


def test_a_reused_client_takes_the_pack_updates(
    examples: Path, tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_up(examples, tmp_path)
    clients = tmp_path / "clients"
    faq = clients / "clinica-sonrisa-pyme-appointment-agent.knowledge" / "clinic_faq.md"
    old = faq.read_text().replace("What is the business about?", "What is the clinic about?")
    faq.write_text(old)  # a client built before the pack's wording changed
    answers = clients / "clinica-sonrisa-pyme-appointment-agent.answers.yaml"
    answers.write_text(answers.read_text().replace("Limpieza", "Limpieza dental"))
    replies = iter(["", "1", "", "#4a1450", "n"])  # key, client 1, rebuild, a new look, offline
    run = lambda argv: main(argv, provider=FakeProvider([Message.assistant("Hola.")]))  # noqa: E731
    again = Setup([examples], clients, ask=lambda _: next(replies), run=run,
                  public_url=URL, root=tmp_path / "repo")  # fmt: skip
    assert again.run_all() == 0
    text = faq.read_text()
    assert "What is the business about?" in text and "clinic about" not in text
    assert "Limpieza dental" in text  # an edited answer reaches the FAQ
    assert "Rebuilt clinica-sonrisa-pyme-appointment-agent" in capsys.readouterr().out
    spec = json.loads((clients / "clinica-sonrisa-pyme-appointment-agent.json").read_text())
    assert spec["values"]["main_model"] == "claude-sonnet-5-5"  # nothing else lost
    assert spec["branding"] == {"colors": {"primary": "#4a1450"}}  # the look given on reuse
    assert "branding" in answers.read_text()  # kept for the next rebuild


def test_a_client_that_cannot_answer_is_never_put_online(
    examples: Path, tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_up(examples, tmp_path)
    out = capsys.readouterr().out
    assert "[needs: its own openai-compatible model endpoint (not set up here)]" in out
    marker = tmp_path / "repo" / ".dif" / "online"
    marker.unlink()  # setup.sh removes it before every run
    replies = iter(["", "1", "", "", "y"])  # key, client 1, rebuild, same look, online
    broken = Setup([examples], tmp_path / "clients", ask=lambda _: next(replies),
                   run=lambda argv: 2,  # "cannot start: secrets.llm: not set", say
                   public_url=URL, root=tmp_path / "repo")  # fmt: skip
    assert broken.run_all() == 1
    assert "The test question did not work" in capsys.readouterr().out
    assert not marker.exists()  # setup.sh starts nothing
