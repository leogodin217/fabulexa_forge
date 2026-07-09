"""Tests for config model parse-time validators.

Each test asserts on model behavior (structural constraints), not that Pydantic
parses successfully — the invariants are tested, not the library.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fabulexa_export.config.models import (
    ColumnDecl,
    DerivedSpec,
    DimensionalConfig,
    ElapsedSpec,
    ExcludeDecl,
    ExportConfig,
    FkClause,
    IncrementalConfig,
    RebaseConfig,
    SourceDecl,
    TableDecl,
    ValueMapSpec,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_RECORDS_SOURCE = {"grain": "records", "kind": "actor"}
MINIMAL_HISTORY_SOURCE = {
    "grain": "history_point",
    "kind": "actor",
    "property": "status",
}
MEMBERSHIP_SOURCE = {"grain": "membership", "kind": "actor", "property": "roles"}

MINIMAL_COLUMN = {"name": "id", "from": "record_id"}
MINIMAL_TABLE = {
    "name": "dim_x",
    "role": "dim",
    "scd": "type1",
    "source": MINIMAL_RECORDS_SOURCE,
    "key": ["id"],
    "columns": [MINIMAL_COLUMN],
}


def _make_table(**overrides: object) -> dict:
    return {**MINIMAL_TABLE, **overrides}


# ---------------------------------------------------------------------------
# Doc worked config round-trip
# ---------------------------------------------------------------------------

WORKED_CONFIG = {
    "mode": "dimensional",
    "dimensional": {
        "exclude": {"kinds": ["scheduler", "clock"]},
        "tables": [
            {
                "name": "dim_patient",
                "role": "dim",
                "scd": "type2",
                "source": {"grain": "records", "kind": "actor"},
                "key": ["id", "valid_from"],
                "columns": [
                    {"name": "id", "from": "record_id"},
                    {"name": "person_gender_code", "from": "prop__gender_code"},
                    {"name": "status", "from": "prop__status"},
                    {"name": "admission_count", "from": "prop__admission_count"},
                    {"name": "total_spell_tariff", "null": True},
                    {"name": "valid_from", "derived": {"scd_window": "valid_from"}},
                    {"name": "valid_to", "derived": {"scd_window": "valid_to"}},
                    {"name": "active", "from": "active"},
                ],
            },
            {
                "name": "dim_consultant",
                "role": "dim",
                "scd": "type1",
                "source": {
                    "grain": "records",
                    "kind": "entity",
                    "filter": {"prop__entity_type": "consultant"},
                },
                "key": ["id", "valid_from"],
                "columns": [
                    {"name": "id", "from": "record_id"},
                    {"name": "main_specialty", "from": "prop__main_specialty"},
                    {"name": "grade", "from": "prop__grade"},
                    {"name": "valid_from", "derived": {"scd_window": "valid_from"}},
                    {"name": "valid_to", "null": True},
                ],
            },
            {
                "name": "fact_ed_arrival",
                "role": "fact",
                "source": {
                    "grain": "records",
                    "kind": "tick_decision",
                    "filter": {"prop__decision_type": "ed_arrival"},
                },
                "key": ["decision_id"],
                "columns": [
                    {"name": "decision_id", "from": "record_id"},
                    {
                        "name": "timestamp",
                        "derived": {"timestamp": {"source": "last_mutation_sim_time"}},
                    },
                    {
                        "name": "event_sequence",
                        "derived": {
                            "ordinal": {
                                "partition_by": "patient_id",
                                "order_by": "timestamp",
                            }
                        },
                    },
                    {
                        "name": "patient_id",
                        "fk": {"to": "dim_patient", "via": "reference"},
                    },
                    {
                        "name": "consultant_id",
                        "fk": {
                            "to": "dim_consultant",
                            "via": "membership",
                            "where": {"elem__role_name": "surgeon"},
                        },
                    },
                    {"name": "attendance_id", "correlation": "prop__journey_instance"},
                    {"name": "state", "from": "prop__decision_type"},
                ],
            },
            {
                "name": "fact_medication_administered",
                "role": "fact",
                "source": {
                    "grain": "membership",
                    "kind": "tick_decision",
                    "property": "bindings",
                    "where": {"elem__role_name": "admitted_meds"},
                },
                "key": ["decision_id", "pick_index"],
                "columns": [
                    {"name": "decision_id", "from": "record_id"},
                    {"name": "pick_index", "from": "elem__pick_index"},
                    {
                        "name": "timestamp",
                        "derived": {"timestamp": {"source": "joined_sim_time"}},
                    },
                    {
                        "name": "patient_id",
                        "fk": {"to": "dim_patient", "via": "reference"},
                    },
                    {
                        "name": "medication_id",
                        "fk": {"to": "dim_medication", "via": "membership"},
                    },
                    {
                        "name": "event_sequence",
                        "derived": {
                            "ordinal": {
                                "partition_by": "patient_id",
                                "order_by": "timestamp",
                            }
                        },
                    },
                ],
            },
            {
                "name": "fact_fft_response",
                "role": "fact",
                "source": {
                    "grain": "history_point",
                    "kind": "actor",
                    "property": "fft_outcome",
                },
                "key": ["decision_id"],
                "columns": [
                    {"name": "decision_id", "from": "record_id"},
                    {
                        "name": "timestamp",
                        "derived": {"timestamp": {"source": "sim_time"}},
                    },
                    {
                        "name": "recommendation_score",
                        "derived": {
                            "value_map": {
                                "from": "value",
                                "map": {
                                    "very_poor": 1,
                                    "poor": 2,
                                    "neither": 3,
                                    "good": 4,
                                    "very_good": 5,
                                },
                            }
                        },
                    },
                    {"name": "patient_id", "from": "record_id"},
                ],
            },
            {
                "name": "fact_journey_states",
                "role": "fact",
                "source": {
                    "grain": "history_interval",
                    "kind": "journey_instance",
                    "property": "state",
                },
                "key": ["journey_instance_id", "entered_at"],
                "columns": [
                    {"name": "journey_instance_id", "from": "record_id"},
                    {"name": "state", "from": "value"},
                    {
                        "name": "entered_at",
                        "derived": {"timestamp": {"source": "sim_time"}},
                    },
                    {
                        "name": "exited_at",
                        "derived": {"timestamp": {"source": "lead_sim_time"}},
                    },
                ],
            },
        ],
    },
}


def test_worked_config_round_trips() -> None:
    """The doc's worked config parses to a fully typed ExportConfig."""
    config = ExportConfig.model_validate(WORKED_CONFIG)
    assert config.mode == "dimensional"
    assert config.dimensional is not None
    dim = config.dimensional
    assert dim.exclude is not None
    assert dim.exclude.kinds == ["scheduler", "clock"]
    assert len(dim.tables) == 6

    # dim_patient: SCD-2, records grain, 8 columns
    dim_patient = dim.tables[0]
    assert dim_patient.name == "dim_patient"
    assert dim_patient.role == "dim"
    assert dim_patient.scd == "type2"
    assert dim_patient.source.grain == "records"
    assert dim_patient.source.kind == "actor"
    assert len(dim_patient.columns) == 8
    assert dim_patient.key == ["id", "valid_from"]

    # null column
    null_col = next(c for c in dim_patient.columns if c.name == "total_spell_tariff")
    assert null_col.null is True

    # scd_window derived column
    valid_from_col = next(c for c in dim_patient.columns if c.name == "valid_from")
    assert valid_from_col.derived is not None
    assert valid_from_col.derived.scd_window == "valid_from"

    # dim_consultant: filter on records grain
    dim_consultant = dim.tables[1]
    assert dim_consultant.source.filter == {"prop__entity_type": "consultant"}
    assert dim_consultant.scd == "type1"

    # fact_ed_arrival: fk reference and membership, correlation, derived ordinal + timestamp
    fact_ed = dim.tables[2]
    assert fact_ed.role == "fact"
    assert fact_ed.scd is None
    patient_fk = next(c for c in fact_ed.columns if c.name == "patient_id")
    assert patient_fk.fk is not None
    assert patient_fk.fk.via == "reference"
    consultant_fk = next(c for c in fact_ed.columns if c.name == "consultant_id")
    assert consultant_fk.fk is not None
    assert consultant_fk.fk.via == "membership"
    assert consultant_fk.fk.where == {"elem__role_name": "surgeon"}
    corr_col = next(c for c in fact_ed.columns if c.name == "attendance_id")
    assert corr_col.correlation == "prop__journey_instance"

    # fact_medication_administered: membership grain with where
    fact_med = dim.tables[3]
    assert fact_med.source.grain == "membership"
    assert fact_med.source.where == {"elem__role_name": "admitted_meds"}
    assert fact_med.source.property == "bindings"

    # fact_fft_response: history_point grain, value_map derived
    fact_fft = dim.tables[4]
    assert fact_fft.source.grain == "history_point"
    rec_score = next(c for c in fact_fft.columns if c.name == "recommendation_score")
    assert rec_score.derived is not None
    assert rec_score.derived.value_map is not None
    assert rec_score.derived.value_map.from_ == "value"
    assert rec_score.derived.value_map.map["very_poor"] == 1
    assert rec_score.derived.value_map.map["very_good"] == 5

    # fact_journey_states: history_interval grain, lead_sim_time
    fact_js = dim.tables[5]
    assert fact_js.source.grain == "history_interval"
    exited = next(c for c in fact_js.columns if c.name == "exited_at")
    assert exited.derived is not None
    assert exited.derived.timestamp is not None
    assert exited.derived.timestamp.source == "lead_sim_time"


