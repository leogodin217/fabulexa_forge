"""Tests for the generated calendar (`compile_date_dimension_spec`) and the
`date_ref` range guard (`check_date_refs_in_range`)."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from exporters._emit_fixtures import build_test_emit
from fabulexa_forge.config.models import DateDimensionConfig
from fabulexa_forge.errors import DateRefOutOfRange
from fabulexa_forge.exporters.date_dimension import (
    DATE_DIMENSION_COLUMNS,
    DATE_DIMENSION_TABLE_NAME,
    CalendarSource,
    check_date_refs_in_range,
    compile_date_dimension_spec,
)
from fabulexa_forge.exporters.query_spec import QuerySpec
from fabulexa_forge.reader.emit import open_emit

if TYPE_CHECKING:
    from pathlib import Path

    from fabulexa_forge.reader.emit import Emit


def _config(from_: str, to: str) -> DateDimensionConfig:
    return DateDimensionConfig.model_validate({"from": from_, "to": to})


def _describe(emit: "Emit", sql: str) -> list[tuple[str, str]]:
    """(column name, DuckDB type text) pairs, in projection order."""
    rows = emit.query(f"DESCRIBE {sql}", ())
    return [(row[0], row[1]) for row in rows]


# ---------------------------------------------------------------------------
# compile_date_dimension_spec
# ---------------------------------------------------------------------------


def test_compiled_columns_match_pinned_names_and_types(tmp_path: "Path") -> None:
    """The materialized relation has exactly the 13 pinned columns, in
    order, with the pinned DuckDB types (INTEGER, not BIGINT)."""
    spec = compile_date_dimension_spec(_config("2024-01-01", "2024-01-01"), "create")
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        described = _describe(emit, spec.sql)
    assert described == list(DATE_DIMENSION_COLUMNS)


def test_full_year_yields_366_rows_for_a_leap_year(tmp_path: "Path") -> None:
    """2024-01-01..2024-12-31 (a leap year) yields 366 rows."""
    spec = compile_date_dimension_spec(_config("2024-01-01", "2024-12-31"), "create")
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        (row,) = emit.query(f"SELECT COUNT(*) FROM ({spec.sql}) AS _t", ())
    assert row[0] == 366


def test_from_equals_to_yields_one_row(tmp_path: "Path") -> None:
    """from == to yields exactly one row."""
    spec = compile_date_dimension_spec(_config("2024-06-01", "2024-06-01"), "create")
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        rows = emit.query(spec.sql, ())
    assert len(rows) == 1


def test_rows_ordered_by_date_key_ascending(tmp_path: "Path") -> None:
    """Rows are ordered by date_key ascending."""
    spec = compile_date_dimension_spec(_config("2024-01-01", "2024-01-10"), "create")
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        rows = emit.query(spec.sql, ())
    keys = [row[0] for row in rows]
    assert keys == sorted(keys)


@pytest.mark.parametrize("write_mode", ["create", "replace"])
def test_write_mode_passes_through(tmp_path: "Path", write_mode: "str") -> None:
    """write_mode passes through unchanged."""
    spec = compile_date_dimension_spec(_config("2024-01-01", "2024-01-01"), write_mode)  # type: ignore[arg-type]
    assert spec.write_mode == write_mode


def test_spec_carries_no_extra_shape(tmp_path: "Path") -> None:
    """keys is None, provenance/kind_values/author_descriptions are empty,
    author_table_description is None, event_log is False, supplement is
    None, calendar == CalendarSource(from, to), references is empty."""
    spec = compile_date_dimension_spec(_config("2024-01-01", "2024-12-31"), "create")
    assert spec.table_name == DATE_DIMENSION_TABLE_NAME
    assert spec.keys is None
    assert spec.provenance == {}
    assert spec.kind_values == {}
    assert spec.author_descriptions == {}
    assert spec.author_table_description is None
    assert spec.event_log is False
    assert spec.supplement is None
    assert spec.calendar == CalendarSource(
        from_=date(2024, 1, 1), to=date(2024, 12, 31)
    )
    assert spec.references == {}


def test_sql_is_byte_identical_for_the_same_range() -> None:
    """The SQL text is byte-identical for the same (from, to)."""
    first = compile_date_dimension_spec(_config("2024-01-01", "2024-12-31"), "create")
    second = compile_date_dimension_spec(_config("2024-01-01", "2024-12-31"), "create")
    assert first.sql == second.sql


def test_rows_identical_across_two_emits_with_different_anchors(
    tmp_path: "Path",
) -> None:
    """The materialized rows are identical across two emits — the compile
    consults no emit, no anchor, no session."""
    spec = compile_date_dimension_spec(_config("2024-01-01", "2024-01-05"), "create")
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    emit_dir_a = build_test_emit(dir_a)
    emit_dir_b = build_test_emit(dir_b)
    with open_emit(emit_dir_a) as emit_a:
        rows_a = emit_a.query(spec.sql, ())
    with open_emit(emit_dir_b) as emit_b:
        rows_b = emit_b.query(spec.sql, ())
    assert rows_a == rows_b


# ---------------------------------------------------------------------------
# Calendar values
# ---------------------------------------------------------------------------


def _row_for(emit: "Emit", spec: QuerySpec, date_key: int) -> tuple[object, ...]:
    (row,) = emit.query(
        f'SELECT * FROM ({spec.sql}) AS _t WHERE "date_key" = {date_key}', ()
    )
    return row


def test_january_first_2024_values(tmp_path: "Path") -> None:
    """2024-01-01 (a Monday) renders the documented field values."""
    spec = compile_date_dimension_spec(_config("2024-01-01", "2024-12-31"), "create")
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        row = _row_for(emit, spec, 20240101)
    (
        date_key,
        _date,
        year,
        quarter,
        month,
        day,
        day_of_week,
        day_of_year,
        iso_year,
        iso_week,
        month_name,
        day_name,
        is_weekend,
    ) = row
    assert date_key == 20240101
    assert year == 2024
    assert quarter == 1
    assert month == 1
    assert day == 1
    assert day_of_week == 1
    assert day_of_year == 1
    assert iso_year == 2024
    assert iso_week == 1
    assert month_name == "January"
    assert day_name == "Monday"
    assert is_weekend is False


def test_2021_01_03_is_iso_week_53_of_prior_year(tmp_path: "Path") -> None:
    """2021-01-03 (a Sunday) belongs to ISO week 53 of 2020 and is a weekend."""
    spec = compile_date_dimension_spec(_config("2020-01-01", "2021-12-31"), "create")
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        row = _row_for(emit, spec, 20210103)
    assert row[6] == 7  # day_of_week
    assert row[8] == 2020  # iso_year
    assert row[9] == 53  # iso_week
    assert row[12] is True  # is_weekend


def test_2024_12_30_rolls_into_iso_year_2025_week_1(tmp_path: "Path") -> None:
    """2024-12-30 belongs to ISO year 2025, week 1."""
    spec = compile_date_dimension_spec(_config("2024-01-01", "2024-12-31"), "create")
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        row = _row_for(emit, spec, 20241230)
    assert row[8] == 2025  # iso_year
    assert row[9] == 1  # iso_week


def test_2024_02_29_is_the_60th_day_of_year(tmp_path: "Path") -> None:
    """2024-02-29 (leap day) is day_of_year 60."""
    spec = compile_date_dimension_spec(_config("2024-01-01", "2024-12-31"), "create")
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        row = _row_for(emit, spec, 20240229)
    assert row[7] == 60  # day_of_year


# ---------------------------------------------------------------------------
# check_date_refs_in_range
# ---------------------------------------------------------------------------


def _spec_with_keys(
    table_name: str, column: str, keys: "list[int | None]"
) -> QuerySpec:
    """A QuerySpec whose relation is a literal VALUES list under `column`,
    referencing `dim_date`. An empty `keys` compiles the typed empty-relation
    form."""
    if not keys:
        sql = f'SELECT CAST(NULL AS INTEGER) AS "{column}" WHERE false'
    else:
        values = ", ".join("(NULL)" if key is None else f"({key})" for key in keys)
        sql = f'SELECT * FROM (VALUES {values}) AS _v("{column}")'
    return QuerySpec(
        table_name=table_name,
        sql=sql,
        write_mode="create",
        references={column: DATE_DIMENSION_TABLE_NAME},
    )


def test_all_keys_in_range_returns(tmp_path: "Path") -> None:
    """Every key inside [from, to] returns cleanly."""
    emit_dir = build_test_emit(tmp_path)
    spec = _spec_with_keys("fact_x", "event_date_key", [20200101, 20261231])
    with open_emit(emit_dir) as emit:
        check_date_refs_in_range(emit, [spec], _config("2020-01-01", "2026-12-31"))


def test_key_below_from_reports_the_minimum(tmp_path: "Path") -> None:
    """A key below `from` raises DateRefOutOfRange reporting the minimum."""
    emit_dir = build_test_emit(tmp_path)
    spec = _spec_with_keys("fact_x", "event_date_key", [19850101, 20240101])
    with open_emit(emit_dir) as emit:
        with pytest.raises(DateRefOutOfRange) as exc_info:
            check_date_refs_in_range(emit, [spec], _config("2020-01-01", "2026-12-31"))
    assert str(exc_info.value) == (
        "table 'fact_x' column 'event_date_key': date key 19850101 lies"
        " outside date_dimension 2020-01-01..2026-12-31"
    )


def test_key_above_to_reports_the_maximum(tmp_path: "Path") -> None:
    """A key above `to` raises DateRefOutOfRange reporting the maximum."""
    emit_dir = build_test_emit(tmp_path)
    spec = _spec_with_keys("fact_x", "event_date_key", [20240101, 20301231])
    with open_emit(emit_dir) as emit:
        with pytest.raises(DateRefOutOfRange) as exc_info:
            check_date_refs_in_range(emit, [spec], _config("2020-01-01", "2026-12-31"))
    assert "date key 20301231" in str(exc_info.value)


def test_both_below_and_above_reports_the_minimum(tmp_path: "Path") -> None:
    """Both a below- and an above-range key present -> the minimum is named."""
    emit_dir = build_test_emit(tmp_path)
    spec = _spec_with_keys("fact_x", "event_date_key", [19850101, 20301231])
    with open_emit(emit_dir) as emit:
        with pytest.raises(DateRefOutOfRange) as exc_info:
            check_date_refs_in_range(emit, [spec], _config("2020-01-01", "2026-12-31"))
    assert "date key 19850101" in str(exc_info.value)


def test_all_null_column_never_violates(tmp_path: "Path") -> None:
    """An all-NULL column never violates."""
    emit_dir = build_test_emit(tmp_path)
    spec = _spec_with_keys("fact_x", "event_date_key", [None, None])
    with open_emit(emit_dir) as emit:
        check_date_refs_in_range(emit, [spec], _config("2020-01-01", "2026-12-31"))


def test_empty_relation_never_violates(tmp_path: "Path") -> None:
    """An empty relation never violates."""
    emit_dir = build_test_emit(tmp_path)
    spec = _spec_with_keys("fact_x", "event_date_key", [])
    with open_emit(emit_dir) as emit:
        check_date_refs_in_range(emit, [spec], _config("2020-01-01", "2026-12-31"))


def test_spec_with_no_date_ref_references_is_not_probed(tmp_path: "Path") -> None:
    """A spec whose references names no dim_date column is not probed —
    even a wildly out-of-range fk-only reference is ignored."""
    emit_dir = build_test_emit(tmp_path)
    spec = QuerySpec(
        table_name="fact_x",
        sql='SELECT CAST(1 AS INTEGER) AS "dim_id"',
        write_mode="create",
        references={"dim_id": "dim_thing"},
    )
    with open_emit(emit_dir) as emit:
        check_date_refs_in_range(emit, [spec], _config("2020-01-01", "2026-12-31"))


def test_first_violating_column_in_output_order_is_named(tmp_path: "Path") -> None:
    """Two violating columns -> the first in output order is named."""
    emit_dir = build_test_emit(tmp_path)
    sql = 'SELECT * FROM (VALUES (19850101, 19860101)) AS _v("first_key", "second_key")'
    spec = QuerySpec(
        table_name="fact_x",
        sql=sql,
        write_mode="create",
        references={
            "first_key": DATE_DIMENSION_TABLE_NAME,
            "second_key": DATE_DIMENSION_TABLE_NAME,
        },
    )
    with open_emit(emit_dir) as emit:
        with pytest.raises(DateRefOutOfRange) as exc_info:
            check_date_refs_in_range(emit, [spec], _config("2020-01-01", "2026-12-31"))
    assert "column 'first_key'" in str(exc_info.value)


def test_bound_keys_computed_in_sql_for_a_leap_day_from(tmp_path: "Path") -> None:
    """from = 2024-02-29 -> lower bound 20240229."""
    emit_dir = build_test_emit(tmp_path)
    spec = _spec_with_keys("fact_x", "event_date_key", [20240228])
    with open_emit(emit_dir) as emit:
        with pytest.raises(DateRefOutOfRange) as exc_info:
            check_date_refs_in_range(emit, [spec], _config("2024-02-29", "2024-12-31"))
    assert "date key 20240228" in str(exc_info.value)
    assert "date_dimension 2024-02-29..2024-12-31" in str(exc_info.value)
