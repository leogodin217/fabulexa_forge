"""Tests for config model parse-time validators.

Each test asserts on model behavior (structural constraints), not that Pydantic
parses successfully — the invariants are tested, not the library.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from fabulexa_forge.config.models import (
    ColumnDecl,
    DateParseSpec,
    DerivedSpec,
    DimensionalConfig,
    ElapsedSpec,
    ExcludeDecl,
    ExportConfig,
    FkClause,
    IncrementalConfig,
    RebaseConfig,
    ScdWindowSpec,
    SourceDecl,
    SourceTableDecl,
    StrictBaseModel,
    TableDecl,
    TimestampSpec,
    ValueMapSpec,
)

if TYPE_CHECKING:
    from collections.abc import Callable

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


_ELAPSED_BASE_PAYLOAD = {
    "correlate_on": "attendance_id",
    "other_where": {"state": "ed_arrival"},
    "start_source": "last_mutation_sim_time",
    "end_source": "last_mutation_sim_time",
}
_ELAPSED_PAYLOAD = {**_ELAPSED_BASE_PAYLOAD, "unit": "minutes"}


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


def test_derived_date_parse_alone_parses() -> None:
    """A DerivedSpec with only `date_parse` set parses into a typed DateParseSpec."""
    spec = DerivedSpec.model_validate(
        {"date_parse": {"from": "prop__dob", "format": "%Y-%m-%d"}}
    )
    assert spec.date_parse is not None
    assert spec.date_parse.from_ == "prop__dob"
    assert spec.date_parse.format == "%Y-%m-%d"
    assert spec.elapsed is None
    assert spec.timestamp is None


def test_derived_date_parse_and_timestamp_raises() -> None:
    """date_parse + timestamp combination raises."""
    with pytest.raises(ValidationError, match="exactly one"):
        DerivedSpec.model_validate(
            {
                "date_parse": {"from": "prop__dob", "format": "%Y-%m-%d"},
                "timestamp": {"source": "sim_time"},
            }
        )


# ---------------------------------------------------------------------------
# ElapsedSpec required fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_field",
    ["correlate_on", "other_where", "start_source", "end_source"],
)
def test_elapsed_spec_missing_required_field_raises(missing_field: str) -> None:
    """Each unconditionally-required ElapsedSpec field raises when omitted.

    `unit` is not in this list — it is conditionally required by
    `exactly_one_rendering`, covered separately below."""
    payload = {k: v for k, v in _ELAPSED_PAYLOAD.items() if k != missing_field}
    with pytest.raises(ValidationError, match=missing_field):
        ElapsedSpec.model_validate(payload)


# ---------------------------------------------------------------------------
# exactly_one_rendering (ElapsedSpec)
# ---------------------------------------------------------------------------


def test_elapsed_spec_unit_alone_parses() -> None:
    """`unit` alone (no `as`) parses — the numeric rendering election."""
    spec = ElapsedSpec.model_validate({**_ELAPSED_BASE_PAYLOAD, "unit": "minutes"})
    assert spec.unit == "minutes"
    assert spec.as_ is None


def test_elapsed_spec_as_interval_alone_parses() -> None:
    """`as: interval` alone (no `unit`) parses — the typed rendering election."""
    spec = ElapsedSpec.model_validate({**_ELAPSED_BASE_PAYLOAD, "as": "interval"})
    assert spec.as_ == "interval"
    assert spec.unit is None


def test_elapsed_spec_both_unit_and_as_raises() -> None:
    """Setting both `unit` and `as` contradicts (exactly_one_rendering)."""
    with pytest.raises(ValidationError, match="exactly one"):
        ElapsedSpec.model_validate(
            {**_ELAPSED_BASE_PAYLOAD, "unit": "minutes", "as": "interval"}
        )


def test_elapsed_spec_neither_unit_nor_as_raises() -> None:
    """Omitting both `unit` and `as` is an error — no default rendering is
    invented."""
    with pytest.raises(ValidationError, match="exactly one"):
        ElapsedSpec.model_validate(_ELAPSED_BASE_PAYLOAD)


def test_elapsed_spec_unknown_unit_raises() -> None:
    """A unit outside the Literal['minutes', 'seconds', 'hours'] raises."""
    with pytest.raises(ValidationError, match="unit"):
        ElapsedSpec.model_validate({**_ELAPSED_PAYLOAD, "unit": "days"})


def test_elapsed_spec_unknown_field_raises() -> None:
    """An unknown extra field on ElapsedSpec raises (extra='forbid')."""
    with pytest.raises(ValidationError):
        ElapsedSpec.model_validate({**_ELAPSED_PAYLOAD, "bogus": "x"})


def test_elapsed_other_where_empty_mapping_rejected() -> None:
    """`other_where: {}` is rejected at parse time.

    The grammar's sole *required* predicate mapping: an empty mapping renders
    no condition at all, a degenerate correlation the elapsed subquery cannot
    express (Breaking Changes).
    """
    with pytest.raises(ValidationError, match="other_where must name at least one"):
        ElapsedSpec.model_validate({**_ELAPSED_PAYLOAD, "other_where": {}})


def test_elapsed_other_where_one_entry_accepted() -> None:
    """`other_where` with exactly one predicate entry is accepted."""
    spec = ElapsedSpec.model_validate(
        {**_ELAPSED_PAYLOAD, "other_where": {"state": "ed_arrival"}}
    )
    assert spec.other_where == {"state": "ed_arrival"}


# ---------------------------------------------------------------------------
# TimestampSpec — `as` absence detection
# ---------------------------------------------------------------------------


def test_timestamp_spec_as_absent_is_none() -> None:
    """`as` absent parses as_ = None (mode-definitional default rendering)."""
    spec = TimestampSpec.model_validate({"source": "sim_time"})
    assert spec.as_ is None


@pytest.mark.parametrize("render", ["timestamp", "date", "time", "timestamptz"])
def test_timestamp_spec_as_value_parses(render: str) -> None:
    """Each of the four TemporalRender values parses as an explicit election."""
    spec = TimestampSpec.model_validate({"source": "sim_time", "as": render})
    assert spec.as_ == render


def test_timestamp_spec_unknown_as_value_raises() -> None:
    """An `as` value outside the TemporalRender literal is refused."""
    with pytest.raises(ValidationError):
        TimestampSpec.model_validate({"source": "sim_time", "as": "epoch"})


# ---------------------------------------------------------------------------
# ScdWindowSpec — object form requires both bound and as
# ---------------------------------------------------------------------------


def test_scd_window_spec_object_form_requires_bound_and_as() -> None:
    """The object form with both `bound` and `as` set parses."""
    spec = ScdWindowSpec.model_validate({"bound": "valid_from", "as": "date"})
    assert spec.bound == "valid_from"
    assert spec.as_ == "date"


def test_scd_window_spec_missing_as_raises() -> None:
    """A bound-only object (missing `as`) is refused."""
    with pytest.raises(ValidationError, match="as"):
        ScdWindowSpec.model_validate({"bound": "valid_from"})


def test_scd_window_spec_missing_bound_raises() -> None:
    """An `as`-only object (missing `bound`) is refused."""
    with pytest.raises(ValidationError, match="bound"):
        ScdWindowSpec.model_validate({"as": "date"})


def test_derived_scd_window_bare_literal_parses() -> None:
    """The bare-literal shorthand `scd_window: valid_from` still parses,
    with no election (the no-election form)."""
    spec = DerivedSpec.model_validate({"scd_window": "valid_from"})
    assert spec.scd_window == "valid_from"


def test_derived_scd_window_object_form_parses() -> None:
    """The object form carries a typed ScdWindowSpec with its election."""
    spec = DerivedSpec.model_validate(
        {"scd_window": {"bound": "valid_to", "as": "timestamptz"}}
    )
    assert isinstance(spec.scd_window, ScdWindowSpec)
    assert spec.scd_window.bound == "valid_to"
    assert spec.scd_window.as_ == "timestamptz"


# ---------------------------------------------------------------------------
# DateParseSpec — format_denotes_a_temporal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["%Y-%m-%d", "%d %B %Y", "%Y%%-%m-%d"])
def test_date_parse_spec_valid_formats_parse(fmt: str) -> None:
    """A format carrying year, month, and day directives parses.

    Includes a `%%` literal directive, which the closed directive set
    (spec Contracts) explicitly allows."""
    spec = DateParseSpec.model_validate({"from": "prop__dob", "format": fmt})
    assert spec.format == fmt


@pytest.mark.parametrize(
    "fmt",
    [
        "%Y-%m-%d %H:%M:%S",
        "%H:%M",
        "%I:%M %p",
        "%H:%M:%S.%f",
        "%H:%M:%S.%g",
    ],
)
def test_date_parse_spec_family_formats_parse(fmt: str) -> None:
    """The instant-string family widening: a date+time format, a 24-hour
    time-only format, a 12-hour time-only format with its AM/PM marker, and
    sub-second fraction formats (`%f` microseconds, `%g` milliseconds) all
    parse."""
    spec = DateParseSpec.model_validate({"from": "prop__dob", "format": fmt})
    assert spec.format == fmt


def test_date_parse_spec_missing_year_directive_raises() -> None:
    """A format with no year directive is refused."""
    with pytest.raises(ValidationError, match="year"):
        DateParseSpec.model_validate({"from": "prop__dob", "format": "%m-%d"})


def test_date_parse_spec_missing_month_directive_raises() -> None:
    """A format with no month directive is refused."""
    with pytest.raises(ValidationError, match="month"):
        DateParseSpec.model_validate({"from": "prop__dob", "format": "%Y-%d"})


def test_date_parse_spec_missing_day_directive_raises() -> None:
    """A format with no day directive is refused."""
    with pytest.raises(ValidationError, match="day"):
        DateParseSpec.model_validate({"from": "prop__dob", "format": "%Y-%m"})


@pytest.mark.parametrize("directive", ["%x", "%A", "%z", "%Z"])
def test_date_parse_spec_locale_zone_directive_still_refused(directive: str) -> None:
    """A locale (`%x`, `%A`) or zone (`%z`, `%Z`) directive stays outside the
    closed set and is refused — the family widening adds time-of-day
    directives only (zone directives and non-VARCHAR sources are doc-pinned
    non-goals)."""
    with pytest.raises(ValidationError, match="unsupported"):
        DateParseSpec.model_validate(
            {"from": "prop__dob", "format": f"%Y-%m-%d {directive}"}
        )


@pytest.mark.parametrize(
    "fmt,match",
    [
        ("%I:%M", "%I and %p"),
        ("%Y-%m-%d %p", "%I and %p"),
        ("%Y-%m-%d %M", "hour"),
        ("%H:%S", r"%S requires %M"),
        ("%H:%M.%f", r"require %S"),
    ],
)
def test_date_parse_spec_pairing_refusals(fmt: str, match: str) -> None:
    """Each pairing rule is refused, naming the rule: an orphaned `%I` or
    `%p`, `%M` with no hour directive, `%S` with no `%M`, `%f`/`%g` with no
    `%S`. `%H`/`%M`/`%S` are now in the closed directive set — these
    formerly "unsupported directive" cases are refused by pairing instead."""
    with pytest.raises(ValidationError, match=match):
        DateParseSpec.model_validate({"from": "prop__dob", "format": fmt})


@pytest.mark.parametrize(
    "fmt,match",
    [
        ("%Y-%m-%d %Y", "year"),
        ("%Y %y %m %d", "year"),
        ("%H %I %p", "hour"),
        ("%H:%M:%S.%f%g", "sub-second fraction"),
    ],
)
def test_date_parse_spec_uniqueness_refusals(fmt: str, match: str) -> None:
    """Each uniqueness rule is refused: a repeated directive, or two
    alternative forms of one temporal field (year, hour, or sub-second
    fraction)."""
    with pytest.raises(ValidationError, match=match):
        DateParseSpec.model_validate({"from": "prop__dob", "format": fmt})


@pytest.mark.parametrize(
    "fmt,match",
    [
        ("%m-%d %H:%M", "year"),
        ("%M:%S", "hour"),
    ],
)
def test_date_parse_spec_completeness_refusals(fmt: str, match: str) -> None:
    """A partial calendar date combined with a complete time is still
    refused (`%Y-%m` alone stays covered by the missing-directive tests
    above); a minute/second pair with no hour directive is caught by the
    `%M` pairing rule."""
    with pytest.raises(ValidationError, match=match):
        DateParseSpec.model_validate({"from": "prop__dob", "format": fmt})


def test_date_parse_spec_empty_from_raises() -> None:
    """An empty `from` is refused."""
    with pytest.raises(ValidationError, match="from"):
        DateParseSpec.model_validate({"from": "", "format": "%Y-%m-%d"})


def test_date_parse_spec_empty_format_raises() -> None:
    """An empty `format` is refused."""
    with pytest.raises(ValidationError, match="non-empty"):
        DateParseSpec.model_validate({"from": "prop__dob", "format": ""})


# ---------------------------------------------------------------------------
# _require_date_parse_map_valid — map-form attach points (SourceTableDecl)
# ---------------------------------------------------------------------------


def test_source_table_decl_date_parse_map_timestamp_format_accepted() -> None:
    """A `date_parse` map entry with a TIMESTAMP-denoting format is accepted."""
    decl = SourceTableDecl.model_validate(
        {
            "name": "visits",
            "kind": "actor",
            "date_parse": {"prop__dob": "%Y-%m-%d %H:%M:%S"},
        }
    )
    assert decl.date_parse == {"prop__dob": "%Y-%m-%d %H:%M:%S"}


def test_source_table_decl_date_parse_map_family_violation_refused() -> None:
    """A map entry violating a family rule is refused, naming the
    entry-keyed field name and the violated rule."""
    with pytest.raises(ValidationError) as excinfo:
        SourceTableDecl.model_validate(
            {
                "name": "visits",
                "kind": "actor",
                "date_parse": {"prop__dob": "%H:%S"},
            }
        )
    message = str(excinfo.value)
    assert "SourceTableDecl.date_parse['prop__dob']" in message
    assert "%S requires %M" in message


# ---------------------------------------------------------------------------
# PredicateValue — scalar-or-list well-formedness across the five surfaces
#
# `filter` / `where` / `value` (SourceDecl), `where` (FkClause), and
# `other_where` (ElapsedSpec) all carry PredicateValue and therefore the one
# shared rule (§ Config Models). Each parametrized case supplies a builder
# that embeds a predicate value into a grain-appropriate payload for its
# model, an accessor reading the parsed value back out, and the dotted field
# path the malformed-value error must name.
# ---------------------------------------------------------------------------


def _source_filter_payload(value: object) -> dict[str, object]:
    return {"grain": "records", "kind": "actor", "filter": {"prop__x": value}}


def _source_where_payload(value: object) -> dict[str, object]:
    return {
        "grain": "membership",
        "kind": "actor",
        "property": "roles",
        "where": {"elem__x": value},
    }


def _source_value_payload(value: object) -> dict[str, object]:
    return {
        "grain": "history_point",
        "kind": "actor",
        "property": "status",
        "value": value,
    }


def _fk_where_payload(value: object) -> dict[str, object]:
    return {"to": "dim_x", "via": "membership", "where": {"elem__x": value}}


def _elapsed_other_where_payload(value: object) -> dict[str, object]:
    return {**_ELAPSED_PAYLOAD, "other_where": {"state": value}}


_PREDICATE_SURFACES = [
    pytest.param(
        SourceDecl,
        _source_filter_payload,
        lambda m: m.filter["prop__x"],
        "filter.prop__x",
        id="source_filter",
    ),
    pytest.param(
        SourceDecl,
        _source_where_payload,
        lambda m: m.where["elem__x"],
        "where.elem__x",
        id="source_where",
    ),
    pytest.param(
        SourceDecl,
        _source_value_payload,
        lambda m: m.value,
        "value",
        id="source_value",
    ),
    pytest.param(
        FkClause,
        _fk_where_payload,
        lambda m: m.where["elem__x"],
        "where.elem__x",
        id="fk_where",
    ),
    pytest.param(
        ElapsedSpec,
        _elapsed_other_where_payload,
        lambda m: m.other_where["state"],
        "other_where.state",
        id="elapsed_other_where",
    ),
]


@pytest.mark.parametrize(
    "model_cls,payload_fn,accessor,field_path", _PREDICATE_SURFACES
)
def test_predicate_value_scalar_accepted_on_all_five_surfaces(
    model_cls: type[StrictBaseModel],
    payload_fn: "Callable[[object], dict[str, object]]",
    accessor: "Callable[[object], object]",
    field_path: str,
) -> None:
    """A scalar predicate value is accepted on every PredicateValue surface."""
    model = model_cls.model_validate(payload_fn("a"))
    assert accessor(model) == "a"


@pytest.mark.parametrize(
    "model_cls,payload_fn,accessor,field_path", _PREDICATE_SURFACES
)
def test_predicate_value_list_accepted_on_all_five_surfaces(
    model_cls: type[StrictBaseModel],
    payload_fn: "Callable[[object], dict[str, object]]",
    accessor: "Callable[[object], object]",
    field_path: str,
) -> None:
    """A non-empty, duplicate-free list predicate value is accepted on every
    PredicateValue surface, preserving config element order."""
    model = model_cls.model_validate(payload_fn(["a", "b", "c"]))
    assert accessor(model) == ["a", "b", "c"]


@pytest.mark.parametrize(
    "model_cls,payload_fn,accessor,field_path", _PREDICATE_SURFACES
)
def test_predicate_value_empty_list_rejected_names_field_path(
    model_cls: type[StrictBaseModel],
    payload_fn: "Callable[[object], dict[str, object]]",
    accessor: "Callable[[object], object]",
    field_path: str,
) -> None:
    """An empty list is rejected at parse time, naming the offending field's
    path (an empty predicate selects nothing; omit the entry or the table)."""
    with pytest.raises(ValidationError, match=field_path):
        model_cls.model_validate(payload_fn([]))


@pytest.mark.parametrize(
    "model_cls,payload_fn,accessor,field_path", _PREDICATE_SURFACES
)
def test_predicate_value_duplicate_element_rejected_names_element(
    model_cls: type[StrictBaseModel],
    payload_fn: "Callable[[object], dict[str, object]]",
    accessor: "Callable[[object], object]",
    field_path: str,
) -> None:
    """A list carrying a repeated element is rejected at parse time, naming
    both the field's path and the repeated element rather than silently
    deduplicating."""
    with pytest.raises(ValidationError, match=rf"{field_path}[\s\S]*'a'"):
        model_cls.model_validate(payload_fn(["a", "b", "a"]))


def test_predicate_value_per_entry_rule_reports_offending_entry_empty() -> None:
    """An empty-list entry among sibling valid entries in a `filter` mapping
    reports only that entry's field path; the well-formed sibling is
    unaffected — the rule rides the type and applies per dict entry."""
    with pytest.raises(ValidationError, match=r"filter\.prop__bad") as exc_info:
        SourceDecl.model_validate(
            {
                "grain": "records",
                "kind": "actor",
                "filter": {"prop__good": "x", "prop__bad": []},
            }
        )
    assert "prop__good" not in str(exc_info.value)


def test_predicate_value_per_entry_rule_reports_offending_entry_duplicate() -> None:
    """A duplicate-bearing entry among sibling valid entries in a `where`
    mapping reports only that entry's field path."""
    with pytest.raises(ValidationError, match=r"where\.elem__bad") as exc_info:
        SourceDecl.model_validate(
            {
                "grain": "membership",
                "kind": "actor",
                "property": "roles",
                "where": {"elem__good": ["x", "y"], "elem__bad": ["p", "p"]},
            }
        )
    assert "elem__good" not in str(exc_info.value)


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
# membership_grain_fk_where_refused
# ---------------------------------------------------------------------------


