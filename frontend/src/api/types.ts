// TypeScript mirror of docs/architecture/pending/fabulmixer-control-api.md.
// This file is a MIRROR, not the source of truth. If the contract doc changes,
// update this file (and the backend Pydantic models) to match.

export type Content = 'state-changes' | 'membership-events'

/** Master section. */
export interface Transport {
  playing: boolean
  /** 0.1 .. 1000, detent at 1.0 (log-scale slider). */
  speed: number
}

/** One channel strip's operator controls. */
export interface TopicDials {
  /** Read-only identity: the routing topic, the dial key (also the strip's title). */
  topic: string
  /** Read-only: which content axis feeds this topic. */
  content: Content
  /** 0.0 .. 4.0, detent at 1.0. Per-stream release-rate multiplier. */
  rate: number
  /** 0 .. 300000 event-time ms. Delivery lag — the money-demo knob. */
  lag_ms: number
  /** Stop releasing this stream; backlog accumulates. */
  mute: boolean
}

/** The settable subset of a channel strip (PUT /topics/{topic} body). */
export type TopicDialsInput = Pick<TopicDials, 'rate' | 'lag_ms' | 'mute'>

/** Full operator state (GET /state). */
export interface ControlState {
  transport: Transport
  topics: TopicDials[]
}

/** One channel strip's read-only meters. */
export interface TopicMeter {
  topic: string
  /** >= 0. Buffered events not yet released/delivered. 0 for an empty topic. */
  backlog: number
  /** Producer-side event-time gap (ms) between frontier and the delivered edge.
   *  null until first delivery and for a declared-but-empty topic. */
  delivery_lag_ms: number | null
  /** ISO 8601 event-time of the last delivered event (producer delivery edge);
   *  null before first delivery. */
  delivery_edge_sim_time: string | null
}

/** Full read-only snapshot (GET /meters, polled at 5 Hz). */
export interface Meters {
  /** ISO 8601 event-time of the master frontier; null before first play. */
  frontier_sim_time: string | null
  /** >= 0. Real time elapsed since play started. */
  wall_elapsed_ms: number
  topics: TopicMeter[]
}

// ── Consumer side ────────────────────────────────────────────────────────────
// The downstream instrument: it reads only message timing off the broker and
// derives watermarks, tumbling-window firings, and enrichment-join null health.
// Present only when the backend ran with --consumer (gate on Capabilities first;
// the consumer routes 404 otherwise). Contract:
// src/fabulexa_forge/exporters/streaming/mixer/wire.py.

/** Backend feature gate (GET /capabilities). */
export interface Capabilities {
  consumer_enabled: boolean
}

/** One consumer channel strip's operator controls (GET /consumer/state, PUT echo). */
export interface ConsumerTopicDials {
  topic: string
  content: Content
  /** 0.0 .. 10000, detent at 1.0. Per-stream pull rate (events / real second). */
  ingest_rate: number
}

/** The settable subset of a consumer strip (PUT /consumer/topics/{topic} body). */
export type ConsumerTopicDialsInput = Pick<ConsumerTopicDials, 'ingest_rate'>

/** Full consumer operator state (GET /consumer/state). */
export interface ConsumerControlState {
  topics: ConsumerTopicDials[]
}

/** One topic's consumer-side meters. */
export interface ConsumerTopicMeter {
  topic: string
  /** ISO 8601 event-time of the last ingested event; null before first ingest. */
  watermark_sim_time: string | null
  /** >= 0. Delivered-but-not-yet-ingested backlog at the consumer. */
  consumer_lag: number
}

/** One declared tumbling window's firing summary. */
export interface WindowMeter {
  size_ms: number
  fired_count: number
  latest_window_end_sim_time: string | null
}

/** One declared fact/dimension enrichment join's null health. */
export interface JoinMeter {
  fact_topic: string
  dimension_topic: string
  fact_count: number
  null_count: number
  /** null_count / fact_count, or null when no facts ingested yet. */
  null_rate: number | null
}

/** Full consumer read-only snapshot (GET /consumer/meters). */
export interface ConsumerMeters {
  /** ISO 8601 min watermark across data-bearing topics; null until all have ingested. */
  global_watermark_sim_time: string | null
  topics: ConsumerTopicMeter[]
  windows: WindowMeter[]
  joins: JoinMeter[]
}

/** Validation/UI bounds, mirrored from the contract. */
export const BOUNDS = {
  speed: { min: 0.1, max: 1000, detent: 1 },
  rate: { min: 0, max: 4, detent: 1 },
  lag_ms: { min: 0, max: 300000 },
  ingest_rate: { min: 0, max: 10000, detent: 1 },
} as const

/** The seam both implementations (mock + http) satisfy. */
export interface FabulMixerApi {
  getState(): Promise<ControlState>
  getMeters(): Promise<Meters>
  putTransport(transport: Transport): Promise<Transport>
  putTopic(topic: string, dials: TopicDialsInput): Promise<TopicDials>
  // Consumer side. getCapabilities is always available; the other three 404 when
  // the backend ran without --consumer, so callers gate on consumer_enabled.
  getCapabilities(): Promise<Capabilities>
  getConsumerState(): Promise<ConsumerControlState>
  getConsumerMeters(): Promise<ConsumerMeters>
  putConsumerTopic(
    topic: string,
    dials: ConsumerTopicDialsInput,
  ): Promise<ConsumerTopicDials>
}
