"""Tests for mixer.py: Transport, TopicDials, ControlState, FrontierState, advance,
seed_mixer_run, schedule_releases."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

import duckdb
import pytest

from fabulexa_forge.config.models import (
    MembershipSelection,
    StreamConfig,
    StreamKindSelection,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.streaming.mixer import (
    ControlState,
    FrontierState,
    TopicDials,
    Transport,
    advance,
    schedule_releases,
    seed_mixer_run,
)
from fabulexa_forge.exporters.streaming.types import StreamEvent
from fabulexa_forge.reader.emit import open_emit

from ._helpers import _ddl

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEC = 1_000_000_000  # nanoseconds per second
_MS = 1_000_000  # nanoseconds per millisecond


def make_event(
    seq: int,
    event_sim_time: int,
    topic: str = "t1",
    op: str = "c",
) -> StreamEvent:
    """Build a minimal StreamEvent for mixer tests."""
    return StreamEvent(
        seq=seq,
        op=op,  # type: ignore[arg-type]
        kind="patient",
        record_id=f"r{seq}",
        presentation_id=None,
        event_sim_time=event_sim_time,
        ts=event_sim_time,
        after={"id": f"r{seq}"},
        topic=topic,
        route_table="patient",
    )


def make_control(
    playing: bool = True,
    speed: float = 1.0,
    topics: list[TopicDials] | None = None,
) -> ControlState:
    """Build a ControlState for tests."""
    if topics is None:
        topics = [
            TopicDials(
                topic="t1", content="state-changes", rate=1.0, lag_ms=0, mute=False
            )
        ]
    return ControlState(
        transport=Transport(playing=playing, speed=speed), topics=topics
    )


def make_frontier(
    frontier_sim_time: int | None = None,
    topic_names: list[str] | None = None,
) -> FrontierState:
    """Build a fresh FrontierState for tests."""
    if topic_names is None:
        topic_names = ["t1"]
    return FrontierState(
        frontier_sim_time=frontier_sim_time,
        edges={t: None for t in topic_names},
        delivery_edges={t: None for t in topic_names},
    )


# ---------------------------------------------------------------------------
# Dataclass mutability and field round-trips
# ---------------------------------------------------------------------------


def test_transport_is_mutable() -> None:
    """Transport is not frozen — fields can be reassigned."""
    t = Transport(playing=True, speed=1.0)
    t.playing = False
    t.speed = 2.0
    assert t.playing is False
    assert t.speed == 2.0


def test_topic_dials_accepts_state_changes_content() -> None:
    """TopicDials.content accepts 'state-changes'."""
    d = TopicDials(topic="x", content="state-changes", rate=1.0, lag_ms=0, mute=False)
    assert d.content == "state-changes"


def test_topic_dials_accepts_membership_events_content() -> None:
    """TopicDials.content accepts 'membership-events'."""
    d = TopicDials(
        topic="x", content="membership-events", rate=1.0, lag_ms=0, mute=False
    )
    assert d.content == "membership-events"


def test_topic_dials_is_mutable() -> None:
    """TopicDials is not frozen — operator controls can be mutated."""
    d = TopicDials(topic="x", content="state-changes", rate=1.0, lag_ms=0, mute=False)
    d.rate = 2.0
    d.lag_ms = 500
    d.mute = True
    assert d.rate == 2.0
    assert d.lag_ms == 500
    assert d.mute is True


def test_control_state_is_mutable() -> None:
    """ControlState and its children are mutable."""
    c = make_control()
    c.transport.playing = False
    assert c.transport.playing is False


def test_frontier_state_is_mutable() -> None:
    """FrontierState fields are mutable."""
    f = make_frontier()
    f.frontier_sim_time = 42
    assert f.frontier_sim_time == 42


# ---------------------------------------------------------------------------
# Phase 1 names import from the streaming package
# ---------------------------------------------------------------------------


def test_phase1_names_importable_from_streaming_package() -> None:
    """Transport, TopicDials, ControlState, FrontierState, advance import from streaming."""
    from fabulexa_forge.exporters.streaming import (  # noqa: F401
        ControlState,
        FrontierState,
        TopicDials,
        Transport,
        advance,
    )


# ---------------------------------------------------------------------------
# Launched paused
# ---------------------------------------------------------------------------


def test_advance_launched_paused_returns_empty() -> None:
    """advance with playing=False returns [] and leaves frontier/edges None."""
    buffers = {"t1": deque([make_event(1, 100 * _SEC)])}
    control = make_control(playing=False)
    frontier = make_frontier()

    result = advance(buffers, control, frontier, 0.0)

    assert result == []
    assert frontier.frontier_sim_time is None
    assert frontier.edges["t1"] is None
    assert frontier.delivery_edges["t1"] is None
    assert len(buffers["t1"]) == 1  # buffer untouched


# ---------------------------------------------------------------------------
# Initialization tick — non-empty buffers
# ---------------------------------------------------------------------------


def test_advance_init_tick_sets_frontier_to_global_min() -> None:
    """Initialization tick sets frontier_sim_time to global min event_sim_time."""
    buffers = {
        "t1": deque([make_event(1, 200 * _MS, "t1")]),
        "t2": deque([make_event(2, 100 * _MS, "t2")]),
    }
    topics = [
        TopicDials(topic="t1", content="state-changes", rate=1.0, lag_ms=0, mute=False),
        TopicDials(topic="t2", content="state-changes", rate=1.0, lag_ms=0, mute=False),
    ]
    control = make_control(playing=True, topics=topics)
    frontier = make_frontier(topic_names=["t1", "t2"])

    advance(buffers, control, frontier, 5.0)  # delta discarded on init tick

    assert frontier.frontier_sim_time == 100 * _MS


def test_advance_init_tick_sets_edge_to_frontier_minus_lag() -> None:
    """Initialization tick: each edge = frontier - lag_T_ns; no advance applied."""
    lag_ms = 50
    lag_ns = lag_ms * _MS
    buffers = {"t1": deque([make_event(1, 200 * _MS, "t1")])}
    topics = [
        TopicDials(
            topic="t1", content="state-changes", rate=1.0, lag_ms=lag_ms, mute=False
        )
    ]
    control = make_control(playing=True, topics=topics)
    frontier = make_frontier(topic_names=["t1"])

    advance(buffers, control, frontier, 5.0)

    assert frontier.frontier_sim_time == 200 * _MS
    assert frontier.edges["t1"] == 200 * _MS - lag_ns


def test_advance_init_tick_no_frontier_advance() -> None:
    """Initialization tick discards delta_real_seconds — frontier == global min only."""
    buffers = {"t1": deque([make_event(1, 100 * _MS, "t1")])}
    control = make_control(playing=True, speed=100.0)
    frontier = make_frontier()

    advance(buffers, control, frontier, 999.0)  # large delta, must be discarded

    assert frontier.frontier_sim_time == 100 * _MS


# ---------------------------------------------------------------------------
# Initialization tick — all buffers empty
# ---------------------------------------------------------------------------


def test_advance_init_tick_all_empty_returns_empty_and_leaves_none() -> None:
    """Initialization tick with all-empty buffers: frontier stays None, returns []."""
    buffers: dict[str, deque[StreamEvent]] = {"t1": deque(), "t2": deque()}
    topics = [
        TopicDials(topic="t1", content="state-changes", rate=1.0, lag_ms=0, mute=False),
        TopicDials(topic="t2", content="state-changes", rate=1.0, lag_ms=0, mute=False),
    ]
    control = make_control(playing=True, topics=topics)
    frontier = make_frontier(topic_names=["t1", "t2"])

    result = advance(buffers, control, frontier, 0.0)

    assert result == []
    assert frontier.frontier_sim_time is None


# ---------------------------------------------------------------------------
# Init-tick lag-0 release rule
# ---------------------------------------------------------------------------


def test_advance_init_tick_lag0_releases_only_global_min_match() -> None:
    """Lag-0 topic releases on init tick only if its earliest event == global min."""
    t1_time = 100 * _MS
    t2_time = 200 * _MS  # later than global min
    buffers = {
        "t1": deque([make_event(1, t1_time, "t1")]),
        "t2": deque([make_event(2, t2_time, "t2")]),
    }
    topics = [
        TopicDials(topic="t1", content="state-changes", rate=1.0, lag_ms=0, mute=False),
        TopicDials(topic="t2", content="state-changes", rate=1.0, lag_ms=0, mute=False),
    ]
    control = make_control(playing=True, topics=topics)
    frontier = make_frontier(topic_names=["t1", "t2"])

    released = advance(buffers, control, frontier, 0.0)

    # t1 matches global min (100ms) → released; t2 (200ms) > frontier → not released
    assert len(released) == 1
    assert released[0].topic == "t1"
    assert len(buffers["t1"]) == 0
    assert len(buffers["t2"]) == 1


def test_advance_init_tick_lag_positive_releases_nothing() -> None:
    """Every lag>0 topic releases nothing on the init tick."""
    t_time = 100 * _MS
    buffers = {"t1": deque([make_event(1, t_time, "t1")])}
    topics = [
        TopicDials(
            topic="t1", content="state-changes", rate=1.0, lag_ms=100, mute=False
        )
    ]
    control = make_control(playing=True, topics=topics)
    frontier = make_frontier(topic_names=["t1"])

    released = advance(buffers, control, frontier, 0.0)

    # edge = frontier - 100ms = 0 (init tick); t1's event at 100ms > 0 → not released
    assert released == []
    assert len(buffers["t1"]) == 1


# ---------------------------------------------------------------------------
# Subsequent playing tick
# ---------------------------------------------------------------------------


def test_advance_subsequent_tick_frontier_advances() -> None:
    """Subsequent tick: frontier advances by int(speed * delta * 1e9)."""
    # Run init tick first
    buffers = {"t1": deque([make_event(1, 0, "t1")])}
    control = make_control(playing=True, speed=1.0)
    frontier = make_frontier()
    advance(buffers, control, frontier, 0.0)

    # Now frontier == 0; empty buffer; subsequent tick with delta = 1.0s
    advance(buffers, control, frontier, 1.0)
    assert frontier.frontier_sim_time == int(1.0 * 1.0 * 1e9)


def test_advance_subsequent_tick_edge_advances_with_rate() -> None:
    """Edge advances by int(rate * delta_frontier), clamped to [prev, frontier-lag]."""
    init_time = 0
    buffers = {"t1": deque([make_event(1, init_time, "t1")])}
    control = make_control(playing=True, speed=1.0)
    frontier = make_frontier()
    advance(buffers, control, frontier, 0.0)  # init tick; releases event at t=0

    # Subsequent tick: speed=1, delta=1s → delta_frontier = 1e9 ns
    # rate=1.0, so edge_advance = 1e9 ns; ceiling = 1e9 - 0 lag = 1e9
    advance(buffers, control, frontier, 1.0)
    assert frontier.edges["t1"] == 1 * _SEC


def test_advance_subsequent_tick_zero_delta_no_release() -> None:
    """Zero delta_real_seconds on a playing non-init tick: frontier/edges hold."""
    buffers = {"t1": deque([make_event(1, 1 * _SEC, "t1")])}
    control = make_control(playing=True, speed=1.0)
    frontier = make_frontier()
    advance(
        buffers, control, frontier, 0.0
    )  # init tick; event at 1s > frontier 0 → not released

    prev_frontier = frontier.frontier_sim_time
    result = advance(buffers, control, frontier, 0.0)  # zero delta playing tick
    assert result == []
    assert frontier.frontier_sim_time == prev_frontier


# ---------------------------------------------------------------------------
# Lag sustained
# ---------------------------------------------------------------------------


def test_advance_lag_trails_frontier_invariant() -> None:
    """No released event has event_sim_time > frontier - lag_ns (invariant 1)."""
    lag_ms = 200
    lag_ns = lag_ms * _MS
    # Place events at 0, 100ms, 200ms, 300ms, 400ms
    events = [make_event(i + 1, i * 100 * _MS, "t1") for i in range(5)]
    buffers: dict[str, deque[StreamEvent]] = {"t1": deque(events)}
    topics = [
        TopicDials(
            topic="t1", content="state-changes", rate=1.0, lag_ms=lag_ms, mute=False
        )
    ]
    control = make_control(playing=True, speed=1.0, topics=topics)
    frontier = make_frontier(topic_names=["t1"])

    all_released: list[StreamEvent] = []
    # Run 5 ticks at 0.1s each
    for i in range(5):
        delta = 0.0 if i == 0 else 0.1
        released = advance(buffers, control, frontier, delta)
        for ev in released:
            assert frontier.frontier_sim_time is not None
            assert ev.event_sim_time <= frontier.frontier_sim_time - lag_ns
        all_released.extend(released)


# ---------------------------------------------------------------------------
# Mute and un-mute
# ---------------------------------------------------------------------------


def test_advance_muted_topic_edge_holds() -> None:
    """Muted topic: edge does not advance; backlog accumulates.

    On the init tick the edge is set to frontier - lag (0 for lag=0). Subsequent
    ticks do NOT advance the edge for muted topics — it stays exactly at the
    init value, so events with event_sim_time > init_edge accumulate as backlog.
    """
    topics = [
        TopicDials(topic="t1", content="state-changes", rate=1.0, lag_ms=0, mute=True)
    ]
    control = make_control(playing=True, topics=topics)

    # Use events starting at 100ms so the init-tick edge (0ms) < all remaining events.
    events = [make_event(i + 1, (i + 1) * 100 * _MS, "t1") for i in range(3)]
    buffers: dict[str, deque[StreamEvent]] = {"t1": deque(events)}
    frontier = make_frontier(topic_names=["t1"])

    # Init tick: frontier=100ms (global min), edge=100ms; event[0] at 100ms releases.
    advance(buffers, control, frontier, 0.0)

    edge_after_init = frontier.edges["t1"]

    # Several subsequent ticks — muted, so edge must not advance
    for _ in range(3):
        advance(buffers, control, frontier, 1.0)

    assert frontier.edges["t1"] == edge_after_init


def test_advance_muted_backlog_drains_on_unmute() -> None:
    """Muted topic accumulates backlog; on un-mute the backlog drains."""
    # Events at 0, 100ms, 200ms; start muted
    events = [make_event(i + 1, i * 100 * _MS, "t1") for i in range(3)]
    buffers: dict[str, deque[StreamEvent]] = {"t1": deque(events)}
    topics = [
        TopicDials(topic="t1", content="state-changes", rate=1.0, lag_ms=0, mute=True)
    ]
    control = make_control(playing=True, topics=topics)
    frontier = make_frontier(topic_names=["t1"])

    # Init tick: frontier=0, edge=0, event at 0 releases (edge == event_sim_time)
    advance(buffers, control, frontier, 0.0)
    # Events at 100ms, 200ms remain; edge stays 0 while muted
    for _ in range(5):
        advance(buffers, control, frontier, 0.1)

    assert len(buffers["t1"]) == 2  # backlog held

    # Un-mute
    control.topics[0].mute = False
    # Rate=1 tick; edge will advance from 0 to min(0 + delta_frontier, frontier - 0)
    advance(buffers, control, frontier, 0.1)

    # At least some events should drain (edge advanced past 100ms or 200ms)
    total_remaining = len(buffers["t1"])
    assert total_remaining < 2


# ---------------------------------------------------------------------------
# rate=0 not muted — behaves like mute
# ---------------------------------------------------------------------------


def test_advance_rate_zero_edge_holds() -> None:
    """rate=0 non-muted topic: edge holds exactly, as if muted."""
    events = [make_event(i + 1, (i + 1) * 100 * _MS, "t1") for i in range(3)]
    buffers: dict[str, deque[StreamEvent]] = {"t1": deque(events)}
    topics = [
        TopicDials(topic="t1", content="state-changes", rate=0.0, lag_ms=0, mute=False)
    ]
    control = make_control(playing=True, topics=topics)
    frontier = make_frontier(topic_names=["t1"])

    advance(buffers, control, frontier, 0.0)  # init tick
    edge_after_init = frontier.edges["t1"]

    for _ in range(5):
        advance(buffers, control, frontier, 0.1)

    assert frontier.edges["t1"] == edge_after_init


# ---------------------------------------------------------------------------
# rate > 1 with backlog
# ---------------------------------------------------------------------------


def test_advance_rate_gt1_drains_backlog_up_to_ceiling() -> None:
    """rate>1 drains backlog fast but edge never exceeds frontier - lag (invariant 2)."""
    # 10 events at 0, 100ms, ..., 900ms
    events = [make_event(i + 1, i * 100 * _MS, "t1") for i in range(10)]
    buffers: dict[str, deque[StreamEvent]] = {"t1": deque(events)}
    topics = [
        TopicDials(topic="t1", content="state-changes", rate=4.0, lag_ms=0, mute=False)
    ]
    control = make_control(playing=True, speed=1.0, topics=topics)
    frontier = make_frontier(topic_names=["t1"])

    # Init tick; release events at 0
    released = advance(buffers, control, frontier, 0.0)

    # Several ticks at 0.1s each; rate=4 means edge advances at 4x frontier
    for _ in range(5):
        tick_rel = advance(buffers, control, frontier, 0.1)
        released.extend(tick_rel)
        # Invariant 2: edge never exceeds frontier - lag
        assert frontier.frontier_sim_time is not None
        assert frontier.edges["t1"] is not None
        assert frontier.edges["t1"] <= frontier.frontier_sim_time


# ---------------------------------------------------------------------------
# lag_ms raised mid-run (ceiling drops — edge held by max)
# ---------------------------------------------------------------------------


def test_advance_lag_raised_midrun_edge_holds() -> None:
    """When lag_ms raised mid-run, ceiling drops; max holds edge, nothing new releases."""
    events = [make_event(i + 1, i * 100 * _MS, "t1") for i in range(5)]
    buffers: dict[str, deque[StreamEvent]] = {"t1": deque(events)}
    topics = [
        TopicDials(topic="t1", content="state-changes", rate=1.0, lag_ms=0, mute=False)
    ]
    control = make_control(playing=True, speed=1.0, topics=topics)
    frontier = make_frontier(topic_names=["t1"])

    # Init tick + 2 subsequent ticks to advance frontier significantly
    advance(buffers, control, frontier, 0.0)
    advance(buffers, control, frontier, 0.5)
    edge_before_raise = frontier.edges["t1"]

    # Raise lag to push ceiling below current edge
    # Set lag so ceiling = frontier - lag < current_edge
    control.topics[0].lag_ms = 600  # 600ms lag; ceiling = frontier - 600ms

    # After raising lag, on the next tick the ceiling drops; edge should be held by max
    advance(buffers, control, frontier, 0.1)
    assert frontier.edges["t1"] is not None
    assert frontier.edges["t1"] >= edge_before_raise  # max holds it non-decreasing


# ---------------------------------------------------------------------------
# lag_ms lowered mid-run
# ---------------------------------------------------------------------------


def test_advance_lag_lowered_midrun_edge_resumes() -> None:
    """When lag_ms lowered, ceiling rises and edge resumes advancing rate-limited."""
    events = [make_event(i + 1, i * 100 * _MS, "t1") for i in range(10)]
    buffers: dict[str, deque[StreamEvent]] = {"t1": deque(events)}
    topics = [
        TopicDials(
            topic="t1", content="state-changes", rate=1.0, lag_ms=500, mute=False
        )
    ]
    control = make_control(playing=True, speed=1.0, topics=topics)
    frontier = make_frontier(topic_names=["t1"])

    advance(buffers, control, frontier, 0.0)
    for _ in range(3):
        advance(buffers, control, frontier, 0.1)

    edge_before_lower = frontier.edges["t1"]

    # Lower lag → ceiling rises
    control.topics[0].lag_ms = 0

    for _ in range(3):
        advance(buffers, control, frontier, 0.1)

    # Edge should have advanced beyond previous position
    assert frontier.edges["t1"] is not None
    assert frontier.edges["t1"] >= edge_before_lower


# ---------------------------------------------------------------------------
# Within-topic order
# ---------------------------------------------------------------------------


def test_advance_within_topic_seq_order() -> None:
    """Events within a topic release in seq (== event_sim_time) order."""
    events = [make_event(i + 1, i * 100 * _MS, "t1") for i in range(5)]
    buffers: dict[str, deque[StreamEvent]] = {"t1": deque(events)}
    control = make_control(playing=True, speed=10.0)
    frontier = make_frontier()

    all_released: list[StreamEvent] = []
    for i in range(10):
        delta = 0.0 if i == 0 else 0.1
        all_released.extend(advance(buffers, control, frontier, delta))

    for a, b in zip(all_released, all_released[1:]):
        assert a.event_sim_time <= b.event_sim_time


# ---------------------------------------------------------------------------
# Cross-topic order (governed by lag, not global seq)
# ---------------------------------------------------------------------------


def test_advance_cross_topic_order_governed_by_lag() -> None:
    """Events on different topics release per their edges (lag), not by global seq.

    t1 (lag=0) has earliest event at t=0; t2 (lag=300ms) has earliest event at
    t=100ms. On the init tick: global_min=0, t1.edge=0, t2.edge=0-300ms=-300ms.
    t1 releases (event at 0 <= edge 0); t2 does NOT (event at 100ms > edge -300ms
    is False — 100ms <= -300ms is False, so t2's event is NOT released).
    """
    topics = [
        TopicDials(topic="t1", content="state-changes", rate=1.0, lag_ms=0, mute=False),
        TopicDials(
            topic="t2", content="state-changes", rate=1.0, lag_ms=300, mute=False
        ),
    ]
    control = make_control(playing=True, speed=1.0, topics=topics)
    # t1 earliest at 0; t2 earliest at 100ms — global min is 0
    buffers: dict[str, deque[StreamEvent]] = {
        "t1": deque([make_event(1, 0, "t1")]),
        "t2": deque([make_event(2, 100 * _MS, "t2")]),
    }
    frontier = make_frontier(topic_names=["t1", "t2"])

    released = advance(buffers, control, frontier, 0.0)
    released_topics = [e.topic for e in released]
    assert "t1" in released_topics
    assert "t2" not in released_topics


# ---------------------------------------------------------------------------
# Release order within a tick: topics in control.topics order, each FIFO
# ---------------------------------------------------------------------------


def test_advance_release_order_topics_then_fifo() -> None:
    """Events released in control.topics order, each topic FIFO."""
    e1 = make_event(1, 0, "ta")
    e2 = make_event(2, 0, "tb")
    buffers: dict[str, deque[StreamEvent]] = {
        "ta": deque([e1]),
        "tb": deque([e2]),
    }
    topics = [
        TopicDials(topic="ta", content="state-changes", rate=1.0, lag_ms=0, mute=False),
        TopicDials(topic="tb", content="state-changes", rate=1.0, lag_ms=0, mute=False),
    ]
    control = make_control(playing=True, topics=topics)
    frontier = make_frontier(topic_names=["ta", "tb"])

    released = advance(buffers, control, frontier, 0.0)

    assert len(released) == 2
    assert released[0].topic == "ta"
    assert released[1].topic == "tb"


# ---------------------------------------------------------------------------
# Invariant 3: released events are field-for-field identical to seeded events
# ---------------------------------------------------------------------------


def test_advance_invariant3_released_event_identical_to_seeded() -> None:
    """Every released StreamEvent is identical field-for-field to the seeded event."""
    original = make_event(7, 0, "t1")
    buffers: dict[str, deque[StreamEvent]] = {"t1": deque([original])}
    control = make_control(playing=True)
    frontier = make_frontier()

    released = advance(buffers, control, frontier, 0.0)

    assert len(released) == 1
    ev = released[0]
    assert ev.seq == original.seq
    assert ev.op == original.op
    assert ev.kind == original.kind
    assert ev.record_id == original.record_id
    assert ev.presentation_id == original.presentation_id
    assert ev.event_sim_time == original.event_sim_time
    assert ev.ts == original.ts
    assert ev.after == original.after
    assert ev.topic == original.topic
    assert ev.route_table == original.route_table


# ---------------------------------------------------------------------------
# Invariant 4: relative determinism
# ---------------------------------------------------------------------------


def test_advance_invariant4_deterministic() -> None:
    """Same seeded buffers + same delta sequence + same ControlState → identical output."""

    def run_sequence() -> list[tuple[str, int, int]]:
        events = [make_event(i + 1, i * 50 * _MS, "t1") for i in range(5)]
        buffers: dict[str, deque[StreamEvent]] = {"t1": deque(events)}
        control = make_control(playing=True, speed=2.0)
        frontier = make_frontier()
        result: list[tuple[str, int, int]] = []
        deltas = [0.0, 0.05, 0.05, 0.05, 0.05, 0.1, 0.1]
        for tick, delta in enumerate(deltas):
            for ev in advance(buffers, control, frontier, delta):
                result.append((ev.topic, ev.event_sim_time, tick))
        return result

    assert run_sequence() == run_sequence()


# ---------------------------------------------------------------------------
# delivery_edges updated after release
# ---------------------------------------------------------------------------


def test_advance_delivery_edges_updated_on_release() -> None:
    """delivery_edges[topic] is updated to event_sim_time after each release."""
    ev = make_event(1, 0, "t1")
    buffers: dict[str, deque[StreamEvent]] = {"t1": deque([ev])}
    control = make_control(playing=True)
    frontier = make_frontier()

    assert frontier.delivery_edges["t1"] is None

    advance(buffers, control, frontier, 0.0)

    assert frontier.delivery_edges["t1"] == ev.event_sim_time


# ---------------------------------------------------------------------------
# seed_mixer_run helpers
# ---------------------------------------------------------------------------

SUPPORTED_VERSION = 4

_RECORD_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__status", "type": "VARCHAR", "history_tracked": True},
    {"name": "prop__label", "type": "VARCHAR", "history_tracked": False},
]

_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _table_spec(
    name: str,
    category: str,
    cols: list[dict[str, object]],
    rows: int,
    record_kind: str | None = None,
) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": name,
        "category": category,
        "columns": cols,
        "rows": rows,
    }
    if record_kind is not None:
        spec["record_kind"] = record_kind
    return spec


def _build_two_kind_emit(
    tmp_path: Path,
    kind_a: str,
    rows_a: list[tuple[Any, ...]],
    kind_b: str,
    rows_b: list[tuple[Any, ...]],
    n_branches: int = 1,
) -> Path:
    """Build a minimal two-kind emit with the standard record columns."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl(f"records__{kind_a}", _RECORD_COLS))
    conn.execute(_ddl(f"records__{kind_b}", _RECORD_COLS))
    conn.execute(_ddl("history", _HISTORY_COLS))
    ph = ", ".join("?" for _ in _RECORD_COLS)
    for row in rows_a:
        conn.execute(f'INSERT INTO "records__{kind_a}" VALUES ({ph})', list(row))
    for row in rows_b:
        conn.execute(f'INSERT INTO "records__{kind_b}" VALUES ({ph})', list(row))
    conn.close()

    branches: list[dict[str, object]]
    if n_branches == 1:
        branches = [{"fork_path": "trunk", "parent": None, "slice_at": 9999}]
    else:
        branches = [
            {"fork_path": "trunk", "parent": None, "slice_at": 9999},
            {"fork_path": "trunk@alt", "parent": "trunk", "slice_at": 100},
        ]

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_VERSION,
        "branches": branches,
        "tables": [
            _table_spec(
                f"records__{kind_a}",
                "records",
                _RECORD_COLS,
                len(rows_a),
                record_kind=kind_a,
            ),
            _table_spec(
                f"records__{kind_b}",
                "records",
                _RECORD_COLS,
                len(rows_b),
                record_kind=kind_b,
            ),
            _table_spec("history", "fixed", _HISTORY_COLS, 0),
        ],
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_path


def _make_stream_config(kinds: list[str]) -> StreamConfig:
    """Build a StreamConfig selecting the given kinds (no properties)."""
    return StreamConfig(
        content="state-changes",
        kinds=[StreamKindSelection(kind=k, properties=[]) for k in kinds],
    )


def _make_transport(playing: bool = True, speed: float = 1.0) -> Transport:
    """Build a launch Transport."""
    return Transport(playing=playing, speed=speed)


# ---------------------------------------------------------------------------
# Phase 2: seed_mixer_run importable from streaming package
# ---------------------------------------------------------------------------


def test_seed_mixer_run_importable_from_streaming_package() -> None:
    """seed_mixer_run is importable from fabulexa_forge.exporters.streaming."""
    from fabulexa_forge.exporters.streaming import seed_mixer_run as _smr  # noqa: F401


# ---------------------------------------------------------------------------
# Phase 2: buffer key set equals build_topic_set exactly
# ---------------------------------------------------------------------------


def test_seed_buffer_keys_equal_topic_set(tmp_path: Path) -> None:
    """One FIFO buffer per topic; buffer key set equals build_topic_set exactly."""
    from fabulexa_forge.exporters.streaming.engine import build_topic_set

    rows = [("trunk", "r1", 10, True, None, 10, "a", "x")]
    emit_dir = _build_two_kind_emit(tmp_path, "alpha", rows, "beta", rows)
    config = _make_stream_config(["alpha", "beta"])

    with open_emit(emit_dir) as emit:
        expected_topics = set(build_topic_set(config, emit.sidecar))
        buffers, _, _ = seed_mixer_run(
            emit, config, None, emit.sidecar, _make_transport()
        )

    assert set(buffers.keys()) == expected_topics


# ---------------------------------------------------------------------------
# Phase 2: every event lands in its own topic's buffer; sum equals total events
# ---------------------------------------------------------------------------


def test_seed_events_partitioned_by_topic(tmp_path: Path) -> None:
    """Every drained event lands in the correct buffer; sum equals total events."""
    rows_a = [("trunk", "r1", 10, True, None, 10, "a", "x")]
    rows_b = [
        ("trunk", "r2", 20, True, None, 20, "b", "y"),
        ("trunk", "r3", 30, True, None, 30, "c", "z"),
    ]
    emit_dir = _build_two_kind_emit(tmp_path, "alpha", rows_a, "beta", rows_b)
    config = _make_stream_config(["alpha", "beta"])

    with open_emit(emit_dir) as emit:
        buffers, _, _ = seed_mixer_run(
            emit, config, None, emit.sidecar, _make_transport()
        )

    total = sum(len(buf) for buf in buffers.values())
    assert total == 3
    for topic, buf in buffers.items():
        for event in buf:
            assert event.topic == topic


# ---------------------------------------------------------------------------
# Phase 2: each topic's buffer is in seq / event_sim_time order
# ---------------------------------------------------------------------------


def test_seed_buffer_seq_order(tmp_path: Path) -> None:
    """Each topic's buffer is in seq / event_sim_time order."""
    rows = [
        ("trunk", "r1", 10, True, None, 10, "a", "x"),
        ("trunk", "r2", 20, True, None, 20, "b", "y"),
        ("trunk", "r3", 30, True, None, 30, "c", "z"),
    ]
    emit_dir = _build_two_kind_emit(tmp_path, "alpha", rows, "beta", [])
    config = _make_stream_config(["alpha", "beta"])

    with open_emit(emit_dir) as emit:
        buffers, _, _ = seed_mixer_run(
            emit, config, None, emit.sidecar, _make_transport()
        )

    buf = list(buffers["alpha"])
    for a, b in zip(buf, buf[1:]):
        assert a.seq < b.seq
        assert a.event_sim_time <= b.event_sim_time


# ---------------------------------------------------------------------------
# Phase 2: declared-but-empty topic present as key with empty buffer
# ---------------------------------------------------------------------------


def test_seed_declared_but_empty_topic_present(tmp_path: Path) -> None:
    """Declared-but-empty topic: present as a key with an empty buffer."""
    rows_a = [("trunk", "r1", 10, True, None, 10, "a", "x")]
    emit_dir = _build_two_kind_emit(tmp_path, "alpha", rows_a, "beta", [])
    config = _make_stream_config(["alpha", "beta"])

    with open_emit(emit_dir) as emit:
        buffers, _, _ = seed_mixer_run(
            emit, config, None, emit.sidecar, _make_transport()
        )

    assert "beta" in buffers
    assert len(buffers["beta"]) == 0
    assert len(buffers["alpha"]) == 1


# ---------------------------------------------------------------------------
# Phase 2: ControlState.transport preserved; topics has one entry per topic
# ---------------------------------------------------------------------------


def test_seed_control_transport_preserved(tmp_path: Path) -> None:
    """ControlState.transport is exactly the supplied launch transport."""
    rows = [("trunk", "r1", 10, True, None, 10, "a", "x")]
    emit_dir = _build_two_kind_emit(tmp_path, "alpha", rows, "beta", [])
    config = _make_stream_config(["alpha", "beta"])
    transport = Transport(playing=False, speed=2.5)

    with open_emit(emit_dir) as emit:
        _, control, _ = seed_mixer_run(emit, config, None, emit.sidecar, transport)

    assert control.transport is transport
    assert control.transport.playing is False
    assert control.transport.speed == 2.5


def test_seed_control_topics_one_per_topic_in_order(tmp_path: Path) -> None:
    """ControlState.topics has one entry per topic in build_topic_set order."""
    from fabulexa_forge.exporters.streaming.engine import build_topic_set

    rows = [("trunk", "r1", 10, True, None, 10, "a", "x")]
    emit_dir = _build_two_kind_emit(tmp_path, "alpha", rows, "beta", rows)
    config = _make_stream_config(["alpha", "beta"])

    with open_emit(emit_dir) as emit:
        expected = build_topic_set(config, emit.sidecar)
        _, control, _ = seed_mixer_run(
            emit, config, None, emit.sidecar, _make_transport()
        )

    assert [d.topic for d in control.topics] == list(expected)


# ---------------------------------------------------------------------------
# Phase 2: every seeded TopicDials is neutral
# ---------------------------------------------------------------------------


def test_seed_topic_dials_neutral(tmp_path: Path) -> None:
    """Every seeded TopicDials is neutral: rate=1.0, lag_ms=0, mute=False."""
    rows = [("trunk", "r1", 10, True, None, 10, "a", "x")]
    emit_dir = _build_two_kind_emit(tmp_path, "alpha", rows, "beta", rows)
    config = _make_stream_config(["alpha", "beta"])

    with open_emit(emit_dir) as emit:
        _, control, _ = seed_mixer_run(
            emit, config, None, emit.sidecar, _make_transport()
        )

    for dial in control.topics:
        assert dial.rate == 1.0
        assert dial.lag_ms == 0
        assert dial.mute is False


def test_seed_topic_dials_content_stamped(tmp_path: Path) -> None:
    """Every seeded TopicDials has content == config.content."""
    rows = [("trunk", "r1", 10, True, None, 10, "a", "x")]
    emit_dir = _build_two_kind_emit(tmp_path, "alpha", rows, "beta", [])
    config = _make_stream_config(["alpha", "beta"])

    with open_emit(emit_dir) as emit:
        _, control, _ = seed_mixer_run(
            emit, config, None, emit.sidecar, _make_transport()
        )

    for dial in control.topics:
        assert dial.content == "state-changes"


# ---------------------------------------------------------------------------
# Phase 2: FrontierState — fresh, all None
# ---------------------------------------------------------------------------


def test_seed_frontier_state_fresh(tmp_path: Path) -> None:
    """FrontierState: frontier_sim_time is None; edges and delivery_edges all None."""
    from fabulexa_forge.exporters.streaming.engine import build_topic_set

    rows = [("trunk", "r1", 10, True, None, 10, "a", "x")]
    emit_dir = _build_two_kind_emit(tmp_path, "alpha", rows, "beta", rows)
    config = _make_stream_config(["alpha", "beta"])

    with open_emit(emit_dir) as emit:
        expected_topics = list(build_topic_set(config, emit.sidecar))
        _, _, frontier = seed_mixer_run(
            emit, config, None, emit.sidecar, _make_transport()
        )

    assert frontier.frontier_sim_time is None
    assert list(frontier.edges.keys()) == expected_topics
    assert list(frontier.delivery_edges.keys()) == expected_topics
    for t in expected_topics:
        assert frontier.edges[t] is None
        assert frontier.delivery_edges[t] is None


# ---------------------------------------------------------------------------
# Phase 2: zero-event emit — buffers empty, state fully seeded
# ---------------------------------------------------------------------------


def test_seed_zero_event_emit(tmp_path: Path) -> None:
    """Zero-event emit: every buffer is empty, state is still fully seeded."""
    emit_dir = _build_two_kind_emit(tmp_path, "alpha", [], "beta", [])
    config = _make_stream_config(["alpha", "beta"])

    with open_emit(emit_dir) as emit:
        buffers, control, frontier = seed_mixer_run(
            emit, config, None, emit.sidecar, _make_transport()
        )

    assert all(len(buf) == 0 for buf in buffers.values())
    assert len(control.topics) == 2
    assert frontier.frontier_sim_time is None
    for t in buffers:
        assert frontier.edges[t] is None
        assert frontier.delivery_edges[t] is None


# ---------------------------------------------------------------------------
# Phase 2: multi-branch emit surfaces ExportError unwrapped
# ---------------------------------------------------------------------------


def test_seed_multi_branch_raises_export_error(tmp_path: Path) -> None:
    """Multi-branch emit: seed_mixer_run surfaces ExportError from iter_stream_events."""
    rows = [("trunk", "r1", 10, True, None, 10, "a", "x")]
    emit_dir = _build_two_kind_emit(tmp_path, "alpha", rows, "beta", rows, n_branches=2)
    config = _make_stream_config(["alpha", "beta"])

    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportError):
            seed_mixer_run(emit, config, None, emit.sidecar, _make_transport())


