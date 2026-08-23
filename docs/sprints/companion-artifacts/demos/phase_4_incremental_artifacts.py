#!/usr/bin/env python
"""
Demo: Incremental threading -- windowed companion artifacts through --next-equivalent
library calls
Sprint: companion-artifacts
Phase: 4

Builds a minimal `mode: base` emit with a sim-time cadence, then drips a csv
export via direct library calls (export_incremental_next / export_window --
the calls cli.py itself makes for --next and --from/--to): shows window-0
artifacts landing at the output root (never inside the window's drop
directory), a whole-state artifact rewrite after window 1 (next_window_index
advancing), an untouched artifact pair on a drained invocation, a
--from/--to range invocation writing incremental.next_window_index: null,
and a mid-drip change to the config's readme_overlay field that does not
trip the drip fingerprint.
"""

from __future__ import annotations

import json
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

from fabulexa_forge.config.models import ExportConfig, IncrementalConfig  # noqa: E402
from fabulexa_forge.exporters.companion.overlay import (  # noqa: E402
    ReadmeOverlay,
    load_readme_overlay,
)
from fabulexa_forge.exporters.notices import render_notice_stderr  # noqa: E402
from fabulexa_forge.incremental.driver import (  # noqa: E402
    export_incremental_next,
    export_window,
)
from fabulexa_forge.incremental.windows import parse_range  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402

_SIM_PERIOD_NS = 100
"""Window width; demo constant, not an author-configured export value."""

_SLICE_AT = 150
"""Branch slice_at, chosen so windows 0 and 1 emit and window 2 drains."""

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
## table: patient
Overlay added mid-drip; the drip continues unaffected because the
fingerprint's canonical config dump excludes readme_overlay.
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
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": _SLICE_AT}],
        extra={"record_roles": {"patient": "dimension"}},
    )


def run_next(
    emit_dir: Path, out: Path, config: ExportConfig, overlay: ReadmeOverlay | None
):
    """One `--next`-equivalent library call: export_incremental_next."""
    with open_emit(emit_dir) as emit:
        return export_incremental_next(
            emit, config, out, "csv", None, render_notice_stderr, overlay
        )


def _read_manifest(out: Path) -> dict[str, object]:
    """Parse `base-manifest.json` at the output root."""
    return json.loads((out / "base-manifest.json").read_text(encoding="utf-8"))


def demo_drip(emit_dir: Path, out: Path, overlay_path: Path) -> None:
    """Drip window 0, window 1 (with a mid-drip readme_overlay change), then drain."""
    print("--- window 0 (--next) ---")
    config_no_overlay = ExportConfig(
        mode="base", incremental=IncrementalConfig(sim_period_ns=_SIM_PERIOD_NS)
    )
    outcome0 = run_next(emit_dir, out, config_no_overlay, None)
    assert outcome0.status == "emitted"
    assert outcome0.window is not None
    print("window label:", outcome0.window.label)
    print("output root entries:", sorted(p.name for p in out.iterdir()))
    assert (out / "base-readme.md").exists()
    assert (out / "base-manifest.json").exists()
    assert (out / outcome0.window.label).is_dir(), (
        "window data lands in its own drop dir, artifacts at the root"
    )
    manifest0 = _read_manifest(out)
    print("manifest incremental block:", manifest0["incremental"])
    manifest0_bytes = (out / "base-manifest.json").read_bytes()
    readme0_bytes = (out / "base-readme.md").read_bytes()

    print("\n--- window 1 (--next, readme_overlay added mid-drip) ---")
    config_with_overlay = ExportConfig(
        mode="base",
        incremental=IncrementalConfig(sim_period_ns=_SIM_PERIOD_NS),
        readme_overlay=str(overlay_path),
    )
    overlay = load_readme_overlay(overlay_path)
    outcome1 = run_next(emit_dir, out, config_with_overlay, overlay)
    assert outcome1.status == "emitted"
    print("readme_overlay added mid-drip: no IncrementalFingerprintMismatch raised")
    manifest1 = _read_manifest(out)
    print("manifest incremental block:", manifest1["incremental"])
    assert manifest1["incremental"]["next_window_index"] == 2
    manifest1_bytes = (out / "base-manifest.json").read_bytes()
    readme1_bytes = (out / "base-readme.md").read_bytes()
    assert manifest1_bytes != manifest0_bytes, (
        "whole-state rewrite changes the manifest"
    )
    assert readme1_bytes != readme0_bytes, "whole-state rewrite changes the README"

    print("\n--- window 2 (--next, drained) ---")
    outcome2 = run_next(emit_dir, out, config_with_overlay, overlay)
    assert outcome2.status == "drained"
    assert outcome2.window is None
    assert outcome2.report is None
    assert (out / "base-manifest.json").read_bytes() == manifest1_bytes, (
        "a drained invocation touches neither artifact file"
    )
    assert (out / "base-readme.md").read_bytes() == readme1_bytes
    print("drained: both artifact files untouched, confirmed")


def demo_range(emit_dir: Path, out: Path) -> None:
    """A --from/--to range export writes incremental.next_window_index: null."""
    print("\n--- explicit range (--from/--to) ---")
    config = ExportConfig(mode="base")
    window = parse_range("0", str(2 * _SIM_PERIOD_NS), None)
    with open_emit(emit_dir) as emit:
        report = export_window(
            emit, config, out, "csv", None, window, None, render_notice_stderr, None
        )
    print("window label:", window.label, "tables:", [t.name for t in report.tables])
    manifest = _read_manifest(out)
    print("manifest incremental block:", manifest["incremental"])
    assert manifest["incremental"]["next_window_index"] is None


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        emit_dir = tmp_dir / "emit"
        emit_dir.mkdir()
        build_demo_emit(emit_dir)

        overlay_path = tmp_dir / "overlay.md"
        overlay_path.write_text(_OVERLAY_TEXT, encoding="utf-8")

        drip_out = tmp_dir / "drip-out"
        demo_drip(emit_dir, drip_out, overlay_path)

        range_out = tmp_dir / "range-out"
        demo_range(emit_dir, range_out)

    print(
        "\nSUCCESS: windowed exports rewrite companion artifacts whole-state,"
        " drained invocations leave them untouched, and readme_overlay never"
        " trips the drip fingerprint"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
