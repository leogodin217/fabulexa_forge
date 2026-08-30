"""Tests for derivations.truncated_tape.

Covers build_truncated_history_sql, build_truncated_membership_sql, and
build_truncated_sidecar — the truncated-tape surface minus the records
builder (Phase 4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
from _support.sidecar_builder import (
    enum_options,
    identity_column,
    prop_column,
    write_emit,
)

from fabulexa_forge.derivations.truncated_tape import (
    build_truncated_history_sql,
    build_truncated_membership_sql,
    build_truncated_sidecar,
)
from fabulexa_forge.reader.emit import Emit, open_emit
from fabulexa_forge.reader.errors import TableNotFoundError

FORK_PATH = "trunk"
OTHER_FORK_PATH = "trunk@branch_a"

# ---------------------------------------------------------------------------
# Column shapes
# ---------------------------------------------------------------------------

_HISTORY_COLS: list[dict[str, Any]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

_MEM_COLS: list[dict[str, Any]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__priority", "type": "VARCHAR"},
]

# A sub-typed kind ("widget"): a slice_only discriminator (exempt), a tracked
# property, and a non-exempt slice_only property.
_WIDGET_COLS: list[dict[str, Any]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__widget_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="slice_only",
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__note", "VARCHAR", history_tracked=False, temporal_class="slice_only"
    ),
]

# A non-sub-typed kind ("gadget"): identity/lifecycle only, one tracked prop.
_GADGET_COLS: list[dict[str, Any]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__label", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
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


def _build_full_emit(
    tmp_path: Path,
    *,
    history_rows: list[tuple[Any, ...]],
    mem_rows: list[tuple[Any, ...]],
    widget_rows: list[tuple[Any, ...]],
    gadget_rows: list[tuple[Any, ...]],
) -> Path:
    """Build a minimal emit carrying history, one membership table, and two
    records kinds — one sub-typed ("widget"), one not ("gadget")."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("history", _HISTORY_COLS))
    conn.execute(_ddl("membership__queue__waiters", _MEM_COLS))
    conn.execute(_ddl("records__widget", _WIDGET_COLS))
    conn.execute(_ddl("records__gadget", _GADGET_COLS))

    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    mem_ph = ", ".join("?" for _ in _MEM_COLS)
    for row in mem_rows:
        conn.execute(
            f'INSERT INTO "membership__queue__waiters" VALUES ({mem_ph})', list(row)
        )
    widget_ph = ", ".join("?" for _ in _WIDGET_COLS)
    for row in widget_rows:
        conn.execute(f'INSERT INTO "records__widget" VALUES ({widget_ph})', list(row))
    gadget_ph = ", ".join("?" for _ in _GADGET_COLS)
    for row in gadget_rows:
        conn.execute(f'INSERT INTO "records__gadget" VALUES ({gadget_ph})', list(row))
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec("history", "fixed", _HISTORY_COLS, len(history_rows)),
            _table_spec(
                "membership__queue__waiters",
                "membership",
                _MEM_COLS,
                len(mem_rows),
                record_kind="queue",
                property_name="waiters",
            ),
            _table_spec(
                "records__widget",
                "records",
                _WIDGET_COLS,
                len(widget_rows),
                record_kind="widget",
            ),
            _table_spec(
                "records__gadget",
                "records",
                _GADGET_COLS,
                len(gadget_rows),
                record_kind="gadget",
            ),
        ],
        branches=[
            {"fork_path": FORK_PATH, "parent": None, "slice_at": 9999},
        ],
        extra={
            "enum_domains": {"widget": {"widget_type": enum_options("alpha", "beta")}},
            "pinned_ids": {"widget": {"first": "w1"}},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2020-01-01T00:00:00+00:00",
            },
            "record_roles": {"widget": "dimension", "gadget": "dimension"},
        },
    )
    return tmp_path


# ---------------------------------------------------------------------------
# build_truncated_history_sql
# ---------------------------------------------------------------------------


