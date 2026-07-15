"""Tests for open_emit: file-location, JSON-parse, version gate, structural floor,
and DuckDB open.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.reader import (
    Emit,
    EmitNotFoundError,
    RunDatabaseError,
    SidecarParseError,
    SidecarStructureError,
    UnsupportedBaseFormatVersionError,
    open_emit,
)

from ._emit_helpers import _minimal_sidecar, write_emit

# ---------------------------------------------------------------------------
# EmitNotFoundError cases
# ---------------------------------------------------------------------------


def test_missing_emit_dir_raises(tmp_path: Path) -> None:
    """A nonexistent emit_dir raises EmitNotFoundError."""
    nonexistent = tmp_path / "no_such_dir"
    with pytest.raises(EmitNotFoundError):
        open_emit(nonexistent)


def test_missing_run_duckdb_raises(tmp_path: Path) -> None:
    """An emit_dir with base.json but no run.duckdb raises EmitNotFoundError."""
    (tmp_path / "base.json").write_text(
        json.dumps(_minimal_sidecar()), encoding="utf-8"
    )
    with pytest.raises(EmitNotFoundError):
        open_emit(tmp_path)


def test_missing_base_json_raises(tmp_path: Path) -> None:
    """An emit_dir with run.duckdb but no base.json raises EmitNotFoundError."""
    import duckdb

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.close()
    with pytest.raises(EmitNotFoundError):
        open_emit(tmp_path)


# ---------------------------------------------------------------------------
# SidecarParseError
# ---------------------------------------------------------------------------


def test_invalid_json_raises_parse_error(tmp_path: Path) -> None:
    """A base.json that is not valid JSON raises SidecarParseError."""
    import duckdb

    (tmp_path / "base.json").write_text("{not valid json", encoding="utf-8")
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.close()
    with pytest.raises(SidecarParseError):
        open_emit(tmp_path)


# ---------------------------------------------------------------------------
# Version gate precedes structure and DB open
# ---------------------------------------------------------------------------


def test_unsupported_version_raises_before_db_open(tmp_path: Path) -> None:
    """A base_format_version: 99 emit raises UnsupportedBaseFormatVersionError
    before any DuckDB open (version gate precedes structure and DB open).
    """
    sidecar = _minimal_sidecar(base_format_version=99)
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    # Write garbage bytes for run.duckdb — if the DB were opened first, this would
    # produce RunDatabaseError. Getting UnsupportedBaseFormatVersionError confirms
    # the gate runs before the DB open.
    (tmp_path / "run.duckdb").write_bytes(b"not a db")
    with pytest.raises(UnsupportedBaseFormatVersionError) as exc_info:
        open_emit(tmp_path)
    assert exc_info.value.found_version == 99


# ---------------------------------------------------------------------------
# SidecarStructureError
# ---------------------------------------------------------------------------


def test_below_floor_sidecar_raises_structure_error(tmp_path: Path) -> None:
    """A base.json that is valid JSON but below the structural floor raises
    SidecarStructureError.
    """
    import duckdb

    # A table missing required 'columns' field
    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [{"name": "firings", "category": "fixed", "rows": 0}],
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.close()
    with pytest.raises(SidecarStructureError):
        open_emit(tmp_path)


# ---------------------------------------------------------------------------
# RunDatabaseError for garbage DB bytes
# ---------------------------------------------------------------------------


def test_garbage_db_raises_run_database_error(tmp_path: Path) -> None:
    """A run.duckdb with non-DuckDB garbage bytes raises RunDatabaseError."""
    write_emit(tmp_path, garbage_db=True)
    with pytest.raises(RunDatabaseError):
        open_emit(tmp_path)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_well_formed_emit_opens(tmp_path: Path) -> None:
    """A well-formed emit returns an open Emit with correct emit_dir and sidecar."""
    write_emit(tmp_path)
    emit = open_emit(tmp_path)
    try:
        assert emit.emit_dir == tmp_path
        assert emit.sidecar is not None
        assert emit.sidecar.base_format_version == SUPPORTED_BASE_FORMAT_VERSION
    finally:
        emit.close()


def test_extra_sibling_files_ignored(tmp_path: Path) -> None:
    """Extra files alongside base.json and run.duckdb do not cause errors."""
    write_emit(tmp_path)
    (tmp_path / "README.txt").write_text("extra file", encoding="utf-8")
    (tmp_path / "bundle.json").write_text("{}", encoding="utf-8")
    with open_emit(tmp_path) as emit:
        assert emit.emit_dir == tmp_path


def test_emit_exposes_emit_dir_and_sidecar(tmp_path: Path) -> None:
    """Emit.emit_dir and Emit.sidecar are exposed after open_emit."""
    write_emit(tmp_path)
    with open_emit(tmp_path) as emit:
        assert isinstance(emit, Emit)
        assert emit.emit_dir == tmp_path
        tables = emit.sidecar.tables()
        assert len(tables) >= 1
