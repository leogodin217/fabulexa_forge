"""Tests for resolve_bootstrap_servers, write_kafka_stream, and Kafka error hierarchy."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from fabulexa_forge.errors import (
    ExporterError,
    ExportRuntimeError,
    KafkaBootstrapUnresolvable,
    KafkaClientUnavailable,
    KafkaDeliveryError,
)
from fabulexa_forge.exporters.streaming.kafka_sink import resolve_bootstrap_servers

from ._helpers import make_anchor as _shared_make_anchor

# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


def test_kafka_bootstrap_unresolvable_is_exporter_error() -> None:
    """KafkaBootstrapUnresolvable subclasses ExporterError."""
    assert issubclass(KafkaBootstrapUnresolvable, ExporterError)


def test_kafka_client_unavailable_is_exporter_error() -> None:
    """KafkaClientUnavailable subclasses ExporterError."""
    assert issubclass(KafkaClientUnavailable, ExporterError)


def test_kafka_delivery_error_is_export_runtime_error() -> None:
    """KafkaDeliveryError subclasses ExportRuntimeError."""
    assert issubclass(KafkaDeliveryError, ExportRuntimeError)


def test_kafka_delivery_error_is_exporter_error() -> None:
    """KafkaDeliveryError (via ExportRuntimeError) subclasses ExporterError."""
    assert issubclass(KafkaDeliveryError, ExporterError)


# ---------------------------------------------------------------------------
# resolve_bootstrap_servers — precedence
# ---------------------------------------------------------------------------


def test_cli_wins_over_config_and_env() -> None:
    """CLI bootstrap wins over config block and env variable."""
    from fabulexa_forge.config.models import KafkaConfig

    config = KafkaConfig(bootstrap_servers="config:9092")
    result = resolve_bootstrap_servers(
        config_kafka=config,
        cli_bootstrap_servers="cli:9092",
        env_bootstrap_servers="env:9092",
    )
    assert result == "cli:9092"


def test_config_wins_over_env_when_no_cli() -> None:
    """Config block bootstrap wins over env when CLI is absent."""
    from fabulexa_forge.config.models import KafkaConfig

    config = KafkaConfig(bootstrap_servers="config:9092")
    result = resolve_bootstrap_servers(
        config_kafka=config,
        cli_bootstrap_servers=None,
        env_bootstrap_servers="env:9092",
    )
    assert result == "config:9092"


def test_env_used_when_only_env_set() -> None:
    """Env bootstrap is used when CLI and config are both absent."""
    result = resolve_bootstrap_servers(
        config_kafka=None,
        cli_bootstrap_servers=None,
        env_bootstrap_servers="env:9092",
    )
    assert result == "env:9092"


def test_returned_value_is_stripped() -> None:
    """The returned bootstrap string is stripped of leading/trailing whitespace."""
    result = resolve_bootstrap_servers(
        config_kafka=None,
        cli_bootstrap_servers="  cli:9092  ",
        env_bootstrap_servers=None,
    )
    assert result == "cli:9092"


def test_env_value_is_stripped() -> None:
    """Env bootstrap string is stripped when returned."""
    result = resolve_bootstrap_servers(
        config_kafka=None,
        cli_bootstrap_servers=None,
        env_bootstrap_servers="  env:9092  ",
    )
    assert result == "env:9092"


def test_config_value_is_stripped() -> None:
    """Config-block bootstrap string is stripped when returned.

    Regression guard: the config branch used to return the raw pydantic value
    unstripped, unlike the CLI and env branches — contradicting the docstring's
    'the returned string is stripped' promise. A YAML value with stray
    whitespace (multi-line block, copy-paste) must normalize identically to the
    same value supplied via --bootstrap-servers or FABEXPORT_KAFKA_BOOTSTRAP.
    """
    from fabulexa_forge.config.models import KafkaConfig

    config = KafkaConfig(bootstrap_servers="  config:9092  ")
    result = resolve_bootstrap_servers(
        config_kafka=config,
        cli_bootstrap_servers=None,
        env_bootstrap_servers=None,
    )
    assert result == "config:9092"


# ---------------------------------------------------------------------------
# Blank fall-through
# ---------------------------------------------------------------------------


def test_blank_cli_falls_through_to_config() -> None:
    """A blank (whitespace-only) CLI value falls through to the config block."""
    from fabulexa_forge.config.models import KafkaConfig

    config = KafkaConfig(bootstrap_servers="config:9092")
    result = resolve_bootstrap_servers(
        config_kafka=config,
        cli_bootstrap_servers="   ",
        env_bootstrap_servers=None,
    )
    assert result == "config:9092"


def test_empty_cli_falls_through_to_config() -> None:
    """An empty CLI string falls through to the config block."""
    from fabulexa_forge.config.models import KafkaConfig

    config = KafkaConfig(bootstrap_servers="config:9092")
    result = resolve_bootstrap_servers(
        config_kafka=config,
        cli_bootstrap_servers="",
        env_bootstrap_servers=None,
    )
    assert result == "config:9092"


def test_blank_env_falls_through_raises_when_no_config() -> None:
    """A blank env value with no config → KafkaBootstrapUnresolvable."""
    with pytest.raises(KafkaBootstrapUnresolvable):
        resolve_bootstrap_servers(
            config_kafka=None,
            cli_bootstrap_servers=None,
            env_bootstrap_servers="   ",
        )


def test_blank_cli_falls_through_to_env() -> None:
    """A blank CLI value falls through to env bootstrap."""
    result = resolve_bootstrap_servers(
        config_kafka=None,
        cli_bootstrap_servers="",
        env_bootstrap_servers="env:9092",
    )
    assert result == "env:9092"


def test_all_blank_raises_unresolvable() -> None:
    """All blank sources → KafkaBootstrapUnresolvable."""
    with pytest.raises(KafkaBootstrapUnresolvable):
        resolve_bootstrap_servers(
            config_kafka=None,
            cli_bootstrap_servers="",
            env_bootstrap_servers="  ",
        )


def test_all_absent_raises_unresolvable() -> None:
    """All absent sources → KafkaBootstrapUnresolvable."""
    with pytest.raises(KafkaBootstrapUnresolvable):
        resolve_bootstrap_servers(
            config_kafka=None,
            cli_bootstrap_servers=None,
            env_bootstrap_servers=None,
        )


def test_unresolvable_message_is_descriptive() -> None:
    """KafkaBootstrapUnresolvable message names the fix options."""
    with pytest.raises(KafkaBootstrapUnresolvable, match="bootstrap-servers"):
        resolve_bootstrap_servers(
            config_kafka=None,
            cli_bootstrap_servers=None,
            env_bootstrap_servers=None,
        )


# ---------------------------------------------------------------------------
# Fake confluent_kafka helpers
# ---------------------------------------------------------------------------


class _FakeKafkaError:
    """Minimal stand-in for confluent_kafka.KafkaError."""

    def __init__(self, code: int) -> None:
        self._code = code

    def code(self) -> int:
        return self._code


class _FakeKafkaException(Exception):
    """Minimal stand-in for confluent_kafka.KafkaException."""

    def __init__(self, error: _FakeKafkaError) -> None:
        super().__init__(error)

    def code(self) -> int:
        return self.args[0].code()


def _make_topic_future(
    exc: Exception | None = None,
) -> MagicMock:
    """Return a mock future for create_topics / delete_topics."""
    f: MagicMock = MagicMock()
    if exc is None:
        f.result.return_value = None
    else:
        f.result.side_effect = exc
    return f


class _FakeNewTopic:
    """Records the args passed to NewTopic(...)."""

    def __init__(
        self, topic: str, num_partitions: int, replication_factor: int
    ) -> None:
        self.topic = topic
        self.num_partitions = num_partitions
        self.replication_factor = replication_factor


class _FakeAdminClient:
    """Minimal AdminClient for unit tests."""

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


ProduceTuple = tuple[str, bytes, bytes, int]


class _FakeProducer:
    """Minimal Producer: records produce() calls and simulates flush()."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.produced: list[ProduceTuple] = []
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
        # Call pending callbacks successfully
        for cb, msg in self._on_delivery_callbacks:
            cb(None, msg)
        self._on_delivery_callbacks.clear()
        return self._flush_unacked


