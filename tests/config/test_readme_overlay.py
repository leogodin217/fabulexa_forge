"""Tests for ExportConfig.readme_overlay (parse-time only; filesystem
resolution and loading live at the overlay/CLI layer)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fabulexa_forge.config.models import ExportConfig


def test_absent_readme_overlay_loads_as_none() -> None:
    """A config with no readme_overlay key loads with the field None."""
    config = ExportConfig.model_validate({"mode": "base"})
    assert config.readme_overlay is None


def test_readme_overlay_string_loads() -> None:
    """A non-empty readme_overlay string loads verbatim."""
    config = ExportConfig.model_validate(
        {"mode": "base", "readme_overlay": "./readme-notes.md"}
    )
    assert config.readme_overlay == "./readme-notes.md"


def test_empty_readme_overlay_rejected() -> None:
    """readme_overlay: '' is rejected by readme_overlay_nonempty."""
    with pytest.raises(ValidationError, match="non-empty"):
        ExportConfig.model_validate({"mode": "base", "readme_overlay": ""})


def test_whitespace_only_readme_overlay_rejected() -> None:
    """readme_overlay: '   ' (whitespace-only) is rejected by
    readme_overlay_nonempty."""
    with pytest.raises(ValidationError, match="non-empty"):
        ExportConfig.model_validate({"mode": "base", "readme_overlay": "   "})
