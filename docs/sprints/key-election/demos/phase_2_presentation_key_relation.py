#!/usr/bin/env python
"""
Demo: The presentation-key resident (build_presentation_key_at_sql /
build_presentation_key_at_end_sql)

Sprint: key-election
Phase: 2

Builds a minimal standalone emit (run.duckdb + base.json) with one
`records__actor` table holding five records, created at sim_time 0, 10, 10,
20, 30:

  - r0 (sim_time 0): declared population, presentation_id "ALPHA_000".
  - r1a / r1b (sim_time 10, record_id "r1" twice): an exactly-duplicated
    corrupted row, both carrying the identical pair (r1, "ALPHA_001") — the
    relation's DISTINCT must collapse the pair to a single row.
  - r2 (sim_time 20): an undeclared population — presentation_id is NULL,
    projecting verbatim (the honest surface value for a population the
    author never elected presentation_id for).
  - r3 (sim_time 30): a later record, excluded at the mid-tape horizon.

Prints the relation at a mid-tape horizon (25) and at the tape's end, and
checks:

  1. The horizoned relation (horizon_ns=25) holds r0, the collapsed r1 pair,
     and r2 — r3 (created at 30) is excluded.
  2. DISTINCT collapses the exactly-duplicated (r1, "ALPHA_001") pair to one
     relation row.
  3. r2's NULL presentation_id projects verbatim.
  4. The end-of-tape SQL carries no horizon predicate, and its result equals
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
from fabulexa_forge.derivations.presentation_key import (
    PRESENTATION_KEY_COLUMNS,
    build_presentation_key_at_end_sql,
    build_presentation_key_at_sql,
)
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"
_KIND = "actor"

_RECORD_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
]

_RECORD_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "r0", "ALPHA_000", 0, True, None, 0, 0),
    # Exactly-duplicated corrupted row: same (record_id, presentation_id) pair.
    (_FORK_PATH, "r1", "ALPHA_001", 10, True, None, 10, 1),
    (_FORK_PATH, "r1", "ALPHA_001", 10, True, None, 10, 1),
    # Undeclared population: presentation_id is NULL, the honest surface value.
    (_FORK_PATH, "r2", None, 20, True, None, 20, 2),
    (_FORK_PATH, "r3", "ALPHA_003", 30, True, None, 30, 3),
]

#: A horizon strictly between r2's and r3's creation instants.
_MID_HORIZON = 25

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
        sql = build_presentation_key_at_sql(emit.sidecar, _FORK_PATH, _KIND, horizon_ns)
        return emit.query(sql, ())


def _query_end(emit_dir: Path) -> list[tuple[object, ...]]:
    with open_emit(emit_dir) as emit:
        sql = build_presentation_key_at_end_sql(emit.sidecar, _FORK_PATH, _KIND)
        print(f"--- build_presentation_key_at_end_sql SQL ---\n{sql}")
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

        rec_id_col = PRESENTATION_KEY_COLUMNS.index("record_id")
        pid_col = PRESENTATION_KEY_COLUMNS.index("presentation_id")

        print(f"columns: {PRESENTATION_KEY_COLUMNS}")

        mid_rows = _query_at(emit_dir, _MID_HORIZON)
        print(f"--- build_presentation_key_at_sql(horizon_ns={_MID_HORIZON}) ---")
        for row in sorted(mid_rows, key=lambda r: r[rec_id_col]):
            print(f"  {row}")

        mid_ids = sorted(row[rec_id_col] for row in mid_rows)
        if mid_ids != ["r0", "r1", "r2"]:
            print(
                f"FAIL: expected the horizoned set ['r0', 'r1', 'r2'], got {mid_ids}",
                file=sys.stderr,
            )
            return 1

        r1_rows = [row for row in mid_rows if row[rec_id_col] == "r1"]
        if len(r1_rows) != 1 or r1_rows[0][pid_col] != "ALPHA_001":
            print(
                f"FAIL: DISTINCT must collapse the duplicated (r1, ALPHA_001)"
                f" pair to one row, got {r1_rows}",
                file=sys.stderr,
            )
            return 1

        r2_rows = [row for row in mid_rows if row[rec_id_col] == "r2"]
        if len(r2_rows) != 1 or r2_rows[0][pid_col] is not None:
            print(
                f"FAIL: r2's NULL presentation_id must project verbatim, got {r2_rows}",
                file=sys.stderr,
            )
            return 1

        end_rows = _query_end(emit_dir)
        print("--- build_presentation_key_at_end_sql ---")
        for row in sorted(end_rows, key=lambda r: r[rec_id_col]):
            print(f"  {row}")

        beyond_rows = _query_at(emit_dir, _BEYOND_EVERYTHING)
        if set(end_rows) != set(beyond_rows):
            print(
                f"FAIL: end-of-tape rows {end_rows} != far-horizon rows {beyond_rows}",
                file=sys.stderr,
            )
            return 1

        end_ids = sorted(row[rec_id_col] for row in end_rows)
        if end_ids != ["r0", "r1", "r2", "r3"]:
            print(
                f"FAIL: expected every record at the tape's end, got {end_ids}",
                file=sys.stderr,
            )
            return 1

        print(
            "SUCCESS: horizoned relation collapses the duplicated (r1, "
            "ALPHA_001) pair and projects r2's NULL presentation_id verbatim;"
            " end-of-tape carries no horizon predicate and equals the"
            " horizoned builder far beyond every creation instant"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
