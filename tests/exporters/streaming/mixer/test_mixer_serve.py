"""Tests for serve_mixer — the mixer lifecycle assembly."""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from fabulexa_export.errors import (
    KafkaConsumeError,
    KafkaDeliveryError,
    MixerExtraUnavailable,
)
from fabulexa_export.exporters.streaming.mixer.consumer import ConsumerLaunch
from fabulexa_export.exporters.streaming.mixer.run_state import MixerRunState

from ._helpers import _make_consumer_run_state, _make_run_state

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeSink:
    """A fake KafkaSink that tracks open/deliver/aclose calls."""

    def __init__(self) -> None:
        self.open_count = 0
        self.open_seq: int | None = None
        self.aclose_count = 0
        self.delivered: list[Any] = []
        self._raise_on_deliver: Exception | None = None

    async def deliver(self, event: Any) -> None:
        if self._raise_on_deliver is not None:
            raise self._raise_on_deliver
        self.delivered.append(event)

    async def aclose(self) -> None:
        self.aclose_count += 1


def _make_fake_sink_cls(fake_sink: _FakeSink, counter: list[int] | None = None) -> type:
    """Return a KafkaSink replacement whose open() returns fake_sink."""

    class _FakeKafkaSink:
        @classmethod
        async def open(
            cls,
            bootstrap_servers: str,
            topic_set: tuple[str, ...],
            render_value: Any,
            anchor: Any,
        ) -> "_FakeKafkaSink":
            if counter is not None:
                fake_sink.open_seq = counter[0]
                counter[0] += 1
            fake_sink.open_count += 1
            return fake_sink  # type: ignore[return-value]

    return _FakeKafkaSink


class _FakeServer:
    """A controllable server that runs the lifespan and then returns.

    Simulates uvicorn's behavior: enters the lifespan, spins until should_exit
    is set (or the release task finishes), then exits the lifespan (triggering
    shutdown), then returns from serve().
    """

    def __init__(self) -> None:
        self.should_exit = False
        self.config: Any = None

    async def serve(self, sockets: Any = None) -> None:
        """Run the app's lifespan; spin until should_exit, then shut down."""
        # Extract the FastAPI app from the config
        app = self.config.app if hasattr(self.config, "app") else None
        if app is None or not hasattr(app, "router"):
            return

        lifespan_ctx = getattr(app.router, "lifespan_context", None)
        if lifespan_ctx is None:
            return

        async with lifespan_ctx(app):
            # Spin until should_exit is set or no pending tasks remain.
            # Poll frequently to detect should_exit quickly.
            for _ in range(200):
                if self.should_exit:
                    break
                await asyncio.sleep(0.005)
            # Exit the lifespan — this triggers shutdown (lifespan __aexit__).


class _FakeConfig:
    def __init__(self, app: Any, host: str, port: int, log_level: str) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.log_level = log_level


def _install_fake_uvicorn(monkeypatch: pytest.MonkeyPatch, server: _FakeServer) -> None:
    """Install a fake uvicorn module that uses _FakeServer and _FakeConfig."""
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.Server = lambda config: _setup_server(server, config)  # type: ignore[attr-defined]
    fake_uvicorn.Config = _FakeConfig  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)


def _setup_server(server: _FakeServer, config: Any) -> _FakeServer:
    server.config = config
    return server


