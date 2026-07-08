"""Tests for the `shift_sim_time` corrupter handler."""

from __future__ import annotations

import random

import pytest

from fabulexa_export.config.models import (
    Amount,
    ClusteredTemporal,
    Distribution,
    ShiftCollide,
    ShiftOffset,
    ShiftSimTime,
    ShiftSwap,
    Target,
)
from fabulexa_export.corrupters.operations.shift_sim_time import ShiftSimTimeCorrupter
from fabulexa_export.corrupters.state import CorruptState
from fabulexa_export.reader.sidecar import BranchEntry, Sidecar

from .._helpers import CallOrderRandom, column_spec, sidecar, table_spec, working_table

_FORK_PATH = "trunk"
_SLICE_AT = 100
_HANDLER = ShiftSimTimeCorrupter()


class _CallOrderRandomWithDelta(CallOrderRandom):
    """`CallOrderRandom` extended to also record `.uniform()` / `.gauss()`
    calls -- shift_sim_time's `offset` mode delta draws.

    Delegates to `random.Random`'s raw (unbound) implementation rather than
    `super().uniform()` / `super().gauss()`: those call `self.random()`
    internally, which -- being itself overridden here -- would log a second,
    misleading "random" entry for the same draw.
    """

    def uniform(self, a: float, b: float) -> float:
        self.calls.append("uniform")
        return a + (b - a) * random.Random.random(self)

    def gauss(self, mu: float = 0.0, sigma: float = 1.0) -> float:
        self.calls.append("gauss")
        return super().gauss(mu, sigma)


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


def _records_actor_spec() -> object:
    return table_spec(
        "records__actor",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("prop__status", "VARCHAR"),
        ),
        record_kind="actor",
    )


def _row(
    sim_time: int,
    value: str,
    *,
    kind: str = "actor",
    record_id: str = "a001",
    property_: str = "status",
) -> dict[str, object]:
    return {
        "fork_path": _FORK_PATH,
        "kind": kind,
        "record_id": record_id,
        "property": property_,
        "sim_time": sim_time,
        "value": value,
    }


def _records_row(status: str, *, record_id: str = "a001") -> dict[str, object]:
    return {"fork_path": _FORK_PATH, "record_id": record_id, "prop__status": status}


def _sidecar(*, slice_at: int = _SLICE_AT) -> Sidecar:
    return sidecar(
        (_history_spec(), _records_actor_spec()),
        branches=(BranchEntry(fork_path=_FORK_PATH, parent=None, slice_at=slice_at),),
    )


def _state(
    history_rows: list[dict[str, object]],
    records_rows: list[dict[str, object]] | None = None,
) -> CorruptState:
    return CorruptState(
        tables={
            "history": working_table(_history_spec(), history_rows),
            "records__actor": working_table(_records_actor_spec(), records_rows or []),
        }
    )


def _apply(
    state: CorruptState,
    shift: object,
    *,
    count: int | None = None,
    rate: float | None = None,
    where: dict[str, str] | None = None,
    seed: int = 1,
    placement: object = None,
    rng: random.Random | None = None,
    slice_at: int = _SLICE_AT,
) -> object:
    amount = Amount(count=count) if count is not None else Amount(rate=rate)
    op = ShiftSimTime(
        kind="shift_sim_time",
        target=Target(table="history", where=where),
        amount=amount,
        placement=placement,
        shift=shift,
    )
    return _HANDLER.apply(
        state,
        op,
        "rule#0",
        rng if rng is not None else random.Random(seed),
        _FORK_PATH,
        _sidecar(slice_at=slice_at),
    )


def _offset(low: float, high: float) -> ShiftOffset:
    """A deterministic offset shift: `distribution` a degenerate uniform
    range so its one delta draw is always `low` (== `high`)."""
    return ShiftOffset(
        kind="offset", distribution=Distribution(shape="uniform", low=low, high=high)
    )


_COLLIDE = ShiftCollide(kind="collide")
_SWAP = ShiftSwap(kind="swap")


# ---------------------------------------------------------------------------
# Populations
# ---------------------------------------------------------------------------


def test_offset_pools_over_all_narrowed_rows() -> None:
    state = _state([_row(10, "a"), _row(20, "b"), _row(30, "c")])
    outcome = _apply(state, _offset(5.0, 5.0), rate=1.0)
    assert outcome.units_selected == 3


def test_collide_excludes_rows_with_no_predecessor() -> None:
    """Two series, each contributing one min-tick row with no predecessor;
    only the two non-minimum rows are collide-eligible."""
    state = _state(
        [
            _row(10, "a", record_id="r1"),
            _row(20, "b", record_id="r1"),
            _row(10, "c", record_id="r2"),
            _row(30, "d", record_id="r2"),
        ]
    )
    outcome = _apply(state, _COLLIDE, rate=1.0)
    assert outcome.units_selected == 2


