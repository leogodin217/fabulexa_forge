"""Column SQL fragment builders for the dimensional exporter.

Each function produces a SQL expression fragment (and optional JOIN clauses)
for one column mode: from, correlation, derived (ordinal / value_map /
timestamp), null, fk.

All functions are module-level for independent testability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ColumnDecl, DimensionalConfig, TableDecl
    from fabulexa_forge.exporters.election import Election
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge._sql import (
    render_date_parse_expr,
    render_decimal_expr,
    render_json_precision_expr,
    render_predicate_condition,
    render_typed_literal,
)
from fabulexa_forge.anchor import render_anchor_temporal_expr
from fabulexa_forge.config.models import (
    scd_window_bound,
    scd_window_render,
    timestamp_render,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.reader.errors import TableNotFoundError

__all__ = ["render_anchor_temporal_expr", "render_typed_literal"]


def resolve_source_column_type(
    sidecar: "Sidecar",
    source_table_name: str,
    column_name: str,
    error_context: str,
) -> str:
    """Look up a source column's sidecar DuckDB type.

    Args:
        sidecar: The emit's typed sidecar.
        source_table_name: The source table to look the column up in.
        column_name: The column to resolve.
        error_context: Description of the calling column, for the ExportError
            message (e.g. "value_map column 'status_label'").

    Returns:
        The column's sidecar-declared DuckDB type, or VARCHAR when the table
        is found but the column is not (no-op cast for VARCHAR columns).

    Raises:
        ExportError: source_table_name is not found in the sidecar.
    """
    try:
        col_specs = sidecar.columns(source_table_name)
    except TableNotFoundError as exc:
        raise ExportError(
            f"{error_context}: source table '{source_table_name}' not found in sidecar"
        ) from exc
    for col_spec in col_specs:
        if col_spec.name == column_name:
            return col_spec.type
    return "VARCHAR"


def _value_map_duckdb_type(map_values: dict[str, int | float | str]) -> str:
    """Infer the DuckDB type for a value_map column from its map values.

    BIGINT when every mapped value is an int; DOUBLE when any is float;
    VARCHAR otherwise.

    Args:
        map_values: The author-supplied {source_value: output_value} mapping.

    Returns:
        A DuckDB type literal string.
    """
    has_float = False
    for v in map_values.values():
        if isinstance(v, bool):
            return "VARCHAR"
        if isinstance(v, float):
            has_float = True
        elif not isinstance(v, int):
            return "VARCHAR"
    return "DOUBLE" if has_float else "BIGINT"


def build_from_expr(col_decl: "ColumnDecl", grain_alias: str | None = None) -> str:
    """Build a SQL expression for a `from:` column (direct projection).

    Args:
        col_decl: A ColumnDecl with from_ set.
        grain_alias: Optional SQL alias for the grain table; when provided,
            qualifies the source column to avoid ambiguity with FK JOINs.

    Returns:
        A SQL expression fragment: `"<src>" AS "<name>"` or
        `"<alias>"."<src>" AS "<name>"` when grain_alias is given.
    """
    assert col_decl.from_ is not None
    src = (
        f'"{grain_alias}"."{col_decl.from_}"'
        if grain_alias is not None
        else f'"{col_decl.from_}"'
    )
    return f'{src} AS "{col_decl.name}"'


def build_correlation_expr(
    col_decl: "ColumnDecl", grain_alias: str | None = None
) -> str:
    """Build a SQL expression for a `correlation:` column (rename, no join).

    Args:
        col_decl: A ColumnDecl with correlation set.
        grain_alias: Optional SQL alias for the grain table; when provided,
            qualifies the source column to avoid ambiguity with FK JOINs.

    Returns:
        A SQL expression fragment: `"<src>" AS "<name>"` or
        `"<alias>"."<src>" AS "<name>"` when grain_alias is given.
    """
    assert col_decl.correlation is not None
    src = (
        f'"{grain_alias}"."{col_decl.correlation}"'
        if grain_alias is not None
        else f'"{col_decl.correlation}"'
    )
    return f'{src} AS "{col_decl.name}"'


def build_null_expr(col_decl: "ColumnDecl") -> str:
    """Build a SQL expression for a `null: true` column (typed NULL pad).

    Args:
        col_decl: A ColumnDecl with null=True.

    Returns:
        A SQL expression fragment: `CAST(NULL AS VARCHAR) AS "<name>"`.
    """
    return f'CAST(NULL AS VARCHAR) AS "{col_decl.name}"'


def _find_raw_ns_source_for_ordinal(
    order_by: str,
    table_decl: "TableDecl | None",
) -> str | None:
    """Return the raw-ns source column for an ordinal order_by, or None.

    The ordinal amendment: when order_by names a rendered-time sibling column
    (derived: timestamp, or scd_window: valid_from) whose election is
    monotone in its raw-ns source (timestamp / date / timestamptz, or the
    default rendering), compile ORDER BY to the column's raw ns source
    instead of the rendered column. A `time`-elected sibling is excluded —
    time-of-day is not monotone in the instant, so the rendered value orders
    correctly and raw-ns substitution would not.

    - For a derived: timestamp sibling: the source is timestamp.source.
    - For an scd_window: valid_from sibling: the source is 'version_start'
      (the CTE column name used in the SCD-2 builder).

    Args:
        order_by: The ordinal.order_by column name.
        table_decl: The enclosing table declaration, or None when unavailable.

    Returns:
        The raw-ns source column name, or None when no amendment applies.
    """
    if table_decl is None:
        return None

    for col in table_decl.columns:
        if col.name != order_by:
            continue
        if col.derived is None:
            return None
        if col.derived.timestamp is not None:
            if timestamp_render(col.derived.timestamp) == "time":
                return None
            return col.derived.timestamp.source
        if scd_window_bound(col.derived.scd_window) == "valid_from":
            assert col.derived.scd_window is not None
            if scd_window_render(col.derived.scd_window) == "time":
                return None
            return "version_start"
    return None


def build_ordinal_expr(
    col_decl: "ColumnDecl",
    grain_alias: str = "_grain",
    table_decl: "TableDecl | None" = None,
) -> str:
    """Build a SQL expression for a `derived: ordinal` column (ROW_NUMBER).

    Appends record_id as the final ORDER BY tie-break after the declared order_by.
    The partition_by/order_by reference sibling output column names, not grain
    surface columns, so they are left unqualified (window function scope).

    Ordinal amendment: when order_by names a sibling rendered-time column
    (derived: timestamp or scd_window: valid_from), the ORDER BY compiles to
    that column's raw ns source, then record_id — full and windowed export alike.
    This ensures same-microsecond rows order by true event order.

    Args:
        col_decl: A ColumnDecl with derived.ordinal set.
        grain_alias: SQL alias for the grain table (used for record_id tie-break).
        table_decl: The enclosing table declaration; enables the ordinal amendment
            when the order_by names a rendered-time sibling.

    Returns:
        A SQL expression fragment with ROW_NUMBER() OVER (...).
    """
    assert col_decl.derived is not None and col_decl.derived.ordinal is not None
    ordinal = col_decl.derived.ordinal

    raw_src = _find_raw_ns_source_for_ordinal(ordinal.order_by, table_decl)
    if raw_src is not None:
        order_by_sql = f'"{grain_alias}"."{raw_src}"'
    else:
        order_by_sql = f'"{ordinal.order_by}"'

    return (
        f"ROW_NUMBER() OVER ("
        f'PARTITION BY "{ordinal.partition_by}" '
        f'ORDER BY {order_by_sql}, "{grain_alias}"."record_id"'
        f') AS "{col_decl.name}"'
    )


def build_value_map_expr(
    col_decl: "ColumnDecl",
    grain_alias: str = "_grain",
    source_col_type: str = "VARCHAR",
) -> str:
    """Build a SQL expression for a `derived: value_map` column (CASE).

    Types every branch (including the unmapped NULL) to the inferred DuckDB type.
    The WHEN comparison side uses render_typed_literal so the predicate literal
    matches the source column's DuckDB type.

    Args:
        col_decl: A ColumnDecl with derived.value_map set.
        grain_alias: SQL alias for the grain table (qualifies the source column).
        source_col_type: DuckDB type of the source column (for WHEN predicate
            literal typing). Defaults to VARCHAR.

    Returns:
        A SQL CASE expression fragment.
    """
    assert col_decl.derived is not None and col_decl.derived.value_map is not None
    vm = col_decl.derived.value_map
    duckdb_type = _value_map_duckdb_type(vm.map)

    when_clauses = []
    for src_val, out_val in vm.map.items():
        src_literal = render_typed_literal(src_val, source_col_type)
        if isinstance(out_val, str):
            out_literal = f"'{out_val}'"
        elif isinstance(out_val, bool):
            out_literal = "TRUE" if out_val else "FALSE"
        elif isinstance(out_val, int):
            out_literal = str(out_val)
        else:
            out_literal = str(out_val)
        when_clauses.append(
            f'WHEN "{grain_alias}"."{vm.from_}" = {src_literal}'
            f" THEN CAST({out_literal} AS {duckdb_type})"
        )

    when_sql = " ".join(when_clauses)
    null_cast = f"CAST(NULL AS {duckdb_type})"
    return f'CASE {when_sql} ELSE {null_cast} END AS "{col_decl.name}"'


def build_timestamp_expr(
    col_decl: "ColumnDecl",
    anchor: "EffectiveAnchor | None",
    grain_alias: str = "_grain",
) -> str:
    """Build a SQL expression for a `derived: timestamp` column.

    When an anchor is present, renders the elected wallclock type (absent
    `as` = the mode-definitional default `timestamp` rendering) via
    `render_anchor_temporal_expr`. When absent, returns the raw sim_time
    integer column (the caller enforces `TemporalRenderRequiresAnchor` for
    any explicit election before this runs).

    Args:
        col_decl: A ColumnDecl with derived.timestamp set.
        anchor: The resolved EffectiveAnchor, or None when absent.
        grain_alias: SQL alias for the grain table (qualifies the source column).

    Returns:
        A SQL expression fragment.
    """
    assert col_decl.derived is not None and col_decl.derived.timestamp is not None
    ts = col_decl.derived.timestamp
    src = ts.source
    qualified_source = f'"{grain_alias}"."{src}"'
    return render_anchor_temporal_expr(
        anchor, qualified_source, col_decl.name, timestamp_render(ts)
    )


_ELAPSED_DIVISORS: dict[str, int] = {
    "minutes": 60_000_000_000,
    "seconds": 1_000_000_000,
    "hours": 3_600_000_000_000,
}


def build_elapsed_expr(
    col_decl: "ColumnDecl",
    source_table_name: str,
    sidecar: "Sidecar",
    grain_alias: str = "_grain",
) -> tuple[str, list[str]]:
    """Build a SQL expression for a `derived: elapsed` column (cross-row time delta).

    Resolves the counterpart row via a LEFT JOIN to a pre-aggregated subquery
    aliased as ``_el_<colname>``.  The subquery selects MIN(start_source) for each
    correlate_on group filtered by other_where, so duplicates are handled
    deterministically (earliest row wins) and there is no fan-out.

    Renders one of two exclusive forms per ElapsedSpec.exactly_one_rendering:
    a numeric DOUBLE at the declared `unit` (today's rendering), or a
    µs-precision INTERVAL (`as: interval`) — sign-preserving, equal to the
    numeric rendering at µs.

    Args:
        col_decl: A ColumnDecl with derived.elapsed set.
        source_table_name: The resolved DuckDB source table name.
        sidecar: The open emit's sidecar (for other_where column type lookup).
        grain_alias: SQL alias for the grain table (default: "_grain").

    Returns:
        (select_expr, [join_clause]) — the SELECT expression and a single JOIN clause.

    Raises:
        ExportError: source_table_name is not found in the sidecar.
    """
    assert col_decl.derived is not None and col_decl.derived.elapsed is not None
    el = col_decl.derived.elapsed
    col_name = col_decl.name
    subquery_alias = f"_el_{col_name}"

    # Resolve column types for other_where literals
    try:
        col_types = {cs.name: cs.type for cs in sidecar.columns(source_table_name)}
    except Exception as exc:
        from fabulexa_forge.reader.errors import TableNotFoundError

        if isinstance(exc, TableNotFoundError):
            raise ExportError(
                f"elapsed column '{col_name}': source table"
                f" '{source_table_name}' not found in sidecar"
            ) from exc
        raise

    # Build the other_where filter for the subquery (there is exactly one k/v pair
    # per the spec, but the model allows multiple; handle all of them)
    where_parts = []
    for disc_col, disc_val in el.other_where.items():
        # disc_col existence is guaranteed by check_elapsed_columns_exist at
        # validate_table; hard lookup fails loud if that invariant is ever bypassed.
        col_type = col_types[disc_col]
        where_parts.append(
            render_predicate_condition(disc_col, disc_val, col_type, None)
        )
    where_sql = " AND ".join(where_parts)

    join_clause = (
        f"LEFT JOIN ("
        f'SELECT "{el.correlate_on}" AS corr,'
        f' MIN(CAST("{el.start_source}" AS BIGINT)) AS start_ns'
        f' FROM "{source_table_name}"'
        f" WHERE {where_sql}"
        f' GROUP BY "{el.correlate_on}"'
        f') AS "{subquery_alias}"'
        f' ON "{subquery_alias}".corr = "{grain_alias}"."{el.correlate_on}"'
    )

    delta_ns = (
        f'(CAST("{grain_alias}"."{el.end_source}" AS BIGINT)'
        f' - "{subquery_alias}".start_ns)'
    )
    if el.as_ == "interval":
        select_expr = f'to_microseconds({delta_ns} // 1000) AS "{col_name}"'
    else:
        assert el.unit is not None
        div = _ELAPSED_DIVISORS[el.unit]
        select_expr = f'{delta_ns} / {div} AS "{col_name}"'

    return select_expr, [join_clause]


def build_date_parse_expr(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    grain_alias: str = "_grain",
) -> str:
    """Build a SQL expression for a `derived: date_parse` column.

    Delegates to `render_date_parse_expr` — the one VARCHAR->DATE parse
    renderer every mode shares. Type and existence gates run at plan time
    (DateParseSourceColumn, ProjectionColumnExists); this builder assumes
    both already passed.

    Args:
        col_decl: A ColumnDecl with derived.date_parse set.
        table_decl: The enclosing table declaration (supplies the guard's
            table label).
        grain_alias: SQL alias for the grain table (qualifies the source column).

    Returns:
        A SQL expression fragment.
    """
    assert col_decl.derived is not None and col_decl.derived.date_parse is not None
    dp = col_decl.derived.date_parse
    qualified_source = f'"{grain_alias}"."{dp.from_}"'
    return render_date_parse_expr(
        qualified_source, dp.format, col_decl.name, table_decl.name
    )


def build_decimal_expr(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    grain_alias: str = "_grain",
) -> str:
    """Build a SQL expression for a `derived: decimal` column.

    Delegates to `render_decimal_expr` — the one decimal rendering authority
    every mode shares. The source-type gate (DecimalSourceIsDouble) runs at
    plan time; this builder assumes it already passed.

    Args:
        col_decl: A ColumnDecl with derived.decimal set.
        table_decl: The enclosing table declaration (supplies the guard's
            table label).
        grain_alias: SQL alias for the grain table (qualifies the source column).

    Returns:
        A SQL expression fragment.
    """
    assert col_decl.derived is not None and col_decl.derived.decimal is not None
    dec = col_decl.derived.decimal
    qualified_source = f'"{grain_alias}"."{dec.from_}"'
    precision, scale = dec.as_
    expr = render_decimal_expr(
        qualified_source, precision, scale, col_decl.name, table_decl.name
    )
    return f'{expr} AS "{col_decl.name}"'


def build_json_precision_expr(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    grain_alias: str = "_grain",
) -> str:
    """Build a SQL expression for a `derived: json_precision` column.

    Delegates to `render_json_precision_expr` — the one json_precision
    rendering authority every mode shares. The source-type gate
    (JsonPrecisionSourceIsVarchar) runs at plan time; this builder assumes it
    already passed.

    Args:
        col_decl: A ColumnDecl with derived.json_precision set.
        table_decl: The enclosing table declaration (supplies the guard's
            table label).
        grain_alias: SQL alias for the grain table (qualifies the source column).

    Returns:
        A SQL expression fragment.
    """
    assert col_decl.derived is not None and col_decl.derived.json_precision is not None
    jp = col_decl.derived.json_precision
    qualified_source = f'"{grain_alias}"."{jp.from_}"'
    expr = render_json_precision_expr(
        qualified_source, jp.leaves, col_decl.name, table_decl.name
    )
    return f'{expr} AS "{col_decl.name}"'


def build_column_expr(
    col_decl: "ColumnDecl",
    anchor: "EffectiveAnchor | None",
    table_decl: "TableDecl | None" = None,
    source_grain: str | None = None,
    anchor_kind: str | None = None,
    config: "DimensionalConfig | None" = None,
    sidecar: "Sidecar | None" = None,
    election: "Election | None" = None,
    grain_alias: str = "_grain",
    source_table_name: str | None = None,
) -> tuple[str, list[str]]:
    """Dispatch a ColumnDecl to the appropriate expression builder.

    Returns a (select_expr, join_clauses) pair. For non-FK columns,
    join_clauses is always empty. For fk columns, join_clauses contains
    the LEFT JOIN fragment(s) required to resolve the path.

    Non-FK grain columns are qualified with grain_alias to avoid ambiguity
    when FK JOINs introduce additional tables (e.g. both the grain and a
    joined records table expose a `record_id` column).

    Args:
        col_decl: The column declaration (exactly one mode set).
        anchor: The resolved EffectiveAnchor, or None.
        table_decl: The output table declaration (required for fk columns).
        source_grain: The grain type string (required for fk columns).
        anchor_kind: The record kind of the grain's anchor (required for fk).
        config: The dimensional config (required for fk columns).
        sidecar: The open emit's sidecar (required for fk columns).
        election: The resolved election (for fk columns), or None to
            resolve the all-default election internally (every population
            elects record_id).
        grain_alias: SQL alias of the base grain table (default: "_grain").
            Used to qualify from/correlation column references.
        source_table_name: The resolved DuckDB source table name, used to look
            up the DuckDB type of value_map.from_ for WHEN predicate typing.
            When None, value_map predicates use VARCHAR.

    Returns:
        (select_expr, join_clauses) — join_clauses is [] for non-FK modes.

    Raises:
        AssertionError: No column mode is set (parse-time validators prevent this).
    """
    if col_decl.from_ is not None:
        return build_from_expr(col_decl, grain_alias), []
    if col_decl.correlation is not None:
        return build_correlation_expr(col_decl, grain_alias), []
    if col_decl.null is not None:
        return build_null_expr(col_decl), []
    if col_decl.fk is not None:
        assert table_decl is not None, "table_decl required for fk column"
        assert source_grain is not None, "source_grain required for fk column"
        assert anchor_kind is not None, "anchor_kind required for fk column"
        assert config is not None, "config required for fk column"
        assert sidecar is not None, "sidecar required for fk column"
        from fabulexa_forge.exporters.dimensional.fk import (
            build_fk_expr,
            check_fk_target_is_dim,
        )
        from fabulexa_forge.exporters.dimensional.populations import (
            resolve_dim_source_populations,
            resolve_fk_surface,
        )

        target_table_decl = check_fk_target_is_dim(col_decl, table_decl, config)
        target_kind = target_table_decl.source.kind
        resolved_election = (
            election if election is not None else resolve_election(sidecar, None)
        )
        dim_populations = resolve_dim_source_populations(
            sidecar, target_kind, target_table_decl.source.filter
        )
        resolved_surface = resolve_fk_surface(
            resolved_election,
            dim_populations,
            col_decl.fk.target_key,
            f"{table_decl.name}.{col_decl.name}",
        )
        return build_fk_expr(
            col_decl=col_decl,
            table_decl=table_decl,
            source_grain=source_grain,
            anchor_kind=anchor_kind,
            target_kind=target_kind,
            sidecar=sidecar,
            resolved_surface=resolved_surface,
            dim_populations=dim_populations,
        )
    if col_decl.derived is not None:
        derived = col_decl.derived
        if derived.ordinal is not None:
            return build_ordinal_expr(col_decl, grain_alias, table_decl), []
        if derived.value_map is not None:
            # Resolve source column type for WHEN predicate literal typing
            source_col_type = "VARCHAR"
            if sidecar is not None and source_table_name is not None:
                source_col_type = resolve_source_column_type(
                    sidecar,
                    source_table_name,
                    derived.value_map.from_,
                    f"value_map column '{col_decl.name}'",
                )
            return build_value_map_expr(col_decl, grain_alias, source_col_type), []
        if derived.timestamp is not None:
            return build_timestamp_expr(col_decl, anchor, grain_alias), []
        if derived.elapsed is not None:
            assert sidecar is not None, "sidecar required for elapsed column"
            assert source_table_name is not None, (
                "source_table_name required for elapsed column"
            )
            return build_elapsed_expr(col_decl, source_table_name, sidecar, grain_alias)
        if derived.date_parse is not None:
            assert table_decl is not None, "table_decl required for date_parse column"
            return build_date_parse_expr(col_decl, table_decl, grain_alias), []
        if derived.decimal is not None:
            assert table_decl is not None, "table_decl required for decimal column"
            return build_decimal_expr(col_decl, table_decl, grain_alias), []
        if derived.json_precision is not None:
            assert table_decl is not None, (
                "table_decl required for json_precision column"
            )
            return build_json_precision_expr(col_decl, table_decl, grain_alias), []
        # scd_window columns are assembled by the SCD-2 builder in scd.py;
        # if reached here the caller passed an scd_window column outside that path
        raise AssertionError(f"unsupported derived spec on column '{col_decl.name}'")
    if col_decl.lookup is not None:
        assert table_decl is not None, "table_decl required for lookup column"
        assert source_grain is not None, "source_grain required for lookup column"
        assert anchor_kind is not None, "anchor_kind required for lookup column"
        assert sidecar is not None, "sidecar required for lookup column"
        from fabulexa_forge.exporters.dimensional.lookup import build_lookup_expr

        return build_lookup_expr(
            col_decl=col_decl,
            table_decl=table_decl,
            anchor_kind=anchor_kind,
            anchor_alias=grain_alias,
            source_grain=source_grain,
            sidecar=sidecar,
        )
    raise AssertionError(f"no column mode set on '{col_decl.name}'")
