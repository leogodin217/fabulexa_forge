"""Tests for the corrupter engine's seeded selector surface: canonical content
order, target.where evaluation, and draw_sample."""

from __future__ import annotations

import random
from collections.abc import Sequence

import pyarrow as pa
import pytest

from fabulexa_export.config.models import (
    Amount,
    ClusteredTemporal,
    Correlated,
    EntityScoped,
    Target,
)
from fabulexa_export.corrupters.selection import (
    build_canonical_order_clause,
    build_predicate_clause,
    derive_row_weights,
    draw_sample,
    draw_weighted_sample,
    match_column_entries,
    resolve_population,
    resolve_target_tables,
)
from fabulexa_export.corrupters.state import WorkingTable
from fabulexa_export.errors import CorruptValidationError
from fabulexa_export.reader.sidecar import ColumnSpec, Sidecar, TableSpec

from ._helpers import column_spec, sidecar, table_spec, working_table

_FORK_PATH = "trunk"


def _patient_spec() -> TableSpec:
    return table_spec(
        "records__patient",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("age", "BIGINT"),
            column_spec("active", "BOOLEAN"),
            column_spec("name", "VARCHAR"),
        ),
        record_kind="patient",
    )


# ---------------------------------------------------------------------------
# Canonical content order
# ---------------------------------------------------------------------------


def test_canonical_order_orders_every_column_ascending_nulls_first() -> None:
    # fork_path and record_id are held constant so `age` is the sole
    # discriminating column, isolating the NULLS FIRST behavior.
    spec = table_spec(
        "records__patient",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("age", "BIGINT"),
        ),
        record_kind="patient",
    )
    wt = working_table(
        spec,
        [
            {"fork_path": _FORK_PATH, "record_id": "p1", "age": None},
            {"fork_path": _FORK_PATH, "record_id": "p1", "age": 40},
            {"fork_path": _FORK_PATH, "record_id": "p1", "age": 10},
        ],
    )
    population = resolve_population(wt, _FORK_PATH, None)
    assert population.column("age").to_pylist() == [None, 10, 40]


def test_canonical_order_independent_of_arrow_input_order() -> None:
    spec = _patient_spec()
    rows = [
        {
            "fork_path": _FORK_PATH,
            "record_id": f"p{i}",
            "age": i,
            "active": True,
            "name": "x",
        }
        for i in (3, 1, 2)
    ]
    wt_a = working_table(spec, rows)
    wt_b = working_table(spec, list(reversed(rows)))
    pop_a = resolve_population(wt_a, _FORK_PATH, None)
    pop_b = resolve_population(wt_b, _FORK_PATH, None)
    assert (
        pop_a.column("record_id").to_pylist() == pop_b.column("record_id").to_pylist()
    )


def test_canonical_order_byte_identical_rows_tie_as_multiset() -> None:
    spec = _patient_spec()
    row = {
        "fork_path": _FORK_PATH,
        "record_id": "p1",
        "age": 5,
        "active": True,
        "name": "dup",
    }
    wt = working_table(spec, [row, row, row])
    population = resolve_population(wt, _FORK_PATH, None)
    assert population.num_rows == 3
    assert set(population.column("record_id").to_pylist()) == {"p1"}


# ---------------------------------------------------------------------------
# target.where evaluation
# ---------------------------------------------------------------------------


def _mixed_type_wt() -> WorkingTable:
    spec = table_spec(
        "records__patient",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("age", "BIGINT"),
            column_spec("active", "BOOLEAN"),
            column_spec("name", "VARCHAR"),
        ),
        record_kind="patient",
    )
    return working_table(
        spec,
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "age": 40,
                "active": True,
                "name": "a",
            },
            {
                "fork_path": _FORK_PATH,
                "record_id": "p2",
                "age": 10,
                "active": False,
                "name": "b",
            },
        ],
    )


def test_where_matches_bigint_column_with_string_literal() -> None:
    wt = _mixed_type_wt()
    population = resolve_population(wt, _FORK_PATH, {"age": "40"})
    assert population.column("record_id").to_pylist() == ["p1"]


