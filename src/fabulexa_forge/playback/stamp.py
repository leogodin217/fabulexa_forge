"""The spine discriminator lookup: shared by the event stream and snapshots.

Verbatim record_id -> discriminator-cell mapping used to stamp a record's
sub_type (event after-images, snapshot record_state) or a membership row's
owner_sub_type (event after-images, snapshot membership_state). One query
per kind, reused across every atom keyed off it.

Layer-direction invariant: imports only the reader, fabulexa_forge._sql, and
stdlib. Never imports exporters.* or config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fabulexa_forge._sql import _sql_literal

if TYPE_CHECKING:
    from fabulexa_forge.reader.emit import Emit


def spine_discriminator_index(
    emit: "Emit",
    fork_path: str,
    kind: str,
    is_subtyped: bool,
    discriminator_declared: bool,
) -> dict[str, str | None]:
    """Verbatim record_id -> discriminator-cell mapping for atom stamping.

    Empty when the kind is not sub-typed (`Sidecar.subtype_values(kind)` is
    empty) or its discriminator column is undeclared (a drifted tape) —
    `.get`'s default then reads None for every record, uniformly covering
    the not-sub-typed case, the drifted-tape case, and (via a membership
    owner lookup against a record index) a corrupted-tape orphan owner with
    no spine row. When present, values are read verbatim — a NULL cell
    yields None, an out-of-domain value is carried unchanged (the stamp is
    data; the declared domain is only the selection vocabulary).

    Args:
        emit: The open emit.
        fork_path: The sole branch's fork_path (from require_single_branch).
        kind: The record kind (a record atom's own kind, or a membership
            atom's owner kind).
        is_subtyped: Whether Sidecar.subtype_values(kind) is non-empty.
        discriminator_declared: Whether prop__<kind>_type is a declared
            column on records__<kind>.

    Returns:
        record_id -> the verbatim discriminator cell value, or None for a
        NULL cell; empty when is_subtyped or discriminator_declared is False.
    """
    if not is_subtyped or not discriminator_declared:
        return {}

    table_name = f"records__{kind}"
    column_name = f"prop__{kind}_type"
    sql = (
        f'SELECT "record_id", "{column_name}" FROM "{table_name}"'
        f' WHERE "fork_path" = {_sql_literal(fork_path)}'
    )
    rows = emit.query(sql, ())
    return {
        str(record_id): (None if value is None else str(value))
        for record_id, value in rows
    }
