"""Tests for KafkaSink — the async Kafka producer for the mixer control plane."""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from fabulexa_forge.errors import KafkaClientUnavailable, KafkaDeliveryError
from fabulexa_forge.exporters.streaming.types import StreamEvent

from .._helpers import make_anchor

# ---------------------------------------------------------------------------
# Shared fake confluent_kafka infrastructure (mirrors test_kafka_sink.py)
# ---------------------------------------------------------------------------


class _FakeKafkaError:
    def __init__(self, code: int) -> None:
        self._code = code

    def code(self) -> int:
        return self._code


class _FakeKafkaException(Exception):
    def __init__(self, error: _FakeKafkaError) -> None:
        super().__init__(error)

    def code(self) -> int:
        return self.args[0].code()


def _make_topic_future(exc: Exception | None = None) -> MagicMock:
    f: MagicMock = MagicMock()
    if exc is None:
        f.result.return_value = None
    else:
        f.result.side_effect = exc
    return f


class _FakeNewTopic:
    def __init__(
        self, topic: str, num_partitions: int, replication_factor: int
    ) -> None:
        self.topic = topic
        self.num_partitions = num_partitions
        self.replication_factor = replication_factor


class _FakeAdminClient:
    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        topic_futures: dict[str, MagicMock] | None = None,
        existing_partitions: dict[str, int] | None = None,
    ) -> None:
        self.cfg = cfg
        self._topic_futures = topic_futures or {}
        self._existing_partitions = existing_partitions or {}
        self.created_topics: list[_FakeNewTopic] = []

    def create_topics(self, new_topics: list[_FakeNewTopic]) -> dict[str, MagicMock]:
        self.created_topics.extend(new_topics)
        result: dict[str, MagicMock] = {}
        for nt in new_topics:
            if nt.topic in self._topic_futures:
                result[nt.topic] = self._topic_futures[nt.topic]
            else:
                result[nt.topic] = _make_topic_future()
        return result

    def list_topics(self, timeout: float = 10.0) -> MagicMock:
        meta = MagicMock()
        topic_meta: dict[str, MagicMock] = {}
        for topic, count in self._existing_partitions.items():
            tm = MagicMock()
            tm.partitions = {i: MagicMock() for i in range(count)}
            topic_meta[topic] = tm
        meta.topics = topic_meta
        return meta


class _FakeDeliveryError:
    def __init__(self, msg: str) -> None:
        self._msg = msg

    def __str__(self) -> str:
        return self._msg


class _FakeProducer:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.produced: list[tuple[str, bytes, bytes, int]] = []
        self._flush_unacked = 0
        self._delivery_error: str | None = None
        self._on_delivery_callbacks: list[tuple[Callable[..., None], Any]] = []

    def produce(
        self,
        topic: str,
        key: bytes,
        value: bytes,
        timestamp: int,
        on_delivery: Callable[..., None],
    ) -> None:
        self.produced.append((topic, key, value, timestamp))
        if self._delivery_error is not None:
            on_delivery(_FakeDeliveryError(self._delivery_error), None)
        else:
            self._on_delivery_callbacks.append((on_delivery, None))

    def poll(self, timeout: float) -> int:
        return 0

    def flush(self) -> int:
        for cb, msg in self._on_delivery_callbacks:
            cb(None, msg)
        self._on_delivery_callbacks.clear()
        return self._flush_unacked


class _FakeCKAdmin(types.ModuleType):
    AdminClient: type[_FakeAdminClient]
    NewTopic: type[_FakeNewTopic]
    KafkaException: type[_FakeKafkaException]


class _FakeCKModule(types.ModuleType):
    Producer: type[_FakeProducer]
    KafkaException: type[_FakeKafkaException]
    admin: _FakeCKAdmin


def _make_fake_ck(
    producer_cls: type[_FakeProducer] | None = None,
    admin_cls: type[_FakeAdminClient] | None = None,
) -> _FakeCKModule:
    if producer_cls is None:
        producer_cls = _FakeProducer
    if admin_cls is None:
        admin_cls = _FakeAdminClient

    ck = _FakeCKModule("confluent_kafka")
    ck.Producer = producer_cls
    ck.KafkaException = _FakeKafkaException

    admin_mod = _FakeCKAdmin("confluent_kafka.admin")
    admin_mod.AdminClient = admin_cls
    admin_mod.NewTopic = _FakeNewTopic
    admin_mod.KafkaException = _FakeKafkaException

    ck.admin = admin_mod
    return ck


def _make_event(
    seq: int = 1,
    record_id: str = "r1",
    topic: str = "topic_a",
    event_sim_time: int = 1_000_000,
) -> StreamEvent:
    return StreamEvent(
        seq=seq,
        op="c",
        kind="entity",
        record_id=record_id,
        event_sim_time=event_sim_time,
        ts="2026-01-01T00:00:00+00:00",
        after={"record_id": record_id},
        topic=topic,
        route_table="entity",
        key_column="record_id",
        key_value=record_id,
    )


