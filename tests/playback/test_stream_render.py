"""Tests for playback/stream_render.py: resolve_stream_render, StreamRender.

Materialized against the shared `_scenario.build_full_scenario` emit (two
kind-shaped streams over patient/widget, two membership-shaped streams over
team/tags). Covers byte parity against the still-shipped
`stream_export` + `write_jsonl_stream` / `write_debezium_stream` path (the
oracle for every op the shipped formats emit today — 'r' is new this sprint
and has no shipped oracle, so it is exercised in `test_jsonl.py` /
`test_debezium.py` instead), key-bytes and timestamp parity,
`value_schema_for`'s table-identity cases including the two declared
schema-identity fixes, the resolve-time gates (self-vetting, anchor/config
requirements), and render purity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink
from _support.sidecar_builder import enum_options, identity_column, prop_column

from fabulexa_forge.config.models import (
    DebeziumConfig,
    DebeziumSourceIdentity,
    StreamConfig,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.streaming.debezium import rebased_epoch_ms
from fabulexa_forge.exporters.streaming.driver import stream_export
from fabulexa_forge.exporters.streaming.encoding import encode_pinned
from fabulexa_forge.exporters.streaming.types import StreamEvent
from fabulexa_forge.playback.stream import open_stream_playback
from fabulexa_forge.playback.stream_render import resolve_stream_render
from fabulexa_forge.reader.emit import open_emit

from ._data_fixtures import RecordSpec, build_data_emit
from ._scenario import build_full_scenario, make_anchor
from ._stream_config import kind_stream, membership_stream

if TYPE_CHECKING:
    from pathlib import Path

    from fabulexa_forge.anchor import EffectiveAnchor

# ---------------------------------------------------------------------------
# Config / scenario helpers
# ---------------------------------------------------------------------------


def _debezium_source() -> DebeziumSourceIdentity:
    """A minimal masquerade source identity for debezium-format tests."""
    return DebeziumSourceIdentity(
        connector="postgresql",
        name="fabulexa",
        db="fabulexa",
        **{"schema": "public"},
        version="2.5.0.Final",
    )


def _debezium_config(
    table_identity: Literal["source_table", "topic"] = "source_table",
    schemas_enable: bool = True,
) -> DebeziumConfig:
    """A DebeziumConfig block over `_debezium_source`."""
    return DebeziumConfig(
        source=_debezium_source(),
        table_identity=table_identity,
        schemas_enable=schemas_enable,
    )


def _state_config() -> StreamConfig:
    """Two kind-shaped streams (patient, widget) with a debezium block —
    ignored under fmt='jsonl', read under fmt='debezium'."""
    return StreamConfig(
        content="state-changes",
        streams=[
            kind_stream("patients", "patient", ["status"]),
            kind_stream("widgets", "widget", ["count"]),
        ],
        debezium=_debezium_config(),
    )


def _membership_config() -> StreamConfig:
    """Two membership-shaped streams (team, tags) with a debezium block."""
    return StreamConfig(
        content="membership-events",
        streams=[
            membership_stream("team_events", "patient", "team", []),
            membership_stream("tag_events", "widget", "tags", []),
        ],
        debezium=_debezium_config(),
    )


def _enum_where_scenario(tmp_path: "Path") -> "Path":
    """One emit, kind 'item', prop__status carries an enum_domains entry
    ('open', 'closed') so a `where` value outside the domain triggers the
    eager pass's out-of-domain notice against its selection-resolution
    spine read."""
    cols = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        prop_column(
            "prop__status", "VARCHAR", history_tracked=False, temporal_class="constant"
        ),
    ]
    return build_data_emit(
        tmp_path,
        records=[
            RecordSpec("item", cols, [("trunk", "i1", 10, True, None, 10, 0, "open")])
        ],
        extra={"enum_domains": {"item": {"status": enum_options("open", "closed")}}},
    )


def _lines_by_topic(
    out_dir: "Path", topics: "tuple[str, ...]"
) -> dict[str, list[bytes]]:
    """Read each topic's file, split into per-line bytes without the
    trailing newline."""
    result: dict[str, list[bytes]] = {}
    for topic in topics:
        raw = (out_dir / f"{topic}.jsonl").read_bytes()
        lines = raw.split(b"\n")
        if lines and lines[-1] == b"":
            lines = lines[:-1]
        result[topic] = lines
    return result


def _events_by_topic(
    events: "list[StreamEvent]",
) -> dict[str, list["StreamEvent"]]:
    """Group events by topic, preserving seq order within each topic."""
    grouped: dict[str, list[StreamEvent]] = {}
    for event in events:
        grouped.setdefault(event.topic, []).append(event)
    return grouped


# ---------------------------------------------------------------------------
# Byte parity — render_bytes vs. the shipped driver/format path
# ---------------------------------------------------------------------------


class TestByteParity:
    """render_bytes equals the shipped driver/format path's per-line bytes
    for every op the shipped formats emit today (c/u/d/join/leave)."""

    def test_state_changes_jsonl(self, tmp_path: "Path") -> None:
        self._assert_parity(tmp_path, _state_config(), "jsonl", None)

    def test_state_changes_debezium(self, tmp_path: "Path") -> None:
        self._assert_parity(tmp_path, _state_config(), "debezium", make_anchor())

    def test_membership_events_jsonl(self, tmp_path: "Path") -> None:
        self._assert_parity(tmp_path, _membership_config(), "jsonl", None)

    def test_membership_events_debezium(self, tmp_path: "Path") -> None:
        self._assert_parity(tmp_path, _membership_config(), "debezium", make_anchor())

    @staticmethod
    def _assert_parity(
        tmp_path: "Path",
        config: StreamConfig,
        fmt: Literal["jsonl", "debezium"],
        anchor: "EffectiveAnchor | None",
    ) -> None:
        emit_dir = build_full_scenario(tmp_path)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt, "file", out_dir, anchor, discard_notice_sink
            )
        with open_emit(emit_dir) as emit:
            render = resolve_stream_render(
                emit, config, fmt, anchor, discard_notice_sink
            )
            head = open_stream_playback(emit, config, anchor, discard_notice_sink)
            events = list(head.events(None, None))
        lines = _lines_by_topic(out_dir, tuple(outcome.events_per_topic))
        grouped = _events_by_topic(events)
        assert set(grouped) <= set(lines)
        for topic, topic_events in grouped.items():
            topic_lines = lines[topic]
            assert len(topic_events) == len(topic_lines)
            for event, line in zip(topic_events, topic_lines):
                assert render.render_bytes(event) == line


# ---------------------------------------------------------------------------
# render_key_bytes / timestamp_ms parity
# ---------------------------------------------------------------------------


class TestRenderKeyBytesParity:
    def test_key_bytes_equal_pinned_key_map(self, tmp_path: "Path") -> None:
        emit_dir = build_full_scenario(tmp_path)
        config = _state_config()
        anchor = make_anchor()
        with open_emit(emit_dir) as emit:
            render = resolve_stream_render(
                emit, config, "debezium", anchor, discard_notice_sink
            )
            head = open_stream_playback(emit, config, anchor, discard_notice_sink)
            events = list(head.events(None, None))
        assert events
        for event in events:
            expected = encode_pinned({event.key_column: event.key_value}).encode(
                "utf-8"
            )
            assert render.render_key_bytes(event) == expected


class TestTimestampMs:
    def test_equals_rebased_epoch_ms(self, tmp_path: "Path") -> None:
        emit_dir = build_full_scenario(tmp_path)
        config = _state_config()
        anchor = make_anchor()
        with open_emit(emit_dir) as emit:
            render = resolve_stream_render(
                emit, config, "debezium", anchor, discard_notice_sink
            )
            head = open_stream_playback(emit, config, anchor, discard_notice_sink)
            events = list(head.events(None, None))
        assert events
        for event in events:
            assert render.timestamp_ms(event) == rebased_epoch_ms(
                event.event_sim_time, anchor
            )

    def test_anchorless_render_raises_export_error(self, tmp_path: "Path") -> None:
        emit_dir = build_full_scenario(tmp_path)
        config = _state_config()
        with open_emit(emit_dir) as emit:
            render = resolve_stream_render(
                emit, config, "jsonl", None, discard_notice_sink
            )
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            event = next(iter(head.events(None, None)))
            with pytest.raises(ExportError, match="anchor"):
                render.timestamp_ms(event)

    def test_jsonl_resolves_with_or_without_anchor(self, tmp_path: "Path") -> None:
        emit_dir = build_full_scenario(tmp_path)
        config = _state_config()
        with open_emit(emit_dir) as emit:
            resolve_stream_render(emit, config, "jsonl", None, discard_notice_sink)
            resolve_stream_render(
                emit, config, "jsonl", make_anchor(), discard_notice_sink
            )


# ---------------------------------------------------------------------------
# value_schema_for — table-identity cases and the two declared fixes
# ---------------------------------------------------------------------------


class TestValueSchemaFor:
    def test_source_table_identity_uses_route_table_leaf(
        self, tmp_path: "Path"
    ) -> None:
        emit_dir = build_full_scenario(tmp_path)
        config = _state_config()
        anchor = make_anchor()
        with open_emit(emit_dir) as emit:
            render = resolve_stream_render(
                emit, config, "debezium", anchor, discard_notice_sink
            )
            head = open_stream_playback(emit, config, anchor, discard_notice_sink)
            events = list(head.events(None, None))
        assert events
        for event in events:
            schema = render.value_schema_for(event)
            assert schema is not None
            assert schema["name"] == f"fabulexa.{event.route_table}.Envelope"

    def test_topic_identity_degenerate_pair(self, tmp_path: "Path") -> None:
        emit_dir = build_full_scenario(tmp_path)
        config = StreamConfig(
            content="state-changes",
            streams=[kind_stream("patients", "patient", ["status"])],
            debezium=_debezium_config(table_identity="topic"),
        )
        anchor = make_anchor()
        with open_emit(emit_dir) as emit:
            render = resolve_stream_render(
                emit, config, "debezium", anchor, discard_notice_sink
            )
            head = open_stream_playback(emit, config, anchor, discard_notice_sink)
            events = list(head.events(None, None))
        assert events
        for event in events:
            schema = render.value_schema_for(event)
            assert schema is not None
            assert schema["name"] == f"fabulexa.{event.topic}.Envelope"

    def test_overlapping_streams_sharing_leaf_embed_distinct_schemas_fix1(
        self, tmp_path: "Path"
    ) -> None:
        """Two streams over the flat 'widget' kind share the route_table
        leaf 'widget' under source_table identity; each embeds its own
        stream's schema rather than the first-declared stream's."""
        emit_dir = build_full_scenario(tmp_path)
        config = StreamConfig(
            content="state-changes",
            streams=[
                kind_stream("by_label", "widget", ["label"]),
                kind_stream("by_count", "widget", ["count"]),
            ],
            debezium=_debezium_config(),
        )
        anchor = make_anchor()
        with open_emit(emit_dir) as emit:
            render = resolve_stream_render(
                emit, config, "debezium", anchor, discard_notice_sink
            )
            head = open_stream_playback(emit, config, anchor, discard_notice_sink)
            events = [e for e in head.events(None, None) if e.op == "c"]
        by_topic = {e.topic: e for e in events}
        assert set(by_topic) == {"by_label", "by_count"}
        assert all(e.route_table == "widget" for e in by_topic.values())

        schema_label = render.value_schema_for(by_topic["by_label"])
        schema_count = render.value_schema_for(by_topic["by_count"])
        assert schema_label is not None
        assert schema_count is not None
        assert schema_label != schema_count

        fields_label = {f["field"] for f in schema_label["fields"][1]["fields"]}
        fields_count = {f["field"] for f in schema_count["fields"][1]["fields"]}
        assert "label" in fields_label and "label" not in fields_count
        assert "count" in fields_count and "count" not in fields_label

    def test_corrupted_out_of_domain_leaf_gets_per_event_schema_fix2(
        self, tmp_path: "Path"
    ) -> None:
        """A route_table outside the schema map's declared domain (a
        corrupted discriminator) still gets a schema, built from the
        event's own carried fields, table verbatim."""
        emit_dir = build_full_scenario(tmp_path)
        config = StreamConfig(
            content="state-changes",
            streams=[kind_stream("patients", "patient", ["status"])],
            debezium=_debezium_config(),
        )
        anchor = make_anchor()
        with open_emit(emit_dir) as emit:
            render = resolve_stream_render(
                emit, config, "debezium", anchor, discard_notice_sink
            )
        corrupted = StreamEvent(
            seq=1,
            op="c",
            kind="patient",
            record_id="p9",
            event_sim_time=0,
            ts=0,
            after={"record_id": "p9", "status": "ghost"},
            topic="patients",
            route_table="ghost_leaf",
            key_column="record_id",
            key_value="p9",
        )
        schema = render.value_schema_for(corrupted)
        assert schema is not None
        assert schema["name"] == "fabulexa.ghost_leaf.Envelope"
        fields = {f["field"] for f in schema["fields"][1]["fields"]}
        assert fields == {"record_id", "status"}

    def test_none_for_jsonl(self, tmp_path: "Path") -> None:
        emit_dir = build_full_scenario(tmp_path)
        config = _state_config()
        with open_emit(emit_dir) as emit:
            render = resolve_stream_render(
                emit, config, "jsonl", None, discard_notice_sink
            )
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            event = next(iter(head.events(None, None)))
        assert render.value_schema_for(event) is None

    def test_none_for_schemas_disabled(self, tmp_path: "Path") -> None:
        emit_dir = build_full_scenario(tmp_path)
        config = StreamConfig(
            content="state-changes",
            streams=[kind_stream("patients", "patient", ["status"])],
            debezium=_debezium_config(schemas_enable=False),
        )
        anchor = make_anchor()
        with open_emit(emit_dir) as emit:
            render = resolve_stream_render(
                emit, config, "debezium", anchor, discard_notice_sink
            )
            head = open_stream_playback(emit, config, anchor, discard_notice_sink)
            event = next(iter(head.events(None, None)))
        assert render.value_schema_for(event) is None


