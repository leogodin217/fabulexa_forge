"""Tests for the `dangle_reference` corrupter handler."""

from __future__ import annotations

import random

from fabulexa_forge.config.models import (
    Amount,
    ClusteredTemporal,
    DangleReference,
    Target,
)
from fabulexa_forge.corrupters.operations.dangle_reference import (
    DANGLING_ID_PREFIX,
    DangleReferenceCorrupter,
)
from fabulexa_forge.corrupters.state import CorruptState
from fabulexa_forge.reader.sidecar import Sidecar

from .._helpers import (
    FixedSampleRandom,
    column_spec,
    sidecar,
    table_spec,
    working_table,
)

_FORK_PATH = "trunk"
_HANDLER = DangleReferenceCorrupter()


def _patient_spec() -> object:
    return table_spec(
        "records__patient",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("prop__name", "VARCHAR", history_tracked=True),
            column_spec(
                "prop__doctor_id",
                "VARCHAR",
                references="doctor",
                history_tracked=True,
            ),
            column_spec("prop__untracked_doctor_id", "VARCHAR", references="doctor"),
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


def _sidecar() -> Sidecar:
    return sidecar((_patient_spec(), _doctor_spec(), _membership_spec()))


def _apply(
    state: CorruptState, table: str, columns: list[str], count: int, seed: int = 1
) -> object:
    op = DangleReference(
        kind="dangle_reference",
        target=Target(table=table, columns=columns),
        amount=Amount(count=count),
    )
    return _HANDLER.apply(
        state, op, "rule#0", random.Random(seed), _FORK_PATH, _sidecar()
    )


# ---------------------------------------------------------------------------
# Membership reference: sentinel + C10
# ---------------------------------------------------------------------------


def _membership_state(pre_existing_sentinel: bool = False) -> CorruptState:
    membership = working_table(
        _membership_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "joined_sim_time": 5,
                "member__doctor__kind": "doctor",
                "member__doctor__id": "d1",
            }
        ],
    )
    doctor_rows = [{"fork_path": _FORK_PATH, "record_id": "d1"}]
    if pre_existing_sentinel:
        doctor_rows.append(
            {"fork_path": _FORK_PATH, "record_id": f"{DANGLING_ID_PREFIX}0"}
        )
    doctors = working_table(_doctor_spec(), doctor_rows)
    return CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": doctors,
        }
    )


def test_membership_id_dangled_declares_c10_with_sentinel() -> None:
    state = _membership_state()
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=1
    )
    assert outcome.units_selected == 1
    assert outcome.units_affected == 1
    defect = outcome.defects[0]
    assert defect.defect_class == "dangling_reference"
    assert defect.impact == ("C10",)
    mutated = state.tables["membership__patient__visits"].data
    assert mutated.column("member__doctor__id").to_pylist() == [
        f"{DANGLING_ID_PREFIX}0"
    ]
    assert mutated.column("member__doctor__kind").to_pylist() == ["doctor"]


def test_sentinel_uses_smallest_absent_suffix() -> None:
    state = _membership_state(pre_existing_sentinel=True)
    _apply(state, "membership__patient__visits", ["member__doctor__id"], count=1)
    mutated = state.tables["membership__patient__visits"].data
    assert mutated.column("member__doctor__id").to_pylist() == [
        f"{DANGLING_ID_PREFIX}1"
    ]


# ---------------------------------------------------------------------------
# Population filters
# ---------------------------------------------------------------------------


def test_null_id_row_excluded_from_population() -> None:
    membership = working_table(
        _membership_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "joined_sim_time": 5,
                "member__doctor__kind": "doctor",
                "member__doctor__id": None,
            }
        ],
    )
    doctors = working_table(
        _doctor_spec(), [{"fork_path": _FORK_PATH, "record_id": "d1"}]
    )
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": doctors,
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
        _membership_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "joined_sim_time": 5,
                "member__doctor__kind": None,
                "member__doctor__id": "d1",
            }
        ],
    )
    doctors = working_table(
        _doctor_spec(), [{"fork_path": _FORK_PATH, "record_id": "d1"}]
    )
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": doctors,
        }
    )
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=1
    )
    assert outcome.units_selected == 0
    assert outcome.defects == ()


