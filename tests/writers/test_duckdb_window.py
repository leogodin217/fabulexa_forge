"""Tests for write_duckdb_window.

Verifies:
- Fresh file: tables created, views installed, _export_meta written, _export_windows row
- Second window: facts append; type-1 dim replaced; SCD-2 __rows appends; view closes prior version
- Atomicity: failing spec mid-window rolls back; ExportRuntimeError raised
- fingerprint=None (range path): no _export_meta, no _export_windows; author tables + views only
- Empty window: zero-row appends, snapshot replace, window row still logged
- Returned row counts: per-table rows written this window; snapshot dims report full snapshot count
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column, write_emit

from fabulexa_forge.config.models import (
    ColumnDecl,
    DerivedSpec,
    DimensionalConfig,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.errors import ExportRuntimeError
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.query_spec import QuerySpec
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.writers.duckdb import write_duckdb_window

# ---------------------------------------------------------------------------
# Shared column declarations
# ---------------------------------------------------------------------------

_ENTITY_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT", "history_tracked": False},
    {"name": "active", "type": "BOOLEAN", "history_tracked": False},
    {"name": "deactivated_at", "type": "BIGINT", "history_tracked": False},
    {"name": "last_mutation_sim_time", "type": "BIGINT", "history_tracked": False},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


# ---------------------------------------------------------------------------
# Emit builders
# ---------------------------------------------------------------------------


def _build_scd2_emit(tmp_path: Path) -> Path:
    """Build an emit with one actor (a001) having status changes at sim_time 10, 20, 30."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    col_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _ENTITY_COLUMNS)
    conn.execute(f'CREATE TABLE "records__actor" ({col_ddl})')
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a001", 10, True, None, 30, 0, "Alice", "discharged"],
    )

    hist_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _HISTORY_COLUMNS)
    conn.execute(f'CREATE TABLE "history" ({hist_ddl})')
    for sim_time, state in [
        (10, "admitted"),
        (20, "under_treatment"),
        (30, "discharged"),
    ]:
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "actor", "a001", "status", sim_time, state],
        )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__actor",
                "category": "records",
                "columns": _ENTITY_COLUMNS,
                "rows": 1,
                "record_kind": "actor",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 3,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )
    return tmp_path


