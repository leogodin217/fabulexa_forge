"""Tests for the `mutate_cells` corrupter handler."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

import pytest

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import (
    Amount,
    Correlated,
    MutateCells,
    MutationCase,
    MutationFormatDirt,
    MutationMojibake,
    MutationOutOfDomain,
    MutationPrecisionDrop,
    MutationResample,
    MutationScale,
    MutationSentinel,
    MutationSpec,
    MutationTruncate,
    MutationTypo,
    MutationWhitespace,
    Target,
)
from fabulexa_forge.corrupters.operations.mutate_cells import MutateCellsCorrupter
from fabulexa_forge.corrupters.state import CorruptState, OperationOutcome
from fabulexa_forge.errors import CorruptError, CorruptValidationError
from fabulexa_forge.reader.sidecar import BranchEntry, RecordRoles, Sidecar, TableSpec

from .._helpers import column_spec, table_spec, working_table

_FORK_PATH = "trunk"
_SLICE_AT = 100
_HANDLER = MutateCellsCorrupter()


class _FixedRandomValues(random.Random):
    """A `random.Random` whose `.random()` returns a fixed sequence, cycling
    if exhausted -- pins `draw_weighted_sample`'s per-unit uniform draws so a
    placement-weighted test can target an exact winner."""

    def __init__(self, values: Sequence[float], seed: int = 0) -> None:
        super().__init__(seed)
        self._values = list(values)
        self._i = 0

    def random(self) -> float:
        value = self._values[self._i % len(self._values)]
        self._i += 1
        return value


# ---------------------------------------------------------------------------
# Fixture specs, rows, sidecar, state
# ---------------------------------------------------------------------------


def _patient_spec() -> TableSpec:
    return table_spec(
        "records__patient",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("presentation_id", "VARCHAR"),
            column_spec("prop__name", "VARCHAR"),
            column_spec("prop__dob", "VARCHAR"),
            column_spec("prop__age", "BIGINT"),
            column_spec("prop__weight_kg", "DOUBLE"),
        ),
        record_kind="patient",
    )


def _actor_spec() -> TableSpec:
    return table_spec(
        "records__actor",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("prop__actor_type", "VARCHAR"),
        ),
        record_kind="actor",
    )


def _membership_spec() -> TableSpec:
    return table_spec(
        "membership__patient__ward",
        "membership",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("joined_sim_time", "BIGINT"),
            column_spec("elem__slot", "VARCHAR"),
            column_spec("elem__weight_kg", "DOUBLE"),
        ),
        record_kind="patient",
        property_="ward",
    )


def _history_spec() -> TableSpec:
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


def _patient_row(
    *,
    record_id: str = "p1",
    name: str | None = "Alice",
    dob: str | None = "1980-01-01",
    age: int | None = 30,
    presentation_id: str | None = "P-1",
    weight_kg: float | None = None,
) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "record_id": record_id,
        "presentation_id": presentation_id,
        "prop__name": name,
        "prop__dob": dob,
        "prop__age": age,
        "prop__weight_kg": weight_kg,
    }


def _actor_row(
    *, record_id: str = "a1", actor_type: str | None = "doctor"
) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "record_id": record_id,
        "prop__actor_type": actor_type,
    }


def _membership_row(
    *,
    record_id: str = "p1",
    joined_sim_time: int = 5,
    slot: str | None = "A",
    weight_kg: float | None = None,
) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "record_id": record_id,
        "joined_sim_time": joined_sim_time,
        "elem__slot": slot,
        "elem__weight_kg": weight_kg,
    }


def _history_row(
    *, kind: str, record_id: str, property_: str, sim_time: int, value: str
) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "kind": kind,
        "record_id": record_id,
        "property": property_,
        "sim_time": sim_time,
        "value": value,
    }


def _sidecar(
    *,
    record_roles: RecordRoles | None = None,
    enum_domains: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None,
) -> Sidecar:
    return Sidecar(
        raw={},
        base_format_version=SUPPORTED_BASE_FORMAT_VERSION,
        branches=(BranchEntry(fork_path=_FORK_PATH, parent=None, slice_at=_SLICE_AT),),
        tables=(_patient_spec(), _actor_spec(), _membership_spec(), _history_spec()),
        runtime=None,
        pinned_ids={},
        enum_domains=enum_domains or {},
        record_roles=record_roles,
    )


def _state(
    *,
    patient_rows: Sequence[dict[str, object]] = (),
    actor_rows: Sequence[dict[str, object]] = (),
    membership_rows: Sequence[dict[str, object]] = (),
    history_rows: Sequence[dict[str, object]] = (),
) -> CorruptState:
    return CorruptState(
        tables={
            "records__patient": working_table(_patient_spec(), list(patient_rows)),
            "records__actor": working_table(_actor_spec(), list(actor_rows)),
            "membership__patient__ward": working_table(
                _membership_spec(), list(membership_rows)
            ),
            "history": working_table(_history_spec(), list(history_rows)),
        }
    )


def _apply(
    state: CorruptState,
    table: str,
    columns: list[str],
    mutation: MutationSpec,
    *,
    count: int = 1,
    seed: int = 1,
    where: dict[str, str] | None = None,
    placement: Correlated | None = None,
    rng: random.Random | None = None,
    sidecar_obj: Sidecar | None = None,
) -> OperationOutcome:
    op = MutateCells(
        kind="mutate_cells",
        target=Target(table=table, columns=columns, where=where),
        amount=Amount(count=count),
        placement=placement,
        mutation=mutation,
    )
    return _HANDLER.apply(
        state,
        op,
        "rule#0",
        rng if rng is not None else random.Random(seed),
        _FORK_PATH,
        sidecar_obj if sidecar_obj is not None else _sidecar(),
    )


# ---------------------------------------------------------------------------
# Sentinel cast oracle
# ---------------------------------------------------------------------------


def test_sentinel_uncastable_literal_raises_before_any_write() -> None:
    state = _state(patient_rows=[_patient_row(age=30)])
    original = state.tables["records__patient"].data
    mutation = MutationSentinel(kind="sentinel", value="abc")
    with pytest.raises(CorruptValidationError, match=r"records__patient\.prop__age"):
        _apply(state, "records__patient", ["prop__age"], mutation)
    assert state.tables["records__patient"].data.equals(original)


# ---------------------------------------------------------------------------
# Handler, per kind
# ---------------------------------------------------------------------------


def test_sentinel_replaces_and_renders_into_column_type() -> None:
    state = _state(patient_rows=[_patient_row(age=30)])
    mutation = MutationSentinel(kind="sentinel", value=-1)
    outcome = _apply(state, "records__patient", ["prop__age"], mutation)
    assert outcome.units_affected == 1
    assert outcome.defects[0].defect_class == "sentinel_value"
    assert state.tables["records__patient"].data.column("prop__age").to_pylist() == [-1]


def test_sentinel_equal_to_stored_value_is_no_mutation() -> None:
    state = _state(patient_rows=[_patient_row(name="ALICE")])
    mutation = MutationSentinel(kind="sentinel", value="ALICE")
    outcome = _apply(state, "records__patient", ["prop__name"], mutation)
    assert outcome.units_selected == 1
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_case_upper_on_already_upper_is_no_mutation() -> None:
    state = _state(patient_rows=[_patient_row(name="ALICE")])
    mutation = MutationCase(kind="case", form="upper")
    outcome = _apply(state, "records__patient", ["prop__name"], mutation)
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_case_upper_mutates_lower_string() -> None:
    state = _state(patient_rows=[_patient_row(name="alice")])
    mutation = MutationCase(kind="case", form="upper")
    outcome = _apply(state, "records__patient", ["prop__name"], mutation)
    assert outcome.units_affected == 1
    assert outcome.defects[0].defect_class == "case_drift"
    assert state.tables["records__patient"].data.column("prop__name").to_pylist() == [
        "ALICE"
    ]


def test_whitespace_always_mutates_present_string() -> None:
    state = _state(patient_rows=[_patient_row(name="Alice")])
    mutation = MutationWhitespace(kind="whitespace", where="trailing")
    outcome = _apply(state, "records__patient", ["prop__name"], mutation)
    assert outcome.units_affected == 1
    assert outcome.defects[0].defect_class == "whitespace_pad"
    assert state.tables["records__patient"].data.column("prop__name").to_pylist() == [
        "Alice "
    ]


def test_whitespace_leading_inserts_at_the_front() -> None:
    state = _state(patient_rows=[_patient_row(name="Alice")])
    mutation = MutationWhitespace(kind="whitespace", where="leading")
    outcome = _apply(state, "records__patient", ["prop__name"], mutation)
    assert outcome.units_affected == 1
    assert state.tables["records__patient"].data.column("prop__name").to_pylist() == [
        " Alice"
    ]


def test_truncate_no_mutation_when_length_within_max() -> None:
    state = _state(patient_rows=[_patient_row(name="Al")])
    mutation = MutationTruncate(kind="truncate", max_length=5)
    outcome = _apply(state, "records__patient", ["prop__name"], mutation)
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_truncate_keeps_prefix_when_length_exceeds_max() -> None:
    state = _state(patient_rows=[_patient_row(name="Alexandria")])
    mutation = MutationTruncate(kind="truncate", max_length=4)
    outcome = _apply(state, "records__patient", ["prop__name"], mutation)
    assert outcome.units_affected == 1
    assert outcome.defects[0].defect_class == "truncated_value"
    assert state.tables["records__patient"].data.column("prop__name").to_pylist() == [
        "Alex"
    ]


# ---------------------------------------------------------------------------
# Handler, per kind (Phase 2: precision_drop, scale, mojibake, format_dirt)
# ---------------------------------------------------------------------------


def test_precision_drop_rounds_half_to_even() -> None:
    state = _state(patient_rows=[_patient_row(weight_kg=2.5)])
    mutation = MutationPrecisionDrop(kind="precision_drop", digits=0)
    outcome = _apply(state, "records__patient", ["prop__weight_kg"], mutation)
    assert outcome.units_affected == 1
    assert outcome.defects[0].defect_class == "precision_drop"
    assert state.tables["records__patient"].data.column(
        "prop__weight_kg"
    ).to_pylist() == [2.0]


def test_precision_drop_no_mutation_when_already_equal_at_precision() -> None:
    state = _state(patient_rows=[_patient_row(weight_kg=2.0)])
    mutation = MutationPrecisionDrop(kind="precision_drop", digits=0)
    outcome = _apply(state, "records__patient", ["prop__weight_kg"], mutation)
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_scale_on_double_stores_the_product() -> None:
    state = _state(patient_rows=[_patient_row(weight_kg=2.5)])
    mutation = MutationScale(kind="scale", factor=1000.0)
    outcome = _apply(state, "records__patient", ["prop__weight_kg"], mutation)
    assert outcome.units_affected == 1
    assert outcome.defects[0].defect_class == "scaled_value"
    assert state.tables["records__patient"].data.column(
        "prop__weight_kg"
    ).to_pylist() == [2500.0]


def test_scale_on_bigint_stores_round_half_to_even() -> None:
    state = _state(patient_rows=[_patient_row(age=3)])
    mutation = MutationScale(kind="scale", factor=0.5)
    outcome = _apply(state, "records__patient", ["prop__age"], mutation)
    assert outcome.units_affected == 1
    # 3 * 0.5 = 1.5 -> round-half-to-even -> 2
    assert state.tables["records__patient"].data.column("prop__age").to_pylist() == [2]


def test_scale_on_bigint_raises_overflow_on_out_of_range_product() -> None:
    state = _state(patient_rows=[_patient_row(age=2**62)])
    mutation = MutationScale(kind="scale", factor=1000.0)
    with pytest.raises(OverflowError):
        _apply(state, "records__patient", ["prop__age"], mutation)


def test_scale_of_zero_cell_is_no_mutation() -> None:
    state = _state(patient_rows=[_patient_row(age=0)])
    mutation = MutationScale(kind="scale", factor=1000.0)
    outcome = _apply(state, "records__patient", ["prop__age"], mutation)
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_mojibake_maps_cafe_to_mojibake() -> None:
    state = _state(patient_rows=[_patient_row(name="café")])
    mutation = MutationMojibake(kind="mojibake")
    outcome = _apply(state, "records__patient", ["prop__name"], mutation)
    assert outcome.units_affected == 1
    assert outcome.defects[0].defect_class == "mojibake_value"
    assert state.tables["records__patient"].data.column("prop__name").to_pylist() == [
        "cafÃ©"
    ]


def test_mojibake_is_no_mutation_for_pure_ascii() -> None:
    state = _state(patient_rows=[_patient_row(name="Alice")])
    mutation = MutationMojibake(kind="mojibake")
    outcome = _apply(state, "records__patient", ["prop__name"], mutation)
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_format_dirt_inserts_separators_into_all_digit_string() -> None:
    state = _state(patient_rows=[_patient_row(name="12345")])
    mutation = MutationFormatDirt(kind="format_dirt")
    outcome = _apply(state, "records__patient", ["prop__name"], mutation)
    assert outcome.units_affected == 1
    assert outcome.defects[0].defect_class == "format_dirt"
    assert state.tables["records__patient"].data.column("prop__name").to_pylist() == [
        "12,345"
    ]


def test_format_dirt_no_mutation_below_four_digits() -> None:
    state = _state(patient_rows=[_patient_row(name="123")])
    mutation = MutationFormatDirt(kind="format_dirt")
    outcome = _apply(state, "records__patient", ["prop__name"], mutation)
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_format_dirt_no_mutation_for_non_digit_string() -> None:
    state = _state(patient_rows=[_patient_row(name="Alice")])
    mutation = MutationFormatDirt(kind="format_dirt")
    outcome = _apply(state, "records__patient", ["prop__name"], mutation)
    assert outcome.units_affected == 0
    assert outcome.defects == ()


# ---------------------------------------------------------------------------
# NULL-invariance
# ---------------------------------------------------------------------------


def test_null_cell_enumerated_but_never_mutated() -> None:
    state = _state(patient_rows=[_patient_row(name=None)])
    mutation = MutationCase(kind="case", form="upper")
    outcome = _apply(state, "records__patient", ["prop__name"], mutation)
    assert outcome.units_selected == 1
    assert outcome.units_affected == 0
    assert outcome.defects == ()
    assert state.tables["records__patient"].data.column("prop__name").to_pylist() == [
        None
    ]


# ---------------------------------------------------------------------------
# Impact
# ---------------------------------------------------------------------------


def test_tracked_prop_with_matching_series_declares_c6_after_mutation() -> None:
    state = _state(
        patient_rows=[_patient_row(record_id="p1", dob="1980-01-01")],
        history_rows=[
            _history_row(
                kind="patient",
                record_id="p1",
                property_="dob",
                sim_time=10,
                value="1980-01-01",
            )
        ],
    )
    mutation = MutationSentinel(kind="sentinel", value="1900-01-01")
    outcome = _apply(state, "records__patient", ["prop__dob"], mutation)
    assert outcome.defects[0].impact == ("C6",)


def test_untracked_prop_declares_beyond_c1_c12() -> None:
    state = _state(patient_rows=[_patient_row(record_id="p1", dob="1980-01-01")])
    mutation = MutationSentinel(kind="sentinel", value="1900-01-01")
    outcome = _apply(state, "records__patient", ["prop__dob"], mutation)
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


def test_sentinel_actor_type_undeclared_subtype_declares_c12() -> None:
    state = _state(actor_rows=[_actor_row(record_id="a1", actor_type="doctor")])
    roles = RecordRoles(_registry={"actor": {"doctor": "dimension", "nurse": "fact"}})
    mutation = MutationSentinel(kind="sentinel", value="ghost")
    outcome = _apply(
        state,
        "records__actor",
        ["prop__actor_type"],
        mutation,
        sidecar_obj=_sidecar(record_roles=roles),
    )
    assert outcome.defects[0].impact == ("C12",)


def test_sentinel_actor_type_undeclared_and_tracked_declares_union() -> None:
    state = _state(
        actor_rows=[_actor_row(record_id="a1", actor_type="doctor")],
        history_rows=[
            _history_row(
                kind="actor",
                record_id="a1",
                property_="actor_type",
                sim_time=10,
                value="doctor",
            )
        ],
    )
    roles = RecordRoles(_registry={"actor": {"doctor": "dimension", "nurse": "fact"}})
    mutation = MutationSentinel(kind="sentinel", value="ghost")
    outcome = _apply(
        state,
        "records__actor",
        ["prop__actor_type"],
        mutation,
        sidecar_obj=_sidecar(record_roles=roles),
    )
    assert set(outcome.defects[0].impact) == {"C6", "C12"}


def test_record_roles_absent_means_no_c12() -> None:
    state = _state(actor_rows=[_actor_row(record_id="a1", actor_type="doctor")])
    mutation = MutationSentinel(kind="sentinel", value="ghost")
    outcome = _apply(
        state,
        "records__actor",
        ["prop__actor_type"],
        mutation,
        sidecar_obj=_sidecar(record_roles=None),
    )
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


def test_presentation_id_never_declares_c6_or_c12() -> None:
    state = _state(patient_rows=[_patient_row(record_id="p1", presentation_id="OLD")])
    mutation = MutationSentinel(kind="sentinel", value="NEW")
    outcome = _apply(state, "records__patient", ["presentation_id"], mutation)
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


def test_elem_column_never_declares_c6_or_c12() -> None:
    state = _state(membership_rows=[_membership_row(record_id="p1", slot="a")])
    mutation = MutationCase(kind="case", form="upper")
    outcome = _apply(state, "membership__patient__ward", ["elem__slot"], mutation)
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


def test_scale_on_tracked_numeric_prop_declares_c6() -> None:
    state = _state(
        patient_rows=[_patient_row(record_id="p1", weight_kg=70.0)],
        history_rows=[
            _history_row(
                kind="patient",
                record_id="p1",
                property_="weight_kg",
                sim_time=10,
                value="70.0",
            )
        ],
    )
    mutation = MutationScale(kind="scale", factor=1000.0)
    outcome = _apply(state, "records__patient", ["prop__weight_kg"], mutation)
    assert outcome.defects[0].impact == ("C6",)


@pytest.mark.parametrize(
    ("column", "slot", "mutation"),
    [
        (
            "elem__weight_kg",
            "A",
            MutationPrecisionDrop(kind="precision_drop", digits=0),
        ),
        ("elem__weight_kg", "A", MutationScale(kind="scale", factor=1000.0)),
        ("elem__slot", "café", MutationMojibake(kind="mojibake")),
        ("elem__slot", "1234", MutationFormatDirt(kind="format_dirt")),
    ],
)
def test_phase_2_kinds_on_elem_column_declare_beyond_c1_c12(
    column: str, slot: str, mutation: MutationSpec
) -> None:
    state = _state(
        membership_rows=[_membership_row(record_id="p1", slot=slot, weight_kg=2.5)]
    )
    outcome = _apply(state, "membership__patient__ward", [column], mutation)
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


# ---------------------------------------------------------------------------
# Mechanics
# ---------------------------------------------------------------------------


def test_units_affected_equals_len_defects() -> None:
    state = _state(
        patient_rows=[
            _patient_row(record_id="p1", name="alice"),
            _patient_row(record_id="p2", name="ALICE"),
        ]
    )
    mutation = MutationCase(kind="case", form="upper")
    outcome = _apply(state, "records__patient", ["prop__name"], mutation, count=2)
    assert outcome.units_affected == len(outcome.defects)
    # p2 is already upper (no-mutation); only p1 mutates.
    assert outcome.units_affected == 1


def test_rerun_with_same_seed_is_identical() -> None:
    rows = [
        _patient_row(record_id="p1", name="alice"),
        _patient_row(record_id="p2", name="bob"),
    ]
    state_a = _state(patient_rows=rows)
    state_b = _state(patient_rows=rows)
    mutation = MutationCase(kind="case", form="upper")
    outcome_a = _apply(
        state_a, "records__patient", ["prop__name"], mutation, count=1, seed=7
    )
    outcome_b = _apply(
        state_b, "records__patient", ["prop__name"], mutation, count=1, seed=7
    )
    assert outcome_a.defects == outcome_b.defects
    assert state_a.tables["records__patient"].data.equals(
        state_b.tables["records__patient"].data
    )


def test_rerun_with_same_seed_is_identical_mixing_phase_1_and_2_kinds() -> None:
    """Determinism re-checked over a config mixing a Phase 1 kind (case) and a
    Phase 2 kind (scale), applied sequentially to a shared state."""

    def _run() -> CorruptState:
        rows = [
            _patient_row(record_id="p1", name="alice", age=3),
            _patient_row(record_id="p2", name="bob", age=7),
        ]
        state = _state(patient_rows=rows)
        _apply(
            state,
            "records__patient",
            ["prop__name"],
            MutationCase(kind="case", form="upper"),
            count=1,
            seed=7,
        )
        _apply(
            state,
            "records__patient",
            ["prop__age"],
            MutationScale(kind="scale", factor=0.5),
            count=1,
            seed=11,
        )
        return state

    state_a = _run()
    state_b = _run()
    assert state_a.tables["records__patient"].data.equals(
        state_b.tables["records__patient"].data
    )


def test_correlated_placement_weights_the_draw() -> None:
    """p2's presentation_id matches the correlated condition and carries a
    heavy weight; with u=0.5 for both units, its key (0.5**(1/100)) beats
    p1's (0.5**(1/1)), so p2 is the sole draw regardless of row order."""
    state = _state(
        patient_rows=[
            _patient_row(record_id="p1", name="alice", presentation_id="OTHER"),
            _patient_row(record_id="p2", name="bob", presentation_id="MATCH"),
        ]
    )
    mutation = MutationCase(kind="case", form="upper")
    placement = Correlated(
        kind="correlated", column="presentation_id", value="MATCH", weight=100.0
    )
    outcome = _apply(
        state,
        "records__patient",
        ["prop__name"],
        mutation,
        count=1,
        placement=placement,
        rng=_FixedRandomValues([0.5, 0.5]),
    )
    assert outcome.units_selected == 1
    assert dict(outcome.defects[0].location.row.keys)["record_id"] == "p2"