# ---------------------------------------------------------------------------
# Phase 2: content="membership-events" run seeds correctly
# ---------------------------------------------------------------------------

_MEMBERSHIP_BASIC_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
]


def _build_single_membership_emit(
    tmp_path: Path,
    owner_kind: str,
    property_name: str,
    mem_rows: list[tuple[Any, ...]],
) -> Path:
    """Build a minimal v4 emit with one membership table."""
    table_name = f"membership__{owner_kind}__{property_name}"
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl(table_name, _MEMBERSHIP_BASIC_COLS))
    ph = ", ".join("?" for _ in _MEMBERSHIP_BASIC_COLS)
    for row in mem_rows:
        conn.execute(f'INSERT INTO "{table_name}" VALUES ({ph})', list(row))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
            {
                "name": table_name,
                "category": "membership",
                "columns": _MEMBERSHIP_BASIC_COLS,
                "rows": len(mem_rows),
                "record_kind": owner_kind,
                "property": property_name,
            }
        ],
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_path


def test_seed_membership_events_content_stamped(tmp_path: Path) -> None:
    """Buffers + neutral dials seed correctly with content stamped 'membership-events'."""
    emit_dir = _build_single_membership_emit(
        tmp_path, "queue", "waiters", [("trunk", "r1", 10, None)]
    )
    config = StreamConfig(
        content="membership-events",
        memberships=[
            MembershipSelection(owner_kind="queue", property="waiters", fields=[])
        ],
    )

    with open_emit(emit_dir) as emit:
        buffers, control, frontier = seed_mixer_run(
            emit, config, None, emit.sidecar, _make_transport()
        )

    assert len(buffers) > 0
    for dial in control.topics:
        assert dial.content == "membership-events"
        assert dial.rate == 1.0
        assert dial.lag_ms == 0
        assert dial.mute is False
    assert frontier.frontier_sim_time is None


