"""Tests for the streaming engine: iter_stream_events, StreamEvent, build_topic_set.

Materialized against minimal in-process emits built via the reader. Covers
the declared-stream grammar (KindStream / MembershipStream / StreamConfig):
per-stream folds, payload-independent event sets, combined-stream after-image
NULLs, stream-name merge/interleave, overlapping-stream multiplicity, business
rules (each message leading with the stream name), and Layer-A-only routing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column
from _support.sidecar_builder import write_emit as _write_sidecar

from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import KindStream, MembershipStream, StreamConfig
from fabulexa_forge.derivations.membership_events import resolve_membership_columns
from fabulexa_forge.derivations.row_state_events import resolve_stream_columns
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.streaming.engine import (
    build_topic_set,
    iter_stream_events,
)
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import TemporalClassUnavailableError

from ._helpers import _ddl, _membership_table_spec

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

_RECORD_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__label",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_RECORD_COLS_WITH_PID: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]

_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


# ---------------------------------------------------------------------------
# Emit builder helpers
# ---------------------------------------------------------------------------


def _table_spec(
    name: str,
    category: str,
    cols: list[dict[str, object]],
    rows: int,
    record_kind: str | None = None,
) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": name,
        "category": category,
        "columns": cols,
        "rows": rows,
    }
    if record_kind is not None:
        spec["record_kind"] = record_kind
    return spec


def _build_single_kind_emit(
    tmp_path: Path,
    kind: str,
    record_rows: list[tuple[Any, ...]],
    history_rows: list[tuple[Any, ...]],
    record_cols: list[dict[str, object]] | None = None,
    n_branches: int = 1,
    extra: dict[str, object] | None = None,
) -> Path:
    """Build a minimal emit with one kind and optional multi-branch support."""
    if record_cols is None:
        record_cols = _RECORD_COLS

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl(f"records__{kind}", record_cols))
    conn.execute(_ddl("history", _HISTORY_COLS))

    placeholders = ", ".join("?" for _ in record_cols)
    for row in record_rows:
        conn.execute(
            f'INSERT INTO "records__{kind}" VALUES ({placeholders})', list(row)
        )
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    if n_branches == 1:
        branches: list[dict[str, object]] = [
            {"fork_path": "trunk", "parent": None, "slice_at": 9999}
        ]
    else:
        branches = [
            {"fork_path": "trunk", "parent": None, "slice_at": 9999},
            {"fork_path": "trunk@alt", "parent": "trunk", "slice_at": 100},
        ]

    _write_sidecar(
        tmp_path,
        tables=[
            _table_spec(
                f"records__{kind}",
                "records",
                record_cols,
                len(record_rows),
                record_kind=kind,
            ),
            _table_spec("history", "fixed", _HISTORY_COLS, len(history_rows)),
        ],
        branches=branches,
        extra=extra,
    )
    return tmp_path


def _build_two_kind_emit(
    tmp_path: Path,
    kind_a: str,
    kind_a_rows: list[tuple[Any, ...]],
    kind_b: str,
    kind_b_rows: list[tuple[Any, ...]],
    history_rows: list[tuple[Any, ...]],
    cols_a: list[dict[str, object]] | None = None,
    cols_b: list[dict[str, object]] | None = None,
) -> Path:
    """Build a minimal emit with two kinds."""
    if cols_a is None:
        cols_a = _RECORD_COLS
    if cols_b is None:
        cols_b = _RECORD_COLS

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl(f"records__{kind_a}", cols_a))
    conn.execute(_ddl(f"records__{kind_b}", cols_b))
    conn.execute(_ddl("history", _HISTORY_COLS))

    ph_a = ", ".join("?" for _ in cols_a)
    for row in kind_a_rows:
        conn.execute(f'INSERT INTO "records__{kind_a}" VALUES ({ph_a})', list(row))

    ph_b = ", ".join("?" for _ in cols_b)
    for row in kind_b_rows:
        conn.execute(f'INSERT INTO "records__{kind_b}" VALUES ({ph_b})', list(row))

    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            _table_spec(
                f"records__{kind_a}",
                "records",
                cols_a,
                len(kind_a_rows),
                record_kind=kind_a,
            ),
            _table_spec(
                f"records__{kind_b}",
                "records",
                cols_b,
                len(kind_b_rows),
                record_kind=kind_b,
            ),
            _table_spec("history", "fixed", _HISTORY_COLS, len(history_rows)),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Config builder helpers
# ---------------------------------------------------------------------------


def _kind_stream(
    name: str,
    kind: str,
    properties: list[str],
    sub_types: list[str] | None = None,
) -> KindStream:
    """Build one KindStream declaration."""
    return KindStream(name=name, kind=kind, properties=properties, sub_types=sub_types)


def _state_changes_config(streams: list[KindStream]) -> StreamConfig:
    """Build a content='state-changes' StreamConfig from KindStream declarations."""
    return StreamConfig(content="state-changes", streams=streams)


def _single_kind_config(kind: str, properties: list[str]) -> StreamConfig:
    """Build a single-stream state-changes config named after its kind."""
    return _state_changes_config([_kind_stream(kind, kind, properties)])


def _membership_stream(
    name: str,
    owner_kind: str,
    property_name: str,
    fields: list[str],
) -> MembershipStream:
    """Build one MembershipStream declaration."""
    return MembershipStream(
        name=name,
        membership={"kind": owner_kind, "property": property_name},
        fields=fields,
    )


def _membership_events_config(streams: list[MembershipStream]) -> StreamConfig:
    """Build a content='membership-events' StreamConfig from declarations."""
    return StreamConfig(content="membership-events", streams=streams)


# ---------------------------------------------------------------------------
# seq tests
# ---------------------------------------------------------------------------


class TestSeq:
    """seq is 1-based, monotonic, gap-free, spanning all streams."""

    def test_seq_is_one_based_monotonic_gap_free(self, tmp_path: Path) -> None:
        """seq starts at 1, increments by 1, never gaps."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[
                ("trunk", "r1", 10, True, None, 10, 0, "a", "x"),
                ("trunk", "r2", 20, True, None, 20, 1, "b", "y"),
            ],
            history_rows=[("trunk", "item", "r1", "status", 30, "c")],
        )
        config = _single_kind_config("item", ["status"])
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        seqs = [e.seq for e in events]
        assert seqs[0] == 1
        assert seqs == list(range(1, len(events) + 1))

    def test_seq_spans_all_streams_not_reset(self, tmp_path: Path) -> None:
        """seq does not reset between streams — it is a single global counter."""
        emit_dir = _build_two_kind_emit(
            tmp_path,
            "alpha",
            [("trunk", "a1", 10, True, None, 10, 0, "x", "p")],
            "beta",
            [("trunk", "b1", 20, True, None, 20, 0, "y", "q")],
            history_rows=[],
        )
        config = _state_changes_config(
            [_kind_stream("alpha", "alpha", []), _kind_stream("beta", "beta", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 2
        seqs = [e.seq for e in events]
        assert seqs == [1, 2]


# ---------------------------------------------------------------------------
# Ordering / stream-name interleave tests
# ---------------------------------------------------------------------------


class TestStreamNameInterleave:
    """Cross-stream ordering follows the canonical merge key (..., stream_name, ...)."""

    def test_cross_stream_interleave_by_sim_time(self, tmp_path: Path) -> None:
        """Events from different streams interleave by event_sim_time."""
        # alpha record at t=5, beta record at t=3 — beta should come first
        emit_dir = _build_two_kind_emit(
            tmp_path,
            "alpha",
            [("trunk", "a1", 5, True, None, 5, 0, "x", "p")],
            "beta",
            [("trunk", "b1", 3, True, None, 3, 0, "y", "q")],
            history_rows=[],
        )
        config = _state_changes_config(
            [_kind_stream("alpha", "alpha", []), _kind_stream("beta", "beta", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert [e.topic for e in events] == ["beta", "alpha"]

    def test_same_instant_same_class_interleaves_by_stream_name_not_kind(
        self, tmp_path: Path
    ) -> None:
        """The tiebreak is the declaring stream's name, not the underlying kind —
        naming a stream 'a_feed' for kind 'zeta' and 'z_feed' for kind 'alpha'
        reverses the kind-alphabetical order."""
        emit_dir = _build_two_kind_emit(
            tmp_path,
            "zeta",
            [("trunk", "z1", 10, True, None, 10, 0, "x", "p")],
            "alpha",
            [("trunk", "a1", 10, True, None, 10, 0, "y", "q")],
            history_rows=[],
        )
        config = _state_changes_config(
            [
                _kind_stream("a_feed", "zeta", []),
                _kind_stream("z_feed", "alpha", []),
            ]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        # stream-name order (a_feed < z_feed) wins, even though kind-alphabetical
        # order (alpha < zeta) would put the other event first.
        assert [e.topic for e in events] == ["a_feed", "z_feed"]
        assert [e.record_id for e in events] == ["z1", "a1"]

    def test_seq_global_1_based_across_streams(self, tmp_path: Path) -> None:
        """seq is a single 1-based counter across every declared stream."""
        emit_dir = _build_two_kind_emit(
            tmp_path,
            "alpha",
            [("trunk", "a1", 10, True, None, 10, 0, "x", "p")],
            "beta",
            [("trunk", "b1", 30, True, None, 30, 0, "y", "q")],
            history_rows=[],
        )
        config = _state_changes_config(
            [_kind_stream("alpha", "alpha", []), _kind_stream("beta", "beta", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert [e.seq for e in events] == [1, 2]


class TestOverlappingStreamMultiplicity:
    """Two streams covering the same population each emit their own event."""

    def test_overlapping_streams_emit_one_event_each_with_distinct_seq(
        self, tmp_path: Path
    ) -> None:
        """The same kind fed to an 'all' stream and a scoped stream both emit the
        record's create event — same record_id/event_sim_time/op, distinct seq
        and distinct topic (the declaring stream's name)."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "x")],
            history_rows=[],
        )
        config = _state_changes_config(
            [
                _kind_stream("all_items", "item", []),
                _kind_stream("status_items", "item", ["status"]),
            ]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 2
        assert {e.topic for e in events} == {"all_items", "status_items"}
        assert all(e.record_id == "r1" for e in events)
        assert all(e.op == "c" for e in events)
        assert all(e.event_sim_time == 10 for e in events)
        assert len({e.seq for e in events}) == 2


# ---------------------------------------------------------------------------
# Payload-independent event set tests
# ---------------------------------------------------------------------------


class TestPayloadIndependentEventSet:
    """Event membership is a fact of the population; projection never affects it."""

    def test_two_streams_different_properties_yield_identical_event_keys(
        self, tmp_path: Path
    ) -> None:
        """Two streams over the same kind with different `properties` yield
        identical (op, record_id, event_sim_time) sequences."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 20, 0, "a", "x")],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
            ],
        )
        config = _state_changes_config(
            [
                _kind_stream("full", "item", ["status", "label"]),
                _kind_stream("empty", "item", []),
            ]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        by_stream = {
            name: sorted(
                (e.op, e.record_id, e.event_sim_time) for e in events if e.topic == name
            )
            for name in ("full", "empty")
        }
        assert by_stream["full"] == by_stream["empty"]
        assert len(by_stream["full"]) == 2  # create + one update

    def test_properties_empty_yields_full_event_set_identity_only_after_image(
        self, tmp_path: Path
    ) -> None:
        """properties: [] yields the full event set (c/u/d), each after-image
        carrying only identity columns (record_id, presentation_id) — no prop__."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, False, 50, 50, 0, "a", "x")],
            history_rows=[("trunk", "item", "r1", "status", 30, "b")],
        )
        config = _single_kind_config("item", [])
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        ops = {e.op for e in events}
        assert ops == {"c", "u", "d"}
        for e in events:
            if e.after is not None:
                assert list(e.after.keys()) == ["record_id"]

    def test_subtyped_stream_update_set_spans_kind_tracked_columns(
        self, tmp_path: Path
    ) -> None:
        """A sub_types-scoped stream's `u` set spans every tracked column of the
        kind — not just the stream's own `properties` selection."""
        cols = [
            identity_column("fork_path", "VARCHAR"),
            identity_column("record_id", "VARCHAR"),
            {"name": "created_sim_time", "type": "BIGINT"},
            {"name": "active", "type": "BOOLEAN"},
            {"name": "deactivated_at", "type": "BIGINT"},
            {"name": "last_mutation_sim_time", "type": "BIGINT"},
            identity_column("record_index", "BIGINT"),
            {
                "name": "prop__actor_type",
                "type": "VARCHAR",
                "history_tracked": False,
                "temporal_class": "constant",
            },
            {
                "name": "prop__a",
                "type": "VARCHAR",
                "history_tracked": True,
                "temporal_class": "tracked",
            },
            {
                "name": "prop__b",
                "type": "VARCHAR",
                "history_tracked": True,
                "temporal_class": "tracked",
            },
        ]
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "actor",
            record_rows=[("trunk", "r1", 10, True, None, 20, 0, "car", "a0", "b0")],
            history_rows=[("trunk", "actor", "r1", "b", 20, "b1")],
            record_cols=cols,
            extra={"enum_domains": {"actor": {"actor_type": ["car", "truck"]}}},
        )
        # Stream selects only 'a' (never 'b') but scopes sub_types=['car'].
        config = _state_changes_config(
            [_kind_stream("cars", "actor", ["a"], sub_types=["car"])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        ops = [e.op for e in events]
        # The 'b' history change still produces a 'u' event even though 'b' is
        # never selected into the after-image.
        assert ops == ["c", "u"]
        update = events[1]
        assert update.after is not None
        assert "prop__b" not in update.after
        assert "prop__a" in update.after


# ---------------------------------------------------------------------------
# Combined-stream after-image NULL tests
# ---------------------------------------------------------------------------


class TestCombinedStreamNulls:
    """A combined stream over a kind's full domain uses one column list; a row
    whose sub-type does not declare a selected property carries NULL for it."""

    _COLS_VEHICLE: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        {
            "name": "prop__vehicle_type",
            "type": "VARCHAR",
            "history_tracked": False,
            "temporal_class": "constant",
        },
        {
            "name": "prop__car_feature",
            "type": "VARCHAR",
            "history_tracked": False,
            "temporal_class": "constant",
        },
        {
            "name": "prop__truck_feature",
            "type": "VARCHAR",
            "history_tracked": False,
            "temporal_class": "constant",
        },
    ]

    def test_combined_stream_one_column_list_null_for_undeclared_subtype_prop(
        self, tmp_path: Path
    ) -> None:
        """A single 'vehicles' stream selects both car_feature and truck_feature;
        a car row's after-image carries NULL for truck_feature and vice versa."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "vehicle",
            record_rows=[
                ("trunk", "c1", 10, True, None, 10, 0, "car", "sedan", None),
                ("trunk", "t1", 20, True, None, 20, 1, "truck", None, "flatbed"),
            ],
            history_rows=[],
            record_cols=self._COLS_VEHICLE,
            extra={"enum_domains": {"vehicle": {"vehicle_type": ["car", "truck"]}}},
        )
        config = _state_changes_config(
            [_kind_stream("vehicles", "vehicle", ["car_feature", "truck_feature"])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        by_record = {e.record_id: e.after for e in events}
        assert by_record["c1"] is not None
        assert by_record["c1"]["prop__car_feature"] == "sedan"
        assert by_record["c1"]["prop__truck_feature"] is None
        assert by_record["t1"] is not None
        assert by_record["t1"]["prop__car_feature"] is None
        assert by_record["t1"]["prop__truck_feature"] == "flatbed"
        # One column list — both events carry the same after-image key set.
        assert set(by_record["c1"].keys()) == set(by_record["t1"].keys())


# ---------------------------------------------------------------------------
# Record identity tests
# ---------------------------------------------------------------------------


class TestRecordIdentity:
    """record_id and presentation_id are set correctly on events."""

    def test_record_id_is_set_on_every_op(self, tmp_path: Path) -> None:
        """record_id is populated on c, u, and d events."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, False, 50, 50, 0, "a", "x")],
            history_rows=[("trunk", "item", "r1", "status", 30, "b")],
        )
        config = _single_kind_config("item", ["status"])
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        ops = {e.op for e in events}
        assert ops == {"c", "u", "d"}
        for e in events:
            assert e.record_id == "r1"

    def test_presentation_id_populated_when_kind_carries_surrogate(
        self, tmp_path: Path
    ) -> None:
        """presentation_id is set when the kind has a presentation_id column."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 1001, 10, True, None, 10, 0, "Alice")],
            history_rows=[],
            record_cols=_RECORD_COLS_WITH_PID,
        )
        config = _single_kind_config("item", [])
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        creates = [e for e in events if e.op == "c"]
        assert len(creates) == 1
        assert creates[0].presentation_id == "1001"

    def test_presentation_id_none_when_kind_has_no_surrogate(
        self, tmp_path: Path
    ) -> None:
        """presentation_id is None when the kind has no presentation_id column."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "x")],
            history_rows=[],
        )
        config = _single_kind_config("item", [])
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        for e in events:
            assert e.presentation_id is None


# ---------------------------------------------------------------------------
# After-image tests
# ---------------------------------------------------------------------------


class TestAfterImage:
    """after is None on delete; record_id present on c/u."""

    def test_delete_event_has_none_after(self, tmp_path: Path) -> None:
        """A 'd' event has after=None; record_id is still set."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, False, 50, 50, 0, "a", "x")],
            history_rows=[],
        )
        config = _single_kind_config("item", [])
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        deletes = [e for e in events if e.op == "d"]
        assert len(deletes) == 1
        assert deletes[0].after is None
        assert deletes[0].record_id == "r1"

    def test_create_event_has_after_with_record_id(self, tmp_path: Path) -> None:
        """A 'c' event has after containing record_id."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "x")],
            history_rows=[],
        )
        config = _single_kind_config("item", ["label"])
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        creates = [e for e in events if e.op == "c"]
        assert len(creates) == 1
        assert creates[0].after is not None
        assert creates[0].after["record_id"] == "r1"
        assert creates[0].after["prop__label"] == "x"


# ---------------------------------------------------------------------------
# Timestamp rendering tests
# ---------------------------------------------------------------------------


class TestTimestampRendering:
    """ts rendering: anchored ISO-8601 string or raw int."""

    def test_raw_ts_is_event_sim_time_int_when_no_anchor(self, tmp_path: Path) -> None:
        """With anchor=None, ts is the raw event_sim_time integer."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[
                ("trunk", "r1", 42_000_000_000, True, None, 42_000_000_000, 0, "a", "x")
            ],
            history_rows=[],
        )
        config = _single_kind_config("item", [])
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        creates = [e for e in events if e.op == "c"]
        assert len(creates) == 1
        assert creates[0].ts == 42_000_000_000

    def test_anchored_ts_is_iso8601_string_with_offset(self, tmp_path: Path) -> None:
        """With a resolved anchor, ts is an offset-bearing ISO-8601 string."""
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        anchor = EffectiveAnchor(start_instant=start, timezone=ZoneInfo("UTC"))
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[
                (
                    "trunk",
                    "r1",
                    3_600_000_000_000,
                    True,
                    None,
                    3_600_000_000_000,
                    0,
                    "a",
                    "x",
                )
            ],
            history_rows=[],
        )
        config = _single_kind_config("item", [])
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(
                    emit, config, anchor, notice_sink=discard_notice_sink
                )
            )

        creates = [e for e in events if e.op == "c"]
        assert len(creates) == 1
        ts = creates[0].ts
        assert isinstance(ts, str)
        assert "+00:00" in ts
        assert "2026-01-01T01:00:00" in ts

    def test_anchored_ts_utc_frame_for_dst_boundary(self, tmp_path: Path) -> None:
        """Elapsed sim_time is added in UTC (not wall-clock), yielding the true
        offset across a DST boundary."""
        start = datetime(2026, 3, 29, 0, 0, 0, tzinfo=timezone.utc)
        anchor = EffectiveAnchor(
            start_instant=start, timezone=ZoneInfo("Europe/London")
        )
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[
                (
                    "trunk",
                    "r1",
                    7_200_000_000_000,
                    True,
                    None,
                    7_200_000_000_000,
                    0,
                    "a",
                    "x",
                )
            ],
            history_rows=[],
        )
        config = _single_kind_config("item", [])
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(
                    emit, config, anchor, notice_sink=discard_notice_sink
                )
            )

        creates = [e for e in events if e.op == "c"]
        assert len(creates) == 1
        ts = creates[0].ts
        assert isinstance(ts, str)
        assert "+01:00" in ts
        assert "2026-03-29T03:00:00" in ts


# ---------------------------------------------------------------------------
# Layer-A-only routing: topic == stream name, route_table == the leaf
# ---------------------------------------------------------------------------


class TestLayerAOnlyRouting:
    """topic is always the declaring stream's name; route_table is the leaf."""

    def test_kind_stream_topic_is_stream_name_route_table_is_kind(
        self, tmp_path: Path
    ) -> None:
        """A flat-kind stream's topic is its declared name; route_table is the
        bare kind, decoupled from the stream name."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "x")],
            history_rows=[],
        )
        config = _state_changes_config([_kind_stream("items_feed", "item", [])])
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 1
        assert events[0].topic == "items_feed"
        assert events[0].route_table == "item"

    def test_membership_stream_route_table_is_owner_double_underscore_property(
        self, tmp_path: Path
    ) -> None:
        """A membership stream's route_table is <owner_kind>__<property>; its
        topic is the declared name, independent of that leaf."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 10, None)],
        )
        config = _membership_events_config(
            [_membership_stream("wait_events", "queue", "waiters", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 1
        assert events[0].topic == "wait_events"
        assert events[0].route_table == "queue__waiters"


# ---------------------------------------------------------------------------
# Business rules: each message leads with the stream name
# ---------------------------------------------------------------------------


class TestStreamKindResolvable:
    """Unknown kind raises ExportError, naming the stream, before any fold runs."""

    def test_unknown_kind_raises_export_error(self, tmp_path: Path) -> None:
        """A kind not in the sidecar raises ExportError naming the stream."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "x")],
            history_rows=[],
        )
        config = _state_changes_config([_kind_stream("ghosts", "ghost", [])])
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match="stream 'ghosts': kind 'ghost' has no records__ghost table",
            ):
                list(
                    iter_stream_events(
                        emit, config, None, notice_sink=discard_notice_sink
                    )
                )

    def test_bad_kind_fails_even_when_other_streams_valid(self, tmp_path: Path) -> None:
        """Validation fails on the bad stream even if other streams would succeed."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "x")],
            history_rows=[],
        )
        config = _state_changes_config(
            [_kind_stream("items", "item", []), _kind_stream("ghosts", "ghost", [])]
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="stream 'ghosts': .*records__ghost"):
                list(
                    iter_stream_events(
                        emit, config, None, notice_sink=discard_notice_sink
                    )
                )


class TestStreamPropertyResolvable:
    """Unknown property raises ExportError, naming the stream."""

    def test_unknown_property_raises_export_error(self, tmp_path: Path) -> None:
        """A property not in the sidecar raises ExportError naming the stream."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "x")],
            history_rows=[],
        )
        config = _state_changes_config([_kind_stream("items", "item", ["nonexistent"])])
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match=(
                    "stream 'items': property 'nonexistent'"
                    " has no prop__nonexistent column"
                ),
            ):
                list(
                    iter_stream_events(
                        emit, config, None, notice_sink=discard_notice_sink
                    )
                )


class TestStreamSubTypesRequireSubtyping:
    """sub_types on a flat kind raises ExportError, naming the stream."""

    def test_sub_types_on_flat_kind_raises(self, tmp_path: Path) -> None:
        """A flat kind refuses sub_types (StreamSubTypesRequireSubtyping)."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "x")],
            history_rows=[],
        )
        config = _state_changes_config(
            [_kind_stream("items", "item", [], sub_types=["a"])]
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match="stream 'items': kind 'item' is not sub-typed",
            ):
                list(
                    iter_stream_events(
                        emit, config, None, notice_sink=discard_notice_sink
                    )
                )


class TestStreamSubTypesDeclared:
    """An undeclared sub_types value raises ExportError, naming the stream."""

    def test_undeclared_sub_type_value_raises(self, tmp_path: Path) -> None:
        """A sub_types value outside the discriminator domain raises."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "widget",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "alpha")],
            history_rows=[],
            record_cols=_RECORD_COLS_DISCRIMINATOR,
            extra={"enum_domains": {"widget": {"widget_type": ["alpha", "beta"]}}},
        )
        config = _state_changes_config(
            [_kind_stream("widgets", "widget", ["widget_type"], sub_types=["gamma"])]
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match="stream 'widgets': sub_type 'gamma' is not declared",
            ):
                list(
                    iter_stream_events(
                        emit, config, None, notice_sink=discard_notice_sink
                    )
                )


class TestSingleBranch:
    """Multi-branch emit raises ExportError."""

    def test_multi_branch_emit_raises_export_error(self, tmp_path: Path) -> None:
        """A multi-branch emit raises ExportError with require_single_branch's message."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "x")],
            history_rows=[],
            n_branches=2,
        )
        config = _single_kind_config("item", [])
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="single-branch emit"):
                list(
                    iter_stream_events(
                        emit, config, None, notice_sink=discard_notice_sink
                    )
                )


class TestEagerValidation:
    """iter_stream_events raises ExportError before the first next() on bad config."""

    def test_unknown_kind_raises_before_next(self, tmp_path: Path) -> None:
        """ExportError for unknown kind is raised at call time, not at next()."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "x")],
            history_rows=[],
        )
        config = _state_changes_config([_kind_stream("ghosts", "ghost", [])])
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="records__ghost"):
                # No list() — error must come from the call itself
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)

    def test_unknown_property_raises_before_next(self, tmp_path: Path) -> None:
        """ExportError for unknown property is raised at call time, not at next()."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "x")],
            history_rows=[],
        )
        config = _state_changes_config([_kind_stream("items", "item", ["nonexistent"])])
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="nonexistent"):
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)

    def test_multi_branch_raises_before_next(self, tmp_path: Path) -> None:
        """ExportError for multi-branch is raised at call time, not at next()."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "x")],
            history_rows=[],
            n_branches=2,
        )
        config = _single_kind_config("item", [])
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="single-branch emit"):
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)


# ---------------------------------------------------------------------------
# StreamPropertySliceOnly column definitions
# ---------------------------------------------------------------------------

_RECORD_COLS_SLICE_ONLY: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__secret",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "slice_only",
    },
]

_RECORD_COLS_UNAVAILABLE_CLASS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    # history_tracked declared with no paired temporal_class — C13-violating.
    {"name": "prop__ghost", "type": "VARCHAR", "history_tracked": True},
]

_RECORD_COLS_DISCRIMINATOR: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    # Declared slice_only, yet exempt as the kind's discriminator — the class
    # is never consulted for it.
    {
        "name": "prop__widget_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "slice_only",
    },
]


class TestStreamPropertySliceOnly:
    """StreamPropertySliceOnly: a non-exempt slice_only property is refused,
    naming the stream."""

    def test_slice_only_property_raises_before_next(self, tmp_path: Path) -> None:
        """A non-exempt slice_only property is refused at call time, naming the
        stream, the kind, the property, and the class."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "s1")],
            history_rows=[],
            record_cols=_RECORD_COLS_SLICE_ONLY,
        )
        config = _state_changes_config([_kind_stream("items", "item", ["secret"])])
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match=(
                    "stream 'items': stream kind 'item': property 'secret' is"
                    " temporal_class: slice_only; it cannot ride the"
                    " state-changes after-image"
                ),
            ):
                # No list() — error must come from the call itself
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)

    def test_tracked_and_constant_properties_unaffected(self, tmp_path: Path) -> None:
        """A tracked property selected alongside the slice_only column stays
        unaffected as long as it is not itself selected."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "s1")],
            history_rows=[],
            record_cols=_RECORD_COLS_SLICE_ONLY,
        )
        config = _state_changes_config([_kind_stream("items", "item", ["status"])])
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )
        assert len(events) == 1

    def test_exempt_discriminator_streams_normally(self, tmp_path: Path) -> None:
        """The <kind>_type discriminator column passes StreamPropertySliceOnly
        at any declared class, and a sub_types selection streams normally."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "widget",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "alpha")],
            history_rows=[],
            record_cols=_RECORD_COLS_DISCRIMINATOR,
            extra={"enum_domains": {"widget": {"widget_type": ["alpha", "beta"]}}},
        )
        config = _state_changes_config(
            [_kind_stream("widgets", "widget", ["widget_type"], sub_types=["alpha"])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )
        assert len(events) == 1
        assert events[0].after is not None
        assert events[0].after["prop__widget_type"] == "alpha"

    def test_missing_temporal_pair_raises_unavailable(self, tmp_path: Path) -> None:
        """A selected property with history_tracked declared but no paired
        temporal_class raises TemporalClassUnavailableError, not ExportError."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "g1")],
            history_rows=[],
            record_cols=_RECORD_COLS_UNAVAILABLE_CLASS,
        )
        config = _state_changes_config([_kind_stream("items", "item", ["ghost"])])
        with open_emit(emit_dir) as emit:
            with pytest.raises(TemporalClassUnavailableError):
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)

    def test_membership_events_content_unaffected(self, tmp_path: Path) -> None:
        """membership-events content never reads a records column's class."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "item",
            "team",
            _MEMBERSHIP_BASIC_COLS,
            mem_rows=[("trunk", "r1", 10, None)],
        )
        config = _membership_events_config(
            [_membership_stream("team_events", "item", "team", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )
        assert len(events) == 1


# ---------------------------------------------------------------------------
# Fold-row column order
# ---------------------------------------------------------------------------

# Interleaved col definition: tracked (status), current (label), tracked (rank)
_RECORD_COLS_INTERLEAVED: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__label",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__rank",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]


class TestFoldColOrder:
    """Engine fold-row column list equals ROW_STATE_EVENT_COLUMNS + resolve[1:]."""

    def test_fold_col_names_equal_row_state_plus_resolve_tail(
        self, tmp_path: Path
    ) -> None:
        """For an interleaved kind, fold columns = ROW_STATE_EVENT_COLUMNS + resolve[1:]."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "lbl", "1")],
            history_rows=[],
            record_cols=_RECORD_COLS_INTERLEAVED,
        )
        config = _state_changes_config(
            [_kind_stream("items", "item", ["status", "label", "rank"])]
        )
        with open_emit(emit_dir) as emit:
            resolved = resolve_stream_columns(
                emit.sidecar, "item", frozenset({"status", "label", "rank"})
            )
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        creates = [e for e in events if e.op == "c"]
        assert len(creates) == 1
        after = creates[0].after
        assert after is not None
        assert list(after.keys()) == resolved

    def test_valid_iter_stream_events_same_events_as_before(
        self, tmp_path: Path
    ) -> None:
        """A valid iter_stream_events call still yields correct events/seq/ts."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 20, 0, "a", "x")],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
            ],
        )
        config = _single_kind_config("item", ["status"])
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 2
        assert [e.op for e in events] == ["c", "u"]
        assert [e.seq for e in events] == [1, 2]
        for e in events:
            assert isinstance(e.ts, int)


