"""Mock ERP REST client: reads fixture master data, writes to an in-memory order store.

Read methods return snapshots of `data/erp/*.json` - the checked-in
fixtures are never mutated by this module. Write methods (`create_order`,
`update_order`, `cancel_order`) mutate only an in-process copy of
`orders.json`, are idempotency-key aware, and each emits exactly one
`audit.py` event. This module never imports `extraction.py` or anything
LLM-related - it is the write surface the extraction LLM session must never
reach.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path

from src import audit
from src.config import settings
from src.schema import (
    Address,
    AuditEvent,
    CreateOrderRequest,
    Customer,
    ErpLineItem,
    ErpOrder,
    PriceEntry,
    Product,
    PurchaseHistoryEntry,
    UpdateOrderRequest,
)

DEFAULT_ERP_DIR = Path(__file__).resolve().parent.parent / "data" / "erp"


class ErpClientError(Exception):
    """Raised for lookups/writes against entities that do not exist."""


def _load_json(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


class ERPClient:
    """In-memory mock of the ERP's read + write surface."""

    def __init__(self, erp_dir: Path | str = DEFAULT_ERP_DIR, *, audit_path: str | Path | None = None):
        erp_dir = Path(erp_dir)
        self._audit_path = audit_path
        # Public so callers that share this ERPClient (pipeline.py,
        # graph.py) can route their own top-level audit.record calls to
        # the same file, rather than silently falling back to
        # settings.audit_path - a real bug found live: those calls
        # weren't threading a path at all, so every "pipeline.decision" /
        # "graph.finalize" audit line was landing in the global default
        # audit log regardless of what this instance was configured with.
        self.audit_path = audit_path or settings.audit_path

        self._customers = {
            row["customer_id"]: Customer(
                **{
                    **row,
                    "billing_address": _to_address(row.get("billing_address")),
                    "shipping_addresses": [_to_address(a) for a in row.get("shipping_addresses", [])],
                }
            )
            for row in _load_json(erp_dir / "customers.json")
        }
        self._products = {row["sku"]: Product(**row) for row in _load_json(erp_dir / "products.json")}
        self._prices = [PriceEntry(**row) for row in _load_json(erp_dir / "prices.json")]
        self._purchase_history = [
            PurchaseHistoryEntry(**row) for row in _load_json(erp_dir / "purchase_history.json")
        ]
        self._orders: dict[str, ErpOrder] = {
            row["order_id"]: ErpOrder(**row) for row in _load_json(erp_dir / "orders.json")
        }
        self._idempotency_keys: dict[str, str] = {}

    # -- reads -----------------------------------------------------------

    def get_customer(self, customer_id: str, *, workflow_id: str) -> Customer | None:
        result = self._customers.get(customer_id)
        self._audit(workflow_id, "erp_client.get_customer", "found" if result else "not_found")
        return result

    def list_customers(self, *, workflow_id: str) -> list[Customer]:
        result = list(self._customers.values())
        self._audit(workflow_id, "erp_client.list_customers", "found")
        return result

    def list_products(self, *, workflow_id: str) -> list[Product]:
        result = list(self._products.values())
        self._audit(workflow_id, "erp_client.list_products", "found")
        return result

    def find_customer_by_email_domain(self, domain: str, *, workflow_id: str) -> Customer | None:
        result = next(
            (c for c in self._customers.values() if domain.lower() in {d.lower() for d in c.email_domains}),
            None,
        )
        self._audit(workflow_id, "erp_client.find_customer_by_email_domain", "found" if result else "not_found")
        return result

    def get_product(self, sku: str, *, workflow_id: str) -> Product | None:
        result = self._products.get(sku)
        self._audit(workflow_id, "erp_client.get_product", "found" if result else "not_found")
        return result

    def get_price(
        self,
        customer_id: str,
        sku: str,
        unit: str,
        currency: str,
        *,
        as_of: date | None = None,
        workflow_id: str,
    ) -> PriceEntry | None:
        as_of = as_of or date.today()
        result = next(
            (
                p
                for p in self._prices
                if p.customer_id == customer_id
                and p.sku == sku
                and p.unit == unit
                and p.currency == currency
                and p.valid_from <= as_of <= p.valid_to
            ),
            None,
        )
        self._audit(workflow_id, "erp_client.get_price", "found" if result else "not_found")
        return result

    def get_default_price(
        self, customer_id: str, sku: str, *, as_of: date | None = None, workflow_id: str
    ) -> PriceEntry | None:
        """Finds a valid price for `customer_id`/`sku` in any unit/currency.

        Used as a fallback when the sender didn't state a unit or currency
        explicitly - the ERP's own price list is authoritative, so a
        create order isn't blocked just because the customer omitted a
        price the ERP already knows.
        """
        as_of = as_of or date.today()
        result = next(
            (
                p
                for p in self._prices
                if p.customer_id == customer_id and p.sku == sku and p.valid_from <= as_of <= p.valid_to
            ),
            None,
        )
        self._audit(workflow_id, "erp_client.get_default_price", "found" if result else "not_found")
        return result

    def get_purchase_history(self, customer_id: str, *, workflow_id: str) -> list[PurchaseHistoryEntry]:
        result = [entry for entry in self._purchase_history if entry.customer_id == customer_id]
        self._audit(workflow_id, "erp_client.get_purchase_history", "found" if result else "not_found")
        return result

    def get_order(self, order_id: str, *, workflow_id: str) -> ErpOrder | None:
        result = self._orders.get(order_id)
        self._audit(workflow_id, "erp_client.get_order", "found" if result else "not_found")
        return result

    # -- writes ------------------------------------------------------------
    # Every write is idempotency-key aware: replaying the same key returns
    # the original result instead of mutating state again, but still emits
    # its own audit event so replays stay visible in the trail.

    def create_order(self, request: CreateOrderRequest, *, workflow_id: str) -> ErpOrder:
        existing_id = self._idempotency_keys.get(request.idempotency_key)
        if existing_id is not None:
            self._audit(workflow_id, "erp_client.create_order", "idempotent_replay", decision=existing_id)
            return self._orders[existing_id]

        if request.customer_id not in self._customers:
            self._audit(workflow_id, "erp_client.create_order", "error", error_category="unknown_customer")
            raise ErpClientError(f"unknown customer_id {request.customer_id!r}")

        order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
        order = ErpOrder(
            order_id=order_id,
            customer_id=request.customer_id,
            purchase_order_reference=request.purchase_order_reference,
            status="confirmed",
            line_items=request.line_items,
        )
        self._orders[order_id] = order
        self._idempotency_keys[request.idempotency_key] = order_id
        self._audit(workflow_id, "erp_client.create_order", "created", decision=order_id)
        return order

    def update_order(self, request: UpdateOrderRequest, *, workflow_id: str) -> ErpOrder:
        existing_id = self._idempotency_keys.get(request.idempotency_key)
        if existing_id is not None:
            self._audit(workflow_id, "erp_client.update_order", "idempotent_replay", decision=existing_id)
            return self._orders[existing_id]

        order = self._orders.get(request.order_id)
        if order is None:
            self._audit(workflow_id, "erp_client.update_order", "error", error_category="unknown_order")
            raise ErpClientError(f"unknown order_id {request.order_id!r}")

        updated = order.model_copy(update={"line_items": request.line_items})
        self._orders[request.order_id] = updated
        self._idempotency_keys[request.idempotency_key] = request.order_id
        self._audit(workflow_id, "erp_client.update_order", "updated", decision=request.order_id)
        return updated

    def cancel_order(self, order_id: str, *, idempotency_key: str, workflow_id: str) -> ErpOrder:
        existing_id = self._idempotency_keys.get(idempotency_key)
        if existing_id is not None:
            self._audit(workflow_id, "erp_client.cancel_order", "idempotent_replay", decision=existing_id)
            return self._orders[existing_id]

        order = self._orders.get(order_id)
        if order is None:
            self._audit(workflow_id, "erp_client.cancel_order", "error", error_category="unknown_order")
            raise ErpClientError(f"unknown order_id {order_id!r}")

        cancelled = order.model_copy(update={"status": "cancelled"})
        self._orders[order_id] = cancelled
        self._idempotency_keys[idempotency_key] = order_id
        self._audit(workflow_id, "erp_client.cancel_order", "cancelled", decision=order_id)
        return cancelled

    def _audit(self, workflow_id: str, stage: str, status: str, **fields) -> None:
        audit.record(
            AuditEvent(workflow_id=workflow_id, stage=stage, status=status, **fields),
            path=self._audit_path,
        )


def _to_address(raw: dict | None) -> Address | None:
    return Address(**raw) if raw else None
