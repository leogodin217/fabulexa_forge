"""Tests for the corrupter config models and load_corrupt_config.

Each test asserts on model behavior (structural constraints), not that
Pydantic parses successfully — the invariants are tested, not the library.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from fabulexa_forge.config.loader import load_corrupt_config
from fabulexa_forge.config.models import (
    Amount,
    ClusteredTemporal,
    Correlated,
    CorruptConfig,
    DangleReference,
    DeleteRows,
    DistortIntervals,
    Distribution,
    DropEvents,
    DuplicateRows,
    EntityScoped,
    FreezeSeries,
    InsertRows,
    MispointReference,
    MutateCells,
    MutationCase,
    MutationFormatDirt,
    MutationMojibake,
    MutationOutOfDomain,
    MutationPrecisionDrop,
    MutationResample,
    MutationScale,
    MutationSentinel,
    MutationTruncate,
    MutationTypo,
    MutationWhitespace,
    NullCells,
    SchemaDrift,
    ShiftCollide,
    ShiftOffset,
    ShiftSimTime,
    ShiftSwap,
    Target,
)
from fabulexa_forge.errors import ConfigError

# ---------------------------------------------------------------------------
# The design doc's § Configuration example
# ---------------------------------------------------------------------------

DESIGN_DOC_EXAMPLE_YAML = textwrap.dedent("""\
    seed: 42
    operations:
      - kind: null_cells
        name: null_patient_contact
        target:
          table: records__patient
          columns: [prop__email, prop__phone]
          where: { prop__active_status: admitted }
        amount: { rate: 0.05 }

      - kind: duplicate_rows
        target: { table: records__encounter, columns: [prop__duration_minutes] }
        amount: { count: 25 }
        jitter:
          shape: normal
          mean: 0.0
          stddev: 3.0

      - kind: schema_drift
        target: { table: records__patient }
        rename_to: { prop__email: prop__email_address }
        retype_to: { prop__age: VARCHAR }
        drop: [prop__middle_name]

      - kind: dangle_reference
        target:
          table: membership__patient__assigned_ward
          columns: [member__consultant__id]
        amount: { rate: 0.02 }
