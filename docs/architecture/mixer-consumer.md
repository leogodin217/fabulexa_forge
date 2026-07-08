# Mixer consumer-side instrument

The downstream half of the FabulMixer live performance: an optional, operator-driven
**pure timing simulator** that runs inside the same `fabexport mixer` asyncio app and
makes the downstream *consequence* of a delivery perturbation visible. It subscribes a
real Kafka consumer to the producer's topic set, pulls each topic at an operator-set
throttle, and from the per-topic ingestion positions derives a global watermark that
stalls, windowed output that stops firing, and an enrichment join whose null-rate
climbs. It is the deliberately non-deterministic sibling of the producer release loop,
exactly as `schedule_releases` is of `pace_events`: the producer side perturbs delivery
and watches producer-side meters; this side watches the pipeline freeze. It is gated by
the `--consumer` launch flag; absent that flag, a mixer run is producer-only.

**Source:**
[`exporters/streaming/mixer/`](../../src/fabulexa_export/exporters/streaming/mixer/) —
[`consumer.py`](../../src/fabulexa_export/exporters/streaming/mixer/consumer.py) (the
runtime dataclasses, `seed_consumer_run`, the pure `ingest`, the async `run_consumer`),
[`source.py`](../../src/fabulexa_export/exporters/streaming/mixer/source.py)
(`KafkaSource`),
[`app.py`](../../src/fabulexa_export/exporters/streaming/mixer/app.py)
(`derive_consumer_meters`, the gated consumer routes),
[`wire.py`](../../src/fabulexa_export/exporters/streaming/mixer/wire.py) (the consumer
request / response models); the `KafkaConsumeError`
([`errors.py`](../../src/fabulexa_export/errors.py)). Tests:
[`tests/exporters/streaming/mixer/`](../../tests/exporters/streaming/mixer/)
(`test_mixer_consumer.py`, `test_mixer_consumer_app.py`,
`test_mixer_consumer_meters.py`, `test_mixer_consumer_wire.py`,
`test_mixer_source.py`).

## Boundary

- **Input.** The delivered event stream, read back off the Kafka broker — never the
  base-layer bundle, the emit, or the vendored `contract/`. The instrument has the same
  outside-the-package status the perform-board frontend has: it consumes what the
  producer published, not what produced it. Plus the operator's per-topic `ingest_rate`
  dials over HTTP.
