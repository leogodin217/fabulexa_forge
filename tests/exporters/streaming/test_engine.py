"""Tests for the streaming engine: iter_stream_events, StreamEvent, StreamOutcome.

Materialized against minimal in-process emits built via the reader. Tests
cover all conditions from the Phase 3 spec.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import pytest

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION as SUPPORTED_VERSION
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import (
    MembershipSelection,
    RoutingConfig,
    StreamConfig,
    StreamKindSelection,
)
from fabulexa_forge.derivations.membership_events import resolve_membership_columns
from fabulexa_forge.derivations.row_state_events import resolve_stream_columns
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.streaming.engine import (
    build_topic_set,
    iter_stream_events,
)
from fabulexa_forge.reader.emit import open_emit

from ._helpers import _ddl

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

_RECORD_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__status", "type": "VARCHAR", "history_tracked": True},
    {"name": "prop__label", "type": "VARCHAR", "history_tracked": False},
]

_RECORD_COLS_WITH_PID: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__name", "type": "VARCHAR", "history_tracked": True},
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
) -> Path:
    """Build a minimal v4 emit with one kind and optional multi-branch support."""
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

    branches: list[dict[str, object]] = []
    if n_branches == 1:
        branches = [{"fork_path": "trunk", "parent": None, "slice_at": 9999}]
    else:
        branches = [
            {"fork_path": "trunk", "parent": None, "slice_at": 9999},
            {"fork_path": "trunk@alt", "parent": "trunk", "slice_at": 100},
        ]

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_VERSION,
        "branches": branches,
        "tables": [
            _table_spec(
                f"records__{kind}",
                "records",
                record_cols,
                len(record_rows),
                record_kind=kind,
            ),
            _table_spec("history", "fixed", _HISTORY_COLS, len(history_rows)),
        ],
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
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
    """Build a minimal v4 emit with two kinds."""
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

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
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
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_path


def _make_config(
    kinds: list[tuple[str, list[str]]],
) -> StreamConfig:
    """Build a StreamConfig from a list of (kind, properties) pairs."""
    return StreamConfig(
        content="state-changes",
        kinds=[StreamKindSelection(kind=k, properties=props) for k, props in kinds],
    )


# ---------------------------------------------------------------------------
# seq tests
# ---------------------------------------------------------------------------


class TestSeq:
    """seq is 1-based, monotonic, gap-free, spanning all kinds."""

    def test_seq_is_one_based_monotonic_gap_free(self, tmp_path: Path) -> None:
        """seq starts at 1, increments by 1, never gaps."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[
                ("trunk", "r1", 10, True, None, 10, "a", "x"),
                ("trunk", "r2", 20, True, None, 20, "b", "y"),
            ],
            history_rows=[("trunk", "item", "r1", "status", 30, "c")],
        )
        config = _make_config([("item", ["status"])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        seqs = [e.seq for e in events]
        assert seqs[0] == 1
        assert seqs == list(range(1, len(events) + 1))

    def test_seq_spans_all_kinds_not_reset(self, tmp_path: Path) -> None:
        """seq does not reset between kinds — it is a single global counter."""
        emit_dir = _build_two_kind_emit(
            tmp_path,
            "alpha",
            [("trunk", "a1", 10, True, None, 10, "x", "p")],
            "beta",
            [("trunk", "b1", 20, True, None, 20, "y", "q")],
            history_rows=[],
        )
        config = _make_config([("alpha", []), ("beta", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        assert len(events) == 2
        seqs = [e.seq for e in events]
        assert seqs == [1, 2]


# ---------------------------------------------------------------------------
# Ordering tests
# ---------------------------------------------------------------------------


class TestOrdering:
    """Cross-kind ordering follows the canonical merge key."""

    def test_cross_kind_interleave_by_sim_time(self, tmp_path: Path) -> None:
        """Events from different kinds interleave by event_sim_time."""
        # alpha record at t=5, beta record at t=3 — beta should come first
        emit_dir = _build_two_kind_emit(
            tmp_path,
            "alpha",
            [("trunk", "a1", 5, True, None, 5, "x", "p")],
            "beta",
            [("trunk", "b1", 3, True, None, 3, "y", "q")],
            history_rows=[],
        )
        config = _make_config([("alpha", []), ("beta", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        kinds_in_order = [e.kind for e in events]
        assert kinds_in_order == ["beta", "alpha"]

    def test_kind_tiebreak_is_deterministic(self, tmp_path: Path) -> None:
        """When two events have the same sim_time, kind orders them deterministically."""
        # Both at t=10 — alphabetically 'alpha' < 'beta'
        emit_dir = _build_two_kind_emit(
            tmp_path,
            "alpha",
            [("trunk", "a1", 10, True, None, 10, "x", "p")],
            "beta",
            [("trunk", "b1", 10, True, None, 10, "y", "q")],
            history_rows=[],
        )
        config = _make_config([("alpha", []), ("beta", [])])
        with open_emit(emit_dir) as emit:
            events1 = list(iter_stream_events(emit, config, None))

        # alpha before beta when sim_time and event_class tie
        assert events1[0].kind == "alpha"
        assert events1[1].kind == "beta"


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
            record_rows=[("trunk", "r1", 10, False, 50, 50, "a", "x")],
            history_rows=[("trunk", "item", "r1", "status", 30, "b")],
        )
        config = _make_config([("item", ["status"])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

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
            record_rows=[("trunk", "r1", 1001, 10, True, None, 10, "Alice")],
            history_rows=[],
            record_cols=_RECORD_COLS_WITH_PID,
        )
        config = _make_config([("item", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

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
            record_rows=[("trunk", "r1", 10, True, None, 10, "a", "x")],
            history_rows=[],
        )
        config = _make_config([("item", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

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
            record_rows=[("trunk", "r1", 10, False, 50, 50, "a", "x")],
            history_rows=[],
        )
        config = _make_config([("item", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        deletes = [e for e in events if e.op == "d"]
        assert len(deletes) == 1
        assert deletes[0].after is None
        assert deletes[0].record_id == "r1"

    def test_create_event_has_after_with_record_id(self, tmp_path: Path) -> None:
        """A 'c' event has after containing record_id."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, "a", "x")],
            history_rows=[],
        )
        config = _make_config([("item", ["label"])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

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
                ("trunk", "r1", 42_000_000_000, True, None, 42_000_000_000, "a", "x")
            ],
            history_rows=[],
        )
        config = _make_config([("item", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        creates = [e for e in events if e.op == "c"]
        assert len(creates) == 1
        assert creates[0].ts == 42_000_000_000

    def test_anchored_ts_is_iso8601_string_with_offset(self, tmp_path: Path) -> None:
        """With a resolved anchor, ts is an offset-bearing ISO-8601 string."""
        # start_instant at 2026-01-01T00:00:00+00:00
        # event_sim_time = 3_600_000_000_000 ns = 1 hour
        # expected ts ~ 2026-01-01T01:00:00+00:00
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        anchor = EffectiveAnchor(
            start_instant=start,
            timezone=ZoneInfo("UTC"),
        )
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
                    "a",
                    "x",
                )
            ],
            history_rows=[],
        )
        config = _make_config([("item", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, anchor))

        creates = [e for e in events if e.op == "c"]
        assert len(creates) == 1
        ts = creates[0].ts
        assert isinstance(ts, str)
        assert "+00:00" in ts
        assert "2026-01-01T01:00:00" in ts

    def test_anchored_ts_utc_frame_for_dst_boundary(self, tmp_path: Path) -> None:
        """Elapsed sim_time is added in UTC (not wall-clock), yielding the true offset.

        Europe/London: 2026-03-29T01:00:00+00:00 springs forward to +01:00.
        An event 2 hours after midnight UTC on that day should be 03:00 BST (+01:00).
        start_instant = 2026-03-29T00:00:00+00:00
        event_sim_time = 2 * 3600 * 1e9 ns = 7_200_000_000_000 ns
        Expected: 2026-03-29T03:00:00+01:00
        """
        start = datetime(2026, 3, 29, 0, 0, 0, tzinfo=timezone.utc)
        anchor = EffectiveAnchor(
            start_instant=start,
            timezone=ZoneInfo("Europe/London"),
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
                    "a",
                    "x",
                )
            ],
            history_rows=[],
        )
        config = _make_config([("item", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, anchor))

        creates = [e for e in events if e.op == "c"]
        assert len(creates) == 1
        ts = creates[0].ts
        assert isinstance(ts, str)
        # Should be in BST (+01:00), not UTC (+00:00)
        assert "+01:00" in ts
        assert "2026-03-29T03:00:00" in ts


# ---------------------------------------------------------------------------
# Business rules: StreamKindResolvable
# ---------------------------------------------------------------------------


class TestStreamKindResolvable:
    """Unknown kind raises ExportError before any fold materializes."""

    def test_unknown_kind_raises_export_error(self, tmp_path: Path) -> None:
        """A kind not in the sidecar raises ExportError with the right message."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, "a", "x")],
            history_rows=[],
        )
        config = _make_config([("ghost", [])])
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError, match="stream kind 'ghost' has no records__ghost table"
            ):
                list(iter_stream_events(emit, config, None))

    def test_bad_kind_fails_even_when_other_kinds_valid(self, tmp_path: Path) -> None:
        """Validation fails on the bad kind even if other kinds would succeed."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, "a", "x")],
            history_rows=[],
        )
        # config has valid "item" and invalid "ghost"
        config = _make_config([("item", []), ("ghost", [])])
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="records__ghost"):
                list(iter_stream_events(emit, config, None))


# ---------------------------------------------------------------------------
# Business rules: StreamPropertyResolvable
# ---------------------------------------------------------------------------


class TestStreamPropertyResolvable:
    """Unknown property raises ExportError."""

    def test_unknown_property_raises_export_error(self, tmp_path: Path) -> None:
        """A property not in the sidecar raises ExportError with the right message."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, "a", "x")],
            history_rows=[],
        )
        config = _make_config([("item", ["nonexistent"])])
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match="stream kind 'item': property 'nonexistent' has no prop__nonexistent column",
            ):
                list(iter_stream_events(emit, config, None))


# ---------------------------------------------------------------------------
# Business rules: SingleBranch
# ---------------------------------------------------------------------------


class TestSingleBranch:
    """Multi-branch emit raises ExportError."""

    def test_multi_branch_emit_raises_export_error(self, tmp_path: Path) -> None:
        """A multi-branch emit raises ExportError with require_single_branch's message."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, "a", "x")],
            history_rows=[],
            n_branches=2,
        )
        config = _make_config([("item", [])])
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="single-branch emit"):
                list(iter_stream_events(emit, config, None))


# ---------------------------------------------------------------------------
# Eager validation tests (Phase 2)
# ---------------------------------------------------------------------------

# Interleaved col definition: tracked (status), current (label), tracked (rank)
_RECORD_COLS_INTERLEAVED: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__status", "type": "VARCHAR", "history_tracked": True},
    {"name": "prop__label", "type": "VARCHAR", "history_tracked": False},
    {"name": "prop__rank", "type": "VARCHAR", "history_tracked": True},
]


class TestEagerValidation:
    """iter_stream_events raises ExportError before the first next() on bad config."""

    def test_unknown_kind_raises_before_next(self, tmp_path: Path) -> None:
        """ExportError for unknown kind is raised at call time, not at next()."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, "a", "x")],
            history_rows=[],
        )
        config = _make_config([("ghost", [])])
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="records__ghost"):
                # No list() — error must come from the call itself
                iter_stream_events(emit, config, None)

    def test_unknown_property_raises_before_next(self, tmp_path: Path) -> None:
        """ExportError for unknown property is raised at call time, not at next()."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, "a", "x")],
            history_rows=[],
        )
        config = _make_config([("item", ["nonexistent"])])
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="nonexistent"):
                iter_stream_events(emit, config, None)

    def test_multi_branch_raises_before_next(self, tmp_path: Path) -> None:
        """ExportError for multi-branch is raised at call time, not at next()."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, "a", "x")],
            history_rows=[],
            n_branches=2,
        )
        config = _make_config([("item", [])])
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="single-branch emit"):
                iter_stream_events(emit, config, None)


# ---------------------------------------------------------------------------
# Fold-row column order (Phase 2)
# ---------------------------------------------------------------------------


class TestFoldColOrder:
    """Engine fold-row column list equals ROW_STATE_EVENT_COLUMNS + resolve[1:]."""

    def test_fold_col_names_equal_row_state_plus_resolve_tail(
        self, tmp_path: Path
    ) -> None:
        """For an interleaved kind, fold columns = ROW_STATE_EVENT_COLUMNS + resolve[1:]."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, "a", "lbl", "1")],
            history_rows=[],
            record_cols=_RECORD_COLS_INTERLEAVED,
        )
        config = _make_config([("item", ["status", "label", "rank"])])
        with open_emit(emit_dir) as emit:
            resolved = resolve_stream_columns(
                emit.sidecar, "item", frozenset({"status", "label", "rank"})
            )
            events = list(iter_stream_events(emit, config, None))

        # Verify that the after-image of the create event has keys in resolve order
        creates = [e for e in events if e.op == "c"]
        assert len(creates) == 1
        after = creates[0].after
        assert after is not None
        # Key order of after dict must match resolved order
        assert list(after.keys()) == resolved

    def test_valid_iter_stream_events_same_events_as_before(
        self, tmp_path: Path
    ) -> None:
        """A valid iter_stream_events call still yields correct events/seq/ts."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[
                ("trunk", "r1", 10, True, None, 20, "a", "x"),
            ],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
            ],
        )
        config = _make_config([("item", ["status"])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        assert len(events) == 2
        ops = [e.op for e in events]
        assert ops == ["c", "u"]
        seqs = [e.seq for e in events]
        assert seqs == [1, 2]
        # With no anchor, ts is raw int
        for e in events:
            assert isinstance(e.ts, int)


# ---------------------------------------------------------------------------
# Membership column definitions
# ---------------------------------------------------------------------------

_MEMBERSHIP_BASIC_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
]

_MEMBERSHIP_SCALAR_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__priority", "type": "VARCHAR"},
]

_MEMBERSHIP_REF_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__owner__kind", "type": "VARCHAR"},
    {"name": "member__owner__id", "type": "VARCHAR"},
]


# ---------------------------------------------------------------------------
# Membership emit builder helpers
# ---------------------------------------------------------------------------


def _table_spec_membership(
    name: str,
    cols: list[dict[str, object]],
    rows: int,
    record_kind: str,
    property_name: str,
) -> dict[str, object]:
    """Build a membership table spec for the sidecar."""
    return {
        "name": name,
        "category": "membership",
        "columns": cols,
        "rows": rows,
        "record_kind": record_kind,
        "property": property_name,
    }


def _build_single_membership_emit(
    tmp_path: Path,
    owner_kind: str,
    property_name: str,
    mem_cols: list[dict[str, object]],
    mem_rows: list[tuple[Any, ...]],
) -> Path:
    """Build a minimal v4 emit with one membership table."""
    table_name = f"membership__{owner_kind}__{property_name}"
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl(table_name, mem_cols))
    placeholders = ", ".join("?" for _ in mem_cols)
    for row in mem_rows:
        conn.execute(f'INSERT INTO "{table_name}" VALUES ({placeholders})', list(row))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
            _table_spec_membership(
                table_name, mem_cols, len(mem_rows), owner_kind, property_name
            )
        ],
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
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
    """Build a minimal v4 emit with two membership tables."""
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

    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
            _table_spec_membership(
                table_a, cols_a, len(rows_a), owner_kind_a, property_a
            ),
            _table_spec_membership(
                table_b, cols_b, len(rows_b), owner_kind_b, property_b
            ),
        ],
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_path


