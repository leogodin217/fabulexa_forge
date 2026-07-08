"""Lookup column SQL builder for the dimensional exporter.

Builds JOIN SQL fragments and SELECT expressions for `lookup:` columns.
Composes the reference-path derivation from derivations/reference_resolution.py.

A lookup column enriches an output row with a type-1 scalar property
(prop__<property>) of a related or own record, reached via:
  - Zero-hop self-join: to is None (or equal to anchor_kind).
  - Reference-edge pathfind: to is set, path resolved via BFS or explicit hint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_export.config.models import ColumnDecl, TableDecl
    from fabulexa_export.reader.sidecar import ColumnSpec, Sidecar

from fabulexa_export.derivations.reference_resolution import (
    _collect_reference_columns,
    _find_all_reference_paths,
    _path_hint_to_cols,
    build_reference_path_sql,
    get_fork_path_from_sidecar,
)
from fabulexa_export.errors import ExportError


def build_lookup_expr(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    anchor_kind: str,
    anchor_alias: str,
    source_grain: str,
    sidecar: "Sidecar",
) -> tuple[str, list[str]]:
    """Build the SELECT expression + JOIN clauses for a `lookup:` column.

    Composes the reference-path derivation: resolves the hop chain, calls
    build_reference_path_sql with terminal_projection='prop__<property>' to
    produce a subquery, and LEFT JOINs it on record_id. The SELECT projects the
    derivation's `resolved` column aliased to col_decl.name. Fan-out-free: the
    derivation guarantees at most one resolved value per anchor record_id.

    For a non-records grain, the derivation subquery already handles the preamble
    records relation internally via records__<anchor_kind>; each reference hop
    adds one LEFT JOIN internally. The grain's anchor_alias is joined only to
    hook the derivation result by record_id. For a records grain the same
    derivation subquery is used — zero-hop self projects the prop directly, and
    multi-hop chains through records tables inside the subquery. All JOIN aliases
    are namespaced by col_decl.name (`_lookup_<col>`); the result is fan-out-free.

    Args:
        col_decl: The column declaration (lookup mode set).
        table_decl: The output table declaration (for error messages).
        anchor_kind: The grain's record kind (table_decl.source.kind).
        anchor_alias: SQL alias for the grain's base table (e.g. "_grain").
        source_grain: The grain type ('records', 'history_point',
            'history_interval', 'membership').
        sidecar: The open emit's sidecar.

    Returns:
        (select_expr, join_clauses) — insert join_clauses before the ORDER BY, use
        select_expr in the SELECT list. join_clauses contains the one derivation
        subquery LEFT JOIN (or empty list for the zero-hop records-grain case).

    Raises:
        ExportError: no reference path resolves from anchor_kind to the terminal kind,
            or the path is ambiguous and no `path` hint was given, or a `path` hop is
            not a references column on its kind.
    """
    assert col_decl.lookup is not None
    lookup = col_decl.lookup

    terminal_kind = lookup.to if lookup.to is not None else anchor_kind
    terminal_projection = f"prop__{lookup.property}"
    alias_ns = f"_lookup_{col_decl.name}"
    context_label = f"{table_decl.name}.{col_decl.name}"

    hops = _resolve_lookup_hops(
        anchor_kind=anchor_kind,
        terminal_kind=terminal_kind,
        path_hint=lookup.path,
        sidecar=sidecar,
        context_label=context_label,
    )

    fork_path = get_fork_path_from_sidecar(sidecar)

    deriv_sql = build_reference_path_sql(
        sidecar=sidecar,
        fork_path=fork_path,
        anchor_kind=anchor_kind,
        hop_columns=hops,
        terminal_projection=terminal_projection,
    )
    deriv_alias = f"{alias_ns}_rp"

    join_clauses = [
        f'LEFT JOIN ({deriv_sql}) AS "{deriv_alias}"'
        f' ON "{deriv_alias}"."record_id" = "{anchor_alias}"."record_id"'
    ]
    select_expr = f'"{deriv_alias}"."resolved" AS "{col_decl.name}"'
    return select_expr, join_clauses


def _resolve_lookup_hops(
    anchor_kind: str,
    terminal_kind: str,
    path_hint: "list[str] | None",
    sidecar: "Sidecar",
    context_label: str,
) -> "list[ColumnSpec]":
    """Resolve lookup hop columns from anchor_kind to terminal_kind.

    Zero-hop (anchor_kind == terminal_kind) returns an empty list.
    Multi-hop uses the path hint if given, otherwise BFS pathfind.

    Args:
        anchor_kind: The anchor record kind.
        terminal_kind: The terminal record kind for the lookup.
        path_hint: Explicit ordered prop__ column names, or None for auto-resolve.
        sidecar: The open emit's sidecar.
        context_label: Human-readable label for error messages (e.g. 'table.col').

    Returns:
        Ordered list of ColumnSpec hops (empty for zero-hop).

    Raises:
        ExportError: No path, ambiguous path, or invalid path hint hop.
    """
    if anchor_kind == terminal_kind:
        return []

    if path_hint is not None:
        return _path_hint_to_cols(path_hint, anchor_kind, sidecar, context_label)

    ref_map = _collect_reference_columns(sidecar)
    paths = _find_all_reference_paths(anchor_kind, terminal_kind, ref_map)
    if not paths:
        raise ExportError(
            f"no reference path from '{anchor_kind}' to '{terminal_kind}'"
            f" for '{context_label}'"
        )
    if len(paths) > 1:
        raise ExportError(
            f"ambiguous reference path from '{anchor_kind}'"
            f" to '{terminal_kind}'"
            f" for '{context_label}';"
            " supply `path` (ordered prop__ columns)"
        )
    return paths[0]


def check_lookup_temporal_safety(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    anchor_kind: str,
    source_grain: str,
    sidecar: "Sidecar",
) -> None:
    """Business rule: a lookup reads only type-1 columns and resolves cleanly.

    Resolves the terminal kind and the reference path, then verifies: (0) the table is
    not `scd: type2` (the SCD-2 wide builder does not project lookup columns); (1) the
    grain is not `records` for a zero-hop self lookup; (2) the terminal records table
    and its prop__<property> column exist; (3) the emit carries history_tracked; (4) the
    terminal property and every traversed hop column are history_tracked: false.

    Args:
        col_decl: The column declaration (lookup mode set).
        table_decl: The output table declaration (carries `scd`; for error messages).
        anchor_kind: The grain's record kind.
        source_grain: The grain type.
        sidecar: The open emit's sidecar.

    Raises:
        ExportError: the table is `scd: type2`; a zero-hop self lookup on a `records`
            grain (redundant with `from`); the terminal records table or
            prop__<property> is absent; the emit lacks the history_tracked flag; the
            reference path is unresolvable or ambiguous; or the terminal property or any
            traversed hop column is history_tracked: true (type-2).
    """
    assert col_decl.lookup is not None
    lookup = col_decl.lookup

    # (0) SCD-2 rejection
    if table_decl.scd == "type2":
        raise ExportError(
            f"lookup column '{table_decl.name}.{col_decl.name}'"
            " is not supported on scd: type2 tables"
        )

    terminal_kind = lookup.to if lookup.to is not None else anchor_kind

    # (1) Zero-hop self lookup on records grain is redundant with `from`
    if terminal_kind == anchor_kind and source_grain == "records":
        raise ExportError(
            f"lookup column '{table_decl.name}.{col_decl.name}':"
            " zero-hop self lookup on a records grain is redundant with `from`;"
            " use `from` to project a records column directly"
        )

    # (2) Terminal records table and prop__<property> must exist
    terminal_table = f"records__{terminal_kind}"
    try:
        terminal_cols = sidecar.columns(terminal_table)
    except Exception:
        raise ExportError(
            f"lookup column '{table_decl.name}.{col_decl.name}':"
            f" terminal kind '{terminal_kind}' has no records table '{terminal_table}'"
        )

    property_col = f"prop__{lookup.property}"
    terminal_col_map = {c.name: c for c in terminal_cols}
    if property_col not in terminal_col_map:
        raise ExportError(
            f"lookup column '{table_decl.name}.{col_decl.name}':"
            f" property '{property_col}' not found on '{terminal_table}'"
        )

    # (3) Emit must carry history_tracked
    if not sidecar.history_tracked_available():
        raise ExportError(
            f"lookup column '{table_decl.name}.{col_decl.name}':"
            " emit does not carry the history_tracked flag;"
            " re-emit with a version that produces history_tracked"
        )

    # (4) Resolve path and check each hop + terminal for history_tracked: false
    context_label = f"{table_decl.name}.{col_decl.name}"
    hops = _resolve_lookup_hops(
        anchor_kind=anchor_kind,
        terminal_kind=terminal_kind,
        path_hint=lookup.path,
        sidecar=sidecar,
        context_label=context_label,
    )

    # Check each traversed hop column for history_tracked
    current_kind = anchor_kind
    for hop_col in hops:
        if hop_col.history_tracked is True:
            raise ExportError(
                f"lookup column '{table_decl.name}.{col_decl.name}':"
                f" traversed hop column '{hop_col.name}' on kind '{current_kind}'"
                " is history_tracked: true (type-2); only type-1 columns are allowed"
            )
        hop_kind = hop_col.references
        assert hop_kind is not None
        current_kind = hop_kind

    # Check terminal property for history_tracked
    terminal_col = terminal_col_map[property_col]
    if terminal_col.history_tracked is True:
        raise ExportError(
            f"lookup column '{table_decl.name}.{col_decl.name}':"
            f" terminal property '{property_col}' on kind '{terminal_kind}'"
            " is history_tracked: true (type-2); only type-1 properties are allowed"
        )
