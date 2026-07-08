"""Tests for `enumerate_member_timelines` / `enumerate_interval_units`, the
`build_membership_intervals` fixture (Phase 1), and `DistortIntervalsCorrupter`
(Phase 2: per-mode rewrites, defects, locators, impacts, placement, pooling,
determinism, and the engine end-to-end)."""

from __future__ import annotations

import random
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from reader._fixtures_build import build_membership_intervals

from fabulexa_export.config.models import (
    Amount,
    ClusteredTemporal,
    Correlated,
    CorruptConfig,
    DeleteRows,
    DistortIntervals,
    DuplicateRows,
    EntityScoped,
    MutateCells,
    MutationSentinel,
    NullCells,
    SchemaDrift,
    Target,
)
from fabulexa_export.corrupters.engine import corrupt_emit
from fabulexa_export.corrupters.operations.delete_rows import DeleteRowsCorrupter
from fabulexa_export.corrupters.operations.distort_intervals import (
    DistortIntervalsCorrupter,
    enumerate_interval_units,
    enumerate_member_timelines,
)
from fabulexa_export.corrupters.operations.duplicate_rows import DuplicateRowsCorrupter
from fabulexa_export.corrupters.operations.mutate_cells import MutateCellsCorrupter
from fabulexa_export.corrupters.operations.null_cells import NullCellsCorrupter
from fabulexa_export.corrupters.operations.schema_drift import SchemaDriftCorrupter
from fabulexa_export.corrupters.state import CorruptState
from fabulexa_export.errors import CorruptError
from fabulexa_export.reader import conformance, open_emit
from fabulexa_export.reader.sidecar import BranchEntry, Sidecar

from .._helpers import (
    CallOrderRandom,
    FixedSampleRandom,
    column_spec,
    sidecar,
    table_spec,
    working_table,
)

if TYPE_CHECKING:
    import pyarrow as pa

    from fabulexa_export.corrupters.state import OperationOutcome

_FORK_PATH = "trunk"
_SLICE_AT = 100


def _membership_spec() -> object:
    return table_spec(
        "membership__actor__oncall",
        "membership",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("joined_sim_time", "BIGINT"),
            column_spec("left_sim_time", "BIGINT"),
            column_spec("elem__tag", "VARCHAR"),
            column_spec("member__doctor__kind", "VARCHAR"),
            column_spec("member__doctor__id", "VARCHAR"),
        ),
        record_kind="actor",
        property_="oncall",
    )


def _row(
    record_id: str,
    joined: int,
    left: int | None,
    *,
    tag: str | None = "tag",
    doctor_id: str = "d1",
    fork_path: str = _FORK_PATH,
) -> dict[str, object]:
    return {
        "fork_path": fork_path,
        "record_id": record_id,
        "joined_sim_time": joined,
        "left_sim_time": left,
        "elem__tag": tag,
        "member__doctor__kind": "doctor",
        "member__doctor__id": doctor_id,
    }


def _table(rows: list[dict[str, object]]) -> "pa.Table":
    return working_table(_membership_spec(), rows).data


# ---------------------------------------------------------------------------
# enumerate_member_timelines: identity, NULL grouping, ordering, fork_path
# ---------------------------------------------------------------------------


def test_timeline_identity_groups_by_record_and_all_elements() -> None:
    """Rows sharing (record_id, every element value) group into one timeline."""
    data = _table(
        [
            _row("a1", 10, 20),
            _row("a1", 30, 40),
        ]
    )
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    assert timelines == ((0, 1),)


def test_differing_elem_value_yields_distinct_timelines() -> None:
    data = _table(
        [
            _row("a1", 10, 20, tag="x"),
            _row("a1", 30, 40, tag="y"),
        ]
    )
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    assert len(timelines) == 2
    assert {t for t in timelines} == {(0,), (1,)}


def test_differing_member_id_yields_distinct_timelines() -> None:
    data = _table(
        [
            _row("a1", 10, 20, doctor_id="d1"),
            _row("a1", 30, 40, doctor_id="d2"),
        ]
    )
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    assert len(timelines) == 2


def test_null_in_same_elem_column_groups_together() -> None:
    """Two rows of one record_id, both NULL in the same elem column, group
    into one timeline -- NULL groups with NULL."""
    data = _table(
        [
            _row("a1", 10, 20, tag=None),
            _row("a1", 30, 40, tag=None),
        ]
    )
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    assert timelines == ((0, 1),)


def test_null_vs_non_null_elem_value_yields_distinct_timelines() -> None:
    data = _table(
        [
            _row("a1", 10, 20, tag=None),
            _row("a1", 10, 20, tag="present"),
        ]
    )
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    assert len(timelines) == 2


def test_within_timeline_order_is_joined_ascending() -> None:
    data = _table(
        [
            _row("a1", 30, 40),
            _row("a1", 10, 20),
        ]
    )
    (timeline,) = enumerate_member_timelines(data, _FORK_PATH)
    assert timeline == (1, 0)


def test_joined_tie_breaks_by_canonical_content_order() -> None:
    """A joined_sim_time tie within one timeline orders by canonical content
    (here, left_sim_time ascending, since every other column ties)."""
    data = _table(
        [
            _row("a1", 10, 25),
            _row("a1", 10, 20),
        ]
    )
    (timeline,) = enumerate_member_timelines(data, _FORK_PATH)
    assert timeline == (1, 0)  # left=20 sorts before left=25


