"""The compare surface's renderers: text and JSON.

`render_comparison_text` produces the CLI's human-readable report; a verdict
line, then one line per equal table or one discrepancy block per table
carrying schema or row differences. `render_comparison_json` produces the
grading consumer's wire format — a byte-stable JSON mirror of
`ComparisonResult`'s dataclass shape (sorted keys, fixed separators; `null`
for `None`, arrays for tuples).

See `docs/architecture/pending/dataset-equivalence.md` § Interface Contracts
for the semantic authority; this module only formats what `ComparisonResult`
already carries.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING

from fabulexa_forge.compare.report import table_is_equal

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fabulexa_forge.compare.report import (
        ComparisonResult,
        SchemaDiscrepancy,
        TableComparison,
    )


def render_comparison_text(result: "ComparisonResult") -> str:
    """
    Render a ComparisonResult as the CLI's human-readable report.

    Args:
        result: The comparison to render.

    Returns:
        A deterministic multi-line report: one verdict line, then one block
        per table carrying discrepancies (equal tables render one line each).
    """
    lines = ["EQUAL" if result.equal else "NOT EQUAL"]
    for table_comparison in result.tables:
        if table_is_equal(table_comparison):
            lines.append(_render_equal_table_line(table_comparison))
        else:
            lines.extend(_render_table_block(table_comparison))
    return "\n".join(lines)


def render_comparison_json(result: "ComparisonResult") -> str:
    """
    Render a ComparisonResult as deterministic JSON for machine consumers.

    Args:
        result: The comparison to render.

    Returns:
        A JSON document mirroring the ComparisonResult shape byte-stably
        (sorted keys, fixed separators) — the grading consumer's wire format.
    """
    return json.dumps(asdict(result), sort_keys=True, separators=(",", ":"))


def _render_equal_table_line(table_comparison: "TableComparison") -> str:
    """One line for a table carrying zero discrepancies."""
    return f"  {table_comparison.table}: equal ({table_comparison.expected_rows} rows)"


def _render_table_block(table_comparison: "TableComparison") -> list[str]:
    """The multi-line discrepancy block for one table: its schema
    discrepancies, then (when row comparison ran) the row-count summary and
    the truncated missing/extra listings."""
    lines = [f"  {table_comparison.table}: NOT EQUAL"]
    for discrepancy in table_comparison.schema:
        lines.append(f"    {_render_schema_discrepancy(discrepancy)}")
    rows = table_comparison.rows
    if rows is not None:
        lines.append(
            f"    rows: expected={table_comparison.expected_rows}"
            f" actual={table_comparison.actual_rows}"
            f" missing_total={rows.missing_total} extra_total={rows.extra_total}"
        )
        if rows.missing:
            lines.append(f"      missing: {_render_row_tuples(rows.missing)}")
        if rows.extra:
            lines.append(f"      extra: {_render_row_tuples(rows.extra)}")
    return lines


def _render_schema_discrepancy(discrepancy: "SchemaDiscrepancy") -> str:
    """One `kind [column=...] [expected_type=...] [actual_type=...]` line."""
    parts: list[str] = [discrepancy.kind]
    if discrepancy.column is not None:
        parts.append(f"column={discrepancy.column}")
    if discrepancy.expected_type is not None:
        parts.append(f"expected_type={discrepancy.expected_type}")
    if discrepancy.actual_type is not None:
        parts.append(f"actual_type={discrepancy.actual_type}")
    return " ".join(parts)


def _render_row_tuples(tuples: "Sequence[tuple[str | None, ...]]") -> str:
    """A comma-separated listing of encoded row tuples."""
    return ", ".join(_render_row_tuple(row) for row in tuples)


def _render_row_tuple(row: tuple[str | None, ...]) -> str:
    """One encoded row tuple as `(val_a, val_b, ...)`, NULL for None entries."""
    return "(" + ", ".join("NULL" if value is None else value for value in row) + ")"
