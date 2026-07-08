"""State-at derivation: point-in-time row reconstruction at a single horizon.

Canonical relation: one row per record of a kind created strictly before a constant
horizon, reconstructed from every event with sim_time < horizon_ns. Models on the
shipped row_state_events.py fold — same layer contract, same `is True`
tracked/current-value partition, same codec-VARCHAR after-image rule — but
reconstructs at a single constant horizon, one row per record, rather than one row
per state-change event. Designed here because snapshot delivery (Unit 3) is its
first consumer, not as scaffolding.

Layer-direction invariant: imports only the reader, fabulexa_export.errors, and
stdlib. Never imports exporters.* or config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_export.reader.sidecar import Sidecar

from fabulexa_export._sql import _sql_literal
from fabulexa_export.derivations.properties import (
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
    horizon_ns: int,
) -> tuple[str, str]:
    """Build the LEFT JOIN resolving a tracked prop's as-of value at horizon_ns.

    The horizon is a single constant shared by every record (not a per-row value),
    so the as-of lookup is a windowed rank over each record's history rows strictly
    before the horizon, taking the most-recent one — a plain LEFT JOIN, not an ASOF
    JOIN (ASOF matches a per-row correlated time; there is none here).

    Args:
        fork_path: The sole branch fork_path.
        kind: The record kind.
        prop: The tracked property name (without prop__ prefix).
        horizon_ns: The exclusive reconstruction horizon in sim-time ns.

    Returns:
        A 2-tuple (join_sql, value_expr): the LEFT JOIN clause (leading space,
        bound to a per-property alias) and the VARCHAR value expression to select
        for this property's as-of value.
    """
    alias = f"_h_{prop}"
    fp_lit = _sql_literal(fork_path)
    kind_lit = _sql_literal(kind)
    prop_lit = _sql_literal(prop)
    join_sql = (
        f" LEFT JOIN ("
        f'SELECT "record_id", "value" FROM ('
        f'SELECT "record_id", "value",'
        f" ROW_NUMBER() OVER ("
        f'PARTITION BY "record_id" ORDER BY "sim_time" DESC'
        f') AS "_rn"'
        f' FROM "history"'
        f' WHERE "fork_path" = {fp_lit} AND "kind" = {kind_lit}'
        f' AND "property" = {prop_lit} AND "sim_time" < {horizon_ns}'
        f') AS "_ranked" WHERE "_rn" = 1'
        f') AS "{alias}" ON "{alias}"."record_id" = "_rec"."record_id"'
    )
    value_expr = f'CAST("{alias}"."value" AS VARCHAR)'
    return join_sql, value_expr


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
    table_name = f"records__{kind}"
    cols = sidecar.columns(table_name)  # raises TableNotFoundError if absent

    # Raises TableNotFoundError / ExportError for an unknown kind / property.
    tracked_props, _current_props = partition_properties(sidecar, kind, properties)
    tracked_set = set(tracked_props)
    has_pid = has_presentation_id(sidecar, kind)

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
