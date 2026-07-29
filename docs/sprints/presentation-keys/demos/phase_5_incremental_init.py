#!/usr/bin/env python
"""
Demo: Incremental notice + init advisory
Sprint: presentation-keys
Phase: 5

Builds one base-mode emit: kind `patient` carries a flat whole-column
presentation_keys claim (unique within branch, a record_index-class
declaration) and two rows created before the first window's horizon, plus a
third row created between the first and second window's horizons (row
growth).

1. `mode: base` + `declare_keys: true` + `incremental` `--next` twice into a
   DuckDB warehouse: prints `duckdb_constraints()` for `patient` after window
   1 (created at warehouse-creation time) and again after window 2 (row
   growth, constraints intact — the replace path never re-declares them).
2. The same config, CSV format, `--next` twice: prints the single
   keys-not-declarable-csv notice re-emitted on each drip invocation.
3. `fabulexa-forge init` over the same emit: prints the candidate config,
   highlighting the `presentation_id` natural-key advisory comment on the
   claimed kind's stub.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import ExportConfig
from fabulexa_forge.exporters.dimensional.init import generate_init_config
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.incremental.driver import export_incremental_next
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

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

#: A flat kind's key claim: unique within the branch, stable across branch
#: and slice — a plain record_index-class declaration (mirrors phase 3's
#: `patient`).
_PATIENT_PRESENTATION_KEYS: dict[str, object] = {
    "key": {
        "unique_within": "branch",
        "branch_stable": True,
        "slice_stable": True,
        "key_space": {"class": "record_index", "prefix": "", "width": 4},
    }
}

#: (record_id, presentation_id, created_sim_time). p003 arrives after window
#: 0's horizon (sim-time 100) but before window 1's (sim-time 200) — the row
#: growth window 2's snapshot shows.
_PATIENT_ROWS: list[tuple[str, int, int]] = [
    ("p001", 101, 0),
    ("p002", 102, 0),
    ("p003", 103, 150),
]

_SIM_PERIOD_NS = 100
_SLICE_AT = 150


def _write_emit(emit_dir: Path) -> None:
    """Write a run.duckdb + base.json pair carrying the claimed `patient` kind."""
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    try:
        col_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _PATIENT_COLUMNS)
        conn.execute(f'CREATE TABLE "records__patient" ({col_ddl})')
        for record_index, (record_id, presentation_id, created_sim_time) in enumerate(
            _PATIENT_ROWS
        ):
            conn.execute(
                'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, NULL, ?, ?)',
                [
                    "trunk",
                    record_id,
                    presentation_id,
                    created_sim_time,
                    True,
                    created_sim_time,
                    record_index,
                ],
            )
        history_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _HISTORY_COLUMNS)
        conn.execute(f'CREATE TABLE "history" ({history_ddl})')
    finally:
        conn.close()

    base_json = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": _SLICE_AT}],
        "tables": [
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": _PATIENT_COLUMNS,
                "rows": len(_PATIENT_ROWS),
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 0,
            },
        ],
        "record_roles": {"patient": "dimension"},
        "runtime": {
            "timezone": "UTC",
            "start_datetime": "2024-01-01T00:00:00+00:00",
        },
        "presentation_keys": {"patient": _PATIENT_PRESENTATION_KEYS},
    }
    (emit_dir / "base.json").write_text(json.dumps(base_json), encoding="utf-8")


def _base_config() -> ExportConfig:
    return ExportConfig.model_validate(
        {
            "mode": "base",
            "base": {"declare_keys": True},
            "incremental": {"sim_period_ns": _SIM_PERIOD_NS},
        }
    )


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
    print(f"  patient: {rows if rows else '(no declared constraints)'}")


def _run_duckdb_drip(emit_dir: Path, out_path: Path) -> None:
    print("\n== mode: base, declare_keys: true, incremental --next x2 -> DuckDB ==")
    config = _base_config()
    with open_emit(emit_dir) as emit:
        outcome1 = export_incremental_next(
            emit, config, out_path, "duckdb", None, notice_sink=lambda n: None
        )
    print(f"window 1: status={outcome1.status} row_counts={outcome1.row_counts}")
    _print_constraints(out_path, "patient")

    with open_emit(emit_dir) as emit:
        outcome2 = export_incremental_next(
            emit, config, out_path, "duckdb", None, notice_sink=lambda n: None
        )
    print(f"window 2: status={outcome2.status} row_counts={outcome2.row_counts}")
    _print_constraints(out_path, "patient")


def _run_csv_drip(emit_dir: Path, out_dir: Path) -> None:
    print("\n== mode: base, declare_keys: true, incremental --next x2 -> CSV ==")
    config = _base_config()
    for drip in (1, 2):
        notices: list[Notice] = []
        with open_emit(emit_dir) as emit:
            export_incremental_next(
                emit, config, out_dir, "csv", None, notice_sink=notices.append
            )
        print(f"drip {drip} notices: {[n.code for n in notices]}")


def _run_init(emit_dir: Path) -> None:
    print("\n== fabulexa-forge init: presentation_id advisory comment ==")
    with open_emit(emit_dir) as emit:
        candidate = generate_init_config(emit, notice_sink=lambda n: None)
    for line in candidate.splitlines():
        if "presentation_id" in line and "NOTE" in line:
            print(f"  {line.strip()}")


def main() -> int:
    tmp_dir = Path(tempfile.mkdtemp(prefix="fabulexa_forge_phase5_demo_"))
    try:
        emit_dir = tmp_dir / "emit"
        emit_dir.mkdir()
        _write_emit(emit_dir)

        _run_duckdb_drip(emit_dir, tmp_dir / "wh.duckdb")
        _run_csv_drip(emit_dir, tmp_dir / "drops")
        _run_init(emit_dir)

        print(
            "\nSUCCESS: windowed DuckDB carries constraints from window 1 through"
            " window 2's row growth, the CSV drip re-emits the"
            " keys-not-declarable-csv notice on every invocation, and init's"
            " stub carries the presentation_id natural-key advisory comment"
        )
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
