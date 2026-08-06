"""Tests for the declared-table `SourceConfig` grammar and its two-sided
mode/section validator.

Each test asserts on model behavior (structural constraints), not that
Pydantic parses successfully — the invariants are tested, not the library.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.config.models import ExportConfig, SourceConfig

# ---------------------------------------------------------------------------
# The declared grammar parses (design doc § Configuration, verbatim)
# ---------------------------------------------------------------------------

_DESIGN_DOC_EXAMPLE = {
    "mode": "source",
    "keys": {"trip": "presentation_id"},
    "source": {
        "tables": [
            {
                "name": "trips",
                "kind": "trip",
                "columns": [
                    "prop__status",
                    "prop__fare",
                    "prop__rider",
                    "created_sim_time",
                ],
                "rename": {"prop__fare": "fare_usd"},
            },
            {
                "name": "customers",
                "kind": "customer",
                "sub_types": ["standard", "vip"],
            },
            {
                "name": "trip_drivers",
                "membership": {"kind": "trip", "property": "drivers"},
            },
        ],
        "events": {
            "name": "versions",
            "sources": [
                {"kind": "trip", "only": ["status", "fare"]},
                {"membership": {"kind": "trip", "property": "drivers"}},
            ],
        },
        "declare_keys": True,
    },
}


def test_design_doc_configuration_example_parses() -> None:
    """The design doc's § Configuration example loads verbatim."""
    config = ExportConfig.model_validate(_DESIGN_DOC_EXAMPLE)
    assert config.mode == "source"
    assert config.keys == {"trip": "presentation_id"}
    assert config.source is not None
    assert [t.name for t in config.source.tables] == [
        "trips",
        "customers",
        "trip_drivers",
    ]

    trips, customers, trip_drivers = config.source.tables
    assert trips.kind == "trip"
    assert trips.columns == (
        "prop__status",
        "prop__fare",
        "prop__rider",
        "created_sim_time",
    )
    assert trips.rename == {"prop__fare": "fare_usd"}
    assert customers.sub_types == ("standard", "vip")
    assert trip_drivers.membership is not None
    assert trip_drivers.membership.kind == "trip"
    assert trip_drivers.membership.property == "drivers"

    events = config.source.events
    assert events is not None
    assert events.name == "versions"
    assert len(events.sources) == 2
    assert events.sources[0].kind == "trip"
    assert events.sources[0].only == ("status", "fare")
    assert events.sources[1].membership is not None
    assert events.sources[1].membership.property == "drivers"

    assert config.source.declare_keys is True


# ---------------------------------------------------------------------------
# Load-time errors: bare mode / empty section / no-output declaration
# ---------------------------------------------------------------------------


def test_bare_mode_source_is_a_load_time_error() -> None:
    """A bare `mode: source` (no `source` section) is refused — the
    bare-dump allowance dies with the exclude/rename grammar."""
    with pytest.raises(ValidationError, match="requires a 'source' section"):
        ExportConfig.model_validate({"mode": "source"})


def test_empty_source_block_is_a_load_time_error() -> None:
    """`source: {}` is refused — `SourceConfig`'s own validator additionally
    requires >= 1 of `tables` / `events`, since a source config declares its
    output or is refused at load."""
    with pytest.raises(ValidationError, match="at least one output"):
        ExportConfig.model_validate({"mode": "source", "source": {}})


def test_no_output_declaration_is_a_load_time_error() -> None:
    """`declare_keys` alone, with no `tables` and no `events`, is refused —
    `declare_keys` is not itself an output declaration."""
    with pytest.raises(ValidationError, match="at least one output"):
        SourceConfig.model_validate({"declare_keys": True})


def test_source_forbids_dimensional_section() -> None:
    """`mode='source'` forbids a `dimensional` section."""
    with pytest.raises(ValidationError, match="forbids a 'dimensional' section"):
        ExportConfig.model_validate(
            {
                "mode": "source",
                "source": {"tables": [{"name": "t", "kind": "k"}]},
                "dimensional": {
                    "tables": [
                        {
                            "name": "dim_actor",
                            "role": "dim",
                            "scd": "type1",
                            "source": {"grain": "records", "kind": "actor"},
                            "key": ["id"],
                            "columns": [{"name": "id", "from": "record_id"}],
                        }
                    ]
                },
            }
        )


def test_source_forbids_base_section() -> None:
    """`mode='source'` forbids a `base` section."""
    with pytest.raises(ValidationError, match="forbids a 'base' section"):
        ExportConfig.model_validate(
            {
                "mode": "source",
                "source": {"tables": [{"name": "t", "kind": "k"}]},
                "base": {"slice_at": 100},
            }
        )


def test_events_only_config_is_legal() -> None:
    """A log-only config (`tables` empty, `events` declared) is legal —
    `tables` defaults empty."""
    config = SourceConfig.model_validate(
        {"events": {"name": "versions", "sources": [{"kind": "trip"}]}}
    )
    assert config.tables == ()
    assert config.events is not None


# ---------------------------------------------------------------------------
# Duplicate table names in the declaration list
# ---------------------------------------------------------------------------