class TestBuildTruncatedHistorySql:
    """Tests for build_truncated_history_sql."""

    def test_rows_at_or_before_t_only(self, tmp_path: Path) -> None:
        """Only sim_time <= T rows are returned."""
        history_rows = [
            (FORK_PATH, "widget", "w1", "status", 50, "on"),
            (FORK_PATH, "widget", "w1", "status", 100, "off"),
            (FORK_PATH, "widget", "w1", "status", 150, "on"),
        ]
        emit_dir = _build_full_emit(
            tmp_path,
            history_rows=history_rows,
            mem_rows=[],
            widget_rows=[],
            gadget_rows=[],
        )
        with open_emit(emit_dir) as emit:
            sql = build_truncated_history_sql(FORK_PATH, 100)
            rows = emit.query(sql, ())
        assert sorted(r[4] for r in rows) == [50, 100]

    def test_fork_path_filtered(self, tmp_path: Path) -> None:
        """Only the specified fork_path's rows appear."""
        history_rows = [
            (FORK_PATH, "widget", "w1", "status", 50, "on"),
            (OTHER_FORK_PATH, "widget", "w2", "status", 50, "on"),
        ]
        emit_dir = _build_full_emit(
            tmp_path,
            history_rows=history_rows,
            mem_rows=[],
            widget_rows=[],
            gadget_rows=[],
        )
        with open_emit(emit_dir) as emit:
            sql = build_truncated_history_sql(FORK_PATH, 1000)
            rows = emit.query(sql, ())
        assert [r[2] for r in rows] == ["w1"]

    def test_column_shape_verbatim(self, tmp_path: Path) -> None:
        """Every history column is present, in the physical table's order."""
        history_rows = [(FORK_PATH, "widget", "w1", "status", 50, "on")]
        emit_dir = _build_full_emit(
            tmp_path,
            history_rows=history_rows,
            mem_rows=[],
            widget_rows=[],
            gadget_rows=[],
        )
        with open_emit(emit_dir) as emit:
            sql = build_truncated_history_sql(FORK_PATH, 1000)
            rows = emit.query(sql, ())
        assert rows == [(FORK_PATH, "widget", "w1", "status", 50, "on")]


# ---------------------------------------------------------------------------
# build_truncated_membership_sql
# ---------------------------------------------------------------------------