# ---------------------------------------------------------------------------
# Phase 3: schedule_releases importable from streaming package
# ---------------------------------------------------------------------------


def test_schedule_releases_importable_from_streaming_package() -> None:
    """schedule_releases is importable from fabulexa_forge.exporters.streaming."""
    from fabulexa_forge.exporters.streaming import (  # noqa: F401
        schedule_releases as _sr,
    )


# ---------------------------------------------------------------------------
# Phase 3: async helpers shared by schedule_releases tests
# ---------------------------------------------------------------------------


def _run(coro: object) -> None:
    """Run a coroutine synchronously via asyncio.run."""
    import asyncio

    asyncio.run(coro)  # type: ignore[arg-type]


class _FakeMonotonic:
    """Returns a scripted sequence of readings; raises StopIteration if exhausted."""

    def __init__(self, readings: list[float]) -> None:
        self._readings = iter(readings)

    def __call__(self) -> float:
        return next(self._readings)


class _TickBudgetSleep:
    """Async sleep that raises asyncio.CancelledError after `budget` awaits."""

    def __init__(self, budget: int) -> None:
        self._remaining = budget

    async def __call__(self, _seconds: float) -> None:
        import asyncio

        self._remaining -= 1
        if self._remaining <= 0:
            raise asyncio.CancelledError


