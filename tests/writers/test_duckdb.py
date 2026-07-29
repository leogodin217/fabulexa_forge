"""Tests for write_duckdb.

Verifies: Arrow-path materialization, input emit read-only untouched, zero-row
yields empty typed table (not dropped), NULL-pad column typed correctly, return
value is {table: row_count}, ExportRuntimeError on failure, keyed tables get
explicit-DDL PRIMARY KEY / UNIQUE constraints.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from _support.duckdb_introspect import constraint_types

from exporters._emit_fixtures import build_test_emit
from fabulexa_forge.errors import ExportRuntimeError
from fabulexa_forge.exporters.query_spec import TableKeys
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.writers.duckdb import write_duckdb


def test_write_duckdb_materializes_rows(tmp_path: Path) -> None:
    """write_duckdb writes each query's rows to the output DuckDB."""
    emit_dir = build_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sql = 'SELECT fork_path, record_id FROM "records__entity" ORDER BY record_id'
        result = write_duckdb(emit, {"dim_entity": sql}, out_path, {})

    assert result == {"dim_entity": 2}
    out_conn = duckdb.connect(str(out_path), read_only=True)
    rows = out_conn.execute(
        "SELECT record_id FROM dim_entity ORDER BY record_id"
    ).fetchall()
    out_conn.close()
    assert rows == [("e001",), ("e002",)]


def test_write_duckdb_returns_row_counts(tmp_path: Path) -> None:
    """write_duckdb returns a mapping of table_name -> row_count for every table."""
    emit_dir = build_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sql_entity = 'SELECT record_id FROM "records__entity" ORDER BY record_id'
        sql_history = 'SELECT record_id FROM "history" ORDER BY record_id'
        result = write_duckdb(emit, {"t1": sql_entity, "t2": sql_history}, out_path, {})

    assert result["t1"] == 2
    assert result["t2"] == 3


def test_write_duckdb_empty_grain_yields_empty_typed_table(tmp_path: Path) -> None:
    """A zero-row query produces an empty typed table, not a dropped one."""
    emit_dir = build_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sql = 'SELECT record_id FROM "records__entity" WHERE 1=0'
        result = write_duckdb(emit, {"empty_table": sql}, out_path, {})

    assert result == {"empty_table": 0}
    out_conn = duckdb.connect(str(out_path), read_only=True)
    tables = out_conn.execute("SHOW TABLES").fetchall()
    schema = out_conn.execute("DESCRIBE empty_table").fetchall()
    out_conn.close()
    assert any("empty_table" in str(row) for row in tables)
    assert len(schema) == 1
    assert "record_id" in schema[0][0]


def test_write_duckdb_null_pad_column_typed(tmp_path: Path) -> None:
    """A CAST(NULL AS VARCHAR) column is written as a typed column, not object."""
    emit_dir = build_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sql = 'SELECT record_id, CAST(NULL AS VARCHAR) AS null_col FROM "records__entity" ORDER BY record_id'
        result = write_duckdb(emit, {"null_test": sql}, out_path, {})

    assert result == {"null_test": 2}
    out_conn = duckdb.connect(str(out_path), read_only=True)
    schema = out_conn.execute("DESCRIBE null_test").fetchall()
    out_conn.close()
    col_names = [row[0] for row in schema]
    col_types = [row[1] for row in schema]
    assert "null_col" in col_names
    null_idx = col_names.index("null_col")
    assert "VARCHAR" in col_types[null_idx]


def test_write_duckdb_input_emit_untouched(tmp_path: Path) -> None:
    """The input emit remains read-only and usable after write_duckdb."""
    emit_dir = build_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sql = 'SELECT record_id FROM "records__entity" ORDER BY record_id'
        write_duckdb(emit, {"t": sql}, out_path, {})
        # emit is still usable and read-only
        rows = emit.query(
            'SELECT record_id FROM "records__entity" ORDER BY record_id', ()
        )
    assert rows == [("e001",), ("e002",)]


def test_write_duckdb_failure_raises_export_runtime_error(tmp_path: Path) -> None:
    """A writer failure (bad output path) surfaces as ExportRuntimeError."""
    emit_dir = build_test_emit(tmp_path)
    # Use a non-existent parent directory to cause duckdb to fail
    bad_path = tmp_path / "not_a_file" / "sub" / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sql = 'SELECT record_id FROM "records__entity"'
        with pytest.raises(ExportRuntimeError):
            write_duckdb(emit, {"t": sql}, bad_path, {})


def test_write_duckdb_query_failure_raises_export_runtime_error(
    tmp_path: Path,
) -> None:
    """A query-execution failure (bad column) raises ExportRuntimeError, per
    the documented contract — not the reader's RunDatabaseError."""
    emit_dir = build_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sql = 'SELECT nonexistent_column FROM "records__entity"'
        with pytest.raises(ExportRuntimeError, match="failed to write table 't'"):
            write_duckdb(emit, {"t": sql}, out_path, {})


