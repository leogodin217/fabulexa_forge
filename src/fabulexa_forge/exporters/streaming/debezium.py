"""Debezium format renderer for the streaming exporter.

Renders StreamEvents as Debezium value messages (the envelope + optional
schema wrapper). Pure output re-wrapping of the S1 event stream — no new
content, no new order, no new fold. Framing and sink delivery live in the
driver's format-agnostic write_line_stream.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

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
    op: str,
) -> dict[str, object]:
    """Build the source block for one event in the pinned source key order.

    `snapshot` reports "true" on a seek snapshot read ('r') and "false" on
    every other op — canonical Debezium snapshot-read semantics; every 'r'
    of one snapshot phase repeats one `lsn` (the shared position N),
    deliberately, since a snapshot read has no distinct source position.
    """
    return {
        "version": source_identity.version,
        "connector": source_identity.connector,
        "name": source_identity.name,
        "ts_ms": ts_ms,
        "snapshot": "true" if op == "r" else "false",
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
      - 'd'              -> before={<key_column>: <key_value>}; after=null; op='d'.
      - 'c' / 'u' / 'r'  -> before=null; after=event.after; op=event.op.
      - 'join' / 'leave' -> before=null; after={'event': op, **event.after}; op='c'.
    """
    if event.op == "d":
        before: dict[str, object] | None = {event.key_column: event.key_value}
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

    source = _build_source_block(ts_ms, event.seq, table, source_identity, event.op)

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

    Builds the envelope: before (null, or {<key_column>: <key_value>} on a
    delete), after (the event's after-image, or null on a delete), op, ts_ms,
    transaction (null), and the source block (static identity + derived
    ts_ms / lsn=event.seq / sequence / snapshot / txId / table).

    Op branches, by event.op:
      - 'd'              -> envelope op 'd'; before={<key_column>: <key_value>};
                            after=null.
      - 'c' / 'u' / 'r'  -> envelope op = event.op; before=null; after=event.after.
                            'r' additionally reports source.snapshot='true'
                            (every other op reports 'false').
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


def resolve_table_identity(event: "StreamEvent", table_identity: str) -> str:
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
