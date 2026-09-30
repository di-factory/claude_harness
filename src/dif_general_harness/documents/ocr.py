"""OCR with a vision model: photos, scans and PDFs without a text layer.

The request goes to the ``ocr`` model role when the spec defines one, else to ``main``; it is
charged like any model call, and the provider's region policy applies as for every role.
Anthropic models read images and PDFs; OpenAI-compatible servers read images (and PDFs when
the server supports file parts).
"""

from __future__ import annotations

import base64
import io
from typing import TYPE_CHECKING

from ..core.messages import MediaBlock, Message, Role, TextBlock
from ..providers.base import ModelRequest, ProviderMessage
from .extract import DocumentError, page_range

if TYPE_CHECKING:
    from ..runtime.instance import Instance

MAX_OCR_BYTES = 20 * 1024 * 1024
MAX_OCR_PAGES = 20
OCR_PROMPT = """You transcribe documents. Write out all the text in the document exactly as
written, in reading order: keep numbers, dates, amounts, ids and names character for
character; write table rows as cells separated by " | "; mark text you cannot read as
[illegible]. Output only the transcription, with no comments."""


def pdf_pages(data: bytes, pages: str | None) -> tuple[bytes, int]:
    """The chosen pages of a PDF as a new PDF (so OCR reads only what was asked)."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        chosen = page_range(pages, len(reader.pages))
    except (PdfReadError, ValueError, KeyError) as exc:
        if isinstance(exc, DocumentError):
            raise
        raise DocumentError(f"unreadable PDF: {exc}") from None
    if len(chosen) > MAX_OCR_PAGES:
        raise DocumentError(f"OCR reads at most {MAX_OCR_PAGES} pages per call; pass pages")
    if not pages:
        return data, len(chosen)
    writer = PdfWriter()
    for i in chosen:
        writer.add_page(reader.pages[i])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), len(chosen)


def ocr_role(inst: Instance) -> str:
    roles = inst.spec.models.roles if inst.spec.models else {}
    return "ocr" if "ocr" in roles else "main"


async def transcribe(inst: Instance, data: bytes, media_type: str) -> str:
    if inst.provider is None:
        raise DocumentError("no model provider is configured for OCR")
    if len(data) > MAX_OCR_BYTES:
        raise DocumentError(f"the document is over {MAX_OCR_BYTES // 1024 // 1024} MB")
    role = ocr_role(inst)
    media = MediaBlock(media_type=media_type, data=base64.b64encode(data).decode())
    request = ModelRequest(
        system=OCR_PROMPT,
        messages=[Message(role=Role.USER, content=[media, TextBlock(text="Transcribe this.")])],
        tools=[],
        model_role=role,
    )
    final: ProviderMessage | None = None
    async for event in inst.provider.stream(request):
        if isinstance(event, ProviderMessage):
            final = event
    if final is None:
        raise DocumentError("the OCR model returned nothing")
    await inst.charge("documents", role, final)
    return final.message.text().strip()
