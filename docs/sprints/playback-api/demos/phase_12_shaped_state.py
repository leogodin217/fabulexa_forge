#!/usr/bin/env python
"""
Demo: tier-2 state and the bridging theorem

Sprint: playback-api
Phase: 12

Builds a minimal standalone emit (run.duckdb + base.json) with:
  - records__gadget: untracked, record_roles role 'dimension' -> a type-1 dim.
  - records__actor: tracked (prop__status), 3 history points (sim_time 0, 10,
    20 -> "new" / "assembled" / "shipped") -> an SCD-2 dim.

Opens a dimensional shape (the type-1 dim plus the SCD-2 dim) and shows:
  - state(T) at an interior T=12 (between the 10 and 20 events): the SCD-2
    dim reconstructs as an as-of-T star schema — the change point at sim_time
    20 is not yet visible, and the version starting at 10 ("assembled") is
    the latest, still open (valid_to NULL);
  - state(T_slice) diffed empty against the shape's full export — the
    bridging theorem: truncation at the slice bound is the identity
    presentation of the tape, so state() at the emit's own slice bound is
    value-identical to a plain full export of the same shape.
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
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.query_spec import query_spec_output_name
from fabulexa_forge.playback.shaped import ShapedTable, open_shaped_playback
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"
_SLICE_AT = 100

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
    """Write a minimal run.duckdb + base.json emit into emit_dir.

    last_mutation_sim_time is self-consistent with the recorded trail on
    every record (the bridging theorem's declared precondition, § Shaped
    state, The recorded trail): gadget's is 0 (no history — matches
    created_sim_time), actor's is 20 (its latest tracked history instant).
    """
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
        [_FORK_PATH, "a1", 0, True, 20, 1, "shipped"],
    )
    for row in (
        (_FORK_PATH, "actor", "a1", "status", 0, "new"),
        (_FORK_PATH, "actor", "a1", "status", 10, "assembled"),
        (_FORK_PATH, "actor", "a1", "status", 20, "shipped"),
    ):
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": _SLICE_AT}],
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
                "rows": 3,
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
            config = _dimensional_shape_config()
            head = open_shaped_playback(emit, config, None, _discard_notice)

            print("=== state(12): an as-of-T star schema, interior T ===")
            at_12 = _by_name(head.state(12))
            actor_12 = at_12["dim_actor_status"]
            rows_12 = {r["status"]: r["valid_to"] for r in actor_12.table.to_pylist()}
            print(f"dim_actor_status delivery={actor_12.delivery!r} rows={rows_12}")

            if set(rows_12) != {"new", "assembled"}:
                print(
                    "FAIL: state(12) must not see the sim_time=20 change point"
                    f" yet — got {set(rows_12)}",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            if rows_12["assembled"] is not None:
                print(
                    "FAIL: 'assembled' must be the latest version, still open"
                    " (valid_to NULL) at T=12",
                    file=sys.stderr,
                )
                raise SystemExit(1)

            print("\n=== the bridging theorem: state(slice) vs the full export ===")
            at_slice = _by_name(head.state(_SLICE_AT))

            assert config.dimensional is not None
            full_specs = build_query_specs(
                emit,
                config.dimensional,
                None,
                None,
                _discard_notice,
                base_relations=None,
            )
            full_by_name = {
                query_spec_output_name(spec): emit.query_arrow(spec.sql, ()).to_pydict()
                for spec in full_specs
            }

        diffs: list[str] = []
        for name, table in at_slice.items():
            stated_rows = table.table.to_pydict()
            full_rows = full_by_name[name]
            if stated_rows != full_rows:
                diffs.append(f"{name}: state(slice)={stated_rows} full={full_rows}")
            if table.delivery != "snapshot":
                diffs.append(f"{name}: state() delivery must be 'snapshot'")

        print(f"tables compared: {sorted(at_slice)}")
        print(f"diff against the full export: {diffs if diffs else '[] (empty)'}")

        if diffs:
            print("FAIL: state(T_slice) diverged from the shape's full export")
            raise SystemExit(1)

        print(
            "\nSUCCESS: state(T) at an interior T reconstructs an as-of-T star"
            " schema with no visible future change point, and state(T_slice)"
            " is value-identical to the shape's full export (the bridging"
            " theorem) — every table, empty diff"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