""")


def test_design_doc_example_parses_four_operations_in_order() -> None:
    """The § Configuration example YAML parses into a CorruptConfig with four
    operations of the right types in order."""
    config = CorruptConfig.model_validate(yaml.safe_load(DESIGN_DOC_EXAMPLE_YAML))
    assert config.seed == 42
    assert len(config.operations) == 4
    assert isinstance(config.operations[0], NullCells)
    assert isinstance(config.operations[1], DuplicateRows)
    assert isinstance(config.operations[2], SchemaDrift)
    assert isinstance(config.operations[3], DangleReference)


# ---------------------------------------------------------------------------
# Amount
# ---------------------------------------------------------------------------


def test_amount_both_rate_and_count_rejected() -> None:
    """Amount with both rate and count raises."""
    with pytest.raises(ValidationError):
        Amount(rate=0.5, count=1)


def test_amount_neither_rate_nor_count_rejected() -> None:
    """Amount with neither rate nor count raises."""
    with pytest.raises(ValidationError):
        Amount()


def test_amount_rate_zero_rejected() -> None:
    """Amount with rate=0 raises (rate must be in (0, 1])."""
    with pytest.raises(ValidationError):
        Amount(rate=0)


def test_amount_rate_above_one_rejected() -> None:
    """Amount with rate=1.5 raises."""
    with pytest.raises(ValidationError):
        Amount(rate=1.5)


def test_amount_count_zero_rejected() -> None:
    """Amount with count=0 raises (count must be >= 1)."""
    with pytest.raises(ValidationError):
        Amount(count=0)


def test_amount_rate_one_is_valid() -> None:
    """Amount with rate=1.0 is valid."""
    amount = Amount(rate=1.0)
    assert amount.rate == 1.0


def test_amount_count_one_is_valid() -> None:
    """Amount with count=1 is valid."""
    amount = Amount(count=1)
    assert amount.count == 1


# ---------------------------------------------------------------------------
# Distribution
# ---------------------------------------------------------------------------


def test_distribution_uniform_with_normal_params_rejected() -> None:
    """Distribution shape='uniform' with mean/stddev set raises."""
    with pytest.raises(ValidationError):
        Distribution(shape="uniform", low=0.0, high=1.0, mean=0.0)


def test_distribution_uniform_low_greater_than_high_rejected() -> None:
    """Distribution shape='uniform' with low > high raises."""
    with pytest.raises(ValidationError):
        Distribution(shape="uniform", low=5.0, high=1.0)


def test_distribution_normal_with_uniform_params_rejected() -> None:
    """Distribution shape='normal' with low/high set raises."""
    with pytest.raises(ValidationError):
        Distribution(shape="normal", mean=0.0, stddev=1.0, low=0.0)


def test_distribution_normal_stddev_non_positive_rejected() -> None:
    """Distribution shape='normal' with stddev <= 0 raises."""
    with pytest.raises(ValidationError):
        Distribution(shape="normal", mean=0.0, stddev=0.0)


def test_distribution_uniform_valid() -> None:
    """Distribution shape='uniform' with low <= high is valid."""
    dist = Distribution(shape="uniform", low=0.0, high=1.0)
    assert dist.low == 0.0
    assert dist.high == 1.0


def test_distribution_normal_valid() -> None:
    """Distribution shape='normal' with stddev > 0 is valid."""
    dist = Distribution(shape="normal", mean=0.0, stddev=1.0)
    assert dist.mean == 0.0
    assert dist.stddev == 1.0


# ---------------------------------------------------------------------------
# DuplicateRows
# ---------------------------------------------------------------------------

_TARGET_NO_COLUMNS = {"table": "records__actor"}
_TARGET_WITH_COLUMNS = {"table": "records__actor", "columns": ["prop__name"]}
_JITTER = {"shape": "normal", "mean": 0.0, "stddev": 1.0}


def test_duplicate_rows_jitter_without_columns_rejected() -> None:
    """jitter present without target.columns raises (perturbation_governs_columns)."""
    with pytest.raises(ValidationError):
        DuplicateRows(
            kind="duplicate_rows",
            target=Target(**_TARGET_NO_COLUMNS),
            amount=Amount(count=1),
            jitter=Distribution(**_JITTER),
        )


def test_duplicate_rows_columns_without_perturbation_rejected() -> None:
    """jitter and mutation both absent with target.columns present raises
    (perturbation_governs_columns)."""
    with pytest.raises(ValidationError):
        DuplicateRows(
            kind="duplicate_rows",
            target=Target(**_TARGET_WITH_COLUMNS),
            amount=Amount(count=1),
        )


def test_duplicate_rows_exact_without_columns_valid() -> None:
    """jitter and mutation absent and target.columns absent is valid (exact
    duplicate)."""
    op = DuplicateRows(
        kind="duplicate_rows",
        target=Target(**_TARGET_NO_COLUMNS),
        amount=Amount(count=1),
    )
    assert op.jitter is None
    assert op.mutation is None


def test_duplicate_rows_near_with_columns_valid() -> None:
    """jitter present with target.columns present is valid (near duplicate)."""
    op = DuplicateRows(
        kind="duplicate_rows",
        target=Target(**_TARGET_WITH_COLUMNS),
        amount=Amount(count=1),
        jitter=Distribution(**_JITTER),
    )
    assert op.jitter is not None


def test_duplicate_rows_jitter_and_mutation_both_set_rejected() -> None:
    """jitter and mutation both set raises (at most one perturbation mode)."""
    with pytest.raises(ValidationError):
        DuplicateRows(
            kind="duplicate_rows",
            target=Target(**_TARGET_WITH_COLUMNS),
            amount=Amount(count=1),
            jitter=Distribution(**_JITTER),
            mutation=MutationTypo(kind="typo"),
        )


def test_duplicate_rows_mutation_without_columns_rejected() -> None:
    """mutation present without target.columns raises
    (perturbation_governs_columns)."""
    with pytest.raises(ValidationError):
        DuplicateRows(
            kind="duplicate_rows",
            target=Target(**_TARGET_NO_COLUMNS),
            amount=Amount(count=1),
            mutation=MutationTypo(kind="typo"),
        )


@pytest.mark.parametrize("mutation_kind", ["typo", "case"])
def test_duplicate_rows_mutation_with_columns_valid(mutation_kind: str) -> None:
    """mutation present with target.columns present parses for a
    marker-only (typo) and a parameterized (case) mutation kind."""
    op = DuplicateRows.model_validate(
        {
            "kind": "duplicate_rows",
            "target": _TARGET_WITH_COLUMNS,
            "amount": {"count": 1},
            "mutation": _MUTATION_PAYLOADS[mutation_kind],
        }
    )
    assert isinstance(op.mutation, _MUTATION_MODELS[mutation_kind])


# ---------------------------------------------------------------------------
# DeleteRows
# ---------------------------------------------------------------------------


def test_delete_rows_target_columns_rejected() -> None:
    """target.columns set on delete_rows raises (no_columns validator)."""
    with pytest.raises(ValidationError):
        DeleteRows(
            kind="delete_rows",
            target=Target(**_TARGET_WITH_COLUMNS),
            amount=Amount(count=1),
        )


def test_delete_rows_minimal_target_and_amount_parses() -> None:
    """target + amount only (no placement) is valid."""
    op = DeleteRows(
        kind="delete_rows",
        target=Target(**_TARGET_NO_COLUMNS),
        amount=Amount(rate=0.02),
    )
    assert op.target.table == "records__actor"
    assert op.placement is None


def test_delete_rows_with_placement_parses() -> None:
    """target + amount + placement is valid."""
    op = DeleteRows(
        kind="delete_rows",
        target=Target(**_TARGET_NO_COLUMNS),
        amount=Amount(count=5),
        placement=EntityScoped(kind="entity_scoped", entities=Amount(count=2)),
    )
    assert isinstance(op.placement, EntityScoped)


def test_delete_rows_unknown_field_rejected() -> None:
    """An unknown extra field on delete_rows is rejected (extra='forbid')."""
    with pytest.raises(ValidationError):
        DeleteRows.model_validate(
            {
                "kind": "delete_rows",
                "target": _TARGET_NO_COLUMNS,
                "amount": {"count": 1},
                "bogus": "x",
            }
        )


def test_delete_rows_kind_accepted_by_corrupt_operation_union() -> None:
    """The CorruptOperation union parses a delete_rows operation."""
    config = CorruptConfig.model_validate(
        {
            "seed": 1,
            "operations": [
                {
                    "kind": "delete_rows",
                    "target": _TARGET_NO_COLUMNS,
                    "amount": {"rate": 0.02},
                }
            ],
        }
    )
    assert isinstance(config.operations[0], DeleteRows)


# ---------------------------------------------------------------------------
# InsertRows
# ---------------------------------------------------------------------------


def test_insert_rows_minimal_target_and_amount_parses_without_columns() -> None:
    """target + amount only, no target.columns: valid with no model validator
    -- phantoms are pure clones."""
    op = InsertRows(
        kind="insert_rows",
        target=Target(**_TARGET_NO_COLUMNS),
        amount=Amount(count=25),
    )
    assert op.target.columns is None
    assert op.placement is None


def test_insert_rows_with_columns_parses() -> None:
    """target.columns present is valid -- eligibility is a business rule, not
    a parse-time constraint."""
    op = InsertRows(
        kind="insert_rows",
        target=Target(**_TARGET_WITH_COLUMNS),
        amount=Amount(count=25),
    )
    assert op.target.columns == ["prop__name"]


def test_insert_rows_with_placement_parses() -> None:
    """target + amount + placement is valid."""
    op = InsertRows(
        kind="insert_rows",
        target=Target(**_TARGET_NO_COLUMNS),
        amount=Amount(count=25),
        placement=EntityScoped(kind="entity_scoped", entities=Amount(count=2)),
    )
    assert isinstance(op.placement, EntityScoped)


def test_insert_rows_unknown_field_rejected() -> None:
    """An unknown extra field on insert_rows is rejected (extra='forbid')."""
    with pytest.raises(ValidationError):
        InsertRows.model_validate(
            {
                "kind": "insert_rows",
                "target": _TARGET_NO_COLUMNS,
                "amount": {"count": 1},
                "bogus": "x",
            }
        )


def test_insert_rows_kind_accepted_by_corrupt_operation_union() -> None:
    """The CorruptOperation union parses an insert_rows operation."""
    config = CorruptConfig.model_validate(
        {
            "seed": 1,
            "operations": [
                {
                    "kind": "insert_rows",
                    "target": _TARGET_NO_COLUMNS,
                    "amount": {"count": 25},
                }
            ],
        }
    )
    assert isinstance(config.operations[0], InsertRows)


# ---------------------------------------------------------------------------
# SchemaDrift
# ---------------------------------------------------------------------------


def test_schema_drift_target_where_rejected() -> None:
    """target.where set on schema_drift raises."""
    with pytest.raises(ValidationError):
        SchemaDrift(
            kind="schema_drift",
            target=Target(table="records__actor", where={"prop__x": "y"}),
            drop=["prop__z"],
        )


def test_schema_drift_target_columns_rejected() -> None:
    """target.columns set on schema_drift raises."""
    with pytest.raises(ValidationError):
        SchemaDrift(
            kind="schema_drift",
            target=Target(table="records__actor", columns=["prop__z"]),
            drop=["prop__z"],
        )


def test_schema_drift_no_action_rejected() -> None:
    """None of rename_to/retype_to/drop set raises."""
    with pytest.raises(ValidationError):
        SchemaDrift(kind="schema_drift", target=Target(table="records__actor"))


def test_schema_drift_overlapping_columns_rejected() -> None:
    """Overlapping column keys across rename_to/retype_to/drop raises."""
    with pytest.raises(ValidationError):
        SchemaDrift(
            kind="schema_drift",
            target=Target(table="records__actor"),
            rename_to={"prop__x": "prop__y"},
            drop=["prop__x"],
        )


def test_schema_drift_valid() -> None:
    """A schema_drift with a single disjoint action is valid."""
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__actor"),
        drop=["prop__z"],
    )
    assert op.drop == ["prop__z"]


# ---------------------------------------------------------------------------
# DropEvents
# ---------------------------------------------------------------------------


def test_drop_events_parses_with_table() -> None:
    """drop_events parses with a concrete `table` selector, no columns."""
    op = DropEvents(
        kind="drop_events",
        target=Target(table="history"),
        amount=Amount(rate=0.02),
    )
    assert op.target.table == "history"
    assert op.placement is None


def test_drop_events_parses_with_glob_and_where() -> None:
    """drop_events parses with `glob` and `where`."""
    op = DropEvents(
        kind="drop_events",
        target=Target(glob="hist*", where={"kind": "actor"}),
        amount=Amount(count=5),
    )
    assert op.target.glob == "hist*"
    assert op.target.where == {"kind": "actor"}


def test_drop_events_parses_with_placement() -> None:
    """drop_events parses with a placement block."""
    op = DropEvents(
        kind="drop_events",
        target=Target(table="history"),
        amount=Amount(count=5),
        placement=ClusteredTemporal(
            kind="clustered_temporal", column="sim_time", clusters=1, width=100
        ),
    )
    assert isinstance(op.placement, ClusteredTemporal)


def test_drop_events_target_columns_rejected() -> None:
    """target.columns set on drop_events raises (columns_forbidden)."""
    with pytest.raises(ValidationError):
        DropEvents(
            kind="drop_events",
            target=Target(table="history", columns=["value"]),
            amount=Amount(count=1),
        )


def test_drop_events_unknown_field_rejected() -> None:
    """An unknown extra field on drop_events is rejected (extra='forbid')."""
    with pytest.raises(ValidationError):
        DropEvents.model_validate(
            {
                "kind": "drop_events",
                "target": {"table": "history"},
                "amount": {"count": 1},
                "bogus": "x",
            }
        )


def test_drop_events_kind_accepted_by_corrupt_operation_union() -> None:
    """The extended CorruptOperation union parses a drop_events operation."""
    config = CorruptConfig.model_validate(
        {
            "seed": 1,
            "operations": [
                {
                    "kind": "drop_events",
                    "target": {"table": "history"},
                    "amount": {"rate": 0.02},
                }
            ],
        }
    )
    assert isinstance(config.operations[0], DropEvents)


# ---------------------------------------------------------------------------
# FreezeSeries
# ---------------------------------------------------------------------------


def test_freeze_series_parses_with_cut_after_first() -> None:
    """freeze_series parses with cut: after_first."""
    op = FreezeSeries(
        kind="freeze_series",
        target=Target(table="history"),
        amount=Amount(count=5),
        cut="after_first",
    )
    assert op.cut == "after_first"
    assert op.placement is None


def test_freeze_series_parses_with_cut_random() -> None:
    """freeze_series parses with cut: random."""
    op = FreezeSeries(
        kind="freeze_series",
        target=Target(table="history", where={"kind": "patient"}),
        amount=Amount(count=5),
        cut="random",
    )
    assert op.cut == "random"


def test_freeze_series_cut_required() -> None:
    """cut is a required field (no default)."""
    with pytest.raises(ValidationError):
        FreezeSeries.model_validate(
            {
                "kind": "freeze_series",
                "target": {"table": "history"},
                "amount": {"count": 5},
            }
        )


def test_freeze_series_cut_unknown_value_rejected() -> None:
    """cut rejects a value outside the Literal["after_first", "random"]."""
    with pytest.raises(ValidationError):
        FreezeSeries.model_validate(
            {
                "kind": "freeze_series",
                "target": {"table": "history"},
                "amount": {"count": 5},
                "cut": "half",
            }
        )


def test_freeze_series_parses_with_placement() -> None:
    """freeze_series parses with a placement block."""
    op = FreezeSeries(
        kind="freeze_series",
        target=Target(table="history"),
        amount=Amount(count=5),
        cut="random",
        placement=ClusteredTemporal(
            kind="clustered_temporal", column="sim_time", clusters=1, width=100
        ),
    )
    assert isinstance(op.placement, ClusteredTemporal)


def test_freeze_series_target_columns_rejected() -> None:
    """target.columns set on freeze_series raises (columns_forbidden)."""
    with pytest.raises(ValidationError):
        FreezeSeries(
            kind="freeze_series",
            target=Target(table="history", columns=["value"]),
            amount=Amount(count=1),
            cut="after_first",
        )


def test_freeze_series_unknown_field_rejected() -> None:
    """An unknown extra field on freeze_series is rejected (extra='forbid')."""
    with pytest.raises(ValidationError):
        FreezeSeries.model_validate(
            {
                "kind": "freeze_series",
                "target": {"table": "history"},
                "amount": {"count": 1},
                "cut": "after_first",
                "bogus": "x",
            }
        )


def test_freeze_series_kind_accepted_by_corrupt_operation_union() -> None:
    """The extended CorruptOperation union parses a freeze_series operation."""
    config = CorruptConfig.model_validate(
        {
            "seed": 1,
            "operations": [
                {
                    "kind": "freeze_series",
                    "target": {"table": "history"},
                    "amount": {"count": 5},
                    "cut": "random",
                }
            ],
        }
    )
    assert isinstance(config.operations[0], FreezeSeries)


# ---------------------------------------------------------------------------
# ShiftSimTime / ShiftSpec
# ---------------------------------------------------------------------------


def test_shift_sim_time_parses_with_offset() -> None:
    """shift_sim_time parses with shift.kind=offset and a distribution."""
    op = ShiftSimTime(
        kind="shift_sim_time",
        target=Target(table="history"),
        amount=Amount(count=20),
        shift=ShiftOffset(
            kind="offset",
            distribution=Distribution(shape="normal", mean=0.0, stddev=1.0),
        ),
    )
    assert isinstance(op.shift, ShiftOffset)
    assert op.shift.distribution.stddev == 1.0
    assert op.placement is None


def test_shift_sim_time_parses_with_collide() -> None:
    """shift_sim_time parses with shift.kind=collide (no extra fields)."""
    op = ShiftSimTime(
        kind="shift_sim_time",
        target=Target(table="history"),
        amount=Amount(count=10),
        shift=ShiftCollide(kind="collide"),
    )
    assert isinstance(op.shift, ShiftCollide)


def test_shift_sim_time_parses_with_swap() -> None:
    """shift_sim_time parses with shift.kind=swap (no extra fields)."""
    op = ShiftSimTime(
        kind="shift_sim_time",
        target=Target(table="history", where={"kind": "actor"}),
        amount=Amount(rate=0.1),
        shift=ShiftSwap(kind="swap"),
    )
    assert isinstance(op.shift, ShiftSwap)


def test_shift_offset_requires_distribution() -> None:
    """distribution is a required field on ShiftOffset."""
    with pytest.raises(ValidationError):
        ShiftOffset.model_validate({"kind": "offset"})


def test_shift_offset_distribution_reuses_params_match_shape() -> None:
    """A normal distribution without stddev is rejected (params_match_shape,
    reused unchanged)."""
    with pytest.raises(ValidationError):
        ShiftSimTime.model_validate(
            {
                "kind": "shift_sim_time",
                "target": {"table": "history"},
                "amount": {"count": 1},
                "shift": {"kind": "offset", "distribution": {"shape": "normal"}},
            }
        )


def test_shift_collide_unknown_field_rejected() -> None:
    """An unknown extra field on ShiftCollide is rejected (extra='forbid')."""
    with pytest.raises(ValidationError):
        ShiftCollide.model_validate({"kind": "collide", "bogus": "x"})


def test_shift_swap_unknown_field_rejected() -> None:
    """An unknown extra field on ShiftSwap is rejected (extra='forbid')."""
    with pytest.raises(ValidationError):
        ShiftSwap.model_validate({"kind": "swap", "bogus": "x"})


def test_shift_spec_unknown_kind_rejected() -> None:
    """An unknown shift.kind is rejected by the discriminator."""
    with pytest.raises(ValidationError):
        ShiftSimTime.model_validate(
            {
                "kind": "shift_sim_time",
                "target": {"table": "history"},
                "amount": {"count": 1},
                "shift": {"kind": "not_a_real_kind"},
            }
        )


def test_shift_sim_time_parses_with_placement() -> None:
    """shift_sim_time parses with a placement block."""
    op = ShiftSimTime(
        kind="shift_sim_time",
        target=Target(table="history"),
        amount=Amount(count=5),
        shift=ShiftCollide(kind="collide"),
        placement=ClusteredTemporal(
            kind="clustered_temporal", column="sim_time", clusters=1, width=100
        ),
    )
    assert isinstance(op.placement, ClusteredTemporal)


def test_shift_sim_time_target_columns_rejected() -> None:
    """target.columns set on shift_sim_time raises (columns_forbidden)."""
    with pytest.raises(ValidationError):
        ShiftSimTime(
            kind="shift_sim_time",
            target=Target(table="history", columns=["value"]),
            amount=Amount(count=1),
            shift=ShiftCollide(kind="collide"),
        )


def test_shift_sim_time_unknown_field_rejected() -> None:
    """An unknown extra field on shift_sim_time is rejected (extra='forbid')."""
    with pytest.raises(ValidationError):
        ShiftSimTime.model_validate(
            {
                "kind": "shift_sim_time",
                "target": {"table": "history"},
                "amount": {"count": 1},
                "shift": {"kind": "collide"},
                "bogus": "x",
            }
        )


def test_shift_sim_time_kind_accepted_by_corrupt_operation_union() -> None:
    """The extended (seven-member) CorruptOperation union parses a
    shift_sim_time operation."""
    config = CorruptConfig.model_validate(
        {
            "seed": 1,
            "operations": [
                {
                    "kind": "shift_sim_time",
                    "target": {"table": "history"},
                    "amount": {"count": 5},
                    "shift": {"kind": "swap"},
                }
            ],
        }
    )
    assert isinstance(config.operations[0], ShiftSimTime)


# ---------------------------------------------------------------------------
# DistortIntervals
# ---------------------------------------------------------------------------


def test_distort_intervals_parses_with_overlap_mode() -> None:
    op = DistortIntervals(
        kind="distort_intervals",
        target=Target(category="membership"),
        amount=Amount(count=5),
        mode="overlap",
    )
    assert op.mode == "overlap"
    assert op.placement is None
    assert op.name is None


def test_distort_intervals_parses_with_gap_and_left_before_join_modes() -> None:
    for mode in ("gap", "left_before_join"):
        op = DistortIntervals(
            kind="distort_intervals",
            target=Target(table="membership__actor__oncall"),
            amount=Amount(rate=0.2),
            mode=mode,
        )
        assert op.mode == mode


def test_distort_intervals_unknown_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        DistortIntervals.model_validate(
            {
                "kind": "distort_intervals",
                "target": {"table": "membership__actor__oncall"},
                "amount": {"count": 1},
                "mode": "not_a_real_mode",
            }
        )


def test_distort_intervals_missing_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        DistortIntervals.model_validate(
            {
                "kind": "distort_intervals",
                "target": {"table": "membership__actor__oncall"},
                "amount": {"count": 1},
            }
        )


def test_distort_intervals_target_columns_rejected() -> None:
    """target.columns set on distort_intervals raises (columns_forbidden)."""
    with pytest.raises(ValidationError):
        DistortIntervals(
            kind="distort_intervals",
            target=Target(table="membership__actor__oncall", columns=["left_sim_time"]),
            amount=Amount(count=1),
            mode="gap",
        )


def test_distort_intervals_unknown_field_rejected() -> None:
    """An unknown extra field on distort_intervals is rejected (extra='forbid')."""
    with pytest.raises(ValidationError):
        DistortIntervals.model_validate(
            {
                "kind": "distort_intervals",
                "target": {"table": "membership__actor__oncall"},
                "amount": {"count": 1},
                "mode": "gap",
                "bogus": "x",
            }
        )


def test_distort_intervals_name_and_placement_optional() -> None:
    op = DistortIntervals(
        kind="distort_intervals",
        name="my_distortion",
        target=Target(table="membership__actor__oncall"),
        amount=Amount(count=1),
        placement=ClusteredTemporal(
            kind="clustered_temporal", column="joined_sim_time", clusters=1, width=10
        ),
        mode="overlap",
    )
    assert op.name == "my_distortion"
    assert isinstance(op.placement, ClusteredTemporal)


def test_distort_intervals_kind_accepted_by_corrupt_operation_union() -> None:
    config = CorruptConfig.model_validate(
        {
            "seed": 1,
            "operations": [
                {
                    "kind": "distort_intervals",
                    "target": {"table": "membership__actor__oncall"},
                    "amount": {"count": 5},
                    "mode": "left_before_join",
                }
            ],
        }
    )
    assert isinstance(config.operations[0], DistortIntervals)


# ---------------------------------------------------------------------------
# MutateCells: sentinel / typo / case / whitespace / truncate /
# precision_drop / scale / mojibake / format_dirt / resample / out_of_domain
# ---------------------------------------------------------------------------

_MUTATION_PAYLOADS: dict[str, dict[str, object]] = {
    "sentinel": {"kind": "sentinel", "value": "1900-01-01"},
    "typo": {"kind": "typo"},
    "case": {"kind": "case", "form": "upper"},
    "whitespace": {"kind": "whitespace", "where": "leading"},
    "truncate": {"kind": "truncate", "max_length": 5},
    "precision_drop": {"kind": "precision_drop", "digits": 2},
    "scale": {"kind": "scale", "factor": 1000.0},
    "mojibake": {"kind": "mojibake"},
    "format_dirt": {"kind": "format_dirt"},
    "resample": {"kind": "resample"},
    "out_of_domain": {"kind": "out_of_domain"},
}

_MUTATION_MODELS: dict[str, type] = {
    "sentinel": MutationSentinel,
    "typo": MutationTypo,
    "case": MutationCase,
    "whitespace": MutationWhitespace,
    "truncate": MutationTruncate,
    "precision_drop": MutationPrecisionDrop,
    "scale": MutationScale,
    "mojibake": MutationMojibake,
    "format_dirt": MutationFormatDirt,
    "resample": MutationResample,
    "out_of_domain": MutationOutOfDomain,
}


def _mutate_cells_operation(mutation_kind: str) -> dict[str, object]:
    return {
        "kind": "mutate_cells",
        "target": {"table": "records__patient", "columns": ["prop__name"]},
        "amount": {"count": 5},
        "mutation": _MUTATION_PAYLOADS[mutation_kind],
    }


@pytest.mark.parametrize(
    "mutation_kind",
    [
        "sentinel",
        "typo",
        "case",
        "whitespace",
        "truncate",
        "precision_drop",
        "scale",
        "mojibake",
        "format_dirt",
        "resample",
        "out_of_domain",
    ],
)
def test_mutate_cells_kind_round_trips_through_corrupt_operation_union(
    mutation_kind: str,
) -> None:
    """A mutate_cells op with each of the eleven mutation kinds parses
    through the CorruptOperation union into the right mutation model."""
    config = CorruptConfig.model_validate(
        {"seed": 1, "operations": [_mutate_cells_operation(mutation_kind)]}
    )
    op = config.operations[0]
    assert isinstance(op, MutateCells)
    assert isinstance(op.mutation, _MUTATION_MODELS[mutation_kind])


def test_mutate_cells_unknown_mutation_kind_rejected() -> None:
    """An unrecognized mutation.kind is rejected by the discriminator."""
    with pytest.raises(ValidationError):
        MutateCells.model_validate(
            {
                "kind": "mutate_cells",
                "target": {"table": "records__patient", "columns": ["prop__name"]},
                "amount": {"count": 1},
                "mutation": {"kind": "not_a_real_kind"},
            }
        )


def test_mutate_cells_unknown_extra_field_rejected() -> None:
    """An unknown extra field on mutate_cells is rejected (extra='forbid')."""
    with pytest.raises(ValidationError):
        MutateCells.model_validate(
            {
                "kind": "mutate_cells",
                "target": {"table": "records__patient", "columns": ["prop__name"]},
                "amount": {"count": 1},
                "mutation": {"kind": "case", "form": "upper"},
                "bogus": "x",
            }
        )


def test_mutate_cells_requires_target_columns() -> None:
    """mutate_cells with target.columns absent raises (requires_columns)."""
    with pytest.raises(ValidationError):
        MutateCells(
            kind="mutate_cells",
            target=Target(table="records__patient"),
            amount=Amount(count=1),
            mutation=MutationCase(kind="case", form="upper"),
        )


def test_mutate_cells_truncate_max_length_zero_rejected() -> None:
    """truncate.max_length=0 raises (>= 1)."""
    with pytest.raises(ValidationError):
        MutationTruncate(kind="truncate", max_length=0)


def test_mutate_cells_sentinel_value_nan_rejected() -> None:
    """sentinel.value=NaN raises (must be finite when a float)."""
    with pytest.raises(ValidationError):
        MutationSentinel(kind="sentinel", value=float("nan"))


def test_mutate_cells_sentinel_value_inf_rejected() -> None:
    """sentinel.value=inf raises (must be finite when a float)."""
    with pytest.raises(ValidationError):
        MutationSentinel(kind="sentinel", value=float("inf"))


def test_mutate_cells_sentinel_null_value_rejected() -> None:
    """sentinel.value=null raises (value is a required scalar)."""
    with pytest.raises(ValidationError):
        MutationSentinel.model_validate({"kind": "sentinel", "value": None})


def test_mutate_cells_precision_drop_negative_digits_rejected() -> None:
    """precision_drop.digits=-1 raises (>= 0)."""
    with pytest.raises(ValidationError):
        MutationPrecisionDrop(kind="precision_drop", digits=-1)


@pytest.mark.parametrize("factor", [0.0, 1.0, float("nan"), float("inf")])
def test_mutate_cells_scale_factor_zero_one_nan_inf_rejected(factor: float) -> None:
    """scale.factor of 0, 1, NaN, or inf raises (finite and not in {0, 1})."""
    with pytest.raises(ValidationError):
        MutationScale(kind="scale", factor=factor)


# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------


def test_target_zero_selector_fields_rejected() -> None:
    """Target with none of table/tables/glob/category/record_kind raises."""
    with pytest.raises(ValidationError):
        Target(columns=["prop__x"])


def test_target_two_selector_fields_rejected() -> None:
    """Target with two selector fields set (e.g. table and glob) raises."""
    with pytest.raises(ValidationError):
        Target(table="records__actor", glob="records__*")


def test_target_table_selector_alone_accepted() -> None:
    """Target with only `table` set is valid (the shipped form)."""
    target = Target(table="records__actor")
    assert target.table == "records__actor"


def test_target_tables_selector_alone_accepted() -> None:
    """Target with only `tables` set is valid."""
    target = Target(tables=["records__actor", "records__doctor"])
    assert target.tables == ["records__actor", "records__doctor"]


def test_target_glob_selector_alone_accepted() -> None:
    """Target with only `glob` set is valid."""
    target = Target(glob="records__*")
    assert target.glob == "records__*"


def test_target_category_selector_alone_accepted() -> None:
    """Target with only `category` set is valid."""
    target = Target(category="records")
    assert target.category == "records"


def test_target_record_kind_selector_alone_accepted() -> None:
    """Target with only `record_kind` set is valid."""
    target = Target(record_kind="actor")
    assert target.record_kind == "actor"


def test_target_tables_empty_rejected() -> None:
    """Target.tables=[] raises (non-empty when present)."""
    with pytest.raises(ValidationError):
        Target(tables=[])


def test_target_tables_duplicate_rejected() -> None:
    """Target.tables with a duplicate name raises."""
    with pytest.raises(ValidationError):
        Target(tables=["records__actor", "records__actor"])


def test_schema_drift_non_table_selector_rejected() -> None:
    """schema_drift with a non-concrete-table selector (e.g. glob) raises."""
    with pytest.raises(ValidationError):
        SchemaDrift(
            kind="schema_drift",
            target=Target(glob="records__*"),
            drop=["prop__z"],
        )


def test_shipped_style_config_parses_unchanged() -> None:
    """A shipped-style config (concrete `table:` + exact `columns`) parses
    the same after the grammar bump."""
    config = CorruptConfig.model_validate(yaml.safe_load(DESIGN_DOC_EXAMPLE_YAML))
    first_op = config.operations[0]
    assert isinstance(first_op, NullCells)
    assert first_op.target.table == "records__patient"
    assert first_op.target.tables is None
    assert first_op.target.glob is None
    assert first_op.target.category is None
    assert first_op.target.record_kind is None


def test_target_columns_empty_rejected() -> None:
    """Target.columns=[] raises (non-empty when present)."""
    with pytest.raises(ValidationError):
        Target(table="records__actor", columns=[])


def test_target_columns_duplicate_rejected() -> None:
    """Target.columns with a duplicate name raises."""
    with pytest.raises(ValidationError):
        Target(table="records__actor", columns=["prop__x", "prop__x"])


def test_target_unknown_kind_rejected() -> None:
    """An unknown operation `kind` is rejected by the discriminator."""
    with pytest.raises(ValidationError):
        CorruptConfig.model_validate(
            {
                "seed": 1,
                "operations": [
                    {
                        "kind": "not_a_real_kind",
                        "target": {"table": "records__actor", "columns": ["prop__x"]},
                        "amount": {"count": 1},
                    }
                ],
            }
        )


def test_target_unknown_extra_field_rejected() -> None:
    """An unknown extra field on Target is rejected (extra='forbid')."""
    with pytest.raises(ValidationError):
        Target.model_validate({"table": "records__actor", "bogus": "x"})


# ---------------------------------------------------------------------------
# Placement: EntityScoped / ClusteredTemporal / Correlated
# ---------------------------------------------------------------------------


def test_entity_scoped_parses() -> None:
    """entity_scoped parses with an Amount entities quantity."""
    placement = EntityScoped(kind="entity_scoped", entities=Amount(rate=0.1))
    assert placement.entities.rate == 0.1


def test_clustered_temporal_parses() -> None:
    """clustered_temporal parses with column/clusters/width."""
    placement = ClusteredTemporal(
        kind="clustered_temporal", column="prop__sim_time", clusters=2, width=100
    )
    assert placement.clusters == 2
    assert placement.width == 100


def test_correlated_parses() -> None:
    """correlated parses with column/value/weight."""
    placement = Correlated(
        kind="correlated", column="prop__status", value="active", weight=3.0
    )
    assert placement.weight == 3.0


def test_placement_clustered_temporal_dict_parses_through_union() -> None:
    """A raw {'kind': 'clustered_temporal', ...} placement dict parses through
    CorruptConfig.model_validate into a typed ClusteredTemporal (the
    discriminator mapping, not direct model construction)."""
    config = CorruptConfig.model_validate(
        {
            "seed": 1,
            "operations": [
                {
                    "kind": "null_cells",
                    "target": _TARGET_WITH_COLUMNS,
                    "amount": {"count": 1},
                    "placement": {
                        "kind": "clustered_temporal",
                        "column": "prop__sim_time",
                        "clusters": 3,
                        "width": 500,
                    },
                }
            ],
        }
    )
    op = config.operations[0]
    assert isinstance(op, NullCells)
    assert isinstance(op.placement, ClusteredTemporal)
    assert op.placement.column == "prop__sim_time"
    assert op.placement.clusters == 3
    assert op.placement.width == 500


def test_placement_correlated_dict_parses_through_union() -> None:
    """A raw {'kind': 'correlated', ...} placement dict parses through
    CorruptConfig.model_validate into a typed Correlated."""
    config = CorruptConfig.model_validate(
        {
            "seed": 1,
            "operations": [
                {
                    "kind": "null_cells",
                    "target": _TARGET_WITH_COLUMNS,
                    "amount": {"count": 1},
                    "placement": {
                        "kind": "correlated",
                        "column": "prop__status",
                        "value": "active",
                        "weight": 3.0,
                    },
                }
            ],
        }
    )
    op = config.operations[0]
    assert isinstance(op, NullCells)
    assert isinstance(op.placement, Correlated)
    assert op.placement.column == "prop__status"
    assert op.placement.value == "active"
    assert op.placement.weight == 3.0


def test_placement_unknown_kind_rejected() -> None:
    """An unknown placement `kind` is rejected by the discriminator."""
    with pytest.raises(ValidationError):
        NullCells.model_validate(
            {
                "kind": "null_cells",
                "target": _TARGET_WITH_COLUMNS,
                "amount": {"count": 1},
                "placement": {"kind": "not_a_real_kind"},
            }
        )


def test_placement_extra_field_rejected() -> None:
    """An unknown extra field on a placement model is rejected (extra='forbid')."""
    with pytest.raises(ValidationError):
        Correlated(
            kind="correlated",
            column="prop__status",
            value="active",
            weight=1.0,
            bogus="x",
        )


def test_clustered_temporal_clusters_zero_rejected() -> None:
    """clusters=0 raises (clusters must be >= 1)."""
    with pytest.raises(ValidationError):
        ClusteredTemporal(
            kind="clustered_temporal", column="prop__sim_time", clusters=0, width=1
        )


def test_clustered_temporal_width_zero_rejected() -> None:
    """width=0 raises (width must be > 0)."""
    with pytest.raises(ValidationError):
        ClusteredTemporal(
            kind="clustered_temporal", column="prop__sim_time", clusters=1, width=0
        )


def test_correlated_weight_zero_rejected() -> None:
    """weight=0 raises (weight must be > 0)."""
    with pytest.raises(ValidationError):
        Correlated(kind="correlated", column="prop__status", value="active", weight=0)


def test_correlated_weight_negative_rejected() -> None:
    """A negative weight raises."""
    with pytest.raises(ValidationError):
        Correlated(
            kind="correlated", column="prop__status", value="active", weight=-1.0
        )


def test_entity_scoped_entities_rate_and_count_rejected() -> None:
    """entity_scoped.entities enforces Amount's rate-xor-count."""
    with pytest.raises(ValidationError):
        EntityScoped(kind="entity_scoped", entities=Amount(rate=0.1, count=5))


