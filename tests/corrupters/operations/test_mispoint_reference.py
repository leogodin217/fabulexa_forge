"""Tests for the `mispoint_reference` corrupter handler (unconstrained mode,
Phase 1, plus the `created_after_reference` constraint, Phase 2)."""

from __future__ import annotations

import random
from collections.abc import Sequence

import pytest

from fabulexa_forge.config.models import Amount, Correlated, MispointReference, Target
from fabulexa_forge.corrupters.operations.mispoint_reference import (
    MispointReferenceCorrupter,
    mispoint_impact,
    resolve_donor_pool,
    resolve_reference_write_anchor,
)
from fabulexa_forge.corrupters.state import CorruptState
from fabulexa_forge.errors import CorruptError
from fabulexa_forge.reader.sidecar import BranchEntry, Sidecar

from .._helpers import CallOrderRandom, column_spec, sidecar, table_spec, working_table

_FORK_PATH = "trunk"
_SLICE_AT = 100
_HANDLER = MispointReferenceCorrupter()


class _FixedRandomValues(random.Random):
    """A `random.Random` whose `.random()` returns a fixed sequence, cycling
    if exhausted -- pins `draw_weighted_sample`'s per-unit uniform draws so a
    placement-weighted test can target an exact winner (mirrors the
    `mutate_cells` test precedent; kept local since it is only needed here)."""

    def __init__(self, values: Sequence[float], seed: int = 0) -> None:
        super().__init__(seed)
        self._values = list(values)
        self._i = 0

    def random(self) -> float:
        value = self._values[self._i % len(self._values)]
        self._i += 1
        return value


# ---------------------------------------------------------------------------
# Fixture specs, state, sidecar
# ---------------------------------------------------------------------------


def _patient_spec() -> object:
    return table_spec(
        "records__patient",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("last_mutation_sim_time", "BIGINT"),
            column_spec(
                "prop__name", "VARCHAR", history_tracked=True, temporal_class="tracked"
            ),
            column_spec(
                "prop__doctor_id",
                "VARCHAR",
                references="doctor",
                history_tracked=True,
                temporal_class="tracked",
            ),
            column_spec("ref_index__doctor_id", "BIGINT"),
            column_spec("prop__untracked_doctor_id", "VARCHAR", references="doctor"),
            column_spec("ref_index__untracked_doctor_id", "BIGINT"),
        ),
        record_kind="patient",
    )


def _doctor_spec() -> object:
    return table_spec(
        "records__doctor",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("record_index", "BIGINT"),
            column_spec("created_sim_time", "BIGINT"),
        ),
        record_kind="doctor",
    )


def _membership_spec() -> object:
    return table_spec(
        "membership__patient__visits",
        "membership",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("joined_sim_time", "BIGINT"),
            column_spec("member__doctor__kind", "VARCHAR"),
            column_spec("member__doctor__id", "VARCHAR"),
        ),
        record_kind="patient",
        property_="visits",
    )


def _history_spec() -> object:
    return table_spec(
        "history",
        "fixed",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("kind", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("property", "VARCHAR"),
            column_spec("sim_time", "BIGINT"),
            column_spec("value", "VARCHAR"),
        ),
    )


def _sidecar(*, slice_at: int = _SLICE_AT) -> Sidecar:
    return sidecar(
        (_patient_spec(), _doctor_spec(), _membership_spec()),
        branches=(BranchEntry(fork_path=_FORK_PATH, parent=None, slice_at=slice_at),),
    )


def _doctors(ids: Sequence[str]) -> object:
    return working_table(
        _doctor_spec(),
        [
            {"fork_path": _FORK_PATH, "record_id": d, "record_index": i}
            for i, d in enumerate(ids)
        ],
    )


def _doctors_with_created(entries: Sequence[tuple[str, int]]) -> object:
    """A `records__doctor` working table with `created_sim_time` set per row --
    `entries` may repeat a `record_id` (duplicate rows), in which case the
    donor's creation time is the minimum among them."""
    return working_table(
        _doctor_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": d,
                "record_index": i,
                "created_sim_time": created,
            }
            for i, (d, created) in enumerate(entries)
        ],
    )


def _membership_row(
    record_id: str,
    *,
    kind: str | None = "doctor",
    id_: str | None,
    joined_sim_time: int = 5,
) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "record_id": record_id,
        "joined_sim_time": joined_sim_time,
        "member__doctor__kind": kind,
        "member__doctor__id": id_,
    }


def _apply(
    state: CorruptState, table: str, columns: list[str], count: int, seed: int = 1
) -> object:
    op = MispointReference(
        kind="mispoint_reference",
        target=Target(table=table, columns=columns),
        amount=Amount(count=count),
    )
    return _HANDLER.apply(
        state, op, "rule#0", random.Random(seed), _FORK_PATH, _sidecar()
    )


