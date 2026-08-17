#!/usr/bin/env python
"""
Demo: The comparison engine — compare_datasets end-to-end
Sprint: dataset-equivalence
Phase: 2

`compare_datasets` decides whether an actual dataset (DuckDB or CSV) is
exactly the relation an expected forge render (DuckDB) describes. This phase
wires Phase 1's canonical-form seam into the full engine: input loading (the
UTC-pinned session, the CSV directory scan + per-family typing casts), table
and column matching, multiset row comparison, and the deterministic
ComparisonResult.

Shows, over one small two-table expected DuckDB file:
  (a) An actual DuckDB copy with scrambled row and column order and a
      narrower integer type -> equal=True (physical drift, not semantic
      drift, is absorbed).
  (b) A CSV directory export of the same relation -> equal=True (CSV typing
      casts every cell toward the expected column's reference type).
  (c) A mutated actual — a dropped table, an extra column, one changed
      value, one duplicated row — -> equal=False, with the discrepancy
      report showing each kind (table-missing, column-extra, and the row
      multiset diff).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge.compare import ComparisonResult, compare_datasets


def _fail(message: str) -> SystemExit:
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def _build_expected(path: Path) -> None:
    """A small two-table expected render: people and events."""
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE people (id BIGINT, name VARCHAR, score DOUBLE)")
    con.execute(
        "INSERT INTO people VALUES "
        "(1, 'Ada', 9.5), (2, 'Grace', 8.25), (3, 'Alan', 7.0)"
    )
    con.execute(
        "CREATE TABLE events (id BIGINT, person_id BIGINT, happened_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO events VALUES "
        "(1, 1, TIMESTAMP '2024-01-01 09:00:00'), "
        "(2, 2, TIMESTAMP '2024-01-02 10:30:00')"
    )
    con.close()


def _build_scrambled_actual(path: Path) -> None:
    """The same relation: column order scrambled, rows scrambled, people.id
    narrowed to INTEGER — lossless physical drift the family table absorbs.
    """
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE people (score DOUBLE, name VARCHAR, id INTEGER)")
    con.execute(
        "INSERT INTO people VALUES "
        "(7.0, 'Alan', 3), (9.5, 'Ada', 1), (8.25, 'Grace', 2)"
    )
    con.execute(
        "CREATE TABLE events (happened_at TIMESTAMP, id BIGINT, person_id BIGINT)"
    )
    con.execute(
        "INSERT INTO events VALUES "
        "(TIMESTAMP '2024-01-02 10:30:00', 2, 2), "
        "(TIMESTAMP '2024-01-01 09:00:00', 1, 1)"
    )
    con.close()


def _build_csv_actual(directory: Path) -> None:
    """A CSV directory export of the same relation, forge's own writer form."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "people.csv").write_text(
        "id,name,score\n1,Ada,9.5\n2,Grace,8.25\n3,Alan,7.0\n"
    )
    (directory / "events.csv").write_text(
        "id,person_id,happened_at\n"
        "1,1,2024-01-01 09:00:00.000000\n"
        "2,2,2024-01-02 10:30:00.000000\n"
    )


def _build_mutated_actual(path: Path) -> None:
    """A dropped table (events), an extra column and a changed value on
    people, and one duplicated row — one instance of every discrepancy kind
    this demo can show without a schema-error input.
    """
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE people (id BIGINT, name VARCHAR, score DOUBLE, dept VARCHAR)"
    )
    con.execute(
        "INSERT INTO people VALUES "
        "(1, 'Ada', 9.5, 'math'), "
        "(2, 'Grace', 8.25, 'math'), "
        "(2, 'Grace', 8.25, 'math'), "  # duplicated row
        "(3, 'Alan', 6.5, 'math')"  # changed value: score 7.0 -> 6.5
    )
    con.close()


def _print_report(result: ComparisonResult) -> None:
    print(f"  equal={result.equal}")
    for table_comparison in result.tables:
        print(f"  table {table_comparison.table!r}:")
        for discrepancy in table_comparison.schema:
            print(f"    schema: {discrepancy}")
        rows = table_comparison.rows
        if rows is not None and (rows.missing_total or rows.extra_total):
            print(f"    rows: {rows}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        expected_path = root / "expected.duckdb"
        _build_expected(expected_path)

        print("(a) Scrambled row/column order + narrowed integer type:")
        scrambled_path = root / "actual_scrambled.duckdb"
        _build_scrambled_actual(scrambled_path)
        result_a = compare_datasets(expected_path, scrambled_path)
        print(f"  equal={result_a.equal}")
        if not result_a.equal:
            raise _fail(f"expected equal=True for the scrambled copy, got {result_a}")
        print()

        print("(b) CSV directory export of the same relation:")
        csv_dir = root / "actual_csv"
        _build_csv_actual(csv_dir)
        result_b = compare_datasets(expected_path, csv_dir)
        print(f"  equal={result_b.equal}")
        if not result_b.equal:
            raise _fail(f"expected equal=True for the CSV export, got {result_b}")
        print()

        print(
            "(c) Mutated actual (dropped table, extra column, changed value, "
            "duplicated row):"
        )
        mutated_path = root / "actual_mutated.duckdb"
        _build_mutated_actual(mutated_path)
        result_c = compare_datasets(expected_path, mutated_path)
        _print_report(result_c)
        if result_c.equal:
            raise _fail("expected equal=False for the mutated actual")
        kinds = {d.kind for tc in result_c.tables for d in tc.schema}
        if "table-missing" not in kinds or "column-extra" not in kinds:
            raise _fail(f"expected table-missing and column-extra, got kinds={kinds}")
        people = next(tc for tc in result_c.tables if tc.table == "people")
        assert people.rows is not None
        if people.rows.missing_total == 0 or people.rows.extra_total == 0:
            raise _fail(f"expected a row multiset diff on people, got {people.rows}")
        print()

    print(
        "SUCCESS: compare_datasets absorbs row/column reordering and lossless "
        "type-width drift, agrees across DuckDB and CSV actual sides, and "
        "reports a table-missing / column-extra / row-multiset discrepancy "
        "for a genuinely mutated actual"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