def test_where_matches_boolean_column() -> None:
    wt = _mixed_type_wt()
    population = resolve_population(wt, _FORK_PATH, {"active": "false"})
    assert population.column("record_id").to_pylist() == ["p2"]


def test_where_matches_varchar_column_needing_quoting() -> None:
    wt = _mixed_type_wt()
    population = resolve_population(wt, _FORK_PATH, {"name": "a"})
    assert population.column("record_id").to_pylist() == ["p1"]


def test_where_conjunction_of_two_keys() -> None:
    wt = _mixed_type_wt()
    population = resolve_population(wt, _FORK_PATH, {"age": "40", "active": "true"})
    assert population.column("record_id").to_pylist() == ["p1"]
    population_none = resolve_population(
        wt, _FORK_PATH, {"age": "40", "active": "false"}
    )
    assert population_none.num_rows == 0


def test_where_zero_matches_yields_empty_population() -> None:
    wt = _mixed_type_wt()
    population = resolve_population(wt, _FORK_PATH, {"age": "999"})
    assert population.num_rows == 0


def test_where_composes_with_prior_mutations() -> None:
    """A where filter selects against the working table's *current* content,
    not a re-read of the source."""
    spec = _patient_spec()
    wt = working_table(
        spec,
        [
            {
                "fork_path": _FORK_PATH,
                "record_id": "p1",
                "age": 40,
                "active": True,
                "name": "a",
            }
        ],
    )
    # Simulate a prior operation's mutation: age changed in place, not re-read.
    mutated_age = pa.array([99], type=pa.int64())
    mutated_data = wt.data.set_column(
        wt.data.schema.get_field_index("age"), "age", mutated_age
    )
    mutated_wt = WorkingTable(spec=wt.spec, data=mutated_data)

    matches_old = resolve_population(mutated_wt, _FORK_PATH, {"age": "40"})
    matches_new = resolve_population(mutated_wt, _FORK_PATH, {"age": "99"})
    assert matches_old.num_rows == 0
    assert matches_new.num_rows == 1


def test_build_predicate_clause_always_includes_fork_path() -> None:
    wt = _mixed_type_wt()
    clause = build_predicate_clause(wt, _FORK_PATH, None)
    assert clause == "WHERE \"fork_path\" = 'trunk'"


def test_build_canonical_order_clause_lists_every_column() -> None:
    wt = _mixed_type_wt()
    clause = build_canonical_order_clause(wt)
    assert clause.count("ASC NULLS FIRST") == 5


# ---------------------------------------------------------------------------
# draw_sample
# ---------------------------------------------------------------------------


def test_draw_sample_rate_draws_floor_of_rate_times_n() -> None:
    amount = Amount(rate=0.34)
    drawn = draw_sample(10, amount, random.Random(1))
    assert len(drawn) == 3  # floor(0.34 * 10)


def test_draw_sample_count_draws_min_k_n() -> None:
    amount = Amount(count=5)
    assert len(draw_sample(3, amount, random.Random(1))) == 3
    assert len(draw_sample(10, amount, random.Random(1))) == 5


def test_draw_sample_empty_population_returns_empty_list() -> None:
    amount = Amount(rate=1.0)
    assert draw_sample(0, amount, random.Random(1)) == []


def test_draw_sample_without_replacement() -> None:
    amount = Amount(count=5)
    drawn = draw_sample(5, amount, random.Random(1))
    assert len(set(drawn)) == len(drawn)
    assert set(drawn) == set(range(5))


def test_draw_sample_same_seed_stream_identical_draws() -> None:
    amount = Amount(rate=0.5)
    first = draw_sample(20, amount, random.Random(42))
    second = draw_sample(20, amount, random.Random(42))
    assert first == second


@pytest.mark.parametrize("population_size", [0, 1, 4])
def test_draw_sample_indices_within_population(population_size: int) -> None:
    amount = Amount(count=100)
    drawn = draw_sample(population_size, amount, random.Random(7))
    assert all(0 <= i < population_size for i in drawn)


# ---------------------------------------------------------------------------
# resolve_target_tables
# ---------------------------------------------------------------------------


