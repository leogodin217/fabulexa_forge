"""Shared test helpers for mixer tests."""

from __future__ import annotations

from collections import deque

from fabulexa_forge.exporters.streaming.mixer.consumer import (
    ConsumerControlState,
    ConsumerDials,
    ConsumerJobShape,
    ConsumerRunState,
    ConsumerState,
    JoinSpec,
    WindowSpec,
)
from fabulexa_forge.exporters.streaming.mixer.run_state import MixerRunState
from fabulexa_forge.exporters.streaming.mixer.scheduler import (
    ControlState,
    FrontierState,
    TopicDials,
    Transport,
)
from fabulexa_forge.exporters.streaming.types import StreamEvent

from .._helpers import make_anchor


def _make_consumer_run_state(
    topics: list[str],
    *,
    gating_topics: tuple[str, ...] | None = None,
    watermarks: dict[str, int | None] | None = None,
    consumer_lag: dict[str, int] | None = None,
    windows: tuple[WindowSpec, ...] = (),
    joins: tuple[JoinSpec, ...] = (),
    window_fired_count: list[int] | None = None,
    window_latest_end_ms: list[int | None] | None = None,
    join_fact_count: list[int] | None = None,
    join_null_count: list[int] | None = None,
) -> ConsumerRunState:
    """Build a ConsumerRunState for testing."""
    dials = [
        ConsumerDials(topic=t, content="state-changes", ingest_rate=1.0) for t in topics
    ]
    control = ConsumerControlState(topics=dials)
    if gating_topics is None:
        gating_topics = tuple(topics)
    state = ConsumerState(
        watermark_ms=(
            watermarks if watermarks is not None else {t: None for t in topics}
        ),
        consumer_lag=(
            consumer_lag if consumer_lag is not None else {t: 0 for t in topics}
        ),
        window_fired_count=(
            window_fired_count if window_fired_count is not None else [0] * len(windows)
        ),
        window_latest_end_ms=(
            window_latest_end_ms
            if window_latest_end_ms is not None
            else [None] * len(windows)
        ),
        join_fact_count=(
            join_fact_count if join_fact_count is not None else [0] * len(joins)
        ),
        join_null_count=(
            join_null_count if join_null_count is not None else [0] * len(joins)
        ),
    )
    shape = ConsumerJobShape(
        windows=windows,
        joins=joins,
        gating_topics=gating_topics,
    )
    return ConsumerRunState(control=control, state=state, shape=shape)


def _make_run_state(
    *,
    topics: list[str] | None = None,
    consumer: ConsumerRunState | None = None,
    playing: bool = False,
    speed: float = 1.0,
    play_origin_monotonic: float | None = None,
    monotonic_val: float = 0.0,
) -> MixerRunState:
    """Build a MixerRunState for testing."""
    if topics is None:
        topics = ["orders", "customers"]
    anchor = make_anchor()
    dials = [
        TopicDials(topic=t, content="state-changes", rate=1.0, lag_ms=0, mute=False)
        for t in topics
    ]
    control = ControlState(
        transport=Transport(playing=playing, speed=speed),
        topics=dials,
    )
    frontier = FrontierState(
        frontier_sim_time=None,
        edges={t: None for t in topics},
        delivery_edges={t: None for t in topics},
    )
    buffers: dict[str, deque[StreamEvent]] = {t: deque() for t in topics}
    return MixerRunState(
        control=control,
        frontier=frontier,
        buffers=buffers,
        anchor=anchor,
        monotonic=lambda: monotonic_val,
        play_origin_monotonic=play_origin_monotonic,
        consumer=consumer,
    )
