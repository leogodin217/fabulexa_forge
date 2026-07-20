#!/usr/bin/env python
"""
Demo: the base_relations compile indirection (name-shadowing wrap)

Sprint: playback-api
Phase: 8

Builds a minimal standalone emit (run.duckdb + base.json) with two kinds
sharing one `history` table:

  journey_instance j1: state history waiting@5, in_progress@15, completed@25.
  widget w1: prop__name (tracked) history alpha@0, beta@20.

Compiles one dimensional shape (fact_state_changes: history_point grain over
journey_instance.state) and one source shape (widget's change-log genre) each
twice: base_relations=None (byte-identical to the pre-parameter compile,
proven by comparing against the underlying grain/render builder called
directly) and with a truncation-shaped replacing relation for `history`
(rows with sim_time <= 15) — showing the shadowed row set drops every row
that truncation excludes, with no physical leak.
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
from fabulexa_forge.derivations import require_single_branch
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.dimensional.grains import build_grain_sql
from fabulexa_forge.exporters.dimensional.validation import validate_table
from fabulexa_forge.exporters.source.engine import build_source_query_specs
from fabulexa_forge.exporters.source.plan import build_source_plan
from fabulexa_forge.exporters.source.renders import build_render_sql
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"
_TRUNCATE_AT = 15  # inclusive: keeps sim_time <= 15, drops later rows

_JOURNEY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__state",
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
        "name": "prop__name",
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

_HISTORY_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "journey_instance", "j1", "state", 5, "waiting"),
    (_FORK_PATH, "journey_instance", "j1", "state", 15, "in_progress"),
    (_FORK_PATH, "journey_instance", "j1", "state", 25, "completed"),
    (_FORK_PATH, "widget", "w1", "name", 0, "alpha"),
    (_FORK_PATH, "widget", "w1", "name", 20, "beta"),
]


def _col_ddl(columns: list[dict[str, object]]) -> str:
    """Build a CREATE TABLE column-list fragment."""
    return ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)


def _build_emit(emit_dir: Path) -> None:
    """Write a minimal run.duckdb + base.json emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(
        f'CREATE TABLE "records__journey_instance" ({_col_ddl(_JOURNEY_COLUMNS)})'
    )
    conn.execute(f'CREATE TABLE "records__widget" ({_col_ddl(_WIDGET_COLUMNS)})')
    conn.execute(f'CREATE TABLE "history" ({_col_ddl(_HISTORY_COLUMNS)})')

    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        [_FORK_PATH, "j1", 5, True, 25, 0, "completed"],
    )
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        [_FORK_PATH, "w1", 0, True, 20, 0, "beta"],
    )
    for row in _HISTORY_ROWS:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": "records__journey_instance",
                "category": "records",
                "columns": _JOURNEY_COLUMNS,
                "rows": 1,
                "record_kind": "journey_instance",
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
                "rows": len(_HISTORY_ROWS),
            },
        ],
        "runtime": {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"},
        "record_roles": {"widget": "dimension", "journey_instance": "dimension"},
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _dimensional_config() -> DimensionalConfig:
    """The fact_state_changes shape: history_point over journey_instance.state."""
    return DimensionalConfig(
        tables=[
            TableDecl(
                name="fact_state_changes",
                role="fact",
                source=SourceDecl(
                    grain="history_point", kind="journey_instance", property="state"
                ),
                key=["record_id"],
                columns=[
                    ColumnDecl(name="record_id", **{"from": "record_id"}),
                    ColumnDecl(name="new_state", **{"from": "value"}),
                    ColumnDecl(name="changed_at", **{"from": "sim_time"}),
                ],
            )
        ]
    )


def _discard_notice(_notice: object) -> None:
    """Swallow plan notices — this demo is indifferent to them."""


def _truncation_relations() -> dict[str, str]:
    """A truncation-shaped replacing relation for `history` (sim_time <= T).

    Schema-qualifies its own self-read (main."history") — DuckDB's binder
    treats a bare same-named self-read as a circular CTE reference rather
    than resolving it outward to the physical table.
    """
    return {
        "history": (
            'SELECT * FROM main."history"'
            f' WHERE "fork_path" = \'{_FORK_PATH}\' AND "sim_time" <= {_TRUNCATE_AT}'
        )
    }


def _demo_dimensional(emit_dir: Path) -> None:
    print("=== dimensional: fact_state_changes (history_point) ===")
    config = _dimensional_config()
    with open_emit(emit_dir) as emit:
        specs_none = build_query_specs(
            emit, config, None, None, notice_sink=_discard_notice, base_relations=None
        )

        sidecar = emit.sidecar
        fork_path = require_single_branch(sidecar)
        table_decl = config.tables[0]
        source_table_name = validate_table(
            table_decl, config, sidecar, None, _discard_notice
        )
        sql_direct, _, _, _ = build_grain_sql(
            table_decl, source_table_name, sidecar, None, fork_path, config, None
        )
        assert specs_none[0].sql == sql_direct, (
            "base_relations=None must be byte-identical"
        )
        print("None: byte-identical to the unparameterized compile — confirmed")

        physical_rows = emit.query(specs_none[0].sql, ())
        print(f"physical: {physical_rows}")

        specs_shadowed = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=_discard_notice,
            base_relations=_truncation_relations(),
        )
        shadowed_rows = emit.query(specs_shadowed[0].sql, ())
        print(f"shadowed (history truncated at T={_TRUNCATE_AT}): {shadowed_rows}")

    if len(physical_rows) != 3:
        print(f"FAIL: expected 3 physical rows, got {physical_rows}", file=sys.stderr)
        raise SystemExit(1)
    if len(shadowed_rows) != 2:
        print(f"FAIL: expected 2 shadowed rows, got {shadowed_rows}", file=sys.stderr)
        raise SystemExit(1)


