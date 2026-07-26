"""Tests for src/audit.py - the append-only JSONL audit trail."""

from src import audit
from src.schema import AuditEvent


def test_append_only_and_ordered_readback(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    events = [
        AuditEvent(workflow_id="wf-1", stage="parse", status="ok"),
        AuditEvent(workflow_id="wf-1", stage="extract", status="ok"),
        AuditEvent(workflow_id="wf-1", stage="risk_gate", status="AUTO_CREATE"),
    ]
    for event in events:
        audit.record(event, path=log_path)

    lines = log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3

    read_back = audit.read_all(path=log_path)
    assert [e.stage for e in read_back] == ["parse", "extract", "risk_gate"]


def test_read_all_missing_file_returns_empty_list(tmp_path):
    assert audit.read_all(path=tmp_path / "does_not_exist.jsonl") == []
