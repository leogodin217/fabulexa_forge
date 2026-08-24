"""Tests for the streaming attach (design doc § Streaming attach): a
declared stream's own `render:` map (`decimal` / `json_precision` only),
validated against the stream's own projection and sidecar column types, and
applied at the codec seam upstream of after-image assembly.

Every scenario runs the full `iter_stream_events` engine over a minimal
in-process emit — the eager validation pass and the after-image render both
live inside the engine, so there is no shallower seam to test against (the
same posture the phase-6 demo,
`docs/sprints/value-rendering-elections/demos/phase_6_streaming_render.py`,
exercises end to end)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column, prop_column
from _support.sidecar_builder import write_emit as _write_sidecar

from fabulexa_forge.config.models import (
    DecimalElection,
    JsonPrecisionElection,
    KindStream,
    MembershipStream,
    StreamConfig,
)
from fabulexa_forge.errors import (
    DecimalSourceIsDouble,
    JsonPrecisionSourceIsVarchar,
    RenderKeyResolves,
)
from fabulexa_forge.exporters.streaming.debezium import build_debezium_value_schema
from fabulexa_forge.exporters.streaming.engine import iter_stream_events
from fabulexa_forge.exporters.streaming.types import StreamEvent
from fabulexa_forge.reader.emit import open_emit

from ._helpers import _ddl, _membership_table_spec

_MS = 1_000_000  # one sim-time "tick", in nanoseconds

# ---------------------------------------------------------------------------
# Kind-shaped fixture: one `widget` kind, a tracked DOUBLE and a constant
# VARCHAR (JSON-payload) property.
# ---------------------------------------------------------------------------

_KIND_RENDER_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__volume", "DOUBLE", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__context", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _build_kind_render_emit(tmp_path: Path) -> Path:
    """One `widget` kind, two records: `w1` is created then updated (a
    `c`/`u` pair over a changing `prop__volume`); `w2` is created then
    deactivated (a `d` tombstone). `prop__context` is constant, so it never
    changes value across `w1`'s events."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__widget", _KIND_RENDER_COLS))
    conn.executemany(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            ("trunk", "w1", 0, True, None, 5 * _MS, 0, 8.2564, '{"pct": 0.1234}'),
            (
                "trunk",
                "w2",
                0,
                False,
                10 * _MS,
                10 * _MS,
                1,
                3.5001,
                '{"pct": 0.6789}',
            ),
        ],
    )
    conn.execute(_ddl("history", _HISTORY_COLS))
    conn.executemany(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        [
            ("trunk", "widget", "w1", "volume", 0, "5.1111"),
            ("trunk", "widget", "w1", "volume", 5 * _MS, "8.2564"),
            ("trunk", "widget", "w2", "volume", 0, "3.5001"),
        ],
    )
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            {
                "name": "records__widget",
                "category": "records",
                "columns": _KIND_RENDER_COLS,
                "rows": 2,
                "record_kind": "widget",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLS,
                "rows": 3,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100 * _MS}],
    )
    return tmp_path


def _kind_stream_render(
    name: str,
    properties: list[str],
    render: dict[str, object] | None = None,
) -> KindStream:
    """Build one `widget`-kind KindStream declaration."""
    return KindStream(name=name, kind="widget", properties=properties, render=render)


def _state_changes_config(streams: list[KindStream]) -> StreamConfig:
    return StreamConfig(content="state-changes", streams=streams)


# ---------------------------------------------------------------------------
# Membership-shaped fixture: one `queue.waiters` membership, a DOUBLE scalar
# field, a VARCHAR (JSON-payload) scalar field, and a reference field.
# ---------------------------------------------------------------------------

_MEMBERSHIP_RENDER_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__amount", "type": "DOUBLE"},
    {"name": "elem__payload", "type": "VARCHAR"},
    {"name": "member__owner__kind", "type": "VARCHAR"},
    {"name": "member__owner__id", "type": "VARCHAR"},
]

_OWNER_RECORD_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]


