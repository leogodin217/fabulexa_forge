"""Tests for jsonl.py: render_jsonl_object.

Covers format shape and key ordering. Sink-writer behavior (stdout/file
routing, byte-identity, empty-stream handling, defensive preconditions,
paced delivery, and abort-cleanup) is exercised through the live sink,
driver.write_line_stream, in test_driver.py.
"""

from __future__ import annotations

from typing import Literal

from fabulexa_forge.exporters.streaming.jsonl import render_jsonl_object
from fabulexa_forge.exporters.streaming.types import StreamEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    seq: int = 1,
    op: Literal["c", "u", "d", "r"] = "c",
    kind: str = "item",
    record_id: str = "r1",
    event_sim_time: int = 1000,
    ts: str | int = "2026-01-01T00:00:00+00:00",
    after: dict[str, object] | None = None,
    key_column: str = "record_id",
    key_value: str | None = None,
) -> StreamEvent:
    """Build a StreamEvent for tests.

    Under default routing (no sub-typing), topic == route_table == kind.
    key_column/key_value default to the byte-identical no-election rendering
    ({"record_id": record_id}); pass an elected surface to exercise the key
    map under election.
    """
    if after is None and op != "d":
        after = {"record_id": record_id, "status": "active"}
    return StreamEvent(
        seq=seq,
        op=op,
        kind=kind,
        record_id=record_id,
        event_sim_time=event_sim_time,
        ts=ts,
        after=after,
        topic=kind,
        route_table=kind,
        key_column=key_column,
        key_value=key_value if key_value is not None else record_id,
    )


# ---------------------------------------------------------------------------
# render_jsonl_object
# ---------------------------------------------------------------------------


class TestRenderJsonlObject:
    """Tests for render_jsonl_object shape and key ordering."""

    def test_key_order_is_seq_op_ts_kind_key_after(self) -> None:
        """Keys must appear in the exact order: seq, op, ts, kind, key, after."""
        event = _make_event()
        obj = render_jsonl_object(event)
        assert list(obj.keys()) == ["seq", "op", "ts", "kind", "key", "after"]

    def test_key_is_record_id_dict(self) -> None:
        """key is {"record_id": ...} under the default (no-election) surface."""
        event = _make_event(record_id="r42")
        obj = render_jsonl_object(event)
        assert obj["key"] == {"record_id": "r42"}

    def test_key_map_renders_elected_presentation_id_surface(self) -> None:
        """A presentation_id-elected event's key map is {"presentation_id": ...}."""
        event = _make_event(key_column="presentation_id", key_value="P_001")
        obj = render_jsonl_object(event)
        assert obj["key"] == {"presentation_id": "P_001"}

    def test_key_map_renders_elected_record_index_surface(self) -> None:
        """A record_index-elected event's key map is {"record_index": "<digits>"}."""
        event = _make_event(key_column="record_index", key_value="7")
        obj = render_jsonl_object(event)
        assert obj["key"] == {"record_index": "7"}

    def test_key_map_single_entry_regardless_of_surface(self) -> None:
        """The key map always carries exactly one entry — the elected surface."""
        event = _make_event(key_column="presentation_id", key_value="P_002")
        obj = render_jsonl_object(event)
        assert len(obj["key"]) == 1

    def test_published_non_elected_surface_rides_after_never_key(self) -> None:
        """A published non-elected surface (presentation_id, here) rides the
        after-image alongside the elected record_id, but the key map still
        carries the elected surface alone."""
        after = {"record_id": "r1", "presentation_id": "P_003", "status": "active"}
        event = _make_event(
            record_id="r1", key_column="record_id", key_value="r1", after=after
        )
        obj = render_jsonl_object(event)
        assert obj["key"] == {"record_id": "r1"}
        assert obj["after"] == after

    def test_after_is_row_map_on_create(self) -> None:
        """after carries the full row map on a 'c' event."""
        after = {"record_id": "r1", "name": "Alice"}
        event = _make_event(op="c", after=after)
        obj = render_jsonl_object(event)
        assert obj["after"] == after

    def test_after_is_row_map_on_update(self) -> None:
        """after carries the full row map on a 'u' event."""
        after = {"record_id": "r1", "name": "Bob"}
        event = _make_event(op="u", after=after)
        obj = render_jsonl_object(event)
        assert obj["after"] == after

    def test_after_is_none_on_delete(self) -> None:
        """after is None on a 'd' event."""
        event = _make_event(op="d", after=None)
        obj = render_jsonl_object(event)
        assert obj["after"] is None

    def test_seq_value(self) -> None:
        """seq in the rendered object matches the event seq."""
        event = _make_event(seq=7)
        obj = render_jsonl_object(event)
        assert obj["seq"] == 7

    def test_ts_value_string(self) -> None:
        """ts is passed through as-is when it is a string."""
        event = _make_event(ts="2026-06-21T12:00:00+02:00")
        obj = render_jsonl_object(event)
        assert obj["ts"] == "2026-06-21T12:00:00+02:00"

    def test_ts_value_int(self) -> None:
        """ts is passed through as-is when it is a raw int."""
        event = _make_event(ts=86_400_000_000_000)
        obj = render_jsonl_object(event)
        assert obj["ts"] == 86_400_000_000_000


# ---------------------------------------------------------------------------
# render_jsonl_object — the 'r' snapshot-read op
# ---------------------------------------------------------------------------


class TestRenderJsonlObjectSnapshot:
    """The 'r' op renders like 'c'/'u': the standard object shape with the
    full after-image, seq carrying the shared snapshot position N."""

    def test_op_is_r(self) -> None:
        event = _make_event(op="r")
        obj = render_jsonl_object(event)
        assert obj["op"] == "r"

    def test_after_is_full_image(self) -> None:
        after = {"record_id": "r1", "status": "active"}
        event = _make_event(op="r", after=after)
        obj = render_jsonl_object(event)
        assert obj["after"] == after

    def test_seq_is_the_shared_snapshot_position(self) -> None:
        event = _make_event(op="r", seq=3, event_sim_time=100)
        obj = render_jsonl_object(event)
        assert obj["seq"] == 3

    def test_key_order_unchanged(self) -> None:
        event = _make_event(op="r")
        obj = render_jsonl_object(event)
        assert list(obj.keys()) == ["seq", "op", "ts", "kind", "key", "after"]
