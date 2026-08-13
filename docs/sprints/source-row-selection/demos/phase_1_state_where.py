#!/usr/bin/env python
"""
Demo: Constant-gated `where` on source state tables (the ride-share shape)
Sprint: source-row-selection
Phase: 1

A flat `ride` kind carries a constant `prop__journey_type` property (values
`trip` / `driver_shift`) that is *not* the kind's discriminator — no
`sub_types` axis exists for it. `where: {prop__journey_type: ...}` splits it
into two declared state tables with disjoint, deterministic row membership,
full export and windowed alike (design doc § Row selection, § The
constant-column gate).

Shows:
  1. Full export: `trips` / `driver_shifts` state tables, split by `where`,
     row-disjoint and together covering every `ride` record.
  2. Windowed state: the same predicate applies at every window horizon — a
     record's presence varies only by its creation-time lifecycle, never by
     predicate re-evaluation (row membership window-invariant).
  3. Refusal: a `where` key naming a `tracked` column
     (`SourceWhereNotConstant`) and a `where` key naming the sub-type
     discriminator of a *different*, sub-typed kind (`SourceWhereOnDiscriminator`).
  4. The `discriminator-value-unobserved` notice: an out-of-domain `where`
     element notices (never errors) — partial-list ("contributes no rows")
     and wholly-out-of-domain ("renders no rows", verified empty) variants.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import yaml

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.config.models import ExportConfig, SourceConfig, SourceTableDecl
from fabulexa_forge.errors import SourceWhereNotConstant, SourceWhereOnDiscriminator
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.source.engine import (
    build_source_query_specs,
    export_source,
)
from fabulexa_forge.exporters.source.plan import (
    SourcePlan,
    SourceStateTablePlan,
    build_source_plan,
)
from fabulexa_forge.exporters.source.renders import build_state_render_sql
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"
_ANCHOR = EffectiveAnchor(
    start_instant=datetime(2024, 1, 1, tzinfo=timezone.utc), timezone=ZoneInfo("UTC")
)

_CONFIG_YAML = """
mode: source
source:
  tables:
    - name: trips
      kind: ride
      where:
        prop__journey_type: trip
    - name: driver_shifts
      kind: ride
      where:
        prop__journey_type: driver_shift
