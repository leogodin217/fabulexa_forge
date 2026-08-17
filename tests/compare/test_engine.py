"""Tests for the compare surface's comparison engine: table matching, column
matching and family compatibility, multiset row comparison, the verdict, and
determinism.

Everything drives `compare_datasets`; DuckDB-only scenarios use two
`build_duckdb` sides so schema/type drift (not CSV typing) is what's under
test — CSV-side semantics belong to `test_inputs.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fabulexa_forge.compare import compare_datasets

from ._helpers import build_duckdb, write_csv_dir

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# identity and physical drift
# ---------------------------------------------------------------------------


def test_identical_duckdb_files_are_equal(tmp_path: "Path") -> None:
    statements = [
        "CREATE TABLE t (id BIGINT, name VARCHAR)",
        "INSERT INTO t VALUES (1, 'Ada'), (2, 'Grace')",
    ]
    expected = build_duckdb(tmp_path / "expected.duckdb", statements)
    actual = build_duckdb(tmp_path / "actual.duckdb", statements)
    result = compare_datasets(expected, actual)
    assert result.equal
    table_comparison = result.tables[0]
    assert table_comparison.schema == ()
    assert table_comparison.expected_rows == table_comparison.actual_rows == 2
    assert table_comparison.rows is not None
    assert table_comparison.rows.missing_total == 0
    assert table_comparison.rows.extra_total == 0


def test_row_order_scrambled_is_equal(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (id BIGINT)", "INSERT INTO t VALUES (1), (2), (3)"],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        ["CREATE TABLE t (id BIGINT)", "INSERT INTO t VALUES (3), (1), (2)"],
    )
    assert compare_datasets(expected, actual).equal


def test_column_declaration_order_scrambled_is_equal(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (id BIGINT, name VARCHAR)",
            "INSERT INTO t VALUES (1, 'Ada')",
        ],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        [
            "CREATE TABLE t (name VARCHAR, id BIGINT)",
            "INSERT INTO t VALUES ('Ada', 1)",
        ],
    )
    assert compare_datasets(expected, actual).equal


def test_actual_integer_for_expected_bigint_equal_values_is_equal(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (id BIGINT)", "INSERT INTO t VALUES (1), (2)"],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        ["CREATE TABLE t (id INTEGER)", "INSERT INTO t VALUES (1), (2)"],
    )
    result = compare_datasets(expected, actual)
    assert result.equal
    assert result.tables[0].schema == ()


def test_actual_float_for_expected_double_compared_after_cast(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (score DOUBLE)", "INSERT INTO t VALUES (1.5), (2.25)"],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        ["CREATE TABLE t (score FLOAT)", "INSERT INTO t VALUES (1.5), (2.25)"],
    )
    result = compare_datasets(expected, actual)
    assert result.equal
    assert result.tables[0].schema == ()


# ---------------------------------------------------------------------------
# column-incompatible
# ---------------------------------------------------------------------------


def test_actual_varchar_for_expected_bigint_is_column_incompatible(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (id BIGINT, name VARCHAR)",
            "INSERT INTO t VALUES (1, 'Ada')",
        ],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        [
            "CREATE TABLE t (id VARCHAR, name VARCHAR)",
            "INSERT INTO t VALUES ('1', 'Ada')",
        ],
    )
    result = compare_datasets(expected, actual)
    assert not result.equal
    table_comparison = result.tables[0]
    assert len(table_comparison.schema) == 1
    discrepancy = table_comparison.schema[0]
    assert discrepancy.kind == "column-incompatible"
    assert discrepancy.column == "id"
    assert discrepancy.expected_type == "BIGINT"
    assert discrepancy.actual_type == "VARCHAR"
    assert table_comparison.rows is not None
    assert table_comparison.rows.columns == ("name",)
    assert table_comparison.rows.missing_total == 0
    assert table_comparison.rows.extra_total == 0


# ---------------------------------------------------------------------------
# table-missing / table-extra
# ---------------------------------------------------------------------------


def test_table_in_expected_only_is_table_missing(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (id BIGINT)", "INSERT INTO t VALUES (1), (2)"],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb", ["CREATE TABLE other (id BIGINT)"]
    )
    result = compare_datasets(expected, actual)
    assert not result.equal
    table_comparison = next(tc for tc in result.tables if tc.table == "t")
    assert table_comparison.schema[0].kind == "table-missing"
    assert table_comparison.expected_rows == 2
    assert table_comparison.actual_rows is None
    assert table_comparison.rows is None


def test_table_in_actual_only_is_table_extra(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb", ["CREATE TABLE t (id BIGINT)"]
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        [
            "CREATE TABLE t (id BIGINT)",
            "CREATE TABLE other (id BIGINT)",
            "INSERT INTO other VALUES (1), (2), (3)",
        ],
    )
    result = compare_datasets(expected, actual)
    assert not result.equal
    table_comparison = next(tc for tc in result.tables if tc.table == "other")
    assert table_comparison.schema[0].kind == "table-extra"
    assert table_comparison.expected_rows is None
    assert table_comparison.actual_rows == 3
    assert table_comparison.rows is None


# ---------------------------------------------------------------------------
# zero-row tables
# ---------------------------------------------------------------------------


def test_zero_row_table_both_sides_matching_columns_is_equal(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb", ["CREATE TABLE t (id BIGINT)"]
    )
    actual = build_duckdb(tmp_path / "actual.duckdb", ["CREATE TABLE t (id BIGINT)"])
    result = compare_datasets(expected, actual)
    assert result.equal
    table_comparison = result.tables[0]
    assert table_comparison.expected_rows == 0
    assert table_comparison.actual_rows == 0
    assert table_comparison.rows is not None
    assert table_comparison.rows.missing_total == 0
    assert table_comparison.rows.extra_total == 0


# ---------------------------------------------------------------------------
# column-missing / column-extra
# ---------------------------------------------------------------------------


def test_column_missing_and_extra_carry_types(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (id BIGINT, name VARCHAR)", "INSERT INTO t VALUES (1, 'Ada')"],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        [
            "CREATE TABLE t (id BIGINT, dept VARCHAR)",
            "INSERT INTO t VALUES (1, 'math')",
        ],
    )
    result = compare_datasets(expected, actual)
    assert not result.equal
    table_comparison = result.tables[0]
    kinds = {d.kind: d for d in table_comparison.schema}
    assert kinds["column-missing"].column == "name"
    assert kinds["column-missing"].expected_type == "VARCHAR"
    assert kinds["column-missing"].actual_type is None
    assert kinds["column-extra"].column == "dept"
    assert kinds["column-extra"].expected_type is None
    assert kinds["column-extra"].actual_type == "VARCHAR"


# ---------------------------------------------------------------------------
# multiset semantics
# ---------------------------------------------------------------------------


def test_multiplicity_mismatch_lists_missing_and_extra_occurrences(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (id BIGINT)", "INSERT INTO t VALUES (1), (1), (1), (2)"],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        ["CREATE TABLE t (id BIGINT)", "INSERT INTO t VALUES (1), (2), (2)"],
    )
    result = compare_datasets(expected, actual)
    assert not result.equal
    rows = result.tables[0].rows
    assert rows is not None
    assert rows.missing == (("1",), ("1",))
    assert rows.missing_total == 2
    assert rows.extra == (("2",),)
    assert rows.extra_total == 1


# ---------------------------------------------------------------------------
# max_row_diffs truncation
# ---------------------------------------------------------------------------


def test_max_row_diffs_truncates_listings_but_not_totals_or_verdict(
    tmp_path: "Path",
) -> None:
    expected_inserts = ", ".join(f"({i})" for i in range(1, 11))
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (id BIGINT)", f"INSERT INTO t VALUES {expected_inserts}"],
    )
    actual_inserts = ", ".join(f"({i})" for i in range(101, 111))
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        ["CREATE TABLE t (id BIGINT)", f"INSERT INTO t VALUES {actual_inserts}"],
    )
    result = compare_datasets(expected, actual, max_row_diffs=3)
    assert not result.equal
    rows = result.tables[0].rows
    assert rows is not None
    assert len(rows.missing) == 3
    assert len(rows.extra) == 3
    assert rows.missing_total == 10
    assert rows.extra_total == 10


# ---------------------------------------------------------------------------
# empty compared-column set
# ---------------------------------------------------------------------------


def test_every_column_incompatible_degenerates_row_pass_to_count_check(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (id BIGINT)", "INSERT INTO t VALUES (1), (2)"],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        ["CREATE TABLE t (id VARCHAR)", "INSERT INTO t VALUES ('1'), ('2')"],
    )
    result = compare_datasets(expected, actual)
    assert not result.equal
    table_comparison = result.tables[0]
    assert table_comparison.schema[0].kind == "column-incompatible"
    assert table_comparison.rows is not None
    assert table_comparison.rows.columns == ()
    assert table_comparison.rows.missing_total == 0
    assert table_comparison.rows.extra_total == 0


def test_no_matching_columns_csv_actual_degenerates_row_pass_to_count_check(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (id BIGINT)", "INSERT INTO t VALUES (1), (2)"],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(csv_dir, {"t.csv": "other_id\n1\n2\n"})
    result = compare_datasets(expected, csv_dir)
    assert not result.equal
    table_comparison = result.tables[0]
    kinds = {d.kind for d in table_comparison.schema}
    assert kinds == {"column-missing", "column-extra"}
    assert table_comparison.rows is not None
    assert table_comparison.rows.columns == ()
    assert table_comparison.rows.missing_total == 0
    assert table_comparison.rows.extra_total == 0


# ---------------------------------------------------------------------------
# `tables` narrowing
# ---------------------------------------------------------------------------


def test_tables_narrowing_excludes_actual_extra_and_expected_unselected(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (id BIGINT)", "CREATE TABLE unselected (id BIGINT)"],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        ["CREATE TABLE t (id BIGINT)", "CREATE TABLE actual_only (id BIGINT)"],
    )
    result = compare_datasets(expected, actual, tables=["t"])
    assert result.equal
    assert [tc.table for tc in result.tables] == ["t"]


# ---------------------------------------------------------------------------
# NULL vs empty string (DuckDB actual)
# ---------------------------------------------------------------------------


def test_null_vs_empty_string_differ_in_duckdb_actual(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (id BIGINT, note VARCHAR)", "INSERT INTO t VALUES (1, NULL)"],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        ["CREATE TABLE t (id BIGINT, note VARCHAR)", "INSERT INTO t VALUES (1, '')"],
    )
    result = compare_datasets(expected, actual)
    assert not result.equal


# ---------------------------------------------------------------------------
# NULL ordering
# ---------------------------------------------------------------------------


def test_null_sorts_before_every_encoded_string_in_listed_tuples(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (note VARCHAR)", "INSERT INTO t VALUES ('aaa'), (NULL), ('')"],
    )
    actual = build_duckdb(tmp_path / "actual.duckdb", ["CREATE TABLE t (note VARCHAR)"])
    result = compare_datasets(expected, actual)
    rows = result.tables[0].rows
    assert rows is not None
    assert rows.missing == ((None,), ("",), ("aaa",))


# ---------------------------------------------------------------------------
# ComparisonResult.tables ordering and universe
# ---------------------------------------------------------------------------


def test_tables_sorted_by_name_spans_union_of_both_sides(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE zeta (id BIGINT)", "CREATE TABLE alpha (id BIGINT)"],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        [
            "CREATE TABLE zeta (id BIGINT)",
            "CREATE TABLE alpha (id BIGINT)",
            "CREATE TABLE beta (id BIGINT)",
        ],
    )
    result = compare_datasets(expected, actual)
    assert [tc.table for tc in result.tables] == ["alpha", "beta", "zeta"]


# ---------------------------------------------------------------------------
# discrepancy ordering within a table
# ---------------------------------------------------------------------------


def test_schema_discrepancy_ordering_follows_kind_declaration_order(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (missing_col BIGINT, incompatible_col BIGINT)",
        ],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        [
            "CREATE TABLE t (incompatible_col VARCHAR, extra_col BIGINT)",
        ],
    )
    result = compare_datasets(expected, actual)
    kinds = [d.kind for d in result.tables[0].schema]
    assert kinds == ["column-missing", "column-extra", "column-incompatible"]


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_determinism_two_runs_produce_equal_results(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (id BIGINT, name VARCHAR)",
            "INSERT INTO t VALUES (3, 'c'), (1, 'a'), (2, 'b'), (1, 'a')",
        ],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        [
            "CREATE TABLE t (id BIGINT, name VARCHAR)",
            "INSERT INTO t VALUES (1, 'a'), (2, 'b'), (2, 'b')",
        ],
    )
    result_1 = compare_datasets(expected, actual)
    result_2 = compare_datasets(expected, actual)
    assert result_1 == result_2


# ---------------------------------------------------------------------------
# timestamp precision drift
# ---------------------------------------------------------------------------


def test_timestamp_precision_drift_same_instants_is_equal(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (id BIGINT, ts TIMESTAMP)",
            "INSERT INTO t VALUES (1, TIMESTAMP '2024-06-01 12:00:00')",
        ],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        [
            "CREATE TABLE t (id BIGINT, ts TIMESTAMP_S)",
            "INSERT INTO t VALUES (1, TIMESTAMP '2024-06-01 12:00:00')",
        ],
    )
    result = compare_datasets(expected, actual)
    assert result.equal
    assert result.tables[0].schema == ()
