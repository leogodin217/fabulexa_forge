"""State-at derivation: point-in-time row reconstruction, two entry points.

Canonical relation: one row per record of a kind, reconstructed from history and
records__<kind>. Models on the shipped row_state_events.py fold — same layer
contract, same `is True` tracked/current-value partition, same codec-VARCHAR
after-image rule — but reconstructs one row per record rather than one row per
state-change event. Designed here because snapshot delivery (Unit 3) is its first
consumer, not as scaffolding.

Two entry points share the fold: build_state_at_sql reconstructs at a single
constant horizon (a created-time row filter, horizon-rendered lifecycle, as-of
property values strictly before the horizon); build_state_at_end_sql reconstructs
at the tape's end (every record, spine lifecycle verbatim, latest-ever property
values) — no horizon parameter, no horizon predicate, so composing it over a
truncated base relation bounds the answer at the truncation.

Layer-direction invariant: imports only the reader, fabulexa_forge.errors, and
stdlib. Never imports exporters.* or config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.reader.sidecar import ColumnSpec, Sidecar

from fabulexa_forge._sql import _sql_literal
from fabulexa_forge.derivations.properties import (
    has_presentation_id,
    partition_properties,
)

#: The fixed canonical prefix columns for state-at reconstruction. presentation_id
#: (when the kind carries it) and prop__<p> columns follow this prefix, per
#: build_state_at_sql's documented column order.
STATE_AT_COLUMNS: tuple[str, ...] = (
    "record_id",
    "created_sim_time",
    "active",
    "deactivated_at",
)


def _build_tracked_prop_join(
    fork_path: str,
    kind: str,
    prop: str,
    horizon_ns: int | None,
) -> tuple[str, str]:
    """Build the LEFT JOIN resolving a tracked prop's most-recent as-of value.

    The as-of lookup is a windowed rank over each record's history rows, taking the
    most-recent one — a plain LEFT JOIN, not an ASOF JOIN (ASOF matches a per-row
    correlated time; there is none here). With horizon_ns given, the horizon is a
    single constant shared by every record and only rows strictly before it are
    ranked. With horizon_ns None (the tape's end), every history row is ranked —
    "most recent" is unbounded.

    Args:
        fork_path: The sole branch fork_path.
        kind: The record kind.
        prop: The tracked property name (without prop__ prefix).
        horizon_ns: The exclusive reconstruction horizon in sim-time ns, or None
            for the tape's end (no horizon predicate).

    Returns:
        A 2-tuple (join_sql, value_expr): the LEFT JOIN clause (leading space,
        bound to a per-property alias) and the VARCHAR value expression to select
        for this property's as-of value.
    """
    alias = f"_h_{prop}"
    fp_lit = _sql_literal(fork_path)
    kind_lit = _sql_literal(kind)
    prop_lit = _sql_literal(prop)
    horizon_predicate = (
        f' AND "sim_time" < {horizon_ns}' if horizon_ns is not None else ""
    )
    join_sql = (
        f" LEFT JOIN ("
        f'SELECT "record_id", "value" FROM ('
        f'SELECT "record_id", "value",'
        f" ROW_NUMBER() OVER ("
        f'PARTITION BY "record_id" ORDER BY "sim_time" DESC'
        f') AS "_rn"'
        f' FROM "history"'
        f' WHERE "fork_path" = {fp_lit} AND "kind" = {kind_lit}'
        f' AND "property" = {prop_lit}{horizon_predicate}'
        f') AS "_ranked" WHERE "_rn" = 1'
        f') AS "{alias}" ON "{alias}"."record_id" = "_rec"."record_id"'
    )
    value_expr = f'CAST("{alias}"."value" AS VARCHAR)'
    return join_sql, value_expr


def _resolve_kind_columns(
    sidecar: "Sidecar",
    kind: str,
    properties: frozenset[str],
) -> tuple[str, tuple["ColumnSpec", ...], set[str], bool]:
    """Resolve records__<kind> columns and the tracked/current property partition.

    Shared setup for both state-at builders: validates the kind and every selected
    property against the sidecar.

    Args:
        sidecar: The open emit's sidecar.
        kind: The record kind to reconstruct.
        properties: The selected property names (bare).

    Returns:
        A 4-tuple (table_name, cols, tracked_set, has_pid).

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
        ExportError: A selected property has no prop__<property> column on the kind.
    """
    table_name = f"records__{kind}"
    cols = sidecar.columns(table_name)  # raises TableNotFoundError if absent
    tracked_props, _current_props = partition_properties(sidecar, kind, properties)
    tracked_set = set(tracked_props)
    has_pid = has_presentation_id(sidecar, kind)
    return table_name, cols, tracked_set, has_pid


def _build_prop_select_and_joins(
    fork_path: str,
    kind: str,
    cols: tuple["ColumnSpec", ...],
    properties: frozenset[str],
    tracked_set: set[str],
    horizon_ns: int | None,
) -> tuple[list[str], list[str]]:
    """Build prop__<p> SELECT expressions and their supporting JOINs.

    Shared between build_state_at_sql and build_state_at_end_sql: iterates
    records__<kind> columns in sidecar declaration order, emitting a tracked
    property's as-of JOIN (bounded by horizon_ns when given, unbounded at the
    tape's end when None) or a current-value CAST for each selected property.

    Args:
        fork_path: The sole branch fork_path.
        kind: The record kind.
        cols: records__<kind> columns in sidecar declaration order.
        properties: The selected property names (bare).
        tracked_set: The subset of properties that are history-tracked.
        horizon_ns: The exclusive reconstruction horizon, or None for the tape's
            end.

    Returns:
        A 2-tuple (select_parts, joins): the prop__<p> SELECT expressions in
        column order, and their supporting LEFT JOIN clauses.
    """
    select_parts: list[str] = []
    joins: list[str] = []
    for col in cols:
        if not col.name.startswith("prop__"):
            continue
        prop = col.name[len("prop__") :]
        if prop not in properties:
            continue
        if prop in tracked_set:
            join_sql, value_expr = _build_tracked_prop_join(
                fork_path, kind, prop, horizon_ns
            )
            joins.append(join_sql)
            select_parts.append(f'{value_expr} AS "prop__{prop}"')
        else:
            select_parts.append(
                f'CAST("_rec"."prop__{prop}" AS VARCHAR) AS "prop__{prop}"'
            )
    return select_parts, joins


def build_state_at_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    properties: frozenset[str],
    horizon_ns: int,
) -> str:
    """Build the canonical state-at SELECT for one kind at a single horizon.

    One row per record of the kind created strictly before the (exclusive) horizon,
    reconstructed from every event with sim_time < horizon_ns. Columns are
    STATE_AT_COLUMNS (record_id, created_sim_time, active, deactivated_at — the
    latter two horizon-rendered, raw ns), plus presentation_id when the kind
    carries it, plus one prop__<p> per selected property in sidecar
    column-declaration order: history-tracked properties as the most-recent
    history.value strictly before the horizon (codec VARCHAR; NULL when none is at
    or before), untracked properties as the current records value (the declared
    temporally-constant exception) cast to codec VARCHAR. The history_tracked
    partition applies the `is True` convention: a flag of exactly True is type-2
    (history-tracked); False or None is type-1 (current-value). Ordered by
    (created_sim_time, record_id). Reads history and records__<kind>, filtered to
    fork_path. Values are raw; wallclock rendering and type casts beyond the
    codec-VARCHAR after-image rule are the mode's representation.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind to reconstruct.
        properties: The selected property names (bare), of either class; may be
            empty (identity + lifecycle only).
        horizon_ns: The exclusive reconstruction horizon in sim-time ns; >= 0.

    Returns:
        A complete, deterministic SELECT producing STATE_AT_COLUMNS (plus
        presentation_id when present, plus one prop__<p> per selected property),
        ordered by (created_sim_time, record_id).

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
        ExportError: A selected property has no prop__<property> column on the kind.
    """
    table_name, cols, tracked_set, has_pid = _resolve_kind_columns(
        sidecar, kind, properties
    )

    fp_lit = _sql_literal(fork_path)

    select_parts: list[str] = [
        '"_rec"."record_id"',
        '"_rec"."created_sim_time"',
        (
            'CASE WHEN "_rec"."deactivated_at" IS NOT NULL'
            f' AND "_rec"."deactivated_at" < {horizon_ns}'
            ' THEN FALSE ELSE TRUE END AS "active"'
        ),
        (
            'CASE WHEN "_rec"."deactivated_at" IS NOT NULL'
            f' AND "_rec"."deactivated_at" < {horizon_ns}'
            ' THEN "_rec"."deactivated_at" ELSE NULL END AS "deactivated_at"'
        ),
    ]

    if has_pid:
        select_parts.append(
            'CAST("_rec"."presentation_id" AS VARCHAR) AS "presentation_id"'
        )

    prop_select_parts, joins = _build_prop_select_and_joins(
        fork_path, kind, cols, properties, tracked_set, horizon_ns
    )
    select_parts.extend(prop_select_parts)

    select_sql = ", ".join(select_parts)
    joins_sql = "".join(joins)

    return (
        f"SELECT {select_sql}"
        f' FROM "{table_name}" AS "_rec"'
        f"{joins_sql}"
        f' WHERE "_rec"."fork_path" = {fp_lit}'
        f' AND "_rec"."created_sim_time" < {horizon_ns}'
        f' ORDER BY "_rec"."created_sim_time", "_rec"."record_id"'
    )


def build_state_at_end_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    properties: frozenset[str],
) -> str:
    """Build the canonical end-of-tape state SELECT for one kind.

    The state-at resident's additive second entry point: the same canonical
    relation as build_state_at_sql with no horizon — no created-time row filter
    (every record of the kind), active / deactivated_at from the spine verbatim,
    each selected tracked property at its latest recorded history value, constant
    properties at their current records value. Columns and declared ORDER BY are
    STATE_AT_COLUMNS exactly as the horizoned builder emits them. "The tape's end"
    is structural: the SQL carries no horizon predicate, so composing this relation
    over truncated base relations bounds it at the truncation position with no
    horizon ever computed. Equivalence contract: equal to build_state_at_sql at any
    horizon_ns strictly beyond every history and lifecycle instant of the composed
    relations. Total over structurally-conformant input.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind to reconstruct.
        properties: Selected bare property names; may be empty (identity +
            lifecycle only).

    Returns:
        A complete, deterministic SELECT producing STATE_AT_COLUMNS plus the
        selected property columns.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
        ExportError: A selected property resolves to no prop__ column.
    """
    table_name, cols, tracked_set, has_pid = _resolve_kind_columns(
        sidecar, kind, properties
    )

    fp_lit = _sql_literal(fork_path)

    select_parts: list[str] = [
        '"_rec"."record_id"',
        '"_rec"."created_sim_time"',
        '"_rec"."active"',
        '"_rec"."deactivated_at"',
    ]

    if has_pid:
        select_parts.append(
            'CAST("_rec"."presentation_id" AS VARCHAR) AS "presentation_id"'
        )

    prop_select_parts, joins = _build_prop_select_and_joins(
        fork_path, kind, cols, properties, tracked_set, None
    )
    select_parts.extend(prop_select_parts)

    select_sql = ", ".join(select_parts)
    joins_sql = "".join(joins)

    return (
        f"SELECT {select_sql}"
        f' FROM "{table_name}" AS "_rec"'
        f"{joins_sql}"
        f' WHERE "_rec"."fork_path" = {fp_lit}'
        f' ORDER BY "_rec"."created_sim_time", "_rec"."record_id"'
    )
