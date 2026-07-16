"""Tests for derivations.versioned_intervals.build_versioned_intervals_sql.

Each condition from the design doc's condition table gets a test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
from _support.sidecar_builder import write_emit as _write_sidecar

from fabulexa_forge.derivations.versioned_intervals import (
    VERSIONED_INTERVAL_COLUMNS,
    build_versioned_intervals_sql,
)
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import TableNotFoundError

# ---------------------------------------------------------------------------
# Emit builders
# ---------------------------------------------------------------------------

_RECORD_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__status", "type": "VARCHAR"},
    {"name": "prop__score", "type": "VARCHAR"},
]

_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _table_spec(
    name: str,
    category: str,
    cols: list[dict[str, object]],
    rows: int,
    record_kind: str | None = None,
    property_name: str | None = None,
) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": name,
        "category": category,
        "columns": cols,
        "rows": rows,
    }
    if record_kind is not None:
        spec["record_kind"] = record_kind
    if property_name is not None:
        spec["property"] = property_name
    return spec


def _build_emit(
    tmp_path: Path,
    history_rows: list[tuple[Any, ...]],
    record_cols: list[dict[str, object]] | None = None,
    record_rows: list[tuple[Any, ...]] | None = None,
    kind: str = "item",
) -> Path:
    """Build a minimal emit for versioned-intervals tests.

    Creates records__<kind> and history tables. Inserts the supplied rows.
    """
    if record_cols is None:
        record_cols = _RECORD_COLS
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl(f"records__{kind}", record_cols))
    conn.execute(_ddl("history", _HISTORY_COLS))

    col_placeholders = ", ".join("?" for _ in record_cols)
    for row in record_rows or []:
        conn.execute(
            f'INSERT INTO "records__{kind}" VALUES ({col_placeholders})',
            list(row),
        )
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            _table_spec(
                f"records__{kind}",
                "records",
                record_cols,
                len(record_rows or []),
                record_kind=kind,
            ),
            _table_spec("history", "fixed", _HISTORY_COLS, len(history_rows)),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REC_ID = VERSIONED_INTERVAL_COLUMNS.index("record_id")
_VS = VERSIONED_INTERVAL_COLUMNS.index("version_start")
_VE = VERSIONED_INTERVAL_COLUMNS.index("version_end")


def _run(
    emit_dir: Path,
    kind: str,
    tracked: frozenset[str],
    discriminator_filter: dict[str, str] | None = None,
) -> list[tuple[Any, ...]]:
    with open_emit(emit_dir) as emit:
        sql = build_versioned_intervals_sql(
            emit.sidecar, "trunk", kind, tracked, discriminator_filter or {}
        )
        return emit.query(sql, ())


# ---------------------------------------------------------------------------
# Single-property tests
# ---------------------------------------------------------------------------


class TestSingleProperty:
    """Single tracked property: one row per change point."""

    def test_single_property_one_row_per_change(self, tmp_path: Path) -> None:
        """One version row per history row when tracking a single property."""
        emit_dir = _build_emit(
            tmp_path,
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
                ("trunk", "item", "r1", "status", 30, "c"),
            ],
            record_rows=[("trunk", "r1", True, None, 30, "c", None)],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        r1_rows = [r for r in rows if r[_REC_ID] == "r1"]
        assert len(r1_rows) == 3

    def test_single_property_version_start_is_sim_time(self, tmp_path: Path) -> None:
        """version_start equals the change-point sim_time."""
        emit_dir = _build_emit(
            tmp_path,
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
            ],
            record_rows=[("trunk", "r1", True, None, 20, "b", None)],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        r1_rows = sorted([r for r in rows if r[_REC_ID] == "r1"], key=lambda r: r[_VS])
        assert r1_rows[0][_VS] == 10
        assert r1_rows[1][_VS] == 20

    def test_single_property_version_end_is_lead_sim_time(self, tmp_path: Path) -> None:
        """version_end = next change sim_time; NULL on last version."""
        emit_dir = _build_emit(
            tmp_path,
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
            ],
            record_rows=[("trunk", "r1", True, None, 20, "b", None)],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        r1_rows = sorted([r for r in rows if r[_REC_ID] == "r1"], key=lambda r: r[_VS])
        assert r1_rows[0][_VE] == 20
        assert r1_rows[1][_VE] is None

    def test_single_property_value_is_boundary_own_value(self, tmp_path: Path) -> None:
        """prop__<p> = the boundary row's own history.value (as-of)."""
        emit_dir = _build_emit(
            tmp_path,
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "alpha"),
                ("trunk", "item", "r1", "status", 20, "beta"),
            ],
            record_rows=[("trunk", "r1", True, None, 20, "beta", None)],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        r1_rows = sorted([r for r in rows if r[_REC_ID] == "r1"], key=lambda r: r[_VS])
        # prop__status is the 4th column (index 3, after record_id, version_start, version_end)
        assert r1_rows[0][3] == "alpha"
        assert r1_rows[1][3] == "beta"

    def test_null_history_value_passthrough(self, tmp_path: Path) -> None:
        """A NULL history.value yields a NULL prop__<p> (codec passthrough)."""
        emit_dir = _build_emit(
            tmp_path,
            history_rows=[
                ("trunk", "item", "r1", "status", 10, None),
            ],
            record_rows=[("trunk", "r1", True, None, 10, None, None)],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        r1_rows = [r for r in rows if r[_REC_ID] == "r1"]
        assert len(r1_rows) == 1
        assert r1_rows[0][3] is None

    def test_no_history_record_absent(self, tmp_path: Path) -> None:
        """A record with no tracked history rows is absent from the relation."""
        emit_dir = _build_emit(
            tmp_path,
            history_rows=[],
            record_rows=[("trunk", "r1", True, None, 10, "x", None)],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        assert all(r[_REC_ID] != "r1" for r in rows)


# ---------------------------------------------------------------------------
# Multi-property tests
# ---------------------------------------------------------------------------


class TestMultiProperty:
    """Multi-property fold: boundaries deduplicated on (record_id, sim_time)."""

    def test_same_sim_time_yields_one_boundary(self, tmp_path: Path) -> None:
        """Two properties changing at the same sim_time yield one boundary."""
        emit_dir = _build_emit(
            tmp_path,
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "score", 10, "5"),
            ],
            record_rows=[("trunk", "r1", True, None, 10, "a", "5")],
        )
        rows = _run(emit_dir, "item", frozenset({"status", "score"}))
        r1_rows = [r for r in rows if r[_REC_ID] == "r1"]
        assert len(r1_rows) == 1

    def test_different_sim_times_yield_separate_boundaries(
        self, tmp_path: Path
    ) -> None:
        """Two properties changing at different sim_times yield separate boundaries."""
        emit_dir = _build_emit(
            tmp_path,
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "score", 20, "5"),
            ],
            record_rows=[("trunk", "r1", True, None, 20, "a", "5")],
        )
        rows = _run(emit_dir, "item", frozenset({"status", "score"}))
        r1_rows = sorted([r for r in rows if r[_REC_ID] == "r1"], key=lambda r: r[_VS])
        assert len(r1_rows) == 2
        assert r1_rows[0][_VS] == 10
        assert r1_rows[1][_VS] == 20

    def test_prop_as_of_at_boundary_created_by_other_prop(self, tmp_path: Path) -> None:
        """prop__B at a boundary created by prop__A uses last-known B value."""
        emit_dir = _build_emit(
            tmp_path,
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                # score changes at 20 (after status)
                ("trunk", "item", "r1", "score", 20, "99"),
            ],
            record_rows=[("trunk", "r1", True, None, 20, "a", "99")],
        )
        rows = _run(emit_dir, "item", frozenset({"status", "score"}))
        r1_rows = sorted([r for r in rows if r[_REC_ID] == "r1"], key=lambda r: r[_VS])
        # At version_start=10 (status change): score has no prior row → NULL
        # At version_start=20 (score change): status = 'a' (last known)
        assert len(r1_rows) == 2
        # Find prop__status and prop__score column indices
        # Columns: record_id(0), version_start(1), version_end(2), prop__score(3 or 4), prop__status(3 or 4)
        # The order is sidecar declaration order: prop__status before prop__score in _RECORD_COLS
        status_idx = 3  # prop__status comes before prop__score in _RECORD_COLS
        score_idx = 4
        # At boundary 10 (status changes): score not yet present → NULL
        assert r1_rows[0][status_idx] == "a"
        assert r1_rows[0][score_idx] is None
        # At boundary 20 (score changes): status last known = 'a'
        assert r1_rows[1][status_idx] == "a"
        assert r1_rows[1][score_idx] == "99"

    def test_prop_null_before_first_history_row(self, tmp_path: Path) -> None:
        """version_start predates a property's first row → prop__<p> is NULL."""
        emit_dir = _build_emit(
            tmp_path,
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "first_status"),
                ("trunk", "item", "r1", "score", 30, "first_score"),
            ],
            record_rows=[
                ("trunk", "r1", True, None, 30, "first_status", "first_score")
            ],
        )
        rows = _run(emit_dir, "item", frozenset({"status", "score"}))
        r1_rows = sorted([r for r in rows if r[_REC_ID] == "r1"], key=lambda r: r[_VS])
        # At boundary 10 (status only): score has no row at or before 10 → NULL
        status_idx = 3  # prop__status before prop__score in sidecar
        score_idx = 4
        assert r1_rows[0][score_idx] is None
        assert r1_rows[0][status_idx] == "first_status"


