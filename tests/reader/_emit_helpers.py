"""Reusable emit-construction helpers for reader tests.

Plain functions (not fixtures) so test modules can call them directly with
custom arguments. The `emit_dir` fixture in conftest.py wraps `write_emit`
for the common minimal-emit case.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION


def _minimal_sidecar(
    base_format_version: object = None,
    extra_tables: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build a minimal valid base.json dict.

    Args:
        base_format_version: Override the base_format_version value.
        extra_tables: Additional tables beyond the default firings table.

    Returns:
        A dict suitable for writing as base.json.
    """
    version = (
        base_format_version
        if base_format_version is not None
        else SUPPORTED_BASE_FORMAT_VERSION
    )
    tables: list[dict[str, object]] = [
        {
            "name": "firings",
            "category": "fixed",
            "columns": [
                {"name": "fork_path", "type": "VARCHAR"},
                {"name": "sim_time", "type": "BIGINT"},
            ],
            "rows": 0,
        }
    ]
    if extra_tables:
        tables.extend(extra_tables)
    return {
        "base_format_version": version,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": tables,
    }


def write_emit(
    tmp_path: Path,
    sidecar: dict[str, object] | None = None,
    db_tables: dict[str, str] | None = None,
    garbage_db: bool = False,
) -> Path:
    """Write a base.json + run.duckdb pair into tmp_path and return tmp_path.

    Args:
        tmp_path: Directory to write the emit artifacts into.
        sidecar: The base.json content as a dict; uses a minimal valid sidecar
            if None.
        db_tables: Mapping of {table_name: CREATE TABLE DDL} for tables to create
            in run.duckdb; creates an empty DuckDB if None.
        garbage_db: If True, write random bytes to run.duckdb instead of a real DB.

    Returns:
        tmp_path (the emit directory).
    """
    if sidecar is None:
        sidecar = _minimal_sidecar()

    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")

    db_path = tmp_path / "run.duckdb"
    if garbage_db:
        db_path.write_bytes(b"this is not a duckdb file\x00\xff")
    else:
        conn = duckdb.connect(str(db_path))
        if db_tables:
            for ddl in db_tables.values():
                conn.execute(ddl)
        conn.close()

    return tmp_path
