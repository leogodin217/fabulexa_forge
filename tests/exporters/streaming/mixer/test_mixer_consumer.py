"""Tests for mixer/consumer.py — Phase 1: Consumer ingest grain.

Covers seed_consumer_run and the pure deterministic ingest fold.
"""

from __future__ import annotations

import pytest

from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.streaming.mixer.consumer import (
    ConsumerRunState,
    IngestedRecord,
    JoinSpec,
    WindowSpec,
    ingest,
    seed_consumer_run,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(topic: str, event_time_ms: int, offset: int = 0) -> IngestedRecord:
    return IngestedRecord(topic=topic, event_time_ms=event_time_ms, offset=offset)


def _run_seed(
    topic_set: tuple[str, ...] = ("topic_a", "topic_b"),
    content: str = "state-changes",
    nonempty_topics: tuple[str, ...] = ("topic_a", "topic_b"),
    windows: tuple[WindowSpec, ...] = (),
    joins: tuple[JoinSpec, ...] = (),
) -> ConsumerRunState:
    return seed_consumer_run(
        topic_set=topic_set,
        content=content,  # type: ignore[arg-type]
        nonempty_topics=nonempty_topics,
        windows=windows,
        joins=joins,
    )


# ---------------------------------------------------------------------------
# seed_consumer_run tests
# ---------------------------------------------------------------------------


class TestSeedConsumerRun:
    def test_neutral_dials_per_topic_in_order(self) -> None:
        """One neutral ConsumerDials (ingest_rate=1.0) per topic in topic_set order."""
        rs = _run_seed(
            topic_set=("alpha", "beta", "gamma"),
            content="membership-events",
            nonempty_topics=("alpha", "beta", "gamma"),
        )
        assert len(rs.control.topics) == 3
        assert [d.topic for d in rs.control.topics] == ["alpha", "beta", "gamma"]
        for dial in rs.control.topics:
            assert dial.ingest_rate == 1.0
            assert dial.content == "membership-events"

    def test_state_zeroed_per_topic(self) -> None:
        """ConsumerState has None watermark and 0 lag per topic, zeroed counters."""
        rs = _run_seed(
            topic_set=("t1", "t2"),
            windows=(WindowSpec(size_ms=1000),),
            joins=(JoinSpec(fact_topic="t1", dimension_topic="t2"),),
        )
        st = rs.state
        assert st.watermark_ms == {"t1": None, "t2": None}
        assert st.consumer_lag == {"t1": 0, "t2": 0}
        assert st.window_fired_count == [0]
        assert st.window_latest_end_ms == [None]
        assert st.join_fact_count == [0]
        assert st.join_null_count == [0]

    def test_gating_topics_equals_nonempty_topics(self) -> None:
        """shape.gating_topics == nonempty_topics (data-bearing only)."""
        rs = _run_seed(
            topic_set=("full", "empty"),
            nonempty_topics=("full",),
        )
        assert rs.shape.gating_topics == ("full",)

    def test_dial_stamped_with_content(self) -> None:
        """Each dial carries the run's content axis."""
        rs = _run_seed(content="membership-events")
        for dial in rs.control.topics:
            assert dial.content == "membership-events"

    def test_joinspec_absent_topic_raises(self) -> None:
        """A JoinSpec referencing a topic absent from topic_set raises ExportError."""
        with pytest.raises(ExportError, match="fact_topic"):
            _run_seed(
                topic_set=("a", "b"),
                joins=(JoinSpec(fact_topic="missing", dimension_topic="a"),),
            )

    def test_joinspec_absent_dimension_raises(self) -> None:
        """A JoinSpec with absent dimension_topic raises ExportError."""
        with pytest.raises(ExportError, match="dimension_topic"):
            _run_seed(
                topic_set=("a", "b"),
                joins=(JoinSpec(fact_topic="a", dimension_topic="ghost"),),
            )

    def test_windowspec_zero_size_raises(self) -> None:
        """A WindowSpec with size_ms == 0 raises ExportError."""
        with pytest.raises(ExportError, match="size_ms"):
            _run_seed(windows=(WindowSpec(size_ms=0),))

    def test_windowspec_negative_size_raises(self) -> None:
        """A WindowSpec with size_ms < 0 raises ExportError."""
        with pytest.raises(ExportError, match="size_ms"):
            _run_seed(windows=(WindowSpec(size_ms=-100),))


# ---------------------------------------------------------------------------
# ingest — watermark tests
# ---------------------------------------------------------------------------


class TestIngestWatermarks:
    def test_per_topic_watermark_advances_to_max_event_time(self) -> None:
        """Per-topic watermark = max event-time (last record, per-topic order trusted)."""
        rs = _run_seed(topic_set=("a", "b"), nonempty_topics=("a", "b"))
        pulled = {
            "a": [_make_record("a", 100), _make_record("a", 200)],
            "b": [_make_record("b", 50)],
        }
        ingest(rs.control, rs.state, rs.shape, pulled, lag={})
        assert rs.state.watermark_ms["a"] == 200
        assert rs.state.watermark_ms["b"] == 50

    def test_topic_with_no_records_holds_watermark(self) -> None:
        """A topic with no pulled records this tick holds its watermark."""
        rs = _run_seed(topic_set=("a", "b"), nonempty_topics=("a", "b"))
        # Tick 1: advance topic_a
        ingest(
            rs.control,
            rs.state,
            rs.shape,
            pulled={"a": [_make_record("a", 100)]},
            lag={},
        )
        assert rs.state.watermark_ms["a"] == 100
        assert rs.state.watermark_ms["b"] is None

        # Tick 2: only topic_b gets records; topic_a holds
        ingest(
            rs.control,
            rs.state,
            rs.shape,
            pulled={"b": [_make_record("b", 80)]},
            lag={},
        )
        assert rs.state.watermark_ms["a"] == 100  # unchanged
        assert rs.state.watermark_ms["b"] == 80

    def test_global_watermark_is_min_over_gating_topics(self) -> None:
        """Global watermark = min over gating_topics after ingest."""
        rs = _run_seed(
            topic_set=("a", "b"),
            nonempty_topics=("a", "b"),
            windows=(WindowSpec(size_ms=10000),),  # large window to avoid firing
        )
        pulled = {
            "a": [_make_record("a", 500)],
            "b": [_make_record("b", 300)],
        }
        ingest(rs.control, rs.state, rs.shape, pulled, lag={})
        # global_wm = min(500, 300) = 300; window_origin set to 300
        assert rs.state.window_origin_ms == 300

    def test_global_watermark_none_while_any_gating_topic_is_none(self) -> None:
        """Global watermark = None while any gating topic's watermark is None."""
        rs = _run_seed(
            topic_set=("a", "b"),
            nonempty_topics=("a", "b"),
            windows=(WindowSpec(size_ms=10),),
        )
        # Only topic_a ingested; topic_b still None
        ingest(
            rs.control,
            rs.state,
            rs.shape,
            pulled={"a": [_make_record("a", 100)]},
            lag={},
        )
        # global_wm is None → origin stays None → no windows fire
        assert rs.state.window_origin_ms is None
        assert rs.state.window_fired_count[0] == 0

    def test_empty_topic_absent_from_gating_never_gates(self) -> None:
        """A declared-but-empty topic absent from gating_topics never stalls the pipeline."""
        rs = _run_seed(
            topic_set=("data", "empty"),
            nonempty_topics=("data",),  # empty not gating
            windows=(WindowSpec(size_ms=100),),
        )
        # data topic has watermark; empty has None — but empty is NOT gating
        ingest(
            rs.control,
            rs.state,
            rs.shape,
            pulled={"data": [_make_record("data", 200)]},
            lag={},
        )
        # global_wm = min over ("data",) = 200; origin = 200
        assert rs.state.window_origin_ms == 200

    def test_consumer_lag_overwritten_each_tick(self) -> None:
        """consumer_lag is overwritten from the lag arg each tick."""
        rs = _run_seed(topic_set=("a", "b"), nonempty_topics=("a", "b"))
        ingest(rs.control, rs.state, rs.shape, pulled={}, lag={"a": 10, "b": 20})
        assert rs.state.consumer_lag["a"] == 10
        assert rs.state.consumer_lag["b"] == 20

        ingest(rs.control, rs.state, rs.shape, pulled={}, lag={"a": 5, "b": 0})
        assert rs.state.consumer_lag["a"] == 5
        assert rs.state.consumer_lag["b"] == 0


# ---------------------------------------------------------------------------
# ingest — window tests
# ---------------------------------------------------------------------------


class TestIngestWindows:
    def test_window_fires_when_global_wm_crosses_end(self) -> None:
        """Window fires (fired_count++, latest_end advances) when window_end <= global_wm."""
        rs = _run_seed(
            topic_set=("t",),
            nonempty_topics=("t",),
            windows=(WindowSpec(size_ms=100),),
        )
        # Tick 1: global_wm = 50 → origin = 50, first window_end = 150 > 50 → no fire
        ingest(
            rs.control,
            rs.state,
            rs.shape,
            pulled={"t": [_make_record("t", 50)]},
            lag={},
        )
        assert rs.state.window_fired_count[0] == 0
        assert rs.state.window_latest_end_ms[0] is None

        # Tick 2: global_wm = 150 → window_end = 150 ≤ 150 → fires once
        ingest(
            rs.control,
            rs.state,
            rs.shape,
            pulled={"t": [_make_record("t", 150)]},
            lag={},
        )
        assert rs.state.window_fired_count[0] == 1
        assert rs.state.window_latest_end_ms[0] == 150  # = origin(50) + size_ms(100)

    def test_stalled_global_wm_freezes_fired_count(self) -> None:
        """A stalled global watermark freezes fired_count."""
        rs = _run_seed(
            topic_set=("t",),
            nonempty_topics=("t",),
            windows=(WindowSpec(size_ms=100),),
        )
        # Advance to fire one window
        ingest(
            rs.control,
            rs.state,
            rs.shape,
            pulled={"t": [_make_record("t", 200)]},
            lag={},
        )
        count_after_tick1 = rs.state.window_fired_count[0]

        # Second tick: no new records for the gating topic → watermark doesn't advance
        ingest(rs.control, rs.state, rs.shape, pulled={}, lag={})
        assert rs.state.window_fired_count[0] == count_after_tick1

    def test_multiple_windows_fire_independently(self) -> None:
        """Multiple windows fire independently based on their own size."""
        rs = _run_seed(
            topic_set=("t",),
            nonempty_topics=("t",),
            windows=(WindowSpec(size_ms=100), WindowSpec(size_ms=500)),
        )
        # global_wm = 1000; origin = 1000
        # window A (100ms): first_end = 1100 > 1000 → no fire
        # window B (500ms): first_end = 1500 > 1000 → no fire
        ingest(
            rs.control,
            rs.state,
            rs.shape,
            pulled={"t": [_make_record("t", 1000)]},
            lag={},
        )
        assert rs.state.window_fired_count[0] == 0
        assert rs.state.window_fired_count[1] == 0

        # global_wm = 1200
        # window A: 1100 <= 1200 → fires (1 fire), 1200 <= 1200 → fires (2 fires), 1300 > 1200 → stop
        # window B: 1500 > 1200 → no fire
        ingest(
            rs.control,
            rs.state,
            rs.shape,
            pulled={"t": [_make_record("t", 1200)]},
            lag={},
        )
        assert rs.state.window_fired_count[0] == 2
        assert rs.state.window_fired_count[1] == 0

        # global_wm = 1600
        # window A: 1300 <= 1600 → 1, 1400 → 2, 1500 → 3, 1600 → 4, 1700 > 1600 → stop → 4 more fires
        # window B: 1500 <= 1600 → 1, 2000 > 1600 → stop → 1 fire
        ingest(
            rs.control,
            rs.state,
            rs.shape,
            pulled={"t": [_make_record("t", 1600)]},
            lag={},
        )
        assert rs.state.window_fired_count[0] == 6
        assert rs.state.window_fired_count[1] == 1

    def test_window_origin_is_first_global_wm(self) -> None:
        """Window origin is the global watermark's first value; first window_end = origin + size_ms."""
        rs = _run_seed(
            topic_set=("t",),
            nonempty_topics=("t",),
            windows=(WindowSpec(size_ms=100),),
        )
        # Tick 1: first global_wm = 1050 (non-round to distinguish from epoch 0)
        ingest(
            rs.control,
            rs.state,
            rs.shape,
            pulled={"t": [_make_record("t", 1050)]},
            lag={},
        )
        assert rs.state.window_origin_ms == 1050
        assert rs.state.window_fired_count[0] == 0  # 1150 > 1050

        # Tick 2: global_wm = 1150 → window_end = 1150 (origin + size_ms) ≤ 1150 → fires
        ingest(
            rs.control,
            rs.state,
            rs.shape,
            pulled={"t": [_make_record("t", 1150)]},
            lag={},
        )
        assert rs.state.window_fired_count[0] == 1
        assert rs.state.window_latest_end_ms[0] == 1150  # = origin(1050) + size(100)

    def test_single_tick_crosses_multiple_window_boundaries(self) -> None:
        """A single tick crossing multiple window boundaries fires once per crossed window."""
        rs = _run_seed(
            topic_set=("t",),
            nonempty_topics=("t",),
            windows=(WindowSpec(size_ms=100),),
        )
        # Tick 1: establish origin = 500
        ingest(
            rs.control,
            rs.state,
            rs.shape,
            pulled={"t": [_make_record("t", 500)]},
            lag={},
        )
        assert rs.state.window_fired_count[0] == 0

        # Tick 2: jump to 850 → windows at 600, 700, 800 all fire (850 >= 800); 900 > 850
        ingest(
            rs.control,
            rs.state,
            rs.shape,
            pulled={"t": [_make_record("t", 850)]},
            lag={},
        )
        assert rs.state.window_fired_count[0] == 3
        assert rs.state.window_latest_end_ms[0] == 800


# ---------------------------------------------------------------------------
# ingest — join tests
# ---------------------------------------------------------------------------


class TestIngestJoins:
    def test_fact_count_increments_per_pulled_fact_record(self) -> None:
        """join_fact_count increments once per pulled fact record."""
        rs = _run_seed(
            topic_set=("facts", "dims"),
            nonempty_topics=("facts", "dims"),
            joins=(JoinSpec(fact_topic="facts", dimension_topic="dims"),),
        )
        pulled = {
            "facts": [
                _make_record("facts", 100),
                _make_record("facts", 200),
                _make_record("facts", 300),
            ]
        }
        ingest(rs.control, rs.state, rs.shape, pulled, lag={})
        assert rs.state.join_fact_count[0] == 3

    def test_null_count_increments_when_dim_wm_none(self) -> None:
        """join_null_count++ when dimension topic watermark is None."""
        rs = _run_seed(
            topic_set=("facts", "dims"),
            nonempty_topics=("facts", "dims"),
            joins=(JoinSpec(fact_topic="facts", dimension_topic="dims"),),
        )
        # Only facts; dimension watermark stays None
        pulled = {"facts": [_make_record("facts", 100)]}
        ingest(rs.control, rs.state, rs.shape, pulled, lag={})
        assert rs.state.join_null_count[0] == 1

    def test_null_count_increments_when_dim_wm_lags_fact(self) -> None:
        """join_null_count++ when dim watermark < fact event_time_ms."""
        rs = _run_seed(
            topic_set=("facts", "dims"),
            nonempty_topics=("facts", "dims"),
            joins=(JoinSpec(fact_topic="facts", dimension_topic="dims"),),
        )
        # Dim has watermark 50; fact is at 100 → null (50 < 100)
        rs.state.watermark_ms["dims"] = 50
        pulled = {"facts": [_make_record("facts", 100)]}
        ingest(rs.control, rs.state, rs.shape, pulled, lag={})
        assert rs.state.join_null_count[0] == 1

    def test_no_null_when_dim_watermark_caught_up(self) -> None:
        """No null_count increment when dimension watermark has caught up."""
        rs = _run_seed(
            topic_set=("facts", "dims"),
            nonempty_topics=("facts", "dims"),
            joins=(JoinSpec(fact_topic="facts", dimension_topic="dims"),),
        )
        # Tick 1: advance dimension to 200
        ingest(
            rs.control,
            rs.state,
            rs.shape,
            pulled={"dims": [_make_record("dims", 200)]},
            lag={},
        )
        # Tick 2: facts at 100, 150 — both <= dim watermark (200)
        ingest(
            rs.control,
            rs.state,
            rs.shape,
            pulled={"facts": [_make_record("facts", 100), _make_record("facts", 150)]},
            lag={},
        )
        assert rs.state.join_fact_count[0] == 2
        assert rs.state.join_null_count[0] == 0

    def test_join_uses_end_of_tick_dim_watermark(self) -> None:
        """Join evaluates against the end-of-tick dimension watermark (updated same tick)."""
        rs = _run_seed(
            topic_set=("facts", "dims"),
            nonempty_topics=("facts", "dims"),
            joins=(JoinSpec(fact_topic="facts", dimension_topic="dims"),),
        )
        # Both fact and dim ingested same tick: dim updates first (watermarks updated
        # before join processing), then join checks dim wm against fact event_time
        pulled = {
            "facts": [_make_record("facts", 100)],
            "dims": [_make_record("dims", 200)],
        }
        ingest(rs.control, rs.state, rs.shape, pulled, lag={})
        # dim wm = 200 after ingest, fact at 100 ≤ 200 → no null
        assert rs.state.join_null_count[0] == 0


# ---------------------------------------------------------------------------
# ingest — determinism
# ---------------------------------------------------------------------------


class TestIngestDeterminism:
    def test_same_inputs_produce_identical_state_mutation(self) -> None:
        """Determinism: same inputs twice produce identical state mutations."""

        def make_run() -> ConsumerRunState:
            return _run_seed(
                topic_set=("a", "b"),
                nonempty_topics=("a", "b"),
                windows=(WindowSpec(size_ms=100),),
                joins=(JoinSpec(fact_topic="a", dimension_topic="b"),),
            )

        pulled = {
            "a": [_make_record("a", 250), _make_record("a", 300)],
            "b": [_make_record("b", 200)],
        }
        lag = {"a": 5, "b": 10}

        rs1 = make_run()
        rs2 = make_run()

        ingest(rs1.control, rs1.state, rs1.shape, pulled, lag)
        ingest(rs2.control, rs2.state, rs2.shape, pulled, lag)

        assert rs1.state.watermark_ms == rs2.state.watermark_ms
        assert rs1.state.consumer_lag == rs2.state.consumer_lag
        assert rs1.state.window_fired_count == rs2.state.window_fired_count
        assert rs1.state.window_latest_end_ms == rs2.state.window_latest_end_ms
        assert rs1.state.join_fact_count == rs2.state.join_fact_count
        assert rs1.state.join_null_count == rs2.state.join_null_count
        assert rs1.state.window_origin_ms == rs2.state.window_origin_ms
