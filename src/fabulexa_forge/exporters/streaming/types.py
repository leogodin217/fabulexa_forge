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
    op: Literal["c", "d", "u", "join", "leave", "r"]
    """The event op — the 1-to-1 recoding of the fold's `event_class`. State-changes
    content uses 'c'/'u'/'d'; membership-events content uses 'join'/'leave'. event_class
    is deliberately not carried here: the cross-stream merge consumes it from the
    materialized fold rows, and once `seq` is stamped the order lives in `seq`.
    'r' is the seek snapshot-read op: the record's published state at the seek
    position, emitted once per covering stream for each record live at T. All
    other fields keep their shipped contracts; on an 'r', seq is the shared
    snapshot position N and event_sim_time is T."""
    kind: str
    """The stream's resolved envelope value (§ Kind vocabulary): the
    stream's own `kind_label` when declared, else the record kind's
    (state-changes) or owner kind's (membership-events) `kind_labels`
    label, else the bare kind name verbatim. Presentation only — routing
    (route_table, key election) always reads the base-layer kind."""
    record_id: str
    """The record's natural id (state-changes) or owner record id (membership-events);
    the event/message key."""
    event_sim_time: int
    """The event-time key: the sim_time (ns) the row state is reconstructed at."""
    ts: str | int
    """The rendered event timestamp: an offset-bearing wallclock ISO-8601 string
    when an anchor resolves (rendered in Python from the EffectiveAnchor), else the
    raw event_sim_time (ns). See § Timestamp rendering."""
    after: dict[str, object] | None
    """The published after-image, output-key-named: the elected identity
    surface under its resolved wire name, and one entry per selected
    property under its resolved wire name, or None on a delete. Every value
    is codec VARCHAR — a str, or null — so the JSONL render is total and
    byte-stable."""
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
    """The message-key entry's resolved output key: the elected surface's
    resolved wire name for the event's population — its contract column name
    ('record_id' when no election applies — the default rendering), or its
    `rename` target when the stream renames it. For membership-events, the
    owner's elected surface."""
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