# ---------------------------------------------------------------------------
# Donor pool
# ---------------------------------------------------------------------------


def test_donor_pool_lexicographically_sorted() -> None:
    state = CorruptState(tables={"records__doctor": _doctors(["d3", "d1", "d2"])})
    pool = resolve_donor_pool(state, _FORK_PATH, "doctor", "d1", None)
    assert pool == ("d2", "d3")


def test_donor_pool_excludes_current_stored_id() -> None:
    state = CorruptState(tables={"records__doctor": _doctors(["d1", "d2"])})
    pool = resolve_donor_pool(state, _FORK_PATH, "doctor", "d1", None)
    assert pool == ("d2",)


def test_donor_pool_sentinel_id_trivially_excluded_when_current() -> None:
    """A previously-dangled sentinel id is eligible and its sentinel is
    trivially excluded from the pool -- only because it *is* current_id."""
    sentinel = "__dangling__0"
    state = CorruptState(tables={"records__doctor": _doctors(["d1", "d2", sentinel])})
    pool = resolve_donor_pool(state, _FORK_PATH, "doctor", sentinel, None)
    assert pool == ("d1", "d2")


def test_donor_pool_sentinel_id_is_a_legitimate_donor_otherwise() -> None:
    sentinel = "__dangling__0"
    state = CorruptState(tables={"records__doctor": _doctors(["d1", "d2", sentinel])})
    pool = resolve_donor_pool(state, _FORK_PATH, "doctor", "d1", None)
    assert sentinel in pool


def test_resolve_donor_pool_absent_target_table_raises_corrupt_error() -> None:
    state = CorruptState(tables={})
    with pytest.raises(CorruptError, match="records__doctor"):
        resolve_donor_pool(state, _FORK_PATH, "doctor", "d1", None)


# ---------------------------------------------------------------------------
# Constrained donor pool (`created_after_reference`)
# ---------------------------------------------------------------------------


def test_constrained_donor_pool_excludes_donor_created_at_or_before_anchor() -> None:
    """Boundary: a donor created exactly *at* the anchor is excluded --
    the constraint is a strict `>`."""
    state = CorruptState(
        tables={
            "records__doctor": _doctors_with_created(
                [("d1", 0), ("d2", 10), ("d3", 11)]
            )
        }
    )
    pool = resolve_donor_pool(state, _FORK_PATH, "doctor", "d1", 10)
    assert pool == ("d3",)


def test_constrained_donor_pool_creation_time_is_minimum_among_duplicate_rows() -> None:
    """A donor's creation time under exact duplicates is the minimum
    `created_sim_time` among its rows."""
    state = CorruptState(
        tables={
            "records__doctor": _doctors_with_created(
                [("d2", 20), ("d2", 5), ("d3", 12)]
            )
        }
    )
    pool = resolve_donor_pool(state, _FORK_PATH, "doctor", "d1", 10)
    assert pool == ("d3",)


def test_constrained_donor_pool_empty_when_no_donor_created_late_enough() -> None:
    state = CorruptState(
        tables={"records__doctor": _doctors_with_created([("d1", 0), ("d2", 5)])}
    )
    pool = resolve_donor_pool(state, _FORK_PATH, "doctor", "d1", 100)
    assert pool == ()


# ---------------------------------------------------------------------------
# Write anchor (`resolve_reference_write_anchor`)
# ---------------------------------------------------------------------------


def test_write_anchor_membership_id_uses_joined_sim_time() -> None:
    state = CorruptState(tables={})
    row = _membership_row("p1", id_="d1", joined_sim_time=42)
    anchor = resolve_reference_write_anchor(
        state,
        _FORK_PATH,
        _SLICE_AT,
        _membership_spec(),
        column_spec("member__doctor__id", "VARCHAR"),
        row,
    )
    assert anchor == 42


def test_write_anchor_tracked_reference_uses_c6_anchor_sim_time() -> None:
    """The C6 anchor's sim_time (10) wins over `last_mutation_sim_time` (25):
    it is the exact write time of the current value."""
    state = _patient_state(
        "d2",
        [
            {
                "fork_path": _FORK_PATH,
                "kind": "patient",
                "record_id": "p1",
                "property": "doctor_id",
                "sim_time": 10,
                "value": "d1",
            }
        ],
        last_mutation_sim_time=25,
    )
    columns_by_name = {col.name: col for col in _patient_spec().columns}
    row = {
        "fork_path": _FORK_PATH,
        "record_id": "p1",
        "last_mutation_sim_time": 25,
        "prop__name": "Alice",
        "prop__doctor_id": "d2",
        "prop__untracked_doctor_id": "d1",
    }
    anchor = resolve_reference_write_anchor(
        state,
        _FORK_PATH,
        _SLICE_AT,
        _patient_spec(),
        columns_by_name["prop__doctor_id"],
        row,
    )
    assert anchor == 10


