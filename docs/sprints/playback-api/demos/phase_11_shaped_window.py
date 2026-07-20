#!/usr/bin/env python
"""
Demo: tier-2 window

Sprint: playback-api
Phase: 11

Builds a minimal standalone emit (run.duckdb + base.json) with:
  - records__gadget: untracked, record_roles role 'dimension' -> a type-1 dim.
  - records__actor: tracked (prop__status), 2 history points (sim_time 0, 10)
    -> an SCD-2 dim.

Opens a dimensional shape (the type-1 dim plus the SCD-2 dim) and drives
three consecutive windows: [0, 4), [4, 8), [8, 12). Shows:
  - the type-1 dim delivers its full current-state snapshot every window
    (same content each time, 'snapshot' delivery);
  - the SCD-2 dim's append-class version rows accumulate across the three
    windows to exactly the content of one wide window([0, 12)) — no
    duplicates, no gaps — as the physical projection (__valid_from_ns, no
    valid_to materialized).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import (
    ColumnDecl,
    DerivedSpec,
    DimensionalConfig,
    ExportConfig,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.playback.shaped import ShapedTable, open_shaped_playback
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"

_GADGET_COLUMNS: list[dict[str, object]] = [
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

_ACTOR_COLUMNS: list[dict[str, object]] = [
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


def _col_ddl(columns: list[dict[str, object]]) -> str:
    """Build a CREATE TABLE column-list fragment."""
    return ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)


def _build_emit(emit_dir: Path) -> None:
    """Write a minimal run.duckdb + base.json emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(f'CREATE TABLE "records__gadget" ({_col_ddl(_GADGET_COLUMNS)})')
    conn.execute(f'CREATE TABLE "records__actor" ({_col_ddl(_ACTOR_COLUMNS)})')
    conn.execute(f'CREATE TABLE "history" ({_col_ddl(_HISTORY_COLUMNS)})')

    conn.execute(
        'INSERT INTO "records__gadget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        [_FORK_PATH, "g1", 0, True, 0, 0, "Widget-A"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        [_FORK_PATH, "a1", 0, True, 10, 1, "assembled"],
    )
    for row in (
        (_FORK_PATH, "actor", "a1", "status", 0, "new"),
        (_FORK_PATH, "actor", "a1", "status", 10, "assembled"),
    ):
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": "records__gadget",
                "category": "records",
                "columns": _GADGET_COLUMNS,
                "rows": 1,
                "record_kind": "gadget",
            },
            {
                "name": "records__actor",
                "category": "records",
                "columns": _ACTOR_COLUMNS,
                "rows": 1,
                "record_kind": "actor",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 2,
            },
        ],
        "runtime": {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"},
        "record_roles": {"gadget": "dimension"},
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _from_col(name: str, src: str) -> ColumnDecl:
    """Build a ColumnDecl projecting `src` verbatim into output column `name`."""
    return ColumnDecl(name=name, **{"from": src})


def _dimensional_shape_config() -> ExportConfig:
    """A type-1 dim (dim_gadget) and an SCD-2 dim (dim_actor_status)."""
    return ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_gadget",
                    role="dim",
                    scd="type1",
                    source=SourceDecl(grain="records", kind="gadget"),
                    key=["id"],
                    columns=[
                        _from_col("id", "record_id"),
                        _from_col("name", "prop__name"),
                    ],
                ),
                TableDecl(
                    name="dim_actor_status",
                    role="dim",
                    scd="type2",
                    source=SourceDecl(grain="records", kind="actor"),
                    key=["id", "valid_from"],
                    columns=[
                        _from_col("id", "record_id"),
                        _from_col("status", "prop__status"),
                        ColumnDecl(
                            name="valid_from",
                            derived=DerivedSpec(scd_window="valid_from"),
                        ),
                        ColumnDecl(
                            name="valid_to",
                            derived=DerivedSpec(scd_window="valid_to"),
                        ),
                    ],
                ),
            ]
        ),
    )


def _discard_notice(_notice: object) -> None:
    """Swallow plan notices — this demo is indifferent to them."""


def _by_name(tables: tuple[ShapedTable, ...]) -> dict[str, ShapedTable]:
    return {t.name: t for t in tables}


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        _build_emit(emit_dir)

        with open_emit(emit_dir) as emit:
            head = open_shaped_playback(
                emit, _dimensional_shape_config(), None, _discard_notice
            )

            print("=== three consecutive windows: [0, 4), [4, 8), [8, 12) ===")
            windows = [(0, 4), (4, 8), (8, 12)]
            per_window = [_by_name(head.window(start, end)) for start, end in windows]
            for (start, end), tables in zip(windows, per_window):
                gadget = tables["dim_gadget"]
                actor = tables["dim_actor_status"]
                actor_ns = sorted(actor.table.column("__valid_from_ns").to_pylist())
                print(
                    f"window [{start}, {end}): dim_gadget delivery="
                    f"{gadget.delivery!r} rows={gadget.table.num_rows};"
                    f" dim_actor_status delivery={actor.delivery!r}"
                    f" __valid_from_ns={actor_ns}"
                )

            print("\n=== one wide window: [0, 12) ===")
            wide = _by_name(head.window(0, 12))
            wide_actor = wide["dim_actor_status"]
            print(
                f"window [0, 12): dim_actor_status rows={wide_actor.table.num_rows}"
                f" __valid_from_ns="
                f"{sorted(wide_actor.table.column('__valid_from_ns').to_pylist())}"
            )

        # Type-1 dim: 'snapshot' delivery, same full content every window.
        gadget_tables = [tables["dim_gadget"] for tables in per_window]
        for gadget in gadget_tables:
            if gadget.delivery != "snapshot" or gadget.table.num_rows != 1:
                print(
                    "FAIL: dim_gadget must be a full 1-row snapshot every window",
                    file=sys.stderr,
                )
                raise SystemExit(1)
        if not all(
            g.table.to_pydict() == gadget_tables[0].table.to_pydict()
            for g in gadget_tables
        ):
            print(
                "FAIL: dim_gadget content must be identical every window",
                file=sys.stderr,
            )
            raise SystemExit(1)

        # SCD-2 append: no valid_to materialized, __valid_from_ns present.
        for tables in per_window:
            actor = tables["dim_actor_status"]
            if actor.delivery != "append":
                print(
                    "FAIL: dim_actor_status must be 'append' delivery", file=sys.stderr
                )
                raise SystemExit(1)
            if "valid_to" in actor.table.schema.names:
                print(
                    "FAIL: windowed SCD-2 must not materialize valid_to",
                    file=sys.stderr,
                )
                raise SystemExit(1)

        # Accumulation: the union of the three windows' version rows equals
        # the content of one wide window, no duplicates or gaps.
        accumulated_ns = sorted(
            ns
            for tables in per_window
            for ns in tables["dim_actor_status"]
            .table.column("__valid_from_ns")
            .to_pylist()
        )
        wide_ns = sorted(wide_actor.table.column("__valid_from_ns").to_pylist())
        if accumulated_ns != wide_ns:
            print(
                f"FAIL: accumulated windows {accumulated_ns} != wide window {wide_ns}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if len(accumulated_ns) != len(set(accumulated_ns)):
            print("FAIL: accumulated windows contain a duplicate row", file=sys.stderr)
            raise SystemExit(1)

        print(
            "\nSUCCESS: three consecutive windows' append-class SCD-2 rows"
            " accumulate to exactly the wide window's content (no duplicates,"
            " no gaps), the physical projection carries no valid_to, and the"
            " type-1 dim delivers its full current-state snapshot every window"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