def test_placement_absent_by_default() -> None:
    """placement defaults to None on the three sampling ops."""
    op = NullCells(
        kind="null_cells",
        target=Target(**_TARGET_WITH_COLUMNS),
        amount=Amount(count=1),
    )
    assert op.placement is None


def test_null_cells_with_placement_parses() -> None:
    """null_cells with a placement block parses."""
    op = NullCells(
        kind="null_cells",
        target=Target(**_TARGET_WITH_COLUMNS),
        amount=Amount(count=1),
        placement=EntityScoped(kind="entity_scoped", entities=Amount(count=1)),
    )
    assert isinstance(op.placement, EntityScoped)
    assert op.placement.kind == "entity_scoped"
    assert op.placement.entities.count == 1


def test_duplicate_rows_with_placement_parses() -> None:
    """duplicate_rows with a placement block parses."""
    op = DuplicateRows(
        kind="duplicate_rows",
        target=Target(**_TARGET_NO_COLUMNS),
        amount=Amount(count=1),
        placement=Correlated(
            kind="correlated", column="prop__status", value="active", weight=2.0
        ),
    )
    assert isinstance(op.placement, Correlated)
    assert op.placement.kind == "correlated"
    assert op.placement.column == "prop__status"
    assert op.placement.value == "active"
    assert op.placement.weight == 2.0