def _build_fact_emit(tmp_path: Path) -> Path:
    """Build an emit with entities at different sim_times for fact/dim split tests.

    Entities:
      - e001: last_mutation_sim_time=10
      - e002: last_mutation_sim_time=20
      - e003: last_mutation_sim_time=30
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    col_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _ENTITY_COLUMNS)
    conn.execute(f'CREATE TABLE "records__entity" ({col_ddl})')
    for record_index, (rec_id, sim_time, name, status) in enumerate(
        [
            ("e001", 10, "Alice", "active"),
            ("e002", 20, "Bob", "active"),
            ("e003", 30, "Carol", "active"),
        ]
    ):
        conn.execute(
            'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            [
                "trunk",
                rec_id,
                sim_time,
                True,
                None,
                sim_time,
                record_index,
                name,
                status,
            ],
        )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__entity",
                "category": "records",
                "columns": _ENTITY_COLUMNS,
                "rows": 3,
                "record_kind": "entity",
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------


def _scd2_config() -> DimensionalConfig:
    """SCD-2 dim config with valid_from and valid_to columns."""
    return DimensionalConfig(
        tables=[
            TableDecl(
                name="dim_actor",
                role="dim",
                scd="type2",
                source=SourceDecl(grain="records", kind="actor"),
                key=["id", "valid_from"],
                columns=[
                    ColumnDecl(name="id", **{"from": "record_id"}),
                    ColumnDecl(name="status", **{"from": "prop__status"}),
                    ColumnDecl(
                        name="valid_from", derived=DerivedSpec(scd_window="valid_from")
                    ),
                    ColumnDecl(
                        name="valid_to", derived=DerivedSpec(scd_window="valid_to")
                    ),
                ],
            )
        ]
    )


def _fact_dim_config() -> DimensionalConfig:
    """Fact (history_point) + type-1 dim (records) config."""
    return DimensionalConfig(
        tables=[
            TableDecl(
                name="dim_entity",
                role="dim",
                scd="type1",
                source=SourceDecl(grain="records", kind="entity"),
                key=["id"],
                columns=[
                    ColumnDecl(name="id", **{"from": "record_id"}),
                    ColumnDecl(name="name", **{"from": "prop__name"}),
                ],
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_window(start_ns: int, end_ns: int, index: int = 0) -> Window:
    return Window(index=index, start_ns=start_ns, end_ns=end_ns, label=f"w{index}")


def _read_table(db_path: Path, table: str) -> list[tuple[object, ...]]:
    conn = duckdb.connect(str(db_path), read_only=True)
    rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
    conn.close()
    return rows


def _table_exists_in_db(db_path: Path, table: str) -> bool:
    conn = duckdb.connect(str(db_path), read_only=True)
    rows = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()
    conn.close()
    return bool(rows and rows[0] > 0)


def _view_exists_in_db(db_path: Path, view: str) -> bool:
    conn = duckdb.connect(str(db_path), read_only=True)
    rows = conn.execute(
        "SELECT COUNT(*) FROM information_schema.views WHERE table_name = ?", [view]
    ).fetchone()
    conn.close()
    return bool(rows and rows[0] > 0)


def _row_count(db_path: Path, table: str) -> int:
    conn = duckdb.connect(str(db_path), read_only=True)
    rows = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
    conn.close()
    return int(rows[0]) if rows else 0


# ---------------------------------------------------------------------------
# Tests: fresh file
# ---------------------------------------------------------------------------


def test_fresh_file_creates_tables_and_meta(tmp_path: Path) -> None:
    """Fresh file: table created, _export_meta written with correct values."""
    emit_dir = _build_fact_emit(tmp_path)
    out_path = tmp_path / "warehouse.duckdb"
    window = _make_window(0, 15, index=0)

    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit, _fact_dim_config(), None, window, notice_sink=discard_notice_sink
        )
        write_duckdb_window(emit, specs, out_path, window, fingerprint="fp123")

    assert _table_exists_in_db(out_path, "dim_entity")
    meta = _read_table(out_path, "_export_meta")
    assert len(meta) == 1
    assert meta[0][0] == 1  # cursor_format_version
    assert meta[0][1] == "fp123"  # fingerprint


def test_fresh_file_export_windows_row_written(tmp_path: Path) -> None:
    """Fresh file: _export_windows contains the window's row."""
    emit_dir = _build_fact_emit(tmp_path)
    out_path = tmp_path / "warehouse.duckdb"
    window = _make_window(0, 15, index=0)

    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit, _fact_dim_config(), None, window, notice_sink=discard_notice_sink
        )
        write_duckdb_window(emit, specs, out_path, window, fingerprint="fp123")

    windows_rows = _read_table(out_path, "_export_windows")
    assert len(windows_rows) == 1
    row = windows_rows[0]
    assert row[0] == 0  # window_index
    assert row[1] == "w0"  # label
    assert row[2] == 0  # start_ns
    assert row[3] == 15  # end_ns


def test_fresh_file_views_installed(tmp_path: Path) -> None:
    """Fresh file: companion views are installed for SCD-2 dims."""
    emit_dir = _build_scd2_emit(tmp_path)
    out_path = tmp_path / "warehouse.duckdb"
    window = _make_window(0, 15, index=0)

    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit, _scd2_config(), None, window, notice_sink=discard_notice_sink
        )
        write_duckdb_window(emit, specs, out_path, window, fingerprint="fp123")

    assert _view_exists_in_db(out_path, "dim_actor")
    assert _table_exists_in_db(out_path, "dim_actor__rows")


# ---------------------------------------------------------------------------
# Tests: second window
# ---------------------------------------------------------------------------


def test_second_window_facts_append(tmp_path: Path) -> None:
    """Second window: type-1 dim rows are replaced (count reflects current snapshot)."""
    emit_dir = _build_fact_emit(tmp_path)
    out_path = tmp_path / "warehouse.duckdb"
    w0 = _make_window(0, 15, index=0)
    w1 = _make_window(15, 35, index=1)

    with open_emit(emit_dir) as emit:
        specs0 = build_query_specs(
            emit, _fact_dim_config(), None, w0, notice_sink=discard_notice_sink
        )
        write_duckdb_window(emit, specs0, out_path, w0, fingerprint="fp")
        specs1 = build_query_specs(
            emit, _fact_dim_config(), None, w1, notice_sink=discard_notice_sink
        )
        result = write_duckdb_window(emit, specs1, out_path, w1, fingerprint="fp")

    # type-1 dim is replaced: full snapshot count returned
    assert result["dim_entity"] == 3


