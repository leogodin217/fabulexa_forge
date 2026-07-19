"""Tests for the `insert_rows` corrupter handler -- phantom-row injection."""

from __future__ import annotations

import random
from collections.abc import Sequence

from fabulexa_forge.config.models import Amount, DeleteRows, InsertRows, Target
from fabulexa_forge.corrupters.operations._mutations import swap_adjacent
from fabulexa_forge.corrupters.operations.dangle_reference import DANGLING_ID_PREFIX
from fabulexa_forge.corrupters.operations.delete_rows import DeleteRowsCorrupter
from fabulexa_forge.corrupters.operations.insert_rows import InsertRowsCorrupter
from fabulexa_forge.corrupters.state import CorruptState
from fabulexa_forge.reader.sidecar import BranchEntry, Sidecar

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
_HANDLER = InsertRowsCorrupter()
_DELETE_HANDLER = DeleteRowsCorrupter()


class _FixedRandomValues(random.Random):
    """A `random.Random` whose `.random()` returns a fixed sequence, cycling
    if exhausted -- pins the per-phantom id-rotation and resample draws so a
    test can force an exact seeded outcome."""

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


def _patient_spec() -> "object":
    return table_spec(
        "records__patient",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("record_index", "BIGINT"),
            column_spec("presentation_id", "VARCHAR"),
            column_spec("prop__age", "BIGINT"),
            column_spec("prop__name", "VARCHAR"),
            column_spec("prop__doctor_id", "VARCHAR", references="doctor"),
            column_spec("ref_index__doctor_id", "BIGINT"),
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
            column_spec("record_index", "BIGINT"),
            column_spec("presentation_id", "VARCHAR"),
            column_spec("prop__name", "VARCHAR"),
            column_spec("prop__specialty", "VARCHAR"),
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
    presentation_id: object = None,
    age: object = None,
    name: object = None,
    doctor_id: object = None,
    ref_index_doctor_id: object = None,
) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "record_id": record_id,
        "presentation_id": presentation_id,
        "prop__age": age,
        "prop__name": name,
        "prop__doctor_id": doctor_id,
        "ref_index__doctor_id": ref_index_doctor_id,
    }


def _doctor_row(
    record_id: str,
    *,
    presentation_id: object = None,
    name: object = None,
    specialty: object = None,
) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "record_id": record_id,
        "presentation_id": presentation_id,
        "prop__name": name,
        "prop__specialty": specialty,
    }


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
    record_id: str, consultant_kind: str, consultant_id: str
) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "record_id": record_id,
        "joined_sim_time": 10,
        "left_sim_time": None,
        "elem__slot": "morning",
        "member__consultant__kind": consultant_kind,
        "member__consultant__id": consultant_id,
    }


