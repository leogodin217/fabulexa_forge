"""Grain SQL builders for the dimensional exporter.

Each function builds the FROM / WHERE / ORDER BY clauses for one grain type.
Column expressions come from the columns module; grain builders assemble them
into complete SELECT statements.

All functions are module-level for independent testability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from fabulexa_export.anchor import EffectiveAnchor
    from fabulexa_export.config.models import DimensionalConfig, TableDecl
    from fabulexa_export.incremental.windows import Window
    from fabulexa_export.reader.sidecar import Sidecar

from fabulexa_export.derivations.versioned_intervals import (
    build_versioned_intervals_sql,
)
from fabulexa_export.errors import ExportError
from fabulexa_export.exporters.dimensional.columns import (
    build_column_expr,
)
from fabulexa_export.exporters.dimensional.scd import (
    build_scd2_rows_sql,
    build_scd2_sql,
    build_scd2_view_sql,
)
from fabulexa_export.reader.errors import TableNotFoundError
from fabulexa_export.reader.relations import (
    build_history_relation_sql,
    build_membership_relation_sql,
    build_records_relation_sql,
)


def _membership_order_by_columns(
    source_table_name: str, sidecar: "Sidecar"
) -> list[str]:
    """Return the ORDER BY columns for a membership grain (after record_id).

    Membership row order: record_id, joined_sim_time, then elem__* columns in
    element_schema declaration order (the order they appear in the sidecar).

    Args:
        source_table_name: The resolved membership DuckDB table name.
        sidecar: The open emit's sidecar.

    Returns:
        A list of quoted column names for ORDER BY, starting with joined_sim_time.
    """
    try:
        cols = sidecar.columns(source_table_name)
    except TableNotFoundError as exc:
        raise ExportError(
            f"cannot build ORDER BY for membership grain: "
            f"source table '{source_table_name}' not found in sidecar"
        ) from exc

    ordered = ['"joined_sim_time"']
    for col in cols:
        if col.name.startswith("elem__"):
            ordered.append(f'"{col.name}"')
    return ordered


def _collect_column_exprs_and_joins(
    table_decl: "TableDecl",
    anchor: "EffectiveAnchor | None",
    source_grain: str,
    anchor_kind: str,
    config: "DimensionalConfig | None",
    sidecar: "Sidecar | None",
    source_table_name: str | None = None,
) -> tuple[list[str], list[str]]:
    """Collect SELECT expressions and JOIN clauses for all columns.

    Args:
        table_decl: The output table declaration.
        anchor: The resolved EffectiveAnchor, or None.
        source_grain: The grain type string.
        anchor_kind: The anchor record kind.
        config: The dimensional config (for fk resolution), or None when no fk.
        sidecar: The open emit's sidecar (for fk resolution), or None when no fk.
        source_table_name: The resolved DuckDB source table name, forwarded to
            build_column_expr for value_map WHEN predicate type resolution.

    Returns:
        (col_exprs, join_clauses) — SELECT expressions and deduplicated JOIN clauses.
    """
    col_exprs: list[str] = []
    join_clauses: list[str] = []
    seen_joins: set[str] = set()

    for col_decl in table_decl.columns:
        expr, joins = build_column_expr(
            col_decl=col_decl,
            anchor=anchor,
            table_decl=table_decl,
            source_grain=source_grain,
            anchor_kind=anchor_kind,
            config=config,
            sidecar=sidecar,
            source_table_name=source_table_name,
        )
        col_exprs.append(expr)
        for j in joins:
            if j not in seen_joins:
                join_clauses.append(j)
                seen_joins.add(j)

    return col_exprs, join_clauses


def build_records_sql(
    table_decl: "TableDecl",
    source_table_name: str,
    anchor: "EffectiveAnchor | None",
    fork_path: str,
    config: "DimensionalConfig | None" = None,
    sidecar: "Sidecar | None" = None,
) -> str:
    """Build the SELECT SQL for a records grain (Type-1 dim or fact).

    Composes the reader records relation as a subquery aliased as "_grain".
    The format authors no base-table SQL.

    Applies an optional filter predicate for discriminator-split tables.
    When config and sidecar are provided, resolves fk columns via JOIN.

    Args:
        table_decl: The output table declaration.
        source_table_name: The resolved DuckDB records__<kind> table name.
        anchor: The resolved EffectiveAnchor, or None.
        fork_path: The sole branch fork_path; composes the reader relation.
        config: The dimensional config (for fk resolution), or None.
        sidecar: The open emit's sidecar; required to compose the reader relation.

    Returns:
        A complete, deterministic SELECT statement.
    """
    assert sidecar is not None, "build_records_sql: sidecar is required"
    source = table_decl.source
    col_exprs, join_clauses = _collect_column_exprs_and_joins(
        table_decl=table_decl,
        anchor=anchor,
        source_grain=source.grain,
        anchor_kind=source.kind,
        config=config,
        sidecar=sidecar,
        source_table_name=source_table_name,
    )
    select_list = ", ".join(col_exprs)

    # Compose the reader relation: the format authors no base-table SQL.
    # discriminator_filter from source.filter; the reader relation handles
    # the fork_path predicate internally.
    discriminator_filter: dict[str, str] = dict(source.filter) if source.filter else {}
    reader_sql = build_records_relation_sql(
        sidecar=sidecar,
        fork_path=fork_path,
        kind=source.kind,
        discriminator_filter=discriminator_filter,
    )
    from_clause = f'FROM ({reader_sql}) AS "_grain"'
    join_sql = (" " + " ".join(join_clauses)) if join_clauses else ""
    order_by = '"_grain"."record_id"'
    return f"SELECT {select_list} {from_clause}{join_sql} ORDER BY {order_by}"


def build_history_point_sql(
    table_decl: "TableDecl",
    anchor: "EffectiveAnchor | None",
    fork_path: str,
    config: "DimensionalConfig | None" = None,
    sidecar: "Sidecar | None" = None,
) -> str:
    """Build the SELECT SQL for a history_point grain.

    Composes the reader history relation as a subquery aliased as "_grain".
    The format authors no base-table SQL.

    Filters history by kind and property, and optionally by value.
    When config and sidecar are provided, resolves fk columns via JOIN.

    Args:
        table_decl: The output table declaration.
        anchor: The resolved EffectiveAnchor, or None.
        fork_path: The sole branch fork_path; composes the reader relation.
        config: The dimensional config (for fk resolution), or None.
        sidecar: The open emit's sidecar (for fk resolution), or None.

    Returns:
        A complete, deterministic SELECT statement.
    """
    assert sidecar is not None, "build_history_point_sql: sidecar is required"
    source = table_decl.source

    col_exprs, join_clauses = _collect_column_exprs_and_joins(
        table_decl=table_decl,
        anchor=anchor,
        source_grain=source.grain,
        anchor_kind=source.kind,
        config=config,
        sidecar=sidecar,
        source_table_name="history",
    )
    select_list = ", ".join(col_exprs)
    join_sql = (" " + " ".join(join_clauses)) if join_clauses else ""
    order_by = '"_grain"."record_id"'

    # Compose the reader relation: the format authors no base-table SQL.
    reader_sql = build_history_relation_sql(
        sidecar=sidecar,
        fork_path=fork_path,
        kind=source.kind,
        property_name=source.property or "",
        value_filter=source.value,
    )
    return (
        f"SELECT {select_list}"
        f' FROM ({reader_sql}) AS "_grain"'
        f"{join_sql}"
        f" ORDER BY {order_by}"
    )


def _grain_sql_literal(value: str) -> str:
    """Render a Python string as a single-quoted SQL literal for grain builders.

    Args:
        value: The string value to quote.

    Returns:
        A SQL single-quoted string literal with internal single-quotes escaped.
    """
    return "'" + value.replace("'", "''") + "'"


def _build_history_interval_grain_inner(
    derivation_sql: str,
    prop_col: str,
    kind: str,
    property_name: str,
    fork_path: str,
) -> str:
    """Build the inner subquery for the history-interval grain (_grain alias).

    Maps versioned-interval derivation columns to the history-grain projectable
    surface (sim_time, lead_sim_time, value, kind, property, fork_path).

    Args:
        derivation_sql: The versioned-intervals derivation SELECT.
        prop_col: The prop__<p> column name in the derivation (e.g. prop__status).
        kind: The source kind (projected as a SQL constant).
        property_name: The sole tracked property (projected as a SQL constant).
        fork_path: The sole branch fork_path (projected as a SQL constant).

    Returns:
        A SELECT fragment forming the _grain inner subquery.
    """
    k_lit = _grain_sql_literal(kind)
    p_lit = _grain_sql_literal(property_name)
    fp_lit = _grain_sql_literal(fork_path)

    return (
        f'SELECT "record_id",'
        f' "version_start" AS "sim_time",'
        f' "version_end" AS "lead_sim_time",'
        f' "{prop_col}" AS "value",'
        f' {k_lit} AS "kind",'
        f' {p_lit} AS "property",'
        f' {fp_lit} AS "fork_path"'
        f' FROM ({derivation_sql}) AS "_vi"'
    )


def build_history_interval_sql(
    table_decl: "TableDecl",
    anchor: "EffectiveAnchor | None",
    fork_path: str,
    config: "DimensionalConfig | None" = None,
    sidecar: "Sidecar | None" = None,
) -> str:
    """Build the SELECT SQL for a history_interval grain.

    Composes the versioned-intervals derivation as a subquery aliased as "_grain",
    mapping derivation columns to the history-grain projectable surface:
    version_start → sim_time, version_end → lead_sim_time, prop__<p> → value.
    The format authors no base-table SQL.

    Row order is (record_id, version_start) — the grain's true identity.

    When config and sidecar are provided, resolves fk columns via JOIN.

    Args:
        table_decl: The output table declaration.
        anchor: The resolved EffectiveAnchor, or None.
        config: The dimensional config (for fk resolution), or None.
        sidecar: The open emit's sidecar (for fk resolution and derivation); required.
        fork_path: The sole branch fork_path; composes reader relations.

    Returns:
        A complete, deterministic SELECT statement using a CTE.

    Raises:
        TableNotFoundError: records__<kind> is absent from the sidecar.
    """
    assert sidecar is not None, "build_history_interval_sql: sidecar is required"
    source = table_decl.source
    kind = source.kind
    property_name = source.property or ""

    # Build the versioned-intervals derivation for the single tracked property.
    # No discriminator filter: source.filter is records-grain-only (the config
    # model rejects it on history_interval), so the whole kind is selected.
    derivation_sql = build_versioned_intervals_sql(
        sidecar=sidecar,
        fork_path=fork_path,
        kind=kind,
        tracked_properties=frozenset({property_name}),
        discriminator_filter={},
    )

    prop_col = f"prop__{property_name}"

    grain_inner = _build_history_interval_grain_inner(
        derivation_sql=derivation_sql,
        prop_col=prop_col,
        kind=kind,
        property_name=property_name,
        fork_path=fork_path,
    )

    col_exprs, join_clauses = _collect_column_exprs_and_joins(
        table_decl=table_decl,
        anchor=anchor,
        source_grain=source.grain,
        anchor_kind=source.kind,
        config=config,
        sidecar=sidecar,
        source_table_name="history",
    )
    select_list = ", ".join(col_exprs)

    join_sql = (" " + " ".join(join_clauses)) if join_clauses else ""
    order_by = '"_grain"."record_id", "_grain"."sim_time"'

    return (
        f"SELECT {select_list}"
        f' FROM ({grain_inner}) AS "_grain"'
        f"{join_sql}"
        f" ORDER BY {order_by}"
    )


def build_membership_sql(
    table_decl: "TableDecl",
    source_table_name: str,
    sidecar: "Sidecar",
    anchor: "EffectiveAnchor | None",
    fork_path: str,
    config: "DimensionalConfig | None" = None,
) -> str:
    """Build the SELECT SQL for a membership grain.

    Composes the reader membership relation as a subquery aliased as "_grain".
    The format authors no base-table SQL.

    Applies an optional where predicate over elem__ columns to restrict bindings.
    When config is provided, resolves fk columns via JOIN.

    Args:
        table_decl: The output table declaration.
        source_table_name: The resolved DuckDB membership table name.
        sidecar: The open emit's sidecar (for elem__ column ordering).
        anchor: The resolved EffectiveAnchor, or None.
        fork_path: The sole branch fork_path; composes the reader relation.
        config: The dimensional config (for fk resolution), or None.

    Returns:
        A complete, deterministic SELECT statement.
    """
    source = table_decl.source

    col_exprs, join_clauses = _collect_column_exprs_and_joins(
        table_decl=table_decl,
        anchor=anchor,
        source_grain=source.grain,
        anchor_kind=source.kind,
        config=config,
        sidecar=sidecar,
        source_table_name=source_table_name,
    )
    select_list = ", ".join(col_exprs)
    join_sql = (" " + " ".join(join_clauses)) if join_clauses else ""

    # Compose the reader relation first — this raises TableNotFoundError if the
    # table is absent, surfacing it before any other sidecar lookup.
    where_predicate: dict[str, str] = dict(source.where) if source.where else {}
    reader_sql = build_membership_relation_sql(
        sidecar=sidecar,
        fork_path=fork_path,
        owner_kind=source.kind,
        property_name=source.property or "",
        where_predicate=where_predicate,
    )
    membership_order_cols = _membership_order_by_columns(source_table_name, sidecar)
    qualified_order = [
        f'"_grain".{c}' if not c.startswith('"_') else c
        for c in ['"record_id"'] + membership_order_cols
    ]
    order_by = ", ".join(qualified_order)
    return (
        f"SELECT {select_list}"
        f' FROM ({reader_sql}) AS "_grain"'
        f"{join_sql}"
        f" ORDER BY {order_by}"
    )


def _wrap_with_window_predicate(
    inner_sql: str,
    window_key_col: str,
    window_start_ns: int,
    window_end_ns: int,
) -> str:
    """Wrap a full-export SELECT with a half-open window predicate.

    Applies the predicate as the outermost WHERE over the full-export relation —
    after window functions and derived columns, so every emitted value equals
    its full-export value.

    Args:
        inner_sql: The full-export SELECT SQL.
        window_key_col: The column name to filter on (from the inner SELECT).
        window_start_ns: Inclusive start (ns).
        window_end_ns: Exclusive end (ns).

    Returns:
        The wrapped SELECT SQL with outer WHERE predicate.
    """
    return (
        f"SELECT * FROM ({inner_sql}) AS _windowed"
        f' WHERE "_windowed"."{window_key_col}" >= {window_start_ns}'
        f' AND "_windowed"."{window_key_col}" < {window_end_ns}'
    )


def _find_window_key_output_col(
    table_decl: "TableDecl",
    raw_key: str,
) -> str | None:
    """Find the output column name that sources the raw window-key column.

    Returns the first output column that uses from_=raw_key or
    derived.timestamp.source=raw_key, or None when none is found.

    Args:
        table_decl: The output table declaration.
        raw_key: The grain's raw window-key column name.

    Returns:
        The output column name, or None.
    """
    for col_decl in table_decl.columns:
        if col_decl.from_ == raw_key:
            return col_decl.name
        if (
            col_decl.derived is not None
            and col_decl.derived.timestamp is not None
            and col_decl.derived.timestamp.source == raw_key
        ):
            return col_decl.name
    return None


def _require_window_key_output_col(
    table_decl: "TableDecl",
    raw_key: str,
) -> str:
    """Resolve the output column projecting the grain's raw window key, or fail.

    Windowed fact export filters on the output column that projects the grain's
    raw window key. When no declared column projects it, fail fast with a clear
    pre-flight error: falling back to the raw key name would either hit an
    opaque DuckDB binder error (the outer WHERE referencing a column absent from
    the projection) or silently window on an unrelated output column that
    happens to carry the raw key's name.

    Args:
        table_decl: The output table declaration.
        raw_key: The grain's raw window-key column name.

    Returns:
        The output column name that projects raw_key.

    Raises:
        ExportError: No declared column projects raw_key.
    """
    key_col = _find_window_key_output_col(table_decl, raw_key)
    if key_col is None:
        raise ExportError(
            f"table '{table_decl.name}': windowed export requires an output"
            f" column projecting the grain's window key '{raw_key}'"
            f" (from: {raw_key}, or derived: timestamp with source: {raw_key});"
            " declare one so the window predicate can bind to it"
        )
    return key_col


def build_grain_sql(
    table_decl: "TableDecl",
    source_table_name: str,
    sidecar: "Sidecar",
    anchor: "EffectiveAnchor | None",
    fork_path: str,
    config: "DimensionalConfig | None" = None,
    window: "Window | None" = None,
) -> tuple[str, Literal["create", "append", "replace"], str | None, str | None]:
    """Dispatch a table declaration to the appropriate grain SQL builder.

    Returns the SQL, write mode, and optional view name and view SQL.

    Full export (window=None): returns (sql, 'create', None, None) — the
    existing shape unchanged.

    Windowed (window not None):
    - Records fact: append with half-open window predicate on last_mutation_sim_time.
    - history_point fact: append with predicate on sim_time.
    - SCD-2 dim with valid_to: append __rows SELECT + companion view; table_name
      becomes '<name>__rows' in the caller.
    - SCD-2 dim without valid_to: append, plain table name, no view.
    - Type-1 dim: replace, full snapshot (no predicate).

    Args:
        table_decl: The output table declaration.
        source_table_name: The resolved DuckDB source table name.
        sidecar: The open emit's sidecar.
        anchor: The resolved EffectiveAnchor, or None.
        fork_path: The sole branch fork_path; grain builders compose the reader
            relation instead of naming base tables directly.
        config: The dimensional config (for fk resolution), or None.
        window: The window to filter to, or None for full export.

    Returns:
        (sql, write_mode, view_name, view_sql)
    """
    if window is None:
        # Full export — existing behavior, write_mode='create', no views
        if table_decl.scd == "type2":
            sql = build_scd2_sql(
                table_decl, source_table_name, sidecar, anchor, fork_path
            )
        else:
            grain = table_decl.source.grain
            if grain == "records":
                sql = build_records_sql(
                    table_decl, source_table_name, anchor, fork_path, config, sidecar
                )
            elif grain == "history_point":
                sql = build_history_point_sql(
                    table_decl, anchor, fork_path, config, sidecar
                )
            elif grain == "history_interval":
                sql = build_history_interval_sql(
                    table_decl, anchor, fork_path, config, sidecar
                )
            else:
                sql = build_membership_sql(
                    table_decl, source_table_name, sidecar, anchor, fork_path, config
                )
        return sql, "create", None, None

    # Windowed dispatch
    grain = table_decl.source.grain

    # Type-1 dim: full snapshot, replace mode, no predicate
    if table_decl.role == "dim" and table_decl.scd == "type1":
        sql = build_records_sql(
            table_decl, source_table_name, anchor, fork_path, config, sidecar
        )
        return sql, "replace", None, None

    # SCD-2 dim
    if table_decl.scd == "type2":
        has_valid_to = any(
            col.derived is not None and col.derived.scd_window == "valid_to"
            for col in table_decl.columns
        )

        if has_valid_to:
            # Rows table: windowed rows without valid_to, plus __valid_from_ns
            rows_sql = build_scd2_rows_sql(
                table_decl,
                source_table_name,
                sidecar,
                anchor,
                window.start_ns,
                window.end_ns,
                fork_path,
            )
            rows_table_name = f"{table_decl.name}__rows"
            view_sql = build_scd2_view_sql(table_decl, rows_table_name)
            return rows_sql, "append", table_decl.name, view_sql
        else:
            # No valid_to: plain append, no view
            sql = build_scd2_rows_sql(
                table_decl,
                source_table_name,
                sidecar,
                anchor,
                window.start_ns,
                window.end_ns,
                fork_path,
            )
            return sql, "append", None, None

    # Facts (records and history_point grains)
    if grain == "records":
        raw_key = "last_mutation_sim_time"
        full_sql = build_records_sql(
            table_decl, source_table_name, anchor, fork_path, config, sidecar
        )
        key_col = _require_window_key_output_col(table_decl, raw_key)
        windowed_sql = _wrap_with_window_predicate(
            full_sql, key_col, window.start_ns, window.end_ns
        )
        return windowed_sql, "append", None, None

    if grain == "history_point":
        raw_key = "sim_time"
        full_sql = build_history_point_sql(
            table_decl, anchor, fork_path, config, sidecar
        )
        key_col = _require_window_key_output_col(table_decl, raw_key)
        windowed_sql = _wrap_with_window_predicate(
            full_sql, key_col, window.start_ns, window.end_ns
        )
        return windowed_sql, "append", None, None

    # Unreachable: validate_table guards history_interval/membership
    raise ExportError(
        f"table '{table_decl.name}': grain '{grain}' is not supported with"
        " windowed export"
    )
