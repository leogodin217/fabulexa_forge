#!/usr/bin/env python
"""
Demo: The membership-state-at derivation (point-in-time membership containment)

Sprint: playback-api
Phase: 1

Builds a minimal standalone emit (run.duckdb + base.json) with one
`membership__queue__waiters` table holding two intervals for record "r1":
one closed interval [100, 200) and one still-open interval starting at 300
(left_sim_time NULL). build_membership_state_at_sql is asked at two
horizons:

  T1 = 150 — inside the closed interval: one containment row.
  T2 = 250 — after the closed interval closed, before the open interval
             starts: no containment rows.

Also asks at T3 = 500, well inside the open interval, to show the open
interval is contained regardless of how far the horizon advances.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.derivations.membership_state_at import (
    MEMBERSHIP_STATE_AT_COLUMNS,
    build_membership_state_at_sql,
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

_MEMBERSHIP_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "r1", 100, 200, "high"),  # closed interval, visible at T1 only
    (_FORK_PATH, "r1", 300, None, "low"),  # still-open interval, visible at T3
]


def _build_emit(emit_dir: Path) -> None:
    """Write a minimal run.duckdb + base.json emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    col_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _MEMBERSHIP_COLUMNS)
    conn.execute(f'CREATE TABLE "membership__queue__waiters" ({col_ddl})')
    placeholders = ", ".join("?" for _ in _MEMBERSHIP_COLUMNS)
    for row in _MEMBERSHIP_ROWS:
        conn.execute(
            f'INSERT INTO "membership__queue__waiters" VALUES ({placeholders})',
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
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _print_containment(emit_dir: Path, horizon_ns: int) -> list[tuple[object, ...]]:
    """Materialize and print the containment rows at one horizon."""
    with open_emit(emit_dir) as emit:
        sql = build_membership_state_at_sql(
            emit.sidecar, _FORK_PATH, "queue", "waiters", ["priority"], horizon_ns
        )
        rows = emit.query(sql, ())
    print(f"--- horizon_ns={horizon_ns} ---")
    print(f"columns: {MEMBERSHIP_STATE_AT_COLUMNS + ('elem__priority',)}")
    for row in rows:
        print(f"  {row}")
    return rows


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        _build_emit(emit_dir)

        t1_rows = _print_containment(emit_dir, 150)
        if t1_rows != [("r1", 100, "high")]:
            print(f"FAIL: unexpected T1 rows: {t1_rows}", file=sys.stderr)
            return 1

        t2_rows = _print_containment(emit_dir, 250)
        if t2_rows != []:
            print(f"FAIL: unexpected T2 rows: {t2_rows}", file=sys.stderr)
            return 1

        t3_rows = _print_containment(emit_dir, 500)
        if t3_rows != [("r1", 300, "low")]:
            print(f"FAIL: unexpected T3 rows: {t3_rows}", file=sys.stderr)
            return 1

        print(
            "SUCCESS: membership-state-at contains the closed interval only at"
            " T1, neither at T2, and the still-open interval at T3"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