def test_byte_identical_rows_order_deterministically_across_calls() -> None:
    data = _table(
        [
            _row("a1", 10, 20),
            _row("a1", 10, 20),
        ]
    )
    first = enumerate_member_timelines(data, _FORK_PATH)
    second = enumerate_member_timelines(data, _FORK_PATH)
    assert first == second


def test_timeline_order_is_first_row_canonical_content_order() -> None:
    data = _table(
        [
            _row("b1", 10, 20),
            _row("a1", 10, 20),
        ]
    )
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    assert timelines == ((1,), (0,))


def test_fork_path_narrows_out_other_branches() -> None:
    data = _table(
        [
            _row("a1", 10, 20, fork_path="trunk"),
            _row("a1", 10, 20, fork_path="trunk@fork"),
        ]
    )
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    assert timelines == ((0,),)


def test_missing_structural_column_raises() -> None:
    spec = table_spec(
        "membership__actor__oncall",
        "membership",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("left_sim_time", "BIGINT"),
        ),
        record_kind="actor",
        property_="oncall",
    )
    data = working_table(spec, [{"fork_path": "trunk", "record_id": "a1"}]).data
    with pytest.raises(CorruptError):
        enumerate_member_timelines(data, _FORK_PATH)


def test_empty_table_yields_empty_timelines() -> None:
    assert enumerate_member_timelines(_table([]), _FORK_PATH) == ()


# ---------------------------------------------------------------------------
# enumerate_interval_units: overlap
# ---------------------------------------------------------------------------


def test_overlap_pairs_adjacent_rows_keyed_on_a() -> None:
    """A is a pair's mutated row of at most one pair; the last row of a
    timeline is no pair's A."""
    data = _table(
        [
            _row("a1", 10, 20),
            _row("a1", 25, 35),
            _row("a1", 40, 50),
        ]
    )
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    population = frozenset(range(data.num_rows))
    units = enumerate_interval_units("overlap", timelines, data, population, _SLICE_AT)
    assert units == ((0, 1), (1, 2))


def test_overlap_excludes_a_with_null_left() -> None:
    data = _table(
        [
            _row("a1", 10, None),
            _row("a1", 25, 35),
        ]
    )
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    population = frozenset(range(data.num_rows))
    units = enumerate_interval_units("overlap", timelines, data, population, _SLICE_AT)
    assert units == ()


def test_overlap_excludes_b_with_span_under_2() -> None:
    data = _table(
        [
            _row("a1", 10, 20),
            _row("a1", 25, 26),  # span 1
        ]
    )
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    population = frozenset(range(data.num_rows))
    units = enumerate_interval_units("overlap", timelines, data, population, _SLICE_AT)
    assert units == ()


def test_overlap_open_b_uses_slice_at_as_boundary() -> None:
    data = _table(
        [
            _row("a1", 10, 20),
            _row("a1", 96, None),  # slice_at(100) - 96 = 4 >= 2
        ]
    )
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    population = frozenset(range(data.num_rows))
    units = enumerate_interval_units("overlap", timelines, data, population, _SLICE_AT)
    assert units == ((0, 1),)

    data_close = _table(
        [
            _row("a2", 10, 20),
            _row("a2", 99, None),  # slice_at(100) - 99 = 1 < 2
        ]
    )
    timelines_close = enumerate_member_timelines(data_close, _FORK_PATH)
    population_close = frozenset(range(data_close.num_rows))
    units_close = enumerate_interval_units(
        "overlap", timelines_close, data_close, population_close, _SLICE_AT
    )
    assert units_close == ()


# ---------------------------------------------------------------------------
# enumerate_interval_units: gap
# ---------------------------------------------------------------------------


def test_gap_requires_closed_span_at_least_2() -> None:
    data = _table(
        [
            _row("a1", 10, 12),  # span 2 -- qualifies
            _row("a2", 10, 11),  # span 1 -- excluded
            _row("a3", 10, 10),  # span 0 -- excluded
            _row("a4", 10, None),  # open -- excluded
        ]
    )
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    population = frozenset(range(data.num_rows))
    units = enumerate_interval_units("gap", timelines, data, population, _SLICE_AT)
    (row_index, successor) = units[0]
    assert len(units) == 1
    assert successor is None
    assert data.column("record_id")[row_index].as_py() == "a1"


# ---------------------------------------------------------------------------
# enumerate_interval_units: left_before_join
# ---------------------------------------------------------------------------


def test_left_before_join_requires_strict_inversion_source() -> None:
    data = _table(
        [
            _row("a1", 10, 11),  # left > joined -- qualifies
            _row("a2", 10, 10),  # left == joined -- excluded
            _row("a3", 10, None),  # open -- excluded
        ]
    )
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    population = frozenset(range(data.num_rows))
    units = enumerate_interval_units(
        "left_before_join", timelines, data, population, _SLICE_AT
    )
    (row_index, successor) = units[0]
    assert len(units) == 1
    assert successor is None
    assert data.column("record_id")[row_index].as_py() == "a1"


# ---------------------------------------------------------------------------
# where semantics: population_indices decides unit membership, not adjacency
# ---------------------------------------------------------------------------


