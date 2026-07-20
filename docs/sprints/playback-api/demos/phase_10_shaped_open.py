#!/usr/bin/env python
"""
Demo: tier-2 open and tables

Sprint: playback-api
Phase: 10

Builds a minimal standalone emit (run.duckdb + base.json) with:
  - records__gadget: untracked, record_roles role 'dimension'.
  - records__shipment: untracked, record_roles role 'fact'.
  - records__widget: tracked (prop__status), plus a membership__widget__parts
    table (owner widget, property 'parts').

Opens two shaped heads over the same emit — a dimensional shape (a type-1
dim, a records-grain fact, and a membership-grain fact) and a bare source
shape (a full dump) — and prints `ShapedPlayback.tables()` for each: the
declared output tables, in the shape's canonical order, tagged with their
static `window_delivery` class. The dimensional shape's membership-grain
table declares `window_delivery=None` — the windowed-grain rule's rejection
case, diagnostic at open rather than a silent skip.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    ColumnDecl,
    DimensionalConfig,
    ExportConfig,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.playback.shaped import ShapedTableDecl, open_shaped_playback
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

_SHIPMENT_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__amount",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_WIDGET_COLUMNS: list[dict[str, object]] = [
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

_WIDGET_PARTS_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__name", "type": "VARCHAR"},
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
    conn.execute(f'CREATE TABLE "records__shipment" ({_col_ddl(_SHIPMENT_COLUMNS)})')
    conn.execute(f'CREATE TABLE "records__widget" ({_col_ddl(_WIDGET_COLUMNS)})')
    conn.execute(f'CREATE TABLE "history" ({_col_ddl(_HISTORY_COLUMNS)})')
    conn.execute(
        f'CREATE TABLE "membership__widget__parts" ({_col_ddl(_WIDGET_PARTS_COLUMNS)})'
    )

    conn.execute(
        'INSERT INTO "records__gadget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        [_FORK_PATH, "g1", 0, True, 0, 0, "Widget-A"],
    )
    conn.execute(
        'INSERT INTO "records__shipment" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        [_FORK_PATH, "s1", 0, True, 0, 1, "100"],
    )
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        [_FORK_PATH, "w1", 0, True, 10, 2, "assembled"],
    )
    for row in (
        (_FORK_PATH, "widget", "w1", "status", 0, "new"),
        (_FORK_PATH, "widget", "w1", "status", 10, "assembled"),
    ):
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.execute(
        'INSERT INTO "membership__widget__parts" VALUES (?, ?, ?, NULL, ?)',
        [_FORK_PATH, "w1", 5, "bolt"],
    )
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
                "name": "records__shipment",
                "category": "records",
                "columns": _SHIPMENT_COLUMNS,
                "rows": 1,
                "record_kind": "shipment",
            },
            {
                "name": "records__widget",
                "category": "records",
                "columns": _WIDGET_COLUMNS,
                "rows": 1,
                "record_kind": "widget",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 2,
            },
            {
                "name": "membership__widget__parts",
                "category": "membership",
                "columns": _WIDGET_PARTS_COLUMNS,
                "rows": 1,
                "record_kind": "widget",
                "property": "parts",
            },
        ],
        "runtime": {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"},
        "record_roles": {"gadget": "dimension", "shipment": "fact"},
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _from_col(name: str, src: str) -> ColumnDecl:
    """Build a ColumnDecl projecting `src` verbatim into output column `name`."""
    return ColumnDecl(name=name, **{"from": src})


def _dimensional_shape_config() -> ExportConfig:
    """A type-1 dim, a records-grain fact, and a membership-grain fact."""
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
                    name="fact_shipment",
                    role="fact",
                    source=SourceDecl(grain="records", kind="shipment"),
                    key=["id"],
                    columns=[
                        _from_col("id", "record_id"),
                        _from_col("amount", "prop__amount"),
                    ],
                ),
                TableDecl(
                    name="mem_widget_parts",
                    role="fact",
                    source=SourceDecl(
                        grain="membership", kind="widget", property="parts"
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("part_name", "elem__name"),
                    ],
                ),
            ]
        ),
    )


def _discard_notice(_notice: object) -> None:
    """Swallow plan notices — this demo is indifferent to them."""


def _print_decls(label: str, decls: tuple[ShapedTableDecl, ...]) -> None:
    print(f"{label}:")
    for decl in decls:
        print(f"  {decl.name}: window_delivery={decl.window_delivery!r}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        _build_emit(emit_dir)

        with open_emit(emit_dir) as emit:
            print("=== dimensional shape ===")
            dim_head = open_shaped_playback(
                emit, _dimensional_shape_config(), None, _discard_notice
            )
            dim_decls = dim_head.tables()
            _print_decls("dimensional tables()", dim_decls)

            print("\n=== source shape (bare full dump) ===")
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            source_head = open_shaped_playback(
                emit, ExportConfig(mode="source"), anchor, _discard_notice
            )
            source_decls = source_head.tables()
            _print_decls("source tables()", source_decls)

        expected_dim = (
            ShapedTableDecl(name="dim_gadget", window_delivery="snapshot"),
            ShapedTableDecl(name="fact_shipment", window_delivery="append"),
            ShapedTableDecl(name="mem_widget_parts", window_delivery=None),
        )
        if dim_decls != expected_dim:
            print(f"FAIL: dimensional tables() mismatch: {dim_decls}", file=sys.stderr)
            raise SystemExit(1)

        membership_decl = next(d for d in dim_decls if d.name == "mem_widget_parts")
        if membership_decl.window_delivery is not None:
            print(
                "FAIL: membership-grain table must declare window_delivery=None",
                file=sys.stderr,
            )
            raise SystemExit(1)

        expected_source_names = ("gadget", "shipment", "widget", "widget_parts")
        if tuple(d.name for d in source_decls) != expected_source_names:
            print(f"FAIL: source tables() mismatch: {source_decls}", file=sys.stderr)
            raise SystemExit(1)

        print(
            "\nSUCCESS: open_shaped_playback validates each shape sidecar-only at"
            " open and ShapedPlayback.tables() reports every declared output"
            " table's static window_delivery class, including None for the"
            " membership-grain table the windowed-grain rule rejects"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