# ---------------------------------------------------------------------------
# exactly_one_column_mode
# ---------------------------------------------------------------------------


def test_column_zero_modes_raises() -> None:
    """A column with no source mode raises."""
    with pytest.raises(ValidationError, match="exactly one"):
        ColumnDecl.model_validate({"name": "x"})


def test_column_two_modes_raises() -> None:
    """A column with two source modes raises."""
    with pytest.raises(ValidationError, match="exactly one"):
        ColumnDecl.model_validate({"name": "x", "from": "record_id", "null": True})


# ---------------------------------------------------------------------------
# exactly_one_derived
# ---------------------------------------------------------------------------


def test_derived_zero_fields_raises() -> None:
    """A DerivedSpec with no field set raises."""
    with pytest.raises(ValidationError, match="exactly one"):
        DerivedSpec.model_validate({})


def test_derived_two_fields_raises() -> None:
    """A DerivedSpec with two fields set raises."""
    with pytest.raises(ValidationError, match="exactly one"):
        DerivedSpec.model_validate(
            {
                "ordinal": {"partition_by": "a", "order_by": "b"},
                "timestamp": {"source": "sim_time"},
            }
        )


def test_derived_scd_window_and_timestamp_raises() -> None:
    """scd_window + timestamp combination raises."""
    with pytest.raises(ValidationError, match="exactly one"):
        DerivedSpec.model_validate(
            {"scd_window": "valid_from", "timestamp": {"source": "sim_time"}}
        )


