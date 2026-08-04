#!/usr/bin/env python
"""
Demo: List-valued predicates through the config envelope and the dimensional
exporter — the motivating multi-process fact table, end to end.

Sprint: list-valued-predicates
Phase: 2

Builds a minimal emit with:
  - `tick_decision` — an untracked fact kind carrying a `prop__decision_type`
    discriminator across five clinical processes.
  - `entity` — a records kind carrying a `prop__entity_type` discriminator,
    the source for a scalar-filtered dim.

A YAML export config groups four of the five `prop__decision_type` values
into one fact table (`fact_emergency_care`) via a list-valued `filter`,
alongside a `dim_ward` unchanged by a scalar filter — the "one table per
process" case a scalar `filter` cannot express (design doc § Problem). Runs
the dimensional export end to end and prints both tables' rows.

Then shows the three parse-time rejections `PredicateValue` and
`ElapsedSpec.other_where_non_empty` introduce: an empty list, a
duplicate-bearing list, and an empty `other_where` mapping — each rejected
with a `ConfigError` naming the offending field.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.errors import ConfigError
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.exporters.notices import render_notice_stderr
from fabulexa_forge.reader.emit import open_emit

_TICK_DECISION_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__decision_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_ENTITY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__entity_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

# Five clinical-process decision types; the fact table's filter groups four
# of them (surgery_performed is left out — it stays unobserved by the fact).
_TICK_DECISION_ROWS: list[tuple[object, ...]] = [
    ("trunk", "td1", 10, True, None, 10, 0, "ed_arrival"),
    ("trunk", "td2", 11, True, None, 11, 1, "triage"),
    ("trunk", "td3", 12, True, None, 12, 2, "ed_assessment"),
    ("trunk", "td4", 13, True, None, 13, 3, "ed_diagnosis"),
    ("trunk", "td5", 14, True, None, 14, 4, "surgery_performed"),
]

_ENTITY_ROWS: list[tuple[object, ...]] = [
    ("trunk", "e1", 5, True, None, 5, 0, "ward"),
    ("trunk", "e2", 6, True, None, 6, 1, "ward"),
    ("trunk", "e3", 7, True, None, 7, 2, "bed"),
]

_LIST_FILTER_CONFIG_YAML = """
mode: dimensional
dimensional:
  tables:
    - name: fact_emergency_care
      role: fact
      source:
        grain: records
        kind: tick_decision
        filter:
          prop__decision_type: [ed_arrival, triage, ed_assessment, ed_diagnosis]
      key: [event_id]
      columns:
        - {name: event_id, from: record_id}
        - {name: decision_type, from: prop__decision_type}
    - name: dim_ward
      role: dim
      scd: type1
      source:
        grain: records
        kind: entity
        filter: {prop__entity_type: ward}
      key: [ward_id]
      columns:
        - {name: ward_id, from: record_id}
"""

_EMPTY_LIST_CONFIG_YAML = """
mode: dimensional
dimensional:
  tables:
    - name: fact_bad
      role: fact
      source:
        grain: records
        kind: tick_decision
        filter:
          prop__decision_type: []
      key: [event_id]
      columns:
        - {name: event_id, from: record_id}
"""

_DUPLICATE_ELEMENT_CONFIG_YAML = """
mode: dimensional
dimensional:
  tables:
    - name: fact_bad
      role: fact
      source:
        grain: records
        kind: tick_decision
        filter:
          prop__decision_type: [ed_arrival, ed_arrival]
      key: [event_id]
      columns:
        - {name: event_id, from: record_id}
"""

_EMPTY_OTHER_WHERE_CONFIG_YAML = """
mode: dimensional
dimensional:
  tables:
    - name: fact_bad
      role: fact
      source:
        grain: records
        kind: tick_decision
      key: [event_id]
      columns:
        - {name: event_id, from: record_id}
        - name: elapsed_minutes
          derived:
            elapsed:
              correlate_on: event_id
              other_where: {}
              start_source: created_sim_time
              end_source: created_sim_time
              unit: minutes