def test_all_first_in_series_population_is_noop_not_error() -> None:
    state = _state([_row(10, "a", record_id="r1"), _row(10, "b", record_id="r2")])
    outcome = _apply(state, _COLLIDE, rate=1.0)
    assert outcome.units_selected == 0
    assert outcome.units_affected == 0
    assert outcome.defects == ()


def test_swap_excludes_rows_with_no_predecessor() -> None:
    state = _state([_row(10, "a"), _row(20, "b")])
    outcome = _apply(state, _SWAP, rate=1.0)
    assert outcome.units_selected == 1


# ---------------------------------------------------------------------------
# offset
# ---------------------------------------------------------------------------


def test_offset_shifts_sim_time_by_rounded_delta() -> None:
    state = _state([_row(10, "a")])
    outcome = _apply(state, _offset(5.4, 5.4), count=1)
    assert outcome.units_affected == 1
    history = state.tables["history"].data
    assert history.column("sim_time").to_pylist() == [15]
    (defect,) = outcome.defects
    assert defect.defect_class == "shifted_event_time"


def test_offset_delta_rounds_half_to_even() -> None:
    """2.5 rounds to 2 (banker's rounding): 10 + 2 = 12."""
    state = _state([_row(10, "a")])
    _apply(state, _offset(2.5, 2.5), count=1)
    assert state.tables["history"].data.column("sim_time").to_pylist() == [12]


def test_offset_defect_locator_is_post_corruption_coordinate() -> None:
    state = _state([_row(10, "a")])
    outcome = _apply(state, _offset(5.0, 5.0), count=1)
    (defect,) = outcome.defects
    assert dict(defect.location.row.keys)["sim_time"] == "15"


def test_offset_zero_rounded_delta_leaves_row_unchanged_no_defect() -> None:
    state = _state([_row(10, "a")])
    outcome = _apply(state, _offset(0.0, 0.0), count=1)
    assert outcome.units_selected == 1
    assert outcome.units_affected == 0
    assert outcome.defects == ()
    assert state.tables["history"].data.column("sim_time").to_pylist() == [10]


def test_offset_zero_delta_still_consumes_the_draw() -> None:
    """A zero-delta run and a non-zero run of equal selected count consume
    the same number of RNG mode-draws (the delta draw happens either way)."""
    zero_rng = _CallOrderRandomWithDelta(seed=1)
    state_zero = _state([_row(10, "a"), _row(20, "b")])
    _apply(state_zero, _offset(0.0, 0.0), rate=1.0, rng=zero_rng)

    nonzero_rng = _CallOrderRandomWithDelta(seed=1)
    state_nonzero = _state([_row(10, "a"), _row(20, "b")])
    _apply(state_nonzero, _offset(5.0, 5.0), rate=1.0, rng=nonzero_rng)

    assert zero_rng.calls.count("uniform") == nonzero_rng.calls.count("uniform") == 2


def test_offset_shift_past_slice_at_removing_anchor_declares_c6() -> None:
    """Anchor is (40, "active"); shifting it to 200 (past slice_at=100)
    exposes (10, "old"), which no longer round-trips."""
    state = _state(
        [_row(10, "old"), _row(40, "active")],
        records_rows=[_records_row("active")],
    )
    outcome = _apply(state, _offset(160.0, 160.0), count=1, where={"sim_time": "40"})
    (defect,) = outcome.defects
    assert defect.impact == ("C6",)


def test_offset_non_anchor_shifted_above_anchor_declares_c6() -> None:
    """Anchor is (40, "active"); shifting the non-anchor (10, "old") to 50
    makes it the new anchor, whose value differs from the records cell."""
    state = _state(
        [_row(10, "old"), _row(40, "active")],
        records_rows=[_records_row("active")],
    )
    outcome = _apply(state, _offset(40.0, 40.0), count=1, where={"sim_time": "10"})
    (defect,) = outcome.defects
    assert defect.impact == ("C6",)


def test_offset_small_non_anchor_shift_declares_beyond_c1_c12() -> None:
    """Anchor stays (40, "active"); shifting the non-anchor (10, "old") to 15
    changes nothing about which row is the anchor."""
    state = _state(
        [_row(10, "old"), _row(40, "active")],
        records_rows=[_records_row("active")],
    )
    outcome = _apply(state, _offset(5.0, 5.0), count=1, where={"sim_time": "10"})
    (defect,) = outcome.defects
    assert defect.impact == ("beyond-c1-c12",)


