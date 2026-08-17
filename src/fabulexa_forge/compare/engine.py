"""The compare surface's comparison engine: `compare_datasets`.

Orchestrates the whole comparison: input validation and loading (via
`inputs.py`), table matching over the comparison universe, column matching
plus family compatibility, multiset row comparison with canonical ordering
and truncation, and the deterministic `ComparisonResult` assembly.

See `docs/architecture/pending/dataset-equivalence.md` § Semantics for the
semantic authority (table matching, column matching and type compatibility,
row comparison, the verdict, determinism).
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import duckdb as _duckdb

from fabulexa_forge.compare.canonical import CanonicalFamily, encode_value, family_of
from fabulexa_forge.compare.errors import CompareInputError
from fabulexa_forge.compare.inputs import (
    ActualSide,
    CsvSide,
    CsvTable,
    attach_expected,
    csv_column_values,
    list_columns,
    list_tables,
    open_compare_session,
    quote_identifier,
    resolve_actual,
)
from fabulexa_forge.compare.report import (
    ComparisonResult,
    RowDiscrepancies,
    SchemaDiscrepancy,
    TableComparison,
)

_SCHEMA_KIND_ORDER = (
    "table-missing",
    "table-extra",
    "column-missing",
    "column-extra",
    "column-incompatible",
)


def compare_datasets(
    expected: "Path",
    actual: "Path",
    *,
    tables: "Sequence[str] | None" = None,
    max_row_diffs: int = 10,
) -> ComparisonResult:
    """
    Compare two materialized datasets for exact equality under the canonical form.

    Args:
        expected: Path to a DuckDB file — the authoritative side (a forge
            render). Defines the table universe, column sets, and types.
        actual: Path to a DuckDB file, or to a directory of <table>.csv files
            with header rows, claiming to be the same reshape.
        tables: Optional narrowing of the comparison universe to exactly
            these tables, on both sides — expected- and actual-side tables
            outside the selection are ignored entirely (an unselected
            actual-side table is not table-extra). None compares every
            expected-side table against the full actual side; an empty
            selection is refused.
        max_row_diffs: Per-table, per-direction cap on *listed* row
            discrepancies. Bounds the report only; totals and the verdict are
            computed over full tables.

    Returns:
        A ComparisonResult; deterministic for identical inputs.

    Raises:
        CompareInputError: expected is not a readable DuckDB file; actual is
            neither a DuckDB file nor a CSV directory; a CSV file lacks a
            header; a `tables` entry names no expected-side table; `tables`
            is empty; an expected-side column's type within the comparison
            universe is outside the canonical families; max_row_diffs < 0.
    """
    if max_row_diffs < 0:
        raise CompareInputError("max_row_diffs must be >= 0")

    conn = open_compare_session()
    attach_expected(conn, expected)
    actual_side = resolve_actual(conn, actual)

    expected_tables = list_tables(conn, "expected_db")
    actual_tables = _actual_table_names(conn, actual_side)

    universe = _resolve_universe(expected_tables, actual_tables, tables)
    _validate_family_coverage(conn, universe, set(expected_tables))

    table_comparisons = tuple(
        _compare_table(
            conn,
            name,
            set(expected_tables),
            set(actual_tables),
            actual_side,
            max_row_diffs,
        )
        for name in universe
    )
    equal = all(_table_is_equal(tc) for tc in table_comparisons)
    return ComparisonResult(equal=equal, tables=table_comparisons)


def _table_is_equal(table_comparison: TableComparison) -> bool:
    """A single table carries zero schema discrepancies and (if row
    comparison ran) zero row discrepancies of either direction."""
    if table_comparison.schema:
        return False
    rows = table_comparison.rows
    return rows is None or (rows.missing_total == 0 and rows.extra_total == 0)


def _resolve_universe(
    expected_tables: "Sequence[str]",
    actual_tables: "Sequence[str]",
    tables: "Sequence[str] | None",
) -> tuple[str, ...]:
    """The working, sorted table-name set: every name classified and reported.

    `tables=None`: `union(expected, actual)` — the default "compare
    everything" scope, under which an actual-only table surfaces as
    table-extra. `tables=[...]`: exactly that (validated-against-expected)
    set, narrowing both sides at once — a table outside the selection is
    invisible to the whole comparison on either side, which is what makes
    narrowing the mechanism for tolerating extras.

    Raises:
        CompareInputError: `tables` is an empty sequence, or names an entry
            absent from the expected-side catalog.
    """
    if tables is None:
        return tuple(sorted(set(expected_tables) | set(actual_tables)))
    selected = tuple(tables)
    if not selected:
        raise CompareInputError("tables selection must not be empty")
    unknown = sorted(set(selected) - set(expected_tables))
    if unknown:
        raise CompareInputError(
            f"tables selection names unknown table(s): {', '.join(unknown)}"
        )
    return tuple(sorted(set(selected)))


def _validate_family_coverage(
    conn: "_duckdb.DuckDBPyConnection",
    universe: "Sequence[str]",
    expected_tables: set[str],
) -> None:
    """Every expected-side column type within the universe must map to a
    canonical family — the comparison's whole-input scope boundary.

    Raises:
        CompareInputError: An expected-side column, in a table within the
            universe, has a type outside every canonical family.
    """
    for table in universe:
        if table not in expected_tables:
            continue
        for column_name, duckdb_type in list_columns(conn, "expected_db", table):
            if family_of(duckdb_type) is None:
                raise CompareInputError(
                    f"expected column {table}.{column_name} has unsupported type "
                    f"{duckdb_type}"
                )


def _actual_table_names(
    conn: "_duckdb.DuckDBPyConnection", actual_side: ActualSide
) -> tuple[str, ...]:
    """The actual side's table names, whichever source it is."""
    if isinstance(actual_side, CsvSide):
        return tuple(actual_side.tables.keys())
    return list_tables(conn, actual_side.alias)


