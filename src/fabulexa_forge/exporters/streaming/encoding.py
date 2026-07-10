"""Shared pinned JSON encoder for all streaming sinks.

A single byte-stable serializer used by every streaming sink (stdout, file, kafka)
so that a given (event, fmt, anchor, schema) yields byte-identical message bodies
across all sinks.
"""

from __future__ import annotations

import json


def encode_pinned(obj: dict[str, object]) -> str:
    """Serialize one JSON object with the pinned deterministic encoder settings.

    The single byte-stable JSON encoder shared by every streaming sink: UTF-8 source
    text, compact separators (',', ':') with no inter-token whitespace,
    ensure_ascii=False, keys left in construction order (sort_keys=False), no trailing
    newline, and no BOM. The jsonl and debezium file/stdout sinks append a single '\\n'
    to frame each record; the kafka sink UTF-8-encodes the returned string as the
    message value (and key) with no framing. Extracting this primitive is what makes a
    given (event, fmt, anchor, schema) yield byte-identical message bodies across the
    stdout, file, and kafka sinks.

    Args:
        obj: The rendered event dict (render_jsonl_object / render_debezium_message
            output, or a key object like {"record_id": ...}).

    Returns:
        The compact JSON string for obj, with no trailing newline.
    """
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, sort_keys=False)
