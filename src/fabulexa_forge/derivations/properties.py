"""Shared property-partition and surrogate-detection helpers for the derivations
layer.

Both the row-state-events fold and the state-at derivation reconstruct a kind's
after-image from the same sidecar-declared classes: history-tracked properties (the
`is True` convention) versus current-value properties, and the optional
`presentation_id` surrogate. This module is the single implementation both derivations
call, so the classification rule never drifts between them.

Layer-direction invariant: imports only the reader, fabulexa_forge.errors, and
stdlib. Never imports exporters.* or config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.reader.sidecar import ColumnSpec, Sidecar

from fabulexa_forge._sql import _sql_literal
from fabulexa_forge.errors import ExportError


def _validate_selected_properties(
    kind: str,
    cols: "tuple[ColumnSpec, ...]",
    properties: frozenset[str],
    *,
    label: str,
) -> None:
    """Raise if any selected property has no prop__<property> column on the kind.

    Shared by `partition_properties` and `resolve_stream_columns` (row_state_events.py)
    so the existence check never drifts between call sites. `label` preserves each
    call site's distinct error-message prefix ("kind" vs "stream kind").

    Args:
        kind: The record kind.
        cols: The kind's sidecar columns (from `sidecar.columns(...)`).
        properties: Selected property names (without prop__ prefix).
        label: The error-message prefix identifying the caller's context.

    Raises:
        ExportError: A selected property has no prop__<property> column on the kind.
    """
    sidecar_prop_names = {
        col.name[len("prop__") :] for col in cols if col.name.startswith("prop__")
    }
    for prop in properties:
        if prop not in sidecar_prop_names:
            raise ExportError(
                f"{label} '{kind}': property '{prop}' has no prop__{prop} column"
            )


def partition_properties(
    sidecar: "Sidecar",
    kind: str,
    properties: frozenset[str],
) -> tuple[list[str], list[str]]:
    """Partition selected properties into history-tracked and current-value sets.

    Uses the `is True` convention: a history_tracked flag of exactly True → type-2
    (history-tracked); False or None → type-1 (current-value). The class is read
    from the sidecar and never inferred from the history table.

    Args:
        sidecar: The open emit's sidecar.
        kind: The record kind.
        properties: Selected property names (without prop__ prefix).

    Returns:
        A 2-tuple of (tracked_props, current_props), each in sidecar
        column-declaration order.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
        ExportError: A selected property has no prop__<property> column on the kind.
    """
    table_name = f"records__{kind}"
    cols = sidecar.columns(table_name)  # raises TableNotFoundError if absent

    _validate_selected_properties(kind, cols, properties, label="kind")

    # Partition into tracked vs current-value in sidecar column-declaration order
    tracked: list[str] = []
    current: list[str] = []
    for col in cols:
        if col.name.startswith("prop__"):
            prop = col.name[len("prop__") :]
            if prop in properties:
                if col.history_tracked is True:
                    tracked.append(prop)
                else:
                    current.append(prop)

    return tracked, current


def has_presentation_id(sidecar: "Sidecar", kind: str) -> bool:
    """Return True if the kind's records table carries a presentation_id column.

    Args:
        sidecar: The open emit's sidecar.
        kind: The record kind.

    Returns:
        True iff 'presentation_id' appears in the kind's sidecar column list.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
    """
    table_name = f"records__{kind}"
    cols = sidecar.columns(table_name)  # raises TableNotFoundError if absent
    return any(col.name == "presentation_id" for col in cols)


def build_history_asof_join(
    fork_path: str,
    kind: str,
    prop: str,
    alias: str,
    bound_ns: int | None,
    *,
    inclusive: bool,
) -> tuple[str, str]:
    """Build a LEFT JOIN resolving one property's most-recent as-of history value.

    The as-of lookup is a windowed rank over each record's history rows for
    (kind, prop), taking the most-recent one — a plain LEFT JOIN, not an ASOF
    JOIN (ASOF matches a per-row correlated time; there is none here). With
    bound_ns given, only rows at-or-before (inclusive=True) or strictly before
    (inclusive=False) the bound are ranked. With bound_ns None every history
    row is ranked — "most recent" is unbounded (the tape's end); inclusive is
    then unused.

    Shared by state_at.py's build_state_at_sql / build_state_at_end_sql
    (exclusive horizon, unbounded end) and truncated_tape.py's
    build_truncated_records_sql (inclusive T) — the identical windowed-rank
    pattern, parameterized by the bound's inclusivity, so it never drifts
    between the two point-in-time reconstructions.

    Args:
        fork_path: The sole branch fork_path.
        kind: The record kind.
        prop: The tracked property name (without prop__ prefix).
        alias: The subquery's SQL alias; must be unique within the enclosing
            SELECT's FROM clause.
        bound_ns: The reconstruction bound in sim-time ns, or None for no
            bound (every history row ranked).
        inclusive: Whether a row at exactly bound_ns is ranked (True) or
            excluded (False). Ignored when bound_ns is None.

    Returns:
        A 2-tuple (join_sql, value_expr): the LEFT JOIN clause (leading
        space, bound to alias) and the VARCHAR value expression to select
        for this property's as-of value.
    """
    fp_lit = _sql_literal(fork_path)
    kind_lit = _sql_literal(kind)
    prop_lit = _sql_literal(prop)
    if bound_ns is None:
        bound_predicate = ""
    else:
        op = "<=" if inclusive else "<"
        bound_predicate = f' AND "sim_time" {op} {bound_ns}'
    join_sql = (
        f" LEFT JOIN ("
        f'SELECT "record_id", "value" FROM ('
        f'SELECT "record_id", "value",'
        f" ROW_NUMBER() OVER ("
        f'PARTITION BY "record_id" ORDER BY "sim_time" DESC'
        f') AS "_rn"'
        f' FROM "history"'
        f' WHERE "fork_path" = {fp_lit} AND "kind" = {kind_lit}'
        f' AND "property" = {prop_lit}{bound_predicate}'
        f') AS "_ranked" WHERE "_rn" = 1'
        f') AS "{alias}" ON "{alias}"."record_id" = "_rec"."record_id"'
    )
    value_expr = f'CAST("{alias}"."value" AS VARCHAR)'
    return join_sql, value_expr
