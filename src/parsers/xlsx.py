"""Parses XLSX bytes into cell-level `DocumentSegment`s.

Loaded with `data_only=True` and formulas are never evaluated by this
module - a cell holding a formula is read as its last-cached value only,
never executed. Empty and merged cells are skipped rather than emitted as
noise.
"""

from __future__ import annotations

from io import BytesIO

import openpyxl

from src.parsers import ParserError
from src.schema import DocumentSegment, ParsedDocument


def parse_xlsx(raw_bytes: bytes) -> ParsedDocument:
    """Parses XLSX bytes into one `DocumentSegment` per non-empty cell.

    Args:
        raw_bytes: Full contents of a `.xlsx` file.

    Returns:
        A `ParsedDocument` with `locator` values of the form
        `sheet:<name>!cell:<ref>`.

    Raises:
        ParserError: If the bytes are not a valid XLSX workbook.
    """
    try:
        workbook = openpyxl.load_workbook(BytesIO(raw_bytes), data_only=True, read_only=True)
    except Exception as exc:
        raise ParserError(f"failed to open XLSX: {exc}") from exc

    segments = []
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if cell.value is None or str(cell.value).strip() == "":
                    continue
                segments.append(
                    DocumentSegment(
                        locator=f"sheet:{sheet.title}!cell:{cell.coordinate}",
                        text=str(cell.value).strip(),
                    )
                )
    workbook.close()

    if not segments:
        raise ParserError("XLSX has no non-empty cells")

    return ParsedDocument(source_type="xlsx", segments=segments)
