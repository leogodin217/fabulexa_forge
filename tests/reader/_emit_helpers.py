"""Reusable emit-construction helpers for reader tests.

Plain functions (not fixtures) so test modules can call them directly with
custom arguments. The `emit_dir` fixture in conftest.py wraps `write_emit`
for the common minimal-emit case.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
from _support.sidecar_builder import write_emit as _write_sidecar

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION

_SIDECAR_TOP_LEVEL_KEYS = frozenset({"base_format_version", "branches", "tables"})


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
    records_shape_valid: bool = True,
) -> Path:
    """Write a base.json + run.duckdb pair into tmp_path and return tmp_path.

    Routes the base.json write through `_support.sidecar_builder.write_emit`
    (the one sidecar authority): the incoming `sidecar` dict is decomposed into
    its tables/branches/base_format_version/extra-blocks components.

    Args:
        tmp_path: Directory to write the emit artifacts into.
        sidecar: The base.json content as a dict; uses a minimal valid sidecar
            if None.
        db_tables: Mapping of {table_name: CREATE TABLE DDL} for tables to create
            in run.duckdb; creates an empty DuckDB if None.
        garbage_db: If True, write random bytes to run.duckdb instead of a real DB.
        records_shape_valid: Forwarded to `_support.sidecar_builder.write_emit`.
            False opts a caller's deliberately mis-shaped records table out of
            the records-shape construction-time assertion.

    Returns:
        tmp_path (the emit directory).
    """
    if sidecar is None:
        sidecar = _minimal_sidecar()

    extra = {
        key: value
        for key, value in sidecar.items()
        if key not in _SIDECAR_TOP_LEVEL_KEYS
    }
    _write_sidecar(
        tmp_path,
        tables=sidecar["tables"],  # type: ignore[arg-type]
        branches=sidecar.get("branches"),  # type: ignore[arg-type]
        extra=extra or None,
        base_format_version=sidecar.get("base_format_version"),  # type: ignore[arg-type]
        records_shape_valid=records_shape_valid,
    )

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
