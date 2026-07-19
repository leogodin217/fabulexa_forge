#!/usr/bin/env python
"""
Demo: Source omission + rename rule (slice_only columns dropped from source export)

Sprint: slice-only-policy
Phase: 3

Builds a standalone emit with two units:
  - patient (tracked -> changelog genre): prop__status (tracked, drives the
    change-log's c/u/d fold) and prop__ssn (non-exempt slice_only).
  - employee (untracked, dimension role -> reference genre): prop__ssn and
    prop__note, BOTH non-exempt slice_only — the degenerate case where every
    property of a unit is slice_only.

Demonstrates, directly against build_source_plan / build_source_query_specs /
export_source:
  - prop__ssn (and prop__note) are absent from every genre's delivered column
    set — change-log, snapshot (change_delivery: snapshot), and reference —
    one 'slice-only-column-omitted' notice per unit x column, in plan order.
  - the change-log's row set (c/u/d count) is unchanged by the narrowing:
    prop__ssn is not history_tracked, so folding it or not never adds/removes
    an event — column-projection-only invariance (Invariant 3), checked
    against a directly-built baseline fold over the full (un-narrowed)
    property set.
  - the degenerate employee unit (every property omitted) still renders its
    one row — identity + lifecycle columns carried, never suppressed.
  - a `rename` entry naming the omitted prop__ssn column -> SourceRenameSliceOnly,
    naming the entry, the column, and the omission reason; renaming a
    delivered column (prop__status) still works.
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.derivations.row_state_events import build_row_state_events_sql
from fabulexa_forge.errors import SourceRenameSliceOnly
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.source.engine import (
    build_source_query_specs,
    export_source,
)
from fabulexa_forge.exporters.source.plan import build_source_plan
from fabulexa_forge.reader.emit import open_emit

# ---------------------------------------------------------------------------
# The emit: patient (changelog, one omitted column) + employee (reference,
# every column omitted — the degenerate case)
# ---------------------------------------------------------------------------

_PATIENT_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__ssn",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "slice_only",  # non-exempt: omitted everywhere it's read
    },
]

_EMPLOYEE_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__ssn",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "slice_only",  # degenerate: every property omitted
    },
    {
        "name": "prop__note",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "slice_only",
    },
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

_ANCHOR = EffectiveAnchor(
    start_instant=datetime(2024, 1, 1, tzinfo=timezone.utc), timezone=ZoneInfo("UTC")
)

YAML_BASE = """
mode: source
"""

YAML_SNAPSHOT = """
mode: source
source:
  change_delivery: snapshot
"""

YAML_RENAME_OMITTED = """
mode: source
source:
  rename:
    - table: records__patient
      columns:
        prop__ssn: ssn
"""

YAML_RENAME_DELIVERED = """
mode: source
source:
  rename:
    - table: records__patient
      columns:
        prop__status: status_code
"""


def _build_emit(emit_dir: Path) -> None:
    """Write the patient + employee run.duckdb + base.json emit."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    patient_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _PATIENT_COLUMNS)
    conn.execute(f'CREATE TABLE "records__patient" ({patient_ddl})')
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "p1", 0, True, 0, 0, "admitted", "111-22-3333"],
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "p2", 0, True, 100, 1, "discharged", "444-55-6666"],
    )

    employee_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _EMPLOYEE_COLUMNS)
    conn.execute(f'CREATE TABLE "records__employee" ({employee_ddl})')
    conn.execute(
        'INSERT INTO "records__employee" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "e1", 0, True, 0, 0, "999-88-7777", "confidential note"],
    )

    history_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _HISTORY_COLUMNS)
    conn.execute(f'CREATE TABLE "history" ({history_ddl})')
    for record_id, sim_time, value in (
        ("p1", 0, "admitted"),
        ("p2", 0, "admitted"),
        ("p2", 100, "discharged"),
    ):
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "patient", record_id, "status", sim_time, value],
        )
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "tables": [
            {
                "name": "records__patient",
                "category": "records",
                "columns": _PATIENT_COLUMNS,
                "rows": 2,
                "record_kind": "patient",
            },
            {
                "name": "records__employee",
                "category": "records",
                "columns": _EMPLOYEE_COLUMNS,
                "rows": 1,
                "record_kind": "employee",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 3,
            },
        ],
        "record_roles": {"employee": "dimension"},
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


class NoticeCollector:
    """Callable NoticeSink appending every received Notice to `self.notices`."""

    def __init__(self) -> None:
        self.notices: list[Notice] = []

    def __call__(self, notice: Notice) -> None:
        self.notices.append(notice)


def _fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def _read_csv_header(csv_path: Path) -> list[str]:
    with csv_path.open(newline="", encoding="utf-8") as f:
        return next(csv.reader(f))


