"""The truncated-tape surface: relation builders and sidecar view for T.

Three relation builders and one sidecar view presenting the emit as if its
slice ended at `at_sim_time` (inclusive). Each builder returns a complete
SELECT that replaces its base table inside a mode's full-export compile
(§ Shaped state, docs/architecture/pending/playback.md); totality over
structurally-conformant input holds as for the folds. Unlike the folds' point-
in-time relations (membership_state_at.py, state_at.py), these builders carry
no canonical ORDER BY — a replacing relation's order is imposed by the compile
that reads it — and, except for their declared deviations, they present each
base table's column shape verbatim rather than a reshaped payload.

Layer-direction invariant: imports only the reader, fabulexa_forge.errors, and
stdlib. Never imports exporters.* or config.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.reader.sidecar import TableSpec

from fabulexa_forge._sql import _sql_literal
from fabulexa_forge.reader.sidecar import Sidecar
from fabulexa_forge.reader.slice_only import is_non_exempt_slice_only


def build_truncated_history_sql(
    fork_path: str,
    at_sim_time: int,
) -> str:
    """The history table truncated at T.

    Rows with sim_time <= at_sim_time, filtered to fork_path; column shape
    verbatim (history is a fixed table).

    Args:
        fork_path: The sole branch, from require_single_branch.
        at_sim_time: The inclusive truncation position T (ns); >= 0.

    Returns:
        A complete SELECT with the history table's column shape.

    Raises:
        Nothing — history is a fixed table; there is no resolvability to
        check.
    """
    fp_lit = _sql_literal(fork_path)
    return (
        'SELECT * FROM "history"'
        f' WHERE "fork_path" = {fp_lit} AND "sim_time" <= {at_sim_time}'
    )


def build_truncated_membership_sql(
    sidecar: Sidecar,
    fork_path: str,
    owner_kind: str,
    property_name: str,
    at_sim_time: int,
) -> str:
    """membership__<owner_kind>__<property_name> truncated at T.

    Intervals with joined_sim_time <= at_sim_time, filtered to fork_path;
    left_sim_time masked NULL when > at_sim_time (an interval still open at
    T, exactly as a slice-at-T emit renders it); every other column verbatim.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        owner_kind: The membership table's owner kind.
        property_name: The membership table's collection property.
        at_sim_time: The inclusive truncation position T (ns); >= 0.

    Returns:
        A complete SELECT with the membership table's column shape.

    Raises:
        TableNotFoundError: No membership__<owner_kind>__<property_name>
            table is in the sidecar.
    """
    table_name = f"membership__{owner_kind}__{property_name}"
    cols = sidecar.columns(table_name)  # raises TableNotFoundError if absent
    fp_lit = _sql_literal(fork_path)

    select_parts = [
        (
            f'CASE WHEN "left_sim_time" > {at_sim_time} THEN NULL'
            f' ELSE "left_sim_time" END AS "left_sim_time"'
            if col.name == "left_sim_time"
            else f'"{col.name}"'
        )
        for col in cols
    ]
    select_sql = ", ".join(select_parts)

    return (
        f'SELECT {select_sql} FROM "{table_name}"'
        f' WHERE "fork_path" = {fp_lit} AND "joined_sim_time" <= {at_sim_time}'
    )


def _truncate_table_columns(sidecar: Sidecar, table: "TableSpec") -> "TableSpec":
    """Drop a records__<kind> table's non-exempt slice_only columns.

    Every other table entry passes through unchanged. Shared by
    build_truncated_sidecar's per-table fold.

    Args:
        sidecar: The open emit's physical sidecar.
        table: One table entry from sidecar.tables().

    Returns:
        table unchanged, unless it is a records-category table with a
        resolvable kind, in which case a copy with its non-exempt slice_only
        columns dropped.
    """
    if table.category != "records" or table.record_kind is None:
        return table
    kind = table.record_kind
    kept = tuple(
        col
        for col in table.columns
        if not is_non_exempt_slice_only(sidecar, kind, col.name)
    )
    if kept == table.columns:
        return table
    return replace(table, columns=kept)


def build_truncated_sidecar(sidecar: Sidecar) -> Sidecar:
    """The truncated tape's sidecar view.

    Identical to the physical sidecar except that each records__<kind>
    table entry's column list drops every temporal_class slice_only
    column — a sub-typed kind's slice_only discriminator
    prop__<kind>_type excepted (the classification carve-out) — exactly
    the columns build_truncated_records_sql does not project
    (last_mutation_sim_time stays declared: the truncated relation
    presents it as the recorded trail). Every other table entry and every
    other sidecar field is unchanged — the branch's slice bound included,
    which is why no compile path under state may read a slice bound from
    the sidecar (a stated invariant of the compile indirection). Pure and
    T-independent: the dropped column set is a
    function of the declared schema, not of the truncation position.
    Column-list agreement with the relation builders is a stated invariant
    of the surface.

    Args:
        sidecar: The open emit's physical sidecar.

    Returns:
        A Sidecar describing the truncated tape; tier-2 state presents it
        over the already-open connection through the reader's public Emit
        composition (the truncated emit view).
    """
    new_tables = tuple(
        _truncate_table_columns(sidecar, table) for table in sidecar.tables()
    )
    return Sidecar(
        raw=sidecar.raw,
        base_format_version=sidecar.base_format_version,
        branches=sidecar.branches(),
        tables=new_tables,
        runtime=sidecar.runtime(),
        pinned_ids=sidecar.pinned_ids(),
        enum_domains=sidecar.enum_domains(),
        record_roles=sidecar.record_roles(),
    )
