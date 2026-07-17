"""Tests for the `null_cells` corrupter handler."""

from __future__ import annotations

import random

from fabulexa_forge.config.models import (
    Amount,
    ClusteredTemporal,
    Correlated,
    EntityScoped,
    NullCells,
    Target,
)
from fabulexa_forge.corrupters.operations.null_cells import NullCellsCorrupter
from fabulexa_forge.corrupters.state import CorruptState

from .._helpers import CallOrderRandom, column_spec, sidecar, table_spec, working_table

_FORK_PATH = "trunk"
_HANDLER = NullCellsCorrupter()


def _patient_spec() -> "object":
    return table_spec(
        "records__patient",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("active", "BOOLEAN"),
            column_spec("deactivated_at", "BIGINT"),
            column_spec(
                "prop__name", "VARCHAR", history_tracked=True, temporal_class="tracked"
            ),
            column_spec(
                "prop__nickname",
                "VARCHAR",
                history_tracked=True,
                temporal_class="tracked",
            ),
            column_spec(
                "prop__birthdate",
                "DATE",
                history_tracked=True,
                temporal_class="tracked",
            ),
            column_spec("prop__notes", "VARCHAR"),
            column_spec("prop__doctor_id", "VARCHAR", references="doctor"),
            column_spec("ref_index__doctor_id", "BIGINT"),
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


def _membership_spec() -> "object":
    return table_spec(
        "membership__patient__visits",
        "membership",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("joined_sim_time", "BIGINT"),
            column_spec("left_sim_time", "BIGINT"),
            column_spec("member__doctor__kind", "VARCHAR"),
            column_spec("member__doctor__id", "VARCHAR"),
        ),
        record_kind="patient",
        property_="visits",
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


def _patient_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "fork_path": _FORK_PATH,
        "record_id": "p1",
        "active": True,
        "deactivated_at": None,
        "prop__name": "Alice",
        "prop__nickname": "Al",
        "prop__birthdate": "1990-01-01",
        "prop__notes": "hello",
        "prop__doctor_id": "d1",
        "ref_index__doctor_id": 3,
    }
    row.update(overrides)
    return row


def _state_with_series() -> CorruptState:
    history = working_table(
        _history_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "kind": "patient",
                "record_id": "p1",
                "property": "name",
                "sim_time": 10,
                "value": "Alice",
            }
        ],
    )
    patients = working_table(_patient_spec(), [_patient_row()])
    return CorruptState(tables={"history": history, "records__patient": patients})


def _apply(
    state: CorruptState, table: str, columns: list[str], count: int, seed: int = 1
) -> object:
    op = NullCells(
        kind="null_cells",
        target=Target(table=table, columns=columns),
        amount=Amount(count=count),
    )
    return _HANDLER.apply(
        state,
        op,
        "rule#0",
        random.Random(seed),
        _FORK_PATH,
        sidecar((_patient_spec(), _membership_spec())),
    )


def _apply_target(
    state: CorruptState, target: Target, amount: Amount, sc: object, seed: int = 1
) -> object:
    op = NullCells(kind="null_cells", target=target, amount=amount)
    return _HANDLER.apply(state, op, "rule#0", random.Random(seed), _FORK_PATH, sc)


# ---------------------------------------------------------------------------
# C6: tracked, round-trippable, has a series
# ---------------------------------------------------------------------------


def test_tracked_round_trippable_with_series_nulls_and_declares_c6() -> None:
    state = _state_with_series()
    outcome = _apply(state, "records__patient", ["prop__name"], count=1)
    assert outcome.units_selected == 1
    assert outcome.units_affected == 1
    assert len(outcome.defects) == 1
    defect = outcome.defects[0]
    assert defect.defect_class == "missing_value"
    assert defect.impact == ("C6",)
    assert defect.location.kind == "cell"
    assert defect.location.column == "prop__name"
    assert defect.location.row.category == "records"
    assert defect.location.row.keys == (("fork_path", "trunk"), ("record_id", "p1"))
    mutated = state.tables["records__patient"].data
    assert mutated.column("prop__name").to_pylist() == [None]


