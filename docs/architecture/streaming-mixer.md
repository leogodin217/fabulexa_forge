# Streaming Mixer

The headless correctness core of the FabulMixer live-performance driver: a
deliberately operator- and wall-clock-driven release scheduler that replays a finished
base emit as a live, mixable feed. It is a *sibling* of `pace_events`, not an extension
— where the pacer is a pure generator that releases every event in global `seq` order on
one fixed clock and is byte-identical, the mixer carries a mutable operator clock and
per-topic release edges, so an operator can `play` / `pause` / re-speed the master
transport and lag, rate-limit, or mute each topic independently mid-run. It perturbs
**delivery timing only** — never an event's value, `seq`, or `event_sim_time` — and
within a topic preserves release order; *across* topics it deliberately breaks global
`seq` order, because per-topic lag is out-of-order arrival, which is the point. The
streaming exporter is documented in [`streaming.md`](streaming.md) and the determinism-
preserving pacer in [`streaming-pacing.md`](streaming-pacing.md); this doc owns the
mixer's mutable state, its frontier release scheduler, and its pure-core / async-shell
contract.

**Source:**
[`exporters/streaming/mixer/scheduler.py`](../../src/fabulexa_forge/exporters/streaming/mixer/scheduler.py)
(`Transport`, `TopicDials`, `ControlState`, `FrontierState`, `seed_mixer_run`, `advance`,
`schedule_releases`), consuming the streaming engine
([`engine.py`](../../src/fabulexa_forge/exporters/streaming/engine.py):
`iter_stream_events`, `build_topic_set`) and `StreamEvent`
([`types.py`](../../src/fabulexa_forge/exporters/streaming/types.py)) unchanged.
Tests:
[`tests/exporters/streaming/test_mixer.py`](../../tests/exporters/streaming/test_mixer.py).

## Boundary

- **Input.** A seeded set of per-topic FIFO buffers of `StreamEvent` (one whole-emit
  drain from `iter_stream_events`, partitioned by `topic`), a mutable `ControlState`
  (the operator dials), and an evolving `FrontierState`. Real time and delivery enter
  `schedule_releases` only through three injected callables — `monotonic`, `sleep`, and
  an async `sink` — so the loop is fully deterministic under fakes, exactly as the pacer
  is.
- **Output.** The same `StreamEvent` objects, unmodified, delivered to the async `sink`
  one at a time. The scheduler consumes each event by its `topic` (the dial / buffer key)
  and `event_sim_time` (the event-time key) and treats every other field as opaque
  payload; it reads, rewrites, and renders nothing else. It also exposes the read-only
  derived state — per-topic buffer depth (backlog), per-topic delivery edge, the master
  frontier — for a meters surface to render.
- **Reads only through the engine.** The mixer opens no `run.duckdb`, parses no
  `base.json`, and adds no coupling to the bundle: `seed_mixer_run` reaches the emit
  solely through `iter_stream_events` and `build_topic_set`, which read through the one
  base reader.
- **No config envelope, no Pydantic model, no new error class.** `ControlState` is
  *runtime* state layered over an existing `StreamConfig`, not config; it has no YAML
  surface. The only failure path is the engine's existing eager validation, surfaced as
  `ExportError` from `iter_stream_events` at drain time and left unwrapped for the caller.
- **Does not touch the pacer or `fabulexa-forge stream`.** `pace_events` and the streaming CLI
  verb stay deterministic and byte-identical; the mixer adds nothing to them and depends
  on none of their timing.

## Semantics

The mixer splits into a **pure synchronous per-tick function** (`advance`) and a **thin
async shell** (`schedule_releases`) that confines real time and the sink to injected
callables — the same separation `pace_events` uses. `advance` is "pure" in the sense of
referential transparency of its return value (invariant 4): it reads a snapshot of
`control`, mutates `frontier` in place, and pops from `buffers`, then returns the events
released this tick. The runtime dataclasses (`Transport`, `TopicDials`, `ControlState`,
`FrontierState`) and the function signatures are defined in
[`scheduler.py`](../../src/fabulexa_forge/exporters/streaming/mixer/scheduler.py).

### The master frontier

