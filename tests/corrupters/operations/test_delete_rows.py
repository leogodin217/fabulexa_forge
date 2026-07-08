"""Tests for the `delete_rows` corrupter handler -- the wake."""

from __future__ import annotations

import random

from fabulexa_export.config.models import Amount, DeleteRows, EntityScoped, Target
from fabulexa_export.corrupters.operations.delete_rows import DeleteRowsCorrupter
from fabulexa_export.corrupters.state import CorruptState
from fabulexa_export.reader.sidecar import BranchEntry, Sidecar

from .._helpers import (
    CallOrderRandom,
    FixedSampleRandom,
    column_spec,
    sidecar,
    table_spec,
    working_table,
)

_FORK_PATH = "trunk"
_SLICE_AT = 100
_HANDLER = DeleteRowsCorrupter()


def _patient_spec() -> "object":
    return table_spec(
        "records__patient",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("prop__age", "BIGINT", history_tracked=True),
            column_spec("prop__birthdate", "DATE", history_tracked=True),
            column_spec("prop__doctor_id", "VARCHAR", references="doctor"),
        ),
        record_kind="patient",
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


def _ward_spec() -> "object":
    return table_spec(
        "membership__patient__ward",
        "membership",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("joined_sim_time", "BIGINT"),
            column_spec("left_sim_time", "BIGINT"),
            column_spec("elem__slot", "VARCHAR"),
            column_spec("member__consultant__kind", "VARCHAR"),
            column_spec("member__consultant__id", "VARCHAR"),
        ),
        record_kind="patient",
        property_="ward",
    )


def _patient_row(
    record_id: str,
    *,
    age: object = None,
    birthdate: object = None,
    doctor_id: object = None,
) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "record_id": record_id,
        "prop__age": age,
        "prop__birthdate": birthdate,
        "prop__doctor_id": doctor_id,
    }


def _doctor_row(record_id: str, name: str = "Dr. Smith") -> dict[str, object]:
    return {"fork_path": _FORK_PATH, "record_id": record_id, "prop__name": name}


def _history_row(
    kind: str, record_id: str, property_: str, sim_time: int, value: str
) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "kind": kind,
        "record_id": record_id,
        "property": property_,
        "sim_time": sim_time,
        "value": value,
    }


def _ward_row(
    record_id: str,
    joined_sim_time: int,
    consultant_kind: str,
    consultant_id: str,
) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "record_id": record_id,
        "joined_sim_time": joined_sim_time,
        "left_sim_time": None,
        "elem__slot": "morning",
        "member__consultant__kind": consultant_kind,
        "member__consultant__id": consultant_id,
    }


def _state(
    *,
    patients: list[dict[str, object]] | None = None,
    doctors: list[dict[str, object]] | None = None,
    history_rows: list[dict[str, object]] | None = None,
    ward_rows: list[dict[str, object]] | None = None,
) -> CorruptState:
    return CorruptState(
        tables={
            "history": working_table(_history_spec(), history_rows or []),
            "records__patient": working_table(_patient_spec(), patients or []),
            "records__doctor": working_table(_doctor_spec(), doctors or []),
            "membership__patient__ward": working_table(_ward_spec(), ward_rows or []),
        }
    )


def _sidecar(*, pinned_ids: dict[str, dict[str, str]] | None = None) -> Sidecar:
    return sidecar(
        (_patient_spec(), _doctor_spec(), _history_spec(), _ward_spec()),
        branches=(BranchEntry(fork_path=_FORK_PATH, parent=None, slice_at=_SLICE_AT),),
        pinned_ids=pinned_ids,
    )


def _op(
    table: str,
    amount: Amount,
    *,
    where: dict[str, str] | None = None,
    placement: EntityScoped | None = None,
) -> DeleteRows:
    return DeleteRows(
        kind="delete_rows",
        target=Target(table=table, where=where),
        amount=amount,
        placement=placement,
    )


def _apply(
    state: CorruptState, op: DeleteRows, sc: Sidecar, rng: random.Random
) -> object:
    return _HANDLER.apply(state, op, "rule#0", rng, _FORK_PATH, sc)


# ---------------------------------------------------------------------------
# Multiset removal and tombstones
# ---------------------------------------------------------------------------


def test_multiset_removes_two_byte_identical_copies_as_one_set() -> None:
    state = _state(patients=[_patient_row("p1", age=30), _patient_row("p1", age=30)])
    sc = _sidecar()
    op = _op("records__patient", Amount(count=2))
    outcome = _apply(state, op, sc, random.Random(1))
    assert outcome.units_selected == 2
    assert outcome.units_affected == 2
    assert len(outcome.defects) == 2
    assert state.tables["records__patient"].data.num_rows == 0


