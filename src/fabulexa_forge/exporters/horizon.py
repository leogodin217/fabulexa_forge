"""Mode-neutral horizon windowing: the delivery-class vocabulary and the delta.

A window horizon is a slice end. A mode's windowed compile is its own
full-export compile run over the truncated tape at the window's two horizons
(`derivations.open_truncated_tape`), and each table is delivered by a class:

- `snapshot` — the end-horizon state, whole (DuckDB: replace the table).
- `upsert` — the rows of the end-horizon state absent from the start-horizon
  state, reconciled by a declared key (DuckDB: delete-by-key, then insert).
- `append` — the same delta, for a table whose rows are never revised
  (DuckDB: insert).

`compose_window_delta_sql` is the one delta composer every mode uses; the
per-mode classifiers live with their modes (`dimensional/windowing.py`,
`source/engine.py`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

from fabulexa_forge._sql import quote_identifier

WindowDelivery = Literal["append", "snapshot", "upsert"]
"""The static per-table window delivery class."""


def compose_window_delta_sql(
    end_sql: str,
    start_sql: str,
    output_columns: "Sequence[str]",
) -> str:
    """The delta of one table between two wrapped horizon compiles.

    `end EXCEPT ALL start` — the multiset of rows present at the end horizon
    and not at the start horizon, under distinct semantics (NULL equals
    NULL) — ordered by every output column in declared order, a total order
    over distinct rows that needs no internal column. Each input is already
    wrapped by its own name-shadowing CTE block and is treated as an opaque
    subquery; sibling subqueries' CTE scopes do not interact.

    Args:
        end_sql: The end-horizon compiled, wrapped query.
        start_sql: The start-horizon compiled, wrapped query.
        output_columns: The table's output column names in declared order.

    Returns:
        A complete, deterministic SELECT.
    """
    cols = ", ".join(quote_identifier(c) for c in output_columns)
    return (
        f"SELECT {cols} FROM ("
        f"SELECT {cols} FROM ({end_sql}) AS _end"
        f" EXCEPT ALL "
        f"SELECT {cols} FROM ({start_sql}) AS _start"
        f") AS _delta ORDER BY {cols}"
    )
