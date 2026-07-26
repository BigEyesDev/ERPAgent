"""Tests for src/entity_resolution.py against the real ERP fixture data."""

import pytest

from src.entity_resolution import resolve_customer, resolve_product
from src.erp_client import ERPClient
from src.schema import MatchType


@pytest.fixture
def erp(tmp_path):
    return ERPClient(audit_path=tmp_path / "audit.jsonl")


def test_exact_customer_match_by_name(erp):
    match = resolve_customer("Nordwind Bau GmbH", "someone@other-domain.example", erp, workflow_id="wf-1")
    assert match.match_type == MatchType.EXACT
    assert match.resolved_id == "CUST-1001"


def test_domain_match_when_no_name_given(erp):
    match = resolve_customer(None, "purchasing@bergtal.example", erp, workflow_id="wf-1")
    assert match.match_type == MatchType.DOMAIN
    assert match.resolved_id == "CUST-1002"


def test_conflict_when_name_and_domain_disagree(erp):
    match = resolve_customer("Bergtal Maschinenbau AG", "einkauf@nordwind-bau.example", erp, workflow_id="wf-1")
    assert match.match_type == MatchType.CONFLICT
    assert set(match.candidates) == {"CUST-1001", "CUST-1002"}


def test_fuzzy_customer_match_via_alias_typo(erp):
    match = resolve_customer("Nordwind Construction Co", "someone@unrelated.example", erp, workflow_id="wf-1")
    assert match.match_type == MatchType.FUZZY
    assert match.resolved_id == "CUST-1001"


def test_unknown_customer_resolves_to_none(erp):
    match = resolve_customer("Totally Unknown Corp", "buyer@unknownco.example", erp, workflow_id="wf-1")
    assert match.match_type == MatchType.NONE
    assert match.resolved_id is None


def test_exact_product_match_by_sku(erp):
    match = resolve_product("SKU-100", erp, workflow_id="wf-1")
    assert match.match_type == MatchType.EXACT
    assert match.resolved_id == "SKU-100"


def test_exact_product_match_by_alias(erp):
    match = resolve_product("M8 bolt", erp, workflow_id="wf-1")
    assert match.match_type == MatchType.EXACT
    assert match.resolved_id == "SKU-200"


def test_unknown_product_resolves_to_none(erp):
    match = resolve_product("Titanium Widget XL-9000", erp, workflow_id="wf-1")
    assert match.match_type == MatchType.NONE
    assert match.resolved_id is None
