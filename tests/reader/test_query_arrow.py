"""Tests for Emit.query_arrow."""

from __future__ import annotations

from pathlib import Path

import pytest

from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import RunDatabaseError

from ._emit_helpers import write_emit


def _emit_with_data(tmp_path: Path) -> Path:
    """Write a minimal emit with a records__thing table containing one row."""
    from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [
            {
                "name": "firings",
                "category": "fixed",
                "columns": [
                    {"name": "fork_path", "type": "VARCHAR"},
                    {"name": "sim_time", "type": "BIGINT"},
                ],
                "rows": 0,
            },
            {
                "name": "records__thing",
                "category": "records",
                "record_kind": "thing",
                "columns": [
                    {"name": "fork_path", "type": "VARCHAR"},
                    {"name": "record_id", "type": "VARCHAR"},
                    {"name": "prop__name", "type": "VARCHAR"},
                ],
                "rows": 1,
            },
        ],
    }
    db_tables = {
        "firings": "CREATE TABLE firings (fork_path VARCHAR, sim_time BIGINT)",
        "records__thing": (
            "CREATE TABLE records__thing AS "
            "SELECT 'trunk' AS fork_path, 'id1' AS record_id, 'alpha' AS prop__name"
        ),
    }
    return write_emit(tmp_path, sidecar=sidecar, db_tables=db_tables)


def test_query_arrow_returns_pyarrow_table(tmp_path: Path) -> None:
    """query_arrow returns a pyarrow.Table with the expected rows."""
    import pyarrow as pa

    emit_dir = _emit_with_data(tmp_path)
    with open_emit(emit_dir) as emit:
        table = emit.query_arrow(
            "SELECT fork_path, prop__name FROM records__thing ORDER BY record_id",
            (),
        )
    assert isinstance(table, pa.Table)
    assert table.num_rows == 1
    assert table.column("prop__name")[0].as_py() == "alpha"


def test_query_arrow_zero_rows_preserves_typed_schema(tmp_path: Path) -> None:
    """A zero-row SELECT still carries the typed schema (not object columns)."""
    import pyarrow as pa

    emit_dir = _emit_with_data(tmp_path)
    with open_emit(emit_dir) as emit:
        table = emit.query_arrow(
            "SELECT fork_path, prop__name FROM records__thing WHERE 1=0",
            (),
        )
    assert isinstance(table, pa.Table)
    assert table.num_rows == 0
    # Columns must be typed — DuckDB VARCHAR maps to pa.large_utf8() or pa.string()
    assert pa.types.is_string(
        table.schema.field("fork_path").type
    ) or pa.types.is_large_string(table.schema.field("fork_path").type)


def test_query_arrow_cast_null_is_typed(tmp_path: Path) -> None:
    """A CAST(NULL AS VARCHAR) column arrives typed, not as an untyped-object column."""
    import pyarrow as pa

    emit_dir = _emit_with_data(tmp_path)
    with open_emit(emit_dir) as emit:
        table = emit.query_arrow(
            "SELECT CAST(NULL AS VARCHAR) AS nullcol FROM records__thing",
            (),
        )
    assert isinstance(table, pa.Table)
    field = table.schema.field("nullcol")
    # Must be a string type, not null type or object
    assert pa.types.is_string(field.type) or pa.types.is_large_string(field.type), (
        f"expected string type, got {field.type}"
    )


def test_query_arrow_non_read_statement_raises(tmp_path: Path) -> None:
    """A non-read statement (DML/DDL) raises RunDatabaseError."""
    emit_dir = write_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        with pytest.raises(RunDatabaseError):
            emit.query_arrow("CREATE TABLE x (a INTEGER)", ())


def test_query_arrow_after_close_raises_run_database_error(tmp_path: Path) -> None:
    """query_arrow() on a closed Emit raises RunDatabaseError, not a bare duckdb error."""
    emit_dir = _emit_with_data(tmp_path)
    emit = open_emit(emit_dir)
    emit.close()
    with pytest.raises(RunDatabaseError, match="query_arrow failed"):
        emit.query_arrow("SELECT prop__name FROM records__thing", ())


def test_query_arrow_row_order_matches_query(tmp_path: Path) -> None:
    """query_arrow respects the ORDER BY of the SQL."""
    import pyarrow as pa

    emit_dir = _emit_with_data(tmp_path)
    with open_emit(emit_dir) as emit:
        table = emit.query_arrow(
            "SELECT prop__name FROM records__thing ORDER BY prop__name DESC",
            (),
        )
    assert isinstance(table, pa.Table)
    assert table.num_rows == 1
    assert table.column("prop__name")[0].as_py() == "alpha"
