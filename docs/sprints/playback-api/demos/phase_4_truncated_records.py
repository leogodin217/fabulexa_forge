#!/usr/bin/env python
"""
Demo: The truncated records builder (build_truncated_records_sql)

Sprint: playback-api
Phase: 4

Builds a minimal standalone emit (run.duckdb + base.json) with one tracked
records__widget property whose history advances past T, and shows:

  1. The as-of value of a tracked property at T=100 beside the physical
     (end-of-tape) value — the property has changed again after T, so the
     two values differ.
  2. The recorded trail last_mutation_sim_time: the last change recorded at
     or before T, not the physical high-water mark.
  3. A reference (ref_index__owner) to a widget created after T re-derives
     to NULL — the truncated target spine excludes it.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.derivations.truncated_tape import build_truncated_records_sql
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"
_AT_SIM_TIME = 100

_WIDGET_COLUMNS: list[dict[str, object]] = [
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
]

# Physical row: created at 10, latest status change (post-T) is "on".
_WIDGET_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "w1", 10, True, None, 999, 0, "on"),
    # Created after T=100 — a reference to this record must not resolve.
    (_FORK_PATH, "w2", 200, True, None, 999, 1, "on"),
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

_HISTORY_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "widget", "w1", "status", 20, "off"),
    (_FORK_PATH, "widget", "w1", "status", 80, "waiting"),
    (_FORK_PATH, "widget", "w1", "status", 150, "on"),  # after T
]

_CONTAINER_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__owner",
        "type": "VARCHAR",
        "references": "widget",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {"name": "ref_index__owner", "type": "BIGINT"},
]

_CONTAINER_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "c1", 30, True, None, 999, 0, "w2", None),
]

_CONTAINER_HISTORY_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "container", "c1", "owner", 25, "w2"),
]


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _build_emit(emit_dir: Path) -> None:
    """Write a minimal run.duckdb + base.json emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_ddl("records__widget", _WIDGET_COLUMNS))
    conn.execute(_ddl("records__container", _CONTAINER_COLUMNS))

    history_ph = ", ".join("?" for _ in _HISTORY_COLUMNS)
    for row in _HISTORY_ROWS + _CONTAINER_HISTORY_ROWS:
        conn.execute(f'INSERT INTO "history" VALUES ({history_ph})', list(row))
    widget_ph = ", ".join("?" for _ in _WIDGET_COLUMNS)
    for row in _WIDGET_ROWS:
        conn.execute(f'INSERT INTO "records__widget" VALUES ({widget_ph})', list(row))
    container_ph = ", ".join("?" for _ in _CONTAINER_COLUMNS)
    for row in _CONTAINER_ROWS:
        conn.execute(
            f'INSERT INTO "records__container" VALUES ({container_ph})', list(row)
        )
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 9999}],
        "tables": [
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": len(_HISTORY_ROWS) + len(_CONTAINER_HISTORY_ROWS),
            },
            {
                "name": "records__widget",
                "category": "records",
                "columns": _WIDGET_COLUMNS,
                "rows": len(_WIDGET_ROWS),
                "record_kind": "widget",
            },
            {
                "name": "records__container",
                "category": "records",
                "columns": _CONTAINER_COLUMNS,
                "rows": len(_CONTAINER_ROWS),
                "record_kind": "container",
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        _build_emit(emit_dir)

        with open_emit(emit_dir) as emit:
            physical_rows = emit.query(
                'SELECT "record_id", "prop__status" FROM "records__widget"'
                " WHERE \"record_id\" = 'w1'",
                (),
            )
            print(f"physical (end-of-tape) w1.prop__status: {physical_rows[0][1]!r}")

            widget_sql = build_truncated_records_sql(
                emit.sidecar, _FORK_PATH, "widget", _AT_SIM_TIME
            )
            widget_cols = [c.name for c in emit.sidecar.columns("records__widget")]
            widget_rows = emit.query(widget_sql, ())
            w1 = next(
                r for r in widget_rows if r[widget_cols.index("record_id")] == "w1"
            )
            status_at_t = w1[widget_cols.index("prop__status")]
            trail_at_t = w1[widget_cols.index("last_mutation_sim_time")]
            print(f"as-of T={_AT_SIM_TIME} w1.prop__status: {status_at_t!r}")
            print(f"as-of T={_AT_SIM_TIME} w1.last_mutation_sim_time: {trail_at_t}")

            container_sql = build_truncated_records_sql(
                emit.sidecar, _FORK_PATH, "container", _AT_SIM_TIME
            )
            container_cols = [
                c.name for c in emit.sidecar.columns("records__container")
            ]
            container_rows = emit.query(container_sql, ())
            c1 = container_rows[0]
            ref_index = c1[container_cols.index("ref_index__owner")]
            print(
                f"c1.ref_index__owner (references w2, created after T): {ref_index!r}"
            )

            if status_at_t != "waiting":
                print(
                    "FAIL: expected the as-of status to be the pre-T history value"
                    " 'waiting', not the physical value",
                    file=sys.stderr,
                )
                return 1
            if physical_rows[0][1] != "on":
                print(
                    "FAIL: expected the physical status to be the post-T value 'on'",
                    file=sys.stderr,
                )
                return 1
            if trail_at_t != 80:
                print(
                    "FAIL: expected the recorded trail to be 80 (the last change"
                    f" recorded at or before T), got {trail_at_t}",
                    file=sys.stderr,
                )
                return 1
            if ref_index is not None:
                print(
                    "FAIL: expected ref_index__owner to be NULL — the referenced"
                    " widget was created after T",
                    file=sys.stderr,
                )
                return 1

        print(
            "SUCCESS: the tracked property's as-of value ('waiting') differs from"
            " the physical value ('on'); the recorded trail (80) is the last"
            " change at or before T; and a reference to a record created after T"
            " re-derives to NULL"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
