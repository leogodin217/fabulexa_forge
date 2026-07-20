"""Tests for derivations.membership_state_at.

Covers build_membership_state_at_sql, materialized against minimal in-process
emits via the reader.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
from _support.sidecar_builder import identity_column as _identity_column
from _support.sidecar_builder import write_emit as _write_sidecar

from fabulexa_forge.derivations.membership_state_at import (
    MEMBERSHIP_STATE_AT_COLUMNS,
    build_membership_state_at_sql,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import TableNotFoundError

FORK_PATH = "trunk"

_MEM_COLS_SCALAR: list[dict[str, Any]] = [
    _identity_column("fork_path", "VARCHAR"),
    _identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__priority", "type": "VARCHAR"},
]

_MEM_COLS_REFERENCE: list[dict[str, Any]] = [
    _identity_column("fork_path", "VARCHAR"),
    _identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__ref__kind", "type": "VARCHAR"},
    {"name": "member__ref__id", "type": "VARCHAR"},
]

_MEM_COLS_EMPTY: list[dict[str, Any]] = [
    _identity_column("fork_path", "VARCHAR"),
    _identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
]


def _ddl(table: str, cols: list[dict[str, Any]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _table_spec(
    name: str,
    category: str,
    cols: list[dict[str, Any]],
    rows: int,
    record_kind: str | None = None,
    property_name: str | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
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
    owner_kind: str,
    property_name: str,
    mem_cols: list[dict[str, Any]],
    mem_rows: list[tuple[Any, ...]],
) -> Path:
    """Build a minimal emit with one membership table."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    table_name = f"membership__{owner_kind}__{property_name}"
    conn.execute(_ddl(table_name, mem_cols))

    placeholders = ", ".join("?" for _ in mem_cols)
    for row in mem_rows:
        conn.execute(
            f'INSERT INTO "{table_name}" VALUES ({placeholders})',
            list(row),
        )
    conn.close()

    tables = [
        _table_spec(
            table_name,
            "membership",
            mem_cols,
            len(mem_rows),
            record_kind=owner_kind,
            property_name=property_name,
        )
    ]
    _write_sidecar(
        tmp_path,
        tables=tables,
        branches=[{"fork_path": FORK_PATH, "parent": None, "slice_at": 9999}],
    )
    return tmp_path


def _run_state_at_sql(
    emit_dir: Path,
    owner_kind: str,
    property_name: str,
    fields: list[str],
    horizon_ns: int,
) -> list[tuple[Any, ...]]:
    with open_emit(emit_dir) as emit:
        sql = build_membership_state_at_sql(
            emit.sidecar,
            FORK_PATH,
            owner_kind,
            property_name,
            tuple(fields),
            horizon_ns,
        )
        return emit.query(sql, ())


