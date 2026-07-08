"""Shared pytest fixtures for the recipe integration tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from ._recipe_fixture import build_recipe_emit


@pytest.fixture(scope="session")
def recipe_emit_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-scoped emit directory built once for all recipe tests.

    Returns:
        Path to the emit directory containing run.duckdb and base.json.
    """
    dest = tmp_path_factory.mktemp("recipe_emit")
    build_recipe_emit(dest)
    return dest