def test_dangle_reference_with_placement_parses() -> None:
    """dangle_reference with a placement block parses."""
    op = DangleReference(
        kind="dangle_reference",
        target=Target(**_TARGET_WITH_COLUMNS),
        amount=Amount(count=1),
        placement=ClusteredTemporal(
            kind="clustered_temporal", column="prop__sim_time", clusters=1, width=10
        ),
    )
    assert isinstance(op.placement, ClusteredTemporal)
    assert op.placement.kind == "clustered_temporal"
    assert op.placement.column == "prop__sim_time"
    assert op.placement.clusters == 1
    assert op.placement.width == 10


def test_schema_drift_placement_rejected() -> None:
    """placement on schema_drift is rejected (extra='forbid'; the field doesn't
    exist on SchemaDrift)."""
    with pytest.raises(ValidationError):
        SchemaDrift.model_validate(
            {
                "kind": "schema_drift",
                "target": {"table": "records__actor"},
                "drop": ["prop__z"],
                "placement": {
                    "kind": "entity_scoped",
                    "entities": {"count": 1},
                },
            }
        )


# ---------------------------------------------------------------------------
# MispointReference
# ---------------------------------------------------------------------------

_MISPOINT_REFERENCE = {
    "kind": "mispoint_reference",
    "target": _TARGET_WITH_COLUMNS,
    "amount": {"count": 1},
}


