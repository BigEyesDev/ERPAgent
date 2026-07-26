"""Append-only audit trail.

Every pipeline stage - including rejections and escalations, not just
successful ERP writes - records one `AuditEvent` here. The log is
write-once: `record` only ever opens the file in append mode, and there is
no function anywhere in this module that truncates or rewrites a line.
"""

from __future__ import annotations

from pathlib import Path

from src.config import settings
from src.schema import AuditEvent


def _resolve_path(path: str | Path | None) -> Path:
    resolved = Path(path) if path is not None else Path(settings.audit_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def record(event: AuditEvent, *, path: str | Path | None = None) -> None:
    """Appends one audit event as a JSON line.

    Args:
        event: The event to record.
        path: Override for `settings.audit_path`, mainly for tests.
    """
    resolved = _resolve_path(path)
    with resolved.open("a", encoding="utf-8") as handle:
        handle.write(event.model_dump_json() + "\n")


def read_all(*, path: str | Path | None = None) -> list[AuditEvent]:
    """Reads every recorded event, in the order they were written.

    Args:
        path: Override for `settings.audit_path`, mainly for tests.

    Returns:
        An empty list if the audit file does not exist yet.
    """
    resolved = _resolve_path(path)
    if not resolved.exists():
        return []
    with resolved.open(encoding="utf-8") as handle:
        return [AuditEvent.model_validate_json(line) for line in handle if line.strip()]
