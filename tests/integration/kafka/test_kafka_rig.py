"""Black-box validator for the streaming export Kafka mechanics.

Phase 1 tests (canned-envelope path): produce the canned Debezium envelopes to a
fresh single-partition topic (via the ``rig`` fixture) and assert a guarantee from
the envelope contract holds end-to-end on a real broker.

Phase 2 tests (write_kafka_stream path): drive ``write_kafka_stream`` end-to-end
against a fresh single-partition topic and assert the sink guarantees (key = record_id,
seq/lsn monotonic, upsert-log shape, rebased timestamp, declared-but-empty topic).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from exporters.streaming._helpers import make_anchor as _shared_make_anchor

from ._envelopes import CANNED_ENVELOPES
from ._harness import (
    RigRunner,
    consume,
    create_single_partition_topic,
    delete_topic,
)

pytestmark = pytest.mark.kafka


# ---------------------------------------------------------------------------
# Phase 1: canned-envelope tests (harness-only, no package code)
# ---------------------------------------------------------------------------


def test_key_is_record_id(rig: RigRunner) -> None:
    """Every message key is exactly {record_id} and matches its after-image id."""
    consumed = rig(CANNED_ENVELOPES)
    assert len(consumed) == len(CANNED_ENVELOPES)
    for msg in consumed:
        assert set(msg.key) == {"record_id"}
        assert msg.key["record_id"] == msg.value["after"]["record_id"]


def test_single_partition_preserves_global_seq_order(rig: RigRunner) -> None:
    """source.lsn (the derived seq) is strictly increasing in consume order."""
    consumed = rig(CANNED_ENVELOPES)
    seqs = [msg.value["source"]["lsn"] for msg in consumed]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_stream_is_an_upsert_log(rig: RigRunner) -> None:
    """First message per record_id is op:c, the rest op:u; before is always null."""
    consumed = rig(CANNED_ENVELOPES)
    seen: set[str] = set()
    for msg in consumed:
        record_id = msg.value["after"]["record_id"]
        op = msg.value["op"]
        if record_id not in seen:
            assert op == "c", f"first event for {record_id} should be a create"
            seen.add(record_id)
        else:
            assert op == "u", f"later event for {record_id} should be an update"
        assert msg.value["before"] is None


def test_message_timestamp_is_rebased_event_time(rig: RigRunner) -> None:
    """The Kafka record timestamp equals payload ts_ms (== source.ts_ms), not now()."""
    consumed = rig(CANNED_ENVELOPES)
    for msg in consumed:
        assert msg.timestamp_ms == msg.value["ts_ms"]
        assert msg.value["ts_ms"] == msg.value["source"]["ts_ms"]


def test_schemas_enable_toggles_the_envelope_wrapper(rig: RigRunner) -> None:
    """schemas.enable=false → bare payload; =true → {schema, payload} wrapper."""
    bare = rig(CANNED_ENVELOPES, schemas_enable=False)
    for msg in bare:
        assert "schema" not in msg.value
        assert msg.value["op"] in {"c", "u"}

    wrapped = rig(CANNED_ENVELOPES, schemas_enable=True)
    for msg in wrapped:
        assert set(msg.value) >= {"schema", "payload"}
        assert msg.value["payload"]["op"] in {"c", "u"}


def test_key_is_never_schema_wrapped(rig: RigRunner) -> None:
    """The message key is always bare {record_id}, never schema-wrapped."""
    wrapped = rig(CANNED_ENVELOPES, schemas_enable=True)
    for msg in wrapped:
        # Key must be bare {record_id}, not {schema, payload}
        assert set(msg.key) == {"record_id"}
        assert "schema" not in msg.key
        assert "payload" not in msg.key


# ---------------------------------------------------------------------------
# Phase 2: write_kafka_stream end-to-end fixtures
# ---------------------------------------------------------------------------


def _make_anchor() -> Any:
    """Return a minimal EffectiveAnchor for integration tests."""
    return _shared_make_anchor()


def _make_stream_event(
    seq: int,
    record_id: str,
    topic: str,
    op: str = "c",
    event_sim_time: int = 0,
) -> Any:
    """Return a minimal StreamEvent for integration tests."""
    from fabulexa_forge.exporters.streaming.types import StreamEvent

    return StreamEvent(
        seq=seq,
        op=op,  # type: ignore[arg-type]
        kind="entity",
        record_id=record_id,
        presentation_id=None,
        event_sim_time=event_sim_time,
        ts="2024-06-17T00:00:00+00:00",
        after={"record_id": record_id, "prop__name": f"name-{record_id}"},
        topic=topic,
        route_table="entity",
    )


def _render_jsonl(event: Any) -> bytes:
    """Minimal render_value for integration tests: produce JSON of the after-image."""
    from fabulexa_forge.exporters.streaming.debezium import rebased_epoch_ms
    from fabulexa_forge.exporters.streaming.encoding import encode_pinned

    anchor = _make_anchor()
    payload = {
        "record_id": event.record_id,
        "seq": event.seq,
        "ts_ms": rebased_epoch_ms(event.event_sim_time, anchor),
        "op": event.op,
    }
    return encode_pinned(payload).encode("utf-8")


@pytest.fixture()
def sink_topic(kafka_bootstrap: str) -> Iterator[str]:
    """A unique single-partition topic for write_kafka_stream tests."""
    topic = f"fabulexa-forge.sink.{uuid.uuid4().hex[:12]}"
    create_single_partition_topic(kafka_bootstrap, topic)
    yield topic
    delete_topic(kafka_bootstrap, topic)


@pytest.fixture()
def sink_empty_topic(kafka_bootstrap: str) -> Iterator[str]:
    """A second unique topic for declared-but-empty tests."""
    topic = f"fabulexa-forge.empty.{uuid.uuid4().hex[:12]}"
    yield topic
    delete_topic(kafka_bootstrap, topic)


# ---------------------------------------------------------------------------
# Phase 2: write_kafka_stream integration tests
# ---------------------------------------------------------------------------


def test_sink_key_is_record_id(kafka_bootstrap: str, sink_topic: str) -> None:
    """write_kafka_stream: every key = pinned {record_id}."""
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    anchor = _make_anchor()
    events = [
        _make_stream_event(1, "r1", sink_topic, event_sim_time=0),
        _make_stream_event(2, "r2", sink_topic, event_sim_time=1_000_000_000),
    ]

    write_kafka_stream(
        events=events,
        render_value=_render_jsonl,
        anchor=anchor,
        bootstrap_servers=kafka_bootstrap,
        topic_set=(sink_topic,),
        paced=False,
    )

    consumed = consume(kafka_bootstrap, sink_topic, expected=2)
    assert len(consumed) == 2
    for msg, event in zip(consumed, events):
        assert msg.key == {"record_id": event.record_id}
        assert set(msg.key) == {"record_id"}


def test_sink_seq_monotonic_in_consume_order(
    kafka_bootstrap: str, sink_topic: str
) -> None:
    """write_kafka_stream: seq is strictly increasing in consume order."""
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    anchor = _make_anchor()
    events = [
        _make_stream_event(1, "r1", sink_topic, op="c", event_sim_time=0),
        _make_stream_event(2, "r2", sink_topic, op="c", event_sim_time=500_000_000),
        _make_stream_event(3, "r1", sink_topic, op="u", event_sim_time=1_000_000_000),
        _make_stream_event(4, "r2", sink_topic, op="u", event_sim_time=1_500_000_000),
    ]

    write_kafka_stream(
        events=events,
        render_value=_render_jsonl,
        anchor=anchor,
        bootstrap_servers=kafka_bootstrap,
        topic_set=(sink_topic,),
        paced=False,
    )

    consumed = consume(kafka_bootstrap, sink_topic, expected=4)
    seqs = [msg.value["seq"] for msg in consumed]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_sink_upsert_log_shape(kafka_bootstrap: str, sink_topic: str) -> None:
    """write_kafka_stream: first event per record_id is op:c, rest are op:u."""
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    anchor = _make_anchor()
    events = [
        _make_stream_event(1, "r1", sink_topic, op="c", event_sim_time=0),
        _make_stream_event(2, "r2", sink_topic, op="c", event_sim_time=500_000_000),
        _make_stream_event(3, "r1", sink_topic, op="u", event_sim_time=1_000_000_000),
    ]

    write_kafka_stream(
        events=events,
        render_value=_render_jsonl,
        anchor=anchor,
        bootstrap_servers=kafka_bootstrap,
        topic_set=(sink_topic,),
        paced=False,
    )

    consumed = consume(kafka_bootstrap, sink_topic, expected=3)
    seen: set[str] = set()
    for msg in consumed:
        key = msg.key
        record_id = key["record_id"]
        val = msg.value
        op = val["op"]
        if record_id not in seen:
            assert op == "c", f"first event for {record_id} should be c"
            seen.add(record_id)
        else:
            assert op == "u", f"later event for {record_id} should be u"


def test_sink_record_timestamp_is_rebased_event_time(
    kafka_bootstrap: str, sink_topic: str
) -> None:
    """write_kafka_stream: record timestamp = rebased_epoch_ms(event_sim_time, anchor)."""
    from fabulexa_forge.exporters.streaming.debezium import rebased_epoch_ms
    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    anchor = _make_anchor()
    sim_time = 3_600_000_000_000  # 1 hour in nanoseconds
    events = [_make_stream_event(1, "r1", sink_topic, event_sim_time=sim_time)]

    write_kafka_stream(
        events=events,
        render_value=_render_jsonl,
        anchor=anchor,
        bootstrap_servers=kafka_bootstrap,
        topic_set=(sink_topic,),
        paced=False,
    )

    consumed = consume(kafka_bootstrap, sink_topic, expected=1)
    assert len(consumed) == 1
    expected_ts = rebased_epoch_ms(sim_time, anchor)
    assert consumed[0].timestamp_ms == expected_ts


def test_sink_declared_but_empty_topic_created(
    kafka_bootstrap: str,
    sink_topic: str,
    sink_empty_topic: str,
) -> None:
    """write_kafka_stream: a declared-but-empty topic is created and has count 0."""
    from confluent_kafka.admin import AdminClient  # type: ignore[import-untyped]

    from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

    anchor = _make_anchor()
    events = [_make_stream_event(1, "r1", sink_topic, event_sim_time=0)]

    outcome = write_kafka_stream(
        events=events,
        render_value=_render_jsonl,
        anchor=anchor,
        bootstrap_servers=kafka_bootstrap,
        topic_set=(sink_topic, sink_empty_topic),
        paced=False,
    )

    assert outcome.events_per_topic[sink_empty_topic] == 0
    assert outcome.events_per_topic[sink_topic] == 1
    assert outcome.total_events == 1

    # Verify the empty topic actually exists on the broker
    admin = AdminClient({"bootstrap.servers": kafka_bootstrap})
    meta = admin.list_topics(timeout=10)
    assert sink_empty_topic in meta.topics
