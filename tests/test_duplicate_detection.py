"""Tests for src/duplicate_detection.py."""

from pathlib import Path

from src.duplicate_detection import DuplicateDetector
from src.parsers.email import parse_email
from src.schema import Intent, LineItem, OrderCandidate, SourceEvidence

DATA = Path(__file__).resolve().parent.parent / "data"
EVIDENCE = SourceEvidence(source_type="email_body", locator="email_body", quoted_text="n/a")


def _order(**overrides) -> OrderCandidate:
    defaults = dict(
        message_id="<m1@example.com>",
        intent=Intent.CREATE,
        language="en",
        customer_reference="Nordwind Bau GmbH",
        po_reference="PO-2026-021",
        line_items=[LineItem(product_reference="SKU-100", quantity=10, source_evidence=EVIDENCE)],
        extraction_confidence=0.9,
    )
    defaults.update(overrides)
    return OrderCandidate(**defaults)


def test_same_message_processed_twice_is_flagged_on_second_pass():
    raw = (DATA / "emails" / "021_duplicate_original.eml").read_bytes()
    email = parse_email(raw)
    order = _order()

    detector = DuplicateDetector()
    first = detector.check(email, order)
    second = detector.check(email, order)

    assert not first.is_duplicate
    assert second.is_duplicate
    assert second.matched_on == "message"


def test_fixture_021_and_022_are_the_same_logical_message_redelivered():
    """021/022 share one Message-ID by design (a redelivery, not a new email) -
    the message fingerprint alone is enough to catch this pair."""
    raw_original = (DATA / "emails" / "021_duplicate_original.eml").read_bytes()
    raw_replay = (DATA / "emails" / "022_duplicate_replay.eml").read_bytes()
    original_email = parse_email(raw_original)
    replay_email = parse_email(raw_replay)
    order = _order()

    detector = DuplicateDetector()
    first = detector.check(original_email, order)
    second = detector.check(replay_email, order)

    assert original_email.message_id == replay_email.message_id
    assert not first.is_duplicate
    assert second.is_duplicate
    assert second.matched_on == "message"


def test_same_order_content_from_a_genuinely_different_email_is_flagged_by_content():
    email_a = parse_email((DATA / "emails" / "001_valid_plain_text.eml").read_bytes())
    email_b = parse_email((DATA / "emails" / "002_valid_pdf.eml").read_bytes())
    assert email_a.message_id != email_b.message_id

    order = _order()
    detector = DuplicateDetector()
    first = detector.check(email_a, order)
    second = detector.check(email_b, order)

    assert not first.is_duplicate
    assert second.is_duplicate
    assert second.matched_on == "order_fingerprint"


def test_distinct_emails_with_distinct_orders_are_not_flagged():
    email_a = parse_email((DATA / "emails" / "001_valid_plain_text.eml").read_bytes())
    email_b = parse_email((DATA / "emails" / "002_valid_pdf.eml").read_bytes())
    detector = DuplicateDetector()

    first = detector.check(email_a, _order(message_id=email_a.message_id, po_reference="PO-A"))
    second = detector.check(email_b, _order(message_id=email_b.message_id, po_reference="PO-B"))

    assert not first.is_duplicate
    assert not second.is_duplicate