def test_absent_target_records_table_excludes_row_all_excluded_is_noop() -> None:
    membership = working_table(
        _membership_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "joined_sim_time": 5,
                "member__doctor__kind": "doctor",
                "member__doctor__id": "d1",
            }
        ],
    )
    state = CorruptState(tables={"membership__patient__visits": membership})
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=1
    )
    assert outcome.units_selected == 0
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_units_selected_equals_units_affected() -> None:
    state = _membership_state()
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=1
    )
    assert outcome.units_selected == outcome.units_affected


# ---------------------------------------------------------------------------
# Records prop__ reference: C6 vs beyond-c1-c12
# ---------------------------------------------------------------------------


def _patient_state_with_history(history_rows: list[dict[str, object]]) -> CorruptState:
    patients = working_table(
        _patient_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "prop__name": "Alice",
                "prop__doctor_id": "d1",
                "prop__untracked_doctor_id": "d1",
            }
        ],
    )
    doctors = working_table(
        _doctor_spec(), [{"fork_path": _FORK_PATH, "record_id": "d1"}]
    )
    history = working_table(_history_spec(), history_rows)
    return CorruptState(
        tables={
            "records__patient": patients,
            "records__doctor": doctors,
            "history": history,
        }
    )


def test_tracked_prop_reference_with_series_declares_c6() -> None:
    state = _patient_state_with_history(
        [
            {
                "fork_path": _FORK_PATH,
                "kind": "patient",
                "record_id": "p1",
                "property": "doctor_id",
                "sim_time": 10,
                "value": "d1",
            }
        ]
    )
    outcome = _apply(state, "records__patient", ["prop__doctor_id"], count=1)
    assert outcome.defects[0].impact == ("C6",)


def test_untracked_prop_reference_declares_beyond_c1_c12() -> None:
    state = _patient_state_with_history([])
    outcome = _apply(state, "records__patient", ["prop__untracked_doctor_id"], count=1)
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


# ---------------------------------------------------------------------------
# Mixed-kind rows dangle against different records tables
# ---------------------------------------------------------------------------


def test_mixed_kind_rows_dangle_against_different_records_tables() -> None:
    membership = working_table(
        _membership_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "joined_sim_time": 5,
                "member__doctor__kind": "doctor",
                "member__doctor__id": "d1",
            },
            {
                "fork_path": _FORK_PATH,
                "record_id": "p2",
                "joined_sim_time": 6,
                "member__doctor__kind": "nurse",
                "member__doctor__id": "n1",
            },
        ],
    )
    doctors = working_table(
        _doctor_spec(), [{"fork_path": _FORK_PATH, "record_id": "d1"}]
    )
    nurse_spec = table_spec(
        "records__nurse",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
        ),
        record_kind="nurse",
    )
    nurses = working_table(nurse_spec, [{"fork_path": _FORK_PATH, "record_id": "n1"}])
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": doctors,
            "records__nurse": nurses,
        }
    )
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=2
    )
    assert outcome.units_selected == 2
    mutated = state.tables["membership__patient__visits"].data
    ids = mutated.column("member__doctor__id").to_pylist()
    assert set(ids) == {f"{DANGLING_ID_PREFIX}0"}
    kinds = mutated.column("member__doctor__kind").to_pylist()
    assert kinds == ["doctor", "nurse"]


# ---------------------------------------------------------------------------
# Break locality, determinism
# ---------------------------------------------------------------------------


def test_only_target_table_entry_replaced() -> None:
    state = _membership_state()
    other = state.tables["records__doctor"]
    _apply(state, "membership__patient__visits", ["member__doctor__id"], count=1)
    assert state.tables["records__doctor"] is other


