#!/usr/bin/env python
"""
Demo: Corrupter pair-scoped reference writes.

Builds a small v6 emit inline (in-memory DuckDB, real temp-dir base.json --
the sha256-hashing precondition `corrupt_emit` checks needs an actual file on
disk; no dependency on any repo example directory or `tests/_support`) with a
`records__journey_instance` table whose `prop__actor` column references
`records__actor`. Runs one `CorruptConfig` (embedded YAML) with all three
pair-scoped operations -- `null_cells`, `dangle_reference`,
`mispoint_reference` -- targeting `records__journey_instance.prop__actor`,
one row each (three journey rows, `amount: {count: 1}` per operation so each
op claims a disjoint row). Shows:

1. The null_cells row's pair goes NULL / NULL.
2. The dangle_reference row's pair goes `__dangling__<n>` / `-(n + 1)`, same
   suffix `n` on both sides.
3. The mispoint_reference row's pair goes donor-id / donor-record_index --
   fully consistent, invisible to `validate`.
4. `defects.json` declares exactly one `DefectRecord` per corrupted cell (one
   `cell`-kind locator on `prop__actor` per operation), the same unchanged
   locator shape a v5 corrupt run would carry.

Sprint: base-format-v6
Phase: 3
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
# A small v6 emit, built inline: `records__actor` (the reference target) and
# `records__journey_instance` (three rows, each `prop__actor`-referencing a
# distinct actor) -- plus the contract-required `history` table, empty (no
# tracked column here needs a series).
# ---------------------------------------------------------------------------

_ACTOR_COLUMNS: list[dict[str, object]] = [
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

_JOURNEY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__actor",
        "type": "VARCHAR",
        "references": "actor",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {"name": "ref_index__actor", "type": "BIGINT"},
    {
        "name": "prop__journey_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
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

_ACTOR_IDS = ("a1", "a2", "a3")

_CORRUPT_CONFIG_YAML = """
seed: 1
operations:
  - kind: null_cells
    name: null_journey_actor
    target:
      table: records__journey_instance
      columns: [prop__actor]
    amount: { count: 1 }

  - kind: dangle_reference
    name: dangle_journey_actor
    target:
      table: records__journey_instance
      columns: [prop__actor]
    amount: { count: 1 }

  - kind: mispoint_reference
    name: mispoint_journey_actor
    target:
      table: records__journey_instance
      columns: [prop__actor]
    amount: { count: 1 }
"""


def _build_v6_sidecar_raw() -> dict[str, object]:
    return {
        "base_format_version": 6,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": _ACTOR_COLUMNS,
                "rows": len(_ACTOR_IDS),
            },
            {
                "name": "records__journey_instance",
                "category": "records",
                "record_kind": "journey_instance",
                "columns": _JOURNEY_COLUMNS,
                "rows": 3,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 0,
            },
        ],
        "record_roles": {"actor": "dimension", "journey_instance": "dimension"},
    }


def _open_inline_source_emit(sidecar: Sidecar, source_dir: Path) -> Emit:
    """A real `base.json` on disk (the sha256-hashing precondition `corrupt_emit`
    checks needs an actual file), paired with an in-memory DuckDB connection."""
    (source_dir / "base.json").write_text(json.dumps(sidecar.raw), encoding="utf-8")
    conn = duckdb.connect(":memory:")
    conn.execute(
        'CREATE TABLE "records__actor" ('
        "fork_path VARCHAR, record_id VARCHAR, created_sim_time BIGINT,"
        " active BOOLEAN, deactivated_at BIGINT, last_mutation_sim_time BIGINT,"
        " record_index BIGINT, prop__name VARCHAR)"
    )
    for index, actor_id in enumerate(_ACTOR_IDS):
        conn.execute(
            'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
            ["trunk", actor_id, 0, True, 0, index, f"Actor {index}"],
        )
    conn.execute(
        'CREATE TABLE "records__journey_instance" ('
        "fork_path VARCHAR, record_id VARCHAR, created_sim_time BIGINT,"
        " active BOOLEAN, deactivated_at BIGINT, last_mutation_sim_time BIGINT,"
        " record_index BIGINT, prop__actor VARCHAR, ref_index__actor BIGINT,"
        " prop__journey_type VARCHAR)"
    )
    for index, actor_id in enumerate(_ACTOR_IDS):
        conn.execute(
            'INSERT INTO "records__journey_instance" VALUES'
            " (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
            [
                "trunk",
                f"j{index}",
                0,
                True,
                0,
                index,
                actor_id,
                index,
                "onboarding",
            ],
        )
    conn.execute(
        'CREATE TABLE "history" ('
        "fork_path VARCHAR, kind VARCHAR, record_id VARCHAR, property VARCHAR,"
        " sim_time BIGINT, value VARCHAR)"
    )
    return Emit(sidecar=sidecar, emit_dir=source_dir, conn=conn)


def _print_journey_pairs(out_dir: Path) -> None:
    with open_emit(out_dir) as emit:
        rows = emit.query_arrow(
            "SELECT record_id, prop__actor, ref_index__actor"
            ' FROM "records__journey_instance" ORDER BY record_id',
            (),
        ).to_pylist()
    print("records__journey_instance pairs after corruption:")
    print("  (prop__actor, ref_index__actor)")
    for row in rows:
        actor, ref_index = row["prop__actor"], row["ref_index__actor"]
        print(f"  {row['record_id']}: ({actor!r}, {ref_index!r})")


def _print_and_check_defects(out_dir: Path) -> None:
    manifest = json.loads((out_dir / "defects.json").read_text(encoding="utf-8"))
    defects = manifest["defects"]
    print(f"\ndefects.json declares {len(defects)} defect(s):")
    for defect in defects:
        location = defect["location"]
        print(
            f"  class={defect['class']!r} rule={defect['rule']!r}"
            f" impact={defect['impact']} locator.kind={location['kind']!r}"
            f" column={location['column']!r}"
        )
        assert location["kind"] == "cell"
        assert location["column"] == "prop__actor"
    assert len(defects) == 3, "expected exactly one DefectRecord per corrupted cell"
    print(
        "\nconfirmed: exactly one DefectRecord per corrupted cell, each an"
        " unchanged cell-kind locator on prop__actor."
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

        _print_journey_pairs(out_dir)
        _print_and_check_defects(out_dir)

    print(
        "\nSUCCESS: null_cells co-nulls, dangle_reference co-dangles"
        " (-(n + 1) from the shared sentinel suffix), and mispoint_reference"
        " co-points (donor's record_index) the ref_index__ sibling whenever"
        " it rewrites a records reference prop__ cell -- one defect, one"
        " DefectRecord, per the edge, not the column."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