def test_write_anchor_untracked_reference_uses_last_mutation_sim_time() -> None:
    state = _patient_state("d2", history_rows=None, last_mutation_sim_time=33)
    columns_by_name = {col.name: col for col in _patient_spec().columns}
    row = {
        "fork_path": _FORK_PATH,
        "record_id": "p1",
        "last_mutation_sim_time": 33,
        "prop__name": "Alice",
        "prop__doctor_id": "d1",
        "prop__untracked_doctor_id": "d2",
    }
    anchor = resolve_reference_write_anchor(
        state,
        _FORK_PATH,
        _SLICE_AT,
        _patient_spec(),
        columns_by_name["prop__untracked_doctor_id"],
        row,
    )
    assert anchor == 33


def test_write_anchor_tracked_reference_empty_c6_view_falls_back_to_last_mutation() -> (
    None
):
    state = _patient_state("d2", history_rows=[], last_mutation_sim_time=7)
    columns_by_name = {col.name: col for col in _patient_spec().columns}
    row = {
        "fork_path": _FORK_PATH,
        "record_id": "p1",
        "last_mutation_sim_time": 7,
        "prop__name": "Alice",
        "prop__doctor_id": "d2",
        "prop__untracked_doctor_id": "d1",
    }
    anchor = resolve_reference_write_anchor(
        state,
        _FORK_PATH,
        _SLICE_AT,
        _patient_spec(),
        columns_by_name["prop__doctor_id"],
        row,
    )
    assert anchor == 7


def test_write_anchor_reads_the_latest_pre_slice_working_history_row() -> None:
    """An earlier family-C rewrite of the series' events moves the anchor:
    resolution reads the working `history` state as of the operation's
    start, not a fixed early row."""
    state = _patient_state(
        "d2",
        [
            {
                "fork_path": _FORK_PATH,
                "kind": "patient",
                "record_id": "p1",
                "property": "doctor_id",
                "sim_time": 10,
                "value": "d1",
            },
            {
                "fork_path": _FORK_PATH,
                "kind": "patient",
                "record_id": "p1",
                "property": "doctor_id",
                "sim_time": 25,
                "value": "d2",
            },
        ],
        last_mutation_sim_time=25,
    )
    columns_by_name = {col.name: col for col in _patient_spec().columns}
    row = {
        "fork_path": _FORK_PATH,
        "record_id": "p1",
        "last_mutation_sim_time": 25,
        "prop__name": "Alice",
        "prop__doctor_id": "d2",
        "prop__untracked_doctor_id": "d1",
    }
    anchor = resolve_reference_write_anchor(
        state,
        _FORK_PATH,
        _SLICE_AT,
        _patient_spec(),
        columns_by_name["prop__doctor_id"],
        row,
    )
    assert anchor == 25


def test_write_anchor_tracked_reference_missing_history_raises_corrupt_error() -> None:
    state = _patient_state("d2", history_rows=None)
    columns_by_name = {col.name: col for col in _patient_spec().columns}
    row = {
        "fork_path": _FORK_PATH,
        "record_id": "p1",
        "last_mutation_sim_time": 7,
        "prop__name": "Alice",
        "prop__doctor_id": "d2",
        "prop__untracked_doctor_id": "d1",
    }
    with pytest.raises(CorruptError, match="history"):
        resolve_reference_write_anchor(
            state,
            _FORK_PATH,
            _SLICE_AT,
            _patient_spec(),
            columns_by_name["prop__doctor_id"],
            row,
        )


def test_write_anchor_missing_lifecycle_column_raises_corrupt_error() -> None:
    state = _patient_state("d2", history_rows=None)
    columns_by_name = {col.name: col for col in _patient_spec().columns}
    row = {
        "fork_path": _FORK_PATH,
        "record_id": "p1",
        "prop__name": "Alice",
        "prop__doctor_id": "d1",
        "prop__untracked_doctor_id": "d2",
        # last_mutation_sim_time deliberately omitted.
    }
    with pytest.raises(CorruptError, match="last_mutation_sim_time"):
        resolve_reference_write_anchor(
            state,
            _FORK_PATH,
            _SLICE_AT,
            _patient_spec(),
            columns_by_name["prop__untracked_doctor_id"],
            row,
        )


