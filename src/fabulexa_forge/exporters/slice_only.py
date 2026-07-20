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

from fabulexa_forge.reader.slice_only import (
    is_exempt_discriminator,
    is_non_exempt_slice_only,
)

__all__ = [
    "is_exempt_discriminator",
    "is_non_exempt_slice_only",
    "slice_only_refusal_message",
]


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
