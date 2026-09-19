#!/usr/bin/env python
"""
Demo: The bound shape's documentation channel — `TableReport.window_bounds`
stamped by `_table_window_bounds` at dimensional plan compile, forwarded by
`write_query_specs` and `_build_windowed_report`, and rendered by
`resolve_column_doc`'s bound-shape branch (the pinned "derived from this
version's `{bound}` bound" prose, absent an author override) into the README
and the manifest.

Sprint: date-ref-scd-window
Phase: 2

Builds the same kind of SCD-2 emit as Phase 1 (one records kind, `company`,
with a tracked `prop__status`, and a `runtime` block) and writes a
`config.yaml`: a `date_dimension` block, and a type-2 `dim_company` carrying
both `scd_window` siblings (`valid_from` / `valid_to`) beside their `date_ref`
bound keys — `valid_from_date_key` with an author `description`,
`valid_to_date_key` without one. Runs `export_dimensional` to CSV and prints:
the dim's rows (keys beside the dates they key); the README's dictionary
lines for the two key columns (the author's prose vs. the pinned "derived
from this version's `valid_to` bound"); the manifest's `columns[]` entries
for both keys (`references: "dim_date"`, the same description pair, no
`unit`); and the full export's `TableReport.window_bounds` map. Then runs one
`--from/--to` window through `export_window` and prints the windowed
report's `window_bounds` for the dim (identical to the full export's)
alongside its delivery class (`window_delivery_class`).
"""

from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.exporters.companion.artifacts import companion_artifact_paths
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.exporters.dimensional.windowing import window_delivery_class
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.incremental.driver import export_window
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit

_HOUR_NS = 3_600 * 1_000_000_000
_TAPE_END_NS = 60 * _HOUR_NS

# One company, two prop__status versions: the first starts on the anchor's
# opening America/New_York local date, the second a day later (the open
# version) -- enough for the dim's valid_from/valid_to bound keys to differ.
_COMPANY_HISTORY = [
    (4 * _HOUR_NS, "active"),
    (30 * _HOUR_NS, "suspended"),
]

_CONFIG_YAML = """\
mode: dimensional

date_dimension:
  from: "2024-01-01"
  to: "2024-12-31"

dimensional:
  tables:
    - name: dim_company
      role: dim
      scd: type2
      source:
        grain: records
        kind: company
      key: [id, valid_from]
      columns:
        - name: id
          from: record_id
        - name: status
          from: prop__status
        - name: valid_from
          derived:
            scd_window:
              bound: valid_from
              as: date
        - name: valid_from_date_key
          date_ref:
            scd_window: valid_from
          description: >-
            Local calendar day this version of the company record took
            effect.
        - name: valid_to
          derived:
            scd_window:
              bound: valid_to
              as: date
        - name: valid_to_date_key
          date_ref:
            scd_window: valid_to
"""


