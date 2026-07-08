"""Faithful-read SQL builders for the reader layer.

These functions return canonical SELECT strings over base tables, filtered to the
sole branch's fork_path and a caller-supplied predicate. They carry no ORDER BY —
the caller's representation step orders. No reshaping: the rows are the base-table
rows that match.

Layer-direction invariant: imports only reader submodules, stdlib, and typing.
Never imports exporters.*, derivations.*, or config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from fabulexa_export.reader.emit import Emit
    from fabulexa_export.reader.sidecar import Sidecar

from fabulexa_export._sql import _sql_literal
from fabulexa_export.reader.errors import TableNotFoundError

# The six fixed history columns, in the order the history table always carries.
_HISTORY_FIXED_COLS: tuple[str, ...] = (
    "fork_path",
    "kind",
    "record_id",
    "property",
    "sim_time",
    "value",
)

# Prefix that marks provenance columns on the history table.
_WRITTEN_BY_PREFIX = "written_by_"


_INTEGER_TYPES: frozenset[str] = frozenset(
    {
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
)
_FLOAT_TYPES: frozenset[str] = frozenset({"DOUBLE", "FLOAT", "REAL"})


def _render_typed_literal(value: str, sql_type: str) -> str:
    """Render a scalar value as a SQL literal typed to sql_type.

    Matches the behaviour of columns.render_typed_literal so filter predicates
    built here are byte-identical to those the grain builders formerly authored:
    VARCHAR → single-quoted; integer / float / BOOLEAN / DECIMAL → CAST form;
    unknown type → VARCHAR fallback (the reader has no ExportError dependency).

    Args:
        value: The string representation of the value.
        sql_type: The DuckDB type of the column.

    Returns:
        A SQL literal string.
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

    # Unknown type: fall back to VARCHAR literal (the reader never raises ExportError)
    return f"'{escaped}'"


def build_records_relation_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    discriminator_filter: Mapping[str, str],
) -> str:
    """Build a faithful SELECT over records__<kind>.

    The relation is filtered to fork_path and to the discriminator predicate
    (each filter literal typed per the column's sidecar DuckDB type); its
    columns are the kind's full sidecar column list, unprojected; it carries no
    ORDER BY (the caller's representation step orders). No reshaping: the rows
    are the records-table rows that match.

    Args:
        sidecar: The open emit's sidecar (schema and column-type source).
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind; resolves to the records__<kind> table.
        discriminator_filter: Column -> required value; empty mapping selects all.

    Returns:
        A complete SELECT producing the kind's records, filtered, in no declared
        order.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
    """
    table_name = f"records__{kind}"
    cols = sidecar.columns(table_name)  # raises TableNotFoundError if absent

    col_list = ", ".join(f'"{c.name}"' for c in cols)

    # Build a type lookup for literal typing
    col_types: dict[str, str] = {c.name: c.type for c in cols}

    conditions: list[str] = [
        f'"fork_path" = {_sql_literal(fork_path)}',
    ]
    for col_name, value in discriminator_filter.items():
        sql_type = col_types.get(col_name, "VARCHAR")
        literal = _render_typed_literal(value, sql_type)
        conditions.append(f'"{col_name}" = {literal}')

    where = " AND ".join(conditions)
    return f'SELECT {col_list} FROM "{table_name}" WHERE {where}'


def build_history_relation_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    property_name: str,
    value_filter: str | None,
) -> str:
    """Build a faithful SELECT over history for one (kind, property[, value]).

    Filtered to fork_path, kind, property_name, and — when value_filter is not
    None — value. The value comparison is a raw VARCHAR literal against
    history.value (always VARCHAR per contract), not type-coerced per a source
    column's DuckDB type the way the records / membership builders type their
    filter literals. This matches today's history-point grain, whose value filter
    is rendered as the raw literal `"value" = '<v>'` (grains.py) and was never
    type-coerced — so the relocation is byte-identical, including for a value:
    filter on a non-VARCHAR source property. Columns are the six fixed history
    columns plus the written_by_* provenance columns when the sidecar lists them;
    no ORDER BY.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind to filter history to.
        property_name: The property to filter history to.
        value_filter: A required value to additionally filter on (matched as a raw
            VARCHAR literal against history.value), or None for no value filter.
            Callers pass None explicitly.

    Returns:
        A complete SELECT producing the matching history rows in no declared order.
    """
    # Enumerate all columns from the sidecar: fixed six + any written_by_* columns
    try:
        all_cols = sidecar.columns("history")
        col_names = [c.name for c in all_cols]
    except TableNotFoundError:
        # history is a contract-guaranteed fixed-category table; if absent, emit
        # only the six fixed columns (conformance check will catch the absence).
        col_names = list(_HISTORY_FIXED_COLS)

    # Project in sidecar order; only include fixed + written_by_* columns
    projected: list[str] = []
    for col_name in col_names:
        if col_name in _HISTORY_FIXED_COLS or col_name.startswith(_WRITTEN_BY_PREFIX):
            projected.append(f'"{col_name}"')

    # If we only got the fixed columns (e.g. table not in sidecar), emit them
    if not projected:
        projected = [f'"{c}"' for c in _HISTORY_FIXED_COLS]

    col_list = ", ".join(projected)

    conditions: list[str] = [
        f'"fork_path" = {_sql_literal(fork_path)}',
        f'"kind" = {_sql_literal(kind)}',
        f'"property" = {_sql_literal(property_name)}',
    ]
    if value_filter is not None:
        conditions.append(f'"value" = {_sql_literal(value_filter)}')

    where = " AND ".join(conditions)
    return f'SELECT {col_list} FROM "history" WHERE {where}'


