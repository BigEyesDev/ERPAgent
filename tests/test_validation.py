"""Tests for src/validation.py - deterministic order-consistency checks."""

import pytest

from src.entity_resolution import resolve_customer, resolve_product
from src.erp_client import ERPClient
from src.schema import Intent, LineItem, OrderCandidate, SourceEvidence, ValidationSeverity
from src.validation import validate_order

EVIDENCE = SourceEvidence(source_type="email_body", locator="email_body", quoted_text="n/a")


@pytest.fixture
def erp(tmp_path):
    return ERPClient(audit_path=tmp_path / "audit.jsonl")


def _order(**overrides) -> OrderCandidate:
    defaults = dict(
        message_id="<m1@example.com>",
        intent=Intent.CREATE,
        language="en",
        customer_reference="Nordwind Bau GmbH",
        line_items=[],
        extraction_confidence=0.9,
    )
    defaults.update(overrides)
    return OrderCandidate(**defaults)


def _resolve(order, erp):
    customer_match = resolve_customer(order.customer_reference, "einkauf@nordwind-bau.example", erp, workflow_id="wf")
    product_matches = [resolve_product(item.product_reference, erp, workflow_id="wf") for item in order.line_items]
    return customer_match, product_matches


def test_clean_order_has_no_issues(erp):
    order = _order(
        line_items=[
            LineItem(product_reference="SKU-100", quantity=10, unit="EA", unit_price="4.50", currency="EUR", source_evidence=EVIDENCE)
        ]
    )
    customer_match, product_matches = _resolve(order, erp)
    result = validate_order(order, customer_match, product_matches, erp, workflow_id="wf")
    assert result.issues == []
    assert not result.has_blocking_issues


def test_missing_line_items_is_a_warning_not_blocking(erp):
    """Zero line items routes to HUMAN_REVIEW, not CLARIFICATION_REQUIRED:
    there's nothing specific to ask the customer for, so a human judgment
    call (check history, request the document again, attempt OCR) is more
    useful than an automated clarification request with nothing to point at."""
    order = _order(line_items=[])
    customer_match, product_matches = _resolve(order, erp)
    result = validate_order(order, customer_match, product_matches, erp, workflow_id="wf")
    assert not result.has_blocking_issues
    assert any(
        issue.code == "MISSING_LINE_ITEMS" and issue.severity == ValidationSeverity.WARNING
        for issue in result.issues
    )


def test_unknown_product_is_blocking(erp):
    order = _order(
        line_items=[LineItem(product_reference="Nonexistent Widget", quantity=1, source_evidence=EVIDENCE)]
    )
    customer_match, product_matches = _resolve(order, erp)
    result = validate_order(order, customer_match, product_matches, erp, workflow_id="wf")
    assert any(issue.code == "UNKNOWN_PRODUCT" and issue.severity == ValidationSeverity.BLOCKING for issue in result.issues)


def test_price_mismatch_is_a_warning(erp):
    order = _order(
        line_items=[
            LineItem(product_reference="SKU-100", quantity=10, unit="EA", unit_price="1.00", currency="EUR", source_evidence=EVIDENCE)
        ]
    )
    customer_match, product_matches = _resolve(order, erp)
    result = validate_order(order, customer_match, product_matches, erp, workflow_id="wf")
    mismatch = next(issue for issue in result.issues if issue.code == "PRICE_MISMATCH")
    assert mismatch.severity == ValidationSeverity.WARNING
    assert not result.has_blocking_issues


def test_large_quantity_is_a_warning(erp):
    order = _order(
        line_items=[LineItem(product_reference="SKU-100", quantity=500000, unit="EA", source_evidence=EVIDENCE)]
    )
    customer_match, product_matches = _resolve(order, erp)
    result = validate_order(order, customer_match, product_matches, erp, workflow_id="wf")
    assert any(issue.code == "LARGE_QUANTITY" and issue.severity == ValidationSeverity.WARNING for issue in result.issues)


def test_update_with_missing_target_order_is_blocking(erp):
    order = _order(intent=Intent.UPDATE, line_items=[], target_order_id="ORD-DOES-NOT-EXIST")
    customer_match, product_matches = _resolve(order, erp)
    result = validate_order(order, customer_match, product_matches, erp, workflow_id="wf")
    assert any(issue.code == "TARGET_ORDER_NOT_FOUND" for issue in result.issues)


def test_update_with_existing_target_order_has_no_target_issue(erp):
    order = _order(intent=Intent.UPDATE, line_items=[], target_order_id="ORD-2026-0101")
    customer_match, product_matches = _resolve(order, erp)
    result = validate_order(order, customer_match, product_matches, erp, workflow_id="wf")
    assert not any(issue.code == "TARGET_ORDER_NOT_FOUND" for issue in result.issues)
