"""Tier-2 render surface: pure per-event StreamEvent -> bytes/timestamp/schema.

`resolve_stream_render` is self-vetting: it runs the same eager
business-rule pass `open_stream_playback` runs (`resolve_streams`), so a
render resolves with no head open, then enforces the format's own
resolve-time gates (`debezium` requires a resolved anchor and a declared
`debezium` block) and builds the per-stream naming/schema state once.
`StreamRender` is thereafter a pure per-event function: `render_bytes`,
`render_key_bytes`, `timestamp_ms`, `value_schema_for`.

Builds the `(topic, table-identity value)` schema map from `debezium.py`'s
pure builders — the design doc's two declared schema-identity fixes:
schemas are keyed by `(topic, leaf)` so overlapping streams sharing one leaf
embed distinct schemas (fix 1), and a corrupted out-of-domain leaf falls
back to a schema built from the event's own carried fields rather than
omitted or refused (fix 2).

Layer-direction invariant: tier-2 sibling of `shaped.py` / `stream.py` —
imports `config` and streaming's pure surfaces only (`engine`'s
`resolve_streams`, `build_topic_set`; `jsonl`; `debezium`; `encoding`),
never `driver`, `kafka_sink`, `pacer`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fabulexa_forge.config.models import KindStream, MembershipStream
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.streaming.debezium import (
    build_debezium_value_schema,
    rebased_epoch_ms,
    render_debezium_message,
    resolve_table_identity,
)
from fabulexa_forge.exporters.streaming.encoding import encode_pinned
from fabulexa_forge.exporters.streaming.engine import StreamResolution, resolve_streams
from fabulexa_forge.exporters.streaming.jsonl import render_jsonl_object
from fabulexa_forge.exporters.streaming.presentation import (
    resolve_membership_output_columns,
    resolve_stream_output_columns,
)
from fabulexa_forge.exporters.streaming.routing import membership_route_attributes

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import DebeziumSourceIdentity, StreamConfig
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.streaming.types import StreamEvent
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar

_DEBEZIUM_REQUIRES_CONFIG_MSG = (
    "format 'debezium' requires a 'debezium' config block with a 'source' identity"
    " (connector, name, db, schema, version)"
)

_DEBEZIUM_REQUIRES_ANCHOR_MSG = (
    "format 'debezium' requires a resolved effective anchor"
    " (set rebase.base_date / rebase.timezone, or rely on the sidecar runtime anchor);"
    " ts_ms must be epoch-milliseconds"
)

_TIMESTAMP_MS_REQUIRES_ANCHOR_MSG = (
    "timestamp_ms requires a render resolved with a resolved effective anchor"
    " (the render-scoped anchor rule); jsonl is the only anchorless render"
)

_SchemaKey = tuple[str, str]
"""A (topic, table-identity value) pair — the schema map's key."""


def _stream_route_tables(sidecar: "Sidecar", stream: "KindStream") -> tuple[str, ...]:
    """The route_table leaves a kind-shaped stream's events can carry.

    Mirrors the engine's route_attributes leaf rule: a flat kind's sole leaf is
    the bare kind name; a sub-typed kind's leaves are its declared sub_types
    scope (the stream's own scope when given, else the kind's full domain).

    Args:
        sidecar: The open emit's sidecar view.
        stream: The kind-shaped stream declaration.

    Returns:
        The distinct route_table values this stream's events can carry.
    """
    domain = sidecar.subtype_values(stream.kind)
    if not domain:
        return (stream.kind,)
    return tuple(stream.sub_types) if stream.sub_types is not None else domain


def _build_schema_map_kinds(
    emit: "Emit",
    resolution: StreamResolution,
    streams: "list[KindStream]",
    source_identity: "DebeziumSourceIdentity",
    table_identity: str,
) -> dict[_SchemaKey, dict[str, object]]:
    """Build the (topic, leaf) -> value schema map for state-changes content.

    Args:
        emit: The open emit.
        resolution: The pair's own eager-pass result.
        streams: The config's kind-shaped stream declarations.
        source_identity: The masquerade source identity.
        table_identity: 'source_table' or 'topic'.

    Returns:
        Mapping (topic, table-identity value) -> value_schema dict; one
        entry per (stream, leaf) pair the stream's events can carry.
    """
    schema_map: dict[_SchemaKey, dict[str, object]] = {}
    for stream in streams:
        identity = resolution.identity_by_stream[stream.name]
        columns = [
            entry.output_key
            for entry in resolve_stream_output_columns(
                emit.sidecar, stream.kind, stream.properties, stream.rename, identity
            )
        ]
        leaves = (
            (stream.name,)
            if table_identity == "topic"
            else _stream_route_tables(emit.sidecar, stream)
        )
        for leaf in leaves:
            schema_map[(stream.name, leaf)] = build_debezium_value_schema(
                table=leaf,
                columns=list(columns),
                source_name=source_identity.name,
                connector=source_identity.connector,
            )
    return schema_map


