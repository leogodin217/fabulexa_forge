"""Kafka rig harness: produce canned envelopes, consume them back, black-box.

Decoupled from the package's `writers/` sink seam — it talks to a real broker
directly so the streaming export's Kafka mechanics can be validated before the sink
adapter exists. `confluent-kafka` is imported lazily inside each function so this
module imports cleanly even when the dev dependency is absent (the rig then skips,
see `conftest.py`).
"""

from __future__ import annotations

import json
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# A (key_payload, value_payload) pair; values match the Debezium envelope contract.
Envelope = tuple[dict[str, Any], dict[str, Any]]
DEFAULT_BOOTSTRAP = "localhost:9092"


@dataclass(frozen=True)
class Consumed:
    """One message read back from Kafka, decoded for assertions."""

    key: dict[str, Any]
    value: dict[str, Any]
    timestamp_ms: int


# A callable that produces the envelopes to a fresh single-partition topic and
# returns the consumed messages in broker order. Supplied by the `rig` fixture.
RigRunner = Callable[..., list[Consumed]]


def bootstrap_servers() -> str:
    """The broker address: ``FABEXPORT_KAFKA_BOOTSTRAP`` env var or the default."""
    import os

    return os.environ.get("FABEXPORT_KAFKA_BOOTSTRAP", DEFAULT_BOOTSTRAP)


def skip_reason(bootstrap: str, *, probe_timeout: float = 3.0) -> str | None:
    """Return a human reason to skip the rig, or None if it can run.

    Fast path: a closed bootstrap port short-circuits in well under a second so
    `make check` does not pay the broker connect timeout when no broker is up.
    """
    host, _, port = bootstrap.partition(":")
    try:
        with socket.create_connection((host, int(port or "9092")), timeout=0.5):
            pass
    except OSError:
        return f"Kafka broker not reachable at {bootstrap}; run `make kafka-up`"

    try:
        from confluent_kafka.admin import AdminClient
    except ImportError:
        return "confluent-kafka not installed; run `uv sync`"

    try:
        AdminClient({"bootstrap.servers": bootstrap}).list_topics(timeout=probe_timeout)
    except Exception as exc:  # broker port open but not serving metadata
        return f"Kafka metadata unavailable at {bootstrap} ({exc}); run `make kafka-up`"
    return None


def create_single_partition_topic(bootstrap: str, topic: str) -> None:
    """Create ``topic`` with exactly one partition (global-`seq` order depends on it)."""
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient({"bootstrap.servers": bootstrap})
    futures = admin.create_topics(
        [NewTopic(topic, num_partitions=1, replication_factor=1)]
    )
    futures[topic].result(timeout=15)


def delete_topic(bootstrap: str, topic: str) -> None:
    """Best-effort topic teardown; swallows errors so cleanup never fails a test."""
    from confluent_kafka.admin import AdminClient

    admin = AdminClient({"bootstrap.servers": bootstrap})
    futures = admin.delete_topics([topic])
    try:
        futures[topic].result(timeout=15)
    except Exception:
        pass


def _wrap(payload: dict[str, Any], schemas_enable: bool) -> dict[str, Any]:
    """Model the JSON converter's ``schemas.enable``: wrap in {schema, payload} or not."""
    if not schemas_enable:
        return payload
    return {
        "schema": {
            "type": "struct",
            "optional": False,
            "name": "fabulexa-forge.Envelope",
        },
        "payload": payload,
    }


def produce(
    bootstrap: str, topic: str, envelopes: list[Envelope], schemas_enable: bool
) -> None:
    """Produce envelopes to ``topic``: key = record_id, message timestamp = ts_ms.

    The message key is always the bare key payload (never schema-wrapped); only the
    value is conditionally wrapped under ``schemas_enable``.
    """
    from confluent_kafka import Producer

    producer = Producer({"bootstrap.servers": bootstrap})
    for key_payload, value_payload in envelopes:
        producer.produce(
            topic,
            key=json.dumps(key_payload).encode("utf-8"),
            value=json.dumps(_wrap(value_payload, schemas_enable)).encode("utf-8"),
            timestamp=int(value_payload["ts_ms"]),
        )
    unsent = producer.flush(15)
    if unsent:
        raise RuntimeError(f"{unsent} Kafka message(s) failed to flush to {topic}")


def consume(
    bootstrap: str, topic: str, expected: int, *, timeout: float = 20.0
) -> list[Consumed]:
    """Read ``expected`` messages from partition 0 (from the start), in broker order."""
    from confluent_kafka import Consumer, TopicPartition

    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": f"fabulexa-forge-rig-{uuid.uuid4().hex}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    consumer.assign([TopicPartition(topic, 0, 0)])
    out: list[Consumed] = []
    deadline = time.monotonic() + timeout
    try:
        while len(out) < expected and time.monotonic() < deadline:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error() is not None:
                raise RuntimeError(f"Kafka consume error on {topic}: {msg.error()}")
            _ts_type, ts_ms = msg.timestamp()
            out.append(
                Consumed(
                    key=json.loads(msg.key().decode("utf-8")),
                    value=json.loads(msg.value().decode("utf-8")),
                    timestamp_ms=ts_ms,
                )
            )
    finally:
        consumer.close()
    return out