def test_membership_grain_fk_where_raises() -> None:
    """fk.where on a membership-grain table's plain membership fk raises,
    naming source.where as the live surface."""
    with pytest.raises(ValidationError, match=r"fk\.where.*source\.where") as exc_info:
        TableDecl.model_validate(
            _make_table(
                source=MEMBERSHIP_SOURCE,
                columns=[
                    MINIMAL_COLUMN,
                    {
                        "name": "actor_id",
                        "fk": {
                            "to": "dim_actor",
                            "via": "membership",
                            "where": {"elem__role": "consultant"},
                        },
                    },
                ],
            )
        )
    assert "actor_id" in str(exc_info.value)


def test_membership_grain_fk_without_where_is_allowed() -> None:
    """The plain membership fk itself stays legal on a membership grain."""
    t = TableDecl.model_validate(
        _make_table(
            source=MEMBERSHIP_SOURCE,
            columns=[
                MINIMAL_COLUMN,
                {"name": "actor_id", "fk": {"to": "dim_actor", "via": "membership"}},
            ],
        )
    )
    assert t.columns[1].fk is not None


def test_records_grain_fk_where_is_allowed() -> None:
    """fk.where stays legal on a records grain — the joined-edge path renders it."""
    t = TableDecl.model_validate(
        _make_table(
            columns=[
                MINIMAL_COLUMN,
                {
                    "name": "actor_id",
                    "fk": {
                        "to": "dim_actor",
                        "via": "membership",
                        "where": {"elem__role": "consultant"},
                    },
                },
            ],
        )
    )
    assert t.columns[1].fk is not None


