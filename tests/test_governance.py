"""The governance plane (M2.2): PII tokenization, consent, audit, retention, spend."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.core.messages import Message, Role, ToolResultBlock, ToolUseBlock, Usage
from dif_general_harness.core.scope import Scope
from dif_general_harness.core.session import Session
from dif_general_harness.governance import (
    AuditLog,
    ConsentStore,
    PiiPolicy,
    Tokenizer,
    TokenVault,
    is_opt_out,
    purge,
)
from dif_general_harness.governance.pii import find
from dif_general_harness.policy.spend import SpendStore
from dif_general_harness.providers import FakeProvider
from dif_general_harness.providers.base import ProviderMessage
from dif_general_harness.runtime import Instance, RuntimeOptions
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.store import SqlSessionStore
from dif_general_harness.tenancy import EnvSecrets
from dif_general_harness.tools import tool

A = Scope(tenant_id="clinica-sonrisa", instance_id="citas")
B = Scope(tenant_id="otra-clinica", instance_id="citas")
ALL = {"name", "email", "phone", "curp", "rfc", "account"}

CURP = "GOMA850312HDFRRN09"
RFC = "GOMA850312AB1"
PLANTED = (
    f"Soy Ana Gómez, mi cel es 55 1234 5678 o +52 1 55 1234 5678, correo ana.gomez@example.mx, "
    f"CURP {CURP}, RFC {RFC}, CLABE 002180700123456789, tarjeta 4111 1111 1111 1111."
)


# --- detection ---------------------------------------------------------------------


def test_detectors_find_planted_pii() -> None:
    kinds = [(PLANTED[s:e], k) for s, e, k in find(PLANTED, ALL)]
    assert kinds == [
        ("Ana Gómez", "name"),
        ("55 1234 5678", "phone"),
        ("+52 1 55 1234 5678", "phone"),
        ("ana.gomez@example.mx", "email"),
        (CURP, "curp"),
        (RFC, "rfc"),
        ("002180700123456789", "account"),
        ("4111 1111 1111 1111", "account"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "La cita es el 2026-10-05 a las 10:00",
        "El total es 20000 MXN, folio 12345",
        "I am happy to help",  # lower case after 'I am' is not a name
    ],
)
def test_detectors_leave_ordinary_text_alone(text: str) -> None:
    assert find(text, ALL) == []


def test_invalid_card_is_not_an_account() -> None:
    assert find("Tarjeta 4111 1111 1111 1112", {"account"}) == []  # fails the Luhn check


def test_known_names_and_disabled_classes() -> None:
    text = "Hola luis, tu cita con la Dra. López"
    assert [text[s:e] for s, e, _ in find(text, {"name"}, ["Luis", "López"])] == ["luis", "López"]
    assert find(PLANTED, {"email"}) == [
        (PLANTED.index("ana.gomez"), PLANTED.index(", CURP"), "email")
    ]


async def test_tokens_are_stable_reversible_and_tenant_scoped(db: Any) -> None:
    vault = TokenVault(db)
    policy = PiiPolicy(classes=ALL, reveal_in_output={"name"})
    pii = Tokenizer(policy, vault, A)
    safe = await pii.tokenize(PLANTED)
    for value in ["Ana Gómez", "1234 5678", "ana.gomez", CURP, RFC, "002180700123456789", "4111"]:
        assert value not in safe
    phones = [t for t in safe.split() if t.startswith("<PHONE_")]
    assert len(set(p.strip(",") for p in phones)) == 1  # both spellings, one token
    assert await pii.tokenize(safe) == safe  # tokens are never re-tokenized

    shown = await pii.detokenize(safe, policy.reveal_in_output)
    assert shown.startswith("Soy Ana Gómez, mi cel es [phone]")
    assert CURP not in shown and "[curp]" in shown
    full = await pii.detokenize(safe, ALL)
    assert "5512345678" in full and CURP in full

    other = Tokenizer(policy, vault, B)  # the same token means nothing in another tenant
    token = phones[0].strip(",")
    assert await other.detokenize(token, ALL) == "[phone]"
    assert await vault.value_of(B, token) is None


async def test_tokenize_disabled_is_a_no_op(db: Any) -> None:
    pii = Tokenizer(PiiPolicy(classes=ALL, tokenize=False), TokenVault(db), A)
    assert await pii.tokenize(PLANTED) == PLANTED
    assert PiiPolicy(classes={"health", "phone"}).undetectable == {"health"}


# --- consent -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "out"),
    [("BAJA", True), ("stop", True), ("  Alto. ", True), ("STOP please", True),
     ("no quiero parar", False), ("", False), ("bajar de peso", False)],
)  # fmt: skip
def test_opt_out_keywords(text: str, out: bool) -> None:
    assert is_opt_out(text, ["BAJA", "STOP", "ALTO"]) is out


async def test_consent(db: Any) -> None:
    consent = ConsentStore(db)
    phone = "+5215512345678"
    assert not await consent.may_contact(A, phone, "whatsapp", required=True)
    assert await consent.may_contact(A, phone, "whatsapp", required=False)
    await consent.set(A, phone, "whatsapp", "granted", "clinic import")
    assert await consent.may_contact(A, phone, "whatsapp", required=True)
    await consent.set(A, phone, "whatsapp", "revoked", "keyword BAJA")
    assert not await consent.may_contact(A, phone, "whatsapp", required=False)
    assert await consent.get(A, phone, "sms") is None  # per channel
    assert await consent.get(B, phone, "whatsapp") is None  # per tenant


# --- audit -------------------------------------------------------------------------


async def test_audit_chain_detects_tampering(db: Any) -> None:
    audit = AuditLog(db)
    for i in range(4):
        await audit.record(A, "agent:receptionist", "tool_call", "calendar.move_event", {"i": i})
    await audit.record(B, "operator:jag", "approval", "inbox/1", {})
    assert await audit.verify(A) is None and await audit.verify(B) is None
    records = await audit.records(A)
    assert [r.seq for r in records] == [1, 2, 3, 4] and records[2].data == {"i": 2}

    await db.execute(
        "UPDATE audit SET data = ? WHERE tenant_id = ? AND seq = 3",
        (json.dumps({"i": 99}), A.tenant_id),
    )
    assert await audit.verify(A) == 3
    await db.execute(
        "UPDATE audit SET data = ? WHERE tenant_id = ? AND seq = 3", ('{"i": 2}', A.tenant_id)
    )
    assert await audit.verify(A) is None
    await db.execute("DELETE FROM audit WHERE tenant_id = ? AND seq = 2", (A.tenant_id,))
    assert await audit.verify(A) == 3


# --- retention and spend -----------------------------------------------------------


async def test_retention_purges_old_conversations_and_audit(db: Any) -> None:
    store = SqlSessionStore(db)
    old, new = Session(scope=A, agent_id="a"), Session(scope=A, agent_id="a")
    other = Session(scope=B, agent_id="a")
    for s in (old, new, other):
        await store.append(s.started_event())
        await store.append(s.add_message(Message.user("hola")))
    day = 86400
    await db.execute(
        "UPDATE sessions SET last_active = ? WHERE session_id IN (?, ?)",
        (time.time() - 200 * day, old.id, other.id),
    )
    await AuditLog(db).record(A, "system", "boot", "instance")
    removed = await purge(db, A, {"conversations": "180d", "audit": "5y"})
    assert removed["sessions"] == 1 and removed["events"] == 2 and removed["audit"] == 0
    assert await store.list_sessions(A) == [new.id]
    assert await store.list_sessions(B) == [other.id]  # another tenant's policy is its own
    later = await purge(db, A, {"audit": "1d"}, now=time.time() + 2 * day)
    assert later["audit"] == 1


async def test_spend_store(db: Any) -> None:
    spend = SpendStore(db)
    await spend.add(A, "tenant", 0.25)
    await spend.add(A, "tenant", 0.5)
    await spend.add(A, "model:claude-opus-5-5", 0.75)
    await spend.add(A, "tenant", 0.0)
    assert await spend.day(A) == {"tenant": 0.75, "model:claude-opus-5-5": 0.75}
    assert await spend.day(B) == {}


# --- the runtime applies it --------------------------------------------------------


@tool("calendar.lookup")
async def lookup(phone: str) -> dict[str, str]:
    """Find a patient's appointment by phone."""
    return {"patient": "Ana Gómez", "phone": phone, "email": "ana.gomez@example.mx"}