def _build_membership_render_emit(tmp_path: Path) -> Path:
    """One `membership__queue__waiters` table, one closed-interval member
    (a `join`/`leave` pair), and `queue`'s minimal owner records shell."""
    table_name = "membership__queue__waiters"
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl(table_name, _MEMBERSHIP_RENDER_COLS))
    conn.execute(
        f'INSERT INTO "{table_name}" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "m1",
            100,
            300,
            8.2564,
            '{"pct": 0.4321}',
            "owner_kind",
            "o1",
        ],
    )
    conn.execute(_ddl("records__queue", _OWNER_RECORD_COLS))
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            _membership_table_spec(
                table_name, _MEMBERSHIP_RENDER_COLS, 1, "queue", "waiters"
            ),
            {
                "name": "records__queue",
                "category": "records",
                "columns": _OWNER_RECORD_COLS,
                "rows": 0,
                "record_kind": "queue",
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )
    return tmp_path


def _membership_stream_render(
    name: str,
    fields: list[str],
    render: dict[str, object] | None = None,
) -> MembershipStream:
    """Build one `queue.waiters` MembershipStream declaration."""
    return MembershipStream(
        name=name,
        membership={"kind": "queue", "property": "waiters"},
        fields=fields,
        render=render,
    )


def _membership_events_config(streams: list[MembershipStream]) -> StreamConfig:
    return StreamConfig(content="membership-events", streams=streams)


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------


def _events_by_op(events: list[StreamEvent], record_id: str) -> dict[str, StreamEvent]:
    """Index a record's events by op (one event per op, these fixtures)."""
    return {e.op: e for e in events if e.record_id == record_id}


def _scoped_dir(tmp_path: Path, label: str) -> Path:
    """A fresh subdirectory of `tmp_path` for one emit build.

    A scenario comparing an elected run against a silent control run builds
    two emits under the same `tmp_path` fixture; each needs its own
    directory (a bare `run.duckdb` collides on the second build).
    """
    scoped = tmp_path / label
    scoped.mkdir()
    return scoped


def _non_after_fields(event: StreamEvent) -> tuple[Any, ...]:
    """Every StreamEvent field except `after` — the routing/ordering
    identity a render election must leave untouched."""
    return (
        event.seq,
        event.op,
        event.kind,
        event.record_id,
        event.presentation_id,
        event.event_sim_time,
        event.ts,
        event.topic,
        event.route_table,
        event.key_column,
        event.key_value,
    )


# ---------------------------------------------------------------------------
# KindStream render validation — RenderKeyResolves / source-type gates
# ---------------------------------------------------------------------------


class TestKindStreamRenderValidation:
    def test_render_key_not_in_properties_raises(self, tmp_path: Path) -> None:
        """A render key naming no declared property raises RenderKeyResolves."""
        emit_dir = _build_kind_render_emit(tmp_path)
        config = _state_changes_config(
            [
                _kind_stream_render(
                    "widgets",
                    ["volume"],
                    render={"other": DecimalElection(decimal=(6, 3))},
                )
            ]
        )
        with open_emit(emit_dir) as emit, pytest.raises(RenderKeyResolves):
            iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)

    def test_decimal_on_non_double_property_raises(self, tmp_path: Path) -> None:
        """`decimal` elected on a VARCHAR property raises DecimalSourceIsDouble."""
        emit_dir = _build_kind_render_emit(tmp_path)
        config = _state_changes_config(
            [
                _kind_stream_render(
                    "widgets",
                    ["context"],
                    render={"context": DecimalElection(decimal=(6, 3))},
                )
            ]
        )
        with open_emit(emit_dir) as emit, pytest.raises(DecimalSourceIsDouble):
            iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)

    def test_json_precision_on_non_varchar_property_raises(
        self, tmp_path: Path
    ) -> None:
        """`json_precision` elected on a DOUBLE property raises
        JsonPrecisionSourceIsVarchar."""
        emit_dir = _build_kind_render_emit(tmp_path)
        config = _state_changes_config(
            [
                _kind_stream_render(
                    "widgets",
                    ["volume"],
                    render={"volume": JsonPrecisionElection(json_precision={"x": 2})},
                )
            ]
        )
        with open_emit(emit_dir) as emit, pytest.raises(JsonPrecisionSourceIsVarchar):
            iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)


