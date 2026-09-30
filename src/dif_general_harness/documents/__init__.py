"""Documents: text from PDF, DOCX, XLSX and HTML, and OCR for scans and photos."""

from .extract import DocumentError, Extracted, extract, format_of, media_type, page_range

__all__ = ["DocumentError", "Extracted", "extract", "format_of", "media_type", "page_range"]