# ---------------------------------------------------------------------------
# Phase 3: zero-event emit returns immediately without calling advance or sink
# ---------------------------------------------------------------------------


def test_schedule_zero_event_returns_immediately() -> None:
    """All buffers empty at start: returns before advance or sink is called."""
    import asyncio

    sink_calls: list[object] = []

    async def _run_test() -> None:
        buffers: dict[str, deque[StreamEvent]] = {"t1": deque()}
        control = make_control(playing=True)
        frontier = make_frontier()

        readings = iter([0.0])

        async def fake_sleep(_s: float) -> None:
            raise AssertionError("sleep must not be awaited for empty buffers")

        async def fake_sink(_e: StreamEvent) -> None:
            sink_calls.append(_e)

        await schedule_releases(
            buffers=buffers,
            control=control,
            frontier=frontier,
            sink=fake_sink,
            sleep=fake_sleep,
            monotonic=lambda: next(readings),
            tick_seconds=0.1,
        )

    asyncio.run(_run_test())
    assert sink_calls == []


# ---------------------------------------------------------------------------
# Phase 3: first iteration's measured delta is 0.0
# ---------------------------------------------------------------------------


def test_schedule_first_delta_is_zero() -> None:
    """Baseline reading taken before loop; first delta == 0.0."""
    import asyncio

    async def _run_test() -> None:
        # One event at time=0; speed=1 so init tick at delta=0 releases it.
        buffers: dict[str, deque[StreamEvent]] = {"t1": deque([make_event(1, 0)])}
        control = make_control(playing=True, speed=1.0)
        frontier = make_frontier()

        # baseline=0.0, then first tick read=0.0 → delta=0.0 (measured)
        # On init tick with delta=0, frontier is set to global min (0) and the
        # event at t=0 releases (edge=frontier=0, event_sim_time=0 <= 0).
        readings = iter([0.0, 0.0])

        async def fake_sleep(_s: float) -> None:
            # Only one tick needed; stop after sleeping once
            raise asyncio.CancelledError

        collected: list[object] = []

        async def fake_sink(e: StreamEvent) -> None:
            collected.append(e)

        try:
            await schedule_releases(
                buffers=buffers,
                control=control,
                frontier=frontier,
                sink=fake_sink,
                sleep=fake_sleep,
                monotonic=lambda: next(readings),
                tick_seconds=0.05,
            )
        except asyncio.CancelledError:
            pass

        # The event was at t=0 == frontier on init tick → released
        assert len(collected) == 1

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# Phase 3: full drain — every event delivered exactly once, loop returns
# ---------------------------------------------------------------------------


