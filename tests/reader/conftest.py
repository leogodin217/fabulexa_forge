"""Shared fixtures for reader tests.

Reusable emit-construction helpers live in `_emit_helpers.py` so test modules
can import them directly; this module exposes the common minimal-emit fixture
and the session-scoped base_fixtures mapping.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._emit_helpers import write_emit
from ._fixtures_build import build_all_fixtures


@pytest.fixture()
def emit_dir(tmp_path: Path) -> Path:
    """A minimal valid emit directory."""
    return write_emit(tmp_path)


@pytest.fixture(scope="session")
def base_fixtures(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Build all base-layer fixtures once per test session.

    Returns:
        A mapping of {fixture_name: fixture_path} for every fixture variant.
        All fixtures are built into a shared session-scoped temporary directory.
    """
    root = tmp_path_factory.mktemp("base_fixtures")
    return build_all_fixtures(root)