def _make_membership_config(
    memberships: list[tuple[str, str, list[str]]],
    routing: RoutingConfig | None = None,
) -> StreamConfig:
    """Build a StreamConfig for content='membership-events'.

    Args:
        memberships: List of (owner_kind, property, fields) tuples.
        routing: Optional routing config.
    """
    return StreamConfig(
        content="membership-events",
        memberships=[
            MembershipSelection(owner_kind=ok, property=prop, fields=fields)
            for ok, prop, fields in memberships
        ],
        routing=routing,
    )


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
        config = _make_membership_config(
            [("queue", "waiters", []), ("team", "members", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        assert len(events) == 2
        seqs = [e.seq for e in events]
        assert seqs == [1, 2]

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
        config = _make_membership_config(
            [("queue", "waiters", []), ("team", "members", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        # 3 events: queue/r1@10, team/r3@20, queue/r2@30
        assert len(events) == 3
        seqs = [e.seq for e in events]
        assert seqs == [1, 2, 3]
        # Time ordering: 10 < 20 < 30
        record_ids = [e.record_id for e in events]
        assert record_ids == ["r1", "r3", "r2"]

    def test_closed_interval_yields_join_and_leave(self, tmp_path: Path) -> None:
        """A closed interval (left_sim_time non-null) yields a join and a leave."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 100, 300)],  # closed interval
        )
        config = _make_membership_config([("queue", "waiters", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        assert len(events) == 2
        ops = [e.op for e in events]
        assert ops == ["join", "leave"]
        assert events[0].event_sim_time == 100
        assert events[1].event_sim_time == 300

    def test_open_interval_yields_join_only(self, tmp_path: Path) -> None:
        """An open interval (left_sim_time IS NULL) yields only a join."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 100, None)],  # open interval
        )
        config = _make_membership_config([("queue", "waiters", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

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
            [("trunk", "r1", 10, 50)],  # closed interval: join + leave
        )
        config = _make_membership_config([("queue", "waiters", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        ops = {e.op for e in events}
        assert ops == {"join", "leave"}

    def test_membership_event_kind_is_owner_kind(self, tmp_path: Path) -> None:
        """kind is the owner_kind, not the property."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 10, None)],
        )
        config = _make_membership_config([("queue", "waiters", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

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
        config = _make_membership_config([("queue", "waiters", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

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
        config = _make_membership_config([("queue", "waiters", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        for e in events:
            assert e.presentation_id is None

    def test_membership_event_route_table_is_owner_property(
        self, tmp_path: Path
    ) -> None:
        """route_table is '<owner_kind>__<property>'."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 10, None)],
        )
        config = _make_membership_config([("queue", "waiters", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        assert len(events) == 1
        assert events[0].route_table == "queue__waiters"

    def test_membership_event_after_nonnull_on_join_and_leave(
        self, tmp_path: Path
    ) -> None:
        """after is non-null on both join and leave events."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 10, 50)],  # closed: join + leave
        )
        config = _make_membership_config([("queue", "waiters", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        assert len(events) == 2
        for e in events:
            assert e.after is not None

    def test_membership_event_topic_resolved(self, tmp_path: Path) -> None:
        """topic is resolved (default: route_table)."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 10, None)],
        )
        config = _make_membership_config([("queue", "waiters", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        assert len(events) == 1
        # Default topic_template = '{route_table}' → 'queue__waiters'
        assert events[0].topic == "queue__waiters"


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
        config = _make_membership_config([("queue", "waiters", [])])
        with open_emit(emit_dir) as emit:
            resolved = resolve_membership_columns(emit.sidecar, "queue", "waiters", [])
            events = list(iter_stream_events(emit, config, None))

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
        config = _make_membership_config([("queue", "waiters", ["priority"])])
        with open_emit(emit_dir) as emit:
            resolved = resolve_membership_columns(
                emit.sidecar, "queue", "waiters", ["priority"]
            )
            events = list(iter_stream_events(emit, config, None))

        assert len(events) == 1
        after = events[0].after
        assert after is not None
        assert list(after.keys()) == list(resolved)
        # record_id first, then elem__priority
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
        )
        config = _make_membership_config([("queue", "waiters", ["owner"])])
        with open_emit(emit_dir) as emit:
            resolved = resolve_membership_columns(
                emit.sidecar, "queue", "waiters", ["owner"]
            )
            events = list(iter_stream_events(emit, config, None))

        assert len(events) == 1
        after = events[0].after
        assert after is not None
        assert list(after.keys()) == list(resolved)
        # record_id first, then member__owner__kind, member__owner__id
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
            [("trunk", "r1", 10, None, None)],  # NULL elem__priority
        )
        config = _make_membership_config([("queue", "waiters", ["priority"])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

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
        config = _make_membership_config([("queue", "waiters", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        assert len(events) == 1
        assert events[0].ts == 42_000_000_000

    def test_membership_ts_iso8601_with_anchor(self, tmp_path: Path) -> None:
        """With a resolved anchor, ts is an offset-bearing ISO-8601 string."""
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        anchor = EffectiveAnchor(
            start_instant=start,
            timezone=ZoneInfo("UTC"),
        )
        # event_sim_time = 1 hour in ns
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 3_600_000_000_000, None)],
        )
        config = _make_membership_config([("queue", "waiters", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, anchor))

        assert len(events) == 1
        ts = events[0].ts
        assert isinstance(ts, str)
        assert "+00:00" in ts
        assert "2026-01-01T01:00:00" in ts


# ---------------------------------------------------------------------------
# Membership source_identity tiebreak
# ---------------------------------------------------------------------------


class TestMembershipSourceIdentityTiebreak:
    """Coincident events from two tables order by source_identity then record_id."""

    def test_coincident_events_order_by_source_identity(self, tmp_path: Path) -> None:
        """Events at same sim_time order by source_identity (owner_kind__property)."""
        # Both tables have an event at t=10
        # source_identity for table A: 'alpha__members' < 'beta__members' alphabetically
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
        # Config order: beta first, alpha second
        config = _make_membership_config(
            [("beta", "members", []), ("alpha", "members", [])]
        )
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        assert len(events) == 2
        # alpha__members < beta__members, so alpha's event comes first
        assert events[0].kind == "alpha"
        assert events[1].kind == "beta"

    def test_same_source_identity_orders_by_record_id(self, tmp_path: Path) -> None:
        """Within the same table, coincident events order by record_id."""
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
        config = _make_membership_config([("queue", "waiters", [])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        assert len(events) == 2
        assert events[0].record_id == "a_first"
        assert events[1].record_id == "z_last"


# ---------------------------------------------------------------------------
# build_topic_set for membership content
# ---------------------------------------------------------------------------


class TestBuildTopicSetMembership:
    """build_topic_set with membership-events config."""

    def test_default_topic_template_one_topic_per_table(self, tmp_path: Path) -> None:
        """Default topic_template → one topic per table (route_table)."""
        emit_dir = _build_two_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [],
            "team",
            "members",
            _MEMBERSHIP_BASIC_COLS,
            [],
        )
        config = _make_membership_config(
            [("queue", "waiters", []), ("team", "members", [])]
        )
        with open_emit(emit_dir) as emit:
            topics = build_topic_set(config, emit.sidecar)
        assert topics == ("queue__waiters", "team__members")

    def test_override_topic_template(self, tmp_path: Path) -> None:
        """Custom topic_template '{owner_kind}.{property}' produces different topics."""
        emit_dir = _build_two_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [],
            "team",
            "members",
            _MEMBERSHIP_BASIC_COLS,
            [],
        )
        routing = RoutingConfig(topic_template="{owner_kind}.{property}")
        config = _make_membership_config(
            [("queue", "waiters", []), ("team", "members", [])],
            routing=routing,
        )
        with open_emit(emit_dir) as emit:
            topics = build_topic_set(config, emit.sidecar)
        assert topics == ("queue.waiters", "team.members")

    def test_groups_merge_collapses_tables(self, tmp_path: Path) -> None:
        """groups merge: two tables collapse onto one topic."""
        emit_dir = _build_two_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [],
            "team",
            "members",
            _MEMBERSHIP_BASIC_COLS,
            [],
        )
        routing = RoutingConfig(
            groups={"all_memberships": ["queue__waiters", "team__members"]}
        )
        config = _make_membership_config(
            [("queue", "waiters", []), ("team", "members", [])],
            routing=routing,
        )
        with open_emit(emit_dir) as emit:
            topics = build_topic_set(config, emit.sidecar)
        # Both map to "all_memberships" via groups
        assert topics == ("all_memberships",)

    def test_declared_but_empty_topic_included(self, tmp_path: Path) -> None:
        """A selected table contributes its topic even with zero events (declared-but-empty)."""
        emit_dir = _build_two_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [],
            "team",
            "members",
            _MEMBERSHIP_BASIC_COLS,
            [],
        )
        config = _make_membership_config(
            [("queue", "waiters", []), ("team", "members", [])]
        )
        with open_emit(emit_dir) as emit:
            topics = build_topic_set(config, emit.sidecar)
        # Both tables declare a topic regardless of whether they have rows
        assert "team__members" in topics
        assert "queue__waiters" in topics


# ---------------------------------------------------------------------------
# Business rules: MembershipResolvable
# ---------------------------------------------------------------------------


class TestMembershipResolvable:
    """A memberships[] pair with no matching table → ExportError before first yield."""

    def test_missing_table_raises_export_error(self, tmp_path: Path) -> None:
        """membership__queue__waiters absent → ExportError at call time."""
        # Build emit with NO membership tables
        db_path = tmp_path / "run.duckdb"
        duckdb.connect(str(db_path)).close()
        sidecar: dict[str, object] = {
            "base_format_version": SUPPORTED_VERSION,
            "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
            "tables": [],
        }
        (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")

        config = _make_membership_config([("queue", "waiters", [])])
        with open_emit(tmp_path) as emit:
            with pytest.raises(ExportError, match="membership__queue__waiters"):
                iter_stream_events(emit, config, None)


# ---------------------------------------------------------------------------
# Business rules: MembershipFieldResolvable
# ---------------------------------------------------------------------------


class TestMembershipFieldResolvable:
    """A selected field with no elem__/member__ column → ExportError."""

    def test_unknown_field_raises_export_error(self, tmp_path: Path) -> None:
        """A field not present as elem__ or member__ columns → ExportError at call time."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,  # no elem__ columns
            [("trunk", "r1", 10, None)],
        )
        config = _make_membership_config([("queue", "waiters", ["nonexistent"])])
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="nonexistent"):
                iter_stream_events(emit, config, None)


# ---------------------------------------------------------------------------
# Business rules: StreamTemplatePlaceholders with membership content
# ---------------------------------------------------------------------------


class TestStreamTemplatePlaceholdersMembership:
    """topic_template referencing {sub_type} under membership-events fails."""

    def test_sub_type_placeholder_fails_for_membership(self, tmp_path: Path) -> None:
        """A topic_template with {sub_type} fails for membership-events (no sub_type)."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 10, None)],
        )
        routing = RoutingConfig(topic_template="{sub_type}")
        config = _make_membership_config(
            [("queue", "waiters", [])],
            routing=routing,
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="sub_type"):
                iter_stream_events(emit, config, None)


# ---------------------------------------------------------------------------
# Business rules: StreamGroupMembersResolve with membership content
# ---------------------------------------------------------------------------


class TestStreamGroupMembersResolveMembership:
    """A groups member that matches no rendered membership topic → ExportError."""

    def test_group_member_not_in_membership_topics_raises_export_error(
        self, tmp_path: Path
    ) -> None:
        """StreamGroupMembersResolve: a group member that matches no membership route
        raises ExportError before the first yield."""
        emit_dir = _build_single_membership_emit(
            tmp_path,
            "queue",
            "waiters",
            _MEMBERSHIP_BASIC_COLS,
            [("trunk", "r1", 10, None)],
        )
        # The rendered topic for queue/waiters is "queue__waiters";
        # groups references "nonexistent_topic" which will not match.
        routing = RoutingConfig(
            groups={"combined": ["nonexistent_topic"]},
        )
        config = _make_membership_config(
            [("queue", "waiters", [])],
            routing=routing,
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="nonexistent_topic"):
                iter_stream_events(emit, config, None)


# ---------------------------------------------------------------------------
# Regression: state-changes runs yield byte-identical events
# ---------------------------------------------------------------------------


class TestStateChangesRegression:
    """State-changes runs are byte-identical to before."""

    def test_state_changes_byte_identical(self, tmp_path: Path) -> None:
        """A state-changes run yields the same events after membership engine changes."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[
                ("trunk", "r1", 10, True, None, 20, "a", "x"),
            ],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
            ],
        )
        config = _make_config([("item", ["status"])])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        assert len(events) == 2
        assert events[0].op == "c"
        assert events[1].op == "u"
        assert events[0].seq == 1
        assert events[1].seq == 2
        assert events[0].kind == "item"
        assert events[0].record_id == "r1"
        assert events[0].after is not None
        assert events[0].after["record_id"] == "r1"
        # ts is raw int with no anchor
        assert isinstance(events[0].ts, int)
        assert isinstance(events[1].ts, int)
