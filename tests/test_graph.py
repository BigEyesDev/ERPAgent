"""Tests for src/graph.py - human-review pause/resume semantics."""

from pathlib import Path

import pytest

from src.duplicate_detection import DuplicateDetector
from src.erp_client import ERPClient
from src.graph import InvalidHumanDecisionError, build_graph, resume_with_human_decision, run_email
from src.schema import HumanDecision, WorkflowOutcome

DATA = Path(__file__).resolve().parent.parent / "data"
CACHE_PATH = DATA / "extraction_cache.json"


@pytest.fixture
def graph_runtime(tmp_path):
    erp = ERPClient(audit_path=tmp_path / "audit.jsonl")
    detector = DuplicateDetector()
    graph = build_graph(erp, detector, use_cache=True, cache_path=CACHE_PATH)
    return erp, graph


@pytest.mark.parametrize(
    ("fixture_name", "decision"),
    [
        (
            "010_price_mismatch.eml",
            HumanDecision(reviewer_id="reviewer-1", action="approve", reason="Approved after manual review."),
        ),
        (
            "012_update_request.eml",
            HumanDecision(
                reviewer_id="reviewer-1",
                action="approve_with_edits",
                edited_fields={"quantity": "300"},
                reason="Approved with corrected quantity.",
            ),
        ),
    ],
)
def test_approved_human_review_executes_with_a_distinct_terminal_outcome(graph_runtime, fixture_name, decision):
    erp, graph = graph_runtime
    raw = (DATA / "emails" / fixture_name).read_bytes()

    paused_result, config = run_email(raw, graph)
    assert "__interrupt__" in paused_result
    assert paused_result["decision"].outcome == WorkflowOutcome.HUMAN_REVIEW

    resumed = resume_with_human_decision(graph, config, decision, erp=erp)

    assert resumed["created_order"] is not None
    assert resumed["decision"].outcome == WorkflowOutcome.EXECUTED_WITH_HUMAN_APPROVAL
    assert "HUMAN_APPROVED_EXECUTION" in resumed["decision"].reason_codes


@pytest.mark.parametrize(
    ("fixture_name", "bad_edit", "good_edit"),
    [
        ("012_update_request.eml", {"quantity": "not-a-number"}, {"quantity": "300"}),
        ("008_unknown_customer.eml", {"customer_id": "CUST-TYPO-999"}, {"customer_id": "CUST-1001"}),
    ],
)
def test_malformed_reviewer_edit_never_reaches_the_graph_and_stays_resumable(
    graph_runtime, fixture_name, bad_edit, good_edit
):
    """A reviewer is a trust boundary too: bad free-text input must not
    crash the workflow or strand the paused thread - it should raise a
    clear, typed error and leave the thread exactly as resumable as before.
    """
    erp, graph = graph_runtime
    raw = (DATA / "emails" / fixture_name).read_bytes()
    _, config = run_email(raw, graph)

    action = "approve_with_edits" if "quantity" in bad_edit else "approve"
    bad_decision = HumanDecision(reviewer_id="reviewer-1", action=action, edited_fields=bad_edit, reason="bad edit")

    with pytest.raises(InvalidHumanDecisionError):
        resume_with_human_decision(graph, config, bad_decision, erp=erp)

    # The interrupt was never consumed - the thread is still paused at
    # human_review, not stuck mid-execute with the bad decision baked in.
    assert graph.get_state(config).next == ("human_review",)

    good_decision = HumanDecision(reviewer_id="reviewer-1", action=action, edited_fields=good_edit, reason="corrected")
    resumed = resume_with_human_decision(graph, config, good_decision, erp=erp)
    assert resumed["decision"].outcome == WorkflowOutcome.EXECUTED_WITH_HUMAN_APPROVAL
