"""Tests for the `duplicate_rows` corrupter handler."""

from __future__ import annotations

import random

import pyarrow as pa
import pytest

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import (
    Amount,
    Distribution,
    DuplicateRows,
    EntityScoped,
    MutationCase,
    MutationResample,
    MutationSentinel,
    MutationTypo,
    NullCells,
    Target,
)
from fabulexa_forge.corrupters.operations._mutations import (
    apply_typo_int,
    apply_typo_str,
)
from fabulexa_forge.corrupters.operations.duplicate_rows import DuplicateRowsCorrupter
from fabulexa_forge.corrupters.operations.null_cells import NullCellsCorrupter
from fabulexa_forge.corrupters.state import CorruptState
from fabulexa_forge.errors import CorruptValidationError
from fabulexa_forge.reader.sidecar import BranchEntry, RecordRoles, Sidecar

from .._helpers import (
    FixedSampleRandom,
    column_spec,
    sidecar,
    table_spec,
    working_table,
)

_FORK_PATH = "trunk"
_DUP = DuplicateRowsCorrupter()
_NULL = NullCellsCorrupter()


def _patient_spec() -> "object":
    return table_spec(
        "records__patient",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("prop__age", "BIGINT", history_tracked=True),
            column_spec("prop__score", "DOUBLE", history_tracked=True),
        ),
        record_kind="patient",
    )


def _history_spec() -> "object":
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


def _doctor_spec() -> "object":
    return table_spec(
        "records__doctor",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("prop__name", "VARCHAR"),
        ),
        record_kind="doctor",
    )


def _history_row(record_id: str, property_: str, value: str) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "kind": "patient",
        "record_id": record_id,
        "property": property_,
        "sim_time": 10,
        "value": value,
    }


def _patient_row(record_id: str, age: object, score: object) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "record_id": record_id,
        "prop__age": age,
        "prop__score": score,
    }


def _state(
    rows: list[dict[str, object]], history_rows: list[dict[str, object]]
) -> CorruptState:
    return CorruptState(
        tables={
            "history": working_table(_history_spec(), history_rows),
            "records__patient": working_table(_patient_spec(), rows),
        }
    )


def _op(
    table: str,
    amount: Amount,
    *,
    where: dict[str, str] | None = None,
    columns: list[str] | None = None,
    jitter: Distribution | None = None,
    placement: EntityScoped | None = None,
) -> DuplicateRows:
    return DuplicateRows(
        kind="duplicate_rows",
        target=Target(table=table, where=where, columns=columns),
        amount=amount,
        jitter=jitter,
        placement=placement,
    )


def _apply(state: CorruptState, op: DuplicateRows, sc: object, seed: int = 1) -> object:
    return _DUP.apply(state, op, "rule#0", random.Random(seed), _FORK_PATH, sc)


def _apply_with_rng(
    state: CorruptState, op: DuplicateRows, sc: object, rng: random.Random
) -> object:
    return _DUP.apply(state, op, "rule#0", rng, _FORK_PATH, sc)


# ---------------------------------------------------------------------------
# Exact duplicates: C9, non-pinned, history, and multiplicity
# ---------------------------------------------------------------------------


def test_exact_duplicate_of_pinned_records_row_declares_c9() -> None:
    state = _state(
        [_patient_row("p1", 30, 1.5)],
        [_history_row("p1", "age", "30")],
    )
    sc = sidecar((_patient_spec(),), pinned_ids={"patient": {"alice": "p1"}})
    op = _op("records__patient", Amount(count=1))
    outcome = _apply(state, op, sc)
    assert outcome.units_selected == 1
    assert outcome.units_affected == 1
    assert outcome.defects[0].defect_class == "duplicate_row"
    assert outcome.defects[0].impact == ("C9",)
    assert state.tables["records__patient"].data.num_rows == 2


def test_exact_duplicate_of_non_pinned_row_declares_beyond_c1_c12() -> None:
    state = _state(
        [_patient_row("p2", 40, 2.5)],
        [],
    )
    sc = sidecar((_patient_spec(),), pinned_ids={"patient": {"alice": "p1"}})
    op = _op("records__patient", Amount(count=1))
    outcome = _apply(state, op, sc)
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