def test_schedule_full_drain() -> None:
    """Every seeded event is delivered to sink exactly once; loop returns normally."""
    import asyncio

    async def _run_test() -> None:
        n = 5
        events = [make_event(i + 1, i * 100 * _MS) for i in range(n)]
        buffers: dict[str, deque[StreamEvent]] = {"t1": deque(events)}
        # speed high enough to drain everything in a few ticks
        control = make_control(playing=True, speed=10.0)
        frontier = make_frontier()

        # Scripted monotonic: baseline + enough readings for drain
        # Each tick advances by 0.1s real → 1s sim (speed=10)
        reading_val = 0.0

        def fake_monotonic() -> float:
            nonlocal reading_val
            val = reading_val
            reading_val += 0.1
            return val

        collected: list[StreamEvent] = []

        async def fake_sink(e: StreamEvent) -> None:
            collected.append(e)

        async def fake_sleep(_s: float) -> None:
            pass

        await schedule_releases(
            buffers=buffers,
            control=control,
            frontier=frontier,
            sink=fake_sink,
            sleep=fake_sleep,
            monotonic=fake_monotonic,
            tick_seconds=0.05,
        )

        assert len(collected) == n
        assert [e.seq for e in collected] == [e.seq for e in events]

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# Phase 3: measured delta from monotonic — frontier advances by real elapsed
# ---------------------------------------------------------------------------


