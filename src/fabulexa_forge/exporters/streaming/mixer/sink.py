"""Async Kafka sink for the mixer control plane.

Produces the event stream to a Kafka broker asynchronously, one message per
event. Reuses the streaming sink's building blocks — confluent availability
check, topic pre-creation, record_id keying, the rebased_epoch_ms record
timestamp, the delivery-error callback, and the idempotent fully-acked
producer — but owns its own incremental produce/poll loop (no
drain-to-exhaustion, no single terminal flush), because a live run releases
in operator-driven bursts and runs indefinitely.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fabulexa_forge.errors import KafkaDeliveryError
from fabulexa_forge.exporters.streaming.debezium import rebased_epoch_ms
from fabulexa_forge.exporters.streaming.encoding import encode_pinned
from fabulexa_forge.exporters.streaming.kafka_sink import (
    _ensure_topics,
    _import_confluent_kafka_checked,
    _make_delivery_callback,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.exporters.streaming.types import StreamEvent


def _build_producer(ck: Any, bootstrap_servers: str) -> Any:
    """Build an idempotent fully-acked confluent_kafka Producer.

    Args:
        ck: The confluent_kafka module.
        bootstrap_servers: The resolved bootstrap-servers string.

    Returns:
        A configured confluent_kafka.Producer instance.
    """
    return ck.Producer(
        {
            "bootstrap.servers": bootstrap_servers,
            "enable.idempotence": True,
            "acks": "all",
        }
    )


def _produce_and_poll(
    producer: Any,
    event_topic: str,
    key_bytes: bytes,
    value_bytes: bytes,
    timestamp_ms: int,
    on_delivery: "Callable[[Any, Any], None]",
) -> None:
    """Produce one message and poll for delivery callbacks."""
    producer.produce(
        event_topic,
        key=key_bytes,
        value=value_bytes,
        timestamp=timestamp_ms,
        on_delivery=on_delivery,
    )
    producer.poll(0)


def _flush_and_close(producer: Any) -> int:
    """Flush all outstanding messages and return the unacked count."""
    return int(producer.flush())


class KafkaSink:
    """Async Kafka delivery for the mixer: one message per event.

    Produce/poll is offloaded to a thread executor; flush+close on shutdown.

    Reuses the streaming sink's building blocks — confluent availability
    check, topic pre-creation, record_id keying, the rebased_epoch_ms record
    timestamp, the delivery-error callback, and the idempotent fully-acked
    producer — but owns its own incremental produce/poll loop (no
    drain-to-exhaustion, no single terminal flush), because a live run
    releases in operator-driven bursts and runs indefinitely.
    """

    def __init__(
        self,
        producer: Any,
        render_value: "Callable[[StreamEvent], bytes]",
        anchor: "EffectiveAnchor",
        delivery_errors: list[str],
    ) -> None:
        self._producer = producer
        self._render_value = render_value
        self._anchor = anchor
        self._delivery_errors = delivery_errors
        self._closed = False

    @classmethod
    async def open(
        cls,
        bootstrap_servers: str,
        topic_set: tuple[str, ...],
        render_value: "Callable[[StreamEvent], bytes]",
        anchor: "EffectiveAnchor",
    ) -> "KafkaSink":
        """Import confluent, pre-create the topic set, and create the producer.

        Topic creation (1 partition / RF 1, idempotent, validates a
        pre-existing topic has exactly 1 partition) runs in a thread executor.

        Raises:
            KafkaClientUnavailable: confluent-kafka is not importable.
            KafkaDeliveryError: topic creation fails, a pre-existing topic
                has != 1 partition, or a topic reported as already existing
                is absent from cluster metadata (count unverifiable).
        """
        ck = _import_confluent_kafka_checked()
        loop = asyncio.get_running_loop()
        AdminClient = ck.admin.AdminClient
        NewTopic = ck.admin.NewTopic
        KafkaException = ck.KafkaException
        await loop.run_in_executor(
            None,
            _ensure_topics,
            AdminClient,
            NewTopic,
            KafkaException,
            bootstrap_servers,
            topic_set,
        )
        delivery_errors: list[str] = []
        producer = _build_producer(ck, bootstrap_servers)
        return cls(producer, render_value, anchor, delivery_errors)

    async def deliver(self, event: "StreamEvent") -> None:
        """Produce one event (the injected schedule_releases sink).

        Keys the message encode_pinned({"record_id": event.record_id})
        (UTF-8), values it render_value(event), timestamps it
        rebased_epoch_ms(event.event_sim_time, anchor), produces to
        event.topic, and polls (0). The blocking produce+poll runs in a
        thread executor. Checks the delivery-error list before and after;
        a reported failure raises.

        Raises:
            KafkaDeliveryError: a delivery callback reported a failure.
        """
        if self._delivery_errors:
            raise KafkaDeliveryError(
                f"Kafka delivery failure: {self._delivery_errors[0]}"
            )

        key_bytes = encode_pinned({"record_id": event.record_id}).encode("utf-8")
        value_bytes = self._render_value(event)
        timestamp_ms = rebased_epoch_ms(event.event_sim_time, self._anchor)
        on_delivery = _make_delivery_callback(self._delivery_errors)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            _produce_and_poll,
            self._producer,
            event.topic,
            key_bytes,
            value_bytes,
            timestamp_ms,
            on_delivery,
        )

        if self._delivery_errors:
            raise KafkaDeliveryError(
                f"Kafka delivery failure: {self._delivery_errors[0]}"
            )

    async def aclose(self) -> None:
        """Flush (block on all outstanding acks) and close the producer.

        Flush runs in a thread executor. Idempotent; safe after a drained
        or cancelled run.

        Raises:
            KafkaDeliveryError: flush leaves unacked messages, or a callback
                reported failure.
        """
        if self._closed:
            return
        self._closed = True
        delivery_errors = self._delivery_errors

        loop = asyncio.get_running_loop()
        unacked = await loop.run_in_executor(None, _flush_and_close, self._producer)

        if delivery_errors:
            raise KafkaDeliveryError(f"Kafka delivery failure: {delivery_errors[0]}")
        if unacked:
            raise KafkaDeliveryError(f"Kafka flush left {unacked} unacked message(s)")