def build_membership_relation_sql(
    sidecar: "Sidecar",
    fork_path: str,
    owner_kind: str,
    property_name: str,
    where_predicate: Mapping[str, str],
) -> str:
    """Build a faithful SELECT over the membership table for (owner_kind, property).

    Resolves membership__<owner_kind>__<property_name> from the sidecar, filtered
    to fork_path and to the where predicate over elem__ columns (literals typed per
    sidecar column type). Columns are the membership table's full sidecar column
    list; no ORDER BY.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        owner_kind: The binding owner's record kind.
        property_name: The collection-struct property naming the membership table.
        where_predicate: elem__ column -> required value; empty mapping selects all.

    Returns:
        A complete SELECT producing the membership rows, filtered, in no declared
        order.

    Raises:
        TableNotFoundError: membership__<owner_kind>__<property_name> is absent.
    """
    table_name = f"membership__{owner_kind}__{property_name}"
    cols = sidecar.columns(table_name)  # raises TableNotFoundError if absent

    col_list = ", ".join(f'"{c.name}"' for c in cols)
    col_types: dict[str, str] = {c.name: c.type for c in cols}

    conditions: list[str] = [
        f'"fork_path" = {_sql_literal(fork_path)}',
    ]
    for col_name, value in where_predicate.items():
        sql_type = col_types.get(col_name, "VARCHAR")
        literal = _render_typed_literal(value, sql_type)
        conditions.append(f'"{col_name}" = {literal}')

    where = " AND ".join(conditions)
    return f'SELECT {col_list} FROM "{table_name}" WHERE {where}'


def distinct_prop_values(
    emit: "Emit",
    kind: str,
    property_name: str,
) -> list[str]:
    """Return the distinct prop__<property_name> values observed in records__<kind>.

    The faithful SELECT DISTINCT that init's discriminator fan-out uses — the sole
    base-table SELECT DISTINCT the format authors today. No business rule needs it:
    DiscriminatorValueObserved reads the sidecar's enum_domains, not the emit.
    Executed against the emit's read-only connection. Takes no fork_path and does not
    filter to a branch — matching the existing helper; trunk-only, records__<kind>
    holds the sole branch's records, so the unfiltered read and a branch-filtered one
    coincide. Excludes NULL and emits `ORDER BY 1`, so values return sorted by the
    source column's native DuckDB type — not a Python string sort — byte-identical to
    the existing `SELECT DISTINCT <col> WHERE <col> IS NOT NULL ORDER BY 1`.

    Args:
        emit: The open emit.
        kind: The record kind; resolves to records__<kind>.
        property_name: The property whose distinct values are read; resolves to
            prop__<property_name>. (init's discriminator fan-out passes <kind>_type,
            the one call site this records-kind / prop__ shape must cover.)

    Returns:
        The observed distinct non-NULL values as strings, in the source column's
        native-type `ORDER BY 1` order (deterministic). Callers must not re-sort: a
        Python string sort would reorder numeric discriminators.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
    """
    table_name = f"records__{kind}"
    col_name = f"prop__{property_name}"

    # Validate that the table is in the sidecar before querying
    emit.sidecar.columns(table_name)  # raises TableNotFoundError if absent

    sql = (
        f'SELECT DISTINCT "{col_name}" FROM "{table_name}"'
        f' WHERE "{col_name}" IS NOT NULL ORDER BY 1'
    )
    rows = emit.query(sql, ())
    return [str(row[0]) for row in rows]
