"""Tests for mixer/source.py and run_consumer — Phase 2: Kafka source + ingestion loop."""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from fabulexa_forge.errors import KafkaClientUnavailable, KafkaConsumeError
from fabulexa_forge.exporters.streaming.mixer.consumer import (
    ConsumerControlState,
    ConsumerJobShape,
    ConsumerState,
    IngestedRecord,
    run_consumer,
    seed_consumer_run,
)
from fabulexa_forge.exporters.streaming.mixer.source import (
    KafkaSource,
    _open_consumer_blocking,
)

# ---------------------------------------------------------------------------
# Fake confluent-kafka infrastructure
# ---------------------------------------------------------------------------


class _FakeKafkaError:
    def __init__(self, msg: str = "broker error") -> None:
        self._msg = msg

    def __str__(self) -> str:
        return self._msg


class _FakeKafkaException(Exception):
    pass


class _FakeTopicPartition:
    def __init__(self, topic: str, partition: int, offset: int = -1001) -> None:
        self.topic = topic
        self.partition = partition
        self.offset = offset


class _FakeMessage:
    def __init__(
        self,
        topic: str,
        ts_ms: int,
        offset: int,
        err: _FakeKafkaError | None = None,
    ) -> None:
        self._topic = topic
        self._ts_ms = ts_ms
        self._offset = offset
        self._err = err

    def topic(self) -> str:
        return self._topic

    def timestamp(self) -> tuple[int, int]:
        return (1, self._ts_ms)  # (TIMESTAMP_CREATE_TIME, epoch_ms)

    def offset(self) -> int:
        return self._offset

    def error(self) -> _FakeKafkaError | None:
        return self._err

    def key(self) -> bytes:
        raise AssertionError(
            "key() must not be accessed — payload-inspection invariant"
        )

    def value(self) -> bytes:
        raise AssertionError(
            "value() must not be accessed — payload-inspection invariant"
        )


class _FakeConsumer:
    """Fake confluent Consumer for unit tests."""

    def __init__(
        self,
        *,
        messages: list[_FakeMessage] | None = None,
        assignment: list[_FakeTopicPartition] | None = None,
        watermarks: dict[tuple[str, int], tuple[int, int]] | None = None,
        positions: dict[tuple[str, int], int] | None = None,
        raise_on_subscribe: bool = False,
        raise_on_list_topics: bool = False,
        raise_on_close: bool = False,
        raise_on_poll: bool = False,
        raise_on_offset_query: bool = False,
    ) -> None:
        self._messages = list(messages or [])
        self._assignment = list(assignment or [])
        self._watermarks = watermarks or {}
        self._positions = positions or {}
        self.raise_on_subscribe = raise_on_subscribe
        self.raise_on_list_topics = raise_on_list_topics
        self.raise_on_close = raise_on_close
        self.raise_on_poll = raise_on_poll
        self.raise_on_offset_query = raise_on_offset_query
        self.cfg: dict[str, str] = {}
        self.subscribed_topics: list[str] = []
        self.paused: list[_FakeTopicPartition] = []
        self.resumed: list[_FakeTopicPartition] = []
        self.closed = False
        self._poll_idx = 0

    def subscribe(self, topics: list[str]) -> None:
        if self.raise_on_subscribe:
            raise _FakeKafkaException("subscribe failed")
        self.subscribed_topics = topics

    def list_topics(self, timeout: float = 10.0) -> object:
        if self.raise_on_list_topics:
            raise _FakeKafkaException("list_topics failed")
        return object()

    def assignment(self) -> list[_FakeTopicPartition]:
        return list(self._assignment)

    def pause(self, partitions: list[_FakeTopicPartition]) -> None:
        self.paused.extend(partitions)

    def resume(self, partitions: list[_FakeTopicPartition]) -> None:
        self.resumed.extend(partitions)

    def poll(self, timeout: float = 0.1) -> _FakeMessage | None:
        if self.raise_on_poll:
            raise _FakeKafkaException("poll failed")
        if self._poll_idx >= len(self._messages):
            return None
        msg = self._messages[self._poll_idx]
        self._poll_idx += 1
        return msg

    def get_watermark_offsets(
        self, tp: _FakeTopicPartition, timeout: float = 1.0
    ) -> tuple[int, int]:
        if self.raise_on_offset_query:
            raise _FakeKafkaException("offset query failed")
        return self._watermarks.get((tp.topic, tp.partition), (0, 0))

    def position(self, tps: list[_FakeTopicPartition]) -> list[_FakeTopicPartition]:
        result = []
        for tp in tps:
            pos = self._positions.get((tp.topic, tp.partition), -1001)
            result.append(_FakeTopicPartition(tp.topic, tp.partition, pos))
        return result

    def close(self) -> None:
        if self.raise_on_close:
            raise _FakeKafkaException("close failed")
        self.closed = True