def test_rerun_with_same_seed_is_identical() -> None:
    state_a = _membership_state()
    state_b = _membership_state()
    outcome_a = _apply(
        state_a, "membership__patient__visits", ["member__doctor__id"], count=1, seed=9
    )
    outcome_b = _apply(
        state_b, "membership__patient__visits", ["member__doctor__id"], count=1, seed=9
    )
    assert outcome_a.defects == outcome_b.defects
    assert state_a.tables["membership__patient__visits"].data.equals(
        state_b.tables["membership__patient__visits"].data
    )


# ---------------------------------------------------------------------------
# Pooled multi-table apply
# ---------------------------------------------------------------------------


def test_pooled_dangle_over_two_tables_per_table_exclusions_still_apply() -> None:
    """Records prop-ref pool: one row eligible, one excluded (already-NULL id)."""
    patients = working_table(
        _patient_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "prop__name": "Alice",
                "prop__doctor_id": "d1",
                "prop__untracked_doctor_id": None,
            }
        ],
    )
    membership = working_table(
        _membership_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "joined_sim_time": 5,
                "member__doctor__kind": "doctor",
                "member__doctor__id": "d1",
            }
        ],
    )
    doctors = working_table(
        _doctor_spec(), [{"fork_path": _FORK_PATH, "record_id": "d1"}]
    )
    state = CorruptState(
        tables={
            "records__patient": patients,
            "membership__patient__visits": membership,
            "records__doctor": doctors,
        }
    )
    op = DangleReference(
        kind="dangle_reference",
        target=Target(
            tables=["membership__patient__visits", "records__patient"],
            columns=[
                "member__doctor__id",
                "prop__doctor_id",
                "prop__untracked_doctor_id",
            ],
        ),
        amount=Amount(rate=1.0),
    )
    outcome = _HANDLER.apply(
        state, op, "rule#0", random.Random(1), _FORK_PATH, _sidecar()
    )
    assert outcome.tables == ("membership__patient__visits", "records__patient")
    # prop__untracked_doctor_id is already NULL on p1 -- excluded from the
    # population; the other two eligible cells (one per table) are dangled.
    assert outcome.units_selected == 2
    assert outcome.units_affected == 2
    by_table = {d.location.table for d in outcome.defects}
    assert by_table == {"membership__patient__visits", "records__patient"}


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def test_clustered_temporal_dangled_rows_within_width_of_drawn_center() -> None:
    membership = working_table(
        _membership_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "joined_sim_time": 10,
                "member__doctor__kind": "doctor",
                "member__doctor__id": "d1",
            },
            {
                "fork_path": _FORK_PATH,
                "record_id": "p2",
                "joined_sim_time": 12,
                "member__doctor__kind": "doctor",
                "member__doctor__id": "d1",
            },
            {
                "fork_path": _FORK_PATH,
                "record_id": "p3",
                "joined_sim_time": 50,
                "member__doctor__kind": "doctor",
                "member__doctor__id": "d1",
            },
            {
                "fork_path": _FORK_PATH,
                "record_id": "p4",
                "joined_sim_time": 100,
                "member__doctor__kind": "doctor",
                "member__doctor__id": "d1",
            },
        ],
    )
    doctors = working_table(
        _doctor_spec(), [{"fork_path": _FORK_PATH, "record_id": "d1"}]
    )
    state = CorruptState(
        tables={
            "membership__patient__visits": membership,
            "records__doctor": doctors,
        }
    )
    op = DangleReference(
        kind="dangle_reference",
        target=Target(
            table="membership__patient__visits", columns=["member__doctor__id"]
        ),
        amount=Amount(count=2),
        placement=ClusteredTemporal(
            kind="clustered_temporal", column="joined_sim_time", clusters=1, width=5
        ),
    )
    # Fixed center 10; amount.count == the positive-weight (within-window) unit
    # count (10, 12), so both are drawn regardless of the unit draw's own
    # random() values.
    rng = FixedSampleRandom([10], seed=4)
    outcome = _HANDLER.apply(state, op, "rule#0", rng, _FORK_PATH, _sidecar())
    assert outcome.units_selected == 2
    joined_times = {
        int(dict(d.location.row.keys)["joined_sim_time"]) for d in outcome.defects
    }
    assert joined_times == {10, 12}
