"""JSONL format renderer for the streaming exporter.

Renders StreamEvents as the S1 plain-JSONL object; framing and sink delivery
live in the driver's format-agnostic write_line_stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.exporters.streaming.types import StreamEvent


def render_jsonl_object(event: "StreamEvent") -> dict[str, object]:
    """Render one event as the S1 plain-JSONL object.

    Shape: {seq, op, ts, kind, key: {<key_column>: <key_value>}, after} — keys
    inserted in exactly that order, which is the serialized order (the sink
    does not sort). The key is the event's elected
    surface — {record_id} under no election (§ Keying), or one entry keyed by
    the elected surface's contract column name. `after` is the reconstructed
    row map (all values str-or-null, codec VARCHAR) on c/u/r and null on d.
    On 'r' (the seek snapshot-read op) `after` is the record's full
    published image at the snapshot position; `seq` is the shared snapshot
    position N and `ts` renders event_sim_time = N's read position T.

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
        "key": {event.key_column: event.key_value},
        "after": event.after,
    }
