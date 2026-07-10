"""Mixer consumer instrument: runtime types, seed, pure ingest fold, and async loop.

Provides the consumer-side mirror of the producer scheduler grain:
nine runtime dataclasses (WindowSpec, JoinSpec, IngestedRecord, ConsumerDials,
ConsumerControlState, ConsumerJobShape, ConsumerState, ConsumerRunState,
ConsumerLaunch), seed_consumer_run, the pure deterministic ingest fold, and
the async run_consumer shell that drives the ingestion loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from fabulexa_forge.errors import ExportError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fabulexa_forge.exporters.streaming.mixer.source import KafkaSource


@dataclass(frozen=True)
class WindowSpec:
    """A tumbling window on the global pipeline watermark.

    Job shape: immutable for the run.
    """

    size_ms: int
    """Tumbling window size in event-time milliseconds. Must be > 0."""


@dataclass(frozen=True)
class JoinSpec:
    """A declared fact/dimension enrichment pairing.

    A timing dependency, not a key join. A fact resolves to null when the dimension
    topic's watermark has not reached the fact's event-time. No key matching.
    """

    fact_topic: str
    dimension_topic: str


@dataclass(frozen=True)
class IngestedRecord:
    """One record's timing metadata — never its payload.

    Built from a Kafka message's .topic()/.timestamp()/.offset() only; .key() and
    .value() are never read (the no-payload-inspection invariant).
    """

    topic: str
    event_time_ms: int
    """Event-time as epoch milliseconds — the producer's CreateTime."""
    offset: int


@dataclass
class ConsumerDials:
    """One channel strip's consumer control plus read-only identity — mutable.

    Mirrors the control-api ConsumerTopicDials wire shape. The API mutates ingest_rate;
    topic/content are read-only identity.
    """

    topic: str
    content: Literal["state-changes", "membership-events"]
    ingest_rate: float
    """Messages/sec pulled for this topic. 0.0 pauses. Bounds 0.0..10000.0 enforced at
    the wire; the loop assumes a value in range."""


@dataclass
class ConsumerControlState:
    """The mutable operator state for the consumer — written by the API, read by
    run_consumer each tick. One asyncio loop owns it; no lock.
    """

    topics: list[ConsumerDials]


@dataclass(frozen=True)
class ConsumerJobShape:
    """Immutable launch-declared job shape and the global-watermark gating set.

    gating_topics is the set of data-bearing (non-empty) topics, the domain of the
    global-watermark min; declared-but-empty topics are excluded.
    """

    windows: tuple[WindowSpec, ...]
    joins: tuple[JoinSpec, ...]
    gating_topics: tuple[str, ...]


@dataclass
class ConsumerState:
    """The consumer's evolving derived timing state — mutated in place by ingest.

    Separated from ConsumerControlState exactly as FrontierState is from ControlState:
    the API mutates the dials; only ingest mutates this.
    """

    watermark_ms: dict[str, int | None]
    """Per-topic watermark (max ingested event-time epoch-ms); None before first ingest.
    A key for every topic from seed."""
    consumer_lag: dict[str, int]
    """Per-topic real broker backlog; a key for every topic from seed (0 until read)."""
    window_fired_count: list[int]
    """Parallel to shape.windows: windows fired so far (monotonic)."""
    window_latest_end_ms: list[int | None]
    """Parallel to shape.windows: most recent fired window end; None before first
    firing."""
    join_fact_count: list[int]
    """Parallel to shape.joins: fact records ingested."""
    join_null_count: list[int]
    """Parallel to shape.joins: facts whose dimension watermark had not caught up."""
    window_origin_ms: int | None = None
    """The global watermark's first non-None value — the tumbling-window epoch origin.
    Set on the first tick where global_wm becomes non-None; None until then.
    This is an implementation detail: the origin anchors all window boundaries."""


@dataclass
class ConsumerRunState:
    """The seed-time consumer bundle hung off MixerRunState.consumer.

    Present iff the run was launched with --consumer; None otherwise.
    """

    control: ConsumerControlState
    state: ConsumerState
    shape: ConsumerJobShape


@dataclass(frozen=True)
class ConsumerLaunch:
    """The async-phase consumer launch parameters (broker positioning).

    Non-None iff MixerRunState.consumer is non-None; passed to serve_mixer.
    """

    group_id: str
    offset_reset: Literal["earliest", "latest"]


