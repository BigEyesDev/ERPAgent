"""Pre-parse security gate for email attachments.

Runs before any `parsers/*` module or the LLM sees attachment bytes.
Rejection here means the attachment is never opened by a parser - the
allowlist and size checks are cheap and evaluated first, so an oversized or
disallowed file never reaches the more expensive encrypted/macro sniffing.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from io import BytesIO

import fitz 

from src.schema import EmailAttachment

ALLOWED_EXTENSIONS = {"pdf", "pptx", "xlsx"}
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

MACRO_EXTENSIONS = {"xlsm", "pptm", "docm", "xltm", "potm"}

EXECUTABLE_MAGIC_BYTES = (b"MZ", b"\x7fELF")


@dataclass(frozen=True)
class SecurityCheckResult:
    """Outcome of screening one attachment."""

    is_safe: bool
    reasons: list[str]


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _is_encrypted_pdf(content: bytes) -> bool:
    try:
        document = fitz.open(stream=content, filetype="pdf")
    except Exception:
        return False
    encrypted = document.is_encrypted
    document.close()
    return encrypted


def _has_embedded_macro(content: bytes) -> bool:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            return any("vbaProject" in name for name in archive.namelist())
    except zipfile.BadZipFile:
        return False


def check_attachment(attachment: EmailAttachment) -> SecurityCheckResult:
    """Screens one attachment before it is handed to a parser.

    Args:
        attachment: The attachment as extracted by `parsers/email.py`.

    Returns:
        A `SecurityCheckResult`. `is_safe=False` means the attachment must
        route to `SECURITY_QUARANTINE` and must not reach a parser.
    """
    reasons: list[str] = []
    extension = _extension(attachment.filename)

    if extension in MACRO_EXTENSIONS:
        reasons.append(f"macro-enabled extension not allowed: .{extension}")
    elif extension not in ALLOWED_EXTENSIONS:
        reasons.append(f"extension not in allowlist: .{extension}")

    if attachment.size_bytes > MAX_ATTACHMENT_BYTES:
        reasons.append(f"attachment exceeds max size ({MAX_ATTACHMENT_BYTES} bytes)")

    if any(attachment.content.startswith(magic) for magic in EXECUTABLE_MAGIC_BYTES):
        reasons.append("executable file signature detected")

    # only for extensions where the check is meaningful.
    if not reasons:
        if extension == "pdf" and _is_encrypted_pdf(attachment.content):
            reasons.append("encrypted PDF not allowed")
        elif extension in {"pptx", "xlsx"} and _has_embedded_macro(attachment.content):
            reasons.append("embedded macro (vbaProject) detected")

    return SecurityCheckResult(is_safe=not reasons, reasons=reasons)
