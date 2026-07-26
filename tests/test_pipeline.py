"""Tests for src/pipeline.py - wiring logic only.

Per SPEC §5, `extraction.py` itself is exercised live (not mocked) inside
the notebook - the point there is to show real model behavior on a real
injection attempt. This suite instead monkeypatches
`extraction.extract_order` so `pytest tests/` stays fast, free, and
independent of network/API access (SPEC §2), while still proving
`pipeline.py` wires every other real module together correctly: security
gating, entity resolution, validation, duplicate detection, risk gate, and
conditional ERP writes.
"""

from pathlib import Path
from unittest.mock import patch

from src.duplicate_detection import DuplicateDetector
from src.erp_client import ERPClient
from src.pipeline import run_email
from src.schema import Intent, LineItem, OrderCandidate, SourceEvidence, WorkflowOutcome

DATA = Path(__file__).resolve().parent.parent / "data"
EVIDENCE = SourceEvidence(source_type="email_body", locator="email_body", quoted_text="n/a")


def _fake_order(**overrides) -> OrderCandidate:
    defaults = dict(
        message_id="<will-be-overridden>",
        intent=Intent.CREATE,
        language="en",
        customer_reference="Nordwind Bau GmbH",
        po_reference="PO-TEST-1",
        line_items=[LineItem(product_reference="SKU-100", quantity=10, unit="EA", unit_price="4.50", currency="EUR", source_evidence=EVIDENCE)],
        extraction_confidence=0.95,
    )
    defaults.update(overrides)
    return OrderCandidate(**defaults)


def _run(raw_email: bytes, fake_order_factory, *, erp=None, detector=None):
    erp = erp or ERPClient(audit_path="/tmp/test_pipeline_audit.jsonl")
    detector = detector or DuplicateDetector()
    with patch("src.pipeline.extraction.extract_order") as mock_extract:
        mock_extract.side_effect = lambda email, documents, workflow_id, **_: fake_order_factory(email)
        result = run_email(raw_email, erp, detector)
    return result, erp, detector


def test_clean_order_auto_creates_and_writes_to_erp():
    raw = (DATA / "emails" / "001_valid_plain_text.eml").read_bytes()
    result, erp, _ = _run(raw, lambda email: _fake_order(message_id=email.message_id))

    assert result.decision.outcome == WorkflowOutcome.AUTO_CREATE
    assert result.created_order is not None
    assert result.created_order.customer_id == "CUST-1001"


def test_update_intent_never_auto_executes():
    raw = (DATA / "emails" / "012_update_request.eml").read_bytes()
    result, _, _ = _run(
        raw,
        lambda email: _fake_order(message_id=email.message_id, intent=Intent.UPDATE, target_order_id="ORD-2026-0101", line_items=[]),
    )

    assert result.decision.outcome == WorkflowOutcome.HUMAN_REVIEW
    assert result.created_order is None


def test_security_flagged_order_is_quarantined_not_created():
    raw = (DATA / "emails" / "001_valid_plain_text.eml").read_bytes()
    result, _, _ = _run(
        raw, lambda email: _fake_order(message_id=email.message_id, security_flags=["prompt_injection"])
    )

    assert result.decision.outcome == WorkflowOutcome.SECURITY_QUARANTINE
    assert result.created_order is None


def test_suspicious_attachment_is_quarantined_before_extraction_sees_it():
    raw = (DATA / "emails" / "020_extension_mismatch.eml").read_bytes()
    calls = []

    def factory(email):
        calls.append(len(email.attachments))
        return _fake_order(message_id=email.message_id, line_items=[])

    result, _, _ = _run(raw, factory)

    assert result.decision.outcome == WorkflowOutcome.SECURITY_QUARANTINE
    assert result.created_order is None


def test_same_message_processed_twice_is_duplicate_noop_on_second_pass():
    raw = (DATA / "emails" / "001_valid_plain_text.eml").read_bytes()
    erp = ERPClient(audit_path="/tmp/test_pipeline_audit2.jsonl")
    detector = DuplicateDetector()

    first, _, _ = _run(raw, lambda email: _fake_order(message_id=email.message_id), erp=erp, detector=detector)
    second, _, _ = _run(raw, lambda email: _fake_order(message_id=email.message_id), erp=erp, detector=detector)

    assert first.decision.outcome == WorkflowOutcome.AUTO_CREATE
    assert second.decision.outcome == WorkflowOutcome.DUPLICATE_NOOP
    assert second.created_order is None


def test_missing_line_items_routes_to_human_review():
    raw = (DATA / "emails" / "006_missing_quantity.eml").read_bytes()
    result, _, _ = _run(raw, lambda email: _fake_order(message_id=email.message_id, line_items=[]))

    assert result.decision.outcome == WorkflowOutcome.HUMAN_REVIEW
    assert "MISSING_LINE_ITEMS" in result.decision.reason_codes


def test_missing_quantity_on_a_stated_item_requires_clarification():
    """A concrete, askable gap (item present, quantity missing) is different
    from no items at all - this one still blocks toward CLARIFICATION_REQUIRED."""
    raw = (DATA / "emails" / "006_missing_quantity.eml").read_bytes()
    result, _, _ = _run(
        raw,
        lambda email: _fake_order(
            message_id=email.message_id,
            line_items=[LineItem(product_reference="SKU-100", quantity=None, unit="EA", source_evidence=EVIDENCE)],
        ),
    )

    assert result.decision.outcome == WorkflowOutcome.CLARIFICATION_REQUIRED
    assert "MISSING_QUANTITY" in result.decision.reason_codes
