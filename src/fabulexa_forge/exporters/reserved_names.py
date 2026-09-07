"""Shared reserved-name constants + predicates for cross-mode output-name
reservations: incremental bookkeeping collisions, and the presentation-name
posture.

Both the dimensional exporter's `check_reserved_table_name` (in
`dimensional/validation.py`, always-on) and the source exporter's
`_check_reserved_names` (in `source/plan.py`) enforce that no author-resolved
output table name collides with the incremental writer's own bookkeeping
tables (`writers/duckdb.py`) — so a full export and a later incremental drip
on the same target agree. The reserved-name *set* is identical across modes;
only the shape of what is being checked (a dimensional `TableDecl` vs. a tuple
of resolved source `SourceTableSpec`s) differs, so each mode keeps its own
iteration and imports these predicates rather than sharing a single check
function (mirroring the mode-neutral home `exporters/query_spec.py`
establishes for `QuerySpec`).

`is_reserved_column_name` also carries the presentation-name posture:
`last_mutation_sim_time` is a sim-internal bookkeeping column — read it
freely (every value channel that reads it stays untouched), deliver it
under its own output name never. Unlike the incremental bookkeeping names,
this reservation applies in both export modes independent of incremental
export; each mode's own always-on enforcement site names the fix (a
presentation name — the source `updated_at` default, a dimensional `from:`
source).
"""

from __future__ import annotations

#: Bookkeeping table names reserved under incremental export.
RESERVED_TABLE_NAMES: frozenset[str] = frozenset({"_export_meta", "_export_windows"})

#: The presentation-name posture: a sim-internal column, read freely, never
#: delivered under its own output name (§ Affected Subsystems).
RESERVED_PRESENTATION_COLUMN_NAME = "last_mutation_sim_time"


def is_reserved_table_name(name: str) -> bool:
    """Whether `name` collides with a reserved incremental bookkeeping table name.

    Args:
        name: A resolved output table name.

    Returns:
        True iff `name` is `_export_meta` / `_export_windows`.
    """
    return name in RESERVED_TABLE_NAMES


def is_reserved_column_name(name: str) -> bool:
    """Whether `name` collides with a reserved output column name.

    The presentation-name posture: `last_mutation_sim_time` — a sim-internal
    column read freely, delivered under its own name never.

    Args:
        name: A resolved output column name.

    Returns:
        True iff `name` is `last_mutation_sim_time`.
    """
    return name == RESERVED_PRESENTATION_COLUMN_NAME
