"""Tests for load_export_config."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fabulexa_forge.config.loader import (
    load_corrupt_config,
    load_export_config,
    load_stream_config,
    load_yaml_mapping,
)
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


# ---------------------------------------------------------------------------
# load_yaml_mapping — duplicate-key refusal
# ---------------------------------------------------------------------------

MINIMAL_VALID_STREAM_YAML = textwrap.dedent("""\
    content: state-changes
    streams:
      - name: patients
        kind: patient
        properties:
          - name
          - status
""")

MINIMAL_VALID_CORRUPT_YAML = textwrap.dedent("""\
    seed: 1
    operations:
      - kind: null_cells
        target:
          table: records__patient
          columns: [prop__email]
        amount: { rate: 0.05 }
""")


def test_duplicate_top_level_key_raises_config_error(tmp_path: Path) -> None:
    """A duplicate top-level key names file, key, and line."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("mode: dimensional\nmode: streaming\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate key 'mode'") as exc_info:
        load_export_config(cfg)
    message = str(exc_info.value)
    assert str(cfg) in message
    assert "line 2" in message


def test_duplicate_nested_key_raises_config_error(tmp_path: Path) -> None:
    """A duplicate key nested inside a mapping (not a list item) is refused."""
    yaml_text = textwrap.dedent("""\
        mode: dimensional
        dimensional:
          tables:
            - name: dim_actor
              role: dim
              scd: type1
              source:
                grain: records
                grain: records
                kind: actor
              key: [id]
              columns:
                - name: id
                  from: record_id
    """)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate key 'grain'"):
        load_export_config(cfg)


def test_duplicate_key_inside_list_item_mapping_raises_config_error(
    tmp_path: Path,
) -> None:
    """A duplicate key inside a list item's own mapping is refused."""
    yaml_text = textwrap.dedent("""\
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
                  name: id_alt
                  from: record_id
    """)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate key 'name'"):
        load_export_config(cfg)


def test_stream_loader_refuses_duplicate_key_with_stream_label(
    tmp_path: Path,
) -> None:
    """load_stream_config labels the duplicate-key message 'stream config'."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "content: state-changes\ncontent: membership-events\nstreams: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate key 'content' in stream config"):
        load_stream_config(cfg)


def test_corrupt_loader_refuses_duplicate_key_with_corrupt_label(
    tmp_path: Path,
) -> None:
    """load_corrupt_config labels the duplicate-key message 'corrupt config'."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("seed: 1\nseed: 2\noperations: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate key 'seed' in corrupt config"):
        load_corrupt_config(cfg)


def test_valid_config_with_repeated_values_still_loads(tmp_path: Path) -> None:
    """Repeated *values* (not keys) are not a duplicate-key error."""
    yaml_text = textwrap.dedent("""\
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
                - name: id_copy
                  from: record_id
    """)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml_text, encoding="utf-8")
    result = load_export_config(cfg)
    assert result.dimensional is not None
    assert len(result.dimensional.tables[0].columns) == 2


def test_load_yaml_mapping_parses_clean_document(tmp_path: Path) -> None:
    """load_yaml_mapping returns the parsed document for clean YAML."""
    result = load_yaml_mapping("a: 1\nb: 2\n", "export config", tmp_path / "x.yaml")
    assert result == {"a": 1, "b": 2}


def test_load_yaml_mapping_still_raises_on_malformed_yaml(tmp_path: Path) -> None:
    """load_yaml_mapping still raises ConfigError for genuine YAML syntax errors."""
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_yaml_mapping("a: [unclosed", "export config", tmp_path / "x.yaml")


def test_stream_loader_valid_config_still_loads(tmp_path: Path) -> None:
    """A clean stream config still loads once duplicate-key refusal is in place."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(MINIMAL_VALID_STREAM_YAML, encoding="utf-8")
    result = load_stream_config(cfg)
    assert result.content == "state-changes"


def test_corrupt_loader_valid_config_still_loads(tmp_path: Path) -> None:
    """A clean corrupt config still loads once duplicate-key refusal is in place."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(MINIMAL_VALID_CORRUPT_YAML, encoding="utf-8")
    result = load_corrupt_config(cfg)
    assert result.seed == 1
