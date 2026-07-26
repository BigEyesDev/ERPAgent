"""Combines every upstream signal into one of the six terminal outcomes.

Checked in a fixed priority order - security and duplicates are decided
before anything about the order's content is even considered, since no
amount of a "clean" order matters if the message is malicious or already
processed:

1. Security flags present -> `SECURITY_QUARANTINE`
2. Duplicate message/order -> `DUPLICATE_NOOP`
3. Blocking validation issues (missing/invalid data) -> `CLARIFICATION_REQUIRED`
4. Update/cancel intent -> `HUMAN_REVIEW`, always, regardless of confidence
5. Low extraction confidence or stated ambiguity -> `HUMAN_REVIEW`
6. Non-exact customer match -> `HUMAN_REVIEW`
7. Any remaining (warning-level) validation issues -> `HUMAN_REVIEW`
8. Otherwise -> `AUTO_CREATE`

`TECHNICAL_FAILURE` is not produced here - it means extraction/parsing
failed before a decision could even be computed, so `pipeline.py` short-
circuits to it without calling `decide`. `technical_failure()` below exists
so that outcome is still constructed in one place, not scattered.
No LLM call.
"""

from __future__ import annotations

from src.duplicate_detection import DuplicateCheckResult
from src.schema import (
    EntityMatch,
    Intent,
    MatchType,
    OrderCandidate,
    RiskDecision,
    ValidationResult,
    ValidationSeverity,
    WorkflowOutcome,
)

LOW_CONFIDENCE_THRESHOLD = 0.5

_NON_EXACT_MATCH_REASON = {
    MatchType.NONE: "UNKNOWN_CUSTOMER",
    MatchType.CONFLICT: "CUSTOMER_IDENTITY_CONFLICT",
    MatchType.FUZZY: "FUZZY_CUSTOMER_MATCH",
    MatchType.DOMAIN: "DOMAIN_ONLY_CUSTOMER_MATCH",
}


def decide(
    order: OrderCandidate,
    customer_match: EntityMatch,
    validation_result: ValidationResult,
    duplicate_result: DuplicateCheckResult,
    security_flags: list[str],
) -> RiskDecision:
    """Produces the final `RiskDecision` for one order.

    Args:
        order: The extracted order.
        customer_match: Result of `entity_resolution.resolve_customer`.
        validation_result: Result of `validation.validate_order`.
        duplicate_result: Result of `duplicate_detection.DuplicateDetector.check`.
        security_flags: Flags from `attachment_security.py` and/or
            `extraction.py`'s own injection detection.

    Returns:
        A `RiskDecision` with machine-readable `reason_codes`.
    """
    if security_flags:
        return RiskDecision(outcome=WorkflowOutcome.SECURITY_QUARANTINE, reason_codes=list(security_flags))

    if duplicate_result.is_duplicate:
        return RiskDecision(
            outcome=WorkflowOutcome.DUPLICATE_NOOP,
            reason_codes=["DUPLICATE_MESSAGE_OR_ORDER"],
            notes=f"matched on {duplicate_result.matched_on}",
        )

    blocking = [i.code for i in validation_result.issues if i.severity == ValidationSeverity.BLOCKING]
    warnings = [i.code for i in validation_result.issues if i.severity == ValidationSeverity.WARNING]

    if blocking:
        return RiskDecision(outcome=WorkflowOutcome.CLARIFICATION_REQUIRED, reason_codes=blocking)

    if order.intent in (Intent.UPDATE, Intent.CANCEL):
        reason = "UPDATE_REQUIRES_APPROVAL" if order.intent == Intent.UPDATE else "CANCELLATION_REQUIRES_APPROVAL"
        return RiskDecision(outcome=WorkflowOutcome.HUMAN_REVIEW, reason_codes=[reason])

    if order.ambiguities:
        return RiskDecision(outcome=WorkflowOutcome.HUMAN_REVIEW, reason_codes=["AMBIGUOUS_REQUEST"])

    if order.extraction_confidence < LOW_CONFIDENCE_THRESHOLD:
        return RiskDecision(outcome=WorkflowOutcome.HUMAN_REVIEW, reason_codes=["LOW_EXTRACTION_CONFIDENCE"])

    if customer_match.match_type != MatchType.EXACT:
        return RiskDecision(
            outcome=WorkflowOutcome.HUMAN_REVIEW,
            reason_codes=[_NON_EXACT_MATCH_REASON[customer_match.match_type]],
        )

    if warnings:
        return RiskDecision(outcome=WorkflowOutcome.HUMAN_REVIEW, reason_codes=warnings)

    return RiskDecision(
        outcome=WorkflowOutcome.AUTO_CREATE, reason_codes=["EXACT_CUSTOMER_MATCH", "VALID_CREATE_REQUEST"]
    )


def technical_failure(reason: str) -> RiskDecision:
    """Builds the `TECHNICAL_FAILURE` decision for an infrastructure failure.

    Args:
        reason: Human-readable cause (e.g. the underlying exception message).
    """
    return RiskDecision(outcome=WorkflowOutcome.TECHNICAL_FAILURE, reason_codes=["TECHNICAL_FAILURE"], notes=reason)
