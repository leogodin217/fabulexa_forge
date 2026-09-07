"""Acceptance: every recipe drips, honestly, to its one-shot export.

Two properties, checked over the dimensional, source, and base recipe corpora
against the recipe emit with a day cadence:

1. Reconciliation — drip to drained into a DuckDB warehouse; the author-named
   tables equal the one-shot export of the same emit (`compare_datasets`).
2. Honesty — after window k has landed, the warehouse equals the one-shot
   export of the same emit *sliced at the window's cutoff* (`slice_emit`, the
   test-side producer slice): every value is what was knowable at the cutoff,
   and a table contains exactly the rows that existed by then.

Neither test knows how a window is computed. They are the definition of done
for horizon windowing and stay as the regression gate afterwards.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.slice_emit import slice_emit
from recipes._harness import RecipeFolder, discover_recipes
from recipes._recipe_fixture import DAY, build_recipe_emit

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.compare.engine import compare_datasets
from fabulexa_forge.compare.render import render_comparison_text
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.config.models import ExportConfig, IncrementalConfig
from fabulexa_forge.exporters.base.engine import export_base
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.exporters.source.engine import export_source
from fabulexa_forge.incremental.driver import export_incremental_next
from fabulexa_forge.reader.emit import open_emit

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor

_RECIPES_ROOT = Path(__file__).parent.parent.parent / "examples" / "recipes"
_RECIPES: list[RecipeFolder] = (
    discover_recipes(_RECIPES_ROOT)
    + discover_recipes(_RECIPES_ROOT / "source")
    + discover_recipes(_RECIPES_ROOT / "base")
)
_EXPORTERS = {
    "dimensional": export_dimensional,
    "source": export_source,
    "base": export_base,
}

#: The recipe emit's data spans days 1..3; a slice at the end of day 4 bounds
#: every instant, so a drip over it drains after four day-windows.
_TAPE_END = 4 * DAY - 1
_WINDOWS = 4


def _dripping_config(
    recipe: RecipeFolder, anchor: "EffectiveAnchor | None"
) -> ExportConfig:
    """The recipe's config with a one-day cadence in the regime its anchor selects."""
    config = load_export_config(recipe.config_path)
    if config.base is not None and config.base.slice_at is not None:
        pytest.skip("base.slice_at and an incremental cadence are mutually exclusive")
    cadence = (
        IncrementalConfig(period="day")
        if anchor is not None
        else IncrementalConfig(sim_period_ns=DAY)
    )
    return config.model_copy(update={"incremental": cadence})


def _anchor_for(recipe: RecipeFolder, emit_dir: Path) -> "EffectiveAnchor | None":
    config = load_export_config(recipe.config_path)
    with open_emit(emit_dir) as emit:
        return resolve_effective_anchor(
            emit.sidecar.runtime(), config.rebase, None, None
        )


def _one_shot(
    config: ExportConfig, emit_dir: Path, out: Path, anchor: "EffectiveAnchor | None"
) -> None:
    with open_emit(emit_dir) as emit:
        _EXPORTERS[config.mode](
            emit, config, out, "duckdb", anchor, discard_notice_sink, None
        )


def _drip(
    config: ExportConfig,
    emit_dir: Path,
    warehouse: Path,
    anchor: "EffectiveAnchor | None",
    windows: int,
) -> None:
    with open_emit(emit_dir) as emit:
        for _ in range(windows):
            outcome = export_incremental_next(
                emit, config, warehouse, "duckdb", anchor, discard_notice_sink, None
            )
            assert outcome.status == "emitted"


def _author_tables(db: Path) -> list[str]:
    """The one-shot export's tables — the compare universe, bookkeeping excluded."""
    conn = duckdb.connect(str(db), read_only=True)
    try:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_type = 'BASE TABLE'"
        ).fetchall()
    finally:
        conn.close()
    return sorted(str(r[0]) for r in rows if not str(r[0]).startswith("_export"))


@pytest.fixture(scope="module")
def bounded_emit_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The recipe emit with `slice_at` bounding its data (the drip's drain point)."""
    recipe_emit = tmp_path_factory.mktemp("recipe_emit")
    build_recipe_emit(recipe_emit)
    return slice_emit(
        recipe_emit, tmp_path_factory.mktemp("bounded") / "emit", _TAPE_END
    )


@pytest.mark.parametrize("recipe", _RECIPES, ids=lambda r: r.name)
def test_drip_reconciles_to_one_shot(
    recipe: RecipeFolder, bounded_emit_dir: Path, tmp_path: Path
) -> None:
    anchor = _anchor_for(recipe, bounded_emit_dir)
    config = _dripping_config(recipe, anchor)

    expected = tmp_path / "one_shot.duckdb"
    _one_shot(config, bounded_emit_dir, expected, anchor)

    warehouse = tmp_path / "drip.duckdb"
    _drip(config, bounded_emit_dir, warehouse, anchor, _WINDOWS)
    with open_emit(bounded_emit_dir) as emit:
        drained = export_incremental_next(
            emit, config, warehouse, "duckdb", anchor, discard_notice_sink, None
        )
    assert drained.status == "drained"

    result = compare_datasets(expected, warehouse, tables=_author_tables(expected))
    assert result.equal, render_comparison_text(result)


@pytest.mark.parametrize("window_index", range(_WINDOWS))
@pytest.mark.parametrize("recipe", _RECIPES, ids=lambda r: r.name)
def test_window_is_honest_at_its_cutoff(
    recipe: RecipeFolder, window_index: int, bounded_emit_dir: Path, tmp_path: Path
) -> None:
    anchor = _anchor_for(recipe, bounded_emit_dir)
    config = _dripping_config(recipe, anchor)
    cutoff = (window_index + 1) * DAY - 1

    sliced = slice_emit(bounded_emit_dir, tmp_path / "sliced", cutoff)
    expected = tmp_path / "one_shot_at_cutoff.duckdb"
    _one_shot(config, sliced, expected, anchor)

    warehouse = tmp_path / "drip.duckdb"
    _drip(config, bounded_emit_dir, warehouse, anchor, window_index + 1)

    result = compare_datasets(expected, warehouse, tables=_author_tables(expected))
    assert result.equal, render_comparison_text(result)
