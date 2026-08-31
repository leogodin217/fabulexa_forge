"""Streaming exporter package for fabulexa_forge.

Exports the base layer as a CDC event stream — one ordered change event per
record state transition (c/u/d), routed by kind and topic.

Public surface:
    StreamEvent   — one ordered change event (format-agnostic, with routing identity)
    StreamOutcome — result of a stream run (per-topic event counts)
    iter_stream_events — yields events in canonical total order with seq stamped
    render_jsonl_object — render one event as the S1 JSONL object dict
    build_debezium_value_schema — Connect value-schema descriptor for one table identity
    rebased_epoch_ms    — rebase an event's sim_time to epoch-milliseconds
    render_debezium_message — re-wrap one StreamEvent as a Debezium value message
    encode_pinned       — byte-stable JSON encoder shared by all streaming sinks
    write_kafka_stream  — produce events to Kafka (requires the kafka extra)
    stream_export       — end-to-end: events → format → sink
    generate_stream_init_config — `init --mode streaming`'s proposal engine
    route_attributes    — derive Layer-A route attributes for one event
    resolve_subtype_index — index a sub-typed kind by discriminator
    ResolvedClock       — the resolved realtime pacing policy for one stream run
    resolve_clock       — resolve config × CLI into one effective pacing policy
    pace_events         — yield events on a drift-free real-time schedule
    Transport           — master transport section of ControlState (mixer)
    TopicDials          — per-topic operator controls (mixer)
    ControlState        — full mutable operator state (mixer)
    FrontierState       — evolving frontier / edge state (mixer)
    advance             — pure per-tick advance (mixer)
    seed_mixer_run      — drain the engine once and build the initial mixer state
    schedule_releases   — async driver loop: advance every tick, drain to completion
"""

from fabulexa_forge.exporters.streaming.debezium import (
    build_debezium_value_schema,
    rebased_epoch_ms,
    render_debezium_message,
)
from fabulexa_forge.exporters.streaming.driver import stream_export
from fabulexa_forge.exporters.streaming.encoding import encode_pinned
from fabulexa_forge.exporters.streaming.engine import iter_stream_events
from fabulexa_forge.exporters.streaming.init import generate_stream_init_config
from fabulexa_forge.exporters.streaming.jsonl import render_jsonl_object
from fabulexa_forge.exporters.streaming.kafka_sink import (
    resolve_bootstrap_servers,
    write_kafka_stream,
)
from fabulexa_forge.exporters.streaming.mixer import (
    ControlState,
    FrontierState,
    TopicDials,
    Transport,
    advance,
    schedule_releases,
    seed_mixer_run,
)
from fabulexa_forge.exporters.streaming.pacer import (
    ResolvedClock,
    pace_events,
    resolve_clock,
)
from fabulexa_forge.exporters.streaming.routing import (
    membership_route_attributes,
    resolve_subtype_index,
    route_attributes,
)
from fabulexa_forge.exporters.streaming.types import StreamEvent, StreamOutcome

__all__ = [
    "ControlState",
    "FrontierState",
    "ResolvedClock",
    "StreamEvent",
    "StreamOutcome",
    "TopicDials",
    "Transport",
    "advance",
    "build_debezium_value_schema",
    "encode_pinned",
    "generate_stream_init_config",
    "iter_stream_events",
    "membership_route_attributes",
    "rebased_epoch_ms",
    "render_debezium_message",
    "render_jsonl_object",
    "resolve_bootstrap_servers",
    "resolve_subtype_index",
    "route_attributes",
    "pace_events",
    "resolve_clock",
    "schedule_releases",
    "seed_mixer_run",
    "stream_export",
    "write_kafka_stream",
]
