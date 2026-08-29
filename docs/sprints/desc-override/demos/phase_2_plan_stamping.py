#!/usr/bin/env python
"""
Demo: Each batch-mode plan compiler stamps `QuerySpec.author_descriptions`
from its own config surface, keyed by output name, and the source/base
`descriptions` key gates widen the existing `rename` / `columns` gates.

Sprint: desc-override
Phase: 2

Synthesizes a single-kind emit (`records__member`, one `prop__tier`
property), then compiles a plan in each of the three batch modes from a
config exercising its description surface: dimensional keys directly by the
column entry's own output name; source and base declare `prop__tier` ->
`loyalty_tier` via `rename` / `columns` and address the description by the
same source identity, showing the source-identity -> output-name
translation. Then shows a `descriptions` key naming no projected column
raising `SourceColumnUnresolved` (source) and `BaseRenameUnresolved` (base)
at plan compile, before anything is written.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import duckdb

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fabulexa_forge.exporters.query_spec import QuerySpec

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.errors import BaseRenameUnresolved, SourceColumnUnresolved
from fabulexa_forge.exporters.base.engine import build_base_query_specs
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.source.engine import build_source_query_specs
from fabulexa_forge.exporters.source.plan import build_source_plan
from fabulexa_forge.reader.emit import open_emit

_DIMENSIONAL_CONFIG = """
mode: dimensional
dimensional:
  tables:
    - name: dim_member
      role: dim
      key: [id]
      source:
        grain: records
        kind: member
      columns:
        - name: id
          from: record_id
        - name: loyalty_tier
          from: prop__tier
          description: "The member's loyalty tier, as captured at signup."
"""

_SOURCE_CONFIG = """
mode: source
source:
  tables:
    - name: member_state
      kind: member
      rename:
        prop__tier: loyalty_tier
      descriptions:
        prop__tier: "The member's raw tier code before relabeling."
"""

_SOURCE_BAD_CONFIG = """
mode: source
source:
  tables:
    - name: member_state
      kind: member
      descriptions:
        prop__nonexistent: "This column does not exist."
"""

_BASE_CONFIG = """
mode: base
base:
  rename:
    - table: records__member
      columns:
        prop__tier: loyalty_tier
      descriptions:
        prop__tier: "The member's raw tier code before relabeling."
"""

_BASE_BAD_CONFIG = """
mode: base
base:
  rename:
    - table: records__member
      descriptions:
        prop__nonexistent: "This column does not exist."
"""

_MEMBER_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__tier",
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

_ANCHOR = EffectiveAnchor(
    start_instant=datetime(2024, 1, 1, tzinfo=ZoneInfo("UTC")), timezone=ZoneInfo("UTC")
)


def _write_config(tmp_dir: Path, name: str, text: str) -> Path:
    """Write one example config's YAML to `tmp_dir/name` and return its path."""
    path = tmp_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def _build_emit(emit_dir: Path) -> None:
    """Write a minimal single-kind emit: records__member plus an empty history."""
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    ddl = ", ".join(f'"{col["name"]}" {col["type"]}' for col in _MEMBER_COLUMNS)
    conn.execute(f'CREATE TABLE "records__member" ({ddl})')
    conn.execute(
        'INSERT INTO "records__member" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "m1", 0, True, 0, 0, "gold"],
    )
    history_ddl = ", ".join(
        f'"{col["name"]}" {col["type"]}' for col in _HISTORY_COLUMNS
    )
    conn.execute(f'CREATE TABLE "history" ({history_ddl})')
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": "records__member",
                "category": "records",
                "record_kind": "member",
                "rows": 1,
                "columns": _MEMBER_COLUMNS,
            },
            {
                "name": "history",
                "category": "fixed",
                "rows": 0,
                "columns": _HISTORY_COLUMNS,
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _print_author_descriptions(mode: str, specs: "Sequence[QuerySpec]") -> None:
    """Print one mode's compiled `author_descriptions`, one line per spec."""
    for spec in specs:
        label = f"{mode}: {spec.table_name}.author_descriptions"
        print(f"{label} = {spec.author_descriptions!r}")


def _demo_dimensional_stamping(tmp_dir: Path, emit_dir: Path) -> None:
    """Compile the dimensional plan and print its `author_descriptions`."""
    config = load_export_config(
        _write_config(tmp_dir, "dimensional.yaml", _DIMENSIONAL_CONFIG)
    )
    assert config.dimensional is not None
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit, config.dimensional, None, None, lambda _n: None, base_relations=None
        )
    _print_author_descriptions("dimensional", specs)


def _demo_source_stamping(tmp_dir: Path, emit_dir: Path) -> None:
    """Compile the source plan and print its `author_descriptions`."""
    config = load_export_config(_write_config(tmp_dir, "source.yaml", _SOURCE_CONFIG))
    with open_emit(emit_dir) as emit:
        election = resolve_election(emit.sidecar, config.keys)
        plan = build_source_plan(
            emit, config, _ANCHOR, election, False, lambda _n: None
        )
        specs = build_source_query_specs(plan, None)
    _print_author_descriptions("source", specs)


def _demo_base_stamping(tmp_dir: Path, emit_dir: Path) -> None:
    """Compile the base plan and print its `author_descriptions`."""
    config = load_export_config(_write_config(tmp_dir, "base.yaml", _BASE_CONFIG))
    with open_emit(emit_dir) as emit:
        specs = build_base_query_specs(emit, config, _ANCHOR, None, lambda _n: None)
    _print_author_descriptions("base", specs)


def _demo_gate_failures(tmp_dir: Path, emit_dir: Path) -> None:
    """Show a bad `descriptions` key refused at plan compile, before any write."""
    bad_source = load_export_config(
        _write_config(tmp_dir, "source_bad.yaml", _SOURCE_BAD_CONFIG)
    )
    with open_emit(emit_dir) as emit:
        election = resolve_election(emit.sidecar, bad_source.keys)
        try:
            build_source_plan(
                emit, bad_source, _ANCHOR, election, False, lambda _n: None
            )
            raise AssertionError("bad source descriptions key should have been refused")
        except SourceColumnUnresolved as exc:
            print(f"source: bad descriptions key refused: {exc}")

    bad_base = load_export_config(
        _write_config(tmp_dir, "base_bad.yaml", _BASE_BAD_CONFIG)
    )
    with open_emit(emit_dir) as emit:
        try:
            build_base_query_specs(emit, bad_base, _ANCHOR, None, lambda _n: None)
            raise AssertionError("bad base descriptions key should have been refused")
        except BaseRenameUnresolved as exc:
            print(f"base: bad descriptions key refused: {exc}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        emit_dir = tmp_dir / "emit"
        emit_dir.mkdir()
        _build_emit(emit_dir)

        _demo_dimensional_stamping(tmp_dir, emit_dir)
        _demo_source_stamping(tmp_dir, emit_dir)
        _demo_base_stamping(tmp_dir, emit_dir)
        _demo_gate_failures(tmp_dir, emit_dir)

    print(
        "SUCCESS: all three plan compilers stamp author_descriptions keyed by"
        " output name; bad descriptions keys are refused at plan compile"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