# ---------------------------------------------------------------------------
# Membership column definitions
# ---------------------------------------------------------------------------

_MEMBERSHIP_BASIC_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
]

_MEMBERSHIP_SCALAR_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__priority", "type": "VARCHAR"},
]

_MEMBERSHIP_REF_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__owner__kind", "type": "VARCHAR"},
    {"name": "member__owner__id", "type": "VARCHAR"},
]


# ---------------------------------------------------------------------------
# Membership emit builder helpers
# ---------------------------------------------------------------------------


def _owner_records_table_spec(
    conn: duckdb.DuckDBPyConnection, kind: str
) -> dict[str, object]:
    """Create and spec a minimal zero-row flat records table for `kind`.

    Election resolution (`resolve_stream_surfaces` / `Election.surface_for`)
    requires every kind a membership stream can name — the owner kind, and
    any membership reference field's per-row target kind — to carry a
    declared `records__<kind>` table, even under the no-`keys` default. The
    membership fixtures below carry no records data of their own, so this
    builds the minimal conformant shell.
    """
    table_name = f"records__{kind}"
    conn.execute(_ddl(table_name, _RECORD_COLS))
    return _table_spec(table_name, "records", _RECORD_COLS, 0, record_kind=kind)


def _build_single_membership_emit(
    tmp_path: Path,
    owner_kind: str,
    property_name: str,
    mem_cols: list[dict[str, object]],
    mem_rows: list[tuple[Any, ...]],
    extra_kinds: tuple[str, ...] = (),
) -> Path:
    """Build a minimal emit with one membership table.

    Also declares a minimal records table for `owner_kind` and every kind in
    `extra_kinds` (a membership reference field's target kind).
    """
    table_name = f"membership__{owner_kind}__{property_name}"
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl(table_name, mem_cols))
    placeholders = ", ".join("?" for _ in mem_cols)
    for row in mem_rows:
        conn.execute(f'INSERT INTO "{table_name}" VALUES ({placeholders})', list(row))

    tables = [
        _membership_table_spec(
            table_name, mem_cols, len(mem_rows), owner_kind, property_name
        )
    ]
    for kind in dict.fromkeys((owner_kind, *extra_kinds)):
        tables.append(_owner_records_table_spec(conn, kind))
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=tables,
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )
    return tmp_path


