"""Tests for write_csv.

Verifies: header + typed values written, zero-row yields header-only file,
return value is row count, ExportRuntimeError on failure.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from exporters._emit_fixtures import build_test_emit
from fabulexa_forge.errors import ExportRuntimeError
from fabulexa_forge.reader.emit import open_emit, pin_session_timezone
from fabulexa_forge.writers.csv import write_csv


def test_write_csv_writes_header_and_rows(tmp_path: Path) -> None:
    """write_csv writes a header row plus typed data rows."""
    emit_dir = build_test_emit(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        sql = 'SELECT fork_path, record_id FROM "records__entity" ORDER BY record_id'
        count = write_csv(emit, "dim_entity", sql, out_dir)

    assert count == 2
    csv_path = out_dir / "dim_entity.csv"
    assert csv_path.exists()
    rows = list(csv.reader(csv_path.read_text(encoding="utf-8").splitlines()))
    assert rows[0] == ["fork_path", "record_id"]
    assert rows[1] == ["trunk", "e001"]
    assert rows[2] == ["trunk", "e002"]


def test_write_csv_zero_row_yields_header_only(tmp_path: Path) -> None:
    """A zero-row query writes a header-only file (not an empty file)."""
    emit_dir = build_test_emit(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        sql = 'SELECT record_id FROM "records__entity" WHERE 1=0'
        count = write_csv(emit, "empty_table", sql, out_dir)

    assert count == 0
    csv_path = out_dir / "empty_table.csv"
    assert csv_path.exists()
    rows = list(csv.reader(csv_path.read_text(encoding="utf-8").splitlines()))
    assert len(rows) == 1
    assert rows[0] == ["record_id"]


def test_write_csv_returns_row_count(tmp_path: Path) -> None:
    """write_csv returns the exact number of data rows (not counting header)."""
    emit_dir = build_test_emit(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        sql = 'SELECT record_id FROM "history" ORDER BY record_id'
        count = write_csv(emit, "history_table", sql, out_dir)

    assert count == 3


def test_write_csv_filename_is_table_name_dot_csv(tmp_path: Path) -> None:
    """The output file is named <table_name>.csv."""
    emit_dir = build_test_emit(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        sql = 'SELECT record_id FROM "records__entity" ORDER BY record_id'
        write_csv(emit, "my_custom_table", sql, out_dir)

    assert (out_dir / "my_custom_table.csv").exists()


def test_write_csv_failure_raises_export_runtime_error(tmp_path: Path) -> None:
    """A write failure (non-existent output directory) raises ExportRuntimeError."""
    emit_dir = build_test_emit(tmp_path)
    bad_dir = tmp_path / "nonexistent" / "deeply" / "nested"

    with open_emit(emit_dir) as emit:
        sql = 'SELECT record_id FROM "records__entity"'
        with pytest.raises(ExportRuntimeError):
            write_csv(emit, "t", sql, bad_dir)


def test_write_csv_null_value_renders_empty_field(tmp_path: Path) -> None:
    """A NULL column value renders as an empty CSV field (the _format_value
    invalid-scalar -> None branch), never the string 'None'."""
    emit_dir = build_test_emit(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        sql = (
            "SELECT record_id, CAST(NULL AS VARCHAR) AS null_col"
            ' FROM "records__entity" ORDER BY record_id'
        )
        count = write_csv(emit, "null_table", sql, out_dir)

    assert count == 2
    csv_path = out_dir / "null_table.csv"
    rows = list(csv.reader(csv_path.read_text(encoding="utf-8").splitlines()))
    assert rows[0] == ["record_id", "null_col"]
    assert rows[1] == ["e001", ""]
    assert rows[2] == ["e002", ""]


def test_write_csv_query_failure_raises_export_runtime_error(tmp_path: Path) -> None:
    """A query-execution failure (bad column) raises ExportRuntimeError, per
    the documented contract — not the reader's RunDatabaseError."""
    emit_dir = build_test_emit(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        sql = 'SELECT nonexistent_column FROM "records__entity"'
        with pytest.raises(ExportRuntimeError):
            write_csv(emit, "t", sql, out_dir)


# ---------------------------------------------------------------------------
# Pinned temporal text forms — DATE / TIME / TIMESTAMPTZ / INTERVAL
# ---------------------------------------------------------------------------


def _rows(csv_path: Path) -> list[list[str]]:
    return list(csv.reader(csv_path.read_text(encoding="utf-8").splitlines()))


def test_write_csv_date_form(tmp_path: Path) -> None:
    """A DATE column serializes as YYYY-MM-DD."""
    emit_dir = build_test_emit(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        write_csv(emit, "t", "SELECT DATE '2024-01-15' AS d", out_dir)

    assert _rows(out_dir / "t.csv") == [["d"], ["2024-01-15"]]


def test_write_csv_time_form(tmp_path: Path) -> None:
    """A TIME column serializes as HH:MM:SS.ffffff, fixed six-digit µs."""
    emit_dir = build_test_emit(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        write_csv(emit, "t", "SELECT TIME '13:45:30.123456' AS t", out_dir)

    assert _rows(out_dir / "t.csv") == [["t"], ["13:45:30.123456"]]


def test_write_csv_timestamptz_form(tmp_path: Path) -> None:
    """A TIMESTAMPTZ column serializes as local wall clock + offset in the
    pinned (anchor) zone, regardless of what zone the value was authored in."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from fabulexa_forge.anchor import EffectiveAnchor

    emit_dir = build_test_emit(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    anchor = EffectiveAnchor(
        start_instant=datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        timezone=ZoneInfo("America/New_York"),
    )
    with open_emit(emit_dir) as emit:
        pin_session_timezone(emit, anchor)
        write_csv(
            emit,
            "t",
            "SELECT TIMESTAMPTZ '2024-01-15 13:45:30.123456-05:00' AS tz",
            out_dir,
        )

    assert _rows(out_dir / "t.csv") == [["tz"], ["2024-01-15 13:45:30.123456-05:00"]]


def test_write_csv_interval_form_positive_over_24h(tmp_path: Path) -> None:
    """A positive INTERVAL serializes as H:MM:SS.ffffff with an unbounded
    (>24) hours field and no day component."""
    emit_dir = build_test_emit(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        sql = "SELECT INTERVAL '90000123456 microseconds' AS iv"
        write_csv(emit, "t", sql, out_dir)

    assert _rows(out_dir / "t.csv") == [["iv"], ["25:00:00.123456"]]


def test_write_csv_interval_form_negative(tmp_path: Path) -> None:
    """A negative INTERVAL keeps its sign in the rendered text form."""
    emit_dir = build_test_emit(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        sql = "SELECT -INTERVAL '90000123456 microseconds' AS iv"
        write_csv(emit, "t", sql, out_dir)

    assert _rows(out_dir / "t.csv") == [["iv"], ["-25:00:00.123456"]]


def test_write_csv_new_types_null_renders_empty_field(tmp_path: Path) -> None:
    """NULL DATE / TIME / TIMESTAMPTZ / INTERVAL render as today's empty
    NULL field, exactly like every other type's NULL."""
    emit_dir = build_test_emit(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        sql = (
            "SELECT CAST(NULL AS DATE) AS d, CAST(NULL AS TIME) AS t,"
            " CAST(NULL AS TIMESTAMPTZ) AS tz, CAST(NULL AS INTERVAL) AS iv"
        )
        write_csv(emit, "t", sql, out_dir)

    assert _rows(out_dir / "t.csv") == [["d", "t", "tz", "iv"], ["", "", "", ""]]


def test_write_csv_timestamp_form_unchanged(tmp_path: Path) -> None:
    """The existing (non-tz) TIMESTAMP form stays byte-identical."""
    emit_dir = build_test_emit(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        sql = "SELECT TIMESTAMP '2024-01-15 13:45:30.123456' AS ts"
        write_csv(emit, "t", sql, out_dir)

    assert _rows(out_dir / "t.csv") == [["ts"], ["2024-01-15 13:45:30.123456"]]


def test_write_csv_double_form_unchanged(tmp_path: Path) -> None:
    """The existing DOUBLE form stays byte-identical."""
    emit_dir = build_test_emit(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        sql = "SELECT CAST(3.14 AS DOUBLE) AS d"
        write_csv(emit, "t", sql, out_dir)

    assert _rows(out_dir / "t.csv") == [["d"], ["3.14"]]
