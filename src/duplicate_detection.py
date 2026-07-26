"""Detects re-sent or re-processed emails so they never create a second ERP order.

Two independent fingerprints, either of which is enough to call something a
duplicate:

- **Message fingerprint** - normalized Message-ID + sender + attachment
  content hashes. Catches the same email literally being processed twice
  (e.g. a retry after a crash, or an inbox re-poll).
- **Order fingerprint** - customer + PO reference + normalized line items +
  requested date. Catches a customer re-sending the same order in a new
  email (different Message-ID, same order content).

State lives in-process for the duration of one pipeline/notebook run - a
production deployment would back this with the audit log or a persistent
store instead, noted in the notebook's production-limitations section.
No LLM call.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from src.schema import OrderCandidate, ParsedEmail


def message_fingerprint(email: ParsedEmail) -> str:
    """Fingerprints an email by identity, not content - for exact-resend detection."""
    attachment_hashes = sorted(hashlib.sha256(a.content).hexdigest() for a in email.attachments)
    payload = "|".join([email.message_id.strip().lower(), email.sender.strip().lower(), *attachment_hashes])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def order_fingerprint(order: OrderCandidate) -> str:
    """Fingerprints an order by business content - for same-order-different-email detection."""
    normalized_lines = sorted(
        (item.product_reference.strip().lower(), str(item.quantity)) for item in order.line_items
    )
    payload = json.dumps(
        {
            "customer": (order.customer_reference or "").strip().lower(),
            "po": (order.po_reference or "").strip().lower(),
            "date": order.requested_date.isoformat() if order.requested_date else None,
            "lines": normalized_lines,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DuplicateCheckResult:
    """Outcome of checking one email/order pair against everything seen so far."""

    is_duplicate: bool
    matched_on: str | None  # "message" | "order_fingerprint" | None
    message_key: str
    order_key: str


class DuplicateDetector:
    """Tracks every message/order fingerprint seen in this process."""

    def __init__(self) -> None:
        self._seen_message_keys: set[str] = set()
        self._seen_order_fingerprints: set[str] = set()

    def check(self, email: ParsedEmail, order: OrderCandidate) -> DuplicateCheckResult:
        """Checks and records one email/order pair.

        Args:
            email: The parsed email being processed.
            order: The order extracted from it.

        Returns:
            A `DuplicateCheckResult`. Call this exactly once per pipeline
            run for a given email - it registers the fingerprints as seen
            regardless of the outcome, so a third identical email is still
            correctly detected as a duplicate.
        """
        message_key = message_fingerprint(email)
        order_key = order_fingerprint(order)
        # An order with no line items carries no real content to compare -
        # matching on it would flag unrelated empty/incomplete orders
        # (different customers, different problems) as duplicates of each
        # other. Only line-item content is treated as a duplicate signal.
        order_key_is_comparable = bool(order.line_items)

        if message_key in self._seen_message_keys:
            matched_on = "message"
        elif order_key_is_comparable and order_key in self._seen_order_fingerprints:
            matched_on = "order_fingerprint"
        else:
            matched_on = None

        self._seen_message_keys.add(message_key)
        if order_key_is_comparable:
            self._seen_order_fingerprints.add(order_key)

        return DuplicateCheckResult(
            is_duplicate=matched_on is not None,
            matched_on=matched_on,
            message_key=message_key,
            order_key=order_key,
        )