class _FakeCKAdmin(types.ModuleType):
    pass


class _FakeCKModule(types.ModuleType):
    Consumer: type[_FakeConsumer]
    KafkaException: type[_FakeKafkaException]
    admin: _FakeCKAdmin


def _make_fake_ck(consumer_cls: type[_FakeConsumer] | None = None) -> _FakeCKModule:
    if consumer_cls is None:
        consumer_cls = _FakeConsumer

    ck = _FakeCKModule("confluent_kafka")
    ck.Consumer = consumer_cls
    ck.KafkaException = _FakeKafkaException

    admin_mod = _FakeCKAdmin("confluent_kafka.admin")
    ck.admin = admin_mod
    return ck


def _install_fake_ck(
    monkeypatch: pytest.MonkeyPatch,
    fake_ck: _FakeCKModule,
) -> None:
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_ck)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", fake_ck.admin)


def _make_source(
    consumer: _FakeConsumer,
    topic_set: tuple[str, ...] = ("topic_a",),
) -> KafkaSource:
    return KafkaSource(consumer, _FakeKafkaException, topic_set)


# ---------------------------------------------------------------------------
# KafkaSource.open tests
# ---------------------------------------------------------------------------


class TestKafkaSourceOpen:
    def test_open_raises_kafka_client_unavailable_when_confluent_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """confluent-kafka absent (sys.modules[...] = None) → KafkaClientUnavailable."""
        monkeypatch.setitem(sys.modules, "confluent_kafka", None)
        with pytest.raises(KafkaClientUnavailable):
            asyncio.run(
                KafkaSource.open("localhost:9092", ("topic_a",), "grp1", "earliest")
            )

    def test_open_raises_kafka_consume_error_on_subscribe_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """subscribe() raises KafkaException → KafkaConsumeError."""
        consumer = _FakeConsumer(raise_on_subscribe=True)

        class _FailingConsumerCls:
            def __new__(cls, cfg: dict[str, str]) -> _FakeConsumer:  # type: ignore[misc]
                return consumer

        fake_ck = _make_fake_ck(_FailingConsumerCls)  # type: ignore[arg-type]
        _install_fake_ck(monkeypatch, fake_ck)
        with pytest.raises(KafkaConsumeError, match="subscribe/metadata failed"):
            asyncio.run(
                KafkaSource.open("localhost:9092", ("topic_a",), "grp1", "earliest")
            )

    def test_open_raises_kafka_consume_error_on_metadata_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """list_topics() raises KafkaException → KafkaConsumeError."""
        consumer = _FakeConsumer(raise_on_list_topics=True)

        class _FailingListTopicsCls:
            def __new__(cls, cfg: dict[str, str]) -> _FakeConsumer:  # type: ignore[misc]
                return consumer

        fake_ck = _make_fake_ck(_FailingListTopicsCls)  # type: ignore[arg-type]
        _install_fake_ck(monkeypatch, fake_ck)
        with pytest.raises(KafkaConsumeError, match="subscribe/metadata failed"):
            asyncio.run(
                KafkaSource.open("localhost:9092", ("topic_a",), "grp1", "earliest")
            )

    def test_open_subscribes_with_correct_topic_set_group_id_offset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Success: Consumer subscribed to exact topic_set with given group_id/offset."""
        consumer = _FakeConsumer()
        captured_cfg: dict[str, Any] = {}

        class _TrackingConsumerCls:
            def __new__(cls, cfg: dict[str, str]) -> _FakeConsumer:  # type: ignore[misc]
                captured_cfg.update(cfg)
                return consumer

        fake_ck = _make_fake_ck(_TrackingConsumerCls)  # type: ignore[arg-type]
        _install_fake_ck(monkeypatch, fake_ck)

        source = asyncio.run(
            KafkaSource.open(
                "broker:9092",
                ("fact_topic", "dim_topic"),
                "my-group",
                "latest",
            )
        )

        assert consumer.subscribed_topics == ["fact_topic", "dim_topic"]
        assert captured_cfg["group.id"] == "my-group"
        assert captured_cfg["auto.offset.reset"] == "latest"
        assert source._topic_set == ("fact_topic", "dim_topic")


# ---------------------------------------------------------------------------
# KafkaSource.pull tests
# ---------------------------------------------------------------------------


class TestKafkaSourcePull:
    def test_pull_never_accesses_key_or_value(self) -> None:
        """IngestedRecord built only from .topic()/.timestamp()/.offset(); key/value forbidden."""
        tp = _FakeTopicPartition("topic_a", 0)
        msg = _FakeMessage("topic_a", ts_ms=5000, offset=3)
        consumer = _FakeConsumer(
            messages=[msg],
            assignment=[tp],
        )
        source = _make_source(consumer, ("topic_a",))
        result = asyncio.run(source.pull({"topic_a": 1}))

        assert len(result["topic_a"]) == 1
        rec = result["topic_a"][0]
        assert rec.topic == "topic_a"
        assert rec.event_time_ms == 5000
        assert rec.offset == 3

    def test_pull_budget_zero_yields_empty_list(self) -> None:
        """A topic with budget 0 yields [] (its partitions paused)."""
        tp = _FakeTopicPartition("topic_a", 0)
        msg = _FakeMessage("topic_a", ts_ms=1000, offset=0)
        consumer = _FakeConsumer(
            messages=[msg],
            assignment=[tp],
        )
        source = _make_source(consumer, ("topic_a",))
        result = asyncio.run(source.pull({"topic_a": 0}))

        assert result["topic_a"] == []
        assert any(p.topic == "topic_a" for p in consumer.paused)

    def test_pull_budget_zero_pauses_partitions(self) -> None:
        """A topic with budget 0 has its partitions paused."""
        tp_a = _FakeTopicPartition("topic_a", 0)
        tp_b = _FakeTopicPartition("topic_b", 0)
        consumer = _FakeConsumer(assignment=[tp_a, tp_b])
        source = _make_source(consumer, ("topic_a", "topic_b"))

        asyncio.run(source.pull({"topic_a": 0, "topic_b": 2}))

        paused_topics = {tp.topic for tp in consumer.paused}
        assert "topic_a" in paused_topics
        assert "topic_b" not in paused_topics

    def test_pull_broker_error_raises_kafka_consume_error(self) -> None:
        """Poll returning a message with error() set → KafkaConsumeError."""
        tp = _FakeTopicPartition("topic_a", 0)
        err_msg = _FakeMessage("topic_a", ts_ms=0, offset=0, err=_FakeKafkaError())
        consumer = _FakeConsumer(
            messages=[err_msg],
            assignment=[tp],
        )
        source = _make_source(consumer, ("topic_a",))
        with pytest.raises(KafkaConsumeError, match="broker error"):
            asyncio.run(source.pull({"topic_a": 2}))

    def test_pull_records_in_broker_delivery_order(self) -> None:
        """Records returned in the order the broker delivered them."""
        tp = _FakeTopicPartition("topic_a", 0)
        msgs = [
            _FakeMessage("topic_a", ts_ms=100, offset=0),
            _FakeMessage("topic_a", ts_ms=200, offset=1),
            _FakeMessage("topic_a", ts_ms=150, offset=2),
        ]
        consumer = _FakeConsumer(messages=msgs, assignment=[tp])
        source = _make_source(consumer, ("topic_a",))
        result = asyncio.run(source.pull({"topic_a": 3}))

        assert [r.event_time_ms for r in result["topic_a"]] == [100, 200, 150]

    def test_pull_respects_per_topic_budget(self) -> None:
        """Records capped at budget; excess messages not included."""
        tp = _FakeTopicPartition("topic_a", 0)
        msgs = [_FakeMessage("topic_a", ts_ms=100, offset=i) for i in range(5)]
        consumer = _FakeConsumer(messages=msgs, assignment=[tp])
        source = _make_source(consumer, ("topic_a",))
        result = asyncio.run(source.pull({"topic_a": 2}))

        assert len(result["topic_a"]) == 2

    def test_pull_resumes_previously_paused_partitions(self) -> None:
        """Partitions paused on one tick are resumed when budget becomes > 0."""
        tp = _FakeTopicPartition("topic_a", 0)
        consumer = _FakeConsumer(assignment=[tp])
        source = _make_source(consumer, ("topic_a",))
        source._paused_topics.add("topic_a")

        asyncio.run(source.pull({"topic_a": 1}))

        assert any(p.topic == "topic_a" for p in consumer.resumed)


# ---------------------------------------------------------------------------
# KafkaSource.lag tests
# ---------------------------------------------------------------------------


class TestKafkaSourceLag:
    def test_lag_returns_end_offset_minus_position_per_topic(self) -> None:
        """lag() returns end_offset - position per topic, >= 0."""
        tp = _FakeTopicPartition("topic_a", 0)
        consumer = _FakeConsumer(
            assignment=[tp],
            watermarks={("topic_a", 0): (0, 100)},
            positions={("topic_a", 0): 40},
        )
        source = _make_source(consumer, ("topic_a",))
        result = asyncio.run(source.lag())

        assert result["topic_a"] == 60

    def test_lag_is_non_negative(self) -> None:
        """lag() clamps to >= 0; position > high is treated as 0."""
        tp = _FakeTopicPartition("topic_a", 0)
        consumer = _FakeConsumer(
            assignment=[tp],
            watermarks={("topic_a", 0): (0, 10)},
            positions={("topic_a", 0): 20},
        )
        source = _make_source(consumer, ("topic_a",))
        result = asyncio.run(source.lag())

        assert result["topic_a"] == 0

    def test_lag_raises_on_offset_query_failure(self) -> None:
        """get_watermark_offsets raising KafkaException → KafkaConsumeError."""
        tp = _FakeTopicPartition("topic_a", 0)
        consumer = _FakeConsumer(
            assignment=[tp],
            raise_on_offset_query=True,
        )
        source = _make_source(consumer, ("topic_a",))
        with pytest.raises(KafkaConsumeError, match="offset query failed"):
            asyncio.run(source.lag())

    def test_lag_keys_for_all_topics(self) -> None:
        """lag() always has a key for every topic in topic_set (0 if no assignment)."""
        consumer = _FakeConsumer(assignment=[])
        source = _make_source(consumer, ("topic_a", "topic_b"))
        result = asyncio.run(source.lag())

        assert "topic_a" in result
        assert "topic_b" in result
        assert result["topic_a"] == 0
        assert result["topic_b"] == 0


# ---------------------------------------------------------------------------
# KafkaSource.aclose tests
# ---------------------------------------------------------------------------


class TestKafkaSourceAclose:
    def test_aclose_closes_consumer(self) -> None:
        """aclose() calls consumer.close()."""
        consumer = _FakeConsumer()
        source = _make_source(consumer)
        asyncio.run(source.aclose())

        assert consumer.closed

    def test_aclose_is_idempotent(self) -> None:
        """Calling aclose() twice does not raise and close() called only once."""
        close_count = 0

        class _CountingConsumer(_FakeConsumer):
            def close(self) -> None:
                nonlocal close_count
                close_count += 1

        consumer = _CountingConsumer()
        source = _make_source(consumer)
        asyncio.run(source.aclose())
        asyncio.run(source.aclose())

        assert close_count == 1

    def test_aclose_raises_on_close_error(self) -> None:
        """consumer.close() raising KafkaException → KafkaConsumeError."""
        consumer = _FakeConsumer(raise_on_close=True)
        source = _make_source(consumer)
        with pytest.raises(KafkaConsumeError, match="close failed"):
            asyncio.run(source.aclose())


# ---------------------------------------------------------------------------
# run_consumer tests
# ---------------------------------------------------------------------------


class _FakeSource:
    """Duck-typed fake source for run_consumer tests."""

    def __init__(
        self,
        pull_results: list[dict[str, list[IngestedRecord]]],
        lag_results: list[dict[str, int]],
    ) -> None:
        self._pull_results = iter(pull_results)
        self._lag_results = iter(lag_results)
        self.pull_calls: list[dict[str, int]] = []
        self.lag_call_count = 0

    async def pull(self, budgets: dict[str, int]) -> dict[str, list[IngestedRecord]]:
        self.pull_calls.append(dict(budgets))
        return next(self._pull_results)

    async def lag(self) -> dict[str, int]:
        self.lag_call_count += 1
        return next(self._lag_results)


def _make_run_state(
    topics: tuple[str, ...] = ("topic_a",),
    ingest_rates: tuple[float, ...] | None = None,
) -> tuple[ConsumerControlState, ConsumerState, ConsumerJobShape]:
    rs = seed_consumer_run(
        topic_set=topics,
        content="state-changes",
        nonempty_topics=topics,
        windows=(),
        joins=(),
    )
    if ingest_rates is not None:
        for dial, rate in zip(rs.control.topics, ingest_rates):
            dial.ingest_rate = rate
    return rs.control, rs.state, rs.shape


def _empty_pulled(topics: tuple[str, ...]) -> dict[str, list[IngestedRecord]]:
    return {t: [] for t in topics}


def _zero_lag(topics: tuple[str, ...]) -> dict[str, int]:
    return {t: 0 for t in topics}


class TestRunConsumer:
    def test_first_measured_delta_is_zero(self) -> None:
        """Baseline monotonic reading makes the first tick delta = 0.0."""
        topics = ("topic_a",)
        control, state, shape = _make_run_state(topics, ingest_rates=(10.0,))
        tick_count = 0

        async def counting_sleep(n: float) -> None:
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 1:
                raise asyncio.CancelledError()

        source = _FakeSource(
            pull_results=[_empty_pulled(topics)],
            lag_results=[_zero_lag(topics)],
        )

        # monotonic always returns 0.0 → delta = 0.0 on first tick
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                run_consumer(
                    source, control, state, shape, counting_sleep, lambda: 0.0, 0.1
                )
            )

        assert source.pull_calls[0] == {"topic_a": 0}

    def test_pull_budget_is_ingest_rate_times_delta_int(self) -> None:
        """budget = int(ingest_rate * delta); fractional carry forwarded to next tick."""
        topics = ("topic_a",)
        control, state, shape = _make_run_state(topics, ingest_rates=(1.5,))
        tick_count = 0

        # monotonic: 0.0 baseline, then 0.8 (delta=0.8), then 0.8 (delta=0.0)
        monotonic_values = iter([0.0, 0.8, 0.8])

        async def sleep_cancel(n: float) -> None:
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 2:
                raise asyncio.CancelledError()

        source = _FakeSource(
            pull_results=[_empty_pulled(topics), _empty_pulled(topics)],
            lag_results=[_zero_lag(topics), _zero_lag(topics)],
        )

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                run_consumer(
                    source,
                    control,
                    state,
                    shape,
                    sleep_cancel,
                    lambda: next(monotonic_values),
                    0.1,
                )
            )

        # First tick: delta=0.8, ingest_rate=1.5 → raw=1.2, budget=1, carry=0.2
        assert source.pull_calls[0] == {"topic_a": 1}
        # Second tick: delta=0.0, raw=0.0+0.2=0.2, budget=0, carry=0.2
        assert source.pull_calls[1] == {"topic_a": 0}

    def test_call_order_pull_lag_ingest_sleep(self) -> None:
        """Each tick: pull → lag → ingest → sleep(tick_seconds)."""
        topics = ("topic_a",)
        control, state, shape = _make_run_state(topics, ingest_rates=(100.0,))
        call_log: list[str] = []

        class _LoggingSource:
            async def pull(
                self, budgets: dict[str, int]
            ) -> dict[str, list[IngestedRecord]]:
                call_log.append("pull")
                return _empty_pulled(topics)

            async def lag(self) -> dict[str, int]:
                call_log.append("lag")
                return _zero_lag(topics)

        async def logging_sleep(n: float) -> None:
            call_log.append(f"sleep({n})")
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                run_consumer(
                    _LoggingSource(),  # type: ignore[arg-type]
                    control,
                    state,
                    shape,
                    logging_sleep,
                    lambda: 0.0,
                    0.25,
                )
            )

        assert call_log == ["pull", "lag", "sleep(0.25)"]

    def test_ingest_rate_zero_budget_is_zero(self) -> None:
        """An ingest_rate of 0 produces budget 0 regardless of delta."""
        topics = ("topic_a",)
        control, state, shape = _make_run_state(topics, ingest_rates=(0.0,))
        monotonic_values = iter([0.0, 1.0])
        tick_count = 0

        async def sleep_once(n: float) -> None:
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 1:
                raise asyncio.CancelledError()

        source = _FakeSource(
            pull_results=[_empty_pulled(topics)],
            lag_results=[_zero_lag(topics)],
        )

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                run_consumer(
                    source,
                    control,
                    state,
                    shape,
                    sleep_once,
                    lambda: next(monotonic_values),
                    0.1,
                )
            )

        assert source.pull_calls[0] == {"topic_a": 0}

    def test_no_drain_termination_runs_until_cancelled(self) -> None:
        """run_consumer runs indefinitely until the task is cancelled."""
        topics = ("topic_a",)
        control, state, shape = _make_run_state(topics, ingest_rates=(1.0,))
        tick_count = 0

        async def sleep_n_then_cancel(n: float) -> None:
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 5:
                raise asyncio.CancelledError()

        source = _FakeSource(
            pull_results=[_empty_pulled(topics)] * 5,
            lag_results=[_zero_lag(topics)] * 5,
        )

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                run_consumer(
                    source, control, state, shape, sleep_n_then_cancel, lambda: 0.0, 0.1
                )
            )

        assert tick_count == 5

    def test_kafka_consume_error_from_pull_propagates(self) -> None:
        """KafkaConsumeError raised by source.pull() propagates out of run_consumer."""
        topics = ("topic_a",)
        control, state, shape = _make_run_state(topics)

        class _ErrorSource:
            async def pull(
                self, budgets: dict[str, int]
            ) -> dict[str, list[IngestedRecord]]:
                raise KafkaConsumeError("poll failed")

            async def lag(self) -> dict[str, int]:
                return _zero_lag(topics)

        async def never_sleep(n: float) -> None:
            pass

        with pytest.raises(KafkaConsumeError, match="poll failed"):
            asyncio.run(
                run_consumer(
                    _ErrorSource(),  # type: ignore[arg-type]
                    control,
                    state,
                    shape,
                    never_sleep,
                    lambda: 0.0,
                    0.1,
                )
            )


# ---------------------------------------------------------------------------
# Module-level helper unit tests
# ---------------------------------------------------------------------------


class TestOpenConsumerBlocking:
    def test_raises_on_subscribe_failure(self) -> None:
        """subscribe() raising KafkaException → KafkaConsumeError."""
        consumer = _FakeConsumer(raise_on_subscribe=True)

        class _FailConsumerCls:
            def __new__(cls, cfg: dict[str, str]) -> _FakeConsumer:  # type: ignore[misc]
                return consumer

        class _FakeCK:
            Consumer = _FailConsumerCls
            KafkaException = _FakeKafkaException

        with pytest.raises(KafkaConsumeError):
            _open_consumer_blocking(_FakeCK, "localhost:9092", ["t"], "g", "earliest")

    def test_returns_consumer_on_success(self) -> None:
        """Success returns the consumer instance."""
        consumer = _FakeConsumer()

        class _SuccessConsumerCls:
            def __new__(cls, cfg: dict[str, str]) -> _FakeConsumer:  # type: ignore[misc]
                return consumer

        class _FakeCK:
            Consumer = _SuccessConsumerCls
            KafkaException = _FakeKafkaException

        result = _open_consumer_blocking(
            _FakeCK, "localhost:9092", ["t"], "g", "earliest"
        )
        assert result is consumer
        assert consumer.subscribed_topics == ["t"]
