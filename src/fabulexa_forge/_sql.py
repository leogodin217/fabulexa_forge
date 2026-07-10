"""Shared SQL-string utilities used across reader, derivation, exporter, and
corrupter layers."""

from __future__ import annotations

import re

from fabulexa_forge.errors import ExportError


def quote_identifier(name: str) -> str:
    """Wrap a SQL identifier in double quotes, doubling any internal quotes.

    The one identifier-quoting helper: every CREATE TABLE / SELECT / DESCRIBE
    splice of a name that is not pattern-gated at config load (e.g. a
    bundle-sourced sidecar table name) must go through this, so an embedded
    `"` can never break out of the identifier position.

    Args:
        name: The identifier string (table or column name).

    Returns:
        The DuckDB double-quoted identifier string.
    """
    return '"' + name.replace('"', '""') + '"'


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

# Strict grammar for the parameterized families. Anchored end-to-end: nothing
# may follow the closing paren, so a payload that closes a CAST and appends
# SQL (e.g. "VARCHAR(10)) FROM read_csv(...) --") can never pass the gate.
_PARAMETERIZED_TYPE_RE = re.compile(
    r"^(?:VARCHAR\(\s*\d+\s*\)|(?:DECIMAL|NUMERIC)\(\s*\d+\s*(?:,\s*\d+\s*)?\))$"
)


def is_recognized_sql_type(sql_type: str) -> bool:
    """Whether `sql_type` is on the recognized DuckDB type allow-list.

    The one type-name gate shared by `render_typed_literal` and every config
    surface that accepts an author-supplied type string (e.g.
    `SchemaDrift.retype_to`) — a type name that fails here must never be
    spliced into SQL. Parameterized forms are matched by an anchored grammar
    (digits-only arguments, nothing after the closing paren), never by prefix.

    Args:
        sql_type: The DuckDB SQL type name (e.g. 'VARCHAR', 'BIGINT').

    Returns:
        True iff the type is VARCHAR, BOOLEAN, an integer family member, a
        float family member, or a strictly-parameterized VARCHAR(n) /
        DECIMAL(p[,s]) / NUMERIC(p[,s]).
    """
    upper = sql_type.upper()
    return (
        upper == "VARCHAR"
        or upper in _INTEGER_TYPES
        or upper in _FLOAT_TYPES
        or upper == "BOOLEAN"
        or _PARAMETERIZED_TYPE_RE.fullmatch(upper) is not None
    )


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
    if not is_recognized_sql_type(sql_type):
        raise ExportError(
            f"render_typed_literal: unrecognized SQL type '{sql_type}'"
            " — no silent VARCHAR fallback"
        )

    escaped = value.replace("'", "''")
    upper = sql_type.upper()

    if upper == "VARCHAR" or upper.startswith("VARCHAR("):
        return f"'{escaped}'"

    return f"CAST('{escaped}' AS {sql_type})"
