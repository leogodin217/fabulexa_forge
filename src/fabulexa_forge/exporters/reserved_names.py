"""Shared reserved-name constants + predicates for cross-mode incremental
bookkeeping name collisions.

Both the dimensional exporter's `check_incremental_reserved_names` (in
`dimensional/validation.py`) and the source exporter's `_check_reserved_names`
(in `source/plan.py`) enforce that no author-resolved output table or column
name collides with the incremental writer's own bookkeeping names/columns
(`writers/duckdb.py`) — so a full export and a later incremental drip on the
same target agree. The reserved-name *set* is identical across modes; only the
shape of what is being checked (a dimensional `TableDecl` vs. a tuple of
resolved source `SourceTableSpec`s) differs, so each mode keeps its own
iteration and imports these predicates rather than sharing a single check
function (mirroring the mode-neutral home `exporters/query_spec.py`
establishes for `QuerySpec`).
"""

from __future__ import annotations

#: Bookkeeping table names reserved under incremental export.
RESERVED_TABLE_NAMES: frozenset[str] = frozenset({"_export_meta", "_export_windows"})

#: Bookkeeping table-name suffix reserved under incremental export.
RESERVED_TABLE_SUFFIX = "__rows"

#: Bookkeeping column name reserved under incremental export.
RESERVED_COLUMN_NAME = "__valid_from_ns"


def is_reserved_table_name(name: str) -> bool:
    """Whether `name` collides with a reserved incremental bookkeeping table name.

    Args:
        name: A resolved output table name.

    Returns:
        True iff `name` is `_export_meta` / `_export_windows`, or ends in `__rows`.
    """
    return name in RESERVED_TABLE_NAMES or name.endswith(RESERVED_TABLE_SUFFIX)


def is_reserved_column_name(name: str) -> bool:
    """Whether `name` collides with the reserved incremental bookkeeping column name.

    Args:
        name: A resolved output column name.

    Returns:
        True iff `name` is `__valid_from_ns`.
    """
    return name == RESERVED_COLUMN_NAME
