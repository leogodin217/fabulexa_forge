"""Tests for derive_meters in the mixer app module."""

from __future__ import annotations

from collections import deque

from fabulexa_forge.exporters.streaming.mixer.app import derive_meters
from fabulexa_forge.exporters.streaming.mixer.run_state import MixerRunState
from fabulexa_forge.exporters.streaming.mixer.scheduler import (
    ControlState,
    FrontierState,
    TopicDials,
    Transport,
)
from fabulexa_forge.exporters.streaming.types import StreamEvent

from .._helpers import make_anchor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ONE_MS_NS = 1_000_000
_ONE_SEC_NS = 1_000_000_000


def _make_event(
    topic: str,
    event_sim_time: int = 0,
    record_id: str = "r1",
) -> StreamEvent:
    return StreamEvent(
        seq=1,
        op="c",
        kind="person",
        record_id=record_id,
        presentation_id=None,
        event_sim_time=event_sim_time,
        ts=event_sim_time,
        after=None,
        topic=topic,
        route_table="person",
    )


def _make_dial(
    topic: str,
    rate: float = 1.0,
    lag_ms: int = 0,
    mute: bool = False,
) -> TopicDials:
    return TopicDials(
        topic=topic,
        content="state-changes",
        rate=rate,
        lag_ms=lag_ms,
        mute=mute,
    )


def _make_state(
    topics: list[str],
    buffers: dict[str, deque[StreamEvent]],
    frontier_sim_time: int | None = None,
    edges: dict[str, int | None] | None = None,
    delivery_edges: dict[str, int | None] | None = None,
    play_origin_monotonic: float | None = None,
    monotonic_val: float = 0.0,
) -> MixerRunState:
    anchor = make_anchor()
    dials = [_make_dial(t) for t in topics]
    control = ControlState(
        transport=Transport(playing=False, speed=1.0),
        topics=dials,
    )
    frontier = FrontierState(
        frontier_sim_time=frontier_sim_time,
        edges=edges if edges is not None else {t: None for t in topics},
        delivery_edges=delivery_edges
        if delivery_edges is not None
        else {t: None for t in topics},
    )
    return MixerRunState(
        control=control,
        frontier=frontier,
        buffers=buffers,
        anchor=anchor,
        monotonic=lambda: monotonic_val,
        play_origin_monotonic=play_origin_monotonic,
    )


# ---------------------------------------------------------------------------
# frontier_sim_time
# ---------------------------------------------------------------------------


class TestFrontierSimTime:
    def test_null_while_frontier_is_none(self) -> None:
        state = _make_state(["t1"], {"t1": deque()})
        meters = derive_meters(state)
        assert meters.frontier_sim_time is None

    def test_iso_string_when_frontier_set(self) -> None:
        # 1 second into sim time
        state = _make_state(["t1"], {"t1": deque()}, frontier_sim_time=_ONE_SEC_NS)
        meters = derive_meters(state)
        assert meters.frontier_sim_time is not None
        assert isinstance(meters.frontier_sim_time, str)
        # Should contain offset-bearing ISO-8601 (has "+" or "Z" or offset)
        assert "2026-01-01T00:00:01" in meters.frontier_sim_time


# ---------------------------------------------------------------------------
# wall_elapsed_ms
# ---------------------------------------------------------------------------


class TestWallElapsedMs:
    def test_zero_while_play_origin_is_none(self) -> None:
        state = _make_state(["t1"], {"t1": deque()}, play_origin_monotonic=None)
        meters = derive_meters(state)
        assert meters.wall_elapsed_ms == 0

    def test_elapsed_from_play_origin(self) -> None:
        # monotonic = 5.0, play_origin = 2.5 → 2500 ms
        state = _make_state(
            ["t1"],
            {"t1": deque()},
            play_origin_monotonic=2.5,
            monotonic_val=5.0,
        )
        meters = derive_meters(state)
        assert meters.wall_elapsed_ms == 2500

    def test_elapsed_is_never_negative(self) -> None:
        # monotonic slightly behind play_origin (shouldn't happen but must not go negative)
        state = _make_state(
            ["t1"],
            {"t1": deque()},
            play_origin_monotonic=10.0,
            monotonic_val=9.999,
        )
        meters = derive_meters(state)
        assert meters.wall_elapsed_ms == 0


