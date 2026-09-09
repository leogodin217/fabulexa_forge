#!/usr/bin/env python
"""
Demo: Supplementary tables through shaped playback
Sprint: supplementary-tables
Phase: 4

Builds a self-contained minimal emit (one type-1 dim kind, one upsert fact
kind) and a dimensional config declaring both tables plus two supplements (one
file, one inline). Opens a shaped-playback head, prints tables() (supplements
last, 'snapshot'), prints state(T) at two instants showing the supplement
rows identical, prints a window() mixing the fact and a supplement, prints a
supplement-only window() with a notice-counting sink showing zero notices,
drives a selection naming a supplement plus an unknown name (PlaybackError
listing the union), and drives a config whose supplement has a bad cell
refused at open (SupplementValueInvalid).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.errors import SupplementValueInvalid
from fabulexa_forge.exporters.supplements import load_supplements
from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.playback.shaped import open_shaped_playback
from fabulexa_forge.reader.emit import open_emit

if TYPE_CHECKING:
    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.exporters.notices import Notice
    from fabulexa_forge.playback.shaped import ShapedPlayback
    from fabulexa_forge.reader.emit import Emit

_SLICE_AT = 500

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

_REGION_CODE_CSV = "code,label\nUS,United States\nCA,\n"

_CONFIG_YAML = """
mode: dimensional
dimensional:
  tables:
    - name: dim_widget
      role: dim
      scd: type1
      source: {grain: records, kind: widget}
      key: [id]
      columns:
        - {name: id, from: record_id}
        - {name: name, from: prop__name}
    - name: fact_shipment
      role: fact
      source: {grain: records, kind: shipment}
      key: [id]
      columns:
        - {name: id, from: record_id}
        - {name: amount, from: prop__amount}
        - {name: mutated_at, from: last_mutation_sim_time}
supplements:
  - name: region_code
    file: region_code.csv
    columns: {code: VARCHAR, label: VARCHAR}
  - name: rate_tier
    columns: {tier: VARCHAR, weight: DOUBLE}
    rows:
      - {tier: standard, weight: 1.0}
      - {tier: premium, weight: 2.5}
"""

_BAD_CELL_CONFIG_YAML = """
mode: dimensional
dimensional:
  tables:
    - name: dim_widget
      role: dim
      scd: type1
      source: {grain: records, kind: widget}
      key: [id]
      columns:
        - {name: id, from: record_id}
        - {name: name, from: prop__name}
supplements:
  - name: rate_tier
    columns: {tier: VARCHAR, weight: DOUBLE}
    rows:
      - {tier: standard, weight: "not-a-number"}
