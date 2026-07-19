"""Tests for derivations.membership_events.

Covers resolve_membership_columns and build_membership_events_sql.
Materialized against minimal in-process emits via the reader.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
from _support.sidecar_builder import identity_column as _identity_column
from _support.sidecar_builder import write_emit as _write_sidecar

from fabulexa_forge.derivations.membership_events import (
    EVENT_CLASS_JOIN,
    EVENT_CLASS_LEAVE,
    MEMBERSHIP_EVENT_COLUMNS,
    build_membership_events_sql,
    resolve_membership_columns,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import TableNotFoundError

FORK_PATH = "trunk"


# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

_MEM_COLS_SCALAR: list[dict[str, Any]] = [
    _identity_column("fork_path", "VARCHAR"),
    _identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__priority", "type": "VARCHAR"},
    {"name": "elem__position", "type": "BIGINT"},
]

_MEM_COLS_REFERENCE: list[dict[str, Any]] = [
    _identity_column("fork_path", "VARCHAR"),
    _identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__ref__kind", "type": "VARCHAR"},
    {"name": "member__ref__id", "type": "VARCHAR"},
]

# Table with both scalar and reference fields in mixed declaration order
_MEM_COLS_MIXED: list[dict[str, Any]] = [
    _identity_column("fork_path", "VARCHAR"),
    _identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__ref__kind", "type": "VARCHAR"},
    {"name": "member__ref__id", "type": "VARCHAR"},
    {"name": "elem__priority", "type": "VARCHAR"},
]

_MEM_COLS_EMPTY: list[dict[str, Any]] = [
    _identity_column("fork_path", "VARCHAR"),
    _identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
]


# ---------------------------------------------------------------------------
# Emit builder helpers
# ---------------------------------------------------------------------------


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
    extra_tables: list[dict[str, Any]] | None = None,
    extra_db_ops: list[tuple[str, list[Any]]] | None = None,
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

    if extra_db_ops:
        for sql, params in extra_db_ops:
            conn.execute(sql, params)

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
    if extra_tables:
        tables.extend(extra_tables)

    _write_sidecar(
        tmp_path,
        tables=tables,
        branches=[{"fork_path": FORK_PATH, "parent": None, "slice_at": 9999}],
    )
    return tmp_path


def _run_membership_sql(
    emit_dir: Path,
    owner_kind: str,
    property_name: str,
    fields: list[str],
) -> list[tuple[Any, ...]]:
    """Open emit and materialize membership events SQL."""
    with open_emit(emit_dir) as emit:
        sql = build_membership_events_sql(
            emit.sidecar, FORK_PATH, owner_kind, property_name, fields
        )
        return emit.query(sql, ())


# ---------------------------------------------------------------------------
# resolve_membership_columns tests
# ---------------------------------------------------------------------------


class TestResolveMembershipColumns:
    """Tests for resolve_membership_columns column ordering."""

    def test_scalar_field_maps_to_elem_col(self, tmp_path: Path) -> None:
        """A scalar field f maps to ('record_id', 'elem__f')."""
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_SCALAR, [])
        with open_emit(emit_dir) as emit:
            result = resolve_membership_columns(
                emit.sidecar, "queue", "waiters", ["priority"]
            )
        assert result == ("record_id", "elem__priority")

    def test_reference_field_maps_to_kind_and_id(self, tmp_path: Path) -> None:
        """A reference field f maps to ('record_id', 'member__f__kind', 'member__f__id')."""
        emit_dir = _build_emit(tmp_path, "team", "members", _MEM_COLS_REFERENCE, [])
        with open_emit(emit_dir) as emit:
            result = resolve_membership_columns(
                emit.sidecar, "team", "members", ["ref"]
            )
        assert result == ("record_id", "member__ref__kind", "member__ref__id")

    def test_record_id_first(self, tmp_path: Path) -> None:
        """record_id is always first regardless of fields."""
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_SCALAR, [])
        with open_emit(emit_dir) as emit:
            result = resolve_membership_columns(
                emit.sidecar, "queue", "waiters", ["priority"]
            )
        assert result[0] == "record_id"

    def test_empty_fields_returns_record_id_only(self, tmp_path: Path) -> None:
        """Empty fields list yields ('record_id',) only."""
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_SCALAR, [])
        with open_emit(emit_dir) as emit:
            result = resolve_membership_columns(emit.sidecar, "queue", "waiters", [])
        assert result == ("record_id",)

    def test_declaration_order_not_fields_order(self, tmp_path: Path) -> None:
        """Result follows element-schema declaration order, not author fields order.

        _MEM_COLS_MIXED declares: member__ref__kind, member__ref__id, elem__priority
        Author specifies fields in reversed order: ['priority', 'ref'].
        Result must follow declaration order: ref columns first, then priority.
        """
        emit_dir = _build_emit(tmp_path, "queue", "tasks", _MEM_COLS_MIXED, [])
        with open_emit(emit_dir) as emit:
            result = resolve_membership_columns(
                emit.sidecar, "queue", "tasks", ["priority", "ref"]
            )
        # Declaration order: member__ref__kind, member__ref__id, elem__priority
        assert result == (
            "record_id",
            "member__ref__kind",
            "member__ref__id",
            "elem__priority",
        )

    def test_raises_export_error_on_unknown_field(self, tmp_path: Path) -> None:
        """A field with no elem__/member__ column raises ExportError."""
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_SCALAR, [])
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="resolves to neither"):
                resolve_membership_columns(
                    emit.sidecar, "queue", "waiters", ["nonexistent"]
                )

    def test_raises_table_not_found_when_absent(self, tmp_path: Path) -> None:
        """TableNotFoundError when the membership table is not in the sidecar."""
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_SCALAR, [])
        with open_emit(emit_dir) as emit:
            with pytest.raises(TableNotFoundError):
                resolve_membership_columns(emit.sidecar, "queue", "no_such_table", [])


# ---------------------------------------------------------------------------
# build_membership_events_sql tests
# ---------------------------------------------------------------------------


class TestBuildMembershipEventsSql:
    """Tests for build_membership_events_sql materialized against in-process emits."""

    def test_closed_interval_produces_join_and_leave(self, tmp_path: Path) -> None:
        """A non-null left_sim_time produces both join and leave event rows."""
        mem_rows = [
            (FORK_PATH, "r1", 100, 200),  # closed interval
        ]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_membership_sql(emit_dir, "queue", "waiters", [])
        ops = [r[MEMBERSHIP_EVENT_COLUMNS.index("op")] for r in rows]
        assert "join" in ops
        assert "leave" in ops
        assert len(rows) == 2

    def test_join_at_joined_sim_time(self, tmp_path: Path) -> None:
        """The join event has event_sim_time == joined_sim_time."""
        mem_rows = [
            (FORK_PATH, "r1", 100, 200),
        ]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_membership_sql(emit_dir, "queue", "waiters", [])
        join_row = next(
            r for r in rows if r[MEMBERSHIP_EVENT_COLUMNS.index("op")] == "join"
        )
        assert join_row[MEMBERSHIP_EVENT_COLUMNS.index("event_sim_time")] == 100

    def test_leave_at_left_sim_time(self, tmp_path: Path) -> None:
        """The leave event has event_sim_time == left_sim_time."""
        mem_rows = [
            (FORK_PATH, "r1", 100, 200),
        ]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_membership_sql(emit_dir, "queue", "waiters", [])
        leave_row = next(
            r for r in rows if r[MEMBERSHIP_EVENT_COLUMNS.index("op")] == "leave"
        )
        assert leave_row[MEMBERSHIP_EVENT_COLUMNS.index("event_sim_time")] == 200

    def test_open_interval_produces_join_only(self, tmp_path: Path) -> None:
        """A NULL left_sim_time produces only a join event."""
        mem_rows = [
            (FORK_PATH, "r1", 100, None),  # open interval
        ]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_membership_sql(emit_dir, "queue", "waiters", [])
        assert len(rows) == 1
        assert rows[0][MEMBERSHIP_EVENT_COLUMNS.index("op")] == "join"

    def test_op_codes(self, tmp_path: Path) -> None:
        """op column is 'join' or 'leave'."""
        mem_rows = [
            (FORK_PATH, "r1", 100, 200),
            (FORK_PATH, "r2", 150, None),
        ]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_membership_sql(emit_dir, "queue", "waiters", [])
        ops = {r[MEMBERSHIP_EVENT_COLUMNS.index("op")] for r in rows}
        assert ops == {"join", "leave"}

    def test_event_class_is_raw_integer(self, tmp_path: Path) -> None:
        """event_class is projected as raw integer (0=join, 1=leave), not VARCHAR."""
        mem_rows = [
            (FORK_PATH, "r1", 100, 200),
        ]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_membership_sql(emit_dir, "queue", "waiters", [])
        cls_idx = MEMBERSHIP_EVENT_COLUMNS.index("event_class")
        for row in rows:
            assert isinstance(row[cls_idx], int)
        join_row = next(
            r for r in rows if r[MEMBERSHIP_EVENT_COLUMNS.index("op")] == "join"
        )
        leave_row = next(
            r for r in rows if r[MEMBERSHIP_EVENT_COLUMNS.index("op")] == "leave"
        )
        assert join_row[cls_idx] == EVENT_CLASS_JOIN  # 0
        assert leave_row[cls_idx] == EVENT_CLASS_LEAVE  # 1

    def test_event_sim_time_is_raw_integer(self, tmp_path: Path) -> None:
        """event_sim_time is projected as a raw integer."""
        mem_rows = [
            (FORK_PATH, "r1", 100, None),
        ]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_membership_sql(emit_dir, "queue", "waiters", [])
        ts_idx = MEMBERSHIP_EVENT_COLUMNS.index("event_sim_time")
        assert isinstance(rows[0][ts_idx], int)

    def test_payload_columns_are_varchar_or_null(self, tmp_path: Path) -> None:
        """After-image payload columns (record_id, field columns) are VARCHAR or NULL."""
        mem_rows = [
            (FORK_PATH, "r1", 100, None, "high", 5),
        ]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_SCALAR, mem_rows)
        rows = _run_membership_sql(emit_dir, "queue", "waiters", ["priority"])
        for row in rows:
            # record_id
            assert isinstance(row[0], str)
            # elem__priority (index 4, after 4 prefix columns)
            payload_val = row[len(MEMBERSHIP_EVENT_COLUMNS)]
            assert payload_val is None or isinstance(payload_val, str)

    def test_order_join_before_leave_same_instant(self, tmp_path: Path) -> None:
        """Coincident join/leave events order join (event_class=0) before leave (event_class=1)."""
        # One record with joined_sim_time == left_sim_time for another record
        mem_rows = [
            (FORK_PATH, "r1", 100, None),  # join at 100
            (FORK_PATH, "r2", 50, 100),  # leave at 100 (same as r1's join)
        ]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_membership_sql(emit_dir, "queue", "waiters", [])
        # Find rows at time 100
        ts_idx = MEMBERSHIP_EVENT_COLUMNS.index("event_sim_time")
        cls_idx = MEMBERSHIP_EVENT_COLUMNS.index("event_class")
        rows_at_100 = [r for r in rows if r[ts_idx] == 100]
        assert len(rows_at_100) == 2
        # join (class 0) must come before leave (class 1)
        assert rows_at_100[0][cls_idx] == EVENT_CLASS_JOIN
        assert rows_at_100[1][cls_idx] == EVENT_CLASS_LEAVE

    def test_order_by_record_id_within_same_class(self, tmp_path: Path) -> None:
        """Rows at same (event_sim_time, event_class) order by record_id."""
        mem_rows = [
            (FORK_PATH, "b", 100, None),
            (FORK_PATH, "a", 100, None),
        ]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_membership_sql(emit_dir, "queue", "waiters", [])
        rec_ids = [r[MEMBERSHIP_EVENT_COLUMNS.index("record_id")] for r in rows]
        assert rec_ids == ["a", "b"]

    def test_nulls_first_on_field_columns(self, tmp_path: Path) -> None:
        """Field-value ordering is NULLS FIRST.

        Use same record_id so field-value is the tiebreaker. Two open intervals
        for the same record_id at the same event_sim_time — the NULL priority
        row must precede the non-null one.
        """
        mem_rows = [
            (FORK_PATH, "r1", 100, None, "z", None),  # non-null priority
            (FORK_PATH, "r1", 100, None, None, None),  # null priority, same record/time
        ]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_SCALAR, mem_rows)
        rows = _run_membership_sql(emit_dir, "queue", "waiters", ["priority"])
        # Only join rows (open intervals)
        op_idx = MEMBERSHIP_EVENT_COLUMNS.index("op")
        join_rows = [r for r in rows if r[op_idx] == "join"]
        assert len(join_rows) == 2
        # Field column is at index len(MEMBERSHIP_EVENT_COLUMNS) == 4
        field_idx = len(MEMBERSHIP_EVENT_COLUMNS)
        # NULL should come first (NULLS FIRST)
        assert join_rows[0][field_idx] is None
        assert join_rows[1][field_idx] == "z"

    def test_reference_field_order_by_kind_then_id(self, tmp_path: Path) -> None:
        """A reference field orders by member__<f>__kind then member__<f>__id.

        Use the same record_id so kind is the tiebreaker; verifies NULLS FIRST
        and ascending kind ordering.
        """
        mem_rows = [
            (FORK_PATH, "r1", 100, None, "z_kind", "z_id"),
            (FORK_PATH, "r1", 100, None, "a_kind", "b_id"),
            (FORK_PATH, "r1", 100, None, None, None),
        ]
        emit_dir = _build_emit(
            tmp_path, "team", "members", _MEM_COLS_REFERENCE, mem_rows
        )
        rows = _run_membership_sql(emit_dir, "team", "members", ["ref"])
        op_idx = MEMBERSHIP_EVENT_COLUMNS.index("op")
        join_rows = [r for r in rows if r[op_idx] == "join"]
        assert len(join_rows) == 3
        # member__ref__kind at index len(MEMBERSHIP_EVENT_COLUMNS)
        kind_idx = len(MEMBERSHIP_EVENT_COLUMNS)
        # Null kind should be first (NULLS FIRST), then ascending
        assert join_rows[0][kind_idx] is None
        assert join_rows[1][kind_idx] == "a_kind"
        assert join_rows[2][kind_idx] == "z_kind"

    def test_duplicate_intervals_produce_identical_rows(self, tmp_path: Path) -> None:
        """Byte-identical duplicate intervals produce byte-identical event rows."""
        mem_rows = [
            (FORK_PATH, "r1", 100, 200),
            (FORK_PATH, "r1", 100, 200),  # exact duplicate
        ]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_membership_sql(emit_dir, "queue", "waiters", [])
        # Expect 4 rows: 2 joins + 2 leaves (all identical pairs)
        assert len(rows) == 4
        op_idx = MEMBERSHIP_EVENT_COLUMNS.index("op")
        join_rows = [r for r in rows if r[op_idx] == "join"]
        leave_rows = [r for r in rows if r[op_idx] == "leave"]
        assert len(join_rows) == 2
        assert len(leave_rows) == 2
        assert join_rows[0] == join_rows[1]
        assert leave_rows[0] == leave_rows[1]

    def test_second_fork_path_excluded(self, tmp_path: Path) -> None:
        """Only the specified fork_path's rows appear; other fork_path is excluded."""
        mem_rows = [
            (FORK_PATH, "r1", 100, None),
            ("other_branch", "r2", 200, None),
        ]
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, mem_rows)
        rows = _run_membership_sql(emit_dir, "queue", "waiters", [])
        rec_ids = {r[MEMBERSHIP_EVENT_COLUMNS.index("record_id")] for r in rows}
        assert "r1" in rec_ids
        assert "r2" not in rec_ids

    def test_raises_table_not_found_when_absent(self, tmp_path: Path) -> None:
        """TableNotFoundError when the membership table is absent."""
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_EMPTY, [])
        with open_emit(emit_dir) as emit:
            with pytest.raises(TableNotFoundError):
                build_membership_events_sql(
                    emit.sidecar, FORK_PATH, "queue", "no_table", []
                )

    def test_raises_export_error_on_unknown_field(self, tmp_path: Path) -> None:
        """ExportError when a selected field has no elem__/member__ column."""
        emit_dir = _build_emit(tmp_path, "queue", "waiters", _MEM_COLS_SCALAR, [])
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="resolves to neither"):
                build_membership_events_sql(
                    emit.sidecar, FORK_PATH, "queue", "waiters", ["no_such_field"]
                )
