"""Tests for extraction.py's replay-mode cache - offline, no live API calls."""

import json
from unittest.mock import patch

from src.extraction import extract_order_with_cache
from src.schema import Intent, OrderCandidate


def _order(message_id: str) -> OrderCandidate:
    return OrderCandidate(
        message_id=message_id, intent=Intent.CREATE, language="en", extraction_confidence=0.9
    )


def test_cache_miss_calls_extract_order_and_writes_cache(tmp_path):
    cache_path = tmp_path / "cache.json"
    email = _fake_email("<m1@example.com>")

    with patch("src.extraction.extract_order", return_value=_order("<m1@example.com>")) as mock_extract:
        order = extract_order_with_cache(email, [], workflow_id="wf", cache_path=cache_path)

    mock_extract.assert_called_once()
    assert order.message_id == "<m1@example.com>"
    assert json.loads(cache_path.read_text())["<m1@example.com>"]["intent"] == "create"


def test_cache_hit_never_calls_extract_order(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"<m1@example.com>": json.loads(_order("<m1@example.com>").model_dump_json())}))
    email = _fake_email("<m1@example.com>")

    with patch("src.extraction.extract_order") as mock_extract:
        order = extract_order_with_cache(email, [], workflow_id="wf", cache_path=cache_path)

    mock_extract.assert_not_called()
    assert order.message_id == "<m1@example.com>"


def _fake_email(message_id: str):
    from src.schema import ParsedEmail

    return ParsedEmail(message_id=message_id, sender="a@b.com", recipients=[], subject="s", body_text="b")