def test_mispoint_reference_round_trips_through_corrupt_config() -> None:
    """A mispoint_reference op with target + amount round-trips through
    CorruptConfig into a MispointReference with its target.columns intact."""
    config = CorruptConfig.model_validate(
        {"seed": 1, "operations": [_MISPOINT_REFERENCE]}
    )
    op = config.operations[0]
    assert isinstance(op, MispointReference)
    assert op.target.columns == ["prop__name"]
    assert op.amount.count == 1


def test_mispoint_reference_requires_target_columns() -> None:
    """mispoint_reference with target.columns absent raises
    ("mispoint_reference requires target.columns")."""
    with pytest.raises(ValidationError, match="mispoint_reference requires"):
        MispointReference(
            kind="mispoint_reference",
            target=Target(**_TARGET_NO_COLUMNS),
            amount=Amount(count=1),
        )


def test_mispoint_reference_unknown_extra_field_rejected() -> None:
    """An unknown extra field on mispoint_reference is rejected
    (extra='forbid')."""
    with pytest.raises(ValidationError):
        MispointReference.model_validate({**_MISPOINT_REFERENCE, "bogus": "x"})


def test_mispoint_reference_with_placement_parses() -> None:
    """mispoint_reference with a placement block parses."""
    op = MispointReference(
        kind="mispoint_reference",
        target=Target(**_TARGET_WITH_COLUMNS),
        amount=Amount(count=1),
        placement=Correlated(
            kind="correlated", column="prop__status", value="active", weight=2.0
        ),
    )
    assert isinstance(op.placement, Correlated)
    assert op.placement.column == "prop__status"