def _build_two_membership_emit(
    tmp_path: Path,
    owner_kind_a: str,
    property_a: str,
    cols_a: list[dict[str, object]],
    rows_a: list[tuple[Any, ...]],
    owner_kind_b: str,
    property_b: str,
    cols_b: list[dict[str, object]],
    rows_b: list[tuple[Any, ...]],
) -> Path:
    """Build a minimal emit with two membership tables and their owners' records
    tables (see `_owner_records_table_spec`)."""
    table_a = f"membership__{owner_kind_a}__{property_a}"
    table_b = f"membership__{owner_kind_b}__{property_b}"
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_ddl(table_a, cols_a))
    ph_a = ", ".join("?" for _ in cols_a)
    for row in rows_a:
        conn.execute(f'INSERT INTO "{table_a}" VALUES ({ph_a})', list(row))

    conn.execute(_ddl(table_b, cols_b))
    ph_b = ", ".join("?" for _ in cols_b)
    for row in rows_b:
        conn.execute(f'INSERT INTO "{table_b}" VALUES ({ph_b})', list(row))

    tables = [
        _membership_table_spec(table_a, cols_a, len(rows_a), owner_kind_a, property_a),
        _membership_table_spec(table_b, cols_b, len(rows_b), owner_kind_b, property_b),
    ]
    for kind in dict.fromkeys((owner_kind_a, owner_kind_b)):
        tables.append(_owner_records_table_spec(conn, kind))
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=tables,
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Membership end-to-end: cross-table merge, global seq
# ---------------------------------------------------------------------------