def test_membership_grain_point_in_time_fk_where_is_allowed() -> None:
    """fk.where stays legal on a membership grain's point-in-time (as_of) fk —
    that form correlates its own membership subquery and renders it."""
    t = TableDecl.model_validate(
        _make_table(
            source=MEMBERSHIP_SOURCE,
            columns=[
                MINIMAL_COLUMN,
                {
                    "name": "owner_id",
                    "fk": {
                        "to": "dim_owner",
                        "via": "membership",
                        "as_of": "joined_sim_time",
                        "member_path": ["prop__owner"],
                        "where": {"elem__role": "consultant"},
                    },
                },
            ],
        )
    )
    assert t.columns[1].fk is not None


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
    assert fk.target_key is None


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
            {
                "mode": "source",
                "source": {"tables": [{"name": "actors", "kind": "actor"}]},
                "dimensional": _MINIMAL_DIMENSIONAL,
            }
        )


def test_export_config_mode_dimensional_with_source_section_raises() -> None:
    """mode='dimensional' with a source section present raises (two-sided)."""
    with pytest.raises(ValidationError, match="forbids a 'source' section"):
        ExportConfig.model_validate(
            {
                "mode": "dimensional",
                "dimensional": _MINIMAL_DIMENSIONAL,
                "source": {"tables": [{"name": "actors", "kind": "actor"}]},
            }
        )


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


