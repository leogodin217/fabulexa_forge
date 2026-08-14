"""Runtime types for the streaming exporter.

Format-agnostic data types produced by the engine and consumed by format
renderers and sinks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class StreamEvent:
    """One ordered change event, format-agnostic, with its resolved routing identity.

    Produced by the engine; consumed by a format renderer and a sink. Carries the
    raw event-time key (event_sim_time) and the rendered wallclock (ts) so a format
    may use either.
    """

    seq: int
    """1-based position in the stream's canonical total order; monotonic, global."""
    op: Literal["c", "d", "u", "join", "leave"]
    """The event op — the 1-to-1 recoding of the fold's `event_class`. State-changes
    content uses 'c'/'u'/'d'; membership-events content uses 'join'/'leave'. event_class
    is deliberately not carried here: the cross-stream merge consumes it from the
    materialized fold rows, and once `seq` is stamped the order lives in `seq`."""
    kind: str
    """The record kind (state-changes) or owner kind (membership-events); stable across
    routing."""
    record_id: str
    """The record's natural id (state-changes) or owner record id (membership-events);
    the event/message key."""
    presentation_id: str | None
    """The record's surrogate id when the kind carries one, else None (always None for
    membership-events). Carried in the after-image only — never the message key
    (§ Keying)."""
    event_sim_time: int
    """The event-time key: the sim_time (ns) the row state is reconstructed at."""
    ts: str | int
    """The rendered event timestamp: an offset-bearing wallclock ISO-8601 string
    when an anchor resolves (rendered in Python from the EffectiveAnchor), else the
    raw event_sim_time (ns). See § Timestamp rendering."""
    after: dict[str, object] | None
    """The full-row after-image (record_id, presentation_id when present, and one
    prop__<p> per selected property), or None on a delete. Every value is codec
    VARCHAR — a str, or null — so the JSONL render is total and byte-stable."""
    topic: str
    """The declaring stream's name — author-verbatim (Layer B is retired; a
    stream's declared name is the topic). The file sink writes <topic>.jsonl;
    stdout interleaves all topics in seq order; Kafka uses it as the topic
    name."""
    route_table: str
    """The per-event leaf logical source table (Layer A): the sub-type value
    for a sub-typed kind, the bare kind for a flat kind, or
    <owner_kind>__<property> for a membership stream. Reported as Debezium
    source.table under table_identity='source_table'."""
    key_column: str
    """The message-key entry's column name: the elected surface's contract
    column name for the event's population ('record_id' when no election
    applies — the default rendering). For membership-events, the owner's
    elected surface."""
    key_value: str
    """The codec-rendered elected key value (record_id verbatim; record_index
    digit-form; presentation_id codec rendering). Equals record_id under the
    default. Renderers build the key map as {key_column: key_value}; ordering
    and merge still read record_id."""


@dataclass(frozen=True)
class StreamOutcome:
    """The result of a stream run, for the CLI summary."""

    total_events: int
    """Total events written across all topics."""
    events_per_topic: dict[str, int]
    """Per-topic event counts. One entry per topic in the run's topic set — including
    declared-but-empty topics (value 0) — in deterministic enumeration order."""