class TestMembershipCrossTableMerge:
    """iter_stream_events with membership-events merges tables into one global seq."""

    def test_two_tables_merged_global_seq(self, tmp_path: Path) -> None:
        """Two membership tables produce a single seq-ordered stream with global seq."""
        emit_dir = _build_two_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 10, None)],
            "team",
            "members",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r2", 20, None)],
        )
        config = _membership_events_config(
            [
                _membership_stream("waiters_feed", "queue", "waiters", []),
                _membership_stream("members_feed", "team", "members", []),
            ]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 2
        assert [e.seq for e in events] == [1, 2]

    def test_seq_monotonic_global_across_tables(self, tmp_path: Path) -> None:
        """seq is 1-based, monotonic, never resets between tables."""
        emit_dir = _build_two_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 10, None), ("trunk", "r2", 30, None)],
            "team",
            "members",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r3", 20, None)],
        )
        config = _membership_events_config(
            [
                _membership_stream("waiters_feed", "queue", "waiters", []),
                _membership_stream("members_feed", "team", "members", []),
            ]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 3
        assert [e.seq for e in events] == [1, 2, 3]
        assert [e.record_id for e in events] == ["r1", "r3", "r2"]

    def test_closed_interval_yields_join_and_leave(self, tmp_path: Path) -> None:
        """A closed interval (left_sim_time non-null) yields a join and a leave."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 100, 300)],
        )
        config = _membership_events_config(
            [_membership_stream("waiters_feed", "queue", "waiters", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 2
        assert [e.op for e in events] == ["join", "leave"]
        assert events[0].event_sim_time == 100
        assert events[1].event_sim_time == 300

    def test_open_interval_yields_join_only(self, tmp_path: Path) -> None:
        """An open interval (left_sim_time IS NULL) yields only a join."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 100, None)],
        )
        config = _membership_events_config(
            [_membership_stream("waiters_feed", "queue", "waiters", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 1
        assert events[0].op == "join"


# ---------------------------------------------------------------------------
# Membership StreamEvent fields
# ---------------------------------------------------------------------------


class TestMembershipStreamEventFields:
    """Membership StreamEvent carries the correct field values."""

    def test_membership_event_op_in_join_leave(self, tmp_path: Path) -> None:
        """op is 'join' or 'leave' for membership-events."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 10, 50)],
        )
        config = _membership_events_config(
            [_membership_stream("waiters_feed", "queue", "waiters", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert {e.op for e in events} == {"join", "leave"}

    def test_membership_event_kind_is_owner_kind(self, tmp_path: Path) -> None:
        """kind is the owner_kind, not the property."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 10, None)],
        )
        config = _membership_events_config(
            [_membership_stream("waiters_feed", "queue", "waiters", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 1
        assert events[0].kind == "queue"

    def test_membership_event_record_id_is_owner_record_id(
        self, tmp_path: Path
    ) -> None:
        """record_id is the owner record id."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "myrecord", 10, None)],
        )
        config = _membership_events_config(
            [_membership_stream("waiters_feed", "queue", "waiters", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 1
        assert events[0].record_id == "myrecord"

    def test_membership_event_presentation_id_is_none(self, tmp_path: Path) -> None:
        """presentation_id is always None for membership-events."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 10, 50)],
        )
        config = _membership_events_config(
            [_membership_stream("waiters_feed", "queue", "waiters", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        for e in events:
            assert e.presentation_id is None

    def test_membership_event_after_nonnull_on_join_and_leave(
        self, tmp_path: Path
    ) -> None:
        """after is non-null on both join and leave events."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 10, 50)],
        )
        config = _membership_events_config(
            [_membership_stream("waiters_feed", "queue", "waiters", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 2
        for e in events:
            assert e.after is not None


# ---------------------------------------------------------------------------
# Membership after-image: keys and order match resolve_membership_columns
# ---------------------------------------------------------------------------


class TestMembershipAfterImage:
    """after keys and order match resolve_membership_columns; values str-or-None."""

    def test_after_keys_match_resolve_membership_columns_no_fields(
        self, tmp_path: Path
    ) -> None:
        """With empty fields, after contains only record_id."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 10, None)],
        )
        config = _membership_events_config(
            [_membership_stream("waiters_feed", "queue", "waiters", [])]
        )
        with open_emit(emit_dir) as emit:
            resolved = resolve_membership_columns(emit.sidecar, "queue", "waiters", [])
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 1
        after = events[0].after
        assert after is not None
        assert list(after.keys()) == list(resolved)

    def test_after_keys_match_resolve_membership_columns_scalar_field(
        self, tmp_path: Path
    ) -> None:
        """With a scalar field, after keys match resolve_membership_columns order."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_SCALAR_COLS,
            [("trunk", "r1", 10, None, "high")],
        )
        config = _membership_events_config(
            [_membership_stream("waiters_feed", "queue", "waiters", ["priority"])]
        )
        with open_emit(emit_dir) as emit:
            resolved = resolve_membership_columns(
                emit.sidecar, "queue", "waiters", ["priority"]
            )
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 1
        after = events[0].after
        assert after is not None
        assert list(after.keys()) == list(resolved)
        assert list(after.keys()) == ["record_id", "elem__priority"]
        assert after["elem__priority"] == "high"

    def test_after_keys_match_resolve_membership_columns_ref_field(
        self, tmp_path: Path
    ) -> None:
        """With a reference field, after keys match resolve_membership_columns order."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_REF_COLS,
            [("trunk", "r1", 10, None, "person", "p1")],
            extra_kinds=("person",),
        )
        config = _membership_events_config(
            [_membership_stream("waiters_feed", "queue", "waiters", ["owner"])]
        )
        with open_emit(emit_dir) as emit:
            resolved = resolve_membership_columns(
                emit.sidecar, "queue", "waiters", ["owner"]
            )
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 1
        after = events[0].after
        assert after is not None
        assert list(after.keys()) == list(resolved)
        assert list(after.keys()) == [
            "record_id",
            "member__owner__kind",
            "member__owner__id",
        ]
        assert after["member__owner__kind"] == "person"
        assert after["member__owner__id"] == "p1"

    def test_after_values_are_str_or_none(self, tmp_path: Path) -> None:
        """All after-image values are str or None (never int, bool, etc.)."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_SCALAR_COLS,
            [("trunk", "r1", 10, None, None)],
        )
        config = _membership_events_config(
            [_membership_stream("waiters_feed", "queue", "waiters", ["priority"])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 1
        after = events[0].after
        assert after is not None
        for val in after.values():
            assert isinstance(val, str) or val is None


# ---------------------------------------------------------------------------
# Membership ts rebase
# ---------------------------------------------------------------------------


class TestMembershipTsRebase:
    """ts rendering reuses the anchor the same way as state-changes."""

    def test_membership_ts_raw_int_when_no_anchor(self, tmp_path: Path) -> None:
        """With anchor=None, ts is the raw event_sim_time int."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 42_000_000_000, None)],
        )
        config = _membership_events_config(
            [_membership_stream("waiters_feed", "queue", "waiters", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 1
        assert events[0].ts == 42_000_000_000

    def test_membership_ts_iso8601_with_anchor(self, tmp_path: Path) -> None:
        """With a resolved anchor, ts is an offset-bearing ISO-8601 string."""
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        anchor = EffectiveAnchor(start_instant=start, timezone=ZoneInfo("UTC"))
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 3_600_000_000_000, None)],
        )
        config = _membership_events_config(
            [_membership_stream("waiters_feed", "queue", "waiters", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(
                    emit, config, anchor, notice_sink=discard_notice_sink
                )
            )

        assert len(events) == 1
        ts = events[0].ts
        assert isinstance(ts, str)
        assert "+00:00" in ts
        assert "2026-01-01T01:00:00" in ts


# ---------------------------------------------------------------------------
# Membership stream-name tiebreak (replaces the retired source_identity tiebreak)
# ---------------------------------------------------------------------------


class TestMembershipStreamNameTiebreak:
    """Coincident events from two membership streams order by stream name."""

    def test_coincident_events_order_by_stream_name_not_owner_kind(
        self, tmp_path: Path
    ) -> None:
        """Events at the same sim_time order by the declaring stream's name, not
        by owner_kind/property — naming the beta-table's stream 'a_feed' and the
        alpha-table's stream 'z_feed' reverses the owner_kind-alphabetical order."""
        emit_dir = _build_two_membership_emit(
            tmp_path,
            "beta",
            "members",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 10, None)],
            "alpha",
            "members",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r2", 10, None)],
        )
        config = _membership_events_config(
            [
                _membership_stream("a_feed", "beta", "members", []),
                _membership_stream("z_feed", "alpha", "members", []),
            ]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 2
        assert [e.topic for e in events] == ["a_feed", "z_feed"]
        assert [e.record_id for e in events] == ["r1", "r2"]

    def test_same_stream_orders_by_record_id(self, tmp_path: Path) -> None:
        """Within the same stream, coincident events order by record_id."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [
                ("trunk", "z_last", 10, None),
                ("trunk", "a_first", 10, None),
            ],
        )
        config = _membership_events_config(
            [_membership_stream("waiters_feed", "queue", "waiters", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 2
        assert events[0].record_id == "a_first"
        assert events[1].record_id == "z_last"


# ---------------------------------------------------------------------------
# build_topic_set: pure function of the config, declared names in order
# ---------------------------------------------------------------------------


class TestBuildTopicSet:
    """build_topic_set is a pure function of config.streams — declaration order,
    declared-but-empty streams included, no sidecar/observation involved."""

    def test_topic_set_equals_declared_names_in_declaration_order(self) -> None:
        """The topic set is exactly the declared stream names, config order."""
        config = _state_changes_config(
            [
                _kind_stream("zeta_stream", "item", []),
                _kind_stream("alpha_stream", "item", ["status"]),
            ]
        )
        assert build_topic_set(config) == ("zeta_stream", "alpha_stream")

    def test_declared_but_empty_stream_included(self, tmp_path: Path) -> None:
        """A stream over a kind with zero matching rows still appears — declared
        intent, not observed rows, drives topic existence."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[],
            history_rows=[],
        )
        config = _single_kind_config("item", [])
        assert build_topic_set(config) == ("item",)
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )
        assert events == []

    def test_membership_topic_set_equals_declared_names(self) -> None:
        """Membership-content topic set is likewise the declared names, in order."""
        config = _membership_events_config(
            [
                _membership_stream("waiters_feed", "queue", "waiters", []),
                _membership_stream("members_feed", "team", "members", []),
            ]
        )
        assert build_topic_set(config) == ("waiters_feed", "members_feed")


# ---------------------------------------------------------------------------
# Business rules: MembershipResolvable / MembershipFieldResolvable
# ---------------------------------------------------------------------------


class TestMembershipResolvable:
    """A membership stream with no matching table -> ExportError before first
    yield, naming the stream."""

    def test_missing_table_raises_export_error(self, tmp_path: Path) -> None:
        """membership__queue__waiters absent -> ExportError at call time."""
        db_path = tmp_path / "run.duckdb"
        duckdb.connect(str(db_path)).close()
        _write_sidecar(
            tmp_path,
            tables=[],
            branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        )

        config = _membership_events_config(
            [_membership_stream("waiters_feed", "queue", "waiters", [])]
        )
        with open_emit(tmp_path) as emit:
            with pytest.raises(
                ExportError,
                match="stream 'waiters_feed': membership 'queue.waiters'"
                " has no membership__queue__waiters table",
            ):
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)


class TestMembershipFieldResolvable:
    """A selected field with no elem__/member__ column -> ExportError naming
    the stream."""

    def test_unknown_field_raises_export_error(self, tmp_path: Path) -> None:
        """A field not present as elem__ or member__ columns -> ExportError at
        call time."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 10, None)],
        )
        config = _membership_events_config(
            [_membership_stream("waiters_feed", "queue", "waiters", ["nonexistent"])]
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match="stream 'waiters_feed': field 'nonexistent'"
                " has no elem__/member__ column",
            ):
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)


# ---------------------------------------------------------------------------
# Regression: state-changes runs yield byte-identical events
# ---------------------------------------------------------------------------


class TestStateChangesRegression:
    """State-changes runs are byte-identical to before."""

    def test_state_changes_byte_identical(self, tmp_path: Path) -> None:
        """A state-changes run yields the same events after the grammar migration."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 20, 0, "a", "x")],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
            ],
        )
        config = _single_kind_config("item", ["status"])
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )

        assert len(events) == 2
        assert events[0].op == "c"
        assert events[1].op == "u"
        assert events[0].seq == 1
        assert events[1].seq == 2
        assert events[0].kind == "item"
        assert events[0].record_id == "r1"
        assert events[0].after is not None
        assert events[0].after["record_id"] == "r1"
        assert isinstance(events[0].ts, int)
        assert isinstance(events[1].ts, int)