def test_pair_qualifies_when_a_included_even_if_b_excluded() -> None:
    data = _table(
        [
            _row("a1", 10, 20),
            _row("a1", 25, 35),
        ]
    )
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    population = frozenset({0})
    units = enumerate_interval_units("overlap", timelines, data, population, _SLICE_AT)
    assert units == ((0, 1),)


def test_pair_excluded_when_a_excluded_even_if_b_included() -> None:
    data = _table(
        [
            _row("a1", 10, 20),
            _row("a1", 25, 35),
        ]
    )
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    population = frozenset({1})
    units = enumerate_interval_units("overlap", timelines, data, population, _SLICE_AT)
    assert units == ()


def test_single_row_unit_qualifies_iff_included() -> None:
    data = _table([_row("a1", 10, 15)])
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    included = enumerate_interval_units(
        "gap", timelines, data, frozenset({0}), _SLICE_AT
    )
    excluded = enumerate_interval_units("gap", timelines, data, frozenset(), _SLICE_AT)
    assert included == ((0, None),)
    assert excluded == ()


def test_adjacency_identical_with_and_without_narrowing() -> None:
    data = _table(
        [
            _row("a1", 10, 20),
            _row("a1", 25, 35),
        ]
    )
    timelines_full = enumerate_member_timelines(data, _FORK_PATH)
    narrow_population = frozenset({0})
    full_population = frozenset(range(data.num_rows))
    narrow_units = enumerate_interval_units(
        "overlap", timelines_full, data, narrow_population, _SLICE_AT
    )
    full_units = enumerate_interval_units(
        "overlap", timelines_full, data, full_population, _SLICE_AT
    )
    assert narrow_units == full_units == ((0, 1),)


# ---------------------------------------------------------------------------
# Unit order, unknown mode, empty population
# ---------------------------------------------------------------------------


def test_unit_order_is_timeline_major_position_minor() -> None:
    data = _table(
        [
            _row("b1", 10, 15),
            _row("a1", 10, 15),
        ]
    )
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    population = frozenset(range(data.num_rows))
    units = enumerate_interval_units("gap", timelines, data, population, _SLICE_AT)
    # a1 (index 1) orders first (canonical order), then b1 (index 0)
    assert units == ((1, None), (0, None))


def test_unknown_mode_raises_corrupt_error() -> None:
    data = _table([_row("a1", 10, 20)])
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    population = frozenset(range(data.num_rows))
    with pytest.raises(CorruptError):
        enumerate_interval_units("bogus", timelines, data, population, _SLICE_AT)


def test_no_qualifying_units_yields_empty_tuple() -> None:
    data = _table([_row("a1", 10, None)])  # lone open interval
    timelines = enumerate_member_timelines(data, _FORK_PATH)
    population = frozenset(range(data.num_rows))
    for mode in ("overlap", "gap", "left_before_join"):
        assert (
            enumerate_interval_units(mode, timelines, data, population, _SLICE_AT) == ()
        )


# ---------------------------------------------------------------------------
# Fixture: build_membership_intervals
# ---------------------------------------------------------------------------


def test_fixture_is_c1_c12_conformant_and_yields_documented_populations(
    tmp_path: Path,
) -> None:
    build_membership_intervals(tmp_path)
    with open_emit(tmp_path) as emit:
        result = conformance.validate(emit)
        assert result.ok, [
            (check.check, check.messages)
            for check in result.results
            if not check.passed
        ]

        working = emit.query_arrow("SELECT * FROM membership__actor__oncall", ())
        branch = emit.sidecar.branches()[0]

    record_ids = working.column("record_id").to_pylist()
    timelines = enumerate_member_timelines(working, branch.fork_path)
    assert len(timelines) == 7

    population = frozenset(range(working.num_rows))

    overlap_units = enumerate_interval_units(
        "overlap", timelines, working, population, branch.slice_at
    )
    assert len(overlap_units) == 2
    assert {record_ids[a] for a, _b in overlap_units} == {"a002", "a003"}

    for mode in ("gap", "left_before_join"):
        units = enumerate_interval_units(
            mode, timelines, working, population, branch.slice_at
        )
        assert {record_ids[i] for i, _s in units} == {"a002", "a004"}


# ---------------------------------------------------------------------------
# DistortIntervalsCorrupter: fixtures, sidecar, state, apply helper
# ---------------------------------------------------------------------------

_HANDLER = DistortIntervalsCorrupter()


def _ward_spec() -> object:
    """A second membership-category table (canonically sorts after
    `membership__actor__oncall`) -- for the pooling test."""
    return table_spec(
        "membership__actor__ward",
        "membership",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("joined_sim_time", "BIGINT"),
            column_spec("left_sim_time", "BIGINT"),
            column_spec("elem__tag", "VARCHAR"),
            column_spec("member__doctor__kind", "VARCHAR"),
            column_spec("member__doctor__id", "VARCHAR"),
        ),
        record_kind="actor",
        property_="ward",
    )


def _handler_sidecar(*, slice_at: int = _SLICE_AT) -> Sidecar:
    return sidecar(
        (_membership_spec(),),
        branches=(BranchEntry(fork_path=_FORK_PATH, parent=None, slice_at=slice_at),),
    )


def _state(rows: list[dict[str, object]]) -> CorruptState:
    return CorruptState(
        tables={"membership__actor__oncall": working_table(_membership_spec(), rows)}
    )


