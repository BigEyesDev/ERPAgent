"""Internal consistency tests for the generated email-to-ERP dataset."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from pathlib import Path

import fitz
import openpyxl
import pytest

from scripts.generate_fixtures import normalized_fingerprint

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MANIFEST_PATH = DATA / "manifests" / "fixture_manifest.json"


@pytest.fixture(scope="module")
def manifest() -> list[dict]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["fixtures"]


def _expected(record: dict) -> dict:
    return json.loads((ROOT / record["expected_path"]).read_text(encoding="utf-8"))


def _message(record: dict):
    return BytesParser(policy=policy.default).parsebytes((ROOT / record["email_path"]).read_bytes())


def test_manifest_has_all_26_scenarios(manifest):
    assert len(manifest) == 26
    assert [record["fixture_id"] for record in manifest] == [f"{index:03d}" for index in range(1, 27)]
    assert all(record["expected_outcome"] in {
        "AUTO_CREATE",
        "HUMAN_REVIEW",
        "CLARIFICATION_REQUIRED",
        "SECURITY_QUARANTINE",
        "DUPLICATE_NOOP",
        "TECHNICAL_FAILURE",
    } for record in manifest)


def test_manifest_files_and_email_integrity(manifest):
    for record in manifest:
        email_path = ROOT / record["email_path"]
        expected_path = ROOT / record["expected_path"]
        assert email_path.is_file()
        assert expected_path.is_file()
        message = _message(record)
        for header in ("Message-ID", "Date", "From", "To", "Subject"):
            assert message[header], f"{record['fixture_id']} missing {header}"
        mime_attachments = list(message.iter_attachments())
        expected_names = [Path(path).name for path in record["attachment_paths"]]
        assert [part.get_filename() for part in mime_attachments] == expected_names
        for part, relative_path in zip(mime_attachments, record["attachment_paths"], strict=True):
            attachment_path = ROOT / relative_path
            assert attachment_path.is_file()
            assert part.get_payload(decode=True) == attachment_path.read_bytes()


def test_expected_schema_and_erp_references(manifest):
    customers = {item["customer_id"]: item for item in json.loads((DATA / "erp/customers.json").read_text())}
    products = {item["sku"]: item for item in json.loads((DATA / "erp/products.json").read_text())}
    orders = {item["order_id"]: item for item in json.loads((DATA / "erp/orders.json").read_text())}
    for record in manifest:
        expected = _expected(record)
        extraction = expected["expected_extraction"]
        workflow = expected["expected_workflow"]
        assert expected["fixture_id"] == record["fixture_id"]
        assert workflow["outcome"] == record["expected_outcome"]
        assert isinstance(workflow["reason_codes"], list) and workflow["reason_codes"]
        assert workflow["erp_write_allowed"] is (workflow["outcome"] == "AUTO_CREATE")
        customer = extraction["customer"]
        if customer and customer["match_type"] == "exact":
            assert customer["customer_id"] in customers
            assert customers[customer["customer_id"]]["name"] == customer["name"]
        for item in extraction["line_items"]:
            if item["sku"] != "SKU-999":
                assert item["sku"] in products
        if extraction["existing_order_id"]:
            assert extraction["existing_order_id"] in orders


def test_positive_fixtures_are_complete_and_decimal_safe(manifest):
    for record in manifest:
        expected = _expected(record)
        if expected["expected_workflow"]["outcome"] != "AUTO_CREATE":
            continue
        extraction = expected["expected_extraction"]
        assert extraction["purchase_order_reference"]
        assert not extraction["ambiguities"]
        assert extraction["line_items"]
        for item in extraction["line_items"]:
            assert isinstance(item["quantity"], (int, float)) and not isinstance(item["quantity"], bool)
            assert item["currency"]
            assert Decimal(item["unit_price"]) > 0


def test_price_labels_match_master_data(manifest):
    prices = {
        (item["customer_id"], item["sku"], item["unit"], item["currency"]): Decimal(item["unit_price"])
        for item in json.loads((DATA / "erp/prices.json").read_text())
    }
    for record in manifest:
        expected = _expected(record)
        extraction = expected["expected_extraction"]
        customer_id = (extraction["customer"] or {}).get("customer_id")
        reasons = expected["expected_workflow"]["reason_codes"]
        for item in extraction["line_items"]:
            key = (customer_id, item["sku"], item["unit"], item["currency"])
            if key not in prices or item["unit_price"] is None or not str(item["unit_price"]).replace(".", "", 1).isdigit():
                continue
            fixture_price = Decimal(str(item["unit_price"]))
            if "PRICE_MISMATCH" in reasons:
                assert fixture_price != prices[key]
            if "PRICE_MATCH" in reasons:
                assert fixture_price == prices[key]


def test_conflict_fixture_preserves_real_source_values(manifest):
    record = next(item for item in manifest if item["fixture_id"] == "014")
    expected = _expected(record)
    conflicts = expected["expected_extraction"]["line_items"][0]["conflicting_values"]
    assert {item["value"] for item in conflicts} == {150, 500}
    assert "150 EA" in _message(record).get_body(preferencelist=("plain",)).get_content()
    workbook = openpyxl.load_workbook(DATA / "attachments/order_conflict.xlsx", data_only=False)
    assert workbook["Order"]["B7"].value == 500
    workbook.close()


def test_security_documents_have_the_claimed_structure(manifest):
    encrypted = fitz.open(DATA / "attachments/order_encrypted.pdf")
    assert encrypted.is_encrypted
    encrypted.close()

    with pytest.raises(Exception):
        malformed = fitz.open(DATA / "attachments/order_malformed.pdf")
        try:
            list(malformed)
        finally:
            malformed.close()

    scanned = fitz.open(DATA / "attachments/order_scanned.pdf")
    assert scanned.page_count == 1
    assert scanned[0].get_text().strip() == ""
    scanned.close()

    mismatch = DATA / "attachments/order_extension_mismatch.xlsm"
    with zipfile.ZipFile(mismatch) as archive:
        names = {name.lower() for name in archive.namelist()}
        assert "xl/vbaproject.bin" not in names
        content_types = archive.read("[Content_Types].xml")
        assert b"macroEnabled" not in content_types

    macro_record = next(item for item in manifest if item["fixture_id"] == "019")
    if macro_record["fixture_available"]:
        with zipfile.ZipFile(DATA / "attachments/order_macro_enabled.xlsm") as archive:
            assert "xl/vbaproject.bin" in {name.lower() for name in archive.namelist()}
    else:
        assert macro_record["attachment_paths"] == []
        assert not (DATA / "attachments/order_macro_enabled.xlsm").exists()


def test_duplicate_fingerprint_is_identical(manifest):
    records = {record["fixture_id"]: record for record in manifest}
    fingerprints = []
    for fixture_id in ("021", "022"):
        record = records[fixture_id]
        fingerprints.append(
            normalized_fingerprint(_expected(record), _message(record)["Message-ID"])
        )
    assert fingerprints[0] == fingerprints[1]


def _file_hashes(root: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted((root / "data").rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in {
            "data/attachments/order_encrypted.pdf",
            "data/emails/017_encrypted_pdf.eml",
        }:
            continue
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def test_generation_is_repeatable_in_isolated_roots(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    command = [sys.executable, str(ROOT / "scripts/generate_fixtures.py"), "--output-root"]
    subprocess.run([*command, str(first)], check=True, capture_output=True, text=True)
    subprocess.run([*command, str(second)], check=True, capture_output=True, text=True)
    assert {
        path.relative_to(first).as_posix() for path in first.rglob("*") if path.is_file()
    } == {
        path.relative_to(second).as_posix() for path in second.rglob("*") if path.is_file()
    }
    assert _file_hashes(first) == _file_hashes(second)