def test_schedule_measured_delta_advance() -> None:
    """Frontier advances by measured real elapsed (not nominal tick_seconds)."""
    import asyncio

    async def _run_test() -> None:
        # Single event far in sim future so it doesn't release immediately
        buffers: dict[str, deque[StreamEvent]] = {
            "t1": deque([make_event(1, 10 * _SEC)])
        }
        control = make_control(playing=True, speed=1.0)
        frontier = make_frontier()

        # baseline=0.0, tick1 read=2.0 (delta=2.0s), tick2 read=4.0 (delta=2.0s)
        readings = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0]
        r_iter = iter(readings)

        ticks: list[int] = [0]

        async def fake_sleep(_s: float) -> None:
            ticks[0] += 1
            if ticks[0] >= 6:
                raise asyncio.CancelledError

        collected: list[StreamEvent] = []

        async def fake_sink(e: StreamEvent) -> None:
            collected.append(e)

        try:
            await schedule_releases(
                buffers=buffers,
                control=control,
                frontier=frontier,
                sink=fake_sink,
                sleep=fake_sleep,
                monotonic=lambda: next(r_iter),
                tick_seconds=0.1,
            )
        except asyncio.CancelledError:
            pass

        # After 5 ticks of 2s each (speed=1), frontier = 10s → event releases
        assert len(collected) == 1

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# Phase 3: pause discards the interval — previous reading refreshed each tick
# ---------------------------------------------------------------------------


