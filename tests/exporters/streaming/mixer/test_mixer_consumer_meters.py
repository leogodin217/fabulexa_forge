"""Tests for derive_consumer_meters in the mixer app module."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fabulexa_forge.exporters.streaming.mixer.app import derive_consumer_meters
from fabulexa_forge.exporters.streaming.mixer.consumer import (
    JoinSpec,
    WindowSpec,
)

from .._helpers import make_anchor
from ._helpers import _make_consumer_run_state

_UTC = timezone.utc

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ONE_SEC_MS = 1_000
_ONE_MIN_MS = 60_000

# epoch_ms for 2026-01-01T00:00:00Z
_EPOCH_MS_2026 = int(datetime(2026, 1, 1, tzinfo=_UTC).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Watermarks
# ---------------------------------------------------------------------------


class TestWatermarkRendering:
    def test_per_topic_none_watermark_renders_as_none(self) -> None:
        consumer = _make_consumer_run_state(["t1"], watermarks={"t1": None})
        anchor = make_anchor()
        meters = derive_consumer_meters(consumer, anchor)
        assert meters.topics[0].watermark_sim_time is None

    def test_per_topic_watermark_renders_as_str(self) -> None:
        consumer = _make_consumer_run_state(["t1"], watermarks={"t1": _EPOCH_MS_2026})
        anchor = make_anchor()
        meters = derive_consumer_meters(consumer, anchor)
        assert meters.topics[0].watermark_sim_time is not None
        assert isinstance(meters.topics[0].watermark_sim_time, str)

    def test_topics_in_control_order(self) -> None:
        """Topics appear in ConsumerControlState.topics order."""
        consumer = _make_consumer_run_state(
            ["alpha", "beta", "gamma"],
            watermarks={"alpha": None, "beta": _EPOCH_MS_2026, "gamma": None},
        )
        anchor = make_anchor()
        meters = derive_consumer_meters(consumer, anchor)
        assert [t.topic for t in meters.topics] == ["alpha", "beta", "gamma"]

    def test_global_watermark_none_when_any_gating_wm_none(self) -> None:
        consumer = _make_consumer_run_state(
            ["t1", "t2"],
            gating_topics=("t1", "t2"),
            watermarks={"t1": _EPOCH_MS_2026, "t2": None},
        )
        anchor = make_anchor()
        meters = derive_consumer_meters(consumer, anchor)
        assert meters.global_watermark_sim_time is None

    def test_global_watermark_min_over_gating_topics(self) -> None:
        t1_ms = _EPOCH_MS_2026
        t2_ms = _EPOCH_MS_2026 + _ONE_MIN_MS  # t2 is ahead
        consumer = _make_consumer_run_state(
            ["t1", "t2"],
            gating_topics=("t1", "t2"),
            watermarks={"t1": t1_ms, "t2": t2_ms},
        )
        anchor = make_anchor()
        meters = derive_consumer_meters(consumer, anchor)
        # global is min = t1_ms; should render t1_ms
        assert meters.global_watermark_sim_time is not None
        # t1 watermark string matches global watermark string
        assert meters.global_watermark_sim_time == meters.topics[0].watermark_sim_time

    def test_declared_but_empty_topic_excluded_from_global(self) -> None:
        """A topic outside gating_topics does not gate the global watermark."""
        consumer = _make_consumer_run_state(
            ["data", "empty"],
            gating_topics=("data",),
            watermarks={"data": _EPOCH_MS_2026, "empty": None},
        )
        anchor = make_anchor()
        meters = derive_consumer_meters(consumer, anchor)
        assert meters.global_watermark_sim_time is not None

    def test_global_watermark_none_when_zero_gating_topics(self) -> None:
        """Empty gating_topics: _compute_global_watermark returns None."""
        consumer = _make_consumer_run_state(
            ["t1"],
            gating_topics=(),
            watermarks={"t1": _EPOCH_MS_2026},
        )
        anchor = make_anchor()
        meters = derive_consumer_meters(consumer, anchor)
        assert meters.global_watermark_sim_time is None

    def test_consumer_lag_verbatim(self) -> None:
        consumer = _make_consumer_run_state(["t1"], consumer_lag={"t1": 42})
        anchor = make_anchor()
        meters = derive_consumer_meters(consumer, anchor)
        assert meters.topics[0].consumer_lag == 42


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


class TestWindowMeters:
    def test_one_window_meter_per_declared_window(self) -> None:
        windows = (WindowSpec(size_ms=1000), WindowSpec(size_ms=5000))
        consumer = _make_consumer_run_state(["t1"], windows=windows)
        anchor = make_anchor()
        meters = derive_consumer_meters(consumer, anchor)
        assert len(meters.windows) == len(windows)

    def test_unfired_window_has_none_latest_end(self) -> None:
        windows = (WindowSpec(size_ms=1000),)
        consumer = _make_consumer_run_state(
            ["t1"], windows=windows, window_fired_count=[0], window_latest_end_ms=[None]
        )
        anchor = make_anchor()
        meters = derive_consumer_meters(consumer, anchor)
        assert meters.windows[0].fired_count == 0
        assert meters.windows[0].latest_window_end_sim_time is None

    def test_fired_window_has_str_latest_end(self) -> None:
        windows = (WindowSpec(size_ms=1000),)
        consumer = _make_consumer_run_state(
            ["t1"],
            windows=windows,
            window_fired_count=[3],
            window_latest_end_ms=[_EPOCH_MS_2026 + 3000],
        )
        anchor = make_anchor()
        meters = derive_consumer_meters(consumer, anchor)
        assert meters.windows[0].fired_count == 3
        assert meters.windows[0].latest_window_end_sim_time is not None
        assert isinstance(meters.windows[0].latest_window_end_sim_time, str)

    def test_window_size_preserved(self) -> None:
        windows = (WindowSpec(size_ms=7500),)
        consumer = _make_consumer_run_state(["t1"], windows=windows)
        anchor = make_anchor()
        meters = derive_consumer_meters(consumer, anchor)
        assert meters.windows[0].size_ms == 7500


# ---------------------------------------------------------------------------
# Joins
# ---------------------------------------------------------------------------


class TestJoinMeters:
    def test_one_join_meter_per_declared_join(self) -> None:
        joins = (
            JoinSpec(fact_topic="facts", dimension_topic="dims"),
            JoinSpec(fact_topic="orders", dimension_topic="products"),
        )
        consumer = _make_consumer_run_state(
            ["facts", "dims", "orders", "products"], joins=joins
        )
        anchor = make_anchor()
        meters = derive_consumer_meters(consumer, anchor)
        assert len(meters.joins) == 2

    def test_null_rate_none_when_fact_count_zero(self) -> None:
        joins = (JoinSpec(fact_topic="facts", dimension_topic="dims"),)
        consumer = _make_consumer_run_state(
            ["facts", "dims"],
            joins=joins,
            join_fact_count=[0],
            join_null_count=[0],
        )
        anchor = make_anchor()
        meters = derive_consumer_meters(consumer, anchor)
        assert meters.joins[0].null_rate is None

    def test_null_rate_computed_when_fact_count_nonzero(self) -> None:
        joins = (JoinSpec(fact_topic="facts", dimension_topic="dims"),)
        consumer = _make_consumer_run_state(
            ["facts", "dims"],
            joins=joins,
            join_fact_count=[20],
            join_null_count=[5],
        )
        anchor = make_anchor()
        meters = derive_consumer_meters(consumer, anchor)
        assert meters.joins[0].null_rate == pytest.approx(0.25)

    def test_join_topics_preserved(self) -> None:
        joins = (JoinSpec(fact_topic="facts", dimension_topic="dims"),)
        consumer = _make_consumer_run_state(["facts", "dims"], joins=joins)
        anchor = make_anchor()
        meters = derive_consumer_meters(consumer, anchor)
        assert meters.joins[0].fact_topic == "facts"
        assert meters.joins[0].dimension_topic == "dims"