def _compare_table(
    conn: "_duckdb.DuckDBPyConnection",
    table: str,
    expected_tables: set[str],
    actual_tables: set[str],
    actual_side: ActualSide,
    max_row_diffs: int,
) -> TableComparison:
    """The full comparison outcome for one table name in the universe."""
    in_expected = table in expected_tables
    in_actual = table in actual_tables

    if not in_expected:
        actual_rows = _actual_row_count(conn, actual_side, table)
        return TableComparison(
            table=table,
            schema=(SchemaDiscrepancy("table-extra", table, None, None, None),),
            expected_rows=None,
            actual_rows=actual_rows,
            rows=None,
        )

    expected_table_ref = f"expected_db.main.{quote_identifier(table)}"
    expected_columns = list_columns(conn, "expected_db", table)
    if not in_actual:
        expected_rows = _row_count(conn, expected_table_ref)
        return TableComparison(
            table=table,
            schema=(SchemaDiscrepancy("table-missing", table, None, None, None),),
            expected_rows=expected_rows,
            actual_rows=None,
            rows=None,
        )

    actual_columns = _actual_columns(conn, actual_side, table)
    schema, compared = _match_columns(table, expected_columns, actual_columns)

    expected_rows = _row_count(conn, expected_table_ref)
    actual_rows = _actual_row_count(conn, actual_side, table)

    expected_row_tuples = _materialize_encoded_rows(conn, expected_table_ref, compared)
    actual_row_tuples = _materialize_actual_rows(conn, actual_side, table, compared)
    rows = _row_discrepancies(
        expected_row_tuples,
        actual_row_tuples,
        tuple(name for name, _ in compared),
        max_row_diffs,
    )
    return TableComparison(
        table=table,
        schema=tuple(sorted(schema, key=_schema_sort_key)),
        expected_rows=expected_rows,
        actual_rows=actual_rows,
        rows=rows,
    )


def _schema_sort_key(discrepancy: SchemaDiscrepancy) -> tuple[int, str]:
    """(kind declaration order, column name) — the within-table discrepancy order."""
    return (_SCHEMA_KIND_ORDER.index(discrepancy.kind), discrepancy.column or "")


def _actual_columns(
    conn: "_duckdb.DuckDBPyConnection", actual_side: ActualSide, table: str
) -> tuple[tuple[str, str | None], ...]:
    """The actual side's columns for one table: `(name, duckdb_type)` for a
    DuckDB source, `(name, None)` for a CSV source (no declared type — every
    present-both column is cast per-value, never schema-incompatible)."""
    if isinstance(actual_side, CsvSide):
        return tuple((name, None) for name in actual_side.tables[table].columns)
    return tuple(
        (name, duckdb_type)
        for name, duckdb_type in list_columns(conn, actual_side.alias, table)
    )


def _match_columns(
    table: str,
    expected_columns: "Sequence[tuple[str, str]]",
    actual_columns: "Sequence[tuple[str, str | None]]",
) -> tuple[list[SchemaDiscrepancy], list[tuple[str, CanonicalFamily]]]:
    """Match expected and actual columns by name; classify each expected
    column and collect the compared-column set (compatible-family matches).

    Returns:
        `(schema_discrepancies, compared_columns)` — `compared_columns` in
        expected-side catalog order.
    """
    actual_types = dict(actual_columns)
    schema: list[SchemaDiscrepancy] = []
    compared: list[tuple[str, CanonicalFamily]] = []
    for name, expected_type in expected_columns:
        family = family_of(expected_type)
        assert family is not None  # already validated by _validate_family_coverage
        if name not in actual_types:
            schema.append(
                SchemaDiscrepancy("column-missing", table, name, expected_type, None)
            )
            continue
        actual_type = actual_types[name]
        if actual_type is not None and family_of(actual_type) != family:
            schema.append(
                SchemaDiscrepancy(
                    "column-incompatible", table, name, expected_type, actual_type
                )
            )
            continue
        compared.append((name, family))
    expected_names = {name for name, _ in expected_columns}
    for name in sorted(set(actual_types) - expected_names):
        schema.append(
            SchemaDiscrepancy("column-extra", table, name, None, actual_types[name])
        )
    return schema, compared


