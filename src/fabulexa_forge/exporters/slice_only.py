"""The mode-neutral `slice_only` export-policy predicates.

Mode-neutral sibling of `reserved_names.py` / `query_spec.py`: re-exports the
reader-layer discriminator carve-out (`is_exempt_discriminator`) and
policy-population predicate (`is_non_exempt_slice_only`) — one shared
implementation, since the derivations truncated-tape surface needs the same
predicates but cannot import exporters.* under the layer-direction invariant
— and adds the export-message renderer `slice_only_refusal_message`. Every
policing surface (dimensional refusal, source omission, streaming refusal,
`init`'s proposal skip) imports from here — see docs/architecture/pending/
slice-only-policy.md § The discriminator carve-out / § Invariant 5.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fabulexa_forge.reader.slice_only import (
    is_exempt_discriminator,
    is_non_exempt_slice_only,
)

if TYPE_CHECKING:
    from fabulexa_forge.reader.sidecar import Sidecar

__all__ = [
    "is_exempt_discriminator",
    "is_non_exempt_slice_only",
    "omitted_slice_only_columns",
    "slice_only_refusal_message",
]


def omitted_slice_only_columns(sidecar: "Sidecar", kind: str) -> tuple[str, ...]:
    """The unit-invariant omitted set for one records kind.

    Every non-exempt temporal_class: slice_only prop__ column of
    records__<kind>, in sidecar column-declaration order. Shared by source's
    per-unit omission and base's per-kind omission — the same predicate, the
    same order.

    Args:
        sidecar: The open emit's sidecar.
        kind: The record kind owning the records__<kind> table.

    Returns:
        Omitted column names (prop__ prefix included), sidecar column order.

    Raises:
        TemporalClassUnavailableError: Propagated.
    """
    source_table = f"records__{kind}"
    return tuple(
        col.name
        for col in sidecar.columns(source_table)
        if is_non_exempt_slice_only(sidecar, kind, col.name)
    )


def slice_only_refusal_message(
    table_name: str,
    output_column: str,
    surface: str,
    kind: str,
    column_name: str,
) -> str:
    """Render the SliceOnlyColumnRefused message shared by every dimensional check.

    Names the output table.column, the base records table.column, the class,
    and states the slice-fact contract clause. `surface` distinguishes the
    read kind in the rendered sentence ("column", "filter key", "fk hop
    column", "as_of column").

    Args:
        table_name: The output table declaration's name.
        output_column: The output column (or filter key) naming the read.
        surface: The human-readable surface label preceding the quoted name.
        kind: The records-category kind owning the offending column.
        column_name: The offending base-layer column name.

    Returns:
        The fully rendered ExportError message text.
    """
    return (
        f"table '{table_name}': {surface} '{output_column}' reads"
        f" 'records__{kind}.{column_name}' which is temporal_class: slice_only;"
        " its value is known only at the emit's slice and cannot be presented"
        " as an as-of value"
    )