class TestBuildTruncatedMembershipSql:
    """Tests for build_truncated_membership_sql."""

    def test_joined_after_t_excluded(self, tmp_path: Path) -> None:
        """An interval whose joined_sim_time > T is excluded entirely."""
        mem_rows = [(FORK_PATH, "r1", 150, None, "high")]
        emit_dir = _build_full_emit(
            tmp_path, history_rows=[], mem_rows=mem_rows, widget_rows=[], gadget_rows=[]
        )
        with open_emit(emit_dir) as emit:
            sql = build_truncated_membership_sql(
                emit.sidecar, FORK_PATH, "queue", "waiters", 100
            )
            rows = emit.query(sql, ())
        assert rows == []

    def test_left_after_t_masked_null(self, tmp_path: Path) -> None:
        """left_sim_time > T is masked NULL — the interval is still open at T,
        exactly as a slice-at-T emit renders it — while every other column,
        including the physical joined_sim_time, is verbatim."""
        mem_rows = [(FORK_PATH, "r1", 50, 150, "high")]
        emit_dir = _build_full_emit(
            tmp_path, history_rows=[], mem_rows=mem_rows, widget_rows=[], gadget_rows=[]
        )
        with open_emit(emit_dir) as emit:
            sql = build_truncated_membership_sql(
                emit.sidecar, FORK_PATH, "queue", "waiters", 100
            )
            rows = emit.query(sql, ())
        assert len(rows) == 1
        assert rows[0] == (FORK_PATH, "r1", 50, None, "high")

    def test_left_at_or_before_t_kept_verbatim(self, tmp_path: Path) -> None:
        """left_sim_time <= T is projected verbatim, unmasked."""
        mem_rows = [(FORK_PATH, "r1", 50, 80, "high")]
        emit_dir = _build_full_emit(
            tmp_path, history_rows=[], mem_rows=mem_rows, widget_rows=[], gadget_rows=[]
        )
        with open_emit(emit_dir) as emit:
            sql = build_truncated_membership_sql(
                emit.sidecar, FORK_PATH, "queue", "waiters", 100
            )
            rows = emit.query(sql, ())
        assert rows == [(FORK_PATH, "r1", 50, 80, "high")]

    def test_open_interval_left_null_stays_null(self, tmp_path: Path) -> None:
        """A physically-open interval (left_sim_time NULL) is included and
        stays NULL."""
        mem_rows = [(FORK_PATH, "r1", 50, None, "high")]
        emit_dir = _build_full_emit(
            tmp_path, history_rows=[], mem_rows=mem_rows, widget_rows=[], gadget_rows=[]
        )
        with open_emit(emit_dir) as emit:
            sql = build_truncated_membership_sql(
                emit.sidecar, FORK_PATH, "queue", "waiters", 100
            )
            rows = emit.query(sql, ())
        assert rows == [(FORK_PATH, "r1", 50, None, "high")]

    def test_fork_path_filtered(self, tmp_path: Path) -> None:
        """Only the specified fork_path's rows appear."""
        mem_rows = [
            (FORK_PATH, "r1", 50, None, "high"),
            (OTHER_FORK_PATH, "r2", 50, None, "high"),
        ]
        emit_dir = _build_full_emit(
            tmp_path, history_rows=[], mem_rows=mem_rows, widget_rows=[], gadget_rows=[]
        )
        with open_emit(emit_dir) as emit:
            sql = build_truncated_membership_sql(
                emit.sidecar, FORK_PATH, "queue", "waiters", 100
            )
            rows = emit.query(sql, ())
        assert [r[1] for r in rows] == ["r1"]

    def test_raises_table_not_found_when_absent(self, tmp_path: Path) -> None:
        """TableNotFoundError when the membership table is absent."""
        emit_dir = _build_full_emit(
            tmp_path, history_rows=[], mem_rows=[], widget_rows=[], gadget_rows=[]
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(TableNotFoundError):
                build_truncated_membership_sql(
                    emit.sidecar, FORK_PATH, "queue", "no_such_property", 100
                )


# ---------------------------------------------------------------------------
# build_truncated_sidecar
# ---------------------------------------------------------------------------


class TestBuildTruncatedSidecar:
    """Tests for build_truncated_sidecar."""

    def _open(self, tmp_path: Path) -> Path:
        return _build_full_emit(
            tmp_path, history_rows=[], mem_rows=[], widget_rows=[], gadget_rows=[]
        )

    def test_drops_non_exempt_slice_only_and_keeps_discriminator(
        self, tmp_path: Path
    ) -> None:
        """A sub-typed kind's non-exempt slice_only column is dropped; its
        slice_only discriminator column is kept; last_mutation_sim_time and
        the tracked property stay declared."""
        emit_dir = self._open(tmp_path)
        with open_emit(emit_dir) as emit:
            truncated = build_truncated_sidecar(emit.sidecar)
            physical_names = [c.name for c in emit.sidecar.columns("records__widget")]
            truncated_names = [c.name for c in truncated.columns("records__widget")]

        assert "prop__note" in physical_names
        assert "prop__note" not in truncated_names
        assert "prop__widget_type" in truncated_names
        assert "prop__status" in truncated_names
        assert "last_mutation_sim_time" in truncated_names
        # Every dropped column is exactly the non-exempt slice_only set.
        assert set(physical_names) - set(truncated_names) == {"prop__note"}

    def test_non_subtyped_kind_drops_its_slice_only_columns_too(
        self, tmp_path: Path
    ) -> None:
        """A non-sub-typed kind still loses non-exempt slice_only columns —
        this fixture's gadget carries none, so its column list is unchanged."""
        emit_dir = self._open(tmp_path)
        with open_emit(emit_dir) as emit:
            truncated = build_truncated_sidecar(emit.sidecar)
            physical = emit.sidecar.columns("records__gadget")
            truncated_cols = truncated.columns("records__gadget")
        assert truncated_cols == physical

    def test_other_table_entries_unchanged(self, tmp_path: Path) -> None:
        """history and membership table entries pass through unchanged."""
        emit_dir = self._open(tmp_path)
        with open_emit(emit_dir) as emit:
            truncated = build_truncated_sidecar(emit.sidecar)
            assert truncated.table("history") == emit.sidecar.table("history")
            assert truncated.table("membership__queue__waiters") == emit.sidecar.table(
                "membership__queue__waiters"
            )

    def test_other_sidecar_fields_unchanged(self, tmp_path: Path) -> None:
        """branches (slice bound included), runtime, pinned_ids, enum_domains,
        and record_roles are all carried through verbatim."""
        emit_dir = self._open(tmp_path)
        with open_emit(emit_dir) as emit:
            truncated = build_truncated_sidecar(emit.sidecar)
            assert truncated.branches() == emit.sidecar.branches()
            assert truncated.runtime() == emit.sidecar.runtime()
            assert truncated.pinned_ids() == emit.sidecar.pinned_ids()
            assert truncated.enum_domains() == emit.sidecar.enum_domains()
            assert truncated.record_roles() is not None
            assert (
                emit.sidecar.record_roles() is not None
                and truncated.record_roles().kinds()  # type: ignore[union-attr]
                == emit.sidecar.record_roles().kinds()  # type: ignore[union-attr]
            )

    def test_pure_and_t_independent(self, tmp_path: Path) -> None:
        """Calling build_truncated_sidecar repeatedly yields the same dropped
        column set — the view takes no T parameter."""
        emit_dir = self._open(tmp_path)
        with open_emit(emit_dir) as emit:
            first = build_truncated_sidecar(emit.sidecar)
            second = build_truncated_sidecar(emit.sidecar)
        assert first.columns("records__widget") == second.columns("records__widget")

    def test_composes_with_public_emit_constructor(self, tmp_path: Path) -> None:
        """The returned Sidecar is a valid drop-in for the public Emit
        constructor over an open connection: its declared kept-column list is
        queryable against the physical table."""
        emit_dir = self._open(tmp_path)
        with open_emit(emit_dir) as emit:
            truncated = build_truncated_sidecar(emit.sidecar)

        conn = duckdb.connect(str(emit_dir / "run.duckdb"), read_only=True)
        truncated_emit = Emit(sidecar=truncated, emit_dir=emit_dir, conn=conn)
        try:
            kept_cols = [
                c.name for c in truncated_emit.sidecar.columns("records__widget")
            ]
            col_list = ", ".join(f'"{c}"' for c in kept_cols)
            rows = truncated_emit.query(f'SELECT {col_list} FROM "records__widget"', ())
            assert rows == []  # no widget rows in this fixture; the query succeeds
        finally:
            truncated_emit.close()