def test_tracked_type_with_incidental_whitespace_still_declares_c6() -> None:
    """The round-trippable gate strips the type literal — an earlier
    `schema_drift` retype stores the author's raw string (e.g. 'VARCHAR ')
    verbatim on the working spec — matching the real `_check_c6` and the
    sibling gates in `_impact.py` / `schema_drift.py`."""
    padded_spec = table_spec(
        "records__patient",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec(
                "prop__name",
                "VARCHAR ",
                history_tracked=True,
                temporal_class="tracked",
            ),
        ),
        record_kind="patient",
    )
    history = working_table(
        _history_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "kind": "patient",
                "record_id": "p1",
                "property": "name",
                "sim_time": 10,
                "value": "Alice",
            }
        ],
    )
    patients = working_table(
        padded_spec,
        [{"fork_path": _FORK_PATH, "record_id": "p1", "prop__name": "Alice"}],
    )
    state = CorruptState(tables={"history": history, "records__patient": patients})
    outcome = _apply(state, "records__patient", ["prop__name"], count=1)
    assert outcome.defects[0].impact == ("C6",)


def test_tracked_column_with_no_series_declares_beyond_c1_c12() -> None:
    state = _state_with_series()
    outcome = _apply(state, "records__patient", ["prop__nickname"], count=1)
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


def test_tracked_non_round_trippable_type_declares_beyond_c1_c12() -> None:
    state = _state_with_series()
    outcome = _apply(state, "records__patient", ["prop__birthdate"], count=1)
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


def test_untracked_prop_column_declares_beyond_c1_c12() -> None:
    state = _state_with_series()
    outcome = _apply(state, "records__patient", ["prop__notes"], count=1)
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


# ---------------------------------------------------------------------------
# C7: membership ref pair and deactivated_at
# ---------------------------------------------------------------------------


def _state_with_membership() -> CorruptState:
    membership = working_table(
        _membership_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "joined_sim_time": 5,
                "left_sim_time": None,
                "member__doctor__kind": "doctor",
                "member__doctor__id": "d1",
            }
        ],
    )
    return CorruptState(tables={"membership__patient__visits": membership})


def test_member_kind_null_with_populated_partner_declares_c7() -> None:
    state = _state_with_membership()
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__kind"], count=1
    )
    assert outcome.defects[0].impact == ("C7",)
    mutated = state.tables["membership__patient__visits"].data
    assert mutated.column("member__doctor__kind").to_pylist() == [None]
    assert mutated.column("member__doctor__id").to_pylist() == ["d1"]


def test_member_pair_both_nulled_same_operation_heals_to_beyond_c1_c12() -> None:
    state = _state_with_membership()
    outcome = _apply(
        state,
        "membership__patient__visits",
        ["member__doctor__kind", "member__doctor__id"],
        count=2,
    )
    assert [d.impact for d in outcome.defects] == [("C7",), ("beyond-c1-c12",)]


def test_member_pair_second_half_nulled_later_operation_heals_to_beyond_c1_c12() -> (
    None
):
    state = _state_with_membership()
    first = _apply(
        state, "membership__patient__visits", ["member__doctor__kind"], count=1
    )
    assert first.defects[0].impact == ("C7",)
    second = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=1
    )
    assert second.defects[0].impact == ("beyond-c1-c12",)


def test_non_prop_non_membership_non_deactivated_column_declares_beyond_c1_c12() -> (
    None
):
    membership = working_table(
        _membership_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "joined_sim_time": 5,
                "left_sim_time": 20,
                "member__doctor__kind": "doctor",
                "member__doctor__id": "d1",
            }
        ],
    )
    state = CorruptState(tables={"membership__patient__visits": membership})
    outcome = _apply(state, "membership__patient__visits", ["left_sim_time"], count=1)
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


def test_deactivated_at_non_null_declares_c7() -> None:
    history = working_table(_history_spec(), [])
    patients = working_table(
        _patient_spec(), [_patient_row(active=False, deactivated_at=999)]
    )
    state = CorruptState(tables={"history": history, "records__patient": patients})
    outcome = _apply(state, "records__patient", ["deactivated_at"], count=1)
    assert outcome.defects[0].impact == ("C7",)


# ---------------------------------------------------------------------------
# Pair-scoped reference writes: a records reference prop__ cell's write
# co-nulls its ref_index__ sibling in the same act
# ---------------------------------------------------------------------------


def test_records_reference_prop_cell_conulls_ref_index_sibling() -> None:
    state = _state_with_series()
    outcome = _apply(state, "records__patient", ["prop__doctor_id"], count=1)
    assert outcome.units_affected == 1
    assert len(outcome.defects) == 1
    assert outcome.defects[0].location.column == "prop__doctor_id"
    mutated = state.tables["records__patient"].data
    assert mutated.column("prop__doctor_id").to_pylist() == [None]
    assert mutated.column("ref_index__doctor_id").to_pylist() == [None]