_ELAPSED_PAYLOAD = {
    "correlate_on": "attendance_id",
    "other_where": {"state": "ed_arrival"},
    "start_source": "last_mutation_sim_time",
    "end_source": "last_mutation_sim_time",
    "unit": "minutes",
}


def test_derived_elapsed_alone_parses() -> None:
    """A DerivedSpec with only `elapsed` set parses into a typed ElapsedSpec."""
    spec = DerivedSpec.model_validate({"elapsed": _ELAPSED_PAYLOAD})
    assert spec.elapsed is not None
    assert spec.elapsed.correlate_on == "attendance_id"
    assert spec.elapsed.other_where == {"state": "ed_arrival"}
    assert spec.elapsed.start_source == "last_mutation_sim_time"
    assert spec.elapsed.end_source == "last_mutation_sim_time"
    assert spec.elapsed.unit == "minutes"
    assert spec.ordinal is None
    assert spec.value_map is None
    assert spec.timestamp is None
    assert spec.scd_window is None


def test_derived_elapsed_and_timestamp_raises() -> None:
    """elapsed + timestamp combination raises (exactly_one_derived covers
    the 'elapsed' arm)."""
    with pytest.raises(ValidationError, match="exactly one"):
        DerivedSpec.model_validate(
            {"elapsed": _ELAPSED_PAYLOAD, "timestamp": {"source": "sim_time"}}
        )


