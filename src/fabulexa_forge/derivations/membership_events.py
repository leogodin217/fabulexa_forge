"""Membership-events fold derivation: membership intervals → join/leave event stream.

Canonical relation: one row per (record_id, event_sim_time) at which a member
joined (event_class=0) or left (event_class=1) a membership table. Each open
interval produces a single join event; a closed interval produces a join at
joined_sim_time and a leave at left_sim_time.

Layer-direction invariant: imports only the reader, fabulexa_forge.errors,
and stdlib. Never imports exporters.* or config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge._sql import _sql_literal
from fabulexa_forge.errors import ExportError

#: Canonical leading columns of the membership-events fold, in order.
#: event_class is 0=join, 1=leave; op is 'join'/'leave'.
#: Field-value columns from resolve_membership_columns follow.
MEMBERSHIP_EVENT_COLUMNS: tuple[str, ...] = (
    "record_id",
    "event_sim_time",
    "event_class",
    "op",
)

#: event_class value for a join event (joined_sim_time).
EVENT_CLASS_JOIN: int = 0

#: event_class value for a leave event (left_sim_time).
EVENT_CLASS_LEAVE: int = 1


def resolve_membership_columns(
    sidecar: "Sidecar",
    owner_kind: str,
    property_name: str,
    fields: Sequence[str],
) -> tuple[str, ...]:
    """Resolve a membership payload's column order.

    Single producer of after-image order.

    record_id first, then each selected element-schema field's column(s) in
    element-schema declaration order — a scalar field f -> ('elem__<f>',); a
    reference field f -> ('member__<f>__kind', 'member__<f>__id'). Both the
    fold's SELECT and the engine's after-image keying call this, so the declared
    order and the rendered rows are one list. Probes the reference pair first,
    then the scalar column, and uses the first that resolves.

    Raises:
        ExportError: A field resolves to neither an elem__<f> column nor a
            member__<f>__id / member__<f>__kind pair on the table.
        TableNotFoundError: membership__<owner_kind>__<property_name> is absent.
    """
    table_name = f"membership__{owner_kind}__{property_name}"
    cols = sidecar.columns(table_name)  # raises TableNotFoundError if absent

    col_names = {c.name for c in cols}
    fields_set = set(fields)

    # Validate each field resolves to elem__ or member__ columns on the table
    for field in fields_set:
        ref_kind_col = f"member__{field}__kind"
        ref_id_col = f"member__{field}__id"
        scalar_col = f"elem__{field}"
        if ref_kind_col in col_names and ref_id_col in col_names:
            continue  # reference field
        if scalar_col in col_names:
            continue  # scalar field
        raise ExportError(
            f"membership field '{field}' on '{table_name}' resolves to neither"
            f" elem__{field} nor member__{field}__kind / member__{field}__id"
        )

    # Build result in element-schema declaration order
    result: list[str] = ["record_id"]

    # Walk columns in declaration order, emitting columns for selected fields
    emitted_fields: set[str] = set()
    for col in cols:
        name = col.name
        # Check for reference field: member__<f>__kind
        if name.startswith("member__") and name.endswith("__kind"):
            field = name[len("member__") : -len("__kind")]
            if field in fields_set and field not in emitted_fields:
                result.append(f"member__{field}__kind")
                result.append(f"member__{field}__id")
                emitted_fields.add(field)
        # Check for scalar field: elem__<f>
        elif name.startswith("elem__"):
            field = name[len("elem__") :]
            if field in fields_set and field not in emitted_fields:
                result.append(f"elem__{field}")
                emitted_fields.add(field)

    return tuple(result)


def fold_row_column_names(
    sidecar: "Sidecar",
    owner_kind: str,
    property_name: str,
    fields: Sequence[str],
) -> list[str]:
    """The membership-events fold's row column names, in emission order.

    MEMBERSHIP_EVENT_COLUMNS (the four fixed prefix columns) followed by
    resolve_membership_columns' payload columns after its leading
    'record_id' (already covered by the prefix) — the single column-name
    list matching build_membership_events_sql's SELECT order. Shared by
    every caller that must destructure a fold row by name (the streaming
    engine, the playback seam).

    Args:
        sidecar: The open emit's sidecar.
        owner_kind: The membership table's owner kind.
        property_name: The membership property name.
        fields: The selected element-schema field names (bare).

    Returns:
        Ordered column names: 'record_id', 'event_sim_time', 'event_class',
        'op', then one or two payload columns per selected field in
        element-schema declaration order.

    Raises:
        TableNotFoundError: membership__<owner_kind>__<property_name> is absent.
        ExportError: A selected field resolves to no elem__/member__ column.
    """
    payload_cols = resolve_membership_columns(
        sidecar, owner_kind, property_name, fields
    )
    # payload_cols[0] == 'record_id', already in MEMBERSHIP_EVENT_COLUMNS[0]
    return list(MEMBERSHIP_EVENT_COLUMNS) + list(payload_cols[1:])


def build_membership_events_sql(
    sidecar: "Sidecar",
    fork_path: str,
    owner_kind: str,
    property_name: str,
    fields: Sequence[str],
) -> str:
    """Build the membership join/leave event stream for one membership table.

    Reads membership__<owner_kind>__<property_name> (filtered to fork_path only;
    no sim_time window) and unpivots each interval into a join at joined_sim_time
    and, when left_sim_time is non-null, a leave at left_sim_time (UNION ALL of
    the join projection and the left_sim_time-non-null leave projection). Output
    columns are MEMBERSHIP_EVENT_COLUMNS plus the resolve_membership_columns tail;
    the owner record_id and every element-field column are wrapped in
    CAST(<col> AS VARCHAR) (VARCHAR-or-NULL); event_sim_time and event_class are
    projected as raw integers. ORDER BY (event_sim_time, event_class, record_id,
    then the selected-field columns in resolve_membership_columns order); each
    field column ordered NULLS FIRST (explicit — DuckDB ASC default is NULLS LAST),
    a reference field by member__<f>__kind then member__<f>__id. Reads only the
    membership table and the sidecar.

    Raises:
        TableNotFoundError: membership__<owner_kind>__<property_name> is absent.
        ExportError: A selected field does not resolve to elem__/member__ columns.
    """
    table_name = f"membership__{owner_kind}__{property_name}"
    # raises TableNotFoundError if absent; also validates fields
    payload_cols = resolve_membership_columns(
        sidecar, owner_kind, property_name, fields
    )
    # payload_cols[0] == "record_id"; remainder are field columns

    fp_lit = _sql_literal(fork_path)

    # Build the field-value SELECT expressions for after-image columns
    # payload_cols[1:] are the field columns in declaration order
    field_col_exprs = _build_field_col_exprs(payload_cols[1:])

    # join projection: event_sim_time = joined_sim_time, event_class = 0
    join_select = _build_event_select(
        table_name,
        fp_lit,
        '"joined_sim_time"',
        EVENT_CLASS_JOIN,
        "join",
        field_col_exprs,
        where_extra=None,
    )

    # leave projection: event_sim_time = left_sim_time, event_class = 1
    # only when left_sim_time IS NOT NULL
    leave_select = _build_event_select(
        table_name,
        fp_lit,
        '"left_sim_time"',
        EVENT_CLASS_LEAVE,
        "leave",
        field_col_exprs,
        where_extra='"left_sim_time" IS NOT NULL',
    )

    # ORDER BY clause — prefix + field tail (NULLS FIRST for each field column)
    order_parts = [
        '"event_sim_time"',
        '"event_class"',
        '"record_id"',
    ]
    for col in payload_cols[1:]:
        order_parts.append(f'CAST("{col}" AS VARCHAR) NULLS FIRST')

    order_by = ", ".join(order_parts)

    return (
        f"SELECT * FROM ({join_select} UNION ALL {leave_select})"
        f" AS _mem_events"
        f" ORDER BY {order_by}"
    )


def _build_field_col_exprs(field_cols: tuple[str, ...]) -> list[str]:
    """Build CAST(<col> AS VARCHAR) expressions for field payload columns.

    Args:
        field_cols: Column names from resolve_membership_columns (after record_id).

    Returns:
        List of SQL expressions in the same order, each cast to VARCHAR.
    """
    return [f'CAST("{col}" AS VARCHAR) AS "{col}"' for col in field_cols]


def _build_event_select(
    table_name: str,
    fp_lit: str,
    event_time_expr: str,
    event_class: int,
    op: str,
    field_col_exprs: list[str],
    where_extra: str | None,
) -> str:
    """Build one projection (join or leave) of the membership event UNION ALL.

    Args:
        table_name: The membership table name (quoted externally).
        fp_lit: SQL literal for the fork_path filter.
        event_time_expr: SQL expression for event_sim_time (e.g. '"joined_sim_time"').
        event_class: Integer event_class value (0=join, 1=leave).
        op: String op value ('join' or 'leave').
        field_col_exprs: CAST expressions for payload field columns.
        where_extra: Optional additional WHERE condition
            (e.g. 'left_sim_time IS NOT NULL').

    Returns:
        A complete SELECT string for this event projection.
    """
    select_parts = [
        'CAST("record_id" AS VARCHAR) AS "record_id"',
        f'{event_time_expr} AS "event_sim_time"',
        f'{event_class} AS "event_class"',
        f"'{op}' AS \"op\"",
    ]
    select_parts.extend(field_col_exprs)

    select_sql = ", ".join(select_parts)

    where_parts = [f'"fork_path" = {fp_lit}']
    if where_extra is not None:
        where_parts.append(where_extra)
    where_sql = " AND ".join(where_parts)

    return f'SELECT {select_sql} FROM "{table_name}" WHERE {where_sql}'
