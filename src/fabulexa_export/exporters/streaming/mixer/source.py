"""Async Kafka read-back for the mixer consumer instrument.

Provides KafkaSource: a real subscribed consumer that reads only record timing
metadata (topic, timestamp, offset) — never payload key/value. Per-topic
throttling is implemented via partition pause/resume.

Reuses _import_confluent_kafka_checked from kafka_sink (same confluent-kafka
availability check). All blocking confluent-kafka calls run in a thread executor.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fabulexa_export.errors import KafkaConsumeError
from fabulexa_export.exporters.streaming.kafka_sink import (
    _import_confluent_kafka_checked,
)
from fabulexa_export.exporters.streaming.mixer.consumer import IngestedRecord


def _build_consumer_config(
    bootstrap_servers: str,
    group_id: str,
    offset_reset: str,
) -> dict[str, str]:
    """Build the confluent-kafka Consumer configuration dict."""
    return {
        "bootstrap.servers": bootstrap_servers,
        "group.id": group_id,
        "auto.offset.reset": offset_reset,
        "enable.auto.commit": "false",
    }


def _open_consumer_blocking(
    ck: Any,
    bootstrap_servers: str,
    topic_list: list[str],
    group_id: str,
    offset_reset: str,
) -> Any:
    """Build a Consumer, subscribe to topics, and verify via metadata fetch.

    Args:
        ck: The confluent_kafka module.
        bootstrap_servers: Kafka bootstrap servers string.
        topic_list: Topics to subscribe to.
        group_id: Consumer group id.
        offset_reset: Initial offset reset policy ("earliest" or "latest").

    Returns:
        A subscribed confluent_kafka.Consumer instance.

    Raises:
        KafkaConsumeError: subscription or metadata fetch fails.
    """
    try:
        config = _build_consumer_config(bootstrap_servers, group_id, offset_reset)
        consumer = ck.Consumer(config)
        consumer.subscribe(topic_list)
        consumer.list_topics(timeout=5.0)
        return consumer
    except ck.KafkaException as exc:
        raise KafkaConsumeError(f"Kafka subscribe/metadata failed: {exc}") from exc


def _poll_records_blocking(
    consumer: Any,
    kafka_exception: Any,
    budgets: dict[str, int],
    paused_topics: set[str],
) -> dict[str, list[IngestedRecord]]:
    """Pull up to budgets[topic] records per topic via a blocking poll loop.

    Pauses partitions for topics with budget 0; resumes partitions for topics
    that were paused but now have budget > 0. Polls until all non-zero budgets
    are satisfied or the consumer returns no more messages.

    Args:
        consumer: The confluent_kafka Consumer.
        kafka_exception: The confluent_kafka.KafkaException class.
        budgets: Per-topic maximum record count this tick.
        paused_topics: Mutable set tracking currently paused topic names;
            updated in place.

    Returns:
        Per-topic list of IngestedRecord in broker delivery order.
        Topics with budget 0 always return [].

    Raises:
        KafkaConsumeError: a poll returned a broker error.
    """
    results: dict[str, list[IngestedRecord]] = {t: [] for t in budgets}

    assignment = consumer.assignment()

    zero_budget = {t for t, b in budgets.items() if b == 0}
    nonzero_budget = {t for t, b in budgets.items() if b > 0}

    to_pause = [
        tp
        for tp in assignment
        if tp.topic in zero_budget and tp.topic not in paused_topics
    ]
    to_resume = [
        tp
        for tp in assignment
        if tp.topic in nonzero_budget and tp.topic in paused_topics
    ]

    if to_pause:
        consumer.pause(to_pause)
        for tp in to_pause:
            paused_topics.add(tp.topic)
    if to_resume:
        consumer.resume(to_resume)
        for tp in to_resume:
            paused_topics.discard(tp.topic)

    remaining: dict[str, int] = {t: b for t, b in budgets.items() if b > 0}

    while remaining:
        msg = consumer.poll(timeout=0.1)

        if msg is None:
            break

        if msg.error():
            raise KafkaConsumeError(f"Kafka poll returned broker error: {msg.error()}")

        t = msg.topic()
        _ts_type, ts_ms = msg.timestamp()
        off = msg.offset()

        if t in remaining:
            results[t].append(IngestedRecord(topic=t, event_time_ms=ts_ms, offset=off))
            remaining[t] -= 1
            if remaining[t] == 0:
                del remaining[t]

    return results


def _read_lag_blocking(
    consumer: Any,
    kafka_exception: Any,
    topic_set: tuple[str, ...],
) -> dict[str, int]:
    """Read per-topic broker backlog: end_offset - position, >= 0.

    Args:
        consumer: The confluent_kafka Consumer.
        kafka_exception: The confluent_kafka.KafkaException class.
        topic_set: The subscribed topics.

    Returns:
        Per-topic lag dict (key for every topic in topic_set; 0 if no partition data).

    Raises:
        KafkaConsumeError: an offset query failed.
    """
    lag: dict[str, int] = {t: 0 for t in topic_set}
    assignment = consumer.assignment()

    for tp in assignment:
        if tp.topic not in lag:
            continue
        try:
            _low, high = consumer.get_watermark_offsets(tp, timeout=1.0)
            pos_list = consumer.position([tp])
            pos = pos_list[0].offset if pos_list else 0
            if pos < 0:
                pos = 0
            lag[tp.topic] += max(0, high - pos)
        except kafka_exception as exc:
            raise KafkaConsumeError(f"Kafka offset query failed: {exc}") from exc

    return lag


def _close_consumer_blocking(
    consumer: Any,
    kafka_exception: Any,
) -> None:
    """Close the consumer (leave the group).

    Args:
        consumer: The confluent_kafka Consumer.
        kafka_exception: The confluent_kafka.KafkaException class.

    Raises:
        KafkaConsumeError: close reported an error.
    """
    try:
        consumer.close()
    except kafka_exception as exc:
        raise KafkaConsumeError(f"Kafka consumer close failed: {exc}") from exc


class KafkaSource:
    """Async Kafka read-back for the consumer: a real subscribed consumer.

    Reuses the confluent availability check; owns a Consumer subscribed to
    the topic set. Reads only message .topic()/.timestamp()/.offset() — never
    .key()/.value(). Per-topic throttling is realized by pausing partitions
    whose tick budget is exhausted.
    """

    def __init__(
        self,
        consumer: Any,
        kafka_exception: Any,
        topic_set: tuple[str, ...],
    ) -> None:
        self._consumer = consumer
        self._kafka_exception = kafka_exception
        self._topic_set = topic_set
        self._paused_topics: set[str] = set()
        self._closed = False

    @classmethod
    async def open(
        cls,
        bootstrap_servers: str,
        topic_set: tuple[str, ...],
        group_id: str,
        offset_reset: Literal["earliest", "latest"],
    ) -> "KafkaSource":
        """Import confluent, build the Consumer, subscribe to the topic set.

        Raises:
            KafkaClientUnavailable: confluent-kafka is not importable.
            KafkaConsumeError: subscription / metadata fetch fails.
        """
        ck = _import_confluent_kafka_checked()
        loop = asyncio.get_running_loop()
        consumer = await loop.run_in_executor(
            None,
            _open_consumer_blocking,
            ck,
            bootstrap_servers,
            list(topic_set),
            group_id,
            offset_reset,
        )
        return cls(consumer, ck.KafkaException, topic_set)

    async def pull(self, budgets: dict[str, int]) -> dict[str, list[IngestedRecord]]:
        """Pull up to budgets[topic] records per topic this tick (poll off-loop).

        Returns a per-topic list of IngestedRecord (timing metadata only), in broker
        delivery order. A topic with budget 0 (paused / nothing available) yields [].

        Raises:
            KafkaConsumeError: a poll returned a broker error.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            _poll_records_blocking,
            self._consumer,
            self._kafka_exception,
            budgets,
            self._paused_topics,
        )

    async def lag(self) -> dict[str, int]:
        """Read each topic's real backlog (end_offset - position), >= 0.

        Raises:
            KafkaConsumeError: an offset query failed.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            _read_lag_blocking,
            self._consumer,
            self._kafka_exception,
            self._topic_set,
        )

    async def aclose(self) -> None:
        """Close the consumer (leave the group). Idempotent.

        Raises:
            KafkaConsumeError: close reported an error.
        """
        if self._closed:
            return
        self._closed = True
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            _close_consumer_blocking,
            self._consumer,
            self._kafka_exception,
        )
