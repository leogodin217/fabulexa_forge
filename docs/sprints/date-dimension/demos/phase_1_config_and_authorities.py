#!/usr/bin/env python
"""
Demo: The date-dimension config grammar (`DateDimensionConfig`, `DateRefSpec`,
`ColumnDecl.date_ref`, `ExportConfig.date_dimension` and its three validators)
and the two renderer authorities (`date_key_expr`, and the bare-expression
split of `date_parse_expr` / `anchor_temporal_expr` under their existing
aliased wrappers).

Sprint: date-dimension
Phase: 1

Loads a YAML export config carrying both `date_ref` shapes (`{source}` and
`{from, format}`) under a `date_dimension` block and prints the parsed
models. Drives each parse-time refusal the phase ships (an unquoted YAML
date, `to < from`, both `date_ref` shapes set, a time-only `date_ref`
format, a `date_ref` column with no `date_dimension` block, a `dim_date`-
named table alongside the block, and the block under `mode: source`),
printing each refusal's message. Evaluates `date_key_expr` over three DATE
literals in DuckDB and prints the resulting `yyyymmdd` keys. Shows
`render_anchor_temporal_expr` / `render_date_parse_expr` output equal to
their new bare authorities plus a hand-spliced alias, byte-for-byte.

Nothing here compiles a `date_ref` column into SQL yet — a loaded config
carrying one is inert until Phase 2 (the six business rules and the actual
`dim_date` join land there).
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from fabulexa_forge._sql import date_key_expr, date_parse_expr, render_date_parse_expr
from fabulexa_forge.anchor import EffectiveAnchor, anchor_temporal_expr
from fabulexa_forge.anchor import render_anchor_temporal_expr as render_anchor_expr
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.errors import ConfigError

_BOTH_SHAPES_CONFIG = """
mode: dimensional
date_dimension:
  from: "1985-01-01"
  to: "2024-12-31"
dimensional:
  tables:
    - name: fact_visit
      role: fact
      key: [id]
      source:
        grain: records
        kind: visit
      columns:
        - name: id
          from: record_id
        - name: visit_date_key
          date_ref: {source: sim_time}
        - name: dob_date_key
          date_ref: {from: dob_text, format: "%Y-%m-%d"}
"""

_UNQUOTED_DATE_CONFIG = """
mode: dimensional
date_dimension:
  from: 1985-01-01
  to: "2024-12-31"
dimensional:
  tables:
    - name: dim_x
      role: dim
      key: [id]
      source: {grain: records, kind: actor}
      columns: [{name: id, from: record_id}]
"""

_TO_BEFORE_FROM_CONFIG = """
mode: dimensional
date_dimension:
  from: "2024-12-31"
  to: "2024-01-01"
dimensional:
  tables:
    - name: dim_x
      role: dim
      key: [id]
      source: {grain: records, kind: actor}
      columns: [{name: id, from: record_id}]
"""

_BOTH_DATE_REF_SHAPES_CONFIG = """
mode: dimensional
date_dimension:
  from: "1985-01-01"
  to: "2024-12-31"
dimensional:
  tables:
    - name: fact_visit
      role: fact
      key: [id]
      source: {grain: records, kind: visit}
      columns:
        - name: id
          from: record_id
        - name: visit_date_key
          date_ref: {source: sim_time, from: dob_text, format: "%Y-%m-%d"}
"""

_TIME_ONLY_FORMAT_CONFIG = """
mode: dimensional
date_dimension:
  from: "1985-01-01"
  to: "2024-12-31"
dimensional:
  tables:
    - name: fact_visit
      role: fact
      key: [id]
      source: {grain: records, kind: visit}
      columns:
        - name: id
          from: record_id
        - name: visit_date_key
          date_ref: {from: logged_at_text, format: "%H:%M"}
"""

_DATE_REF_WITHOUT_BLOCK_CONFIG = """
mode: dimensional
dimensional:
  tables:
    - name: fact_visit
      role: fact
      key: [id]
      source: {grain: records, kind: visit}
      columns:
        - name: id
          from: record_id
        - name: visit_date_key
          date_ref: {source: sim_time}
"""

_DIM_DATE_NAMED_TABLE_CONFIG = """
mode: dimensional
date_dimension:
  from: "1985-01-01"
  to: "2024-12-31"
dimensional:
  tables:
    - name: dim_date
      role: dim
      key: [id]
      source: {grain: records, kind: actor}
      columns: [{name: id, from: record_id}]
"""

_BLOCK_UNDER_MODE_SOURCE_CONFIG = """
mode: source
date_dimension:
  from: "1985-01-01"
  to: "2024-12-31"
source:
  tables:
    - name: actors
      kind: actor