def test_tombstone_records_record_id_membership_removal_records_nothing() -> None:
    state = _state(patients=[_patient_row("p1", age=30)])
    sc = _sidecar()
    outcome = _apply(
        state, _op("records__patient", Amount(count=1)), sc, random.Random(1)
    )
    assert outcome.units_affected == 1
    assert state.deleted_record_ids == {"patient": {"p1"}}

    state2 = _state(ward_rows=[_ward_row("p1", 10, "doctor", "d1")])
    _apply(
        state2, _op("membership__patient__ward", Amount(count=1)), sc, random.Random(1)
    )
    assert state2.deleted_record_ids == {}


# ---------------------------------------------------------------------------
# Wake C9
# ---------------------------------------------------------------------------


def test_wake_c9_pinned_zero_survivors_table_non_empty() -> None:
    state = _state(patients=[_patient_row("p1", age=30), _patient_row("p2", age=40)])
    sc = _sidecar(pinned_ids={"patient": {"alice": "p1"}})
    op = _op("records__patient", Amount(count=1), where={"record_id": "p1"})
    outcome = _apply(state, op, sc, random.Random(1))
    assert outcome.defects[0].impact == ("C9",)


def test_wake_c9_vacuous_pass_when_table_emptied() -> None:
    state = _state(patients=[_patient_row("p1", age=30)])
    sc = _sidecar(pinned_ids={"patient": {"alice": "p1"}})
    op = _op("records__patient", Amount(count=1))
    outcome = _apply(state, op, sc, random.Random(1))
    assert state.tables["records__patient"].data.num_rows == 0
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


# ---------------------------------------------------------------------------
# Wake C6
# ---------------------------------------------------------------------------


def test_wake_c6_orphaned_series_with_round_trippable_type_declares_c6() -> None:
    state = _state(
        patients=[_patient_row("p3", age=30)],
        history_rows=[_history_row("patient", "p3", "age", 10, "30")],
    )
    sc = _sidecar()
    op = _op("records__patient", Amount(count=1), where={"record_id": "p3"})
    outcome = _apply(state, op, sc, random.Random(1))
    assert outcome.defects[0].impact == ("C6",)


def test_wake_c6_not_declared_for_post_slice_only_series() -> None:
    state = _state(
        patients=[_patient_row("p4", age=40)],
        history_rows=[_history_row("patient", "p4", "age", 150, "40")],
    )
    sc = _sidecar()
    op = _op("records__patient", Amount(count=1), where={"record_id": "p4"})
    outcome = _apply(state, op, sc, random.Random(1))
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


def test_wake_c6_history_absent_from_working_set_cannot_declare() -> None:
    """An engine-invariant edge: a working set carrying no `history` table at
    all (never true for a contract-conformant emit, whose sidecar always
    declares one) makes the C6 series lookup find nothing rather than raise
    -- the zero-survivors branch degrades to no C6 rather than erroring."""
    state = CorruptState(
        tables={
            "records__patient": working_table(_patient_spec(), [_patient_row("p9")])
        }
    )
    sc = _sidecar()
    op = _op("records__patient", Amount(count=1))
    outcome = _apply(state, op, sc, random.Random(1))
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


def test_wake_c6_not_declared_for_non_round_trippable_type() -> None:
    state = _state(
        patients=[_patient_row("p5", birthdate="1990-01-01")],
        history_rows=[_history_row("patient", "p5", "birthdate", 10, "1990-01-01")],
    )
    sc = _sidecar()
    op = _op("records__patient", Amount(count=1), where={"record_id": "p5"})
    outcome = _apply(state, op, sc, random.Random(1))
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


# ---------------------------------------------------------------------------
# Wake C10
# ---------------------------------------------------------------------------


def test_wake_c10_surviving_membership_reference_declares_c10() -> None:
    state = _state(
        doctors=[_doctor_row("d1")],
        ward_rows=[_ward_row("p_ward1", 10, "doctor", "d1")],
    )
    sc = _sidecar()
    op = _op("records__doctor", Amount(count=1))
    outcome = _apply(state, op, sc, random.Random(1))
    assert outcome.defects[0].impact == ("C10",)


# ---------------------------------------------------------------------------
# Contributes-nothing consequences and membership-row deletion
# ---------------------------------------------------------------------------


def test_dangling_records_prop_reference_contributes_nothing() -> None:
    state = _state(
        patients=[_patient_row("p6", doctor_id="d1")],
        doctors=[_doctor_row("d1")],
    )
    sc = _sidecar()
    op = _op("records__doctor", Amount(count=1))
    outcome = _apply(state, op, sc, random.Random(1))
    assert outcome.defects[0].impact == ("beyond-c1-c12",)
    # the dangle is silent: the patient's referencing cell is untouched
    assert state.tables["records__patient"].data.column(
        "prop__doctor_id"
    ).to_pylist() == ["d1"]


