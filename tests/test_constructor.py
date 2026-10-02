"""The constructor v1 (M1.6): match, interview, build, validate, evals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from dif_general_harness.cli import main
from dif_general_harness.constructor import (
    AnswerError,
    Question,
    build,
    interview,
    load_answers,
    match,
    pack_questions,
    parse,
    run_suites,
)
from dif_general_harness.core.messages import Message, Role, ToolUseBlock
from dif_general_harness.providers import FakeProvider
from dif_general_harness.runtime import Instance, RuntimeOptions
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.tenancy import EnvSecrets

CLINIC = {
    "tenant": {
        "name": "Clínica Sonrisa",
        "id": "clinica-sonrisa",
        "timezone": "America/Mexico_City",
    },
    "solution": {"locale": "es-MX"},
    "values": {
        "business_name": "Clínica Sonrisa",
        "business_hours": "mon-fri: 09:00-19:00; sat: 09:00-14:00",
        "whatsapp_number": "+52XXXXXXXXXX",
        "calendar_ids": "dra-lopez@example.com, dr-ramirez@example.com",
        "tpl_reminder_id": "HX1",
        "tpl_nudge_id": "HX2",
        "ops_email": "recepcion@example.com",
        "main_model": "claude-opus-5-5",
        "fast_model": "claude-haiku-4-5",
    },
    "knowledge": {
        "clinic_faq": {
            "about": "Clínica dental familiar en Coyoacán, abierta desde 2012.",
            "services": "Limpieza (45 min), resinas (1 h), ortodoncia y blanqueamiento.",
            "location": "Av. Universidad 123, Col. Del Valle, CDMX.",
        }
    },
}


def _flat(answers: dict[str, Any]) -> dict[str, Any]:
    flat = {f"{s}.{k}": v for s, sub in answers.items() if s != "knowledge" for k, v in sub.items()}
    for corpus, items in answers.get("knowledge", {}).items():
        flat |= {f"knowledge.{corpus}.{k}": v for k, v in items.items()}
    return flat


def _q(kind: str, **kw: Any) -> Question:
    return Question("values.x", "x?", kind, kw.pop("required", True), **kw)


# --- matching ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_text", "pack"),
    [
        ("appointment reminders for a dental clinic on WhatsApp", "pyme-appointment-agent"),
        ("resolve IT tickets and reset passwords", "service-desk-cell"),
        ("answer questions from our documents with citations", "conversational-rag"),
        ("process CFDI invoices into the ERP", "pyme-receipt-processing"),
        ("coding agents for our GitHub backlog", "dev-cell"),
        ("a one person company run by AI executives", "opc-c-suite"),
    ],
)
def test_match_finds_the_pack(examples: Path, request_text: str, pack: str) -> None:
    assert match(PackCatalog(roots=[examples]), request_text)[0].pack_id == pack


def test_no_pack_fits(examples: Path) -> None:
    assert match(PackCatalog(roots=[examples]), "crypto trading bot") == []
    assert match(PackCatalog(roots=[examples]), "the and of") == []


# --- questions and answers ---------------------------------------------------------


def test_questions_come_from_the_pack(examples: Path) -> None:
    qs = pack_questions(PackCatalog(roots=[examples]), ["pyme-appointment-agent"])
    keys = [q.key for q in qs]
    assert keys[:5] == [
        "tenant.name",
        "tenant.id",
        "tenant.timezone",
        "solution.id",
        "solution.locale",
    ]
    # groups in the pack's order, then order within the group
    assert keys.index("values.business_name") < keys.index("values.business_hours")
    assert keys.index("values.business_hours") < keys.index("values.reminder_hours")
    by_key = {q.key: q for q in qs}
    assert by_key["values.reminder_hours"].default == 24
    # a placeholder default is not an answer
    assert by_key["values.main_model"].required and by_key["values.main_model"].default is None
    assert by_key["values.tpl_reminder_id"].answered_by == "difactory"


def test_variables_without_ask_use_their_description(examples: Path) -> None:
    qs = pack_questions(PackCatalog(roots=[examples]), ["service-desk-cell"])
    by_key = {q.key: q for q in qs}
    assert by_key["values.idp_url"].text == "Identity provider admin API base URL"
    assert by_key["values.company_name"].text == "company name"
    assert "values.sla_first_response" not in by_key  # optional, real default, no question


@pytest.mark.parametrize(
    ("question", "raw", "value"),
    [
        (_q("string"), "  Ana ", "Ana"),
        (_q("integer", minimum=1, maximum=72), "24", 24),
        (_q("number"), "20000", 20000.0),
        (_q("boolean"), "sí", True),
        (_q("boolean"), "no", False),
        (_q("boolean"), False, False),
        (_q("enum", options=["a", "b"]), "b", "b"),
        (_q("list"), "a@x.mx, b@x.mx", ["a@x.mx", "b@x.mx"]),
        (_q("list"), '["a", "b"]', ["a", "b"]),
        (_q("list"), ["a"], ["a"]),
        (
            _q("schedule"),
            "mon-fri: 09:00-19:00; sat: 09:00-14:00",
            {"mon-fri": "09:00-19:00", "sat": "09:00-14:00"},
        ),
        (
            _q("schedule"),
            "mon-fri 09:00-19:00\nsat 09:00-14:00",
            {"mon-fri": "09:00-19:00", "sat": "09:00-14:00"},
        ),
        (_q("object"), '{"a": 1}', {"a": 1}),
        (_q("duration"), "4h", "4h"),
        (_q("id"), "clinica-sonrisa", "clinica-sonrisa"),
        (_q("integer", default=24), "", 24),
        (_q("string", required=False), "", None),
    ],
)
def test_parse(question: Question, raw: Any, value: Any) -> None:
    assert parse(question, raw) == value


@pytest.mark.parametrize(
    ("question", "raw", "message"),
    [
        (_q("string"), "", "required"),
        (_q("integer"), "many", "whole number"),
        (_q("integer", minimum=1, maximum=72), "100", "at most 72"),
        (_q("number", minimum=0), "-1", "at least 0"),
        (_q("boolean"), "maybe", "yes or no"),
        (_q("enum", options=["a"]), "z", "one of"),
        (_q("list"), "[1,", "invalid JSON"),
        (_q("object"), "[1]", "mapping"),
        (_q("schedule"), "whenever", "key: value"),
        (_q("duration"), "a while", "duration"),
        (_q("id"), "Clínica Sonrisa", "lowercase"),
        (_q("file"), "/no/such/file", "does not exist"),
    ],
)
def test_parse_rejects(question: Question, raw: Any, message: str) -> None:
    with pytest.raises(AnswerError, match=message):
        parse(question, raw)


def test_interview_non_interactive_reports_every_gap(examples: Path) -> None:
    qs = pack_questions(PackCatalog(roots=[examples]), ["pyme-appointment-agent"])
    partial = _flat(CLINIC)
    del partial["values.ops_email"]
    partial["values.reminder_hours"] = "500"
    partial["values.nope"] = "x"
    got, problems = interview(qs, partial)
    assert any(p.startswith("values.ops_email: an answer is required") for p in problems)
    assert any(p.startswith("values.reminder_hours: must be at most") for p in problems)
    assert "values.nope: not a question of this pack" in problems
    assert got["values.calendar_ids"] == ["dra-lopez@example.com", "dr-ramirez@example.com"]


def test_interview_asks_again_until_valid() -> None:
    q = _q("integer", minimum=1, maximum=72)
    replies = iter(["lots", "100", "12"])
    seen: list[str | None] = []

    def ask(question: Question, error: str | None) -> str:
        seen.append(error)
        return next(replies)

    got, problems = interview([q], None, ask)
    assert got == {"values.x": 12} and not problems
    assert seen == [None, "expected a whole number", "must be at most 72"]


def test_answers_file_formats(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(CLINIC, allow_unicode=True))
    (tmp_path / "a.json").write_text(
        json.dumps({"values.business_name": "X", "ops_email": "o@x.mx"})
    )
    assert load_answers(tmp_path / "a.yaml")["tenant.id"] == "clinica-sonrisa"
    assert load_answers(tmp_path / "a.json") == {"values.business_name": "X", "ops_email": "o@x.mx"}
    (tmp_path / "bad.yaml").write_text("- a list\n")
    with pytest.raises(AnswerError):
        load_answers(tmp_path / "bad.yaml")


# --- build -------------------------------------------------------------------------


def test_build_writes_a_valid_instance(examples: Path, tmp_path: Path) -> None:
    catalog = PackCatalog(roots=[examples])
    result = build(catalog, ["pyme-appointment-agent"], tmp_path / "out", answers=_flat(CLINIC))
    assert result.ok, (result.problems, result.resolved and result.resolved.issues)
    spec = json.loads(result.spec_path.read_text())
    assert spec["extends"] == ["pyme-appointment-agent@^1.0"]
    assert spec["solution"]["id"] == "clinica-sonrisa-pyme-appointment-agent"
    assert spec["solution"]["locale"] == "es-MX"
    assert spec["values"]["business_hours"] == {"mon-fri": "09:00-19:00", "sat": "09:00-14:00"}
    assert spec["values"]["reminder_hours"] == 24  # the pack default, written explicitly
    # the business answers became the client's FAQ, which replaces the pack's example
    [source] = spec["knowledge"]["corpora"]["clinic_faq"]["sources"]
    faq = (result.spec_path.parent / source["path"]).read_text()
    assert "## What is the business about?\nClínica dental familiar" in faq
    assert "## Which services do you offer?" in faq and "How much" not in faq  # unanswered

    summary = result.summary_path.read_text()
    assert "## Secrets the client stores in their own vault" in summary
    assert "- `twilio`: Messaging gateway credentials" in summary
    assert "- Passed." in summary

    # adjusting is "edit the answers, rebuild": the answers file round-trips
    again = build(
        catalog,
        ["pyme-appointment-agent"],
        tmp_path / "out",
        answers=load_answers(result.answers_path),
    )
    assert json.loads(again.spec_path.read_text()) == spec
    assert again.resolved and result.resolved
    assert again.resolved.version_hash == result.resolved.version_hash


def test_build_with_gaps_is_not_ready(examples: Path, tmp_path: Path) -> None:
    answers = _flat(CLINIC)
    del answers["values.main_model"]
    result = build(
        PackCatalog(roots=[examples]), ["pyme-appointment-agent"], tmp_path, answers=answers
    )
    assert not result.ok and result.resolved is None
    assert any("values.main_model" in p for p in result.problems)
    assert "## Open answers" in result.summary_path.read_text()


def test_cli_build(
    examples: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = tmp_path / "clinic.yaml"
    answers.write_text(yaml.safe_dump(CLINIC, allow_unicode=True))
    base = ["build", "--packs", str(examples), "--out", str(tmp_path / "out")]
    request = ["--request", "appointment reminders for a dental clinic on WhatsApp"]
    assert main([*base, *request, "--answers", str(answers)]) == 0
    assert "OK: instance built and validated" in capsys.readouterr().out

    assert main([*base, "--request", "crypto trading bot", "--answers", str(answers)]) == 3
    assert "No pack fits" in capsys.readouterr().out

    # interactive: pick the first match, then answer every question (empty = default)
    qs = pack_questions(PackCatalog(roots=[examples]), ["pyme-appointment-agent"])
    flat = _flat(CLINIC)
    replies = iter(["1"] + [str(flat.get(q.key, "")) for q in qs])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    assert main([*base, *request]) == 0
    assert "OK: instance built and validated" in capsys.readouterr().out


# --- evals -------------------------------------------------------------------------

SUITE = """\
id: saves-a-note
turns:
  - user: "remember that Luis called"
  - expect_tool: notes.write
  - expect_reply_contains: [saved]
