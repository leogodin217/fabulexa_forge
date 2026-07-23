#!/usr/bin/env python
"""
Demo: build_base_query_specs + export_base — full export and incremental drip
Sprint: base-mode
Phase: 4

Runs a full base export to DuckDB (`export_base`) over the shared `patient`
fixture (tests/exporters/base/_base_fixtures — p001 admitted -> active
(@2*DAY) -> discharged (@4*DAY); branch slice_at = 5*DAY), then drips two
incremental windows (sim_period_ns = 2*DAY) via `export_incremental_next`
against a `mode: base` config, printing every window's per-table row count and
p001's reconstructed `prop__status` — the same record, a different snapshot
horizon per window, exercising the driver's `mode == 'base'` dispatch branch.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge.config.models import ExportConfig
from fabulexa_forge.exporters.base.engine import export_base
from fabulexa_forge.exporters.notices import render_notice_stderr
from fabulexa_forge.incremental.driver import export_incremental_next
from fabulexa_forge.reader.emit import open_emit

_REPO_ROOT = Path(__file__).resolve().parents[4]

_DAY_NS = 86_400 * 1_000_000_000  # one civil day, in sim-time nanoseconds


def build_patient_emit_dir(dest: Path) -> Path:
    """Build the shared `patient` fixture emit into dest and return it."""
    sys.path.insert(0, str(_REPO_ROOT / "tests"))
    from exporters.base._base_fixtures import build_base_test_emit

    dest.mkdir(parents=True, exist_ok=True)
    build_base_test_emit(dest)
    return dest


def run_full_export(emit_dir: Path, out_path: Path) -> dict[str, int]:
    """Run a full (window=None) mode='base' export to DuckDB."""
    with open_emit(emit_dir) as emit:
        config = ExportConfig(mode="base")
        return export_base(emit, config, out_path, "duckdb", None, render_notice_stderr)


def _read_p001_status(out_path: Path) -> str:
    """Read p001's reconstructed prop__status from the drip warehouse."""
    conn = duckdb.connect(str(out_path), read_only=True)
    try:
        row = conn.execute(
            'SELECT "prop__status" FROM "patient" WHERE id = ?', ["p001"]
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    status: str = row[0]
    return status


def run_two_window_drip(
    emit_dir: Path, out_path: Path, sim_period_ns: int
) -> list[tuple[int, dict[str, int], str]]:
    """Drip two `--next` windows over a `mode: base` config.

    Returns one (window index, per-table row counts, p001's reconstructed
    prop__status) tuple per window, in window order.
    """
    config = ExportConfig.model_validate(
        {"mode": "base", "incremental": {"sim_period_ns": sim_period_ns}}
    )
    results: list[tuple[int, dict[str, int], str]] = []
    with open_emit(emit_dir) as emit:
        for _ in range(2):
            outcome = export_incremental_next(
                emit, config, out_path, "duckdb", None, render_notice_stderr
            )
            assert outcome.status == "emitted"
            assert outcome.window is not None
            results.append(
                (outcome.window.index, outcome.row_counts, _read_p001_status(out_path))
            )
    return results


def print_full_counts(full_counts: dict[str, int]) -> None:
    """Print the full export's per-table row counts."""
    print("--- Full export (tape's end) ---")
    for table_name, count in full_counts.items():
        print(f"  {table_name}: {count} rows")


def print_drip_results(drip_results: list[tuple[int, dict[str, int], str]]) -> None:
    """Print each window's per-table row counts and p001's reconstructed status."""
    print("\n--- Two-window incremental drip (sim_period_ns=2*DAY) ---")
    for index, row_counts, p001_status in drip_results:
        counts_str = ", ".join(f"{t}: {c} rows" for t, c in row_counts.items())
        print(f"  [window {index}] {counts_str}; p001.prop__status={p001_status!r}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = build_patient_emit_dir(tmp_path / "emit")

        full_counts = run_full_export(emit_dir, tmp_path / "full.duckdb")
        print_full_counts(full_counts)

        drip_results = run_two_window_drip(
            emit_dir, tmp_path / "drip.duckdb", sim_period_ns=2 * _DAY_NS
        )
        print_drip_results(drip_results)

    assert full_counts == {"patient": 3}
    assert drip_results[0][2] == "admitted"
    assert drip_results[1][2] == "active"

    print(
        "\nSUCCESS: export_base wrote the full snapshot, and the incremental"
        " drip's two windows reconstructed p001's evolving prop__status"
        " ('admitted' -> 'active') via the driver's mode == 'base' dispatch"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
