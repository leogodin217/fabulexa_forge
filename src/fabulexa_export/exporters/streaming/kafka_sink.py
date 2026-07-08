"""Kafka sink for the streaming exporter.

Produces the event stream to a Kafka broker, one message per event. Format-agnostic:
the value bytes come from the caller's render_value function. Bootstrap resolution
follows CLI → config block → environment precedence, mirroring resolve_effective_anchor
and resolve_clock (Principle #7: no invented default).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from fabulexa_export.errors import (
    KafkaBootstrapUnresolvable,
    KafkaClientUnavailable,
    KafkaDeliveryError,
)
from fabulexa_export.exporters.streaming.encoding import encode_pinned

if TYPE_CHECKING:
    from fabulexa_export.anchor import EffectiveAnchor
    from fabulexa_export.config.models import KafkaConfig
    from fabulexa_export.exporters.streaming.types import StreamEvent, StreamOutcome

_KAFKA_BOOTSTRAP_UNRESOLVABLE_MSG = (
    "sink 'kafka' requires a bootstrap-servers address; set --bootstrap-servers,"
    " a kafka.bootstrap_servers config block, or FABEXPORT_KAFKA_BOOTSTRAP"
)


def resolve_bootstrap_servers(
    config_kafka: KafkaConfig | None,
    cli_bootstrap_servers: str | None,
    env_bootstrap_servers: str | None,
) -> str:
    """Resolve the one effective Kafka bootstrap-servers string for a run.

    CLI-wins precedence, mirroring resolve_effective_anchor / resolve_clock:
    --bootstrap-servers, then the config `kafka` block, then the
    FABEXPORT_KAFKA_BOOTSTRAP environment value. There is no hard-coded default — a
    bootstrap endpoint is environment-specific and the package invents none (Principle
    #7).

    Empty/blank is absent. A CLI flag or environment value that is empty or
    whitespace-only is treated as not supplied for precedence and resolution falls
    through to the next source — `--bootstrap-servers ''` does not "win" and then raise;
    it yields to a valid `kafka` block or environment value, and only when no source
    contributes a non-blank string does this raise. (config_kafka.bootstrap_servers is
    pydantic-validated non-empty, so the config source is either absent or already
    non-blank; the raw CLI/env strings are the only way a blank reaches here.) The
    returned string is stripped.

    Args:
        config_kafka: The validated `kafka` block, or None when absent.
        cli_bootstrap_servers: The --bootstrap-servers value, or None when unset; an
            empty/whitespace-only value is treated as unset (falls through).
        env_bootstrap_servers: The FABEXPORT_KAFKA_BOOTSTRAP value, or None when unset
            (the CLI reads os.environ and passes it; the function reads no environment
            itself, for testability); an empty/whitespace-only value is treated as unset
            (falls through).

    Returns:
        The resolved non-empty bootstrap-servers string.

    Raises:
        KafkaBootstrapUnresolvable: None of CLI, config block, or environment supplies a
            non-blank bootstrap-servers string (empty/whitespace-only sources count as
            absent).
    """
    if cli_bootstrap_servers is not None and cli_bootstrap_servers.strip():
        return cli_bootstrap_servers.strip()
    if config_kafka is not None:
        return config_kafka.bootstrap_servers
    if env_bootstrap_servers is not None and env_bootstrap_servers.strip():
        return env_bootstrap_servers.strip()
    raise KafkaBootstrapUnresolvable(_KAFKA_BOOTSTRAP_UNRESOLVABLE_MSG)


def _import_confluent_kafka_checked() -> Any:
    """Import confluent_kafka, treating sys.modules[...]=None as absent.

    Returns:
        The confluent_kafka module.

    Raises:
        KafkaClientUnavailable: confluent-kafka is not importable or is None.
    """
    sentinel = object()
    mod = sys.modules.get("confluent_kafka", sentinel)
    if mod is sentinel:
        # Not in sys.modules at all — try importing
        try:
            import confluent_kafka
            import confluent_kafka.admin  # ensures .admin is registered on the parent

            return confluent_kafka
        except ImportError:
            raise KafkaClientUnavailable(
                "confluent-kafka is not installed; install the 'kafka' extra:"
                " pip install fabulexa-forge[kafka]"
            ) from None
    if mod is None:
        raise KafkaClientUnavailable(
            "confluent-kafka is not installed; install the 'kafka' extra:"
            " pip install fabulexa-forge[kafka]"
        )
    # confluent_kafka is already in sys.modules; ensure admin submodule is loaded.
    # Sentinel distinguishes "missing" from "explicitly blocked" (None).
    admin_sentinel = object()
    admin_mod = sys.modules.get("confluent_kafka.admin", admin_sentinel)
    if admin_mod is admin_sentinel:
        try:
            import confluent_kafka.admin  # noqa: F401
        except ImportError:
            raise KafkaClientUnavailable(
                "confluent-kafka is not installed; install the 'kafka' extra:"
                " pip install fabulexa-forge[kafka]"
            ) from None
    elif admin_mod is None:
        raise KafkaClientUnavailable(
            "confluent-kafka is not installed; install the 'kafka' extra:"
            " pip install fabulexa-forge[kafka]"
        )
    else:
        # admin is in sys.modules but may not be linked as an attribute on the
        # parent module (e.g. confluent_kafka was loaded first, then admin was
        # injected into sys.modules without being imported). Link it explicitly
        # so that ck.admin attribute access works regardless of import order.
        setattr(mod, "admin", admin_mod)
    return mod


def _ensure_topics(
    AdminClient: Any,
    NewTopic: Any,
    KafkaException: Any,
    bootstrap_servers: str,
    topic_set: tuple[str, ...],
) -> None:
    """Create all topics with 1 partition / RF 1, validate pre-existing ones.

    A topic that already exists with exactly 1 partition is used as-is. A topic
    with a partition count other than 1 raises KafkaDeliveryError naming the topic
    and partition count.

    Args:
        AdminClient: The confluent_kafka.admin.AdminClient class.
        NewTopic: The confluent_kafka.admin.NewTopic class.
        KafkaException: The confluent_kafka.KafkaException class.
        bootstrap_servers: The resolved bootstrap-servers string.
        topic_set: The full topic set to create/validate.

    Raises:
        KafkaDeliveryError: Topic creation fails or a pre-existing topic has ≠ 1
            partition.
    """
    admin = AdminClient({"bootstrap.servers": bootstrap_servers})
    new_topics = [
        NewTopic(t, num_partitions=1, replication_factor=1) for t in topic_set
    ]
    futures = admin.create_topics(new_topics)
    for topic, future in futures.items():
        try:
            future.result()
        except KafkaException as exc:
            # TOPIC_ALREADY_EXISTS (error code 36) → check partition count
            err = exc.args[0]
            if err.code() == 36:  # TOPIC_ALREADY_EXISTS
                meta = admin.list_topics(timeout=10)
                if topic in meta.topics:
                    part_count = len(meta.topics[topic].partitions)
                    if part_count != 1:
                        raise KafkaDeliveryError(
                            f"topic {topic!r} already exists with {part_count}"
                            f" partition(s); exactly 1 required"
                        ) from exc
                # else: exists with 1 partition — use as-is
            else:
                raise KafkaDeliveryError(
                    f"failed to create topic {topic!r}: {exc}"
                ) from exc


def _make_delivery_callback(errors: list[str]) -> Callable[..., None]:
    """Return a delivery callback that appends error descriptions to errors.

    Args:
        errors: Mutable list; the callback appends to it on failure.

    Returns:
        A callable suitable for confluent_kafka Producer.produce's on_delivery.
    """

    def _on_delivery(err: Any, msg: Any) -> None:
        if err is not None:
            errors.append(str(err))

    return _on_delivery


def write_kafka_stream(
    events: Iterable[StreamEvent],
    render_value: Callable[[StreamEvent], bytes],
    anchor: EffectiveAnchor,
    bootstrap_servers: str,
    topic_set: tuple[str, ...],
    paced: bool,
) -> StreamOutcome:
    """Produce the event stream to Kafka, one message per event.

    Format-agnostic: the value bytes come from render_value (built by the driver from
    the selected format), so this sink holds no jsonl/debezium knowledge. Pre-creates
    every topic in topic_set (1 partition, replication factor 1, idempotent) before the
    first produce, configures an ordered idempotent fully-acked producer, produces each
    event keyed by encode_pinned({"record_id": event.record_id}) (UTF-8) with record
    timestamp rebased_epoch_ms(event.event_sim_time, anchor), and flushes (blocks on all
    acks) before returning. A topic that receives zero events still appears in
    events_per_topic with count 0.

    paced=True serves delivery incrementally as each event arrives (the pacer governs
    arrival); paced=False produces all events then flushes once. Produced keys, values,
    timestamps, topics, and per-partition order are identical across both modes.

    confluent-kafka (Producer + admin AdminClient/NewTopic) is imported lazily inside
    this function only; the import never runs at package import time.

    Args:
        events: The merged, seq-stamped event stream in canonical order, already wrapped
            by the pacer when the run is realtime.
        render_value: Per-event value serializer producing the pinned-encoded message
            bytes (no trailing newline); built by the driver from the format.
        anchor: The resolved effective anchor (the driver guarantees non-None for the
            kafka sink); used for the epoch-millisecond record timestamp.
        bootstrap_servers: The resolved non-empty bootstrap-servers string.
        topic_set: The full enumerated topic set; every entry is created and seeded with
            a zero count.
        paced: True for incremental delivery under a realtime clock; False for unpaced.

    Returns:
        The StreamOutcome (total and per-topic message counts).

    Raises:
        KafkaClientUnavailable: confluent-kafka is not importable (the `kafka` extra is
            not installed).
        KafkaDeliveryError: connection, topic creation, produce, or flush fails; a
            delivery callback reports failure; flush leaves unacked messages; or a
            pre-existing topic has a partition count other than 1.
    """
    from fabulexa_export.exporters.streaming.debezium import rebased_epoch_ms
    from fabulexa_export.exporters.streaming.types import StreamOutcome

    ck = _import_confluent_kafka_checked()
    Producer = ck.Producer
    AdminClient = ck.admin.AdminClient
    NewTopic = ck.admin.NewTopic
    KafkaException = ck.KafkaException

    _ensure_topics(AdminClient, NewTopic, KafkaException, bootstrap_servers, topic_set)

    counts: dict[str, int] = {t: 0 for t in topic_set}
    delivery_errors: list[str] = []
    on_delivery = _make_delivery_callback(delivery_errors)

    producer = Producer(
        {
            "bootstrap.servers": bootstrap_servers,
            "enable.idempotence": True,
            "acks": "all",
        }
    )

    for event in events:
        if delivery_errors:
            raise KafkaDeliveryError(f"Kafka delivery failure: {delivery_errors[0]}")
        key_bytes = encode_pinned({"record_id": event.record_id}).encode("utf-8")
        value_bytes = render_value(event)
        timestamp_ms = rebased_epoch_ms(event.event_sim_time, anchor)

        producer.produce(
            event.topic,
            key=key_bytes,
            value=value_bytes,
            timestamp=timestamp_ms,
            on_delivery=on_delivery,
        )
        counts[event.topic] += 1

        if paced:
            producer.poll(0)

    unacked = producer.flush()
    if delivery_errors:
        raise KafkaDeliveryError(f"Kafka delivery failure: {delivery_errors[0]}")
    if unacked:
        raise KafkaDeliveryError(f"Kafka flush left {unacked} unacked message(s)")

    return StreamOutcome(
        total_events=sum(counts.values()),
        events_per_topic=counts,
    )
