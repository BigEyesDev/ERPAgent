"""Straight-line pipeline glue: no framework, plain functions.

Wires every stage in fixed order: parse email -> attachment security gate
-> parse safe attachments -> extract -> resolve entities -> validate ->
duplicate-check -> risk gate -> (auto-create only) ERP write -> audit.
Proven here as plain functions before any orchestration framework wraps it
(`graph.py`, a later phase) - SPEC §6's "straight-line script first."

Only `AUTO_CREATE` performs an ERP write. Every other outcome - including
`HUMAN_REVIEW` for update/cancel requests - stops at the decision; actually
applying a human-approved update/cancel is a later phase's concern
(simulated human review + `graph.py` resume), not this module's.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from src import audit, extraction
from src.attachment_security import check_attachment
from src.duplicate_detection import DuplicateCheckResult, DuplicateDetector
from src.entity_resolution import resolve_customer, resolve_product
from src.erp_client import ERPClient
from src.parsers import ParserError
from src.parsers.email import parse_email
from src.parsers.pdf import parse_pdf
from src.parsers.pptx import parse_pptx
from src.parsers.xlsx import parse_xlsx
from src.risk_gate import decide, technical_failure
from src.schema import (
    AuditEvent,
    EntityMatch,
    ErpLineItem,
    ErpOrder,
    CreateOrderRequest,
    LineItem,
    OrderCandidate,
    ParsedDocument,
    ParsedEmail,
    RiskDecision,
    ValidationIssue,
    ValidationResult,
    ValidationSeverity,
    WorkflowOutcome,
)
from src.validation import validate_order

_PARSER_BY_EXTENSION = {"pdf": parse_pdf, "pptx": parse_pptx, "xlsx": parse_xlsx}


@dataclass
class PipelineResult:
    """Everything one email's run through the pipeline produced, for the notebook to display."""

    workflow_id: str
    email: ParsedEmail | None
    order: OrderCandidate | None = None
    customer_match: EntityMatch | None = None
    product_matches: list[EntityMatch] = field(default_factory=list)
    validation_result: ValidationResult | None = None
    duplicate_result: DuplicateCheckResult | None = None
    decision: RiskDecision | None = None
    created_order: ErpOrder | None = None


def parse_safe_attachments(
    email: ParsedEmail, *, workflow_id: str, audit_path: str | Path | None = None
) -> tuple[list[ParsedDocument], list[str]]:
    """Runs the attachment-security gate, then parses only what passes it.

    Args:
        audit_path: Passed straight through to `audit.record` - pass the
            calling `ERPClient`'s `audit_path` so this function's own
            audit lines land in the same file as everything else for the
            run, not `settings.audit_path`'s default.

    Returns:
        `(documents, security_flags)` - `documents` contains only
        attachments that passed security *and* parsed successfully;
        `security_flags` collects every reason an attachment was rejected
        at either stage, for `risk_gate.py` to route on.
    """
    documents: list[ParsedDocument] = []
    security_flags: list[str] = []

    for attachment in email.attachments:
        check = check_attachment(attachment)
        if not check.is_safe:
            security_flags.extend(check.reasons)
            continue

        extension = attachment.filename.rsplit(".", 1)[-1].lower()
        parser = _PARSER_BY_EXTENSION.get(extension)
        if parser is None:
            security_flags.append(f"no parser available for .{extension}")
            continue

        try:
            documents.append(parser(attachment.content))
        except ParserError as exc:
            audit.record(
                AuditEvent(workflow_id=workflow_id, stage="pipeline.parse_attachment", status="parser_error", error_category=str(exc)),
                path=audit_path,
            )
            documents.append(None)  # marks an unreadable-but-not-unsafe attachment

    return documents, security_flags


