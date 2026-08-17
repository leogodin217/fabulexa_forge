"""The compare surface's runtime report types.

Four frozen dataclasses carrying the outcome of one `compare_datasets` call:
`ComparisonResult` (the verdict, § the top-level object) wraps one
`TableComparison` per table in the union of both sides' table names, each
carrying its `SchemaDiscrepancy` entries and (when the table exists on both
sides) one `RowDiscrepancies`. See `docs/architecture/pending/dataset-
equivalence.md` § Interface Contracts for the semantic authority; this module
only defines the shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SchemaDiscrepancy:
    """One table- or column-level difference between the two sides."""

    kind: Literal[
        "table-missing",
        "table-extra",
        "column-missing",
        "column-extra",
        "column-incompatible",
    ]
    table: str
    column: str | None  # None for table-level kinds
    expected_type: str | None  # DuckDB type name; None where inapplicable
    actual_type: str | None


@dataclass(frozen=True)
class RowDiscrepancies:
    """The multiset difference for one compared table, canonically ordered."""

    columns: tuple[str, ...]  # compared-column set, expected-side catalog order
    missing: tuple[
        tuple[str | None, ...], ...
    ]  # encoded tuples in expected, absent/short in actual;
    extra: tuple[
        tuple[str | None, ...], ...
    ]  #   one entry per occurrence, truncated to max_row_diffs
    missing_total: int  # untruncated occurrence counts
    extra_total: int


@dataclass(frozen=True)
class TableComparison:
    """The full comparison outcome for one table name, drawn from the union
    of the expected- and actual-side table sets. A table-extra entry has no
    expected-side counterpart; a table-missing entry has no actual-side one.
    """

    table: str
    schema: tuple[SchemaDiscrepancy, ...]
    expected_rows: int | None  # None when the table is absent from the expected side
    actual_rows: int | None  # None when the table is absent from the actual side
    rows: RowDiscrepancies | None  # None when row comparison did not run


@dataclass(frozen=True)
class ComparisonResult:
    """The verdict and report for one dataset comparison.

    equal is True iff every TableComparison carries zero schema
    discrepancies and zero row discrepancies (missing_total == extra_total == 0).
    """

    equal: bool
    tables: tuple[TableComparison, ...]  # ordered by table name