def _install_fake_fastapi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure fastapi is importable (it should already be, but this makes it explicit)."""
    # FastAPI is a real dep in this sprint; no fake needed.
    pass


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _install_fake_sink(
    monkeypatch: pytest.MonkeyPatch,
    fake_sink: _FakeSink,
    counter: list[int] | None = None,
) -> None:
    from fabulexa_export.exporters.streaming.mixer import sink as sink_mod

    monkeypatch.setattr(sink_mod, "KafkaSink", _make_fake_sink_cls(fake_sink, counter))


# ---------------------------------------------------------------------------
# Test: MixerExtraUnavailable raised before sink opens
# ---------------------------------------------------------------------------


class TestMixerExtraUnavailableBeforeSink:
    def test_raised_before_sink_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """serve_mixer raises MixerExtraUnavailable before the sink opens.

        When FastAPI / uvicorn is absent (simulated by removing them from sys.modules
        and blocking re-import), the error must surface before KafkaSink.open is called.
        """
        fake_sink = _FakeSink()

        # Block fastapi import to force MixerExtraUnavailable
        monkeypatch.setitem(sys.modules, "fastapi", None)  # type: ignore[misc]
        monkeypatch.setitem(sys.modules, "uvicorn", None)  # type: ignore[misc]

        # Also patch KafkaSink.open to track if it was called
        from fabulexa_export.exporters.streaming.mixer import sink as sink_mod

        monkeypatch.setattr(sink_mod, "KafkaSink", _make_fake_sink_cls(fake_sink))

        state = _make_run_state()

        with pytest.raises(MixerExtraUnavailable):
            _run(
                serve_mixer_under_test(
                    state=state,
                    render_value=lambda e: b"",
                    bootstrap_servers="localhost:9092",
                    topic_set=("orders",),
                    tick_seconds=0.1,
                    host="127.0.0.1",
                    port=19999,
                )
            )

        # The sink must NOT have been opened
        assert fake_sink.open_count == 0


# ---------------------------------------------------------------------------
# Test: task failure flips should_exit and exception is re-raised
# ---------------------------------------------------------------------------


class TestTaskFailureFlipsShoudlExit:
    def test_delivery_error_reraises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A KafkaDeliveryError from schedule_releases is re-raised after server returns."""
        fake_sink = _FakeSink()
        server = _FakeServer()

        _install_fake_uvicorn(monkeypatch, server)
        _install_fake_sink(monkeypatch, fake_sink)

        delivery_err = KafkaDeliveryError("simulated delivery failure")
        call_count = 0

        async def _failing_schedule_releases(**kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            raise delivery_err

        from fabulexa_export.exporters.streaming.mixer import scheduler as sched_mod

        monkeypatch.setattr(sched_mod, "schedule_releases", _failing_schedule_releases)

        state = _make_run_state()

        with pytest.raises(KafkaDeliveryError) as exc_info:
            _run(
                _call_serve_mixer(state, server),
            )

        assert exc_info.value is delivery_err
        # should_exit was flipped by the done-callback
        assert server.should_exit is True


# ---------------------------------------------------------------------------
# Test: clean interrupt — cancelled task → no exception
# ---------------------------------------------------------------------------


class TestCleanInterrupt:
    def test_cancelled_task_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cancelled release task (operator interrupt) leaves serve_mixer returning None."""
        fake_sink = _FakeSink()
        server = _FakeServer()

        _install_fake_uvicorn(monkeypatch, server)
        _install_fake_sink(monkeypatch, fake_sink)

        async def _slow_schedule_releases(**kwargs: Any) -> None:
            # Simulates an indefinitely-running loop that will be cancelled at shutdown.
            await asyncio.sleep(9999)

        from fabulexa_export.exporters.streaming.mixer import scheduler as sched_mod

        monkeypatch.setattr(sched_mod, "schedule_releases", _slow_schedule_releases)

        state = _make_run_state()
        result = _run(_call_serve_mixer(state, server))
        assert result is None


# ---------------------------------------------------------------------------
# Test: drained run — task returns on its own → no exception
# ---------------------------------------------------------------------------


class TestDrainedRun:
    def test_drained_task_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cleanly-returned (drained) release task leaves serve_mixer returning None."""
        fake_sink = _FakeSink()
        server = _FakeServer()

        _install_fake_uvicorn(monkeypatch, server)
        _install_fake_sink(monkeypatch, fake_sink)

        async def _drain_immediately(**kwargs: Any) -> None:
            # All buffers empty — returns without releasing anything.
            return

        from fabulexa_export.exporters.streaming.mixer import scheduler as sched_mod

        monkeypatch.setattr(sched_mod, "schedule_releases", _drain_immediately)

        state = _make_run_state()
        result = _run(_call_serve_mixer(state, server))
        assert result is None


# ---------------------------------------------------------------------------
# Test: lifespan stamps play_origin_monotonic and aclose called once
# ---------------------------------------------------------------------------


class TestLifespanBehavior:
    def test_stamps_play_origin_when_playing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lifespan startup stamps state.play_origin_monotonic when launched playing."""
        fake_sink = _FakeSink()
        server = _FakeServer()
        _install_fake_uvicorn(monkeypatch, server)
        _install_fake_sink(monkeypatch, fake_sink)

        from fabulexa_export.exporters.streaming.mixer import scheduler as sched_mod

        monkeypatch.setattr(
            sched_mod, "schedule_releases", _make_noop_schedule_releases()
        )

        state = _make_run_state(playing=True, monotonic_val=42.0)
        assert state.play_origin_monotonic is None

        _run(_call_serve_mixer(state, server))

        assert state.play_origin_monotonic == 42.0

    def test_no_stamp_when_paused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Lifespan startup leaves play_origin_monotonic None when launched paused."""
        fake_sink = _FakeSink()
        server = _FakeServer()
        _install_fake_uvicorn(monkeypatch, server)
        _install_fake_sink(monkeypatch, fake_sink)

        from fabulexa_export.exporters.streaming.mixer import scheduler as sched_mod

        monkeypatch.setattr(
            sched_mod, "schedule_releases", _make_noop_schedule_releases()
        )

        state = _make_run_state(playing=False, monotonic_val=42.0)
        _run(_call_serve_mixer(state, server))

        assert state.play_origin_monotonic is None

    def test_aclose_called_exactly_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Shutdown awaits KafkaSink.aclose() exactly once."""
        fake_sink = _FakeSink()
        server = _FakeServer()
        _install_fake_uvicorn(monkeypatch, server)
        _install_fake_sink(monkeypatch, fake_sink)

        from fabulexa_export.exporters.streaming.mixer import scheduler as sched_mod

        monkeypatch.setattr(
            sched_mod, "schedule_releases", _make_noop_schedule_releases()
        )

        state = _make_run_state()
        _run(_call_serve_mixer(state, server))

        assert fake_sink.aclose_count == 1


# ---------------------------------------------------------------------------
# Shared async entrypoint helpers
# ---------------------------------------------------------------------------


def _make_noop_schedule_releases() -> Any:
    async def _noop(**kwargs: Any) -> None:
        return

    return _noop


async def _call_serve_mixer(state: MixerRunState, server: _FakeServer) -> None:
    """Call serve_mixer with the patched fake server in scope."""
    from fabulexa_export.exporters.streaming.mixer.serve import serve_mixer

    return await serve_mixer(
        state=state,
        render_value=lambda e: b"",
        bootstrap_servers="localhost:9092",
        topic_set=tuple(d.topic for d in state.control.topics),
        tick_seconds=0.1,
        host="127.0.0.1",
        port=19999,
    )


async def serve_mixer_under_test(
    state: MixerRunState,
    render_value: Any,
    bootstrap_servers: str,
    topic_set: tuple[str, ...],
    tick_seconds: float,
    host: str,
    port: int,
) -> None:
    """Thin wrapper used in tests that need to run serve_mixer directly."""
    from fabulexa_export.exporters.streaming.mixer.serve import serve_mixer

    return await serve_mixer(
        state=state,
        render_value=render_value,
        bootstrap_servers=bootstrap_servers,
        topic_set=topic_set,
        tick_seconds=tick_seconds,
        host=host,
        port=port,
    )


# ---------------------------------------------------------------------------
# Phase 4 helpers — consumer wiring
# ---------------------------------------------------------------------------


def _make_consumer_launch() -> ConsumerLaunch:
    return ConsumerLaunch(group_id="test-group", offset_reset="earliest")


def _make_run_state_with_consumer(
    topics: list[str] | None = None,
    monotonic_val: float = 100.0,
) -> MixerRunState:
    """Build a MixerRunState with a ConsumerRunState attached."""
    if topics is None:
        topics = ["orders"]
    state = _make_run_state(topics=topics, monotonic_val=monotonic_val)
    state.consumer = _make_consumer_run_state(topics)
    return state


class _FakeSource:
    """A fake KafkaSource that tracks open/pull/lag/aclose calls."""

    def __init__(self) -> None:
        self.open_count = 0
        self.open_seq: int | None = None
        self.aclose_count = 0
        self.pull_count = 0
        self.lag_count = 0
        self._raise_on_pull: Exception | None = None

    async def pull(self, budgets: dict[str, int]) -> dict[str, list[Any]]:
        self.pull_count += 1
        if self._raise_on_pull is not None:
            raise self._raise_on_pull
        return {t: [] for t in budgets}

    async def lag(self) -> dict[str, int]:
        self.lag_count += 1
        return {}

    async def aclose(self) -> None:
        self.aclose_count += 1


def _make_fake_source_cls(
    fake_source: _FakeSource, counter: list[int] | None = None
) -> type:
    """Return a KafkaSource replacement whose open() returns fake_source."""

    class _FakeKafkaSource:
        @classmethod
        async def open(
            cls,
            bootstrap_servers: str,
            topic_set: tuple[str, ...],
            group_id: str,
            offset_reset: str,
        ) -> "_FakeKafkaSource":
            if counter is not None:
                fake_source.open_seq = counter[0]
                counter[0] += 1
            fake_source.open_count += 1
            return fake_source  # type: ignore[return-value]

    return _FakeKafkaSource


def _install_fake_source(
    monkeypatch: pytest.MonkeyPatch,
    fake_source: _FakeSource,
    counter: list[int] | None = None,
) -> None:
    from fabulexa_export.exporters.streaming.mixer import source as source_mod

    monkeypatch.setattr(
        source_mod, "KafkaSource", _make_fake_source_cls(fake_source, counter)
    )


async def _call_serve_mixer_with_consumer(
    state: MixerRunState,
    server: _FakeServer,
    consumer_launch: ConsumerLaunch | None = None,
) -> None:
    """Call serve_mixer with consumer_launch passed through."""
    from fabulexa_export.exporters.streaming.mixer.serve import serve_mixer

    return await serve_mixer(
        state=state,
        render_value=lambda e: b"",
        bootstrap_servers="localhost:9092",
        topic_set=tuple(d.topic for d in state.control.topics),
        tick_seconds=0.1,
        host="127.0.0.1",
        port=19999,
        consumer_launch=consumer_launch,
    )


# ---------------------------------------------------------------------------
# Phase 4 Tests
# ---------------------------------------------------------------------------


class TestConsumerLaunchNone:
    """consumer_launch=None: existing behavior unchanged — no source, no consumer task."""

    def test_no_source_opened_when_consumer_launch_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When consumer_launch is None, no KafkaSource opens."""
        fake_sink = _FakeSink()
        fake_source = _FakeSource()
        server = _FakeServer()

        _install_fake_uvicorn(monkeypatch, server)
        _install_fake_sink(monkeypatch, fake_sink)
        _install_fake_source(monkeypatch, fake_source)

        from fabulexa_export.exporters.streaming.mixer import scheduler as sched_mod

        monkeypatch.setattr(
            sched_mod, "schedule_releases", _make_noop_schedule_releases()
        )

        state = _make_run_state()
        _run(_call_serve_mixer_with_consumer(state, server, consumer_launch=None))

        assert fake_source.open_count == 0
        assert fake_sink.aclose_count == 1

    def test_consumer_task_absent_when_consumer_launch_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When consumer_launch is None, run_consumer is never called."""
        fake_sink = _FakeSink()
        fake_source = _FakeSource()
        server = _FakeServer()

        _install_fake_uvicorn(monkeypatch, server)
        _install_fake_sink(monkeypatch, fake_sink)
        _install_fake_source(monkeypatch, fake_source)

        consumer_called = [False]

        async def _fake_run_consumer(**kwargs: Any) -> None:
            consumer_called[0] = True

        from fabulexa_export.exporters.streaming.mixer import consumer as consumer_mod

        monkeypatch.setattr(consumer_mod, "run_consumer", _fake_run_consumer)

        from fabulexa_export.exporters.streaming.mixer import scheduler as sched_mod

        monkeypatch.setattr(
            sched_mod, "schedule_releases", _make_noop_schedule_releases()
        )

        state = _make_run_state()
        _run(_call_serve_mixer_with_consumer(state, server, consumer_launch=None))

        assert consumer_called[0] is False


class TestConsumerTaskStartsAndShutdown:
    """consumer_launch given + state.consumer set: source opens, task starts, both close on shutdown."""

    def test_source_opens_after_sink(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With consumer_launch, KafkaSource opens (after sink) once, sink opens first."""
        fake_sink = _FakeSink()
        fake_source = _FakeSource()
        server = _FakeServer()
        open_counter: list[int] = [0]

        _install_fake_uvicorn(monkeypatch, server)
        _install_fake_sink(monkeypatch, fake_sink, open_counter)
        _install_fake_source(monkeypatch, fake_source, open_counter)

        from fabulexa_export.exporters.streaming.mixer import scheduler as sched_mod

        monkeypatch.setattr(
            sched_mod, "schedule_releases", _make_noop_schedule_releases()
        )

        async def _slow_consumer(**kwargs: Any) -> None:
            await asyncio.sleep(9999)

        from fabulexa_export.exporters.streaming.mixer import consumer as consumer_mod

        monkeypatch.setattr(consumer_mod, "run_consumer", _slow_consumer)

        state = _make_run_state_with_consumer()
        _run(_call_serve_mixer_with_consumer(state, server, _make_consumer_launch()))

        assert fake_source.open_count == 1
        assert fake_sink.open_count == 1
        assert fake_sink.open_seq is not None
        assert fake_source.open_seq is not None
        assert fake_sink.open_seq < fake_source.open_seq

    def test_both_source_and_sink_aclose_on_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shutdown aclose()s both the source and the sink."""
        fake_sink = _FakeSink()
        fake_source = _FakeSource()
        server = _FakeServer()

        _install_fake_uvicorn(monkeypatch, server)
        _install_fake_sink(monkeypatch, fake_sink)
        _install_fake_source(monkeypatch, fake_source)

        from fabulexa_export.exporters.streaming.mixer import scheduler as sched_mod

        monkeypatch.setattr(
            sched_mod, "schedule_releases", _make_noop_schedule_releases()
        )

        async def _slow_consumer(**kwargs: Any) -> None:
            await asyncio.sleep(9999)

        from fabulexa_export.exporters.streaming.mixer import consumer as consumer_mod

        monkeypatch.setattr(consumer_mod, "run_consumer", _slow_consumer)

        state = _make_run_state_with_consumer()
        _run(_call_serve_mixer_with_consumer(state, server, _make_consumer_launch()))

        assert fake_sink.aclose_count == 1
        assert fake_source.aclose_count == 1


class TestConsumerTaskFailureFlipsShould_exit:
    """A KafkaConsumeError from run_consumer flips should_exit and is re-raised."""

    def test_consume_error_reraises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A KafkaConsumeError raised by the consumer task is re-raised after server returns."""
        fake_sink = _FakeSink()
        fake_source = _FakeSource()
        server = _FakeServer()

        _install_fake_uvicorn(monkeypatch, server)
        _install_fake_sink(monkeypatch, fake_sink)
        _install_fake_source(monkeypatch, fake_source)

        consume_err = KafkaConsumeError("simulated consume failure")

        from fabulexa_export.exporters.streaming.mixer import scheduler as sched_mod

        monkeypatch.setattr(
            sched_mod, "schedule_releases", _make_noop_schedule_releases()
        )

        async def _failing_consumer(**kwargs: Any) -> None:
            raise consume_err

        from fabulexa_export.exporters.streaming.mixer import consumer as consumer_mod

        monkeypatch.setattr(consumer_mod, "run_consumer", _failing_consumer)

        state = _make_run_state_with_consumer()

        with pytest.raises(KafkaConsumeError) as exc_info:
            _run(
                _call_serve_mixer_with_consumer(state, server, _make_consumer_launch())
            )

        assert exc_info.value is consume_err
        assert server.should_exit is True


class TestSourceOpenOrdering:
    """Sink opens before source; source-open failure still aclose()s the sink."""

    def test_source_open_failure_closes_sink(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If source open raises, the already-open sink is aclose()d before re-raising."""
        fake_sink = _FakeSink()
        server = _FakeServer()

        _install_fake_uvicorn(monkeypatch, server)
        _install_fake_sink(monkeypatch, fake_sink)

        source_err = KafkaConsumeError("source open failed")

        class _FailingKafkaSource:
            @classmethod
            async def open(cls, **kwargs: Any) -> "_FailingKafkaSource":
                raise source_err

        from fabulexa_export.exporters.streaming.mixer import source as source_mod

        monkeypatch.setattr(source_mod, "KafkaSource", _FailingKafkaSource)

        state = _make_run_state_with_consumer()

        with pytest.raises(KafkaConsumeError) as exc_info:
            _run(
                _call_serve_mixer_with_consumer(state, server, _make_consumer_launch())
            )

        assert exc_info.value is source_err
        # Sink must have been aclose()d before re-raising
        assert fake_sink.aclose_count == 1