def test_locator_is_cell_kind_with_row_ref() -> None:
    state = _state(patient_rows=[_patient_row(record_id="p1", name="alice")])
    mutation = MutationCase(kind="case", form="upper")
    outcome = _apply(state, "records__patient", ["prop__name"], mutation)
    loc = outcome.defects[0].location
    assert loc.kind == "cell"
    assert loc.table == "records__patient"
    assert loc.column == "prop__name"
    assert loc.row.category == "records"
    assert dict(loc.row.keys)["record_id"] == "p1"


def test_where_narrows_population() -> None:
    state = _state(
        patient_rows=[
            _patient_row(record_id="p1", name="alice", presentation_id="X"),
            _patient_row(record_id="p2", name="bob", presentation_id="Y"),
        ]
    )
    mutation = MutationCase(kind="case", form="upper")
    outcome = _apply(
        state,
        "records__patient",
        ["prop__name"],
        mutation,
        count=5,
        where={"presentation_id": "X"},
    )
    assert outcome.units_selected == 1
    assert dict(outcome.defects[0].location.row.keys)["record_id"] == "p1"


# ---------------------------------------------------------------------------
# RNG discipline (Phase 3): slot-(3) mode draws
# ---------------------------------------------------------------------------


def _rng_marker(seed: int, population_size: int, k: int, *, extra_draws: int) -> float:
    """Replay `draw_sample`'s single `.sample()` call plus `extra_draws` bare
    `.random()` calls on a fresh `random.Random(seed)`, then return the next
    `.random()` draw -- the "marker" a real apply() call's post-operation RNG
    position must match iff it consumed exactly `extra_draws` `.random()`
    calls beyond the unit draw."""
    rng = random.Random(seed)
    rng.sample(range(population_size), k)
    for _ in range(extra_draws):
        rng.random()
    return rng.random()