def _selector_sidecar() -> Sidecar:
    tables: tuple[TableSpec, ...] = (
        table_spec("history", "fixed", (column_spec("fork_path", "VARCHAR"),)),
        table_spec(
            "records__actor",
            "records",
            (column_spec("fork_path", "VARCHAR"), column_spec("record_id", "VARCHAR")),
            record_kind="actor",
        ),
        table_spec(
            "records__doctor",
            "records",
            (column_spec("fork_path", "VARCHAR"), column_spec("record_id", "VARCHAR")),
            record_kind="doctor",
        ),
        table_spec(
            "membership__actor__appointments",
            "membership",
            (column_spec("fork_path", "VARCHAR"), column_spec("record_id", "VARCHAR")),
            record_kind="actor",
        ),
    )
    return sidecar(tables)


def test_resolve_target_tables_table_selector_returns_one_element_list() -> None:
    result = resolve_target_tables(Target(table="records__actor"), _selector_sidecar())
    assert result == ["records__actor"]


def test_resolve_target_tables_unknown_table_raises_shipped_message() -> None:
    with pytest.raises(
        CorruptValidationError, match=r"table 'records__nope' is not in this emit"
    ):
        resolve_target_tables(Target(table="records__nope"), _selector_sidecar())


def test_resolve_target_tables_tables_absent_entry_raises_naming_it() -> None:
    target = Target(tables=["records__actor", "records__nope"])
    with pytest.raises(CorruptValidationError, match=r"'records__nope'"):
        resolve_target_tables(target, _selector_sidecar())


def test_resolve_target_tables_glob_case_mismatch_does_not_match() -> None:
    with pytest.raises(CorruptValidationError):
        resolve_target_tables(Target(glob="RECORDS__*"), _selector_sidecar())


def test_resolve_target_tables_glob_matches_lexicographic_order() -> None:
    result = resolve_target_tables(Target(glob="records__*"), _selector_sidecar())
    assert result == ["records__actor", "records__doctor"]


def test_resolve_target_tables_category_fixed() -> None:
    result = resolve_target_tables(Target(category="fixed"), _selector_sidecar())
    assert result == ["history"]


def test_resolve_target_tables_category_records() -> None:
    result = resolve_target_tables(Target(category="records"), _selector_sidecar())
    assert result == ["records__actor", "records__doctor"]


def test_resolve_target_tables_category_membership() -> None:
    result = resolve_target_tables(Target(category="membership"), _selector_sidecar())
    assert result == ["membership__actor__appointments"]


def test_resolve_target_tables_record_kind_returns_records_and_membership() -> None:
    result = resolve_target_tables(Target(record_kind="actor"), _selector_sidecar())
    assert result == ["membership__actor__appointments", "records__actor"]


def test_resolve_target_tables_zero_match_glob_raises() -> None:
    with pytest.raises(CorruptValidationError):
        resolve_target_tables(Target(glob="no_such__*"), _selector_sidecar())


def test_resolve_target_tables_zero_match_category_raises() -> None:
    narrow = sidecar(
        (table_spec("history", "fixed", (column_spec("fork_path", "VARCHAR"),)),)
    )
    with pytest.raises(CorruptValidationError):
        resolve_target_tables(Target(category="records"), narrow)


def test_resolve_target_tables_zero_match_record_kind_raises() -> None:
    with pytest.raises(CorruptValidationError):
        resolve_target_tables(Target(record_kind="no_such_kind"), _selector_sidecar())


def test_resolve_target_tables_order_independent_of_sidecar_declaration_order() -> None:
    reversed_sidecar = sidecar(tuple(reversed(_selector_sidecar().tables())))
    result = resolve_target_tables(Target(category="records"), reversed_sidecar)
    assert result == ["records__actor", "records__doctor"]


# ---------------------------------------------------------------------------
# match_column_entries
# ---------------------------------------------------------------------------


def test_match_column_entries_exact_matches_only_itself() -> None:
    result = match_column_entries(["prop__status"], ["prop__status", "prop__status2"])
    assert result == ["prop__status"]


def test_match_column_entries_wildcard_star_is_a_pattern() -> None:
    result = match_column_entries(["prop__*"], ["prop__a", "prop__b", "other"])
    assert result == ["prop__a", "prop__b"]


