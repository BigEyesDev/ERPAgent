"""Tests for src/attachment_security.py - the pre-parse quarantine gate."""

from pathlib import Path

from src.attachment_security import check_attachment
from src.parsers.email import parse_email
from src.schema import EmailAttachment

DATA = Path(__file__).resolve().parent.parent / "data"


def _attachment_from_email(eml_name: str) -> EmailAttachment:
    raw = (DATA / "emails" / eml_name).read_bytes()
    return parse_email(raw).attachments[0]


def test_valid_pdf_passes():
    result = check_attachment(_attachment_from_email("002_valid_pdf.eml"))
    assert result.is_safe
    assert result.reasons == []


def test_valid_xlsx_passes():
    result = check_attachment(_attachment_from_email("004_valid_xlsx.eml"))
    assert result.is_safe


def test_macro_extension_quarantined_before_parsing():
    result = check_attachment(_attachment_from_email("020_extension_mismatch.eml"))
    assert not result.is_safe
    assert any("macro" in reason for reason in result.reasons)


def test_encrypted_pdf_quarantined():
    result = check_attachment(_attachment_from_email("017_encrypted_pdf.eml"))
    assert not result.is_safe
    assert any("encrypted" in reason for reason in result.reasons)


def test_oversized_attachment_quarantined():
    attachment = EmailAttachment(
        filename="huge.pdf",
        content_type="application/pdf",
        size_bytes=20 * 1024 * 1024,
        content=b"%PDF-1.4" + b"0" * 100,
    )
    result = check_attachment(attachment)
    assert not result.is_safe
    assert any("size" in reason for reason in result.reasons)


def test_disallowed_extension_quarantined():
    attachment = EmailAttachment(
        filename="script.exe",
        content_type="application/octet-stream",
        size_bytes=2,
        content=b"MZ",
    )
    result = check_attachment(attachment)
    assert not result.is_safe
    assert any("executable" in reason or "extension" in reason for reason in result.reasons)