class _FakeDeliveryError:
    def __init__(self, msg: str) -> None:
        self._msg = msg

    def __str__(self) -> str:
        return self._msg


class _FakeCKAdmin(types.ModuleType):
    """Typed stub for the confluent_kafka.admin submodule."""

    AdminClient: type[_FakeAdminClient]
    NewTopic: type[_FakeNewTopic]
    KafkaException: type[_FakeKafkaException]


class _FakeCKModule(types.ModuleType):
    """Typed stub for the confluent_kafka top-level module."""

    Producer: type[_FakeProducer]
    KafkaException: type[_FakeKafkaException]
    admin: _FakeCKAdmin


def _make_fake_ck(
    producer_cls: type[_FakeProducer] | None = None,
    admin_cls: type[_FakeAdminClient] | None = None,
) -> _FakeCKModule:
    """Build a fake confluent_kafka module with sub-module admin."""
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


# ---------------------------------------------------------------------------
# Fixtures and helpers for write_kafka_stream tests
# ---------------------------------------------------------------------------


def _make_anchor() -> Any:
    """Return a minimal EffectiveAnchor for write_kafka_stream tests."""
    return _shared_make_anchor()


def _render_key(event: Any) -> bytes:
    """The render surface's key-bytes shape: encode_pinned({key_column: key_value})."""
    from fabulexa_forge.exporters.streaming.encoding import encode_pinned

    return encode_pinned({event.key_column: event.key_value}).encode("utf-8")


