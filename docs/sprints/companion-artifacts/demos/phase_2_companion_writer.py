#!/usr/bin/env python
"""
Demo: Companion writer -- README + manifest for a hand-built ExportReport
Sprint: companion-artifacts
Phase: 2

Builds a minimal emit via tests/_support/sidecar_builder.write_emit,
hand-assembles an ExportReport + overlay, and calls write_companion_artifacts
against both a CSV (directory) target and a DuckDB (file) target. Prints the
rendered README and manifest, then re-renders to show byte-identity.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "tests"))

from _support.sidecar_builder import write_emit  # noqa: E402

from fabulexa_forge.anchor import EffectiveAnchor  # noqa: E402
from fabulexa_forge.config.models import ExportConfig  # noqa: E402
from fabulexa_forge.exporters.companion import (  # noqa: E402
    ExportReport,
    ReadmeOverlay,
    TableReport,
    is_companion_artifact_name,
    write_companion_artifacts,
)
from fabulexa_forge.exporters.query_spec import TableKeys  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402


def build_demo_emit(emit_dir: Path) -> None:
    """Write a minimal one-table emit to `emit_dir`.

    The companion writer reads only the sidecar's identity facts (version,
    branch, runtime) -- never table contents -- so one bare `fixed`-category
    table and an empty run.duckdb are all a demo emit needs.
    """
    tables = [
        {
            "name": "clinic_settings",
            "category": "fixed",
            "rows": 1,
            "columns": [
                {"name": "setting_key", "type": "VARCHAR"},
                {"name": "setting_value", "type": "VARCHAR"},
            ],
        }
    ]
    write_emit(emit_dir, tables=tables)

    import duckdb

    duckdb.connect(str(emit_dir / "run.duckdb")).close()


def build_demo_report() -> ExportReport:
    """Hand-assemble a two-table ExportReport, one with declared keys."""
    return ExportReport(
        tables=(
            TableReport(
                name="patients",
                columns=(("id", "BIGINT"), ("status", "VARCHAR")),
                row_count=1,
                keys=TableKeys(primary_key=("id",), unique=()),
            ),
            TableReport(
                name="visits",
                columns=(("visit_id", "BIGINT"), ("patient_id", "BIGINT")),
                row_count=0,
                keys=None,
            ),
        )
    )


def build_demo_overlay() -> ReadmeOverlay:
    """Hand-assemble an overlay noting one of the report's two tables."""
    return ReadmeOverlay(
        overview="Nightly extract of the clinic's operational database.",
        table_notes={"patients": "One row per registered patient."},
    )


def demo_write_csv_target(emit_dir: Path, out_dir: Path) -> None:
    """Write companion artifacts against a CSV (directory) target."""
    print("--- csv target ---")
    with open_emit(emit_dir) as emit:
        config = ExportConfig(mode="base")
        report = build_demo_report()
        overlay = build_demo_overlay()
        anchor = EffectiveAnchor(
            start_instant=datetime(2024, 1, 1, tzinfo=timezone.utc),
            timezone=ZoneInfo("UTC"),
        )
        write_companion_artifacts(
            emit=emit,
            config=config,
            fmt="csv",
            anchor=anchor,
            report=report,
            overlay=overlay,
            target=out_dir,
            windowed=None,
        )
        readme_path = out_dir / "base-readme.md"
        manifest_path = out_dir / "base-manifest.json"
        print(readme_path.read_text(encoding="utf-8"))
        print(manifest_path.read_text(encoding="utf-8"))

        first_manifest_bytes = manifest_path.read_bytes()
        write_companion_artifacts(
            emit=emit,
            config=config,
            fmt="csv",
            anchor=anchor,
            report=report,
            overlay=overlay,
            target=out_dir,
            windowed=None,
        )
        second_manifest_bytes = manifest_path.read_bytes()
        assert first_manifest_bytes == second_manifest_bytes, (
            "re-rendering the same inputs must be byte-identical"
        )
        print("re-render is byte-identical: confirmed")


def demo_write_duckdb_target(emit_dir: Path, out_dir: Path) -> None:
    """Write companion artifacts against a DuckDB (file) target."""
    print("--- duckdb target ---")
    with open_emit(emit_dir) as emit:
        config = ExportConfig(mode="base")
        report = build_demo_report()
        db_path = out_dir / "warehouse.duckdb"
        write_companion_artifacts(
            emit=emit,
            config=config,
            fmt="duckdb",
            anchor=None,
            report=report,
            overlay=None,
            target=db_path,
            windowed=None,
        )
        readme_path = out_dir / "warehouse-base-readme.md"
        manifest_path = out_dir / "warehouse-base-manifest.json"
        print(f"wrote {readme_path.name} and {manifest_path.name}")
        assert readme_path.exists()
        assert manifest_path.exists()


def demo_is_companion_artifact_name() -> None:
    """Show the census-exclusion predicate's true/false split."""
    print("--- is_companion_artifact_name ---")
    for mode in ("dimensional", "source", "base"):
        for suffix in ("readme.md", "manifest.json"):
            name = f"{mode}-{suffix}"
            print(f"{name}: {is_companion_artifact_name(name)}")
    for name in ("streaming-readme.md", "dimensional-readme.txt", "foo.csv", ".hidden"):
        print(f"{name}: {is_companion_artifact_name(name)}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        emit_dir = tmp_dir / "emit"
        emit_dir.mkdir()
        build_demo_emit(emit_dir)

        csv_out = tmp_dir / "csv-out"
        csv_out.mkdir()
        demo_write_csv_target(emit_dir, csv_out)

        duckdb_out = tmp_dir / "duckdb-out"
        duckdb_out.mkdir()
        demo_write_duckdb_target(emit_dir, duckdb_out)

        demo_is_companion_artifact_name()

    print("SUCCESS: companion writer renders README + manifest deterministically")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
