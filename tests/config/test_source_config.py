"""Tests for SourceConfig, RenameEntry, and the two-sided mode/section validator.

Each test asserts on model behavior (structural constraints), not that Pydantic
parses successfully — the invariants are tested, not the library.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fabulexa_forge.config.models import ExportConfig, RenameEntry, SourceConfig

# ---------------------------------------------------------------------------
# Bare mode: source parses
# ---------------------------------------------------------------------------


def test_bare_mode_source_parses() -> None:
    """A bare mode: source config parses; config.source is None."""
    config = ExportConfig.model_validate({"mode": "source"})
    assert config.mode == "source"
    assert config.source is None
    assert config.dimensional is None


def test_mode_source_with_exclude_kinds_parses() -> None:
    """mode: source with source.exclude.kinds parses."""
    config = ExportConfig.model_validate(
        {
            "mode": "source",
            "source": {"exclude": {"kinds": ["scheduler"]}},
        }
    )
    assert config.source is not None
    assert config.source.exclude is not None
    assert config.source.exclude.kinds == ["scheduler"]


def test_mode_source_with_rename_parses() -> None:
    """mode: source with source.rename parses."""
    config = ExportConfig.model_validate(
        {
            "mode": "source",
            "source": {
                "rename": [{"table": "records__actor", "name": "actors"}],
            },
        }
    )
    assert config.source is not None
    assert config.source.rename is not None
    assert config.source.rename[0].table == "records__actor"
    assert config.source.rename[0].name == "actors"


# ---------------------------------------------------------------------------
# at_least_one_field (SourceConfig)
# ---------------------------------------------------------------------------


def test_bare_source_block_rejected() -> None:
    """A bare source: {} (no field explicitly set) is rejected."""
    with pytest.raises(ValidationError, match="at least one"):
        SourceConfig.model_validate({})


def test_source_with_only_exclude_is_valid() -> None:
    """A source block setting only exclude is valid."""
    cfg = SourceConfig.model_validate({"exclude": {"kinds": ["scheduler"]}})
    assert cfg.exclude is not None
    assert cfg.rename is None


def test_source_with_only_rename_is_valid() -> None:
    """A source block setting only rename is valid."""
    cfg = SourceConfig.model_validate(
        {"rename": [{"table": "records__actor", "name": "actors"}]}
    )
    assert cfg.rename is not None
    assert cfg.exclude is None


# ---------------------------------------------------------------------------
# entry_well_formed (RenameEntry)
# ---------------------------------------------------------------------------


def test_rename_entry_neither_name_nor_columns_raises() -> None:
    """A RenameEntry with neither name nor columns raises."""
    with pytest.raises(ValidationError, match="at least one"):
        RenameEntry.model_validate({"table": "records__actor"})


def test_rename_entry_with_name_only_is_valid() -> None:
    """A RenameEntry with name only is valid."""
    entry = RenameEntry.model_validate({"table": "records__actor", "name": "actors"})
    assert entry.name == "actors"
    assert entry.columns is None


def test_rename_entry_with_columns_only_is_valid() -> None:
    """A RenameEntry with columns only is valid."""
    entry = RenameEntry.model_validate(
        {"table": "records__actor", "columns": {"record_id": "actor_id"}}
    )
    assert entry.columns == {"record_id": "actor_id"}


def test_rename_entry_empty_columns_raises() -> None:
    """A RenameEntry with an empty columns map raises."""
    with pytest.raises(ValidationError, match="must not be empty"):
        RenameEntry.model_validate({"table": "records__actor", "columns": {}})


def test_rename_entry_empty_column_key_raises() -> None:
    """A RenameEntry with an empty columns key raises."""
    with pytest.raises(ValidationError, match="keys must be non-empty"):
        RenameEntry.model_validate({"table": "records__actor", "columns": {"": "x"}})


def test_rename_entry_empty_column_value_raises() -> None:
    """A RenameEntry with an empty columns value raises."""
    with pytest.raises(ValidationError, match="values must be non-empty"):
        RenameEntry.model_validate({"table": "records__actor", "columns": {"x": ""}})


def test_rename_entry_duplicate_column_values_raises() -> None:
    """Two source columns renamed to the same output name raises."""
    with pytest.raises(ValidationError, match="distinct"):
        RenameEntry.model_validate(
            {
                "table": "records__actor",
                "columns": {"prop__id": "id", "record_id": "id"},
            }
        )


def test_rename_entry_empty_table_raises() -> None:
    """An empty table string raises."""
    with pytest.raises(ValidationError):
        RenameEntry.model_validate({"table": "", "name": "actors"})


def test_rename_entry_empty_name_raises() -> None:
    """An empty name string raises."""
    with pytest.raises(ValidationError, match="name must be a non-empty string"):
        RenameEntry.model_validate({"table": "records__actor", "name": ""})


def test_rename_entry_empty_sub_type_raises() -> None:
    """An empty sub_type string raises."""
    with pytest.raises(ValidationError, match="sub_type must be a non-empty string"):
        RenameEntry.model_validate(
            {"table": "records__entity", "sub_type": "", "name": "consultants"}
        )


# ---------------------------------------------------------------------------
# entries_disjoint (SourceConfig)
# ---------------------------------------------------------------------------


def test_two_rename_entries_same_table_and_sub_type_raises() -> None:
    """Two rename entries targeting the same (table, sub_type) raise."""
    with pytest.raises(ValidationError, match="same \\(table, sub_type\\)"):
        SourceConfig.model_validate(
            {
                "rename": [
                    {"table": "records__entity", "sub_type": "consultant", "name": "a"},
                    {"table": "records__entity", "sub_type": "consultant", "name": "b"},
                ]
            }
        )


def test_two_rename_entries_same_table_different_sub_type_is_valid() -> None:
    """Two rename entries on the same table but different sub_type are valid."""
    cfg = SourceConfig.model_validate(
        {
            "rename": [
                {"table": "records__entity", "sub_type": "consultant", "name": "a"},
                {"table": "records__entity", "sub_type": "nurse", "name": "b"},
            ]
        }
    )
    assert cfg.rename is not None
    assert len(cfg.rename) == 2


# ---------------------------------------------------------------------------
# change_delivery (SourceConfig)
# ---------------------------------------------------------------------------


def test_change_delivery_defaults_to_changelog() -> None:
    """change_delivery defaults to 'changelog' when absent."""
    cfg = SourceConfig.model_validate({"exclude": {"kinds": ["scheduler"]}})
    assert cfg.change_delivery == "changelog"


def test_change_delivery_parses_snapshot() -> None:
    """change_delivery parses the explicit 'snapshot' value."""
    cfg = SourceConfig.model_validate({"change_delivery": "snapshot"})
    assert cfg.change_delivery == "snapshot"


def test_change_delivery_alone_satisfies_at_least_one_field() -> None:
    """An explicit change_delivery: changelog alone passes at_least_one_field."""
    cfg = SourceConfig.model_validate({"change_delivery": "changelog"})
    assert cfg.change_delivery == "changelog"
    assert "change_delivery" in cfg.model_fields_set


def test_change_delivery_unknown_value_raises() -> None:
    """An unknown change_delivery value raises ValidationError."""
    with pytest.raises(ValidationError):
        SourceConfig.model_validate({"change_delivery": "bogus"})


# ---------------------------------------------------------------------------
# declare_keys (SourceConfig)
# ---------------------------------------------------------------------------


def test_declare_keys_true_alone_is_valid_section() -> None:
    """source: {declare_keys: true} alone is a valid, non-empty section."""
    cfg = SourceConfig.model_validate({"declare_keys": True})
    assert cfg.declare_keys is True


def test_declare_keys_false_behaves_as_absent() -> None:
    """source: {declare_keys: false} loads; declare_keys reads False, same
    off-posture as the field being absent — the config layer stores the
    author's explicit value verbatim, the engine's off/on decision is a
    separate concern."""
    cfg = SourceConfig.model_validate({"declare_keys": False})
    assert cfg.declare_keys is False


def test_declare_keys_non_bool_rejected() -> None:
    """source: {declare_keys: [...]} (not a bool) is rejected."""
    with pytest.raises(ValidationError):
        SourceConfig.model_validate({"declare_keys": []})


def test_empty_source_block_error_names_declare_keys() -> None:
    """The at-least-one-field error message names declare_keys."""
    with pytest.raises(ValidationError, match="declare_keys"):
        SourceConfig.model_validate({})


def test_declare_keys_composes_freely_with_change_delivery() -> None:
    """declare_keys and change_delivery compose freely in one section."""
    cfg = SourceConfig.model_validate(
        {"declare_keys": True, "change_delivery": "snapshot"}
    )
    assert cfg.declare_keys is True
    assert cfg.change_delivery == "snapshot"


# ---------------------------------------------------------------------------
# rebase / incremental remain valid siblings under mode: source
# ---------------------------------------------------------------------------


def test_rebase_and_incremental_are_valid_siblings_under_mode_source() -> None:
    """rebase and incremental parse alongside mode: source."""
    config = ExportConfig.model_validate(
        {
            "mode": "source",
            "rebase": {"timezone": "UTC"},
            "incremental": {"period": "day"},
        }
    )
    assert config.rebase is not None
    assert config.rebase.timezone == "UTC"
    assert config.incremental is not None
    assert config.incremental.period == "day"


# ---------------------------------------------------------------------------
# Rename targets are SQL identifiers (they become output table/column names)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_target",
    ["../../etc/cron.d/evil", "/etc/evil", 'triage" ; ATTACH', "has space"],
)
def test_rename_table_target_not_sql_identifier_raises(bad_target: str) -> None:
    """A rename entry's output table name outside the identifier pattern raises."""
    with pytest.raises(ValidationError, match="SQL identifier"):
        RenameEntry.model_validate({"table": "records__queue", "name": bad_target})


def test_rename_column_target_not_sql_identifier_raises() -> None:
    """A rename entry's output column name with an embedded quote raises."""
    with pytest.raises(ValidationError, match="SQL identifier"):
        RenameEntry.model_validate(
            {"table": "records__location", "columns": {"prop__name": 'na"me'}}
        )


def test_rename_sidecar_keys_stay_unrestricted() -> None:
    """Only rename *targets* are gated; sidecar-identity keys (table / column
    keys) keep their full character set (e.g. membership__K__p)."""
    entry = RenameEntry.model_validate(
        {
            "table": "membership__team__members",
            "name": "team_membership",
            "columns": {"prop__display_name": "display_name"},
        }
    )
    assert entry.name == "team_membership"