def seed_consumer_run(
    topic_set: tuple[str, ...],
    content: Literal["state-changes", "membership-events"],
    nonempty_topics: tuple[str, ...],
    windows: tuple[WindowSpec, ...],
    joins: tuple[JoinSpec, ...],
) -> ConsumerRunState:
    """Build the initial consumer control + timing state from the topic set and shape.

    Creates one neutral ConsumerDials (ingest_rate=1.0) per topic in topic_set order, a
    ConsumerState with a None watermark / 0 lag per topic and zeroed window/join
    counters, and a ConsumerJobShape carrying the windows, joins, and gating set
    (= nonempty_topics).

    Args:
        topic_set: The routed topic set (build_topic_set order) the consumer
            subscribes to.
        content: The run's single content axis, stamped on each dial.
        nonempty_topics: Topics with at least one event this emit; the global-watermark
            gating domain.
        windows: Launch-declared tumbling windows.
        joins: Launch-declared fact/dimension pairings.

    Returns:
        A ConsumerRunState ready to hang off MixerRunState.consumer.

    Raises:
        ExportError: a JoinSpec references a topic absent from topic_set,
            or a WindowSpec has size_ms <= 0.
    """
    topic_set_set = set(topic_set)

    for window in windows:
        if window.size_ms <= 0:
            raise ExportError(f"WindowSpec.size_ms must be > 0; got {window.size_ms}")

    for join in joins:
        if join.fact_topic not in topic_set_set:
            raise ExportError(
                f"JoinSpec.fact_topic {join.fact_topic!r} is not in topic_set"
            )
        if join.dimension_topic not in topic_set_set:
            raise ExportError(
                f"JoinSpec.dimension_topic {join.dimension_topic!r} is not in topic_set"
            )

    dials = [
        ConsumerDials(topic=t, content=content, ingest_rate=1.0) for t in topic_set
    ]
    control = ConsumerControlState(topics=dials)

    state = ConsumerState(
        watermark_ms={t: None for t in topic_set},
        consumer_lag={t: 0 for t in topic_set},
        window_fired_count=[0] * len(windows),
        window_latest_end_ms=[None] * len(windows),
        join_fact_count=[0] * len(joins),
        join_null_count=[0] * len(joins),
    )

    shape = ConsumerJobShape(
        windows=windows,
        joins=joins,
        gating_topics=nonempty_topics,
    )

    return ConsumerRunState(control=control, state=state, shape=shape)


def _compute_global_watermark(
    watermark_ms: dict[str, int | None],
    gating_topics: tuple[str, ...],
) -> int | None:
    """Return the global pipeline watermark: min across gating topics' watermarks.

    Returns None if any gating topic's watermark is None or there are no gating topics.
    """
    if not gating_topics:
        return None
    values: list[int] = []
    for topic in gating_topics:
        w = watermark_ms.get(topic)
        if w is None:
            return None
        values.append(w)
    return min(values)


def _advance_windows(
    state: ConsumerState,
    shape: ConsumerJobShape,
    global_wm: int | None,
) -> None:
    """Fire tumbling windows whose end falls at or before the global watermark.

    Window origin is the global watermark's first non-None value
    (state.window_origin_ms). All windows share a single origin. Each window tracks
    its own fired count and latest end independently. A window fires when
    window_end <= global_wm.
    """
    if global_wm is None:
        return

    # Establish origin on first non-None global watermark
    if state.window_origin_ms is None:
        state.window_origin_ms = global_wm

    origin = state.window_origin_ms

    for i, window in enumerate(shape.windows):
        latest_end = state.window_latest_end_ms[i]

        if latest_end is None:
            # No window has fired yet; start from origin
            window_end = origin + window.size_ms
        else:
            # Continue from the last fired window end
            window_end = latest_end + window.size_ms

        fired = 0
        while window_end <= global_wm:
            fired += 1
            latest_end = window_end
            window_end += window.size_ms

        if fired:
            state.window_fired_count[i] += fired
            state.window_latest_end_ms[i] = latest_end


