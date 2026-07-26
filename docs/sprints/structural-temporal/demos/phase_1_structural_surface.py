#!/usr/bin/env python
"""
Demo: The reader's structural-temporal surface + sidecar category gate.

Sprint: structural-temporal
Phase: 1

Prints, for each of the contract's three table categories, the structural
columns that carry a sim-time instant; prints the mutability answer for
every records structural column; demonstrates loudness for four
out-of-domain questions; and writes a minimal emit whose sidecar carries an
out-of-set table `category`, showing `open_emit` refuse it with
`SidecarStructureError` rather than opening and deferring to conformance.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.reader import SidecarStructureError, open_emit
from fabulexa_forge.reader.records_columns import (
    records_structural_column_is_mutable,
    structural_instant_columns,
)

_TABLE_CATEGORIES = ("records", "fixed", "membership")

_RECORDS_STRUCTURAL_COLUMNS = (
    "created_sim_time",
    "active",
    "deactivated_at",
    "last_mutation_sim_time",
    "fork_path",
    "record_id",
    "record_index",
    "presentation_id",
)

_LOUD_MUTABILITY_QUESTIONS = (
    "prop__status",
    "ref_index__owner",
    "sim_time",
)


def print_instant_mappings() -> None:
    """Print structural_instant_columns for each contract table category."""
    print("== structural_instant_columns per category ==")
    for category in _TABLE_CATEGORIES:
        mapping = structural_instant_columns(category)
        print(f"  {category}:")
        for column, instant in mapping.items():
            print(f"    {column} -> {instant}")


def print_mutability_table() -> None:
    """Print the mutability answer for every records structural column."""
    print("== records_structural_column_is_mutable per column ==")
    for column in _RECORDS_STRUCTURAL_COLUMNS:
        mutable = records_structural_column_is_mutable(column)
        print(f"  {column}: {'mutable' if mutable else 'set-once'}")


def demonstrate_loudness() -> None:
    """Show ValueError for an unknown category, a prop__, a ref_index__, and
    an unpinned name — none of these silently answer."""
    print("== loudness ==")
    try:
        structural_instant_columns("bogus")
    except ValueError as exc:
        print(f"  structural_instant_columns('bogus') -> ValueError: {exc}")

    for column in _LOUD_MUTABILITY_QUESTIONS:
        try:
            records_structural_column_is_mutable(column)
        except ValueError as exc:
            print(f"  records_structural_column_is_mutable({column!r}):")
            print(f"    -> ValueError: {exc}")


def _write_bogus_category_emit(emit_dir: Path) -> None:
    """Write a minimal emit whose sidecar carries a table `category: 'bogus'`."""
    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [
            {
                "name": "firings",
                "category": "bogus",
                "columns": [{"name": "fork_path", "type": "VARCHAR"}],
                "rows": 0,
            }
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.close()


def demonstrate_category_refusal(emit_dir: Path) -> bool:
    """Show open_emit refusing an out-of-set sidecar category.

    Returns:
        True iff open_emit raised SidecarStructureError, as expected.
    """
    print("== sidecar category gate ==")
    _write_bogus_category_emit(emit_dir)
    try:
        open_emit(emit_dir)
    except SidecarStructureError as exc:
        print(f"  open_emit refused: SidecarStructureError: {exc}")
        return True
    print("  UNEXPECTED: open_emit did not refuse the out-of-set category")
    return False


def main() -> int:
    print_instant_mappings()
    print_mutability_table()
    demonstrate_loudness()

    with tempfile.TemporaryDirectory() as tmp:
        refused = demonstrate_category_refusal(Path(tmp))

    if not refused:
        print("FAILURE: category gate did not refuse a bogus category")
        return 1

    print("SUCCESS: structural-temporal surface + category gate demonstrated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