def test_deterministic_kind_draws_no_rng() -> None:
    """A deterministic kind's `.random()` draw count beyond the unit draw is
    zero -- the post-operation RNG position matches a bare `.sample()` replay
    with no extra draws (draw-count parity)."""
    state = _state(
        patient_rows=[
            _patient_row(record_id="p1", name="alice"),
            _patient_row(record_id="p2", name="bob"),
            _patient_row(record_id="p3", name="carol"),
        ]
    )
    seed = 123
    rng = random.Random(seed)
    _apply(
        state,
        "records__patient",
        ["prop__name"],
        MutationCase(kind="case", form="upper"),
        count=3,
        rng=rng,
    )
    assert rng.random() == _rng_marker(seed, 3, 3, extra_draws=0)


@pytest.mark.parametrize(
    "mutation",
    [
        MutationTypo(kind="typo"),
        MutationResample(kind="resample"),
        MutationOutOfDomain(kind="out_of_domain"),
    ],
)
def test_seeded_kind_draws_exactly_one_random_per_selected_unit(
    mutation: MutationSpec,
) -> None:
    """A seeded kind's `.random()` draw count beyond the unit draw equals
    `units_selected` -- draw-count parity against a bare `.sample()` replay
    with exactly that many extra draws."""
    state = _state(
        actor_rows=[
            _actor_row(record_id="a1", actor_type="doctor"),
            _actor_row(record_id="a2", actor_type="nurse"),
            _actor_row(record_id="a3", actor_type="medic"),
        ]
    )
    seed = 7
    rng = random.Random(seed)
    sidecar_obj = _sidecar(
        enum_domains={"actor": {"actor_type": ("doctor", "nurse", "medic")}}
    )
    outcome = _apply(
        state,
        "records__actor",
        ["prop__actor_type"],
        mutation,
        count=3,
        rng=rng,
        sidecar_obj=sidecar_obj,
    )
    assert outcome.units_selected == 3
    assert rng.random() == _rng_marker(seed, 3, 3, extra_draws=3)


