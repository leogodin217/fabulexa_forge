"""Shared column-naming primitives for source-mode planning and rendering.

Both `plan.py` (classification / default column-set resolution) and
`renders.py` (the change-log render's fold-property set) need the same
`prop__<p>` scalar-property lookup over a records table; this sibling module
holds the one definition so neither file duplicates it (and neither imports
the other — plan.py and renders.py are independent leaves of the source
sub-package, both used by engine.py). It is also the one labeling authority
(`build_kind_label_expr`) both the junction render's `member__<f>__kind`
column and the event log's `<f>_kind` entry values render through.

Layer-direction invariant: imports only the reader (TYPE_CHECKING only),
`fabulexa_forge._sql` (SQL-literal escaping), and stdlib. Never imports other
exporters.*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge._sql import _sql_literal

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


def build_kind_label_expr(
    value_expr: str,
    labels: "tuple[tuple[str, str], ...]",
) -> str:
    """The label-rendered SQL expression for one kind-name-valued expression.

    A compile-time CASE over the declared (kind, label) pairs with identity
    fall-through: a value matching no pair — an unlabeled kind, or a
    corrupted emit's mutated cell — renders verbatim, and NULL stays NULL
    (`NULL = <literal>` is NULL, never TRUE, so the CASE falls through to the
    ELSE `value_expr`). Byte-identical passthrough (`value_expr` unchanged)
    when `labels` is empty, mirroring the no-join composition rule for
    default elections.

    The one labeling authority for both call sites: the junction render's
    projected `member__<f>__kind` column, and the event log's `<f>_kind`
    entry values (old and new halves) inside the `changes` JSON assembly.

    Args:
        value_expr: A VARCHAR-typed SQL expression carrying a kind name —
            the junction's qualified `member__<f>__kind`, or the fold's
            `<f>_kind` after-image value expression.
        labels: The resolved (kind, label) pairs, declaration order.

    Returns:
        A VARCHAR-typed SQL expression.
    """
    if not labels:
        return value_expr
    whens = " ".join(
        f"WHEN {value_expr} = {_sql_literal(kind)} THEN {_sql_literal(label)}"
        for kind, label in labels
    )
    return f"CASE {whens} ELSE {value_expr} END"
