"""Tests for Emit.query, Emit.close, and the context manager."""

from __future__ import annotations

from pathlib import Path

import pytest
from _support.sidecar_builder import identity_column, prop_column

from fabulexa_forge.reader import RunDatabaseError, open_emit

from ._emit_helpers import _minimal_sidecar, write_emit


def _emit_with_data(tmp_path: Path) -> Path:
    """Write an emit with two tables containing typed data."""
    sidecar = _minimal_sidecar(
        extra_tables=[
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": [
                    identity_column("fork_path", "VARCHAR"),
                    identity_column("record_id", "VARCHAR"),
                    {"name": "created_sim_time", "type": "BIGINT"},
                    {"name": "active", "type": "BOOLEAN"},
                    {"name": "deactivated_at", "type": "BIGINT"},
                    {"name": "last_mutation_sim_time", "type": "BIGINT"},
                    identity_column("record_index", "BIGINT"),
                    prop_column(
                        "prop__age",
                        "BIGINT",
                        history_tracked=False,
                        temporal_class="constant",
                    ),
                    prop_column(
                        "prop__score",
                        "DOUBLE",
                        history_tracked=False,
                        temporal_class="constant",
                    ),
                ],
                "rows": 2,
            }
        ]
    )
    db_tables = {
        "firings": ("CREATE TABLE firings (fork_path VARCHAR, sim_time BIGINT)"),
        "records__patient": (
            "CREATE TABLE records__patient "
            "(fork_path VARCHAR, record_id VARCHAR, created_sim_time BIGINT, "
            "active BOOLEAN, deactivated_at BIGINT, last_mutation_sim_time BIGINT, "
            "record_index BIGINT, prop__age BIGINT, prop__score DOUBLE)"
        ),
    }
    write_emit(tmp_path, sidecar=sidecar, db_tables=db_tables)

    # Insert rows into the data table
    import duckdb

    conn = duckdb.connect(str(tmp_path / "run.duckdb"))
    conn.execute(
        "INSERT INTO records__patient VALUES "
        "('trunk', 'p1', 0, true, NULL, 0, 0, 30, 0.75), "
        "('trunk', 'p2', 0, false, NULL, 0, 1, 45, 0.5)"
    )
    conn.close()

    return tmp_path


# ---------------------------------------------------------------------------
# Query returns rows as tuples
# ---------------------------------------------------------------------------


def test_query_returns_rows_as_tuples(tmp_path: Path) -> None:
    """query returns rows as tuples in ORDER BY order."""
    _emit_with_data(tmp_path)
    with open_emit(tmp_path) as emit:
        rows = emit.query(
            "SELECT record_id, prop__age FROM records__patient ORDER BY prop__age", ()
        )
    assert rows == [("p1", 30), ("p2", 45)]


def test_query_native_types_are_preserved(tmp_path: Path) -> None:
    """DuckDB-native Python types come back untransformed."""
    _emit_with_data(tmp_path)
    with open_emit(tmp_path) as emit:
        rows = emit.query(
            "SELECT record_id, prop__age, prop__score, active "
            "FROM records__patient ORDER BY record_id",
            (),
        )
    assert rows[0] == ("p1", 30, 0.75, True)
    assert isinstance(rows[0][0], str)
    assert isinstance(rows[0][1], int)
    assert isinstance(rows[0][2], float)
    assert isinstance(rows[0][3], bool)


# ---------------------------------------------------------------------------
# Bound parameters
# ---------------------------------------------------------------------------


def test_query_bound_parameter(tmp_path: Path) -> None:
    """A bound parameter is passed through parameters and not string-interpolated."""
    _emit_with_data(tmp_path)
    with open_emit(tmp_path) as emit:
        rows = emit.query(
            "SELECT record_id FROM records__patient WHERE prop__age = ?", (30,)
        )
    assert rows == [("p1",)]


# ---------------------------------------------------------------------------
# Read-only: DML/DDL raises RunDatabaseError
# ---------------------------------------------------------------------------


def test_create_table_raises_run_database_error(tmp_path: Path) -> None:
    """A CREATE TABLE statement raises RunDatabaseError (connection is read-only)."""
    write_emit(tmp_path)
    with open_emit(tmp_path) as emit:
        with pytest.raises(RunDatabaseError):
            emit.query("CREATE TABLE forbidden (x INT)", ())


def test_insert_raises_run_database_error(tmp_path: Path) -> None:
    """An INSERT statement raises RunDatabaseError (connection is read-only)."""
    _emit_with_data(tmp_path)
    with open_emit(tmp_path) as emit:
        with pytest.raises(RunDatabaseError):
            emit.query(
                "INSERT INTO records__patient VALUES "
                "('trunk', 'p3', 0, true, NULL, 0, 2, 50, 0.9)",
                (),
            )


# ---------------------------------------------------------------------------
# close is idempotent
# ---------------------------------------------------------------------------


def test_close_is_idempotent(tmp_path: Path) -> None:
    """Two calls to close() produce no error."""
    write_emit(tmp_path)
    emit = open_emit(tmp_path)
    emit.close()
    emit.close()  # second call must not raise


# ---------------------------------------------------------------------------
# query after close raises RunDatabaseError
# ---------------------------------------------------------------------------


def test_query_after_close_raises_run_database_error(tmp_path: Path) -> None:
    """query() on a closed Emit raises RunDatabaseError, not a bare duckdb error."""
    _emit_with_data(tmp_path)
    emit = open_emit(tmp_path)
    emit.close()
    with pytest.raises(RunDatabaseError, match="query failed"):
        emit.query("SELECT record_id FROM records__patient", ())


# ---------------------------------------------------------------------------
# Context manager closes on exit
# ---------------------------------------------------------------------------


def test_context_manager_closes_on_exit(tmp_path: Path) -> None:
    """with open_emit(...) as emit: closes on exit without error."""
    write_emit(tmp_path)
    with open_emit(tmp_path) as emit:
        assert emit.emit_dir == tmp_path
    # After the with block, a second close must still be idempotent
    emit.close()
