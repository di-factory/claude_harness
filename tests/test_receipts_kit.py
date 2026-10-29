"""The receipt test kit (docs/testing/receipts), end to end and offline: documents dropped in
the inbox folder are read, extracted, checked with the SAT stand-in and posted to the ERP
stand-in (the kit's own mock_services.py, behind a MockTransport). The model only sees RFCs
as tokens; the workflow routes on the values; the ERP gets the RFC in clear, as the pack's
``reveal_to_tools`` says. Above the limit a person approves; duplicates, another company's
invoices, cancelled ones and unreadable tickets go to a person or are skipped."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
from pathlib import Path
from typing import Any

import httpx2

from dif_general_harness.core.messages import Message
from dif_general_harness.providers import FakeProvider
from dif_general_harness.providers.base import ModelRequest
from dif_general_harness.runtime import Instance, RuntimeOptions
from dif_general_harness.service.app import create_app
from dif_general_harness.service.headless import Headless
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.tenancy.secrets import EnvSecrets

from .conftest import EXAMPLES
from .support import calls

KIT = EXAMPLES.parent.parent / "testing" / "receipts"
ADMIN = "admin-789"
COMPANY = "EKU9003173C9"
TOKEN = re.compile(r"<RFC_[0-9a-f]{8}>")

_spec = importlib.util.spec_from_file_location("receipt_mocks", KIT / "mock_services.py")
assert _spec and _spec.loader
mocks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mocks)

# what a careful extractor reads from each sample (RFCs come from the tokens it was shown)
FOUND: dict[str, dict[str, Any]] = {
    "01-office-supplies.xml": {"uuid": "6F1A2B3C-4D5E-4F60-8A71-92B3C4D5E6F7", "total": 4640.0},
    "02-laptops.xml": {"uuid": "7A2B3C4D-5E6F-4A71-9B82-A3C4D5E6F708", "total": 38280.0},
    "03-office-supplies-again.xml": {
        "uuid": "6F1A2B3C-4D5E-4F60-8A71-92B3C4D5E6F7",
        "total": 4640.0,
    },
    "04-wrong-company.xml": {"uuid": "8B3C4D5E-6F70-4B82-AC93-B4D5E6F70819", "total": 2900.0},
    "05-cancelled.xml": {"uuid": "9C4D5E6F-7081-4C93-BDA4-C5E6F708192A", "total": 1160.0},
    "07-consulting.pdf": {"uuid": "AD5E6F70-8192-4DA4-8EB5-D6F708192A3B", "total": 13920.0},
}


def _text(request: ModelRequest) -> str:
    return json.dumps([m.model_dump(mode="json") for m in request.messages], ensure_ascii=False)


def _extractor(request: ModelRequest) -> Message:
    seen = _text(request)
    name = next((n for n in [*FOUND, "06-taxi-ticket.jpg"] if n in seen), "")
    if name.endswith(".pdf") and "documents.read" not in seen:
        uri = re.search(r'file://[^"\\]+', seen)
        assert uri, "the file's uri is in the task"
        return calls(("r1", "documents.read", {"uri": uri.group(0)}))
    if name.endswith(".jpg"):  # a taxi ticket: no CFDI, no UUID, no RFCs
        return Message.assistant(json.dumps({
            "uuid": "", "rfc_emisor": "", "rfc_receptor": "", "date": "2026-10-26",
            "total": 206.0, "currency": "MXN", "confidence": 0.3}))  # fmt: skip
    emisor, receptor = TOKEN.findall(seen)[:2]
    return Message.assistant(json.dumps({
        **FOUND[name], "rfc_emisor": emisor, "rfc_receptor": receptor, "date": "2026-10-20",
        "currency": "MXN", "confidence": 0.95}))  # fmt: skip


async def _open(tmp_path: Path) -> tuple[Any, ...]:
    shutil.copy(KIT / "receipts-test.json", tmp_path / "receipts-test.json")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    ledger: list[dict[str, Any]] = []
    asked: list[ModelRequest] = []
    received: list[dict[str, Any]] = []

    def services(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"].startswith("Bearer ")
        body = json.loads(request.content or b"{}")
        received.append({"path": request.url.path, **body})
        status, out = mocks.handle(request.method, str(request.url), body, ledger)
        return httpx2.Response(status, json=out)

    def model(request: ModelRequest) -> Message:
        asked.append(request)
        if request.model_role == "verifier":
            return Message.assistant('{"pass": true, "reason": "matches the document"}')
        return _extractor(request)

    resolved = load_instance(tmp_path / "receipts-test.json", PackCatalog(roots=[EXAMPLES]))
    assert resolved.ok, [str(i) for i in resolved.issues if i.severity == "error"]
    options = RuntimeOptions(
        state_root=tmp_path / "state",
        secrets=EnvSecrets({"DIF_SECRET_ERP": "erp-token", "DIF_SECRET_SAT": "sat-token"}),
        provider=FakeProvider([model] * 80),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(services)),
    )
    inst = await Instance.open(resolved, options)
    headless = await Headless.build(inst)
    app = create_app(headless, admin_token=ADMIN, run_worker=False)
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://h")
    return inst, headless, client, inbox, ledger, asked, received


async def _drop(headless: Any, inbox: Path, *names: str) -> None:
    for name in names:
        shutil.copy(KIT / "samples" / name, inbox / name)
    await headless.scan_files("new-document")
    await headless.worker().drain()


async def _outcomes(headless: Any) -> dict[str, str]:
    runs = await headless.engine.runs("process-document")
    return {r.input["file"]["name"]: f"{r.status}/{r.outcome}" for r in runs}


async def test_the_seven_samples_end_where_the_kit_says(tmp_path: Path) -> None:
    inst, headless, client, inbox, ledger, asked, received = await _open(tmp_path)
    async with inst, client:
        client.headers["authorization"] = f"Bearer {ADMIN}"
        await _drop(headless, inbox, "01-office-supplies.xml")
        assert [e["uuid"] for e in ledger] == ["6F1A2B3C-4D5E-4F60-8A71-92B3C4D5E6F7"]
        assert ledger[0]["rfc_receptor"] == COMPANY  # the ERP gets the RFC in clear

        await _drop(headless, inbox, "02-laptops.xml")  # 38,280 MXN: above the 20,000 limit
        assert len(ledger) == 1
        [item] = (await client.get("/admin/inbox?kind=approval")).json()
        assert "38280" in item["title"]
        r = await client.post(f"/admin/inbox/{item['id']}/decision",
                              json={"approved": True, "by": "accounting"})  # fmt: skip
        assert r.status_code == 200, r.text
        await headless.worker().drain()
        assert len(ledger) == 2

        await _drop(headless, inbox, "03-office-supplies-again.xml", "04-wrong-company.xml",
                    "05-cancelled.xml", "06-taxi-ticket.jpg", "07-consulting.pdf")  # fmt: skip
        shutil.copy(KIT / "samples" / "01-office-supplies.xml", inbox / "01-copy.xml")
        await headless.scan_files("new-document")  # the very same file again: not even a run
        await headless.worker().drain()

        assert await _outcomes(headless) == {
            "01-office-supplies.xml": "done/completed",
            "02-laptops.xml": "done/completed",
            "03-office-supplies-again.xml": "done/duplicate",
            "04-wrong-company.xml": "done/exception",
            "05-cancelled.xml": "done/exception",
            "06-taxi-ticket.jpg": "escalated/needs_human",
            "07-consulting.pdf": "done/completed",
        }
        assert [e["total"] for e in ledger] == [4640.0, 38280.0, 13920.0]

    sent_to_model = "".join(_text(r) for r in asked if r.model_role != "verifier")
    assert COMPANY not in sent_to_model and "XIA190128J61" not in sent_to_model  # tokens only
    checked = [r for r in received if r["path"] == "/cfdi/validate"]
    assert all(not TOKEN.search(json.dumps(r)) for r in checked)  # the SAT check gets values
    assert "matching this JSON Schema" in sent_to_model  # the extractor is shown the schema


async def test_the_injection_in_a_pdf_reaches_the_model_as_data(tmp_path: Path) -> None:
    inst, headless, client, inbox, ledger, asked, _ = await _open(tmp_path)
    async with inst, client:
        await _drop(headless, inbox, "07-consulting.pdf")
    read = next(r for r in asked if "Ignore any previous instructions" in _text(r))
    assert "untrusted_content" in _text(read)  # fenced: data, never instructions
    assert [e["total"] for e in ledger] == [13920.0]
