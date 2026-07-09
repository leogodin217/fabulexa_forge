"""Tests for incremental/cursor.py — read_cursor, write_csv_cursor.

All IO tests use tmp_path. DuckDB tests build minimal warehouse files.
CSV tests build directory layouts.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from fabulexa_export.errors import IncrementalCursorInvalid
from fabulexa_export.incremental.cursor import (
    _CURRENT_CURSOR_FORMAT_VERSION,
    Cursor,
    read_cursor,
    write_csv_cursor,
)

_WINDOW_ZERO_LABEL = "w00000_2024-01-01"
_FINGERPRINT = "a" * 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_warehouse(path: Path, with_meta: bool = True, num_windows: int = 0) -> None:
    """Build a minimal DuckDB warehouse with optional bookkeeping tables.

    Args:
        path: Output .duckdb file path.
        with_meta: Whether to create _export_meta and _export_windows.
        num_windows: Number of window rows to insert into _export_windows.
    """
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE some_table (id VARCHAR)")
    if with_meta:
        conn.execute(
            "CREATE TABLE _export_meta (cursor_format_version INTEGER NOT NULL, fingerprint VARCHAR NOT NULL)"
        )
        conn.execute(
            "INSERT INTO _export_meta VALUES (?, ?)",
            [_CURRENT_CURSOR_FORMAT_VERSION, _FINGERPRINT],
        )
        conn.execute(
            "CREATE TABLE _export_windows (window_index INTEGER NOT NULL, label VARCHAR NOT NULL, start_ns BIGINT NOT NULL, end_ns BIGINT NOT NULL)"
        )
        for i in range(num_windows):
            conn.execute(
                "INSERT INTO _export_windows VALUES (?, ?, ?, ?)",
                [i, f"w{i:05d}", i * 100, (i + 1) * 100],
            )
    conn.close()


def _make_empty_warehouse(path: Path) -> None:
    """Build a DuckDB file with no tables."""
    conn = duckdb.connect(str(path))
    conn.close()


# ---------------------------------------------------------------------------
# DuckDB fresh states
# ---------------------------------------------------------------------------


def test_duckdb_fresh_absent_file(tmp_path: Path) -> None:
    """Absent .duckdb file → None (fresh target)."""
    result = read_cursor(tmp_path / "warehouse.duckdb", "duckdb", _WINDOW_ZERO_LABEL)
    assert result is None


def test_duckdb_fresh_empty_catalog(tmp_path: Path) -> None:
    """Empty catalog (zero tables/views) → None (fresh target)."""
    wh = tmp_path / "warehouse.duckdb"
    _make_empty_warehouse(wh)
    result = read_cursor(wh, "duckdb", _WINDOW_ZERO_LABEL)
    assert result is None


def test_duckdb_nonempty_catalog_without_export_meta_raises(tmp_path: Path) -> None:
    """Non-empty catalog without _export_meta → IncrementalCursorInvalid."""
    wh = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(wh))
    conn.execute("CREATE TABLE author_table (id VARCHAR)")
    conn.close()

    with pytest.raises(IncrementalCursorInvalid, match="_export_meta"):
        read_cursor(wh, "duckdb", _WINDOW_ZERO_LABEL)


# ---------------------------------------------------------------------------
# DuckDB read — cursor content
# ---------------------------------------------------------------------------


def test_duckdb_read_returns_cursor_with_correct_next_index(tmp_path: Path) -> None:
    """_export_meta + 3 window rows → next_window_index = 3."""
    wh = tmp_path / "warehouse.duckdb"
    _make_warehouse(wh, with_meta=True, num_windows=3)

    result = read_cursor(wh, "duckdb", _WINDOW_ZERO_LABEL)
    assert result is not None
    assert result.fingerprint == _FINGERPRINT
    assert result.cursor_format_version == _CURRENT_CURSOR_FORMAT_VERSION
    assert result.next_window_index == 3


def test_duckdb_meta_present_zero_window_rows_next_index_is_0(tmp_path: Path) -> None:
    """_export_meta present but _export_windows empty → next_window_index = 0.

    The MAX(window_index) query returns a NULL row for an empty table; that
    defaults the drip position to window 0 rather than raising.
    """
    wh = tmp_path / "warehouse.duckdb"
    _make_warehouse(wh, with_meta=True, num_windows=0)

    result = read_cursor(wh, "duckdb", _WINDOW_ZERO_LABEL)
    assert result is not None
    assert result.fingerprint == _FINGERPRINT
    assert result.cursor_format_version == _CURRENT_CURSOR_FORMAT_VERSION
    assert result.next_window_index == 0


def test_duckdb_read_single_window_next_index_is_1(tmp_path: Path) -> None:
    """One window row → next_window_index = 1."""
    wh = tmp_path / "warehouse.duckdb"
    _make_warehouse(wh, with_meta=True, num_windows=1)

    result = read_cursor(wh, "duckdb", _WINDOW_ZERO_LABEL)
    assert result is not None
    assert result.next_window_index == 1


def test_duckdb_unknown_cursor_format_version_raises(tmp_path: Path) -> None:
    """Unknown cursor_format_version in _export_meta → IncrementalCursorInvalid."""
    wh = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(wh))
    conn.execute("CREATE TABLE some_table (id VARCHAR)")
    conn.execute(
        "CREATE TABLE _export_meta (cursor_format_version INTEGER NOT NULL, fingerprint VARCHAR NOT NULL)"
    )
    conn.execute("INSERT INTO _export_meta VALUES (999, 'abc')")
    conn.execute(
        "CREATE TABLE _export_windows (window_index INTEGER NOT NULL, label VARCHAR NOT NULL, start_ns BIGINT NOT NULL, end_ns BIGINT NOT NULL)"
    )
    conn.close()

    with pytest.raises(IncrementalCursorInvalid, match="cursor_format_version"):
        read_cursor(wh, "duckdb", _WINDOW_ZERO_LABEL)


# ---------------------------------------------------------------------------
# CSV fresh states
# ---------------------------------------------------------------------------


def test_csv_fresh_absent_out(tmp_path: Path) -> None:
    """Absent out directory → None (fresh target)."""
    result = read_cursor(tmp_path / "drops", "csv", _WINDOW_ZERO_LABEL)
    assert result is None


def test_csv_fresh_only_dot_entries(tmp_path: Path) -> None:
    """Only dot-entries (hidden files) → None (fresh target)."""
    drops = tmp_path / "drops"
    drops.mkdir()
    (drops / ".fabexport-cursor.json").write_text("{}")
    result = read_cursor(drops, "csv", _WINDOW_ZERO_LABEL)
    assert result is None


def test_csv_fresh_only_tmp_leftover(tmp_path: Path) -> None:
    """Only .tmp_* staging directory → treated as no non-hidden entries → None."""
    # .tmp_* starts with '.', so it is treated as hidden by _list_non_hidden
    # Actually .tmp_ does not start with '.', let me re-check the spec.
    # Per spec: "no non-hidden entries (dot-entries never count)"
    # .tmp_* starts with '.', so it IS hidden — correct.
    drops = tmp_path / "drops"
    drops.mkdir()
    tmp_dir = drops / ".tmp_w00000_2024-01-01"
    tmp_dir.mkdir()
    result = read_cursor(drops, "csv", _WINDOW_ZERO_LABEL)
    assert result is None


def test_csv_crash_recovery_correct_label(tmp_path: Path) -> None:
    """Exactly one non-hidden dir named window_zero_label → None (restart at 0)."""
    drops = tmp_path / "drops"
    drops.mkdir()
    (drops / _WINDOW_ZERO_LABEL).mkdir()

    result = read_cursor(drops, "csv", _WINDOW_ZERO_LABEL)
    assert result is None


def test_csv_crash_recovery_wrong_label_raises(tmp_path: Path) -> None:
    """Exactly one non-hidden dir with different label → IncrementalCursorInvalid."""
    drops = tmp_path / "drops"
    drops.mkdir()
    (drops / "w00000_2024-01-02").mkdir()  # different from window_zero_label

    with pytest.raises(IncrementalCursorInvalid, match="crash-recovery"):
        read_cursor(drops, "csv", _WINDOW_ZERO_LABEL)


def test_csv_two_entries_no_cursor_file_raises(tmp_path: Path) -> None:
    """Two non-hidden entries without cursor file → IncrementalCursorInvalid."""
    drops = tmp_path / "drops"
    drops.mkdir()
    (drops / "w00000_2024-01-01").mkdir()
    (drops / "w00001_2024-01-02").mkdir()

    with pytest.raises(IncrementalCursorInvalid, match="cursor"):
        read_cursor(drops, "csv", _WINDOW_ZERO_LABEL)


# ---------------------------------------------------------------------------
# CSV read/write round-trip
# ---------------------------------------------------------------------------


def test_csv_write_read_roundtrip(tmp_path: Path) -> None:
    """write_csv_cursor then read_cursor returns an identical Cursor."""
    drops = tmp_path / "drops"
    drops.mkdir()

    # Write a window drop dir so the cursor file is not the sole non-hidden entry
    (drops / _WINDOW_ZERO_LABEL).mkdir()

    cursor = Cursor(
        cursor_format_version=_CURRENT_CURSOR_FORMAT_VERSION,
        fingerprint=_FINGERPRINT,
        next_window_index=1,
    )
    write_csv_cursor(drops, cursor)

    result = read_cursor(drops, "csv", _WINDOW_ZERO_LABEL)
    assert result == cursor


def test_csv_cursor_json_keys_are_exact_field_names(tmp_path: Path) -> None:
    """Cursor JSON uses exactly the Cursor field names as keys."""
    drops = tmp_path / "drops"
    drops.mkdir()

    cursor = Cursor(
        cursor_format_version=_CURRENT_CURSOR_FORMAT_VERSION,
        fingerprint=_FINGERPRINT,
        next_window_index=5,
    )
    write_csv_cursor(drops, cursor)

    raw = json.loads((drops / ".fabexport-cursor.json").read_text())
    assert set(raw.keys()) == {
        "cursor_format_version",
        "fingerprint",
        "next_window_index",
    }
    assert raw["cursor_format_version"] == _CURRENT_CURSOR_FORMAT_VERSION
    assert raw["fingerprint"] == _FINGERPRINT
    assert raw["next_window_index"] == 5


def test_csv_unknown_cursor_format_version_raises(tmp_path: Path) -> None:
    """Cursor file with unknown cursor_format_version → IncrementalCursorInvalid."""
    drops = tmp_path / "drops"
    drops.mkdir()
    (drops / _WINDOW_ZERO_LABEL).mkdir()

    (drops / ".fabexport-cursor.json").write_text(
        json.dumps(
            {
                "cursor_format_version": 999,
                "fingerprint": _FINGERPRINT,
                "next_window_index": 1,
            }
        )
    )

    with pytest.raises(IncrementalCursorInvalid, match="cursor_format_version"):
        read_cursor(drops, "csv", _WINDOW_ZERO_LABEL)


def test_csv_unparseable_json_raises(tmp_path: Path) -> None:
    """Cursor file with invalid JSON → IncrementalCursorInvalid."""
    drops = tmp_path / "drops"
    drops.mkdir()
    (drops / _WINDOW_ZERO_LABEL).mkdir()
    (drops / ".fabexport-cursor.json").write_text("NOT JSON {{{")

    with pytest.raises(IncrementalCursorInvalid, match="invalid JSON"):
        read_cursor(drops, "csv", _WINDOW_ZERO_LABEL)
