"""Presentation-key derivation: the record-id-to-presentation-id join relation.

The derivations layer's next resident after record_index — its exact sibling:
build_presentation_key_at_sql bounds the relation to records created strictly
before a horizon; build_presentation_key_at_end_sql carries no horizon
predicate at all, so composing it over a truncated base relation bounds it at
the truncation with no horizon ever computed.

A join relation, not a reconstruction: it declares no ORDER BY, because a
consumer LEFT JOINs it rather than reading it ordered. The DISTINCT in both
builders is a no-op on a conformant emit; it exists so a row-duplicated
corrupted emit, whose duplicate row carries an identical (record_id,
presentation_id) pair, cannot fan a consumer's key join out. `presentation_id`
is projected verbatim — genesis-minted, never re-minted, so it is temporally
constant by the same argument as record_index. A NULL presentation_id (an
undeclared population's honest surface value) projects verbatim.

Layer-direction invariant: imports only the reader, fabulexa_forge._sql /
fabulexa_forge.errors, and stdlib. Never imports exporters.* or config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge._sql import _sql_literal
from fabulexa_forge.errors import ExportError

#: The canonical column list of the presentation-key relation, in emission order.
PRESENTATION_KEY_COLUMNS: tuple[str, ...] = ("record_id", "presentation_id")


def _require_presentation_id_column(
    sidecar: "Sidecar", table_name: str, kind: str
) -> None:
    """Assert `table_name` declares a `presentation_id` column.

    Args:
        sidecar: The open emit's sidecar.
        table_name: The records table name (records__<kind>).
        kind: The record kind, for the error message.

    Raises:
        TableNotFoundError: `table_name` is not in the sidecar.
        ExportError: `table_name` declares no `presentation_id` column — a
            caller gating error (the election gates make it unreachable from
            a gated plan).
    """
    columns = sidecar.columns(table_name)  # raises TableNotFoundError if absent
    if not any(col.name == "presentation_id" for col in columns):
        raise ExportError(
            f"records__{kind} declares no presentation_id column — a caller "
            "gating error (the election gates make it unreachable from a "
            "gated plan)"
        )


def build_presentation_key_at_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    horizon_ns: int,
) -> str:
    """Build the record-id-to-presentation-id relation for one kind at a horizon.

    The record-index derivation's exact sibling: one row per distinct
    (record_id, presentation_id) pair among the kind's records created
    strictly before the exclusive horizon, projecting PRESENTATION_KEY_COLUMNS
    — on a conformant emit, one row per record. The DISTINCT is a no-op under
    conformance; it exists so a row-duplicated corrupted emit, whose duplicate
    carries the identical pair, cannot fan a consumer's key join out.
    `presentation_id` is projected verbatim — genesis-minted, never re-minted,
    so it is a temporally-constant value read at a creation instant already
    bounded below the horizon; a NULL presentation_id (an undeclared
    population's honest surface value) projects verbatim too. Rows are
    filtered on creation time and to `fork_path`; `active` is never a
    predicate, so a record deactivated before the horizon is present and
    remains a resolvable reference target. A join relation, not a fold: it
    declares no ORDER BY, because a consumer LEFT JOINs it rather than
    reading it ordered.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind whose presentation-key relation to build; its
            table must carry a presentation_id column.
        horizon_ns: The exclusive horizon in sim-time ns; >= 0.

    Returns:
        A complete, deterministic SELECT producing PRESENTATION_KEY_COLUMNS.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
        ExportError: records__<kind> declares no presentation_id column — a
            caller gating error (the election gates make it unreachable from
            a gated plan).
    """
    table_name = f"records__{kind}"
    _require_presentation_id_column(sidecar, table_name, kind)

    fp_lit = _sql_literal(fork_path)

    return (
        'SELECT DISTINCT "record_id", "presentation_id"'
        f' FROM "{table_name}"'
        f' WHERE "fork_path" = {fp_lit}'
        f' AND "created_sim_time" < {horizon_ns}'
    )


def build_presentation_key_at_end_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
) -> str:
    """Build the record-id-to-presentation-id relation for one kind at the tape's end.

    The resident's second entry point: the same DISTINCT
    PRESENTATION_KEY_COLUMNS relation with no horizon — every record of the
    kind, filtered only to `fork_path`. "The tape's end" is structural: the
    SQL carries no horizon predicate, so composing this relation over a
    truncated base relation bounds it at the truncation with no horizon
    computed. Equivalence contract: equal to build_presentation_key_at_sql at
    any horizon strictly beyond every creation instant of the composed
    relation.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind whose presentation-key relation to build; its
            table must carry a presentation_id column.

    Returns:
        A complete, deterministic SELECT producing PRESENTATION_KEY_COLUMNS.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
        ExportError: records__<kind> declares no presentation_id column.
    """
    table_name = f"records__{kind}"
    _require_presentation_id_column(sidecar, table_name, kind)

    fp_lit = _sql_literal(fork_path)

    return (
        'SELECT DISTINCT "record_id", "presentation_id"'
        f' FROM "{table_name}"'
        f' WHERE "fork_path" = {fp_lit}'
    )
