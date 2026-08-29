#!/usr/bin/env python
"""
Demo: The three per-column description override config surfaces
(`ColumnDecl.description`, `SourceTableDecl.descriptions`,
`RenameEntry.descriptions`) parse and validate at load time, and the
compiled-plan carriage (`QuerySpec.author_descriptions` ->
`TableReport.author_descriptions`) forwards verbatim through
`write_query_specs`.

Sprint: desc-override
Phase: 1

Parses one example export config per mode (dimensional, source, base), each
exercising its description surface, then shows a whitespace-only
`description` refused at load. Separately, synthesizes a minimal single-table
emit, builds a `QuerySpec` carrying a literal `author_descriptions` map, runs
`write_query_specs`, and prints the resulting `TableReport.author_descriptions`
to show it rode across the write dispatch unchanged. Stamping the map from a
mode's own config surface is Phase 2's job -- this phase only carries it.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.errors import ConfigError
from fabulexa_forge.exporters.query_spec import QuerySpec, write_query_specs
from fabulexa_forge.reader.emit import open_emit

_DIMENSIONAL_CONFIG = """
mode: dimensional
dimensional:
  tables:
    - name: dim_entity
      role: dim
      key: [id]
      source:
        grain: records
        kind: entity
      columns:
        - name: id
          from: record_id
          description: "Unique identifier for each entity record."
        - name: entity_type
          from: prop__entity_type
"""

_SOURCE_CONFIG = """
mode: source
source:
  tables:
    - name: entity_state
      kind: entity
      descriptions:
        prop__entity_type: "The entity's classification (consultant, nurse, admin)."
"""

_BASE_CONFIG = """
mode: base
base:
  rename:
    - table: records__entity
      descriptions:
        prop__entity_type: "The entity's classification."
"""

_WHITESPACE_DESCRIPTION_CONFIG = """
mode: dimensional
dimensional:
  tables:
    - name: dim_entity
      role: dim
      key: [id]
      source:
        grain: records
        kind: entity
      columns:
        - name: id
          from: record_id
          description: "   "
"""

_ENTITY_COLUMNS: list[tuple[str, str]] = [
    ("fork_path", "VARCHAR"),
    ("record_id", "VARCHAR"),
    ("created_sim_time", "BIGINT"),
    ("active", "BOOLEAN"),
    ("deactivated_at", "BIGINT"),
    ("last_mutation_sim_time", "BIGINT"),
    ("record_index", "BIGINT"),
    ("prop__entity_type", "VARCHAR"),
]

_HISTORY_COLUMNS: list[tuple[str, str]] = [
    ("fork_path", "VARCHAR"),
    ("kind", "VARCHAR"),
    ("record_id", "VARCHAR"),
    ("property", "VARCHAR"),
    ("sim_time", "BIGINT"),
    ("value", "VARCHAR"),
]


def _write_config(tmp_dir: Path, name: str, text: str) -> Path:
    """Write one example config's YAML to `tmp_dir/name` and return its path."""
    path = tmp_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def _parse_config_surfaces(tmp_dir: Path) -> None:
    """Parse the three per-mode description surfaces, then show a
    whitespace-only description refused at load."""
    dimensional = load_export_config(
        _write_config(tmp_dir, "dimensional.yaml", _DIMENSIONAL_CONFIG)
    )
    assert dimensional.dimensional is not None
    column = dimensional.dimensional.tables[0].columns[0]
    print(f"dimensional: ColumnDecl.description = {column.description!r}")

    source = load_export_config(_write_config(tmp_dir, "source.yaml", _SOURCE_CONFIG))
    assert source.source is not None
    print(
        "source: SourceTableDecl.descriptions ="
        f" {source.source.tables[0].descriptions!r}"
    )

    base = load_export_config(_write_config(tmp_dir, "base.yaml", _BASE_CONFIG))
    assert base.base is not None
    assert base.base.rename is not None
    print(f"base: RenameEntry.descriptions = {base.base.rename[0].descriptions!r}")

    try:
        load_export_config(
            _write_config(tmp_dir, "bad.yaml", _WHITESPACE_DESCRIPTION_CONFIG)
        )
        raise AssertionError("whitespace-only description should have been refused")
    except ConfigError as exc:
        print(f"whitespace-only description refused at load: {exc}")


def _build_emit(emit_dir: Path) -> None:
    """Write a minimal single-kind emit: records__entity plus an empty history."""
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    ddl = ", ".join(f'"{name}" {type_}' for name, type_ in _ENTITY_COLUMNS)
    conn.execute(f'CREATE TABLE "records__entity" ({ddl})')
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "e1", 0, True, 0, 0, "consultant"],
    )
    history_ddl = ", ".join(f'"{name}" {type_}' for name, type_ in _HISTORY_COLUMNS)
    conn.execute(f'CREATE TABLE "history" ({history_ddl})')
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": "records__entity",
                "category": "records",
                "record_kind": "entity",
                "rows": 1,
                "columns": [
                    {"name": name, "type": type_} for name, type_ in _ENTITY_COLUMNS
                ],
            },
            {
                "name": "history",
                "category": "fixed",
                "rows": 0,
                "columns": [
                    {"name": name, "type": type_} for name, type_ in _HISTORY_COLUMNS
                ],
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _demo_carriage(tmp_dir: Path) -> None:
    """Build a QuerySpec carrying `author_descriptions`, write it, and print
    the forwarded `TableReport.author_descriptions`."""
    emit_dir = tmp_dir / "emit"
    emit_dir.mkdir()
    _build_emit(emit_dir)

    author_descriptions = {
        "id": "Unique identifier for each entity record.",
        "entity_type": "The entity's classification.",
    }
    spec = QuerySpec(
        table_name="dim_entity",
        sql="SELECT record_id AS id, prop__entity_type AS entity_type"
        ' FROM "records__entity" ORDER BY record_id',
        write_mode="create",
        view_name=None,
        view_sql=None,
        author_descriptions=author_descriptions,
    )

    with open_emit(emit_dir) as emit:
        report = write_query_specs(emit, [spec], emit_dir / "out.duckdb", "duckdb")

    table = report.tables[0]
    print(f"TableReport.author_descriptions = {table.author_descriptions!r}")
    assert table.author_descriptions == author_descriptions, (
        "author_descriptions must forward verbatim from the compiled QuerySpec"
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _parse_config_surfaces(tmp_dir)
        _demo_carriage(tmp_dir)

    print(
        "SUCCESS: all three description config surfaces parse and validate;"
        " author_descriptions carries verbatim through write_query_specs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