def _apply(
    state: CorruptState,
    mode: str,
    *,
    count: int | None = None,
    rate: float | None = None,
    where: dict[str, str] | None = None,
    seed: int = 1,
    placement: object = None,
    rng: random.Random | None = None,
    slice_at: int = _SLICE_AT,
) -> "OperationOutcome":
    amount = Amount(count=count) if count is not None else Amount(rate=rate)
    op = DistortIntervals(
        kind="distort_intervals",
        target=Target(table="membership__actor__oncall", where=where),
        amount=amount,
        placement=placement,
        mode=mode,
    )
    return _HANDLER.apply(
        state,
        op,
        "rule#0",
        rng if rng is not None else random.Random(seed),
        _FORK_PATH,
        _handler_sidecar(slice_at=slice_at),
    )


# ---------------------------------------------------------------------------
# overlap rewrite
# ---------------------------------------------------------------------------


def test_overlap_rewrite_computes_midpoint_and_stays_c10_green() -> None:
    """A.left' == B.joined + floor((B_end - B.joined) / 2); the post-state
    overlaps B.joined and A stays C10-green (A.left' >= A.joined)."""
    state = _state([_row("a1", 10, 20), _row("a1", 25, 35)])
    outcome = _apply(state, "overlap", count=1)
    assert outcome.units_affected == 1
    lefts = (
        state.tables["membership__actor__oncall"]
        .data.column("left_sim_time")
        .to_pylist()
    )
    assert lefts == [30, 35]  # 25 + floor((35 - 25) / 2) == 30
    assert lefts[0] > 25  # A.left' overlaps B.joined
    assert lefts[0] >= 10  # A stays C10-green
    (defect,) = outcome.defects
    assert defect.defect_class == "overlapping_interval"
    assert defect.impact == ("beyond-c1-c12",)
    assert defect.location.kind == "cell"
    assert defect.location.column == "left_sim_time"


def test_overlap_open_b_uses_slice_at_as_rewrite_boundary() -> None:
    state = _state([_row("a1", 10, 20), _row("a1", 96, None)])
    outcome = _apply(state, "overlap", count=1, slice_at=_SLICE_AT)
    assert outcome.units_affected == 1
    lefts = (
        state.tables["membership__actor__oncall"]
        .data.column("left_sim_time")
        .to_pylist()
    )
    assert lefts[0] == 98  # 96 + floor((100 - 96) / 2) == 98


def test_overlap_one_defect_per_counted_unit() -> None:
    state = _state(
        [
            _row("a1", 10, 20),
            _row("a1", 25, 35),
            _row("b1", 40, 50),
            _row("b1", 55, 65),
        ]
    )
    outcome = _apply(state, "overlap", rate=1.0)
    assert outcome.units_selected == 2
    assert outcome.units_affected == 2
    assert len(outcome.defects) == 2
    assert {d.defect_class for d in outcome.defects} == {"overlapping_interval"}


def test_overlap_no_mutation_selected_but_not_counted() -> None:
    """A whose left_sim_time already equals the rewrite target is selected but
    not counted: no defect, units_affected excludes it, no rewrite."""
    state = _state([_row("a1", 10, 22), _row("a1", 20, 24)])
    outcome = _apply(state, "overlap", count=1)
    assert outcome.units_selected == 1
    assert outcome.units_affected == 0
    assert outcome.defects == ()
    lefts = (
        state.tables["membership__actor__oncall"]
        .data.column("left_sim_time")
        .to_pylist()
    )
    assert lefts == [22, 24]


def test_overlap_no_mutation_does_not_perturb_rng_consumption() -> None:
    """The no-mutation drop happens after the draw: the unit-selection draw's
    RNG calls depend only on population size and requested count, identical
    whether or not the drawn unit turns out to be no-mutation."""
    state_normal = _state(
        [_row("a1", 10, 20), _row("a1", 25, 35), _row("b1", 10, 20), _row("b1", 25, 35)]
    )
    rng_normal = CallOrderRandom(seed=5)
    _apply(state_normal, "overlap", count=1, rng=rng_normal)

    state_no_mutation = _state(
        [_row("a1", 10, 22), _row("a1", 20, 24), _row("b1", 10, 20), _row("b1", 25, 35)]
    )
    rng_no_mutation = CallOrderRandom(seed=5)
    _apply(state_no_mutation, "overlap", count=1, rng=rng_no_mutation)

    assert rng_normal.calls == rng_no_mutation.calls


# ---------------------------------------------------------------------------
# gap rewrite
# ---------------------------------------------------------------------------


def test_gap_rewrite_strict_shrink() -> None:
    state = _state([_row("a1", 10, 20)])
    outcome = _apply(state, "gap", count=1)
    assert outcome.units_affected == 1
    lefts = (
        state.tables["membership__actor__oncall"]
        .data.column("left_sim_time")
        .to_pylist()
    )
    assert lefts == [15]  # 10 + floor((20 - 10) / 2) == 15
    assert 10 <= lefts[0] < 20
    (defect,) = outcome.defects
    assert defect.defect_class == "interval_gap"
    assert defect.impact == ("beyond-c1-c12",)
    assert defect.location.kind == "cell"
    assert defect.location.column == "left_sim_time"


# ---------------------------------------------------------------------------
# left_before_join rewrite
# ---------------------------------------------------------------------------


