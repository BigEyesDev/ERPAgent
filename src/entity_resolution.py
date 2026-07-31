"""Resolves raw sender text against ERP identities.

Answers "who/what is this" - a different question from "is this order
internally consistent" (`validation.py`). Kept separate so a fuzzy or
conflicting match routes to human review as one clean rule instead of a
condition buried inside validation logic. No LLM call: matching is exact
string comparison, sender-domain lookup, and a stdlib similarity ratio for
the fuzzy tier - never an upgrade from fuzzy to exact based on plausibility.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from src.erp_client import ERPClient
from src.schema import Customer, EntityMatch, MatchType

FUZZY_THRESHOLD = 0.6


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _best_alias_ratio(reference: str, candidate_names: list[str]) -> float:
    normalized_reference = _normalize(reference)
    return max(
        (SequenceMatcher(None, normalized_reference, _normalize(name)).ratio() for name in candidate_names),
        default=0.0,
    )


def _find_exact_customer(reference: str, customers: list[Customer]) -> Customer | None:
    normalized_reference = _normalize(reference)
    for customer in customers:
        names = [customer.name, *customer.aliases]
        if normalized_reference in {_normalize(name) for name in names}:
            return customer
    return None


def resolve_customer(
    customer_reference: str | None, sender_email: str, erp: ERPClient, *, workflow_id: str
) -> EntityMatch:
    """Resolves a customer from the raw sender text and/or sending domain.

    Args:
        customer_reference: Customer name/ID as written by the sender, if any.
        sender_email: The email's `From` address - used for domain matching.
        erp: Read-only ERP access.
        workflow_id: Threaded through to the audit trail via `erp`'s own calls.

    Returns:
        An `EntityMatch` :
        `EXACT`/`DOMAIN` only when unambiguous,
        `CONFLICT` when name and domain disagree, 
        `FUZZY` on a strong-but-imperfect name/alias match,
        `NONE` when nothing resolves.
    """
    customers = erp.list_customers(workflow_id=workflow_id)
    domain = sender_email.rsplit("@", 1)[-1] if "@" in sender_email else ""
    domain_match = erp.find_customer_by_email_domain(domain, workflow_id=workflow_id) if domain else None
    name_match = _find_exact_customer(customer_reference, customers) if customer_reference else None

    if name_match and domain_match and name_match.customer_id != domain_match.customer_id:
        return EntityMatch(
            match_type=MatchType.CONFLICT,
            resolved_id=None,
            confidence=0.5,
            candidates=[name_match.customer_id, domain_match.customer_id],
        )

    if name_match:
        return EntityMatch(match_type=MatchType.EXACT, resolved_id=name_match.customer_id, confidence=1.0)

    if domain_match:
        return EntityMatch(match_type=MatchType.DOMAIN, resolved_id=domain_match.customer_id, confidence=0.85)

    if customer_reference:
        scored = [
            (customer, _best_alias_ratio(customer_reference, [customer.name, *customer.aliases]))
            for customer in customers
        ]
        best_customer, best_ratio = max(scored, key=lambda pair: pair[1], default=(None, 0.0))
        if best_customer is not None and best_ratio >= FUZZY_THRESHOLD:
            return EntityMatch(
                match_type=MatchType.FUZZY, resolved_id=best_customer.customer_id, confidence=best_ratio
            )

    return EntityMatch(match_type=MatchType.NONE, resolved_id=None, confidence=0.0)


def resolve_product(product_reference: str, erp: ERPClient, *, workflow_id: str) -> EntityMatch:
    """Resolves a product from raw sender text against SKU, description, or alias.

    Args:
        product_reference: Product name/SKU as written by the sender.
        erp: Read-only ERP access.
        workflow_id: Threaded through to the audit trail via `erp`'s own calls.

    Returns:
        An `EntityMatch` for the SKU.
        `EXACT` on a direct SKU/description/alias hit,
        `FUZZY` on a strong-but-imperfect text match,
        `NONE` otherwise i.e. an unresolved product is never silently dropped from
        the order.
    """
    products = erp.list_products(workflow_id=workflow_id)
    normalized_reference = _normalize(product_reference)

    exact = next(
        (
            product
            for product in products
            if normalized_reference == _normalize(product.sku)
            or normalized_reference in {_normalize(name) for name in [product.description, *product.aliases]}
        ),
        None,
    )
    if exact is not None:
        return EntityMatch(match_type=MatchType.EXACT, resolved_id=exact.sku, confidence=1.0)

    scored = [
        (product, _best_alias_ratio(product_reference, [product.description, *product.aliases, product.sku]))
        for product in products
    ]
    best_product, best_ratio = max(scored, key=lambda pair: pair[1], default=(None, 0.0))
    if best_product is not None and best_ratio >= FUZZY_THRESHOLD:
        return EntityMatch(match_type=MatchType.FUZZY, resolved_id=best_product.sku, confidence=best_ratio)

    return EntityMatch(match_type=MatchType.NONE, resolved_id=None, confidence=0.0)