def test_write_anchor_membership_missing_joined_sim_time_raises_corrupt_error() -> None:
    state = CorruptState(tables={})
    row = {
        "fork_path": _FORK_PATH,
        "record_id": "p1",
        "member__doctor__kind": "doctor",
        "member__doctor__id": "d1",
        # joined_sim_time deliberately omitted.
    }
    with pytest.raises(CorruptError, match="joined_sim_time"):
        resolve_reference_write_anchor(
            state,
            _FORK_PATH,
            _SLICE_AT,
            _membership_spec(),
            column_spec("member__doctor__id", "VARCHAR"),
            row,
        )


# ---------------------------------------------------------------------------
# Population filters
# ---------------------------------------------------------------------------


def test_null_id_row_excluded_from_population() -> None:
    membership = working_table(_membership_spec(), [_membership_row("p1", id_=None)])
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": _doctors(["d1", "d2"]),
        }
    )
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=1
    )
    assert outcome.units_selected == 0
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_null_partner_kind_excludes_row_from_population() -> None:
    membership = working_table(
        _membership_spec(), [_membership_row("p1", kind=None, id_="d1")]
    )
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": _doctors(["d1", "d2"]),
        }
    )
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=1
    )
    assert outcome.units_selected == 0
    assert outcome.defects == ()


def test_absent_target_records_table_excludes_row_all_excluded_is_noop() -> None:
    membership = working_table(_membership_spec(), [_membership_row("p1", id_="d1")])
    state = CorruptState(tables={"membership__patient__visits": membership})
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=1
    )
    assert outcome.units_selected == 0
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_no_other_donor_excludes_row_new_filter() -> None:
    """Filter 4 (new): the target table holds no *other* id -- an empty
    donor pool population-filters the cell, never an error."""
    membership = working_table(_membership_spec(), [_membership_row("p1", id_="d1")])
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": _doctors(["d1"]),
        }
    )
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=1
    )
    assert outcome.units_selected == 0
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_all_units_filtered_out_across_all_four_filters_is_noop() -> None:
    membership = working_table(
        _membership_spec(),
        [
            _membership_row("p1", id_="d1"),  # filter 4: no other doctor donor
            _membership_row(
                "p2", kind="nurse", id_="n1"
            ),  # filter 3: no records__nurse
            _membership_row("p3", id_=None),  # filter 1: NULL id
            _membership_row("p4", kind=None, id_="d1"),  # filter 2: NULL kind partner
        ],
    )
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": _doctors(["d1"]),
        }
    )
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=4
    )
    assert outcome.units_selected == 0
    assert outcome.units_affected == 0
    assert outcome.defects == ()


# ---------------------------------------------------------------------------
# Draw discipline: slot (3) -- one randrange per selected unit
# ---------------------------------------------------------------------------


def test_slot_three_one_randrange_per_selected_unit_after_unit_draw() -> None:
    membership = working_table(
        _membership_spec(),
        [_membership_row("p1", id_="d1"), _membership_row("p2", id_="d1")],
    )
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": _doctors(["d1", "d2", "d3"]),
        }
    )
    op = MispointReference(
        kind="mispoint_reference",
        target=Target(
            table="membership__patient__visits", columns=["member__doctor__id"]
        ),
        amount=Amount(count=2),
    )
    rng = CallOrderRandom(seed=3)
    outcome = _HANDLER.apply(state, op, "rule#0", rng, _FORK_PATH, _sidecar())
    assert outcome.units_selected == 2
    # CallOrderRandom overrides .random() without overriding .getrandbits(),
    # so CPython's internal _randbelow_without_getrandbits fallback re-enters
    # .random() incidentally inside both .sample() and .randrange() -- filter
    # that noise to see the meaningful sequence: one placement-free unit
    # draw, then one randrange per selected unit.
    meaningful_calls = [call for call in rng.calls if call != "random"]
    assert meaningful_calls == ["sample", "randrange", "randrange"]


# ---------------------------------------------------------------------------
# Rewrite: id-only, resolution by construction, reads-before-writes
# ---------------------------------------------------------------------------


def test_only_id_cell_rewritten_kind_partner_untouched() -> None:
    membership = working_table(_membership_spec(), [_membership_row("p1", id_="d1")])
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": _doctors(["d1", "d2"]),
        }
    )
    _apply(state, "membership__patient__visits", ["member__doctor__id"], count=1)
    mutated = state.tables["membership__patient__visits"].data
    assert mutated.column("member__doctor__id").to_pylist() == ["d2"]
    assert mutated.column("member__doctor__kind").to_pylist() == ["doctor"]


