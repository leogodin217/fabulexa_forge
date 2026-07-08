"""Tests for the `freeze_series` corrupter handler."""

from __future__ import annotations

import random

from fabulexa_export.config.models import (
    Amount,
    ClusteredTemporal,
    EntityScoped,
    FreezeSeries,
    Target,
)
from fabulexa_export.corrupters.operations.freeze_series import FreezeSeriesCorrupter
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
_HANDLER = FreezeSeriesCorrupter()


class _FixedCutRandom(random.Random):
    """A `random.Random` whose `.randrange()` always returns a fixed cut,
    regardless of its `(start, stop)` arguments — pins `cut: random`'s draw
    so an impact test can target an exact kept-prefix length."""

    def __init__(self, cut: int, seed: int = 0) -> None:
        super().__init__(seed)
        self._cut = cut

    def randrange(self, start: int, stop: int | None = None, step: int = 1) -> int:
        return self._cut


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
    *,
    cut: str = "after_first",
    count: int | None = None,
    rate: float | None = None,
    where: dict[str, str] | None = None,
    seed: int = 1,
    placement: object = None,
    rng: random.Random | None = None,
    slice_at: int = _SLICE_AT,
) -> object:
    amount = Amount(count=count) if count is not None else Amount(rate=rate)
    op = FreezeSeries(
        kind="freeze_series",
        target=Target(table="history", where=where),
        amount=amount,
        placement=placement,
        cut=cut,
    )
    return _HANDLER.apply(
        state,
        op,
        "rule#0",
        rng if rng is not None else random.Random(seed),
        _FORK_PATH,
        _sidecar(slice_at=slice_at),
    )


# ---------------------------------------------------------------------------
# Units: population, lexicographic order, single-event exclusion
# ---------------------------------------------------------------------------


def test_single_event_series_is_a_noop() -> None:
    state = _state([_row(10, "a", record_id="a001")])
    outcome = _apply(state, rate=1.0)
    assert outcome.units_selected == 0
    assert outcome.units_affected == 0
    assert outcome.defects == ()
    assert state.tables["history"].data.num_rows == 1


def test_series_units_enumerated_in_lexicographic_order() -> None:
    """Three qualifying series of distinct timeline lengths; cut: random
    draws one randrange(1, N) per selected series, in ascending (kind,
    record_id, property) order -- verified by the per-series N in each call.
    """
    rows = [
        *[_row(t, "v", record_id="a1") for t in (10, 20, 30)],  # N=3
        *[_row(t, "v", record_id="b1") for t in (10, 20, 30, 40)],  # N=4
        *[_row(t, "v", record_id="c1") for t in (10, 20)],  # N=2
    ]
    state = _state(rows)
    rng = CallOrderRandom(seed=5)
    outcome = _apply(state, cut="random", rate=1.0, rng=rng)
    assert outcome.units_selected == 3
    assert rng.randrange_calls == [(1, 3), (1, 4), (1, 2)]


# ---------------------------------------------------------------------------
# Cut semantics
# ---------------------------------------------------------------------------


def test_cut_after_first_keeps_exactly_the_first_row() -> None:
    state = _state([_row(10, "old"), _row(20, "mid"), _row(30, "new")])
    _apply(state, cut="after_first", count=1)
    history = state.tables["history"].data
    assert history.num_rows == 1
    assert history.column("sim_time").to_pylist() == [10]


def test_rows_past_slice_at_are_part_of_the_removed_tail() -> None:
    state = _state([_row(10, "old"), _row(20, "mid"), _row(150, "future")])
    outcome = _apply(state, cut="after_first", count=1, slice_at=100)
    assert outcome.units_affected == 1
    assert len(outcome.defects) == 2
    history = state.tables["history"].data
    assert history.column("sim_time").to_pylist() == [10]


def test_where_matching_one_row_still_freezes_the_series_full_tail() -> None:
    """`where` narrows series *membership* (sim_time=20 matches only the
    middle row), but the freeze acts on the whole timeline: the un-matched
    tail row (sim_time=30) is removed too."""
    state = _state([_row(10, "old"), _row(20, "mid"), _row(30, "new")])
    outcome = _apply(state, cut="after_first", rate=1.0, where={"sim_time": "20"})
    assert outcome.units_affected == 1
    history = state.tables["history"].data
    assert history.column("sim_time").to_pylist() == [10]
    removed_times = {dict(d.location.row.keys)["sim_time"] for d in outcome.defects}
    assert removed_times == {"20", "30"}


# ---------------------------------------------------------------------------
# Defects: locator, source coordinate, units_affected != len(defects)
# ---------------------------------------------------------------------------


def test_one_frozen_series_event_defect_per_removed_row() -> None:
    state = _state([_row(10, "old"), _row(20, "mid"), _row(30, "new")])
    outcome = _apply(state, cut="after_first", count=1)
    assert outcome.units_selected == 1
    assert outcome.units_affected == 1
    assert len(outcome.defects) == 2
    assert {d.defect_class for d in outcome.defects} == {"frozen_series_event"}