def test_seeded_kind_draw_consumed_for_null_cell() -> None:
    """A NULL cell is a selected unit; its mode draw is still consumed even
    though the cell is never mutated (RNG cost is a fixed function of the
    selected-unit count)."""
    state = _state(patient_rows=[_patient_row(name=None)])
    seed = 42
    rng = random.Random(seed)
    outcome = _apply(
        state,
        "records__patient",
        ["prop__name"],
        MutationResample(kind="resample"),
        rng=rng,
    )
    assert outcome.units_selected == 1
    assert outcome.units_affected == 0
    assert rng.random() == _rng_marker(seed, 1, 1, extra_draws=1)


# ---------------------------------------------------------------------------
# typo
# ---------------------------------------------------------------------------


class _FixedSeed(random.Random):
    """A `random.Random` whose `.random()` always returns a fixed value; every
    other draw uses the real stream seeded from `seed`."""

    def __init__(self, value: float, seed: int = 0) -> None:
        super().__init__(seed)
        self._value = value

    def random(self) -> float:
        return self._value


def test_typo_str_exchanges_adjacent_chars_at_seeded_position() -> None:
    state = _state(patient_rows=[_patient_row(name="abcdef")])
    outcome = _apply(
        state,
        "records__patient",
        ["prop__name"],
        MutationTypo(kind="typo"),
        rng=_FixedSeed(0.0),
    )
    assert outcome.units_affected == 1
    assert outcome.defects[0].defect_class == "typo_value"
    assert state.tables["records__patient"].data.column("prop__name").to_pylist() == [
        "bacdef"
    ]


