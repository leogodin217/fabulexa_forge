#!/usr/bin/env python
"""
Demo: the base recipe corpus, end to end, plus corrupter composition
Sprint: base-mode
Phase: 5

Runs every recipe under `examples/recipes/base/` through `export_base` over
the shared recipe fixture emit, printing each recipe's output tables and row
counts. Then builds a fresh copy of the same fixture, corrupts one cell
(`records__patient.prop__name` on p001) with a `null_cells` operation, and
runs a bare `mode: base` export over the corrupted emit -- showing the
declared defect surfaced unchanged in the reconstructed value (not dropped),
and that the corrupted export's row counts exactly match the uncorrupted
export's (the totality guarantee: no row dropped, no cast error).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.config.models import (
    Amount,
    CorruptConfig,
    ExportConfig,
    NullCells,
    Target,
)
from fabulexa_forge.corrupters.engine import corrupt_emit
from fabulexa_forge.exporters.base.engine import export_base
from fabulexa_forge.exporters.notices import render_notice_stderr
from fabulexa_forge.reader.emit import open_emit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BASE_RECIPES_ROOT = _REPO_ROOT / "examples" / "recipes" / "base"

sys.path.insert(0, str(_REPO_ROOT / "tests"))
from recipes._harness import RecipeFolder, discover_recipes  # noqa: E402
from recipes._recipe_fixture import build_recipe_emit  # noqa: E402


def run_recipe_corpus(
    emit_dir: Path, out_dir: Path
) -> list[tuple[str, dict[str, int]]]:
    """Run every base recipe through export_base; return (name, row_counts)
    for each, in discovery order."""
    recipes: list[RecipeFolder] = discover_recipes(_BASE_RECIPES_ROOT)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, dict[str, int]]] = []
    for recipe in recipes:
        config = load_export_config(recipe.config_path)
        out_path = out_dir / f"{recipe.name}.duckdb"
        with open_emit(emit_dir) as emit:
            anchor = resolve_effective_anchor(
                emit.sidecar.runtime(), config.rebase, None, None
            )
            row_counts = export_base(
                emit, config, out_path, "duckdb", anchor, render_notice_stderr
            )
        results.append((recipe.name, row_counts))
    return results


def print_recipe_results(results: list[tuple[str, dict[str, int]]]) -> None:
    """Print each recipe's output tables and row counts."""
    print("--- Base recipe corpus ---")
    for name, row_counts in results:
        counts_str = ", ".join(f"{t}: {c} rows" for t, c in sorted(row_counts.items()))
        print(f"  [{name}] {counts_str}")


def _null_patient_name_config() -> CorruptConfig:
    """A one-operation corrupt config: null records__patient.prop__name on p001."""
    return CorruptConfig(
        seed=1,
        operations=[
            NullCells(
                kind="null_cells",
                name="null_patient_name",
                target=Target(
                    table="records__patient",
                    where={"record_id": "p001"},
                    columns=["prop__name"],
                ),
                amount=Amount(rate=1.0),
            ),
        ],
    )


def run_base_export(emit_dir: Path, out_path: Path) -> dict[str, int]:
    """Run a bare mode='base' export of emit_dir to CSV; return row counts."""
    out_path.mkdir(parents=True, exist_ok=True)
    config = ExportConfig(mode="base")
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        return export_base(emit, config, out_path, "csv", anchor, render_notice_stderr)


def read_p001_name(out_dir: Path) -> str:
    """Read p001's prop__name cell from the patient.csv output, via DuckDB."""
    conn = duckdb.connect()
    try:
        (name,) = conn.execute(
            f"SELECT prop__name FROM read_csv_auto('{out_dir / 'patient.csv'}')"
            " WHERE id = 'p001'"
        ).fetchone()  # type: ignore[misc]
    finally:
        conn.close()
    return "" if name is None else str(name)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        fixture_dir = tmp_path / "fixture"
        build_recipe_emit(fixture_dir)

        corpus_results = run_recipe_corpus(fixture_dir, tmp_path / "corpus")
        print_recipe_results(corpus_results)

        baseline_dir = tmp_path / "baseline_dump"
        baseline_counts = run_base_export(fixture_dir, baseline_dir)
        baseline_name = read_p001_name(baseline_dir)

        corrupt_source_dir = tmp_path / "corrupt_source"
        build_recipe_emit(corrupt_source_dir)
        corrupted_dir = tmp_path / "corrupted"
        with open_emit(corrupt_source_dir) as emit:
            report = corrupt_emit(emit, _null_patient_name_config(), corrupted_dir)
        assert report.outcomes[0].units_affected == 1

        corrupted_dump_dir = tmp_path / "corrupted_dump"
        corrupted_counts = run_base_export(corrupted_dir, corrupted_dump_dir)
        corrupted_name = read_p001_name(corrupted_dump_dir)

        print("\n--- Corrupter composition: base export over a corrupted emit ---")
        print(f"  baseline  p001.prop__name={baseline_name!r}")
        print(
            f"  corrupted p001.prop__name={corrupted_name!r}  (surfaced, not dropped)"
        )
        print(f"  baseline row counts:  {sorted(baseline_counts.items())}")
        print(f"  corrupted row counts: {sorted(corrupted_counts.items())}")

    assert baseline_name == "Alice"
    assert corrupted_name == ""
    assert corrupted_counts == baseline_counts

    print(
        "\nSUCCESS: every base recipe exported through export_base, and a base"
        " export over a corrupted emit surfaced the declared defect unchanged"
        " with row counts identical to the uncorrupted export (totality)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