must_not:
  - tool: coding.bash
---
id: forbidden-tool
turns:
  - user: "clean the repo"
  - expect_tool: notes.list
must_not:
  - tool: coding.bash
---
id: needs-a-trigger
turns:
  - trigger: reminder
  - expect_template: reminder
---
id: outcome-with-approval
setup: { message: "note that the meeting moved" }
expect:
  tools_called: [notes.write]
  approval_requested: true
"""


def _calls(*calls: tuple[str, str, dict[str, Any]]) -> Message:
    return Message(
        role=Role.ASSISTANT, content=[ToolUseBlock(id=i, name=n, input=a) for i, n, a in calls]
    )


async def test_eval_runner(tmp_path: Path, helper_solution: Path) -> None:
    instance_file = helper_solution
    suite = tmp_path / "suite.yaml"
    suite.write_text(SUITE)
    script = [
        _calls(("a", "notes.write", {"key": "luis", "text": "called"})),
        Message.assistant("Saved it."),
        _calls(("b", "coding.bash", {"command": "rm -rf ."})),
        Message.assistant("I tried."),
        _calls(("c", "notes.write", {"key": "meeting", "text": "moved"})),
        Message.assistant("Noted."),
    ]
    repo = tmp_path / "repo"
    repo.mkdir()
    provider = FakeProvider(script)
    resolved = load_instance(instance_file, PackCatalog(roots=[tmp_path / "packs"]))

    async def open_instance(state: Path) -> Instance:
        options = RuntimeOptions(
            state_root=state,
            secrets=EnvSecrets({}),
            workspaces={"repo": repo},
            provider=provider,
        )
        return await Instance.open(resolved, options)

    report = await run_suites(
        open_instance,
        [suite, tmp_path / "missing.yaml"],
        {"pass_rate": 0.9},
        work=tmp_path / "work",
    )
    by_case = {r.case: r for r in report.results}
    assert by_case["saves-a-note"].status == "passed"
    forbidden = by_case["forbidden-tool"]
    assert forbidden.status == "failed" and forbidden.unsafe_actions == 1
    assert any("expected a call to notes.list" in r for r in forbidden.reasons)
    no_trigger = by_case["needs-a-trigger"]  # triggers run now; this spec has none
    assert no_trigger.status == "failed"
    assert no_trigger.reasons[0] == "trigger 'reminder' is not defined or cannot run here"
    assert by_case["outcome-with-approval"].status == "passed"
    assert by_case["*"].reasons == ["suite not written yet"]
    assert report.pass_rate == pytest.approx(2 / 4) and report.unsafe_actions == 1
    assert not report.ok
    assert list(repo.iterdir()) == []  # the denied shell command never ran


def test_example_evals_run_and_fail_honestly_without_a_model(
    examples: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    instance = examples / "instances" / "clinica-sonrisa.json"
    code = main(["eval", str(instance), "--state", str(tmp_path)], provider=FakeProvider([]))
    out = capsys.readouterr().out
    # the opt-out case needs no model and passes; the others fail, never pass silently
    assert "PASSED   confirm.yaml / opt-out-before-reminder" in out
    assert "FAILED   confirm.yaml / confirm-1" in out and "FakeProvider script exhausted" in out
    assert "pass rate 33% over 3 case(s)" in out
    assert code == 1
