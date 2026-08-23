"""Tests for the shared writer transcription authority: `describe_arrow_columns`
and `describe_arrow_table` (`writers/relation.py`).

Both writers (CSV, DuckDB) report a written relation's columns through this
one authority; these tests pin that the two entry points agree with each
other and with DuckDB's own `DESCRIBE` output, including through the
DuckDB writer's keyed-creation path.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from exporters._emit_fixtures import build_test_emit
from fabulexa_forge.exporters.query_spec import TableKeys
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.writers.duckdb import write_duckdb
from fabulexa_forge.writers.relation import describe_arrow_columns, describe_arrow_table


def test_describe_arrow_columns_reads_duckdb_describe() -> None:
    """describe_arrow_columns transcribes DuckDB's own DESCRIBE type text,
    in schema order."""
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (id BIGINT, name VARCHAR)")
        arrow_table = conn.execute("SELECT * FROM t").arrow()
        conn.register("src", arrow_table)
        columns = describe_arrow_columns(conn, "src")
    finally:
        conn.close()

    assert columns == (("id", "BIGINT"), ("name", "VARCHAR"))


def test_describe_arrow_table_matches_describe_arrow_columns() -> None:
    """describe_arrow_table's own scratch-connection path agrees with a
    caller-supplied connection describing the identical Arrow table."""
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (id BIGINT, name VARCHAR, flag BOOLEAN)")
        conn.execute("INSERT INTO t VALUES (1, 'a', TRUE)")
        arrow_table = conn.execute("SELECT * FROM t").arrow()
    finally:
        conn.close()

    scratch_conn = duckdb.connect(":memory:")
    try:
        scratch_conn.register("src", arrow_table)
        via_connection = describe_arrow_columns(scratch_conn, "src")
    finally:
        scratch_conn.close()

    assert describe_arrow_table(arrow_table) == via_connection


def test_describe_arrow_table_matches_keyed_creation_path(tmp_path: Path) -> None:
    """describe_arrow_table's type text for a query result equals the type
    text the DuckDB writer's keyed-creation path (`_create_keyed_table_from_arrow`,
    reached via a non-empty `keys` mapping) records for the identical query --
    one transcription authority, two callers."""
    emit_dir = build_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"
    sql = 'SELECT fork_path, record_id FROM "records__entity" ORDER BY record_id'
    keys = {"dim_entity": TableKeys(primary_key=("record_id",), unique=())}

    with open_emit(emit_dir) as emit:
        arrow_table = emit.query_arrow(sql, ())
        via_csv_path = describe_arrow_table(arrow_table)
        written = write_duckdb(emit, {"dim_entity": sql}, out_path, keys)

    assert via_csv_path == written["dim_entity"].columns
    assert via_csv_path == (("fork_path", "VARCHAR"), ("record_id", "VARCHAR"))