def _render_value(event: StreamEvent) -> bytes:
    import json

    return json.dumps({"record_id": event.record_id}).encode("utf-8")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _install_fake_ck(
    monkeypatch: pytest.MonkeyPatch,
    fake_ck: _FakeCKModule,
) -> None:
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_ck)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", fake_ck.admin)


# ---------------------------------------------------------------------------
# open — pre-creates topics
# ---------------------------------------------------------------------------


def test_open_pre_creates_all_topics(monkeypatch: pytest.MonkeyPatch) -> None:
    """open() pre-creates every topic with 1 partition / RF 1 via _ensure_topics."""
    from fabulexa_forge.exporters.streaming.mixer.sink import KafkaSink

    admins: list[_FakeAdminClient] = []

    class _SpyAdmin(_FakeAdminClient):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(cfg)
            admins.append(self)

    fake_ck = _make_fake_ck(admin_cls=_SpyAdmin)
    _install_fake_ck(monkeypatch, fake_ck)

    anchor = make_anchor()
    topic_set = ("topic_a", "topic_b", "topic_empty")
    sink = _run(
        KafkaSink.open(
            bootstrap_servers="localhost:9092",
            topic_set=topic_set,
            render_value=_render_value,
            anchor=anchor,
        )
    )
    assert len(admins) == 1
    created = {nt.topic for nt in admins[0].created_topics}
    assert created == set(topic_set)
    for nt in admins[0].created_topics:
        assert nt.num_partitions == 1
        assert nt.replication_factor == 1
    _run(sink.aclose())


def test_open_creates_idempotent_producer(monkeypatch: pytest.MonkeyPatch) -> None:
    """open() creates an idempotent fully-acked producer."""
    from fabulexa_forge.exporters.streaming.mixer.sink import KafkaSink

    producers: list[_FakeProducer] = []

    class _SpyProducer(_FakeProducer):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(cfg)
            producers.append(self)

    fake_ck = _make_fake_ck(producer_cls=_SpyProducer)
    _install_fake_ck(monkeypatch, fake_ck)

    anchor = make_anchor()
    sink = _run(
        KafkaSink.open(
            bootstrap_servers="localhost:9092",
            topic_set=("topic_a",),
            render_value=_render_value,
            anchor=anchor,
        )
    )
    assert len(producers) == 1
    cfg = producers[0].cfg
    assert cfg.get("enable.idempotence") is True
    assert cfg.get("acks") == "all"
    _run(sink.aclose())


def test_open_raises_kafka_client_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """open() raises KafkaClientUnavailable when confluent-kafka is not importable."""
    from fabulexa_forge.exporters.streaming.mixer.sink import KafkaSink

    monkeypatch.setitem(sys.modules, "confluent_kafka", None)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", None)

    anchor = make_anchor()
    with pytest.raises(KafkaClientUnavailable):
        _run(
            KafkaSink.open(
                bootstrap_servers="localhost:9092",
                topic_set=("topic_a",),
                render_value=_render_value,
                anchor=anchor,
            )
        )


def test_open_raises_kafka_delivery_error_on_topic_creation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """open() raises KafkaDeliveryError when topic creation fails."""
    from fabulexa_forge.exporters.streaming.mixer.sink import KafkaSink

    class _FailingAdmin(_FakeAdminClient):
        def create_topics(
            self, new_topics: list[_FakeNewTopic]
        ) -> dict[str, MagicMock]:
            err = _FakeKafkaError(5)  # non-36 error code
            result: dict[str, MagicMock] = {}
            for nt in new_topics:
                result[nt.topic] = _make_topic_future(exc=_FakeKafkaException(err))
            return result

    fake_ck = _make_fake_ck(admin_cls=_FailingAdmin)
    _install_fake_ck(monkeypatch, fake_ck)

    anchor = make_anchor()
    with pytest.raises(KafkaDeliveryError):
        _run(
            KafkaSink.open(
                bootstrap_servers="localhost:9092",
                topic_set=("topic_a",),
                render_value=_render_value,
                anchor=anchor,
            )
        )