def test_derived_elapsed_and_scd_window_raises() -> None:
    """elapsed + scd_window combination raises."""
    with pytest.raises(ValidationError, match="exactly one"):
        DerivedSpec.model_validate(
            {"elapsed": _ELAPSED_PAYLOAD, "scd_window": "valid_from"}
        )


# ---------------------------------------------------------------------------
# ElapsedSpec required fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_field",
    ["correlate_on", "other_where", "start_source", "end_source", "unit"],
)
def test_elapsed_spec_missing_required_field_raises(missing_field: str) -> None:
    """Each ElapsedSpec field is required — omitting any one raises."""
    payload = {k: v for k, v in _ELAPSED_PAYLOAD.items() if k != missing_field}
    with pytest.raises(ValidationError, match=missing_field):
        ElapsedSpec.model_validate(payload)


def test_elapsed_spec_unknown_unit_raises() -> None:
    """A unit outside the Literal['minutes', 'seconds', 'hours'] raises."""
    with pytest.raises(ValidationError, match="unit"):
        ElapsedSpec.model_validate({**_ELAPSED_PAYLOAD, "unit": "days"})


def test_elapsed_spec_unknown_field_raises() -> None:
    """An unknown extra field on ElapsedSpec raises (extra='forbid')."""
    with pytest.raises(ValidationError):
        ElapsedSpec.model_validate({**_ELAPSED_PAYLOAD, "bogus": "x"})


# ---------------------------------------------------------------------------
# scd_only_on_dims
# ---------------------------------------------------------------------------


def test_scd_on_fact_raises() -> None:
    """scd on role=fact raises."""
    with pytest.raises(ValidationError, match="scd"):
        TableDecl.model_validate(
            _make_table(role="fact", scd="type1", source=MINIMAL_RECORDS_SOURCE)
        )


def test_dim_without_scd_is_allowed() -> None:
    """A dim without scd is grammar-valid (engine rule covers type2 needs)."""
    t = TableDecl.model_validate(_make_table(scd=None))
    assert t.scd is None
    assert t.role == "dim"


# ---------------------------------------------------------------------------
# source_fields_match_grain
# ---------------------------------------------------------------------------


def test_history_without_property_raises() -> None:
    """history_point without property raises."""
    with pytest.raises(ValidationError, match="property"):
        SourceDecl.model_validate({"grain": "history_point", "kind": "actor"})


def test_history_interval_without_property_raises() -> None:
    """history_interval without property raises."""
    with pytest.raises(ValidationError, match="property"):
        SourceDecl.model_validate({"grain": "history_interval", "kind": "actor"})


def test_membership_without_property_raises() -> None:
    """membership without property raises."""
    with pytest.raises(ValidationError, match="property"):
        SourceDecl.model_validate({"grain": "membership", "kind": "actor"})


def test_filter_on_non_records_raises() -> None:
    """filter on a non-records grain raises."""
    with pytest.raises(ValidationError, match="filter"):
        SourceDecl.model_validate(
            {
                "grain": "history_point",
                "kind": "actor",
                "property": "p",
                "filter": {"x": "y"},
            }
        )


def test_where_on_non_membership_raises() -> None:
    """where on a non-membership grain raises."""
    with pytest.raises(ValidationError, match="where"):
        SourceDecl.model_validate(
            {
                "grain": "history_point",
                "kind": "actor",
                "property": "p",
                "where": {"x": "y"},
            }
        )


