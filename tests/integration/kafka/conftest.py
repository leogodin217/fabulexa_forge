"""Fixtures for the Kafka rig: broker-reachability skip gate + topic lifecycle.

The whole rig is gated by the ``kafka`` marker and skips itself when no broker is
reachable, so `make check` runs it as skips and stays docker-free. Run it for real
with `make kafka-it` (after `make kafka-up`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from ._harness import (
    Consumed,
    Envelope,
    RigRunner,
    bootstrap_servers,
    consume,
    create_single_partition_topic,
    delete_topic,
    produce,
    skip_reason,
)


@pytest.fixture(scope="session")
def kafka_bootstrap() -> str:
    """The reachable broker address, or skip the rig with a human reason."""
    bootstrap = bootstrap_servers()
    reason = skip_reason(bootstrap)
    if reason is not None:
        pytest.skip(reason)
    return bootstrap


@pytest.fixture()
def rig(kafka_bootstrap: str) -> Iterator[RigRunner]:
    """Produce envelopes to a fresh single-partition topic, return them consumed.

    Each call creates a uniquely-named topic and tears it down after the test, so
    runs are isolated and repeatable.
    """
    created: list[str] = []

    def _run(
        envelopes: list[Envelope], *, schemas_enable: bool = False
    ) -> list[Consumed]:
        topic = f"fabexport.rig.{uuid.uuid4().hex[:12]}"
        create_single_partition_topic(kafka_bootstrap, topic)
        created.append(topic)
        produce(kafka_bootstrap, topic, envelopes, schemas_enable)
        return consume(kafka_bootstrap, topic, expected=len(envelopes))

    yield _run

    for topic in created:
        delete_topic(kafka_bootstrap, topic)