def _build_schema_map_membership(
    emit: "Emit",
    resolution: StreamResolution,
    streams: "list[MembershipStream]",
    source_identity: "DebeziumSourceIdentity",
    table_identity: str,
) -> dict[_SchemaKey, dict[str, object]]:
    """Build the (topic, leaf) -> value schema map for membership-events content.

    Args:
        emit: The open emit.
        resolution: The pair's own eager-pass result.
        streams: The config's membership-shaped stream declarations.
        source_identity: The masquerade source identity.
        table_identity: 'source_table' or 'topic'.

    Returns:
        Mapping (topic, table-identity value) -> value_schema dict; one
        entry per declared stream.
    """
    schema_map: dict[_SchemaKey, dict[str, object]] = {}
    for stream in streams:
        owner_identity = resolution.identity_by_stream[stream.name]
        attrs = membership_route_attributes(
            stream.membership.kind, stream.membership.property
        )
        leaf = stream.name if table_identity == "topic" else attrs["route_table"]
        payload_columns = [
            entry.output_key
            for entry in resolve_membership_output_columns(
                emit.sidecar,
                stream.membership,
                stream.fields,
                stream.rename,
                owner_identity,
            )
        ]
        columns = ["event", *payload_columns]
        schema_map[(stream.name, leaf)] = build_debezium_value_schema(
            table=leaf,
            columns=columns,
            source_name=source_identity.name,
            connector=source_identity.connector,
        )
    return schema_map


def _build_schema_map(
    emit: "Emit",
    config: "StreamConfig",
    resolution: StreamResolution,
    source_identity: "DebeziumSourceIdentity",
    table_identity: str,
) -> dict[_SchemaKey, dict[str, object]]:
    """Build the run's full (topic, table-identity value) schema map.

    Dispatches on `config.content`: 'membership-events' builds one entry per
    declared stream; every other content builds one entry per (stream, leaf)
    pair a kind-shaped stream's events can carry — the (topic, leaf) keying
    that keeps overlapping streams sharing one leaf from colliding (fix 1).

    Args:
        emit: The open emit.
        config: The validated streaming configuration.
        resolution: The pair's own eager-pass result (resolve_streams).
        source_identity: The masquerade source identity.
        table_identity: 'source_table' or 'topic'.

    Returns:
        Mapping (topic, table-identity value) -> value_schema dict.
    """
    if config.content == "membership-events":
        membership_streams = [
            stream for stream in config.streams if isinstance(stream, MembershipStream)
        ]
        return _build_schema_map_membership(
            emit, resolution, membership_streams, source_identity, table_identity
        )
    kind_streams = [
        stream for stream in config.streams if isinstance(stream, KindStream)
    ]
    return _build_schema_map_kinds(
        emit, resolution, kind_streams, source_identity, table_identity
    )


def _build_event_schema(
    event: "StreamEvent",
    table: str,
    source_identity: "DebeziumSourceIdentity",
) -> dict[str, object]:
    """Build a per-event value schema for a leaf outside the declared domain.

    Permissive totality (fix 2): when an event's resolved table identity is
    not a key the schema map declares — a corrupted discriminator value —
    the schema is built from the event's own carried fields (its after-image
    keys, or the key column alone on a delete) rather than omitted or
    refused, so every sink still embeds a schema for the message.

    Args:
        event: The event whose resolved table identity is out of the
            declared domain.
        table: The resolved table identity value (route_table or topic,
            verbatim — the corrupted leaf itself).
        source_identity: The masquerade source identity.

    Returns:
        A Connect value-schema descriptor built from the event's own fields.
    """
    columns = list(event.after) if event.after is not None else [event.key_column]
    return build_debezium_value_schema(
        table=table,
        columns=columns,
        source_name=source_identity.name,
        connector=source_identity.connector,
    )