def test_value_on_non_history_point_raises() -> None:
    """value on a non-history_point grain raises."""
    with pytest.raises(ValidationError, match="value"):
        SourceDecl.model_validate(
            {
                "grain": "history_interval",
                "kind": "actor",
                "property": "p",
                "value": "x",
            }
        )


# ---------------------------------------------------------------------------
# membership_fk_shape
# ---------------------------------------------------------------------------


def test_fk_reference_with_where_raises() -> None:
    """via=reference fk with where raises."""
    with pytest.raises(ValidationError, match="membership"):
        FkClause.model_validate(
            {"to": "dim_x", "via": "reference", "where": {"x": "y"}}
        )


def test_fk_reference_with_member_field_raises() -> None:
    """via=reference fk with member_field raises."""
    with pytest.raises(ValidationError, match="membership"):
        FkClause.model_validate(
            {"to": "dim_x", "via": "reference", "member_field": "m"}
        )


def test_fk_reference_with_property_raises() -> None:
    """via=reference fk with property raises."""
    with pytest.raises(ValidationError, match="membership"):
        FkClause.model_validate({"to": "dim_x", "via": "reference", "property": "p"})


def test_fk_membership_with_path_raises() -> None:
    """via=membership fk with path raises."""
    with pytest.raises(ValidationError, match="path"):
        FkClause.model_validate(
            {"to": "dim_x", "via": "membership", "path": ["a", "b"]}
        )


def test_fk_reference_with_path_parses() -> None:
    """via=reference fk legitimately carries a multi-hop `path` (the
    reference-edge hop chain documented on FkClause.path)."""
    fk = FkClause.model_validate(
        {
            "to": "dim_patient",
            "via": "reference",
            "path": ["prop__encounter", "prop__patient"],
        }
    )
    assert fk.via == "reference"
    assert fk.path == ["prop__encounter", "prop__patient"]
    assert fk.target_key == "record_id"


def test_fk_membership_member_path_without_as_of_raises() -> None:
    """point-in-time fk with member_path but no as_of raises."""
    with pytest.raises(ValidationError, match="member_path.*requires 'as_of'"):
        FkClause.model_validate(
            {"to": "dim_x", "via": "membership", "member_path": ["prop__a"]}
        )


def test_fk_membership_as_of_without_member_path_raises() -> None:
    """point-in-time fk with as_of but no member_path raises (clean error, not assert)."""
    with pytest.raises(ValidationError, match="as_of.*requires 'member_path'"):
        FkClause.model_validate(
            {"to": "dim_x", "via": "membership", "as_of": "last_mutation_sim_time"}
        )


# ---------------------------------------------------------------------------
# non_empty_collections
# ---------------------------------------------------------------------------


def test_empty_tables_raises() -> None:
    """Empty tables list raises."""
    with pytest.raises(ValidationError, match="tables"):
        DimensionalConfig.model_validate({"tables": []})


def test_empty_columns_raises() -> None:
    """Empty columns list raises."""
    with pytest.raises(ValidationError, match="columns"):
        TableDecl.model_validate(_make_table(columns=[]))


def test_empty_key_raises() -> None:
    """Empty key list raises."""
    with pytest.raises(ValidationError, match="key"):
        TableDecl.model_validate(_make_table(key=[]))


def test_empty_exclude_kinds_raises() -> None:
    """Empty exclude.kinds list raises."""
    with pytest.raises(ValidationError, match="kinds"):
        ExcludeDecl.model_validate({"kinds": []})


def test_empty_exclude_tables_raises() -> None:
    """Empty exclude.tables list raises."""
    with pytest.raises(ValidationError, match="tables"):
        ExcludeDecl.model_validate({"tables": []})


def test_empty_value_map_raises() -> None:
    """Empty value_map.map raises."""
    with pytest.raises(ValidationError, match="empty"):
        ValueMapSpec.model_validate({"from": "x", "map": {}})


