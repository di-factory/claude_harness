"""The ``documents`` tool pack: read the files a solution receives.

- ``documents.read``: the text of a PDF (its text layer), DOCX, XLSX, HTML or text file;
  says ``needs_ocr`` for photos and scans.
- ``documents.ocr``: the text of an image or a scanned PDF, read by a vision model.

Documents are named by ``uri`` (as a ``file`` trigger announces them) and only the
solution's own sources are readable: the folders and buckets of its file triggers and the
pack's ``storage`` (``tools.config.documents.storage``, a folder or S3 source). Any other
path or bucket is refused, so a prompt cannot read the host's files.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ...documents.extract import DocumentError, extract, format_of, media_type
from ..registry import Tool, tool

MAX_CHARS = 20_000
Reader = Callable[[str], Awaitable[bytes]]
Transcriber = Callable[[bytes, str], Awaitable[str]]


def _name(uri: str) -> str:
    return uri.rstrip("/").rsplit("/", 1)[-1]


def documents_tools(read: Reader, transcribe: Transcriber) -> list[Tool]:
    @tool("documents.read")
    async def documents_read(
        uri: str, pages: str = "", max_chars: int = MAX_CHARS
    ) -> dict[str, Any]:
        """Read the text of a document (PDF, DOCX, XLSX, HTML, XML, CSV, text) by its uri.
        pages: e.g. "1-3,5" (PDF only). When needs_ocr is true (a photo or a scan), call
        documents.ocr instead."""
        data = await read(uri)
        found = extract(data, _name(uri), pages or None)
        limit = max(1000, min(int(max_chars), 200_000))
        return {
            "uri": uri,
            "format": found.format,
            "pages": len(found.pages) or None,
            "text": found.text[:limit],
            "truncated": len(found.text) > limit,
            "needs_ocr": found.needs_ocr,
        }

    @tool("documents.ocr")
    async def documents_ocr(uri: str, pages: str = "") -> dict[str, Any]:
        """Transcribe an image (PNG, JPEG, WebP, GIF) or a scanned PDF with a vision model.
        pages: e.g. "1-2" (PDF only; at most 20 pages per call)."""
        from ...documents.ocr import pdf_pages

        name = _name(uri)
        kind = media_type(name)
        if kind is None:
            fmt = format_of(name)
            raise DocumentError(
                f"{name} has a text format; use documents.read"
                if fmt
                else f"{name}: format not supported for OCR"
            )
        data = await read(uri)
        count = 1
        if kind == "application/pdf":
            data, count = pdf_pages(data, pages or None)
        text = await transcribe(data, kind)
        return {"uri": uri, "pages": count, "text": text, "method": "ocr"}

    return [documents_read, documents_ocr]