def test_exact_duplicate_of_history_row_declares_beyond_c1_c12() -> None:
    state = _state([], [_history_row("p1", "age", "30")])
    sc = sidecar(
        (_patient_spec(), _history_spec()), pinned_ids={"patient": {"alice": "p1"}}
    )
    op = _op("history", Amount(count=1))
    outcome = _apply(state, op, sc)
    assert outcome.defects[0].defect_class == "duplicate_row"
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


def test_count_exceeding_identical_rows_produces_multiplicity_at_one_row_locator() -> (
    None
):
    tick = _history_row("p1", "age", "30")
    state = _state([], [tick, dict(tick), dict(tick)])
    sc = sidecar((_patient_spec(), _history_spec()))
    op = _op("history", Amount(count=3))
    outcome = _apply(state, op, sc)
    assert outcome.units_selected == 3
    assert len(outcome.defects) == 3
    locations = {d.location.row.keys for d in outcome.defects}
    assert locations == {
        (
            ("fork_path", "trunk"),
            ("kind", "patient"),
            ("record_id", "p1"),
            ("property", "age"),
            ("sim_time", "10"),
        )
    }
    assert state.tables["history"].data.num_rows == 6


# ---------------------------------------------------------------------------
# Near-duplicates (jitter)
# ---------------------------------------------------------------------------


def test_jitter_double_stores_value_plus_delta() -> None:
    state = _state([_patient_row("p1", 30, 1.5)], [_history_row("p1", "score", "1.5")])
    sc = sidecar((_patient_spec(),))
    jitter = Distribution(shape="uniform", low=1.0, high=1.0)
    op = _op(
        "records__patient", Amount(count=1), columns=["prop__score"], jitter=jitter
    )
    outcome = _apply(state, op, sc, seed=3)
    assert state.tables["records__patient"].data.column("prop__score").to_pylist() == [
        1.5,
        2.5,
    ]
    assert outcome.defects[0].defect_class == "near_duplicate_row"
    assert outcome.defects[0].impact == ("C6",)


def test_jitter_bigint_rounds_half_to_even_and_keeps_type() -> None:
    state = _state([_patient_row("p1", 30, 1.5)], [_history_row("p1", "age", "30")])
    sc = sidecar((_patient_spec(),))
    jitter = Distribution(shape="uniform", low=4.5, high=4.5)
    op = _op("records__patient", Amount(count=1), columns=["prop__age"], jitter=jitter)
    outcome = _apply(state, op, sc, seed=3)
    mutated = state.tables["records__patient"].data
    assert mutated.column("prop__age").to_pylist() == [30, 34]  # round(34.5) -> 34
    assert mutated.schema.field("prop__age").type.equals(pa.int64())
    assert outcome.defects[0].impact == ("C6",)


def test_jitter_null_cell_stays_null_and_consumes_no_delta() -> None:
    state = _state(
        [_patient_row("p1", None, 1.5)], [_history_row("p1", "score", "1.5")]
    )
    sc = sidecar((_patient_spec(),))
    jitter = Distribution(shape="uniform", low=0.0, high=10.0)
    op = _op(
        "records__patient",
        Amount(count=1),
        columns=["prop__age", "prop__score"],
        jitter=jitter,
    )
    outcome = _apply(state, op, sc, seed=3)
    mutated = state.tables["records__patient"].data
    assert mutated.column("prop__age").to_pylist() == [None, None]
    # draw_sample's own rng.sample() consumption precedes the delta stream;
    # only ONE delta is then drawn (for prop__score) -- the NULL prop__age
    # cell consumes none, so this is the very next uniform() call.
    expected_rng = random.Random(3)
    expected_rng.sample(range(1), 1)
    expected_delta = expected_rng.uniform(0.0, 10.0)
    assert mutated.column("prop__score").to_pylist() == [1.5, 1.5 + expected_delta]
    assert outcome.defects[0].impact == ("C6",)


def test_jitter_vanishing_delta_still_injects_copy_without_c6() -> None:
    state = _state([_patient_row("p1", 30, 1.5)], [_history_row("p1", "age", "30")])
    sc = sidecar((_patient_spec(),))
    jitter = Distribution(shape="uniform", low=0.2, high=0.2)
    op = _op("records__patient", Amount(count=1), columns=["prop__age"], jitter=jitter)
    outcome = _apply(state, op, sc, seed=3)
    mutated = state.tables["records__patient"].data
    assert mutated.column("prop__age").to_pylist() == [30, 30]
    assert outcome.units_affected == 1
    assert len(outcome.defects) == 1
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


