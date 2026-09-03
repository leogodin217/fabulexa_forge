"""Tests for the shared pinned encoder (encode_pinned)."""

from __future__ import annotations

import json

from fabulexa_forge.exporters.streaming.encoding import encode_pinned

# ---------------------------------------------------------------------------
# encode_pinned — unit tests
# ---------------------------------------------------------------------------


def test_encode_pinned_compact_separators() -> None:
    """encode_pinned uses compact separators with no inter-token whitespace."""
    result = encode_pinned({"a": 1, "b": 2})
    assert result == '{"a":1,"b":2}'


def test_encode_pinned_no_trailing_newline() -> None:
    """encode_pinned returns no trailing newline."""
    result = encode_pinned({"x": "y"})
    assert not result.endswith("\n")


def test_encode_pinned_ensure_ascii_false() -> None:
    """Non-ASCII characters survive unescaped."""
    result = encode_pinned({"name": "José"})
    assert "José" in result
    assert "\\u" not in result


def test_encode_pinned_construction_order_preserved() -> None:
    """Keys appear in construction (insertion) order, not sorted."""
    obj = {"z": 1, "a": 2, "m": 3}
    result = encode_pinned(obj)
    keys = [k for k in json.loads(result)]
    assert keys == ["z", "a", "m"]


def test_encode_pinned_null_value() -> None:
    """None serializes as JSON null."""
    result = encode_pinned({"after": None})
    assert result == '{"after":null}'


def test_encode_pinned_nested_dict() -> None:
    """Nested dicts are compactly serialized."""
    result = encode_pinned({"key": {"record_id": "abc"}})
    assert result == '{"key":{"record_id":"abc"}}'