def test_open_raises_kafka_delivery_error_on_wrong_partition_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """open() raises KafkaDeliveryError when a pre-existing topic has != 1 partition."""
    from fabulexa_forge.exporters.streaming.mixer.sink import KafkaSink

    class _MultiPartAdmin(_FakeAdminClient):
        def create_topics(
            self, new_topics: list[_FakeNewTopic]
        ) -> dict[str, MagicMock]:
            err = _FakeKafkaError(36)  # TOPIC_ALREADY_EXISTS
            result: dict[str, MagicMock] = {}
            for nt in new_topics:
                result[nt.topic] = _make_topic_future(exc=_FakeKafkaException(err))
            return result

        def list_topics(self, timeout: float = 10.0) -> MagicMock:
            meta = MagicMock()
            tm = MagicMock()
            tm.partitions = {0: MagicMock(), 1: MagicMock()}  # 2 partitions
            meta.topics = {"topic_a": tm}
            return meta

    fake_ck = _make_fake_ck(admin_cls=_MultiPartAdmin)
    _install_fake_ck(monkeypatch, fake_ck)

    anchor = make_anchor()
    with pytest.raises(KafkaDeliveryError, match="partition"):
        _run(
            KafkaSink.open(
                bootstrap_servers="localhost:9092",
                topic_set=("topic_a",),
                render_value=_render_value,
                anchor=anchor,
            )
        )


# ---------------------------------------------------------------------------
# deliver — keying, value, timestamp, topic, executor
# ---------------------------------------------------------------------------


def test_deliver_keys_by_record_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """deliver() keys the message as encode_pinned({"record_id": ...}) (UTF-8)."""
    from fabulexa_forge.exporters.streaming.encoding import encode_pinned
    from fabulexa_forge.exporters.streaming.mixer.sink import KafkaSink

    producers: list[_FakeProducer] = []

    class _SpyProducer(_FakeProducer):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(cfg)
            producers.append(self)

    fake_ck = _make_fake_ck(producer_cls=_SpyProducer)
    _install_fake_ck(monkeypatch, fake_ck)

    anchor = make_anchor()
    sink = _run(
        KafkaSink.open(
            bootstrap_servers="localhost:9092",
            topic_set=("topic_a",),
            render_value=_render_value,
            anchor=anchor,
        )
    )
    event = _make_event(record_id="my-record")
    _run(sink.deliver(event))

    expected_key = encode_pinned({"record_id": "my-record"}).encode("utf-8")
    topic, key, value, _ts = producers[0].produced[0]
    assert key == expected_key
    _run(sink.aclose())


def test_deliver_values_render_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """deliver() values the message from render_value(event)."""
    from fabulexa_forge.exporters.streaming.mixer.sink import KafkaSink

    producers: list[_FakeProducer] = []

    class _SpyProducer(_FakeProducer):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(cfg)
            producers.append(self)

    fake_ck = _make_fake_ck(producer_cls=_SpyProducer)
    _install_fake_ck(monkeypatch, fake_ck)

    anchor = make_anchor()
    sink = _run(
        KafkaSink.open(
            bootstrap_servers="localhost:9092",
            topic_set=("topic_a",),
            render_value=_render_value,
            anchor=anchor,
        )
    )
    event = _make_event(record_id="rec42")
    _run(sink.deliver(event))

    expected_value = _render_value(event)
    _, _key, value, _ts = producers[0].produced[0]
    assert value == expected_value
    _run(sink.aclose())


def test_deliver_timestamps_rebased_epoch_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """deliver() timestamps the message with rebased_epoch_ms(event.event_sim_time, anchor)."""
    from datetime import datetime, timezone

    from fabulexa_forge.exporters.streaming.debezium import rebased_epoch_ms
    from fabulexa_forge.exporters.streaming.mixer.sink import KafkaSink

    producers: list[_FakeProducer] = []

    class _SpyProducer(_FakeProducer):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(cfg)
            producers.append(self)

    fake_ck = _make_fake_ck(producer_cls=_SpyProducer)
    _install_fake_ck(monkeypatch, fake_ck)

    anchor = make_anchor(
        start_instant=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    )
    sink = _run(
        KafkaSink.open(
            bootstrap_servers="localhost:9092",
            topic_set=("topic_a",),
            render_value=_render_value,
            anchor=anchor,
        )
    )
    event = _make_event(event_sim_time=5_000_000_000)
    _run(sink.deliver(event))

    expected_ts = rebased_epoch_ms(event.event_sim_time, anchor)
    _, _key, _value, timestamp = producers[0].produced[0]
    assert timestamp == expected_ts
    _run(sink.aclose())


def test_deliver_produces_to_correct_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    """deliver() produces to event.topic."""
    from fabulexa_forge.exporters.streaming.mixer.sink import KafkaSink

    producers: list[_FakeProducer] = []

    class _SpyProducer(_FakeProducer):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(cfg)
            producers.append(self)

    fake_ck = _make_fake_ck(producer_cls=_SpyProducer)
    _install_fake_ck(monkeypatch, fake_ck)

    anchor = make_anchor()
    sink = _run(
        KafkaSink.open(
            bootstrap_servers="localhost:9092",
            topic_set=("orders",),
            render_value=_render_value,
            anchor=anchor,
        )
    )
    event = _make_event(topic="orders")
    _run(sink.deliver(event))

    topic, _key, _value, _ts = producers[0].produced[0]
    assert topic == "orders"
    _run(sink.aclose())


