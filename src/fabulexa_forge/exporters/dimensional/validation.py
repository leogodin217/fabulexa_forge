"""Business-rule validation for the dimensional exporter.

Enforces all business rules at build_query_specs time:
SourceTableExists, KeyColumnsDeclared, ProjectionColumnExists,
OrdinalRefsSiblings, TimestampSourceAvailable, DiscriminatorValueObserved,
ExcludedKindNotSourced, ExcludedTableNotSourced, FkTargetIsDim,
ReferencePathResolvable, MembershipEdgeResolvable, Scd2NeedsHistory,
Scd2ColumnModeSupported.

The SingleBranch rule is enforced by derivations.require_single_branch (the
stage-wide guard); dimensional calls it but does not own it.

When a Window is supplied, ten additional incremental gates run:
IncrementalGrainUnsupported, IncrementalElapsedUnsupported,
IncrementalFkMembershipUnsupported, IncrementalFkMutableHop,
IncrementalOrdinalOrderBy, IncrementalSliceColumnMutable,
IncrementalFilterColumnMutable, IncrementalScd2IdentityKey,
IncrementalScd2ValidFromUnique, IncrementalReservedName.

Each rule is a module-level function taking only what it needs, so each is
independently testable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.config.models import (
        ColumnDecl,
        DimensionalConfig,
        SourceDecl,
        TableDecl,
    )
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.incremental.windows import Window
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge.derivations.reference_resolution import (
    _collect_reference_columns,
    _find_all_reference_paths,
    _path_hint_to_cols,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.fk import check_fk_target_is_dim
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.reserved_names import (
    is_reserved_column_name,
    is_reserved_table_name,
)
from fabulexa_forge.reader.errors import TableNotFoundError

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

#: Timestamp sources available per grain.
_TIMESTAMP_SOURCES_BY_GRAIN: dict[str, frozenset[str]] = {
    "records": frozenset({"last_mutation_sim_time"}),
    "history_point": frozenset({"sim_time"}),
    "history_interval": frozenset({"sim_time", "lead_sim_time"}),
    "membership": frozenset({"joined_sim_time", "left_sim_time"}),
}

#: Sources mutable under incremental (may change post-creation).
_MUTABLE_SOURCES: frozenset[str] = frozenset(
    {
        "active",
        "deactivated_at",
        "last_mutation_sim_time",
    }
)


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

    Checks from, correlation, and derived.value_map.from against the grain surface.

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


def check_discriminator_value_observed(
    source: "SourceDecl",
    sidecar: "Sidecar",
    notice_sink: "NoticeSink",
) -> None:
    """Emit a 'discriminator-value-unobserved' Notice if a records filter value
    is not observed in the emit.

    Args:
        source: The grain source binding (must be records grain with a filter).
        sidecar: The open emit's sidecar.
        notice_sink: Receiver for the notice.

    Raises:
        Nothing. Never affects output data or exit code.
    """
    if source.grain != "records" or not source.filter:
        return

    for prop, value in source.filter.items():
        # Check enum_domains for observed values
        domains = sidecar.enum_domains()
        kind_domains = domains.get(source.kind, {})
        bare_prop = prop.removeprefix("prop__") if prop.startswith("prop__") else prop
        observed_values = kind_domains.get(bare_prop, ()) or kind_domains.get(prop, ())
        if observed_values and value not in observed_values:
            notice_sink(
                Notice(
                    code="discriminator-value-unobserved",
                    message=(
                        f"discriminator value '{value}' not observed for"
                        f" '{source.kind}.{prop}'; table will be empty"
                    ),
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
            and col_decl.derived.scd_window == "valid_from"
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
    """Enforce Scd2ColumnModeSupported: type2 columns use only implemented modes.

    The SCD-2 type2 builder implements exactly three column modes: from, null,
    and derived: scd_window. Any other mode — fk, correlation, or a derived
    ordinal / value_map / timestamp / elapsed — would render as NULL on every
    row: accepted config, silently wrong data (Principle #7). Reject at
    validate time instead. (lookup is gated separately by LookupColumnSafety.)

    Args:
        col_decl: The column declaration.
        table_decl: The output table declaration (mode gate applies iff
            scd: type2; also used for error messages).

    Raises:
        ExportError: The column uses an unimplemented mode on an scd: type2
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
        elif col_decl.derived.value_map is not None:
            mode = "derived: value_map"
        elif col_decl.derived.timestamp is not None:
            mode = "derived: timestamp"
        elif col_decl.derived.elapsed is not None:
            mode = "derived: elapsed"

    if mode is not None:
        raise ExportError(
            f"table '{table_decl.name}' column '{col_decl.name}':"
            f" {mode} is not supported on an scd: type2 table; type2 columns"
            " support only from, null, and derived: scd_window"
        )


# ---------------------------------------------------------------------------
# Incremental business-rule gates
# ---------------------------------------------------------------------------


def check_incremental_grain_supported(table_decl: "TableDecl") -> None:
    """Enforce IncrementalGrainUnsupported: no history_interval or membership grain.

    Args:
        table_decl: The output table declaration.

    Raises:
        ExportError: The grain is history_interval or membership.
    """
    grain = table_decl.source.grain
    if grain in ("history_interval", "membership"):
        raise ExportError(
            f"table '{table_decl.name}': grain '{grain}' is not supported with"
            " incremental export; model interval ends as history_point events"
        )


def check_incremental_reserved_names(table_decl: "TableDecl") -> None:
    """Enforce IncrementalReservedName: no reserved table or column names.

    Table names must not end in '__rows' or equal '_export_meta' /
    '_export_windows'. No column may be named '__valid_from_ns'.

    Args:
        table_decl: The output table declaration.

    Raises:
        ExportError: A reserved name is used.
    """
    name = table_decl.name
    if is_reserved_table_name(name):
        raise ExportError(
            f"table '{table_decl.name}': name '{name}' is reserved under"
            " incremental export"
        )
    for col_decl in table_decl.columns:
        if is_reserved_column_name(col_decl.name):
            raise ExportError(
                f"table '{table_decl.name}': name '{col_decl.name}' is reserved"
                " under incremental export"
            )


def check_incremental_scd2_identity_key(table_decl: "TableDecl") -> None:
    """Enforce IncrementalScd2IdentityKey: SCD-2 key has >= 1 non-scd_window column.

    Args:
        table_decl: The output table declaration (must be scd: type2).

    Raises:
        ExportError: All key columns are scd_window columns (no identity).
    """
    if table_decl.scd != "type2":
        return

    scd_window_cols: set[str] = set()
    for col_decl in table_decl.columns:
        if col_decl.derived is not None and col_decl.derived.scd_window is not None:
            scd_window_cols.add(col_decl.name)

    key_set = set(table_decl.key)
    non_scd_key = key_set - scd_window_cols
    if not non_scd_key:
        raise ExportError(
            f"table '{table_decl.name}': incremental SCD-2 requires a"
            " non-scd_window identity column in 'key'"
        )


def check_incremental_scd2_valid_from_unique(table_decl: "TableDecl") -> None:
    """Enforce IncrementalScd2ValidFromUnique: exactly one scd_window: valid_from column
    when a valid_to column is declared.

    Args:
        table_decl: The output table declaration (must be scd: type2).

    Raises:
        ExportError: Not exactly one valid_from column alongside a valid_to column.
    """
    if table_decl.scd != "type2":
        return

    valid_from_count = 0
    valid_to_count = 0
    for col_decl in table_decl.columns:
        if col_decl.derived is not None:
            if col_decl.derived.scd_window == "valid_from":
                valid_from_count += 1
            elif col_decl.derived.scd_window == "valid_to":
                valid_to_count += 1

    if valid_to_count > 0 and valid_from_count != 1:
        raise ExportError(
            f"table '{table_decl.name}': incremental SCD-2 requires exactly one"
            " scd_window: valid_from column"
        )


def check_incremental_elapsed_unsupported(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
) -> None:
    """Enforce IncrementalElapsedUnsupported: no derived: elapsed column.

    Args:
        col_decl: The column declaration.
        table_decl: The output table declaration (for error messages).

    Raises:
        ExportError: The column uses derived: elapsed.
    """
    if col_decl.derived is not None and col_decl.derived.elapsed is not None:
        raise ExportError(
            f"table '{table_decl.name}' column '{col_decl.name}':"
            " derived: elapsed is not supported with incremental export"
        )


def check_incremental_fk_membership_unsupported(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
) -> None:
    """Enforce IncrementalFkMembershipUnsupported: no fk via: membership.

    Args:
        col_decl: The column declaration.
        table_decl: The output table declaration (for error messages).

    Raises:
        ExportError: The column uses fk via: membership.
    """
    if col_decl.fk is not None and col_decl.fk.via == "membership":
        raise ExportError(
            f"table '{table_decl.name}' column '{col_decl.name}':"
            " fk via: membership is not supported with incremental export;"
            " model member events as history_point facts"
        )


def check_incremental_fk_mutable_hop_with_config(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    config: "DimensionalConfig",
    sidecar: "Sidecar",
) -> None:
    """Enforce IncrementalFkMutableHop with full config for target resolution.

    Args:
        col_decl: The column declaration with an fk clause.
        table_decl: The output table declaration (for error messages).
        config: The dimensional config (for target kind resolution).
        sidecar: The open emit's sidecar (for history_tracked lookup).

    Raises:
        ExportError: A hop is history_tracked: true, or the emit lacks the flag.
    """
    if col_decl.fk is None or col_decl.fk.via != "reference":
        return

    if not sidecar.history_tracked_available():
        raise ExportError(
            f"table '{table_decl.name}' column '{col_decl.name}':"
            " fk hop '' is not history_tracked: false;"
            " incremental fk paths must be temporally constant"
        )

    anchor_kind = table_decl.source.kind
    path_hint = col_decl.fk.path

    ref_map = _collect_reference_columns(sidecar)

    if path_hint is not None:
        hops = _path_hint_to_cols(
            path_hint,
            anchor_kind,
            sidecar,
            f"{table_decl.name}.{col_decl.name}",
        )
    else:
        target_table_decl = check_fk_target_is_dim(col_decl, table_decl, config)
        target_kind = target_table_decl.source.kind
        paths = _find_all_reference_paths(anchor_kind, target_kind, ref_map)
        if not paths:
            # No path — validate_table would already have errored; skip
            return
        # Use the first path (ambiguous paths would error earlier)
        hops = paths[0]

    for hop_col in hops:
        if hop_col.history_tracked is not False:
            raise ExportError(
                f"table '{table_decl.name}' column '{col_decl.name}':"
                f" fk hop '{hop_col.name}' is not history_tracked: false;"
                " incremental fk paths must be temporally constant"
            )


def _get_window_key_cols(table_decl: "TableDecl") -> frozenset[str]:
    """Return the set of window-key column names for an append-mode table.

    For records grain: the column sourcing last_mutation_sim_time (via from:).
    For history_point grain: the column sourcing sim_time (via from:).
    For SCD-2 dim: the scd_window: valid_from column(s).
    Plus any derived: timestamp whose source is the grain's time column.

    Args:
        table_decl: The output table declaration.

    Returns:
        Set of output column names that are valid ordinal order_by targets.
    """
    grain = table_decl.source.grain

    if table_decl.scd == "type2":
        # SCD-2: window key is the scd_window: valid_from column
        valid_from_cols: set[str] = set()
        for col_decl in table_decl.columns:
            if col_decl.derived is None:
                continue
            if col_decl.derived.scd_window == "valid_from":
                valid_from_cols.add(col_decl.name)
        return frozenset(valid_from_cols)

    if grain == "records":
        raw_key = "last_mutation_sim_time"
    elif grain == "history_point":
        raw_key = "sim_time"
    else:
        return frozenset()

    window_key_cols: set[str] = set()
    for col_decl in table_decl.columns:
        if col_decl.from_ == raw_key:
            window_key_cols.add(col_decl.name)
        if (
            col_decl.derived is not None
            and col_decl.derived.timestamp is not None
            and col_decl.derived.timestamp.source == raw_key
        ):
            window_key_cols.add(col_decl.name)

    return frozenset(window_key_cols)


def check_incremental_ordinal_order_by(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    is_append_table: bool,
) -> None:
    """Enforce IncrementalOrdinalOrderBy: ordinal order_by resolves to the window key.

    Only applies to append-mode tables (facts and SCD-2 dims). Snapshot-class
    (type-1 dims) are exempt.

    Args:
        col_decl: The column declaration containing an ordinal derived spec.
        table_decl: The output table declaration.
        is_append_table: True when the table is an append-mode table.

    Raises:
        ExportError: The ordinal order_by does not resolve to the window-key time.
    """
    if col_decl.derived is None or col_decl.derived.ordinal is None:
        return
    if not is_append_table:
        return

    ordinal = col_decl.derived.ordinal
    window_key_cols = _get_window_key_cols(table_decl)

    if ordinal.order_by not in window_key_cols:
        raise ExportError(
            f"table '{table_decl.name}' column '{col_decl.name}':"
            " ordinal order_by must resolve to the table's window-key time"
            " under incremental export"
        )


def _is_column_source_mutable(
    col_decl: "ColumnDecl",
    sidecar: "Sidecar",
    source_table_name: str,
) -> bool:
    """Return True if a column's source value may change after initial creation.

    Mutable sources: active, deactivated_at, last_mutation_sim_time,
    and any prop__ column with history_tracked: true. Requires the emit to
    carry history_tracked (caller must enforce).

    Args:
        col_decl: The column declaration to check.
        sidecar: The open emit's sidecar.
        source_table_name: The resolved DuckDB source table name.

    Returns:
        True when the column's source is temporally mutable.
    """
    # Check from_ / correlation / timestamp.source / value_map.from_
    src: str | None = None
    if col_decl.from_ is not None:
        src = col_decl.from_
    elif col_decl.correlation is not None:
        src = col_decl.correlation
    elif col_decl.derived is not None and col_decl.derived.timestamp is not None:
        src = col_decl.derived.timestamp.source
    elif col_decl.derived is not None and col_decl.derived.value_map is not None:
        src = col_decl.derived.value_map.from_

    if src is None:
        return False

    if src in _MUTABLE_SOURCES:
        return True

    if src.startswith("prop__"):
        # Check history_tracked flag on this column
        try:
            for col_spec in sidecar.columns(source_table_name):
                if col_spec.name == src:
                    return col_spec.history_tracked is True
        except TableNotFoundError:
            pass

    return False


def check_incremental_slice_column_mutable(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    sidecar: "Sidecar",
    source_table_name: str,
    is_slice_read: bool,
) -> None:
    """Enforce IncrementalSliceColumnMutable: slice-read columns must be constant.

    Slice-read columns: any column of a scd: type1 dim, and static columns
    (non-scd_window) of a scd: type2 dim. Records-grain facts are exempt.

    The emit must carry history_tracked; if not, refuse outright (same stance as
    LookupColumnSafety and IncrementalFkMutableHop).

    Args:
        col_decl: The column declaration.
        table_decl: The output table declaration (for error messages).
        sidecar: The open emit's sidecar.
        source_table_name: The resolved DuckDB source table name.
        is_slice_read: True when this column must be temporally constant.

    Raises:
        ExportError: The column reads a mutable source.
    """
    if not is_slice_read:
        return

    # scd_window columns are not slice-read (they carry the version boundary)
    if col_decl.derived is not None and col_decl.derived.scd_window is not None:
        return

    if not sidecar.history_tracked_available():
        # Cannot determine constancy without the flag — refuse outright
        # Check if any potential mutable source is referenced
        src: str | None = None
        if col_decl.from_ is not None:
            src = col_decl.from_
        elif col_decl.correlation is not None:
            src = col_decl.correlation
        elif col_decl.derived is not None and col_decl.derived.timestamp is not None:
            src = col_decl.derived.timestamp.source
        elif col_decl.derived is not None and col_decl.derived.value_map is not None:
            src = col_decl.derived.value_map.from_

        if src is not None and src.startswith("prop__"):
            raise ExportError(
                f"table '{table_decl.name}' column '{col_decl.name}':"
                " slice-read columns must be temporally constant under"
                " incremental export"
            )
        return

    if _is_column_source_mutable(col_decl, sidecar, source_table_name):
        raise ExportError(
            f"table '{table_decl.name}' column '{col_decl.name}':"
            " slice-read columns must be temporally constant under incremental export"
        )


def check_incremental_filter_column_mutable(
    table_decl: "TableDecl",
    sidecar: "Sidecar",
    source_table_name: str,
) -> None:
    """Enforce IncrementalFilterColumnMutable: dim filter predicates must be constant.

    Only applies to dim tables (type1 and type2 with a source.filter). Records-grain
    facts are exempt: keyed on last_mutation_sim_time, their classification is final.

    The emit must carry history_tracked; if not, refuse outright.

    Args:
        table_decl: The output table declaration.
        sidecar: The open emit's sidecar.
        source_table_name: The resolved DuckDB source table name.

    Raises:
        ExportError: A filter column reads a mutable (history-tracked) source.
    """
    if table_decl.role != "dim":
        return
    if not table_decl.source.filter:
        return

    for prop_name in table_decl.source.filter:
        if not sidecar.history_tracked_available():
            # Cannot verify constancy — if it's a prop__ key, refuse outright
            if prop_name.startswith("prop__"):
                raise ExportError(
                    f"table '{table_decl.name}': filter column '{prop_name}'"
                    " must be temporally constant under incremental export"
                )
            continue

        # Look up history_tracked for this prop
        try:
            for col_spec in sidecar.columns(source_table_name):
                if col_spec.name == prop_name:
                    if col_spec.history_tracked is True:
                        raise ExportError(
                            f"table '{table_decl.name}': filter column '{prop_name}'"
                            " must be temporally constant under incremental export"
                        )
                    break
        except TableNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Main validation entry point
# ---------------------------------------------------------------------------


def validate_table(
    table_decl: "TableDecl",
    config: "DimensionalConfig",
    sidecar: "Sidecar",
    window: "Window | None",
    notice_sink: "NoticeSink",
) -> str:
    """Run all business rules for a single table declaration.

    Runs: SourceTableExists, ExcludedKindNotSourced, ExcludedTableNotSourced,
    KeyColumnsDeclared, ProjectionColumnExists, OrdinalRefsSiblings,
    TimestampSourceAvailable, DiscriminatorValueObserved, FkTargetIsDim,
    ReferencePathResolvable, MembershipEdgeResolvable, Scd2NeedsHistory,
    Scd2ColumnModeSupported, LookupColumnSafety.

    When window is not None, also runs the ten incremental gates:
    IncrementalGrainUnsupported, IncrementalElapsedUnsupported,
    IncrementalFkMembershipUnsupported, IncrementalFkMutableHop,
    IncrementalOrdinalOrderBy, IncrementalSliceColumnMutable,
    IncrementalFilterColumnMutable, IncrementalScd2IdentityKey,
    IncrementalScd2ValidFromUnique, IncrementalReservedName.

    Args:
        table_decl: The output table declaration.
        config: The dimensional config.
        sidecar: The open emit's sidecar.
        window: The window for windowed export, or None for full export.
        notice_sink: Receiver for plan notices (threaded to
            check_discriminator_value_observed).

    Returns:
        The resolved DuckDB source table name.

    Raises:
        ExportError: Any business rule fails.
    """
    from fabulexa_forge.exporters.dimensional.fk import (
        build_fk_expr,
        check_fk_target_is_dim,
    )
    from fabulexa_forge.exporters.dimensional.lookup import (
        check_lookup_temporal_safety,
    )

    source = table_decl.source

    # --- Table-level rules (always) ---
    check_excluded_kind_not_sourced(table_decl, config)
    source_table_name = check_source_table_exists(source, sidecar)
    check_excluded_table_not_sourced(table_decl, source_table_name, config)
    check_key_columns_declared(table_decl)
    check_discriminator_value_observed(source, sidecar, notice_sink)

    if table_decl.scd == "type2":
        check_scd2_needs_history(table_decl, source_table_name, sidecar)

    # --- Table-level incremental rules ---
    if window is not None:
        check_incremental_grain_supported(table_decl)
        check_incremental_reserved_names(table_decl)
        check_incremental_filter_column_mutable(table_decl, sidecar, source_table_name)
        if table_decl.scd == "type2":
            check_incremental_scd2_identity_key(table_decl)
            check_incremental_scd2_valid_from_unique(table_decl)

    surface = _grain_projectable_surface(source, sidecar, source_table_name)

    # Determine append-mode status for ordinal gate
    is_append_table = window is not None and not (
        table_decl.role == "dim" and table_decl.scd == "type1"
    )

    # Determine slice-read status for column gate
    # type1 dims: all columns are slice-read
    # type2 dims: static columns (non-scd_window) are slice-read
    is_type1_dim = table_decl.role == "dim" and table_decl.scd == "type1"
    is_type2_dim = table_decl.role == "dim" and table_decl.scd == "type2"

    for col_decl in table_decl.columns:
        check_scd2_column_mode_supported(col_decl, table_decl)
        check_projection_column_exists(col_decl, table_decl, surface)
        check_ordinal_refs_siblings(col_decl, table_decl)
        check_timestamp_source_available(col_decl, table_decl, source, surface)
        check_elapsed_columns_exist(col_decl, table_decl, source_table_name, sidecar)

        if col_decl.fk is not None:
            target_table_decl = check_fk_target_is_dim(col_decl, table_decl, config)
            target_kind = target_table_decl.source.kind
            build_fk_expr(
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

        # --- Column-level incremental rules ---
        if window is not None:
            check_incremental_elapsed_unsupported(col_decl, table_decl)
            check_incremental_fk_membership_unsupported(col_decl, table_decl)
            check_incremental_fk_mutable_hop_with_config(
                col_decl, table_decl, config, sidecar
            )
            check_incremental_ordinal_order_by(col_decl, table_decl, is_append_table)

            # For SCD-2: tracked prop__ columns are version columns, not static.
            # Only static (non-tracked) columns need the slice-read gate.
            col_is_scd2_tracked = False
            from_col = col_decl.from_
            if is_type2_dim and from_col is not None and from_col.startswith("prop__"):
                if sidecar.history_tracked_available():
                    try:
                        for cs in sidecar.columns(source_table_name):
                            if cs.name == from_col and cs.history_tracked is True:
                                col_is_scd2_tracked = True
                                break
                    except TableNotFoundError:
                        pass

            is_slice_read = is_type1_dim or (
                is_type2_dim
                and not col_is_scd2_tracked
                and (col_decl.derived is None or col_decl.derived.scd_window is None)
            )
            check_incremental_slice_column_mutable(
                col_decl, table_decl, sidecar, source_table_name, is_slice_read
            )

    return source_table_name
