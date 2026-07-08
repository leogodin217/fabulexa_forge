// In-memory simulated frontier. Lets the FabulMixer perform-board run — and the
// money demo be performed — with NO backend. Swapping to httpApi (same interface)
// when the backend lands changes nothing in the UI.
//
// Model (intentionally simple, abstract "events"):
//   - The master frontier advances through sim-time at `speed` x real time while
//     playing. Each topic produces events at a fixed density.
//   - A stream's delivery edge chases (frontier - lag), rate-limited by `rate`;
//     `rate > 1` drains backlog, `mute` freezes it.
//   - backlog = produced - delivered. watermark = sim-time of last delivered event.
// All dynamics the demo needs (lag -> stalled watermark + rising backlog; mute ->
// runaway backlog; speed-up -> drain) fall out of this.

import type {
  Capabilities,
  ConsumerControlState,
  ConsumerMeters,
  ConsumerTopicDials,
  ConsumerTopicDialsInput,
  ConsumerTopicMeter,
  ControlState,
  FabulMixerApi,
  JoinMeter,
  Meters,
  Transport,
  TopicDials,
  TopicDialsInput,
  WindowMeter,
} from './types'

interface Sim {
  dials: TopicDials
  /** Consumer-side pull rate (events / real second), detent 1.0. */
  ingestRate: number
  /** Events produced per sim-millisecond. */
  density: number
  /** Cumulative events produced (frontier passed their sim-time). */
  produced: number
  /** Cumulative events delivered downstream (producer delivery edge). */
  delivered: number
  /** Cumulative events ingested by the consumer (consumer watermark edge). */
  consumed: number
}

// Mock consumer shape — the no-backend mirror of the demo's `--window` / `--join`
// flags. The window is deliberately short (sim-ms) so firings tick over in a
// short session; the join enriches `cdc.encounter` facts with the `cdc.patient`
// dim, so lagging/muting/throttling `cdc.patient` makes the null-rate climb.
const MOCK_WINDOW_MS = 60_000
const MOCK_JOIN = { fact: 'cdc.encounter', dim: 'cdc.patient' } as const

const SEED: ReadonlyArray<{ topic: string; content: TopicDials['content']; density: number }> = [
  { topic: 'cdc.patient', content: 'state-changes', density: 0.03 },
  { topic: 'cdc.encounter', content: 'state-changes', density: 0.05 },
  { topic: 'cdc.dim_ward', content: 'state-changes', density: 0.008 },
  { topic: 'evt.ward_queue', content: 'membership-events', density: 0.02 },
  { topic: 'evt.triage_queue', content: 'membership-events', density: 0.04 },
  // declared-but-empty: routed, zero events for this emit — still gets a strip.
  { topic: 'evt.discharge_queue', content: 'membership-events', density: 0 },
]

function makeSims(): Sim[] {
  return SEED.map((s) => ({
    dials: { topic: s.topic, content: s.content, rate: 1, lag_ms: 0, mute: false },
    ingestRate: 1,
    density: s.density,
    produced: 0,
    delivered: 0,
    consumed: 0,
  }))
}