def test_write_duckdb_quotes_embedded_quote_in_table_name(tmp_path: Path) -> None:
    """A table name containing a double-quote lands as a literal catalog name;
    the embedded quote never breaks out of the identifier position."""
    emit_dir = build_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"
    evil_name = "orders\" ; ATTACH '/tmp/x.db' AS x; --"

    with open_emit(emit_dir) as emit:
        sql = 'SELECT record_id FROM "records__entity" ORDER BY record_id'
        result = write_duckdb(emit, {evil_name: sql}, out_path, {})

    assert result == {evil_name: 2}
    out_conn = duckdb.connect(str(out_path), read_only=True)
    try:
        names = {
            row[0]
            for row in out_conn.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
    finally:
        out_conn.close()
    assert evil_name in names


def test_write_duckdb_keyed_table_declares_constraints(tmp_path: Path) -> None:
    """A keyed table is created with explicit DDL: PRIMARY KEY + UNIQUE land
    in duckdb_constraints(); row counts match the unkeyed path."""
    emit_dir = build_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sql = 'SELECT record_id, record_index FROM "records__entity" ORDER BY record_id'
        keys = {
            "dim_entity": TableKeys(
                primary_key=("record_id",), unique=(("record_index",),)
            )
        }
        result = write_duckdb(emit, {"dim_entity": sql}, out_path, keys)

    assert result == {"dim_entity": 2}
    types = constraint_types(out_path, "dim_entity")
    assert "PRIMARY KEY" in types
    assert "UNIQUE" in types


def test_write_duckdb_empty_keyed_table(tmp_path: Path) -> None:
    """An empty keyed table still declares its constraints and is present."""
    emit_dir = build_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sql = 'SELECT record_id, record_index FROM "records__entity" WHERE 1=0'
        keys = {
            "dim_entity": TableKeys(
                primary_key=("record_id",), unique=(("record_index",),)
            )
        }
        result = write_duckdb(emit, {"dim_entity": sql}, out_path, keys)

    assert result == {"dim_entity": 0}
    types = constraint_types(out_path, "dim_entity")
    assert "PRIMARY KEY" in types
    assert "UNIQUE" in types


def test_write_duckdb_multi_column_unique_constraint(tmp_path: Path) -> None:
    """A multi-column `unique` tuple entry produces one composite UNIQUE."""
    emit_dir = build_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sql = 'SELECT record_id, fork_path FROM "records__entity" ORDER BY record_id'
        keys = {
            "dim_entity": TableKeys(
                primary_key=("record_id",), unique=(("fork_path", "record_id"),)
            )
        }
        result = write_duckdb(emit, {"dim_entity": sql}, out_path, keys)

    assert result == {"dim_entity": 2}
    out_conn = duckdb.connect(str(out_path), read_only=True)
    try:
        composite = out_conn.execute(
            "SELECT constraint_column_names FROM duckdb_constraints()"
            " WHERE table_name = ? AND constraint_type = 'UNIQUE'",
            ["dim_entity"],
        ).fetchall()
    finally:
        out_conn.close()
    assert len(composite) == 1
    assert set(composite[0][0]) == {"fork_path", "record_id"}


def test_write_duckdb_unique_allows_nulls(tmp_path: Path) -> None:
    """NULLs pass a UNIQUE constraint under SQL semantics (the
    partitioned-kind undeclared-sub-type case)."""
    emit_dir = build_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sql = (
            "SELECT record_id, CAST(NULL AS VARCHAR) AS sub_type"
            ' FROM "records__entity" ORDER BY record_id'
        )
        keys = {
            "dim_entity": TableKeys(primary_key=("record_id",), unique=(("sub_type",),))
        }
        result = write_duckdb(emit, {"dim_entity": sql}, out_path, keys)

    assert result == {"dim_entity": 2}


def test_write_duckdb_duplicate_unique_value_raises(tmp_path: Path) -> None:
    """A duplicate non-NULL value in a declared unique column fails loudly,
    naming the table."""
    emit_dir = build_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sql = "SELECT record_id, 'dup' AS dup_col FROM \"records__entity\""
        keys = {
            "dim_entity": TableKeys(primary_key=("record_id",), unique=(("dup_col",),))
        }
        with pytest.raises(
            ExportRuntimeError, match="failed to write table 'dim_entity'"
        ):
            write_duckdb(emit, {"dim_entity": sql}, out_path, keys)


def test_write_duckdb_keys_unknown_table_raises_value_error(tmp_path: Path) -> None:
    """`keys` naming a table absent from `queries` raises ValueError."""
    emit_dir = build_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sql = 'SELECT record_id FROM "records__entity"'
        keys = {"not_a_query": TableKeys(primary_key=("record_id",), unique=())}
        with pytest.raises(ValueError, match="not_a_query"):
            write_duckdb(emit, {"dim_entity": sql}, out_path, keys)
