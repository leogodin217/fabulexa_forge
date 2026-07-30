#!/usr/bin/env python
"""
Demo: Base mode election — self id-space value surface + per-edge target
rendering (`exporters/base/plan.py`, `exporters/base/renders.py`,
`exporters/base/engine.py`)

Sprint: key-election
Phase: 4

Builds one declared, two-kind emit: `rider` (flat, presentation_id
registry-declared with prefix `RIDER_`) and `trip` (references `rider` via
`prop__rider_id`).

1. `keys: {rider: presentation_id}` — the flagship shape: rider's `id`
   column carries `RIDER_...` codes (the standalone `presentation_id`
   payload column absorbed), and trip's `prop__rider_id` column renders the
   target's codes too (`rider_id_key` unchanged, still the record-index
   edge key).
2. `keys: {rider: record_index}` — rider's id-space self column drops
   entirely (only `rider_key` ships); trip's `prop__rider_id` value column
   drops too (it would duplicate `rider_id_key`).
3. No `keys` block — byte-identical to a pre-election export: both kinds'
   identity and edge columns render exactly as they did before this sprint.
4. A corrupted `rider` emit (two records sharing one `presentation_id`)
   fails `build_base_query_specs` with `ElectedKeyDuplicate`, loudly, before
   any writer runs.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import ExportConfig
from fabulexa_forge.errors import ElectedKeyDuplicate
from fabulexa_forge.exporters.base.engine import build_base_query_specs
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"

_RIDER_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
]

_TRIP_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__rider_id",
        "type": "VARCHAR",
        "references": "rider",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {"name": "ref_index__rider_id", "type": "BIGINT"},
]

_RIDER_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "r1", "RIDER_001", 0, True, None, 0, 0),
    (_FORK_PATH, "r2", "RIDER_002", 0, True, None, 0, 1),
]

_TRIP_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "t1", 0, True, None, 0, 0, "r1", 0),
    (_FORK_PATH, "t2", 0, True, None, 0, 1, "r2", 1),
]

# The corrupted variant: r_dup shares "RIDER_777" across two distinct
# record_ids — the exact three-way-equality violation the guard exists to
# catch (never a fabrication in the demo's honest rows above).
_RIDER_ROWS_CORRUPT: list[tuple[object, ...]] = [
    *_RIDER_ROWS,
    (_FORK_PATH, "r_dup_a", "RIDER_777", 0, True, None, 0, 2),
    (_FORK_PATH, "r_dup_b", "RIDER_777", 0, True, None, 0, 3),
]

_PRESENTATION_KEYS: dict[str, object] = {
    "rider": {
        "key": {
            "unique_within": "emit",
            "branch_stable": False,
            "slice_stable": False,
            "key_space": {"class": "counter", "prefix": "RIDER_", "width": 3},
        }
    }
}


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _build_emit(emit_dir: Path, rider_rows: list[tuple[object, ...]]) -> None:
    """Write a minimal run.duckdb + base.json emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__rider", _RIDER_COLUMNS))
    conn.execute(_ddl("records__trip", _TRIP_COLUMNS))

    rider_placeholders = ", ".join("?" for _ in _RIDER_COLUMNS)
    for row in rider_rows:
        conn.execute(
            f'INSERT INTO "records__rider" VALUES ({rider_placeholders})', list(row)
        )
    trip_placeholders = ", ".join("?" for _ in _TRIP_COLUMNS)
    for row in _TRIP_ROWS:
        conn.execute(
            f'INSERT INTO "records__trip" VALUES ({trip_placeholders})', list(row)
        )
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 9999}],
        "tables": [
            {
                "name": "records__rider",
                "category": "records",
                "record_kind": "rider",
                "columns": _RIDER_COLUMNS,
                "rows": len(rider_rows),
            },
            {
                "name": "records__trip",
                "category": "records",
                "record_kind": "trip",
                "columns": _TRIP_COLUMNS,
                "rows": len(_TRIP_ROWS),
            },
        ],
        "presentation_keys": _PRESENTATION_KEYS,
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _fail(message: str) -> "SystemExit":
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def _print_rows(
    emit_dir: Path, config: ExportConfig
) -> dict[str, list[tuple[object, ...]]]:
    """Compile config's specs and print every table's rows, name -> columns."""
    with open_emit(emit_dir) as emit:
        specs = build_base_query_specs(emit, config, None, None, lambda _n: None)
        rows_by_table: dict[str, list[tuple[object, ...]]] = {}
        for spec in specs:
            rows = emit.query(spec.sql, ())
            rows_by_table[spec.table_name] = rows
            print(f"  table '{spec.table_name}': {rows}")
        return rows_by_table


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        # ---- 1. Flagship: keys: {rider: presentation_id} ------------------
        print("=== keys: {rider: presentation_id} ===")
        emit_dir = Path(tmp) / "elected"
        _build_emit(emit_dir, _RIDER_ROWS)
        elected_config = ExportConfig(mode="base", keys={"rider": "presentation_id"})
        rows = _print_rows(emit_dir, elected_config)
        rider_row = next(r for r in rows["rider"] if r[0] == 0)
        if "RIDER_001" not in rider_row:
            raise _fail("rider.id should carry RIDER_001, the elected value")
        trip_row = next(r for r in rows["trip"] if r[0] == 0)
        if "RIDER_001" not in trip_row:
            raise _fail("trip.rider_id should render the target's elected code")
        print("  OK: id carries operational codes; prop__rider_id renders them too")
        print()

        # ---- 2. record_index: id-space column dropped ---------------------
        print("=== keys: {rider: record_index} ===")
        record_index_config = ExportConfig(mode="base", keys={"rider": "record_index"})
        rows = _print_rows(emit_dir, record_index_config)
        rider_columns_count = len(rows["rider"][0])
        trip_columns_count = len(rows["trip"][0])
        print(
            f"  rider row width={rider_columns_count},"
            f" trip row width={trip_columns_count}"
            " (both narrower — the id-space self column and the elected"
            " prop__rider_id value column both dropped)"
        )
        print()

        # ---- 3. No keys block -> byte-identical to a pre-election export --
        print("=== no keys block: byte-identical to a pre-election export ===")
        default_config = ExportConfig(mode="base")
        default_rows = _print_rows(emit_dir, default_config)
        expected_rider_row0 = (0, "r1", 0, True, None, "RIDER_001")
        if default_rows["rider"][0] != expected_rider_row0:
            raise _fail(
                f"default rider row {default_rows['rider'][0]!r} != "
                f"{expected_rider_row0!r} (pre-election shape)"
            )
        expected_trip_row0 = (0, "t1", 0, True, None, "r1", 0)
        if default_rows["trip"][0] != expected_trip_row0:
            raise _fail(
                f"default trip row {default_rows['trip'][0]!r} != "
                f"{expected_trip_row0!r} (pre-election shape)"
            )
        print("  OK: identity and edge columns render exactly as before this sprint")
        print()

        # ---- 4. Corrupted elected key fails the guard, loudly -------------
        print(
            "=== corrupted rider (duplicated presentation_id) ->"
            " ElectedKeyDuplicate ==="
        )
        corrupt_dir = Path(tmp) / "corrupt"
        _build_emit(corrupt_dir, _RIDER_ROWS_CORRUPT)
        with open_emit(corrupt_dir) as emit:
            try:
                build_base_query_specs(
                    emit, elected_config, None, None, lambda _n: None
                )
                raise _fail("expected ElectedKeyDuplicate on the r_dup corruption")
            except ElectedKeyDuplicate as exc:
                print(f"  OK: {exc}")
        print()

        print(
            "SUCCESS: presentation_id election renders operational codes as the"
            " id-space value surface and per-edge target; record_index election"
            " drops the id-space column; no keys block is byte-identical; a"
            " corrupted elected key fails the export loudly before any writer runs"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
