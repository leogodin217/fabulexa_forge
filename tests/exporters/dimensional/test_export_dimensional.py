"""Tests for export_dimensional: the dispatcher that wires build_query_specs to writers.

Verifies: CSV dispatch (one file per table), DuckDB dispatch (one file),
row counts for every table (including 0-row tables), idempotent re-run
(identical row counts and CSV bytes), ExportRuntimeError propagation.
"""

from __future__ import annotations

import csv
from pathlib import Path

import duckdb
import pytest
from _support.sidecar_builder import write_emit

from exporters._emit_fixtures import _create_ddl, _table_spec
from fabulexa_forge.config.models import (
    ColumnDecl,
    DerivedSpec,
    DimensionalConfig,
    ExportConfig,
    FkClause,
    OrdinalSpec,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.errors import ExportRuntimeError
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.reader.emit import open_emit

# ---------------------------------------------------------------------------
# Emit fixture for export_dimensional tests
# ---------------------------------------------------------------------------

_ACTOR_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR", "history_tracked": False},
    {"name": "record_id", "type": "VARCHAR", "history_tracked": False},
    {"name": "active", "type": "BOOLEAN", "history_tracked": False},
    {"name": "deactivated_at", "type": "BIGINT", "history_tracked": False},
    {"name": "last_mutation_sim_time", "type": "BIGINT", "history_tracked": False},
    {"name": "prop__name", "type": "VARCHAR", "history_tracked": False},
    {"name": "prop__status", "type": "VARCHAR", "history_tracked": True},
]

_JOURNEY_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__actor_id", "type": "VARCHAR", "references": "actor"},
]

