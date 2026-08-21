"""Tests for the compare surface's input surface, exercised through
`compare_datasets`: expected/actual resolution, `main`-schema-only DuckDB
catalogs, the CSV directory scan, and the per-family CSV typing casts
(including the blob and interval bespoke parses).

Never opens `inputs.py`'s helpers directly — every case drives
`compare_datasets`, matching the design doc's "the input surface is only
observable through the public entry point" framing.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from fabulexa_forge.compare import CompareInputError, compare_datasets

from ._helpers import build_duckdb, write_csv_dir

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# expected-side resolution
# ---------------------------------------------------------------------------


def test_expected_missing_path_raises(tmp_path: "Path") -> None:
    actual = build_duckdb(tmp_path / "actual.duckdb", ["CREATE TABLE t (id BIGINT)"])
    missing = tmp_path / "missing.duckdb"
    with pytest.raises(CompareInputError, match="expected side must be a DuckDB file"):
        compare_datasets(missing, actual)


def test_expected_plain_text_file_raises(tmp_path: "Path") -> None:
    expected = tmp_path / "expected.duckdb"
    expected.write_text("not a duckdb file")
    actual = build_duckdb(tmp_path / "actual.duckdb", ["CREATE TABLE t (id BIGINT)"])
    with pytest.raises(CompareInputError, match="expected side must be a DuckDB file"):
        compare_datasets(expected, actual)


# ---------------------------------------------------------------------------
# actual-side resolution
# ---------------------------------------------------------------------------


def test_actual_missing_path_raises(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb", ["CREATE TABLE t (id BIGINT)"]
    )
    with pytest.raises(
        CompareInputError, match="neither a DuckDB file nor a CSV directory"
    ):
        compare_datasets(expected, tmp_path / "missing")


def test_actual_empty_directory_raises(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb", ["CREATE TABLE t (id BIGINT)"]
    )
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(
        CompareInputError, match="neither a DuckDB file nor a CSV directory"
    ):
        compare_datasets(expected, empty_dir)


def test_actual_directory_with_only_non_csv_files_raises(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb", ["CREATE TABLE t (id BIGINT)"]
    )
    txt_dir = tmp_path / "txt_only"
    txt_dir.mkdir()
    (txt_dir / "notes.txt").write_text("hello")
    with pytest.raises(
        CompareInputError, match="neither a DuckDB file nor a CSV directory"
    ):
        compare_datasets(expected, txt_dir)


def test_actual_csv_zero_byte_file_raises_naming_file(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb", ["CREATE TABLE t (id BIGINT)"]
    )
    csv_dir = tmp_path / "csvs"
    csv_dir.mkdir()
    empty_csv = csv_dir / "t.csv"
    empty_csv.write_text("")
    with pytest.raises(CompareInputError, match=re.escape(str(empty_csv))):
        compare_datasets(expected, csv_dir)


# ---------------------------------------------------------------------------
# `tables` and `max_row_diffs` business rules
# ---------------------------------------------------------------------------


def test_tables_unknown_table_raises_listing_names(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb", ["CREATE TABLE t (id BIGINT)"]
    )
    actual = build_duckdb(tmp_path / "actual.duckdb", ["CREATE TABLE t (id BIGINT)"])
    with pytest.raises(
        CompareInputError, match="tables selection names unknown table.*nope"
    ):
        compare_datasets(expected, actual, tables=["nope"])


def test_tables_empty_selection_raises(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb", ["CREATE TABLE t (id BIGINT)"]
    )
    actual = build_duckdb(tmp_path / "actual.duckdb", ["CREATE TABLE t (id BIGINT)"])
    with pytest.raises(CompareInputError, match="tables selection must not be empty"):
        compare_datasets(expected, actual, tables=[])


def test_max_row_diffs_negative_raises(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb", ["CREATE TABLE t (id BIGINT)"]
    )
    actual = build_duckdb(tmp_path / "actual.duckdb", ["CREATE TABLE t (id BIGINT)"])
    with pytest.raises(CompareInputError, match="max_row_diffs must be >= 0"):
        compare_datasets(expected, actual, max_row_diffs=-1)


def test_max_row_diffs_zero_accepted_empty_listings_totals_reported(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (id BIGINT)", "INSERT INTO t VALUES (1), (2)"],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        ["CREATE TABLE t (id BIGINT)", "INSERT INTO t VALUES (1), (3)"],
    )
    result = compare_datasets(expected, actual, max_row_diffs=0)
    table_comparison = result.tables[0]
    assert table_comparison.rows is not None
    assert table_comparison.rows.missing == ()
    assert table_comparison.rows.extra == ()
    assert table_comparison.rows.missing_total == 1
    assert table_comparison.rows.extra_total == 1
    assert not result.equal


# ---------------------------------------------------------------------------
# family coverage — the comparison-universe scope rule
# ---------------------------------------------------------------------------


def test_expected_uuid_column_in_universe_raises(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (id BIGINT, token UUID)"],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        ["CREATE TABLE t (id BIGINT, token UUID)"],
    )
    with pytest.raises(CompareInputError, match="unsupported type"):
        compare_datasets(expected, actual)


def test_expected_uuid_column_excluded_by_tables_no_error(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (id BIGINT, token UUID)",
            "CREATE TABLE u (id BIGINT)",
            "INSERT INTO u VALUES (1)",
        ],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        [
            "CREATE TABLE t (id BIGINT, token UUID)",
            "CREATE TABLE u (id BIGINT)",
            "INSERT INTO u VALUES (1)",
        ],
    )
    result = compare_datasets(expected, actual, tables=["u"])
    assert result.equal


# ---------------------------------------------------------------------------
# `main`-schema-only catalogs
# ---------------------------------------------------------------------------


def test_table_in_other_schema_invisible_on_both_sides(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (id BIGINT)",
            "INSERT INTO t VALUES (1)",
            "CREATE SCHEMA other",
            "CREATE TABLE other.hidden (id BIGINT)",
        ],
    )
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        [
            "CREATE TABLE t (id BIGINT)",
            "INSERT INTO t VALUES (1)",
            "CREATE SCHEMA other",
            "CREATE TABLE other.extra (id BIGINT)",
        ],
    )
    result = compare_datasets(expected, actual)
    assert result.equal
    assert [table.table for table in result.tables] == ["t"]


# ---------------------------------------------------------------------------
# CSV directory scan
# ---------------------------------------------------------------------------


def test_csv_directory_scan_ignores_subdirs_non_csv_and_uppercase_extension(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE people (id BIGINT)", "INSERT INTO people VALUES (1), (2)"],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(
        csv_dir,
        {
            "people.csv": "id\n1\n2\n",
            "notes.txt": "ignored",
            "people.CSV": "id\n99\n",
        },
    )
    (csv_dir / "subdir").mkdir()
    (csv_dir / "subdir" / "people2.csv").write_text("id\n1\n")
    result = compare_datasets(expected, csv_dir)
    assert result.equal
    assert [table.table for table in result.tables] == ["people"]


# ---------------------------------------------------------------------------
# CSV typing casts
# ---------------------------------------------------------------------------


def test_csv_typing_casts_toward_expected_family(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (n BIGINT, f DOUBLE, b BOOLEAN, d DATE)",
            "INSERT INTO t VALUES (5, 1.5, true, DATE '2024-06-01')",
        ],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(csv_dir, {"t.csv": "n,f,b,d\n5,1.5,true,2024-06-01\n"})
    result = compare_datasets(expected, csv_dir)
    assert result.equal


def test_csv_failing_cast_is_row_discrepancy_not_error(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (id BIGINT)", "INSERT INTO t VALUES (1)"],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(csv_dir, {"t.csv": "id\nnotanumber\n"})
    result = compare_datasets(expected, csv_dir)
    assert not result.equal
    table_comparison = result.tables[0]
    assert table_comparison.rows is not None
    assert table_comparison.rows.missing == (("1",),)
    assert table_comparison.rows.extra == (("notanumber",),)


# ---------------------------------------------------------------------------
# CSV NULL vs empty string
# ---------------------------------------------------------------------------


def test_csv_unquoted_empty_reads_as_null_quoted_empty_reads_as_empty_string(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (id BIGINT, note VARCHAR)",
            "INSERT INTO t VALUES (1, NULL), (2, '')",
        ],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(csv_dir, {"t.csv": 'id,note\n1,\n2,""\n'})
    result = compare_datasets(expected, csv_dir)
    assert result.equal


def test_csv_null_and_empty_string_not_equal_to_each_other(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (id BIGINT, note VARCHAR)", "INSERT INTO t VALUES (1, NULL)"],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(csv_dir, {"t.csv": 'id,note\n1,""\n'})
    result = compare_datasets(expected, csv_dir)
    assert not result.equal


# ---------------------------------------------------------------------------
# CSV blob
# ---------------------------------------------------------------------------


def test_csv_blob_lowercase_hex_decodes(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (id BIGINT, data BLOB)",
            r"INSERT INTO t VALUES (1, '\xDE\xAD\xBE\xEF'::BLOB)",
        ],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(csv_dir, {"t.csv": "id,data\n1,deadbeef\n"})
    result = compare_datasets(expected, csv_dir)
    assert result.equal


def test_csv_blob_bad_hex_is_row_discrepancy_not_error(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (id BIGINT, data BLOB)",
            r"INSERT INTO t VALUES (1, '\xDE\xAD\xBE\xEF'::BLOB)",
        ],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(csv_dir, {"t.csv": "id,data\n1,zz\n"})
    result = compare_datasets(expected, csv_dir)
    assert not result.equal
    table_comparison = result.tables[0]
    assert table_comparison.rows is not None
    assert table_comparison.rows.missing == (("1", "deadbeef"),)
    assert table_comparison.rows.extra == (("1", "zz"),)


# ---------------------------------------------------------------------------
# CSV interval
# ---------------------------------------------------------------------------


def test_csv_interval_writer_form_parses_exactly(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (id BIGINT, dur INTERVAL)",
            "INSERT INTO t VALUES (1, INTERVAL 26 HOUR)",
        ],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(csv_dir, {"t.csv": "id,dur\n1,26:00:00.000000\n"})
    result = compare_datasets(expected, csv_dir)
    assert result.equal


def test_csv_interval_negative_writer_form_parses_exactly(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (id BIGINT, dur INTERVAL)",
            "INSERT INTO t VALUES (1, -INTERVAL 1 SECOND)",
        ],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(csv_dir, {"t.csv": "id,dur\n1,-0:00:01.000000\n"})
    result = compare_datasets(expected, csv_dir)
    assert result.equal


def test_csv_interval_alternate_vocabulary_compares_equal_under_day_fold(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (id BIGINT, dur INTERVAL)",
            "INSERT INTO t VALUES (1, INTERVAL 26 HOUR)",
        ],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(csv_dir, {"t.csv": "id,dur\n1,1 day 02:00:00\n"})
    result = compare_datasets(expected, csv_dir)
    assert result.equal


# ---------------------------------------------------------------------------
# CSV timestamptz
# ---------------------------------------------------------------------------


def test_csv_timestamptz_offsetless_text_reads_as_utc_wall_clock(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (id BIGINT, dt TIMESTAMPTZ)",
            "INSERT INTO t VALUES (1, TIMESTAMPTZ '2024-06-01 12:00:00+00')",
        ],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(csv_dir, {"t.csv": "id,dt\n1,2024-06-01 12:00:00\n"})
    result = compare_datasets(expected, csv_dir)
    assert result.equal


def test_csv_timestamptz_offset_carrying_text_compares_as_same_instant(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (id BIGINT, dt TIMESTAMPTZ)",
            "INSERT INTO t VALUES (1, TIMESTAMPTZ '2024-06-01 12:00:00+00')",
        ],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(csv_dir, {"t.csv": "id,dt\n1,2024-06-01 08:00:00-04\n"})
    result = compare_datasets(expected, csv_dir)
    assert result.equal


# ---------------------------------------------------------------------------
# RFC4180 tokenizer edge cases
# ---------------------------------------------------------------------------


def test_csv_doubled_quote_escapes_embedded_quote(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (id BIGINT, note VARCHAR)",
            """INSERT INTO t VALUES (1, 'She said "hi"')""",
        ],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(csv_dir, {"t.csv": 'id,note\n1,"She said ""hi"""\n'})
    result = compare_datasets(expected, csv_dir)
    assert result.equal