def test_left_before_join_swaps_values_and_declares_c10() -> None:
    state = _state([_row("a1", 10, 15)])
    outcome = _apply(state, "left_before_join", count=1)
    assert outcome.units_affected == 1
    data = state.tables["membership__actor__oncall"].data
    assert data.column("joined_sim_time").to_pylist() == [15]
    assert data.column("left_sim_time").to_pylist() == [10]
    (defect,) = outcome.defects
    assert defect.defect_class == "inverted_interval"
    assert defect.impact == ("C10",)
    assert defect.location.kind == "row"
    assert dict(defect.location.row.keys)["joined_sim_time"] == "15"


def test_left_before_join_exactly_counted_rows_violate_ordering() -> None:
    state = _state([_row("a1", 10, 15), _row("b1", 20, 28)])
    outcome = _apply(state, "left_before_join", rate=1.0)
    assert outcome.units_affected == 2
    data = state.tables["membership__actor__oncall"].data
    joined = data.column("joined_sim_time").to_pylist()
    left = data.column("left_sim_time").to_pylist()
    for j, left_value in zip(joined, left):
        assert left_value < j


# ---------------------------------------------------------------------------
# Simultaneous rewrite
# ---------------------------------------------------------------------------


def test_simultaneous_overlap_pair_rewrite_uses_operation_start_boundaries() -> None:
    """R1 is B of (R0, R1) and A of (R1, R2); R0's rewrite target is computed
    from R1's operation-start boundaries, not R1's rewritten one."""
    state = _state([_row("a1", 10, 20), _row("a1", 25, 35), _row("a1", 40, 50)])
    outcome = _apply(state, "overlap", rate=1.0)
    assert outcome.units_affected == 2
    lefts = (
        state.tables["membership__actor__oncall"]
        .data.column("left_sim_time")
        .to_pylist()
    )
    assert lefts[0] == 30  # R1.joined(25) + floor((R1.left(35) - 25) / 2)
    assert lefts[1] == 45  # R2.joined(40) + floor((R2.left(50) - 40) / 2)


# ---------------------------------------------------------------------------
# Interval locality
# ---------------------------------------------------------------------------


def test_interval_locality_only_timing_cells_of_counted_rows_change() -> None:
    state = _state([_row("a1", 10, 20, tag="keep"), _row("a1", 25, 35, tag="keep")])
    original_spec = state.tables["membership__actor__oncall"].spec
    outcome = _apply(state, "overlap", count=1)
    assert outcome.units_affected == 1
    table = state.tables["membership__actor__oncall"]
    assert table.spec is original_spec
    assert table.data.num_rows == 2
    assert table.data.column("elem__tag").to_pylist() == ["keep", "keep"]
    assert table.data.column("member__doctor__id").to_pylist() == ["d1", "d1"]
    assert table.data.column("joined_sim_time").to_pylist() == [10, 25]


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def test_placement_pair_weight_derives_from_earlier_row_a_not_b() -> None:
    """clustered_temporal's center/weight derivation reads each pair unit's
    earlier (A) row only: Q's A is far from the forced center even though its
    B sits right on it -- Q is excluded, not selected."""
    rows = [
        _row("p1", 500, 510),  # A of pair P: joined=500 -- inside the window
        _row("p1", 600, 610),  # B of pair P: joined=600 -- irrelevant
        _row("q1", 0, 10),  # A of pair Q: joined=0 -- outside the window
        _row("q1", 500, 505),  # B of pair Q: joined=500 -- would be "inside" if used
    ]
    state = _state(rows)
    placement = ClusteredTemporal(
        kind="clustered_temporal", column="joined_sim_time", clusters=1, width=3
    )
    outcome = _apply(
        state,
        "overlap",
        count=1,
        placement=placement,
        rng=FixedSampleRandom([500], seed=1),
    )
    assert outcome.units_selected == 1
    lefts = (
        state.tables["membership__actor__oncall"]
        .data.column("left_sim_time")
        .to_pylist()
    )
    assert lefts[0] == 605  # P's A rewritten: 600 + floor((610 - 600) / 2)
    assert lefts[2] == 10  # Q's A untouched


def test_placement_correlated_weight_matches_a_condition() -> None:
    state = _state(
        [
            _row("x1", 10, 20, tag="MATCH"),
            _row("x1", 25, 35, tag="MATCH"),
            _row("y1", 10, 20, tag="OTHER"),
            _row("y1", 25, 35, tag="OTHER"),
        ]
    )
    placement = Correlated(
        kind="correlated", column="elem__tag", value="MATCH", weight=1e9
    )
    outcome = _apply(
        state, "overlap", count=1, placement=placement, rng=random.Random(1)
    )
    assert outcome.units_selected == 1
    data = state.tables["membership__actor__oncall"].data
    assert data.column("record_id").to_pylist()[0] == "x1"


def test_placement_entity_scoped_universe_uses_pair_a_record_ids() -> None:
    state = _state(
        [
            _row("p1", 10, 20),
            _row("p1", 25, 35),
            _row("q1", 10, 20),
            _row("q1", 25, 35),
        ]
    )
    placement = EntityScoped(kind="entity_scoped", entities=Amount(count=1))
    outcome = _apply(
        state,
        "overlap",
        rate=1.0,
        placement=placement,
        rng=FixedSampleRandom(["p1"], seed=1),
    )
    assert outcome.units_selected == 1
    lefts = (
        state.tables["membership__actor__oncall"]
        .data.column("left_sim_time")
        .to_pylist()
    )
    assert lefts[0] != 20  # p1's A rewritten
    assert lefts[2] == 20  # q1's A untouched


