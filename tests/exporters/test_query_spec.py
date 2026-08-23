"""Tests for the shared exporter shape (`exporters/query_spec.py`):
`query_spec_output_name` and the `write_query_specs` dispatch, including its
DuckDB-arm key flattening.
"""

from __future__ import annotations

from pathlib import Path

from _support.duckdb_introspect import constraint_types

from exporters._emit_fixtures import build_test_emit
from fabulexa_forge.exporters.query_spec import QuerySpec, TableKeys, write_query_specs
from fabulexa_forge.reader.emit import open_emit


def test_write_query_specs_duckdb_arm_lands_keyed_constraints(tmp_path: Path) -> None:
    """A keyed spec's constraints land in the DuckDB output via the shared
    dispatch's flattening of `spec.keys` into `write_duckdb`'s `keys` mapping."""
    emit_dir = build_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"

    specs = [
        QuerySpec(
            table_name="dim_entity",
            sql='SELECT record_id FROM "records__entity" ORDER BY record_id',
            write_mode="create",
            view_name=None,
            view_sql=None,
            keys=TableKeys(primary_key=("record_id",), unique=()),
        ),
        QuerySpec(
            table_name="dim_history",
            sql='SELECT record_id FROM "history" ORDER BY record_id',
            write_mode="create",
            view_name=None,
            view_sql=None,
        ),
    ]

    with open_emit(emit_dir) as emit:
        report = write_query_specs(emit, specs, out_path, "duckdb")

    row_counts = {table.name: table.row_count for table in report.tables}
    assert row_counts == {"dim_entity": 2, "dim_history": 3}
    assert "PRIMARY KEY" in constraint_types(out_path, "dim_entity")
    assert constraint_types(out_path, "dim_history") == []


def test_write_query_specs_csv_arm_ignores_keys(tmp_path: Path) -> None:
    """The CSV arm writes data unaffected by `spec.keys` — no constraint
    surface to declare, no notice emitted at this layer."""
    emit_dir = build_test_emit(tmp_path)
    out_dir = tmp_path / "csv_out"
    out_dir.mkdir()

    specs = [
        QuerySpec(
            table_name="dim_entity",
            sql='SELECT record_id FROM "records__entity" ORDER BY record_id',
            write_mode="create",
            view_name=None,
            view_sql=None,
            keys=TableKeys(primary_key=("record_id",), unique=()),
        ),
    ]

    with open_emit(emit_dir) as emit:
        report = write_query_specs(emit, specs, out_dir, "csv")

    row_counts = {table.name: table.row_count for table in report.tables}
    assert row_counts == {"dim_entity": 2}
    assert (out_dir / "dim_entity.csv").exists()