def test_match_column_entries_wildcard_question_mark_is_a_pattern() -> None:
    result = match_column_entries(["prop__?"], ["prop__a", "prop__ab"])
    assert result == ["prop__a"]


def test_match_column_entries_bracket_is_a_pattern() -> None:
    result = match_column_entries(["prop__[ab]"], ["prop__a", "prop__b", "prop__c"])
    assert result == ["prop__a", "prop__b"]


def test_match_column_entries_order_is_entry_order_then_eligible_order() -> None:
    result = match_column_entries(["prop__b", "prop__*"], ["prop__a", "prop__b"])
    assert result == ["prop__b", "prop__a"]


def test_match_column_entries_deduplicated_at_first_match() -> None:
    result = match_column_entries(["prop__*", "prop__a"], ["prop__a", "prop__b"])
    assert result == ["prop__a", "prop__b"]


def test_match_column_entries_never_matches_a_column_outside_eligible_list() -> None:
    # Caller supplies only eligible columns; an existing-but-ineligible column
    # (absent from eligible_columns) can never be matched.
    result = match_column_entries(["record_id"], ["prop__a"])
    assert result == []


def test_match_column_entries_empty_result_not_raised() -> None:
    result = match_column_entries(["prop__nope"], ["prop__a"])
    assert result == []


# ---------------------------------------------------------------------------
# draw_weighted_sample
# ---------------------------------------------------------------------------


class _CountingRandom(random.Random):
    """A `random.Random` subclass that counts `.random()` calls."""

    def __init__(self, seed: int) -> None:
        super().__init__(seed)
        self.random_calls = 0

    def random(self) -> float:
        self.random_calls += 1
        return super().random()


class _FixedRandom(random.Random):
    """A `random.Random` subclass whose `.random()` replays a fixed sequence."""

    def __init__(self, values: list[float]) -> None:
        super().__init__()
        self._values = values
        self._next = 0

    def random(self) -> float:
        value = self._values[self._next]
        self._next += 1
        return value


def test_draw_weighted_sample_k_over_full_n_including_zero_weight() -> None:
    weights = [0.0, 0.0, 1.0, 1.0, 1.0]
    amount = Amount(count=2)
    drawn = draw_weighted_sample(weights, amount, random.Random(1))
    assert len(drawn) == 2


def test_draw_weighted_sample_never_chooses_zero_weight_unit() -> None:
    weights = [0.0, 5.0, 0.0, 3.0]
    amount = Amount(count=4)
    drawn = draw_weighted_sample(weights, amount, random.Random(1))
    assert set(drawn) == {1, 3}


def test_draw_weighted_sample_k_above_positive_weight_count_yields_all_positive() -> (
    None
):
    weights = [0.0, 1.0, 0.0, 2.0, 0.0]
    amount = Amount(count=100)
    drawn = draw_weighted_sample(weights, amount, random.Random(1))
    assert drawn == [1, 3]


def test_draw_weighted_sample_all_zero_weights_empty_list() -> None:
    weights = [0.0, 0.0, 0.0]
    amount = Amount(rate=1.0)
    assert draw_weighted_sample(weights, amount, random.Random(1)) == []


def test_draw_weighted_sample_tie_break_lower_index() -> None:
    weights = [1.0, 1.0, 1.0]
    amount = Amount(count=1)
    rng = _FixedRandom([0.5, 0.5, 0.5])
    drawn = draw_weighted_sample(weights, amount, rng)
    assert drawn == [0]


def test_draw_weighted_sample_exactly_n_random_calls_regardless_of_weights() -> None:
    weights = [0.0, 0.0, 0.0, 5.0]
    amount = Amount(count=1)
    rng = _CountingRandom(1)
    draw_weighted_sample(weights, amount, rng)
    assert rng.random_calls == len(weights)


def test_draw_weighted_sample_result_ascending() -> None:
    weights = [1.0] * 10
    amount = Amount(count=5)
    drawn = draw_weighted_sample(weights, amount, random.Random(3))
    assert drawn == sorted(drawn)