# ---------------------------------------------------------------------------
# Ordering tests
# ---------------------------------------------------------------------------


class TestOrdering:
    """Row order: (record_id, version_start)."""

    def test_rows_ordered_by_record_id_then_version_start(self, tmp_path: Path) -> None:
        """Rows are ordered (record_id, version_start) — the tightened identity."""
        emit_dir = _build_emit(
            tmp_path,
            history_rows=[
                ("trunk", "item", "r2", "status", 5, "x"),
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r2", "status", 15, "y"),
                ("trunk", "item", "r1", "status", 20, "b"),
            ],
            record_rows=[
                ("trunk", "r1", True, None, 20, "b", None),
                ("trunk", "r2", True, None, 15, "y", None),
            ],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        ids_and_starts = [(r[_REC_ID], r[_VS]) for r in rows]
        assert ids_and_starts == sorted(ids_and_starts)


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrors:
    """Error conditions."""

    def test_missing_records_table_raises_table_not_found(self, tmp_path: Path) -> None:
        """TableNotFoundError when records__<kind> is absent from the sidecar."""
        db_path = tmp_path / "run.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute(_ddl("history", _HISTORY_COLS))
        conn.close()

        _write_sidecar(
            tmp_path,
            tables=[
                _table_spec("history", "fixed", _HISTORY_COLS, 0),
                # records__item intentionally absent
            ],
            branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        )

        with open_emit(tmp_path) as emit:
            with pytest.raises(TableNotFoundError):
                build_versioned_intervals_sql(
                    emit.sidecar, "trunk", "item", frozenset({"status"}), {}
                )

    def test_versioned_interval_columns_constant(self) -> None:
        """VERSIONED_INTERVAL_COLUMNS has the canonical three fixed columns."""
        assert VERSIONED_INTERVAL_COLUMNS == (
            "record_id",
            "version_start",
            "version_end",
        )


# ---------------------------------------------------------------------------
# ASOF-join shape (regression: the correlated per-row subquery OOM'd)
# ---------------------------------------------------------------------------


class TestAsofShape:
    """The as-of lookback compiles to ASOF JOINs, not correlated subqueries."""

    def test_sql_uses_asof_join_not_correlated_subquery(self, tmp_path: Path) -> None:
        """One ASOF LEFT JOIN per tracked property; no per-row lookback subquery.

        Regression for the O(events x history) correlated-subquery pattern that
        OOM'd row_state_events (~13 GB on one record with ~9.7k history rows).
        """
        emit_dir = _build_emit(
            tmp_path,
            history_rows=[("trunk", "item", "r1", "status", 10, "a")],
            record_rows=[("trunk", "r1", True, None, 10, "a", None)],
        )
        with open_emit(emit_dir) as emit:
            sql = build_versioned_intervals_sql(
                emit.sidecar, "trunk", "item", frozenset({"status", "score"}), {}
            )
        assert sql.count("ASOF LEFT JOIN") == 2
        # The old correlated shape ended each prop expression with this suffix.
        assert 'ORDER BY "sim_time" DESC LIMIT 1' not in sql

    def test_asof_values_match_correlated_semantics(self, tmp_path: Path) -> None:
        """ASOF lookback is inclusive at the boundary and NULL before first row."""
        emit_dir = _build_emit(
            tmp_path,
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "score", 20, "7"),
                ("trunk", "item", "r1", "status", 30, "b"),
            ],
            record_rows=[("trunk", "r1", True, None, 30, "b", "7")],
        )
        rows = _run(emit_dir, "item", frozenset({"status", "score"}))
        rows = sorted(rows, key=lambda r: r[_VS])
        # Columns: record_id, version_start, version_end, prop__status, prop__score
        # (sidecar declaration order: prop__status before prop__score).
        assert [(r[_VS], r[3], r[4]) for r in rows] == [
            (10, "a", None),
            (20, "a", "7"),
            (30, "b", "7"),
        ]