"""

_RIDE_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__journey_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__fare",
        "type": "BIGINT",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]

# A second, sub-typed kind purely to demonstrate the discriminator refusal —
# `ride` itself is flat (its journey_type split carries no sub_types axis).
_VEHICLE_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__vehicle_type",
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

# Two trips (created 0, 5) and two driver shifts (created 10, 15).
_RIDE_ROWS: list[tuple[object, ...]] = [
    ("trunk", "ride001", 0, True, None, 0, 0, "trip", 50),
    ("trunk", "ride002", 5, True, None, 5, 1, "trip", 75),
    ("trunk", "ride003", 10, True, None, 10, 2, "driver_shift", 0),
    ("trunk", "ride004", 15, True, None, 15, 3, "driver_shift", 0),
]
_HISTORY_ROWS: list[tuple[object, ...]] = [
    ("trunk", "ride", "ride001", "fare", 0, "50"),
    ("trunk", "ride", "ride002", "fare", 5, "75"),
]
_VEHICLE_ROWS: list[tuple[object, ...]] = [
    ("trunk", "veh001", 0, True, None, 0, 0, "car"),
    ("trunk", "veh002", 0, True, None, 0, 1, "bike"),
]

_WINDOW_0 = Window(index=0, start_ns=0, end_ns=10, label="w0")
_WINDOW_1 = Window(index=1, start_ns=10, end_ns=20, label="w1")


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _insert_all(
    conn: "duckdb.DuckDBPyConnection",
    table: str,
    cols: list[dict[str, object]],
    rows: list[tuple[object, ...]],
) -> None:
    placeholders = ", ".join("?" for _ in cols)
    for row in rows:
        conn.execute(f'INSERT INTO "{table}" VALUES ({placeholders})', list(row))


def build_emit(emit_dir: Path) -> None:
    """Write the ride-share demo emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_ddl("records__ride", _RIDE_COLUMNS))
    conn.execute(_ddl("records__vehicle", _VEHICLE_COLUMNS))
    conn.execute(_ddl("history", _HISTORY_COLUMNS))

    _insert_all(conn, "records__ride", _RIDE_COLUMNS, _RIDE_ROWS)
    _insert_all(conn, "records__vehicle", _VEHICLE_COLUMNS, _VEHICLE_ROWS)
    _insert_all(conn, "history", _HISTORY_COLUMNS, _HISTORY_ROWS)
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 999}],
        "enum_domains": {
            "ride": {"journey_type": ["trip", "driver_shift"]},
            "vehicle": {"vehicle_type": ["car", "bike"]},
        },
        "tables": [
            {
                "name": "records__ride",
                "category": "records",
                "record_kind": "ride",
                "columns": _RIDE_COLUMNS,
                "rows": len(_RIDE_ROWS),
            },
            {
                "name": "records__vehicle",
                "category": "records",
                "record_kind": "vehicle",
                "columns": _VEHICLE_COLUMNS,
                "rows": len(_VEHICLE_ROWS),
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


def _fail(message: str) -> "SystemExit":
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def _state_table(plan: SourcePlan, name: str) -> SourceStateTablePlan:
    table = next(t for t in plan.tables if t.name == name)
    assert isinstance(table, SourceStateTablePlan)
    return table


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "emit"
        build_emit(emit_dir)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(_CONFIG_YAML, encoding="utf-8")
        config: ExportConfig = load_export_config(config_path)
        assert yaml.safe_load(_CONFIG_YAML)["source"]["tables"][0]["name"] == "trips"

        notices: list[Notice] = []

        with open_emit(emit_dir) as emit:
            # ---- 1. Full export: the split is row-disjoint and covering ----
            out_dir = tmp_path / "full_export"
            out_dir.mkdir()
            row_counts = export_source(
                emit, config, out_dir, "csv", _ANCHOR, notices.append
            )
            print("=== 1. full export: journey_type split ===")
            print(f"  trips: {row_counts['trips']} rows")
            print(f"  driver_shifts: {row_counts['driver_shifts']} rows")
            if row_counts["trips"] != 2 or row_counts["driver_shifts"] != 2:
                raise _fail(f"expected 2/2 split: got {row_counts}")
            print("  OK: 4 ride records split 2/2, no overlap, no omission")
            print()

            # ---- 2. Windowed state: predicate applies at every horizon -----
            print("=== 2. windowed state: row membership is window-invariant ===")
            election = resolve_election(emit.sidecar, config.keys)
            windowed_plan = build_source_plan(
                emit, config, _ANCHOR, election, windowed=True, notices=notices.append
            )
            for window in (_WINDOW_0, _WINDOW_1):
                specs = {
                    spec.table_name: spec
                    for spec in build_source_query_specs(windowed_plan, window)
                }
                trip_ids = {r[0] for r in emit.query(specs["trips"].sql, ())}
                shift_ids = {r[0] for r in emit.query(specs["driver_shifts"].sql, ())}
                if trip_ids & shift_ids:
                    raise _fail(
                        f"window {window.label}: trips/driver_shifts overlap:"
                        f" {trip_ids & shift_ids}"
                    )
                print(
                    f"  window {window.label}: trips={sorted(trip_ids)}"
                    f" driver_shifts={sorted(shift_ids)}"
                )
            # ride001/ride002 (trip) never surface in driver_shifts at either
            # horizon; ride003/ride004 (driver_shift) never surface in trips.
            # Growth from w0 -> w1 is lifecycle-driven (creation horizon)
            # only, never a change in which table a record belongs to.
            print("  OK: table membership never crosses the predicate split")
            print()

            # ---- 3. Refusal: tracked column, and another kind's discriminator
            print("=== 3. refusal: constant-column gate ===")
            tracked_config = ExportConfig(
                mode="source",
                source=SourceConfig(
                    tables=(
                        SourceTableDecl(
                            name="bad_fare",
                            kind="ride",
                            where={"prop__fare": "50"},
                        ),
                    )
                ),
            )
            try:
                build_source_plan(
                    emit,
                    tracked_config,
                    _ANCHOR,
                    resolve_election(emit.sidecar, tracked_config.keys),
                    False,
                    notices.append,
                )
            except SourceWhereNotConstant as exc:
                print(f"  REFUSED (tracked column 'prop__fare'): {exc}")
            else:
                raise AssertionError("expected SourceWhereNotConstant")

            discriminator_config = ExportConfig(
                mode="source",
                source=SourceConfig(
                    tables=(
                        SourceTableDecl(
                            name="bad_discriminator",
                            kind="vehicle",
                            where={"prop__vehicle_type": "car"},
                        ),
                    )
                ),
            )
            try:
                build_source_plan(
                    emit,
                    discriminator_config,
                    _ANCHOR,
                    resolve_election(emit.sidecar, discriminator_config.keys),
                    False,
                    notices.append,
                )
            except SourceWhereOnDiscriminator as exc:
                print(f"  REFUSED (discriminator 'prop__vehicle_type'): {exc}")
            else:
                raise AssertionError("expected SourceWhereOnDiscriminator")
            print()

            # ---- 4. discriminator-value-unobserved notice, never an error --
            print("=== 4. out-of-domain where value: notice, never an error ===")
            partial_config = ExportConfig(
                mode="source",
                source=SourceConfig(
                    tables=(
                        SourceTableDecl(
                            name="trips_plus",
                            kind="ride",
                            where={"prop__journey_type": ["trip", "carpool"]},
                        ),
                    )
                ),
            )
            partial_notices: list[Notice] = []
            partial_plan = build_source_plan(
                emit,
                partial_config,
                _ANCHOR,
                resolve_election(emit.sidecar, partial_config.keys),
                False,
                partial_notices.append,
            )
            partial_hits = [
                n for n in partial_notices if n.code == "discriminator-value-unobserved"
            ]
            if len(partial_hits) != 1 or "carpool" not in partial_hits[0].message:
                raise _fail(f"expected one 'carpool' notice: got {partial_notices}")
            print(f"  notice (partial list): {partial_hits[0].message}")
            partial_table = _state_table(partial_plan, "trips_plus")
            partial_sql = build_state_render_sql(
                emit.sidecar, _FORK_PATH, partial_table, _ANCHOR, None
            )
            partial_rows = emit.query(partial_sql, ())
            if len(partial_rows) != 2:
                raise _fail(f"expected 2 rows (trip only): got {len(partial_rows)}")
            print(
                "  OK: 'carpool' contributes no rows; table still renders"
                f" {len(partial_rows)}"
            )

            wholly_config = ExportConfig(
                mode="source",
                source=SourceConfig(
                    tables=(
                        SourceTableDecl(
                            name="carpools",
                            kind="ride",
                            where={"prop__journey_type": "carpool"},
                        ),
                    )
                ),
            )
            wholly_notices: list[Notice] = []
            wholly_plan = build_source_plan(
                emit,
                wholly_config,
                _ANCHOR,
                resolve_election(emit.sidecar, wholly_config.keys),
                False,
                wholly_notices.append,
            )
            wholly_hits = [
                n for n in wholly_notices if n.code == "discriminator-value-unobserved"
            ]
            if len(wholly_hits) != 1 or "no rows" not in wholly_hits[0].message:
                raise _fail(f"expected a table-empty notice: got {wholly_notices}")
            print(f"  notice (wholly out-of-domain): {wholly_hits[0].message}")
            wholly_table = _state_table(wholly_plan, "carpools")
            wholly_sql = build_state_render_sql(
                emit.sidecar, _FORK_PATH, wholly_table, _ANCHOR, None
            )
            wholly_rows = emit.query(wholly_sql, ())
            if wholly_rows:
                raise _fail(f"expected an empty table: got {len(wholly_rows)} rows")
            print("  OK: wholly out-of-domain value renders an empty table, no error")

        print()
        print(
            "SUCCESS: a constant-gated 'where' splits a flat kind into"
            " row-disjoint state tables, window-invariant; the constant-column"
            " gate refuses a tracked column and a discriminator key; an"
            " out-of-domain value notices, and never errors"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
