"""Row-state-events fold derivation: history → per-record c/u/d event stream.

Canonical relation: one row per (record_id, sim_time) at which the record's
state changes — a 'c' at created_sim_time for every record, a 'u' at each later
distinct history sim_time of the selected history-tracked properties, and a 'd' at
deactivated_at when the record is deactivated. After-image columns reconstruct the
full row at the event sim_time. Ordered (event_sim_time, event_class, record_id).

Layer-direction invariant: imports only the reader, fabulexa_forge.errors,
and stdlib. Never imports exporters.* or config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge._sql import _sql_literal
from fabulexa_forge.derivations.properties import (
    _validate_selected_properties,
    has_presentation_id,
    partition_properties,
)

#: The fixed canonical prefix columns for row-state-events.
#: 'op' is a SQL CASE recode of event_class (0→'c', 1→'u', 2→'d').
#: 'presentation_id' and prop__<p> columns follow when applicable.
ROW_STATE_EVENT_COLUMNS: tuple[str, ...] = (
    "record_id",
    "event_sim_time",
    "event_class",
    "op",
)

#: event_class value for a create event (genesis on created_sim_time).
EVENT_CLASS_CREATE: int = 0

#: event_class value for an update event (later history change point).
EVENT_CLASS_UPDATE: int = 1

#: event_class value for a delete event (deactivation at deactivated_at).
EVENT_CLASS_DELETE: int = 2


def resolve_stream_columns(
    sidecar: "Sidecar",
    kind: str,
    properties: frozenset[str],
) -> list[str]:
    """The ordered carried-column names for a kind's after-image.

    The single rule the fold's SELECT ordering, the engine's after-image keying, and
    the Debezium value schema all follow, so the declared schema matches the rendered
    rows. A derivation-layer function: it takes a sidecar + bare kind + property set,
    never a config model, so the fold — which imports no config — calls it directly;
    the engine and the schema builder destructure their StreamKindSelection at the
    call site, the established convention.

    Args:
        sidecar: The open emit's sidecar (resolves the surrogate and prop__ columns).
        kind: The record kind.
        properties: The selected property names (bare).

    Returns:
        Column names in after-image order: 'record_id', then 'presentation_id' when
        the kind carries a surrogate, then one 'prop__<p>' per selected property in
        sidecar column-declaration order. This is the single after-image column
        order; the engine's fold-row column list is ROW_STATE_EVENT_COLUMNS +
        this[1:], and the fold's SELECT emits its after-image columns in this order.

    Raises:
        TableNotFoundError: The kind has no records__<kind> table.
        ExportError: A selected property has no prop__<property> column on the kind.
    """
    table_name = f"records__{kind}"
    cols = sidecar.columns(table_name)  # raises TableNotFoundError if absent

    _validate_selected_properties(kind, cols, properties, label="stream kind")

    names: list[str] = ["record_id"]

    # presentation_id second, when the kind carries a surrogate
    if has_presentation_id(sidecar, kind):
        names.append("presentation_id")

    # prop__<p> columns in sidecar column-declaration order for selected properties
    for col in cols:
        if col.name.startswith("prop__"):
            prop = col.name[len("prop__") :]
            if prop in properties:
                names.append(f"prop__{prop}")

    return names


def fold_row_column_names(
    sidecar: "Sidecar",
    kind: str,
    properties: frozenset[str],
) -> list[str]:
    """The row-state-events fold's row column names, in emission order.

    ROW_STATE_EVENT_COLUMNS (the four fixed prefix columns) followed by
    resolve_stream_columns' after-image columns after its leading
    'record_id' (already covered by the prefix) — the single column-name
    list matching build_row_state_events_sql's SELECT order. Shared by every
    caller that must destructure a fold row by name (the streaming engine,
    the playback seam).

    Args:
        sidecar: The open emit's sidecar.
        kind: The record kind.
        properties: The selected property names (bare), of either class.

    Returns:
        Ordered column names: 'record_id', 'event_sim_time', 'event_class',
        'op', then 'presentation_id' (when the kind carries one), then one
        'prop__<p>' per selected property in sidecar declaration order.

    Raises:
        TableNotFoundError: The kind has no records__<kind> table.
        ExportError: A selected property has no prop__<property> column.
    """
    after_image = resolve_stream_columns(sidecar, kind, properties)
    # after_image[0] == 'record_id', which is already in ROW_STATE_EVENT_COLUMNS[0]
    return list(ROW_STATE_EVENT_COLUMNS) + after_image[1:]


def _build_prop_asof_join(
    fork_path: str,
    kind: str,
    prop: str,
    events_alias: str,
) -> tuple[str, str]:
    """Build the ASOF LEFT JOIN that reconstructs a history-tracked prop's after-image.

    DuckDB's ASOF JOIN resolves the as-of lookback in a single sort-merge pass — for
    each event row it matches the one history row with the greatest sim_time at or
    before event_sim_time. This replaces the former per-row correlated subquery,
    which the optimizer did not collapse and which scanned a record's whole history
    once per event row — O(events x history) per record, quadratic when a single
    record concentrates many history rows (one resource changing ~9.7k times OOM'd at
    ~13 GB). LEFT keeps event rows with no prior history (the prop reads NULL there);
    the single inequality (sim_time <= event_sim_time) is inclusive, preserving the
    create-event seed and update-point semantics the correlated subquery had.

    Args:
        fork_path: The sole branch fork_path.
        kind: The record kind.
        prop: The tracked property name (without prop__ prefix).
        events_alias: The alias of the events relation to match against.

    Returns:
        A 2-tuple (join_sql, value_expr): the ASOF LEFT JOIN clause (leading space,
        bound to a per-property alias) and the VARCHAR value expression to select for
        this property's after-image.
    """
    alias = f"_h_{prop}"
    join_sql = (
        f' ASOF LEFT JOIN "history" AS "{alias}"'
        f' ON "{alias}"."fork_path" = {_sql_literal(fork_path)}'
        f' AND "{alias}"."kind" = {_sql_literal(kind)}'
        f' AND "{alias}"."property" = {_sql_literal(prop)}'
        f' AND "{alias}"."record_id" = "{events_alias}"."record_id"'
        f' AND "{alias}"."sim_time" <= "{events_alias}"."event_sim_time"'
    )
    value_expr = f'CAST("{alias}"."value" AS VARCHAR)'
    return join_sql, value_expr


def _build_events_cte(
    fork_path: str,
    kind: str,
    tracked_props: list[str],
) -> str:
    """Build the _events CTE: the union of create, update, and delete event times.

    Genesis (c) comes from records__<kind>.created_sim_time for every record.
    Updates (u) come from the distinct later history sim_times of tracked props.
    Deletes (d) come from records__<kind>.deactivated_at when non-NULL.

    Args:
        fork_path: The sole branch fork_path.
        kind: The record kind.
        tracked_props: History-tracked property names (may be empty).

    Returns:
        SQL for the _events CTE body.
    """
    records_table = f"records__{kind}"
    fp_lit = _sql_literal(fork_path)
    kind_lit = _sql_literal(kind)

    # c events: genesis on created_sim_time for every record
    create_sel = (
        f'SELECT "record_id",'
        f' "created_sim_time" AS "event_sim_time",'
        f' {EVENT_CLASS_CREATE} AS "event_class"'
        f' FROM "{records_table}"'
        f' WHERE "fork_path" = {fp_lit}'
    )

    parts = [create_sel]

    # u events: later distinct history change points (strictly after created_sim_time)
    if tracked_props:
        prop_filters = " OR ".join(
            f'"property" = {_sql_literal(p)}' for p in sorted(tracked_props)
        )
        update_sel = (
            f'SELECT DISTINCT "h"."record_id",'
            f' "h"."sim_time" AS "event_sim_time",'
            f' {EVENT_CLASS_UPDATE} AS "event_class"'
            f' FROM "history" AS "h"'
            f' JOIN "{records_table}" AS "r"'
            f' ON "r"."record_id" = "h"."record_id"'
            f' AND "r"."fork_path" = {fp_lit}'
            f' WHERE "h"."fork_path" = {fp_lit}'
            f' AND "h"."kind" = {kind_lit}'
            f" AND ({prop_filters})"
            f' AND "h"."sim_time" > "r"."created_sim_time"'
        )
        parts.append(update_sel)

    # d events: deactivated records (deactivated_at is non-NULL)
    delete_sel = (
        f'SELECT "record_id",'
        f' "deactivated_at" AS "event_sim_time",'
        f' {EVENT_CLASS_DELETE} AS "event_class"'
        f' FROM "{records_table}"'
        f' WHERE "fork_path" = {fp_lit}'
        f' AND "deactivated_at" IS NOT NULL'
    )
    parts.append(delete_sel)

    union_sql = " UNION ALL ".join(parts)
    return union_sql


def build_row_state_events_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    properties: frozenset[str],
    change_scope: frozenset[str],
) -> str:
    """Build the canonical row-state-events SELECT for one kind.

    The two-scope contract (design doc § Per-stream folds and after-images):
    event membership and after-image projection are independently scoped.
    One event row per (record_id, sim_time) at which the record's state
    changes: a 'c' at created_sim_time for every record, a 'u' at each later
    distinct history sim_time of `change_scope`'s history-tracked subset, and
    a 'd' at deactivated_at when deactivated. The after-image columns are
    `properties` resolved by resolve_stream_columns — the projection scope
    never widens or narrows the event set, and the change scope never adds a
    column to the SELECT.

    The after-image columns reconstruct the full row at the event sim_time —
    each selected history-tracked property as the most-recent history.value
    at or before that time (inclusive sim_time <= event_sim_time; codec
    VARCHAR; NULL when none is at or before), each selected current-value
    property as the record's current records__<kind> value cast to codec
    VARCHAR (temporally constant). The identity after-image columns are
    likewise cast to codec VARCHAR (a BIGINT presentation_id is cast, not
    assumed already-string), so every after-image column is VARCHAR
    (str-or-NULL). On a 'd' row the after-image columns are NULL. Both
    `properties` and `change_scope` are partitioned into history-tracked vs
    current-value classes by reading each column's sidecar history_tracked
    flag, applying the shipped `is True` convention (_collect_tracked_props,
    scd.py): a flag of exactly True is type-2 (history); anything else —
    False, or None on a non-conformant emit — is type-1 (current value); a
    current-value name in `change_scope` contributes no 'u' events. The class
    is never inferred from history and has no inference fallback. Reads
    history (filtered to the kind and each scope's history-tracked subset)
    and records__<kind> (spine + current values + column order).
    Single-branch: filtered to fork_path. Values are raw — wallclock
    rendering and the global seq are the engine's representation.

    See § After-image reconstruction, § Op classification, § Event generation per
    record, § Ordering and `seq`.

    Args:
        sidecar: The open emit's sidecar (schema, column order, history_tracked,
            presentation_id presence).
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind whose row state is reconstructed.
        properties: The after-image property names (bare), of either class;
            may be empty (identity + lifecycle only).
        change_scope: The property names (bare) whose history-tracked subset
            drives 'u' event membership; may equal `properties` (the shipped
            single-scope behavior, byte-identical) and may be a superset or
            disjoint. Callers state both scopes explicitly — no default.

    Returns:
        A complete, deterministic SELECT producing ROW_STATE_EVENT_COLUMNS (plus
        presentation_id when present, plus one prop__<p> per selected property),
        ordered by (event_sim_time, event_class, record_id).

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar. Defensive: the
            engine's up-front validation (StreamKindResolvable) catches this first.
        ExportError: A name in `properties` or `change_scope` has no
            prop__<name> column on the kind. Likewise defensive —
            StreamPropertyResolvable catches it first.
    """
    table_name = f"records__{kind}"
    # Raises TableNotFoundError if absent; also validates properties
    after_image_cols = resolve_stream_columns(sidecar, kind, properties)

    change_tracked_props, _ = partition_properties(sidecar, kind, change_scope)
    after_image_tracked_props, _ = partition_properties(sidecar, kind, properties)
    has_pid = has_presentation_id(sidecar, kind)

    fp_lit = _sql_literal(fork_path)

    events_cte_sql = _build_events_cte(fork_path, kind, change_tracked_props)

    # Build the final SELECT columns list
    # Fixed prefix: record_id, event_sim_time, event_class, op
    select_parts: list[str] = [
        '"_events"."record_id"',
        '"_events"."event_sim_time"',
        '"_events"."event_class"',
        (
            'CASE "_events"."event_class"'
            f" WHEN {EVENT_CLASS_CREATE} THEN 'c'"
            f" WHEN {EVENT_CLASS_UPDATE} THEN 'u'"
            f" WHEN {EVENT_CLASS_DELETE} THEN 'd'"
            ' END AS "op"'
        ),
    ]

    # The after-image's history-tracked properties (properties ∩ full tracked set) —
    # independent of change_scope's tracked subset, which drives event membership only.
    sidecar_tracked: set[str] = set(after_image_tracked_props)

    # One ASOF LEFT JOIN per after-image history-tracked property reconstructs its
    # value in a single linear pass. after_image_tracked_props is exactly the
    # properties-selected tracked props in sidecar order, so its value expressions
    # key by prop below.
    asof_joins: list[str] = []
    tracked_value_exprs: dict[str, str] = {}
    for prop in after_image_tracked_props:
        join_sql, value_expr = _build_prop_asof_join(fork_path, kind, prop, "_events")
        asof_joins.append(join_sql)
        tracked_value_exprs[prop] = value_expr

    # After-image columns in resolve_stream_columns order (presentation_id then props)
    # after_image_cols[0] is 'record_id' — skip it (already in fixed prefix)
    for col_name in after_image_cols[1:]:
        if col_name == "presentation_id":
            select_parts.append(
                f'CASE WHEN "_events"."event_class" = {EVENT_CLASS_DELETE}'
                f" THEN NULL"
                f' ELSE CAST("_rec"."presentation_id" AS VARCHAR)'
                f' END AS "presentation_id"'
            )
        else:
            # col_name is prop__<p>
            prop = col_name[len("prop__") :]
            if prop in sidecar_tracked:
                value_expr = tracked_value_exprs[prop]
                select_parts.append(
                    f'CASE WHEN "_events"."event_class" = {EVENT_CLASS_DELETE}'
                    f" THEN NULL"
                    f" ELSE {value_expr}"
                    f' END AS "prop__{prop}"'
                )
            else:
                select_parts.append(
                    f'CASE WHEN "_events"."event_class" = {EVENT_CLASS_DELETE}'
                    f" THEN NULL"
                    f' ELSE CAST("_rec"."prop__{prop}" AS VARCHAR)'
                    f' END AS "prop__{prop}"'
                )

    select_sql = ", ".join(select_parts)

    # We need a JOIN to records__<kind> for presentation_id and current-value props
    current_props_in_selection = properties - sidecar_tracked
    need_rec_join = has_pid or bool(current_props_in_selection)

    if need_rec_join:
        join_clause = (
            f' LEFT JOIN "{table_name}" AS "_rec"'
            f' ON "_rec"."record_id" = "_events"."record_id"'
            f' AND "_rec"."fork_path" = {fp_lit}'
        )
    else:
        join_clause = ""

    asof_clause = "".join(asof_joins)

    return (
        f'WITH "_events" AS ({events_cte_sql})'
        f" SELECT {select_sql}"
        f' FROM "_events"'
        f"{join_clause}"
        f"{asof_clause}"
        f' ORDER BY "_events"."event_sim_time", "_events"."event_class",'
        f' "_events"."record_id"'
    )
