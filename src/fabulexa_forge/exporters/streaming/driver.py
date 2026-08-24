"""Stream-export driver: ties engine → format → sink for one run.

Integrates iter_stream_events (engine), render/write functions (format), and
the sink. Also handles the selected-topics guarantee for empty streams. The
Debezium value-schema builders read `presentation.py`'s naming resolvers
directly — the same naming authority the engine's after-image assembly
reads — so the declared schema and the rendered rows cannot diverge.
Layer-direction invariant: imports engine, presentation, jsonl, debezium,
config, anchor, errors — never CLI or writers.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fabulexa_forge.config.models import KindStream, MembershipStream
from fabulexa_forge.errors import (
    ExportError,
    ExportRuntimeError,
    KafkaBootstrapUnresolvable,
)
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.streaming.engine import (
    build_topic_set,
    iter_stream_events,
    resolve_stream_surfaces,
)
from fabulexa_forge.exporters.streaming.jsonl import write_jsonl_stream
from fabulexa_forge.exporters.streaming.presentation import (
    resolve_membership_output_columns,
    resolve_stream_output_columns,
)
from fabulexa_forge.exporters.streaming.types import StreamOutcome

if TYPE_CHECKING:
    from collections.abc import Callable

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import (
        DebeziumSourceIdentity,
        KeySurface,
        StreamConfig,
    )
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.streaming.pacer import ResolvedClock
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

_KAFKA_REQUIRES_ANCHOR_MSG = (
    "sink 'kafka' requires a resolved effective anchor"
    " (set rebase.base_date / rebase.timezone, or rely on the sidecar runtime anchor);"
    " the Kafka record timestamp must be epoch-milliseconds"
)


def stream_export(
    emit: "Emit",
    config: "StreamConfig",
    fmt: Literal["jsonl", "debezium"],
    sink: Literal["stdout", "file", "kafka"],
    out: Path | None,
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
    clock: "ResolvedClock | None" = None,
    bootstrap_servers: str | None = None,
) -> StreamOutcome:
    """Run a stream end to end: events -> (pace when realtime) -> format -> sink.

    Adds the kafka sink branch to today's contract; stdout/file behaviour is unchanged
    and bootstrap_servers is ignored on those paths. For sink='kafka' the driver:
    requires a resolved anchor (KafkaRequiresAnchor — ExportError, reusing a message
    constant exactly as the debezium-requires-anchor rule does) and a resolved
    bootstrap_servers; reuses the existing debezium business rules and value-schema
    build when fmt='debezium' (DebeziumRequiresConfig); builds the per-event
    value-render closure (jsonl: encode_pinned(render_jsonl_object(event)).encode(
    'utf-8'); debezium: encode_pinned(render_debezium_message(...)).encode('utf-8')
    with ts_ms = rebased_epoch_ms and the table_identity-keyed schema); and delegates
    delivery to write_kafka_stream. Pacing composes with the kafka sink exactly as
    with the others (paced = clock is not None).

    Args:
        emit: The open emit.
        config: The validated streaming configuration.
        fmt: 'jsonl' or 'debezium'.
        sink: 'stdout', 'file', or 'kafka'.
        out: The output directory for the file sink; None for stdout and kafka.
        anchor: The resolved effective anchor, or None.
        notice_sink: The caller-supplied notice receiver, threaded to every
            internal iter_stream_events call (required — a caller wanting
            silence passes a discarding sink).
        clock: The resolved realtime pacing policy, or None for unpaced delivery.
        bootstrap_servers: The resolved bootstrap-servers string; non-None for
            sink='kafka', None (ignored) otherwise.

    Returns:
        The StreamOutcome (total and per-topic counts) — independent of pacing.

    Raises:
        ExportError: fmt='debezium' with no resolved anchor or no debezium block;
            sink='kafka' with no resolved anchor (KafkaRequiresAnchor); a single-branch,
            config-resolvability, or business-rule failure from the engine.
        ExportRuntimeError: an unsupported fmt or a sink/out mismatch.
        KafkaDeliveryError: sink='kafka' and a connection, topic-creation, produce, or
            flush failure (a child of ExportRuntimeError).
        KafkaClientUnavailable: sink='kafka' and confluent-kafka is not installed.
    """
    if fmt not in ("jsonl", "debezium"):
        raise ExportRuntimeError(
            f"unsupported format: {fmt!r}; supported formats are 'jsonl', 'debezium'"
        )

    # Defensive preconditions — mirrored in write_jsonl_stream / write_debezium_stream
    if sink == "file" and out is None:
        raise ExportRuntimeError(
            "sink='file' requires an output directory (out must not be None)"
        )
    if sink == "stdout" and out is not None:
        raise ExportRuntimeError(
            "sink='stdout' requires out=None (no output directory)"
        )

    topic_set = build_topic_set(config)

    if sink == "kafka":
        return _stream_export_kafka(
            emit, config, fmt, anchor, notice_sink, topic_set, clock, bootstrap_servers
        )

    if fmt == "debezium":
        return _stream_export_debezium(
            emit, config, sink, out, anchor, notice_sink, topic_set, clock
        )

    raw_events = iter_stream_events(emit, config, anchor, notice_sink)
    paced = clock is not None
    if paced:
        from fabulexa_forge.exporters.streaming.pacer import pace_events

        events = pace_events(raw_events, clock, time.sleep, time.monotonic)  # type: ignore[arg-type]
    else:
        events = raw_events

    outcome = write_jsonl_stream(events, sink, out, topic_set=topic_set, paced=paced)

    return _merge_outcome(outcome, topic_set, sink, out)


def build_kafka_render_value(
    emit: "Emit",
    config: "StreamConfig",
    fmt: Literal["jsonl", "debezium"],
    anchor: "EffectiveAnchor",
    topic_set: tuple[str, ...],
) -> "Callable[[StreamEvent], bytes]":
    """Build the per-event value-render closure for a Kafka run (stream or mixer).

    Encapsulates the format branch and the Debezium business rules in one place so
    the streaming Kafka path and the mixer share them. For fmt='jsonl', returns the
    pinned-encoded JSONL object bytes (no trailing newline). For fmt='debezium',
    enforces DebeziumRequiresConfig, builds the per-stream value schemas keyed by
    config.debezium.table_identity when schemas are enabled, and returns the
    pinned-encoded Debezium message bytes with the rebased ts_ms. The returned bytes
    are byte-identical to the corresponding file/stdout line minus its trailing
    newline.

    The typed anchor: EffectiveAnchor parameter means each caller has already
    resolved and non-None-checked the anchor (KafkaRequiresAnchor) before calling;
    this builder enforces only DebeziumRequiresConfig, preserving the existing
    KafkaRequiresAnchor -> DebeziumRequiresConfig precedence across the extraction.

    Args:
        emit: The open emit (read for Debezium value schemas; unused for jsonl).
        config: The validated streaming configuration.
        fmt: 'jsonl' or 'debezium'.
        anchor: The resolved effective anchor (non-None).
        topic_set: The full topic set.

    Returns:
        A callable mapping a StreamEvent to its UTF-8 message-value bytes.

    Raises:
        ExportError: fmt='debezium' with no debezium block (DebeziumRequiresConfig).
    """
    if fmt == "debezium":
        # Business rule: DebeziumRequiresConfig
        if config.debezium is None:
            raise ExportError(_DEBEZIUM_REQUIRES_CONFIG_MSG)

        debezium_cfg = config.debezium
        source_identity = debezium_cfg.source
        table_identity = debezium_cfg.table_identity

        value_schemas: dict[str, dict[str, object]] | None = None
        if debezium_cfg.schemas_enable:
            value_schemas = _build_value_schemas(
                emit, config, source_identity, table_identity
            )

        return _build_debezium_render_closure(
            anchor, source_identity, value_schemas, table_identity
        )

    return _build_jsonl_render_closure()


def _build_jsonl_render_closure() -> "Callable[[StreamEvent], bytes]":
    """Build a render_value closure for the jsonl format on the kafka sink.

    Returns:
        A callable that takes a StreamEvent and returns UTF-8 bytes with no
        trailing newline, byte-identical to the JSONL file/stdout line minus its
        trailing '\\n'.
    """
    from fabulexa_forge.exporters.streaming.encoding import encode_pinned
    from fabulexa_forge.exporters.streaming.jsonl import render_jsonl_object

    def _render(event: "StreamEvent") -> bytes:
        return encode_pinned(render_jsonl_object(event)).encode("utf-8")

    return _render


def _build_debezium_render_closure(
    anchor: "EffectiveAnchor",
    source_identity: "DebeziumSourceIdentity",
    value_schemas: "dict[str, dict[str, object]] | None",
    table_identity: str,
) -> "Callable[[StreamEvent], bytes]":
    """Build a render_value closure for the debezium format on the kafka sink.

    Args:
        anchor: The resolved effective anchor.
        source_identity: The masquerade source identity.
        value_schemas: Table-identity-keyed value schemas when schemas_enable is
            True; None for bare payloads.
        table_identity: 'source_table' or 'topic'.

    Returns:
        A callable that takes a StreamEvent and returns UTF-8 bytes with no
        trailing newline, byte-identical to the Debezium file/stdout line minus
        its trailing '\\n'.
    """
    from fabulexa_forge.exporters.streaming.debezium import (
        rebased_epoch_ms,
        render_debezium_message,
    )
    from fabulexa_forge.exporters.streaming.encoding import encode_pinned

    def _render(event: "StreamEvent") -> bytes:
        ts_ms = rebased_epoch_ms(event.event_sim_time, anchor)
        table = event.topic if table_identity == "topic" else event.route_table
        value_schema = value_schemas.get(table) if value_schemas is not None else None
        msg = render_debezium_message(
            event, ts_ms, source_identity, table, value_schema
        )
        return encode_pinned(msg).encode("utf-8")

    return _render


def _stream_export_kafka(
    emit: "Emit",
    config: "StreamConfig",
    fmt: Literal["jsonl", "debezium"],
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
    topic_set: tuple[str, ...],
    clock: "ResolvedClock | None",
    bootstrap_servers: str | None,
) -> StreamOutcome:
    """Dispatch the kafka sink path, enforcing business rules and building render_value.

    Args:
        emit: The open emit.
        config: The validated streaming configuration.
        fmt: 'jsonl' or 'debezium'.
        anchor: The resolved effective anchor, or None.
        notice_sink: The caller-supplied notice receiver, threaded to
            iter_stream_events.
        topic_set: The full topic set.
        clock: The resolved realtime pacing policy, or None.
        bootstrap_servers: The resolved bootstrap-servers string.

    Returns:
        The StreamOutcome.

    Raises:
        ExportError: KafkaRequiresAnchor; DebeziumRequiresConfig when fmt='debezium'.
        KafkaDeliveryError: Delivery failure.
        KafkaClientUnavailable: confluent-kafka not installed.
    """
    import fabulexa_forge.exporters.streaming.kafka_sink as _kafka_sink_mod

    # Business rule: KafkaRequiresAnchor
    if anchor is None:
        raise ExportError(_KAFKA_REQUIRES_ANCHOR_MSG)

    render_value = build_kafka_render_value(emit, config, fmt, anchor, topic_set)

    raw_events = iter_stream_events(emit, config, anchor, notice_sink)
    paced = clock is not None
    if paced:
        from fabulexa_forge.exporters.streaming.pacer import pace_events

        events = pace_events(raw_events, clock, time.sleep, time.monotonic)  # type: ignore[arg-type]
    else:
        events = raw_events

    if bootstrap_servers is None:
        raise KafkaBootstrapUnresolvable(
            "bootstrap_servers must be resolved before _stream_export_kafka is called"
        )
    return _kafka_sink_mod.write_kafka_stream(
        events,
        render_value,
        anchor,
        bootstrap_servers,
        topic_set,
        paced=paced,
    )


def _stream_export_debezium(
    emit: "Emit",
    config: "StreamConfig",
    sink: Literal["stdout", "file"],
    out: Path | None,
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
    topic_set: tuple[str, ...],
    clock: "ResolvedClock | None" = None,
) -> StreamOutcome:
    """Dispatch the debezium format path, enforcing business rules.

    Args:
        emit: The open emit.
        config: The validated streaming configuration.
        sink: 'stdout' or 'file'.
        out: The output directory for the file sink; None for stdout.
        anchor: The resolved effective anchor, or None.
        notice_sink: The caller-supplied notice receiver, threaded to
            iter_stream_events.
        topic_set: The full topic set.
        clock: The resolved realtime pacing policy, or None.

    Returns:
        The StreamOutcome.

    Raises:
        ExportError: DebeziumRequiresConfig or DebeziumRequiresAnchor.
    """
    from fabulexa_forge.exporters.streaming.debezium import (
        write_debezium_stream,
    )

    # Business rule: DebeziumRequiresConfig
    if config.debezium is None:
        raise ExportError(_DEBEZIUM_REQUIRES_CONFIG_MSG)

    # Business rule: DebeziumRequiresAnchor
    if anchor is None:
        raise ExportError(_DEBEZIUM_REQUIRES_ANCHOR_MSG)

    debezium_cfg = config.debezium
    source_identity = debezium_cfg.source
    table_identity = debezium_cfg.table_identity

    # Build value schemas keyed by table_identity when schemas_enable is True.
    # Well-defined by construction: one topic = one stream = one column list.
    value_schemas: dict[str, dict[str, object]] | None = None
    if debezium_cfg.schemas_enable:
        value_schemas = _build_value_schemas(
            emit, config, source_identity, table_identity
        )

    raw_events = iter_stream_events(emit, config, anchor, notice_sink)
    paced = clock is not None
    if paced:
        from fabulexa_forge.exporters.streaming.pacer import pace_events

        events = pace_events(raw_events, clock, time.sleep, time.monotonic)  # type: ignore[arg-type]
    else:
        events = raw_events

    outcome = write_debezium_stream(
        events,
        sink,
        out,
        anchor,
        source_identity,
        value_schemas,
        table_identity=table_identity,
        topic_set=topic_set,
        paced=paced,
    )

    return _merge_outcome(outcome, topic_set, sink, out)


def _stream_route_tables(
    sidecar: "Sidecar",
    stream: "KindStream",
) -> tuple[str, ...]:
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


def _build_value_schemas(
    emit: "Emit",
    config: "StreamConfig",
    source_identity: "DebeziumSourceIdentity",
    table_identity: str,
) -> dict[str, dict[str, object]]:
    """Build Debezium value schemas keyed by the table_identity value.

    For table_identity='source_table', keys are route_table values.
    For table_identity='topic', keys are stream names.

    Dispatches on config.content: 'membership-events' loops the config's
    membership-shaped streams with a leading 'event' column; all other content
    loops its kind-shaped streams. Every stream's declared column list is
    re-keyed through `elect_after_image_columns` under its gated elected
    surface (`resolve_stream_surfaces`) — a pure recomputation of the same
    gates and surfaces the engine's own validation pass resolves, so the
    declared schema and the rendered after-image stay the same list by
    construction (mirrors `exporters.base.engine`'s recompute-not-thread
    posture).

    Args:
        emit: The open emit.
        config: The validated streaming configuration.
        source_identity: The masquerade source identity.
        table_identity: 'source_table' or 'topic'.

    Returns:
        Mapping table_identity_key -> value_schema dict.

    Raises:
        ElectionMixedIdentity: A stream's spanned populations elect differing
            surfaces.
        ElectionUnionUnsafe: A uniform presentation_id election's spanned key
            spaces, or an edge's admitted target key spaces, contain a
            pairwise-unsafe pair.
    """
    election = resolve_election(emit.sidecar, config.keys)
    surfaces = resolve_stream_surfaces(emit.sidecar, election, config)
    if config.content == "membership-events":
        return _build_value_schemas_membership(
            emit, config, source_identity, table_identity, surfaces
        )
    return _build_value_schemas_kinds(
        emit, config, source_identity, table_identity, surfaces
    )


def _build_value_schemas_kinds(
    emit: "Emit",
    config: "StreamConfig",
    source_identity: "DebeziumSourceIdentity",
    table_identity: str,
    surfaces: "dict[str, KeySurface]",
) -> dict[str, dict[str, object]]:
    """Build value schemas for state-changes content, one per declared stream.

    Args:
        emit: The open emit.
        config: The validated streaming configuration.
        source_identity: The masquerade source identity.
        table_identity: 'source_table' or 'topic'.
        surfaces: Every stream's gated uniform elected surface.

    Returns:
        Mapping table_identity_key -> value_schema dict.
    """
    from fabulexa_forge.exporters.streaming.debezium import build_debezium_value_schema

    value_schemas: dict[str, dict[str, object]] = {}

    for stream in config.streams:
        assert isinstance(stream, KindStream)
        columns = [
            output_key
            for _fold_column, output_key in resolve_stream_output_columns(
                emit.sidecar,
                stream.kind,
                stream.properties,
                stream.rename,
                surfaces[stream.name],
            )
        ]
        if table_identity == "topic":
            schema_keys: tuple[str, ...] = (stream.name,)
        else:
            schema_keys = _stream_route_tables(emit.sidecar, stream)

        for schema_key in schema_keys:
            if schema_key not in value_schemas:
                value_schemas[schema_key] = build_debezium_value_schema(
                    table=schema_key,
                    columns=list(columns),
                    source_name=source_identity.name,
                    connector=source_identity.connector,
                )

    return value_schemas


def _build_value_schemas_membership(
    emit: "Emit",
    config: "StreamConfig",
    source_identity: "DebeziumSourceIdentity",
    table_identity: str,
    surfaces: "dict[str, KeySurface]",
) -> dict[str, dict[str, object]]:
    """Build value schemas for membership-events content, one per declared stream.

    Args:
        emit: The open emit.
        config: The validated streaming configuration.
        source_identity: The masquerade source identity.
        table_identity: 'source_table' or 'topic'.
        surfaces: Every stream's gated uniform owner elected surface.

    Returns:
        Mapping table_identity_key -> value_schema dict.
    """
    from fabulexa_forge.exporters.streaming.debezium import build_debezium_value_schema
    from fabulexa_forge.exporters.streaming.routing import membership_route_attributes

    value_schemas: dict[str, dict[str, object]] = {}

    for stream in config.streams:
        assert isinstance(stream, MembershipStream)
        owner_kind = stream.membership.kind
        property_name = stream.membership.property
        attrs = membership_route_attributes(owner_kind, property_name)
        schema_key = stream.name if table_identity == "topic" else attrs["route_table"]

        if schema_key not in value_schemas:
            payload_columns = [
                output_key
                for _fold_column, output_key in resolve_membership_output_columns(
                    emit.sidecar,
                    stream.membership,
                    stream.fields,
                    stream.rename,
                    surfaces[stream.name],
                )
            ]
            columns = ["event", *payload_columns]
            value_schemas[schema_key] = build_debezium_value_schema(
                table=schema_key,
                columns=columns,
                source_name=source_identity.name,
                connector=source_identity.connector,
            )

    return value_schemas


def _merge_outcome(
    outcome: StreamOutcome,
    topic_set: tuple[str, ...],
    sink: Literal["stdout", "file"],
    out: Path | None,
) -> StreamOutcome:
    """Apply the topic-set zero-count / empty-file guarantee.

    Args:
        outcome: The raw outcome from the write function.
        topic_set: All topics in the run's topic set.
        sink: 'stdout' or 'file'.
        out: The output directory, or None for stdout.

    Returns:
        A StreamOutcome with every topic in topic_set guaranteed present.
    """
    merged_counts: dict[str, int] = {topic: 0 for topic in topic_set}
    merged_counts.update(outcome.events_per_topic)

    # For file sink, create empty files for any topic with zero events
    if sink == "file" and out is not None:
        for topic in topic_set:
            file_path = out / f"{topic}.jsonl"
            if not file_path.exists():
                file_path.write_text("", encoding="utf-8")

    return StreamOutcome(
        total_events=outcome.total_events,
        events_per_topic=merged_counts,
    )