def _render_timestamp(event: Any) -> int:
    """The render surface's timestamp shape: rebased_epoch_ms under a fixed anchor."""
    from fabulexa_forge.exporters.streaming.debezium import rebased_epoch_ms

    return rebased_epoch_ms(event.event_sim_time, _make_anchor())


def _make_event(
    seq: int,
    record_id: str,
    topic: str,
    op: str = "c",
    event_sim_time: int = 0,
) -> Any:
    """Return a minimal StreamEvent."""
    from fabulexa_forge.exporters.streaming.types import StreamEvent

    return StreamEvent(
        seq=seq,
        op=op,  # type: ignore[arg-type]
        kind="entity",
        record_id=record_id,
        event_sim_time=event_sim_time,
        ts="2024-01-01T00:00:00+00:00",
        after={"record_id": record_id},
        topic=topic,
        route_table="entity",
        key_column="record_id",
        key_value=record_id,
    )


def _render_value(event: Any) -> bytes:
    """Minimal render_value: returns the record_id as JSON bytes."""
    import json

    return json.dumps({"record_id": event.record_id}).encode("utf-8")


def _run_write(
    events: list[Any],
    topic_set: tuple[str, ...],
    paced: bool,
    fake_ck: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, list[_FakeAdminClient], list[_FakeProducer]]:
    """Run write_kafka_stream with a fake confluent_kafka, return outcome + spies."""
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    admins: list[_FakeAdminClient] = []
    producers: list[_FakeProducer] = []

    class _SpyAdmin(_FakeAdminClient):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(cfg)
            admins.append(self)

    class _SpyProducer(_FakeProducer):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(cfg)
            producers.append(self)

    spy_ck = _make_fake_ck(producer_cls=_SpyProducer, admin_cls=_SpyAdmin)

    monkeypatch.setitem(sys.modules, "confluent_kafka", spy_ck)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", spy_ck.admin)

    outcome = write_kafka_stream(
        events=events,
        render_value=_render_value,
        render_key=_render_key,
        render_timestamp=_render_timestamp,
        bootstrap_servers="localhost:9092",
        topic_set=topic_set,
        paced=paced,
    )
    return outcome, admins, producers


# ---------------------------------------------------------------------------
# write_kafka_stream — topic creation
# ---------------------------------------------------------------------------


def test_all_topics_created_before_first_produce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every topic in topic_set is created with num_partitions=1, replication_factor=1."""
    events = [_make_event(1, "r1", "topic_a"), _make_event(2, "r2", "topic_b")]
    topic_set = ("topic_a", "topic_b", "topic_empty")

    outcome, admins, producers = _run_write(
        events, topic_set, paced=False, fake_ck=_make_fake_ck(), monkeypatch=monkeypatch
    )

    assert len(admins) == 1
    created_names = {nt.topic for nt in admins[0].created_topics}
    assert created_names == set(topic_set)
    for nt in admins[0].created_topics:
        assert nt.num_partitions == 1
        assert nt.replication_factor == 1


def test_declared_but_empty_topic_in_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared-but-empty topic appears in events_per_topic with count 0."""
    events = [_make_event(1, "r1", "topic_a")]
    topic_set = ("topic_a", "topic_empty")

    outcome, _, _ = _run_write(
        events, topic_set, paced=False, fake_ck=_make_fake_ck(), monkeypatch=monkeypatch
    )

    assert outcome.events_per_topic["topic_empty"] == 0
    assert outcome.events_per_topic["topic_a"] == 1
    assert outcome.total_events == 1