def test_offset_normal_distribution_shifts_sim_time_by_rounded_delta() -> None:
    """`draw_delta`'s `normal` branch (`rng.gauss`) -- every other offset test
    in this file uses `shape='uniform'`; stddev is negligibly small so the
    rounded delta is deterministically `round(mean)`."""
    state = _state([_row(10, "a")])
    shift = ShiftOffset(
        kind="offset",
        distribution=Distribution(shape="normal", mean=100.0, stddev=1e-9),
    )
    outcome = _apply(state, shift, count=1)
    assert outcome.units_affected == 1
    assert state.tables["history"].data.column("sim_time").to_pylist() == [110]


def test_offset_bigint_overflow_fails_loudly() -> None:
    """A sum outside BIGINT range raises (never a silent wrap) -- the shared
    Arrow/DuckDB integer domain, the same failure domain as the shipped
    BIGINT jitter store."""
    state = _state([_row(2**62, "a")])
    with pytest.raises(OverflowError):
        _apply(state, _offset(float(2**62), float(2**62)), count=1)


# ---------------------------------------------------------------------------
# collide
# ---------------------------------------------------------------------------


def test_collide_moves_sim_time_to_predecessor_tick() -> None:
    state = _state([_row(10, "a"), _row(20, "b")])
    outcome = _apply(state, _COLLIDE, count=1, where={"sim_time": "20"})
    assert outcome.units_affected == 1
    history = state.tables["history"].data
    assert sorted(history.column("sim_time").to_pylist()) == [10, 10]
    (defect,) = outcome.defects
    assert defect.defect_class == "tick_collision"
    assert dict(defect.location.row.keys)["sim_time"] == "10"


def test_collide_non_anchor_declares_beyond_c1_c12() -> None:
    """Anchor is (40, "active"); colliding the non-anchor (20, "mid") onto
    its predecessor (10) leaves the anchor untouched -- the canonical tick
    collision, subconformant but not C6."""
    state = _state(
        [_row(10, "old"), _row(20, "mid"), _row(40, "active")],
        records_rows=[_records_row("active")],
    )
    outcome = _apply(state, _COLLIDE, count=1, where={"sim_time": "20"})
    (defect,) = outcome.defects
    assert defect.impact == ("beyond-c1-c12",)


def test_collide_anchor_onto_differing_predecessor_declares_c6() -> None:
    """Anchor (40, "active") collides onto predecessor tick 10; the tie
    resolves by value DESC -- "old" > "active" lexicographically, so the
    resolved value diverges from the records cell."""
    state = _state(
        [_row(10, "old"), _row(40, "active")],
        records_rows=[_records_row("active")],
    )
    outcome = _apply(state, _COLLIDE, count=1, where={"sim_time": "40"})
    (defect,) = outcome.defects
    assert defect.impact == ("C6",)


def test_collide_anchor_onto_equal_resolved_value_declares_beyond_c1_c12() -> None:
    """Anchor (40, "same") collides onto predecessor tick 10, whose value is
    identical: the tie-break resolves to "same" either way, which still
    round-trips against the records cell -- the actual-divergence stance
    declares beyond-c1-c12 despite anchor participation."""
    state = _state(
        [_row(10, "same"), _row(40, "same")],
        records_rows=[_records_row("same")],
    )
    outcome = _apply(state, _COLLIDE, count=1, where={"sim_time": "40"})
    (defect,) = outcome.defects
    assert defect.impact == ("beyond-c1-c12",)


# ---------------------------------------------------------------------------
# swap
# ---------------------------------------------------------------------------


def test_swap_exchanges_ticks_with_predecessor_partner() -> None:
    state = _state([_row(10, "old"), _row(20, "mid")])
    outcome = _apply(state, _SWAP, count=1, where={"sim_time": "20"})
    assert outcome.units_affected == 1
    assert len(outcome.defects) == 2
    history = state.tables["history"].data
    by_value = dict(
        zip(history.column("value").to_pylist(), history.column("sim_time").to_pylist())
    )
    assert by_value["old"] == 20
    assert by_value["mid"] == 10
    assert {d.defect_class for d in outcome.defects} == {"reordered_event"}


def test_swap_partner_excluded_by_where_is_still_rewritten() -> None:
    """`where` narrows the selected-unit population, not the partner
    resolution: the predecessor row is rewritten even though `where` would
    exclude it."""
    state = _state([_row(10, "old"), _row(20, "mid")])
    outcome = _apply(state, _SWAP, count=1, where={"sim_time": "20"})
    assert outcome.units_affected == 1
    history = state.tables["history"].data
    assert sorted(history.column("sim_time").to_pylist()) == [10, 20]


def test_swap_equal_value_pair_is_noop() -> None:
    state = _state([_row(10, "same"), _row(20, "same")])
    outcome = _apply(state, _SWAP, count=1, where={"sim_time": "20"})
    assert outcome.units_selected == 1
    assert outcome.units_affected == 0
    assert outcome.defects == ()
    history = state.tables["history"].data
    assert sorted(history.column("sim_time").to_pylist()) == [10, 20]


