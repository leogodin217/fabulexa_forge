#!/usr/bin/env python
"""
Demo: Event-log selection and selection-aware disjointness (the audit-stream split)
Sprint: source-row-selection
Phase: 3

A flat `ride` kind carries a constant `prop__journey_type` property (values
`trip` / `driver_shift`, the ride-share shape from Phase 1) plus a constant
`prop__batch` BIGINT property. `events.sources[].where` splits its audit
stream into two item-types on one shared column whose typed value sets are
disjoint (design doc § Event-source disjointness): `journey_type = trip` /
`journey_type = driver_shift` legally share the `audit_log` table, each
audited item disjoint from the other.

Shows:
  1. Full export: `audit_log` carries one 'create' event per ride, dense
     1-based `id` across both sources, ordered by `event_sim_time`.
  2. Windowed export: the same tape, split across two windows — each
     window's rows carry the identical `id` values the full export assigned
     them (tape-anchored beneath the window predicate, doc § Row selection).
  3. Refusal: two sources with no common `where` column at all (population
     overlap, nothing to disjoin) and the `'5'` / `'05'` typed-value case (a
     `BIGINT` column's two spellings of one value never establish
     disjointness) — both raise `SourceEventSourceOverlap`, message suffixed
     `"; selections do not establish disjointness"`.
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
from fabulexa_forge.config.models import (
    ExportConfig,
    SourceConfig,
    SourceEventsDecl,
    SourceEventSourceDecl,
)
from fabulexa_forge.errors import SourceEventSourceOverlap
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.source.engine import (
    build_source_query_specs,
    export_source,
)
from fabulexa_forge.exporters.source.plan import build_source_plan
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"
_ANCHOR = EffectiveAnchor(
    start_instant=datetime(2024, 1, 1, tzinfo=timezone.utc), timezone=ZoneInfo("UTC")
)

_CONFIG_YAML = """
mode: source
source:
  events:
    name: audit_log
    sources:
      - kind: ride
        where:
          journey_type: trip
        item_type: trip
      - kind: ride
        where:
          journey_type: driver_shift
        item_type: driver_shift
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
        "name": "prop__batch",
        "type": "BIGINT",
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

