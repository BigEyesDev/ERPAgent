"""Parses PDF bytes into page-level `DocumentSegment`s.

Text-only: no OCR. A textless (image-only) page is reported as an explicit
empty-text segment with a `[no extractable text]` marker rather than being
dropped, so downstream stages can tell "page had nothing" apart from "page
was never read."
"""

from __future__ import annotations

import fitz  # PyMuPDF

from src.parsers import ParserError
from src.schema import DocumentSegment, ParsedDocument

NO_TEXT_MARKER = "[no extractable text]"


def parse_pdf(raw_bytes: bytes) -> ParsedDocument:
    """Parses PDF bytes into one `DocumentSegment` per page.

    Args:
        raw_bytes: Full contents of a `.pdf` file.

    Returns:
        A `ParsedDocument` with `locator` values of the form `page:<n>`
        (1-indexed).

    Raises:
        ParserError: If the bytes are not a valid, unencrypted PDF.
    """
    try:
        document = fitz.open(stream=raw_bytes, filetype="pdf")
    except Exception as exc:
        raise ParserError(f"failed to open PDF: {exc}") from exc

    if document.is_encrypted:
        document.close()
        raise ParserError("PDF is encrypted; must be quarantined before parsing")

    segments = []
    for page_index, page in enumerate(document, start=1):
        text = page.get_text().strip()
        segments.append(
            DocumentSegment(locator=f"page:{page_index}", text=text or NO_TEXT_MARKER)
        )
    document.close()

    if not segments:
        raise ParserError("PDF has no pages")

    return ParsedDocument(source_type="pdf", segments=segments)
