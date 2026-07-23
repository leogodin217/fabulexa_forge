"""Base recipe corpus gate tests.

The base sibling of test_source_recipes.py. The corpus lives under
``examples/recipes/base/``; each recipe is a ``config.yaml`` (an ExportConfig
with ``mode: base``) plus an ``expect.yaml`` (a RecipeExpectation over the
DuckDB output — the same expectation schema test_recipes.py uses, since
``export_base`` also writes a DuckDB file of named tables).

Three gates:
1. config-load   : load_export_config succeeds for every base recipe.
2. run-and-assert: open emit -> load config -> resolve anchor -> export_base
                   -> assert_recipe_output. Base never requires an anchor
                   (unlike source), so every recipe runs the full-export path
                   uniformly -- no windowed-compile branch is needed.
3. corpus guard  : corpus is non-empty; every folder contains exactly the two
                   expected files.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _support.notices import discard_notice_sink

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.exporters.base.engine import export_base
from fabulexa_forge.reader.emit import open_emit

from ._harness import (
    RecipeFolder,
    assert_recipe_output,
    discover_recipes,
    load_expectation,
)

_BASE_RECIPES_ROOT = (
    Path(__file__).parent.parent.parent / "examples" / "recipes" / "base"
)

# Collect once at module import so parametrize IDs are stable.
_ALL_BASE_RECIPES: list[RecipeFolder] = discover_recipes(_BASE_RECIPES_ROOT)


# ---------------------------------------------------------------------------
# Gate 1 — config load
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recipe", _ALL_BASE_RECIPES, ids=lambda r: r.name)
def test_base_recipe_config_loads(recipe: RecipeFolder) -> None:
    """load_export_config raises no ConfigError for a valid base recipe."""
    load_export_config(recipe.config_path)  # raises ConfigError on failure


# ---------------------------------------------------------------------------
# Gate 2 — run-and-assert
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recipe", _ALL_BASE_RECIPES, ids=lambda r: r.name)
def test_base_recipe_run_and_assert(
    recipe: RecipeFolder, recipe_emit_dir: Path, tmp_path: Path
) -> None:
    """Full round-trip: export the recipe through export_base and assert
    against expect.yaml. Base never requires a resolved anchor -- every
    recipe runs the uniform full-export path."""
    config = load_export_config(recipe.config_path)
    expectation = load_expectation(recipe.expect_path)

    out_path = tmp_path / f"{recipe.name}.duckdb"

    with open_emit(recipe_emit_dir) as emit:
        anchor = resolve_effective_anchor(
            emit.sidecar.runtime(),
            config.rebase,
            None,
            None,
        )
        export_base(
            emit,
            config,
            out_path,
            "duckdb",
            anchor,
            notice_sink=discard_notice_sink,
        )

    assert_recipe_output(expectation, out_path)


# ---------------------------------------------------------------------------
# Gate 3 — corpus guard
# ---------------------------------------------------------------------------


def test_base_recipe_corpus_nonempty() -> None:
    """The base recipe corpus contains at least one recipe."""
    assert _ALL_BASE_RECIPES, (
        f"No base recipes found under {_BASE_RECIPES_ROOT}. "
        "Add at least one recipe folder with config.yaml and expect.yaml."
    )


@pytest.mark.parametrize("recipe", _ALL_BASE_RECIPES, ids=lambda r: r.name)
def test_base_recipe_folder_well_formed(recipe: RecipeFolder) -> None:
    """Each base recipe folder contains exactly {config.yaml, expect.yaml}."""
    folder = recipe.config_path.parent
    actual_names = {p.name for p in folder.iterdir() if not p.name.startswith(".")}
    expected_names = {"config.yaml", "expect.yaml"}
    assert actual_names == expected_names, (
        f"Base recipe folder '{recipe.name}' must contain exactly"
        f" {{config.yaml, expect.yaml}}; found: {sorted(actual_names)}"
    )