# ---------------------------------------------------------------------------
# Resolve-time gates
# ---------------------------------------------------------------------------


class TestResolveTimeGates:
    def test_debezium_without_anchor_refused(self, tmp_path: "Path") -> None:
        emit_dir = build_full_scenario(tmp_path)
        config = StreamConfig(
            content="state-changes",
            streams=[kind_stream("patients", "patient", [])],
            debezium=_debezium_config(),
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="resolved effective anchor"):
                resolve_stream_render(
                    emit, config, "debezium", None, discard_notice_sink
                )

    def test_debezium_without_config_block_refused(self, tmp_path: "Path") -> None:
        emit_dir = build_full_scenario(tmp_path)
        config = StreamConfig(
            content="state-changes",
            streams=[kind_stream("patients", "patient", [])],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="debezium"):
                resolve_stream_render(
                    emit, config, "debezium", make_anchor(), discard_notice_sink
                )

    def test_eager_pass_gate_identity_raised_at_resolve(self, tmp_path: "Path") -> None:
        emit_dir = build_full_scenario(tmp_path)
        config = StreamConfig(
            content="state-changes",
            streams=[kind_stream("ghosts", "ghost", [])],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="records__ghost"):
                resolve_stream_render(emit, config, "jsonl", None, discard_notice_sink)

    def test_notices_emit_to_supplied_sink_self_vetting_no_head_open(
        self, tmp_path: "Path"
    ) -> None:
        emit_dir = _enum_where_scenario(tmp_path)
        config = StreamConfig(
            content="state-changes",
            streams=[kind_stream("items", "item", [], where={"status": "archived"})],
        )
        sink = RecordingNoticeSink()
        with open_emit(emit_dir) as emit:
            resolve_stream_render(emit, config, "jsonl", None, sink)
        assert len(sink.notices) == 1
        assert sink.notices[0].code == "discriminator-value-unobserved"