def test_jitter_deltas_drawn_canonical_row_then_column_order() -> None:
    state = _state(
        [_patient_row("p1", 10, 1.0), _patient_row("p2", 20, 2.0)],
        [_history_row("p1", "age", "10"), _history_row("p2", "age", "20")],
    )
    sc = sidecar((_patient_spec(),))
    jitter = Distribution(shape="uniform", low=0.0, high=10.0)
    op = _op(
        "records__patient",
        Amount(count=2),
        columns=["prop__age", "prop__score"],
        jitter=jitter,
    )
    outcome = _apply(state, op, sc, seed=11)
    expected_rng = random.Random(11)
    expected_rng.sample(range(2), 2)  # draw_sample's own rng consumption, first
    d_p1_age = round(10 + expected_rng.uniform(0.0, 10.0))
    d_p1_score = 1.0 + expected_rng.uniform(0.0, 10.0)
    d_p2_age = round(20 + expected_rng.uniform(0.0, 10.0))
    d_p2_score = 2.0 + expected_rng.uniform(0.0, 10.0)
    mutated = state.tables["records__patient"].data
    ages = mutated.column("prop__age").to_pylist()[2:]
    scores = mutated.column("prop__score").to_pylist()[2:]
    assert sorted(ages) == sorted([d_p1_age, d_p2_age])
    assert sorted(scores) == sorted([d_p1_score, d_p2_score])
    assert outcome.units_selected == 2


def test_jitter_c9_recomputed_not_inherited_from_exact() -> None:
    state = _state([_patient_row("p1", 30, 1.5)], [])
    sc = sidecar((_patient_spec(),), pinned_ids={"patient": {"alice": "p1"}})
    jitter = Distribution(shape="uniform", low=0.0, high=0.0)
    op = _op("records__patient", Amount(count=1), columns=["prop__age"], jitter=jitter)
    outcome = _apply(state, op, sc, seed=3)
    assert outcome.defects[0].impact == ("C9",)


# ---------------------------------------------------------------------------
# Break locality, determinism, and cross-handler working-state reads
# ---------------------------------------------------------------------------


def test_only_target_table_entry_replaced() -> None:
    state = _state([_patient_row("p1", 30, 1.5)], [_history_row("p1", "age", "30")])
    sc = sidecar((_patient_spec(),))
    other = state.tables["history"]
    _apply(state, _op("records__patient", Amount(count=1)), sc)
    assert state.tables["history"] is other


def test_rerun_with_same_seed_is_identical() -> None:
    sc = sidecar((_patient_spec(),), pinned_ids={"patient": {"alice": "p1"}})
    state_a = _state([_patient_row("p1", 30, 1.5)], [])
    state_b = _state([_patient_row("p1", 30, 1.5)], [])
    op = _op("records__patient", Amount(count=1))
    outcome_a = _apply(state_a, op, sc, seed=9)
    outcome_b = _apply(state_b, op, sc, seed=9)
    assert outcome_a.defects == outcome_b.defects
    assert state_a.tables["records__patient"].data.equals(
        state_b.tables["records__patient"].data
    )


def test_duplicate_injected_by_prior_op_is_selectable_by_later_null_cells() -> None:
    state = _state([_patient_row("p1", 30, 1.5)], [_history_row("p1", "age", "30")])
    sc = sidecar((_patient_spec(),))
    _apply(state, _op("records__patient", Amount(count=1)), sc)
    assert state.tables["records__patient"].data.num_rows == 2

    null_op = NullCells(
        kind="null_cells",
        target=Target(table="records__patient", columns=["prop__age"]),
        amount=Amount(count=2),
    )
    outcome = _NULL.apply(state, null_op, "rule#1", random.Random(5), _FORK_PATH, sc)
    assert outcome.units_selected == 2
    assert state.tables["records__patient"].data.column("prop__age").to_pylist() == [
        None,
        None,
    ]


# ---------------------------------------------------------------------------
# Pooled multi-table apply
# ---------------------------------------------------------------------------


def _pooled_state() -> CorruptState:
    patients = working_table(_patient_spec(), [_patient_row("p1", 30, 1.5)])
    doctors = working_table(
        _doctor_spec(),
        [{"fork_path": _FORK_PATH, "record_id": "d1", "prop__name": "Bob"}],
    )
    return CorruptState(
        tables={"records__patient": patients, "records__doctor": doctors}
    )


