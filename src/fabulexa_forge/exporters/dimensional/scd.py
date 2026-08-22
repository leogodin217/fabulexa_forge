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

from fabulexa_forge.anchor import render_anchor_temporal_expr
from fabulexa_forge.config.models import scd_window_bound, scd_window_render
from fabulexa_forge.derivations.versioned_intervals import (
    build_versioned_intervals_sql,
)
from fabulexa_forge.exporters.dimensional.columns import (
    build_date_parse_expr,
    build_decimal_expr,
    build_json_precision_expr,
    build_timestamp_expr,
    build_value_map_expr,
    resolve_source_column_type,
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


def _column_source_name(col_decl: "ColumnDecl") -> str | None:
    """Resolve the single source column a ColumnDecl reads its value from.

    The mapping across the source-bearing spellings the type2 build admits:
    `from` -> col_decl.from_; `derived: decimal` -> decimal.from_;
    `derived: json_precision` -> json_precision.from_;
    `derived: date_parse` -> date_parse.from_;
    `derived: value_map` -> value_map.from_;
    `derived: timestamp` -> timestamp.source. Modes with no source column
    (`null`, `derived: scd_window`) return None.

    Callers pass only ColumnDecls the type2 mode gate
    (Scd2ColumnModeSupported) admits; other modes are out of contract.

    Args:
        col_decl: The output column declaration.

    Returns:
        The source column name as declared (e.g. "prop__status",
        "sim_time_created", "presentation_id"), or None when the mode reads
        no source column.
    """
    if col_decl.derived is None:
        return col_decl.from_
    derived = col_decl.derived
    if derived.decimal is not None:
        return derived.decimal.from_
    if derived.json_precision is not None:
        return derived.json_precision.from_
    if derived.date_parse is not None:
        return derived.date_parse.from_
    if derived.value_map is not None:
        return derived.value_map.from_
    if derived.timestamp is not None:
        return derived.timestamp.source
    return None


def build_scd2_column_expr_flag(
    col_decl: "ColumnDecl",
    version_alias: str,
    records_alias: str,
    tracked_props: frozenset[str],
    anchor: "EffectiveAnchor | None",
    sidecar: "Sidecar",
    source_table_name: str,
    table_label: str,
) -> str:
    """Build a SQL expression for one SCD-2 column.

    Resolves the column's source column (_column_source_name) and its class:
    a source named `prop__<p>` with `<p>` in tracked_props is tracked and
    reads per version from version_alias; every other source (constant
    prop__, structural, projection-introduced, exempt discriminator) reads
    per record from records_alias. Structural sources are never tracked —
    they never carry the prop__ prefix.

    Compilation per mode:
    - `derived: scd_window` renders the version bounds
      (version_start / version_end) through render_anchor_temporal_expr.
    - `null` emits a typed NULL.
    - A pure per-row value rendering (`derived: timestamp` / `date_parse` /
      `value_map` / `decimal` / `json_precision`) compiles through the same
      per-column builder every records-grain column uses
      (build_timestamp_expr / build_date_parse_expr / build_value_map_expr /
      build_decimal_expr / build_json_precision_expr), handed a source
      expression per the source class: tracked ->
      CAST("<version_alias>"."prop__<p>" AS <sidecar declared type>) — the
      derivation serves tracked values as codec VARCHAR; the cast is the
      same representation step the tracked `from` path performs — untracked
      -> "<records_alias>"."<src>". The rendered SQL for an untracked
      source is byte-identical to the records grain's modulo alias; for the
      same source value the rendered output is byte-identical across source
      classes (source-class-blind rendering). value_map's WHEN-predicate
      literal typing uses the source's sidecar declared type for both
      classes — matching the tracked cast.
    - `from` projects the tracked cast or the records-relation column.

    No election reads or renumbers version rows: version bounds come from
    version_alias regardless of any value election on the table
    (version structure is election-invariant).

    Args:
        col_decl: The output column declaration (a type2-admitted mode).
        version_alias: Alias of the versioned-intervals derivation subquery.
        records_alias: Alias of the records-relation subquery.
        tracked_props: History-tracked property names (without the prop__
            prefix), from _collect_tracked_props.
        anchor: The resolved EffectiveAnchor, or None.
        sidecar: The emit's typed sidecar, for source-column declared-type
            reads (tracked-path casts, value_map literal typing).
        source_table_name: The dim's source records table, for sidecar
            column reads.
        table_label: The output table name for renderer error messages.

    Returns:
        A SQL expression fragment: `<expr> AS "<col_name>"`.

    Raises:
        ExportError: source_table_name is not found in the sidecar
            (resolve_source_column_type, on paths that read a declared
            type).
    """
    if col_decl.derived is not None and col_decl.derived.scd_window is not None:
        bound = scd_window_bound(col_decl.derived.scd_window)
        render = scd_window_render(col_decl.derived.scd_window)
        col_name = "version_start" if bound == "valid_from" else "version_end"
        qualified_source = f'"{version_alias}"."{col_name}"'
        return render_anchor_temporal_expr(
            anchor, qualified_source, col_decl.name, render
        )

    if col_decl.null is not None:
        return f'CAST(NULL AS VARCHAR) AS "{col_decl.name}"'

    src = _column_source_name(col_decl)
    assert src is not None, f"column '{col_decl.name}': no source column resolved"

    is_value_map = (
        col_decl.derived is not None and col_decl.derived.value_map is not None
    )
    is_tracked = src.startswith("prop__") and src[len("prop__") :] in tracked_props

    if is_tracked:
        source_col_type = resolve_source_column_type(
            sidecar, source_table_name, src, f"tracked column '{col_decl.name}'"
        )
        source_expr = f'CAST("{version_alias}"."{src}" AS {source_col_type})'
    else:
        source_expr = f'"{records_alias}"."{src}"'
        source_col_type = (
            resolve_source_column_type(
                sidecar, source_table_name, src, f"value_map column '{col_decl.name}'"
            )
            if is_value_map
            else "VARCHAR"
        )

    if col_decl.derived is not None and col_decl.derived.timestamp is not None:
        return build_timestamp_expr(col_decl, anchor, source_expr)

    if col_decl.derived is not None and col_decl.derived.date_parse is not None:
        return build_date_parse_expr(col_decl, source_expr, table_label)

    if is_value_map:
        return build_value_map_expr(col_decl, source_expr, source_col_type)

    if col_decl.derived is not None and col_decl.derived.decimal is not None:
        return build_decimal_expr(col_decl, source_expr, table_label)

    if col_decl.derived is not None and col_decl.derived.json_precision is not None:
        return build_json_precision_expr(col_decl, source_expr, table_label)

    # `from`: the tracked cast or the records-relation column.
    return f'{source_expr} AS "{col_decl.name}"'


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

    Tracked columns read per version from the derivation's pre-computed
    prop__<p> columns; static columns LEFT JOIN the reader records relation
    on record_id. Column expressions — including the pure per-row value
    renderings, evaluated per version over tracked sources and per record
    otherwise — compile through build_scd2_column_expr_flag, which resolves
    each column's source class from the sidecar tracked set.

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

    Raises:
        ExportError: A column's declared-type read finds source_table_name
            missing from the sidecar (build_scd2_column_expr_flag).
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

    col_exprs: list[str] = []
    for col_decl in table_decl.columns:
        expr = build_scd2_column_expr_flag(
            col_decl,
            _VERSIONS_ALIAS,
            _RECORDS_ALIAS,
            tracked_props,
            anchor,
            sidecar,
            source_table_name,
            table_decl.name,
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

    Composes the versioned-intervals derivation for version bounds and
    tracked prop__<p> values, and the reader records relation for static
    columns. Column expressions — including the pure per-row value
    renderings, evaluated per version over tracked sources and per record
    otherwise — compile through build_scd2_column_expr_flag, which resolves
    each column's source class from the sidecar tracked set. The window
    predicate and __valid_from_ns read raw version bounds, untouched by any
    value election (version structure is election-invariant).

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

    Raises:
        ExportError: A column's declared-type read finds source_table_name
            missing from the sidecar (build_scd2_column_expr_flag).
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

    col_exprs: list[str] = []
    for col_decl in table_decl.columns:
        # Skip valid_to slots — not materialized in the rows table.
        if (
            col_decl.derived is not None
            and scd_window_bound(col_decl.derived.scd_window) == "valid_to"
        ):
            continue

        expr = build_scd2_column_expr_flag(
            col_decl,
            _VERSIONS_ALIAS,
            _RECORDS_ALIAS,
            tracked_props,
            anchor,
            sidecar,
            source_table_name,
            table_decl.name,
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
            if scd_window_bound(col_decl.derived.scd_window) == "valid_from":
                valid_from_col = col_decl.name

    # Identity = key minus scd_window columns
    identity_cols = [k for k in table_decl.key if k not in scd_window_col_names]

    # Partition BY for LEAD
    partition_by = ", ".join(f'"{c}"' for c in identity_cols)

    view_exprs: list[str] = []
    for col_decl in table_decl.columns:
        if (
            col_decl.derived is not None
            and scd_window_bound(col_decl.derived.scd_window) == "valid_to"
        ):
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