# ---------------------------------------------------------------------------
# MembershipStream render validation — RenderKeyResolves / source-type gates
# ---------------------------------------------------------------------------


class TestMembershipStreamRenderValidation:
    def test_render_key_not_in_fields_raises(self, tmp_path: Path) -> None:
        """A render key naming no declared field raises RenderKeyResolves."""
        emit_dir = _build_membership_render_emit(tmp_path)
        config = _membership_events_config(
            [
                _membership_stream_render(
                    "waiters",
                    ["amount"],
                    render={"other": DecimalElection(decimal=(6, 3))},
                )
            ]
        )
        with open_emit(emit_dir) as emit, pytest.raises(RenderKeyResolves):
            iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)

    def test_decimal_on_non_double_field_raises(self, tmp_path: Path) -> None:
        """`decimal` elected on a VARCHAR field raises DecimalSourceIsDouble."""
        emit_dir = _build_membership_render_emit(tmp_path)
        config = _membership_events_config(
            [
                _membership_stream_render(
                    "waiters",
                    ["payload"],
                    render={"payload": DecimalElection(decimal=(6, 3))},
                )
            ]
        )
        with open_emit(emit_dir) as emit, pytest.raises(DecimalSourceIsDouble):
            iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)

    def test_json_precision_on_non_varchar_field_raises(self, tmp_path: Path) -> None:
        """`json_precision` elected on a DOUBLE field raises
        JsonPrecisionSourceIsVarchar."""
        emit_dir = _build_membership_render_emit(tmp_path)
        config = _membership_events_config(
            [
                _membership_stream_render(
                    "waiters",
                    ["amount"],
                    render={"amount": JsonPrecisionElection(json_precision={"x": 2})},
                )
            ]
        )
        with open_emit(emit_dir) as emit, pytest.raises(JsonPrecisionSourceIsVarchar):
            iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)

    def test_render_key_naming_reference_field_raises(self, tmp_path: Path) -> None:
        """A render key naming a reference field (member__<key>__kind) is
        outside the typed-election domain — reference identity is key
        election's surface."""
        emit_dir = _build_membership_render_emit(tmp_path)
        config = _membership_events_config(
            [
                _membership_stream_render(
                    "waiters",
                    ["owner"],
                    render={"owner": DecimalElection(decimal=(6, 3))},
                )
            ]
        )
        with (
            open_emit(emit_dir) as emit,
            pytest.raises(RenderKeyResolves, match="reference field"),
        ):
            iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)


# ---------------------------------------------------------------------------
# KindStream render events — elected after-image text, unaffected surfaces
# ---------------------------------------------------------------------------