def test_defect_location_is_source_coordinate() -> None:
    state = _state([_row(10, "old"), _row(20, "mid"), _row(30, "new")])
    outcome = _apply(state, cut="after_first", count=1)
    times = {dict(d.location.row.keys)["sim_time"] for d in outcome.defects}
    assert times == {"20", "30"}


def test_units_affected_lower_than_defect_count_over_multiple_series() -> None:
    """Two selected series, each losing >= 1 row: units_affected (2) < the
    total defect count -- the first operation where the shipped
    units_affected == len(defects) equality breaks."""
    rows = [
        *[_row(t, "v", record_id="a1") for t in (10, 20, 30)],
        *[_row(t, "v", record_id="b1") for t in (10, 20)],
    ]
    state = _state(rows)
    outcome = _apply(state, cut="after_first", rate=1.0)
    assert outcome.units_affected == 2
    assert len(outcome.defects) == 3


# ---------------------------------------------------------------------------
# Impact: the anchor-participant rule (§ The impact rule)
# ---------------------------------------------------------------------------


def test_suppressed_tail_containing_anchor_declares_c6_on_ex_anchor_only() -> None:
    """Anchor is (40, "active"); freezing after the first row suppresses
    both the mid row and the anchor. Only the ex-anchor row's record
    declares C6; the rest of the tail declares beyond-c1-c12."""
    state = _state(
        [_row(10, "old"), _row(20, "mid"), _row(40, "active")],
        records_rows=[_records_row("active")],
    )
    outcome = _apply(state, cut="after_first", count=1)
    by_time = {dict(d.location.row.keys)["sim_time"]: d.impact for d in outcome.defects}
    assert by_time["40"] == ("C6",)
    assert by_time["20"] == ("beyond-c1-c12",)


def test_cut_entirely_below_anchor_declares_all_beyond_c1_c12() -> None:
    """slice_at=25 puts the C6-view anchor at (20, "mid"); a fixed cut of 2
    keeps [10, 20] (the anchor) and removes only the post-slice (40,
    "future") row -- anchor kept, so every removed row is a non-participant."""
    state = _state(
        [_row(10, "old"), _row(20, "mid"), _row(40, "future")],
        records_rows=[_records_row("mid")],
    )
    outcome = _apply(
        state, cut="random", rng=_FixedCutRandom(cut=2), slice_at=25, count=1
    )
    assert outcome.defects != ()
    assert {d.impact for d in outcome.defects} == {("beyond-c1-c12",)}
    history = state.tables["history"].data
    assert history.column("sim_time").to_pylist() == [10, 20]


# ---------------------------------------------------------------------------
# Placement: a series takes its terminal row's weight
# ---------------------------------------------------------------------------


def test_entity_scoped_universe_is_terminal_row_record_ids() -> None:
    state = _state(
        [
            _row(10, "v", record_id="rA"),
            _row(20, "v", record_id="rA"),
            _row(10, "v", record_id="rB"),
            _row(20, "v", record_id="rB"),
        ]
    )
    op_rng = FixedSampleRandom(["rA"], seed=2)
    outcome = _apply(
        state,
        cut="after_first",
        count=1,
        placement=EntityScoped(kind="entity_scoped", entities=Amount(count=1)),
        rng=op_rng,
    )
    assert outcome.units_selected == 1
    (defect,) = outcome.defects
    assert dict(defect.location.row.keys)["record_id"] == "rA"


def test_clustered_temporal_centers_draw_from_terminal_row_sim_times() -> None:
    state = _state(
        [
            _row(10, "v", record_id="rA"),
            _row(20, "v", record_id="rA"),
            _row(50, "v", record_id="rB"),
            _row(60, "v", record_id="rB"),
        ]
    )
    # Terminal rows' sim_times are {20, 60}; force the one cluster center to
    # 20 -- only rA's series falls within the window (width 1).
    op_rng = FixedSampleRandom([20], seed=3)
    outcome = _apply(
        state,
        cut="after_first",
        count=1,
        placement=ClusteredTemporal(
            kind="clustered_temporal", column="sim_time", clusters=1, width=1
        ),
        rng=op_rng,
    )
    assert outcome.units_selected == 1
    (defect,) = outcome.defects
    assert dict(defect.location.row.keys)["record_id"] == "rA"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_rerun_with_same_seed_is_identical() -> None:
    rows = [
        *[_row(t, "v", record_id="a1") for t in (10, 20, 30)],
        *[_row(t, "v", record_id="b1") for t in (10, 20, 30, 40)],
    ]
    state_a = _state(rows)
    state_b = _state(rows)
    outcome_a = _apply(state_a, cut="random", rate=1.0, seed=9)
    outcome_b = _apply(state_b, cut="random", rate=1.0, seed=9)
    assert outcome_a.defects == outcome_b.defects
    assert state_a.tables["history"].data.equals(state_b.tables["history"].data)