def test_deliver_raises_before_produce_when_delivery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deliver() raises KafkaDeliveryError before produce when error list is non-empty."""
    from fabulexa_forge.exporters.streaming.mixer.sink import KafkaSink

    fake_ck = _make_fake_ck()
    _install_fake_ck(monkeypatch, fake_ck)

    anchor = make_anchor()
    sink = _run(
        KafkaSink.open(
            bootstrap_servers="localhost:9092",
            topic_set=("topic_a",),
            render_value=_render_value,
            anchor=anchor,
        )
    )
    # Inject a pre-existing error directly into the internal list
    sink._delivery_errors.append("prior failure")

    produce_called = False
    original_produce = sink._producer.produce

    def _spy_produce(*args: Any, **kwargs: Any) -> None:
        nonlocal produce_called
        produce_called = True
        original_produce(*args, **kwargs)

    sink._producer.produce = _spy_produce  # type: ignore[method-assign]

    with pytest.raises(KafkaDeliveryError):
        _run(sink.deliver(_make_event()))

    assert not produce_called, (
        "produce() must not be called when error list is pre-populated"
    )


def test_deliver_raises_after_produce_when_delivery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deliver() raises KafkaDeliveryError after produce+poll when error is reported."""
    from fabulexa_forge.exporters.streaming.mixer.sink import KafkaSink

    producers: list[_FakeProducer] = []

    class _ImmediateErrorProducer(_FakeProducer):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(cfg)
            self._delivery_error = "delivery failure"
            producers.append(self)

    fake_ck = _make_fake_ck(producer_cls=_ImmediateErrorProducer)
    _install_fake_ck(monkeypatch, fake_ck)

    anchor = make_anchor()
    sink = _run(
        KafkaSink.open(
            bootstrap_servers="localhost:9092",
            topic_set=("topic_a",),
            render_value=_render_value,
            anchor=anchor,
        )
    )
    with pytest.raises(KafkaDeliveryError):
        _run(sink.deliver(_make_event()))


# ---------------------------------------------------------------------------
# aclose — flush, idempotent
# ---------------------------------------------------------------------------


def test_aclose_flushes_and_is_safe_after_no_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aclose() flushes the producer; safe when no events were delivered."""
    from fabulexa_forge.exporters.streaming.mixer.sink import KafkaSink

    producers: list[_FakeProducer] = []

    class _SpyProducer(_FakeProducer):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(cfg)
            self.flush_calls = 0
            producers.append(self)

        def flush(self) -> int:
            self.flush_calls += 1
            return super().flush()

    fake_ck = _make_fake_ck(producer_cls=_SpyProducer)
    _install_fake_ck(monkeypatch, fake_ck)

    anchor = make_anchor()
    sink = _run(
        KafkaSink.open(
            bootstrap_servers="localhost:9092",
            topic_set=("topic_a",),
            render_value=_render_value,
            anchor=anchor,
        )
    )
    _run(sink.aclose())
    assert producers[0].flush_calls == 1


def test_aclose_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """aclose() is safe to call twice — second call is a no-op."""
    from fabulexa_forge.exporters.streaming.mixer.sink import KafkaSink

    producers: list[_FakeProducer] = []

    class _SpyProducer(_FakeProducer):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(cfg)
            self.flush_calls = 0
            producers.append(self)

        def flush(self) -> int:
            self.flush_calls += 1
            return super().flush()

    fake_ck = _make_fake_ck(producer_cls=_SpyProducer)
    _install_fake_ck(monkeypatch, fake_ck)

    anchor = make_anchor()
    sink = _run(
        KafkaSink.open(
            bootstrap_servers="localhost:9092",
            topic_set=("topic_a",),
            render_value=_render_value,
            anchor=anchor,
        )
    )
    _run(sink.aclose())
    _run(sink.aclose())
    assert producers[0].flush_calls == 1


def test_aclose_raises_on_unacked_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """aclose() raises KafkaDeliveryError when flush leaves unacked messages."""
    from fabulexa_forge.exporters.streaming.mixer.sink import KafkaSink

    class _UnackedProducer(_FakeProducer):
        def flush(self) -> int:
            return 3  # 3 unacked messages

    fake_ck = _make_fake_ck(producer_cls=_UnackedProducer)
    _install_fake_ck(monkeypatch, fake_ck)

    anchor = make_anchor()
    sink = _run(
        KafkaSink.open(
            bootstrap_servers="localhost:9092",
            topic_set=("topic_a",),
            render_value=_render_value,
            anchor=anchor,
        )
    )
    with pytest.raises(KafkaDeliveryError, match="unacked"):
        _run(sink.aclose())
