"""Source recipe corpus gate tests.

The source sibling of test_recipes.py (dimensional). The corpus lives under
``examples/recipes/source/``; each recipe is a ``config.yaml`` (an ExportConfig
with ``mode: source``) plus an ``expect.yaml`` (a RecipeExpectation over the
DuckDB output — the same expectation schema test_recipes.py uses, since
``export_source`` also writes a DuckDB file of named tables).

Three gates:
1. config-load   : load_export_config succeeds for every source recipe.
2. run-and-assert: open emit -> load config -> resolve anchor -> export_source
                   -> assert_recipe_output. A `change_delivery: snapshot`
                   recipe cannot run through export_source (a full export
                   under snapshot raises SourceSnapshotRequiresWindows), so
                   it runs the windowed compile with one explicit full-range
                   window instead — the same specs the CLI's --from/--to
                   path applies.
3. corpus guard  : corpus is non-empty; every folder contains exactly the two
                   expected files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.exporters.source.engine import (
    build_source_query_specs,
    export_source,
)
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.writers.duckdb import write_duckdb_window

from ._harness import (
    RecipeFolder,
    assert_recipe_output,
    discover_recipes,
    load_expectation,
)
from ._recipe_fixture import DAY

_SOURCE_RECIPES_ROOT = (
    Path(__file__).parent.parent.parent / "examples" / "recipes" / "source"
)

# Collect once at module import so parametrize IDs are stable.
_ALL_SOURCE_RECIPES: list[RecipeFolder] = discover_recipes(_SOURCE_RECIPES_ROOT)


# ---------------------------------------------------------------------------
# Gate 1 — config load
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recipe", _ALL_SOURCE_RECIPES, ids=lambda r: r.name)
def test_source_recipe_config_loads(recipe: RecipeFolder) -> None:
    """load_export_config raises no ConfigError for a valid source recipe."""
    load_export_config(recipe.config_path)  # raises ConfigError on failure


# ---------------------------------------------------------------------------
# Gate 2 — run-and-assert
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recipe", _ALL_SOURCE_RECIPES, ids=lambda r: r.name)
def test_source_recipe_run_and_assert(
    recipe: RecipeFolder, recipe_emit_dir: Path, tmp_path: Path
) -> None:
    """Full round-trip: export the recipe and assert against expect.yaml.

    Snapshot-delivery recipes require a windowed invocation, so they run
    one explicit full-range window ([0, 4*DAY) covers every fixture event;
    fingerprint=None is the explicit-range path — no bookkeeping tables)
    through the windowed compile + warehouse writer instead of
    export_source.
    """
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
        if config.source is not None and config.source.change_delivery == "snapshot":
            window = Window(index=None, start_ns=0, end_ns=4 * DAY, label="full-range")
            specs = build_source_query_specs(emit, config, anchor, window)
            write_duckdb_window(emit, specs, out_path, window, fingerprint=None)
        else:
            export_source(emit, config, out_path, "duckdb", anchor)

    assert_recipe_output(expectation, out_path)


# ---------------------------------------------------------------------------
# Gate 3 — corpus guard
# ---------------------------------------------------------------------------


def test_source_recipe_corpus_nonempty() -> None:
    """The source recipe corpus contains at least one recipe."""
    assert _ALL_SOURCE_RECIPES, (
        f"No source recipes found under {_SOURCE_RECIPES_ROOT}. "
        "Add at least one recipe folder with config.yaml and expect.yaml."
    )


@pytest.mark.parametrize("recipe", _ALL_SOURCE_RECIPES, ids=lambda r: r.name)
def test_source_recipe_folder_well_formed(recipe: RecipeFolder) -> None:
    """Each source recipe folder contains exactly {config.yaml, expect.yaml}."""
    folder = recipe.config_path.parent
    actual_names = {p.name for p in folder.iterdir() if not p.name.startswith(".")}
    expected_names = {"config.yaml", "expect.yaml"}
    assert actual_names == expected_names, (
        f"Source recipe folder '{recipe.name}' must contain exactly"
        f" {{config.yaml, expect.yaml}}; found: {sorted(actual_names)}"
    )