def test_typo_str_single_char_is_cannot_apply() -> None:
    state = _state(patient_rows=[_patient_row(name="a")])
    outcome = _apply(
        state,
        "records__patient",
        ["prop__name"],
        MutationTypo(kind="typo"),
        rng=_FixedSeed(0.0),
    )
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_typo_str_equal_neighbors_is_no_mutation() -> None:
    state = _state(patient_rows=[_patient_row(name="aa")])
    outcome = _apply(
        state,
        "records__patient",
        ["prop__name"],
        MutationTypo(kind="typo"),
        rng=_FixedSeed(0.0),
    )
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_typo_bigint_preserves_sign() -> None:
    state = _state(patient_rows=[_patient_row(age=-12)])
    outcome = _apply(
        state,
        "records__patient",
        ["prop__age"],
        MutationTypo(kind="typo"),
        rng=_FixedSeed(0.0),
    )
    assert outcome.units_affected == 1
    assert state.tables["records__patient"].data.column("prop__age").to_pylist() == [
        -21
    ]


def test_typo_bigint_leading_zero_result_parses_smaller() -> None:
    state = _state(patient_rows=[_patient_row(age=102)])
    outcome = _apply(
        state,
        "records__patient",
        ["prop__age"],
        MutationTypo(kind="typo"),
        rng=_FixedSeed(0.0),
    )
    assert outcome.units_affected == 1
    assert state.tables["records__patient"].data.column("prop__age").to_pylist() == [12]


