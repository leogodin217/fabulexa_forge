"""The compare subsystem: dataset-equivalence verdicts over forge renders.

`compare_datasets()` decides whether an external dataset (DuckDB or CSV) is
exactly the relation an expected forge render (DuckDB) describes — a boolean
verdict plus a deterministic, bounded discrepancy report. It reads its two
inputs through its own in-memory DuckDB session; it never opens an emit, so
the reader-first rule (which governs `run.duckdb` + `base.json`) is not in
play.

`diff_manifest_row_counts()` is the surface's second mode: a per-table
row-count diff between two companion manifests (the previously published
render's and a fresh one's), where a table going to zero is the verdict and
every other movement is information.

See `docs/architecture/compare.md` for the semantic authority.
"""

from __future__ import annotations

from fabulexa_forge.compare.canonical import CanonicalFamily, encode_value, family_of
from fabulexa_forge.compare.engine import compare_datasets
from fabulexa_forge.compare.errors import CompareInputError
from fabulexa_forge.compare.manifests import (
    RowCountDelta,
    RowCountDiff,
    diff_manifest_row_counts,
    render_row_count_diff_json,
    render_row_count_diff_text,
)
from fabulexa_forge.compare.render import render_comparison_json, render_comparison_text
from fabulexa_forge.compare.report import (
    ComparisonResult,
    RowDiscrepancies,
    SchemaDiscrepancy,
    TableComparison,
)

__all__ = [
    "CanonicalFamily",
    "ComparisonResult",
    "CompareInputError",
    "RowCountDelta",
    "RowCountDiff",
    "RowDiscrepancies",
    "SchemaDiscrepancy",
    "TableComparison",
    "compare_datasets",
    "diff_manifest_row_counts",
    "encode_value",
    "family_of",
    "render_comparison_json",
    "render_comparison_text",
    "render_row_count_diff_json",
    "render_row_count_diff_text",
]