# ---------------------------------------------------------------------------
# TopicMeter fields
# ---------------------------------------------------------------------------


class TestTopicMeterBacklog:
    def test_backlog_equals_buffer_length(self) -> None:
        buf: deque[StreamEvent] = deque([_make_event("t1"), _make_event("t1")])
        state = _make_state(["t1"], {"t1": buf})
        meters = derive_meters(state)
        assert meters.topics[0].backlog == 2

    def test_backlog_zero_for_empty_buffer(self) -> None:
        state = _make_state(["t1"], {"t1": deque()})
        meters = derive_meters(state)
        assert meters.topics[0].backlog == 0


class TestTopicMeterDeliveryLagMs:
    def test_null_when_frontier_none(self) -> None:
        state = _make_state(
            ["t1"],
            {"t1": deque()},
            frontier_sim_time=None,
            delivery_edges={"t1": 1000},
        )
        meters = derive_meters(state)
        assert meters.topics[0].delivery_lag_ms is None

    def test_null_when_delivery_edge_none(self) -> None:
        state = _make_state(
            ["t1"],
            {"t1": deque()},
            frontier_sim_time=_ONE_SEC_NS,
            delivery_edges={"t1": None},
        )
        meters = derive_meters(state)
        assert meters.topics[0].delivery_lag_ms is None

    def test_lag_ms_computed_correctly(self) -> None:
        # frontier = 2 s, delivery_edge = 1 s → lag = 1 s = 1000 ms
        state = _make_state(
            ["t1"],
            {"t1": deque()},
            frontier_sim_time=2 * _ONE_SEC_NS,
            delivery_edges={"t1": _ONE_SEC_NS},
        )
        meters = derive_meters(state)
        assert meters.topics[0].delivery_lag_ms == 1000

    def test_lag_ms_is_non_negative(self) -> None:
        # frontier == delivery_edge → lag = 0
        state = _make_state(
            ["t1"],
            {"t1": deque()},
            frontier_sim_time=_ONE_SEC_NS,
            delivery_edges={"t1": _ONE_SEC_NS},
        )
        meters = derive_meters(state)
        assert meters.topics[0].delivery_lag_ms == 0


class TestTopicMeterDeliveryEdgeSimTime:
    def test_null_before_first_delivery(self) -> None:
        state = _make_state(
            ["t1"],
            {"t1": deque()},
            delivery_edges={"t1": None},
        )
        meters = derive_meters(state)
        assert meters.topics[0].delivery_edge_sim_time is None

    def test_iso_string_after_first_delivery(self) -> None:
        state = _make_state(
            ["t1"],
            {"t1": deque()},
            frontier_sim_time=_ONE_SEC_NS,
            delivery_edges={"t1": _ONE_SEC_NS},
        )
        meters = derive_meters(state)
        assert meters.topics[0].delivery_edge_sim_time is not None
        assert "2026-01-01T00:00:01" in meters.topics[0].delivery_edge_sim_time


# ---------------------------------------------------------------------------
# Topics ordering and completeness
# ---------------------------------------------------------------------------


class TestTopicsOrderAndCompleteness:
    def test_topics_order_matches_control_state(self) -> None:
        topics = ["alpha", "beta", "gamma"]
        buffers: dict[str, deque[StreamEvent]] = {t: deque() for t in topics}
        state = _make_state(topics, buffers)
        meters = derive_meters(state)
        assert [m.topic for m in meters.topics] == topics

    def test_declared_but_empty_topic_included(self) -> None:
        """A topic with no events (declared-but-empty) appears in meters."""
        topics = ["lagged", "unlagged", "empty"]
        buffers: dict[str, deque[StreamEvent]] = {t: deque() for t in topics}
        state = _make_state(topics, buffers)
        meters = derive_meters(state)
        assert len(meters.topics) == 3
        topic_names = [m.topic for m in meters.topics]
        assert "empty" in topic_names