export function createMockApi(): FabulMixerApi {
  const transport: Transport = { playing: false, speed: 1 }
  const sims = makeSims()

  // Sim-clock state. frontierSim is "sim-ms since the run origin".
  let frontierSim = 0
  let playStartedWall: number | null = null
  let lastWall = Date.now()

  function advance(): void {
    const now = Date.now()
    const dtReal = now - lastWall
    lastWall = now
    if (!transport.playing || dtReal <= 0) return

    const dtSim = dtReal * transport.speed
    frontierSim += dtSim

    for (const s of sims) {
      s.produced = frontierSim * s.density
      // Desired delivery edge in sim-time, then the events due by that edge.
      const edgeSim = Math.max(0, frontierSim - s.dials.lag_ms)
      const desired = edgeSim * s.density
      // How many events this stream may release this tick.
      const maxStep = s.dials.mute ? 0 : s.dials.rate * s.density * transport.speed * dtReal
      const target = Math.min(desired, s.delivered + maxStep)
      s.delivered = Math.min(s.produced, Math.max(s.delivered, target))

      // Consumer pulls from the delivered edge at ingestRate. ingestRate=1 keeps
      // pace with a rate=1 producer; 0 stalls; > 1 drains consumer backlog. The
      // consumer can never get ahead of what the producer delivered.
      const maxPull = s.ingestRate * s.density * transport.speed * dtReal
      s.consumed = Math.min(s.delivered, s.consumed + maxPull)
    }
  }

  function meterFor(s: Sim): Meters['topics'][number] {
    const backlog = Math.max(0, Math.round(s.produced - s.delivered))
    if (s.delivered <= 0) {
      // before first delivery, or a declared-but-empty topic
      return { topic: s.dials.topic, backlog, delivery_lag_ms: null, delivery_edge_sim_time: null }
    }
    const edgeSim = s.delivered / s.density
    return {
      topic: s.dials.topic,
      backlog,
      delivery_lag_ms: Math.max(0, Math.round(frontierSim - edgeSim)),
      delivery_edge_sim_time: simToIso(edgeSim),
    }
  }

  // Consumer-side sim-time edge of a topic (last ingested event), or null before
  // first ingest / for a declared-but-empty topic.
  function consumerEdgeSim(s: Sim): number | null {
    if (s.density <= 0 || s.consumed <= 0) return null
    return s.consumed / s.density
  }

  function consumerMeterFor(s: Sim): ConsumerTopicMeter {
    const edge = consumerEdgeSim(s)
    return {
      topic: s.dials.topic,
      watermark_sim_time: edge === null ? null : simToIso(edge),
      consumer_lag: Math.max(0, Math.round(s.delivered - s.consumed)),
    }
  }

  // Global watermark = min over data-bearing topics; null until every one has
  // ingested at least one event (a single stalled stream holds it back).
  function globalWatermarkSim(): number | null {
    const edges = sims.filter((s) => s.density > 0).map(consumerEdgeSim)
    if (edges.length === 0 || edges.some((e) => e === null)) return null
    return Math.min(...(edges as number[]))
  }

  function windowMeter(): WindowMeter {
    const gw = globalWatermarkSim()
    const fired = gw === null ? 0 : Math.floor(gw / MOCK_WINDOW_MS)
    return {
      size_ms: MOCK_WINDOW_MS,
      fired_count: fired,
      latest_window_end_sim_time: fired > 0 ? simToIso(fired * MOCK_WINDOW_MS) : null,
    }
  }

  function joinMeter(): JoinMeter {
    const fact = sims.find((s) => s.dials.topic === MOCK_JOIN.fact)
    const dim = sims.find((s) => s.dials.topic === MOCK_JOIN.dim)
    const factCount = fact ? Math.round(fact.consumed) : 0
    // A fact is null-enriched when its event-time runs ahead of the dim's
    // watermark — the matching dim row hasn't been ingested yet.
    const factEdge = fact ? (consumerEdgeSim(fact) ?? 0) : 0
    const dimEdge = dim ? (consumerEdgeSim(dim) ?? 0) : 0
    const nullSpanSim = Math.max(0, factEdge - dimEdge)
    const nullCount =
      fact && factCount > 0
        ? Math.min(factCount, Math.round(nullSpanSim * fact.density))
        : 0
    return {
      fact_topic: MOCK_JOIN.fact,
      dimension_topic: MOCK_JOIN.dim,
      fact_count: factCount,
      null_count: nullCount,
      null_rate: factCount > 0 ? nullCount / factCount : null,
    }
  }

  return {
    async getState(): Promise<ControlState> {
      advance()
      return { transport: { ...transport }, topics: sims.map((s) => ({ ...s.dials })) }
    },

    async getMeters(): Promise<Meters> {
      advance()
      return {
        frontier_sim_time: playStartedWall === null ? null : simToIso(frontierSim),
        wall_elapsed_ms: playStartedWall === null ? 0 : Date.now() - playStartedWall,
        topics: sims.map(meterFor),
      }
    },

    async putTransport(next: Transport): Promise<Transport> {
      advance()
      if (next.playing && !transport.playing && playStartedWall === null) {
        playStartedWall = Date.now()
      }
      transport.playing = next.playing
      transport.speed = next.speed
      return { ...transport }
    },

    async putTopic(topic: string, dials: TopicDialsInput): Promise<TopicDials> {
      advance()
      const s = sims.find((x) => x.dials.topic === topic)
      if (!s) throw new Error(`unknown topic: ${topic}`)
      s.dials.rate = dials.rate
      s.dials.lag_ms = dials.lag_ms
      s.dials.mute = dials.mute
      return { ...s.dials }
    },

    async getCapabilities(): Promise<Capabilities> {
      return { consumer_enabled: true }
    },

    async getConsumerState(): Promise<ConsumerControlState> {
      advance()
      return {
        topics: sims.map((s) => ({
          topic: s.dials.topic,
          content: s.dials.content,
          ingest_rate: s.ingestRate,
        })),
      }
    },

    async getConsumerMeters(): Promise<ConsumerMeters> {
      advance()
      const gw = globalWatermarkSim()
      return {
        global_watermark_sim_time: gw === null ? null : simToIso(gw),
        topics: sims.map(consumerMeterFor),
        windows: [windowMeter()],
        joins: [joinMeter()],
      }
    },

    async putConsumerTopic(
      topic: string,
      dials: ConsumerTopicDialsInput,
    ): Promise<ConsumerTopicDials> {
      advance()
      const s = sims.find((x) => x.dials.topic === topic)
      if (!s) throw new Error(`unknown topic: ${topic}`)
      s.ingestRate = dials.ingest_rate
      return { topic: s.dials.topic, content: s.dials.content, ingest_rate: s.ingestRate }
    },
  }
}

// Render a sim-ms offset as an ISO instant off a fixed, arbitrary run origin.
const RUN_ORIGIN_MS = Date.parse('2026-01-01T00:00:00Z')
function simToIso(simMs: number): string {
  return new Date(RUN_ORIGIN_MS + simMs).toISOString()
}