# Two trips (created 0, 5) and two driver shifts (created 10, 15); one shared
# batch value (5) so the refusal demo's typed-value comparison has something
# real to compare.
_RIDE_ROWS: list[tuple[object, ...]] = [
    ("trunk", "ride001", 0, True, None, 0, 0, "trip", 5),
    ("trunk", "ride002", 5, True, None, 5, 1, "trip", 5),
    ("trunk", "ride003", 10, True, None, 10, 2, "driver_shift", 5),
    ("trunk", "ride004", 15, True, None, 15, 3, "driver_shift", 5),
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
    """Write the ride-share audit-stream demo emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_ddl("records__ride", _RIDE_COLUMNS))
    conn.execute(_ddl("history", _HISTORY_COLUMNS))

    _insert_all(conn, "records__ride", _RIDE_COLUMNS, _RIDE_ROWS)
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 999}],
        "enum_domains": {"ride": {"journey_type": ["trip", "driver_shift"]}},
        "runtime": {
            "timezone": "UTC",
            "start_datetime": "2024-01-01T00:00:00+00:00",
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
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 0,
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _fail(message: str) -> "SystemExit":
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def _batch_where_config(first: str, second: str) -> ExportConfig:
    """Build a two-source `ride` events config, both `where: {batch: ...}`."""
    return ExportConfig(
        mode="source",
        source=SourceConfig(
            events=SourceEventsDecl(
                name="audit_log",
                sources=(
                    SourceEventSourceDecl(
                        kind="ride", where={"batch": first}, item_type="a"
                    ),
                    SourceEventSourceDecl(
                        kind="ride", where={"batch": second}, item_type="b"
                    ),
                ),
            )
        ),
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "emit"
        build_emit(emit_dir)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(_CONFIG_YAML, encoding="utf-8")
        config: ExportConfig = load_export_config(config_path)
        assert (
            yaml.safe_load(_CONFIG_YAML)["source"]["events"]["sources"][0]["item_type"]
            == "trip"
        )

        notices: list[Notice] = []

        with open_emit(emit_dir) as emit:
            # ---- 1. Full export: disjoint-column split, dense tape id -----
            out_dir = tmp_path / "full_export"
            out_dir.mkdir()
            row_counts = export_source(
                emit, config, out_dir, "csv", _ANCHOR, notices.append
            )
            print("=== 1. full export: journey_type-disjoint audit stream ===")
            print(f"  audit_log: {row_counts['audit_log']} rows")
            if row_counts["audit_log"] != 4:
                raise _fail(
                    f"expected 4 events (one create per ride): got {row_counts}"
                )

            election = resolve_election(emit.sidecar, config.keys)
            plan = build_source_plan(
                emit, config, _ANCHOR, election, windowed=False, notices=notices.append
            )
            full_specs = {s.table_name: s for s in build_source_query_specs(plan, None)}
            full_rows = emit.query(full_specs["audit_log"].sql, ())
            full_by_record: dict[object, int] = {}
            for row_id, item_type, item_id, event, *_ in full_rows:
                print(f"  id={row_id} item_type={item_type} item_id={item_id} {event}")
                full_by_record[item_id] = row_id
            if sorted(r[0] for r in full_rows) != [1, 2, 3, 4]:
                raise _fail(f"expected dense ids 1..4: got {sorted(full_rows)}")
            print("  OK: dense 1-based id spans both disjoint-selection sources")
            print()

            # ---- 2. Windowed export: identical ids, tape-anchored ---------
            print("=== 2. windowed export: ids match the full export's ===")
            windowed_plan = build_source_plan(
                emit, config, _ANCHOR, election, windowed=True, notices=notices.append
            )
            for window in (_WINDOW_0, _WINDOW_1):
                specs = {
                    s.table_name: s
                    for s in build_source_query_specs(windowed_plan, window)
                }
                window_rows = emit.query(specs["audit_log"].sql, ())
                for row_id, _item_type, item_id, *_ in window_rows:
                    if full_by_record[item_id] != row_id:
                        raise _fail(
                            f"window {window.label}: {item_id} id {row_id} !="
                            f" full export's {full_by_record[item_id]}"
                        )
                print(
                    f"  window {window.label}: ids={sorted(r[0] for r in window_rows)}"
                )
            print("  OK: every windowed id equals its full-export id (tape-anchored)")
            print()

            # ---- 3. Refusal: no common column, then the '5'/'05' case -----
            print("=== 3. refusal: selection-aware disjointness ===")
            no_selection_config = ExportConfig(
                mode="source",
                source=SourceConfig(
                    events=SourceEventsDecl(
                        name="audit_log",
                        sources=(
                            SourceEventSourceDecl(kind="ride", item_type="a"),
                            SourceEventSourceDecl(kind="ride", item_type="b"),
                        ),
                    )
                ),
            )
            try:
                build_source_plan(
                    emit,
                    no_selection_config,
                    _ANCHOR,
                    resolve_election(emit.sidecar, no_selection_config.keys),
                    False,
                    notices.append,
                )
            except SourceEventSourceOverlap as exc:
                print(f"  REFUSED (no common where column at all): {exc}")
                if "selections do not establish disjointness" not in str(exc):
                    raise _fail(f"expected the appended clause: got {exc}") from exc
            else:
                raise AssertionError("expected SourceEventSourceOverlap")

            typed_value_config = _batch_where_config("5", "05")
            try:
                build_source_plan(
                    emit,
                    typed_value_config,
                    _ANCHOR,
                    resolve_election(emit.sidecar, typed_value_config.keys),
                    False,
                    notices.append,
                )
            except SourceEventSourceOverlap as exc:
                print(
                    f"  REFUSED ('5' / '05' on BIGINT resolve one typed value): {exc}"
                )
            else:
                raise AssertionError(
                    "expected SourceEventSourceOverlap: '5'/'05' are one BIGINT value"
                )

            # A genuinely disjoint pair of batch values, by contrast, plans clean.
            disjoint_value_config = _batch_where_config("5", "9")
            disjoint_plan = build_source_plan(
                emit,
                disjoint_value_config,
                _ANCHOR,
                resolve_election(emit.sidecar, disjoint_value_config.keys),
                False,
                notices.append,
            )
            if disjoint_plan.events is None:
                raise _fail("expected the disjoint-batch config to plan an events log")
            print(
                "  OK: batch=5 vs batch=9 (genuinely disjoint typed values) plans clean"
            )

        print()
        print(
            "SUCCESS: a common where column's disjoint typed values legally split"
            " one kind's audit stream; id stays dense and tape-anchored across a"
            " windowed run; the overlap gate refuses a selection-less collision"
            " and the '5'/'05' typed-value collision alike"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
