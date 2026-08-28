"""Recipe corpus gate tests.

Three gates:
1. config-load  : load_export_config succeeds for every recipe.
2. run-and-assert: open emit → load config → resolve anchor → export_dimensional
                   → assert_recipe_output.
3. corpus guard : corpus is non-empty; every folder contains exactly the two
                  expected files.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _support.notices import discard_notice_sink

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.reader.emit import open_emit

from ._harness import (
    RecipeFolder,
    assert_recipe_output,
    discover_recipes,
    load_expectation,
)

_RECIPES_ROOT = Path(__file__).parent.parent.parent / "examples" / "recipes"

# Collect once at module import so parametrize IDs are stable.
_ALL_RECIPES: list[RecipeFolder] = discover_recipes(_RECIPES_ROOT)


# ---------------------------------------------------------------------------
# Gate 1 — config load
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recipe", _ALL_RECIPES, ids=lambda r: r.name)
def test_recipe_config_loads(recipe: RecipeFolder) -> None:
    """load_export_config raises no ConfigError for a valid recipe config."""
    load_export_config(recipe.config_path)  # raises ConfigError on failure


# ---------------------------------------------------------------------------
# Gate 2 — run-and-assert
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recipe", _ALL_RECIPES, ids=lambda r: r.name)
def test_recipe_run_and_assert(
    recipe: RecipeFolder, recipe_emit_dir: Path, tmp_path: Path
) -> None:
    """Full round-trip: export the recipe and assert against expect.yaml."""
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
        export_dimensional(
            emit,
            config,
            out_path,
            "duckdb",
            anchor,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    assert_recipe_output(expectation, out_path)


# ---------------------------------------------------------------------------
# Gate 3 — corpus guard
# ---------------------------------------------------------------------------


def test_recipe_corpus_nonempty() -> None:
    """The recipe corpus contains at least one recipe."""
    assert _ALL_RECIPES, (
        f"No recipes found under {_RECIPES_ROOT}. "
        "Add at least one recipe folder with config.yaml and expect.yaml."
    )


@pytest.mark.parametrize("recipe", _ALL_RECIPES, ids=lambda r: r.name)
def test_recipe_folder_well_formed(recipe: RecipeFolder) -> None:
    """Each recipe folder contains exactly {config.yaml, expect.yaml}."""
    folder = recipe.config_path.parent
    actual_names = {p.name for p in folder.iterdir() if not p.name.startswith(".")}
    expected_names = {"config.yaml", "expect.yaml"}
    assert actual_names == expected_names, (
        f"Recipe folder '{recipe.name}' must contain exactly"
        f" {{config.yaml, expect.yaml}}; found: {sorted(actual_names)}"
    )