def test_rewritten_id_resolves_in_working_target_table() -> None:
    membership = working_table(_membership_spec(), [_membership_row("p1", id_="d1")])
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": _doctors(["d1", "d2", "d3"]),
        }
    )
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=1
    )
    donor_ids = set(
        state.tables["records__doctor"].data.column("record_id").to_pylist()
    )
    rewritten = (
        state.tables["membership__patient__visits"]
        .data.column("member__doctor__id")[0]
        .as_py()
    )
    assert rewritten in donor_ids
    assert outcome.units_affected == 1


# ---------------------------------------------------------------------------
# Pair-scoped reference writes: a records reference prop__ cell's
# mispoint co-points its ref_index__ sibling to the donor's record_index
# ---------------------------------------------------------------------------


def test_records_reference_prop_cell_copoints_ref_index_sibling() -> None:
    """Pool excludes the current id ('d1'), leaving the sole donor 'd2' --
    a deterministic draw regardless of seed."""
    state = _patient_state("d1", history_rows=[], doctors=_doctors(["d1", "d2"]))
    outcome = _apply(state, "records__patient", ["prop__doctor_id"], count=1)
    assert outcome.units_affected == 1
    mutated = state.tables["records__patient"].data
    assert mutated.column("prop__doctor_id").to_pylist() == ["d2"]
    assert mutated.column("ref_index__doctor_id").to_pylist() == [1]


def test_constrained_records_reference_copoints_ref_index_sibling() -> None:
    patients = working_table(
        _patient_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "last_mutation_sim_time": 10,
                "prop__name": "Alice",
                "prop__doctor_id": "d1",
                "prop__untracked_doctor_id": "d1",
            }
        ],
    )
    state = CorruptState(
        tables={
            "records__patient": patients,
            "records__doctor": _doctors_with_created([("d1", 0), ("d2", 20)]),
            "history": working_table(_history_spec(), []),
        }
    )
    op = MispointReference(
        kind="mispoint_reference",
        target=Target(table="records__patient", columns=["prop__doctor_id"]),
        amount=Amount(count=1),
        constraint="created_after_reference",
    )
    outcome = _HANDLER.apply(
        state, op, "rule#0", random.Random(1), _FORK_PATH, _sidecar()
    )
    assert outcome.units_affected == 1
    mutated = state.tables["records__patient"].data
    assert mutated.column("prop__doctor_id").to_pylist() == ["d2"]
    assert mutated.column("ref_index__doctor_id").to_pylist() == [1]


def test_membership_member_id_mispoint_has_no_sibling_write() -> None:
    """No `ref_index__` analog on membership reference pairs -- the write
    stays scoped to the id column, exactly as today."""
    membership = working_table(_membership_spec(), [_membership_row("p1", id_="d1")])
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": _doctors(["d1", "d2"]),
        }
    )
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=1
    )
    assert outcome.units_affected == 1
    mutated = state.tables["membership__patient__visits"].data
    assert mutated.column("member__doctor__id").to_pylist() == ["d2"]


def test_two_selected_cells_may_draw_the_same_donor_not_shrunk_by_sibling() -> None:
    """Both cells' donor pools are resolved before any write-back, so the
    sole donor is available to both -- neither observes the other's rewrite."""
    membership = working_table(
        _membership_spec(),
        [_membership_row("p1", id_="d1"), _membership_row("p2", id_="d1")],
    )
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": _doctors(["d1", "d2"]),
        }
    )
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=2
    )
    assert outcome.units_selected == 2
    assert outcome.units_affected == 2
    assert len(outcome.defects) == 2
    mutated = state.tables["membership__patient__visits"].data
    assert mutated.column("member__doctor__id").to_pylist() == ["d2", "d2"]


# ---------------------------------------------------------------------------
# Impact
# ---------------------------------------------------------------------------


def _patient_state(
    prop_doctor_id: str,
    history_rows: list[dict[str, object]] | None = None,
    *,
    last_mutation_sim_time: int = 20,
    doctors: object | None = None,
) -> CorruptState:
    patients = working_table(
        _patient_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "last_mutation_sim_time": last_mutation_sim_time,
                "prop__name": "Alice",
                "prop__doctor_id": prop_doctor_id,
                "prop__untracked_doctor_id": "d1",
            }
        ],
    )
    tables: dict[str, object] = {
        "records__patient": patients,
        "records__doctor": doctors if doctors is not None else _doctors(["d1", "d2"]),
    }
    if history_rows is not None:
        tables["history"] = working_table(_history_spec(), history_rows)
    return CorruptState(tables=tables)