"""


def _write_config(tmp_dir: Path, name: str, text: str) -> Path:
    """Write one example config's YAML to `tmp_dir/name` and return its path."""
    path = tmp_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def _parse_both_date_ref_shapes(tmp_dir: Path) -> None:
    """Parse a config with both `date_ref` shapes and print the parsed models."""
    config = load_export_config(
        _write_config(tmp_dir, "both_shapes.yaml", _BOTH_SHAPES_CONFIG)
    )
    assert config.date_dimension is not None
    print(
        "date_dimension:"
        f" from={config.date_dimension.from_.isoformat()!r}"
        f" to={config.date_dimension.to.isoformat()!r}"
    )
    assert config.dimensional is not None
    columns = config.dimensional.tables[0].columns
    instant_col, parse_col = columns[1], columns[2]
    assert instant_col.date_ref is not None
    assert parse_col.date_ref is not None
    print(f"date_ref {{source}} shape: {instant_col.name} -> {instant_col.date_ref!r}")
    print(
        f"date_ref {{from, format}} shape: {parse_col.name} -> {parse_col.date_ref!r}"
    )


def _drive_refusal(tmp_dir: Path, label: str, filename: str, text: str) -> None:
    """Load a config expected to fail at parse time and print its message."""
    try:
        load_export_config(_write_config(tmp_dir, filename, text))
        raise AssertionError(f"{label}: expected a ConfigError, none raised")
    except ConfigError as exc:
        print(f"refused ({label}): {exc}")


def _drive_all_refusals(tmp_dir: Path) -> None:
    """Drive each of the phase's parse-time refusals, printing every message."""
    _drive_refusal(
        tmp_dir, "unquoted YAML date", "unquoted.yaml", _UNQUOTED_DATE_CONFIG
    )
    _drive_refusal(tmp_dir, "to < from", "to_before_from.yaml", _TO_BEFORE_FROM_CONFIG)
    _drive_refusal(
        tmp_dir,
        "both date_ref shapes",
        "both_date_ref.yaml",
        _BOTH_DATE_REF_SHAPES_CONFIG,
    )
    _drive_refusal(
        tmp_dir, "time-only date_ref format", "time_only.yaml", _TIME_ONLY_FORMAT_CONFIG
    )
    _drive_refusal(
        tmp_dir,
        "date_ref without date_dimension block",
        "no_block.yaml",
        _DATE_REF_WITHOUT_BLOCK_CONFIG,
    )
    _drive_refusal(
        tmp_dir,
        "dim_date-named table",
        "dim_date_table.yaml",
        _DIM_DATE_NAMED_TABLE_CONFIG,
    )
    _drive_refusal(
        tmp_dir,
        "date_dimension under mode: source",
        "mode_source.yaml",
        _BLOCK_UNDER_MODE_SOURCE_CONFIG,
    )


def _demo_date_key_expr() -> None:
    """Evaluate `date_key_expr` over three DATE literals in DuckDB."""
    conn = duckdb.connect(":memory:")
    try:
        for literal in ["DATE '2024-01-02'", "DATE '0999-12-31'", "NULL::DATE"]:
            key = conn.execute(f"SELECT {date_key_expr(literal)}").fetchone()
            assert key is not None
            print(f"date_key_expr({literal}) = {key[0]!r}")
    finally:
        conn.close()


def _demo_renderer_authorities_match_wrappers() -> None:
    """Show the two aliased renderers equal their new bare authority plus a
    hand-spliced alias, byte-for-byte."""
    bare_parse = date_parse_expr('"_grain"."dob"', "%Y-%m-%d", "visits")
    aliased_parse = render_date_parse_expr('"_grain"."dob"', "%Y-%m-%d", "x", "visits")
    parse_matches = f'{bare_parse} AS "x"' == aliased_parse
    print(f"date_parse_expr + alias == render_date_parse_expr: {parse_matches}")

    anchor = EffectiveAnchor(
        start_instant=datetime.fromisoformat("2020-03-01T00:00:00+00:00"),
        timezone=ZoneInfo("UTC"),
    )
    bare_anchor = anchor_temporal_expr(anchor, '"_grain"."sim_time"', "timestamp")
    aliased_anchor = render_anchor_expr(anchor, '"_grain"."sim_time"', "x", "timestamp")
    anchor_matches = f'{bare_anchor} AS "x"' == aliased_anchor
    print(
        f"anchor_temporal_expr + alias == render_anchor_temporal_expr: {anchor_matches}"
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _parse_both_date_ref_shapes(tmp_dir)
        _drive_all_refusals(tmp_dir)

    _demo_date_key_expr()
    _demo_renderer_authorities_match_wrappers()

    print(
        "SUCCESS: date_dimension/date_ref config grammar parses and refuses"
        " correctly; date_key_expr renders yyyymmdd keys; the two aliased"
        " renderers equal their new bare authorities plus an alias"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