# ---------------------------------------------------------------------------
# Discriminator filter (regression: SCD-2 dropped source.filter)
# ---------------------------------------------------------------------------

_TYPED_RECORD_COLS: list[dict[str, object]] = _RECORD_COLS + [
    {"name": "prop__item_type", "type": "VARCHAR"},
]


def _build_typed_emit(tmp_path: Path) -> Path:
    """Emit with two sub-types (widget r1, gadget r2) and history for both."""
    return _build_emit(
        tmp_path,
        history_rows=[
            ("trunk", "item", "r1", "status", 10, "a"),
            ("trunk", "item", "r1", "status", 20, "b"),
            ("trunk", "item", "r2", "status", 15, "x"),
        ],
        record_cols=_TYPED_RECORD_COLS,
        record_rows=[
            ("trunk", "r1", True, None, 20, "b", None, "widget"),
            ("trunk", "r2", True, None, 15, "x", None, "gadget"),
        ],
    )


class TestDiscriminatorFilter:
    """discriminator_filter restricts intervals to the matching records."""

    def test_filter_selects_only_matching_subtype(self, tmp_path: Path) -> None:
        """Only the filtered sub-type's records contribute interval rows."""
        emit_dir = _build_typed_emit(tmp_path)
        rows = _run(
            emit_dir,
            "item",
            frozenset({"status"}),
            {"prop__item_type": "widget"},
        )
        assert {r[_REC_ID] for r in rows} == {"r1"}
        assert len(rows) == 2

    def test_empty_filter_selects_all_records(self, tmp_path: Path) -> None:
        """An empty filter selects the whole kind (no restriction)."""
        emit_dir = _build_typed_emit(tmp_path)
        rows = _run(emit_dir, "item", frozenset({"status"}), {})
        assert {r[_REC_ID] for r in rows} == {"r1", "r2"}

    def test_filter_no_match_yields_no_rows(self, tmp_path: Path) -> None:
        """A filter value matching no record yields an empty relation."""
        emit_dir = _build_typed_emit(tmp_path)
        rows = _run(
            emit_dir,
            "item",
            frozenset({"status"}),
            {"prop__item_type": "gizmo"},
        )
        assert rows == []