def test_non_reference_prop_cell_leaves_unrelated_ref_index_column_untouched() -> None:
    state = _state_with_series()
    outcome = _apply(state, "records__patient", ["prop__name"], count=1)
    assert outcome.units_affected == 1
    mutated = state.tables["records__patient"].data
    assert mutated.column("prop__name").to_pylist() == [None]
    assert mutated.column("ref_index__doctor_id").to_pylist() == [3]


def test_membership_member_id_cell_null_has_no_sibling_write() -> None:
    state = _state_with_membership()
    outcome = _apply(
        state, "membership__patient__visits", ["member__doctor__id"], count=1
    )
    assert outcome.units_affected == 1
    mutated = state.tables["membership__patient__visits"].data
    assert mutated.column("member__doctor__id").to_pylist() == [None]


# ---------------------------------------------------------------------------
# Already-NULL cell
# ---------------------------------------------------------------------------


def test_already_null_cell_counted_selected_not_affected() -> None:
    history = working_table(_history_spec(), [])
    patients = working_table(_patient_spec(), [_patient_row(prop__notes=None)])
    state = CorruptState(tables={"history": history, "records__patient": patients})
    outcome = _apply(state, "records__patient", ["prop__notes"], count=1)
    assert outcome.units_selected == 1
    assert outcome.units_affected == 0
    assert outcome.defects == ()


# ---------------------------------------------------------------------------
# Break locality, determinism, and reading the working state
# ---------------------------------------------------------------------------


def test_only_target_table_entry_replaced() -> None:
    state = _state_with_series()
    other = state.tables["history"]
    _apply(state, "records__patient", ["prop__name"], count=1)
    assert state.tables["history"] is other


def test_rerun_with_same_seed_is_identical() -> None:
    state_a = _state_with_series()
    state_b = _state_with_series()
    outcome_a = _apply(state_a, "records__patient", ["prop__name"], count=1, seed=7)
    outcome_b = _apply(state_b, "records__patient", ["prop__name"], count=1, seed=7)
    assert outcome_a.defects == outcome_b.defects
    assert state_a.tables["records__patient"].data.equals(
        state_b.tables["records__patient"].data
    )


# ---------------------------------------------------------------------------
# Pooled multi-table apply
# ---------------------------------------------------------------------------


def _pooled_state() -> CorruptState:
    history = working_table(
        _history_spec(),
        [
            {
                "fork_path": _FORK_PATH,
                "kind": "patient",
                "record_id": "p1",
                "property": "name",
                "sim_time": 10,
                "value": "Alice",
            }
        ],
    )
    patients = working_table(_patient_spec(), [_patient_row()])
    doctors = working_table(
        _doctor_spec(),
        [{"fork_path": _FORK_PATH, "record_id": "d1", "prop__name": "Bob"}],
    )
    return CorruptState(
        tables={
            "history": history,
            "records__patient": patients,
            "records__doctor": doctors,
        }
    )


def _pooled_sidecar() -> "object":
    return sidecar((_patient_spec(), _doctor_spec()))


def test_pooled_cell_units_ordered_table_row_column_per_table_impact() -> None:
    state = _pooled_state()
    outcome = _apply_target(
        state,
        Target(tables=["records__patient", "records__doctor"], columns=["prop__name"]),
        Amount(count=2),
        _pooled_sidecar(),
    )
    assert outcome.tables == ("records__doctor", "records__patient")
    assert outcome.units_selected == 2
    assert len(outcome.defects) == 2
    by_table = {d.location.table: d for d in outcome.defects}
    assert by_table["records__doctor"].impact == ("beyond-c1-c12",)
    assert by_table["records__patient"].impact == ("C6",)
    assert {d.rule for d in outcome.defects} == {"rule#0"}


def test_amount_rate_floors_over_pooled_population() -> None:
    state = _pooled_state()
    outcome = _apply_target(
        state,
        Target(tables=["records__patient", "records__doctor"], columns=["prop__name"]),
        Amount(rate=0.5),
        _pooled_sidecar(),
    )
    assert outcome.units_selected == 1  # floor(0.5 * 2) == 1


def test_amount_count_clips_to_pooled_population() -> None:
    state = _pooled_state()
    outcome = _apply_target(
        state,
        Target(tables=["records__patient", "records__doctor"], columns=["prop__name"]),
        Amount(count=100),
        _pooled_sidecar(),
    )
    assert outcome.units_selected == 2  # min(100, 2) == 2


