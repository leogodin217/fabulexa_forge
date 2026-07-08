"""Shared SQL-string utilities used across reader, derivation, exporter, and
corrupter layers."""

from __future__ import annotations

from fabulexa_export.errors import ExportError


def _sql_literal(value: str) -> str:
    """Render a Python string as a single-quoted SQL literal (escaped).

    Args:
        value: The string value to quote.

    Returns:
        A SQL single-quoted string literal with internal single-quotes escaped.
    """
    return "'" + value.replace("'", "''") + "'"


# Integer families recognized by DuckDB
_INTEGER_TYPES = {
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "UHUGEINT",
}

# Floating-point families
_FLOAT_TYPES = {"DOUBLE", "FLOAT", "REAL"}


def render_typed_literal(value: str, sql_type: str) -> str:
    """Render a scalar value as a SQL literal typed to sql_type.

    VARCHAR (or VARCHAR( prefix) → single-quoted with '' escaping.
    Integer family (TINYINT/SMALLINT/INTEGER/BIGINT/HUGEINT + unsigned variants)
    / DOUBLE/FLOAT/REAL / DECIMAL(p,s) / BOOLEAN → CAST('<escaped>' AS <type>).
    Unknown types → raise ExportError.

    Args:
        value: The raw string value to render.
        sql_type: The DuckDB SQL type name (e.g. 'VARCHAR', 'BIGINT', 'BOOLEAN').

    Returns:
        A SQL literal fragment (not a full expression).

    Raises:
        ExportError: sql_type is not a recognized DuckDB type.
    """
    escaped = value.replace("'", "''")
    upper = sql_type.upper()

    if upper == "VARCHAR" or upper.startswith("VARCHAR("):
        return f"'{escaped}'"

    if upper in _INTEGER_TYPES:
        return f"CAST('{escaped}' AS {sql_type})"

    if upper in _FLOAT_TYPES:
        return f"CAST('{escaped}' AS {sql_type})"

    if upper == "BOOLEAN":
        return f"CAST('{escaped}' AS {sql_type})"

    if upper.startswith("DECIMAL(") or upper.startswith("NUMERIC("):
        return f"CAST('{escaped}' AS {sql_type})"

    raise ExportError(
        f"render_typed_literal: unrecognized SQL type '{sql_type}'"
        " — no silent VARCHAR fallback"
    )
