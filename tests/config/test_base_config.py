"""Tests for BaseConfig and the base arm of ExportConfig's cross-field validators.

Each test asserts on model behavior (structural constraints), not that Pydantic
parses successfully — the invariants are tested, not the library.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fabulexa_forge.config.models import BaseConfig, ExportConfig

# ---------------------------------------------------------------------------
# Bare mode: base parses
# ---------------------------------------------------------------------------


def test_bare_mode_base_parses() -> None:
    """A bare mode: base config parses; config.base is None."""
    config = ExportConfig.model_validate({"mode": "base"})
    assert config.mode == "base"
    assert config.base is None
    assert config.dimensional is None
    assert config.source is None


def test_mode_base_with_slice_at_parses() -> None:
    """mode: base with base.slice_at parses."""
    config = ExportConfig.model_validate({"mode": "base", "base": {"slice_at": 100}})
    assert config.base is not None
    assert config.base.slice_at == 100


def test_mode_base_with_exclude_and_rename_parses() -> None:
    """mode: base with base.exclude and base.rename parses."""
    config = ExportConfig.model_validate(
        {
            "mode": "base",
            "base": {
                "exclude": {"kinds": ["scheduler"]},
                "rename": [{"table": "records__actor", "name": "actors"}],
            },
        }
    )
    assert config.base is not None
    assert config.base.exclude is not None
    assert config.base.exclude.kinds == ["scheduler"]
    assert config.base.rename is not None
    assert config.base.rename[0].table == "records__actor"


# ---------------------------------------------------------------------------
# at_least_one_field (BaseConfig)
# ---------------------------------------------------------------------------


def test_empty_base_block_rejected() -> None:
    """A bare base: {} (no field explicitly set) is rejected."""
    with pytest.raises(ValidationError, match="at least one"):
        BaseConfig.model_validate({})


def test_slice_at_zero_is_valid() -> None:
    """base: {slice_at: 0} loads — zero is a valid horizon, not 'unset'."""
    cfg = BaseConfig.model_validate({"slice_at": 0})
    assert cfg.slice_at == 0


# ---------------------------------------------------------------------------
# slice_at_non_negative (BaseConfig)
# ---------------------------------------------------------------------------


def test_slice_at_negative_rejected() -> None:
    """base: {slice_at: -1} is rejected by slice_at_non_negative."""
    with pytest.raises(ValidationError, match="non-negative"):
        BaseConfig.model_validate({"slice_at": -1})


# ---------------------------------------------------------------------------
# rename_no_sub_type (BaseConfig)
# ---------------------------------------------------------------------------


def test_rename_with_sub_type_rejected() -> None:
    """A base.rename entry setting sub_type is rejected by rename_no_sub_type."""
    with pytest.raises(ValidationError, match="sub_type"):
        BaseConfig.model_validate(
            {
                "rename": [
                    {
                        "table": "records__entity",
                        "sub_type": "consultant",
                        "name": "consultants",
                    }
                ]
            }
        )


# ---------------------------------------------------------------------------
# entries_disjoint (BaseConfig)
# ---------------------------------------------------------------------------


def test_two_rename_entries_same_table_rejected() -> None:
    """Two base.rename entries with the same table are rejected by entries_disjoint."""
    with pytest.raises(ValidationError, match="same"):
        BaseConfig.model_validate(
            {
                "rename": [
                    {"table": "records__actor", "name": "a"},
                    {"table": "records__actor", "name": "b"},
                ]
            }
        )


def test_two_rename_entries_different_tables_valid() -> None:
    """Two base.rename entries with different tables are valid."""
    cfg = BaseConfig.model_validate(
        {
            "rename": [
                {"table": "records__actor", "name": "a"},
                {"table": "records__patient", "name": "b"},
            ]
        }
    )
    assert cfg.rename is not None
    assert len(cfg.rename) == 2


# ---------------------------------------------------------------------------
# declare_keys
# ---------------------------------------------------------------------------


def test_declare_keys_true_alone_is_valid_section() -> None:
    """base: {declare_keys: true} alone is a valid, non-empty section."""
    cfg = BaseConfig.model_validate({"declare_keys": True})
    assert cfg.declare_keys is True


def test_declare_keys_false_behaves_as_absent() -> None:
    """base: {declare_keys: false} loads; declare_keys reads False, same
    off-posture as the field being absent — the config layer stores the
    author's explicit value verbatim, the engine's off/on decision is a
    separate concern."""
    cfg = BaseConfig.model_validate({"declare_keys": False})
    assert cfg.declare_keys is False


def test_declare_keys_non_bool_rejected() -> None:
    """base: {declare_keys: [...]} (not a bool) is rejected."""
    with pytest.raises(ValidationError):
        BaseConfig.model_validate({"declare_keys": []})


def test_empty_base_block_error_names_declare_keys() -> None:
    """The at-least-one-field error message names declare_keys."""
    with pytest.raises(ValidationError, match="declare_keys"):
        BaseConfig.model_validate({})


# ---------------------------------------------------------------------------
# mode_section_matches — base arm
# ---------------------------------------------------------------------------


def test_mode_base_with_source_section_rejected() -> None:
    """mode: base with a source: section is rejected by mode_section_matches."""
    with pytest.raises(ValidationError, match="forbids a 'source' section"):
        ExportConfig.model_validate(
            {"mode": "base", "source": {"exclude": {"kinds": ["x"]}}}
        )


def test_mode_base_with_dimensional_section_rejected() -> None:
    """mode: base with a dimensional: section is rejected by mode_section_matches."""
    with pytest.raises(ValidationError, match="forbids a 'dimensional' section"):
        ExportConfig.model_validate(
            {
                "mode": "base",
                "dimensional": {
                    "tables": [
                        {
                            "name": "dim_actor",
                            "role": "dim",
                            "source": {"grain": "records", "kind": "actor"},
                            "key": ["id"],
                            "columns": [{"name": "id", "from": "record_id"}],
                        }
                    ]
                },
            }
        )


def test_mode_dimensional_with_base_section_rejected() -> None:
    """mode: dimensional with a base: section is rejected by mode_section_matches."""
    with pytest.raises(ValidationError, match="forbids a 'base' section"):
        ExportConfig.model_validate(
            {
                "mode": "dimensional",
                "dimensional": {
                    "tables": [
                        {
                            "name": "dim_actor",
                            "role": "dim",
                            "source": {"grain": "records", "kind": "actor"},
                            "key": ["id"],
                            "columns": [{"name": "id", "from": "record_id"}],
                        }
                    ]
                },
                "base": {"slice_at": 0},
            }
        )


def test_mode_source_with_base_section_rejected() -> None:
    """mode: source with a base: section is rejected by mode_section_matches."""
    with pytest.raises(ValidationError, match="forbids a 'base' section"):
        ExportConfig.model_validate({"mode": "source", "base": {"slice_at": 0}})


# ---------------------------------------------------------------------------
# base_slice_at_excludes_incremental
# ---------------------------------------------------------------------------


def test_slice_at_with_incremental_rejected() -> None:
    """base.slice_at + an incremental block is rejected."""
    with pytest.raises(ValidationError, match="mutually exclusive"):
        ExportConfig.model_validate(
            {
                "mode": "base",
                "base": {"slice_at": 100},
                "incremental": {"sim_period_ns": 1},
            }
        )


def test_incremental_without_slice_at_loads() -> None:
    """mode: base + incremental with no slice_at loads (the windowed path)."""
    config = ExportConfig.model_validate(
        {"mode": "base", "incremental": {"sim_period_ns": 1}}
    )
    assert config.incremental is not None
    assert config.base is None


def test_incremental_with_exclude_and_rename_no_slice_at_loads() -> None:
    """mode: base + base.exclude/rename (no slice_at) + incremental loads."""
    config = ExportConfig.model_validate(
        {
            "mode": "base",
            "base": {
                "exclude": {"kinds": ["scheduler"]},
                "rename": [{"table": "records__actor", "name": "actors"}],
            },
            "incremental": {"sim_period_ns": 1},
        }
    )
    assert config.base is not None
    assert config.base.slice_at is None
    assert config.incremental is not None
