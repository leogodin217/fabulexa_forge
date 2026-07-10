"""JSONL format renderer and sink for the streaming exporter.

Renders StreamEvents as newline-delimited JSON (JSONL) and writes them to
either stdout or one-file-per-topic under an output directory.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Literal

from fabulexa_forge.errors import ExportRuntimeError
from fabulexa_forge.exporters.streaming.encoding import encode_pinned
from fabulexa_forge.exporters.streaming.types import StreamOutcome

if TYPE_CHECKING:
    from fabulexa_forge.exporters.streaming.types import StreamEvent


def render_jsonl_object(event: "StreamEvent") -> dict[str, object]:
    """Render one event as the S1 plain-JSONL object.

    Shape: {seq, op, ts, kind, key: {record_id}, after} — keys inserted in exactly
    that order, which is the serialized order (write_jsonl_stream does not sort). The
    key is always {record_id} — never presentation_id, even for a kind that carries a
    surrogate (§ Keying). `after` is the reconstructed row map (all values
    str-or-null, codec VARCHAR) on c/u and null on d.

    Args:
        event: The event to render.

    Returns:
        A JSON-serializable dict for one newline-delimited record (str / int / dict /
        null leaves only — no native DECIMAL / DATE / float).
    """
    return {
        "seq": event.seq,
        "op": event.op,
        "ts": event.ts,
        "kind": event.kind,
        "key": {"record_id": event.record_id},
        "after": event.after,
    }


def _serialize_object(obj: dict[str, object]) -> str:
    """Serialize one JSONL object with the pinned encoder settings.

    UTF-8, compact separators, no BOM, no inter-token whitespace, construction
    order preserved (sort_keys=False), exactly one trailing newline.

    Args:
        obj: The rendered event dict.

    Returns:
        A compact JSON string terminated by exactly one newline.
    """
    return encode_pinned(obj) + "\n"


def _write_jsonl_stdout_paced(
    events: Iterable["StreamEvent"],
    events_per_topic: dict[str, int],
) -> int:
    """Write events to stdout with per-line flush (paced mode).

    Args:
        events: The ordered events to write.
        events_per_topic: Mutable dict updated with per-topic counts.

    Returns:
        Total events written.
    """
    total_events = 0
    for event in events:
        obj = render_jsonl_object(event)
        sys.stdout.write(_serialize_object(obj))
        sys.stdout.flush()
        events_per_topic[event.topic] = events_per_topic.get(event.topic, 0) + 1
        total_events += 1
    return total_events


def _write_jsonl_file_paced(
    events: Iterable["StreamEvent"],
    out: Path,
    events_per_topic: dict[str, int],
) -> int:
    """Write events to per-topic files with lazy open and per-line flush (paced mode).

    Each topic's handle is opened on first event, kept open across the run, and
    closed in a finally on completion or abort. A zero-event topic opens no handle.

    Args:
        events: The ordered events to write.
        out: The output directory for topic files.
        events_per_topic: Mutable dict updated with per-topic counts.

    Returns:
        Total events written.
    """
    import io

    handles: dict[str, io.TextIOWrapper] = {}
    total_events = 0
    try:
        for event in events:
            topic = event.topic
            if topic not in handles:
                handles[topic] = open(  # noqa: WPS515
                    out / f"{topic}.jsonl", "w", encoding="utf-8"
                )
            obj = render_jsonl_object(event)
            handles[topic].write(_serialize_object(obj))
            handles[topic].flush()
            events_per_topic[topic] = events_per_topic.get(topic, 0) + 1
            total_events += 1
    finally:
        for handle in handles.values():
            handle.close()
    return total_events


def write_jsonl_stream(
    events: Iterable["StreamEvent"],
    sink: Literal["stdout", "file"],
    out: Path | None,
    topic_set: tuple[str, ...] = (),
    paced: bool = False,
) -> StreamOutcome:
    """Serialize events as newline-delimited JSON to the chosen sink.

    For 'stdout', writes every event (all topics interleaved) to stdout in arrival
    (seq) order. For 'file', writes one <topic>.jsonl file per topic under `out`, each
    in seq order. Each line is render_jsonl_object(event) serialized as compact JSON
    with the encoder pinned for the byte-identical-stream invariant: UTF-8,
    separators (',', ':') with no inter-token whitespace, ensure_ascii=False, keys
    left in construction order (sort_keys=False), exactly one '\\n' terminating each
    record, and no BOM. A topic with zero events still produces an (empty) <topic>.jsonl
    (guaranteed by the driver via topic_set), and events_per_topic records 0 for it;
    a fully empty stream writes nothing to stdout. Either way the run succeeds.

    Args:
        events: The ordered events to write.
        sink: 'stdout' or 'file'.
        out: The output directory for the file sink; must be None for stdout.
        topic_set: Ordered topic set for initializing zero-count entries;
            provided by the driver from enumerate_topics.
        paced: True to flush each line as written (incremental delivery); False for
            buffered/at-close delivery. Byte output is identical across modes.

    Returns:
        The StreamOutcome (total and per-topic counts).

    Raises:
        ExportRuntimeError: Defensive precondition — the file sink is selected with
            out=None, or stdout with a non-None out. The CLI is the primary guard.
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
            total_events = _write_jsonl_stdout_paced(events, events_per_topic)
        else:
            for event in events:
                obj = render_jsonl_object(event)
                sys.stdout.write(_serialize_object(obj))
                events_per_topic[event.topic] = events_per_topic.get(event.topic, 0) + 1
                total_events += 1
    else:
        assert out is not None
        if paced:
            total_events = _write_jsonl_file_paced(events, out, events_per_topic)
        else:
            # file sink: buffer per topic, then write
            buffers: dict[str, list[str]] = {}
            for event in events:
                topic = event.topic
                if topic not in buffers:
                    buffers[topic] = []
                obj = render_jsonl_object(event)
                buffers[topic].append(_serialize_object(obj))
                events_per_topic[topic] = events_per_topic.get(topic, 0) + 1
                total_events += 1

            # Write all buffered topics
            for topic, lines in buffers.items():
                file_path = out / f"{topic}.jsonl"
                file_path.write_text("".join(lines), encoding="utf-8")

    return StreamOutcome(
        total_events=total_events,
        events_per_topic=events_per_topic,
    )