def test_placement_exact_amount_ceiling_caps_at_positive_weight_count() -> None:
    """amount.count above the positive-weight unit count draws only the
    positive-weight units, never a zero-weight one to fill the quota."""
    state = _state(
        [
            _row("p1", 10, 20),
            _row("p1", 25, 35),
            _row("q1", 10, 20),
            _row("q1", 25, 35),
            _row("r1", 10, 20),
            _row("r1", 25, 35),
        ]
    )
    placement = EntityScoped(kind="entity_scoped", entities=Amount(count=2))
    outcome = _apply(
        state,
        "overlap",
        count=10,
        placement=placement,
        rng=FixedSampleRandom(["p1", "q1"], seed=1),
    )
    assert outcome.units_selected == 2


# ---------------------------------------------------------------------------
# Pooling
# ---------------------------------------------------------------------------


def test_pooling_across_two_membership_tables_canonical_table_order() -> None:
    ward_spec = _ward_spec()
    state = CorruptState(
        tables={
            "membership__actor__oncall": working_table(
                _membership_spec(), [_row("a1", 10, 20)]
            ),
            "membership__actor__ward": working_table(ward_spec, [_row("b1", 10, 20)]),
        }
    )
    sc = sidecar(
        (_membership_spec(), ward_spec),
        branches=(BranchEntry(fork_path=_FORK_PATH, parent=None, slice_at=_SLICE_AT),),
    )
    op = DistortIntervals(
        kind="distort_intervals",
        target=Target(category="membership"),
        amount=Amount(count=2),
        mode="gap",
    )
    outcome = _HANDLER.apply(state, op, "rule#0", random.Random(1), _FORK_PATH, sc)
    assert outcome.units_selected == 2
    assert outcome.units_affected == 2
    assert state.tables["membership__actor__oncall"].data.column(
        "left_sim_time"
    ).to_pylist() == [15]
    assert state.tables["membership__actor__ward"].data.column(
        "left_sim_time"
    ).to_pylist() == [15]


# ---------------------------------------------------------------------------
# Determinism / RNG order
# ---------------------------------------------------------------------------


def test_rng_order_placement_then_unit_draw_no_mode_draws() -> None:
    state = _state([_row("a1", 10, 20), _row("a1", 25, 35), _row("b1", 5, 15)])
    op = DistortIntervals(
        kind="distort_intervals",
        target=Target(table="membership__actor__oncall"),
        amount=Amount(count=1),
        placement=EntityScoped(kind="entity_scoped", entities=Amount(count=1)),
        mode="overlap",
    )
    rng = CallOrderRandom(seed=3)
    _HANDLER.apply(state, op, "rule#0", rng, _FORK_PATH, _handler_sidecar())
    assert rng.calls[0] == "sample"
    assert set(rng.calls[1:]) <= {"random"}


def test_no_mode_draws_for_any_mode() -> None:
    for mode, rows in (
        ("overlap", [_row("a1", 10, 20), _row("a1", 25, 35)]),
        ("gap", [_row("a1", 10, 20)]),
        ("left_before_join", [_row("a1", 10, 15)]),
    ):
        state = _state(rows)
        rng = CallOrderRandom(seed=3)
        _apply(state, mode, count=1, rng=rng)
        assert "uniform" not in rng.calls
        assert "gauss" not in rng.calls


def test_rerun_with_same_seed_is_identical() -> None:
    rows = [_row("a1", 10, 20), _row("a1", 25, 35), _row("b1", 5, 15)]
    state_a = _state(rows)
    state_b = _state(rows)
    outcome_a = _apply(state_a, "overlap", rate=1.0, seed=9)
    outcome_b = _apply(state_b, "overlap", rate=1.0, seed=9)
    assert outcome_a.defects == outcome_b.defects
    assert state_a.tables["membership__actor__oncall"].data.equals(
        state_b.tables["membership__actor__oncall"].data
    )


# ---------------------------------------------------------------------------
# Zero-unit population
# ---------------------------------------------------------------------------


def test_zero_unit_population_is_noop_not_error() -> None:
    """A single-interval timeline has no overlap pair -- units_selected == 0,
    no defects, no error."""
    state = _state([_row("a1", 10, 20)])
    outcome = _apply(state, "overlap", rate=1.0)
    assert outcome.units_selected == 0
    assert outcome.units_affected == 0
    assert outcome.defects == ()


# ---------------------------------------------------------------------------
# Engine end-to-end
# ---------------------------------------------------------------------------


