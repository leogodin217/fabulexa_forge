"""Shared SQL-string utilities used across reader, derivation, exporter, and
corrupter layers."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

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


def render_predicate_condition(
    column: str,
    value: str | list[str],
    sql_type: str,
    alias: str | None,
) -> str:
    """Render one config predicate entry as a SQL condition.

    A `str` is a scalar and renders `= <literal>`; a `list` is a set of
    alternatives and renders `IN (<literal>, ...)` preserving element order.
    Discrimination is on `isinstance(value, str)` — never on `Sequence`, which
    a `str` itself satisfies. Every element is typed by `render_typed_literal`
    against `sql_type`, so a list is exactly the scalar rule applied
    element-wise.

    The one predicate-rendering authority: no module outside this one renders
    `=` or `IN` over a config predicate value.

    Args:
        column: The predicate column name, quoted through `quote_identifier`.
        value: The required value — a scalar, or a non-empty list of
            alternatives. Emptiness is a parse-time failure and is not
            re-checked here.
        sql_type: The column's DuckDB type from the sidecar, used to type each
            literal. `VARCHAR` yields the raw single-quoted literal, which is
            how the `history.value` surface gets its untyped comparison — a
            caller's type choice, not a mode of this function.
        alias: A relation alias to qualify the column with, or None for an
            unqualified condition. Only the point-in-time membership foreign
            key needs one; every other caller passes None explicitly.

    Returns:
        One SQL condition, unparenthesized, suitable for AND-joining with
        sibling conditions.

    Raises:
        ExportError: `sql_type` is not a recognized DuckDB type. Raised by
            `render_typed_literal`, which never falls back to VARCHAR.
    """
    quoted_column = quote_identifier(column)
    qualified = quoted_column if alias is None else f"{alias}.{quoted_column}"

    if isinstance(value, str):
        return f"{qualified} = {render_typed_literal(value, sql_type)}"

    literals = ", ".join(render_typed_literal(element, sql_type) for element in value)
    return f"{qualified} IN ({literals})"


# DuckDB's VARCHAR->BOOLEAN literal grammar (case-insensitive), mirrored here so
# `cast_predicate_element` never needs a live connection to decide castability.
_BOOLEAN_TRUE_LITERALS = frozenset({"true", "t", "yes", "y", "1"})
_BOOLEAN_FALSE_LITERALS = frozenset({"false", "f", "no", "n", "0"})


def cast_predicate_element(element: str, sql_type: str) -> object:
    """The plan-time constant evaluation of the CAST `render_typed_literal`
    compiles: one predicate element's typed value under a column's declared
    DuckDB type.

    Returned values are hashable, and `==` / `hash` realize typed-value
    identity under `sql_type` — two spellings of one value ('5' / '05'
    under BIGINT) are one value. Reads no rows.

    Args:
        element: The raw config string element.
        sql_type: The column's DuckDB type from the sidecar.

    Returns:
        The typed value.

    Raises:
        ValueError: `sql_type` cannot cast `element` (the caller wraps this
            into `SourceWhereValueUncastable` with owner context).
        ExportError: `sql_type` is not a recognized DuckDB type — never a
            silent VARCHAR fallback (per `render_typed_literal`).
    """
    if not is_recognized_sql_type(sql_type):
        raise ExportError(
            f"cast_predicate_element: unrecognized SQL type {sql_type!r}"
            " — no silent VARCHAR fallback"
        )

    upper = sql_type.upper()

    if upper == "VARCHAR" or upper.startswith("VARCHAR("):
        return element

    if upper in _INTEGER_TYPES:
        try:
            return int(element)
        except ValueError as exc:
            raise ValueError(f"{element!r} does not cast to {sql_type}") from exc

    if upper in _FLOAT_TYPES:
        try:
            return float(element)
        except ValueError as exc:
            raise ValueError(f"{element!r} does not cast to {sql_type}") from exc

    if upper == "BOOLEAN":
        lowered = element.strip().lower()
        if lowered in _BOOLEAN_TRUE_LITERALS:
            return True
        if lowered in _BOOLEAN_FALSE_LITERALS:
            return False
        raise ValueError(f"{element!r} does not cast to {sql_type}")

    # The remaining recognized family: DECIMAL(p[,s]) / NUMERIC(p[,s]).
    try:
        return Decimal(element)
    except InvalidOperation as exc:
        raise ValueError(f"{element!r} does not cast to {sql_type}") from exc
