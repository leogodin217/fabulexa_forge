#!/usr/bin/env python
"""
Demo: the two declared mode changes

Sprint: playback-api
Phase: 9

Builds a minimal standalone emit (run.duckdb + base.json) with one changelog-
genre kind (`widget`, an untracked-role `dimension` reclassified to change-log
by its sole tracked prop__ column): created at t=0, one tracked-property
history event at t=20, deactivated at t=100 — after its last history event,
the case a history-only horizon would render active but the tape's end
correctly renders inactive.

1. Horizon-less snapshot delivery: a `mode: source` export with
   `change_delivery: snapshot` and no window — refused before this phase
   (`SourceSnapshotRequiresWindows`) — now reconstructs at the tape's end via
   `build_state_at_end_sql`, spanning the deactivation-after-history-event
   lifecycle instant.
2. The presentation-name posture: a dimensional config naming an output
   column `last_mutation_sim_time` is refused at load time, naming the fix
   (deliver it under a presentation name — a `from:` source).
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    ColumnDecl,
    DimensionalConfig,
    ExportConfig,
    SourceConfig,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.validation import validate_table
from fabulexa_forge.exporters.source.engine import export_source
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"

_WIDGET_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
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


def _col_ddl(columns: list[dict[str, object]]) -> str:
    """Build a CREATE TABLE column-list fragment."""
    return ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)


def _build_emit(emit_dir: Path) -> None:
    """Write a minimal run.duckdb + base.json emit into emit_dir.

    widget w1: created t=0, history event (name -> "beta") at t=20,
    deactivated at t=100 — deactivation strictly after its last history
    event, the lifecycle-instant case a history-only horizon gets wrong.
    """
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(f'CREATE TABLE "records__widget" ({_col_ddl(_WIDGET_COLUMNS)})')
    conn.execute(f'CREATE TABLE "history" ({_col_ddl(_HISTORY_COLUMNS)})')

    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        [_FORK_PATH, "w1", 0, False, 100, 100, 0, "beta"],
    )
    for row in (
        (_FORK_PATH, "widget", "w1", "name", 0, "alpha"),
        (_FORK_PATH, "widget", "w1", "name", 20, "beta"),
    ):
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 200}],
        "tables": [
            {
                "name": "records__widget",
                "category": "records",
                "columns": _WIDGET_COLUMNS,
                "rows": 1,
                "record_kind": "widget",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 2,
            },
        ],
        "runtime": {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"},
        "record_roles": {"widget": "dimension"},
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _discard_notice(_notice: object) -> None:
    """Swallow plan notices — this demo is indifferent to them."""


def _demo_horizon_less_snapshot(emit_dir: Path, out_dir: Path) -> None:
    print("=== 1. horizon-less change_delivery: snapshot ===")
    config = ExportConfig(
        mode="source", source=SourceConfig(change_delivery="snapshot")
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        # Previously refused (SourceSnapshotRequiresWindows); now reconstructs.
        row_counts = export_source(
            emit, config, out_dir, "csv", anchor, notice_sink=_discard_notice
        )
    print(f"row counts: {row_counts}")

    with (out_dir / "widget.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"end-of-run widget rows: {rows}")

    if row_counts != {"widget": 1}:
        print(f"FAIL: expected one end-of-run row, got {row_counts}", file=sys.stderr)
        raise SystemExit(1)
    row = rows[0]
    if row["active"] != "False":
        print(
            f"FAIL: expected w1 inactive at the tape's end, got {row}", file=sys.stderr
        )
        raise SystemExit(1)
    if row["name"] != "beta":
        print(f"FAIL: expected latest recorded name 'beta', got {row}", file=sys.stderr)
        raise SystemExit(1)
    print(
        "widget w1 (deactivated at t=100, after its last history event at t=20)"
        " renders inactive with name='beta' — the tape's end, not a history-only"
        " horizon"
    )


def _demo_presentation_name_posture(emit_dir: Path) -> None:
    print("\n=== 2. the presentation-name posture ===")
    table_decl = TableDecl(
        name="dim_widget",
        role="dim",
        scd="type1",
        source=SourceDecl(grain="records", kind="widget"),
        key=["id"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            # Author-named output column reserved for the sim-internal
            # bookkeeping value — refused at load time.
            ColumnDecl(name="last_mutation_sim_time", **{"from": "record_id"}),
        ],
    )
    config = DimensionalConfig(tables=[table_decl])
    with open_emit(emit_dir) as emit:
        try:
            validate_table(table_decl, config, emit.sidecar, None, _discard_notice)
        except ExportError as exc:
            print(f"refused as expected: {exc}")
            if "last_mutation_sim_time" not in str(exc):
                print(f"FAIL: message does not name the fix: {exc}", file=sys.stderr)
                raise SystemExit(1) from exc
            return
    print("FAIL: expected ExportError, export succeeded", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        out_dir = Path(tmp) / "out"
        out_dir.mkdir()
        _build_emit(emit_dir)

        _demo_horizon_less_snapshot(emit_dir, out_dir)
        _demo_presentation_name_posture(emit_dir)

        print(
            "\nSUCCESS: horizon-less change_delivery: snapshot reconstructs at the"
            " tape's end instead of refusing; a dimensional output column named"
            " last_mutation_sim_time is refused at load time, naming the fix"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