def test_mispoint_reference_constraint_absent_is_none() -> None:
    """`constraint` absent from a mispoint_reference op parses as None."""
    op = MispointReference.model_validate(_MISPOINT_REFERENCE)
    assert op.constraint is None


def test_mispoint_reference_constraint_created_after_reference_round_trips() -> None:
    """`constraint: created_after_reference` round-trips through
    CorruptConfig."""
    config = CorruptConfig.model_validate(
        {
            "seed": 1,
            "operations": [
                {**_MISPOINT_REFERENCE, "constraint": "created_after_reference"}
            ],
        }
    )
    op = config.operations[0]
    assert isinstance(op, MispointReference)
    assert op.constraint == "created_after_reference"


def test_mispoint_reference_unknown_constraint_rejected() -> None:
    """A constraint value other than 'created_after_reference' fails at
    parse time (Literal)."""
    with pytest.raises(ValidationError):
        MispointReference.model_validate(
            {**_MISPOINT_REFERENCE, "constraint": "created_before_reference"}
        )


# ---------------------------------------------------------------------------
# CorruptConfig
# ---------------------------------------------------------------------------


def test_corrupt_config_empty_operations_rejected() -> None:
    """CorruptConfig with empty operations raises."""
    with pytest.raises(ValidationError):
        CorruptConfig(seed=1, operations=[])