def _pooled_sidecar() -> "object":
    return sidecar((_patient_spec(), _doctor_spec()))


def _apply_target(
    state: CorruptState, op: DuplicateRows, sc: object, seed: int = 1
) -> object:
    return _DUP.apply(state, op, "rule#0", random.Random(seed), _FORK_PATH, sc)


def test_exact_mode_every_resolved_table_contributes_all_rows() -> None:
    state = _pooled_state()
    op = DuplicateRows(
        kind="duplicate_rows",
        target=Target(tables=["records__doctor", "records__patient"]),
        amount=Amount(count=2),
    )
    outcome = _apply_target(state, op, _pooled_sidecar())
    assert outcome.tables == ("records__doctor", "records__patient")
    assert outcome.units_selected == 2
    assert state.tables["records__doctor"].data.num_rows == 2
    assert state.tables["records__patient"].data.num_rows == 2


def test_near_mode_table_with_no_jitter_eligible_match_contributes_zero_rows() -> None:
    state = _pooled_state()
    jitter = Distribution(shape="uniform", low=0.0, high=0.0)
    op = DuplicateRows(
        kind="duplicate_rows",
        target=Target(
            tables=["records__doctor", "records__patient"], columns=["prop__age"]
        ),
        amount=Amount(count=5),
        jitter=jitter,
    )
    outcome = _apply_target(state, op, _pooled_sidecar())
    # records__doctor has no BIGINT/DOUBLE prop__ column matching "prop__age":
    # it contributes zero row units; only records__patient's one row is in N_pooled.
    assert outcome.units_selected == 1
    assert state.tables["records__doctor"].data.num_rows == 1
    assert state.tables["records__patient"].data.num_rows == 2


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def _four_patient_state() -> CorruptState:
    return _state(
        [
            _patient_row("p1", 10, 1.0),
            _patient_row("p2", 20, 2.0),
            _patient_row("p3", 30, 3.0),
            _patient_row("p4", 40, 4.0),
        ],
        [],
    )


def test_entity_scoped_duplicated_rows_within_drawn_subset_and_subset_size() -> None:
    state = _four_patient_state()
    sc = sidecar((_patient_spec(),))
    op = _op(
        "records__patient",
        Amount(count=2),
        placement=EntityScoped(kind="entity_scoped", entities=Amount(count=2)),
    )
    # Fixed subset {"p2", "p4"}; amount.count == the subset size, so every
    # positive-weight unit (exactly the subset's 2 rows) is drawn regardless
    # of the unit draw's own random() values.
    rng = FixedSampleRandom(["p2", "p4"], seed=1)
    outcome = _apply_with_rng(state, op, sc, rng)
    assert outcome.units_selected == 2
    duplicated_ids = (
        state.tables["records__patient"].data.column("record_id").to_pylist()[4:]
    )
    assert set(duplicated_ids) == {"p2", "p4"}


def test_entity_scoped_drawable_ceiling_selects_exactly_positive_weight_units() -> None:
    state = _four_patient_state()
    sc = sidecar((_patient_spec(),))
    op = _op(
        "records__patient",
        Amount(count=100),
        placement=EntityScoped(kind="entity_scoped", entities=Amount(count=2)),
    )
    rng = FixedSampleRandom(["p1", "p3"], seed=2)
    outcome = _apply_with_rng(state, op, sc, rng)
    # count=100 > N_pooled=4 and > the positive-weight (subset) count of 2:
    # the drawable population is the ceiling -- exactly the 2 subset rows.
    assert outcome.units_selected == 2
    duplicated_ids = (
        state.tables["records__patient"].data.column("record_id").to_pylist()[4:]
    )
    assert set(duplicated_ids) == {"p1", "p3"}


# ---------------------------------------------------------------------------
# Conflicting duplicates (mutation mode)
# ---------------------------------------------------------------------------

_SLICE_AT = 100


def _conflict_patient_spec() -> "object":
    return table_spec(
        "records__patient",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("prop__name", "VARCHAR", history_tracked=True),
            column_spec("prop__age", "BIGINT", history_tracked=True),
            column_spec("prop__dob", "DATE", history_tracked=True),
            column_spec("prop__code", "VARCHAR"),
        ),
        record_kind="patient",
    )


