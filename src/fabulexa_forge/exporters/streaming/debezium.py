"""Debezium format renderer and sink for the streaming exporter.

Renders StreamEvents as Debezium value messages (the envelope + optional
schema wrapper) and writes them to stdout or one-file-per-topic under an output
directory. Pure output re-wrapping of the S1 event stream — no new content,
no new order, no new fold.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Literal

from fabulexa_forge.errors import ExportRuntimeError
from fabulexa_forge.exporters.streaming.encoding import encode_pinned
from fabulexa_forge.exporters.streaming.types import StreamOutcome

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import DebeziumSourceIdentity
    from fabulexa_forge.exporters.streaming.types import StreamEvent

# ---------------------------------------------------------------------------
# Internal helpers — schema construction
# ---------------------------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Source struct field types in the pinned serialized source order.
# Each entry: (field_name, connect_type, optional)
_SOURCE_FIELDS: list[tuple[str, str, bool]] = [
    ("version", "string", False),
    ("connector", "string", False),
    ("name", "string", False),
    ("ts_ms", "int64", False),
    ("snapshot", "string", True),
    ("db", "string", False),
    ("sequence", "string", True),
    ("schema", "string", True),
    ("table", "string", True),
    ("txId", "int64", True),
    ("lsn", "int64", True),
]


def _string_field(optional: bool) -> dict[str, object]:
    """Build a Connect string field descriptor."""
    return {"type": "string", "optional": optional}


def _int64_field(optional: bool) -> dict[str, object]:
    """Build a Connect int64 field descriptor."""
    return {"type": "int64", "optional": optional}


def _build_value_struct(
    columns: list[str],
    name: str,
    optional: bool,
) -> dict[str, object]:
    """Build an optional Connect struct for the before/after value schema."""
    fields = [{"field": col, **_string_field(True)} for col in columns]
    return {
        "type": "struct",
        "fields": fields,
        "optional": optional,
        "name": name,
    }


def _build_source_struct(connector: str) -> dict[str, object]:
    """Build the non-optional Connect source struct in the pinned field order."""
    fields = [
        {"field": name, **(_int64_field(opt) if typ == "int64" else _string_field(opt))}
        for name, typ, opt in _SOURCE_FIELDS
    ]
    return {
        "type": "struct",
        "fields": fields,
        "optional": False,
        "name": f"io.debezium.connector.{connector}.Source",
    }


def _build_transaction_struct() -> dict[str, object]:
    """Build the optional Debezium transaction struct."""
    return {
        "type": "struct",
        "fields": [
            {"field": "id", **_string_field(False)},
            {"field": "total_order", **_int64_field(False)},
            {"field": "data_collection_order", **_int64_field(False)},
        ],
        "optional": True,
        "name": "event.block",
    }


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def build_debezium_value_schema(
    table: str,
    columns: list[str],
    source_name: str,
    connector: str,
) -> dict[str, object]:
    """Build the Kafka-Connect value-schema descriptor for one table identity.

    Args:
        table: The table identity (route_table or topic per table_identity policy);
            the schema namespace component.
        columns: The carried-column names in after-image order (resolve_stream_columns).
        source_name: The source.name; the envelope/value schema namespace.
        connector: The source.connector; names the source schema struct.

    Returns:
        A JSON-serializable Connect struct schema: optional before/after structs of
        optional-string columns (named <source_name>.<table>.Value), a non-optional
        source struct, a non-optional string op, an optional int64 ts_ms, an optional
        transaction struct, named <source_name>.<table>.Envelope.
    """
    value_name = f"{source_name}.{table}.Value"
    envelope_name = f"{source_name}.{table}.Envelope"

    before_struct = _build_value_struct(columns, value_name, optional=True)
    after_struct = _build_value_struct(columns, value_name, optional=True)

    return {
        "type": "struct",
        "fields": [
            {"field": "before", **before_struct},
            {"field": "after", **after_struct},
            {"field": "source", **_build_source_struct(connector)},
            {"field": "op", **_string_field(False)},
            {"field": "ts_ms", **_int64_field(True)},
            {"field": "transaction", **_build_transaction_struct()},
        ],
        "optional": False,
        "name": envelope_name,
    }


def rebased_epoch_ms(
    event_sim_time: int,
    anchor: "EffectiveAnchor",
) -> int:
    """The rebased event instant in epoch-milliseconds.

    Computes the absolute instant in the UTC frame — the anchor's resolved start
    instant plus event_sim_time nanoseconds — in integer arithmetic and truncates
    to milliseconds: (start_instant_epoch_ns + event_sim_time) // 1_000_000.
    EffectiveAnchor carries no epoch field, so start_instant_epoch_ns is derived
    from its tz-aware start_instant without floating point — project to UTC and take
    exact integer microseconds since the Unix epoch via timedelta floor-division,
    then scale to nanoseconds:

        epoch_us = ((anchor.start_instant.astimezone(timezone.utc)
                     - datetime(1970, 1, 1, tzinfo=timezone.utc))
                    // timedelta(microseconds=1))
        return (epoch_us * 1000 + event_sim_time) // 1_000_000

    start_instant is microsecond-resolution so this is exact; datetime.timestamp()
    (float) is never used. The same absolute frame the JSONL `ts` uses (the JSONL
    path truncates to microseconds for its ISO string; the millisecond value
    agrees). Never consults the wall clock and never routes through _render_ts.

    Args:
        event_sim_time: The event-time key in nanoseconds.
        anchor: The resolved effective anchor (never None for the Debezium format).

    Returns:
        Epoch-milliseconds (UTC) of the rebased instant.
    """
    epoch_us: int = (
        anchor.start_instant.astimezone(timezone.utc) - _EPOCH
    ) // timedelta(microseconds=1)
    return (epoch_us * 1000 + event_sim_time) // 1_000_000


def _build_source_block(
    ts_ms: int,
    seq: int,
    table: str,
    source_identity: "DebeziumSourceIdentity",
) -> dict[str, object]:
    """Build the source block for one event in the pinned source key order."""
    return {
        "version": source_identity.version,
        "connector": source_identity.connector,
        "name": source_identity.name,
        "ts_ms": ts_ms,
        "snapshot": "false",
        "db": source_identity.db,
        "sequence": f'[null,"{seq}"]',
        "schema": source_identity.schema_,
        "table": table,
        "txId": None,
        "lsn": seq,
    }


def _build_envelope(
    event: "StreamEvent",
    ts_ms: int,
    source_identity: "DebeziumSourceIdentity",
    table: str,
) -> dict[str, object]:
    """Build the Debezium envelope payload in the pinned envelope key order.

    Op branches:
      - 'd'              -> before={record_id}; after=null; op='d'.
      - 'c' / 'u'        -> before=null; after=event.after; op=event.op.
      - 'join' / 'leave' -> before=null; after={'event': op, **event.after}; op='c'.
    """
    if event.op == "d":
        before: dict[str, object] | None = {"record_id": event.record_id}
        after: dict[str, object] | None = None
        envelope_op: str = event.op
    elif event.op in ("join", "leave"):
        before = None
        membership_after = event.after if event.after is not None else {}
        after = {"event": event.op, **membership_after}
        envelope_op = "c"
    else:
        before = None
        after = event.after
        envelope_op = event.op

    source = _build_source_block(ts_ms, event.seq, table, source_identity)

    return {
        "before": before,
        "after": after,
        "source": source,
        "op": envelope_op,
        "ts_ms": ts_ms,
        "transaction": None,
    }


def render_debezium_message(
    event: "StreamEvent",
    ts_ms: int,
    source_identity: "DebeziumSourceIdentity",
    table: str,
    value_schema: dict[str, object] | None,
) -> dict[str, object]:
    """Re-wrap one StreamEvent as a Debezium value message.

    Builds the envelope: before (null, or {record_id} on a delete), after (the
    event's after-image, or null on a delete), op, ts_ms, transaction (null), and
    the source block (static identity + derived ts_ms / lsn=event.seq / sequence /
    snapshot / txId / table).

    Op branches, by event.op:
      - 'd'              -> envelope op 'd'; before={record_id}; after=null.
      - 'c' / 'u'        -> envelope op = event.op; before=null; after=event.after.
      - 'join' / 'leave' -> envelope op 'c'; before=null;
                            after={'event': event.op, **event.after}.

    Args:
        event: The event to render.
        ts_ms: The rebased event time in epoch-milliseconds (rebased_epoch_ms).
        source_identity: The masquerade source identity.
        table: The table identity value (route_table or topic per table_identity).
        value_schema: The table identity's value schema when schemas are enabled;
            None for the bare payload.

    Returns:
        A JSON-serializable dict: {schema, payload} when value_schema is non-None,
        else the bare envelope payload.
    """
    envelope = _build_envelope(event, ts_ms, source_identity, table)
    if value_schema is None:
        return envelope
    return {"schema": value_schema, "payload": envelope}


def _serialize_message(obj: dict[str, object]) -> str:
    """Serialize one Debezium message with the pinned encoder settings."""
    return encode_pinned(obj) + "\n"


def _resolve_table_identity(event: "StreamEvent", table_identity: str) -> str:
    """Resolve the Debezium source.table / value-schema key for one event.

    Args:
        event: The stream event.
        table_identity: 'source_table' or 'topic'.

    Returns:
        event.route_table when table_identity='source_table', event.topic otherwise.
    """
    if table_identity == "topic":
        return event.topic
    return event.route_table


def _render_debezium_line(
    event: "StreamEvent",
    anchor: "EffectiveAnchor",
    source_identity: "DebeziumSourceIdentity",
    table_identity: str,
    value_schemas: dict[str, dict[str, object]] | None,
) -> tuple[str, str]:
    """Render one event to a serialized Debezium line and its topic.

    Args:
        event: The stream event.
        anchor: The resolved effective anchor.
        source_identity: The masquerade source identity.
        table_identity: 'source_table' or 'topic'.
        value_schemas: Value schemas keyed by table identity; None for bare payloads.

    Returns:
        A (topic, serialized_line) pair.
    """
    ts_ms = rebased_epoch_ms(event.event_sim_time, anchor)
    table = _resolve_table_identity(event, table_identity)
    schema = value_schemas[table] if value_schemas is not None else None
    msg = render_debezium_message(event, ts_ms, source_identity, table, schema)
    return event.topic, _serialize_message(msg)


def _write_debezium_stdout_paced(
    events: "Iterable[StreamEvent]",
    anchor: "EffectiveAnchor",
    source_identity: "DebeziumSourceIdentity",
    table_identity: str,
    value_schemas: dict[str, dict[str, object]] | None,
    events_per_topic: dict[str, int],
) -> int:
    """Write Debezium events to stdout with per-line flush (paced mode).

    Args:
        events: The ordered events to write.
        anchor: The resolved effective anchor.
        source_identity: The masquerade source identity.
        table_identity: 'source_table' or 'topic'.
        value_schemas: Value schemas keyed by table identity; None for bare payloads.
        events_per_topic: Mutable dict updated with per-topic counts.

    Returns:
        Total events written.
    """
    total_events = 0
    for event in events:
        topic, line = _render_debezium_line(
            event, anchor, source_identity, table_identity, value_schemas
        )
        sys.stdout.write(line)
        sys.stdout.flush()
        events_per_topic[topic] = events_per_topic.get(topic, 0) + 1
        total_events += 1
    return total_events


def _write_debezium_file_paced(
    events: "Iterable[StreamEvent]",
    out: Path,
    anchor: "EffectiveAnchor",
    source_identity: "DebeziumSourceIdentity",
    table_identity: str,
    value_schemas: dict[str, dict[str, object]] | None,
    events_per_topic: dict[str, int],
) -> int:
    """Write Debezium events to per-topic files with lazy open and per-line flush.

    Each topic's handle is opened on first event, kept open across the run, and
    closed in a finally on completion or abort. A zero-event topic opens no handle.

    Args:
        events: The ordered events to write.
        out: The output directory for topic files.
        anchor: The resolved effective anchor.
        source_identity: The masquerade source identity.
        table_identity: 'source_table' or 'topic'.
        value_schemas: Value schemas keyed by table identity; None for bare payloads.
        events_per_topic: Mutable dict updated with per-topic counts.

    Returns:
        Total events written.
    """
    import io

    handles: dict[str, io.TextIOWrapper] = {}
    total_events = 0
    try:
        for event in events:
            topic, line = _render_debezium_line(
                event, anchor, source_identity, table_identity, value_schemas
            )
            if topic not in handles:
                handles[topic] = open(  # noqa: WPS515
                    out / f"{topic}.jsonl", "w", encoding="utf-8"
                )
            handles[topic].write(line)
            handles[topic].flush()
            events_per_topic[topic] = events_per_topic.get(topic, 0) + 1
            total_events += 1
    finally:
        for handle in handles.values():
            handle.close()
    return total_events


def write_debezium_stream(
    events: "Iterable[StreamEvent]",
    sink: Literal["stdout", "file"],
    out: "Path | None",
    anchor: "EffectiveAnchor",
    source_identity: "DebeziumSourceIdentity",
    value_schemas: dict[str, dict[str, object]] | None,
    table_identity: str = "source_table",
    topic_set: tuple[str, ...] = (),
    paced: bool = False,
) -> "StreamOutcome":
    """Serialize events as newline-delimited Debezium value messages to the sink.

    Mirrors write_jsonl_stream: the same pinned deterministic encoder (UTF-8,
    compact separators, no BOM, construction order, one trailing newline), the same
    stdout-interleaved / one-file-per-topic layout, and the same per-topic counts. Per
    event, computes ts_ms via rebased_epoch_ms and renders via render_debezium_message
    with the value schema keyed by table_identity.

    Args:
        events: The ordered events to write.
        sink: 'stdout' or 'file'.
        out: The output directory for the file sink; must be None for stdout.
        anchor: The resolved effective anchor (the driver guarantees non-None).
        source_identity: The masquerade source identity.
        value_schemas: Value schemas keyed by table_identity (route_table or topic)
            when schemas are enabled; None for bare payloads across the run.
        table_identity: 'source_table' (default) or 'topic'; controls source.table
            and value-schema lookup key.
        topic_set: Ordered topic set for initializing zero-count entries;
            provided by the driver from enumerate_topics.
        paced: True to flush each line as written (incremental delivery); False for
            buffered/at-close delivery. Byte output is identical across modes.

    Returns:
        The StreamOutcome (total and per-topic counts).

    Raises:
        ExportRuntimeError: A sink/out mismatch — defensive; the CLI is the
            primary guard.
    """
    if sink == "file" and out is None:
        raise ExportRuntimeError(
            "sink='file' requires an output directory (out must not be None)"
        )
    if sink == "stdout" and out is not None:
        raise ExportRuntimeError(
            "sink='stdout' requires out=None (no output directory)"
        )

    events_per_topic: dict[str, int] = {topic: 0 for topic in topic_set}
    total_events = 0

    if sink == "stdout":
        if paced:
            total_events = _write_debezium_stdout_paced(
                events,
                anchor,
                source_identity,
                table_identity,
                value_schemas,
                events_per_topic,
            )
        else:
            for event in events:
                ts_ms = rebased_epoch_ms(event.event_sim_time, anchor)
                table = _resolve_table_identity(event, table_identity)
                schema = value_schemas[table] if value_schemas is not None else None
                msg = render_debezium_message(
                    event, ts_ms, source_identity, table, schema
                )
                sys.stdout.write(_serialize_message(msg))
                t = event.topic
                events_per_topic[t] = events_per_topic.get(t, 0) + 1
                total_events += 1
    else:
        assert out is not None
        if paced:
            total_events = _write_debezium_file_paced(
                events,
                out,
                anchor,
                source_identity,
                table_identity,
                value_schemas,
                events_per_topic,
            )
        else:
            buffers: dict[str, list[str]] = {}
            for event in events:
                ts_ms = rebased_epoch_ms(event.event_sim_time, anchor)
                table = _resolve_table_identity(event, table_identity)
                schema = value_schemas[table] if value_schemas is not None else None
                msg = render_debezium_message(
                    event, ts_ms, source_identity, table, schema
                )
                topic = event.topic
                if topic not in buffers:
                    buffers[topic] = []
                buffers[topic].append(_serialize_message(msg))
                events_per_topic[topic] = events_per_topic.get(topic, 0) + 1
                total_events += 1

            for topic, lines in buffers.items():
                file_path = out / f"{topic}.jsonl"
                file_path.write_text("".join(lines), encoding="utf-8")

    return StreamOutcome(
        total_events=total_events,
        events_per_topic=events_per_topic,
    )
