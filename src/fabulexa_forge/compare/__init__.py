"""The compare subsystem: dataset-equivalence verdicts over forge renders.

`compare_datasets()` decides whether an external dataset (DuckDB or CSV) is
exactly the relation an expected forge render (DuckDB) describes — a boolean
verdict plus a deterministic, bounded discrepancy report. It reads its two
inputs through its own in-memory DuckDB session; it never opens an emit, so
the reader-first rule (which governs `run.duckdb` + `base.json`) is not in
play. Public exports grow per phase; Phase 1 exposes the canonical-form
authority and the report/error types.

See `docs/architecture/pending/dataset-equivalence.md` for the semantic
authority.
"""

from __future__ import annotations

from fabulexa_forge.compare.canonical import CanonicalFamily, encode_value, family_of
from fabulexa_forge.compare.errors import CompareInputError
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
    "RowDiscrepancies",
    "SchemaDiscrepancy",
    "TableComparison",
    "encode_value",
    "family_of",
]
