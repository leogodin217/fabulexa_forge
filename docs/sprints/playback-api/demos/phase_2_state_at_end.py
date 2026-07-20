#!/usr/bin/env python
"""
Demo: The end-of-tape state entry point (build_state_at_end_sql)

Sprint: playback-api
Phase: 2

Builds a minimal standalone emit (run.duckdb + base.json) with one
`records__item` table holding a single record "r1": created at sim_time 0,
tracked through two history events (status "a" then "b"), then deactivated
at sim_time 100 — strictly after its last history event.

Prints the end-of-tape state (build_state_at_end_sql, no horizon parameter)
and shows two things:

  1. Equality with build_state_at_sql at a horizon far beyond every history
     and lifecycle instant — the equivalence contract.
  2. The divergence a history-only horizon would cause: asking
     build_state_at_sql at the last history instant (20) gets the lifecycle
     wrong — the record still reads active there, because the deactivation
     at 100 is invisible to a horizon that stops at the last history event.
     End-of-tape state reads the spine verbatim and gets it right.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.derivations.state_at import (
    STATE_AT_COLUMNS,
    build_state_at_end_sql,
    build_state_at_sql,
)
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"
_KIND = "item"

_RECORD_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__status", "type": "VARCHAR", "history_tracked": True},
]

_RECORD_ROWS: list[tuple[object, ...]] = [
    # created at 0, deactivated at 100 — 80 ns after its last history event (20)
    (_FORK_PATH, "r1", 0, False, 100, 100, "b"),
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
    (_FORK_PATH, _KIND, "r1", "status", 0, "a"),
    (_FORK_PATH, _KIND, "r1", "status", 20, "b"),
]

#: A horizon strictly beyond every history and lifecycle instant this emit uses.
_BEYOND_EVERYTHING = 10_000


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _build_emit(emit_dir: Path) -> None:
    """Write a minimal run.duckdb + base.json emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl(f"records__{_KIND}", _RECORD_COLUMNS))
    conn.execute(_ddl("history", _HISTORY_COLUMNS))

    rec_placeholders = ", ".join("?" for _ in _RECORD_COLUMNS)
    for row in _RECORD_ROWS:
        conn.execute(
            f'INSERT INTO "records__{_KIND}" VALUES ({rec_placeholders})',
            list(row),
        )
    hist_placeholders = ", ".join("?" for _ in _HISTORY_COLUMNS)
    for row in _HISTORY_ROWS:
        conn.execute(f'INSERT INTO "history" VALUES ({hist_placeholders})', list(row))
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 9999}],
        "tables": [
            {
                "name": f"records__{_KIND}",
                "category": "records",
                "columns": _RECORD_COLUMNS,
                "rows": len(_RECORD_ROWS),
                "record_kind": _KIND,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": len(_HISTORY_ROWS),
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _query_end(emit_dir: Path) -> list[tuple[object, ...]]:
    with open_emit(emit_dir) as emit:
        sql = build_state_at_end_sql(
            emit.sidecar, _FORK_PATH, _KIND, frozenset({"status"})
        )
        return emit.query(sql, ())


def _query_at(emit_dir: Path, horizon_ns: int) -> list[tuple[object, ...]]:
    with open_emit(emit_dir) as emit:
        sql = build_state_at_sql(
            emit.sidecar, _FORK_PATH, _KIND, frozenset({"status"}), horizon_ns
        )
        return emit.query(sql, ())


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        _build_emit(emit_dir)

        columns = STATE_AT_COLUMNS + ("prop__status",)
        active_idx = STATE_AT_COLUMNS.index("active")
        deact_idx = STATE_AT_COLUMNS.index("deactivated_at")

        end_rows = _query_end(emit_dir)
        print(f"columns: {columns}")
        print("--- build_state_at_end_sql (no horizon) ---")
        for row in end_rows:
            print(f"  {row}")

        # 1. Equivalence contract: equal to build_state_at_sql far beyond everything.
        beyond_rows = _query_at(emit_dir, _BEYOND_EVERYTHING)
        print(f"--- build_state_at_sql(horizon_ns={_BEYOND_EVERYTHING}) ---")
        for row in beyond_rows:
            print(f"  {row}")
        if end_rows != beyond_rows:
            print(
                f"FAIL: end-of-tape rows {end_rows} != far-horizon rows {beyond_rows}",
                file=sys.stderr,
            )
            return 1

        if end_rows[0][active_idx] is not False or end_rows[0][deact_idx] != 100:
            print(
                f"FAIL: expected inactive/deactivated_at=100: {end_rows[0]}",
                file=sys.stderr,
            )
            return 1

        # 2. Divergence: a history-only horizon (the last history instant, 20)
        # gets the lifecycle wrong — the deactivation at 100 is invisible there.
        history_only_rows = _query_at(emit_dir, 20)
        print("--- build_state_at_sql(horizon_ns=20, the last history instant) ---")
        for row in history_only_rows:
            print(f"  {row}")
        if history_only_rows[0][active_idx] is not True:
            print(
                f"FAIL: expected the history-only horizon to (wrongly) read active: "
                f"{history_only_rows[0]}",
                file=sys.stderr,
            )
            return 1

        print(
            "SUCCESS: end-of-tape state equals build_state_at_sql far beyond every"
            " instant, and correctly reads the record inactive where a"
            " history-only horizon (20) would wrongly read it active"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