- **Reads only timing metadata.** For each pulled record the source reads only its
  `.topic()`, `.timestamp()` (the producer's `CreateTime`, epoch-ms), and `.offset()`.
  The record key and value are never inspected — there is no payload read, no
  deserialization, no key matching, and no state store.
- **Output.** The consumer control-API JSON — the per-topic `ingest_rate` dials, and the
  derived meters: per-topic and global watermarks, per-topic broker lag, per-window
  firing counts, and per-join null health. These ride dedicated `/api/consumer/*`
  endpoints and an always-present `/api/capabilities` discovery endpoint; the producer
  wire shapes are untouched.
- **No new install extra.** The instrument uses only `confluent-kafka`, already covered
  by the `[kafka]` extra; the gate is the existing `[mixer]` extra composing `[kafka]`.
- **Shares the producer's event loop.** The consumer runs as a second async task on the
  same single asyncio loop as the release task, with a second control + derived-state
  pair; it is not a separate process or loop. The control plane
  ([`mixer-control-plane.md`](mixer-control-plane.md)) owns the launch lifecycle, the
  HTTP surface registration, and the shutdown wiring that drive it.

## Semantics

The instrument mirrors the producer's pure-core / async-shell split: a pure per-tick
fold (`ingest`, sibling of the scheduler's `advance`) over an async driver
(`run_consumer`, sibling of `schedule_releases`). The runtime dataclasses, signatures,
and docstrings are the contract; they live in
[`consumer.py`](../../src/fabulexa_export/exporters/streaming/mixer/consumer.py). The
state split mirrors the producer's: the operator API mutates `ConsumerControlState`
(the dials); only `ingest` mutates `ConsumerState` (the derived timing state);
`ConsumerJobShape` is the immutable launch-declared windows / joins / gating set.

### Ingestion and throttle

A tick pulls up to `int(ingest_rate × delta_real_seconds)` records per topic, with the
fractional remainder carried to the next tick. `ingest_rate == 0` pauses a topic:
nothing is pulled, its backlog accumulates on the broker, and its watermark holds.
Per-topic throttling is realized at the source by pausing the partitions of any topic
whose tick budget is exhausted. When the producer delivers a topic faster than the
consumer pulls it, that topic's `consumer_lag` — the real broker backlog
(`end_offset − position`) — grows.

### Watermark

A topic's watermark is the maximum event-time ingested for it — and, because per-topic
order is trusted, that is simply the last ingested record's event-time. It is `null`
before the topic's first ingest. The **global** pipeline watermark is the `min` across
the watermarks of the **data-bearing** topics — those non-empty in this emit
(`ConsumerJobShape.gating_topics`) — and is `null` while any data-bearing topic has not
yet ingested its first record. A declared-but-empty topic is excluded from the global
`min`: it carries no events to gate on, so an empty stream never freezes the pipeline.
The watermark is measured, never set — it is an output of ingestion only; the operator
controls ingestion, never the watermark directly.

### Windowing

A window is a tumbling size in event-time milliseconds, declared at launch (job shape,
not a dial); multiple windows may be declared. Its origin is the global watermark's
first value. When the global watermark crosses a window's end
(`window_end ≤ global watermark`), the window fires: its `fired_count` increments and
`latest_window_end` advances. When the global watermark stalls — a paused, throttled, or
starved data-bearing topic holding the `min` — no further windows fire and `fired_count`
freezes. This is the operator-visible signal that results stop emitting.

### Enrichment join

A join is a declared fact/dimension topic pairing and is a **timing dependency only** —
no key matching, no aggregation, no state store. For each fact record ingested, the
matching join's `fact_count` increments; if the dimension topic's end-of-tick watermark
is `null` or `< t_fact` (the dimension stream has not caught up), the fact resolves to
null and `null_count` increments. As the dimension topic lags, more facts find it
behind, so `null_rate = null_count / fact_count` climbs.

### Meters

`derive_consumer_meters` computes the consumer meters snapshot from raw consumer state,
pure with respect to its inputs. It renders each per-topic watermark and the global
watermark from epoch-ms through the anchor zone; reports per-topic `consumer_lag`
verbatim; emits one window meter per declared window (size, `fired_count`,
anchor-rendered latest end) and one join meter per declared join (`fact_count`,
`null_count`, `null_rate = null/fact` or `None`). Topics are reported in
`ConsumerControlState.topics` order. The response shapes are the consumer wire models in
[`wire.py`](../../src/fabulexa_export/exporters/streaming/mixer/wire.py); like the
producer wire models they are plain Pydantic `BaseModel`s (not the config layer's strict
base) and request bodies set `extra="ignore"`.

### Driver and consistency

`run_consumer` drives the loop until the caller cancels its task — unlike the producer
loop there is no drain-termination, because a live consumer has no end. Each tick it
reads the control snapshot, computes a per-topic pull budget from `ingest_rate × measured
real delta` (fractional carry retained across ticks), pulls via `source.pull` and reads
backlog via `source.lag` (both off-loop I/O), calls `ingest`, then sleeps the tick
quantum. Its sleep and monotonic clock are injected so tests drive it with fakes.

The consumer adds a **second** control + derived-state pair to the one event loop. As
with the producer, request handlers mutate `ConsumerControlState` and `ingest` reads it,
both only at synchronous points (no intervening `await`); only `ingest` mutates
`ConsumerState`. The consumer task suspends only between ticks (`await pull` / `await
sleep`). No lock is required: a dial edit lands atomically between ticks and no
concurrent read tears. The launch lifecycle that seeds this pair and starts the task is
documented with the producer's in
[`mixer-control-plane.md`](mixer-control-plane.md) § Launch lifecycle.

### Edge cases

| Condition | Result |
|-----------|--------|
| `--consumer` absent | Producer-only run; no `KafkaSource` opens, no consumer task runs, consumer routes are unregistered, and `/api/capabilities` reports `consumer_enabled: false`. |
| All data-bearing topics paused from launch | Global watermark stays `null`; no window fires; every join `null_rate` stays `null` (no facts ingested) — the pipeline never starts. |
| `--window` / `--join` given without `--consumer` | Usage error → exit 1 before the funnel. |
| A `--join` names a topic absent from the topic set | `ExportError` at `seed_consumer_run` (setup) → exit 1. |
| Consumer poll / offset read fails mid-run | `KafkaConsumeError`; the consumer task's done-callback flips `should_exit`; `serve_mixer` re-raises it → exit 1. |

## Invariants

1. **Watermark is measured, not prescribed.** Per-topic and global watermarks are
   functions of ingestion only; the operator's sole control is `ingest_rate`.
2. **No payload is ever read.** `KafkaSource` reads only `.topic()` / `.timestamp()` /
   `.offset()`; no deserialization, key match, aggregation, or state store exists.
3. **Global watermark = `min` across data-bearing topics.** A single throttled or paused
   data-bearing topic stalls the whole pipeline; declared-but-empty topics are excluded.
4. **Lock-free consistency.** One event loop owns the consumer's control + derived
   pair; `ingest` and every consumer handler touch it only at synchronous points.
5. **Two-sided causality.** Producer perturbation (rate / lag / mute) and consumer
   throttle both flow through the same per-topic ingestion position to the same
   watermark, so a topic falls behind whether the producer stopped sending it or the
   consumer stopped pulling it.
6. **No coupling widening.** The consumer reads the broker, never the bundle or
   `contract/`.

## Validation Rules

**Flag-level usage checks (CLI, exit 1 before the `(ReaderError, ExporterError)`
funnel).** `--window` / `--join` require `--consumer`; each `--window` value must be
`> 0`; each `--join` must parse as exactly `fact_topic:dimension_topic` (one `:`);
`--consumer-offset` must be `earliest` or `latest`.

**Business rules (setup, surfaced via the funnel as `ExportError` → exit 1).** Both
sides of every join must resolve to a topic in the topic set (`join-topics-resolve`);
every window size must be `> 0` (`window-positive`). These are enforced at
`seed_consumer_run`.

The consumer dial bound (`ingest_rate ∈ [0.0, 10000.0]`) lives only at the wire
(`PUT /api/consumer/topics/{topic}` → `422` out of bounds, `404` unknown topic); the
loop assumes an in-range value.

## Rationale

- **A pure timing simulator, not a stream processor.** The lesson is manipulating
  ingestion *live, with a knob, and seeing the watermark respond*. A production stream
  processor makes ingestion redeploy-time job config, not a mid-performance dial — which
  would defeat the demo — so the instrument reads only timing positions and derives
  consequences arithmetically; it never deserializes or joins on keys.
- **Two-sided causality is the lesson.** The textbook "one slow stream stalls the whole
  pipeline" result (a global watermark = `min` flatlining, windows freezing, joins
  returning nulls) needs a downstream consumer to be visible at all; the producer-side
  meters can only *predict* it. Routing both producer perturbation and consumer throttle
  through the same per-topic position to the same watermark makes the cause symmetric and
  the consequence one signal.
- **Watermark = `min` across data-bearing topics only.** A declared-but-empty topic
  carries no events, so gating the pipeline on it would freeze a demo that never had data
  to wait for; excluding it keeps the `min` over exactly the streams that can stall.
- **Mirrors the producer's pure-core / async-shell split.** `ingest` / `run_consumer` /
  `ConsumerControlState` / `ConsumerState` parallel `advance` / `schedule_releases` /
  `ControlState` / `FrontierState`, so the lock-free single-loop reasoning and the
  testability (pure fold + injected clock) apply identically.

## Boundaries

The instrument owns the consumer timing simulation and its meters; it deliberately does
not own:

- **Any payload semantics.** No key matching, aggregation, deserialization, or state
  store — the join is a watermark-reached check, not a relational join. Measuring real
  enrichment correctness is outside this surface.
- **The watermark as a control.** The operator turns `ingest_rate` only; the watermark,
  window firings, and join health are strictly derived and never directly set.
- **The base-layer coupling.** The consumer reads the delivered stream off the broker; it
  never opens the bundle or the vendored `contract/`. Widening the coupling is out.
- **Determinism.** Like the producer side, the instrument is wall-clock- and
  operator-driven; scripted determinism is deferred, inherited from the producer side.
- **The run lifecycle and HTTP registration.** Seeding the consumer pair, opening the
  source, starting / cancelling the consumer task, and registering the gated routes are
  the control plane's ([`mixer-control-plane.md`](mixer-control-plane.md)); this surface
  defines the timing semantics, the source, and the meter shapes those steps wire in.

## Related

| Doc | Why |
|-----|-----|
| [`mixer-control-plane.md`](mixer-control-plane.md) | The driver that hosts this instrument — the shared event loop, the launch lifecycle that seeds and starts the consumer task, the HTTP surface that registers the gated routes, and the shutdown wiring. |
| [`streaming-mixer.md`](streaming-mixer.md) | The producer-side headless scheduler whose `advance` / `schedule_releases` / `ControlState` / `FrontierState` split this instrument mirrors. |
| [`pending/fabulmixer-control-api.md`](pending/fabulmixer-control-api.md) | The shared cross-language wire contract this backend mirrors — the consumer data-model shapes and `/api/consumer/*` + `/api/capabilities` endpoints. |
| [`anchor.md`](anchor.md) | The effective-anchor resolution the consumer meters render watermarks through. |