def _clinic(examples: Path, pii: dict[str, Any]) -> Any:
    path = examples / "instances" / "clinica-sonrisa.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["governance"] = {"pii": pii, "audit": {"level": "full"}}
    path.write_text(json.dumps(data), encoding="utf-8")
    resolved = load_instance(path, PackCatalog(roots=[examples]))
    assert resolved.ok, resolved.issues
    return resolved


async def test_runtime_keeps_pii_from_the_model(examples: Path, tmp_path: Path) -> None:
    seen_args: list[dict[str, Any]] = []

    @tool("calendar.lookup")
    async def spy(phone: str) -> dict[str, str]:
        """Find a patient's appointment by phone."""
        seen_args.append({"phone": phone})
        return await lookup.handler(phone=phone)  # type: ignore[no-any-return]

    resolved = _clinic(examples, {"reveal_to_tools": {"calendar.*": ["phone"]}})
    provider = FakeProvider([])
    options = RuntimeOptions(state_root=tmp_path, secrets=EnvSecrets({}), provider=provider)
    async with await Instance.open(resolved, options) as inst:
        assert any(i.code == "pii_undetectable" for i in inst.issues)  # 'health' has no detector
        inst.tools.register(spy)
        agent = inst.agent()
        agent.tools.register(spy)

        text = f"Hola, soy Ana Gómez, mi cel es 55 1234 5678 y mi CURP {CURP}"
        safe = await inst.pii.tokenize(text, ["Ana Gómez"])
        phone_token = next(w for w in safe.replace(",", " ").split() if w.startswith("<PHONE_"))
        provider._script = [
            Message(
                role=Role.ASSISTANT,
                content=[
                    ToolUseBlock(id="t1", name="calendar.lookup", input={"phone": phone_token})
                ],
            ),
            ProviderMessage(
                message=Message.assistant(f"Listo, tu cita sigue en pie. Tel {phone_token}"),
                usage=Usage(input_tokens=1000, output_tokens=100),
                stop_reason="end_turn",
                model="claude-opus-5-5",
            ),
        ]
        session = await agent.new_session(contact_key="+5215512345678")
        events = [e async for e in agent.send(session, text, names=["Ana Gómez"])]

        # nothing the model received holds the planted values
        wire = json.dumps(
            [[m.model_dump(mode="json") for m in r.messages] for r in provider.requests]
        )
        for value in ["Ana Gómez", "1234 5678", "5512345678", CURP, "ana.gomez@example.mx"]:
            assert value not in wire
        # the tool got the real phone (reveal_to_tools), and its result was tokenized
        assert seen_args == [{"phone": "5512345678"}]
        result = session.messages[2].content[0]
        assert isinstance(result, ToolResultBlock) and "Ana Gómez" not in json.dumps(result.content)

        # the reply the contact gets: phone is not in reveal_in_output, so it is masked
        final = events[-2]
        assert final.type == "message_added"
        shown = await agent.reply(final.message.text())
        assert "[phone]" in shown and "1234" not in shown

        # stored events hold tokens, never the values
        rows = await inst.db.fetchall("SELECT data FROM events")
        stored = "".join(r["data"] for r in rows)
        assert CURP not in stored and "Ana Gómez" not in stored

        # the tool call is audited (level full) with tokenized input, and spend persisted
        audit = await inst.audit.records(inst.scope, action="tool_call")
        assert [r.subject for r in audit] == ["calendar.lookup"]
        assert audit[0].data["input"] == {"phone": phone_token}
        assert await inst.audit.verify(inst.scope) is None
        spent = await inst.spend.day(inst.scope)
        assert spent["tenant"] == pytest.approx(0.006) and spent[
            "agent:receptionist"
        ] == pytest.approx(0.006)
        assert spent["model:claude-opus-5-5"] == pytest.approx(0.006)


async def test_daily_budget_survives_restarts(examples: Path, tmp_path: Path) -> None:
    resolved = _clinic(examples, {})
    options = RuntimeOptions(state_root=tmp_path, secrets=EnvSecrets({}), provider=FakeProvider([]))
    async with await Instance.open(resolved, options) as inst:
        await inst.spend.add(inst.scope, "tenant", 3.5)  # clinic's day limit is $3 (pack)
    provider = FakeProvider([Message.assistant("never")])
    options = RuntimeOptions(state_root=tmp_path, secrets=EnvSecrets({}), provider=provider)
    async with await Instance.open(resolved, options) as inst:  # a fresh process
        agent = inst.agent()
        events = [e async for e in agent.send(await agent.new_session(), "hola")]
        assert events[-1].type == "turn_ended" and events[-1].reason == "budget"
        assert provider.requests == []
