"""LangGraph orchestration: one node per real pipeline stage.

Not a wrapper node around the whole `pipeline.py` - each node below is one
stage from that same straight-line pipeline (SPEC §6's architecture
decision), reusing `pipeline.py`'s own helper functions
(`parse_safe_attachments`, `resolve_erp_line_item`,
`unreadable_attachment_issue`) rather than reimplementing them, so this
module cannot silently drift into a hidden monolith inside the "safe"
framework layer.

The one thing `graph.py` can do that `pipeline.py` deliberately can't:
pause on `HUMAN_REVIEW` via LangGraph's `interrupt()`, and resume with a
`HumanDecision` that can approve (with or without edits), reject, request
clarification, or quarantine - including actually executing an
approved update/cancel, which `pipeline.py` never does.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal, TypedDict

import inspect
from decimal import Decimal, InvalidOperation

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from src import audit, extraction, schema
from src.duplicate_detection import DuplicateCheckResult, DuplicateDetector
from src.entity_resolution import resolve_customer, resolve_product
from src.erp_client import ERPClient
from src.parsers import ParserError
from src.parsers.email import parse_email
from src.pipeline import parse_safe_attachments, resolve_erp_line_item, unreadable_attachment_issue
from src.risk_gate import decide, technical_failure
from src.schema import (
    AuditEvent,
    CreateOrderRequest,
    EntityMatch,
    ErpOrder,
    HumanDecision,
    Intent,
    OrderCandidate,
    ParsedDocument,
    ParsedEmail,
    RiskDecision,
    UpdateOrderRequest,
    ValidationResult,
    WorkflowOutcome,
)
from src.validation import validate_order


class InvalidHumanDecisionError(Exception):
    """Raised when a reviewer's edited fields can't be applied.

    Raised by `resume_with_human_decision` *before* the decision ever
    reaches the graph - a reviewer is a trust boundary too, the same way
    the LLM's output is: free-text edits get validated before anything
    downstream trusts them, not after. This is what keeps the paused
    thread genuinely resumable: since the interrupt is never consumed,
    the reviewer can simply retry with a corrected `HumanDecision`.
    """


def _describe_edit_errors(
    decision: HumanDecision, order: OrderCandidate, customer_match: EntityMatch, erp: ERPClient, *, workflow_id: str
) -> list[str]:
    """Validates a reviewer's edited fields against the paused order's state.

    Only checks fields `node_execute` actually consumes
    (`customer_id`, `quantity`) - `po_reference` accepts any string, there
    is nothing to validate. A no-op for `reject`/`request_clarification`/
    `quarantine`, since those never reach `execute`.
    """
    if decision.action not in ("approve", "approve_with_edits"):
        return []

    errors: list[str] = []
    edited_fields = decision.edited_fields

    customer_id = edited_fields.get("customer_id") or customer_match.resolved_id
    if customer_id is None:
        errors.append(
            "customer was never resolved "
            f"(match_type={customer_match.match_type.value}) and edited_fields['customer_id'] was not supplied"
        )
    elif erp.get_customer(customer_id, workflow_id=workflow_id) is None:
        errors.append(f"edited_fields['customer_id']={customer_id!r} does not exist in the ERP")

    if "quantity" in edited_fields:
        try:
            quantity = Decimal(edited_fields["quantity"])
            if quantity <= 0:
                errors.append(f"edited_fields['quantity']={edited_fields['quantity']!r} must be positive")
        except InvalidOperation:
            errors.append(f"edited_fields['quantity']={edited_fields['quantity']!r} is not a valid number")

    return errors


class GraphState(TypedDict, total=False):
    """LangGraph state threaded through every node.

    A `TypedDict` rather than a Pydantic model because LangGraph node
    functions return *partial* dict updates it merges in - the framework's
    own state-update contract, not a design preference.
    """

    raw_email_bytes: bytes
    workflow_id: str
    email: ParsedEmail | None
    documents: list[ParsedDocument | None]
    security_flags: list[str]
    order: OrderCandidate | None
    customer_match: EntityMatch | None
    product_matches: list[EntityMatch]
    validation_result: ValidationResult | None
    duplicate_result: DuplicateCheckResult | None
    decision: RiskDecision | None
    human_decision: dict | None
    created_order: ErpOrder | None


def build_graph(
    erp: ERPClient,
    duplicate_detector: DuplicateDetector,
    *,
    use_cache: bool = False,
    cache_path: Path | None = None,
):
    """Compiles the LangGraph state machine.

    Args:
        erp: Shared `ERPClient` - same instance across every email run
            through this graph, so idempotency/duplicate behavior holds.
        duplicate_detector: Shared `DuplicateDetector`, same reason.
        use_cache: If `True`, extraction calls go through
            `extraction.extract_order_with_cache` instead of a live LLM
            call - the notebook's replay mode.
        cache_path: Required when `use_cache=True`.

    Returns:
        A compiled, checkpointed LangGraph graph. Call `.invoke(...)` with
        a `{"raw_email_bytes": ...}` input and a `config` carrying a
        `thread_id` - `HUMAN_REVIEW` cases pause mid-run; resume with
        `graph.invoke(Command(resume=<HumanDecision dict>), config)`.
    """

    def node_ingest(state: GraphState) -> dict:
        workflow_id = state.get("workflow_id") or str(uuid.uuid4())
        try:
            email = parse_email(state["raw_email_bytes"])
        except ParserError as exc:
            audit.record(
                AuditEvent(workflow_id=workflow_id, stage="graph.ingest", status="failed", error_category=str(exc)),
                path=erp.audit_path,
            )
            return {"workflow_id": workflow_id, "email": None, "decision": technical_failure(f"unparseable email: {exc}")}
        return {"workflow_id": workflow_id, "email": email}

    def route_after_ingest(state: GraphState) -> Literal["security", "finalize"]:
        return "finalize" if state.get("decision") is not None else "security"

    def node_security(state: GraphState) -> dict:
        documents, security_flags = parse_safe_attachments(
            state["email"], workflow_id=state["workflow_id"], audit_path=erp.audit_path
        )
        return {"documents": documents, "security_flags": security_flags}

    def node_extract(state: GraphState) -> dict:
        email = state["email"]
        documents = [doc for doc in state["documents"] if doc is not None]
        try:
            if use_cache:
                order = extraction.extract_order_with_cache(
                    email, documents, workflow_id=state["workflow_id"], cache_path=cache_path, audit_path=erp.audit_path
                )
            else:
                order = extraction.extract_order(
                    email, documents, workflow_id=state["workflow_id"], audit_path=erp.audit_path
                )
        except extraction.ExtractionError as exc:
            return {"decision": technical_failure(str(exc))}
        return {"order": order}

    def route_after_extract(state: GraphState) -> Literal["resolve", "finalize"]:
        return "finalize" if state.get("decision") is not None else "resolve"

    def node_resolve(state: GraphState) -> dict:
        order = state["order"]
        email = state["email"]
        customer_match = resolve_customer(order.customer_reference, email.sender, erp, workflow_id=state["workflow_id"])
        product_matches = [
            resolve_product(item.product_reference, erp, workflow_id=state["workflow_id"]) for item in order.line_items
        ]
        return {"customer_match": customer_match, "product_matches": product_matches}

    def node_validate(state: GraphState) -> dict:
        validation_result = validate_order(
            state["order"], state["customer_match"], state["product_matches"], erp, workflow_id=state["workflow_id"]
        )
        if any(doc is None for doc in state["documents"]):
            validation_result.issues.append(unreadable_attachment_issue())
        return {"validation_result": validation_result}

    def node_duplicate_check(state: GraphState) -> dict:
        return {"duplicate_result": duplicate_detector.check(state["email"], state["order"])}

    def node_risk_gate(state: GraphState) -> dict:
        combined_security_flags = list(dict.fromkeys([*state["security_flags"], *state["order"].security_flags]))
        decision = decide(
            state["order"], state["customer_match"], state["validation_result"], state["duplicate_result"], combined_security_flags
        )
        return {"decision": decision}

    def route_after_risk_gate(state: GraphState) -> Literal["execute", "human_review", "finalize"]:
        outcome = state["decision"].outcome
        if outcome == WorkflowOutcome.AUTO_CREATE:
            return "execute"
        if outcome == WorkflowOutcome.HUMAN_REVIEW:
            return "human_review"
        return "finalize"

    def node_human_review(state: GraphState) -> dict:
        request_payload = {
            "workflow_id": state["workflow_id"],
            "order": state["order"].model_dump(mode="json"),
            "validation_issues": [issue.model_dump() for issue in state["validation_result"].issues],
            "risk_decision": state["decision"].model_dump(),
        }
        human_response = interrupt(request_payload)
        audit.record(
            AuditEvent(
                workflow_id=state["workflow_id"],
                stage="graph.human_review",
                status="resumed",
                human_action=human_response.get("action"),
            ),
            path=erp.audit_path,
        )
        return {"human_decision": human_response}

    def route_after_human_review(state: GraphState) -> Literal["execute", "finalize"]:
        return "execute" if state["human_decision"]["action"] in ("approve", "approve_with_edits") else "finalize"

    def node_execute(state: GraphState) -> dict:
        order = state["order"]
        workflow_id = state["workflow_id"]
        human_decision = state.get("human_decision")
        edited_fields = human_decision["edited_fields"] if human_decision else {}

        # Defense in depth: `resume_with_human_decision` already validates
        # this before the graph ever sees it, but `execute` doesn't trust
        # that every caller went through that door (e.g. a direct
        # `graph.invoke(Command(resume=...))`) - reuses the exact same
        # check so the two can't drift apart.
        if human_decision is not None:
            errors = _describe_edit_errors(
                HumanDecision.model_validate(human_decision), order, state["customer_match"], erp, workflow_id=workflow_id
            )
            if errors:
                raise ValueError(f"cannot execute workflow {workflow_id}: {'; '.join(errors)}")

        customer_id = edited_fields.get("customer_id") or state["customer_match"].resolved_id
        line_items = [
            resolve_erp_line_item(item, product_match, customer_id, erp, workflow_id=workflow_id)
            for item, product_match in zip(order.line_items, state["product_matches"], strict=True)
        ]
        if "quantity" in edited_fields and line_items:
            # `model_copy(update=...)` does not re-validate/coerce - an
            # edited value must be converted to the field's real type
            # before the copy, or it lands in the ERP write as a raw
            # string instead of a `Decimal`.
            line_items[0] = line_items[0].model_copy(update={"quantity": Decimal(edited_fields["quantity"])})

        idempotency_key = state["duplicate_result"].order_key
        if order.intent == Intent.CREATE:
            created_order = erp.create_order(
                CreateOrderRequest(
                    idempotency_key=idempotency_key,
                    customer_id=customer_id,
                    purchase_order_reference=edited_fields.get("po_reference", order.po_reference),
                    line_items=line_items,
                ),
                workflow_id=workflow_id,
            )
        elif order.intent == Intent.UPDATE:
            target = erp.get_order(order.target_order_id, workflow_id=workflow_id)
            created_order = erp.update_order(
                UpdateOrderRequest(
                    idempotency_key=idempotency_key,
                    order_id=order.target_order_id,
                    line_items=line_items or target.line_items,
                ),
                workflow_id=workflow_id,
            )
        else:  # Intent.CANCEL
            created_order = erp.cancel_order(order.target_order_id, idempotency_key=idempotency_key, workflow_id=workflow_id)

        return {"created_order": created_order}

    def node_finalize(state: GraphState) -> dict:
        decision = state["decision"]
        human_decision = state.get("human_decision")
        if human_decision:
            action = human_decision["action"]
            if action in ("approve", "approve_with_edits") and state.get("created_order") is not None:
                decision = RiskDecision(
                    outcome=WorkflowOutcome.EXECUTED_WITH_HUMAN_APPROVAL,
                    reason_codes=[*decision.reason_codes, "HUMAN_APPROVED_EXECUTION"],
                    notes=human_decision["reason"],
                )
            else:
                override = {
                    "reject": decision.outcome,  # stays HUMAN_REVIEW; human_action records the rejection
                    "request_clarification": WorkflowOutcome.CLARIFICATION_REQUIRED,
                    "quarantine": WorkflowOutcome.SECURITY_QUARANTINE,
                }.get(action, decision.outcome)
                decision = RiskDecision(
                    outcome=override,
                    reason_codes=decision.reason_codes,
                    notes=human_decision["reason"],
                )

        # AuditEvent has no dedicated reviewer_id/edits/reason fields, so
        # they're folded into decision_reasons as prefixed entries - keeps
        # the reviewer's identity, edits, and reason on the audit line
        # itself (Task 21's requirement) without widening the schema for
        # a human-review-only concern.
        reason_codes = list(decision.reason_codes)
        if human_decision:
            reason_codes.append(f"reviewer:{human_decision['reviewer_id']}")
            reason_codes.append(f"reviewer_reason:{human_decision['reason']}")
            reason_codes.extend(f"edit:{field}={value}" for field, value in human_decision["edited_fields"].items())

        audit.record(
            AuditEvent(
                workflow_id=state["workflow_id"],
                stage="graph.finalize",
                status=decision.outcome.value,
                decision=decision.outcome.value,
                decision_reasons=reason_codes,
                human_action=human_decision["action"] if human_decision else None,
            ),
            path=erp.audit_path,
        )
        return {"decision": decision}

    builder = StateGraph(GraphState)
    builder.add_node("ingest", node_ingest)
    builder.add_node("security", node_security)
    builder.add_node("extract", node_extract)
    builder.add_node("resolve", node_resolve)
    builder.add_node("validate", node_validate)
    builder.add_node("duplicate_check", node_duplicate_check)
    builder.add_node("risk_gate", node_risk_gate)
    builder.add_node("human_review", node_human_review)
    builder.add_node("execute", node_execute)
    builder.add_node("finalize", node_finalize)

    builder.add_edge(START, "ingest")
    builder.add_conditional_edges("ingest", route_after_ingest)
    builder.add_edge("security", "extract")
    builder.add_conditional_edges("extract", route_after_extract)
    builder.add_edge("resolve", "validate")
    builder.add_edge("validate", "duplicate_check")
    builder.add_edge("duplicate_check", "risk_gate")
    builder.add_conditional_edges("risk_gate", route_after_risk_gate)
    builder.add_conditional_edges("human_review", route_after_human_review)
    builder.add_edge("execute", "finalize")
    builder.add_edge("finalize", END)

    # The checkpointer only ever (de)serializes state this same process
    # just produced (never untrusted external input), so trusting our own
    # domain types for msgpack round-tripping is safe - avoids a
    # deprecation-warning storm from the default serializer refusing to
    # recognize them. langgraph matches on exact (module, qualname) pairs,
    # not module wildcards, so every `schema.py` class is listed by
    # reflection rather than hand-enumerated (and silently kept in sync
    # if a class is added later).
    schema_classes = [obj for _, obj in inspect.getmembers(schema, inspect.isclass) if obj.__module__ == schema.__name__]
    serde = JsonPlusSerializer(allowed_msgpack_modules=[*schema_classes, DuplicateCheckResult])
    return builder.compile(checkpointer=MemorySaver(serde=serde))


def run_email(raw_email_bytes: bytes, graph, *, thread_id: str | None = None) -> tuple[dict, dict]:
    """Runs one email through the graph up to completion or the first interrupt.

    Args:
        raw_email_bytes: Full contents of a `.eml` file.
        graph: A graph from `build_graph`.
        thread_id: LangGraph checkpoint thread id; defaults to a fresh UUID.
            Needed again, unchanged, to resume via `resume_with_human_decision`.

    Returns:
        `(result_state, config)` - `config` must be passed to
        `resume_with_human_decision` if `result_state` contains
        `"__interrupt__"`.
    """
    config = {"configurable": {"thread_id": thread_id or str(uuid.uuid4())}}
    result = graph.invoke({"raw_email_bytes": raw_email_bytes}, config=config)
    return result, config


def resume_with_human_decision(graph, config: dict, decision: HumanDecision, *, erp: ERPClient) -> dict:
    """Resumes a paused (`HUMAN_REVIEW`) graph run with a reviewer's decision.

    Validates `decision.edited_fields` against the paused state *before*
    calling `graph.invoke` - a reviewer's free-text edits are untrusted
    input the same way the LLM's output is. Because validation happens
    before the interrupt is consumed, an invalid decision never touches
    the graph: the thread stays paused and genuinely resumable, so the
    caller can simply call this again with a corrected `HumanDecision`.

    Args:
        graph: The same compiled graph the run was started on.
        config: The `config` returned by `run_email` for that run.
        decision: approve / approve_with_edits / reject / request_clarification / quarantine.
        erp: The same `ERPClient` instance the graph was built with - used
            to check an edited `customer_id` actually exists.

    Returns:
        The final state after resuming.

    Raises:
        InvalidHumanDecisionError: If `decision.edited_fields` can't be
            applied (e.g. a non-numeric quantity, an unknown customer_id).
            The paused thread is unaffected - retry with a corrected decision.
    """
    state = graph.get_state(config).values
    errors = _describe_edit_errors(decision, state["order"], state["customer_match"], erp, workflow_id=state["workflow_id"])
    if errors:
        raise InvalidHumanDecisionError("; ".join(errors))
    return graph.invoke(Command(resume=decision.model_dump()), config=config)