"""


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


def _build_emit(emit_dir: Path) -> None:
    """Write the two-kind emit (tick_decision + entity) into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_ddl("records__tick_decision", _TICK_DECISION_COLUMNS))
    conn.execute(_ddl("records__entity", _ENTITY_COLUMNS))
    _insert_all(
        conn, "records__tick_decision", _TICK_DECISION_COLUMNS, _TICK_DECISION_ROWS
    )
    _insert_all(conn, "records__entity", _ENTITY_COLUMNS, _ENTITY_ROWS)
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
            {
                "name": "records__tick_decision",
                "category": "records",
                "record_kind": "tick_decision",
                "columns": _TICK_DECISION_COLUMNS,
                "rows": len(_TICK_DECISION_ROWS),
            },
            {
                "name": "records__entity",
                "category": "records",
                "record_kind": "entity",
                "columns": _ENTITY_COLUMNS,
                "rows": len(_ENTITY_ROWS),
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _write_yaml(tmp_dir: Path, name: str, text: str) -> Path:
    path = tmp_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def _fail(message: str) -> "SystemExit":
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def demo_list_filter_export(tmp_dir: Path, emit_dir: Path) -> None:
    """Run the list-valued-filter fact + scalar-filtered dim end to end."""
    print("=== list-valued filter groups four decision types into one table ===")
    config_path = _write_yaml(tmp_dir, "list_filter.yaml", _LIST_FILTER_CONFIG_YAML)
    config = load_export_config(config_path)

    out_path = tmp_dir / "out.duckdb"
    with open_emit(emit_dir) as emit:
        counts = export_dimensional(
            emit, config, out_path, "duckdb", None, notice_sink=render_notice_stderr
        )

    out_conn = duckdb.connect(str(out_path), read_only=True)
    fact_rows = out_conn.execute(
        "SELECT event_id, decision_type FROM fact_emergency_care ORDER BY event_id"
    ).fetchall()
    dim_rows = out_conn.execute(
        "SELECT ward_id FROM dim_ward ORDER BY ward_id"
    ).fetchall()
    out_conn.close()

    if counts["fact_emergency_care"] != 4:
        raise _fail(
            f"fact_emergency_care should have 4 rows"
            f" (the filtered decision types); got {counts['fact_emergency_care']}"
        )
    if counts["dim_ward"] != 2:
        raise _fail(f"dim_ward should have 2 ward rows; got {counts['dim_ward']}")

    print(f"  fact_emergency_care ({counts['fact_emergency_care']} rows):")
    for row in fact_rows:
        print(f"    {row}")
    print(f"  dim_ward ({counts['dim_ward']} rows, scalar filter unchanged):")
    for row in dim_rows:
        print(f"    {row}")
    print(
        "  OK: the list-valued filter renders IN over the four grouped values;"
        " the scalar-filtered dim is unaffected"
    )
    print()


def demo_empty_list_rejected(tmp_dir: Path) -> None:
    """An empty list predicate value is a parse-time ConfigError."""
    print("=== rejection: empty list predicate value ===")
    config_path = _write_yaml(tmp_dir, "empty_list.yaml", _EMPTY_LIST_CONFIG_YAML)
    try:
        load_export_config(config_path)
    except ConfigError as exc:
        print(f"  OK: {exc}")
    else:
        raise _fail("expected ConfigError for an empty list predicate value")
    print()


def demo_duplicate_element_rejected(tmp_dir: Path) -> None:
    """A duplicate-bearing list predicate value is a parse-time ConfigError."""
    print("=== rejection: duplicate element in a list predicate value ===")
    config_path = _write_yaml(
        tmp_dir, "duplicate_element.yaml", _DUPLICATE_ELEMENT_CONFIG_YAML
    )
    try:
        load_export_config(config_path)
    except ConfigError as exc:
        print(f"  OK: {exc}")
    else:
        raise _fail("expected ConfigError for a duplicate-bearing list")
    print()


def demo_empty_other_where_rejected(tmp_dir: Path) -> None:
    """An empty `other_where` mapping is a parse-time ConfigError."""
    print("=== rejection: empty other_where mapping ===")
    config_path = _write_yaml(
        tmp_dir, "empty_other_where.yaml", _EMPTY_OTHER_WHERE_CONFIG_YAML
    )
    try:
        load_export_config(config_path)
    except ConfigError as exc:
        print(f"  OK: {exc}")
    else:
        raise _fail("expected ConfigError for an empty other_where mapping")
    print()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        emit_dir = tmp_dir / "emit"
        _build_emit(emit_dir)

        demo_list_filter_export(tmp_dir, emit_dir)
        demo_empty_list_rejected(tmp_dir)
        demo_duplicate_element_rejected(tmp_dir)
        demo_empty_other_where_rejected(tmp_dir)

        print(
            "SUCCESS: a list-valued filter groups several discriminator values"
            " into one table; empty lists, duplicate-bearing lists, and empty"
            " other_where mappings are all rejected at config load time"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