class StreamRender:
    """The pure per-event format render: StreamEvent -> message body bytes,
    key bytes, and record timestamp.

    One event yields one body byte sequence regardless of sink; the bytes
    equal the shipped sinks' per-message bytes outside the two declared
    schema-identity fixes (line sinks add their one trailing newline; the
    Kafka sink uses them verbatim).
    """

    def __init__(
        self,
        fmt: Literal["jsonl", "debezium"],
        anchor: "EffectiveAnchor | None",
        table_identity: str | None,
        source_identity: "DebeziumSourceIdentity | None",
        schema_map: "dict[_SchemaKey, dict[str, object]] | None",
    ) -> None:
        self._fmt = fmt
        self._anchor = anchor
        self._table_identity = table_identity
        self._source_identity = source_identity
        self._schema_map = schema_map

    def render_bytes(self, event: "StreamEvent") -> bytes:
        """The message body: UTF-8 pinned-encoder JSON of the format's
        rendered object ({seq, op, ts, kind, key, after} for jsonl; the
        Debezium value message, schema-wrapped when enabled, for debezium).

        Args:
            event: The event to render.

        Returns:
            The message body bytes, unframed.
        """
        if self._fmt == "jsonl":
            return encode_pinned(render_jsonl_object(event)).encode("utf-8")

        assert self._source_identity is not None
        assert self._table_identity is not None
        table = resolve_table_identity(event, self._table_identity)
        ts_ms = self.timestamp_ms(event)
        value_schema = self.value_schema_for(event)
        msg = render_debezium_message(
            event, ts_ms, self._source_identity, table, value_schema
        )
        return encode_pinned(msg).encode("utf-8")

    def render_key_bytes(self, event: "StreamEvent") -> bytes:
        """The message key: UTF-8 pinned-encoder JSON of the one-entry
        elected key map {key_column: key_value}.

        Args:
            event: The event to render.

        Returns:
            The key bytes, unframed.
        """
        return encode_pinned({event.key_column: event.key_value}).encode("utf-8")

    def timestamp_ms(self, event: "StreamEvent") -> int:
        """The Kafka record timestamp: the rebased event instant in epoch
        milliseconds under the render's anchor — the shipped
        integer-truncation rule (anchor start-instant epoch-ns plus
        event_sim_time, floor-divided to ms), byte-for-byte the timestamp
        the shipped Kafka sink stamps today.

        Args:
            event: The event to stamp.

        Returns:
            Epoch-milliseconds (UTC) of the rebased instant.

        Raises:
            ExportError: The render was resolved with anchor=None — the
                render surface's own anchor-requirement rule (jsonl is the
                only anchorless render; the shipped sink- and format-scoped
                identities do not cover a sink-free render).
        """
        if self._anchor is None:
            raise ExportError(_TIMESTAMP_MS_REQUIRES_ANCHOR_MSG)
        return rebased_epoch_ms(event.event_sim_time, self._anchor)

    def value_schema_for(self, event: "StreamEvent") -> dict[str, object] | None:
        """The value schema this event's rendered message embeds, resolved
        from the event's own (topic, table-identity value) pair — the
        stream name under table_identity='topic', the route_table leaf
        under 'source_table'; the topic component disambiguates
        overlapping streams sharing a leaf with distinct properties. Built
        identically from the event itself on a corrupted out-of-domain
        leaf. None when fmt='jsonl' or schemas are disabled. Total over
        the head's events.

        Args:
            event: The event whose message's schema to return.

        Returns:
            The Connect value-schema descriptor, or None.
        """
        if self._fmt == "jsonl" or self._schema_map is None:
            return None
        assert self._table_identity is not None
        assert self._source_identity is not None
        table = resolve_table_identity(event, self._table_identity)
        schema = self._schema_map.get((event.topic, table))
        if schema is not None:
            return schema
        return _build_event_schema(event, table, self._source_identity)


def resolve_stream_render(
    emit: "Emit",
    config: "StreamConfig",
    fmt: Literal["jsonl", "debezium"],
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
) -> StreamRender:
    """Resolve the pure per-event render for one (emit, config, fmt, anchor).

    Builds the per-stream naming/schema state once (the naming authority's
    output keys; for debezium, the (topic, table-identity)-keyed value
    schemas and the table_identity resolution) and enforces the format's
    business rules. Self-vetting: runs streaming's eager business-rule pass
    exactly as open_stream_playback does (the pass's selection-resolution
    spine read included), so a render resolves with no head open.
    `anchor` is the same resolved anchor the paired head was opened with
    (one run, one anchor — the caller's contract; the seam does not compare).

    Args:
        emit: The open emit (the eager pass's reads only; no replay).
        config: The validated streaming configuration.
        fmt: The output format.
        anchor: The resolved effective anchor, or None.
        notice_sink: Receiver for the eager pass's notices (required — the
            notice-channel contract; a caller composing head and render
            passes one sink to both).

    Returns:
        A StreamRender whose per-event methods are pure functions.

    Raises:
        ExportError: A streaming business rule failed (the eager pass's
            existing gate identities, including the election subclasses, and
            the single-branch guard); or fmt='debezium' with anchor=None
            (the epoch-milliseconds rule), or with no debezium block
            declared (no invented mapping values — the block carries the
            source identity) — the existing error identities, unchanged.
        TemporalClassUnavailableError: Propagated from the eager pass's
            slice_only check (a reader-domain identity, passed through).
    """
    resolution = resolve_streams(emit, config, notice_sink)

    if fmt == "jsonl":
        return StreamRender(
            fmt="jsonl",
            anchor=anchor,
            table_identity=None,
            source_identity=None,
            schema_map=None,
        )

    if config.debezium is None:
        raise ExportError(_DEBEZIUM_REQUIRES_CONFIG_MSG)
    if anchor is None:
        raise ExportError(_DEBEZIUM_REQUIRES_ANCHOR_MSG)

    debezium_cfg = config.debezium
    schema_map = (
        _build_schema_map(
            emit, config, resolution, debezium_cfg.source, debezium_cfg.table_identity
        )
        if debezium_cfg.schemas_enable
        else None
    )
    return StreamRender(
        fmt="debezium",
        anchor=anchor,
        table_identity=debezium_cfg.table_identity,
        source_identity=debezium_cfg.source,
        schema_map=schema_map,
    )