def _conflict_actor_spec() -> "object":
    return table_spec(
        "records__actor",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("prop__actor_type", "VARCHAR", history_tracked=True),
        ),
        record_kind="actor",
    )


def _conflict_history_spec() -> "object":
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


def _conflict_patient_row(
    record_id: str, *, name: object, age: object, dob: object, code: object = "x1"
) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "record_id": record_id,
        "prop__name": name,
        "prop__age": age,
        "prop__dob": dob,
        "prop__code": code,
    }


def _conflict_actor_row(record_id: str, actor_type: object) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "record_id": record_id,
        "prop__actor_type": actor_type,
    }


def _conflict_history_row(
    record_id: str, property_: str, value: str, *, kind: str = "patient"
) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "kind": kind,
        "record_id": record_id,
        "property": property_,
        "sim_time": 10,
        "value": value,
    }


def _conflict_state(
    patient_rows: list[dict[str, object]], history_rows: list[dict[str, object]]
) -> CorruptState:
    return CorruptState(
        tables={
            "history": working_table(_conflict_history_spec(), history_rows),
            "records__patient": working_table(_conflict_patient_spec(), patient_rows),
        }
    )


def _conflict_sidecar(
    tables: tuple[object, ...],
    *,
    pinned_ids: dict[str, dict[str, str]] | None = None,
    record_roles: RecordRoles | None = None,
) -> Sidecar:
    return Sidecar(
        raw={},
        base_format_version=SUPPORTED_BASE_FORMAT_VERSION,
        branches=(BranchEntry(fork_path=_FORK_PATH, parent=None, slice_at=_SLICE_AT),),
        tables=tables,
        runtime=None,
        pinned_ids=pinned_ids or {},
        enum_domains={},
        record_roles=record_roles,
    )


def _mutation_op(
    table: str,
    columns: list[str],
    mutation: object,
    *,
    amount: Amount = Amount(count=1),
    where: dict[str, str] | None = None,
) -> DuplicateRows:
    return DuplicateRows(
        kind="duplicate_rows",
        target=Target(table=table, columns=columns, where=where),
        amount=amount,
        mutation=mutation,
    )


def _apply_conflict(
    state: CorruptState, op: DuplicateRows, sc: object, seed: int = 1
) -> object:
    return _DUP.apply(state, op, "rule#0", random.Random(seed), _FORK_PATH, sc)


def test_mutation_transforms_only_the_copys_matched_cells() -> None:
    state = _conflict_state(
        [_conflict_patient_row("p1", name="Alice", age=10, dob="1980-01-01")], []
    )
    sc = _conflict_sidecar((_conflict_patient_spec(),))
    op = _mutation_op("records__patient", ["prop__name"], MutationTypo(kind="typo"))
    outcome = _apply_conflict(state, op, sc, seed=3)
    assert outcome.defects[0].defect_class == "conflicting_duplicate_row"
    names = state.tables["records__patient"].data.column("prop__name").to_pylist()
    assert names[0] == "Alice"  # original row untouched
    assert names[1] != "Alice"  # copy's matched cell transformed
    assert sorted(names[1]) == sorted("Alice")  # typo: adjacent swap only


def test_mutation_resample_donor_pool_excludes_source_value() -> None:
    state = _conflict_state(
        [
            _conflict_patient_row("p1", name="Alice", age=10, dob="1980-01-01"),
            _conflict_patient_row("p2", name="Bob", age=20, dob="1990-01-01"),
        ],
        [],
    )
    sc = _conflict_sidecar((_conflict_patient_spec(),))
    op = _mutation_op(
        "records__patient",
        ["prop__name"],
        MutationResample(kind="resample"),
        where={"record_id": "p1"},
    )
    outcome = _apply_conflict(state, op, sc, seed=7)
    names = state.tables["records__patient"].data.column("prop__name").to_pylist()
    assert names[0] == "Alice"
    assert names[1] == "Bob"
    assert names[2] == "Bob"  # the only donor once "Alice" is excluded
    assert outcome.defects[0].impact == ("beyond-c1-c12",)  # no history series


def test_mutation_null_cell_stays_null() -> None:
    state = _conflict_state(
        [_conflict_patient_row("p1", name=None, age=10, dob="1980-01-01")], []
    )
    sc = _conflict_sidecar((_conflict_patient_spec(),))
    op = _mutation_op(
        "records__patient", ["prop__name"], MutationSentinel(kind="sentinel", value="Z")
    )
    outcome = _apply_conflict(state, op, sc, seed=1)
    names = state.tables["records__patient"].data.column("prop__name").to_pylist()
    assert names == [None, None]
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