def test_draw_weighted_sample_same_seed_stream_identical_draws() -> None:
    weights = [1.0, 0.0, 2.0, 3.0, 0.0]
    amount = Amount(count=2)
    first = draw_weighted_sample(weights, amount, random.Random(7))
    second = draw_weighted_sample(weights, amount, random.Random(7))
    assert first == second


# ---------------------------------------------------------------------------
# derive_row_weights
# ---------------------------------------------------------------------------


class _StubSample(random.Random):
    """A `random.Random` subclass whose `.sample()` returns a fixed
    subsequence, recording each call's `(population, k)` for assertion."""

    def __init__(self, chosen: list[object]) -> None:
        super().__init__()
        self._chosen = chosen
        self.calls: list[tuple[list[object], int]] = []

    def sample(
        self, population: Sequence[object], k: int, *, counts: object = None
    ) -> list[object]:
        self.calls.append((list(population), k))
        return list(self._chosen)


def _spec_with_record_id(
    name: str, kind: str, extra: tuple[ColumnSpec, ...] = ()
) -> TableSpec:
    return table_spec(
        name,
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            *extra,
        ),
        record_kind=kind,
    )


def test_derive_row_weights_entity_scoped_subset_via_one_rng_sample() -> None:
    spec_a = _spec_with_record_id("records__a", "a")
    spec_b = _spec_with_record_id("records__b", "b")
    wt_a = working_table(
        spec_a,
        [
            {"fork_path": _FORK_PATH, "record_id": "x1"},
            {"fork_path": _FORK_PATH, "record_id": "x2"},
        ],
    )
    wt_b = working_table(
        spec_b,
        [
            {"fork_path": _FORK_PATH, "record_id": "x2"},
            {"fork_path": _FORK_PATH, "record_id": "x3"},
        ],
    )
    content_a = resolve_population(wt_a, _FORK_PATH, None)
    content_b = resolve_population(wt_b, _FORK_PATH, None)
    placement = EntityScoped(kind="entity_scoped", entities=Amount(count=1))
    rng = _StubSample(["x2"])
    weights = derive_row_weights(placement, [(wt_a, content_a), (wt_b, content_b)], rng)
    assert rng.calls == [(["x1", "x2", "x3"], 1)]
    assert weights[0] == [0.0, 1.0]
    assert weights[1] == [1.0, 0.0]


def test_derive_row_weights_clustered_temporal_window_membership() -> None:
    spec = _spec_with_record_id(
        "records__sensor", "sensor", (column_spec("ts", "BIGINT"),)
    )
    wt = working_table(
        spec,
        [
            {"fork_path": _FORK_PATH, "record_id": "r1", "ts": 10},
            {"fork_path": _FORK_PATH, "record_id": "r2", "ts": 12},
            {"fork_path": _FORK_PATH, "record_id": "r3", "ts": 50},
            {"fork_path": _FORK_PATH, "record_id": "r4", "ts": None},
        ],
    )
    content = resolve_population(wt, _FORK_PATH, None)
    placement = ClusteredTemporal(
        kind="clustered_temporal", column="ts", clusters=1, width=5
    )
    rng = _StubSample([10])
    weights = derive_row_weights(placement, [(wt, content)], rng)
    assert rng.calls == [([10, 12, 50], 1)]
    ts_by_position = content.column("ts").to_pylist()
    expected = [
        1.0 if v is not None and abs(v - 10) <= 5 else 0.0 for v in ts_by_position
    ]
    assert weights[0] == expected
    assert weights[0][ts_by_position.index(50)] == 0.0


def test_derive_row_weights_clustered_temporal_table_lacking_column_all_zero() -> None:
    spec_with = _spec_with_record_id(
        "records__sensor", "sensor", (column_spec("ts", "BIGINT"),)
    )
    spec_without = _spec_with_record_id("records__other", "other")
    wt_with = working_table(
        spec_with, [{"fork_path": _FORK_PATH, "record_id": "r1", "ts": 10}]
    )
    wt_without = working_table(
        spec_without, [{"fork_path": _FORK_PATH, "record_id": "r2"}]
    )
    content_with = resolve_population(wt_with, _FORK_PATH, None)
    content_without = resolve_population(wt_without, _FORK_PATH, None)
    placement = ClusteredTemporal(
        kind="clustered_temporal", column="ts", clusters=1, width=5
    )
    rng = _StubSample([10])
    weights = derive_row_weights(
        placement, [(wt_with, content_with), (wt_without, content_without)], rng
    )
    assert weights[1] == [0.0]


