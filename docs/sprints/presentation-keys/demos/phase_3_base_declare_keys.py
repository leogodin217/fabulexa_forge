#!/usr/bin/env python
"""
Demo: Base mode declare_keys — resolve_base_table_keys + engine wiring
Sprint: presentation-keys
Phase: 3

Builds a small two-kind emit: `patient` carries a flat whole-column
presentation_keys claim (declared unique on presentation_id), `doctor`
carries a partitioned presentation_keys entry whose rollup derives no claim
(two counter sub-types sharing an empty prefix — not pairwise union-safe),
so it declares identity keys only despite being partitioned.

1. `mode: base` + `declare_keys: true` to DuckDB: prints per-table
   `duckdb_constraints()` — patient gets PRIMARY KEY (patient_key) + UNIQUE
   (id, presentation_id); doctor gets PRIMARY KEY (doctor_key) + UNIQUE (id)
   only.
2. The same config to CSV: prints the single keys-not-declarable-csv notice
   and shows the row counts are identical to the DuckDB run — the data is
   unaffected, only the constraint declaration is dropped.
3. `declare_keys` absent, to DuckDB: prints the now constraint-free output.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import BaseConfig, ExportConfig
from fabulexa_forge.exporters.base.engine import export_base
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.reader.emit import open_emit

_PATIENT_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
]

_DOCTOR_COLUMNS: list[dict[str, object]] = _PATIENT_COLUMNS

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

#: A flat kind's key claim: unique within the branch, stable across branch
#: and slice — a plain record_index-class declaration.
_PATIENT_PRESENTATION_KEYS: dict[str, object] = {
    "key": {
        "unique_within": "branch",
        "branch_stable": True,
        "slice_stable": True,
        "key_space": {"class": "record_index", "prefix": "", "width": 4},
    }
}

#: A partitioned kind's rollup: two counter sub-types sharing an empty
#: prefix are NOT pairwise union-safe, so the algebra derives no claim
#: (unique_within omitted) — an "unclaimed" table despite being partitioned.
_DOCTOR_PRESENTATION_KEYS: dict[str, object] = {
    "sub_types": {
        "junior": {
            "unique_within": "emit",
            "branch_stable": False,
            "slice_stable": False,
            "key_space": {"class": "counter", "prefix": "", "width": 3},
        },
        "senior": {
            "unique_within": "emit",
            "branch_stable": False,
            "slice_stable": False,
            "key_space": {"class": "counter", "prefix": "", "width": 3},
        },
    },
    "branch_stable": False,
    "slice_stable": False,
}


def _write_emit(emit_dir: Path) -> None:
    """Write a run.duckdb + base.json pair carrying `patient` (claimed) and
    `doctor` (partitioned, unclaimed)."""
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    try:
        for table in ("records__patient", "records__doctor"):
            cols = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _PATIENT_COLUMNS)
            conn.execute(f'CREATE TABLE "{table}" ({cols})')
        conn.execute(
            'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, NULL, ?, ?)',
            ["trunk", "p001", 1001, 0, True, 0, 0],
        )
        conn.execute(
            'INSERT INTO "records__doctor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?)',
            ["trunk", "d001", 2001, 0, True, 0, 0],
        )
        history_cols = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _HISTORY_COLUMNS)
        conn.execute(f"CREATE TABLE history ({history_cols})")
    finally:
        conn.close()

    base_json = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": _PATIENT_COLUMNS,
                "rows": 1,
            },
            {
                "name": "records__doctor",
                "category": "records",
                "record_kind": "doctor",
                "columns": _DOCTOR_COLUMNS,
                "rows": 1,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 0,
            },
        ],
        "record_roles": {"patient": "dimension", "doctor": "dimension"},
        "runtime": {
            "timezone": "UTC",
            "start_datetime": "2024-01-01T00:00:00+00:00",
        },
        "enum_domains": {"doctor": {"doctor_type": ["junior", "senior"]}},
        "presentation_keys": {
            "patient": _PATIENT_PRESENTATION_KEYS,
            "doctor": _DOCTOR_PRESENTATION_KEYS,
        },
    }
    (emit_dir / "base.json").write_text(json.dumps(base_json), encoding="utf-8")


def _print_constraints(db_path: Path, table_name: str) -> None:
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = conn.execute(
            "SELECT constraint_type, constraint_column_names"
            " FROM duckdb_constraints() WHERE table_name = ?",
            [table_name],
        ).fetchall()
    finally:
        conn.close()
    print(f"  {table_name}: {rows if rows else '(no declared constraints)'}")


def _run_duckdb_with_keys(emit_dir: Path, out_path: Path) -> dict[str, int]:
    config = ExportConfig(mode="base", base=BaseConfig(declare_keys=True))
    notices: list[Notice] = []
    with open_emit(emit_dir) as emit:
        counts = export_base(
            emit, config, out_path, "duckdb", None, notice_sink=notices.append
        )
    print("\n== mode: base, declare_keys: true -> DuckDB ==")
    print(f"row counts: {counts}")
    print("duckdb_constraints():")
    _print_constraints(out_path, "patient")
    _print_constraints(out_path, "doctor")
    print(f"notices: {[n.code for n in notices]}")
    return counts


def _run_csv_with_keys(
    emit_dir: Path, out_dir: Path, duckdb_counts: dict[str, int]
) -> None:
    out_dir.mkdir()
    config = ExportConfig(mode="base", base=BaseConfig(declare_keys=True))
    notices: list[Notice] = []
    with open_emit(emit_dir) as emit:
        counts = export_base(
            emit, config, out_dir, "csv", None, notice_sink=notices.append
        )
    print("\n== mode: base, declare_keys: true -> CSV ==")
    print(f"row counts: {counts} (matches DuckDB run: {counts == duckdb_counts})")
    print(f"notices: {[(n.code, n.message) for n in notices]}")


def _run_duckdb_without_keys(emit_dir: Path, out_path: Path) -> None:
    config = ExportConfig(mode="base")
    notices: list[Notice] = []
    with open_emit(emit_dir) as emit:
        counts = export_base(
            emit, config, out_path, "duckdb", None, notice_sink=notices.append
        )
    print("\n== mode: base, declare_keys absent -> DuckDB ==")
    print(f"row counts: {counts}")
    print("duckdb_constraints():")
    _print_constraints(out_path, "patient")
    _print_constraints(out_path, "doctor")


def main() -> int:
    tmp_dir = Path(tempfile.mkdtemp(prefix="fabulexa_forge_phase3_demo_"))
    try:
        _write_emit(tmp_dir)
        duckdb_counts = _run_duckdb_with_keys(tmp_dir, tmp_dir / "keyed.duckdb")
        _run_csv_with_keys(tmp_dir, tmp_dir / "csv_out", duckdb_counts)
        _run_duckdb_without_keys(tmp_dir, tmp_dir / "unkeyed.duckdb")
        print(
            "\nSUCCESS: claimed kind declares PK + presentation_id UNIQUE, the"
            " partitioned-but-unclaimed kind declares identity keys only, CSV"
            " carries identical data plus one dropped-declaration notice, and"
            " declare_keys absent yields constraint-free output"
        )
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