def test_typo_bigint_19_digit_overflow_is_cannot_apply() -> None:
    """9223372036854775801 (fits int64) swapped at the last digit pair becomes
    9223372036854775810, which overflows int64 -- cannot-apply: nothing
    written, no defect, no crash."""
    state = _state(patient_rows=[_patient_row(age=9223372036854775801)])
    original = state.tables["records__patient"].data
    outcome = _apply(
        state,
        "records__patient",
        ["prop__age"],
        MutationTypo(kind="typo"),
        rng=_FixedSeed(0.95),
    )
    assert outcome.units_affected == 0
    assert outcome.defects == ()
    assert state.tables["records__patient"].data.equals(original)


# ---------------------------------------------------------------------------
# resample
# ---------------------------------------------------------------------------


def test_resample_draws_a_real_other_value_ascending_total_order() -> None:
    state = _state(
        patient_rows=[
            _patient_row(record_id="p1", name="alice"),
            _patient_row(record_id="p2", name="bob"),
            _patient_row(record_id="p3", name="carol"),
        ]
    )
    outcome = _apply(
        state,
        "records__patient",
        ["prop__name"],
        MutationResample(kind="resample"),
        where={"record_id": "p1"},
        rng=_FixedSeed(0.0),
    )
    assert outcome.units_affected == 1
    # donor pool excluding "alice", ascending: ["bob", "carol"]; seed 0.0 -> "bob"
    assert (
        state.tables["records__patient"].data.column("prop__name").to_pylist()[0]
        == "bob"
    )


def test_resample_ignores_target_where_for_donor_pool() -> None:
    """The where filter narrows the sampled population to p1, but the donor
    pool draws from the whole table (never narrowed by target.where)."""
    state = _state(
        patient_rows=[
            _patient_row(record_id="p1", name="alice", presentation_id="X"),
            _patient_row(record_id="p2", name="bob", presentation_id="Y"),
            _patient_row(record_id="p3", name="carol", presentation_id="Z"),
        ]
    )
    outcome = _apply(
        state,
        "records__patient",
        ["prop__name"],
        MutationResample(kind="resample"),
        where={"presentation_id": "X"},
        rng=_FixedSeed(0.99),
    )
    assert outcome.units_affected == 1
    mutated = state.tables["records__patient"].data.column("prop__name").to_pylist()[0]
    assert mutated in ("bob", "carol")


def test_resample_empty_donor_pool_is_no_mutation() -> None:
    """A constant column (single distinct value) has an empty donor pool once
    the current value is excluded."""
    state = _state(patient_rows=[_patient_row(record_id="p1", name="alice")])
    outcome = _apply(
        state,
        "records__patient",
        ["prop__name"],
        MutationResample(kind="resample"),
        rng=_FixedSeed(0.0),
    )
    assert outcome.units_affected == 0
    assert outcome.defects == ()


# ---------------------------------------------------------------------------
# out_of_domain
# ---------------------------------------------------------------------------