_HISTORY_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _build_export_emit(tmp_path: Path) -> Path:
    """Build a test emit with actor (SCD-2 capable) + journey (FK source) + history."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(_create_ddl("records__journey_instance", _JOURNEY_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    # Two actors
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a001", True, None, 100, "Alice", "active"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a002", True, None, 200, "Bob", "active"],
    )

    # Two journeys referencing actors
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "j001", True, 10, "a001"],
    )
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "j002", True, 20, "a002"],
    )

    # History: status changes for a001 at two sim_times
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "actor", "a001", "status", 10, "pending"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "actor", "a001", "status", 50, "active"],
    )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor",
                "records",
                _ACTOR_COLUMNS,
                2,
                record_kind="actor",
            ),
            _table_spec(
                "records__journey_instance",
                "records",
                _JOURNEY_COLUMNS,
                2,
                record_kind="journey_instance",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 2),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        extra={
            "enum_domains": {
                "journey_instance": {"entity_type": ["type_a", "type_b"]},
            }
        },
    )
    return tmp_path


def _make_export_config() -> ExportConfig:
    """Build a multi-table ExportConfig covering SCD-2, Type-1, FK fact, empty table."""
    dim_actor_scd2 = TableDecl(
        name="dim_actor",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="actor"),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="name", **{"from": "prop__name"}),
            ColumnDecl(name="status", **{"from": "prop__status"}),
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
            ColumnDecl(name="valid_to", derived=DerivedSpec(scd_window="valid_to")),
        ],
    )

    dim_journey = TableDecl(
        name="dim_journey",
        role="dim",
        scd="type1",
        source=SourceDecl(grain="records", kind="journey_instance"),
        key=["id"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="actor_id", **{"from": "prop__actor_id"}),
        ],
    )

    fact_journey_with_fk = TableDecl(
        name="fact_journey_event",
        role="fact",
        source=SourceDecl(grain="records", kind="journey_instance"),
        key=["journey_id"],
        columns=[
            ColumnDecl(name="journey_id", **{"from": "record_id"}),
            ColumnDecl(name="actor_raw_id", **{"from": "prop__actor_id"}),
            ColumnDecl(
                name="actor_fk",
                fk=FkClause(to="dim_actor", via="reference"),
            ),
            ColumnDecl(
                name="journey_seq",
                derived=DerivedSpec(
                    ordinal=OrdinalSpec(
                        partition_by="journey_id",
                        order_by="actor_raw_id",
                    )
                ),
            ),
        ],
    )

    # This table uses a nonexistent property value filter → zero rows
    fact_empty = TableDecl(
        name="fact_empty",
        role="fact",
        source=SourceDecl(
            grain="history_point",
            kind="actor",
            property="nonexistent_property",
        ),
        key=["id"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="val", **{"from": "value"}),
        ],
    )

    return ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(
            tables=[dim_actor_scd2, dim_journey, fact_journey_with_fk, fact_empty]
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_export_dimensional_csv_writes_one_file_per_table(tmp_path: Path) -> None:
    """export_dimensional(fmt='csv') writes one .csv per declared table."""
    emit_dir = _build_export_emit(tmp_path / "emit")
    out_dir = tmp_path / "csv_out"
    out_dir.mkdir()
    config = _make_export_config()

    with open_emit(emit_dir) as emit:
        counts = export_dimensional(emit, config, out_dir, "csv", None)

    assert set(counts.keys()) == {
        "dim_actor",
        "dim_journey",
        "fact_journey_event",
        "fact_empty",
    }
    for table_name in counts:
        assert (out_dir / f"{table_name}.csv").exists()


def test_export_dimensional_csv_empty_table_is_header_only(tmp_path: Path) -> None:
    """An empty-grain table produces a header-only CSV, not a missing file."""
    emit_dir = _build_export_emit(tmp_path / "emit")
    out_dir = tmp_path / "csv_out"
    out_dir.mkdir()
    config = _make_export_config()

    with open_emit(emit_dir) as emit:
        counts = export_dimensional(emit, config, out_dir, "csv", None)

    assert counts["fact_empty"] == 0
    csv_path = out_dir / "fact_empty.csv"
    assert csv_path.exists()
    rows = list(csv.reader(csv_path.read_text(encoding="utf-8").splitlines()))
    assert len(rows) == 1  # header only
    assert rows[0] != []  # header has column names


def test_export_dimensional_duckdb_writes_all_tables(tmp_path: Path) -> None:
    """export_dimensional(fmt='duckdb') writes every declared table to one file."""
    emit_dir = _build_export_emit(tmp_path / "emit")
    out_path = tmp_path / "out.duckdb"
    config = _make_export_config()

    with open_emit(emit_dir) as emit:
        counts = export_dimensional(emit, config, out_path, "duckdb", None)

    assert set(counts.keys()) == {
        "dim_actor",
        "dim_journey",
        "fact_journey_event",
        "fact_empty",
    }
    assert out_path.exists()
    out_conn = duckdb.connect(str(out_path), read_only=True)
    tables = {row[0] for row in out_conn.execute("SHOW TABLES").fetchall()}
    out_conn.close()
    assert {"dim_actor", "dim_journey", "fact_journey_event", "fact_empty"}.issubset(
        tables
    )


def test_export_dimensional_duckdb_empty_table_typed_not_dropped(
    tmp_path: Path,
) -> None:
    """An empty-grain DuckDB table is an empty typed table, not dropped."""
    emit_dir = _build_export_emit(tmp_path / "emit")
    out_path = tmp_path / "out.duckdb"
    config = _make_export_config()

    with open_emit(emit_dir) as emit:
        counts = export_dimensional(emit, config, out_path, "duckdb", None)

    assert counts["fact_empty"] == 0
    out_conn = duckdb.connect(str(out_path), read_only=True)
    schema = out_conn.execute("DESCRIBE fact_empty").fetchall()
    out_conn.close()
    assert len(schema) > 0


def test_export_dimensional_idempotent_csv(tmp_path: Path) -> None:
    """Exporting twice to CSV yields identical row counts and identical bytes."""
    emit_dir = _build_export_emit(tmp_path / "emit")
    config = _make_export_config()

    out_dir1 = tmp_path / "run1"
    out_dir1.mkdir()
    out_dir2 = tmp_path / "run2"
    out_dir2.mkdir()

    with open_emit(emit_dir) as emit:
        counts1 = export_dimensional(emit, config, out_dir1, "csv", None)
    with open_emit(emit_dir) as emit:
        counts2 = export_dimensional(emit, config, out_dir2, "csv", None)

    assert counts1 == counts2
    for table_name in counts1:
        f1 = (out_dir1 / f"{table_name}.csv").read_bytes()
        f2 = (out_dir2 / f"{table_name}.csv").read_bytes()
        assert f1 == f2, f"CSV bytes differ for {table_name}"


def test_export_dimensional_idempotent_duckdb_row_counts(tmp_path: Path) -> None:
    """Exporting twice to DuckDB yields identical row counts."""
    emit_dir = _build_export_emit(tmp_path / "emit")
    config = _make_export_config()

    out1 = tmp_path / "out1.duckdb"
    out2 = tmp_path / "out2.duckdb"

    with open_emit(emit_dir) as emit:
        counts1 = export_dimensional(emit, config, out1, "duckdb", None)
    with open_emit(emit_dir) as emit:
        counts2 = export_dimensional(emit, config, out2, "duckdb", None)

    assert counts1 == counts2


def test_export_dimensional_writer_failure_raises_export_runtime_error(
    tmp_path: Path,
) -> None:
    """A writer failure surfaces as ExportRuntimeError."""
    emit_dir = _build_export_emit(tmp_path / "emit")
    bad_path = tmp_path / "no_parent" / "sub" / "out.duckdb"
    config = _make_export_config()

    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportRuntimeError):
            export_dimensional(emit, config, bad_path, "duckdb", None)
