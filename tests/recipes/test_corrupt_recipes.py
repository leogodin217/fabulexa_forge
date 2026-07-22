"""Corrupt recipe corpus gate tests.

The corrupter sibling of test_recipes.py / test_stream_recipes.py. The corpus lives
under ``examples/recipes/corrupt/``; each recipe is a ``config.yaml`` (a
CorruptConfig) plus an ``expect.yaml`` (a CorruptRecipeExpectation over the
emitted defects.json).

Four gates:
1. config-load    : load_corrupt_config succeeds for every corrupt recipe.
2. run-and-assert : open emit -> load config -> corrupt_emit -> assert_corrupt_output
                    over defects.json (defect counts by class, impact union,
                    spot-checked defects).
3. corpus guard   : corpus is non-empty; every folder contains exactly the two
                    expected files.
4. set equality   : validate's failing-check set, scoped to the manifest's C1-C13
                    impact vocabulary, == manifest impact union minus
                    'beyond-c1-c12' -- the recipes are curated so every declared
                    code fires (no heals, no skip-guard cases). Containment alone
                    is asserted more broadly in tests/corrupters/test_agreement.py.
                    The C1-C13 scoping keeps a sentinel-labeled C14 break accurate,
                    not false: C14 (a sidecar-only sub-type check) is outside the
                    manifest's ImpactCode vocabulary entirely, and no corrupter can
                    break it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from reader._fixtures_build import build_membership_intervals

from fabulexa_forge.config.loader import load_corrupt_config
from fabulexa_forge.corrupters.engine import corrupt_emit
from fabulexa_forge.reader import conformance
from fabulexa_forge.reader.emit import open_emit

from ._harness import (
    RecipeFolder,
    assert_corrupt_output,
    discover_recipes,
    failing_checks_in_manifest_vocabulary,
    load_corrupt_expectation,
)

_CORRUPT_RECIPES_ROOT = (
    Path(__file__).parent.parent.parent / "examples" / "recipes" / "corrupt"
)

# Collect once at module import so parametrize IDs are stable.
_ALL_CORRUPT_RECIPES: list[RecipeFolder] = discover_recipes(_CORRUPT_RECIPES_ROOT)


@pytest.fixture(scope="session")
def corrupt_recipe_emit_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-scoped source emit for the corrupt recipe corpus.

    Uses the base-reader "membership_intervals" fixture
    (tests/reader/_fixtures_build.py) -- the family-E superset of
    "history_series": identical history, records, and
    membership__actor__appointments rows (so its `slice_at` (100) still sits
    after each history series' latest pre-slice row, keeping the null_cells /
    schema_drift C6 trip the curated recipes' strict manifest/validate
    set-equality gate depends on), plus one added interval-rich membership
    table, `membership__actor__oncall`, family E's recipes select over. The
    records/membership rows `build_spanning` and `build_history_series` share
    are identical here too, so the four exact-`table:` recipes predating this
    sprint are unaffected; only `target-glob-and-record-kind`, whose
    `record_kind: actor` selector pools every actor-kind table, now also pools
    the added oncall rows (its expect.yaml is re-baselined for this).

    Returns:
        Path to the emit directory containing run.duckdb and base.json.
    """
    dest = tmp_path_factory.mktemp("corrupt_recipe_emit")
    build_membership_intervals(dest)
    return dest


# ---------------------------------------------------------------------------
# Gate 1 — config load
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recipe", _ALL_CORRUPT_RECIPES, ids=lambda r: r.name)
def test_corrupt_recipe_config_loads(recipe: RecipeFolder) -> None:
    """load_corrupt_config raises no ConfigError for a valid corrupt recipe."""
    load_corrupt_config(recipe.config_path)  # raises ConfigError on failure


# ---------------------------------------------------------------------------
# Gate 2 — run-and-assert
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recipe", _ALL_CORRUPT_RECIPES, ids=lambda r: r.name)
def test_corrupt_recipe_run_and_assert(
    recipe: RecipeFolder, corrupt_recipe_emit_dir: Path, tmp_path: Path
) -> None:
    """Full round-trip: corrupt the recipe emit and assert against expect.yaml."""
    config = load_corrupt_config(recipe.config_path)
    expectation = load_corrupt_expectation(recipe.expect_path)

    out_dir = tmp_path / recipe.name
    with open_emit(corrupt_recipe_emit_dir) as emit:
        corrupt_emit(emit, config, out_dir)

    assert_corrupt_output(expectation, out_dir / "defects.json")


# ---------------------------------------------------------------------------
# Gate 3 — corpus guard
# ---------------------------------------------------------------------------


def test_corrupt_recipe_corpus_nonempty() -> None:
    """The corrupt recipe corpus contains at least one recipe."""
    assert _ALL_CORRUPT_RECIPES, (
        f"No corrupt recipes found under {_CORRUPT_RECIPES_ROOT}. "
        "Add at least one recipe folder with config.yaml and expect.yaml."
    )


@pytest.mark.parametrize("recipe", _ALL_CORRUPT_RECIPES, ids=lambda r: r.name)
def test_corrupt_recipe_folder_well_formed(recipe: RecipeFolder) -> None:
    """Each corrupt recipe folder contains exactly {config.yaml, expect.yaml}."""
    folder = recipe.config_path.parent
    actual_names = {p.name for p in folder.iterdir() if not p.name.startswith(".")}
    expected_names = {"config.yaml", "expect.yaml"}
    assert actual_names == expected_names, (
        f"Corrupt recipe folder '{recipe.name}' must contain exactly"
        f" {{config.yaml, expect.yaml}}; found: {sorted(actual_names)}"
    )


# ---------------------------------------------------------------------------
# Gate 4 — manifest / validate agreement, set equality
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recipe", _ALL_CORRUPT_RECIPES, ids=lambda r: r.name)
def test_corrupt_recipe_agreement_set_equality(
    recipe: RecipeFolder, corrupt_recipe_emit_dir: Path, tmp_path: Path
) -> None:
    """validate's C1-C12-scoped failing-check set == manifest impact union minus
    'beyond-c1-c12'.

    Curated recipes avoid heals and skip-guard cases, so containment tightens to
    strict set equality here.
    """
    config = load_corrupt_config(recipe.config_path)
    out_dir = tmp_path / f"{recipe.name}_agreement"
    with open_emit(corrupt_recipe_emit_dir) as emit:
        corrupt_emit(emit, config, out_dir)

    with open_emit(out_dir) as corrupted:
        report = conformance.validate(corrupted)
    failing = failing_checks_in_manifest_vocabulary(report)

    manifest = json.loads((out_dir / "defects.json").read_text(encoding="utf-8"))
    impact_union = {
        code
        for defect in manifest["defects"]
        for code in defect["impact"]
        if code != "beyond-c1-c12"
    }
    assert failing == impact_union
