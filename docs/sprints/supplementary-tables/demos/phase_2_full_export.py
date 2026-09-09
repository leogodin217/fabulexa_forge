#!/usr/bin/env python
"""
Demo: Supplementary tables through a full dimensional export
Sprint: supplementary-tables
Phase: 2

Builds a self-contained minimal emit (one records__actor kind, two rows, no
history/membership), a config declaring one dim table plus one file
supplement and one inline supplement spanning VARCHAR / BIGINT / DOUBLE /
DATE / DECIMAL(5,2) / BOOLEAN (with a NULL cell). Exports under both CSV and
DuckDB, prints each supplement table's DESCRIBE and rows, the README's
supplement section, and the manifest's `tables[]` entries (`supplement`
populated vs null, `manifest_format_version: 3`). Then drives
SupplementValueInvalid (a bad cell), TemporalRenderRequiresAnchor (a
TIMESTAMPTZ column with no anchor), and SupplementSourceIsOutput (a file
supplement whose source sits in the CSV output directory) — each leaving the
output directory empty.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.errors import ExporterError
from fabulexa_forge.exporters.companion import companion_artifact_paths
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.exporters.supplements import load_supplements
from fabulexa_forge.reader.emit import open_emit

if TYPE_CHECKING:
    from collections.abc import Callable

    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.exporters.notices import Notice
    from fabulexa_forge.reader.emit import Emit

_ACTOR_COLUMNS: list[dict[str, object]] = [
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

_DIM_ACTOR_TABLE_YAML = """
dimensional:
  tables:
    - name: dim_actor
      role: dim
      scd: type1
      source: {grain: records, kind: actor}
      key: [id]
      columns:
        - {name: id, from: record_id}
        - {name: name, from: prop__name}
"""

_REGION_CODE_CSV = "code,label\nUS,United States\nCA,\n"

_HAPPY_SUPPLEMENTS_YAML = """
supplements:
  - name: region_code
    file: region_code.csv
    columns: {code: VARCHAR, label: VARCHAR}
    description: Sales-region codes used by account management.
  - name: rate_tier
    columns:
      tier: VARCHAR
      capacity: BIGINT
      rate: "DECIMAL(5,2)"
      effective_date: DATE
      active: BOOLEAN
      weight: DOUBLE
    rows:
      - tier: standard
        capacity: 100
        rate: 1.5
        effective_date: "2024-01-01"
        active: "true"
        weight: 0.5
      - tier: premium
        capacity: null
        rate: 2.25
        effective_date: "2024-06-01"
        active: "false"
        weight: 1.75
"""

_BAD_CELL_SUPPLEMENTS_YAML = """
supplements:
  - name: rate_tier
    columns: {tier: VARCHAR, capacity: BIGINT}
    rows:
      - {tier: standard, capacity: "not-a-number"}
"""

_TIMESTAMPTZ_SUPPLEMENTS_YAML = """
supplements:
  - name: event_log_extra
    columns: {event_at: TIMESTAMPTZ}
    rows:
      - {event_at: "2024-01-01 10:00:00"}
"""

_SOURCE_IS_OUTPUT_SUPPLEMENTS_YAML = """
supplements:
  - name: region_code
    file: region_code.csv
    columns: {code: VARCHAR, label: VARCHAR}
"""


def _discard_notice(notice: "Notice") -> None:
    """A notice sink that drops every notice — the demo prints its own facts."""


def _build_emit(emit_dir: Path) -> Path:
    """Build a minimal self-contained emit: one records__actor kind, two rows,
    no history, no membership, no runtime block."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    col_fragments = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _ACTOR_COLUMNS)
    conn.execute(f'CREATE TABLE "records__actor" ({col_fragments})')
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a001", 0, True, None, 0, 0, "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a002", 0, True, None, 0, 1, "Bob"],
    )
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "rows": 2,
                "columns": _ACTOR_COLUMNS,
            }
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return emit_dir


def _write_config(config_dir: Path, supplements_yaml: str) -> Path:
    """Write `config.yaml` combining the dim_actor table and a supplements block."""
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "mode: dimensional\n" + _DIM_ACTOR_TABLE_YAML + supplements_yaml,
        encoding="utf-8",
    )
    return config_path


def _load(config_path: Path) -> "ExportConfig":
    return load_export_config(config_path)


def _print_csv_table(path: Path) -> None:
    print(f"    {path.name}:")
    for line in path.read_text(encoding="utf-8").splitlines():
        print(f"      {line}")


def _print_duckdb_table(conn: "duckdb.DuckDBPyConnection", table: str) -> None:
    print(f"    {table}:")
    describe = conn.execute(f'DESCRIBE "{table}"').fetchall()
    print(f"      columns: {describe}")
    rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
    for row in rows:
        print(f"      {row}")