def test_out_of_domain_result_is_outside_domain_and_not_original() -> None:
    state = _state(actor_rows=[_actor_row(record_id="a1", actor_type="doctor")])
    sidecar_obj = _sidecar(enum_domains={"actor": {"actor_type": ("doctor", "nurse")}})
    outcome = _apply(
        state,
        "records__actor",
        ["prop__actor_type"],
        MutationOutOfDomain(kind="out_of_domain"),
        rng=_FixedSeed(0.0),
        sidecar_obj=sidecar_obj,
    )
    assert outcome.units_affected == 1
    mutated = (
        state.tables["records__actor"].data.column("prop__actor_type").to_pylist()[0]
    )
    assert mutated not in ("doctor", "nurse")


def test_out_of_domain_fallback_fires_when_transpositions_stay_in_domain() -> None:
    """A single-char value has no transposition candidates; the fallback
    (repeated final-character append) fires and must skip a domain member."""
    state = _state(actor_rows=[_actor_row(record_id="a1", actor_type="a")])
    sidecar_obj = _sidecar(enum_domains={"actor": {"actor_type": ("a", "aa")}})
    outcome = _apply(
        state,
        "records__actor",
        ["prop__actor_type"],
        MutationOutOfDomain(kind="out_of_domain"),
        rng=_FixedSeed(0.0),
        sidecar_obj=sidecar_obj,
    )
    assert outcome.units_affected == 1
    assert state.tables["records__actor"].data.column(
        "prop__actor_type"
    ).to_pylist() == ["aaa"]


def test_out_of_domain_empty_value_is_no_mutation() -> None:
    state = _state(actor_rows=[_actor_row(record_id="a1", actor_type="")])
    sidecar_obj = _sidecar(enum_domains={"actor": {"actor_type": ("doctor", "nurse")}})
    outcome = _apply(
        state,
        "records__actor",
        ["prop__actor_type"],
        MutationOutOfDomain(kind="out_of_domain"),
        rng=_FixedSeed(0.0),
        sidecar_obj=sidecar_obj,
    )
    assert outcome.units_affected == 0
    assert outcome.defects == ()


# ---------------------------------------------------------------------------
# Impact (Phase 3)
# ---------------------------------------------------------------------------


def test_out_of_domain_on_actor_type_declares_c12() -> None:
    state = _state(actor_rows=[_actor_row(record_id="a1", actor_type="doctor")])
    roles = RecordRoles(_registry={"actor": {"doctor": "dimension", "nurse": "fact"}})
    sidecar_obj = _sidecar(
        record_roles=roles,
        enum_domains={"actor": {"actor_type": ("doctor", "nurse")}},
    )
    outcome = _apply(
        state,
        "records__actor",
        ["prop__actor_type"],
        MutationOutOfDomain(kind="out_of_domain"),
        rng=_FixedSeed(0.0),
        sidecar_obj=sidecar_obj,
    )
    assert outcome.defects[0].impact == ("C12",)


def test_resample_drawing_back_a_prior_corruption_heals_to_beyond_c1_c12() -> None:
    """p1's dob is tracked; an earlier sentinel corrupts it away from the
    history anchor (C6). A later resample draws p1's pre-corruption value
    back (present via p2's real value); the round-trip now passes, so the
    resample's own defect is `beyond-c1-c12` -- the healing stance."""
    state = _state(
        patient_rows=[
            _patient_row(record_id="p1", dob="1980-01-01"),
            _patient_row(record_id="p2", dob="1980-01-01"),
        ],
        history_rows=[
            _history_row(
                kind="patient",
                record_id="p1",
                property_="dob",
                sim_time=10,
                value="1980-01-01",
            )
        ],
    )
    first = _apply(
        state,
        "records__patient",
        ["prop__dob"],
        MutationSentinel(kind="sentinel", value="1900-01-01"),
        where={"record_id": "p1"},
    )
    assert first.defects[0].impact == ("C6",)

    second = _apply(
        state,
        "records__patient",
        ["prop__dob"],
        MutationResample(kind="resample"),
        where={"record_id": "p1"},
        rng=_FixedSeed(0.0),
    )
    assert second.units_affected == 1
    assert (
        state.tables["records__patient"].data.column("prop__dob").to_pylist()[0]
        == "1980-01-01"
    )
    assert second.defects[0].impact == ("beyond-c1-c12",)


# ---------------------------------------------------------------------------
# history.value territory (Phase 4)
# ---------------------------------------------------------------------------


def test_resample_donor_pool_narrowed_to_same_kind_and_property() -> None:
    """resample's donor pool for history.value pools only rows of the same
    (kind, property) -- a name is never drawn into a weight series."""
    state = _state(
        history_rows=[
            _history_row(
                kind="patient",
                record_id="p1",
                property_="name",
                sim_time=10,
                value="Alice",
            ),
            _history_row(
                kind="patient",
                record_id="p1",
                property_="name",
                sim_time=20,
                value="Bob",
            ),
            _history_row(
                kind="patient",
                record_id="p1",
                property_="weight_kg",
                sim_time=10,
                value="50.0",
            ),
            _history_row(
                kind="patient",
                record_id="p1",
                property_="weight_kg",
                sim_time=20,
                value="60.0",
            ),
        ],
    )
    outcome = _apply(
        state,
        "history",
        ["value"],
        MutationResample(kind="resample"),
        where={"property": "name", "sim_time": "10"},
        rng=_FixedSeed(0.0),
    )
    assert outcome.units_affected == 1
    # The selected row (sim_time=10, "Alice") is the sole eligible cell;
    # its donor pool is the "name" series only ({"Bob"}), never "weight_kg"'s
    # values -- the weight_kg rows are untouched.
    assert state.tables["history"].data.column("value").to_pylist() == [
        "Bob",
        "Bob",
        "50.0",
        "60.0",
    ]