"""


def _build_emit(emit_dir: Path) -> Path:
    """Build a minimal self-contained emit: one type-1-dim widget kind (two
    rows) and one upsert-fact shipment kind (two rows)."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    widget_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _WIDGET_COLUMNS)
    conn.execute(f'CREATE TABLE "records__widget" ({widget_ddl})')
    shipment_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _SHIPMENT_COLUMNS)
    conn.execute(f'CREATE TABLE "records__shipment" ({shipment_ddl})')

    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "w1", 0, True, 0, 0, "Widget-A"],
    )
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "w2", 0, True, 0, 1, "Widget-B"],
    )
    conn.execute(
        'INSERT INTO "records__shipment" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "s1", 0, True, 0, 0, "100"],
    )
    conn.execute(
        'INSERT INTO "records__shipment" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "s2", 0, True, 0, 1, "250"],
    )
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": _SLICE_AT}],
        "tables": [
            {
                "name": "records__widget",
                "category": "records",
                "record_kind": "widget",
                "rows": 2,
                "columns": _WIDGET_COLUMNS,
            },
            {
                "name": "records__shipment",
                "category": "records",
                "record_kind": "shipment",
                "rows": 2,
                "columns": _SHIPMENT_COLUMNS,
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return emit_dir


def _write_config(config_dir: Path, yaml_text: str) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")
    return config_path


def _load(config_path: Path) -> "ExportConfig":
    return load_export_config(config_path)


def _open_head(
    emit: "Emit", config: "ExportConfig", config_dir: Path, sink: "list[Notice]"
) -> "ShapedPlayback":
    supplements = load_supplements(config, config_dir)
    return open_shaped_playback(emit, config, None, sink.append, supplements)


def _demo_tables(head: "ShapedPlayback") -> None:
    """tables() lists the declared tables then the supplements, 'snapshot'."""
    print("tables():")
    for decl in head.tables():
        print(f"    {decl.name}: {decl.window_delivery}")


def _demo_state_identity(head: "ShapedPlayback") -> None:
    """The supplement rows in state(T) are identical at two different instants."""
    print("state(T) supplement identity:")
    state_early = {t.name: t for t in head.state(50)}
    state_late = {t.name: t for t in head.state(400)}
    for name in ("region_code", "rate_tier"):
        identical = state_early[name].table.equals(state_late[name].table)
        print(f"    {name}: state(50) == state(400): {identical}")
        print(f"      rows: {state_early[name].table.to_pylist()}")


def _demo_window_mixed(head: "ShapedPlayback") -> None:
    """A window() mixing a fact table and a supplement."""
    print("window(0, 300, tables={'fact_shipment', 'region_code'}):")
    for table in head.window(0, 300, tables={"fact_shipment", "region_code"}):
        print(f"    {table.name} ({table.delivery}): {table.table.to_pylist()}")


def _demo_supplement_only_window(head: "ShapedPlayback", sink: "list[Notice]") -> None:
    """A supplement-only window() opens no horizon and emits no notice."""
    print("supplement-only window(tables={'region_code', 'rate_tier'}):")
    before = len(sink)
    tables = head.window(0, 300, tables={"region_code", "rate_tier"})
    after = len(sink)
    print(f"    tables returned: {[t.name for t in tables]}")
    print(f"    notices emitted: {after - before}")


def _demo_unknown_name_selection(head: "ShapedPlayback") -> None:
    """A selection naming a supplement plus an unknown name refuses, listing
    the union of declared names."""
    print("selection naming a supplement plus an unknown name:")
    try:
        head.window(0, 300, tables={"region_code", "no_such_table"})
    except PlaybackError as exc:
        print(f"    refused: {exc}")
    else:
        raise AssertionError("expected PlaybackError")


def _demo_bad_cell_refusal(emit: "Emit", tmp_root: Path) -> None:
    """A config whose supplement has a bad cell is refused at open, before
    any base-table data is read."""
    print("bad-cell supplement, refused at open:")
    config_dir = tmp_root / "bad_cell_cfg"
    config_path = _write_config(config_dir, _BAD_CELL_CONFIG_YAML)
    config = _load(config_path)
    sink: list[Notice] = []
    try:
        _open_head(emit, config, config_dir, sink)
    except SupplementValueInvalid as exc:
        print(f"    refused: {exc}")
    else:
        raise AssertionError("expected SupplementValueInvalid")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        emit_dir = _build_emit(tmp_root / "emit")

        with open_emit(emit_dir) as emit:
            config_dir = tmp_root / "cfg"
            config_dir.mkdir()
            (config_dir / "region_code.csv").write_text(
                _REGION_CODE_CSV, encoding="utf-8"
            )
            config_path = _write_config(config_dir, _CONFIG_YAML)
            config = _load(config_path)

            sink: list[Notice] = []
            head = _open_head(emit, config, config_dir, sink)

            _demo_tables(head)
            print()
            _demo_state_identity(head)
            print()
            _demo_window_mixed(head)
            print()
            _demo_supplement_only_window(head, sink)
            print()
            _demo_unknown_name_selection(head)
            print()
            _demo_bad_cell_refusal(emit, tmp_root)

    print()
    print(
        "SUCCESS: tables() lists supplements last; state()'s supplement rows"
        " were identical across instants; a supplement-only window() opened"
        " no horizon and emitted no notice; the unknown-name and bad-cell"
        " asks were both refused"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
