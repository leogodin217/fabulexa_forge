"""Business-rule validation for the dimensional exporter.

Enforces all business rules at build_query_specs time:
SourceTableExists, KeyColumnsDeclared, ProjectionColumnExists,
OrdinalRefsSiblings, TimestampSourceAvailable, DiscriminatorValueObserved,
ExcludedKindNotSourced, ExcludedTableNotSourced, FkTargetIsDim,
ReferencePathResolvable, MembershipEdgeResolvable, Scd2NeedsHistory,
Scd2ColumnModeSupported, SliceOnlyColumnRefused
(filter keys, column reads, and fk hops), ReservedPresentationName
(last_mutation_sim_time — always-on, full export included).

The SingleBranch rule is enforced by derivations.require_single_branch (the
stage-wide guard); dimensional calls it but does not own it. The windowed
rules (WindowKeyMutable, WindowKeyDuplicate, IncrementalReservedName) live in
`windowing.py`; every rule here is always-on.

Each rule is a module-level function taking only what it needs, so each is
independently testable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import (
        ColumnDecl,
        DimensionalConfig,
        KeySurface,
        SourceDecl,
        TableDecl,
    )
    from fabulexa_forge.exporters.election import Election
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge.config.models import (
    ScdWindowSpec,
    scd_window_bound,
)
from fabulexa_forge.errors import (
    DateParseSourceColumn,
    DecimalSourceIsDouble,
    ElectionDimKeyDisagrees,
    ExportError,
    JsonPrecisionSourceIsVarchar,
    TemporalRenderRequiresAnchor,
)
from fabulexa_forge.exporters.dimensional.fk import check_fk_target_is_dim
from fabulexa_forge.exporters.dimensional.populations import (
    dim_key_projects_surface,
    dim_population_sub_types,
    resolve_dim_source_populations,
    resolve_fk_surface,
)
from fabulexa_forge.exporters.election import check_edge_union_safety, resolve_election
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.reserved_names import (
    RESERVED_PRESENTATION_COLUMN_NAME,
)
from fabulexa_forge.exporters.slice_only import (
    is_non_exempt_slice_only,
    slice_only_refusal_message,
)
from fabulexa_forge.reader.errors import TableNotFoundError
from fabulexa_forge.reader.records_columns import (
    structural_instant_columns,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Grain surface helpers
# ---------------------------------------------------------------------------

#: Columns always available on a records grain surface.
_RECORDS_BASE_COLS: frozenset[str] = frozenset(
    {
        "fork_path",
        "record_id",
        "active",
        "deactivated_at",
        "last_mutation_sim_time",
    }
)

#: Columns always available on history_point / history_interval grain surfaces.
_HISTORY_BASE_COLS: frozenset[str] = frozenset(
    {
        "fork_path",
        "kind",
        "record_id",
        "property",
        "sim_time",
        "value",
    }
)

#: Columns always available on a membership grain surface.
_MEMBERSHIP_BASE_COLS: frozenset[str] = frozenset(
    {
        "fork_path",
        "record_id",
        "joined_sim_time",
        "left_sim_time",
    }
)

#: The one virtual column added by history_interval.
_LEAD_SIM_TIME = "lead_sim_time"

#: Each grain's sidecar table category, for resolving timestamp sources
#: through the reader's structural-temporal surface. history_interval shares
#: history_point's category (both read the `history` table); its virtual
#: `lead_sim_time` column is dimensional's own, layered in separately below.
_GRAIN_CATEGORY: dict[str, str] = {
    "records": "records",
    "history_point": "fixed",
    "history_interval": "fixed",
    "membership": "membership",
}

#: Timestamp sources available per grain: each grain's category's structural
#: instant columns, resolved through the reader (`structural_instant_columns`),
#: plus the virtual `lead_sim_time` for history_interval only.
_TIMESTAMP_SOURCES_BY_GRAIN: dict[str, frozenset[str]] = {
    grain: (
        frozenset(structural_instant_columns(category)) | {_LEAD_SIM_TIME}
        if grain == "history_interval"
        else frozenset(structural_instant_columns(category))
    )
    for grain, category in _GRAIN_CATEGORY.items()
}


def _resolve_source_table_name(source: "SourceDecl") -> str:
    """Return the expected sidecar table name for a source declaration.

    Args:
        source: The grain source binding.

    Returns:
        The DuckDB table name this source maps to.
    """
    grain = source.grain
    if grain == "records":
        return f"records__{source.kind}"
    if grain in ("history_point", "history_interval"):
        return "history"
    # membership
    return f"membership__{source.kind}__{source.property}"


def _grain_projectable_surface(
    source: "SourceDecl",
    sidecar: "Sidecar",
    table_name: str,
) -> frozenset[str]:
    """Return the set of column names projectable from a grain's source.

    Includes sidecar-declared columns for the grain's DuckDB table plus the
    virtual lead_sim_time for history_interval.

    Args:
        source: The grain source binding.
        sidecar: The open emit's sidecar.
        table_name: The resolved DuckDB source table name.

    Returns:
        All column names available for from/correlation projection.
    """
    try:
        sidecar_cols = {col.name for col in sidecar.columns(table_name)}
    except TableNotFoundError as exc:
        raise ExportError(
            f"Source table '{table_name}' not found while resolving available columns"
            f" for validation; check the source binding in your scenario config."
        ) from exc

    grain = source.grain
    if grain == "records":
        surface = _RECORDS_BASE_COLS | sidecar_cols
    elif grain == "history_point":
        surface = _HISTORY_BASE_COLS | sidecar_cols
    elif grain == "history_interval":
        surface = _HISTORY_BASE_COLS | sidecar_cols | {_LEAD_SIM_TIME}
    else:
        surface = _MEMBERSHIP_BASE_COLS | sidecar_cols

    return frozenset(surface)


# ---------------------------------------------------------------------------
# Business rules
# ---------------------------------------------------------------------------


def check_source_table_exists(
    source: "SourceDecl",
    sidecar: "Sidecar",
) -> str:
    """Enforce SourceTableExists: the grain's DuckDB table is in the sidecar.

    Args:
        source: The grain source binding.
        sidecar: The open emit's sidecar.

    Returns:
        The resolved DuckDB table name.

    Raises:
        ExportError: The expected source table is not in the sidecar.
    """
    table_name = _resolve_source_table_name(source)
    known = {t.name for t in sidecar.tables()}
    if table_name not in known:
        raise ExportError(
            f"table for source kind '{source.kind}' (grain '{source.grain}')"
            " not found in emit"
        )
    return table_name


def check_key_columns_declared(
    table_decl: "TableDecl",
) -> None:
    """Enforce KeyColumnsDeclared: each key entry names a declared column.

    Args:
        table_decl: The output table declaration.

    Raises:
        ExportError: A key column names an undeclared output column.
    """
    declared = {col.name for col in table_decl.columns}
    for key_col in table_decl.key:
        if key_col not in declared:
            raise ExportError(
                f"key column '{key_col}' is not declared in table '{table_decl.name}'"
            )


def check_projection_column_exists(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    surface: frozenset[str],
) -> None:
    """Enforce ProjectionColumnExists for a single column declaration.

    Checks from, correlation, derived.value_map.from, derived.date_parse.from,
    derived.decimal.from, and derived.json_precision.from against the grain
    surface — each derived `from` resolves off the grain's projectable
    surface exactly as `value_map.from` does.

    Args:
        col_decl: The column declaration being validated.
        table_decl: The output table declaration (for error messages).
        surface: The set of projectable column names for the grain.

    Raises:
        ExportError: A referenced source column is absent from the grain surface.
    """
    src: str | None = None
    if col_decl.from_ is not None:
        src = col_decl.from_
    elif col_decl.correlation is not None:
        src = col_decl.correlation
    elif col_decl.derived is not None and col_decl.derived.value_map is not None:
        src = col_decl.derived.value_map.from_
    elif col_decl.derived is not None and col_decl.derived.date_parse is not None:
        src = col_decl.derived.date_parse.from_
    elif col_decl.derived is not None and col_decl.derived.decimal is not None:
        src = col_decl.derived.decimal.from_
    elif col_decl.derived is not None and col_decl.derived.json_precision is not None:
        src = col_decl.derived.json_precision.from_

    if src is not None and src not in surface:
        raise ExportError(
            f"column '{src}' not found on source of table '{table_decl.name}'"
        )


def check_ordinal_refs_siblings(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
) -> None:
    """Enforce OrdinalRefsSiblings: ordinal partition_by/order_by name sibling columns.

    Args:
        col_decl: The column declaration containing an ordinal derived spec.
        table_decl: The output table declaration (the sibling set).

    Raises:
        ExportError: An ordinal references a column not declared in the same table.
    """
    if col_decl.derived is None or col_decl.derived.ordinal is None:
        return
    ordinal = col_decl.derived.ordinal
    declared = {c.name for c in table_decl.columns}
    for ref in (ordinal.partition_by, ordinal.order_by):
        if ref not in declared:
            raise ExportError(
                f"ordinal in '{table_decl.name}.{col_decl.name}'"
                f" references undeclared column '{ref}'"
            )


def check_timestamp_source_available(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    source: "SourceDecl",
    surface: frozenset[str],
) -> None:
    """Enforce TimestampSourceAvailable: derived timestamp source is on the grain.

    The accepted timestamp sources per grain are the structural columns only
    (`_TIMESTAMP_SOURCES_BY_GRAIN`) plus any `prop__<t>` source present on the
    grain surface. `created_by_sim_time` is no longer accepted — it cannot occur
    in a sanitised emit, so it now fails like any unavailable source.

    Args:
        col_decl: The column declaration with a timestamp derived spec.
        table_decl: The output table declaration (for error messages).
        source: The grain source binding.
        surface: The projectable column surface for the grain.

    Raises:
        ExportError: The timestamp source is not available on this grain.
    """
    if col_decl.derived is None or col_decl.derived.timestamp is None:
        return

    ts_source = col_decl.derived.timestamp.source
    grain = source.grain

    # The fixed per-grain allowed set
    grain_fixed = _TIMESTAMP_SOURCES_BY_GRAIN.get(grain, frozenset())

    if ts_source in grain_fixed:
        return

    # A prop__<t> source: must exist on the grain surface (like ProjectionColumnExists)
    if ts_source.startswith("prop__"):
        if ts_source in surface:
            return
        raise ExportError(
            f"timestamp source '{ts_source}' is not available on grain"
            f" '{grain}' for '{table_decl.name}.{col_decl.name}'"
        )

    # lead_sim_time: history_interval only (already in grain_fixed if so)
    # joined_sim_time / left_sim_time: membership only (already in grain_fixed)
    raise ExportError(
        f"timestamp source '{ts_source}' is not available on grain"
        f" '{grain}' for '{table_decl.name}.{col_decl.name}'"
    )


def check_temporal_render_requires_anchor(
    col_decl: "ColumnDecl",
    anchor: "EffectiveAnchor | None",
) -> None:
    """Enforce TemporalRenderRequiresAnchor: an explicit election needs an anchor.

    Covers `derived: timestamp` with `as` set (the mode-definitional default
    `timestamp` rendering, absence detection, is exempt) and the
    `scd_window` object form (always an explicit election — the object form
    exists to elect). `elapsed: interval` and `date_parse` are exempt: a
    duration is a physical delta and a parse reads no sim_time (§ Anchor
    requirement).

    Args:
        col_decl: The column declaration.
        anchor: The resolved EffectiveAnchor, or None.

    Raises:
        TemporalRenderRequiresAnchor: An explicit election is set and no
            anchor resolved.
    """
    if anchor is not None or col_decl.derived is None:
        return

    derived = col_decl.derived
    render: str | None = None
    if derived.timestamp is not None and derived.timestamp.as_ is not None:
        render = derived.timestamp.as_
    elif isinstance(derived.scd_window, ScdWindowSpec):
        render = derived.scd_window.as_

    if render is not None:
        raise TemporalRenderRequiresAnchor(
            f"column '{col_decl.name}': temporal rendering '{render}' requires"
            " a resolved anchor; this emit declares no runtime calendar and"
            " none was supplied"
        )


def _resolve_grain_source_type(
    from_: str,
    source_table_name: str,
    sidecar: "Sidecar",
) -> str | None:
    """Resolve a grain-surface source column's declared DuckDB type, or None.

    Reads the sidecar's column list for the resolved source table — the same
    type authority `value_map`'s literal typing reads, which already covers
    the history_interval grain's `value` alias (its source table is
    'history', whose sidecar `value` column carries the type). A structural,
    virtual, or grain-constant source with no matching sidecar column
    returns None — no declared type behind it. Shared by the three typed
    derived-source gates (`date_parse`, `decimal`, `json_precision`).

    Args:
        from_: The source column's name (source spelling).
        source_table_name: The resolved DuckDB source table name.
        sidecar: The open emit's sidecar.

    Returns:
        The column's declared DuckDB type, or None when no sidecar column
        by that name exists on the source table.
    """
    try:
        cols = sidecar.columns(source_table_name)
    except TableNotFoundError:
        return None
    for col_spec in cols:
        if col_spec.name == from_:
            return col_spec.type
    return None


def check_date_parse_source_column(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    source_table_name: str,
    sidecar: "Sidecar",
) -> None:
    """Enforce DateParseSourceColumn: the parse source is a declared VARCHAR column.

    Existence/resolution is ProjectionColumnExists' (`from` resolves off the
    grain's projectable surface exactly as `value_map.from` does); this rule
    additionally requires the resolved column carry a declared VARCHAR type.

    Args:
        col_decl: The column declaration potentially containing a date_parse spec.
        table_decl: The output table declaration (for error messages).
        source_table_name: The resolved DuckDB source table name.
        sidecar: The open emit's sidecar.

    Raises:
        DateParseSourceColumn: The resolved source column carries no declared
            VARCHAR type.
    """
    if col_decl.derived is None or col_decl.derived.date_parse is None:
        return

    dp_from = col_decl.derived.date_parse.from_
    resolved_type = _resolve_grain_source_type(dp_from, source_table_name, sidecar)
    if resolved_type is None or resolved_type.upper() != "VARCHAR":
        got = resolved_type if resolved_type is not None else "no declared type"
        raise DateParseSourceColumn(
            f"date_parse column '{col_decl.name}' on '{table_decl.name}':"
            f" source must be an existing VARCHAR column (got {got})"
        )


def check_decimal_source_column(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    source_table_name: str,
    sidecar: "Sidecar",
) -> None:
    """Enforce DecimalSourceIsDouble: the decimal source is a declared DOUBLE column.

    Existence/resolution is ProjectionColumnExists' (`from` resolves off the
    grain's projectable surface exactly as `value_map.from` does); this rule
    additionally requires the resolved column carry a declared DOUBLE type —
    the contract's one floating-point type.

    Args:
        col_decl: The column declaration potentially containing a decimal spec.
        table_decl: The output table declaration (for error messages).
        source_table_name: The resolved DuckDB source table name.
        sidecar: The open emit's sidecar.

    Raises:
        DecimalSourceIsDouble: The resolved source column carries no declared
            DOUBLE type.
    """
    if col_decl.derived is None or col_decl.derived.decimal is None:
        return

    dec_from = col_decl.derived.decimal.from_
    resolved_type = _resolve_grain_source_type(dec_from, source_table_name, sidecar)
    if resolved_type is None or resolved_type.upper() != "DOUBLE":
        got = resolved_type if resolved_type is not None else "no declared type"
        raise DecimalSourceIsDouble(
            f"decimal column '{col_decl.name}' on '{table_decl.name}':"
            f" source must be an existing DOUBLE column (got {got})"
        )


def check_json_precision_source_column(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    source_table_name: str,
    sidecar: "Sidecar",
) -> None:
    """Enforce JsonPrecisionSourceIsVarchar: the source is a declared VARCHAR column.

    Existence/resolution is ProjectionColumnExists' (`from` resolves off the
    grain's projectable surface exactly as `value_map.from` does); this rule
    additionally requires the resolved column carry a declared VARCHAR type.

    Args:
        col_decl: The column declaration potentially containing a
            json_precision spec.
        table_decl: The output table declaration (for error messages).
        source_table_name: The resolved DuckDB source table name.
        sidecar: The open emit's sidecar.

    Raises:
        JsonPrecisionSourceIsVarchar: The resolved source column carries no
            declared VARCHAR type.
    """
    if col_decl.derived is None or col_decl.derived.json_precision is None:
        return

    jp_from = col_decl.derived.json_precision.from_
    resolved_type = _resolve_grain_source_type(jp_from, source_table_name, sidecar)
    if resolved_type is None or resolved_type.upper() != "VARCHAR":
        got = resolved_type if resolved_type is not None else "no declared type"
        raise JsonPrecisionSourceIsVarchar(
            f"json_precision column '{col_decl.name}' on '{table_decl.name}':"
            f" source must be an existing VARCHAR column (got {got})"
        )


def check_elapsed_columns_exist(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    source_table_name: str,
    sidecar: "Sidecar",
) -> None:
    """Enforce ElapsedColumnsExist: all elapsed column references exist on the table.

    Validates that correlate_on, start_source, end_source, and every key in
    other_where all exist as columns on the source table.

    Args:
        col_decl: The column declaration potentially containing an elapsed spec.
        table_decl: The output table declaration (for error messages).
        source_table_name: The resolved DuckDB source table name.
        sidecar: The open emit's sidecar (to look up column names).

    Raises:
        ExportError: Any referenced column is absent from the source table.
    """
    if col_decl.derived is None or col_decl.derived.elapsed is None:
        return

    el = col_decl.derived.elapsed
    try:
        col_names = {cs.name for cs in sidecar.columns(source_table_name)}
    except Exception as exc:
        from fabulexa_forge.reader.errors import TableNotFoundError

        if isinstance(exc, TableNotFoundError):
            raise ExportError(
                f"elapsed column '{col_decl.name}' in '{table_decl.name}':"
                f" source table '{source_table_name}' not found in sidecar"
            ) from exc
        raise

    for ref_name, ref_col in [
        ("correlate_on", el.correlate_on),
        ("start_source", el.start_source),
        ("end_source", el.end_source),
    ]:
        if ref_col not in col_names:
            raise ExportError(
                f"elapsed column '{col_decl.name}' in '{table_decl.name}':"
                f" {ref_name} '{ref_col}' not found"
                f" on source table '{source_table_name}'"
            )

    for disc_col in el.other_where:
        if disc_col not in col_names:
            raise ExportError(
                f"elapsed column '{col_decl.name}' in '{table_decl.name}':"
                f" other_where key '{disc_col}' not found"
                f" on source table '{source_table_name}'"
            )


def _predicate_elements(value: str | list[str]) -> list[str]:
    """Normalize a predicate value to its element list, in config order.

    Args:
        value: A scalar (treated as a one-element list) or a list.

    Returns:
        The value's elements, in order.
    """
    return [value] if isinstance(value, str) else list(value)


def _unobserved_discriminator_notice_message(
    kind: str, prop: str, element: str, wholly_unobserved: bool
) -> str:
    """Render one discriminator-value-unobserved notice's message.

    The table-will-be-empty wording holds for a scalar or a wholly-unobserved
    list (the table really is empty); a partially-observed list's still-observed
    elements keep the table non-empty, so its unobserved elements take the
    weaker per-element wording (§ The unobserved-value notice matrix).

    Args:
        kind: The records kind the filter targets.
        prop: The discriminator column name.
        element: The unobserved filter value.
        wholly_unobserved: Whether every element of the filter's value is
            unobserved.

    Returns:
        The notice message text.
    """
    if wholly_unobserved:
        return (
            f"discriminator value '{element}' not observed for"
            f" '{kind}.{prop}'; table will be empty"
        )
    return (
        f"discriminator value '{element}' not observed for"
        f" '{kind}.{prop}'; it contributes no rows"
    )


def check_discriminator_value_observed(
    source: "SourceDecl",
    sidecar: "Sidecar",
    notice_sink: "NoticeSink",
) -> None:
    """Emit a 'discriminator-value-unobserved' Notice per unobserved element of
    a records filter value.

    The unobserved set is computed before any notice is emitted; notices follow
    the filter's config element order. A scalar or wholly-unobserved list keeps
    the `table will be empty` wording verbatim; a partially-observed list's
    unobserved elements take the weaker per-element `it contributes no rows`
    wording, since the table is not, in fact, empty (§ The unobserved-value
    notice matrix).

    Args:
        source: The grain source binding (must be records grain with a filter).
        sidecar: The open emit's sidecar.
        notice_sink: Receiver for the notice.

    Raises:
        Nothing. Never affects output data or exit code.
    """
    if source.grain != "records" or not source.filter:
        return

    domains = sidecar.enum_domains()
    kind_domains = domains.get(source.kind, {})

    for prop, value in source.filter.items():
        bare_prop = prop.removeprefix("prop__") if prop.startswith("prop__") else prop
        observed_values = kind_domains.get(bare_prop, ()) or kind_domains.get(prop, ())
        if not observed_values:
            continue

        elements = _predicate_elements(value)
        unobserved = [e for e in elements if e not in observed_values]
        if not unobserved:
            continue

        wholly_unobserved = len(unobserved) == len(elements)
        for element in unobserved:
            notice_sink(
                Notice(
                    code="discriminator-value-unobserved",
                    message=_unobserved_discriminator_notice_message(
                        source.kind, prop, element, wholly_unobserved
                    ),
                )
            )


def check_slice_only_filter_keys(
    source: "SourceDecl",
    table_decl: "TableDecl",
    source_table_name: str,
    sidecar: "Sidecar",
) -> None:
    """Enforce SliceOnlyColumnRefused over records `filter` keys.

    Row membership derives from the value, so a non-exempt slice_only filter
    key is refused. No-op unless grain is records with a filter. The exempt
    discriminator passes — filter on prop__<kind>_type is the classification
    read (init's pre-fill relies on it).

    Args:
        source: The grain source binding.
        table_decl: The output table declaration (for error messages).
        source_table_name: The resolved DuckDB source table name (unused;
            kept for the check's sibling signature symmetry).
        sidecar: The open emit's sidecar.

    Raises:
        ExportError: A filter key resolves to a non-exempt slice_only column.
        TemporalClassUnavailableError: Propagated.
    """
    if source.grain != "records" or not source.filter:
        return
    for filter_key in source.filter:
        if is_non_exempt_slice_only(sidecar, source.kind, filter_key):
            raise ExportError(
                slice_only_refusal_message(
                    table_decl.name, filter_key, "filter key", source.kind, filter_key
                )
            )


def _collect_value_read_sources(col_decl: "ColumnDecl") -> list[str]:
    """Collect every source-column name a column declaration's value derives from.

    Covers from, correlation, resolved value_map.from, derived: timestamp
    source, derived: elapsed correlate_on/start_source/end_source/
    other_where keys, derived: date_parse.from, derived: decimal.from, and
    derived: json_precision.from — the exhaustive SliceOnlyColumnRefused
    value-read surface (lookup and fk hops are checked separately). Each
    derived source is a value-read like any other: it joins this surface
    list, so deriving from a slice_only column is refused at plan time
    (§ The declared date parse).

    Args:
        col_decl: The column declaration.

    Returns:
        Every referenced source-column name, in declaration order.
    """
    refs: list[str] = []
    if col_decl.from_ is not None:
        refs.append(col_decl.from_)
    if col_decl.correlation is not None:
        refs.append(col_decl.correlation)
    if col_decl.derived is not None:
        derived = col_decl.derived
        if derived.value_map is not None:
            refs.append(derived.value_map.from_)
        if derived.timestamp is not None:
            refs.append(derived.timestamp.source)
        if derived.elapsed is not None:
            refs.append(derived.elapsed.correlate_on)
            refs.append(derived.elapsed.start_source)
            refs.append(derived.elapsed.end_source)
            refs.extend(derived.elapsed.other_where.keys())
        if derived.date_parse is not None:
            refs.append(derived.date_parse.from_)
        if derived.decimal is not None:
            refs.append(derived.decimal.from_)
        if derived.json_precision is not None:
            refs.append(derived.json_precision.from_)
    return refs


def check_slice_only_column_reads(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    source: "SourceDecl",
    source_table_name: str,
    sidecar: "Sidecar",
) -> None:
    """Enforce SliceOnlyColumnRefused over one column's own value-reads.

    Covers from, correlation, resolved value_map.from, derived: timestamp
    source, derived: elapsed correlate_on/start_source/end_source/other_where
    keys. Only prop__-named references on the kind's records table are in the
    population (the predicate scopes); membership/history grain surface
    columns are classless and pass untouched. lookup is
    check_lookup_temporal_safety's; fk hops are check_fk_slice_only's.
    Always-on — runs in full and windowed exports alike.

    Args:
        col_decl: The column declaration being validated.
        table_decl: The output table declaration (for error messages).
        source: The grain source binding (source.kind owns the column).
        source_table_name: The resolved DuckDB source table name (unused;
            kept for the check's sibling signature symmetry).
        sidecar: The open emit's sidecar.

    Raises:
        ExportError: A read resolves to a non-exempt slice_only column;
            message names output table.column, base table.column, the
            class, and the slice-only contract fact.
        TemporalClassUnavailableError: Propagated.
    """
    for ref in _collect_value_read_sources(col_decl):
        if is_non_exempt_slice_only(sidecar, source.kind, ref):
            raise ExportError(
                slice_only_refusal_message(
                    table_decl.name, col_decl.name, "column", source.kind, ref
                )
            )


def check_excluded_kind_not_sourced(
    table_decl: "TableDecl",
    config: "DimensionalConfig",
) -> None:
    """Enforce ExcludedKindNotSourced: no declared table sources an excluded kind.

    Args:
        table_decl: The output table declaration.
        config: The dimensional config (for exclude.kinds).

    Raises:
        ExportError: A declared table sources an excluded kind.
    """
    if config.exclude is None or not config.exclude.kinds:
        return
    kind = table_decl.source.kind
    if kind in config.exclude.kinds:
        raise ExportError(f"table '{table_decl.name}' sources excluded kind '{kind}'")


def check_excluded_table_not_sourced(
    table_decl: "TableDecl",
    source_table_name: str,
    config: "DimensionalConfig",
) -> None:
    """Enforce ExcludedTableNotSourced: no table sources an excluded sidecar table.

    Args:
        table_decl: The output table declaration.
        source_table_name: The resolved DuckDB table name for this table's source.
        config: The dimensional config (for exclude.tables).

    Raises:
        ExportError: A declared table's source resolves to an excluded sidecar table.
    """
    if config.exclude is None or not config.exclude.tables:
        return
    if source_table_name in config.exclude.tables:
        raise ExportError(
            f"table '{table_decl.name}' sources excluded sidecar table"
            f" '{source_table_name}'"
        )


def check_scd2_needs_history(
    table_decl: "TableDecl",
    source_table_name: str,
    sidecar: "Sidecar",
) -> None:
    """Enforce Scd2NeedsHistory: scd: type2 table has valid_from and tracked columns.

    A scd: type2 table must declare a valid_from scd_window column in its key,
    and the emit must carry the history_tracked flag, and the kind must have at
    least one tracked column (flag-authoritative). A flag-absent emit is refused
    with a clear message — re-emit with history_tracked to use scd: type2.

    Args:
        table_decl: The output table declaration (must be scd: type2).
        source_table_name: The resolved records__<kind> DuckDB table name.
        sidecar: The open emit's sidecar.

    Raises:
        ExportError: Missing valid_from scd_window key column, flag-absent emit,
            or no tracked column.
    """
    # Refuse flag-absent emits — the SCD-2 reconstruction is flag-only.
    if not sidecar.history_tracked_available():
        raise ExportError(
            f"scd type2 table '{table_decl.name}' requires the history_tracked"
            " flag on prop__ columns; re-emit with history_tracked to use scd:"
            " type2"
        )

    # Check valid_from scd_window column in key.
    key_set = set(table_decl.key)
    has_valid_from_key = False
    for col_decl in table_decl.columns:
        if (
            col_decl.name in key_set
            and col_decl.derived is not None
            and scd_window_bound(col_decl.derived.scd_window) == "valid_from"
        ):
            has_valid_from_key = True
            break

    # Flag-authoritative: any column with history_tracked=True qualifies,
    # even if it has no history rows (flag is authoritative).
    has_tracked = False
    try:
        cols = sidecar.columns(source_table_name)
    except TableNotFoundError as exc:
        raise ExportError(
            f"Source table '{source_table_name}' not found while checking"
            f" history-tracked columns; check the source binding in your"
            f" scenario config."
        ) from exc
    for col_spec in cols:
        if col_spec.history_tracked is True and col_spec.name.startswith("prop__"):
            has_tracked = True
            break

    if not has_valid_from_key or not has_tracked:
        raise ExportError(
            f"scd type2 table '{table_decl.name}' needs a valid_from scd_window"
            " column and at least one tracked column"
        )


def check_scd2_column_mode_supported(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
) -> None:
    """Enforce Scd2ColumnModeSupported: type2 columns use supported modes.

    The type2 surface admits from, null, derived: scd_window, and the pure
    per-row value renderings derived: timestamp / date_parse / value_map /
    decimal / json_precision — each a pure function of one row's source
    value, evaluated per record for constant sources and per version for
    tracked sources. It refuses fk, correlation, derived: ordinal, and
    derived: elapsed — cross-row or grain-surface semantics the type2 build
    does not define. (lookup is gated separately by LookupColumnSafety;
    slice_only sources by the export-wide slice-only surface.)

    Args:
        col_decl: The column declaration.
        table_decl: The output table declaration (gate applies iff
            scd: type2; also used for error messages).

    Raises:
        ExportError: The column uses an unsupported mode on an scd: type2
            table.
    """
    if table_decl.scd != "type2":
        return

    mode: str | None = None
    if col_decl.fk is not None:
        mode = "fk"
    elif col_decl.correlation is not None:
        mode = "correlation"
    elif col_decl.derived is not None and col_decl.derived.scd_window is None:
        if col_decl.derived.ordinal is not None:
            mode = "derived: ordinal"
        elif col_decl.derived.elapsed is not None:
            mode = "derived: elapsed"

    if mode is not None:
        raise ExportError(
            f"column '{col_decl.name}' on table '{table_decl.name}':"
            f" {mode} is not supported on an scd: type2 table; type2 columns"
            " support only from, null, derived: scd_window, and the value"
            " renderings derived: timestamp, derived: date_parse, derived:"
            " value_map, derived: decimal, and derived: json_precision"
        )


# ---------------------------------------------------------------------------
# The presentation-name posture
# ---------------------------------------------------------------------------


def check_reserved_presentation_name(table_decl: "TableDecl") -> None:
    """Enforce the presentation-name posture: no output column named
    last_mutation_sim_time.

    Always-on, full export included — unlike IncrementalReservedName's
    table-name check (`windowing.py`), which applies under incremental
    export only. `last_mutation_sim_time` is a sim-internal
    bookkeeping column: every value channel that reads it (the `from:` /
    `correlation:` / `derived: timestamp` sources, `ordinal.order_by`) stays
    untouched — only delivering it under its own output name is refused.

    Args:
        table_decl: The output table declaration.

    Raises:
        ExportError: A column resolves to output name last_mutation_sim_time.
    """
    for col_decl in table_decl.columns:
        if col_decl.name == RESERVED_PRESENTATION_COLUMN_NAME:
            raise ExportError(
                f"table '{table_decl.name}': column '{col_decl.name}' names the"
                " reserved last_mutation_sim_time column — it is sim-internal"
                " bookkeeping; deliver its value under a presentation name (a"
                " `from:` source) instead"
            )


# ---------------------------------------------------------------------------
# Dim-key agreement (key election)
# ---------------------------------------------------------------------------


def check_dim_key_agreement(
    dim_table_decl: "TableDecl",
    resolved_surface: "KeySurface",
    target_key: "KeySurface | None",
    edge_name: str,
) -> None:
    """Gate one FK's resolved surface against its destination dim's declared key.

    Doc § Dim-key agreement: an FK's value is only useful if the destination
    dim is keyed on the same surface. Applies only to an *inherited*
    (`target_key is None`) non-`record_id` resolution — an explicit
    `target_key` is the author's own escape, and `record_id` is the
    default identity, always agreeable.

    Args:
        dim_table_decl: The destination dim's output table declaration.
        resolved_surface: The FK's one resolved surface.
        target_key: The edge's explicit override, verbatim from FkClause;
            None when the surface was inherited.
        edge_name: The referencing table · column identity, for the error.

    Raises:
        ElectionDimKeyDisagrees: The resolution is inherited and
            non-default, and no declared key column of `dim_table_decl`
            sources `from:` the elected contract column; names the dim,
            its key sources, and the elected surface.
    """
    if target_key is not None or resolved_surface == "record_id":
        return
    if dim_key_projects_surface(dim_table_decl, resolved_surface):
        return
    key_sources = ", ".join(
        f"{col.name} (from: {col.from_!r})"
        for col in dim_table_decl.columns
        if col.name in set(dim_table_decl.key)
    )
    raise ElectionDimKeyDisagrees(
        f"{edge_name}: inherits elected surface '{resolved_surface}' from"
        f" dim '{dim_table_decl.name}', but its declared key ({key_sources})"
        f" sources no column from: '{resolved_surface}' — add an explicit"
        " target_key on the edge, or re-key the dim"
    )


# ---------------------------------------------------------------------------
# Main validation entry point
# ---------------------------------------------------------------------------


def validate_table(
    table_decl: "TableDecl",
    config: "DimensionalConfig",
    sidecar: "Sidecar",
    notice_sink: "NoticeSink",
    *,
    anchor: "EffectiveAnchor | None" = None,
    election: "Election | None" = None,
) -> str:
    """Run all business rules for a single table declaration.

    Runs: SourceTableExists, ExcludedKindNotSourced, ExcludedTableNotSourced,
    KeyColumnsDeclared, ProjectionColumnExists, OrdinalRefsSiblings,
    TimestampSourceAvailable, TemporalRenderRequiresAnchor,
    DateParseSourceColumn, DecimalSourceIsDouble, JsonPrecisionSourceIsVarchar,
    DiscriminatorValueObserved, FkTargetIsDim,
    ReferencePathResolvable, MembershipEdgeResolvable, Scd2NeedsHistory,
    Scd2ColumnModeSupported, LookupColumnSafety, SliceOnlyColumnRefused
    (filter keys, column reads including date_parse.from / decimal.from /
    json_precision.from, fk hops),
    ReservedPresentationName (last_mutation_sim_time — always-on, full
    export included). Per fk column: resolves the destination dim's source
    population set (`resolve_dim_source_populations`), the edge's one
    resolved surface (`resolve_fk_surface` — inherited or the explicit
    `target_key`), `check_edge_union_safety` over that set with the
    resolved surface as `surface_override`, then `check_dim_key_agreement`;
    the probe `build_fk_expr` call passes the resolved surface + population
    set.


    Args:
        table_decl: The output table declaration.
        config: The dimensional config.
        sidecar: The open emit's sidecar.
        notice_sink: Receiver for plan notices (threaded to
            check_discriminator_value_observed).
        anchor: The resolved EffectiveAnchor, or None — threaded to
            TemporalRenderRequiresAnchor.
        election: The resolved election, or None to resolve the all-default
            election internally (every population elects record_id — the
            caller has no `keys` block to thread, or is an election-free
            internal/test caller).

    Returns:
        The resolved DuckDB source table name.

    Raises:
        ExportError: Any business rule fails.
        TemporalRenderRequiresAnchor: An explicitly-elected instant rendering
            has no resolved anchor.
        DateParseSourceColumn: A date_parse source is not a declared VARCHAR
            column.
        DecimalSourceIsDouble: A decimal source is not a declared DOUBLE
            column.
        JsonPrecisionSourceIsVarchar: A json_precision source is not a
            declared VARCHAR column.
        ElectionInheritanceAmbiguous: An fk column's `target_key` is absent
            and the destination dim's source population set carries more
            than one distinct election.
        ElectionUnionUnsafe: An fk column's admitted target populations'
            resolved key spaces contain a pairwise-unsafe pair.
        ElectionPresentationUndeclared: An fk column resolves
            presentation_id over a source population set with an uncovered
            population.
        ElectionDimKeyDisagrees: An fk column inherits a non-default
            surface the destination dim's declared key does not project.
        TemporalClassUnavailableError: A consulted column's temporal pair is
            unavailable (non-conformant emit).
    """
    from fabulexa_forge.exporters.dimensional.fk import (
        build_fk_expr,
        check_fk_slice_only,
    )
    from fabulexa_forge.exporters.dimensional.lookup import (
        check_lookup_temporal_safety,
    )

    resolved_election = (
        election if election is not None else resolve_election(sidecar, None)
    )
    source = table_decl.source

    # --- Table-level rules (always) ---
    check_excluded_kind_not_sourced(table_decl, config)
    source_table_name = check_source_table_exists(source, sidecar)
    check_excluded_table_not_sourced(table_decl, source_table_name, config)
    check_key_columns_declared(table_decl)
    check_discriminator_value_observed(source, sidecar, notice_sink)
    check_slice_only_filter_keys(source, table_decl, source_table_name, sidecar)
    check_reserved_presentation_name(table_decl)

    if table_decl.scd == "type2":
        check_scd2_needs_history(table_decl, source_table_name, sidecar)

    surface = _grain_projectable_surface(source, sidecar, source_table_name)

    for col_decl in table_decl.columns:
        check_scd2_column_mode_supported(col_decl, table_decl)
        check_projection_column_exists(col_decl, table_decl, surface)
        check_ordinal_refs_siblings(col_decl, table_decl)
        check_timestamp_source_available(col_decl, table_decl, source, surface)
        check_temporal_render_requires_anchor(col_decl, anchor)
        check_date_parse_source_column(col_decl, table_decl, source_table_name, sidecar)
        check_decimal_source_column(col_decl, table_decl, source_table_name, sidecar)
        check_json_precision_source_column(
            col_decl, table_decl, source_table_name, sidecar
        )
        check_elapsed_columns_exist(col_decl, table_decl, source_table_name, sidecar)
        check_slice_only_column_reads(
            col_decl, table_decl, source, source_table_name, sidecar
        )

        if col_decl.fk is not None:
            target_table_decl = check_fk_target_is_dim(col_decl, table_decl, config)
            target_kind = target_table_decl.source.kind
            edge_name = f"{table_decl.name}.{col_decl.name}"
            dim_populations = resolve_dim_source_populations(
                sidecar, target_kind, target_table_decl.source.filter
            )
            resolved_surface = resolve_fk_surface(
                resolved_election, dim_populations, col_decl.fk.target_key, edge_name
            )
            check_edge_union_safety(
                resolved_election,
                target_kind,
                dim_population_sub_types(dim_populations),
                edge_name,
                surface_override=resolved_surface,
            )
            check_dim_key_agreement(
                target_table_decl, resolved_surface, col_decl.fk.target_key, edge_name
            )
            build_fk_expr(
                col_decl=col_decl,
                table_decl=table_decl,
                source_grain=source.grain,
                anchor_kind=source.kind,
                target_kind=target_kind,
                sidecar=sidecar,
                resolved_surface=resolved_surface,
                dim_populations=dim_populations,
            )
            check_fk_slice_only(
                col_decl=col_decl,
                table_decl=table_decl,
                source_grain=source.grain,
                anchor_kind=source.kind,
                target_kind=target_kind,
                sidecar=sidecar,
            )
        if col_decl.lookup is not None:
            check_lookup_temporal_safety(
                col_decl=col_decl,
                table_decl=table_decl,
                anchor_kind=source.kind,
                source_grain=source.grain,
                sidecar=sidecar,
            )

    return source_table_name