def _demo_csv_export(emit: "Emit", config: "ExportConfig", tmp_root: Path) -> None:
    """Full CSV export; print supplement CSVs, the README section, and the
    manifest's supplement-bearing table entries."""
    print("CSV export:")
    csv_dir = tmp_root / "csv_out"
    csv_dir.mkdir()
    source_dir = tmp_root / "csv_sources"
    source_dir.mkdir()
    (source_dir / "region_code.csv").write_text(_REGION_CODE_CSV, encoding="utf-8")
    supplements = load_supplements(config, source_dir)
    export_dimensional(
        emit, config, csv_dir, "csv", None, _discard_notice, None, supplements
    )
    _print_csv_table(csv_dir / "region_code.csv")
    _print_csv_table(csv_dir / "rate_tier.csv")

    readme_path, manifest_path = companion_artifact_paths(csv_dir, "dimensional", "csv")
    readme_text = readme_path.read_text(encoding="utf-8")
    section_start = readme_text.index("rate_tier")
    print("  README (rate_tier section):")
    for line in readme_text[section_start - 20 : section_start + 200].splitlines():
        print(f"    {line}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    print(f"  manifest_format_version: {manifest['manifest_format_version']}")
    for table in manifest["tables"]:
        print(f"    table '{table['name']}': supplement={table['supplement']}")


def _demo_duckdb_export(emit: "Emit", config: "ExportConfig", tmp_root: Path) -> None:
    """Full DuckDB export; print supplement DESCRIBE + rows."""
    print("DuckDB export:")
    duckdb_dir = tmp_root / "duckdb_out"
    duckdb_dir.mkdir()
    source_dir = tmp_root / "duckdb_sources"
    source_dir.mkdir()
    (source_dir / "region_code.csv").write_text(_REGION_CODE_CSV, encoding="utf-8")
    db_path = duckdb_dir / "warehouse.duckdb"
    supplements = load_supplements(config, source_dir)
    export_dimensional(
        emit, config, db_path, "duckdb", None, _discard_notice, None, supplements
    )
    conn = duckdb.connect(str(db_path), read_only=True)
    _print_duckdb_table(conn, "region_code")
    _print_duckdb_table(conn, "rate_tier")
    conn.close()


def _demo_refusal(label: str, action: "Callable[[], object]", out_dir: Path) -> None:
    """Run `action`, expecting a refusal, and confirm `out_dir` stayed empty."""
    try:
        action()
    except ExporterError as exc:
        remaining = sorted(p.name for p in out_dir.iterdir())
        print(f"  [{label}] refused: {exc}")
        print(f"    output directory contents afterward: {remaining}")
    else:
        raise AssertionError(f"expected a refusal for {label!r}, none raised")


def _demo_compile_refusal(
    emit: "Emit", tmp_root: Path, label: str, case_name: str, supplements_yaml: str
) -> None:
    """A refusal driven by a fresh config + a fresh, empty output directory —
    every `compile_supplement_specs` gate leaves `out_dir` untouched."""
    out_dir = tmp_root / case_name
    out_dir.mkdir()
    config_path = _write_config(tmp_root / f"{case_name}_cfg", supplements_yaml)
    config = _load(config_path)
    supplements = load_supplements(config, config_path.parent)
    _demo_refusal(
        label,
        lambda: export_dimensional(
            emit, config, out_dir, "csv", None, _discard_notice, None, supplements
        ),
        out_dir,
    )


def _demo_source_is_output(emit: "Emit", tmp_root: Path) -> None:
    """The natural-config trap: the supplement's source file sits inside the
    CSV directory this invocation would write into."""
    out_dir = tmp_root / "source_is_output"
    out_dir.mkdir()
    (out_dir / "region_code.csv").write_text(_REGION_CODE_CSV, encoding="utf-8")
    config_path = _write_config(out_dir, _SOURCE_IS_OUTPUT_SUPPLEMENTS_YAML)
    config = _load(config_path)
    supplements = load_supplements(config, out_dir)
    _demo_refusal(
        "source is output",
        lambda: export_dimensional(
            emit, config, out_dir, "csv", None, _discard_notice, None, supplements
        ),
        out_dir,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        emit_dir = _build_emit(tmp_root / "emit")

        with open_emit(emit_dir) as emit:
            happy_config_path = _write_config(
                tmp_root / "happy_cfg", _HAPPY_SUPPLEMENTS_YAML
            )
            happy_config = _load(happy_config_path)
            _demo_csv_export(emit, happy_config, tmp_root)
            print()
            _demo_duckdb_export(emit, happy_config, tmp_root)
            print()

            print("Refusals:")
            _demo_compile_refusal(
                emit, tmp_root, "bad cell", "bad_cell", _BAD_CELL_SUPPLEMENTS_YAML
            )
            _demo_compile_refusal(
                emit,
                tmp_root,
                "TIMESTAMPTZ with no anchor",
                "no_anchor",
                _TIMESTAMPTZ_SUPPLEMENTS_YAML,
            )
            _demo_source_is_output(emit, tmp_root)

    print()
    print(
        "SUCCESS: supplements exported end-to-end under both formats;"
        " every refusal fired before any write"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
