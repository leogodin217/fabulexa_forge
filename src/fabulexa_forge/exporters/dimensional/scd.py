"""SCD-2 type2 wide reconstruction for the dimensional exporter.

Composes the versioned-intervals derivation (build_versioned_intervals_sql) for
version boundaries and tracked prop__<p> values, and the reader records relation
(build_records_relation_sql) for static columns. The format authors no base-table
SQL — base-table reads live in the derivation and reader layers.

Tracked/static split rules:
- Read ColumnSpec.history_tracked per column (flag-authoritative).
- A projection-introduced column (no upstream property) is never tracked.
- Emits with history_tracked absent are refused by check_scd2_needs_history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fabulexa_forge.anchor import render_anchor_timestamp_expr
from fabulexa_forge.derivations.versioned_intervals import (
    build_versioned_intervals_sql,
)
from fabulexa_forge.reader.relations import build_records_relation_sql

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ColumnDecl, TableDecl
    from fabulexa_forge.reader.sidecar import Sidecar

# Alias for the versioned-intervals derivation subquery.
_VERSIONS_ALIAS = "_versions"
# Alias for the reader records-relation subquery (static columns).
_RECORDS_ALIAS = "_records"


def build_scd2_column_expr_flag(
    col_decl: "ColumnDecl",
    version_alias: str,
    records_alias: str,
    is_tracked: bool,
    anchor: "EffectiveAnchor | None",
    source_col_type: str = "VARCHAR",
) -> str:
    """Build a SQL expression for one SCD-2 column from the derivation.

    Tracked columns project directly from the derivation's pre-computed prop__<p>
    column (cast to the source column's DuckDB type). Static columns project from
    the reader records relation. scd_window columns come from the version bounds.
    null columns are typed NULL.

    Args:
        col_decl: The output column declaration.
        version_alias: The alias of the versioned-intervals derivation subquery.
        records_alias: The alias of the reader records-relation subquery.
        is_tracked: Whether this column is history-tracked.
        anchor: The resolved EffectiveAnchor, or None.
        source_col_type: DuckDB type of the source column (for CAST in tracked path).
            Defaults to VARCHAR (no-op cast for VARCHAR columns).

    Returns:
        A SQL expression fragment: `<expr> AS "<col_name>"`.
    """
    if col_decl.derived is not None and col_decl.derived.scd_window is not None:
        bound = col_decl.derived.scd_window  # "valid_from" or "valid_to"
        col_name = "version_start" if bound == "valid_from" else "version_end"
        qualified_source = f'"{version_alias}"."{col_name}"'
        return render_anchor_timestamp_expr(anchor, qualified_source, col_decl.name)

    if col_decl.null is not None:
        return f'CAST(NULL AS VARCHAR) AS "{col_decl.name}"'

    if col_decl.from_ is None:
        return f'NULL AS "{col_decl.name}"'

    if is_tracked:
        # Project the pre-computed prop__<p> value from the derivation.
        prop_col = col_decl.from_  # e.g. "prop__status"
        return (
            f'CAST("{version_alias}"."{prop_col}"'
            f" AS {source_col_type})"
            f' AS "{col_decl.name}"'
        )

    # Static: read from the reader records relation.
    return f'"{records_alias}"."{col_decl.from_}" AS "{col_decl.name}"'


def _collect_tracked_props(
    sidecar: "Sidecar",
    source_table_name: str,
) -> frozenset[str]:
    """Collect the set of history-tracked property names from the sidecar flag.

    Returns property names (without the prop__ prefix) whose sidecar ColumnSpec
    has history_tracked=True. Reads the sidecar column list; always flag-authoritative.

    Args:
        sidecar: The open emit's sidecar.
        source_table_name: The resolved records__<kind> DuckDB table name.

    Returns:
        A frozenset of tracked property names (without prop__ prefix).
    """
    tracked: set[str] = set()
    for col_spec in sidecar.columns(source_table_name):
        if col_spec.history_tracked is True and col_spec.name.startswith("prop__"):
            tracked.add(col_spec.name[len("prop__") :])
    return frozenset(tracked)


def build_scd2_sql(
    table_decl: "TableDecl",
    source_table_name: str,
    sidecar: "Sidecar",
    anchor: "EffectiveAnchor | None",
    fork_path: str,
) -> str:
    """Build the SELECT SQL for an scd: type2 records grain.

    Composes the versioned-intervals derivation (build_versioned_intervals_sql)
    for version bounds and tracked prop__<p> values, and the reader records relation
    (build_records_relation_sql) for static columns. The format authors no
    base-table SQL.

    Tracked columns project from the derivation's pre-computed prop__<p> columns;
    static columns LEFT JOIN the reader records relation on record_id.

    Honors table_decl.source.filter: a discriminator-split source restricts both
    the derivation's version rows and the records relation to the filtered
    sub-type's records.

    Args:
        table_decl: The output table declaration (scd: type2, grain: records).
        source_table_name: The resolved records__<kind> DuckDB table name.
        sidecar: The open emit's sidecar.
        anchor: The resolved EffectiveAnchor, or None.
        fork_path: The sole branch fork_path; passed to the derivation and records
            relation builders.

    Returns:
        A complete, deterministic SELECT statement composing the derivation and
        the reader records relation.
    """
    source = table_decl.source
    kind = source.kind

    tracked_props = _collect_tracked_props(sidecar, source_table_name)

    # discriminator_filter from source.filter — a discriminator-split scd: type2
    # dim must contain only the filtered sub-type's rows.
    discriminator_filter: dict[str, str | list[str]] = (
        dict(source.filter) if source.filter else {}
    )

    # Compose the versioned-intervals derivation.
    derivation_sql = build_versioned_intervals_sql(
        sidecar=sidecar,
        fork_path=fork_path,
        kind=kind,
        tracked_properties=tracked_props,
        discriminator_filter=discriminator_filter,
    )

    # Compose the reader records relation for static columns.
    records_sql = build_records_relation_sql(
        sidecar=sidecar,
        fork_path=fork_path,
        kind=kind,
        discriminator_filter=discriminator_filter,
    )

    # Build a lookup of column name -> DuckDB type for the source table.
    source_col_types: dict[str, str] = {
        col_spec.name: col_spec.type for col_spec in sidecar.columns(source_table_name)
    }

    col_exprs: list[str] = []
    for col_decl in table_decl.columns:
        if col_decl.from_ is not None and col_decl.from_.startswith("prop__"):
            prop = col_decl.from_[len("prop__") :]
            is_tracked = prop in tracked_props
        else:
            is_tracked = False
        col_type = source_col_types.get(col_decl.from_ or "", "VARCHAR")
        expr = build_scd2_column_expr_flag(
            col_decl,
            _VERSIONS_ALIAS,
            _RECORDS_ALIAS,
            is_tracked,
            anchor,
            col_type,
        )
        col_exprs.append(expr)

    select_list = ", ".join(col_exprs)
    join_clause = (
        f' LEFT JOIN ({records_sql}) AS "{_RECORDS_ALIAS}"'
        f' ON "{_RECORDS_ALIAS}"."record_id" = "{_VERSIONS_ALIAS}"."record_id"'
    )

    return (
        f"SELECT {select_list}"
        f' FROM ({derivation_sql}) AS "{_VERSIONS_ALIAS}"'
        f"{join_clause}"
        f' ORDER BY "{_VERSIONS_ALIAS}"."record_id",'
        f' "{_VERSIONS_ALIAS}"."version_start"'
    )


def build_scd2_rows_sql(
    table_decl: "TableDecl",
    source_table_name: str,
    sidecar: "Sidecar",
    anchor: "EffectiveAnchor | None",
    window_start_ns: int,
    window_end_ns: int,
    fork_path: str,
) -> str:
    """Build the SELECT SQL for a windowed SCD-2 physical rows table.

    Produces all declared columns except scd_window: valid_to slots, in
    declared order, plus a trailing __valid_from_ns column (the version's raw
    sim-time change point). Applies a half-open window predicate on the raw
    change point.

    Composes the versioned-intervals derivation for version bounds and tracked
    prop__<p> values, and the reader records relation for static columns.

    Honors table_decl.source.filter: a discriminator-split source restricts both
    the derivation's version rows and the records relation to the filtered
    sub-type's records.

    Args:
        table_decl: The output table declaration (scd: type2, grain: records).
        source_table_name: The resolved records__<kind> DuckDB table name.
        sidecar: The open emit's sidecar.
        anchor: The resolved EffectiveAnchor, or None.
        window_start_ns: The window's inclusive start in sim-time ns.
        window_end_ns: The window's exclusive end in sim-time ns.
        fork_path: The sole branch fork_path; passed to the derivation and records
            relation builders.

    Returns:
        A complete SELECT statement for the physical __rows table.
    """
    source = table_decl.source
    kind = source.kind

    tracked_props = _collect_tracked_props(sidecar, source_table_name)

    # discriminator_filter from source.filter — a discriminator-split scd: type2
    # dim must contain only the filtered sub-type's rows.
    discriminator_filter: dict[str, str | list[str]] = (
        dict(source.filter) if source.filter else {}
    )

    # Compose the versioned-intervals derivation.
    derivation_sql = build_versioned_intervals_sql(
        sidecar=sidecar,
        fork_path=fork_path,
        kind=kind,
        tracked_properties=tracked_props,
        discriminator_filter=discriminator_filter,
    )

    # Compose the reader records relation for static columns.
    records_sql = build_records_relation_sql(
        sidecar=sidecar,
        fork_path=fork_path,
        kind=kind,
        discriminator_filter=discriminator_filter,
    )

    source_col_types: dict[str, str] = {
        col_spec.name: col_spec.type for col_spec in sidecar.columns(source_table_name)
    }

    col_exprs: list[str] = []
    for col_decl in table_decl.columns:
        # Skip valid_to slots — not materialized in the rows table.
        if col_decl.derived is not None and col_decl.derived.scd_window == "valid_to":
            continue

        if col_decl.from_ is not None and col_decl.from_.startswith("prop__"):
            prop = col_decl.from_[len("prop__") :]
            is_tracked = prop in tracked_props
        else:
            is_tracked = False
        col_type = source_col_types.get(col_decl.from_ or "", "VARCHAR")
        expr = build_scd2_column_expr_flag(
            col_decl,
            _VERSIONS_ALIAS,
            _RECORDS_ALIAS,
            is_tracked,
            anchor,
            col_type,
        )
        col_exprs.append(expr)

    # Trailing bookkeeping column: raw ns change point.
    col_exprs.append(f'"{_VERSIONS_ALIAS}"."version_start" AS "__valid_from_ns"')

    select_list = ", ".join(col_exprs)
    join_clause = (
        f' LEFT JOIN ({records_sql}) AS "{_RECORDS_ALIAS}"'
        f' ON "{_RECORDS_ALIAS}"."record_id" = "{_VERSIONS_ALIAS}"."record_id"'
    )

    # Half-open window predicate on the raw change point.
    window_pred = (
        f' WHERE "{_VERSIONS_ALIAS}"."version_start" >= {window_start_ns}'
        f' AND "{_VERSIONS_ALIAS}"."version_start" < {window_end_ns}'
    )

    return (
        f"SELECT {select_list}"
        f' FROM ({derivation_sql}) AS "{_VERSIONS_ALIAS}"'
        f"{join_clause}"
        f"{window_pred}"
        f' ORDER BY "{_VERSIONS_ALIAS}"."record_id",'
        f' "{_VERSIONS_ALIAS}"."version_start"'
    )


def build_scd2_view_sql(
    table_decl: "TableDecl",
    source_table_name: str,
) -> str:
    """Build the DDL SELECT body for a SCD-2 companion view.

    Projects the declared column list (not __valid_from_ns), with each
    valid_to slot computed as:
        LEAD(<valid_from column>) OVER (PARTITION BY <identity columns>
        ORDER BY __valid_from_ns)

    Identity columns = the table's key minus its scd_window columns, in key order.

    Args:
        table_decl: The output table declaration (scd: type2 with a valid_to column).
        source_table_name: The resolved physical __rows table name.

    Returns:
        A SELECT statement body for the view (without CREATE VIEW prefix).
    """
    # Identify valid_from and identity columns
    scd_window_col_names: set[str] = set()
    valid_from_col: str | None = None

    for col_decl in table_decl.columns:
        if col_decl.derived is not None and col_decl.derived.scd_window is not None:
            scd_window_col_names.add(col_decl.name)
            if col_decl.derived.scd_window == "valid_from":
                valid_from_col = col_decl.name

    # Identity = key minus scd_window columns
    identity_cols = [k for k in table_decl.key if k not in scd_window_col_names]

    # Partition BY for LEAD
    partition_by = ", ".join(f'"{c}"' for c in identity_cols)

    view_exprs: list[str] = []
    for col_decl in table_decl.columns:
        if col_decl.derived is not None and col_decl.derived.scd_window == "valid_to":
            # Compute valid_to as LEAD(valid_from) over identity ordered by raw ns
            assert valid_from_col is not None
            expr = (
                f'LEAD("{valid_from_col}") OVER'
                f" (PARTITION BY {partition_by}"
                f' ORDER BY "__valid_from_ns") AS "{col_decl.name}"'
            )
        else:
            expr = f'"{col_decl.name}"'
        view_exprs.append(expr)

    select_list = ", ".join(view_exprs)
    return f'SELECT {select_list} FROM "{source_table_name}"'
