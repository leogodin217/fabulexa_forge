#!/usr/bin/env python
"""
Demo: record_index minting and the stated never-selectable / jitter-exclusion
invariants.

Builds a small v6 emit inline (in-memory DuckDB, real temp-dir base.json --
the sha256-hashing precondition `corrupt_emit` checks needs an actual file on
disk; no dependency on any repo example directory or `tests/_support`) with a
`records__patient` table (5 rows, dense `record_index` 0..4) and a
`records__doctor` table (2 rows, `prop__supervisor_id` referencing
`records__doctor` itself, paired with `ref_index__supervisor_id`). Runs one
`CorruptConfig` (embedded YAML):

1. Two `delete_rows` operations remove the *suffix* patient rows (record_index
   3 and 4). A later `insert_rows` on the same table mints two phantom
   `record_index` values -- printed to show they land at 5 and 6, strictly
   above the pre-delete maximum, never resurrecting the tombstoned 3 or 4.
2. A `duplicate_rows` `jitter` pass over `records__doctor`, `columns:
   [prop__*]`, perturbs only `prop__age` (numeric); `prop__supervisor_id` /
   `ref_index__supervisor_id` are jitter-ineligible by the declared
   reference-exclusion clause (Phase 4), not a numeric-type coincidence --
   printed to show the reference pair travels into the near-duplicate row
   unchanged.

Sprint: base-format-v6
Phase: 4
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb
import yaml

from fabulexa_forge.config.models import CorruptConfig
from fabulexa_forge.corrupters.engine import corrupt_emit
from fabulexa_forge.reader.emit import Emit, open_emit
from fabulexa_forge.reader.sidecar import Sidecar

# ---------------------------------------------------------------------------
# A small v6 emit, built inline: `records__patient` (5 rows, dense
# record_index 0..4) and `records__doctor` (2 rows, a self-referencing
# prop__supervisor_id / ref_index__supervisor_id pair) -- plus the
# contract-required `history` table, empty (no tracked column here needs a
# series).
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
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_DOCTOR_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__age",
        "type": "BIGINT",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__supervisor_id",
        "type": "VARCHAR",
        "references": "doctor",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {"name": "ref_index__supervisor_id", "type": "BIGINT"},
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

_PATIENT_IDS = ("p0", "p1", "p2", "p3", "p4")

_CORRUPT_CONFIG_YAML = """
seed: 1
operations:
  - kind: delete_rows
    name: delete_suffix_p4
    target:
      table: records__patient
      where: { record_id: p4 }
    amount: { count: 1 }

  - kind: delete_rows
    name: delete_suffix_p3
    target:
      table: records__patient
      where: { record_id: p3 }
    amount: { count: 1 }

  - kind: insert_rows
    name: insert_phantoms
    target:
      table: records__patient
    amount: { count: 2 }

  - kind: duplicate_rows
    name: jitter_doctor_age
    target:
      table: records__doctor
      where: { record_id: d1 }
      columns: [prop__*]
    amount: { count: 1 }
    jitter: { shape: uniform, low: 1.0, high: 1.0 }
