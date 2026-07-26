"""Parses raw `.eml` bytes into a `ParsedEmail`.

Ingestion entry point of the pipeline - every other parser is invoked on an
attachment this module extracts, never called directly on raw email bytes.
"""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from src.parsers import ParserError
from src.schema import EmailAttachment, ParsedEmail


def parse_email(raw_bytes: bytes) -> ParsedEmail:
    """Parses raw `.eml` content into a `ParsedEmail`.

    Args:
        raw_bytes: Full contents of a `.eml` file.

    Returns:
        A `ParsedEmail` with normalized body text/HTML and attachment metadata.

    Raises:
        ParserError: If the bytes are not a parseable email, or a required
            header (`Message-ID`, `From`) is missing.
    """
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    except Exception as exc:
        raise ParserError(f"failed to parse email bytes: {exc}") from exc

    message_id = message.get("Message-ID")
    sender = message.get("From")
    if not message_id or not sender:
        raise ParserError("email is missing required Message-ID or From header")

    recipients = [addr for _, addr in getaddresses(message.get_all("To", []))]

    timestamp = None
    date_header = message.get("Date")
    if date_header:
        try:
            timestamp = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            timestamp = None

    body_part = message.get_body(preferencelist=("plain",))
    body_text = body_part.get_content() if body_part else ""

    html_part = message.get_body(preferencelist=("html",))
    body_html = html_part.get_content() if html_part else None

    attachments = [
        EmailAttachment(
            filename=part.get_filename() or "unnamed",
            content_type=part.get_content_type(),
            size_bytes=len(part.get_payload(decode=True) or b""),
            content=part.get_payload(decode=True) or b"",
        )
        for part in message.iter_attachments()
    ]

    return ParsedEmail(
        message_id=message_id,
        sender=sender,
        recipients=recipients,
        subject=message.get("Subject", ""),
        timestamp=timestamp,
        body_text=body_text.strip(),
        body_html=body_html,
        attachments=attachments,
    )