The frontier is a single integer-nanosecond event-time position shared by all topics —
the performance clock. It is stored as `FrontierState.frontier_sim_time` and is `None`
until the first tick observed with `transport.playing = True`.

- **Initialization tick** — the first tick with `playing = True`, taken iff
  `frontier_sim_time is None and transport.playing`. If every buffer is empty, the
  frontier is left uninitialized (`None`) and nothing releases. Otherwise it is set to
  the **global minimum `event_sim_time`** across all non-empty seeded buffers, each edge
  is initialized to `frontier − lag_T`, and **no frontier or edge advance is applied this
  tick** — but the per-topic release step still runs.
- **A subsequent playing tick** advances the frontier by
  `Δfrontier = int(speed · Δreal · 1e9)`, where `Δreal` is the **measured** real time
  elapsed since the previous tick (`monotonic` now minus the previous reading), not the
  nominal tick quantum. Measuring the true elapsed time keeps the frontier tracking
  `wall × speed` without accumulating drift across a slow tick.
- **A paused tick** (`playing = False`) holds the frontier and all edges and releases
  nothing; the shell still re-reads `monotonic` and refreshes its previous reading, so a
  paused interval is **discarded**, never banked into the first post-pause playing tick.

Integer nanoseconds are used because `event_sim_time` is raw ns at epoch magnitude, where
float64 loses ~100 ns of precision. The only quantization is the sub-nanosecond remainder
discarded by `int(speed · Δreal · 1e9)`, negligible at event-time (ms / s) scale.

### Per-topic release edge

Each topic `T` carries a release edge in integer nanoseconds (`FrontierState.edges[T]`),
`None` until the initialization tick sets it, monotonic non-decreasing thereafter. With
`lag_ns = lag_ms_T · 1_000_000`, the edge evolves each playing tick by the recurrence

> `edge_T ← max( edge_T,  min( frontier − lag_ns,  edge_T + int(rate_T · Δfrontier) ) )`

(the rate term is `int(rate_T · Δfrontier)` for an un-muted topic and `0` for a muted
one). This integer form is authoritative; the three terms unify the three dials:

- The inner `min(frontier − lag_ns, …)` is the **hard ceiling** — no stream ever leads
  the frontier, whatever `rate_T`. `lag_ms_T` is a fixed event-time offset: at steady
  state the edge trails the frontier by exactly `lag`.
- The `int(rate_T · Δfrontier)` term **rate-limits** the approach to the ceiling.
  `rate < 1` advances the edge slower than the frontier (the stream falls progressively
  behind, backlog grows); `rate > 1` advances faster but **only drains backlog** — once
  the edge reaches the ceiling, the ceiling binds and excess rate is inert.
- The outer `max(edge_T, …)` keeps the edge **monotonic**. Raising `lag_ms_T` at runtime
  drops the ceiling below the current edge; the `max` holds the edge, so already-delivered
  events stay delivered and no new event releases until the frontier rises enough that the
  ceiling re-passes the edge. Lowering `lag_ms_T` raises the ceiling and the edge resumes
  advancing, rate-limited by `rate_T`.

A **muted** topic (`mute_T = True`) holds its edge exactly: the rate term is zero, so the
`min` is `≤ edge_T` and the `max` returns `edge_T` identically. `rate_T = 0` un-muted is
the same held edge by the same arithmetic — mute and `rate = 0` are the two spellings of a
held edge, and backlog accumulates behind it, draining deterministically on un-mute or a
`rate` raised above zero.

After the edge is updated, topic `T` releases — in buffer (FIFO) order — every head event
whose `event_sim_time ≤ edge_T`, recording the last released event's `event_sim_time` as
its **delivery edge** (`FrontierState.delivery_edges[T]`).

### Release ordering within a tick

Topics release in `ControlState.topics` display order (`build_topic_set` order); within a
topic, eligible head events follow in FIFO order, which the engine guarantees equals
`seq` / `event_sim_time` order. Two events on **different** topics with the same
`event_sim_time` are ordered by their topics' edges, not their relative `seq`: cross-topic
arrival order is governed by lag, not by global `seq`.

### The async shell