def _read_csv_row_count(csv_path: Path) -> int:
    with csv_path.open(newline="", encoding="utf-8") as f:
        return sum(1 for _ in csv.reader(f)) - 1  # minus header


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "emit"
        _build_emit(emit_dir)

        config_base_path = tmp_path / "base.yaml"
        config_base_path.write_text(YAML_BASE, encoding="utf-8")
        config_base = load_export_config(config_base_path)

        with open_emit(emit_dir) as emit:
            sidecar = emit.sidecar
            fork_path = require_single_branch(sidecar)

            # --- Plan-level: notices + omitted columns, both units ---
            plan_collector = NoticeCollector()
            specs = build_source_plan(sidecar, config_base.source, plan_collector)
            patient_spec = next(
                s for s in specs if s.source_table == "records__patient"
            )
            employee_spec = next(
                s for s in specs if s.source_table == "records__employee"
            )

            patient_outputs = {out for _, out in patient_spec.columns}
            if "ssn" in patient_outputs:
                _fail("prop__ssn was not omitted from the patient changelog columns")
            if "status" not in patient_outputs:
                _fail("prop__status (tracked, delivered) missing from patient columns")

            employee_outputs = {out for _, out in employee_spec.columns}
            if employee_outputs & {"ssn", "note"}:
                _fail("employee's slice_only columns were not omitted")
            if not employee_outputs:
                _fail("degenerate employee unit rendered no columns at all")
            print(
                "OMITTED (plan): patient columns"
                f" {sorted(patient_outputs)}; employee (degenerate) columns"
                f" {sorted(employee_outputs)}"
            )

            omission_notices = [
                n
                for n in plan_collector.notices
                if n.code == "slice-only-column-omitted"
            ]
            if len(omission_notices) != 3:
                _fail(
                    "expected 3 slice-only-column-omitted notices (patient.ssn,"
                    f" employee.ssn, employee.note), got {len(omission_notices)}:"
                    f" {plan_collector.notices}"
                )
            for notice in omission_notices:
                print(f"NOTICE: {notice.message}")

            # --- change_delivery: snapshot: ssn absent there too ---
            snapshot_path = tmp_path / "snapshot.yaml"
            snapshot_path.write_text(YAML_SNAPSHOT, encoding="utf-8")
            config_snapshot = load_export_config(snapshot_path)
            snapshot_specs = build_source_plan(
                sidecar, config_snapshot.source, NoticeCollector()
            )
            snapshot_patient = next(
                s for s in snapshot_specs if s.source_table == "records__patient"
            )
            snapshot_outputs = {out for _, out in snapshot_patient.columns}
            if "ssn" in snapshot_outputs or "status" not in snapshot_outputs:
                _fail("snapshot render did not narrow the same way as the changelog")
            print(f"OMITTED (snapshot): patient columns {sorted(snapshot_outputs)}")

            # --- Row-count invariance: the narrowed changelog fold vs. a
            # directly-built baseline fold over the full (un-narrowed) property
            # set — prop__ssn is not history_tracked, so it contributes no
            # 'u' events either way; the row set must match exactly.
            narrowed_specs = build_source_query_specs(
                emit, config_base, _ANCHOR, None, NoticeCollector()
            )
            narrowed_patient_sql = next(
                q.sql for q in narrowed_specs if q.table_name == "patient"
            )
            narrowed_rows = emit.query(
                f"SELECT COUNT(*) FROM ({narrowed_patient_sql}) AS t", ()
            )[0][0]

            baseline_sql = build_row_state_events_sql(
                sidecar, fork_path, "patient", frozenset({"status", "ssn"})
            )
            baseline_rows = emit.query(
                f"SELECT COUNT(*) FROM ({baseline_sql}) AS t", ()
            )[0][0]

            if narrowed_rows != baseline_rows:
                _fail(
                    f"row count changed under narrowing: narrowed={narrowed_rows},"
                    f" baseline={baseline_rows}"
                )
            print(
                f"INVARIANT: narrowed changelog row count ({narrowed_rows}) =="
                f" un-narrowed baseline ({baseline_rows})"
            )

            # --- Full export: written CSVs carry the narrowed shape ---
            out_dir = tmp_path / "out"
            out_dir.mkdir()
            export_collector = NoticeCollector()
            counts = export_source(
                emit, config_base, out_dir, "csv", _ANCHOR, export_collector
            )
            patient_header = _read_csv_header(out_dir / "patient.csv")
            employee_header = _read_csv_header(out_dir / "employee.csv")
            if "ssn" in patient_header:
                _fail("patient.csv still carries the ssn column")
            if {"ssn", "note"} & set(employee_header):
                _fail("employee.csv still carries a slice_only column")
            if _read_csv_row_count(out_dir / "employee.csv") != 1:
                _fail("degenerate employee unit did not render its row")
            print(
                f"EXPORTED: patient.csv columns {patient_header}, row counts {counts}"
            )

        # --- rename naming the omitted column -> SourceRenameSliceOnly ---
        rename_omitted_path = tmp_path / "rename_omitted.yaml"
        rename_omitted_path.write_text(YAML_RENAME_OMITTED, encoding="utf-8")
        config_rename_omitted = load_export_config(rename_omitted_path)
        with open_emit(emit_dir) as emit:
            try:
                build_source_plan(
                    emit.sidecar, config_rename_omitted.source, NoticeCollector()
                )
            except SourceRenameSliceOnly as exc:
                if "prop__ssn" not in str(exc) or "slice_only" not in str(exc):
                    _fail(f"SourceRenameSliceOnly message missing detail: {exc}")
                print(f"REFUSED (rename omitted): {exc}")
            else:
                _fail("rename naming an omitted column did not raise")

            # A rename of a delivered column still works.
            rename_delivered_path = tmp_path / "rename_delivered.yaml"
            rename_delivered_path.write_text(YAML_RENAME_DELIVERED, encoding="utf-8")
            config_rename_delivered = load_export_config(rename_delivered_path)
            delivered_specs = build_source_plan(
                emit.sidecar, config_rename_delivered.source, NoticeCollector()
            )
            delivered_patient = next(
                s for s in delivered_specs if s.source_table == "records__patient"
            )
            if "status_code" not in {out for _, out in delivered_patient.columns}:
                _fail("renaming a delivered column stopped working")
            print("RENAMED (delivered column): prop__status -> status_code")

        print(
            "SUCCESS: slice_only columns omitted from changelog/snapshot/reference"
            " renders with one notice per unit x column, row counts unchanged by"
            " the narrowing, the degenerate unit still renders, and a rename"
            " naming an omitted column is refused"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
