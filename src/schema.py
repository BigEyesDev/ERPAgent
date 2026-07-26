"""Typed data contracts shared across every pipeline stage.

Every value that crosses a trust boundary (LLM output, parsed attachment
content, human-review input) is validated against a model defined here.
Enums are explicit and closed - no stage accepts a free-text status where a
member of `Intent` or `WorkflowOutcome` is expected, so a typo in a
downstream branch fails loudly instead of silently falling through.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Intent(StrEnum):
    """What the sender is asking for, as classified by extraction."""

    CREATE = "create"
    UPDATE = "update"
    CANCEL = "cancel"
    UNCLEAR = "unclear"


class WorkflowOutcome(StrEnum):
    """The terminal states a workflow run can reach.

    `risk_gate.py` emits the first six below; `graph.py` can additionally
    promote a human-reviewed run to `EXECUTED_WITH_HUMAN_APPROVAL` once a
    reviewer has explicitly approved it and the ERP write has happened.
    Deliberately not three (`auto_approve/escalate/reject`): each member
    implies a different next action, which is what `risk_gate.py` and
    `graph.py` route on.
    """

    AUTO_CREATE = "AUTO_CREATE"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    SECURITY_QUARANTINE = "SECURITY_QUARANTINE"
    DUPLICATE_NOOP = "DUPLICATE_NOOP"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
    EXECUTED_WITH_HUMAN_APPROVAL = "EXECUTED_WITH_HUMAN_APPROVAL"


class MatchType(StrEnum):
    """How an entity-resolution reference was (or wasn't) tied to an ERP identity.

    `DOMAIN` and `CONFLICT` exist because "who is this" has more failure
    modes than a plain similarity score: a sender's domain can identify a
    customer even with no name in the text (`DOMAIN`), and a named
    customer can legitimately disagree with the sending domain
    (`CONFLICT`) - both need to reach a human, but for different reasons.
    """

    EXACT = "exact"
    DOMAIN = "domain"
    FUZZY = "fuzzy"
    CONFLICT = "conflict"
    NONE = "none"


class ValidationSeverity(StrEnum):
    """Whether a validation issue blocks auto-creation or is informational."""

    BLOCKING = "blocking"
    WARNING = "warning"


class SourceEvidence(BaseModel):
    """Points an extracted field back to the exact place it came from.

    Answers "why did the model extract this value from where" literally,
    rather than via a log line, satisfying the traceability requirement.
    """

    source_type: str = Field(description="email_body | pdf | pptx | xlsx")
    locator: str = Field(
        description="e.g. 'page:2', 'slide:3', 'sheet:Orders!cell:B4', 'email_body'"
    )
    quoted_text: str = Field(description="Verbatim snippet the value was extracted from")


class LineItem(BaseModel):
    """One ordered product line, as extracted (pre-resolution).

    `quantity` is nullable on purpose: forcing a numeric value would give
    extraction no way to represent "the sender never actually stated
    this" other than fabricating one. `None` is a validation concern
    (`validation.py`'s `MISSING_QUANTITY`), not a schema violation.
    """

    product_reference: str = Field(description="Product name/SKU as written by the sender")
    quantity: Decimal | None = Field(default=None, gt=0)
    unit: str | None = None
    unit_price: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    source_evidence: SourceEvidence


class OrderCandidate(BaseModel):
    """The structured output of `extraction.py` - one email's worth of order.

    "Candidate" because nothing here is resolved or validated yet: customer
    and product references are still raw sender text, not ERP identities.
    """

    message_id: str
    intent: Intent
    language: str = Field(description="ISO 639-1 code, e.g. 'en', 'de'")
    customer_reference: str | None = Field(
        default=None, description="Customer name/ID as written by the sender"
    )
    po_reference: str | None = None
    requested_date: date | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    target_order_id: str | None = Field(
        default=None, description="Order being updated/cancelled, if intent is update/cancel"
    )
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    ambiguities: list[str] = Field(default_factory=list)
    security_flags: list[str] = Field(default_factory=list)


class ValidationIssue(BaseModel):
    """One deterministic finding from `validation.py`."""

    code: str
    message: str
    severity: ValidationSeverity
    field: str | None = None


class ValidationResult(BaseModel):
    """Aggregate output of `validation.py` for one `OrderCandidate`."""

    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def has_blocking_issues(self) -> bool:
        return any(i.severity == ValidationSeverity.BLOCKING for i in self.issues)


class EntityMatch(BaseModel):
    """Result of resolving one raw reference (customer or product) against ERP data."""

    match_type: MatchType
    resolved_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    candidates: list[str] = Field(default_factory=list, description="Other plausible matches")


class RiskDecision(BaseModel):
    """Output of `risk_gate.py`: the terminal outcome plus why."""

    outcome: WorkflowOutcome
    reason_codes: list[str] = Field(default_factory=list)
    notes: str | None = None


class AuditEvent(BaseModel):
    """One append-only line in the audit log (`audit.py`)."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    workflow_id: str
    stage: str
    status: str
    model_id: str | None = None
    prompt_version: str | None = None
    decision: str | None = None
    decision_reasons: list[str] = Field(default_factory=list)
    retry_count: int = 0
    human_action: str | None = None
    error_category: str | None = None


class HumanReviewRequest(BaseModel):
    """What a reviewer sees when a workflow pauses at `HUMAN_REVIEW`."""

    workflow_id: str
    order_candidate: OrderCandidate
    validation_result: ValidationResult
    risk_decision: RiskDecision


class Address(BaseModel):
    """A billing or shipping address as held in ERP customer master data."""

    street: str
    postal_code: str
    city: str
    country: str
    address_id: str | None = None


class Customer(BaseModel):
    """ERP customer master record, as read by `erp_client.py`."""

    customer_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    email_domains: list[str] = Field(default_factory=list)
    billing_address: Address | None = None
    shipping_addresses: list[Address] = Field(default_factory=list)
    preferred_currency: str
    status: str = Field(description="active | blocked")


class Product(BaseModel):
    """ERP product master record."""

    sku: str
    description: str
    aliases: list[str] = Field(default_factory=list)
    allowed_units: list[str] = Field(default_factory=list)
    status: str = Field(description="active | discontinued")


class PriceEntry(BaseModel):
    """One customer/SKU-specific negotiated price, valid over a date range."""

    customer_id: str
    sku: str
    unit: str
    currency: str
    unit_price: Decimal
    valid_from: date
    valid_to: date


class ErpLineItem(BaseModel):
    """One resolved order line as stored in the ERP (SKU already known)."""

    sku: str
    quantity: Decimal = Field(gt=0)
    unit: str
    unit_price: Decimal = Field(ge=0)
    currency: str


class ErpOrder(BaseModel):
    """An order as stored in the mock ERP (`data/erp/orders.json`)."""

    order_id: str
    customer_id: str
    purchase_order_reference: str | None = None
    status: str = Field(description="pending | confirmed | cancelled")
    line_items: list[ErpLineItem]


class PurchaseHistoryEntry(BaseModel):
    """One past order for a customer, used only as an entity-resolution signal."""

    order_id: str
    customer_id: str
    purchase_order_reference: str | None = None
    order_date: date
    line_items: list[ErpLineItem]


class CreateOrderRequest(BaseModel):
    """Write payload for `erp_client.create_order`."""

    idempotency_key: str
    customer_id: str
    purchase_order_reference: str | None = None
    line_items: list[ErpLineItem]


class UpdateOrderRequest(BaseModel):
    """Write payload for `erp_client.update_order`."""

    idempotency_key: str
    order_id: str
    line_items: list[ErpLineItem]


class EmailAttachment(BaseModel):
    """One MIME attachment extracted from a `.eml` by `parsers/email.py`."""

    filename: str
    content_type: str
    size_bytes: int
    content: bytes


class ParsedEmail(BaseModel):
    """Structured output of `parsers/email.py` - the ingestion entry point."""

    message_id: str
    sender: str
    recipients: list[str]
    subject: str
    timestamp: datetime | None = None
    body_text: str
    body_html: str | None = None
    attachments: list[EmailAttachment] = Field(default_factory=list)


class DocumentSegment(BaseModel):
    """One provenance-tagged unit of text from a parsed attachment.

    `locator` uses the same format as `SourceEvidence.locator` (e.g.
    `page:2`, `slide:3`, `sheet:Orders!cell:B4`) so extraction can pass it
    straight through into `SourceEvidence` without reformatting.
    """

    locator: str
    text: str


class ParsedDocument(BaseModel):
    """Structured output of a `parsers/{pdf,pptx,xlsx}.py` module."""

    source_type: str = Field(description="pdf | pptx | xlsx")
    segments: list[DocumentSegment]


class HumanDecision(BaseModel):
    """A reviewer's resolution of a `HumanReviewRequest`."""

    reviewer_id: str
    action: str = Field(
        description="approve | approve_with_edits | reject | request_clarification | quarantine"
    )
    edited_fields: dict[str, str] = Field(default_factory=dict)
    reason: str

    @field_validator("action")
    @classmethod
    def _action_is_known(cls, value: str) -> str:
        allowed = {
            "approve",
            "approve_with_edits",
            "reject",
            "request_clarification",
            "quarantine",
        }
        if value not in allowed:
            raise ValueError(f"unknown human action {value!r}, expected one of {allowed}")
        return value