`schedule_releases` drives the loop until every buffer is empty. It takes a baseline
`monotonic` reading before the first tick, so the first iteration's measured delta is
`0.0`. Each tick begins with the **termination check** — if every buffer is empty, return
immediately, before the `monotonic` read and the `advance` call, so a zero-event emit
returns before `advance` is ever invoked. Otherwise it reads `monotonic`, computes the
delta since the previous reading, stores this reading as the new previous reading **on
every tick including paused ticks** (so a pause's real duration is never folded forward),
calls `advance`, awaits `sink` once per released event in release order, then awaits
`sleep(tick_seconds)`.

Draining every buffer is the **only** internal exit: the coroutine carries no stop flag,
so a run that never drains — a permanently muted non-empty topic, or a launch left paused
— runs until the caller cancels the task. Because the loop awaits `sleep` every tick,
cancellation always has a suspension point to land on.

### Seeding

`seed_mixer_run` drains `iter_stream_events` exactly once into one FIFO buffer per topic
in `build_topic_set` order — declared-but-empty topics included — and builds the initial
`ControlState` (the launch `Transport` plus one **neutral** `TopicDials` per topic) and a
fresh `FrontierState` (`frontier_sim_time` `None`, an `edges` and a `delivery_edges` key
for every topic, all `None`). Because events arrive in global `seq` order, each topic's
buffer is already in `seq` / `event_sim_time` order. The buffering is whole-emit
in-memory, appropriate at sanitized-fixture scale; a lagged or muted stream accumulates
its backlog with no bound logic.

## Invariants

1. **No stream leads the frontier.** Every released event satisfies
   `event_sim_time ≤ frontier − lag_T` at its release tick, for every topic `T`. The
   `min(frontier − lag_ns, …)` ceiling enforces it unconditionally.
2. **Rate caps at the frontier; speed-up only drains backlog.** A topic's edge never
   exceeds `frontier − lag_T`, whatever `rate_T`. `rate_T > 1` advances the edge faster
   only while it is below the ceiling (i.e. while backlog exists); at the ceiling, excess
   rate is inert.
3. **Only arrival is perturbed.** Released `StreamEvent`s are identical in `seq`, `op`,
   `kind`, `record_id`, `presentation_id`, `event_sim_time`, `ts`, `after`, `topic`, and
   `route_table` to the seeded events. Within a topic, release order is `seq` order;
   across topics it may differ — the intended out-of-order arrival. No value, no
   `event_sim_time`, and no `seq` is ever rewritten.
4. **Determinism is relative to the injected clock and observed control.** `advance` is a
   pure function of `(buffers, control snapshot, frontier state, Δreal)`: the same seeded
   buffers, the same per-tick `Δreal` sequence, and the same `ControlState` values
   observed at each tick yield an identical released `(topic, event, tick-index)`
   sequence. The mixer makes no absolute-determinism guarantee over wall-clock or operator
   input — that is its declared purpose — but its core is fully reproducible under a fake
   clock and a scripted dial sequence, which is what makes it testable exactly as
   `pace_events` is.

These build on the engine guarantees the mixer relies on: `event_sim_time` is
non-decreasing in `seq` within a topic, and every `StreamEvent` carries `topic` and
`event_sim_time`.

## Validation Rules

No parse-time or business-rule validation is added. `Transport`, `TopicDials`, and
`ControlState` are runtime dataclasses, not Pydantic models; `advance` assumes every dial
is within its documented bounds and does not re-validate or clamp. The wire-level bounds —
`speed ∈ [0.1, 1000]`, `rate ∈ [0.0, 4.0]`, `lag_ms ∈ [0, 300000]`, unknown-topic
rejection — are enforced by the control-plane request models, not here. The three release
invariants are enforced **structurally** by `advance` (the `min` ceiling, the rate-limited
edge, the monotonic `max`), not by a validation runner. The only error path is the
engine's existing eager validation, surfaced as `ExportError` from `iter_stream_events` at
drain time and left to the caller to funnel.

## Rationale

- **A pure `advance` plus a thin async shell.** Confining the schedule math to a pure
  synchronous function — with real time and the sink injected into a separate loop — makes
  the three invariants unit-testable with no async, no sleeping, and no I/O, and mirrors
  how `pace_events` confines `sleep` / `monotonic` to its parameters. The riskiest
  semantics are provable before any HTTP or Kafka surface exists.
- **A sibling of `pace_events`, not an extension.** `pace_events` is a pure generator that
  must stay deterministic and byte-identical; the mixer is the deliberately
  non-deterministic, operator-driven counterpart. Welding a mutable clock and per-topic
  edges into `pace_events` would forfeit the streaming determinism invariant. Two
  functions share one idea — release scheduled against an event-time position — and
  nothing else.
- **One recurrence unifies lag, rate, and mute.** A single per-topic edge update — a
  `frontier − lag` ceiling, a rate-limited approach to it, and a monotonic clamp —
  expresses all three dials and their runtime changes without special cases. Lag is a
  fixed event-time offset, rate is a derivative limit on how fast the edge approaches the
  ceiling, and mute is `rate = 0` for the duration.
- **Integer-nanosecond frontier, measured per tick.** An integer frontier avoids the
  ~100 ns float64 precision loss at epoch magnitude; measuring actual elapsed real time
  each tick (rather than assuming the nominal quantum) keeps the frontier tracking
  `wall × speed` without drift.
- **Neutral dial seed is the identity transform.** Seeding `rate = 1`, `lag = 0`,
  `mute = False` makes the mixer at rest a faithful passthrough the operator perturbs
  *from*. These are runtime control rest positions, never author-specified scenario values
  (the educator writes none), so they are not a Principle-#7 default. The launch transport
  and the tick quantum, which the caller genuinely chooses, are required parameters with no
  default.
- **`FrontierState` separate from `ControlState`.** The operator owns the dials; only
  `advance` owns the derived schedule. Splitting them keeps the mutation surface
  (`ControlState`) free of schedule internals and gives the meters surface a clean
  read-only state (`FrontierState` plus buffer depths) to render from. One asyncio loop
  owns the state, so no lock is required.

## Boundaries

The mixer owns the headless scheduler and nothing downstream of it:

- **All I/O and serving.** No FastAPI, no asyncio serving topology, no run lifecycle
  wiring, no `mixer` verb. `schedule_releases` is the seam the control plane
  ([`mixer-control-plane.md`](mixer-control-plane.md)) builds on — it provides the
  injection points (`sink`, `sleep`, `monotonic`) but owns no transport.
- **Rendering and Kafka.** No `ts` / format rendering, no topic pre-creation, no keying,
  produce, poll, or flush. Those live behind the async `sink` a caller injects; the mixer
  deals only in `StreamEvent` objects.
- **The meters wire shape and its computation.** The mixer exposes the raw read-only state
  (per-topic backlog as buffer depth, per-topic delivery edge, the frontier); deriving
  delivery-lag and the meters envelope from it belongs to a separate surface.
- **Multi-content runs.** A run carries the one content axis of its `StreamConfig`; every
  `TopicDials.content` equals that run's content. Merging `state-changes` and
  `membership-events` into one mixer run is out of scope.
- **Bounded buffering, `seek`, `solo`, per-event warts (jitter / drop / duplicate),
  scenes / automation / record, and scripted-determinism replay.** The scheduler buffers
  whole-emit in memory and exposes only the transport and per-topic lag / rate / mute
  dials.

## Related

| Doc | Why |
|---|---|
| [`streaming.md`](streaming.md) | The streaming exporter whose `iter_stream_events` / `build_topic_set` / `StreamEvent` the mixer consumes unchanged as its event source and dial-set enumerator. |
| [`streaming-pacing.md`](streaming-pacing.md) | The pure, deterministic pacing sibling — the same pure-core / injected-clock split, the opposite determinism contract. |
| [`streaming-routing.md`](streaming-routing.md) | Defines the topic set (`build_topic_set`, declared-but-empty topics) the mixer seeds one buffer and one dial strip per. |
| [`mixer-control-plane.md`](mixer-control-plane.md) | The control plane that drives this scheduler — the run lifecycle, the HTTP control API, the async Kafka sink, and the meters derivation built over `schedule_releases`'s injection seam. |