# ---------------------------------------------------------------------------
# write_kafka_stream — per-event produce tuples
# ---------------------------------------------------------------------------


def test_produce_topic_is_event_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Produce topic equals event.topic."""
    events = [_make_event(1, "r1", "my_topic")]
    outcome, _, producers = _run_write(
        events,
        ("my_topic",),
        paced=False,
        fake_ck=_make_fake_ck(),
        monkeypatch=monkeypatch,
    )

    assert len(producers[0].produced) == 1
    topic, _, _, _ = producers[0].produced[0]
    assert topic == "my_topic"


def test_produce_key_is_record_id_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Produce key = render_key(event)'s output, verbatim: encode_pinned({"record_id": ...})."""
    from fabulexa_forge.exporters.streaming.encoding import encode_pinned

    events = [_make_event(1, "abc-123", "topic_a")]
    outcome, _, producers = _run_write(
        events,
        ("topic_a",),
        paced=False,
        fake_ck=_make_fake_ck(),
        monkeypatch=monkeypatch,
    )

    _, key, _, _ = producers[0].produced[0]
    expected_key = encode_pinned({"record_id": "abc-123"}).encode("utf-8")
    assert key == expected_key


def test_produce_key_never_presentation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Produce key (render_key's output) is record_id, never presentation_id."""
    import json

    events = [_make_event(1, "r1", "topic_a")]
    outcome, _, producers = _run_write(
        events,
        ("topic_a",),
        paced=False,
        fake_ck=_make_fake_ck(),
        monkeypatch=monkeypatch,
    )

    _, key, _, _ = producers[0].produced[0]
    key_obj = json.loads(key.decode("utf-8"))
    assert "presentation_id" not in key_obj
    assert key_obj["record_id"] == "r1"


def test_produce_key_for_delete_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete (op='d') events also produce render_key's encode_pinned({"record_id": ...})."""
    from fabulexa_forge.exporters.streaming.encoding import encode_pinned

    events = [_make_event(1, "r1", "topic_a", op="d")]
    outcome, _, producers = _run_write(
        events,
        ("topic_a",),
        paced=False,
        fake_ck=_make_fake_ck(),
        monkeypatch=monkeypatch,
    )

    _, key, _, _ = producers[0].produced[0]
    expected_key = encode_pinned({"record_id": "r1"}).encode("utf-8")
    assert key == expected_key


def test_produce_value_is_render_value_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Produce value = render_value(event)."""
    events = [_make_event(1, "r1", "topic_a")]
    outcome, _, producers = _run_write(
        events,
        ("topic_a",),
        paced=False,
        fake_ck=_make_fake_ck(),
        monkeypatch=monkeypatch,
    )

    _, _, value, _ = producers[0].produced[0]
    assert value == _render_value(events[0])


def test_produce_timestamp_is_rebased_epoch_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Produce timestamp = render_timestamp(event)'s output, verbatim: rebased_epoch_ms."""
    from fabulexa_forge.exporters.streaming.debezium import rebased_epoch_ms

    sim_time = 5_000_000_000  # 5 seconds in nanoseconds
    events = [_make_event(1, "r1", "topic_a", event_sim_time=sim_time)]
    anchor = _make_anchor()

    outcome, _, producers = _run_write(
        events,
        ("topic_a",),
        paced=False,
        fake_ck=_make_fake_ck(),
        monkeypatch=monkeypatch,
    )

    _, _, _, ts = producers[0].produced[0]
    assert ts == rebased_epoch_ms(sim_time, anchor)


# ---------------------------------------------------------------------------
# write_kafka_stream — producer config
# ---------------------------------------------------------------------------


def test_producer_is_idempotent_acks_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Producer is configured with enable.idempotence=True and acks=all."""
    events: list[Any] = []
    outcome, _, producers = _run_write(
        events,
        ("topic_a",),
        paced=False,
        fake_ck=_make_fake_ck(),
        monkeypatch=monkeypatch,
    )

    assert len(producers) == 1
    assert producers[0].cfg["enable.idempotence"] is True
    assert producers[0].cfg["acks"] == "all"


def test_flush_called_before_return(monkeypatch: pytest.MonkeyPatch) -> None:
    """flush() is called before write_kafka_stream returns."""
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    flush_called = False

    class _FlushSpy(_FakeProducer):
        def flush(self) -> int:
            nonlocal flush_called
            flush_called = True
            return super().flush()

    spy_ck = _make_fake_ck(producer_cls=_FlushSpy)
    monkeypatch.setitem(sys.modules, "confluent_kafka", spy_ck)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", spy_ck.admin)

    write_kafka_stream(
        events=[],
        render_value=_render_value,
        render_key=_render_key,
        render_timestamp=_render_timestamp,
        bootstrap_servers="localhost:9092",
        topic_set=("t",),
        paced=False,
    )
    assert flush_called


def test_unacked_at_flush_raises_delivery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unacked message count at flush raises KafkaDeliveryError."""
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    class _UnackedProducer(_FakeProducer):
        def flush(self) -> int:
            return 3  # 3 unacked

    spy_ck = _make_fake_ck(producer_cls=_UnackedProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", spy_ck)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", spy_ck.admin)

    with pytest.raises(KafkaDeliveryError, match="unacked"):
        write_kafka_stream(
            events=[],
            render_value=_render_value,
            render_key=_render_key,
            render_timestamp=_render_timestamp,
            bootstrap_servers="localhost:9092",
            topic_set=("t",),
            paced=False,
        )


def test_delivery_callback_error_raises_delivery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivery callback reporting error → KafkaDeliveryError."""
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    class _ErrorProducer(_FakeProducer):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(cfg)
            self._delivery_error = "broker connection lost"

    spy_ck = _make_fake_ck(producer_cls=_ErrorProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", spy_ck)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", spy_ck.admin)

    with pytest.raises(KafkaDeliveryError):
        write_kafka_stream(
            events=[_make_event(1, "r1", "t")],
            render_value=_render_value,
            render_key=_render_key,
            render_timestamp=_render_timestamp,
            bootstrap_servers="localhost:9092",
            topic_set=("t",),
            paced=False,
        )


def test_loop_entry_delivery_error_raised_before_second_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delivery error registered during first produce fires at loop-entry for second event.

    Covers the early-exit guard inside the event loop (kafka_sink.py line ~276):
    ``if delivery_errors: raise KafkaDeliveryError(...)`` fires at the TOP of the
    loop before the second event is produced, not at flush time.  This is distinct
    from the flush-time check tested by test_delivery_callback_error_raises_delivery_error
    (single event) and test_unacked_at_flush_raises_delivery_error.
    """
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    class _ImmediateErrorProducer(_FakeProducer):
        """Producer whose first produce() immediately fires the error callback."""

        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(cfg)
            self._delivery_error = "broker connection lost"

    spy_ck = _make_fake_ck(producer_cls=_ImmediateErrorProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", spy_ck)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", spy_ck.admin)

    # Two events: first produce() fires the callback immediately → delivery_errors
    # is non-empty before the second iteration → loop-entry raise fires.
    with pytest.raises(KafkaDeliveryError):
        write_kafka_stream(
            events=[_make_event(1, "r1", "t"), _make_event(2, "r2", "t")],
            render_value=_render_value,
            render_key=_render_key,
            render_timestamp=_render_timestamp,
            bootstrap_servers="localhost:9092",
            topic_set=("t",),
            paced=False,
        )


# ---------------------------------------------------------------------------
# write_kafka_stream — pre-existing topic handling
# ---------------------------------------------------------------------------


def test_preexisting_topic_wrong_partitions_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing topic with partition count ≠ 1 → KafkaDeliveryError naming topic."""
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    already_exists_error = _FakeKafkaException(_FakeKafkaError(36))

    class _PreexistAdmin(_FakeAdminClient):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(
                cfg,
                topic_futures={"multi_topic": _make_topic_future(already_exists_error)},
                existing_partitions={"multi_topic": 3},
            )

    spy_ck = _make_fake_ck(admin_cls=_PreexistAdmin)
    monkeypatch.setitem(sys.modules, "confluent_kafka", spy_ck)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", spy_ck.admin)

    with pytest.raises(KafkaDeliveryError, match="multi_topic"):
        write_kafka_stream(
            events=[],
            render_value=_render_value,
            render_key=_render_key,
            render_timestamp=_render_timestamp,
            bootstrap_servers="localhost:9092",
            topic_set=("multi_topic",),
            paced=False,
        )


def test_preexisting_topic_wrong_partitions_message_includes_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KafkaDeliveryError for wrong partition count names the partition count."""
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    already_exists_error = _FakeKafkaException(_FakeKafkaError(36))

    class _PreexistAdmin(_FakeAdminClient):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(
                cfg,
                topic_futures={"my_topic": _make_topic_future(already_exists_error)},
                existing_partitions={"my_topic": 5},
            )

    spy_ck = _make_fake_ck(admin_cls=_PreexistAdmin)
    monkeypatch.setitem(sys.modules, "confluent_kafka", spy_ck)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", spy_ck.admin)

    with pytest.raises(KafkaDeliveryError, match="5"):
        write_kafka_stream(
            events=[],
            render_value=_render_value,
            render_key=_render_key,
            render_timestamp=_render_timestamp,
            bootstrap_servers="localhost:9092",
            topic_set=("my_topic",),
            paced=False,
        )


def test_preexisting_topic_one_partition_used_as_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing topic with exactly 1 partition is used as-is (no error)."""
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    already_exists_error = _FakeKafkaException(_FakeKafkaError(36))

    class _PreexistAdmin(_FakeAdminClient):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(
                cfg,
                topic_futures={"good_topic": _make_topic_future(already_exists_error)},
                existing_partitions={"good_topic": 1},
            )

    spy_ck = _make_fake_ck(admin_cls=_PreexistAdmin)
    monkeypatch.setitem(sys.modules, "confluent_kafka", spy_ck)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", spy_ck.admin)

    outcome = write_kafka_stream(
        events=[],
        render_value=_render_value,
        render_key=_render_key,
        render_timestamp=_render_timestamp,
        bootstrap_servers="localhost:9092",
        topic_set=("good_topic",),
        paced=False,
    )
    assert outcome.events_per_topic["good_topic"] == 0


def test_preexisting_topic_absent_from_metadata_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TOPIC_ALREADY_EXISTS but topic absent from metadata → fail closed.

    The broker reports the topic as already existing, yet the re-read cluster
    metadata omits it, so the partition count is unverifiable. The guard must
    raise rather than fall through to acceptance — an unchecked topic reaching
    the producer would silently void the per-topic ordering guarantee.
    """
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    already_exists_error = _FakeKafkaException(_FakeKafkaError(36))

    class _GhostAdmin(_FakeAdminClient):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(
                cfg,
                topic_futures={"ghost_topic": _make_topic_future(already_exists_error)},
                # existing_partitions deliberately empty: list_topics omits the topic
            )

    spy_ck = _make_fake_ck(admin_cls=_GhostAdmin)
    monkeypatch.setitem(sys.modules, "confluent_kafka", spy_ck)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", spy_ck.admin)

    with pytest.raises(
        KafkaDeliveryError, match="'ghost_topic'.*absent from cluster metadata"
    ):
        write_kafka_stream(
            events=[],
            render_value=_render_value,
            render_key=_render_key,
            render_timestamp=_render_timestamp,
            bootstrap_servers="localhost:9092",
            topic_set=("ghost_topic",),
            paced=False,
        )


# ---------------------------------------------------------------------------
# write_kafka_stream — generic topic-creation failure
# ---------------------------------------------------------------------------


def test_topic_creation_generic_failure_raises_delivery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-already-exists KafkaException during topic creation → KafkaDeliveryError.

    Covers the generic branch of _ensure_topics (err.code() != 36): a genuine
    creation failure — broker unreachable, auth failure — must surface as
    KafkaDeliveryError naming the topic and chained from the KafkaException,
    not propagate as the raw confluent_kafka exception and not route through
    the TOPIC_ALREADY_EXISTS partition-count check.
    """
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    # Code 7 (REQUEST_TIMED_OUT) — any code other than 36 takes the generic branch.
    creation_error = _FakeKafkaException(_FakeKafkaError(7))

    class _FailingAdmin(_FakeAdminClient):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(
                cfg,
                topic_futures={"bad_topic": _make_topic_future(creation_error)},
            )

    spy_ck = _make_fake_ck(admin_cls=_FailingAdmin)
    monkeypatch.setitem(sys.modules, "confluent_kafka", spy_ck)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", spy_ck.admin)

    with pytest.raises(
        KafkaDeliveryError, match="failed to create topic 'bad_topic'"
    ) as exc_info:
        write_kafka_stream(
            events=[],
            render_value=_render_value,
            render_key=_render_key,
            render_timestamp=_render_timestamp,
            bootstrap_servers="localhost:9092",
            topic_set=("bad_topic",),
            paced=False,
        )
    assert exc_info.value.__cause__ is creation_error


# ---------------------------------------------------------------------------
# write_kafka_stream — paced vs unpaced produce identical tuples
# ---------------------------------------------------------------------------


def test_paced_and_unpaced_produce_identical_tuples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """paced=True and paced=False produce identical (topic, key, value, timestamp) tuples."""
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    events = [
        _make_event(1, "r1", "topic_a", event_sim_time=0),
        _make_event(2, "r2", "topic_b", event_sim_time=1_000_000),
    ]
    topic_set = ("topic_a", "topic_b")

    unpaced_producers: list[_FakeProducer] = []
    paced_producers: list[_FakeProducer] = []

    class _TrackProducer(_FakeProducer):
        pass

    for paced, tracker in [(False, unpaced_producers), (True, paced_producers)]:

        class _TrackedProducer(_FakeProducer):
            def __init__(self, cfg: dict[str, Any]) -> None:
                super().__init__(cfg)
                tracker.append(self)

        spy_ck = _make_fake_ck(producer_cls=_TrackedProducer)
        monkeypatch.setitem(sys.modules, "confluent_kafka", spy_ck)
        monkeypatch.setitem(sys.modules, "confluent_kafka.admin", spy_ck.admin)

        write_kafka_stream(
            events=events,
            render_value=_render_value,
            render_key=_render_key,
            render_timestamp=_render_timestamp,
            bootstrap_servers="localhost:9092",
            topic_set=topic_set,
            paced=paced,
        )

    assert len(unpaced_producers) == 1
    assert len(paced_producers) == 1
    assert unpaced_producers[0].produced == paced_producers[0].produced


# ---------------------------------------------------------------------------
# write_kafka_stream — poll(0) on every iteration; BufferError surfacing
# ---------------------------------------------------------------------------


class _PollCountingProducer(_FakeProducer):
    """Producer that counts poll() calls."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        self.poll_count = 0

    def poll(self, timeout: float) -> int:
        self.poll_count += 1
        return super().poll(timeout)


@pytest.mark.parametrize("paced", [False, True])
def test_poll_called_every_iteration_regardless_of_paced(
    monkeypatch: pytest.MonkeyPatch, paced: bool
) -> None:
    """producer.poll(0) runs once per produced event in BOTH paced and unpaced runs.

    Regression guard: poll(0) used to run only when paced=True, so an unpaced run
    (--fast / no clock) never serviced the local delivery-report queue and
    producer.produce() would eventually raise BufferError once librdkafka's
    queue.buffering.max.messages filled.
    """
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    producers: list[_PollCountingProducer] = []

    class _SpyPollProducer(_PollCountingProducer):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__(cfg)
            producers.append(self)

    spy_ck = _make_fake_ck(producer_cls=_SpyPollProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", spy_ck)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", spy_ck.admin)

    events = [_make_event(i, f"r{i}", "t") for i in range(1, 4)]
    write_kafka_stream(
        events=events,
        render_value=_render_value,
        render_key=_render_key,
        render_timestamp=_render_timestamp,
        bootstrap_servers="localhost:9092",
        topic_set=("t",),
        paced=paced,
    )

    assert len(producers) == 1
    assert producers[0].poll_count == len(events)


@pytest.mark.parametrize("paced", [False, True])
def test_produce_buffererror_raises_delivery_error(
    monkeypatch: pytest.MonkeyPatch, paced: bool
) -> None:
    """A BufferError from produce() (local queue full) → KafkaDeliveryError.

    Regression guard: the raw confluent_kafka BufferError used to propagate
    uncaught, contradicting the docstring's promise that a produce failure
    surfaces as KafkaDeliveryError.
    """
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    class _FullQueueProducer(_FakeProducer):
        def produce(
            self,
            topic: str,
            key: bytes,
            value: bytes,
            timestamp: int,
            on_delivery: Callable[..., None],
        ) -> None:
            raise BufferError("Local: Queue full")

    spy_ck = _make_fake_ck(producer_cls=_FullQueueProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", spy_ck)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", spy_ck.admin)

    with pytest.raises(KafkaDeliveryError, match="queue is full") as exc_info:
        write_kafka_stream(
            events=[_make_event(1, "r1", "t")],
            render_value=_render_value,
            render_key=_render_key,
            render_timestamp=_render_timestamp,
            bootstrap_servers="localhost:9092",
            topic_set=("t",),
            paced=paced,
        )
    assert isinstance(exc_info.value.__cause__, BufferError)


# ---------------------------------------------------------------------------
# write_kafka_stream — KafkaClientUnavailable
# ---------------------------------------------------------------------------


def test_client_unavailable_when_confluent_kafka_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KafkaClientUnavailable raised when sys.modules['confluent_kafka'] = None."""
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    monkeypatch.setitem(sys.modules, "confluent_kafka", None)  # type: ignore[arg-type]

    with pytest.raises(KafkaClientUnavailable):
        write_kafka_stream(
            events=[],
            render_value=_render_value,
            render_key=_render_key,
            render_timestamp=_render_timestamp,
            bootstrap_servers="localhost:9092",
            topic_set=("t",),
            paced=False,
        )


def test_client_unavailable_when_confluent_kafka_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KafkaClientUnavailable raised when confluent_kafka is absent from sys.modules
    and the real import raises ImportError (the package is not installed).

    This covers the ImportError branch in _import_confluent_kafka_checked — distinct
    from the sys.modules[...]=None branch tested above.  We simulate a missing package
    by removing confluent_kafka from sys.modules entirely and inserting a meta_path
    finder that raises ImportError for it.
    """
    import importlib.abc
    import importlib.machinery

    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    class _BlockConfluent(importlib.abc.MetaPathFinder):
        def find_spec(
            self,
            fullname: str,
            path: object,
            target: object = None,
        ) -> importlib.machinery.ModuleSpec | None:
            if fullname in ("confluent_kafka", "confluent_kafka.admin"):
                raise ImportError(f"No module named {fullname!r}")
            return None

    blocker = _BlockConfluent()
    # Remove confluent_kafka from sys.modules so the sentinel branch fires
    monkeypatch.delitem(sys.modules, "confluent_kafka", raising=False)
    monkeypatch.delitem(sys.modules, "confluent_kafka.admin", raising=False)
    # Insert blocker at the front so it takes priority over the real finders
    sys.meta_path.insert(0, blocker)
    try:
        with pytest.raises(KafkaClientUnavailable):
            write_kafka_stream(
                events=[],
                render_value=_render_value,
                render_key=_render_key,
                render_timestamp=_render_timestamp,
                bootstrap_servers="localhost:9092",
                topic_set=("t",),
                paced=False,
            )
    finally:
        sys.meta_path.remove(blocker)


# ---------------------------------------------------------------------------
# Regression: admin submodule import order (GH-XXX)
# ---------------------------------------------------------------------------


def test_write_kafka_stream_does_not_require_pre_imported_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """write_kafka_stream works even when confluent_kafka.admin is not yet imported.

    Regression guard: the old code did ``ck.admin.AdminClient`` which fails with
    AttributeError when ``confluent_kafka.admin`` has never been imported in the
    current process (the .admin attribute is not set on the parent module until
    the submodule is explicitly imported).  The fix must import admin itself so
    AdminClient resolves regardless of import order.

    We simulate "admin not yet imported" by injecting a fake confluent_kafka
    module without a pre-set .admin attribute, then injecting the real fake
    admin into sys.modules['confluent_kafka.admin']. Python's import machinery
    links the submodule onto the parent when ``import confluent_kafka.admin``
    runs — exactly what _import_confluent_kafka_checked now does. The old code
    (bare attribute access) would raise AttributeError here.
    """
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    # Build a fake confluent_kafka WITHOUT .admin attribute pre-set
    ck_no_admin = _FakeCKModule("confluent_kafka")
    ck_no_admin.Producer = _FakeProducer
    ck_no_admin.KafkaException = _FakeKafkaException
    # Deliberately do NOT set ck_no_admin.admin

    # Build the fake admin submodule separately
    admin_mod = _FakeCKAdmin("confluent_kafka.admin")
    admin_mod.AdminClient = _FakeAdminClient
    admin_mod.NewTopic = _FakeNewTopic
    admin_mod.KafkaException = _FakeKafkaException

    # Inject parent WITHOUT admin attribute, and inject admin into sys.modules
    monkeypatch.setitem(sys.modules, "confluent_kafka", ck_no_admin)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", admin_mod)

    # This must NOT raise AttributeError. Old code: ck.admin.AdminClient fails.
    # New code: _import_confluent_kafka_checked runs `import confluent_kafka.admin`
    # which links admin_mod onto ck_no_admin.admin, so the attribute access works.
    outcome = write_kafka_stream(
        events=[],
        render_value=_render_value,
        render_key=_render_key,
        render_timestamp=_render_timestamp,
        bootstrap_servers="localhost:9092",
        topic_set=("t",),
        paced=False,
    )
    assert outcome.total_events == 0
