"""Membership-state-at derivation: point-in-time membership containment fold.

Canonical relation: one row per interval of a membership__<owner_kind>__<property>
table that contains a constant horizon — the point-in-time counterpart to
membership-events (membership_events.py). Reuses that module's column-order
resolver so both folds' payload shapes are one source of truth.

Layer-direction invariant: imports only the reader, fabulexa_forge.errors, and
stdlib. Never imports exporters.* or config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge._sql import _sql_literal
from fabulexa_forge.derivations.membership_events import resolve_membership_columns

#: Canonical leading columns of the membership-state-at fold, in order.
#: joined_sim_time is the raw containment-interval start (ns); left_sim_time is
#: never projected. Field-value columns from resolve_membership_columns follow.
MEMBERSHIP_STATE_AT_COLUMNS: tuple[str, ...] = (
    "record_id",
    "joined_sim_time",
)


def build_membership_state_at_sql(
    sidecar: "Sidecar",
    fork_path: str,
    owner_kind: str,
    property_name: str,
    fields: Sequence[str],
    horizon_ns: int,
) -> str:
    """Build the canonical membership containment SELECT at one horizon.

    One row per membership__<owner_kind>__<property_name> interval containing
    the exclusive horizon: joined_sim_time < horizon_ns AND (left_sim_time IS
    NULL OR left_sim_time >= horizon_ns). Columns are
    MEMBERSHIP_STATE_AT_COLUMNS -- record_id (the owner), joined_sim_time (raw
    ns) -- plus each selected element-schema field's column shape (scalar
    elem__<f>, or the reference pair member__<f>__kind / member__<f>__id) in
    resolve_membership_columns order, each cast to codec VARCHAR.
    left_sim_time is never projected (future state relative to the horizon).
    Ordered by (joined_sim_time, record_id, <field tail>), the tail compared
    as CAST(... AS VARCHAR) NULLS FIRST. Total over structurally-conformant
    input: distorted intervals answer deterministically, never error.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        owner_kind: The membership table's owner kind.
        property_name: The membership table's collection property.
        fields: Selected element-schema field names (bare); may be empty
            (owner identity + joined_sim_time only).
        horizon_ns: The exclusive containment horizon in sim-time ns; >= 0.

    Returns:
        A complete, deterministic SELECT producing
        MEMBERSHIP_STATE_AT_COLUMNS plus the selected field columns.

    Raises:
        TableNotFoundError: No membership__<owner_kind>__<property_name>
            table is in the sidecar.
        ExportError: A selected field resolves to no elem__/member__ column
            shape on the table.
    """
    table_name = f"membership__{owner_kind}__{property_name}"
    # raises TableNotFoundError if absent; also validates fields
    payload_cols = resolve_membership_columns(
        sidecar, owner_kind, property_name, fields
    )
    # payload_cols[0] == "record_id"; remainder are field columns

    fp_lit = _sql_literal(fork_path)

    select_parts = [
        '"record_id"',
        '"joined_sim_time"',
        *(f'CAST("{col}" AS VARCHAR) AS "{col}"' for col in payload_cols[1:]),
    ]
    select_sql = ", ".join(select_parts)

    order_parts = ['"joined_sim_time"', '"record_id"']
    for col in payload_cols[1:]:
        order_parts.append(f'CAST("{col}" AS VARCHAR) NULLS FIRST')
    order_by = ", ".join(order_parts)

    return (
        f'SELECT {select_sql} FROM "{table_name}"'
        f' WHERE "fork_path" = {fp_lit}'
        f' AND "joined_sim_time" < {horizon_ns}'
        f' AND ("left_sim_time" IS NULL OR "left_sim_time" >= {horizon_ns})'
        f" ORDER BY {order_by}"
    )
