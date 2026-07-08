"""Async lifecycle assembly for the mixer control plane.

`serve_mixer` is the single entry point: probe extra, open sink, build app,
run uvicorn with lifespan, drive schedule_releases, shutdown cleanly.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator

if TYPE_CHECKING:
    from collections.abc import Callable

    from fabulexa_export.exporters.streaming.mixer.consumer import ConsumerLaunch
    from fabulexa_export.exporters.streaming.mixer.run_state import MixerRunState
    from fabulexa_export.exporters.streaming.types import StreamEvent


def _probe_mixer_extra() -> None:
    """Import FastAPI and uvicorn to confirm the `mixer` extra is installed.

    Called before the sink opens so that when both `mixer` and `kafka` extras
    are absent, the operator sees MixerExtraUnavailable (which resolves both,
    since [mixer] composes [kafka]) rather than KafkaClientUnavailable.

    Raises:
        MixerExtraUnavailable: FastAPI or uvicorn is not importable.
    """
    try:
        import fastapi as _fastapi_probe  # noqa: F401
        import uvicorn as _uvicorn_probe  # noqa: F401
    except ImportError as exc:
        from fabulexa_export.errors import MixerExtraUnavailable

        raise MixerExtraUnavailable(
            "FastAPI or uvicorn is not importable — install the `mixer` extra: "
            "pip install fabulexa-export[mixer]"
        ) from exc


def _make_done_callback(
    server_holder: "list[Any]",
    stored: list[BaseException],
) -> "Callable[[asyncio.Task[None]], None]":
    """Return a task done-callback that flips server.should_exit on failure.

    On a non-CancelledError exception the exception is stored in `stored` and
    `server.should_exit` is set to True so uvicorn begins its graceful shutdown.
    A cancelled task or a cleanly-returned task leaves `stored` empty.

    Args:
        server_holder: A single-element list whose first entry is the uvicorn.Server.
            The indirection lets the callback be registered before the server object
            is fully constructed.
        stored: A list for the exception; mutated by the callback.

    Returns:
        A callable suitable for Task.add_done_callback.
    """

    def _callback(task: "asyncio.Task[None]") -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            stored.append(exc)
            # server_holder[0] is the uvicorn.Server; set should_exit to begin shutdown.
            server_holder[0].should_exit = True

    return _callback


async def serve_mixer(
    state: "MixerRunState",
    render_value: "Callable[[StreamEvent], bytes]",
    bootstrap_servers: str,
    topic_set: tuple[str, ...],
    tick_seconds: float,
    host: str,
    port: int,
    consumer_launch: "ConsumerLaunch | None" = None,
) -> None:
    """Run the serving phase: probe mixer extra, open the sink, serve, shut down.

    Probes the `mixer` extra (lazily import FastAPI + the ASGI server) FIRST,
    before the sink opens, so when both `mixer` and `kafka` are absent the
    operator sees the MixerExtraUnavailable install hint (which, because [mixer]
    composes [kafka], resolves both) rather than KafkaClientUnavailable. Then
    opens a KafkaSink, builds the app over `state`, and runs uvicorn on host:port
    with a lifespan that (startup) stamps the play origin when launched playing
    and starts the schedule_releases task with sink=KafkaSink.deliver,
    sleep=asyncio.sleep, monotonic=state.monotonic, tick_seconds=tick_seconds;
    and (shutdown) cancels the task and awaits KafkaSink.aclose(). The release
    task carries a done-callback that, on a non-cancelled exception, flips
    uvicorn's should_exit to begin shutdown. After server.serve() returns
    (interrupt, drain, or task failure), serve_mixer re-raises a non-cancelled
    task exception so cmd_mixer's funnel maps it to exit 1; a cancelled
    (interrupt) or cleanly-returned (drain) task leaves no exception.

    When consumer_launch is not None and state.consumer is not None, opens a
    KafkaSource after the sink, starts a second run_consumer task under the same
    lifespan / done-callback wiring. Shutdown cancels both tasks and aclose()s
    both source and sink. The sink opens before the source; a source-open failure
    still aclose()s the already-open sink.

    Args:
        state: The shared mutable run state.
        render_value: Serialise a StreamEvent to bytes for Kafka delivery.
        bootstrap_servers: Kafka bootstrap servers connection string.
        topic_set: The routed topic names the sink creates and publishes to.
        tick_seconds: Loop tick quantum in real seconds.
        host: Host address for uvicorn to bind.
        port: Port for uvicorn to bind.
        consumer_launch: When not None (correlated with state.consumer not None),
            opens a KafkaSource and starts a run_consumer task.

    Raises:
        MixerExtraUnavailable: FastAPI or the ASGI server is not importable
            (probed before the sink opens, so it precedes
            KafkaClientUnavailable when both extras absent).
        KafkaClientUnavailable: confluent-kafka is not importable.
        KafkaDeliveryError: topic creation, delivery, or flush fails.
        KafkaConsumeError: consumer poll or offset read fails.
        OSError: the server cannot bind host:port.
    """
    _probe_mixer_extra()

    import uvicorn

    from fabulexa_export.exporters.streaming.mixer.scheduler import schedule_releases
    from fabulexa_export.exporters.streaming.mixer.sink import KafkaSink

    sink = await KafkaSink.open(
        bootstrap_servers=bootstrap_servers,
        topic_set=topic_set,
        render_value=render_value,
        anchor=state.anchor,
    )

    # Open the source after the sink. If source open fails, close the sink first.
    source = None
    if consumer_launch is not None and state.consumer is not None:
        from fabulexa_export.exporters.streaming.mixer.source import KafkaSource

        try:
            source = await KafkaSource.open(
                bootstrap_servers=bootstrap_servers,
                topic_set=topic_set,
                group_id=consumer_launch.group_id,
                offset_reset=consumer_launch.offset_reset,
            )
        except Exception:
            await sink.aclose()
            raise

    from fabulexa_export.exporters.streaming.mixer.app import build_app

    app = build_app(state)

    # Use a holder list so the lifespan callback can reference the server object
    # which is created after the lifespan context manager is defined.
    server_holder: list[object] = []
    task_exception: list[BaseException] = []
    done_callback = _make_done_callback(server_holder, task_exception)

    @asynccontextmanager
    async def _lifespan(app_: object) -> AsyncIterator[None]:
        # Startup: stamp play origin if launched playing, then start the release task.
        if state.control.transport.playing and state.play_origin_monotonic is None:
            state.play_origin_monotonic = state.monotonic()

        release_task: asyncio.Task[None] = asyncio.ensure_future(
            schedule_releases(
                buffers=state.buffers,
                control=state.control,
                frontier=state.frontier,
                sink=sink.deliver,
                sleep=asyncio.sleep,
                monotonic=state.monotonic,
                tick_seconds=tick_seconds,
            )
        )
        release_task.add_done_callback(done_callback)

        consumer_task: asyncio.Task[None] | None = None
        if source is not None and state.consumer is not None:
            from fabulexa_export.exporters.streaming.mixer.consumer import run_consumer

            consumer_task = asyncio.ensure_future(
                run_consumer(
                    source=source,
                    control=state.consumer.control,
                    state=state.consumer.state,
                    shape=state.consumer.shape,
                    sleep=asyncio.sleep,
                    monotonic=state.monotonic,
                    tick_seconds=tick_seconds,
                )
            )
            consumer_task.add_done_callback(done_callback)

        yield

        # Shutdown: cancel tasks and drain the sink and source.
        release_task.cancel()
        try:
            await release_task
        except (asyncio.CancelledError, Exception):
            pass

        if consumer_task is not None:
            consumer_task.cancel()
            try:
                await consumer_task
            except (asyncio.CancelledError, Exception):
                pass

        await sink.aclose()
        if source is not None:
            await source.aclose()

    app.router.lifespan_context = _lifespan

    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    server_holder.append(server)

    await server.serve()

    # Re-raise any non-cancelled task exception.
    if task_exception:
        raise task_exception[0]