# ---------------------------------------------------------------------------
# table_names_unique
# ---------------------------------------------------------------------------


def test_duplicate_table_names_raise() -> None:
    """Two TableDecl entries sharing a name raise, naming the duplicate."""
    with pytest.raises(ValidationError, match=r"duplicate table names.*dim_x"):
        DimensionalConfig.model_validate(
            {
                "tables": [
                    _make_table(),
                    _make_table(source=MINIMAL_HISTORY_SOURCE),
                ]
            }
        )


def test_duplicate_table_names_across_roles_raise() -> None:
    """A dim and a fact sharing a name still raise (uniqueness is name-wide)."""
    with pytest.raises(ValidationError, match=r"duplicate table names.*customer"):
        DimensionalConfig.model_validate(
            {
                "tables": [
                    _make_table(name="customer"),
                    _make_table(name="customer", role="fact", scd=None),
                ]
            }
        )


def test_distinct_table_names_are_allowed() -> None:
    """Tables with distinct names pass the uniqueness validator."""
    dim = DimensionalConfig.model_validate(
        {"tables": [_make_table(name="dim_a"), _make_table(name="dim_b")]}
    )
    assert [t.name for t in dim.tables] == ["dim_a", "dim_b"]


# ---------------------------------------------------------------------------
# column_names_unique
# ---------------------------------------------------------------------------


def test_duplicate_column_names_raise() -> None:
    """Two ColumnDecl entries sharing a name within one table raise."""
    with pytest.raises(ValidationError, match=r"duplicate column names.*id"):
        TableDecl.model_validate(
            _make_table(
                columns=[
                    {"name": "id", "from": "record_id"},
                    {"name": "id", "from": "prop__status"},
                ]
            )
        )


def test_duplicate_column_names_error_names_the_table() -> None:
    """The duplicate-column error names the enclosing table."""
    with pytest.raises(ValidationError, match=r"table 'dim_x'"):
        TableDecl.model_validate(
            _make_table(
                columns=[
                    {"name": "status", "from": "prop__status"},
                    {"name": "status", "null": True},
                ]
            )
        )


def test_distinct_column_names_are_allowed() -> None:
    """Columns with distinct names pass the uniqueness validator."""
    t = TableDecl.model_validate(
        _make_table(
            columns=[
                {"name": "id", "from": "record_id"},
                {"name": "status", "from": "prop__status"},
            ]
        )
    )
    assert [c.name for c in t.columns] == ["id", "status"]


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


def test_from_alias_on_column_decl() -> None:
    """from: alias loads into from_ field."""
    col = ColumnDecl.model_validate({"name": "id", "from": "record_id"})
    assert col.from_ == "record_id"


def test_from_alias_on_value_map_spec() -> None:
    """value_map.from: alias loads into from_ field."""
    vm = ValueMapSpec.model_validate({"from": "value", "map": {"a": 1}})
    assert vm.from_ == "value"


# ---------------------------------------------------------------------------
# RebaseConfig
# ---------------------------------------------------------------------------


def test_rebase_config_with_base_date_only() -> None:
    """RebaseConfig with base_date only (no timezone) is valid."""
    rc = RebaseConfig.model_validate({"base_date": "2026-01-01T00:00:00"})
    assert rc.base_date is not None
    assert rc.timezone is None


def test_rebase_config_with_timezone_only() -> None:
    """RebaseConfig with timezone only (no base_date) is valid."""
    rc = RebaseConfig.model_validate({"timezone": "America/New_York"})
    assert rc.timezone == "America/New_York"
    assert rc.base_date is None


def test_rebase_config_with_both_knobs() -> None:
    """RebaseConfig with both knobs is valid."""
    rc = RebaseConfig.model_validate(
        {"base_date": "2026-01-01T00:00:00", "timezone": "UTC"}
    )
    assert rc.base_date is not None
    assert rc.timezone == "UTC"


