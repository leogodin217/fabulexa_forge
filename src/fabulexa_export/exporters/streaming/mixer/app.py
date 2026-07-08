"""FastAPI control-plane app for the mixer.

`build_app` is the sole public entry point. It imports FastAPI lazily so
importing this module does not require the `mixer` extra to be installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from fastapi import FastAPI

    from fabulexa_export.anchor import EffectiveAnchor
    from fabulexa_export.exporters.streaming.mixer.consumer import ConsumerRunState
    from fabulexa_export.exporters.streaming.mixer.run_state import MixerRunState
    from fabulexa_export.exporters.streaming.mixer.wire import (
        ConsumerMetersOut,
        MetersOut,
    )


def _render_ts_str(event_sim_time: int, anchor: Any) -> str:
    """Render an event_sim_time to an offset-bearing ISO-8601 string.

    Delegates to the engine's _render_ts; the anchor is always non-None on a
    mixer run, so the return value is always a str.
    """
    from fabulexa_export.exporters.streaming.engine import _render_ts

    result = _render_ts(event_sim_time, anchor)
    return str(result)


def _wall_elapsed_ms(
    play_origin_monotonic: float | None,
    monotonic: Callable[[], float],
) -> int:
    """Compute wall elapsed ms since the play origin, or 0 if not yet playing."""
    if play_origin_monotonic is None:
        return 0
    return max(0, round((monotonic() - play_origin_monotonic) * 1000))


def _topic_delivery_lag_ms(
    frontier_sim_time: int | None,
    delivery_edge: int | None,
) -> int | None:
    """Compute the delivery lag in ms for one topic.

    Returns None when the frontier is None, the delivery_edge is None (topic
    never delivered). Always >= 0.
    """
    if frontier_sim_time is None or delivery_edge is None:
        return None
    lag_ns = frontier_sim_time - delivery_edge
    return lag_ns // 1_000_000


def _render_epoch_ms_str(epoch_ms: int, anchor: Any) -> str:
    """Render an absolute epoch-millisecond timestamp in the anchor's timezone.

    Converts epoch_ms to a tz-aware UTC datetime, then projects into the anchor's
    timezone and returns an offset-bearing ISO-8601 string.
    """
    from datetime import datetime, timedelta
    from datetime import timezone as _tz

    utc_dt = datetime(1970, 1, 1, tzinfo=_tz.utc) + timedelta(milliseconds=epoch_ms)
    return str(utc_dt.astimezone(anchor.timezone).isoformat())


def derive_consumer_meters(
    consumer: "ConsumerRunState",
    anchor: "EffectiveAnchor",
) -> "ConsumerMetersOut":
    """Compute the consumer meters snapshot from raw consumer state.

    Pure w.r.t. its inputs. Renders each per-topic watermark and the global watermark
    (min over shape.gating_topics, None if any gating watermark is None) from epoch-ms
    through the anchor zone; per-topic consumer_lag verbatim; one WindowMeterOut per
    declared window (size, fired_count, anchor-rendered latest end); one JoinMeterOut
    per declared join (fact_count, null_count, null_rate = null/fact or None). Topics in
    ConsumerControlState.topics order.
    """
    from fabulexa_export.exporters.streaming.mixer.wire import (
        ConsumerMetersOut,
        ConsumerTopicMeterOut,
        JoinMeterOut,
        WindowMeterOut,
    )

    control = consumer.control
    state = consumer.state
    shape = consumer.shape

    # Global watermark: min over gating topics; None if any gating watermark is None.
    global_wm_ms: int | None = None
    if shape.gating_topics:
        gating_wms = [state.watermark_ms.get(t) for t in shape.gating_topics]
        if all(w is not None for w in gating_wms):
            global_wm_ms = min(w for w in gating_wms if w is not None)

    global_wm_str = (
        _render_epoch_ms_str(global_wm_ms, anchor) if global_wm_ms is not None else None
    )

    # Per-topic meters in control.topics order.
    topic_meters: list[ConsumerTopicMeterOut] = []
    for dial in control.topics:
        wm = state.watermark_ms.get(dial.topic)
        wm_str = _render_epoch_ms_str(wm, anchor) if wm is not None else None
        topic_meters.append(
            ConsumerTopicMeterOut(
                topic=dial.topic,
                watermark_sim_time=wm_str,
                consumer_lag=state.consumer_lag.get(dial.topic, 0),
            )
        )

    # Windows: one per declared window, in shape order.
    window_meters: list[WindowMeterOut] = []
    for i, window in enumerate(shape.windows):
        latest_end = state.window_latest_end_ms[i]
        latest_str = (
            _render_epoch_ms_str(latest_end, anchor) if latest_end is not None else None
        )
        window_meters.append(
            WindowMeterOut(
                size_ms=window.size_ms,
                fired_count=state.window_fired_count[i],
                latest_window_end_sim_time=latest_str,
            )
        )

    # Joins: one per declared join, null_rate = null/fact or None when fact_count == 0.
    join_meters: list[JoinMeterOut] = []
    for i, join in enumerate(shape.joins):
        fact_count = state.join_fact_count[i]
        null_count = state.join_null_count[i]
        null_rate = null_count / fact_count if fact_count > 0 else None
        join_meters.append(
            JoinMeterOut(
                fact_topic=join.fact_topic,
                dimension_topic=join.dimension_topic,
                fact_count=fact_count,
                null_count=null_count,
                null_rate=null_rate,
            )
        )

    return ConsumerMetersOut(
        global_watermark_sim_time=global_wm_str,
        topics=topic_meters,
        windows=window_meters,
        joins=join_meters,
    )


def derive_meters(state: "MixerRunState") -> "MetersOut":
    """Compute the producer-side meters snapshot from raw scheduler state.

    Pure with respect to its inputs (reads state.monotonic() and the mutable
    frontier / buffers); unit-testable with a fabricated FrontierState, buffers,
    and fake monotonic. Renders frontier_sim_time / delivery_edge_sim_time through
    _render_ts(.., anchor) (anchor non-None on a mixer run, so both cast to str);
    delivery_lag_ms is the nanosecond frontier - delivery_edge gap floored to ms
    (// 1_000_000); backlog is buffer depth; wall_elapsed_ms from
    play_origin_monotonic. Topics emitted in ControlState.topics order. See design
    § Meters derivation for the per-field null cases.
    """
    from fabulexa_export.exporters.streaming.mixer.wire import MetersOut, TopicMeterOut

    frontier = state.frontier
    buffers = state.buffers
    anchor = state.anchor

    if frontier.frontier_sim_time is None:
        frontier_sim_time_str: str | None = None
    else:
        frontier_sim_time_str = _render_ts_str(frontier.frontier_sim_time, anchor)

    wall_ms = _wall_elapsed_ms(state.play_origin_monotonic, state.monotonic)

    topic_meters: list[TopicMeterOut] = []
    for dial in state.control.topics:
        topic = dial.topic
        buf = buffers[dial.topic]
        backlog = len(buf)

        delivery_edge = frontier.delivery_edges.get(topic)
        lag_ms = _topic_delivery_lag_ms(frontier.frontier_sim_time, delivery_edge)

        if delivery_edge is None:
            delivery_edge_str: str | None = None
        else:
            delivery_edge_str = _render_ts_str(delivery_edge, anchor)

        topic_meters.append(
            TopicMeterOut(
                topic=topic,
                backlog=backlog,
                delivery_lag_ms=lag_ms,
                delivery_edge_sim_time=delivery_edge_str,
            )
        )

    return MetersOut(
        frontier_sim_time=frontier_sim_time_str,
        wall_elapsed_ms=wall_ms,
        topics=topic_meters,
    )


def build_app(state: "MixerRunState") -> "FastAPI":
    """Build the FastAPI app serving the control API over the run state.

    Registers four endpoints under base path /api: GET /state (ControlStateOut),
    GET /meters (derive_meters), PUT /transport (mutate transport, stamp play
    origin on the first False->True, echo TransportOut), PUT /topics/{topic}
    (mutate the matching TopicDials or 404, echo TopicDialsOut). Out-of-bounds
    bodies are 422 by the request models. Handlers mutate / read `state`
    synchronously (no mid-handler await), preserving the lock-free consistency
    invariant. FastAPI is imported lazily inside this function; importing the
    module does not require the `mixer` extra.

    Raises:
        MixerExtraUnavailable: FastAPI is not importable (the `mixer` extra is
            absent).
    """
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as exc:
        from fabulexa_export.errors import MixerExtraUnavailable

        raise MixerExtraUnavailable(
            "FastAPI is not importable — install the `mixer` extra: "
            "pip install fabulexa-export[mixer]"
        ) from exc

    # Import wire models now so they are in scope for the handler annotations.
    # These imports are intentionally not at module level — importing this module
    # must not require the `mixer` extra.
    from fabulexa_export.exporters.streaming.mixer.wire import (
        CapabilitiesOut,
        ConsumerControlStateOut,
        ConsumerMetersOut,
        ConsumerTopicDialsOut,
        ConsumerTopicDialsUpdate,
        ControlStateOut,
        MetersOut,
        TopicDialsOut,
        TopicDialsUpdate,
        TransportOut,
        TransportUpdate,
    )

    app = FastAPI()

    @app.get("/api/state", response_model=ControlStateOut)
    def get_state() -> ControlStateOut:
        control = state.control
        transport_out = TransportOut(
            playing=control.transport.playing,
            speed=control.transport.speed,
        )
        topics_out = [
            TopicDialsOut(
                topic=d.topic,
                content=d.content,
                rate=d.rate,
                lag_ms=d.lag_ms,
                mute=d.mute,
            )
            for d in control.topics
        ]
        return ControlStateOut(transport=transport_out, topics=topics_out)

    def _get_meters() -> MetersOut:
        return derive_meters(state)

    _get_meters.__globals__["MetersOut"] = MetersOut
    app.get("/api/meters")(_get_meters)

    # PEP 563 (`from __future__ import annotations`) turns all annotations into
    # strings. FastAPI resolves them via `typing.get_type_hints` using the handler's
    # `__globals__`, which for inner functions are this module's globals — so locally
    # imported names like `TransportUpdate` are invisible to FastAPI at route
    # registration time. Injecting them into __globals__ makes them resolvable.

    def _put_transport(body: TransportUpdate) -> TransportOut:
        was_playing = state.control.transport.playing
        state.control.transport.playing = body.playing
        state.control.transport.speed = body.speed
        if not was_playing and body.playing and state.play_origin_monotonic is None:
            state.play_origin_monotonic = state.monotonic()
        return TransportOut(playing=body.playing, speed=body.speed)

    _put_transport.__globals__["TransportUpdate"] = TransportUpdate
    _put_transport.__globals__["TransportOut"] = TransportOut
    app.put("/api/transport", response_model=TransportOut)(_put_transport)

    def _put_topic(topic: str, body: TopicDialsUpdate) -> TopicDialsOut:
        for dial in state.control.topics:
            if dial.topic == topic:
                dial.rate = body.rate
                dial.lag_ms = body.lag_ms
                dial.mute = body.mute
                return TopicDialsOut(
                    topic=dial.topic,
                    content=dial.content,
                    rate=dial.rate,
                    lag_ms=dial.lag_ms,
                    mute=dial.mute,
                )
        raise HTTPException(status_code=404, detail=f"Unknown topic: {topic!r}")

    _put_topic.__globals__["TopicDialsUpdate"] = TopicDialsUpdate
    _put_topic.__globals__["TopicDialsOut"] = TopicDialsOut
    app.put("/api/topics/{topic}", response_model=TopicDialsOut)(_put_topic)

    def _get_capabilities() -> CapabilitiesOut:
        return CapabilitiesOut(consumer_enabled=state.consumer is not None)

    _get_capabilities.__globals__["CapabilitiesOut"] = CapabilitiesOut
    app.get("/api/capabilities", response_model=CapabilitiesOut)(_get_capabilities)

    if state.consumer is not None:

        def _get_consumer_state() -> ConsumerControlStateOut:
            consumer = state.consumer
            assert consumer is not None
            topics_out = [
                ConsumerTopicDialsOut(
                    topic=d.topic,
                    content=d.content,
                    ingest_rate=d.ingest_rate,
                )
                for d in consumer.control.topics
            ]
            return ConsumerControlStateOut(topics=topics_out)

        _get_consumer_state.__globals__["ConsumerControlStateOut"] = (
            ConsumerControlStateOut
        )
        _get_consumer_state.__globals__["ConsumerTopicDialsOut"] = ConsumerTopicDialsOut
        app.get("/api/consumer/state", response_model=ConsumerControlStateOut)(
            _get_consumer_state
        )

        def _get_consumer_meters() -> ConsumerMetersOut:
            consumer = state.consumer
            assert consumer is not None
            return derive_consumer_meters(consumer, state.anchor)

        _get_consumer_meters.__globals__["ConsumerMetersOut"] = ConsumerMetersOut
        app.get("/api/consumer/meters", response_model=ConsumerMetersOut)(
            _get_consumer_meters
        )

        def _put_consumer_topic(
            topic: str, body: ConsumerTopicDialsUpdate
        ) -> ConsumerTopicDialsOut:
            consumer = state.consumer
            assert consumer is not None
            for dial in consumer.control.topics:
                if dial.topic == topic:
                    dial.ingest_rate = body.ingest_rate
                    return ConsumerTopicDialsOut(
                        topic=dial.topic,
                        content=dial.content,
                        ingest_rate=dial.ingest_rate,
                    )
            raise HTTPException(
                status_code=404, detail=f"Unknown consumer topic: {topic!r}"
            )

        _put_consumer_topic.__globals__["ConsumerTopicDialsUpdate"] = (
            ConsumerTopicDialsUpdate
        )
        _put_consumer_topic.__globals__["ConsumerTopicDialsOut"] = ConsumerTopicDialsOut
        app.put("/api/consumer/topics/{topic}", response_model=ConsumerTopicDialsOut)(
            _put_consumer_topic
        )

    return app
