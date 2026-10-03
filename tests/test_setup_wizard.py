"""The guided setup (./setup.sh): a fresh clone to a tested, signed, ready-to-serve client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.cli import main
from dif_general_harness.constructor import pack_questions
from dif_general_harness.constructor.deploy import check_approval
from dif_general_harness.constructor.setup import Setup, parse_choice
from dif_general_harness.core.messages import Message
from dif_general_harness.providers import FakeProvider
from dif_general_harness.providers.base import ModelRequest
from dif_general_harness.spec import PackCatalog
from tests.test_constructor import CLINIC, _flat

PACK = "pyme-appointment-agent"
KEY = "sk-ant-api03-" + "k" * 90


def _replies(examples: Path) -> list[str]:
    """What a person types, in order: the client's answers (Di-Factory's models are defaults;
    the template ids are left blank, so they become pending)."""
    flat = _flat(CLINIC)
    skip = {"values.main_model", "values.fast_model"}
    out = ["", ""]  # the brand look: skipped; the web chat: open to anyone (Enter)
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
    assert "Not connected yet. The agent works without these" in out
    assert "tpl_reminder_id (Approved WhatsApp template id for reminders)" in out
    assert "without it: WhatsApp only lets a business start a conversation" in out
    assert "channel whatsapp: WhatsApp/SMS messages are not received" in out  # twilio
    assert "the agent cannot see free slots" in out  # google
    summary = tmp_path / "clients" / "clinica-sonrisa-pyme-appointment-agent.summary.md"
    assert "## Not connected yet, and what that means" in summary.read_text()

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

    again = iter(["", "1", "", ""])  # keep the key; reuse client 1; rebuild it; same look
    second = Setup([examples], clients, ask=lambda _: next(again), run=run)
    assert second.run_all() == 0
    out = capsys.readouterr().out
    assert "clinica-sonrisa-pyme-appointment-agent" in out and "Hola otra vez." in out
    assert "No public address known" in out  # without setup.sh it never goes online


def test_subscription_tokens_are_refused(
    examples: Path, tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keys: Any = iter(["sk-ant-oat01-abc", KEY * 4, KEY])
    setup = Setup([examples], tmp_path, ask=lambda _: "", ask_secret=lambda _: next(keys))
    assert setup.model_key()
    out = capsys.readouterr().out
    assert "subscription (OAuth) token" in out
    assert "holds 4 keys one after another" in out  # a paste that arrived four times
    assert (home / ".dif" / "secrets" / "anthropic").read_text() == KEY


def test_a_broken_stored_key_is_asked_again(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secrets = home / ".dif" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "anthropic").write_text(KEY * 4)
    setup = Setup([tmp_path], tmp_path, ask=lambda _: "", ask_secret=lambda _: KEY)
    assert setup.model_key() and setup.key == KEY
    assert "The stored Anthropic key cannot work" in capsys.readouterr().out
    assert (secrets / "anthropic").read_text() == KEY


def test_choices_accept_several_numbers() -> None:
    assert parse_choice("1 and 2", 3) == [0, 1]
    assert parse_choice("1 y 3", 3) == [0, 2] and parse_choice("2,2", 3) == [1]
    assert parse_choice("4", 3) is None and parse_choice("one", 3) is None


def test_the_advisor_recommends_and_names_the_gaps(
    examples: Path, tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secrets = home / ".dif" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "anthropic").write_text(KEY)
    advice = {
        "packs": [PACK, "no-such-pack"],
        "covered": ["citas por WhatsApp"],
        "gaps": ["cobros", "notas de terapia"],
        "why": "La agenda es lo central.",
    }
    advisor = FakeProvider([Message.assistant("Here it is:\n" + json.dumps(advice))])
    keys: list[str] = []

    def factory(key: str) -> FakeProvider:
        keys.append(key)
        return advisor

    replies = iter([
        "", "terapeuta: citas, pagos y notas",  # keep the key; the need
        "1 and 2",  # two packs that cannot share one instance...
        "",  # ...so Enter takes the recommendation
        *_replies(examples),
    ])  # fmt: skip
    setup = Setup([examples], tmp_path / "clients", ask=lambda _: next(replies), advisor=factory)
    assert setup.run_all() == 0
    out = capsys.readouterr().out
    assert keys == [KEY] and advisor.requests[0].model_role == "fast"
    assert PACK in advisor.requests[0].messages[0].text()  # it saw the catalog
    assert "Not covered by any pack yet (noted as Di-Factory design work): cobros" in out
    assert " ★1. " in out and "no-such-pack" not in out  # invented packs are dropped
    assert "cannot run together in one instance yet" in out and "safety_weakened" in out
    summary = tmp_path / "clients" / "clinica-sonrisa-pyme-appointment-agent.summary.md"
    text = summary.read_text()
    assert "## Needs not covered yet (Di-Factory design work)" in text
    assert "- notas de terapia" in text


def test_without_the_advisor_word_matching_still_works(
    examples: Path, tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = FakeProvider([RuntimeError("offline")])
    setup = Setup([examples], tmp_path, ask=lambda _: "", advisor=lambda _: broken)
    setup.key = KEY
    replies = iter(["dental appointments on WhatsApp", ""])
    setup.ask = lambda _: next(replies)
    assert setup.choose_pack() == [PACK]
    assert "did not answer: RuntimeError" in capsys.readouterr().out


def test_the_consultant_asks_follow_ups_and_recommends(
    examples: Path, tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secrets = home / ".dif" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "anthropic").write_text(KEY)

    def consult(request: ModelRequest) -> Message:
        asked = request.messages[0].text()
        if "recommendations" in request.system:
            assert "Limpiezas: 600 MXN, 45 minutos" in asked  # the follow-up made it in
            return Message.assistant('{"recommendations": ["Define a cancellation fee."]}')
        if "Question: Which services do you offer" in asked:
            return Message.assistant('{"follow_up": "¿Cuánto cuesta y cuánto dura cada uno?"}')
        return Message.assistant('{"follow_up": null}')

    consultant = FakeProvider([consult] * 20)
    business = [q for q in pack_questions(PackCatalog(roots=[examples]), [PACK])
                if q.key.startswith("knowledge.")]  # fmt: skip
    services = next(i for i, q in enumerate(business) if q.key.endswith(".services"))
    replies = _replies(examples)
    # the answer to "services" is followed by the consultant's question
    flat_keys = [q.key for q in pack_questions(PackCatalog(roots=[examples]), [PACK])
                 if q.key not in {"values.main_model", "values.fast_model"}]  # fmt: skip
    at = flat_keys.index(business[services].key)
    replies.insert(at + 3, "Limpiezas: 600 MXN, 45 minutos")  # after brand and web chat
    script = iter(["", "dental appointments on WhatsApp", "1", "y", *replies])
    setup = Setup([examples], tmp_path / "clients", ask=lambda _: next(script),
                  consultant=lambda _: consultant)  # fmt: skip
    assert setup.run_all() == 0
    out = capsys.readouterr().out
    assert "Consultant: ¿Cuánto cuesta y cuánto dura cada uno?" in out
    assert consultant.requests[0].model_role == "consultant"
    folder = tmp_path / "clients"
    faq = folder / "clinica-sonrisa-pyme-appointment-agent.knowledge" / "clinic_faq.md"
    assert "Limpiezas: 600 MXN, 45 minutos" in faq.read_text()
    summary = (folder / "clinica-sonrisa-pyme-appointment-agent.summary.md").read_text()
    assert "## The business consultant's recommendations" in summary
    assert "- Define a cancellation fee." in summary


def test_files_left_by_an_earlier_run_give_the_fix_not_a_traceback(
    examples: Path, tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    clients = tmp_path / "clients"
    replies = iter(["dental appointments on WhatsApp", "1", *_replies(examples)])
    setup = Setup([examples], clients, ask=lambda _: next(replies), ask_secret=lambda _: KEY,
                  public_url="https://3-148-79-116.sslip.io", root=tmp_path / "repo")  # fmt: skip
    setup.model_key()
    result = setup.questionnaire(setup.choose_pack() or [])

    def locked(*_: Any, **__: Any) -> Any:
        raise PermissionError(13, "Permission denied", "/deploy/build/x/secrets/README.md")

    monkeypatch.setattr("dif_general_harness.constructor.setup.plan_docker", locked)
    setup.ask = lambda _: "y"
    assert setup.go_online(result) is None
    out = capsys.readouterr().out
    assert "Cannot write /deploy/build/x/secrets/README.md" in out and "sudo chown -R" in out
    assert not (tmp_path / "repo" / ".dif" / "online").exists()  # setup.sh starts nothing


def test_gaps_follow_the_pack_actually_chosen(
    examples: Path, tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    advice = {"packs": ["service-desk-cell"], "covered": ["tickets"],
              "gaps": ["Appointment scheduling via chat"], "why": "A help desk."}  # fmt: skip
    advisor = FakeProvider([
        Message.assistant(json.dumps(advice)),
        Message.assistant('{"gaps": ["Online payments"]}'),  # asked again for the choice
    ])  # fmt: skip
    setup = Setup([examples], tmp_path, ask=lambda _: "", advisor=lambda _: advisor)
    setup.key = KEY
    options = [
        "service-desk-cell",
        *sorted(
            p
            for p in [
                "conversational-rag",
                "dev-cell",
                "opc-c-suite",
                PACK,
                "pyme-receipt-processing",
            ]
        ),
    ]
    order = iter(["a clothing shop that books fittings", str(options.index(PACK) + 1)])
    setup.ask = lambda _: next(order)
    assert setup.choose_pack() == [PACK]
    assert setup.gaps == ["Online payments"]  # not the gaps of the pack it recommended
    assert "Chosen packs:" in advisor.requests[1].messages[0].text()
    assert PACK in advisor.requests[1].messages[0].text()
    assert "With that choice, not covered yet: Online payments" in capsys.readouterr().out


def test_a_pack_on_another_endpoint_runs_on_the_anthropic_key(
    examples: Path, tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from dif_general_harness.constructor import build
    from dif_general_harness.constructor.impact import missing
    from dif_general_harness.constructor.interview import load_answers
    from dif_general_harness.spec import load_instance

    catalog = PackCatalog(roots=[examples])
    packs = {la.data["solution"]["id"]: la.data for la in catalog.latest()}
    setup = Setup([examples], tmp_path, ask=lambda _: "")  # Enter: yes, on Anthropic
    carried = setup._model_choice(["conversational-rag"], packs)
    assert "runs on its own model endpoint" not in capsys.readouterr().out  # asked, not warned
    assert carried["models"]["providers"] == {
        "openai-compatible": None, "anthropic": {"api_key": {"$secret": "anthropic"}}}  # fmt: skip

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "faq.md").write_text("# Reparo\n\n## What we do\nWe repair phones.\n")
    answers = {
        "tenant.id": "reparo", "tenant.name": "Reparo", "values.assistant_name": "Reparo",
        "values.corpus_sources": [str(docs)], "values.support_email": "a@reparo.mx",
        "values.main_model": "claude-sonnet-5-5", "values.fast_model": "claude-haiku-4-5",
        **ABOUT, **carried,
    }  # fmt: skip
    first = build(catalog, ["conversational-rag"], tmp_path / "out", answers=answers)
    assert first.ok, first.problems
    again = build(
        catalog, ["conversational-rag"], tmp_path / "out", answers=load_answers(first.answers_path)
    )  # a rebuild keeps the choice
    resolved = load_instance(again.spec_path, catalog)
    roles = resolved.data["models"]["roles"]
    assert {r["provider"] for r in roles.values()} == {"anthropic"}
    assert set(resolved.data["models"]["providers"]) == {"anthropic"}
    left = {m.name for m in missing(resolved.spec, resolved.data, lambda n: n == "anthropic")}
    assert "llm" not in left and "telegram" in left  # nothing uses llm any more

    no = Setup([examples], tmp_path, ask=lambda _: "n")
    assert no._model_choice(["conversational-rag"], packs) == {}
    assert "not put online" in capsys.readouterr().out


ABOUT = {
    "knowledge.about.about": "Reparo fixes phones and laptops in Guadalajara.",
    "knowledge.about.topics": "Repair times, warranties and prices.",
    "knowledge.about.contact": "soporte@reparo.mx, 33 1234 5678.",
}


def test_a_document_assistant_gets_a_page_and_an_open_or_closed_chat(
    examples: Path, tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from dif_general_harness.channels.landing import render_landing
    from dif_general_harness.channels.web_page import render_chat
    from dif_general_harness.constructor import build

    catalog = PackCatalog(roots=[examples])
    docs = tmp_path / "docs.md"
    docs.write_text("# Manual\n\n## Internal pricing\nNot for the web page.\n")
    answers = {
        "tenant.id": "reparo", "tenant.name": "Reparo", "values.assistant_name": "Reparo",
        "values.corpus_sources": [str(docs)], "values.support_email": "a@reparo.mx",
        "values.main_model": "m", "values.fast_model": "f", **ABOUT,
    }  # fmt: skip
    open_ = build(catalog, ["conversational-rag"], tmp_path / "open", answers=dict(answers))
    assert open_.ok and open_.resolved is not None, open_.problems
    assert open_.resolved.spec.channels["web"].public is True  # the pack's chat is open
    page = render_landing(open_.resolved.spec, open_.resolved.data, "web")
    assert "Reparo fixes phones and laptops" in page and "Repair times" in page
    assert "Internal pricing" not in page  # the page shows the client's answers, not documents
    assert 'id="bubble"' in page and page.count('href="/chat/web"') == 4

    setup = Setup([examples], tmp_path, ask=lambda _: "n")  # not for anyone with the link
    setup.ask_web_access(["conversational-rag"])
    code = home / ".dif" / "secrets" / "web_access_code"
    assert code.read_text() and code.stat().st_mode & 0o777 == 0o600
    assert str(code) in capsys.readouterr().out and code.read_text() not in str(setup.carried)
    closed = build(catalog, ["conversational-rag"], tmp_path / "closed",
                   answers={**answers, **setup.carried})  # fmt: skip
    assert closed.ok and closed.resolved is not None, closed.problems
    web = closed.resolved.spec.channels["web"]
    assert web.public is False and closed.resolved.data["channels"]["web"]["credentials"] == {
        "$secret": "web_access_code"}  # fmt: skip
    assert "channels" in closed.answers_path.read_text()  # kept for every rebuild
    private = render_landing(closed.resolved.spec, closed.resolved.data, "web", public=False)
    assert 'id="bubble"' not in private and "Chat (access code)" in private
    assert private.count('href="/chat/web"') == 1  # one quiet link for people with the code

    chat = render_chat("web", "Reparo", locale="es", public=False)
    assert 'id="gate"' in chat and "prompt(" not in chat  # asked on the page, not a pop-up
    assert "Este chat es privado" in chat
