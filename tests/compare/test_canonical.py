"""Tests for the compare surface's canonical form: `family_of` + `encode_value`.

Covers classification over every DuckDB type name the family table names,
the encoding table per family (including the interval day-fold and
month-carrying fallback), NULL handling, byte-identity with the C6 codec's
`to_csv_text` for the four overlapping families, and the report dataclasses'
frozen-ness / constructibility (Phase 1 smoke; semantics land in Phase 2).
"""

from __future__ import annotations

import datetime
import decimal

import pyarrow as pa
import pytest

from fabulexa_forge.compare.canonical import CanonicalFamily, encode_value, family_of
from fabulexa_forge.compare.report import (
    ComparisonResult,
    RowDiscrepancies,
    SchemaDiscrepancy,
    TableComparison,
)
from fabulexa_forge.reader.conformance import to_csv_text

from ._helpers import assert_frozen

# ---------------------------------------------------------------------------
# family_of
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "duckdb_type",
    [
        "BIGINT",
        "INTEGER",
        "SMALLINT",
        "TINYINT",
        "HUGEINT",
        "UBIGINT",
        "UINTEGER",
        "USMALLINT",
        "UTINYINT",
        "UHUGEINT",
    ],
)
def test_family_of_integer_types(duckdb_type: str) -> None:
    assert family_of(duckdb_type) == "integer"


@pytest.mark.parametrize("duckdb_type", ["DOUBLE", "FLOAT"])
def test_family_of_float_types(duckdb_type: str) -> None:
    assert family_of(duckdb_type) == "float"


def test_family_of_boolean() -> None:
    assert family_of("BOOLEAN") == "boolean"


def test_family_of_text() -> None:
    assert family_of("VARCHAR") == "text"


@pytest.mark.parametrize(
    "duckdb_type", ["TIMESTAMP", "TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMP_NS"]
)
def test_family_of_timestamp_precisions(duckdb_type: str) -> None:
    assert family_of(duckdb_type) == "timestamp"


def test_family_of_date() -> None:
    assert family_of("DATE") == "date"


def test_family_of_time() -> None:
    assert family_of("TIME") == "time"


def test_family_of_timestamptz() -> None:
    assert family_of("TIMESTAMP WITH TIME ZONE") == "timestamptz"


def test_family_of_interval() -> None:
    assert family_of("INTERVAL") == "interval"


def test_family_of_blob() -> None:
    assert family_of("BLOB") == "blob"


@pytest.mark.parametrize("duckdb_type", ["UUID"])
def test_family_of_unclassified_type_returns_none(duckdb_type: str) -> None:
    assert family_of(duckdb_type) is None


@pytest.mark.parametrize(
    "duckdb_type", ["DECIMAL(18,3)", "DECIMAL(4,0)", "DECIMAL(9,9)"]
)
def test_family_of_decimal_types(duckdb_type: str) -> None:
    assert family_of(duckdb_type) == "decimal"


# ---------------------------------------------------------------------------
# encode_value — per family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [(5, "5"), (-5, "-5"), (0, "0")])
def test_encode_integer(value: int, expected: str) -> None:
    assert encode_value(value, "integer") == expected


@pytest.mark.parametrize(
    "value, expected",
    [(0.1, "0.1"), (0.30000000000000004, "0.30000000000000004")],
)
def test_encode_float(value: float, expected: str) -> None:
    assert encode_value(value, "float") == expected


@pytest.mark.parametrize("value, expected", [(True, "true"), (False, "false")])
def test_encode_boolean(value: bool, expected: str) -> None:
    assert encode_value(value, "boolean") == expected


@pytest.mark.parametrize("value", ["hello", ""])
def test_encode_text_identity(value: str) -> None:
    assert encode_value(value, "text") == value


def test_encode_timestamp() -> None:
    value = datetime.datetime(2024, 6, 1, 12, 30, 45, 123456)
    assert encode_value(value, "timestamp") == "2024-06-01 12:30:45.123456"


def test_encode_timestamp_zero_microseconds() -> None:
    value = datetime.datetime(2024, 6, 1, 12, 30, 45)
    assert encode_value(value, "timestamp") == "2024-06-01 12:30:45.000000"


def test_encode_date() -> None:
    assert encode_value(datetime.date(2024, 6, 1), "date") == "2024-06-01"


def test_encode_time() -> None:
    value = datetime.time(12, 30, 45, 123456)
    assert encode_value(value, "time") == "12:30:45.123456"


def test_encode_timestamptz_normalizes_to_utc() -> None:
    zone = datetime.timezone(datetime.timedelta(hours=-4))
    value = datetime.datetime(2024, 6, 1, 12, 0, 0, 500000, tzinfo=zone)
    assert encode_value(value, "timestamptz") == "2024-06-01 16:00:00.500000+00:00"


# ---------------------------------------------------------------------------
# encode_value — decimal
# ---------------------------------------------------------------------------


def test_encode_decimal_strips_trailing_fractional_zeros() -> None:
    assert encode_value(decimal.Decimal("1.50"), "decimal") == "1.5"


def test_encode_decimal_scale_normalization_makes_equal_values_match() -> None:
    assert encode_value(decimal.Decimal("1.50"), "decimal") == encode_value(
        decimal.Decimal("1.5"), "decimal"
    )