class TestKindStreamRenderEvents:
    def _elect(self, tmp_path: Path) -> list[StreamEvent]:
        emit_dir = _build_kind_render_emit(_scoped_dir(tmp_path, "elect"))
        config = _state_changes_config(
            [
                _kind_stream_render(
                    "widgets",
                    ["volume", "context"],
                    render={
                        "volume": DecimalElection(decimal=(6, 3)),
                        "context": JsonPrecisionElection(json_precision={"pct": 2}),
                    },
                )
            ]
        )
        with open_emit(emit_dir) as emit:
            return list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

    def _silent(self, tmp_path: Path) -> list[StreamEvent]:
        emit_dir = _build_kind_render_emit(_scoped_dir(tmp_path, "silent"))
        config = _state_changes_config(
            [_kind_stream_render("widgets", ["volume", "context"])]
        )
        with open_emit(emit_dir) as emit:
            return list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

    def test_create_after_image_carries_elected_text(self, tmp_path: Path) -> None:
        """The 'c' after-image's elected columns carry the rendered text —
        byte-identical to the table modes' decimal/json_precision render of
        the same source values."""
        events = self._elect(tmp_path)
        c = _events_by_op(events, "w1")["c"]
        assert c.after is not None
        assert c.after["volume"] == "5.111"
        assert c.after["context"] == '{"pct": 0.12}'

    def test_update_after_image_carries_elected_text(self, tmp_path: Path) -> None:
        """The 'u' after-image's elected columns carry the rendered text too."""
        events = self._elect(tmp_path)
        u = _events_by_op(events, "w1")["u"]
        assert u.after is not None
        assert u.after["volume"] == "8.256"
        assert u.after["context"] == '{"pct": 0.12}'

    def test_delete_tombstone_unaffected(self, tmp_path: Path) -> None:
        """A 'd' tombstone carries no after-image to elect — unaffected."""
        events = self._elect(tmp_path)
        d = _events_by_op(events, "w2")["d"]
        assert d.after is None

    def test_debezium_value_schema_unaffected(self) -> None:
        """The Debezium value schema stays string-typed — the election
        changes value text only, never the codec type."""
        schema = build_debezium_value_schema(
            table="widget",
            columns=["record_id", "prop__volume", "prop__context"],
            source_name="fabulexa",
            connector="postgresql",
        )
        after_struct = next(f for f in schema["fields"] if f["field"] == "after")
        value_types = {f["field"]: f["type"] for f in after_struct["fields"]}
        assert set(value_types.values()) == {"string"}

    def test_non_after_fields_unchanged_with_elections_on(self, tmp_path: Path) -> None:
        """Message key, merge order, seq, and ts are unchanged whether or not
        elections apply — only after-image text differs."""
        elected = self._elect(tmp_path)
        silent = self._silent(tmp_path)
        assert [_non_after_fields(e) for e in elected] == [
            _non_after_fields(e) for e in silent
        ]

    def test_no_election_stream_after_image_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """A stream declaring no render map renders after-image text exactly
        as it does today — unrounded, unrendered."""
        events = self._silent(tmp_path)
        c = _events_by_op(events, "w1")["c"]
        assert c.after is not None
        assert c.after["volume"] == "5.1111"
        assert c.after["context"] == '{"pct": 0.1234}'


# ---------------------------------------------------------------------------
# MembershipStream render events — elected after-image text
# ---------------------------------------------------------------------------


class TestMembershipStreamRenderEvents:
    def _elect(self, tmp_path: Path) -> list[StreamEvent]:
        emit_dir = _build_membership_render_emit(_scoped_dir(tmp_path, "elect"))
        config = _membership_events_config(
            [
                _membership_stream_render(
                    "waiters",
                    ["amount", "payload"],
                    render={
                        "amount": DecimalElection(decimal=(6, 3)),
                        "payload": JsonPrecisionElection(json_precision={"pct": 2}),
                    },
                )
            ]
        )
        with open_emit(emit_dir) as emit:
            return list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

    def _silent(self, tmp_path: Path) -> list[StreamEvent]:
        emit_dir = _build_membership_render_emit(_scoped_dir(tmp_path, "silent"))
        config = _membership_events_config(
            [_membership_stream_render("waiters", ["amount", "payload"])]
        )
        with open_emit(emit_dir) as emit:
            return list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

    def test_join_and_leave_carry_elected_text(self, tmp_path: Path) -> None:
        """Both the 'join' and 'leave' after-images carry the rendered text —
        byte-identical to the table modes' render of the same source values."""
        events = self._elect(tmp_path)
        by_op = _events_by_op(events, "m1")
        for op in ("join", "leave"):
            after = by_op[op].after
            assert after is not None
            assert after["amount"] == "8.256"
            assert after["payload"] == '{"pct": 0.43}'

    def test_non_after_fields_unchanged_with_elections_on(self, tmp_path: Path) -> None:
        """Message key, merge order, seq, and ts are unchanged with the
        election on."""
        elected = self._elect(tmp_path)
        silent = self._silent(tmp_path)
        assert [_non_after_fields(e) for e in elected] == [
            _non_after_fields(e) for e in silent
        ]

    def test_no_election_stream_after_image_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """A stream declaring no render map renders after-image text exactly
        as it does today."""
        events = self._silent(tmp_path)
        after = _events_by_op(events, "m1")["join"].after
        assert after is not None
        assert after["amount"] == "8.2564"
        assert after["payload"] == '{"pct": 0.4321}'