def _demo_source(emit_dir: Path) -> None:
    print("=== source: widget (change-log genre) ===")
    config = ExportConfig(mode="source")
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None

        specs_none = build_source_query_specs(
            emit, config, anchor, None, notice_sink=_discard_notice, base_relations=None
        )
        widget_none = next(s for s in specs_none if s.table_name == "widget")

        sidecar = emit.sidecar
        fork_path = require_single_branch(sidecar)
        table_specs = build_source_plan(sidecar, config.source, _discard_notice)
        widget_spec = next(t for t in table_specs if t.name == "widget")
        sql_direct = build_render_sql(sidecar, fork_path, widget_spec, anchor, None)
        assert widget_none.sql == sql_direct, (
            "base_relations=None must be byte-identical"
        )
        print("None: byte-identical to the unparameterized compile — confirmed")

        physical_rows = emit.query(widget_none.sql, ())
        print(f"physical: {physical_rows}")

        specs_shadowed = build_source_query_specs(
            emit,
            config,
            anchor,
            None,
            notice_sink=_discard_notice,
            base_relations=_truncation_relations(),
        )
        widget_shadowed = next(s for s in specs_shadowed if s.table_name == "widget")
        shadowed_rows = emit.query(widget_shadowed.sql, ())
        print(f"shadowed (history truncated at T={_TRUNCATE_AT}): {shadowed_rows}")

    if len(physical_rows) != 2:
        print(f"FAIL: expected 2 physical rows, got {physical_rows}", file=sys.stderr)
        raise SystemExit(1)
    if len(shadowed_rows) != 1:
        print(f"FAIL: expected 1 shadowed row, got {shadowed_rows}", file=sys.stderr)
        raise SystemExit(1)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        _build_emit(emit_dir)

        _demo_dimensional(emit_dir)
        _demo_source(emit_dir)

        print(
            "SUCCESS: base_relations=None is byte-identical to the unparameterized"
            " compile; a truncation-shaped history mapping shadows both the"
            " dimensional history_point read and the source change-log read,"
            " with no physical leak"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
