"""Tests for the shared pinned encoder (encode_pinned)."""

from __future__ import annotations

import json

import pytest

from fabulexa_forge.exporters.streaming.encoding import encode_pinned
from fabulexa_forge.exporters.streaming.jsonl import (
    _serialize_object,
    render_jsonl_object,
)

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


# ---------------------------------------------------------------------------
# Byte-identity with jsonl _serialize_object
# ---------------------------------------------------------------------------


def _make_event_dict() -> dict[str, object]:
    return {
        "seq": 1,
        "op": "c",
        "ts": "2024-01-01T00:00:00Z",
        "kind": "patient",
        "key": {"record_id": "r1"},
        "after": {"name": "Alice", "status": "active"},
    }


def test_encode_pinned_plus_newline_equals_jsonl_serialize_object() -> None:
    """encode_pinned(obj) + '\\n' is byte-identical to _serialize_object(obj)."""
    obj = _make_event_dict()
    assert encode_pinned(obj) + "\n" == _serialize_object(obj)


def test_encode_pinned_matches_jsonl_render_pipeline() -> None:
    """encode_pinned(render_jsonl_object(event)) + '\\n' equals _serialize_object result."""

    class _FakeEvent:
        seq = 42
        op = "u"
        ts = "2024-06-01T12:00:00Z"
        kind = "ward"
        record_id = "r99"
        after: dict[str, object] | None = {"bed_count": "10"}
        topic = "ward"
        key_column = "record_id"
        key_value = "r99"

    event = _FakeEvent()
    obj = render_jsonl_object(event)  # type: ignore[arg-type]
    assert encode_pinned(obj) + "\n" == _serialize_object(obj)


# ---------------------------------------------------------------------------
# Byte-identity with debezium _serialize_message
# ---------------------------------------------------------------------------


def test_encode_pinned_plus_newline_equals_debezium_serialize_message() -> None:
    """encode_pinned(msg) + '\\n' is byte-identical to debezium _serialize_message(msg)."""
    from fabulexa_forge.exporters.streaming.debezium import _serialize_message

    msg: dict[str, object] = {
        "schema": None,
        "payload": {"op": "c", "source": {"table": "patient"}},
    }
    assert encode_pinned(msg) + "\n" == _serialize_message(msg)


def test_encode_pinned_debezium_non_ascii_survives() -> None:
    """Non-ASCII in a debezium payload survives unescaped through encode_pinned."""
    from fabulexa_forge.exporters.streaming.debezium import _serialize_message

    msg: dict[str, object] = {"payload": {"name": "Ünïcödé"}}
    pinned_line = encode_pinned(msg) + "\n"
    debezium_line = _serialize_message(msg)
    assert pinned_line == debezium_line
    assert "Ünïcödé" in pinned_line


# ---------------------------------------------------------------------------
# encode_pinned with schemas on and off (debezium render)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value_schema", [None, {"type": "struct", "fields": []}])
def test_encode_pinned_debezium_render_message_byte_identity(
    value_schema: dict[str, object] | None,
) -> None:
    """encode_pinned(render_debezium_message(...)) + '\\n' equals _serialize_message."""
    from fabulexa_forge.exporters.streaming.debezium import (
        _serialize_message,
        render_debezium_message,
    )

    class _FakeEvent:
        seq = 1
        op = "c"
        ts = "2024-01-01T00:00:00Z"
        kind = "patient"
        record_id = "r1"
        after: dict[str, object] | None = {"name": "Alice"}
        topic = "patient"
        route_table = "patient"

    from fabulexa_forge.config.models import DebeziumSourceIdentity

    source_identity = DebeziumSourceIdentity.model_validate(
        {
            "connector": "fabulexa",
            "name": "test",
            "db": "testdb",
            "schema": "public",
            "version": "1.0.0",
        }
    )

    ts_ms = 1704067200000
    msg = render_debezium_message(
        event=_FakeEvent(),  # type: ignore[arg-type]
        ts_ms=ts_ms,
        source_identity=source_identity,
        table="patient",
        value_schema=value_schema,
    )
    assert encode_pinned(msg) + "\n" == _serialize_message(msg)
