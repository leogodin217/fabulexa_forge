#!/usr/bin/env python
"""
Demo: Full-export threading -- companion artifacts through the library entry point
Sprint: companion-artifacts
Phase: 3

Builds a minimal `mode: base` emit, then runs `export_base` (the library entry
point `cli.py` itself calls) for both csv and duckdb targets with an author
overlay: lists the output directory, prints the rendered README and manifest,
re-runs to show byte-identity, then shows the unknown-table overlay refusal
leaving the target empty.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "tests"))

from _support.sidecar_builder import (  # noqa: E402
    identity_column,
    prop_column,
    write_emit,
)

from fabulexa_forge.config.models import ExportConfig  # noqa: E402
from fabulexa_forge.errors import ReadmeOverlayUnknownTable  # noqa: E402
from fabulexa_forge.exporters.base.engine import export_base  # noqa: E402
from fabulexa_forge.exporters.companion.overlay import (  # noqa: E402
    ReadmeOverlay,
    load_readme_overlay,
)
from fabulexa_forge.exporters.notices import render_notice_stderr  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402

_PATIENT_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_OVERLAY_TEXT = """\
## overview
Nightly flat dump of the clinic's patient records, for the data-engineering
course.

## table: patient
One row per patient, current state at export time.
"""

_UNKNOWN_TABLE_OVERLAY_TEXT = """\
## table: does_not_exist
This table is never produced by the plan.
"""


def build_demo_emit(emit_dir: Path) -> None:
    """Write a minimal one-kind `records__patient` emit to `emit_dir`."""
    import duckdb

    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    col_fragments = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _PATIENT_COLUMNS)
    conn.execute(f'CREATE TABLE "records__patient" ({col_fragments})')
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "p001", 1001, 0, True, 0, 0, "admitted"],
    )
    conn.close()

    write_emit(
        emit_dir,
        tables=[
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": _PATIENT_COLUMNS,
                "rows": 1,
            }
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra={"record_roles": {"patient": "dimension"}},
    )


def run_base_export(
    emit_dir: Path, out: Path, fmt: str, overlay: ReadmeOverlay | None
) -> None:
    """Run `export_base` against `emit_dir`, writing to `out`."""
    with open_emit(emit_dir) as emit:
        config = ExportConfig(mode="base")
        report = export_base(
            emit, config, out, fmt, None, render_notice_stderr, overlay
        )
        for table in report.tables:
            print(f"  {table.name}: {table.row_count} rows")


def demo_csv_target(emit_dir: Path, out_dir: Path, overlay: ReadmeOverlay) -> None:
    """Full csv export with an overlay; list, print, and re-run for byte-identity."""
    print("--- csv target ---")
    run_base_export(emit_dir, out_dir, "csv", overlay)
    print("output directory:", sorted(p.name for p in out_dir.iterdir()))

    readme_path = out_dir / "base-readme.md"
    manifest_path = out_dir / "base-manifest.json"
    print(readme_path.read_text(encoding="utf-8"))
    print(manifest_path.read_text(encoding="utf-8"))

    first_manifest_bytes = manifest_path.read_bytes()
    first_readme_bytes = readme_path.read_bytes()
    run_base_export(emit_dir, out_dir, "csv", overlay)
    assert manifest_path.read_bytes() == first_manifest_bytes, (
        "re-running an identical export must be byte-identical (manifest)"
    )
    assert readme_path.read_bytes() == first_readme_bytes, (
        "re-running an identical export must be byte-identical (readme)"
    )
    print("re-render is byte-identical: confirmed")


def demo_duckdb_target(emit_dir: Path, out_dir: Path, overlay: ReadmeOverlay) -> None:
    """Full duckdb export with an overlay; show the sibling artifact names."""
    print("--- duckdb target ---")
    db_path = out_dir / "warehouse.duckdb"
    run_base_export(emit_dir, db_path, "duckdb", overlay)
    print("output directory:", sorted(p.name for p in out_dir.iterdir()))


def demo_unknown_table_refusal(emit_dir: Path, out_dir: Path) -> None:
    """An overlay naming a table the plan never produces refuses before any write."""
    print("--- unknown-table overlay refusal ---")
    overlay_path = out_dir / "bad_overlay.md"
    overlay_path.write_text(_UNKNOWN_TABLE_OVERLAY_TEXT, encoding="utf-8")
    overlay = load_readme_overlay(overlay_path)

    target = out_dir / "refused"
    target.mkdir()
    try:
        run_base_export(emit_dir, target, "csv", overlay)
        raise AssertionError("expected ReadmeOverlayUnknownTable")
    except ReadmeOverlayUnknownTable as exc:
        print(f"refused: {exc}")
    print("target directory left empty:", list(target.iterdir()) == [])


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        emit_dir = tmp_dir / "emit"
        emit_dir.mkdir()
        build_demo_emit(emit_dir)

        overlay_path = tmp_dir / "overlay.md"
        overlay_path.write_text(_OVERLAY_TEXT, encoding="utf-8")
        overlay = load_readme_overlay(overlay_path)

        csv_out = tmp_dir / "csv-out"
        csv_out.mkdir()
        demo_csv_target(emit_dir, csv_out, overlay)

        duckdb_out = tmp_dir / "duckdb-out"
        duckdb_out.mkdir()
        demo_duckdb_target(emit_dir, duckdb_out, overlay)

        demo_unknown_table_refusal(emit_dir, tmp_dir)

    print("SUCCESS: full base export writes companion artifacts deterministically")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
