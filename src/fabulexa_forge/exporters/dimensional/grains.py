"""Grain SQL builders for the dimensional exporter.

Each function builds the FROM / WHERE / ORDER BY clauses for one grain type.
Column expressions come from the columns module; grain builders assemble them
into complete SELECT statements.

All functions are module-level for independent testability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import DimensionalConfig, TableDecl
    from fabulexa_forge.exporters.election import Election
    from fabulexa_forge.exporters.query_spec import ColumnProvenance
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge.derivations.versioned_intervals import (
    build_versioned_intervals_sql,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.columns import (
    build_column_expr,
    build_table_provenance,
)
from fabulexa_forge.exporters.dimensional.scd import build_scd2_sql
from fabulexa_forge.reader.errors import TableNotFoundError
from fabulexa_forge.reader.relations import (
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
    election: "Election | None" = None,
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
        election: The resolved election (for fk columns), or None to resolve
            the all-default election internally.

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
            election=election,
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
    extra_col_exprs: "list[str] | None" = None,
    election: "Election | None" = None,
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
        extra_col_exprs: Additional SELECT-list expressions (already qualified
            against "_grain") appended after the declared columns — used to
            inject the internal raw-ns window-key helper for windowed export.
        election: The resolved election (for fk columns), or None to resolve
            the all-default election internally.

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
        election=election,
    )
    if extra_col_exprs:
        col_exprs = [*col_exprs, *extra_col_exprs]
    select_list = ", ".join(col_exprs)

    # Compose the reader relation: the format authors no base-table SQL.
    # discriminator_filter from source.filter; the reader relation handles
    # the fork_path predicate internally.
    discriminator_filter: dict[str, str | list[str]] = (
        dict(source.filter) if source.filter else {}
    )
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
    extra_col_exprs: "list[str] | None" = None,
    election: "Election | None" = None,
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
        extra_col_exprs: Additional SELECT-list expressions (already qualified
            against "_grain") appended after the declared columns — used to
            inject the internal raw-ns window-key helper for windowed export.
        election: The resolved election (for fk columns), or None to resolve
            the all-default election internally.

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
        election=election,
    )
    if extra_col_exprs:
        col_exprs = [*col_exprs, *extra_col_exprs]
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
    election: "Election | None" = None,
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
        election: The resolved election (for fk columns), or None to resolve
            the all-default election internally.

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
        election=election,
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
    election: "Election | None" = None,
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
        election: The resolved election (for fk columns), or None to resolve
            the all-default election internally.

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
        election=election,
    )
    select_list = ", ".join(col_exprs)
    join_sql = (" " + " ".join(join_clauses)) if join_clauses else ""

    # Compose the reader relation first — this raises TableNotFoundError if the
    # table is absent, surfacing it before any other sidecar lookup.
    where_predicate: dict[str, str | list[str]] = (
        dict(source.where) if source.where else {}
    )
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


def build_grain_sql(
    table_decl: "TableDecl",
    source_table_name: str,
    sidecar: "Sidecar",
    anchor: "EffectiveAnchor | None",
    fork_path: str,
    config: "DimensionalConfig | None" = None,
    election: "Election | None" = None,
) -> tuple[str, "Mapping[str, ColumnProvenance]"]:
    """Dispatch a table declaration to the appropriate grain SQL builder.

    Returns the full-export SQL and the per-output-column provenance map —
    one entry per faithfully carried column (projection, rename, cast-back,
    temporal/value rendering election, `lookup`), keyed by output column
    name. Computed columns (derived measures, elapsed, ordinal, SCD-2
    valid_from / valid_to, re-derived fk identity surfaces) get no entry.
    Uniform across every grain and scd: type2 — source_table_name is already
    the grain's resolved DuckDB source table (`validate_table`'s return), the
    same identity provenance stamps against. A window is never seen here: a
    windowed export runs this same compile over the truncated tape
    (`engine.build_query_specs`).

    Args:
        table_decl: The output table declaration.
        source_table_name: The resolved DuckDB source table name.
        sidecar: The open emit's sidecar.
        anchor: The resolved EffectiveAnchor, or None.
        fork_path: The sole branch fork_path; grain builders compose the reader
            relation instead of naming base tables directly.
        config: The dimensional config (for fk resolution), or None.
        election: The resolved election (for fk columns), or None to resolve
            the all-default election internally.

    Returns:
        (sql, provenance)
    """
    provenance = build_table_provenance(
        table_decl, source_table_name, table_decl.source.kind
    )
    if table_decl.scd == "type2":
        sql = build_scd2_sql(table_decl, source_table_name, sidecar, anchor, fork_path)
    else:
        grain = table_decl.source.grain
        if grain == "records":
            sql = build_records_sql(
                table_decl,
                source_table_name,
                anchor,
                fork_path,
                config,
                sidecar,
                election=election,
            )
        elif grain == "history_point":
            sql = build_history_point_sql(
                table_decl, anchor, fork_path, config, sidecar, election=election
            )
        elif grain == "history_interval":
            sql = build_history_interval_sql(
                table_decl, anchor, fork_path, config, sidecar, election=election
            )
        else:
            sql = build_membership_sql(
                table_decl,
                source_table_name,
                sidecar,
                anchor,
                fork_path,
                config,
                election=election,
            )
    return sql, provenance
