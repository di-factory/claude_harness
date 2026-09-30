"""Text from documents: PDF (its text layer), DOCX, XLSX, HTML and plain text formats.

Everything here is local and deterministic. What has no text layer (photos, scans, a PDF
of images) comes back with ``needs_ocr`` set; ``documents.ocr`` reads those with a vision
model (``ocr.py``).
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from xml.etree import ElementTree

from ..knowledge.chunk import TEXT_SUFFIXES, html_to_markdown

MIN_CHARS_PER_PAGE = 3  # a PDF page with (almost) no text layer is an image: a scan
MAX_ZIP_MEMBER = 50 * 1024 * 1024  # a DOCX/XLSX part larger than this is refused (zip bombs)
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class DocumentError(ValueError):
    pass


@dataclass
class Extracted:
    format: str  # pdf, docx, xlsx, html, text, image
    text: str
    pages: list[str] = field(default_factory=list)  # per page (PDF) or sheet (XLSX)
    needs_ocr: bool = False


def format_of(name: str) -> str | None:
    suffix = PurePosixPath(name.lower()).suffix
    if suffix in (".pdf", ".docx", ".xlsx"):
        return suffix[1:]
    if suffix in IMAGE_TYPES:
        return "image"
    if suffix in (".html", ".htm"):
        return "html"
    if suffix in TEXT_SUFFIXES or suffix in (".xml", ".json", ".csv", ".tsv", ".yaml", ".yml"):
        return "text"
    return None


def media_type(name: str) -> str | None:
    suffix = PurePosixPath(name.lower()).suffix
    return "application/pdf" if suffix == ".pdf" else IMAGE_TYPES.get(suffix)


def page_range(spec: str | None, count: int) -> list[int]:
    """``"1-3,5"`` (1-based, inclusive) as 0-based indexes within ``count`` pages."""
    if not spec:
        return list(range(count))
    wanted: list[int] = []
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        try:
            start, end = int(a), int(b or a)
        except ValueError:
            raise DocumentError(f"bad page range {spec!r} (use e.g. 1-3,5)") from None
        if start < 1 or end < start:
            raise DocumentError(f"bad page range {spec!r}")
        wanted += [i - 1 for i in range(start, min(end, count) + 1) if i - 1 not in wanted]
    return wanted


def extract(data: bytes, name: str, pages: str | None = None) -> Extracted:
    fmt = format_of(name)
    if fmt is None:
        raise DocumentError(f"{name}: format not supported")
    if fmt == "pdf":
        return _pdf(data, pages)
    if fmt == "docx":
        return Extracted("docx", _docx(data))
    if fmt == "xlsx":
        sheets = _xlsx(data)
        return Extracted("xlsx", "\n\n".join(sheets), pages=sheets)
    if fmt == "image":
        return Extracted("image", "", needs_ocr=True)
    text = _decode(data)
    if fmt == "html":
        return Extracted("html", re.sub(r"\n{3,}", "\n\n", html_to_markdown(text)).strip())
    return Extracted("text", text)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _pdf(data: bytes, pages: str | None) -> Extracted:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocumentError("the PDF is password-protected")
        chosen = page_range(pages, len(reader.pages))
        texts = [_clean(reader.pages[i].extract_text() or "") for i in chosen]
    except (PdfReadError, ValueError, KeyError) as exc:
        if isinstance(exc, DocumentError):
            raise
        raise DocumentError(f"unreadable PDF: {exc}") from None
    scanned = sum(len(t.strip()) < MIN_CHARS_PER_PAGE for t in texts)
    return Extracted(
        "pdf",
        "\n\n".join(f"[page {i + 1}]\n{t}" for i, t in zip(chosen, texts, strict=True)),
        pages=texts,
        needs_ocr=bool(texts) and scanned * 2 >= len(texts),  # mostly images: read it by OCR
    )


def _clean(text: str) -> str:
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def _zip_part(archive: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_ZIP_MEMBER:
        raise DocumentError(f"{name} is too large")
    return archive.read(info)


def _open_zip(data: bytes, kind: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise DocumentError(f"not a valid {kind} file") from None


def _xml(data: bytes) -> ElementTree.Element:
    if b"<!DOCTYPE" in data[:2048] or b"<!ENTITY" in data[:4096]:
        raise DocumentError("documents with DTDs or entities are refused")
    return ElementTree.fromstring(data)


def _docx(data: bytes) -> str:
    with _open_zip(data, "DOCX") as archive:
        part = _zip_part(archive, "word/document.xml")
        if part is None:
            raise DocumentError("not a valid DOCX file (no word/document.xml)")
        root = _xml(part)
    body = root.find(f"{_W}body")
    lines = _blocks(body if body is not None else root)
    return "\n".join(line for line in lines if line.strip())


def _blocks(parent: ElementTree.Element) -> list[str]:
    """Paragraphs and tables in document order (content controls are opened)."""
    lines: list[str] = []
    for block in parent:
        if block.tag == f"{_W}p":
            lines.append(_paragraph(block))
        elif block.tag == f"{_W}tbl":
            for row in block.iter(f"{_W}tr"):
                cells = [
                    " ".join(_paragraph(p) for p in cell.iter(f"{_W}p")).strip()
                    for cell in row.iter(f"{_W}tc")
                ]
                lines.append(" | ".join(cells))
        elif block.tag in (f"{_W}sdt", f"{_W}sdtContent", f"{_W}customXml"):
            lines += _blocks(block)
    return lines


def _paragraph(p: ElementTree.Element) -> str:
    parts = []
    for node in p.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    return "".join(parts)


def _xlsx(data: bytes) -> list[str]:
    with _open_zip(data, "XLSX") as archive:
        shared: list[str] = []
        raw = _zip_part(archive, "xl/sharedStrings.xml")
        if raw is not None:
            for si in _xml(raw).iter(f"{_S}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_S}t")))
        names = sorted(
            (n for n in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
            key=lambda n: int(re.sub(r"\D", "", n)),
        )
        sheets = []
        for n, name in enumerate(names, start=1):
            rows = []
            for row in _xml(_zip_part(archive, name) or b"<x/>").iter(f"{_S}row"):
                cells = []
                for c in row.iter(f"{_S}c"):
                    v = c.find(f"{_S}v")
                    inline = c.find(f"{_S}is")
                    if c.get("t") == "s" and v is not None and v.text is not None:
                        cells.append(shared[int(v.text)] if int(v.text) < len(shared) else "")
                    elif inline is not None:
                        cells.append("".join(t.text or "" for t in inline.iter(f"{_S}t")))
                    else:
                        cells.append(v.text or "" if v is not None else "")
                rows.append(",".join(_csv_cell(x) for x in cells))
            sheets.append(f"[sheet {n}]\n" + "\n".join(rows))
        return sheets


def _csv_cell(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"' if any(c in value for c in ',"\n') else value