def test_membership_row_deletion_always_beyond_c1_c12() -> None:
    state = _state(ward_rows=[_ward_row("p1", 10, "doctor", "d1")])
    sc = _sidecar()
    op = _op("membership__patient__ward", Amount(count=1))
    outcome = _apply(state, op, sc, random.Random(1))
    assert outcome.defects[0].defect_class == "deleted_row"
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


# ---------------------------------------------------------------------------
# Healing: deleting one of two copies of a pinned row
# ---------------------------------------------------------------------------


def test_deleting_one_of_two_copies_of_pinned_row_declares_no_code() -> None:
    state = _state(patients=[_patient_row("p7", age=1), _patient_row("p7", age=1)])
    sc = _sidecar(pinned_ids={"patient": {"alice": "p7"}})
    op = _op("records__patient", Amount(count=1))
    outcome = _apply(state, op, sc, random.Random(1))
    assert outcome.defects[0].impact == ("beyond-c1-c12",)
    assert state.tables["records__patient"].data.num_rows == 1


# ---------------------------------------------------------------------------
# Defect class, source-coordinate locator, strict 1:1
# ---------------------------------------------------------------------------


def test_defect_class_and_source_coordinate_locator() -> None:
    state = _state(patients=[_patient_row("p1", age=30)])
    sc = _sidecar()
    op = _op("records__patient", Amount(count=1))
    outcome = _apply(state, op, sc, random.Random(1))
    assert outcome.units_selected == outcome.units_affected == len(outcome.defects)
    defect = outcome.defects[0]
    assert defect.defect_class == "deleted_row"
    assert defect.location.table == "records__patient"
    assert defect.location.row.keys == (
        ("fork_path", "trunk"),
        ("record_id", "p1"),
    )


# ---------------------------------------------------------------------------
# RNG order and determinism
# ---------------------------------------------------------------------------


def test_rng_order_uniform_draw_only_no_mode_draws() -> None:
    """The unit draw is the sole RNG consumer: one `.sample()` call, whose
    own internal population walk (`.random()`, via `CallOrderRandom`
    overriding `random()` but not `getrandbits()`) is the only further
    consumption -- no mode-draw call follows it."""
    state = _state(patients=[_patient_row("p1", age=30)])
    sc = _sidecar()
    rng = CallOrderRandom(seed=1)
    _apply(state, _op("records__patient", Amount(count=1)), sc, rng)
    assert rng.calls[0] == "sample"
    assert rng.calls[1:] == ["random"] * (len(rng.calls) - 1)


def test_rng_order_weighted_draw_placement_setup_then_unit_draw() -> None:
    state = _state(
        patients=[
            _patient_row("p1", age=10),
            _patient_row("p2", age=20),
        ]
    )
    sc = _sidecar()
    rng = CallOrderRandom(seed=1)
    op = _op(
        "records__patient",
        Amount(count=1),
        placement=EntityScoped(kind="entity_scoped", entities=Amount(count=1)),
    )
    _apply(state, op, sc, rng)
    assert rng.calls[0] == "sample"
    assert "sample" not in rng.calls[1:]


def test_rerun_with_same_seed_is_identical() -> None:
    sc = _sidecar(pinned_ids={"patient": {"alice": "p1"}})
    state_a = _state(patients=[_patient_row("p1", age=30)])
    state_b = _state(patients=[_patient_row("p1", age=30)])
    op = _op("records__patient", Amount(count=1))
    outcome_a = _apply(state_a, op, sc, random.Random(9))
    outcome_b = _apply(state_b, op, sc, random.Random(9))
    assert outcome_a.defects == outcome_b.defects
    assert state_a.tables["records__patient"].data.equals(
        state_b.tables["records__patient"].data
    )


# ---------------------------------------------------------------------------
# where narrowing and placement weighting compose
# ---------------------------------------------------------------------------


def test_where_narrowing_and_placement_weighting_compose() -> None:
    state = _state(
        patients=[
            _patient_row("p1", doctor_id="d1"),
            _patient_row("p2", doctor_id="d1"),
            _patient_row("p3", doctor_id="d2"),
            _patient_row("p4", doctor_id="d1"),
        ]
    )
    sc = _sidecar()
    op = _op(
        "records__patient",
        Amount(count=2),
        where={"prop__doctor_id": "d1"},
        placement=EntityScoped(kind="entity_scoped", entities=Amount(count=2)),
    )
    rng = FixedSampleRandom(["p2", "p4"], seed=1)
    outcome = _apply(state, op, sc, rng)
    assert outcome.units_selected == 2
    remaining = set(
        state.tables["records__patient"].data.column("record_id").to_pylist()
    )
    assert remaining == {"p1", "p3"}