# ---------------------------------------------------------------------------
# ExportConfig.keys — key election (parse-time only; emit-independent)
# ---------------------------------------------------------------------------


def test_keys_absent_parses_as_none() -> None:
    """No `keys` block parses cleanly; the field is None."""
    cfg = ExportConfig.model_validate(
        {"mode": "dimensional", "dimensional": _MINIMAL_DIMENSIONAL}
    )
    assert cfg.keys is None


def test_keys_empty_map_raises() -> None:
    """`keys: {}` (present but empty) is rejected."""
    with pytest.raises(ValidationError, match="must not be empty"):
        ExportConfig.model_validate(
            {
                "mode": "dimensional",
                "dimensional": _MINIMAL_DIMENSIONAL,
                "keys": {},
            }
        )


@pytest.mark.parametrize("surface", ["record_id", "record_index", "presentation_id"])
def test_keys_scalar_election_parses_for_each_surface(surface: str) -> None:
    """A scalar election parses for each of the three surfaces."""
    cfg = ExportConfig.model_validate(
        {
            "mode": "dimensional",
            "dimensional": _MINIMAL_DIMENSIONAL,
            "keys": {"entity": surface},
        }
    )
    assert cfg.keys == {"entity": surface}


def test_keys_per_sub_type_map_parses() -> None:
    """A per-sub-type map elects independently per sub-type."""
    cfg = ExportConfig.model_validate(
        {
            "mode": "dimensional",
            "dimensional": _MINIMAL_DIMENSIONAL,
            "keys": {"entity": {"alpha": "presentation_id", "beta": "record_index"}},
        }
    )
    assert cfg.keys == {"entity": {"alpha": "presentation_id", "beta": "record_index"}}


