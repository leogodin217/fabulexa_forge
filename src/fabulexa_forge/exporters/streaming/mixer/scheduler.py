"""Mixer scheduler: runtime types, seed, pure advance, and async schedule_releases.

Provides the four mutable runtime dataclasses (Transport, TopicDials,
ControlState, FrontierState), the seed entry point (seed_mixer_run), the
pure synchronous per-tick advance (advance), and the async driver loop
(schedule_releases).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Literal

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import StreamConfig
    from fabulexa_forge.exporters.streaming.types import StreamEvent
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar


@dataclass
class Transport:
    """The master transport section of ControlState — mutable; the API writes it.

    Mirrors the control-api Transport wire shape. playing gates whether the
    frontier advances; speed is the event-time advance per unit real time.
    """

    playing: bool
    """Whether the master frontier advances through event-time."""
    speed: float
    """Event-time advance per unit real time. Bounds 0.1 <= speed <= 1000 are enforced
    at the wire (doc 2); the scheduler assumes a value in range."""


@dataclass
class TopicDials:
    """One channel strip's operator controls plus its read-only identity — mutable.

    Mirrors the control-api TopicDials wire shape. topic and content are identity
    and presentation (read-only by convention); rate, lag_ms, and mute are the
    operator controls the API mutates and the scheduler reads each tick.
    """

    topic: str
    """The routing topic; the dial key and buffer key. Read-only identity."""
    content: Literal["state-changes", "membership-events"]
    """The content axis feeding this topic — equal to the run's StreamConfig.content for
    every topic in the POC. Read-only presentation."""
    rate: float
    """Per-stream release-rate multiplier. Bounds 0.0 <= rate <= 4.0 enforced at the
    wire. >1 only drains backlog (the edge is capped at frontier - lag)."""
    lag_ms: int
    """Delivery lag for this stream in EVENT-TIME milliseconds, subtracted from the
    frontier. Bounds 0 <= lag_ms <= 300000 enforced at the wire."""
    mute: bool
    """When True, the stream releases nothing; backlog accumulates and drains on
    un-mute / speed-up."""


@dataclass
class ControlState:
    """The full mutable operator state — the object the API reads and writes.

    Seeded by seed_mixer_run with the launch transport and one neutral TopicDials per
    topic in build_topic_set order. Read (as a snapshot) by advance each tick; mutated
    by the control plane (doc 2). No lock is required — one asyncio loop owns it.
    """

    transport: Transport
    """The master section."""
    topics: list[TopicDials]
    """One entry per routed topic, in stable display order (build_topic_set order)."""


@dataclass
class FrontierState:
    """The scheduler's evolving release position — mutated in place by advance.

    Separated from ControlState so the operator state (dials) and the derived schedule
    state (frontier, edges, delivery edges) have distinct owners: the API mutates
    ControlState; only advance mutates FrontierState.
    """

    frontier_sim_time: int | None
    """The master frontier in event-time nanoseconds, or None before the first
    advance observed under play."""
    edges: dict[str, int | None]
    """Per-topic release edge in event-time nanoseconds. A key is present for every
    topic from seed, valued None until the initialization tick sets it to
    frontier - lag_T; an int thereafter, monotonic non-decreasing per topic."""
    delivery_edges: dict[str, int | None]
    """Per-topic event_sim_time of the last released event. A key is present for
    every topic from seed, valued None before that topic's first release. The
    read-only quantity doc 3 renders as delivery_edge_sim_time; every topic is
    therefore indexable from seed, including while launched-paused before the
    initialization tick."""


def seed_mixer_run(
    emit: "Emit",
    config: "StreamConfig",
    anchor: "EffectiveAnchor | None",
    sidecar: "Sidecar",
    transport: Transport,
) -> "tuple[dict[str, deque[StreamEvent]], ControlState, FrontierState]":
    """Drain the engine once into per-topic buffers and build the initial mixer state.

    Enumerates the topic set with build_topic_set(config, sidecar) and creates an
    empty FIFO buffer for every topic in it — declared-but-empty topics included. Drains
    iter_stream_events(emit, config, anchor) exactly once, appending each event to the
    buffer for its `topic` (events arrive in global seq order, so each topic's buffer is
    in seq / event_sim_time order). Builds a ControlState carrying the supplied launch
    `transport` and one neutral TopicDials (rate=1.0, lag_ms=0, mute=False) per topic in
    build_topic_set order, each stamped with the run's content. Builds a fresh
    FrontierState — `frontier_sim_time` None, and `edges` / `delivery_edges` each
    carrying a key for **every** topic in build_topic_set order, all valued None
    (no edge is initialized and nothing is released until the first play tick).

    Whole-emit in-memory; appropriate at sanitized-fixture scale. Bounded buffering is
    deferred.

    Args:
        emit: The open emit (reader + connection).
        config: The validated streaming configuration (one content axis).
        anchor: The resolved effective anchor, or None.
        sidecar: The open emit's sidecar view — the source of each selected kind's
            declared sub-type set via ``subtype_values``.
        transport: The launch transport (playing, speed) from the mixer-verb flags.

    Returns:
        A triple of (per-topic FIFO buffers keyed by topic, the seeded ControlState,
        a fresh FrontierState).

    Raises:
        ExportError: The engine's eager validation pass failed — more than one branch
            (single-branch guard), an unresolvable kind / membership table / property /
            field, or a routing business rule. Raised by iter_stream_events at drain
            time; seed_mixer_run does not wrap or reinterpret it.
    """
    from fabulexa_forge.exporters.streaming.engine import (
        build_topic_set,
        iter_stream_events,
    )

    topics_ordered: tuple[str, ...] = build_topic_set(config, sidecar)

    buffers: dict[str, deque[StreamEvent]] = {t: deque() for t in topics_ordered}

    for event in iter_stream_events(emit, config, anchor):
        buffers[event.topic].append(event)

    content = config.content
    topic_dials = [
        TopicDials(
            topic=t,
            content=content,
            rate=1.0,
            lag_ms=0,
            mute=False,
        )
        for t in topics_ordered
    ]
    control = ControlState(transport=transport, topics=topic_dials)

    frontier = FrontierState(
        frontier_sim_time=None,
        edges={t: None for t in topics_ordered},
        delivery_edges={t: None for t in topics_ordered},
    )

    return buffers, control, frontier


def advance(
    buffers: "dict[str, deque[StreamEvent]]",
    control: ControlState,
    frontier: FrontierState,
    delta_real_seconds: float,
) -> "list[StreamEvent]":
    """Advance the frontier and per-topic edges by one tick; return released events.

    Deterministic and synchronous: no clock, no sleep, no I/O. "Pure" here is
    referential transparency of the return value (invariant 4), not freedom from
    side effects: it reads a snapshot of control, mutates frontier, and pops from
    buffers, then returns the events released this tick in deterministic order
    (topics in control.topics order, each topic FIFO).

    The initialization branch is taken iff frontier.frontier_sim_time is None and
    control.transport.playing is True. On that initialization tick: if every buffer is
    empty, the frontier is left uninitialized (frontier_sim_time stays None) and the
    returned list is empty. Otherwise it initializes frontier.frontier_sim_time to the
    global minimum event_sim_time across all non-empty buffers and each edge to
    (frontier - lag_T_ns), applying no frontier/edge advance on that initialization tick
    — the release step below still runs. On later playing ticks, advances the frontier
    by int(control.transport.speed * delta_real_seconds * 1e9) and each non-muted
    topic's edge by int(rate_T * delta_frontier), each edge clamped to
    [previous edge, frontier - lag_T_ns]. When playing is False, the frontier and edges
    hold and the returned list is empty. Releases, per topic, every head event with
    event_sim_time <= edge_T, updating that topic's delivery edge.

    Assumes every dial is within its documented bounds (the wire layer enforces them)
    and that within a topic event_sim_time is non-decreasing (the engine guarantee).

    Args:
        buffers: Per-topic FIFO buffers; mutated (released events are popped).
        control: The current operator state snapshot (read only).
        frontier: The evolving frontier / edge / delivery-edge state; mutated in place.
        delta_real_seconds: Measured real seconds elapsed since the previous tick;
            0.0 on the first loop iteration. The initialization tick discards the
            delta regardless, so a launched-paused start — whose initialization tick
            is a later iteration carrying a non-zero delta — is unaffected.

    Returns:
        The events released on this tick, in release order. Empty when paused, when no
        edge advanced past a buffered event, or when all buffers are empty. A zero
        delta_real_seconds on a playing, non-initialization tick gives delta_frontier=0,
        so the frontier and every edge hold and nothing new releases.
    """
    released: list[StreamEvent] = []

    if not control.transport.playing:
        return released

    # Initialization branch: frontier_sim_time is None and playing is True
    if frontier.frontier_sim_time is None:
        # Find global minimum event_sim_time across all non-empty buffers
        global_min: int | None = None
        for buf in buffers.values():
            if buf:
                head_time = buf[0].event_sim_time
                if global_min is None or head_time < global_min:
                    global_min = head_time

        if global_min is None:
            # All buffers empty; leave frontier uninitialized
            return released

        # Set frontier and each edge; no advance applied on initialization tick
        frontier.frontier_sim_time = global_min
        for dial in control.topics:
            lag_ns = dial.lag_ms * 1_000_000
            frontier.edges[dial.topic] = frontier.frontier_sim_time - lag_ns

    else:
        # Subsequent playing tick: advance frontier and edges
        delta_frontier = int(control.transport.speed * delta_real_seconds * 1e9)
        frontier.frontier_sim_time += delta_frontier

        for dial in control.topics:
            if dial.mute:
                continue
            lag_ns = dial.lag_ms * 1_000_000
            ceiling = frontier.frontier_sim_time - lag_ns
            prev_edge = frontier.edges[dial.topic]
            if prev_edge is None:
                # Edge was never set (this topic had no init-tick edge set somehow)
                new_edge = ceiling
            else:
                edge_advance = int(dial.rate * delta_frontier)
                new_edge = max(prev_edge, min(prev_edge + edge_advance, ceiling))
            frontier.edges[dial.topic] = new_edge

    # Release step: for each topic in control.topics order, pop head events <= edge
    for dial in control.topics:
        edge = frontier.edges[dial.topic]
        if edge is None:
            continue
        buf = buffers[dial.topic]
        while buf and buf[0].event_sim_time <= edge:
            event = buf.popleft()
            frontier.delivery_edges[dial.topic] = event.event_sim_time
            released.append(event)

    return released


async def schedule_releases(
    buffers: "dict[str, deque[StreamEvent]]",
    control: ControlState,
    frontier: FrontierState,
    sink: "Callable[[StreamEvent], Awaitable[None]]",
    sleep: Callable[[float], Awaitable[None]],
    monotonic: Callable[[], float],
    tick_seconds: float,
) -> None:
    """Drive the release loop until all buffers are empty, confining real time.

    The thin async shell over advance. Before the first tick it takes a baseline
    `monotonic` reading, so the first loop iteration's measured delta is 0.0
    (matching advance's delta_real_seconds contract). Each tick begins with the
    termination check: if every buffer is empty, return immediately — this check
    sits at the top of the loop, before the `monotonic` read and the advance call,
    so a zero-event emit returns before advance is ever invoked and advance is
    never called on all-empty buffers. Otherwise: read `monotonic`, compute the
    measured delta since the previous reading, then store this reading as the new
    previous reading on every tick — including paused ticks, where advance discards
    the delta — so a pause's real duration is never folded into the first post-pause
    playing tick. Then call advance, await `sink` once per released event in release
    order, then await `sleep(tick_seconds)`.

    Draining every buffer is the only internal exit: the coroutine carries no stop
    flag, so any run that never drains — a permanently muted non-empty topic, or a
    launch left paused — runs until the caller cancels the task.

    Args:
        buffers: Per-topic FIFO buffers (drained as the loop runs).
        control: The mutable operator state, re-read every tick.
        frontier: The evolving frontier / edge state, advanced every tick.
        sink: Async per-event delivery callable; awaited once per released event.
        sleep: Async sleep of N real seconds; asyncio.sleep in production, a fake
            in tests.
        monotonic: Monotonic real-clock reading in seconds; time.monotonic in
            production, a fake in tests.
        tick_seconds: The release-loop tick quantum in real seconds.

    Returns:
        None, when all buffers are empty. A run that never drains (permanent mute /
        launched paused) does not return on its own.
    """
    previous = monotonic()

    while True:
        # Termination check: all buffers empty → return before any advance call
        if all(len(buf) == 0 for buf in buffers.values()):
            return

        now = monotonic()
        delta = now - previous
        previous = now

        released = advance(buffers, control, frontier, delta)

        for event in released:
            await sink(event)

        await sleep(tick_seconds)