def _numbered(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """`rows` with a `record_index` cell assigned by list position (dense,
    0-based) -- the fixture's stand-in for the contract's creation-order
    numbering, so `CorruptState.__post_init__` captures a real high-water
    mark for these records-category tables."""
    return [dict(row, record_index=i) for i, row in enumerate(rows)]


def _state(
    *,
    patients: list[dict[str, object]] | None = None,
    doctors: list[dict[str, object]] | None = None,
    history_rows: list[dict[str, object]] | None = None,
    ward_rows: list[dict[str, object]] | None = None,
    deleted_record_ids: dict[str, set[str]] | None = None,
) -> CorruptState:
    return CorruptState(
        tables={
            "history": working_table(_history_spec(), history_rows or []),
            "records__patient": working_table(
                _patient_spec(), _numbered(patients or [])
            ),
            "records__doctor": working_table(_doctor_spec(), _numbered(doctors or [])),
            "membership__patient__ward": working_table(_ward_spec(), ward_rows or []),
        },
        deleted_record_ids=deleted_record_ids or {},
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
    columns: list[str] | None = None,
    where: dict[str, str] | None = None,
) -> InsertRows:
    return InsertRows(
        kind="insert_rows",
        target=Target(table=table, columns=columns, where=where),
        amount=amount,
    )


def _apply(
    state: CorruptState, op: InsertRows, sc: Sidecar, rng: random.Random
) -> object:
    return _HANDLER.apply(state, op, "rule#0", rng, _FORK_PATH, sc)


# ---------------------------------------------------------------------------
# Phantom assembly: verbatim clone except record_id
# ---------------------------------------------------------------------------


def test_phantom_cloned_verbatim_except_record_id() -> None:
    state = _state(
        doctors=[
            _doctor_row(
                "d1", presentation_id="pres-1", name="Dr. Smith", specialty="cardiology"
            )
        ]
    )
    sc = _sidecar()
    outcome = _apply(
        state, _op("records__doctor", Amount(count=1)), sc, random.Random(1)
    )
    assert outcome.units_affected == 1
    rows = state.tables["records__doctor"].data.to_pylist()
    assert len(rows) == 2
    donor = next(r for r in rows if r["record_id"] == "d1")
    phantom = next(r for r in rows if r["record_id"] != "d1")
    assert phantom["fork_path"] == donor["fork_path"]
    assert phantom["presentation_id"] == donor["presentation_id"]
    assert phantom["prop__name"] == donor["prop__name"]
    assert phantom["prop__specialty"] == donor["prop__specialty"]
    assert phantom["record_id"] != donor["record_id"]


# ---------------------------------------------------------------------------
# Id derivation
# ---------------------------------------------------------------------------


def test_id_derivation_repeated_adjacent_characters_skip_to_next_position() -> None:
    """Donor id 'aab': swapping positions (0,1) ('a','a') reproduces the
    donor's own id, already a member of the universe (working records ids)
    -- the derivation moves to position (1,2)."""
    state = _state(doctors=[_doctor_row("aab")])
    sc = _sidecar()
    outcome = _apply(
        state,
        _op("records__doctor", Amount(count=1)),
        sc,
        _FixedRandomValues([0.0]),
    )
    assert outcome.units_affected == 1
    ids = set(state.tables["records__doctor"].data.column("record_id").to_pylist())
    assert ids == {"aab", "aba"}


def test_id_derivation_first_candidate_absent_from_universe_wins() -> None:
    """seed 0.9 starts the rotation at position 1: swap_adjacent('abc', 1) ==
    'acb', absent from the universe, wins immediately."""
    state = _state(doctors=[_doctor_row("abc")])
    sc = _sidecar()
    outcome = _apply(
        state,
        _op("records__doctor", Amount(count=1)),
        sc,
        _FixedRandomValues([0.9]),
    )
    assert outcome.units_affected == 1
    ids = set(state.tables["records__doctor"].data.column("record_id").to_pylist())
    assert ids == {"abc", "acb"}


def test_id_derivation_total_fallback_appends_final_character_repeatedly() -> None:
    """Donor id 'ab': the sole adjacent-exchange candidate 'ba' and the first
    fallback candidate 'abb' are both taken; the derivation appends the final
    character again to reach 'abbb'."""
    state = _state(
        doctors=[_doctor_row("ab")],
        history_rows=[
            _history_row("doctor", "ba", "specialty", 1, "x"),
            _history_row("doctor", "abb", "specialty", 1, "x"),
        ],
    )
    sc = _sidecar()
    outcome = _apply(
        state,
        _op("records__doctor", Amount(count=1)),
        sc,
        _FixedRandomValues([0.0]),
    )
    assert outcome.units_affected == 1
    ids = set(state.tables["records__doctor"].data.column("record_id").to_pylist())
    assert ids == {"ab", "abbb"}


def test_id_derivation_empty_donor_id_falls_back_to_appending_zero() -> None:
    """An empty-string donor id has no exchange pair and no final character;
    the fallback appends '0'."""
    state = _state(doctors=[_doctor_row("")])
    sc = _sidecar()
    outcome = _apply(
        state,
        _op("records__doctor", Amount(count=1)),
        sc,
        _FixedRandomValues([0.0]),
    )
    assert outcome.units_affected == 1
    ids = set(state.tables["records__doctor"].data.column("record_id").to_pylist())
    assert ids == {"", "0"}


# ---------------------------------------------------------------------------
# Id universe: one test per surface
# ---------------------------------------------------------------------------


def test_id_universe_history_record_id_for_kind() -> None:
    state = _state(
        doctors=[_doctor_row("abc")],
        history_rows=[_history_row("doctor", "bac", "specialty", 1, "x")],
    )
    sc = _sidecar()
    outcome = _apply(
        state,
        _op("records__doctor", Amount(count=1)),
        sc,
        _FixedRandomValues([0.0]),
    )
    assert outcome.units_affected == 1
    ids = set(state.tables["records__doctor"].data.column("record_id").to_pylist())
    assert ids == {"abc", "acb"}


def test_id_universe_membership_partner_id_for_kind() -> None:
    state = _state(
        doctors=[_doctor_row("abc")],
        ward_rows=[_ward_row("p1", "doctor", "bac")],
    )
    sc = _sidecar()
    outcome = _apply(
        state,
        _op("records__doctor", Amount(count=1)),
        sc,
        _FixedRandomValues([0.0]),
    )
    assert outcome.units_affected == 1
    ids = set(state.tables["records__doctor"].data.column("record_id").to_pylist())
    assert ids == {"abc", "acb"}


def test_id_universe_reference_prop_cell_targeting_kind() -> None:
    state = _state(
        doctors=[_doctor_row("abc")],
        patients=[_patient_row("pat1", doctor_id="bac")],
    )
    sc = _sidecar()
    outcome = _apply(
        state,
        _op("records__doctor", Amount(count=1)),
        sc,
        _FixedRandomValues([0.0]),
    )
    assert outcome.units_affected == 1
    ids = set(state.tables["records__doctor"].data.column("record_id").to_pylist())
    assert ids == {"abc", "acb"}


def test_id_universe_sidecar_pin_for_kind() -> None:
    state = _state(doctors=[_doctor_row("abc")])
    sc = _sidecar(pinned_ids={"doctor": {"alice": "bac"}})
    outcome = _apply(
        state,
        _op("records__doctor", Amount(count=1)),
        sc,
        _FixedRandomValues([0.0]),
    )
    assert outcome.units_affected == 1
    ids = set(state.tables["records__doctor"].data.column("record_id").to_pylist())
    assert ids == {"abc", "acb"}


def test_id_universe_kind_tombstones() -> None:
    state = _state(
        doctors=[_doctor_row("abc")],
        deleted_record_ids={"doctor": {"bac"}},
    )
    sc = _sidecar()
    outcome = _apply(
        state,
        _op("records__doctor", Amount(count=1)),
        sc,
        _FixedRandomValues([0.0]),
    )
    assert outcome.units_affected == 1
    ids = set(state.tables["records__doctor"].data.column("record_id").to_pylist())
    assert ids == {"abc", "acb"}


def test_id_universe_earlier_phantoms_of_same_operation() -> None:
    """Both donors carry id 'z'; the first phantom (P1, ascending
    presentation_id order) claims 'zz', so the second donor's own fallback
    candidate 'zz' is already taken and it falls further to 'zzz'."""
    state = _state(
        doctors=[
            _doctor_row("z", presentation_id="P1"),
            _doctor_row("z", presentation_id="P2"),
        ]
    )
    sc = _sidecar()
    outcome = _apply(
        state,
        _op("records__doctor", Amount(count=2)),
        sc,
        _FixedRandomValues([0.0]),
    )
    assert outcome.units_affected == 2
    ids = state.tables["records__doctor"].data.column("record_id").to_pylist()
    assert sorted(ids) == ["z", "z", "zz", "zzz"]


def test_no_healing_dangling_sentinel_in_a_dangled_cell_is_in_the_universe() -> None:
    """A `DANGLING_ID_PREFIX` sentinel value already sitting in a dangled
    reference cell is a member of the id universe (the reference-prop
    surface) -- a phantom's id-derivation never reuses it, even when it is
    the very first rotation candidate."""
    dangling_id = f"{DANGLING_ID_PREFIX}1"
    donor_id = swap_adjacent(dangling_id, 5)
    assert swap_adjacent(donor_id, 5) == dangling_id  # sanity: first candidate collides
    state = _state(
        doctors=[_doctor_row(donor_id)],
        patients=[_patient_row("pat1", doctor_id=dangling_id)],
    )
    sc = _sidecar()
    outcome = _apply(
        state,
        _op("records__doctor", Amount(count=1)),
        sc,
        _FixedRandomValues([0.45]),
    )
    assert outcome.units_affected == 1
    ids = set(state.tables["records__doctor"].data.column("record_id").to_pylist())
    assert dangling_id not in ids
    assert donor_id in ids


# ---------------------------------------------------------------------------
# Resample
# ---------------------------------------------------------------------------


def test_resample_matched_eligible_cell_replaced_excluding_current_value() -> None:
    state = _state(
        doctors=[
            _doctor_row("d1", name="Alice"),
            _doctor_row("d2", name="Bob"),
            _doctor_row("d3", name="Carol"),
        ]
    )
    sc = _sidecar()
    outcome = _apply(
        state,
        _op(
            "records__doctor",
            Amount(count=1),
            columns=["prop__name"],
            where={"record_id": "d1"},
        ),
        sc,
        _FixedRandomValues([0.0]),
    )
    assert outcome.units_affected == 1
    rows = state.tables["records__doctor"].data.to_pylist()
    phantom = next(r for r in rows if r["record_id"] not in ("d1", "d2", "d3"))
    # donor pool excludes "Alice" (the donor's own current value), ascending:
    # ["Bob", "Carol"]; seed 0.0 -> index 0 -> "Bob"
    assert phantom["prop__name"] == "Bob"


def test_resample_empty_pool_leaves_cloned_value() -> None:
    """A constant column (single distinct value) has an empty donor pool once
    the current value is excluded -- the cloned value survives unchanged."""
    state = _state(doctors=[_doctor_row("d1", name="Solo")])
    sc = _sidecar()
    outcome = _apply(
        state,
        _op("records__doctor", Amount(count=1), columns=["prop__name"]),
        sc,
        _FixedRandomValues([0.0]),
    )
    assert outcome.units_affected == 1
    rows = state.tables["records__doctor"].data.to_pylist()
    phantom = next(r for r in rows if r["record_id"] != "d1")
    assert phantom["prop__name"] == "Solo"


def test_resample_null_cloned_cell_stays_null() -> None:
    state = _state(
        doctors=[
            _doctor_row("d1", name=None),
            _doctor_row("d2", name="Bob"),
        ]
    )
    sc = _sidecar()
    outcome = _apply(
        state,
        _op(
            "records__doctor",
            Amount(count=1),
            columns=["prop__name"],
            where={"record_id": "d1"},
        ),
        sc,
        _FixedRandomValues([0.0]),
    )
    assert outcome.units_affected == 1
    rows = state.tables["records__doctor"].data.to_pylist()
    phantom = next(r for r in rows if r["record_id"] not in ("d1", "d2"))
    assert phantom["prop__name"] is None


def test_resample_table_matching_zero_eligible_columns_still_contributes_pure_clone_phantoms() -> (  # noqa: E501
    None
):
    """A `category: records` target resolves both records__patient and
    records__doctor; `prop__specialty` matches only records__doctor. The
    patient population still contributes its whole donor set -- its phantom
    is a pure clone, with zero resample draws."""
    state = _state(
        patients=[_patient_row("p1", name="Alice")],
        doctors=[_doctor_row("d1", specialty="cardiology")],
    )
    sc = _sidecar()
    op = InsertRows(
        kind="insert_rows",
        target=Target(category="records", columns=["prop__specialty"]),
        amount=Amount(count=2),
    )
    outcome = _apply(state, op, sc, _FixedRandomValues([0.0]))
    assert outcome.units_affected == 2
    patient_rows = state.tables["records__patient"].data.to_pylist()
    doctor_rows = state.tables["records__doctor"].data.to_pylist()
    assert len(patient_rows) == 2
    assert len(doctor_rows) == 2
    patient_phantom = next(r for r in patient_rows if r["record_id"] != "p1")
    assert patient_phantom["prop__name"] == "Alice"


def test_multi_table_target_zero_selected_rows_in_one_table_leaves_it_untouched() -> (
    None
):
    """A `category: records` target resolves both records__doctor and
    records__patient; forcing every drawn unit into records__doctor leaves
    records__patient's phantom list empty -- the write-back loop's defensive
    `continue` for a population that ended up with zero phantoms."""
    state = _state(patients=[_patient_row("p1")], doctors=[_doctor_row("d1")])
    sc = _sidecar()
    op = InsertRows(
        kind="insert_rows",
        target=Target(category="records"),
        amount=Amount(count=1),
    )
    # Row units, canonical table order (records__doctor < records__patient):
    # unit 0 == records__doctor's sole row. Forcing the unit draw to [0]
    # selects only the doctor row.
    outcome = _apply(state, op, sc, FixedSampleRandom([0], seed=1))
    assert outcome.units_affected == 1
    assert state.tables["records__doctor"].data.num_rows == 2
    assert state.tables["records__patient"].data.num_rows == 1


# ---------------------------------------------------------------------------
# Amount
# ---------------------------------------------------------------------------


def test_amount_count_caps_at_population_size_without_replacement() -> None:
    state = _state(doctors=[_doctor_row("d1"), _doctor_row("d2"), _doctor_row("d3")])
    sc = _sidecar()
    outcome = _apply(
        state, _op("records__doctor", Amount(count=5)), sc, random.Random(1)
    )
    assert outcome.units_selected == 3
    assert outcome.units_affected == 3
    assert state.tables["records__doctor"].data.num_rows == 6


def test_amount_rate_floors_over_population() -> None:
    state = _state(doctors=[_doctor_row("d1"), _doctor_row("d2"), _doctor_row("d3")])
    sc = _sidecar()
    outcome = _apply(
        state, _op("records__doctor", Amount(rate=0.6)), sc, random.Random(1)
    )
    assert outcome.units_selected == 1
    assert outcome.units_affected == 1
    assert state.tables["records__doctor"].data.num_rows == 4


def test_amount_zero_donor_population_is_a_data_dependent_no_op() -> None:
    state = _state()
    sc = _sidecar()
    outcome = _apply(
        state, _op("records__doctor", Amount(count=1)), sc, random.Random(1)
    )
    assert outcome.units_selected == 0
    assert outcome.units_affected == 0
    assert outcome.defects == ()
    assert state.tables["records__doctor"].data.num_rows == 0


# ---------------------------------------------------------------------------
# record_index minting
# ---------------------------------------------------------------------------


def test_minted_record_index_ascends_above_rows_in_selected_unit_order() -> None:
    """A fresh table of 3 rows (record_index 0, 1, 2): phantoms mint 3, 4 --
    in ascending selected-unit order, the donor with the lower physical
    (canonical-order) position minting first."""
    state = _state(
        doctors=[
            _doctor_row("d1", name="D1"),
            _doctor_row("d2", name="D2"),
            _doctor_row("d3", name="D3"),
        ]
    )
    sc = _sidecar()
    # Canonical order == ascending record_id == d1, d2, d3 (physical rows 0,
    # 1, 2); forcing the unit draw to [0, 2] selects d1 then d3.
    outcome = _apply(
        state,
        _op("records__doctor", Amount(count=2)),
        sc,
        FixedSampleRandom([0, 2], seed=1),
    )
    assert outcome.units_affected == 2
    rows = state.tables["records__doctor"].data.to_pylist()
    phantom_of_d1 = next(
        r for r in rows if r["prop__name"] == "D1" and r["record_id"] != "d1"
    )
    phantom_of_d3 = next(
        r for r in rows if r["prop__name"] == "D3" and r["record_id"] != "d3"
    )
    assert phantom_of_d1["record_index"] == 3
    assert phantom_of_d3["record_index"] == 4


def test_minted_record_index_never_reuses_a_deleted_suffix_ordinal() -> None:
    """`delete_rows` removes the suffix rows (record_index 3, 4); a later
    `insert_rows` on the same table mints strictly above the pre-delete
    maximum (4), never the tombstoned 3 or 4 -- a current-max implementation
    would resurrect one of them."""
    state = _state(
        doctors=[
            _doctor_row("d1"),
            _doctor_row("d2"),
            _doctor_row("d3"),
            _doctor_row("d4"),
            _doctor_row("d5"),
        ]
    )
    sc = _sidecar()
    for suffix_id in ("d5", "d4"):
        delete_op = DeleteRows(
            kind="delete_rows",
            target=Target(table="records__doctor", where={"record_id": suffix_id}),
            amount=Amount(count=1),
        )
        _DELETE_HANDLER.apply(
            state, delete_op, "delete#0", random.Random(1), _FORK_PATH, sc
        )
    assert state.tables["records__doctor"].data.num_rows == 3

    outcome = _apply(
        state, _op("records__doctor", Amount(count=1)), sc, random.Random(1)
    )
    assert outcome.units_affected == 1
    minted = [
        r["record_index"]
        for r in state.tables["records__doctor"].data.to_pylist()
        if r["record_id"] not in ("d1", "d2", "d3")
    ]
    assert minted == [5]


def test_second_insert_rows_operation_continues_above_the_first_phantoms() -> None:
    """Two `insert_rows` applications against the same working state: the
    second's mint continues above the first's, the mark advances rather than
    resetting."""
    state = _state(doctors=[_doctor_row("d1"), _doctor_row("d2"), _doctor_row("d3")])
    sc = _sidecar()
    _apply(state, _op("records__doctor", Amount(count=1)), sc, FixedSampleRandom([0]))
    first_indices = set(
        state.tables["records__doctor"].data.column("record_index").to_pylist()
    )
    assert first_indices == {0, 1, 2, 3}

    _apply(state, _op("records__doctor", Amount(count=1)), sc, FixedSampleRandom([0]))
    second_indices = set(
        state.tables["records__doctor"].data.column("record_index").to_pylist()
    )
    assert second_indices == {0, 1, 2, 3, 4}


def test_phantom_ref_index_cell_clones_donor_verbatim() -> None:
    """A phantom's `ref_index__` sibling clones the donor's value verbatim --
    only `record_id` and `record_index` are freshly assigned."""
    state = _state(patients=[_patient_row("p1", doctor_id="d1", ref_index_doctor_id=7)])
    sc = _sidecar()
    outcome = _apply(
        state, _op("records__patient", Amount(count=1)), sc, random.Random(1)
    )
    assert outcome.units_affected == 1
    rows = state.tables["records__patient"].data.to_pylist()
    phantom = next(r for r in rows if r["record_id"] != "p1")
    assert phantom["prop__doctor_id"] == "d1"
    assert phantom["ref_index__doctor_id"] == 7
    assert phantom["record_index"] == 1


# ---------------------------------------------------------------------------
# Defect class, impact, and post-corruption locator
# ---------------------------------------------------------------------------


def test_defect_class_beyond_c1_c12_and_post_corruption_locator() -> None:
    state = _state(doctors=[_doctor_row("d1")])
    sc = _sidecar()
    outcome = _apply(
        state, _op("records__doctor", Amount(count=1)), sc, random.Random(1)
    )
    assert outcome.units_selected == outcome.units_affected == len(outcome.defects) == 1
    defect = outcome.defects[0]
    assert defect.defect_class == "phantom_row"
    assert defect.impact == ("beyond-c1-c12",)
    assert defect.location.table == "records__doctor"
    keys = dict(defect.location.row.keys)
    assert keys["record_id"] != "d1"  # the fresh phantom id, not the donor's


# ---------------------------------------------------------------------------
# RNG order and determinism
# ---------------------------------------------------------------------------


def test_rng_order_id_draw_then_one_draw_per_resolved_resample_column() -> None:
    """Per phantom, ascending selected-unit order: one draw for the
    id-derivation rotation, then one draw per resolved resample column, in
    resolved-column order -- 2 phantoms * (1 id draw + 2 column draws) == 6
    mode-draw `.random()` calls, beyond whatever `.sample()` itself consumes
    internally (`CallOrderRandom` overrides `.random()` but not
    `.getrandbits()`, so its own population walk also shows up as "random" --
    a bare replay with the same seed and population/k isolates that count)."""
    state = _state(
        doctors=[
            _doctor_row("d1", name="Alice", specialty="cardio"),
            _doctor_row("d2", name="Bob", specialty="derma"),
        ]
    )
    sc = _sidecar()
    rng = CallOrderRandom(seed=1)
    op = _op(
        "records__doctor", Amount(count=2), columns=["prop__name", "prop__specialty"]
    )
    _apply(state, op, sc, rng)
    assert rng.calls[0] == "sample"

    probe = CallOrderRandom(seed=1)
    probe.sample(range(2), 2)
    sample_internal_random_calls = probe.calls.count("random")

    mode_draws = rng.calls[1:]
    assert all(call == "random" for call in mode_draws)
    assert len(mode_draws) == sample_internal_random_calls + 6


def test_rerun_with_same_seed_is_identical() -> None:
    def _run() -> CorruptState:
        state = _state(
            doctors=[
                _doctor_row("d1", name="Alice"),
                _doctor_row("d2", name="Bob"),
            ]
        )
        sc = _sidecar()
        op = _op("records__doctor", Amount(count=1), columns=["prop__name"])
        _apply(state, op, sc, random.Random(9))
        return state

    state_a = _run()
    state_b = _run()
    assert state_a.tables["records__doctor"].data.equals(
        state_b.tables["records__doctor"].data
    )
