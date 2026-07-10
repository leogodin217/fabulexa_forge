"""Tests for the `drop_events` corrupter handler."""

from __future__ import annotations

import random

from fabulexa_forge.config.models import (
    Amount,
    ClusteredTemporal,
    DropEvents,
    Target,
)
from fabulexa_forge.corrupters.operations.drop_events import DropEventsCorrupter
from fabulexa_forge.corrupters.state import CorruptState
from fabulexa_forge.reader.sidecar import BranchEntry, Sidecar

from .._helpers import CallOrderRandom, column_spec, sidecar, table_spec, working_table

_FORK_PATH = "trunk"
_SLICE_AT = 100
_HANDLER = DropEventsCorrupter()


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


def _records_row(status: str) -> dict[str, object]:
    return {"fork_path": _FORK_PATH, "record_id": "a001", "prop__status": status}


def _sidecar() -> Sidecar:
    return sidecar(
        (_history_spec(), _records_actor_spec()),
        branches=(BranchEntry(fork_path=_FORK_PATH, parent=None, slice_at=_SLICE_AT),),
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
    count: int | None = None,
    rate: float | None = None,
    where: dict[str, str] | None = None,
    seed: int = 1,
    placement: object = None,
) -> object:
    amount = Amount(count=count) if count is not None else Amount(rate=rate)
    op = DropEvents(
        kind="drop_events",
        target=Target(table="history", where=where),
        amount=amount,
        placement=placement,
    )
    return _HANDLER.apply(
        state, op, "rule#0", random.Random(seed), _FORK_PATH, _sidecar()
    )


# ---------------------------------------------------------------------------
# Basic mutation: removed rows gone, kept rows byte-identical, locality
# ---------------------------------------------------------------------------


def test_selected_row_removed_kept_row_untouched() -> None:
    state = _state(
        [_row(10, "old"), _row(40, "active")],
        records_rows=[_records_row("active")],
    )
    outcome = _apply(state, count=1, where={"sim_time": "10"})
    assert outcome.units_selected == 1
    assert outcome.units_affected == 1
    history = state.tables["history"].data
    assert history.num_rows == 1
    assert history.column("sim_time").to_pylist() == [40]
    assert history.column("value").to_pylist() == ["active"]


def test_only_history_table_touched() -> None:
    state = _state([_row(10, "old"), _row(40, "active")])
    other = state.tables["records__actor"]
    _apply(state, count=1, where={"sim_time": "10"})
    assert state.tables["records__actor"] is other


def test_one_dropped_event_defect_per_removed_row() -> None:
    state = _state([_row(10, "a"), _row(20, "b"), _row(30, "c")])
    outcome = _apply(state, rate=1.0)
    assert outcome.units_selected == 3
    assert outcome.units_affected == 3
    assert len(outcome.defects) == 3
    assert {d.defect_class for d in outcome.defects} == {"dropped_event"}
    assert state.tables["history"].data.num_rows == 0


def test_source_coordinate_is_pre_removal_row() -> None:
    state = _state([_row(10, "old"), _row(40, "active")])
    outcome = _apply(state, count=1, where={"sim_time": "10"})
    (defect,) = outcome.defects
    keys = dict(defect.location.row.keys)
    assert keys["sim_time"] == "10"


# ---------------------------------------------------------------------------
# Empty population: no-op, never an error
# ---------------------------------------------------------------------------


def test_empty_where_match_is_noop() -> None:
    state = _state([_row(10, "old"), _row(40, "active")])
    outcome = _apply(state, rate=1.0, where={"kind": "doctor"})
    assert outcome.units_selected == 0
    assert outcome.units_affected == 0
    assert outcome.defects == ()
    assert state.tables["history"].data.num_rows == 2


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_rerun_with_same_seed_is_identical() -> None:
    rows = [_row(10, "a"), _row(20, "b"), _row(30, "c"), _row(40, "d")]
    state_a = _state(rows)
    state_b = _state(rows)
    outcome_a = _apply(state_a, count=2, seed=9)
    outcome_b = _apply(state_b, count=2, seed=9)
    assert outcome_a.defects == outcome_b.defects
    assert state_a.tables["history"].data.equals(state_b.tables["history"].data)


# ---------------------------------------------------------------------------
# Impact: the anchor-participant rule (§ The impact rule)
# ---------------------------------------------------------------------------


def test_drop_mid_series_non_anchor_declares_beyond_c1_c12() -> None:
    """Anchor is (40, "active"); dropping the older non-anchor row leaves the
    round-trip passing (the anchor survives untouched)."""
    state = _state(
        [_row(10, "old"), _row(40, "active")],
        records_rows=[_records_row("active")],
    )
    outcome = _apply(state, count=1, where={"sim_time": "10"})
    (defect,) = outcome.defects
    assert defect.impact == ("beyond-c1-c12",)


def test_drop_anchor_differing_exposed_value_declares_c6() -> None:
    """Dropping the anchor (40, "active") exposes (10, "old"), which no
    longer round-trips against the records cell "active"."""
    state = _state(
        [_row(10, "old"), _row(40, "active")],
        records_rows=[_records_row("active")],
    )
    outcome = _apply(state, count=1, where={"sim_time": "40"})
    (defect,) = outcome.defects
    assert defect.impact == ("C6",)


def test_drop_anchor_codec_equal_exposed_value_declares_beyond_c1_c12() -> None:
    """Dropping the anchor (40, "active") exposes (10, "active") -- same
    codec text as the records cell, so the round-trip still passes: the
    actual-divergence stance declares beyond-c1-c12 despite anchor
    participation."""
    state = _state(
        [_row(10, "active"), _row(40, "active")],
        records_rows=[_records_row("active")],
    )
    outcome = _apply(state, count=1, where={"sim_time": "40"})
    (defect,) = outcome.defects
    assert defect.impact == ("beyond-c1-c12",)


def test_drop_entire_c6_view_declares_all_beyond_c1_c12() -> None:
    """Dropping every pre-slice row of a series empties its C6 view: the
    series leaves C6's iteration entirely (orphaned snapshot, subconformant),
    never a C6 declaration."""
    state = _state(
        [_row(10, "v1"), _row(40, "v2")],
        records_rows=[_records_row("v2")],
    )
    outcome = _apply(state, rate=1.0)
    assert outcome.units_affected == 2
    assert {d.impact for d in outcome.defects} == {("beyond-c1-c12",)}


# ---------------------------------------------------------------------------
# Placement: RNG order = placement setup -> unit draw (no mode draws)
# ---------------------------------------------------------------------------


def test_placement_rng_order_setup_before_unit_draw() -> None:
    state = _state([_row(10, "a"), _row(20, "b"), _row(30, "c")])
    op = DropEvents(
        kind="drop_events",
        target=Target(table="history"),
        amount=Amount(count=1),
        placement=ClusteredTemporal(
            kind="clustered_temporal", column="sim_time", clusters=1, width=100
        ),
    )
    rng = CallOrderRandom(seed=3)
    _HANDLER.apply(state, op, "rule#0", rng, _FORK_PATH, _sidecar())
    assert rng.calls[0] == "sample"
    assert rng.calls[1:] == ["random"] * (len(rng.calls) - 1)