def test_mutation_all_no_mutation_degenerates_to_exact_copy_still_injected() -> None:
    state = _conflict_state(
        [_conflict_patient_row("p1", name="ALICE", age=10, dob="1980-01-01")],
        [_conflict_history_row("p1", "name", "ALICE")],
    )
    sc = _conflict_sidecar((_conflict_patient_spec(),))
    op = _mutation_op(
        "records__patient",
        ["prop__name"],
        MutationCase(kind="case", form="upper"),
    )
    outcome = _apply_conflict(state, op, sc, seed=1)
    assert state.tables["records__patient"].data.num_rows == 2
    assert outcome.units_affected == 1
    names = state.tables["records__patient"].data.column("prop__name").to_pylist()
    assert names == ["ALICE", "ALICE"]
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


def test_mutation_sentinel_unrepresentable_literal_names_duplicate_rows() -> None:
    state = _conflict_state(
        [_conflict_patient_row("p1", name="Alice", age=10, dob="1980-01-01")], []
    )
    sc = _conflict_sidecar((_conflict_patient_spec(),))
    op = _mutation_op(
        "records__patient",
        ["prop__age"],
        MutationSentinel(kind="sentinel", value="not-an-int"),
    )
    with pytest.raises(CorruptValidationError, match=r"duplicate_rows"):
        _apply_conflict(state, op, sc, seed=1)


def test_mutation_c9_fires_when_pinned_untracked_column_mutated() -> None:
    state = _conflict_state(
        [
            _conflict_patient_row(
                "p1", name="Alice", age=10, dob="1980-01-01", code="x1"
            )
        ],
        [],
    )
    sc = _conflict_sidecar(
        (_conflict_patient_spec(),), pinned_ids={"patient": {"alice": "p1"}}
    )
    op = _mutation_op(
        "records__patient", ["prop__code"], MutationCase(kind="case", form="upper")
    )
    outcome = _apply_conflict(state, op, sc, seed=1)
    assert outcome.defects[0].impact == ("C9",)


def test_mutation_c6_fires_for_round_trippable_tracked_column_with_divergence() -> None:
    state = _conflict_state(
        [_conflict_patient_row("p1", name="Alice", age=10, dob="1980-01-01")],
        [_conflict_history_row("p1", "name", "Alice")],
    )
    sc = _conflict_sidecar((_conflict_patient_spec(),))
    op = _mutation_op("records__patient", ["prop__name"], MutationTypo(kind="typo"))
    outcome = _apply_conflict(state, op, sc, seed=3)
    assert outcome.defects[0].impact == ("C6",)


def test_mutation_c6_skipped_for_non_round_trippable_tracked_column() -> None:
    """prop__dob is DATE (not round-trippable). `resample`'s any-type gate
    admits it (unlike `typo`'s VARCHAR/BIGINT gate) and diverges the stored
    value with no DuckDB cast involved, but the C6 gate still skips it."""
    state = _conflict_state(
        [
            _conflict_patient_row("p1", name="Alice", age=10, dob="1980-01-01"),
            _conflict_patient_row("p2", name="Bob", age=20, dob="1990-01-01"),
        ],
        [_conflict_history_row("p1", "dob", "1980-01-01")],
    )
    sc = _conflict_sidecar((_conflict_patient_spec(),))
    op = _mutation_op(
        "records__patient",
        ["prop__dob"],
        MutationResample(kind="resample"),
        where={"record_id": "p1"},
    )
    outcome = _apply_conflict(state, op, sc, seed=1)
    dobs = state.tables["records__patient"].data.column("prop__dob").to_pylist()
    assert dobs[2] == "1990-01-01"  # the only donor once p1's own value excluded
    assert dobs[2] != dobs[0]
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


def test_mutation_c12_actor_subtype_undeclared() -> None:
    state = CorruptState(
        tables={
            "records__actor": working_table(
                _conflict_actor_spec(), [_conflict_actor_row("a1", "doctor")]
            )
        }
    )
    roles = RecordRoles(_registry={"actor": {"doctor": "dimension", "nurse": "fact"}})
    sc = _conflict_sidecar((_conflict_actor_spec(),), record_roles=roles)
    op = _mutation_op(
        "records__actor",
        ["prop__actor_type"],
        MutationSentinel(kind="sentinel", value="ghost"),
    )
    outcome = _apply_conflict(state, op, sc, seed=1)
    assert outcome.defects[0].impact == ("C12",)