class TestBuildMembershipStateAtSql:
    """Tests for build_membership_state_at_sql."""

    def test_closed_interval_visible_within_span(self, tmp_path: Path) -> None:
        """joined <= T < left yields exactly one row."""
        mem_rows = [(FORK_PATH, "r1", 100, 200)]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_state_at_sql(emit_dir, "queue", "waiters", [], 150)
        assert len(rows) == 1
        assert rows[0][MEMBERSHIP_STATE_AT_COLUMNS.index("record_id")] == "r1"

    def test_open_interval_is_contained(self, tmp_path: Path) -> None:
        """A NULL left_sim_time (still open) contains every horizon after join."""
        mem_rows = [(FORK_PATH, "r1", 100, None)]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_state_at_sql(emit_dir, "queue", "waiters", [], 1_000_000)
        assert len(rows) == 1

    def test_interval_after_horizon_is_absent(self, tmp_path: Path) -> None:
        """An interval whose joined_sim_time is >= T is absent."""
        mem_rows = [(FORK_PATH, "r1", 500, None)]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_state_at_sql(emit_dir, "queue", "waiters", [], 100)
        assert rows == []

    def test_interval_left_before_horizon_is_absent(self, tmp_path: Path) -> None:
        """A closed interval fully before the horizon is absent."""
        mem_rows = [(FORK_PATH, "r1", 100, 150)]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_state_at_sql(emit_dir, "queue", "waiters", [], 200)
        assert rows == []

    def test_zero_width_interval_contained_at_no_horizon(self, tmp_path: Path) -> None:
        """joined == left never satisfies joined < T <= ... AND T <= left."""
        mem_rows = [(FORK_PATH, "r1", 100, 100)]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        for horizon in (50, 100, 101, 200):
            rows = _run_state_at_sql(emit_dir, "queue", "waiters", [], horizon)
            assert rows == [], f"unexpected containment at horizon={horizon}"

    def test_inverted_interval_contained_at_no_horizon(self, tmp_path: Path) -> None:
        """left < joined is total, never an error, and contained at no horizon."""
        mem_rows = [(FORK_PATH, "r1", 200, 100)]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        for horizon in (0, 100, 150, 200, 300):
            rows = _run_state_at_sql(emit_dir, "queue", "waiters", [], horizon)
            assert rows == [], f"unexpected containment at horizon={horizon}"

    def test_overlapping_duplicate_intervals_yield_one_row_each(
        self, tmp_path: Path
    ) -> None:
        """Byte-identical duplicate intervals each yield one contained row."""
        mem_rows = [(FORK_PATH, "r1", 100, None), (FORK_PATH, "r1", 100, None)]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_state_at_sql(emit_dir, "queue", "waiters", [], 200)
        assert len(rows) == 2

    def test_left_sim_time_never_projected(self, tmp_path: Path) -> None:
        """left_sim_time never appears among the projected columns."""
        assert "left_sim_time" not in MEMBERSHIP_STATE_AT_COLUMNS
        mem_rows = [(FORK_PATH, "r1", 100, 500)]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_state_at_sql(emit_dir, "queue", "waiters", [], 200)
        assert len(rows) == 1
        assert len(rows[0]) == len(MEMBERSHIP_STATE_AT_COLUMNS)

    def test_scalar_field_column_shape(self, tmp_path: Path) -> None:
        """A scalar field projects as elem__<f>, cast VARCHAR."""
        mem_rows = [(FORK_PATH, "r1", 100, None, "high")]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_SCALAR, mem_rows)
        rows = _run_state_at_sql(emit_dir, "queue", "waiters", ["priority"], 200)
        assert len(rows) == 1
        field_idx = len(MEMBERSHIP_STATE_AT_COLUMNS)
        assert rows[0][field_idx] == "high"
        assert isinstance(rows[0][field_idx], str)

    def test_reference_field_column_shape(self, tmp_path: Path) -> None:
        """A reference field projects as the kind/id pair, each cast VARCHAR."""
        mem_rows = [(FORK_PATH, "r1", 100, None, "worker", "w1")]
        emit_dir = _build_emit(
            tmp_path, "team", "members", _MEM_COLS_REFERENCE, mem_rows
        )
        rows = _run_state_at_sql(emit_dir, "team", "members", ["ref"], 200)
        assert len(rows) == 1
        kind_idx = len(MEMBERSHIP_STATE_AT_COLUMNS)
        id_idx = kind_idx + 1
        assert rows[0][kind_idx] == "worker"
        assert rows[0][id_idx] == "w1"

    def test_empty_fields_returns_identity_and_joined_only(
        self, tmp_path: Path
    ) -> None:
        """Empty fields tuple projects owner identity + joined_sim_time only."""
        mem_rows = [(FORK_PATH, "r1", 100, None)]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_state_at_sql(emit_dir, "queue", "waiters", [], 200)
        assert len(rows[0]) == 2
        assert rows[0] == ("r1", 100)

    def test_joined_sim_time_is_raw_integer(self, tmp_path: Path) -> None:
        """joined_sim_time is projected as a raw integer, not VARCHAR."""
        mem_rows = [(FORK_PATH, "r1", 100, None)]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_state_at_sql(emit_dir, "queue", "waiters", [], 200)
        idx = MEMBERSHIP_STATE_AT_COLUMNS.index("joined_sim_time")
        assert isinstance(rows[0][idx], int)

    def test_order_by_joined_then_record_id(self, tmp_path: Path) -> None:
        """Rows order by (joined_sim_time, record_id)."""
        mem_rows = [
            (FORK_PATH, "b", 100, None),
            (FORK_PATH, "a", 100, None),
            (FORK_PATH, "z", 50, None),
        ]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_state_at_sql(emit_dir, "queue", "waiters", [], 200)
        rec_ids = [r[MEMBERSHIP_STATE_AT_COLUMNS.index("record_id")] for r in rows]
        assert rec_ids == ["z", "a", "b"]

    def test_order_by_field_tail_nulls_first(self, tmp_path: Path) -> None:
        """Same (joined_sim_time, record_id) breaks the tie on the field tail,
        NULLS FIRST."""
        mem_rows = [
            (FORK_PATH, "r1", 100, None, "z"),
            (FORK_PATH, "r1", 100, 400, None),
        ]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_SCALAR, mem_rows)
        rows = _run_state_at_sql(emit_dir, "queue", "waiters", ["priority"], 200)
        assert len(rows) == 2
        field_idx = len(MEMBERSHIP_STATE_AT_COLUMNS)
        assert rows[0][field_idx] is None
        assert rows[1][field_idx] == "z"

    def test_second_fork_path_excluded(self, tmp_path: Path) -> None:
        """Only the specified fork_path's rows appear."""
        mem_rows = [
            (FORK_PATH, "r1", 100, None),
            ("other_branch", "r2", 100, None),
        ]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_state_at_sql(emit_dir, "queue", "waiters", [], 200)
        rec_ids = {r[MEMBERSHIP_STATE_AT_COLUMNS.index("record_id")] for r in rows}
        assert rec_ids == {"r1"}

    def test_raises_table_not_found_when_absent(self, tmp_path: Path) -> None:
        """TableNotFoundError when the membership table is absent."""
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, [])
        with open_emit(emit_dir) as emit:
            with pytest.raises(TableNotFoundError):
                build_membership_state_at_sql(
                    emit.sidecar, FORK_PATH, "queue", "no_table", (), 100
                )

    def test_raises_export_error_on_unknown_field(self, tmp_path: Path) -> None:
        """ExportError when a selected field has no elem__/member__ column."""
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_SCALAR, [])
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="resolves to neither"):
                build_membership_state_at_sql(
                    emit.sidecar,
                    FORK_PATH,
                    "queue",
                    "waiters",
                    ("no_such_field",),
                    100,
                )
