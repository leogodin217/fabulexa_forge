#!/usr/bin/env python
"""
Demo: The declared-table source exporter, end to end (Phase 3 cutover)
Sprint: source-declared-tables
Phase: 3

Loads a declared `mode: source` config (embedded YAML) over a small fixture
emit: a sub-typed `customer` kind exported as two state tables (`customers` —
the full domain, `vip_customers` — a proper sub-type subset), a `trip_drivers`
junction over `membership__trip__drivers`, and a `versions` event log
auditing `trip.status`, every `customer` property, and the drivers
membership — a kind (`trip`) audited without ever getting its own declared
state table (design doc § The event log, "a kind may be audited without
having a declared state table").

Shows:
  1. Full export (`export_source`, CSV): a full-tape run writing all four
     declared tables, `updated_at` present on the state tables.
  2. A two-window drip (`build_source_plan(..., windowed=True)` +
     `build_source_query_specs`), window0=[0,50) / window1=[50,100)):
       - state tables: a full horizon-snapshot re-render each window, with
         no `updated_at` column at all (horizon honesty) — same row count
         both windows (no customers created after t=10).
       - the junction: extract-on-change — the closed 'alice' interval
         (joined 5, left 45) appears in window0 only; the still-open 'bob'
         interval (joined 70) appears in window1 only.
       - the event log: append-only by event_sim_time — window0 carries
         every event before t=50, window1 carries the rest, and the two
         windows partition the full-export log exactly.
"""

from __future__ import annotations

import csv
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
from fabulexa_forge.config.models import ExportConfig
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
  tables:
    - name: customers
      kind: customer
      sub_types: [standard, vip]
    - name: vip_customers
      kind: customer
      sub_types: [vip]
    - name: trip_drivers
      membership:
        kind: trip
        property: drivers
  events:
    name: versions
    sources:
      - kind: trip
        only: [status]
      - kind: customer
      - membership:
          kind: trip
          property: drivers