def test_engine_end_to_end_preserves_rows_and_structural_conformance(
    tmp_path: Path,
) -> None:
    emit_dir = tmp_path / "membership_intervals"
    build_membership_intervals(emit_dir)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            DistortIntervals(
                kind="distort_intervals",
                target=Target(category="membership"),
                amount=Amount(rate=1.0),
                mode="gap",
            )
        ],
    )

    with open_emit(emit_dir) as emit:
        source_rows = next(
            t.rows
            for t in emit.sidecar.tables()
            if t.name == "membership__actor__oncall"
        )
        corrupt_emit(emit, config, out_dir)

    with open_emit(out_dir) as corrupted:
        result = conformance.validate(corrupted)
        sidecar_rows = next(
            t.rows
            for t in corrupted.sidecar.tables()
            if t.name == "membership__actor__oncall"
        )
        actual_rows = (
            corrupted.query_arrow(
                "SELECT COUNT(*) AS n FROM membership__actor__oncall", ()
            )
            .column("n")[0]
            .as_py()
        )

    assert sidecar_rows == source_rows == actual_rows
    structural = {"C1", "C2", "C3", "C4", "C5", "C8"}
    for check in result.results:
        if check.check in structural:
            assert check.passed, f"{check.check} failed: {check.messages}"


# ---------------------------------------------------------------------------
# Composition -- other operations feeding distort_intervals, and
# distort_intervals feeding itself (DD § Composition)
# ---------------------------------------------------------------------------


def test_null_cells_then_overlap_reads_nulled_successor_at_slice_at() -> None:
    """An earlier null_cells nulling B's left_sim_time drops B from the gap /
    left_before_join populations, yet the pair still qualifies for `overlap`
    -- B's boundary now reads as slice_at."""
    state = _state([_row("a1", 10, 10), _row("a1", 15, 25)])
    null_op = NullCells(
        kind="null_cells",
        target=Target(
            table="membership__actor__oncall",
            where={"record_id": "a1", "joined_sim_time": "15"},
            columns=["left_sim_time"],
        ),
        amount=Amount(rate=1.0),
    )
    NullCellsCorrupter().apply(
        state, null_op, "null_b", random.Random(1), _FORK_PATH, _handler_sidecar()
    )
    lefts = (
        state.tables["membership__actor__oncall"]
        .data.column("left_sim_time")
        .to_pylist()
    )
    assert lefts == [10, None]  # B is now open

    # B alone no longer qualifies for gap (left_sim_time non-NULL required).
    gap_outcome = _apply(state, "gap", rate=1.0, where={"joined_sim_time": "15"})
    assert gap_outcome.units_selected == 0

    # The (A, B) pair still qualifies for overlap -- B's boundary is slice_at.
    overlap_outcome = _apply(state, "overlap", rate=1.0)
    assert overlap_outcome.units_affected == 1
    lefts = (
        state.tables["membership__actor__oncall"]
        .data.column("left_sim_time")
        .to_pylist()
    )
    assert lefts[0] == 57  # 15 + floor((100 - 15) / 2), B read at slice_at
    assert lefts[1] is None  # B itself untouched


def test_mutate_cells_then_overlap_regroups_by_evolved_element_value() -> None:
    """An earlier mutate_cells rewriting a pair's B elem value regroups it
    into a distinct timeline from its former A -- the pair no longer exists."""
    state = _state([_row("a2", 10, 10, tag="day"), _row("a2", 15, 25, tag="day")])
    mutate_op = MutateCells(
        kind="mutate_cells",
        target=Target(
            table="membership__actor__oncall",
            where={"record_id": "a2", "joined_sim_time": "15"},
            columns=["elem__tag"],
        ),
        amount=Amount(rate=1.0),
        mutation=MutationSentinel(kind="sentinel", value="dusk"),
    )
    MutateCellsCorrupter().apply(
        state, mutate_op, "drift_tag", random.Random(1), _FORK_PATH, _handler_sidecar()
    )
    tags = (
        state.tables["membership__actor__oncall"].data.column("elem__tag").to_pylist()
    )
    assert tags == ["day", "dusk"]  # working values now differ -- distinct timelines

    outcome = _apply(state, "overlap", rate=1.0)
    assert outcome.units_selected == 0
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_delete_rows_then_overlap_pairs_the_flanking_survivors() -> None:
    """An earlier delete_rows removing a timeline's middle interval re-derives
    adjacency over the survivors -- the flanking rows become a pair."""
    state = _state([_row("a3", 10, 20), _row("a3", 25, 35), _row("a3", 40, 50)])
    delete_op = DeleteRows(
        kind="delete_rows",
        target=Target(
            table="membership__actor__oncall",
            where={"record_id": "a3", "joined_sim_time": "25"},
        ),
        amount=Amount(count=1),
    )
    DeleteRowsCorrupter().apply(
        state,
        delete_op,
        "delete_middle",
        random.Random(1),
        _FORK_PATH,
        _handler_sidecar(),
    )
    data = state.tables["membership__actor__oncall"].data
    assert data.num_rows == 2
    assert data.column("joined_sim_time").to_pylist() == [10, 40]

    outcome = _apply(state, "overlap", rate=1.0)
    assert outcome.units_affected == 1
    lefts = (
        state.tables["membership__actor__oncall"]
        .data.column("left_sim_time")
        .to_pylist()
    )
    assert lefts == [45, 50]  # 40 + floor((50 - 40) / 2), the flanking survivors


