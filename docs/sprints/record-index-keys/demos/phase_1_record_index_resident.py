#!/usr/bin/env python
"""
Demo: The record-index resident (build_record_index_at_sql /
build_record_index_at_end_sql)

Sprint: record-index-keys
Phase: 1

Builds a minimal standalone emit (run.duckdb + base.json) with one
`records__item` table holding four records, created at sim_time 0, 10, 20, 30
with record_index 0, 1, 2, 3 respectively. The record created at sim_time 10
(record_index 1) is deactivated at sim_time 12 — well before the mid-tape
horizon used below.

Prints the relation at a mid-tape horizon (15) and at the tape's end, and
checks three things:

  1. The horizoned relation (horizon_ns=15) is the dense creation-order
     prefix 0..1 — only the records created strictly before 15 appear.
  2. The deactivated record (record_index 1) is present in the horizoned
     relation — `active` is never a predicate.
  3. The end-of-tape SQL carries no horizon predicate, and its result equals
     the horizoned builder at a horizon strictly beyond every creation
     instant (the equivalence contract).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.derivations.record_index import (
    RECORD_INDEX_COLUMNS,
    build_record_index_at_end_sql,
    build_record_index_at_sql,
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
    {"name": "record_index", "type": "BIGINT"},
]

_RECORD_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "r0", 0, True, None, 0, 0),
    # deactivated at 12 — before the mid-tape horizon (15), still resolvable.
    (_FORK_PATH, "r1", 10, False, 12, 12, 1),
    (_FORK_PATH, "r2", 20, True, None, 20, 2),
    (_FORK_PATH, "r3", 30, True, None, 30, 3),
]

#: A horizon strictly between the second and third records' creation instants.
_MID_HORIZON = 15

#: A horizon strictly beyond every creation instant this emit uses.
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

    rec_placeholders = ", ".join("?" for _ in _RECORD_COLUMNS)
    for row in _RECORD_ROWS:
        conn.execute(
            f'INSERT INTO "records__{_KIND}" VALUES ({rec_placeholders})',
            list(row),
        )
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
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _query_at(emit_dir: Path, horizon_ns: int) -> list[tuple[object, ...]]:
    with open_emit(emit_dir) as emit:
        sql = build_record_index_at_sql(emit.sidecar, _FORK_PATH, _KIND, horizon_ns)
        return emit.query(sql, ())


def _query_end(emit_dir: Path) -> list[tuple[object, ...]]:
    with open_emit(emit_dir) as emit:
        sql = build_record_index_at_end_sql(emit.sidecar, _FORK_PATH, _KIND)
        print(f"--- build_record_index_at_end_sql SQL ---\n{sql}")
        if "created_sim_time" in sql:
            print(
                "FAIL: end-of-tape SQL must carry no horizon predicate",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return emit.query(sql, ())


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        _build_emit(emit_dir)

        idx_col = RECORD_INDEX_COLUMNS.index("record_index")

        print(f"columns: {RECORD_INDEX_COLUMNS}")

        mid_rows = _query_at(emit_dir, _MID_HORIZON)
        print(f"--- build_record_index_at_sql(horizon_ns={_MID_HORIZON}) ---")
        for row in sorted(mid_rows, key=lambda r: r[idx_col]):
            print(f"  {row}")

        mid_indexes = sorted(row[idx_col] for row in mid_rows)
        if mid_indexes != [0, 1]:
            print(
                f"FAIL: expected the dense creation-order prefix [0, 1],"
                f" got {mid_indexes}",
                file=sys.stderr,
            )
            return 1

        if 1 not in {row[idx_col] for row in mid_rows}:
            print(
                "FAIL: the deactivated record (record_index 1) must be present",
                file=sys.stderr,
            )
            return 1

        end_rows = _query_end(emit_dir)
        print("--- build_record_index_at_end_sql ---")
        for row in sorted(end_rows, key=lambda r: r[idx_col]):
            print(f"  {row}")

        beyond_rows = _query_at(emit_dir, _BEYOND_EVERYTHING)
        if set(end_rows) != set(beyond_rows):
            print(
                f"FAIL: end-of-tape rows {end_rows} != far-horizon rows {beyond_rows}",
                file=sys.stderr,
            )
            return 1

        end_indexes = sorted(row[idx_col] for row in end_rows)
        if end_indexes != [0, 1, 2, 3]:
            print(
                f"FAIL: expected every record at the tape's end, got {end_indexes}",
                file=sys.stderr,
            )
            return 1

        print(
            "SUCCESS: horizoned relation is the dense creation-order prefix"
            " [0, 1] with the deactivated record present, and end-of-tape"
            " equals the horizoned builder far beyond every creation instant"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