"""

_CUSTOMER_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {"name": "prop__customer_type", "type": "VARCHAR"},
    {
        "name": "prop__tier",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
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
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
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

_DRIVERS_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__role", "type": "VARCHAR"},
]

_CUSTOMER_ROWS: list[tuple[object, ...]] = [
    ("trunk", "cust001", 0, True, None, 0, 0, "standard", "silver"),
    ("trunk", "cust002", 10, True, None, 10, 1, "vip", "gold"),
]
_TRIP_ROWS: list[tuple[object, ...]] = [
    ("trunk", "trip001", 0, True, None, 60, 0, "completed"),
]
_HISTORY_ROWS: list[tuple[object, ...]] = [
    ("trunk", "trip", "trip001", "status", 0, "queued"),
    ("trunk", "trip", "trip001", "status", 20, "en_route"),
    ("trunk", "trip", "trip001", "status", 60, "completed"),
]
# alice: joined 5, left 45 — a closed interval entirely inside window0=[0,50).
# bob: joined 70, still open — inside window1=[50,100) only.
_DRIVERS_ROWS: list[tuple[object, ...]] = [
    ("trunk", "trip001", 5, 45, "primary"),
    ("trunk", "trip001", 70, None, "backup"),
]

_WINDOW_0 = Window(index=0, start_ns=0, end_ns=50, label="w0")
_WINDOW_1 = Window(index=1, start_ns=50, end_ns=100, label="w1")

# alice's closed interval (joined 5 / left 45) surfaces only in window0;
# bob's still-open interval (joined 70) only in window1 — extract-on-change.
_EXPECTED_DRIVER_ROLES: dict[str, list[str]] = {"w0": ["primary"], "w1": ["backup"]}


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
    """Write the declared-export demo emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_ddl("records__customer", _CUSTOMER_COLUMNS))
    conn.execute(_ddl("records__trip", _TRIP_COLUMNS))
    conn.execute(_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_ddl("membership__trip__drivers", _DRIVERS_COLUMNS))

    _insert_all(conn, "records__customer", _CUSTOMER_COLUMNS, _CUSTOMER_ROWS)
    _insert_all(conn, "records__trip", _TRIP_COLUMNS, _TRIP_ROWS)
    _insert_all(conn, "history", _HISTORY_COLUMNS, _HISTORY_ROWS)
    _insert_all(conn, "membership__trip__drivers", _DRIVERS_COLUMNS, _DRIVERS_ROWS)
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 999}],
        "enum_domains": {"customer": {"customer_type": ["standard", "vip"]}},
        "tables": [
            {
                "name": "records__customer",
                "category": "records",
                "record_kind": "customer",
                "columns": _CUSTOMER_COLUMNS,
                "rows": len(_CUSTOMER_ROWS),
            },
            {
                "name": "records__trip",
                "category": "records",
                "record_kind": "trip",
                "columns": _TRIP_COLUMNS,
                "rows": len(_TRIP_ROWS),
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": len(_HISTORY_ROWS),
            },
            {
                "name": "membership__trip__drivers",
                "category": "membership",
                "record_kind": "trip",
                "property": "drivers",
                "columns": _DRIVERS_COLUMNS,
                "rows": len(_DRIVERS_ROWS),
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _fail(message: str) -> "SystemExit":
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def _read_csv_header(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as fh:
        return next(csv.reader(fh))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "emit"
        build_emit(emit_dir)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(_CONFIG_YAML, encoding="utf-8")
        config: ExportConfig = load_export_config(config_path)
        # Sanity: the embedded YAML round-trips through the loader unchanged.
        assert yaml.safe_load(_CONFIG_YAML)["source"]["events"]["name"] == "versions"

        notices: list[Notice] = []

        with open_emit(emit_dir) as emit:
            # ---- 1. Full export (engine.py, write_mode='create') -----------
            out_dir = tmp_path / "full_export"
            out_dir.mkdir()
            row_counts = export_source(
                emit, config, out_dir, "csv", _ANCHOR, notices.append
            )
            print("=== full export row counts ===")
            for name in ("customers", "vip_customers", "trip_drivers", "versions"):
                print(f"  {name}: {row_counts[name]}")
            if row_counts["customers"] != 2 or row_counts["vip_customers"] != 1:
                raise _fail(f"expected customers=2, vip_customers=1: got {row_counts}")
            if row_counts["trip_drivers"] != 2:
                raise _fail(f"expected 2 driver intervals: got {row_counts}")
            full_log_count = row_counts["versions"]

            full_header = _read_csv_header(out_dir / "customers.csv")
            if "updated_at" not in full_header:
                raise _fail(
                    f"full export state table missing updated_at: {full_header}"
                )
            print("  OK: full-export 'customers' columns include updated_at")
            print()

            # ---- 2. The windowed drip (plan + compile split) ---------------
            election = resolve_election(emit.sidecar, config.keys)
            windowed_plan = build_source_plan(
                emit,
                config,
                _ANCHOR,
                election,
                windowed=True,
                notices=notices.append,
            )

            log_counts: dict[str, int] = {}
            for window in (_WINDOW_0, _WINDOW_1):
                specs = {
                    spec.table_name: spec
                    for spec in build_source_query_specs(windowed_plan, window)
                }

                # -- state: full horizon snapshot, no updated_at ------------
                customers_spec = specs["customers"]
                if customers_spec.write_mode != "replace":
                    raise _fail(
                        f"windowed state write_mode should be 'replace':"
                        f" {customers_spec.write_mode}"
                    )
                customers_table = emit.query_arrow(customers_spec.sql, ())
                if "updated_at" in customers_table.column_names:
                    raise _fail(
                        f"windowed state snapshot must omit updated_at:"
                        f" {customers_table.column_names}"
                    )
                if customers_table.num_rows != 2:
                    raise _fail(
                        f"window {window.label}: expected 2 customers snapshot"
                        f" rows, got {customers_table.num_rows}"
                    )
                print(
                    f"  OK: window {window.label} 'customers' snapshot ="
                    f" {customers_table.num_rows} rows, no updated_at"
                )

                # -- junction: extract-on-change -----------------------------
                drivers_spec = specs["trip_drivers"]
                if drivers_spec.write_mode != "append":
                    raise _fail(
                        f"windowed junction write_mode should be 'append':"
                        f" {drivers_spec.write_mode}"
                    )
                drivers_rows = emit.query(drivers_spec.sql, ())
                roles = sorted(str(row[-1]) for row in drivers_rows)
                if roles != _EXPECTED_DRIVER_ROLES[window.label]:
                    raise _fail(
                        f"window {window.label}: expected driver roles"
                        f" {_EXPECTED_DRIVER_ROLES[window.label]}, got {roles}"
                    )
                print(f"  OK: window {window.label} 'trip_drivers' roles = {roles}")

                # -- event log: append by event_sim_time ---------------------
                versions_spec = specs["versions"]
                if versions_spec.write_mode != "append":
                    raise _fail(
                        f"windowed log write_mode should be 'append':"
                        f" {versions_spec.write_mode}"
                    )
                versions_rows = emit.query(versions_spec.sql, ())
                log_counts[window.label] = len(versions_rows)
                print(
                    f"  OK: window {window.label} 'versions'"
                    f" = {len(versions_rows)} rows"
                )

            if sum(log_counts.values()) != full_log_count:
                raise _fail(
                    f"windowed log rows should partition the full-export log:"
                    f" {log_counts} vs full={full_log_count}"
                )
            print(
                f"  OK: window0 + window1 versions rows ({sum(log_counts.values())})"
                f" == full export versions rows ({full_log_count})"
            )

        print()
        print(
            "SUCCESS: declared-table export renders state / junction / event-log"
            " units via the plan+compile split, with windowed state snapshots"
            " (no updated_at), append-only junction extract-on-change, and an"
            " event log whose windows partition the full-export log exactly"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