"""


def _build_v6_sidecar_raw() -> dict[str, object]:
    return {
        "base_format_version": 6,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": _PATIENT_COLUMNS,
                "rows": len(_PATIENT_IDS),
            },
            {
                "name": "records__doctor",
                "category": "records",
                "record_kind": "doctor",
                "columns": _DOCTOR_COLUMNS,
                "rows": 2,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 0,
            },
        ],
        "record_roles": {"patient": "dimension", "doctor": "dimension"},
    }


def _open_inline_source_emit(sidecar: Sidecar, source_dir: Path) -> Emit:
    """A real `base.json` on disk (the sha256-hashing precondition `corrupt_emit`
    checks needs an actual file), paired with an in-memory DuckDB connection."""
    (source_dir / "base.json").write_text(json.dumps(sidecar.raw), encoding="utf-8")
    conn = duckdb.connect(":memory:")
    conn.execute(
        'CREATE TABLE "records__patient" ('
        "fork_path VARCHAR, record_id VARCHAR, created_sim_time BIGINT,"
        " active BOOLEAN, deactivated_at BIGINT, last_mutation_sim_time BIGINT,"
        " record_index BIGINT, prop__name VARCHAR)"
    )
    for index, patient_id in enumerate(_PATIENT_IDS):
        conn.execute(
            'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
            ["trunk", patient_id, 0, True, index, index, f"Patient {index}"],
        )
    conn.execute(
        'CREATE TABLE "records__doctor" ('
        "fork_path VARCHAR, record_id VARCHAR, created_sim_time BIGINT,"
        " active BOOLEAN, deactivated_at BIGINT, last_mutation_sim_time BIGINT,"
        " record_index BIGINT, prop__age BIGINT, prop__supervisor_id VARCHAR,"
        " ref_index__supervisor_id BIGINT)"
    )
    conn.execute(
        'INSERT INTO "records__doctor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL)',
        ["trunk", "d0", 0, True, 0, 0, 40],
    )
    conn.execute(
        'INSERT INTO "records__doctor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "d1", 0, True, 0, 1, 50, "d0", 0],
    )
    conn.execute(
        'CREATE TABLE "history" ('
        "fork_path VARCHAR, kind VARCHAR, record_id VARCHAR, property VARCHAR,"
        " sim_time BIGINT, value VARCHAR)"
    )
    return Emit(sidecar=sidecar, emit_dir=source_dir, conn=conn)


def _print_minted_indices(out_dir: Path) -> None:
    with open_emit(out_dir) as emit:
        rows = emit.query_arrow(
            "SELECT record_id, record_index"
            ' FROM "records__patient" ORDER BY record_index',
            (),
        ).to_pylist()
    print("records__patient after delete_rows (suffix) + insert_rows:")
    for row in rows:
        print(f"  {row['record_id']}: record_index={row['record_index']}")
    survivor_ids = {row["record_id"] for row in rows if row["record_index"] <= 2}
    minted = sorted(row["record_index"] for row in rows if row["record_index"] > 2)
    assert survivor_ids == {"p0", "p1", "p2"}
    assert minted == [5, 6], (
        f"expected minted indices [5, 6] (strictly above the pre-delete max"
        f" 4, never the tombstoned 3/4); got {minted}"
    )
    print(
        f"\nconfirmed: minted phantom indices {minted} sit strictly above the"
        " pre-delete maximum (4) -- neither tombstoned ordinal (3, 4) is reused."
    )


def _print_reference_pair_travels_untouched(out_dir: Path) -> None:
    with open_emit(out_dir) as emit:
        rows = emit.query_arrow(
            "SELECT record_id, prop__age, prop__supervisor_id,"
            ' ref_index__supervisor_id FROM "records__doctor"'
            " WHERE prop__supervisor_id IS NOT NULL ORDER BY prop__age",
            (),
        ).to_pylist()
    print("\nrecords__doctor rows referencing d0, after duplicate_rows jitter:")
    for row in rows:
        print(
            f"  age={row['prop__age']} supervisor=({row['prop__supervisor_id']!r},"
            f" {row['ref_index__supervisor_id']!r})"
        )
    assert len(rows) == 2, "the jitter pass must have added one near-duplicate row"
    pairs = {
        (row["prop__supervisor_id"], row["ref_index__supervisor_id"]) for row in rows
    }
    ages = sorted(row["prop__age"] for row in rows)
    assert pairs == {("d0", 0)}, "the reference pair must travel unchanged"
    assert ages == [50, 51], "prop__age must be the sole jittered column"
    print(
        "\nconfirmed: prop__supervisor_id / ref_index__supervisor_id travel into"
        " the near-duplicate row unchanged -- jitter-ineligible by the declared"
        " reference-exclusion clause, not by DuckDB type."
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        source_dir = Path(tmp) / "source"
        source_dir.mkdir()
        out_dir = Path(tmp) / "corrupted"

        sidecar = Sidecar.from_raw(_build_v6_sidecar_raw())
        emit = _open_inline_source_emit(sidecar, source_dir)
        config = CorruptConfig.model_validate(yaml.safe_load(_CORRUPT_CONFIG_YAML))
        try:
            corrupt_emit(emit, config, out_dir)
        finally:
            emit.close()

        _print_minted_indices(out_dir)
        _print_reference_pair_travels_untouched(out_dir)

    print(
        "\nSUCCESS: insert_rows mints record_index above the per-table"
        " high-water mark (a deletion gap is never reused), and"
        " is_jitter_eligible's declared reference-exclusion clause keeps a"
        " reference prop__/ref_index__ pair intact through duplicate_rows jitter."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