def test_second_window_export_windows_gains_row(tmp_path: Path) -> None:
    """Second window: _export_windows gains one more row."""
    emit_dir = _build_fact_emit(tmp_path)
    out_path = tmp_path / "warehouse.duckdb"
    w0 = _make_window(0, 15, index=0)
    w1 = _make_window(15, 35, index=1)

    with open_emit(emit_dir) as emit:
        specs0 = build_query_specs(
            emit, _fact_dim_config(), None, w0, notice_sink=discard_notice_sink
        )
        write_duckdb_window(emit, specs0, out_path, w0, fingerprint="fp")
        specs1 = build_query_specs(
            emit, _fact_dim_config(), None, w1, notice_sink=discard_notice_sink
        )
        write_duckdb_window(emit, specs1, out_path, w1, fingerprint="fp")

    windows_rows = _read_table(out_path, "_export_windows")
    assert len(windows_rows) == 2


def test_second_window_scd2_rows_append(tmp_path: Path) -> None:
    """SCD-2 __rows table appends across windows."""
    emit_dir = _build_scd2_emit(tmp_path)
    out_path = tmp_path / "warehouse.duckdb"
    w0 = _make_window(0, 15, index=0)
    w1 = _make_window(15, 35, index=1)

    with open_emit(emit_dir) as emit:
        specs0 = build_query_specs(
            emit, _scd2_config(), None, w0, notice_sink=discard_notice_sink
        )
        result0 = write_duckdb_window(emit, specs0, out_path, w0, fingerprint="fp")
        specs1 = build_query_specs(
            emit, _scd2_config(), None, w1, notice_sink=discard_notice_sink
        )
        result1 = write_duckdb_window(emit, specs1, out_path, w1, fingerprint="fp")

    # w0 [0,15) catches change at sim_time=10 → 1 row
    assert result0["dim_actor__rows"] == 1
    # w1 [15,35) catches changes at sim_time=20,30 → 2 rows
    assert result1["dim_actor__rows"] == 2
    # total accumulated in table
    assert _row_count(out_path, "dim_actor__rows") == 3


def test_second_window_scd2_view_latest_version_null_valid_to(tmp_path: Path) -> None:
    """After two windows the SCD-2 view's latest version has valid_to IS NULL."""
    emit_dir = _build_scd2_emit(tmp_path)
    out_path = tmp_path / "warehouse.duckdb"
    w0 = _make_window(0, 15, index=0)
    w1 = _make_window(15, 35, index=1)

    with open_emit(emit_dir) as emit:
        specs0 = build_query_specs(
            emit, _scd2_config(), None, w0, notice_sink=discard_notice_sink
        )
        write_duckdb_window(emit, specs0, out_path, w0, fingerprint="fp")
        specs1 = build_query_specs(
            emit, _scd2_config(), None, w1, notice_sink=discard_notice_sink
        )
        write_duckdb_window(emit, specs1, out_path, w1, fingerprint="fp")

    conn = duckdb.connect(str(out_path), read_only=True)
    open_rows = conn.execute(
        'SELECT COUNT(*) FROM "dim_actor" WHERE valid_to IS NULL'
    ).fetchone()
    conn.close()
    assert open_rows is not None and open_rows[0] == 1


# ---------------------------------------------------------------------------
# Tests: atomicity (rollback on failure)
# ---------------------------------------------------------------------------


def test_atomicity_bad_sql_rolls_back(tmp_path: Path) -> None:
    """A failing spec mid-window triggers rollback; table contents unchanged."""
    emit_dir = _build_fact_emit(tmp_path)
    out_path = tmp_path / "warehouse.duckdb"
    w0 = _make_window(0, 15, index=0)

    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit, _fact_dim_config(), None, w0, notice_sink=discard_notice_sink
        )
        write_duckdb_window(emit, specs, out_path, w0, fingerprint="fp")

    # Capture pre-failure state
    rows_before = _row_count(out_path, "dim_entity")
    windows_before = _row_count(out_path, "_export_windows")

    # Inject a bad spec that will fail mid-transaction
    bad_spec = QuerySpec(
        table_name="dim_entity",
        sql="SELECT * FROM nonexistent_table_xyz",
        write_mode="append",
        view_name=None,
        view_sql=None,
    )
    w1 = _make_window(15, 35, index=1)

    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportRuntimeError):
            write_duckdb_window(emit, [bad_spec], out_path, w1, fingerprint="fp")

    # Warehouse must be unchanged
    assert _row_count(out_path, "dim_entity") == rows_before
    assert _row_count(out_path, "_export_windows") == windows_before


