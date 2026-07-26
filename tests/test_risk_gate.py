"""Tests for src/risk_gate.py - one test per reachable WorkflowOutcome."""

from src.duplicate_detection import DuplicateCheckResult
from src.risk_gate import decide, technical_failure
from src.schema import (
    EntityMatch,
    Intent,
    LineItem,
    MatchType,
    OrderCandidate,
    SourceEvidence,
    ValidationIssue,
    ValidationResult,
    ValidationSeverity,
    WorkflowOutcome,
)

EVIDENCE = SourceEvidence(source_type="email_body", locator="email_body", quoted_text="n/a")
NOT_DUPLICATE = DuplicateCheckResult(is_duplicate=False, matched_on=None, message_key="m", order_key="o")
IS_DUPLICATE = DuplicateCheckResult(is_duplicate=True, matched_on="message", message_key="m", order_key="o")
EXACT_CUSTOMER = EntityMatch(match_type=MatchType.EXACT, resolved_id="CUST-1001", confidence=1.0)
CLEAN_VALIDATION = ValidationResult(issues=[])


def _order(**overrides) -> OrderCandidate:
    defaults = dict(
        message_id="<m1@example.com>",
        intent=Intent.CREATE,
        language="en",
        customer_reference="Nordwind Bau GmbH",
        line_items=[LineItem(product_reference="SKU-100", quantity=10, source_evidence=EVIDENCE)],
        extraction_confidence=0.95,
    )
    defaults.update(overrides)
    return OrderCandidate(**defaults)


def test_clean_create_auto_creates():
    decision = decide(_order(), EXACT_CUSTOMER, CLEAN_VALIDATION, NOT_DUPLICATE, security_flags=[])
    assert decision.outcome == WorkflowOutcome.AUTO_CREATE


def test_security_flags_always_win_first():
    decision = decide(
        _order(), EXACT_CUSTOMER, CLEAN_VALIDATION, NOT_DUPLICATE, security_flags=["PROMPT_INJECTION_DETECTED"]
    )
    assert decision.outcome == WorkflowOutcome.SECURITY_QUARANTINE
    assert decision.reason_codes == ["PROMPT_INJECTION_DETECTED"]


def test_duplicate_wins_over_a_clean_order():
    decision = decide(_order(), EXACT_CUSTOMER, CLEAN_VALIDATION, IS_DUPLICATE, security_flags=[])
    assert decision.outcome == WorkflowOutcome.DUPLICATE_NOOP


def test_blocking_validation_issue_requires_clarification():
    validation = ValidationResult(
        issues=[ValidationIssue(code="MISSING_LINE_ITEMS", message="no items", severity=ValidationSeverity.BLOCKING)]
    )
    decision = decide(_order(), EXACT_CUSTOMER, validation, NOT_DUPLICATE, security_flags=[])
    assert decision.outcome == WorkflowOutcome.CLARIFICATION_REQUIRED
    assert "MISSING_LINE_ITEMS" in decision.reason_codes


def test_update_intent_always_needs_human_review_even_at_high_confidence():
    order = _order(intent=Intent.UPDATE, extraction_confidence=0.99, target_order_id="ORD-2026-0101")
    decision = decide(order, EXACT_CUSTOMER, CLEAN_VALIDATION, NOT_DUPLICATE, security_flags=[])
    assert decision.outcome == WorkflowOutcome.HUMAN_REVIEW
    assert decision.reason_codes == ["UPDATE_REQUIRES_APPROVAL"]


def test_cancel_intent_always_needs_human_review():
    order = _order(intent=Intent.CANCEL, target_order_id="ORD-2026-0101")
    decision = decide(order, EXACT_CUSTOMER, CLEAN_VALIDATION, NOT_DUPLICATE, security_flags=[])
    assert decision.outcome == WorkflowOutcome.HUMAN_REVIEW
    assert decision.reason_codes == ["CANCELLATION_REQUIRES_APPROVAL"]


def test_low_confidence_needs_human_review():
    decision = decide(
        _order(extraction_confidence=0.2), EXACT_CUSTOMER, CLEAN_VALIDATION, NOT_DUPLICATE, security_flags=[]
    )
    assert decision.outcome == WorkflowOutcome.HUMAN_REVIEW
    assert decision.reason_codes == ["LOW_EXTRACTION_CONFIDENCE"]


def test_non_exact_customer_match_needs_human_review():
    fuzzy_match = EntityMatch(match_type=MatchType.FUZZY, resolved_id="CUST-1001", confidence=0.7)
    decision = decide(_order(), fuzzy_match, CLEAN_VALIDATION, NOT_DUPLICATE, security_flags=[])
    assert decision.outcome == WorkflowOutcome.HUMAN_REVIEW
    assert decision.reason_codes == ["FUZZY_CUSTOMER_MATCH"]


def test_warning_level_validation_issue_needs_human_review():
    validation = ValidationResult(
        issues=[ValidationIssue(code="LARGE_QUANTITY", message="big order", severity=ValidationSeverity.WARNING)]
    )
    decision = decide(_order(), EXACT_CUSTOMER, validation, NOT_DUPLICATE, security_flags=[])
    assert decision.outcome == WorkflowOutcome.HUMAN_REVIEW
    assert decision.reason_codes == ["LARGE_QUANTITY"]


def test_technical_failure_helper():
    decision = technical_failure("OpenRouter API timed out after 2 retries")
    assert decision.outcome == WorkflowOutcome.TECHNICAL_FAILURE
    assert decision.notes == "OpenRouter API timed out after 2 retries"


def test_risk_gate_and_technical_failure_cover_the_pre_human_outcomes():
    reachable = {
        decide(_order(), EXACT_CUSTOMER, CLEAN_VALIDATION, NOT_DUPLICATE, security_flags=[]).outcome,
        decide(_order(), EXACT_CUSTOMER, CLEAN_VALIDATION, NOT_DUPLICATE, security_flags=["X"]).outcome,
        decide(_order(), EXACT_CUSTOMER, CLEAN_VALIDATION, IS_DUPLICATE, security_flags=[]).outcome,
        decide(
            _order(),
            EXACT_CUSTOMER,
            ValidationResult(issues=[ValidationIssue(code="X", message="x", severity=ValidationSeverity.BLOCKING)]),
            NOT_DUPLICATE,
            security_flags=[],
        ).outcome,
        decide(_order(intent=Intent.UPDATE), EXACT_CUSTOMER, CLEAN_VALIDATION, NOT_DUPLICATE, security_flags=[]).outcome,
        technical_failure("x").outcome,
    }
    assert reachable == {
        WorkflowOutcome.AUTO_CREATE,
        WorkflowOutcome.HUMAN_REVIEW,
        WorkflowOutcome.CLARIFICATION_REQUIRED,
        WorkflowOutcome.SECURITY_QUARANTINE,
        WorkflowOutcome.DUPLICATE_NOOP,
        WorkflowOutcome.TECHNICAL_FAILURE,
    }