def test_where_absent_from_one_table_contributes_zero_units_from_it() -> None:
    state = _pooled_state()
    outcome = _apply_target(
        state,
        Target(
            tables=["records__patient", "records__doctor"],
            columns=["prop__name"],
            where={"active": "true"},
        ),
        Amount(rate=1.0),
        _pooled_sidecar(),
    )
    # records__doctor has no `active` column: it contributes zero units;
    # only records__patient's row (active=True) is drawn.
    assert outcome.units_selected == 1
    assert outcome.defects[0].location.table == "records__patient"


def test_zero_rows_in_every_resolved_table_is_noop() -> None:
    state = _pooled_state()
    outcome = _apply_target(
        state,
        Target(
            tables=["records__patient", "records__doctor"],
            columns=["prop__name"],
            where={"record_id": "no-such-record"},
        ),
        Amount(rate=1.0),
        _pooled_sidecar(),
    )
    assert outcome.units_selected == 0
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_single_table_selector_degenerates_to_concrete_table() -> None:
    state_a = _pooled_state()
    state_b = _pooled_state()
    outcome_a = _apply_target(
        state_a,
        Target(table="records__patient", columns=["prop__name"]),
        Amount(count=1),
        _pooled_sidecar(),
        seed=3,
    )
    outcome_b = _apply_target(
        state_b,
        Target(tables=["records__patient"], columns=["prop__name"]),
        Amount(count=1),
        _pooled_sidecar(),
        seed=3,
    )
    assert outcome_a.defects == outcome_b.defects
    assert state_a.tables["records__patient"].data.equals(
        state_b.tables["records__patient"].data
    )


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def test_correlated_high_weight_lands_on_matching_row_deterministically() -> None:
    history = working_table(_history_spec(), [])
    patients = working_table(
        _patient_spec(),
        [
            _patient_row(record_id="p1", prop__notes="a"),
            _patient_row(record_id="p2", prop__notes="b"),
        ],
    )
    state = CorruptState(tables={"history": history, "records__patient": patients})
    op = NullCells(
        kind="null_cells",
        target=Target(table="records__patient", columns=["prop__notes"]),
        amount=Amount(count=1),
        placement=Correlated(
            kind="correlated", column="record_id", value="p1", weight=1e9
        ),
    )
    outcome = _HANDLER.apply(
        state,
        op,
        "rule#0",
        random.Random(1),
        _FORK_PATH,
        sidecar((_patient_spec(), _membership_spec())),
    )
    # weight=1e9 vs the non-matching row's weight 1 makes the matching row's
    # Efraimidis-Spirakis key (u ** (1/1e9), ~= 1) beat the other row's key
    # (u ** 1 = u, < 1) for any realizable pair of random.random() draws.
    assert outcome.units_selected == 1
    assert outcome.defects[0].location.row.keys == (
        ("fork_path", _FORK_PATH),
        ("record_id", "p1"),
    )


def test_clustered_temporal_all_null_column_is_noop() -> None:
    history = working_table(_history_spec(), [])
    patients = working_table(
        _patient_spec(),
        [
            _patient_row(record_id="p1", deactivated_at=None),
            _patient_row(record_id="p2", deactivated_at=None),
        ],
    )
    state = CorruptState(tables={"history": history, "records__patient": patients})
    op = NullCells(
        kind="null_cells",
        target=Target(table="records__patient", columns=["prop__notes"]),
        amount=Amount(rate=1.0),
        placement=ClusteredTemporal(
            kind="clustered_temporal", column="deactivated_at", clusters=1, width=10
        ),
    )
    outcome = _HANDLER.apply(
        state,
        op,
        "rule#0",
        random.Random(1),
        _FORK_PATH,
        sidecar((_patient_spec(), _membership_spec())),
    )
    assert outcome.units_selected == 0
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_placement_setup_draw_precedes_unit_draws_in_rng_order() -> None:
    state = _state_with_series()
    rng = CallOrderRandom(seed=1)
    op = NullCells(
        kind="null_cells",
        target=Target(table="records__patient", columns=["prop__name"]),
        amount=Amount(count=1),
        placement=EntityScoped(kind="entity_scoped", entities=Amount(count=1)),
    )
    _HANDLER.apply(
        state,
        op,
        "rule#0",
        rng,
        _FORK_PATH,
        sidecar((_patient_spec(), _membership_spec())),
    )
    assert rng.calls[0] == "sample"
    assert rng.calls[1:] == ["random"] * (len(rng.calls) - 1)
