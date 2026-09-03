"""Stream-export driver: ties the playback seam (head + render) to a sink for one run.

Re-seamed over the tier-2 playback surfaces: `stream_export` opens a
`StreamPlayback` head and resolves a `StreamRender` (both from
`fabulexa_forge.playback`) against the one `notice_sink` the caller supplies,
consumes the head's whole-tape events, paces them when realtime, and
dispatches to a sink. The line-based sinks (stdout/file) share one
callable-driven writer, `write_line_stream`; the Kafka sink is driven by the
render's own `render_bytes` / `render_key_bytes` / `timestamp_ms` callables.
Layer-direction invariant: imports playback, engine (`build_topic_set` only),
config, anchor, errors — never CLI. The playback imports are deferred inside
`stream_export` (not module-level): `playback/__init__.py`'s own import of
`stream.py` / `stream_render.py` reaches back into `exporters.streaming.engine`,
which — as a submodule of this package — forces this package's `__init__.py`
(and therefore this module) to finish importing first; a module-level import
of playback here would be a genuine circular import.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fabulexa_forge.errors import (
    ExportError,
    ExportRuntimeError,
    KafkaBootstrapUnresolvable,
)
from fabulexa_forge.exporters.streaming.engine import build_topic_set
from fabulexa_forge.exporters.streaming.types import StreamOutcome

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import StreamConfig
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.streaming.pacer import ResolvedClock
    from fabulexa_forge.exporters.streaming.types import StreamEvent
    from fabulexa_forge.reader.emit import Emit

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
    """Run a stream end to end: head -> render -> (pace when realtime) -> sink.

    Opens a `StreamPlayback` head (`open_stream_playback`) and resolves a
    `StreamRender` (`resolve_stream_render`) over the same (emit, config,
    anchor), passing `notice_sink` to both — the eager business-rule pass
    therefore runs twice, the accepted double-pass cost of the re-seam.
    Consumes `head.events(None, None)` (the whole tape), paces it when
    `clock` is not None, then dispatches: 'stdout'/'file' through
    `write_line_stream` driven by `render.render_bytes`; 'kafka' through
    `write_kafka_stream` driven by `render.render_bytes` /
    `render.render_key_bytes` / `render.timestamp_ms`.

    Args:
        emit: The open emit.
        config: The validated streaming configuration.
        fmt: 'jsonl' or 'debezium'.
        sink: 'stdout', 'file', or 'kafka'.
        out: The output directory for the file sink; None for stdout and kafka.
            Must already exist — the driver refuses a missing directory rather
            than creating one.
        anchor: The resolved effective anchor, or None.
        notice_sink: The caller-supplied notice receiver, threaded to both the
            head's open and the render's resolve (required — a caller wanting
            silence passes a discarding sink).
        clock: The resolved realtime pacing policy, or None for unpaced delivery.
        bootstrap_servers: The resolved bootstrap-servers string; non-None for
            sink='kafka', None (ignored) otherwise.

    Returns:
        The StreamOutcome (total and per-topic counts) — independent of pacing.

    Raises:
        ExportError: fmt='debezium' with no resolved anchor or no debezium block;
            sink='kafka' with no resolved anchor (KafkaRequiresAnchor); a single-branch,
            config-resolvability, or business-rule failure from the eager pass.
        ExportRuntimeError: an unsupported fmt, a sink/out mismatch, or
            sink='file' with an `out` that is not an existing directory.
        KafkaDeliveryError: sink='kafka' and a connection, topic-creation, produce, or
            flush failure (a child of ExportRuntimeError).
        KafkaClientUnavailable: sink='kafka' and confluent-kafka is not installed.
    """
    if fmt not in ("jsonl", "debezium"):
        raise ExportRuntimeError(
            f"unsupported format: {fmt!r}; supported formats are 'jsonl', 'debezium'"
        )

    # Defensive preconditions — mirrored in write_line_stream
    if sink == "file" and out is None:
        raise ExportRuntimeError(
            "sink='file' requires an output directory (out must not be None)"
        )
    if sink == "stdout" and out is not None:
        raise ExportRuntimeError(
            "sink='stdout' requires out=None (no output directory)"
        )
    # The file sink writes one <topic>.jsonl per topic into `out`; it does not
    # create the directory (matching `export`, which refuses a missing output
    # path rather than minting one). Checked up front so a missing directory
    # fails before any topic file is written, never mid-run.
    if sink == "file" and out is not None and not out.is_dir():
        detail = (
            "path exists but is not a directory"
            if out.exists()
            else "no such directory"
        )
        raise ExportRuntimeError(
            f"sink='file' requires an existing output directory — {detail}: {out}"
        )

    # Business rule: KafkaRequiresAnchor — checked before the head opens or the
    # render resolves, so a kafka run with no anchor fails before any topic is
    # created.
    if sink == "kafka" and anchor is None:
        raise ExportError(_KAFKA_REQUIRES_ANCHOR_MSG)

    topic_set = build_topic_set(config)

    from fabulexa_forge.playback.stream import open_stream_playback
    from fabulexa_forge.playback.stream_render import resolve_stream_render

    head = open_stream_playback(emit, config, anchor, notice_sink)
    render = resolve_stream_render(emit, config, fmt, anchor, notice_sink)

    raw_events = head.events(None, None)
    paced = clock is not None
    if paced:
        from fabulexa_forge.exporters.streaming.pacer import pace_events

        events = pace_events(raw_events, clock, time.sleep, time.monotonic)  # type: ignore[arg-type]
    else:
        events = raw_events

    if sink == "kafka":
        if bootstrap_servers is None:
            raise KafkaBootstrapUnresolvable(
                "bootstrap_servers must be resolved before stream_export is called"
                " with sink='kafka'"
            )
        from fabulexa_forge.exporters.streaming.kafka_sink import write_kafka_stream

        return write_kafka_stream(
            events,
            render.render_bytes,
            render.render_key_bytes,
            render.timestamp_ms,
            bootstrap_servers,
            topic_set,
            paced=paced,
        )

    outcome = write_line_stream(
        events, render.render_bytes, sink, out, topic_set=topic_set, paced=paced
    )
    return _merge_outcome(outcome, topic_set, sink, out)


def _write_line_stdout_paced(
    events: "Iterable[StreamEvent]",
    render_value: "Callable[[StreamEvent], bytes]",
    events_per_topic: dict[str, int],
) -> int:
    """Write rendered lines to stdout with per-line flush (paced mode).

    Args:
        events: The ordered events to write.
        render_value: Per-event unframed message-body bytes.
        events_per_topic: Mutable dict updated with per-topic counts.

    Returns:
        Total events written.
    """
    total_events = 0
    for event in events:
        sys.stdout.buffer.write(render_value(event) + b"\n")
        sys.stdout.buffer.flush()
        events_per_topic[event.topic] = events_per_topic.get(event.topic, 0) + 1
        total_events += 1
    return total_events


def _write_line_file_paced(
    events: "Iterable[StreamEvent]",
    render_value: "Callable[[StreamEvent], bytes]",
    out: Path,
    events_per_topic: dict[str, int],
) -> int:
    """Write rendered lines to per-topic files with lazy open and per-line flush.

    Each topic's handle is opened on first event, kept open across the run, and
    closed in a finally on completion or abort. A zero-event topic opens no handle.

    Args:
        events: The ordered events to write.
        render_value: Per-event unframed message-body bytes.
        out: The output directory for topic files.
        events_per_topic: Mutable dict updated with per-topic counts.

    Returns:
        Total events written.
    """
    import io

    handles: dict[str, io.BufferedWriter] = {}
    total_events = 0
    try:
        for event in events:
            topic = event.topic
            if topic not in handles:
                handles[topic] = open(out / f"{topic}.jsonl", "wb")  # noqa: WPS515
            handles[topic].write(render_value(event) + b"\n")
            handles[topic].flush()
            events_per_topic[topic] = events_per_topic.get(topic, 0) + 1
            total_events += 1
    finally:
        for handle in handles.values():
            handle.close()
    return total_events


def write_line_stream(
    events: "Iterable[StreamEvent]",
    render_value: "Callable[[StreamEvent], bytes]",
    sink: Literal["stdout", "file"],
    out: Path | None,
    topic_set: tuple[str, ...] = (),
    paced: bool = False,
) -> StreamOutcome:
    """Write one framed line per event to the chosen sink, format-agnostic.

    Fully format-agnostic: the message body comes from `render_value` (the
    render surface's `render_bytes`); this writer knows only how to frame it
    (append one '\\n' to the unframed bytes) and where to put it. For
    'stdout', writes every event (all topics interleaved) in arrival (seq)
    order. For 'file', writes one <topic>.jsonl file per topic under `out`,
    each in seq order. A topic with zero events still produces an (empty)
    <topic>.jsonl (guaranteed by the driver via topic_set), and
    events_per_topic records 0 for it; a fully empty stream writes nothing to
    stdout. Either way the run succeeds.

    Args:
        events: The ordered events to write.
        render_value: Per-event unframed message-body bytes (the render
            surface's render_bytes).
        sink: 'stdout' or 'file'.
        out: The output directory for the file sink; must be None for stdout.
        topic_set: Ordered topic set for initializing zero-count entries;
            provided by the driver from build_topic_set.
        paced: True to flush each line as written (incremental delivery); False for
            buffered/at-close delivery. Byte output is identical across modes.

    Returns:
        The StreamOutcome (total and per-topic counts).

    Raises:
        ExportRuntimeError: Defensive precondition — the file sink is selected with
            out=None, or stdout with a non-None out. stream_export is the primary guard.
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
            total_events = _write_line_stdout_paced(
                events, render_value, events_per_topic
            )
        else:
            for event in events:
                sys.stdout.buffer.write(render_value(event) + b"\n")
                events_per_topic[event.topic] = events_per_topic.get(event.topic, 0) + 1
                total_events += 1
    else:
        assert out is not None
        if paced:
            total_events = _write_line_file_paced(
                events, render_value, out, events_per_topic
            )
        else:
            buffers: dict[str, list[bytes]] = {}
            for event in events:
                topic = event.topic
                buffers.setdefault(topic, []).append(render_value(event) + b"\n")
                events_per_topic[topic] = events_per_topic.get(topic, 0) + 1
                total_events += 1

            for topic, lines in buffers.items():
                file_path = out / f"{topic}.jsonl"
                file_path.write_bytes(b"".join(lines))

    return StreamOutcome(
        total_events=total_events,
        events_per_topic=events_per_topic,
    )


def _merge_outcome(
    outcome: StreamOutcome,
    topic_set: tuple[str, ...],
    sink: Literal["stdout", "file"],
    out: Path | None,
) -> StreamOutcome:
    """Apply the topic-set zero-count / empty-file guarantee.

    Args:
        outcome: The raw outcome from write_line_stream.
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
