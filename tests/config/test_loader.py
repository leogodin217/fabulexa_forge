"""Tests for load_export_config."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.errors import ConfigError

MINIMAL_VALID_YAML = textwrap.dedent("""\
    mode: dimensional
    dimensional:
      tables:
        - name: dim_actor
          role: dim
          scd: type1
          source:
            grain: records
            kind: actor
          key: [id]
          columns:
            - name: id
              from: record_id
""")


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    """Missing file raises ConfigError."""
    with pytest.raises(ConfigError, match="not found"):
        load_export_config(tmp_path / "does_not_exist.yaml")


def test_invalid_yaml_raises_config_error(tmp_path: Path) -> None:
    """Invalid YAML raises ConfigError."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("mode: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_export_config(bad)


def test_unknown_top_level_field_raises_config_error(tmp_path: Path) -> None:
    """Unknown top-level field raises ConfigError."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(MINIMAL_VALID_YAML + "unknown_field: bad\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="validation failed"):
        load_export_config(cfg)


def test_valid_config_loads(tmp_path: Path) -> None:
    """A valid YAML config loads to an ExportConfig."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(MINIMAL_VALID_YAML, encoding="utf-8")
    result = load_export_config(cfg)
    assert result.mode == "dimensional"
    assert result.dimensional is not None
    assert len(result.dimensional.tables) == 1
    assert result.dimensional.tables[0].name == "dim_actor"


def test_missing_required_field_raises_config_error(tmp_path: Path) -> None:
    """A config missing a required field raises ConfigError."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("mode: dimensional\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="validation failed"):
        load_export_config(cfg)