def test_rebase_config_empty_block_rejected() -> None:
    """rebase: {} (both absent) is rejected by at_least_one_knob."""
    with pytest.raises(ValidationError, match="at least one"):
        RebaseConfig.model_validate({})


def test_export_config_without_rebase_loads_cleanly() -> None:
    """ExportConfig without rebase block loads cleanly (identity default)."""
    config = ExportConfig.model_validate(
        {
            "mode": "dimensional",
            "dimensional": {
                "tables": [
                    {
                        "name": "dim_x",
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
    assert config.rebase is None


def test_export_config_with_rebase_loads_cleanly() -> None:
    """ExportConfig with a valid rebase block loads cleanly."""
    config = ExportConfig.model_validate(
        {
            "mode": "dimensional",
            "rebase": {"timezone": "UTC"},
            "dimensional": {
                "tables": [
                    {
                        "name": "dim_x",
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
    assert config.rebase is not None
    assert config.rebase.timezone == "UTC"


# ---------------------------------------------------------------------------
# IncrementalConfig validation
# ---------------------------------------------------------------------------

_MINIMAL_DIMENSIONAL = {
    "tables": [
        {
            "name": "dim_x",
            "role": "dim",
            "scd": "type1",
            "source": {"grain": "records", "kind": "actor"},
            "key": ["id"],
            "columns": [{"name": "id", "from": "record_id"}],
        }
    ]
}


def test_incremental_config_period_only_valid() -> None:
    """period alone (no sim_period_ns) is valid."""
    cfg = IncrementalConfig.model_validate({"period": "day"})
    assert cfg.period == "day"
    assert cfg.sim_period_ns is None


def test_incremental_config_sim_period_ns_only_valid() -> None:
    """sim_period_ns alone (no period) is valid."""
    cfg = IncrementalConfig.model_validate({"sim_period_ns": 86_400_000_000_000})
    assert cfg.period is None
    assert cfg.sim_period_ns == 86_400_000_000_000


def test_incremental_config_both_fields_raises() -> None:
    """Both period and sim_period_ns → validation error."""
    with pytest.raises(ValidationError):
        IncrementalConfig.model_validate({"period": "day", "sim_period_ns": 1_000_000})


def test_incremental_config_neither_field_raises() -> None:
    """Neither period nor sim_period_ns → validation error."""
    with pytest.raises(ValidationError):
        IncrementalConfig.model_validate({})


def test_incremental_config_sim_period_ns_zero_raises() -> None:
    """sim_period_ns == 0 → validation error."""
    with pytest.raises(ValidationError):
        IncrementalConfig.model_validate({"sim_period_ns": 0})


def test_incremental_config_sim_period_ns_negative_raises() -> None:
    """sim_period_ns < 0 → validation error."""
    with pytest.raises(ValidationError):
        IncrementalConfig.model_validate({"sim_period_ns": -1})


def test_incremental_config_unknown_field_raises() -> None:
    """Unknown field on StrictBaseModel → validation error."""
    with pytest.raises(ValidationError):
        IncrementalConfig.model_validate({"period": "day", "unknown_field": "x"})


def test_incremental_config_week_month_valid() -> None:
    """'week' and 'month' are valid period values."""
    for period in ("week", "month"):
        cfg = IncrementalConfig.model_validate({"period": period})
        assert cfg.period == period


def test_export_config_accepts_incremental_block() -> None:
    """ExportConfig accepts an incremental block."""
    cfg = ExportConfig.model_validate(
        {
            "mode": "dimensional",
            "incremental": {"period": "day"},
            "dimensional": _MINIMAL_DIMENSIONAL,
        }
    )
    assert cfg.incremental is not None
    assert cfg.incremental.period == "day"


def test_export_config_accepts_no_incremental_block() -> None:
    """ExportConfig without incremental is valid (incremental is optional)."""
    cfg = ExportConfig.model_validate(
        {
            "mode": "dimensional",
            "dimensional": _MINIMAL_DIMENSIONAL,
        }
    )
    assert cfg.incremental is None


# ---------------------------------------------------------------------------
# ExportConfig — mode_section_matches
# ---------------------------------------------------------------------------


def test_export_config_mode_dimensional_missing_section_raises() -> None:
    """mode='dimensional' without dimensional section raises."""
    with pytest.raises(ValidationError, match="requires a 'dimensional' section"):
        ExportConfig.model_validate({"mode": "dimensional"})


def test_export_config_mode_source_with_dimensional_section_raises() -> None:
    """mode='source' with a dimensional section present raises (two-sided)."""
    with pytest.raises(ValidationError, match="forbids a 'dimensional' section"):
        ExportConfig.model_validate(
            {"mode": "source", "dimensional": _MINIMAL_DIMENSIONAL}
        )


def test_export_config_mode_dimensional_with_source_section_raises() -> None:
    """mode='dimensional' with a source section present raises (two-sided)."""
    with pytest.raises(ValidationError, match="forbids a 'source' section"):
        ExportConfig.model_validate(
            {
                "mode": "dimensional",
                "dimensional": _MINIMAL_DIMENSIONAL,
                "source": {"exclude": {"kinds": ["scheduler"]}},
            }
        )


def test_export_config_mode_source_with_no_section_is_ok() -> None:
    """mode='source' with no source section at all is valid (unlike dimensional,
    the source section is pure escape hatches — never required)."""
    config = ExportConfig.model_validate({"mode": "source"})
    assert config.mode == "source"
    assert config.source is None


def test_export_config_cdc_block_rejected_as_unknown_field() -> None:
    """A 'cdc:' block on a dimensional config is rejected as an unknown field."""
    with pytest.raises(ValidationError):
        ExportConfig.model_validate(
            {
                "mode": "dimensional",
                "dimensional": _MINIMAL_DIMENSIONAL,
                "cdc": {"table": "change_events"},
            }
        )


def test_export_config_unknown_mode_raises() -> None:
    """Unknown mode string raises."""
    with pytest.raises(ValidationError):
        ExportConfig.model_validate(
            {"mode": "streaming", "dimensional": _MINIMAL_DIMENSIONAL}
        )


def test_incremental_with_mode_dimensional_valid() -> None:
    """incremental block with mode='dimensional' is valid."""
    cfg = ExportConfig.model_validate(
        {
            "mode": "dimensional",
            "incremental": {"period": "day"},
            "dimensional": _MINIMAL_DIMENSIONAL,
        }
    )
    assert cfg.incremental is not None


# ---------------------------------------------------------------------------
# SQL-identifier validation (names spliced into SQL and output filenames)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "../../etc/cron.d/evil",
        "/etc/cron.d/evil",
        "orders\" ; ATTACH '/tmp/x.db' AS x; --",
        "1_starts_with_digit",
        "has space",
        "has-dash",
    ],
)
def test_table_name_not_sql_identifier_raises(bad_name: str) -> None:
    """A TableDecl.name outside ^[A-Za-z_][A-Za-z0-9_]*$ is a load-time error."""
    with pytest.raises(ValidationError, match="SQL identifier"):
        TableDecl.model_validate(_make_table(name=bad_name))


@pytest.mark.parametrize(
    "bad_name",
    ['na"me', "col name", "../col", "9lives"],
)
def test_column_name_not_sql_identifier_raises(bad_name: str) -> None:
    """A ColumnDecl.name outside ^[A-Za-z_][A-Za-z0-9_]*$ is a load-time error."""
    with pytest.raises(ValidationError, match="SQL identifier"):
        ColumnDecl.model_validate({"name": bad_name, "from": "record_id"})


def test_plain_identifier_table_and_column_names_pass() -> None:
    """Ordinary snake_case names (leading underscore included) still parse."""
    t = TableDecl.model_validate(
        _make_table(
            name="_dim_customer_2",
            columns=[{"name": "Id_2", "from": "record_id"}],
            key=["Id_2"],
        )
    )
    assert t.name == "_dim_customer_2"
    assert t.columns[0].name == "Id_2"