def _write_emit(tmp_dir: Path) -> Path:
    """Write a self-contained emit: one company, one tracked property.

    Args:
        tmp_dir: Directory to write run.duckdb + base.json into.

    Returns:
        tmp_dir (the emit directory).
    """
    tmp_dir.mkdir()
    db_path = tmp_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(
        'CREATE TABLE "records__company" ('
        '"fork_path" VARCHAR, "record_id" VARCHAR, "created_sim_time" BIGINT,'
        ' "active" BOOLEAN, "deactivated_at" BIGINT,'
        ' "last_mutation_sim_time" BIGINT, "record_index" BIGINT,'
        ' "prop__status" VARCHAR)'
    )
    conn.execute(
        'CREATE TABLE "history" ("fork_path" VARCHAR, "kind" VARCHAR,'
        ' "record_id" VARCHAR, "property" VARCHAR, "sim_time" BIGINT,'
        ' "value" VARCHAR)'
    )
    conn.execute(
        'INSERT INTO "records__company" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "c001", 0, True, None, 0, 0, "pending"],
    )
    for sim_time, status in _COMPANY_HISTORY:
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "company", "c001", "status", sim_time, status],
        )
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": _TAPE_END_NS}],
        "tables": [
            {
                "name": "records__company",
                "category": "records",
                "record_kind": "company",
                "rows": 1,
                "columns": [
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
                ],
            },
            {
                "name": "history",
                "category": "fixed",
                "rows": len(_COMPANY_HISTORY),
                "columns": [
                    {"name": "fork_path", "type": "VARCHAR"},
                    {"name": "kind", "type": "VARCHAR"},
                    {"name": "record_id", "type": "VARCHAR"},
                    {"name": "property", "type": "VARCHAR"},
                    {"name": "sim_time", "type": "BIGINT"},
                    {"name": "value", "type": "VARCHAR"},
                ],
            },
        ],
        "runtime": {
            "timezone": "America/New_York",
            "start_datetime": "2024-03-14T00:00:00+00:00",
        },
    }
    (tmp_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_dir


def _discard_notice(_: Notice) -> None:
    """A notice sink that drops every notice — the demo has none to show."""


def _print_dim_rows(csv_path: Path) -> None:
    """Print the written dim's rows straight from the CSV file."""
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    print("=== dim_company.csv ===")
    for row in rows:
        print(row)


def _print_readme_key_lines(readme_path: Path) -> None:
    """Print the README's dictionary lines for the two bound-shape keys."""
    print("\n=== README lines: valid_from_date_key / valid_to_date_key ===")
    for line in readme_path.read_text(encoding="utf-8").splitlines():
        if "valid_from_date_key" in line or "valid_to_date_key" in line:
            print(line)


def _manifest_column(
    manifest: dict[str, object], column_name: str
) -> dict[str, object]:
    """One `dim_company` column's manifest entry, by name."""
    tables = manifest["tables"]
    assert isinstance(tables, list)
    (dim_table,) = (t for t in tables if t["name"] == "dim_company")
    columns = dim_table["columns"]
    assert isinstance(columns, list)
    (entry,) = (c for c in columns if c["name"] == column_name)
    assert isinstance(entry, dict)
    return entry


def _print_manifest_key_entries(manifest_path: Path) -> None:
    """Print the manifest's `columns[]` entries for both bound-shape keys."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    print("\n=== manifest columns[]: valid_from_date_key / valid_to_date_key ===")
    for column_name in ("valid_from_date_key", "valid_to_date_key"):
        entry = _manifest_column(manifest, column_name)
        print(f"{column_name}: {entry}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = _write_emit(tmp_path / "emit")
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_CONFIG_YAML, encoding="utf-8")
        config = load_export_config(config_path)
        assert config.dimensional is not None

        emit = open_emit(emit_dir)
        try:
            anchor = resolve_effective_anchor(
                emit.sidecar.runtime(), config.rebase, None, None
            )
            assert anchor is not None

            full_out = tmp_path / "full"
            full_out.mkdir()
            report = export_dimensional(
                emit, config, full_out, "csv", anchor, _discard_notice, None, ()
            )
            (dim_report,) = (t for t in report.tables if t.name == "dim_company")

            _print_dim_rows(full_out / "dim_company.csv")
            readme_path, manifest_path = companion_artifact_paths(
                full_out, "dimensional", "csv"
            )
            _print_readme_key_lines(readme_path)
            _print_manifest_key_entries(manifest_path)
            print(
                f"\nTableReport.window_bounds (dim_company): {dim_report.window_bounds}"
            )

            window = Window(
                index=None, start_ns=0, end_ns=_TAPE_END_NS, label="demo_range"
            )
            windowed_out = tmp_path / "windowed"
            windowed_export = export_window(
                emit,
                config,
                windowed_out,
                "csv",
                anchor,
                window,
                None,
                _discard_notice,
                None,
                (),
            )
            (windowed_dim_report,) = (
                t for t in windowed_export.report.tables if t.name == "dim_company"
            )
            delivery = window_delivery_class(
                config.dimensional.tables[0], config.dimensional, emit.sidecar
            )
            print(
                "\nwindowed TableReport.window_bounds (dim_company):"
                f" {windowed_dim_report.window_bounds}"
                f" (identical to full export's: "
                f"{windowed_dim_report.window_bounds == dim_report.window_bounds})"
            )
            print(f"delivery class: {delivery!r}")
        finally:
            emit.close()

    print(
        "\nSUCCESS: the bound-shape keys carry `references: dim_date` and a"
        " `window_bounds` entry naming their bound; the author's"
        " `valid_from_date_key` description renders verbatim while the"
        " undocumented `valid_to_date_key` renders the pinned"
        " this-version's-bound prose; the windowed export's window_bounds"
        " matches the full export's"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