def test_membership_id_declares_beyond_c1_c12() -> None:
    """The membership path never reads `history` and cannot raise -- an
    empty state (no tables at all) proves it."""
    state = CorruptState(tables={})
    impact = mispoint_impact(
        state,
        "member__doctor__id",
        column_spec("member__doctor__id", "VARCHAR"),
        _membership_spec(),
        _FORK_PATH,
        _SLICE_AT,
        "p1",
    )
    assert impact == ("beyond-c1-c12",)


def test_tracked_prop_reference_post_write_round_trip_fails_declares_c6() -> None:
    """The post-write cell ("d2") diverges from the series' anchor value
    ("d1") -- C6."""
    state = _patient_state(
        "d2",
        [
            {
                "fork_path": _FORK_PATH,
                "kind": "patient",
                "record_id": "p1",
                "property": "doctor_id",
                "sim_time": 10,
                "value": "d1",
            }
        ],
    )
    columns_by_name = {col.name: col for col in _patient_spec().columns}
    impact = mispoint_impact(
        state,
        "prop__doctor_id",
        columns_by_name["prop__doctor_id"],
        _patient_spec(),
        _FORK_PATH,
        _SLICE_AT,
        "p1",
    )
    assert impact == ("C6",)


def test_donor_equal_to_anchor_declares_beyond_c1_c12_heal_case() -> None:
    """The donor coincidentally equals the series' anchor value -- the
    post-write round trip succeeds (actual-divergence stance): not C6."""
    state = _patient_state(
        "d2",
        [
            {
                "fork_path": _FORK_PATH,
                "kind": "patient",
                "record_id": "p1",
                "property": "doctor_id",
                "sim_time": 10,
                "value": "d2",
            }
        ],
    )
    columns_by_name = {col.name: col for col in _patient_spec().columns}
    impact = mispoint_impact(
        state,
        "prop__doctor_id",
        columns_by_name["prop__doctor_id"],
        _patient_spec(),
        _FORK_PATH,
        _SLICE_AT,
        "p1",
    )
    assert impact == ("beyond-c1-c12",)


def test_untracked_prop_reference_declares_beyond_c1_c12() -> None:
    """The untracked path never reads `history` and cannot raise -- no
    `history` table in the state proves it."""
    state = _patient_state("d2", history_rows=None)
    columns_by_name = {col.name: col for col in _patient_spec().columns}
    impact = mispoint_impact(
        state,
        "prop__untracked_doctor_id",
        columns_by_name["prop__untracked_doctor_id"],
        _patient_spec(),
        _FORK_PATH,
        _SLICE_AT,
        "p1",
    )
    assert impact == ("beyond-c1-c12",)


def test_tracked_prop_reference_missing_history_table_raises_corrupt_error() -> None:
    state = _patient_state("d2", history_rows=None)
    columns_by_name = {col.name: col for col in _patient_spec().columns}
    with pytest.raises(CorruptError, match="history"):
        mispoint_impact(
            state,
            "prop__doctor_id",
            columns_by_name["prop__doctor_id"],
            _patient_spec(),
            _FORK_PATH,
            _SLICE_AT,
            "p1",
        )


# ---------------------------------------------------------------------------
# Mechanics
# ---------------------------------------------------------------------------


def test_units_affected_equals_units_selected_equals_len_defects() -> None:
    membership = working_table(_membership_spec(), [_membership_row("p1", id_="d1")])
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": _doctors(["d1", "d2"]),
        }
    )
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=1
    )
    assert outcome.units_selected == outcome.units_affected == len(outcome.defects)


def test_rerun_with_same_seed_is_identical() -> None:
    def _state() -> CorruptState:
        membership = working_table(
            _membership_spec(),
            [_membership_row("p1", id_="d1"), _membership_row("p2", id_="d1")],
        )
        return CorruptState(
            tables={
                "membership__patient__visits": membership,
                "records__doctor": _doctors(["d1", "d2", "d3"]),
            }
        )

    state_a, state_b = _state(), _state()
    outcome_a = _apply(
        state_a, "membership__patient__visits", ["member__doctor__id"], count=2, seed=9
    )
    outcome_b = _apply(
        state_b, "membership__patient__visits", ["member__doctor__id"], count=2, seed=9
    )
    assert outcome_a.defects == outcome_b.defects
    assert state_a.tables["membership__patient__visits"].data.equals(
        state_b.tables["membership__patient__visits"].data
    )