def test_keys_empty_per_kind_map_raises() -> None:
    """`keys: {entity: {}}` (empty per-kind map) is rejected."""
    with pytest.raises(ValidationError, match="per-sub-type map must not be empty"):
        ExportConfig.model_validate(
            {
                "mode": "dimensional",
                "dimensional": _MINIMAL_DIMENSIONAL,
                "keys": {"entity": {}},
            }
        )


@pytest.mark.parametrize(
    "bad_election",
    ["uuid", {"alpha": "uuid"}],
)
def test_keys_non_surface_value_raises(bad_election: object) -> None:
    """A scalar or map election value outside the KeySurface literal is refused."""
    with pytest.raises(ValidationError):
        ExportConfig.model_validate(
            {
                "mode": "dimensional",
                "dimensional": _MINIMAL_DIMENSIONAL,
                "keys": {"entity": bad_election},
            }
        )


def test_keys_kind_existence_not_checked_at_parse_time() -> None:
    """`keys` accepts a kind name Pydantic can't check against any emit — kind/
    sub-type existence is an export-time gate, not a parse-time error
    (emit-independence)."""
    cfg = ExportConfig.model_validate(
        {
            "mode": "dimensional",
            "dimensional": _MINIMAL_DIMENSIONAL,
            "keys": {"no_such_kind": "presentation_id"},
        }
    )
    assert cfg.keys == {"no_such_kind": "presentation_id"}


# ---------------------------------------------------------------------------
# FkClause.target_key — widened surface, inheritance default
# ---------------------------------------------------------------------------


def test_fk_target_key_absent_is_none() -> None:
    """`target_key` absent parses as None (inherit), not 'record_id'."""
    fk = FkClause.model_validate({"to": "dim_x", "via": "reference"})
    assert fk.target_key is None


def test_fk_target_key_record_index_parses() -> None:
    """`target_key: record_index` parses."""
    fk = FkClause.model_validate(
        {"to": "dim_x", "via": "reference", "target_key": "record_index"}
    )
    assert fk.target_key == "record_index"


def test_fk_target_key_invalid_literal_raises() -> None:
    """An invalid `target_key` literal is refused."""
    with pytest.raises(ValidationError):
        FkClause.model_validate(
            {"to": "dim_x", "via": "reference", "target_key": "uuid"}
        )