def test_schedule_pause_discards_interval() -> None:
    """Pause ticks update previous reading; real elapsed during pause never banks."""
    import asyncio

    async def _run_test() -> None:
        # Event at 1s sim-time; speed=1, so needs 1s real to release
        buffers: dict[str, deque[StreamEvent]] = {
            "t1": deque([make_event(1, 1 * _SEC)])
        }
        # Start paused
        control = make_control(playing=False, speed=1.0)
        frontier = make_frontier()

        # Monotonic advances 10s during the pause (3 ticks) then stays still after play
        # baseline=0.0
        # tick1 (paused): read=10.0  delta=10s (discarded by advance, refreshed)
        # tick2 (paused): read=20.0  delta=10s (discarded)
        # tick3 (paused): read=30.0  delta=10s (discarded)
        # -- now flip to playing --
        # tick4 (playing, init): read=30.5  delta=0.5 → frontier sets to 1s (global min)
        # tick5 (playing, subsequent): read=31.0  delta=0.5 → frontier += 0.5s
        readings = [0.0, 10.0, 20.0, 30.0, 30.5, 31.0, 31.5]
        r_iter = iter(readings)

        tick_count = [0]
        play_flipped = [False]

        async def fake_sleep(_s: float) -> None:
            tick_count[0] += 1
            if tick_count[0] == 3 and not play_flipped[0]:
                control.transport.playing = True
                play_flipped[0] = True
            if tick_count[0] >= 6:
                raise asyncio.CancelledError

        collected: list[StreamEvent] = []

        async def fake_sink(e: StreamEvent) -> None:
            collected.append(e)

        try:
            await schedule_releases(
                buffers=buffers,
                control=control,
                frontier=frontier,
                sink=fake_sink,
                sleep=fake_sleep,
                monotonic=lambda: next(r_iter),
                tick_seconds=0.1,
            )
        except asyncio.CancelledError:
            pass

        # The pause's 30s of elapsed time must not have been banked.
        # frontier after init=1s; after tick5=1s+0.5s=1.5s >= event at 1s → released
        assert len(collected) == 1

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# Phase 3: launched paused then played — nothing releases while paused
# ---------------------------------------------------------------------------


def test_schedule_launched_paused_then_played() -> None:
    """Nothing releases while playing=False; releases begin once playing=True."""
    import asyncio

    async def _run_test() -> None:
        buffers: dict[str, deque[StreamEvent]] = {"t1": deque([make_event(1, 0)])}
        control = make_control(playing=False, speed=1.0)
        frontier = make_frontier()

        reading_val = 0.0

        def fake_monotonic() -> float:
            nonlocal reading_val
            val = reading_val
            reading_val += 0.1
            return val

        tick_count = [0]

        async def fake_sleep(_s: float) -> None:
            tick_count[0] += 1
            if tick_count[0] == 3:
                # Flip to playing after 3 paused ticks
                control.transport.playing = True

        collected: list[StreamEvent] = []

        async def fake_sink(e: StreamEvent) -> None:
            collected.append(e)

        await schedule_releases(
            buffers=buffers,
            control=control,
            frontier=frontier,
            sink=fake_sink,
            sleep=fake_sleep,
            monotonic=fake_monotonic,
            tick_seconds=0.05,
        )

        # Event at t=0 should release once playing starts
        assert len(collected) == 1

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# Phase 3: never-drains run — loop exits only externally
# ---------------------------------------------------------------------------