def test_encode_decimal_genuinely_different_values_do_not_match() -> None:
    assert encode_value(decimal.Decimal("1.50"), "decimal") != encode_value(
        decimal.Decimal("1.51"), "decimal"
    )


def test_encode_decimal_all_zero_fraction_drops_point() -> None:
    assert encode_value(decimal.Decimal("2.00"), "decimal") == "2"


def test_encode_decimal_negative_value() -> None:
    assert encode_value(decimal.Decimal("-1.50"), "decimal") == "-1.5"


def test_encode_decimal_no_exponent_for_large_scale() -> None:
    assert encode_value(decimal.Decimal("1000000.00"), "decimal") == "1000000"


# ---------------------------------------------------------------------------
# encode_value — interval
# ---------------------------------------------------------------------------


def test_encode_interval_pure_microseconds_unbounded_hours() -> None:
    value = pa.MonthDayNano((0, 0, 26 * 3600 * 1_000_000_000))
    assert encode_value(value, "interval") == "26:00:00.000000"


def test_encode_interval_day_fold_at_24_hours() -> None:
    value = pa.MonthDayNano((0, 1, 2 * 3600 * 1_000_000_000))
    assert encode_value(value, "interval") == "26:00:00.000000"


def test_encode_interval_negative_leading_sign() -> None:
    value = pa.MonthDayNano((0, 0, -1 * 1_000_000_000))
    assert encode_value(value, "interval") == "-0:00:01.000000"


def test_encode_interval_month_carrying_uses_duckdb_text() -> None:
    value = pa.MonthDayNano((1, 2, 3 * 3600 * 1_000_000_000))
    assert encode_value(value, "interval") == "1 month 2 days 03:00:00"


# ---------------------------------------------------------------------------
# encode_value — blob
# ---------------------------------------------------------------------------


def test_encode_blob_lowercase_hex() -> None:
    assert encode_value(b"\xde\xad\xbe\xef", "blob") == "deadbeef"


def test_encode_blob_empty_bytes() -> None:
    assert encode_value(b"", "blob") == ""


# ---------------------------------------------------------------------------
# encode_value — NULL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "family",
    [
        "integer",
        "float",
        "boolean",
        "text",
        "timestamp",
        "date",
        "time",
        "timestamptz",
        "interval",
        "blob",
        "decimal",
    ],
)
def test_encode_null_returns_none(family: CanonicalFamily) -> None:
    assert encode_value(None, family) is None


# ---------------------------------------------------------------------------
# byte-identity with the C6 codec's to_csv_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, family, duckdb_type",
    [
        (5, "integer", "BIGINT"),
        (-5, "integer", "BIGINT"),
        (0, "integer", "BIGINT"),
        (0.1, "float", "DOUBLE"),
        (0.30000000000000004, "float", "DOUBLE"),
        (True, "boolean", "BOOLEAN"),
        (False, "boolean", "BOOLEAN"),
        ("hello world", "text", "VARCHAR"),
        ("", "text", "VARCHAR"),
    ],
)
def test_encode_value_byte_identical_to_conformance_codec(
    value: object, family: CanonicalFamily, duckdb_type: str
) -> None:
    assert encode_value(value, family) == to_csv_text(value, duckdb_type)


# ---------------------------------------------------------------------------
# Report types — frozen dataclasses, smoke construction
# ---------------------------------------------------------------------------


def test_schema_discrepancy_is_frozen() -> None:
    discrepancy = SchemaDiscrepancy(
        kind="column-missing",
        table="dim_person",
        column="name",
        expected_type="VARCHAR",
        actual_type=None,
    )
    assert_frozen(discrepancy, "table", "other")


def test_row_discrepancies_is_frozen() -> None:
    rows = RowDiscrepancies(
        columns=("id",),
        missing=(),
        extra=(),
        missing_total=0,
        extra_total=0,
    )
    assert_frozen(rows, "missing_total", 1)


def test_table_comparison_is_frozen() -> None:
    table = TableComparison(
        table="dim_person",
        schema=(),
        expected_rows=3,
        actual_rows=3,
        rows=RowDiscrepancies(
            columns=("id",), missing=(), extra=(), missing_total=0, extra_total=0
        ),
    )
    assert_frozen(table, "expected_rows", 4)


def test_comparison_result_is_frozen() -> None:
    result = ComparisonResult(equal=True, tables=())
    assert_frozen(result, "equal", False)


def test_comparison_result_constructible_with_nested_tuples() -> None:
    row_discrepancy_tuple: tuple[str | None, ...] = ("1", None, "true")
    rows = RowDiscrepancies(
        columns=("id", "note", "active"),
        missing=(row_discrepancy_tuple,),
        extra=(),
        missing_total=1,
        extra_total=0,
    )
    schema = (
        SchemaDiscrepancy(
            kind="column-incompatible",
            table="dim_person",
            column="active",
            expected_type="BOOLEAN",
            actual_type="VARCHAR",
        ),
    )
    tables = (
        TableComparison(
            table="dim_person",
            schema=schema,
            expected_rows=3,
            actual_rows=3,
            rows=rows,
        ),
    )
    result = ComparisonResult(equal=False, tables=tables)
    assert result.tables[0].rows is not None
    assert result.tables[0].rows.missing[0] == row_discrepancy_tuple