def run_email(
    raw_email_bytes: bytes,
    erp: ERPClient,
    duplicate_detector: DuplicateDetector,
    *,
    use_cache: bool = False,
    cache_path: Path | None = None,
) -> PipelineResult:
    """Runs one raw email through the full straight-line pipeline.

    Args:
        raw_email_bytes: Full contents of a `.eml` file.
        erp: Shared `ERPClient` instance (its in-memory order store persists
            across calls, so duplicate/idempotency behavior works across a
            fixture-set run).
        duplicate_detector: Shared `DuplicateDetector` instance, same reason.
        use_cache: If `True`, extraction goes through
            `extraction.extract_order_with_cache` - the notebook's replay
            mode, so a fully-populated cache means zero live LLM calls.
        cache_path: Required when `use_cache=True`.

    Returns:
        A `PipelineResult`. Always populated up to the point the pipeline
        could reach - never raises for a fixture-level problem (malformed
        email, unreadable attachment, extraction failure); those become
        `TECHNICAL_FAILURE` or `CLARIFICATION_REQUIRED` decisions instead.
    """
    workflow_id = str(uuid.uuid4())

    try:
        email = parse_email(raw_email_bytes)
    except ParserError as exc:
        audit.record(
            AuditEvent(workflow_id=workflow_id, stage="pipeline.parse_email", status="failed", error_category=str(exc)),
            path=erp.audit_path,
        )
        return PipelineResult(workflow_id=workflow_id, email=None, decision=technical_failure(f"unparseable email: {exc}"))

    documents_or_none, security_flags = parse_safe_attachments(email, workflow_id=workflow_id, audit_path=erp.audit_path)
    unreadable_attachment = any(doc is None for doc in documents_or_none)
    documents = [doc for doc in documents_or_none if doc is not None]

    try:
        if use_cache:
            order = extraction.extract_order_with_cache(
                email, documents, workflow_id=workflow_id, cache_path=cache_path, audit_path=erp.audit_path
            )
        else:
            order = extraction.extract_order(email, documents, workflow_id=workflow_id, audit_path=erp.audit_path)
    except extraction.ExtractionError as exc:
        return PipelineResult(workflow_id=workflow_id, email=email, decision=technical_failure(str(exc)))

    combined_security_flags = list(dict.fromkeys([*security_flags, *order.security_flags]))

    customer_match = resolve_customer(order.customer_reference, email.sender, erp, workflow_id=workflow_id)
    product_matches = [resolve_product(item.product_reference, erp, workflow_id=workflow_id) for item in order.line_items]

    validation_result = validate_order(order, customer_match, product_matches, erp, workflow_id=workflow_id)
    if unreadable_attachment:
        validation_result.issues.append(
            unreadable_attachment_issue()
        )

    duplicate_result = duplicate_detector.check(email, order)
    decision = decide(order, customer_match, validation_result, duplicate_result, combined_security_flags)

    created_order = None
    if decision.outcome == WorkflowOutcome.AUTO_CREATE:
        line_items = [
            resolve_erp_line_item(item, product_match, customer_match.resolved_id, erp, workflow_id=workflow_id)
            for item, product_match in zip(order.line_items, product_matches, strict=True)
        ]
        created_order = erp.create_order(
            CreateOrderRequest(
                idempotency_key=duplicate_result.order_key,
                customer_id=customer_match.resolved_id,
                purchase_order_reference=order.po_reference,
                line_items=line_items,
            ),
            workflow_id=workflow_id,
        )

    audit.record(
        AuditEvent(
            workflow_id=workflow_id,
            stage="pipeline.decision",
            status=decision.outcome.value,
            decision=decision.outcome.value,
            decision_reasons=decision.reason_codes,
        ),
        path=erp.audit_path,
    )

    return PipelineResult(
        workflow_id=workflow_id,
        email=email,
        order=order,
        customer_match=customer_match,
        product_matches=product_matches,
        validation_result=validation_result,
        duplicate_result=duplicate_result,
        decision=decision,
        created_order=created_order,
    )


def resolve_erp_line_item(
    item: LineItem, product_match: EntityMatch, customer_id: str, erp: ERPClient, *, workflow_id: str
) -> ErpLineItem:
    """Fills in unit/price/currency from the ERP price list where the sender omitted them.

    `validate_order` already guarantees a price reference exists for every
    line item that reaches `AUTO_CREATE` (via `MISSING_PRICE_REFERENCE`),
    so this lookup succeeding is not optional here - it re-fetches rather
    than threading the reference through, since validation and order
    creation are deliberately separate passes.
    """
    reference = None
    if item.unit and item.currency:
        reference = erp.get_price(customer_id, product_match.resolved_id, item.unit, item.currency, workflow_id=workflow_id)
    if reference is None:
        reference = erp.get_default_price(customer_id, product_match.resolved_id, workflow_id=workflow_id)

    return ErpLineItem(
        sku=product_match.resolved_id,
        quantity=item.quantity,
        unit=item.unit or reference.unit,
        unit_price=item.unit_price or reference.unit_price,
        currency=item.currency or reference.currency,
    )


def unreadable_attachment_issue() -> ValidationIssue:
    return ValidationIssue(
        code="UNREADABLE_ATTACHMENT",
        message="an attachment passed the security gate but could not be parsed",
        severity=ValidationSeverity.BLOCKING,
    )


def run_fixture_set(
    email_dir: Path,
    erp: ERPClient,
    duplicate_detector: DuplicateDetector,
    *,
    use_cache: bool = False,
    cache_path: Path | None = None,
) -> list[PipelineResult]:
    """Runs every `.eml` file in `email_dir` through `run_email`, in filename order.

    Shares one `erp`/`duplicate_detector` across the whole set so
    duplicate-detection and ERP-order-count behavior reflect a single
    inbox being processed end to end, matching the single-inbox
    constraint. `use_cache`/`cache_path` are passed straight
    through to every `run_email` call - see there for what they do.
    """
    return [
        run_email(path.read_bytes(), erp, duplicate_detector, use_cache=use_cache, cache_path=cache_path)
        for path in sorted(email_dir.glob("*.eml"))
    ]