def test_swap_chained_pair_is_skipped_not_counted_no_error() -> None:
    """Three-event series 10 < 20 < 30, both (20 -> partner 10) and
    (30 -> partner 20) selected: whichever is processed second chains onto a
    row the first already rewrote, and is skipped."""
    state = _state([_row(10, "a"), _row(20, "b"), _row(30, "c")])
    outcome = _apply(state, _SWAP, rate=1.0)
    assert outcome.units_selected == 2
    assert outcome.units_affected == 1
    assert len(outcome.defects) == 2


def test_swap_units_affected_lower_than_defect_count() -> None:
    state = _state([_row(10, "old"), _row(20, "mid")])
    outcome = _apply(state, _SWAP, count=1, where={"sim_time": "20"})
    assert outcome.units_affected == 1
    assert len(outcome.defects) == 2


def test_swap_not_involving_anchor_declares_beyond_c1_c12_both() -> None:
    """Anchor is (90, "Alice"); swapping the two older, non-anchor rows
    leaves the anchor untouched."""
    state = _state(
        [_row(10, "v0"), _row(30, "v1"), _row(90, "Alice")],
        records_rows=[_records_row("Alice")],
    )
    outcome = _apply(state, _SWAP, count=1, where={"sim_time": "30"})
    assert {d.impact for d in outcome.defects} == {("beyond-c1-c12",)}


def test_swap_involving_anchor_failing_round_trip_declares_c6_on_both() -> None:
    """Anchor (40, "active") swaps with predecessor (10, "old"): the anchor's
    tick now carries "old" (post-swap anchor), while the moved anchor row
    (now at tick 10) was the pre-op anchor -- both moved rows participate."""
    state = _state(
        [_row(10, "old"), _row(40, "active")],
        records_rows=[_records_row("active")],
    )
    outcome = _apply(state, _SWAP, count=1, where={"sim_time": "40"})
    assert {d.impact for d in outcome.defects} == {("C6",)}


# ---------------------------------------------------------------------------
# Simultaneous rewrite
# ---------------------------------------------------------------------------


def test_two_selected_events_in_one_series_resolve_against_same_start_state() -> None:
    """Both events in a 4-tick series are offset-shifted; each delta draw
    (and its resulting new sim_time) is computed from the pre-operation
    positions, never from a partially-mutated intermediate state."""
    state = _state(
        [_row(10, "a"), _row(20, "b"), _row(30, "c"), _row(40, "d")],
    )
    outcome = _apply(state, _offset(1.0, 1.0), rate=1.0)
    assert outcome.units_affected == 4
    assert sorted(state.tables["history"].data.column("sim_time").to_pylist()) == [
        11,
        21,
        31,
        41,
    ]


# ---------------------------------------------------------------------------
# RNG consumption order
# ---------------------------------------------------------------------------


def test_offset_rng_order_placement_then_unit_draw_then_mode_draws() -> None:
    state = _state([_row(10, "a"), _row(20, "b"), _row(30, "c")])
    op = ShiftSimTime(
        kind="shift_sim_time",
        target=Target(table="history"),
        amount=Amount(count=1),
        placement=ClusteredTemporal(
            kind="clustered_temporal", column="sim_time", clusters=1, width=100
        ),
        shift=_offset(1.0, 1.0),
    )
    rng = _CallOrderRandomWithDelta(seed=3)
    _HANDLER.apply(state, op, "rule#0", rng, _FORK_PATH, _sidecar())
    assert rng.calls[0] == "sample"
    assert rng.calls[-1] == "uniform"
    assert set(rng.calls[1:-1]) <= {"random"}


def test_collide_draws_no_mode_draws() -> None:
    state = _state([_row(10, "a"), _row(20, "b")])
    rng = _CallOrderRandomWithDelta(seed=3)
    _apply(state, _COLLIDE, count=1, rng=rng)
    assert "uniform" not in rng.calls
    assert "gauss" not in rng.calls


def test_swap_draws_no_mode_draws() -> None:
    state = _state([_row(10, "a"), _row(20, "b")])
    rng = _CallOrderRandomWithDelta(seed=3)
    _apply(state, _SWAP, count=1, rng=rng)
    assert "uniform" not in rng.calls
    assert "gauss" not in rng.calls


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_rerun_with_same_seed_is_identical() -> None:
    rows = [_row(10, "a"), _row(20, "b"), _row(30, "c"), _row(40, "d")]
    state_a = _state(rows)
    state_b = _state(rows)
    outcome_a = _apply(state_a, _offset(3.0, 3.0), rate=1.0, seed=9)
    outcome_b = _apply(state_b, _offset(3.0, 3.0), rate=1.0, seed=9)
    assert outcome_a.defects == outcome_b.defects
    assert state_a.tables["history"].data.equals(state_b.tables["history"].data)