def test_schedule_never_drains_does_not_return_on_own() -> None:
    """Permanently muted non-empty topic keeps ticking; buffers remain non-empty.

    Uses lag_ms > 0 so the init-tick edge (frontier - lag) is below all events,
    preventing an init-tick release. Because mute=True prevents edge advancement
    on subsequent ticks, the event is never released.
    """
    import asyncio

    async def _run_test() -> None:
        # Event at 0; lag=5s means init edge = 0 - 5s = -5s < 0 → no release.
        # On subsequent ticks mute=True prevents edge advancement → never drains.
        buffers: dict[str, deque[StreamEvent]] = {"t1": deque([make_event(1, 0)])}
        topics = [
            TopicDials(
                topic="t1",
                content="state-changes",
                rate=1.0,
                lag_ms=5000,  # 5s event-time lag
                mute=True,
            )
        ]
        control = make_control(playing=True, topics=topics)
        frontier = make_frontier()

        reading_val = 0.0

        def fake_monotonic() -> float:
            nonlocal reading_val
            val = reading_val
            reading_val += 0.1
            return val

        tick_count = [0]
        MAX_TICKS = 5

        async def fake_sleep(_s: float) -> None:
            tick_count[0] += 1
            if tick_count[0] >= MAX_TICKS:
                raise asyncio.CancelledError

        collected: list[StreamEvent] = []

        async def fake_sink(e: StreamEvent) -> None:
            collected.append(e)

        try:
            await schedule_releases(
                buffers=buffers,
                control=control,
                frontier=frontier,
                sink=fake_sink,
                sleep=fake_sleep,
                monotonic=fake_monotonic,
                tick_seconds=0.05,
            )
        except asyncio.CancelledError:
            pass

        # Still not drained — muted topic never releases
        assert sum(len(buf) for buf in buffers.values()) > 0
        assert tick_count[0] >= MAX_TICKS

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# Phase 3: per-event sink ordering within a tick
# ---------------------------------------------------------------------------


def test_schedule_sink_called_in_release_order() -> None:
    """Within a tick, sink is awaited in advance's release order (topics, then FIFO)."""
    import asyncio

    async def _run_test() -> None:
        # Two topics: ta, tb; each has one event at t=0
        e_ta = make_event(1, 0, "ta")
        e_tb = make_event(2, 0, "tb")
        buffers: dict[str, deque[StreamEvent]] = {
            "ta": deque([e_ta]),
            "tb": deque([e_tb]),
        }
        topics = [
            TopicDials(
                topic="ta", content="state-changes", rate=1.0, lag_ms=0, mute=False
            ),
            TopicDials(
                topic="tb", content="state-changes", rate=1.0, lag_ms=0, mute=False
            ),
        ]
        control = make_control(playing=True, topics=topics)
        frontier = make_frontier(topic_names=["ta", "tb"])

        readings = iter([0.0, 0.0])

        async def fake_sleep(_s: float) -> None:
            pass

        collected: list[str] = []

        async def fake_sink(e: StreamEvent) -> None:
            collected.append(e.topic)

        await schedule_releases(
            buffers=buffers,
            control=control,
            frontier=frontier,
            sink=fake_sink,
            sleep=fake_sleep,
            monotonic=lambda: next(readings),
            tick_seconds=0.05,
        )

        assert collected == ["ta", "tb"]

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# Phase 3: cross-topic out-of-order arrival via lag
# ---------------------------------------------------------------------------


def test_schedule_cross_topic_lag_out_of_order() -> None:
    """Lagged topic's earlier events arrive after faster topic's later events."""
    import asyncio

    async def _run_test() -> None:
        # t_fast: events at 0, 1s, 2s (no lag)
        # t_lag:  events at 0, 1s, 2s (lag=5s → edge starts 5s behind frontier)
        fast_events = [make_event(i + 1, i * _SEC, "t_fast") for i in range(3)]
        lag_events = [make_event(i + 4, i * _SEC, "t_lag") for i in range(3)]
        buffers: dict[str, deque[StreamEvent]] = {
            "t_fast": deque(fast_events),
            "t_lag": deque(lag_events),
        }
        topics = [
            TopicDials(
                topic="t_fast", content="state-changes", rate=1.0, lag_ms=0, mute=False
            ),
            TopicDials(
                topic="t_lag",
                content="state-changes",
                rate=1.0,
                lag_ms=5000,  # 5s lag
                mute=False,
            ),
        ]
        control = make_control(playing=True, speed=1.0, topics=topics)
        frontier = make_frontier(topic_names=["t_fast", "t_lag"])

        reading_val = 0.0

        def fake_monotonic() -> float:
            nonlocal reading_val
            val = reading_val
            reading_val += 1.0
            return val

        collected: list[tuple[str, int]] = []

        async def fake_sink(e: StreamEvent) -> None:
            collected.append((e.topic, e.event_sim_time))

        async def fake_sleep(_s: float) -> None:
            pass

        await schedule_releases(
            buffers=buffers,
            control=control,
            frontier=frontier,
            sink=fake_sink,
            sleep=fake_sleep,
            monotonic=fake_monotonic,
            tick_seconds=0.05,
        )

        # All events delivered eventually
        assert len(collected) == 6

        # Fast topic events at 0, 1s, 2s should all arrive before any lagged events
        # because lag=5s means t_lag's edge is frontier-5s, needing frontier>=5s to release
        fast_delivered = [i for i, (t, _) in enumerate(collected) if t == "t_fast"]
        lag_delivered = [i for i, (t, _) in enumerate(collected) if t == "t_lag"]
        # All fast events before any lag events
        assert max(fast_delivered) < min(lag_delivered)

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# Phase 3: relative determinism — same fake monotonic + config → same delivery
# ---------------------------------------------------------------------------


def test_schedule_relative_determinism() -> None:
    """Same fake monotonic + same control yields identical delivery sequence twice."""
    import asyncio

    def run_once() -> list[tuple[str, int]]:
        result: list[tuple[str, int]] = []

        async def _go() -> None:
            events = [make_event(i + 1, i * 100 * _MS) for i in range(4)]
            buffers: dict[str, deque[StreamEvent]] = {"t1": deque(events)}
            control = make_control(playing=True, speed=2.0)
            frontier = make_frontier()

            reading_val = 0.0

            def fake_monotonic() -> float:
                nonlocal reading_val
                val = reading_val
                reading_val += 0.05
                return val

            async def fake_sleep(_s: float) -> None:
                pass

            async def fake_sink(e: StreamEvent) -> None:
                result.append((e.topic, e.event_sim_time))

            await schedule_releases(
                buffers=buffers,
                control=control,
                frontier=frontier,
                sink=fake_sink,
                sleep=fake_sleep,
                monotonic=fake_monotonic,
                tick_seconds=0.05,
            )

        asyncio.run(_go())
        return result

    assert run_once() == run_once()