# ---------------------------------------------------------------------------
# load_corrupt_config
# ---------------------------------------------------------------------------


def test_load_corrupt_config_missing_file_raises_config_error(tmp_path: Path) -> None:
    """Missing file raises ConfigError."""
    with pytest.raises(ConfigError, match="not found"):
        load_corrupt_config(tmp_path / "does_not_exist.yaml")


def test_load_corrupt_config_invalid_yaml_raises_config_error(tmp_path: Path) -> None:
    """Invalid YAML raises ConfigError."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("seed: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_corrupt_config(bad)


def test_load_corrupt_config_validation_failure_raises_config_error(
    tmp_path: Path,
) -> None:
    """A config failing Pydantic validation raises ConfigError."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("seed: 1\noperations: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="validation failed"):
        load_corrupt_config(cfg)


def test_load_corrupt_config_valid_file_returns_model(tmp_path: Path) -> None:
    """A valid file returns the parsed CorruptConfig."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(DESIGN_DOC_EXAMPLE_YAML, encoding="utf-8")
    result = load_corrupt_config(cfg)
    assert isinstance(result, CorruptConfig)
    assert result.seed == 42
    assert len(result.operations) == 4


# ---------------------------------------------------------------------------
# schema_drift rename targets / retype types are gated at config load
# ---------------------------------------------------------------------------


def test_schema_drift_rename_target_not_sql_identifier_raises() -> None:
    """A rename_to target with an embedded quote is a load-time config error."""
    with pytest.raises(ValidationError, match="SQL identifier"):
        SchemaDrift(
            kind="schema_drift",
            target=Target(table="records__actor"),
            rename_to={"prop__name": 'prop__na"me'},
        )


def test_schema_drift_retype_type_not_on_allow_list_raises() -> None:
    """A retype_to type string off the DuckDB allow-list is a load-time error
    (never spliced into SQL)."""
    with pytest.raises(ValidationError, match="not.*recognized DuckDB type"):
        SchemaDrift(
            kind="schema_drift",
            target=Target(table="records__actor"),
            retype_to={"status": "INTEGER); ATTACH '/tmp/x.db' AS x; --"},
        )


def test_schema_drift_allow_listed_retype_types_pass() -> None:
    """Recognized DuckDB types (bare and parameterized) still parse."""
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__actor"),
        retype_to={
            "prop__a": "DOUBLE",
            "prop__b": "VARCHAR(10)",
            "prop__c": "DECIMAL(9,2)",
            "prop__d": "BOOLEAN",
        },
    )
    assert op.retype_to is not None and len(op.retype_to) == 4


@pytest.mark.parametrize(
    "payload",
    [
        # Single-statement injection riding the VARCHAR( prefix: closes the
        # CAST paren, appends a table function, comments out the rest.
        "VARCHAR(10)) AS x FROM read_csv('/etc/hostname') --",
        "DECIMAL(9,2)) || (SELECT 1) --",
        # Any trailing text after the closing paren is off-grammar.
        "NUMERIC(1) x",
        # Non-digit parameter is off-grammar.
        "VARCHAR(abc)",
        "VARCHAR()",
    ],
)
def test_schema_drift_retype_parameterized_prefix_payloads_rejected(
    payload: str,
) -> None:
    """A parameterized-type prefix must not admit trailing SQL: the allow-list
    matches an anchored VARCHAR(n) / DECIMAL(p[,s]) / NUMERIC(p[,s]) grammar,
    never a prefix."""
    with pytest.raises(ValidationError, match="not.*recognized DuckDB type"):
        SchemaDrift(
            kind="schema_drift",
            target=Target(table="records__actor"),
            retype_to={"prop__a": payload},
        )


def test_schema_drift_retype_parameterized_inner_whitespace_still_passes() -> None:
    """The anchored grammar tolerates whitespace inside the parens."""
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__actor"),
        retype_to={"prop__a": "VARCHAR( 10 )", "prop__b": "DECIMAL(9, 2)"},
    )
    assert op.retype_to is not None and len(op.retype_to) == 2