def test_duplicate_table_names_rejected() -> None:
    """Two `tables[]` entries sharing a `name` are refused at parse time."""
    with pytest.raises(ValidationError, match="duplicate table names"):
        SourceConfig.model_validate(
            {
                "tables": [
                    {"name": "trips", "kind": "trip"},
                    {"name": "trips", "kind": "customer"},
                ]
            }
        )


def test_distinct_table_names_are_valid() -> None:
    """Two `tables[]` entries with distinct names are valid, even when they
    address the same population."""
    config = SourceConfig.model_validate(
        {
            "tables": [
                {"name": "trips_a", "kind": "trip"},
                {"name": "trips_b", "kind": "trip"},
            ]
        }
    )
    assert len(config.tables) == 2


# ---------------------------------------------------------------------------
# `kind_labels` (source-domain-vocabulary)
# ---------------------------------------------------------------------------


def test_kind_labels_parses() -> None:
    """A well-formed `kind_labels` map parses."""
    config = SourceConfig.model_validate(
        {
            "tables": [{"name": "trips", "kind": "trip"}],
            "kind_labels": {"actor": "patient", "resource": "consultant"},
        }
    )
    assert config.kind_labels == {"actor": "patient", "resource": "consultant"}


def test_kind_labels_empty_rejected() -> None:
    """`kind_labels: {}` -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceConfig.model_validate(
            {"tables": [{"name": "trips", "kind": "trip"}], "kind_labels": {}}
        )


def test_kind_labels_empty_key_rejected() -> None:
    """`kind_labels` with an empty key -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceConfig.model_validate(
            {
                "tables": [{"name": "trips", "kind": "trip"}],
                "kind_labels": {"": "patient"},
            }
        )


def test_kind_labels_empty_value_rejected() -> None:
    """`kind_labels` with an empty value -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceConfig.model_validate(
            {
                "tables": [{"name": "trips", "kind": "trip"}],
                "kind_labels": {"actor": ""},
            }
        )


def test_kind_labels_duplicate_target_labels_rejected() -> None:
    """Two kinds mapping to one label -> rejected."""
    with pytest.raises(ValidationError, match="distinct"):
        SourceConfig.model_validate(
            {
                "tables": [{"name": "trips", "kind": "trip"}],
                "kind_labels": {"actor": "patient", "resource": "patient"},
            }
        )


def test_kind_labels_defaults_none() -> None:
    """`kind_labels` defaults to None when absent."""
    config = SourceConfig.model_validate(
        {"tables": [{"name": "trips", "kind": "trip"}]}
    )
    assert config.kind_labels is None


# ---------------------------------------------------------------------------
# `declare_keys` composes with `tables` and `events`
# ---------------------------------------------------------------------------


def test_declare_keys_composes_with_tables() -> None:
    """`declare_keys` composes freely with a `tables`-only declaration."""
    config = SourceConfig.model_validate(
        {"tables": [{"name": "trips", "kind": "trip"}], "declare_keys": True}
    )
    assert config.declare_keys is True
    assert config.tables[0].name == "trips"


def test_declare_keys_composes_with_events() -> None:
    """`declare_keys` composes freely with an `events`-only declaration."""
    config = SourceConfig.model_validate(
        {
            "events": {"name": "versions", "sources": [{"kind": "trip"}]},
            "declare_keys": True,
        }
    )
    assert config.declare_keys is True
    assert config.events is not None


def test_declare_keys_composes_with_both_tables_and_events() -> None:
    """`declare_keys` composes freely with both `tables` and `events`
    declared together."""
    config = SourceConfig.model_validate(
        {
            "tables": [{"name": "trips", "kind": "trip"}],
            "events": {"name": "versions", "sources": [{"kind": "trip"}]},
            "declare_keys": True,
        }
    )
    assert config.declare_keys is True
    assert len(config.tables) == 1
    assert config.events is not None


def test_declare_keys_defaults_false() -> None:
    """`declare_keys` defaults to False when absent."""
    config = SourceConfig.model_validate(
        {"tables": [{"name": "trips", "kind": "trip"}]}
    )
    assert config.declare_keys is False


# ---------------------------------------------------------------------------
# rebase / incremental remain valid siblings under mode: source
# ---------------------------------------------------------------------------


def test_rebase_and_incremental_are_valid_siblings_under_mode_source() -> None:
    """rebase and incremental parse alongside mode: source."""
    config = ExportConfig.model_validate(
        {
            "mode": "source",
            "source": {"tables": [{"name": "trips", "kind": "trip"}]},
            "rebase": {"timezone": "UTC"},
            "incremental": {"period": "day"},
        }
    )
    assert config.rebase is not None
    assert config.rebase.timezone == "UTC"
    assert config.incremental is not None
    assert config.incremental.period == "day"


# ---------------------------------------------------------------------------
# Loader round-trip
# ---------------------------------------------------------------------------


def test_loader_round_trips_the_declared_grammar(tmp_path: Path) -> None:
    """`load_export_config` round-trips the declared grammar from a YAML
    file on disk, identically to `ExportConfig.model_validate`."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(_DESIGN_DOC_EXAMPLE), encoding="utf-8")
    config = load_export_config(config_path)
    assert config.mode == "source"
    assert config.source is not None
    assert [t.name for t in config.source.tables] == [
        "trips",
        "customers",
        "trip_drivers",
    ]
    assert config.source.events is not None
    assert config.source.events.name == "versions"
    assert config.source.declare_keys is True