def test_correlated_placement_weights_the_draw() -> None:
    """p2's prop__name matches the correlated condition and carries a heavy
    weight; with u=0.5 for both units, its key beats p1's, so p2 is the sole
    draw regardless of row order."""
    patients = working_table(
        _patient_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "prop__name": "OTHER",
                "prop__doctor_id": "d1",
                "prop__untracked_doctor_id": "d1",
            },
            {
                "fork_path": _FORK_PATH,
                "record_id": "p2",
                "prop__name": "MATCH",
                "prop__doctor_id": "d1",
                "prop__untracked_doctor_id": "d1",
            },
        ],
    )
    state = CorruptState(
        tables={
            "records__patient": patients,
            "records__doctor": _doctors(["d1", "d2"]),
            "history": working_table(_history_spec(), []),
        }
    )
    op = MispointReference(
        kind="mispoint_reference",
        target=Target(table="records__patient", columns=["prop__doctor_id"]),
        amount=Amount(count=1),
        placement=Correlated(
            kind="correlated", column="prop__name", value="MATCH", weight=100.0
        ),
    )
    outcome = _HANDLER.apply(
        state, op, "rule#0", _FixedRandomValues([0.5, 0.5]), _FORK_PATH, _sidecar()
    )
    assert outcome.units_selected == 1
    row_keys = dict(outcome.defects[0].location.row.keys)
    assert row_keys["record_id"] == "p2"
    mutated = state.tables["records__patient"].data
    doctor_ids = dict(
        zip(
            mutated.column("record_id").to_pylist(),
            mutated.column("prop__doctor_id").to_pylist(),
        )
    )
    assert doctor_ids == {"p1": "d1", "p2": "d2"}


def test_where_narrows_the_population() -> None:
    membership = working_table(
        _membership_spec(),
        [_membership_row("p1", id_="d1"), _membership_row("p2", id_="d1")],
    )
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": _doctors(["d1", "d2"]),
        }
    )
    op = MispointReference(
        kind="mispoint_reference",
        target=Target(
            table="membership__patient__visits",
            columns=["member__doctor__id"],
            where={"record_id": "p1"},
        ),
        amount=Amount(count=1),
    )
    outcome = _HANDLER.apply(
        state, op, "rule#0", random.Random(1), _FORK_PATH, _sidecar()
    )
    assert outcome.units_selected == 1
    row_keys = dict(outcome.defects[0].location.row.keys)
    assert row_keys["record_id"] == "p1"
    mutated = state.tables["membership__patient__visits"].data
    ids = dict(
        zip(
            mutated.column("record_id").to_pylist(),
            mutated.column("member__doctor__id").to_pylist(),
        )
    )
    assert ids == {"p1": "d2", "p2": "d1"}


def test_membership_locator_is_cell_kind_row_ref_category_membership_excludes_member_column() -> (
    None
):
    membership = working_table(_membership_spec(), [_membership_row("p1", id_="d1")])
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": _doctors(["d1", "d2"]),
        }
    )
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=1
    )
    defect = outcome.defects[0]
    assert defect.defect_class == "mispointed_reference"
    assert defect.location.kind == "cell"
    assert defect.location.table == "membership__patient__visits"
    assert defect.location.column == "member__doctor__id"
    assert defect.location.row.category == "membership"
    key_names = {name for name, _ in defect.location.row.keys}
    assert key_names == {"fork_path", "record_id", "joined_sim_time"}


# ---------------------------------------------------------------------------
# Constraint (`created_after_reference`): class flip, filter 4, determinism
# ---------------------------------------------------------------------------


def test_constrained_empty_pool_filters_unit_noop() -> None:
    """Boundary: the sole other donor was created at the anchor, not after
    it -- filter 4, a data-dependent no-op."""
    membership = working_table(
        _membership_spec(), [_membership_row("p1", id_="d1", joined_sim_time=100)]
    )
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": _doctors_with_created([("d1", 0), ("d2", 5)]),
        }
    )
    op = MispointReference(
        kind="mispoint_reference",
        target=Target(
            table="membership__patient__visits", columns=["member__doctor__id"]
        ),
        amount=Amount(count=1),
        constraint="created_after_reference",
    )
    outcome = _HANDLER.apply(
        state, op, "rule#0", random.Random(1), _FORK_PATH, _sidecar()
    )
    assert outcome.units_selected == 0
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_constrained_membership_mispoint_declares_point_in_time_class_and_beyond_c1_c12() -> (
    None
):
    """A constrained membership mis-point still declares `beyond-c1-c12`
    (impact unchanged by the constraint), but the defect class flips."""
    membership = working_table(
        _membership_spec(), [_membership_row("p1", id_="d1", joined_sim_time=5)]
    )
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": _doctors_with_created([("d1", 0), ("d2", 10)]),
        }
    )
    op = MispointReference(
        kind="mispoint_reference",
        target=Target(
            table="membership__patient__visits", columns=["member__doctor__id"]
        ),
        amount=Amount(count=1),
        constraint="created_after_reference",
    )
    outcome = _HANDLER.apply(
        state, op, "rule#0", random.Random(1), _FORK_PATH, _sidecar()
    )
    assert outcome.units_affected == 1
    defect = outcome.defects[0]
    assert defect.defect_class == "point_in_time_dangling_reference"
    assert defect.impact == ("beyond-c1-c12",)
    rewritten = (
        state.tables["membership__patient__visits"]
        .data.column("member__doctor__id")[0]
        .as_py()
    )
    assert rewritten == "d2"


