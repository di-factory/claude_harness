"""The documents pack (G3): text from PDF, DOCX and XLSX, OCR for scans and photos."""

from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.core.messages import MediaBlock, Message, Role, ToolResultBlock
from dif_general_harness.core.scope import Scope
from dif_general_harness.documents import DocumentError, extract, page_range
from dif_general_harness.documents.ocr import pdf_pages
from dif_general_harness.knowledge import KnowledgeBase
from dif_general_harness.providers.anthropic import to_anthropic_messages
from dif_general_harness.providers.openai_compat import to_openai_messages
from dif_general_harness.runtime import answer
from dif_general_harness.store.db import connect
from dif_general_harness.store.schema import migrate
from tests.support import Env, calls


def make_pdf(pages: list[str]) -> bytes:
    """A small, valid PDF: one line of Helvetica text per page ("" for a blank, scan-like page)."""
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")  # 3
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        content = len(objects)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R"
            b" /Resources << /Font << /F1 3 0 R >> >> >>" % content
        )
        kids.append(len(objects))
    refs = b" ".join(b"%d 0 R" % k for k in kids)
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (refs, len(kids))
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (number, body))
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, xref))  # fmt: skip
    return out.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def make_docx() -> bytes:
    body = (
        f"<w:document {W}><w:body>"
        "<w:p><w:r><w:t>Contrato de servicios</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Concepto</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Monto</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>Limpieza</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>1,200.00</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "<w:p><w:r><w:t>Vigencia:</w:t></w:r><w:r><w:tab/><w:t>12 meses</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("word/document.xml", body)
    return out.getvalue()


def make_xlsx() -> bytes:
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    shared = f"<sst {ns}><si><t>RFC</t></si><si><t>Total</t></si><si><t>AAA010101AAA</t></si></sst>"
    sheet = (
        f"<worksheet {ns}><sheetData>"
        '<row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row>'
        '<row><c t="s"><v>2</v></c><c><v>1160.5</v></c></row>'
        '<row><c t="inlineStr"><is><t>nota, con coma</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("xl/sharedStrings.xml", shared)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return out.getvalue()


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_text_comes_out_of_each_format() -> None:
    pdf = extract(make_pdf(["Factura A-17 total 1160.00", "Pagina dos"]), "f.pdf")
    assert pdf.format == "pdf" and len(pdf.pages) == 2 and not pdf.needs_ocr
    assert "Factura A-17 total 1160.00" in pdf.text and "[page 2]" in pdf.text
    second = extract(make_pdf(["uno", "dos"]), "f.pdf", pages="2")
    assert second.pages == ["dos"]

    docx = extract(make_docx(), "contrato.docx").text.splitlines()
    assert docx == ["Contrato de servicios", "Concepto | Monto", "Limpieza | 1,200.00",
                    "Vigencia:\t12 meses"]  # fmt: skip
    xlsx = extract(make_xlsx(), "cuentas.xlsx")
    assert xlsx.text.splitlines() == [
        "[sheet 1]", "RFC,Total", "AAA010101AAA,1160.5", '"nota, con coma"'
    ]  # fmt: skip
    assert extract(b"<h1>Hola</h1><p>mundo</p>", "a.html").text.startswith("# Hola")
    assert extract(PNG, "ticket.png").needs_ocr
    assert extract(make_pdf(["", ""]), "scan.pdf").needs_ocr  # no text layer: a scan

    for data, name in [(b"not a zip", "x.docx"), (b"%PDF-1.4 broken", "x.pdf"), (b"", "x.exe")]:
        with pytest.raises(DocumentError):
            extract(data, name)
    bomb = f'<!DOCTYPE d [<!ENTITY a "aaaa">]><w:document {W}/>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", bomb)
    with pytest.raises(DocumentError, match="DTD"):
        extract(buf.getvalue(), "x.docx")
    assert page_range("1-2,5", 4) == [0, 1] and page_range(None, 2) == [0, 1]
    with pytest.raises(DocumentError):
        page_range("3-1", 5)
    subset, count = pdf_pages(make_pdf(["a", "b", "c"]), "2-3")
    assert count == 2 and extract(subset, "s.pdf").pages == ["b", "c"]


def test_media_reaches_both_provider_kinds() -> None:
    msg = Message(
        role=Role.USER,
        content=[MediaBlock(media_type="application/pdf", data="JVBE"),
                 MediaBlock(media_type="image/png", data="iVBO")],
    )  # fmt: skip
    [anthropic] = to_anthropic_messages([msg])
    assert [b["type"] for b in anthropic["content"]] == ["document", "image"]
    assert anthropic["content"][1]["source"] == {
        "type": "base64", "media_type": "image/png", "data": "iVBO"
    }  # fmt: skip
    [openai] = to_openai_messages("", [msg])
    assert openai["content"][0]["file"]["file_data"] == "data:application/pdf;base64,JVBE"
    assert openai["content"][1] == {
        "type": "image_url", "image_url": {"url": "data:image/png;base64,iVBO"}
    }  # fmt: skip


def _documents(storage: Path) -> Any:
    def edit(spec: dict[str, Any]) -> None:
        spec["tools"]["packs"].append("documents")
        folder = {"type": "folder", "path": str(storage)}
        spec["tools"]["config"] = {"documents": {"storage": folder}}
        spec["models"]["roles"]["ocr"] = {"provider": "anthropic", "model": "claude-haiku-4-5"}
        spec["agents"]["ops"]["tools"] = ["documents.*"]

    return edit


def _results(request: Any) -> list[ToolResultBlock]:
    return [b for m in request.messages for b in m.content if isinstance(b, ToolResultBlock)]


async def test_agents_read_and_ocr_their_own_documents(tmp_path: Path) -> None:
    storage = tmp_path / "docs"
    storage.mkdir()
    (storage / "contrato.docx").write_bytes(make_docx())
    (storage / "ticket.png").write_bytes(PNG)
    (tmp_path / "secret.txt").write_text("host file")
    base = storage.resolve().as_uri()
    script = [
        calls(
            ("d1", "documents.read", {"uri": f"{base}/contrato.docx"}),
            ("d2", "documents.read", {"uri": f"{base}/ticket.png"}),
            ("d3", "documents.read", {"uri": (tmp_path / "secret.txt").resolve().as_uri()}),
            ("d4", "documents.read", {"uri": f"{base}/../secret.txt"}),
        ),
        calls(("o1", "documents.ocr", {"uri": f"{base}/ticket.png"})),
        Message.assistant("OXXO 12/09/2026 TOTAL 85.50"),  # the OCR model's transcription
        Message.assistant("El ticket es de OXXO por 85.50."),
    ]
    env = Env(tmp_path, script, edit=_documents(storage))
    inst, _, client = await env.open()
    async with inst, client:
        agent = inst.agent("ops")
        session = await agent.new_session()
        texts, _ = await answer(agent.send(session, "Revisa el contrato y el ticket"))
        assert texts == ["El ticket es de OXXO por 85.50."]

        read, image, outside, escape = _results(env.provider.requests[1])
        assert "Limpieza | 1,200.00" in read.content["text"]
        assert image.content["needs_ocr"] is True
        assert outside.status == "error" and "not in this solution" in (outside.error or "")
        assert escape.status == "error"  # no way out of the folder

        ocr_request = env.provider.requests[2]
        assert ocr_request.model_role == "ocr" and ocr_request.tools == []
        [media] = [b for b in ocr_request.messages[0].content if isinstance(b, MediaBlock)]
        assert media.media_type == "image/png" and base64.b64decode(media.data) == PNG
        [ocr] = _results(env.provider.requests[3])[-1:]
        assert ocr.content == {"uri": f"{base}/ticket.png", "pages": 1,
                               "text": "OXXO 12/09/2026 TOTAL 85.50", "method": "ocr"}  # fmt: skip
        usage = await inst.db.fetchall("SELECT role FROM usage")
        assert "ocr" in {r["role"] for r in usage}  # OCR is charged like any model call


async def test_knowledge_indexes_pdf_and_docx_text(tmp_path: Path) -> None:
    folder = tmp_path / "kb"
    folder.mkdir()
    (folder / "politicas.pdf").write_bytes(make_pdf(["Reembolsos en 30 dias naturales"]))
    (folder / "contrato.docx").write_bytes(make_docx())
    (folder / "escaneo.pdf").write_bytes(make_pdf([""]))  # needs OCR: skipped, not guessed
    db = await connect(f"sqlite:///{tmp_path / 'kb.db'}")
    await migrate(db)
    corpora = {"docs": {"sources": [{"type": "file", "path": str(folder)}]}}
    kb = KnowledgeBase(db, Scope(tenant_id="acme", instance_id="desk"), corpora)
    report = await kb.sync("docs")
    assert report.added == 2 and report.skipped == [str(folder / "escaneo.pdf")]
    [hit, *_] = await kb.search("docs", "reembolsos 30 dias")
    assert "30 dias" in hit.text
    await db.close()
