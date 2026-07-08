"""Tests for write_csv.

Verifies: header + typed values written, zero-row yields header-only file,
return value is row count, ExportRuntimeError on failure.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from exporters._emit_fixtures import build_test_emit
from fabulexa_export.errors import ExportRuntimeError
from fabulexa_export.reader.emit import open_emit
from fabulexa_export.writers.csv import write_csv


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