# ---------------------------------------------------------------------------
# Render purity
# ---------------------------------------------------------------------------


class TestRenderPurity:
    def test_two_renders_of_one_event_are_equal(self, tmp_path: "Path") -> None:
        emit_dir = build_full_scenario(tmp_path)
        config = _state_config()
        anchor = make_anchor()
        with open_emit(emit_dir) as emit:
            render = resolve_stream_render(
                emit, config, "debezium", anchor, discard_notice_sink
            )
            head = open_stream_playback(emit, config, anchor, discard_notice_sink)
            event = next(iter(head.events(None, None)))
            first = render.render_bytes(event)
            second = render.render_bytes(event)
        assert first == second

    def test_renders_resolved_twice_agree(self, tmp_path: "Path") -> None:
        emit_dir = build_full_scenario(tmp_path)
        config = _state_config()
        anchor = make_anchor()
        with open_emit(emit_dir) as emit:
            render_a = resolve_stream_render(
                emit, config, "debezium", anchor, discard_notice_sink
            )
            render_b = resolve_stream_render(
                emit, config, "debezium", anchor, discard_notice_sink
            )
            head = open_stream_playback(emit, config, anchor, discard_notice_sink)
            event = next(iter(head.events(None, None)))
        assert render_a.render_bytes(event) == render_b.render_bytes(event)
