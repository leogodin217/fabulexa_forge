"""Shared writer fact: what one relation write materialized.

`WrittenRelation` and the DESCRIBE transcription authority behind it are
promoted here so both writers (`writers/csv.py`, `writers/duckdb.py`) share
one column-transcription path instead of each re-deriving DuckDB type text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb
    import pyarrow as pa

from fabulexa_forge._sql import quote_identifier


@dataclass(frozen=True)
class WrittenRelation:
    """What one relation write materialized: rows written and the
    (name, type-text) column pairs of the written relation.

    `row_count` is the invocation's written rows even on windowed paths
    (None-for-windowed is a report-assembly decision, not a writer fact).
    """

    row_count: int
    columns: tuple[tuple[str, str], ...]


def describe_arrow_columns(
    conn: "duckdb.DuckDBPyConnection",
    registered_name: str,
) -> tuple[tuple[str, str], ...]:
    """Return (column_name, duckdb_type) pairs for a registered Arrow relation.

    Reads DuckDB's own `DESCRIBE` output — the type strings are DuckDB's,
    never re-derived from the Arrow schema by hand. The single transcription
    authority every writer's column reporting goes through.

    Args:
        conn: The DuckDB connection the relation is registered on.
        registered_name: The relation's registered name.

    Returns:
        (column, type-text) pairs, in schema order.
    """
    rows = conn.execute(f"DESCRIBE {quote_identifier(registered_name)}").fetchall()
    return tuple((str(row[0]), str(row[1])) for row in rows)


def describe_arrow_table(arrow_table: "pa.Table") -> tuple[tuple[str, str], ...]:
    """Transcribe an Arrow table's DuckDB column types via a scratch connection.

    Registers `arrow_table` on a fresh in-memory DuckDB connection and
    delegates to `describe_arrow_columns` — never routes through the emit's
    own connection. Used by the CSV write path, which has no warehouse
    connection of its own to register against.

    Args:
        arrow_table: The materialized Arrow table to describe.

    Returns:
        (column, type-text) pairs, in schema order.
    """
    import duckdb

    conn = duckdb.connect(":memory:")
    try:
        conn.register("_arrow_src", arrow_table)
        return describe_arrow_columns(conn, "_arrow_src")
    finally:
        conn.close()
