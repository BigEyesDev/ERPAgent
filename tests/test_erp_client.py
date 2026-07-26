"""Tests for src/erp_client.py - mock ERP reads, writes, and idempotency."""

import pytest

from src import audit
from src.erp_client import ERPClient, ErpClientError
from src.schema import CreateOrderRequest, ErpLineItem, UpdateOrderRequest


@pytest.fixture
def client(tmp_path):
    return ERPClient(audit_path=tmp_path / "audit.jsonl")


def test_reads_return_fixture_data(client):
    customer = client.get_customer("CUST-1001", workflow_id="wf-1")
    assert customer is not None
    assert customer.name == "Nordwind Bau GmbH"

    product = client.get_product("SKU-100", workflow_id="wf-1")
    assert product is not None and product.status == "active"

    assert client.get_customer("CUST-9999", workflow_id="wf-1") is None


def test_create_update_cancel_round_trip(client):
    line_items = [ErpLineItem(sku="SKU-100", quantity=10, unit="EA", unit_price="4.50", currency="EUR")]
    created = client.create_order(
        CreateOrderRequest(idempotency_key="create-1", customer_id="CUST-1001", line_items=line_items),
        workflow_id="wf-1",
    )
    assert created.status == "confirmed"
    assert client.get_order(created.order_id, workflow_id="wf-1") == created

    updated_items = [ErpLineItem(sku="SKU-100", quantity=20, unit="EA", unit_price="4.50", currency="EUR")]
    updated = client.update_order(
        UpdateOrderRequest(idempotency_key="update-1", order_id=created.order_id, line_items=updated_items),
        workflow_id="wf-1",
    )
    assert updated.line_items[0].quantity == 20

    cancelled = client.cancel_order(created.order_id, idempotency_key="cancel-1", workflow_id="wf-1")
    assert cancelled.status == "cancelled"


def test_duplicate_idempotency_key_does_not_create_second_order(client):
    line_items = [ErpLineItem(sku="SKU-100", quantity=10, unit="EA", unit_price="4.50", currency="EUR")]
    request = CreateOrderRequest(idempotency_key="dup-key", customer_id="CUST-1001", line_items=line_items)

    orders_before = len(client._orders)
    first = client.create_order(request, workflow_id="wf-1")
    second = client.create_order(request, workflow_id="wf-1")

    assert first.order_id == second.order_id
    assert len(client._orders) == orders_before + 1


def test_update_unknown_order_raises(client):
    with pytest.raises(ErpClientError):
        client.update_order(
            UpdateOrderRequest(idempotency_key="k", order_id="ORD-DOES-NOT-EXIST", line_items=[]),
            workflow_id="wf-1",
        )


def test_every_call_produces_one_audit_line(client, tmp_path):
    client.get_customer("CUST-1001", workflow_id="wf-1")
    client.create_order(
        CreateOrderRequest(
            idempotency_key="audit-1",
            customer_id="CUST-1001",
            line_items=[ErpLineItem(sku="SKU-100", quantity=1, unit="EA", unit_price="4.50", currency="EUR")],
        ),
        workflow_id="wf-1",
    )
    events = audit.read_all(path=tmp_path / "audit.jsonl")
    assert [e.stage for e in events] == ["erp_client.get_customer", "erp_client.create_order"]
