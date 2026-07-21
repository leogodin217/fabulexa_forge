"""Tests for derivations.guard.require_single_branch."""

from __future__ import annotations

import pytest

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.errors import ExportError
from fabulexa_forge.reader.sidecar import Sidecar


def _make_sidecar(branches: list[dict[str, object]]) -> Sidecar:
    """Build a minimal Sidecar with the given branches list."""
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": branches,
        "tables": [],
    }
    return Sidecar.from_raw(raw)


def test_single_branch_returns_fork_path() -> None:
    """require_single_branch on a single-branch emit returns the fork_path."""
    from fabulexa_forge.derivations import require_single_branch

    sidecar = _make_sidecar([{"fork_path": "trunk", "parent": None, "slice_at": 100}])
    result = require_single_branch(sidecar)
    assert result == "trunk"


def test_two_branch_emit_raises_export_error() -> None:
    """Two-branch emit raises ExportError with the unified SingleBranch message."""
    from fabulexa_forge.derivations import require_single_branch

    sidecar = _make_sidecar(
        [
            {"fork_path": "trunk", "parent": None, "slice_at": 0},
            {"fork_path": "trunk@branch_a", "parent": "trunk", "slice_at": 50},
        ]
    )
    with pytest.raises(ExportError, match="export requires a single-branch emit"):
        require_single_branch(sidecar)


def test_zero_branch_sidecar_raises_export_error() -> None:
    """Zero-branch sidecar raises ExportError (the n == 0 direction of n != 1).

    Sidecar.from_raw rejects an empty branches list at parse time, so the
    zero-branch Sidecar is constructed directly to exercise the guard's own
    defensive handling of an empty branches tuple.
    """
    from fabulexa_forge.derivations import require_single_branch

    sidecar = Sidecar(
        raw={},
        base_format_version=SUPPORTED_BASE_FORMAT_VERSION,
        branches=(),
        tables=(),
        runtime=None,
        pinned_ids={},
        enum_domains={},
        record_roles=None,
        sub_type_columns=None,
    )
    with pytest.raises(ExportError, match="emit has 0 branches"):
        require_single_branch(sidecar)


def test_layer_direction_guard_imports() -> None:
    """derivations.guard imports nothing from exporters.* or config."""
    import importlib
    import sys

    mod = importlib.import_module("fabulexa_forge.derivations.guard")
    for name, submod in sys.modules.items():
        if name.startswith("fabulexa_forge.exporters") or name.startswith(
            "fabulexa_forge.config"
        ):
            assert submod not in vars(mod).values(), (
                f"derivations.guard must not import from {name}"
            )