def _process_joins(
    state: ConsumerState,
    shape: ConsumerJobShape,
    pulled: dict[str, list[IngestedRecord]],
) -> None:
    """Update join counters from this tick's pulled fact records.

    For each fact record, increments fact_count. Increments null_count when the
    dimension topic's end-of-tick watermark is None or < the fact's event_time_ms.
    """
    for i, join in enumerate(shape.joins):
        fact_records = pulled.get(join.fact_topic, [])
        dim_wm = state.watermark_ms.get(join.dimension_topic)

        for record in fact_records:
            state.join_fact_count[i] += 1
            if dim_wm is None or dim_wm < record.event_time_ms:
                state.join_null_count[i] += 1


def ingest(
    control: ConsumerControlState,
    state: ConsumerState,
    shape: ConsumerJobShape,
    pulled: dict[str, list[IngestedRecord]],
    lag: dict[str, int],
) -> None:
    """Fold one tick of pulled records into the consumer timing state — deterministic.

    No clock, no I/O. Updates each topic's watermark to the max event-time among its
    pulled records (per-topic order trusted); overwrites consumer_lag from `lag`;
    recomputes the global watermark = min over shape.gating_topics' watermarks (None if
    any is None); advances each window's fired_count / latest_end while
    window_end <= the global watermark; and for each fact record pulled this tick
    increments the matching join's fact_count and (when the dimension topic's
    end-of-tick watermark is None or < the fact event-time) its null_count.

    Args:
        control: Operator dials (read only; identity / order).
        state: The evolving timing state; mutated in place.
        shape: Immutable windows / joins / gating set.
        pulled: Per-topic records ingested this tick (already throttled by the source).
        lag: Per-topic broker backlog read this tick.
    """
    # 1. Per-topic watermarks: max event-time (order trusted — last record wins)
    for topic, records in pulled.items():
        if records:
            state.watermark_ms[topic] = records[-1].event_time_ms

    # 2. Overwrite consumer_lag
    for topic, topic_lag in lag.items():
        state.consumer_lag[topic] = topic_lag

    # 3. Compute global watermark AFTER updating per-topic watermarks
    global_wm = _compute_global_watermark(state.watermark_ms, shape.gating_topics)

    # 4. Process joins using end-of-tick dimension watermarks
    _process_joins(state, shape, pulled)

    # 5. Advance windows
    _advance_windows(state, shape, global_wm)


async def run_consumer(
    source: "KafkaSource",
    control: ConsumerControlState,
    state: ConsumerState,
    shape: ConsumerJobShape,
    sleep: "Callable[[float], Awaitable[None]]",
    monotonic: "Callable[[], float]",
    tick_seconds: float,
) -> None:
    """Drive the ingestion loop — the async shell over ingest. Sibling of
    schedule_releases.

    Takes a baseline monotonic reading (first measured delta 0.0). Each tick: read the
    control snapshot, compute a per-topic pull budget from ingest_rate × measured delta
    (fractional carry retained across ticks), pull up to budget per topic via
    source.pull (I/O, off-loop), read per-topic backlog via source.lag, call ingest,
    then sleep(tick_seconds). Unlike the producer loop there is no drain-termination: a
    live consumer runs until the caller cancels the task.

    Args:
        source: The open KafkaSource.
        control: The mutable operator state, re-read every tick.
        state: The evolving timing state; mutated in place by ingest each tick.
        shape: Immutable windows / joins / gating set.
        sleep: Async sleep of N real seconds; asyncio.sleep in production, a fake
            in tests.
        monotonic: Monotonic real-clock reading in seconds; time.monotonic in
            production, a fake in tests.
        tick_seconds: Loop tick quantum in real seconds.

    Raises:
        KafkaConsumeError: a poll or offset read failed.
    """
    previous = monotonic()
    carry: dict[str, float] = {}

    while True:
        now = monotonic()
        delta = now - previous
        previous = now

        budgets: dict[str, int] = {}
        for dial in control.topics:
            topic_carry = carry.get(dial.topic, 0.0)
            raw = dial.ingest_rate * delta + topic_carry
            budget = int(raw)
            carry[dial.topic] = raw - budget
            budgets[dial.topic] = budget

        pulled = await source.pull(budgets)
        lag = await source.lag()
        ingest(control, state, shape, pulled, lag)
        await sleep(tick_seconds)