def _row_count(conn: "_duckdb.DuckDBPyConnection", table_ref: str) -> int:
    """`COUNT(*)` over one already-qualified table reference."""
    row = conn.execute(f"SELECT COUNT(*) FROM {table_ref}").fetchone()
    assert row is not None
    return cast(int, row[0])


def _actual_row_count(
    conn: "_duckdb.DuckDBPyConnection", actual_side: ActualSide, table: str
) -> int:
    """The actual side's row count for one table, whichever source it is."""
    if isinstance(actual_side, CsvSide):
        return _row_count(conn, actual_side.tables[table].view_name)
    return _row_count(conn, f"{actual_side.alias}.main.{quote_identifier(table)}")


def _materialize_encoded_rows(
    conn: "_duckdb.DuckDBPyConnection",
    table_ref: str,
    columns: "Sequence[tuple[str, CanonicalFamily]]",
) -> list[tuple[str | None, ...]]:
    """Materialize one DuckDB-sourced table's compared columns as canonically
    encoded row tuples.

    An empty compared-column set degenerates to a row-count check: every row
    encodes as the empty tuple.
    """
    if not columns:
        return [() for _ in range(_row_count(conn, table_ref))]
    column_list = ", ".join(quote_identifier(name) for name, _ in columns)
    arrow_table = conn.execute(
        f"SELECT {column_list} FROM {table_ref}"
    ).fetch_arrow_table()
    families = [family for _, family in columns]
    arrow_columns = [arrow_table.column(i) for i in range(len(columns))]
    rows: list[tuple[str | None, ...]] = []
    for i in range(arrow_table.num_rows):
        rows.append(
            tuple(
                encode_value(arrow_columns[j][i].as_py(), families[j])
                for j in range(len(columns))
            )
        )
    return rows


def _materialize_actual_rows(
    conn: "_duckdb.DuckDBPyConnection",
    actual_side: ActualSide,
    table: str,
    columns: "Sequence[tuple[str, CanonicalFamily]]",
) -> list[tuple[str | None, ...]]:
    """Materialize the actual side's compared columns as canonically encoded
    row tuples, whichever source it is."""
    if isinstance(actual_side, CsvSide):
        csv_table: CsvTable = actual_side.tables[table]
        if not columns:
            return [() for _ in range(_row_count(conn, csv_table.view_name))]
        return csv_column_values(conn, csv_table, columns)
    table_ref = f"{actual_side.alias}.main.{quote_identifier(table)}"
    return _materialize_encoded_rows(conn, table_ref, columns)


def _tuple_sort_key(
    row: tuple[str | None, ...],
) -> tuple[tuple[int, str], ...]:
    """Elementwise canonical sort key: NULL sorts before every encoded string."""
    return tuple((0, "") if value is None else (1, value) for value in row)


def _row_discrepancies(
    expected_rows: "Sequence[tuple[str | None, ...]]",
    actual_rows: "Sequence[tuple[str | None, ...]]",
    columns: tuple[str, ...],
    max_row_diffs: int,
) -> RowDiscrepancies:
    """The multiset difference between two tables' encoded row tuples,
    canonically ordered and truncated per direction."""
    expected_counts = Counter(expected_rows)
    actual_counts = Counter(actual_rows)
    missing: list[tuple[str | None, ...]] = []
    extra: list[tuple[str | None, ...]] = []
    missing_total = 0
    extra_total = 0
    all_tuples = sorted(set(expected_counts) | set(actual_counts), key=_tuple_sort_key)
    for row in all_tuples:
        expected_count = expected_counts.get(row, 0)
        actual_count = actual_counts.get(row, 0)
        if expected_count > actual_count:
            deficit = expected_count - actual_count
            missing_total += deficit
            missing.extend([row] * deficit)
        elif actual_count > expected_count:
            surplus = actual_count - expected_count
            extra_total += surplus
            extra.extend([row] * surplus)
    return RowDiscrepancies(
        columns=columns,
        missing=tuple(missing[:max_row_diffs]),
        extra=tuple(extra[:max_row_diffs]),
        missing_total=missing_total,
        extra_total=extra_total,
    )