def test_constrained_tracked_records_mispoint_declares_c6_on_actual_divergence() -> (
    None
):
    """A constrained tracked records mis-point still declares `C6` on actual
    divergence -- impact is unchanged by the constraint."""
    patients = working_table(
        _patient_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "last_mutation_sim_time": 10,
                "prop__name": "Alice",
                "prop__doctor_id": "d1",
                "prop__untracked_doctor_id": "d1",
            }
        ],
    )
    state = CorruptState(
        tables={
            "records__patient": patients,
            "records__doctor": _doctors_with_created([("d1", 0), ("d2", 20)]),
            "history": working_table(
                _history_spec(),
                [
                    {
                        "fork_path": _FORK_PATH,
                        "kind": "patient",
                        "record_id": "p1",
                        "property": "doctor_id",
                        "sim_time": 10,
                        "value": "d1",
                    }
                ],
            ),
        }
    )
    op = MispointReference(
        kind="mispoint_reference",
        target=Target(table="records__patient", columns=["prop__doctor_id"]),
        amount=Amount(count=1),
        constraint="created_after_reference",
    )
    outcome = _HANDLER.apply(
        state, op, "rule#0", random.Random(1), _FORK_PATH, _sidecar()
    )
    assert outcome.units_affected == 1
    defect = outcome.defects[0]
    assert defect.defect_class == "point_in_time_dangling_reference"
    assert defect.impact == ("C6",)


def test_constraint_does_not_change_slot_three_rng_order() -> None:
    """Anchor resolution and constrained pool filtering read state only --
    no RNG calls -- so slot (3)'s order is unchanged by the constraint."""
    membership = working_table(
        _membership_spec(),
        [
            _membership_row("p1", id_="d1", joined_sim_time=1),
            _membership_row("p2", id_="d1", joined_sim_time=1),
        ],
    )
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": _doctors_with_created([("d1", 0), ("d2", 5), ("d3", 5)]),
        }
    )
    op = MispointReference(
        kind="mispoint_reference",
        target=Target(
            table="membership__patient__visits", columns=["member__doctor__id"]
        ),
        amount=Amount(count=2),
        constraint="created_after_reference",
    )
    rng = CallOrderRandom(seed=3)
    outcome = _HANDLER.apply(state, op, "rule#0", rng, _FORK_PATH, _sidecar())
    assert outcome.units_selected == 2
    meaningful_calls = [call for call in rng.calls if call != "random"]
    assert meaningful_calls == ["sample", "randrange", "randrange"]


def test_rerun_with_mixed_constrained_and_unconstrained_operations_is_identical() -> (
    None
):
    def _run() -> tuple[CorruptState, tuple[object, object]]:
        membership = working_table(
            _membership_spec(),
            [
                _membership_row("p1", id_="d1", joined_sim_time=1),
                _membership_row("p2", id_="d1", joined_sim_time=1),
            ],
        )
        state = CorruptState(
            tables={
                "membership__patient__visits": membership,
                "records__doctor": _doctors_with_created(
                    [("d1", 0), ("d2", 5), ("d3", 8)]
                ),
            }
        )
        op_unconstrained = MispointReference(
            kind="mispoint_reference",
            target=Target(
                table="membership__patient__visits",
                columns=["member__doctor__id"],
                where={"record_id": "p1"},
            ),
            amount=Amount(count=1),
        )
        op_constrained = MispointReference(
            kind="mispoint_reference",
            target=Target(
                table="membership__patient__visits",
                columns=["member__doctor__id"],
                where={"record_id": "p2"},
            ),
            amount=Amount(count=1),
            constraint="created_after_reference",
        )
        outcome_a = _HANDLER.apply(
            state, op_unconstrained, "rule#0", random.Random(7), _FORK_PATH, _sidecar()
        )
        outcome_b = _HANDLER.apply(
            state, op_constrained, "rule#1", random.Random(7), _FORK_PATH, _sidecar()
        )
        return state, (outcome_a.defects, outcome_b.defects)

    state_a, defects_a = _run()
    state_b, defects_b = _run()
    assert defects_a == defects_b
    assert state_a.tables["membership__patient__visits"].data.equals(
        state_b.tables["membership__patient__visits"].data
    )