def test_history_value_installation_via_tie_break_declares_c6_when_round_trip_fails() -> (
    None
):
    """A same-tick non-anchor row mutated to rank above the anchor becomes
    the post-state anchor (the `value DESC` tie-break); participation holds,
    so `C6` iff the round-trip now fails."""
    state = _state(
        patient_rows=[_patient_row(record_id="p1", name="Alice")],
        history_rows=[
            _history_row(
                kind="patient",
                record_id="p1",
                property_="name",
                sim_time=40,
                value="Alice",
            ),
            _history_row(
                kind="patient",
                record_id="p1",
                property_="name",
                sim_time=40,
                value="Aaron",
            ),
        ],
    )
    outcome = _apply(
        state,
        "history",
        ["value"],
        MutationSentinel(kind="sentinel", value="Zed"),
        where={"value": "Aaron"},
    )
    assert outcome.units_affected == 1
    assert outcome.defects[0].impact == ("C6",)


def test_history_value_demotion_of_anchor_declares_c6_when_round_trip_fails() -> None:
    """Mutating the operation-start anchor to a value ranking below a
    same-tick sibling demotes it -- the sibling becomes the post-state
    anchor, but operation-start participation still holds -- `C6` iff the
    round-trip now fails."""
    state = _state(
        patient_rows=[_patient_row(record_id="p1", name="Alice")],
        history_rows=[
            _history_row(
                kind="patient",
                record_id="p1",
                property_="name",
                sim_time=40,
                value="Alice",
            ),
            _history_row(
                kind="patient",
                record_id="p1",
                property_="name",
                sim_time=40,
                value="Aaron",
            ),
        ],
    )
    outcome = _apply(
        state,
        "history",
        ["value"],
        MutationSentinel(kind="sentinel", value="Aaa"),
        where={"value": "Alice"},
    )
    assert outcome.units_affected == 1
    assert outcome.defects[0].impact == ("C6",)


def test_history_value_mutation_restoring_anchor_codec_text_heals_to_beyond_c1_c12() -> (
    None
):
    """A mutation whose result equals the anchor's would-be codec text passes
    the round-trip -- `beyond-c1-c12`, even though the row held the
    (round-trip-broken) anchor before this operation."""
    state = _state(
        patient_rows=[_patient_row(record_id="p1", name="Alice")],
        history_rows=[
            _history_row(
                kind="patient",
                record_id="p1",
                property_="name",
                sim_time=10,
                value="Bogus",
            )
        ],
    )
    outcome = _apply(
        state, "history", ["value"], MutationSentinel(kind="sentinel", value="Alice")
    )
    assert outcome.units_affected == 1
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


def test_history_value_post_slice_row_never_participates_in_anchor() -> None:
    """A post-slice_at row can never equal a valid anchor pair
    (`resolve_c6_anchor` excludes rows past `slice_at`): even when the
    series' round-trip fails via the anchor row's own mutation in the same
    operation, the post-slice row's own defect stays `beyond-c1-c12`."""
    state = _state(
        patient_rows=[_patient_row(record_id="p1", name="Alice")],
        history_rows=[
            _history_row(
                kind="patient",
                record_id="p1",
                property_="name",
                sim_time=40,
                value="Alice",
            ),
            _history_row(
                kind="patient",
                record_id="p1",
                property_="name",
                sim_time=150,
                value="OldFuture",
            ),
        ],
    )
    outcome = _apply(
        state,
        "history",
        ["value"],
        MutationSentinel(kind="sentinel", value="Bob"),
        count=2,
    )
    assert outcome.units_affected == 2
    by_sim_time = {
        dict(d.location.row.keys)["sim_time"]: d.impact for d in outcome.defects
    }
    assert by_sim_time["40"] == ("C6",)
    assert by_sim_time["150"] == ("beyond-c1-c12",)


def test_mutated_history_value_participates_in_later_operations_anchor_resolution() -> (
    None
):
    """A history.value mutation from an earlier operation is working-set
    truth for a later operation's anchor/round-trip lookup."""
    state = _state(
        patient_rows=[_patient_row(record_id="p1", name="Alice")],
        history_rows=[
            _history_row(
                kind="patient",
                record_id="p1",
                property_="name",
                sim_time=40,
                value="Alice",
            )
        ],
    )
    first = _apply(
        state, "history", ["value"], MutationSentinel(kind="sentinel", value="Carol")
    )
    assert first.defects[0].impact == ("C6",)

    second = _apply(
        state,
        "records__patient",
        ["prop__name"],
        MutationSentinel(kind="sentinel", value="Carol"),
    )
    assert second.defects[0].impact == ("beyond-c1-c12",)


def test_history_value_impact_raises_when_working_history_absent() -> None:
    """`CorruptError` when the working `history` table is absent from the
    state during impact resolution -- an engine-invariant breach."""
    state = _state(patient_rows=[_patient_row(record_id="p1", dob="1980-01-01")])
    del state.tables["history"]
    mutation = MutationSentinel(kind="sentinel", value="1900-01-01")
    with pytest.raises(CorruptError, match="working set carries no 'history' table"):
        _apply(state, "records__patient", ["prop__dob"], mutation)
