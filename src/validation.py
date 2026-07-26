"""Deterministic order-consistency checks.

Answers "is this order internally consistent" - a different question from
"who/what does it refer to" (`entity_resolution.py`). This module never
decides the final outcome; it only produces typed, machine-readable
`ValidationIssue`s that `risk_gate.py` combines with entity-resolution and
duplicate-detection results. No LLM call.

Thresholds (`LARGE_QUANTITY_THRESHOLD`, `PRICE_TOLERANCE`) are illustrative
for this fixture-driven demo, not calibrated against real order-volume
data - named explicitly here so they read as a stated scope decision
rather than a silent assumption.
"""

from __future__ import annotations

from decimal import Decimal

from src.erp_client import ERPClient
from src.schema import (
    EntityMatch,
    Intent,
    LineItem,
    MatchType,
    OrderCandidate,
    ValidationIssue,
    ValidationResult,
    ValidationSeverity,
)

LARGE_QUANTITY_THRESHOLD = Decimal(1000)
PRICE_TOLERANCE = Decimal("0.01")  # 1% relative difference


def _blocking(code: str, message: str, field: str | None = None) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, severity=ValidationSeverity.BLOCKING, field=field)


def _warning(code: str, message: str, field: str | None = None) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, severity=ValidationSeverity.WARNING, field=field)


def _validate_line_item(
    item: LineItem, match: EntityMatch, customer_id: str | None, erp: ERPClient, *, workflow_id: str
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if match.match_type == MatchType.NONE:
        issues.append(_blocking("UNKNOWN_PRODUCT", f"product {item.product_reference!r} not found in ERP"))
        return issues  # nothing further can be checked without a resolved SKU
    if match.match_type == MatchType.FUZZY:
        issues.append(_warning("FUZZY_PRODUCT_MATCH", f"product {item.product_reference!r} matched loosely"))

    product = erp.get_product(match.resolved_id, workflow_id=workflow_id)
    if item.unit and product and product.allowed_units and item.unit not in product.allowed_units:
        issues.append(_blocking("INVALID_UNIT", f"unit {item.unit!r} not valid for {match.resolved_id}", "unit"))

    if item.quantity is None:
        issues.append(_blocking("MISSING_QUANTITY", f"no quantity stated for {match.resolved_id}", "quantity"))
    elif item.quantity > LARGE_QUANTITY_THRESHOLD:
        issues.append(_warning("LARGE_QUANTITY", f"quantity {item.quantity} exceeds normal order size"))

    if customer_id:
        reference = None
        if item.unit and item.currency:
            reference = erp.get_price(customer_id, match.resolved_id, item.unit, item.currency, workflow_id=workflow_id)
        if reference is None:
            reference = erp.get_default_price(customer_id, match.resolved_id, workflow_id=workflow_id)

        if reference is None:
            issues.append(
                _blocking("MISSING_PRICE_REFERENCE", f"no ERP price found for {match.resolved_id}", "unit_price")
            )
        elif item.unit_price is not None:
            relative_diff = abs(item.unit_price - reference.unit_price) / reference.unit_price
            if relative_diff > PRICE_TOLERANCE:
                issues.append(
                    _warning(
                        "PRICE_MISMATCH",
                        f"stated price {item.unit_price} differs from ERP price {reference.unit_price}",
                        "unit_price",
                    )
                )

    return issues


def validate_order(
    order: OrderCandidate,
    customer_match: EntityMatch,
    product_matches: list[EntityMatch],
    erp: ERPClient,
    *,
    workflow_id: str,
) -> ValidationResult:
    """Runs every deterministic consistency check for one `OrderCandidate`.

    Args:
        order: The extracted order.
        customer_match: Result of `entity_resolution.resolve_customer`.
        product_matches: One `EntityMatch` per `order.line_items`, same order.
        erp: Read-only ERP access, for unit/price reference checks.
        workflow_id: Threaded through to the audit trail via `erp`'s own calls.

    Returns:
        A `ValidationResult` aggregating every issue found; empty if the
        order is fully consistent.
    """
    issues: list[ValidationIssue] = []

    if order.intent in (Intent.UPDATE, Intent.CANCEL):
        target = erp.get_order(order.target_order_id, workflow_id=workflow_id) if order.target_order_id else None
        if target is None:
            issues.append(
                _blocking("TARGET_ORDER_NOT_FOUND", f"order {order.target_order_id!r} not found", "target_order_id")
            )

    if order.intent == Intent.CREATE and not order.line_items:
        # A warning, not blocking: zero line items means there is nothing
        # specific to ask the customer to clarify (unlike, say, a missing
        # quantity on an otherwise-concrete item) - a human reviewer
        # judging whether to consult purchase history, request the
        # original document again, or manually transcribe a scanned file
        # is the more useful next step than an automated clarification
        # request with nothing concrete to point at.
        issues.append(_warning("MISSING_LINE_ITEMS", "order has no line items"))

    for item, match in zip(order.line_items, product_matches, strict=True):
        issues.extend(_validate_line_item(item, match, customer_match.resolved_id, erp, workflow_id=workflow_id))

    return ValidationResult(issues=issues)
