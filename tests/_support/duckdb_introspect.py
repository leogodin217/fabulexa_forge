"""DuckDB constraint-introspection helpers shared by writer-constraint tests.

Reads DuckDB's own `duckdb_constraints()` system table function so assertions
never re-derive constraint state by hand.
"""

from __future__ import annotations

from pathlib import Path

import duckdb


def constraint_types(db_path: Path, table_name: str) -> list[str]:
    """The `constraint_type` values DuckDB records for `table_name`.

    Args:
        db_path: The DuckDB file to open read-only.
        table_name: The table to inspect.

    Returns:
        One entry per declared constraint (e.g. 'PRIMARY KEY', 'UNIQUE').
    """
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = conn.execute(
            "SELECT constraint_type FROM duckdb_constraints() WHERE table_name = ?",
            [table_name],
        ).fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows]