def test_duplicate_rows_then_overlap_pairs_with_twin_no_special_case() -> None:
    """An earlier duplicate_rows byte-identical copy joins its source row's
    timeline and forms an overlap pair with it -- no special-cased handling."""
    state = _state([_row("a4", 10, 15)])
    duplicate_op = DuplicateRows(
        kind="duplicate_rows",
        target=Target(table="membership__actor__oncall", where={"record_id": "a4"}),
        amount=Amount(count=1),
    )
    DuplicateRowsCorrupter().apply(
        state,
        duplicate_op,
        "duplicate_a4",
        random.Random(1),
        _FORK_PATH,
        _handler_sidecar(),
    )
    data = state.tables["membership__actor__oncall"].data
    assert data.num_rows == 2
    assert data.column("left_sim_time").to_pylist() == [15, 15]

    outcome = _apply(state, "overlap", rate=1.0)
    assert outcome.units_affected == 1
    lefts = (
        state.tables["membership__actor__oncall"]
        .data.column("left_sim_time")
        .to_pylist()
    )
    assert lefts == [12, 15]  # 10 + floor((15 - 10) / 2); the twin B untouched


def test_left_before_join_then_overlap_heals_inversion_as_a() -> None:
    """A later `overlap` selecting a `left_before_join`-inverted row as its A
    rewrites A.left' to stay >= A.joined -- healing the inversion. The
    earlier `left_before_join` defect still declares C10, a sound
    over-declaration once the working state no longer violates it."""
    state = _state([_row("a5", 10, 15), _row("a5", 20, 30)])
    invert_outcome = _apply(
        state, "left_before_join", rate=1.0, where={"joined_sim_time": "10"}
    )
    assert invert_outcome.units_affected == 1
    (invert_defect,) = invert_outcome.defects
    assert invert_defect.defect_class == "inverted_interval"
    assert invert_defect.impact == ("C10",)
    joined = (
        state.tables["membership__actor__oncall"]
        .data.column("joined_sim_time")
        .to_pylist()
    )
    left = (
        state.tables["membership__actor__oncall"]
        .data.column("left_sim_time")
        .to_pylist()
    )
    assert (joined[0], left[0]) == (15, 10)  # inverted: left < joined

    overlap_outcome = _apply(state, "overlap", rate=1.0)
    assert overlap_outcome.units_affected == 1
    joined = (
        state.tables["membership__actor__oncall"]
        .data.column("joined_sim_time")
        .to_pylist()
    )
    left = (
        state.tables["membership__actor__oncall"]
        .data.column("left_sim_time")
        .to_pylist()
    )
    assert left[0] == 25  # 20 + floor((30 - 20) / 2)
    assert left[0] >= joined[0]  # A's inversion is healed -- the earlier C10
    # declaration now over-approximates the (healed) working state.


def test_left_before_join_then_gap_and_left_before_join_exclude_inverted_row() -> None:
    """A `left_before_join`-inverted row fails both the `gap` and
    `left_before_join` population filters -- their strict/duration thresholds
    read the working (post-inversion) values, not the operation-start ones."""
    for later_mode in ("gap", "left_before_join"):
        state = _state([_row("a6", 10, 15)])
        _apply(state, "left_before_join", rate=1.0)
        joined = (
            state.tables["membership__actor__oncall"]
            .data.column("joined_sim_time")
            .to_pylist()
        )
        left = (
            state.tables["membership__actor__oncall"]
            .data.column("left_sim_time")
            .to_pylist()
        )
        assert (joined[0], left[0]) == (15, 10)

        outcome = _apply(state, later_mode, rate=1.0)
        assert outcome.units_selected == 0, later_mode


def test_two_distort_intervals_operations_compose_on_one_table() -> None:
    """A second `distort_intervals` operation over the same table resolves
    its population and rewrite against the first operation's output, not the
    operation-start state."""
    state = _state([_row("a7", 10, 20), _row("a7", 25, 35)])
    overlap_outcome = _apply(state, "overlap", rate=1.0)
    assert overlap_outcome.units_affected == 1
    lefts = (
        state.tables["membership__actor__oncall"]
        .data.column("left_sim_time")
        .to_pylist()
    )
    assert lefts == [30, 35]  # A rewritten against B's boundaries

    gap_outcome = _apply(state, "gap", rate=1.0, where={"joined_sim_time": "10"})
    assert gap_outcome.units_affected == 1
    lefts = (
        state.tables["membership__actor__oncall"]
        .data.column("left_sim_time")
        .to_pylist()
    )
    # 10 + floor((30 - 10) / 2) == 20, resolved against the first op's
    # output (30) -- not 15, which floor((20 - 10) / 2) would give against
    # the operation-start left_sim_time (20).
    assert lefts[0] == 20


def test_schema_drift_rename_then_timelines_group_on_evolved_schema() -> None:
    """An earlier schema_drift rename of an elem__ column does not disturb
    timeline identity -- element-field grouping reads whichever columns the
    evolved working schema currently carries, not a hard-coded name."""
    state = _state(
        [
            _row("a8", 10, 10, tag=None),
            _row("a8", 10, 10, tag="present"),
        ]
    )
    rename_op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="membership__actor__oncall"),
        rename_to={"elem__tag": "elem__renamed"},
    )
    SchemaDriftCorrupter().apply(
        state, rename_op, "rename_tag", random.Random(1), _FORK_PATH, _handler_sidecar()
    )
    working = state.tables["membership__actor__oncall"]
    assert "elem__renamed" in working.data.schema.names
    assert "elem__tag" not in working.data.schema.names

    timelines = enumerate_member_timelines(working.data, _FORK_PATH)
    assert len(timelines) == 2  # NULL vs "present" still group distinctly
    assert all(len(timeline) == 1 for timeline in timelines)