def test_mutation_c12_and_c6_union() -> None:
    state = CorruptState(
        tables={
            "records__actor": working_table(
                _conflict_actor_spec(), [_conflict_actor_row("a1", "doctor")]
            ),
            "history": working_table(
                _conflict_history_spec(),
                [_conflict_history_row("a1", "actor_type", "doctor", kind="actor")],
            ),
        }
    )
    roles = RecordRoles(_registry={"actor": {"doctor": "dimension", "nurse": "fact"}})
    sc = _conflict_sidecar((_conflict_actor_spec(),), record_roles=roles)
    op = _mutation_op(
        "records__actor",
        ["prop__actor_type"],
        MutationSentinel(kind="sentinel", value="ghost"),
    )
    outcome = _apply_conflict(state, op, sc, seed=1)
    assert set(outcome.defects[0].impact) == {"C6", "C12"}


def test_mutation_seeded_kind_draws_once_per_row_per_resolved_column_in_order() -> None:
    state = _conflict_state(
        [
            _conflict_patient_row("p1", name="Alice", age=10, dob="1980-01-01"),
            _conflict_patient_row("p2", name="Bob", age=20, dob="1990-01-01"),
        ],
        [],
    )
    sc = _conflict_sidecar((_conflict_patient_spec(),))
    op = _mutation_op(
        "records__patient",
        ["prop__age", "prop__name"],
        MutationTypo(kind="typo"),
        amount=Amount(count=2),
    )
    seed = 7
    outcome = _apply_conflict(state, op, sc, seed=seed)
    assert outcome.units_selected == 2

    expected_rng = random.Random(seed)
    expected_rng.sample(range(2), 2)  # draw_sample's own consumption, first
    # per selected row in pooled order (p1, p2), per resolved column in
    # resolved order (prop__age, prop__name): one seed draw each.
    age_p1 = apply_typo_int(10, expected_rng.random())
    name_p1 = apply_typo_str("Alice", expected_rng.random())
    age_p2 = apply_typo_int(20, expected_rng.random())
    name_p2 = apply_typo_str("Bob", expected_rng.random())

    data = state.tables["records__patient"].data
    assert data.column("prop__age").to_pylist()[2:] == [age_p1, age_p2]
    assert data.column("prop__name").to_pylist()[2:] == [name_p1, name_p2]


def test_mutation_non_seeded_kind_result_independent_of_rng_stream() -> None:
    """A non-seeded kind (`case`) draws nothing beyond `draw_sample`'s own
    consumption: the transformed value is identical regardless of the RNG
    stream that follows (the eight non-seeded kinds draw nothing -- the
    `mutate_cells` discipline)."""
    op = _mutation_op(
        "records__patient", ["prop__name"], MutationCase(kind="case", form="upper")
    )
    results = []
    for seed in (1, 2, 3):
        state = _conflict_state(
            [_conflict_patient_row("p1", name="alice", age=10, dob="1980-01-01")], []
        )
        sc = _conflict_sidecar((_conflict_patient_spec(),))
        _apply_conflict(state, op, sc, seed=seed)
        results.append(
            state.tables["records__patient"].data.column("prop__name").to_pylist()[1]
        )
    assert results == ["ALICE", "ALICE", "ALICE"]


def test_mutation_rerun_with_same_seed_is_identical() -> None:
    sc = _conflict_sidecar((_conflict_patient_spec(),))
    op = _mutation_op("records__patient", ["prop__name"], MutationTypo(kind="typo"))
    state_a = _conflict_state(
        [_conflict_patient_row("p1", name="Alice", age=10, dob="1980-01-01")], []
    )
    state_b = _conflict_state(
        [_conflict_patient_row("p1", name="Alice", age=10, dob="1980-01-01")], []
    )
    outcome_a = _apply_conflict(state_a, op, sc, seed=9)
    outcome_b = _apply_conflict(state_b, op, sc, seed=9)
    assert outcome_a.defects == outcome_b.defects
    assert state_a.tables["records__patient"].data.equals(
        state_b.tables["records__patient"].data
    )
