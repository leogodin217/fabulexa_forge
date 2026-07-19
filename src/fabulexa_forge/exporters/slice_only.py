"""The mode-neutral `slice_only` export-policy predicates.

Mode-neutral sibling of `reserved_names.py` / `query_spec.py`: one shared
implementation of the discriminator carve-out and the policy-population
predicate, imported by every policing surface (dimensional refusal, source
omission, streaming refusal, `init`'s proposal skip) so the exemption stays
mechanical and identical everywhere — see docs/architecture/pending/
slice-only-policy.md § The discriminator carve-out / § Invariant 5.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.reader.sidecar import Sidecar

#: Prefix marking a records-category column as belonging to the policy
#: population (identity/lifecycle/membership/history columns are outside it).
_PROP_PREFIX = "prop__"


def is_exempt_discriminator(sidecar: "Sidecar", kind: str, column_name: str) -> bool:
    """The discriminator carve-out, applied identically on every surface.

    Args:
        sidecar: The open emit's sidecar (subtype_values is the oracle).
        kind: The records-category kind owning the column.
        column_name: Column name as declared (prop__ prefix included).

    Returns:
        True iff column_name == f"prop__{kind}_type" and
        sidecar.subtype_values(kind) is non-empty. Mechanical; the column's
        class is never consulted (exempt at any class).

    Raises:
        Nothing (subtype_values is total).
    """
    return column_name == f"{_PROP_PREFIX}{kind}_type" and bool(
        sidecar.subtype_values(kind)
    )


def is_non_exempt_slice_only(sidecar: "Sidecar", kind: str, column_name: str) -> bool:
    """The policy-population predicate every policing surface consults.

    Returns False without a class read when column_name lacks the prop__
    prefix (outside the population: identity/lifecycle/membership/history
    columns) or when is_exempt_discriminator is True — exemption
    short-circuits, so an exempt discriminator never triggers a class read.
    Otherwise reads sidecar.temporal_class(f"records__{kind}", column_name).

    Args:
        sidecar: The open emit's sidecar.
        kind: The records-category kind owning the column.
        column_name: Column name as declared.

    Returns:
        True iff the column's temporal_class is 'slice_only' and it is not
        the exempt discriminator.

    Raises:
        TemporalClassUnavailableError: Propagated from the reader —
            unverifiable is refused, never inferred.
        TableNotFoundError, ColumnNotFoundError: records__<kind> or the
            column is absent (callers establish existence first).
    """
    if not column_name.startswith(_PROP_PREFIX):
        return False
    if is_exempt_discriminator(sidecar, kind, column_name):
        return False
    return sidecar.temporal_class(f"records__{kind}", column_name) == "slice_only"


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
