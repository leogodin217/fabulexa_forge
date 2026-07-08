"""Shared column-naming primitives for source-mode planning and rendering.

Both `plan.py` (classification / default column-set resolution) and
`renders.py` (the change-log render's fold-property set) need the same
`prop__<p>` scalar-property lookup over a records table; this sibling module
holds the one definition so neither file duplicates it (and neither imports
the other — plan.py and renders.py are independent leaves of the source
sub-package, both used by engine.py).

Layer-direction invariant: imports only the reader (TYPE_CHECKING only) and
stdlib. Never imports other exporters.*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_export.reader.sidecar import Sidecar

#: Prefix marking a records-table scalar-property column.
_PROP_PREFIX = "prop__"


def _scalar_properties(sidecar: "Sidecar", records_table: str) -> frozenset[str]:
    """The kind's full scalar (`prop__<p>`) property set, bare names.

    Args:
        sidecar: The open emit's sidecar.
        records_table: The kind's `records__<kind>` table name.

    Returns:
        Bare property names for every `prop__` column on the table.
    """
    return frozenset(
        col.name[len(_PROP_PREFIX) :]
        for col in sidecar.columns(records_table)
        if col.name.startswith(_PROP_PREFIX)
    )
