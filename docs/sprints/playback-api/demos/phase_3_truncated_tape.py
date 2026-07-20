#!/usr/bin/env python
"""
Demo: The truncated-tape surface (history, membership, sidecar view)

Sprint: playback-api
Phase: 3

Builds a minimal standalone emit (run.duckdb + base.json) with one
membership__queue__waiters table and one sub-typed records__widget kind
carrying a slice_only column and a slice_only sub-type discriminator
(prop__widget_type).

Shows two things:

  1. A membership interval physically open past T (left_sim_time = 150) is
     presented by build_truncated_membership_sql at T=100 with left_sim_time
     masked NULL — "still open at T", exactly as a slice-at-T emit would
     render it — printed beside the untouched physical row.
  2. build_truncated_sidecar's dropped-column set for records__widget: the
     non-exempt slice_only column (prop__note) is gone; the slice_only
     sub-type discriminator (prop__widget_type) and every other declared
     column, including last_mutation_sim_time, survive.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.derivations.truncated_tape import (
    build_truncated_membership_sql,
    build_truncated_sidecar,
)
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"

_MEMBERSHIP_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__priority", "type": "VARCHAR"},
]

# joined at 50, left at 150 — still "open" relative to a T=100 slice.
_MEMBERSHIP_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "worker_1", 50, 150, "high"),
]

_WIDGET_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__widget_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "slice_only",
    },
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__note",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "slice_only",
    },
]

_AT_SIM_TIME = 100


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _build_emit(emit_dir: Path) -> None:
    """Write a minimal run.duckdb + base.json emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("membership__queue__waiters", _MEMBERSHIP_COLUMNS))
    conn.execute(_ddl("records__widget", _WIDGET_COLUMNS))

    mem_placeholders = ", ".join("?" for _ in _MEMBERSHIP_COLUMNS)
    for row in _MEMBERSHIP_ROWS:
        conn.execute(
            f'INSERT INTO "membership__queue__waiters" VALUES ({mem_placeholders})',
            list(row),
        )
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 9999}],
        "tables": [
            {
                "name": "membership__queue__waiters",
                "category": "membership",
                "columns": _MEMBERSHIP_COLUMNS,
                "rows": len(_MEMBERSHIP_ROWS),
                "record_kind": "queue",
                "property": "waiters",
            },
            {
                "name": "records__widget",
                "category": "records",
                "columns": _WIDGET_COLUMNS,
                "rows": 0,
                "record_kind": "widget",
            },
        ],
        "enum_domains": {"widget": {"widget_type": ["alpha", "beta"]}},
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        _build_emit(emit_dir)

        with open_emit(emit_dir) as emit:
            physical_rows = emit.query('SELECT * FROM "membership__queue__waiters"', ())
            print(f"physical row: {physical_rows[0]}")

            truncated_sql = build_truncated_membership_sql(
                emit.sidecar, _FORK_PATH, "queue", "waiters", _AT_SIM_TIME
            )
            truncated_rows = emit.query(truncated_sql, ())
            print(f"truncated row @ T={_AT_SIM_TIME}: {truncated_rows[0]}")

            left_idx = 3  # fork_path, record_id, joined_sim_time, left_sim_time, ...
            if physical_rows[0][left_idx] != 150:
                print(
                    "FAIL: expected the physical left_sim_time to be 150",
                    file=sys.stderr,
                )
                return 1
            if truncated_rows[0][left_idx] is not None:
                print(
                    "FAIL: expected left_sim_time masked NULL in the truncated row",
                    file=sys.stderr,
                )
                return 1

            physical_widget_cols = {
                c.name for c in emit.sidecar.columns("records__widget")
            }
            truncated_sidecar = build_truncated_sidecar(emit.sidecar)
            truncated_widget_cols = {
                c.name for c in truncated_sidecar.columns("records__widget")
            }
            dropped = physical_widget_cols - truncated_widget_cols
            print(f"records__widget physical columns:  {sorted(physical_widget_cols)}")
            print(f"records__widget truncated columns: {sorted(truncated_widget_cols)}")
            print(f"dropped-column set: {sorted(dropped)}")

            if dropped != {"prop__note"}:
                print(
                    f"FAIL: expected only the non-exempt slice_only column "
                    f"dropped, got {dropped}",
                    file=sys.stderr,
                )
                return 1
            if "prop__widget_type" not in truncated_widget_cols:
                print(
                    "FAIL: the slice_only sub-type discriminator must survive",
                    file=sys.stderr,
                )
                return 1
            if "last_mutation_sim_time" not in truncated_widget_cols:
                print(
                    "FAIL: last_mutation_sim_time must stay declared",
                    file=sys.stderr,
                )
                return 1

        print(
            "SUCCESS: the truncated membership row masks left_sim_time NULL beside"
            " the untouched physical row, and the truncated sidecar drops exactly"
            " the non-exempt slice_only column while keeping the sub-type"
            " discriminator and last_mutation_sim_time"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