def test_csv_crlf_line_endings_parse_same_as_lf(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (id BIGINT)", "INSERT INTO t VALUES (1), (2)"],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(csv_dir, {"t.csv": "id\r\n1\r\n2\r\n"})
    result = compare_datasets(expected, csv_dir)
    assert result.equal


def test_csv_no_trailing_newline_still_reads_last_row(tmp_path: "Path") -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        ["CREATE TABLE t (id BIGINT)", "INSERT INTO t VALUES (1), (2)"],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(csv_dir, {"t.csv": "id\n1\n2"})
    result = compare_datasets(expected, csv_dir)
    assert result.equal


# ---------------------------------------------------------------------------
# CSV interval TRY_CAST failure
# ---------------------------------------------------------------------------


def test_csv_interval_uncastable_text_is_row_discrepancy_not_error(
    tmp_path: "Path",
) -> None:
    expected = build_duckdb(
        tmp_path / "expected.duckdb",
        [
            "CREATE TABLE t (id BIGINT, dur INTERVAL)",
            "INSERT INTO t VALUES (1, INTERVAL 26 HOUR)",
        ],
    )
    csv_dir = tmp_path / "actual"
    write_csv_dir(csv_dir, {"t.csv": "id,dur\n1,not-an-interval\n"})
    result = compare_datasets(expected, csv_dir)
    assert not result.equal
    table_comparison = result.tables[0]
    assert table_comparison.rows is not None
    assert table_comparison.rows.extra == (("1", "not-an-interval"),)
