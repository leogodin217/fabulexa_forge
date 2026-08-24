"""Streaming recipe corpus gate tests.

The streaming sibling of test_recipes.py. The corpus lives under
``examples/recipes/streaming/``; each recipe is a ``config.yaml`` (a StreamConfig)
plus an ``expect.yaml`` (a StreamRecipeExpectation over the JSONL output).

Three gates:
1. config-load   : load_stream_config succeeds for every streaming recipe.
2. run-and-assert: open emit -> load config -> resolve anchor -> stream_export
                   (file sink) -> assert_stream_output over the <kind>.jsonl files.
3. corpus guard  : corpus is non-empty; every folder contains exactly the two
                   expected files.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _support.notices import discard_notice_sink

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_stream_config
from fabulexa_forge.exporters.streaming.driver import stream_export
from fabulexa_forge.reader.emit import open_emit

from ._harness import (
    RecipeFolder,
    assert_stream_output,
    discover_recipes,
    load_stream_expectation,
)

_STREAM_RECIPES_ROOT = (
    Path(__file__).parent.parent.parent / "examples" / "recipes" / "streaming"
)

# Collect once at module import so parametrize IDs are stable.
_ALL_STREAM_RECIPES: list[RecipeFolder] = discover_recipes(_STREAM_RECIPES_ROOT)


# ---------------------------------------------------------------------------
# Gate 1 — config load
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recipe", _ALL_STREAM_RECIPES, ids=lambda r: r.name)
def test_stream_recipe_config_loads(recipe: RecipeFolder) -> None:
    """load_stream_config raises no ConfigError for a valid streaming recipe."""
    load_stream_config(recipe.config_path)  # raises ConfigError on failure


# ---------------------------------------------------------------------------
# Gate 2 — run-and-assert
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recipe", _ALL_STREAM_RECIPES, ids=lambda r: r.name)
def test_stream_recipe_run_and_assert(
    recipe: RecipeFolder, recipe_emit_dir: Path, tmp_path: Path
) -> None:
    """Full round-trip: stream the recipe to a file sink and assert against expect.yaml.

    The expectation's ``format`` (default ``jsonl``) selects the renderer, so a
    recipe can exercise the Debezium path by declaring ``format: debezium`` in its
    expect.yaml (the config must then carry a ``debezium`` block and resolve an
    anchor). The sink is always ``file`` — the Kafka sink needs a live broker and so
    is out of the recipe corpus.
    """
    config = load_stream_config(recipe.config_path)
    expectation = load_stream_expectation(recipe.expect_path)

    out_dir = tmp_path / recipe.name
    out_dir.mkdir()

    with open_emit(recipe_emit_dir) as emit:
        anchor = resolve_effective_anchor(
            emit.sidecar.runtime(),
            config.rebase,
            None,
            None,
        )
        stream_export(
            emit,
            config,
            expectation.format,
            "file",
            out_dir,
            anchor,
            notice_sink=discard_notice_sink,
        )

    assert_stream_output(expectation, out_dir)


# ---------------------------------------------------------------------------
# Gate 3 — corpus guard
# ---------------------------------------------------------------------------


def test_stream_recipe_corpus_nonempty() -> None:
    """The streaming recipe corpus contains at least one recipe."""
    assert _ALL_STREAM_RECIPES, (
        f"No streaming recipes found under {_STREAM_RECIPES_ROOT}. "
        "Add at least one recipe folder with config.yaml and expect.yaml."
    )


@pytest.mark.parametrize("recipe", _ALL_STREAM_RECIPES, ids=lambda r: r.name)
def test_stream_recipe_folder_well_formed(recipe: RecipeFolder) -> None:
    """Each streaming recipe folder contains exactly {config.yaml, expect.yaml}."""
    folder = recipe.config_path.parent
    actual_names = {p.name for p in folder.iterdir() if not p.name.startswith(".")}
    expected_names = {"config.yaml", "expect.yaml"}
    assert actual_names == expected_names, (
        f"Streaming recipe folder '{recipe.name}' must contain exactly"
        f" {{config.yaml, expect.yaml}}; found: {sorted(actual_names)}"
    )
