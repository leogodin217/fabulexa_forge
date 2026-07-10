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