def test_derive_row_weights_correlated_match_and_nonmatch_and_null() -> None:
    spec = _spec_with_record_id(
        "records__patient", "patient", (column_spec("status", "VARCHAR"),)
    )
    wt = working_table(
        spec,
        [
            {"fork_path": _FORK_PATH, "record_id": "p1", "status": "active"},
            {"fork_path": _FORK_PATH, "record_id": "p2", "status": "discharged"},
            {"fork_path": _FORK_PATH, "record_id": "p3", "status": None},
        ],
    )
    content = resolve_population(wt, _FORK_PATH, None)
    placement = Correlated(
        kind="correlated", column="status", value="active", weight=5.0
    )
    weights = derive_row_weights(placement, [(wt, content)], random.Random(0))
    status_by_position = content.column("status").to_pylist()
    expected = [5.0 if s == "active" else 1.0 for s in status_by_position]
    assert weights[0] == expected


def test_derive_row_weights_correlated_typed_equality_string_against_bigint() -> None:
    spec = _spec_with_record_id(
        "records__actor", "actor", (column_spec("age", "BIGINT"),)
    )
    wt = working_table(
        spec,
        [
            {"fork_path": _FORK_PATH, "record_id": "a1", "age": 40},
            {"fork_path": _FORK_PATH, "record_id": "a2", "age": 10},
        ],
    )
    content = resolve_population(wt, _FORK_PATH, None)
    placement = Correlated(kind="correlated", column="age", value="40", weight=2.0)
    weights = derive_row_weights(placement, [(wt, content)], random.Random(0))
    assert weights[0] == [2.0, 1.0]


def test_derive_row_weights_correlated_table_lacking_column_all_one() -> None:
    spec = _spec_with_record_id("records__other", "other")
    wt = working_table(spec, [{"fork_path": _FORK_PATH, "record_id": "o1"}])
    content = resolve_population(wt, _FORK_PATH, None)
    placement = Correlated(
        kind="correlated", column="status", value="active", weight=5.0
    )
    weights = derive_row_weights(placement, [(wt, content)], random.Random(0))
    assert weights[0] == [1.0]


def test_derive_row_weights_correlated_consumes_no_rng() -> None:
    spec = _spec_with_record_id(
        "records__patient", "patient", (column_spec("status", "VARCHAR"),)
    )
    wt = working_table(
        spec, [{"fork_path": _FORK_PATH, "record_id": "p1", "status": "active"}]
    )
    content = resolve_population(wt, _FORK_PATH, None)
    placement = Correlated(
        kind="correlated", column="status", value="active", weight=5.0
    )
    rng = random.Random(0)
    state_before = rng.getstate()
    derive_row_weights(placement, [(wt, content)], rng)
    assert rng.getstate() == state_before


def test_derive_row_weights_alignment_matches_canonical_row_order() -> None:
    """Population crafted so a naive unordered readback would misalign the
    computed flag with the wrong row (§ Placement)."""
    spec = _spec_with_record_id(
        "records__patient", "patient", (column_spec("status", "VARCHAR"),)
    )
    # Inserted out of canonical (ascending record_id) order.
    wt = working_table(
        spec,
        [
            {"fork_path": _FORK_PATH, "record_id": "p9", "status": "active"},
            {"fork_path": _FORK_PATH, "record_id": "p1", "status": "discharged"},
        ],
    )
    content = resolve_population(wt, _FORK_PATH, None)
    assert content.column("record_id").to_pylist() == ["p1", "p9"]
    placement = Correlated(
        kind="correlated", column="status", value="active", weight=9.0
    )
    weights = derive_row_weights(placement, [(wt, content)], random.Random(0))
    assert weights[0] == [1.0, 9.0]