def test_connect_failure_raises_export_runtime_error(tmp_path: Path) -> None:
    """Failing to open the warehouse file itself (missing parent directory)
    raises ExportRuntimeError from the outer connect branch, before any
    transaction begins; no output file appears."""
    emit_dir = _build_fact_emit(tmp_path)
    bad_path = tmp_path / "nonexistent" / "deeply" / "warehouse.duckdb"
    w0 = _make_window(0, 15, index=0)

    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit, _fact_dim_config(), None, w0, notice_sink=discard_notice_sink
        )
        with pytest.raises(ExportRuntimeError, match="failed to open warehouse DuckDB"):
            write_duckdb_window(emit, specs, bad_path, w0, fingerprint="fp")

    assert not bad_path.exists()


# ---------------------------------------------------------------------------
# Tests: fingerprint=None (range path)
# ---------------------------------------------------------------------------


def test_range_path_no_meta_no_windows(tmp_path: Path) -> None:
    """fingerprint=None: no _export_meta or _export_windows written."""
    emit_dir = _build_fact_emit(tmp_path)
    out_path = tmp_path / "warehouse.duckdb"
    window = Window(index=None, start_ns=0, end_ns=35, label="range")

    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit, _fact_dim_config(), None, window, notice_sink=discard_notice_sink
        )
        write_duckdb_window(emit, specs, out_path, window, fingerprint=None)

    assert not _table_exists_in_db(out_path, "_export_meta")
    assert not _table_exists_in_db(out_path, "_export_windows")
    assert _table_exists_in_db(out_path, "dim_entity")


def test_range_path_author_tables_present(tmp_path: Path) -> None:
    """fingerprint=None: author-named tables are written correctly."""
    emit_dir = _build_scd2_emit(tmp_path)
    out_path = tmp_path / "warehouse.duckdb"
    window = Window(index=None, start_ns=0, end_ns=35, label="range")

    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit, _scd2_config(), None, window, notice_sink=discard_notice_sink
        )
        write_duckdb_window(emit, specs, out_path, window, fingerprint=None)

    assert _table_exists_in_db(out_path, "dim_actor__rows")
    assert _view_exists_in_db(out_path, "dim_actor")


# ---------------------------------------------------------------------------
# Tests: empty window
# ---------------------------------------------------------------------------


def test_empty_window_still_logs_window_row(tmp_path: Path) -> None:
    """Empty window (no rows match): window row still inserted in _export_windows."""
    emit_dir = _build_fact_emit(tmp_path)
    out_path = tmp_path / "warehouse.duckdb"
    # First window to create the file
    w0 = _make_window(0, 15, index=0)
    # Second window with no matching rows (beyond all data)
    w1 = _make_window(100, 200, index=1)

    with open_emit(emit_dir) as emit:
        specs0 = build_query_specs(
            emit, _fact_dim_config(), None, w0, notice_sink=discard_notice_sink
        )
        write_duckdb_window(emit, specs0, out_path, w0, fingerprint="fp")
        specs1 = build_query_specs(
            emit, _fact_dim_config(), None, w1, notice_sink=discard_notice_sink
        )
        result = write_duckdb_window(emit, specs1, out_path, w1, fingerprint="fp")

    # window row must be logged even for empty window
    assert _row_count(out_path, "_export_windows") == 2
    # type-1 dim returns snapshot count even when no new rows in window
    assert "dim_entity" in result


# ---------------------------------------------------------------------------
# Tests: returned row counts
# ---------------------------------------------------------------------------


def test_snapshot_dim_reports_full_snapshot_count(tmp_path: Path) -> None:
    """Type-1 dim (replace mode) returns the full snapshot row count each window."""
    emit_dir = _build_fact_emit(tmp_path)
    out_path = tmp_path / "warehouse.duckdb"
    w0 = _make_window(0, 15, index=0)
    w1 = _make_window(15, 35, index=1)

    with open_emit(emit_dir) as emit:
        specs0 = build_query_specs(
            emit, _fact_dim_config(), None, w0, notice_sink=discard_notice_sink
        )
        write_duckdb_window(emit, specs0, out_path, w0, fingerprint="fp")
        specs1 = build_query_specs(
            emit, _fact_dim_config(), None, w1, notice_sink=discard_notice_sink
        )
        result1 = write_duckdb_window(emit, specs1, out_path, w1, fingerprint="fp")

    # After 2 windows, snapshot should show 3 total entities
    assert result1["dim_entity"] == 3
