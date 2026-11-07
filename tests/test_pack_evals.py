"""The example packs' eval suites: every suite a pack declares exists, every case is one the
runner can check (a skipped case gates nothing), and the receipt pack's suites run end to
end offline, with a model scripted as a careful extractor and the SAT and ERP as fixtures."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.constructor.evals import load_suite, run_suites, unsupported
from dif_general_harness.core.messages import Message
from dif_general_harness.providers import FakeProvider
from dif_general_harness.providers.base import ModelRequest
from dif_general_harness.runtime import Instance, RuntimeOptions
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.spec.loader import load_pack
from dif_general_harness.tenancy.secrets import EnvSecrets

from .conftest import EXAMPLES, PACK_IDS
from .support import calls

KIT = EXAMPLES.parent.parent / "testing" / "receipts"
TOKEN = re.compile(r"<RFC_[0-9a-f]{8}>")


@pytest.mark.parametrize("pack", PACK_IDS)
def test_every_declared_suite_exists_and_every_case_can_run(pack: str) -> None:
    folder = EXAMPLES / pack
    resolved = load_pack(folder / "pack.json")
    roles = set((resolved.data.get("models") or {}).get("roles") or {})
    for suite in (resolved.data.get("evals") or {}).get("suites") or []:
        path = folder / suite
        assert path.is_file(), f"{pack}: {suite} is declared but not written"
        for case in load_suite(path):
            assert not unsupported(case, roles), (pack, suite, case["id"], unsupported(case))


def _text(request: ModelRequest) -> str:
    return json.dumps([m.model_dump(mode="json") for m in request.messages], ensure_ascii=False)


def _extractor(request: ModelRequest) -> Message:
    """Reads what it was given: the XML inline, or the OCR text of a photo."""
    seen = _text(request)
    if request.model_role == "verifier":
        return Message.assistant('{"pass": true, "reason": "matches the document"}')
    if ".jpg" in seen and "documents.ocr" not in seen:
        uri = re.search(r"file:///inbox/[\w.-]+", seen)
        assert uri
        return calls(("o1", "documents.ocr", {"uri": uri.group(0)}))
    total = re.search(r'Total=[\\"]*([\d.]+)', seen)  # the XML, escaped once per JSON layer
    uuid = re.search(r'UUID=[\\"]*([0-9A-F-]{36})', seen)
    if not total or not uuid:  # a ticket: no CFDI, so nothing to be sure of
        return Message.assistant(json.dumps({
            "uuid": "", "rfc_emisor": "", "rfc_receptor": "", "date": "2026-10-26",
            "total": 206.0, "currency": "MXN", "confidence": 0.3}))  # fmt: skip
    emisor, receptor = TOKEN.findall(seen)[:2]
    return Message.assistant(json.dumps({
        "uuid": uuid.group(1), "rfc_emisor": emisor, "rfc_receptor": receptor,
        "date": "2026-10-20", "total": float(total.group(1)), "currency": "MXN",
        "confidence": 0.95}))  # fmt: skip


async def test_the_receipt_suites_pass_offline(tmp_path: Path) -> None:
    shutil.copy(KIT / "receipts-test.json", tmp_path / "receipts-test.json")
    resolved = load_instance(tmp_path / "receipts-test.json", PackCatalog(roots=[EXAMPLES]))
    assert resolved.ok

    async def open_instance(work: Path) -> Instance:
        options = RuntimeOptions(
            state_root=work,
            secrets=EnvSecrets({"DIF_SECRET_ERP": "e", "DIF_SECRET_SAT": "s"}),
            provider=FakeProvider([_extractor] * 40),
        )
        return await Instance.open(resolved, options)

    evals = EXAMPLES / "pyme-receipt-processing" / "evals"
    suites = [evals / f for f in ("extraction.yaml", "limits.yaml", "duplicates.yaml")]
    report = await run_suites(open_instance, suites, work=tmp_path / "evals")
    outcome: dict[str, Any] = {r.case: (r.status, r.reasons) for r in report.results}
    assert all(status == "passed" for status, _ in outcome.values()), outcome
    assert len(outcome) == 7 and sum(r.unsafe_actions for r in report.results) == 0
