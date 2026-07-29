"""Record-index derivation: the record-id-to-record-index join relation.

The derivations layer's seventh resident. Two entry points, mirroring the
folds' horizon / end-of-tape split (state_at.py, truncated_tape.py):
build_record_index_at_sql bounds the relation to records created strictly
before a horizon; build_record_index_at_end_sql carries no horizon predicate
at all, so composing it over a truncated base relation bounds it at the
truncation with no horizon ever computed.

Unlike the folds, this is a join relation, not a reconstruction: it declares
no ORDER BY, because a consumer LEFT JOINs it rather than reading it ordered.
The DISTINCT in both builders is a no-op on a conformant emit; it exists so a
row-duplicated corrupted emit, whose duplicate row carries an identical
(record_id, record_index) pair, cannot fan a consumer's key join out.

Layer-direction invariant: imports only the reader, fabulexa_forge._sql /
fabulexa_forge.errors, and stdlib. Never imports exporters.* or config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge._sql import _sql_literal

#: The canonical column list of the record-index relation, in emission order.
RECORD_INDEX_COLUMNS: tuple[str, ...] = ("record_id", "record_index")


def build_record_index_at_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    horizon_ns: int,
) -> str:
    """Build the record-id-to-record-index relation for one kind at a horizon.

    One row per distinct (record_id, record_index) pair among the kind's
    records created strictly before the exclusive horizon, projecting
    RECORD_INDEX_COLUMNS — on a conformant emit, one row per record. The
    DISTINCT is a no-op under conformance; it exists so a row-duplicated
    corrupted emit, whose duplicate carries the identical pair, cannot fan a
    consumer's key join out. `record_index` is projected verbatim — the
    contract pins it as set once at creation and never renumbered, so it is a
    temporally-constant value read at a creation instant already bounded
    below the horizon. Rows are filtered on creation time and to `fork_path`;
    `active` is never a predicate, so a record deactivated before the horizon
    is present and remains a resolvable reference target. A join relation,
    not a fold: it declares no ORDER BY, because a consumer LEFT JOINs it
    rather than reading it ordered.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind whose index relation to build.
        horizon_ns: The exclusive horizon in sim-time ns; >= 0.

    Returns:
        A complete, deterministic SELECT producing RECORD_INDEX_COLUMNS.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
    """
    table_name = f"records__{kind}"
    sidecar.columns(table_name)  # raises TableNotFoundError if absent

    fp_lit = _sql_literal(fork_path)

    return (
        'SELECT DISTINCT "record_id", "record_index"'
        f' FROM "{table_name}"'
        f' WHERE "fork_path" = {fp_lit}'
        f' AND "created_sim_time" < {horizon_ns}'
    )


def build_record_index_at_end_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
) -> str:
    """Build the record-id-to-record-index relation for one kind at the tape's end.

    The resident's second entry point: the same DISTINCT RECORD_INDEX_COLUMNS
    relation with no horizon — every record of the kind, filtered only to
    `fork_path`. "The tape's end" is structural: the SQL carries no horizon
    predicate, so composing this relation over a truncated base relation
    bounds it at the truncation with no horizon computed. Equivalence
    contract: equal to build_record_index_at_sql at any horizon strictly
    beyond every creation instant of the composed relation.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind whose index relation to build.

    Returns:
        A complete, deterministic SELECT producing RECORD_INDEX_COLUMNS.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
    """
    table_name = f"records__{kind}"
    sidecar.columns(table_name)  # raises TableNotFoundError if absent

    fp_lit = _sql_literal(fork_path)

    return (
        'SELECT DISTINCT "record_id", "record_index"'
        f' FROM "{table_name}"'
        f' WHERE "fork_path" = {fp_lit}'
    )
