"""Tests for src/parsers/*. One test class per file type, per SPEC §5."""

from pathlib import Path

import pytest

from src.parsers import ParserError
from src.parsers.email import parse_email
from src.parsers.pdf import parse_pdf
from src.parsers.pptx import parse_pptx
from src.parsers.xlsx import parse_xlsx

DATA = Path(__file__).resolve().parent.parent / "data"


class TestEmailParser:
    def test_parses_plaintext_order(self):
        raw = (DATA / "emails" / "001_valid_plain_text.eml").read_bytes()
        parsed = parse_email(raw)
        assert parsed.message_id == "<fixture-001@nordwind-bau.example>"
        assert parsed.sender == "purchasing@nordwind-bau.example"
        assert "PO-2026-001" in parsed.body_text
        assert parsed.attachments == []

    def test_extracts_attachment_metadata(self):
        raw = (DATA / "emails" / "002_valid_pdf.eml").read_bytes()
        parsed = parse_email(raw)
        assert len(parsed.attachments) == 1
        attachment = parsed.attachments[0]
        assert attachment.filename == "order_valid.pdf"
        assert attachment.content_type == "application/pdf"
        assert attachment.size_bytes == len(attachment.content) > 0

    def test_malformed_input_raises(self):
        with pytest.raises(ParserError):
            parse_email(b"not an email at all, just bytes")


class TestPdfParser:
    def test_extracts_page_text_with_provenance(self):
        raw = (DATA / "attachments" / "order_valid.pdf").read_bytes()
        parsed = parse_pdf(raw)
        assert parsed.source_type == "pdf"
        assert parsed.segments[0].locator == "page:1"
        assert "SKU-100" in parsed.segments[0].text

    def test_malformed_pdf_raises(self):
        raw = (DATA / "attachments" / "order_malformed.pdf").read_bytes()
        with pytest.raises(ParserError):
            parse_pdf(raw)

    def test_encrypted_pdf_raises(self):
        raw = (DATA / "attachments" / "order_encrypted.pdf").read_bytes()
        with pytest.raises(ParserError):
            parse_pdf(raw)


class TestPptxParser:
    def test_extracts_slide_text_with_provenance(self):
        raw = (DATA / "attachments" / "order_valid.pptx").read_bytes()
        parsed = parse_pptx(raw)
        assert parsed.source_type == "pptx"
        assert parsed.segments[0].locator == "slide:1"
        table_text = "\n".join(segment.text for segment in parsed.segments)
        assert "SKU-100" in table_text

    def test_malformed_pptx_raises(self):
        with pytest.raises(ParserError):
            parse_pptx(b"not a real pptx")


class TestXlsxParser:
    def test_extracts_cell_text_with_provenance(self):
        raw = (DATA / "attachments" / "order_valid.xlsx").read_bytes()
        parsed = parse_xlsx(raw)
        assert parsed.source_type == "xlsx"
        locators = [segment.locator for segment in parsed.segments]
        assert any(loc.startswith("sheet:Order!cell:") for loc in locators)
        values = [segment.text for segment in parsed.segments]
        assert "SKU-100" in values

    def test_malformed_xlsx_raises(self):
        with pytest.raises(ParserError):
            parse_xlsx(b"not a real xlsx")
