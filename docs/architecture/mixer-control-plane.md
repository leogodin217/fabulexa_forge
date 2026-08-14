# Mixer control plane

The driver that turns the headless mixer scheduler into a live, operator-driven
performance: a single-event-loop asyncio application that opens an emit, seeds the
scheduler, delivers to a Kafka broker, and serves the FabulMixer control API. An
operator plays / pauses / re-speeds the master transport and lags, rate-limits, or
mutes each topic mid-run, and reads producer-side meters back. It sits downstream of
the streaming engine and the mixer scheduler ([`streaming-mixer.md`](streaming-mixer.md)):
the scheduler owns the pure per-tick release semantics; this plane owns the run
lifecycle, the HTTP control surface, the async Kafka sink, and the meters derivation. It
is the deliberately non-deterministic, wall-clock- and operator-driven counterpart to
`fabulexa-forge stream` — a separate `fabulexa-forge mixer` verb, never a flag on `stream`.

**Source:**
[`exporters/streaming/mixer/`](../../src/fabulexa_forge/exporters/streaming/mixer/) —
[`run_state.py`](../../src/fabulexa_forge/exporters/streaming/mixer/run_state.py)
(`MixerRunState`),
[`wire.py`](../../src/fabulexa_forge/exporters/streaming/mixer/wire.py) (the request /
response models), [`sink.py`](../../src/fabulexa_forge/exporters/streaming/mixer/sink.py)
(`KafkaSink`), [`app.py`](../../src/fabulexa_forge/exporters/streaming/mixer/app.py)
(`derive_meters`, `build_app`),
[`serve.py`](../../src/fabulexa_forge/exporters/streaming/mixer/serve.py)
(`serve_mixer`); the shared render-closure builder `build_kafka_render_value`
([`driver.py`](../../src/fabulexa_forge/exporters/streaming/driver.py)); the
`MixerExtraUnavailable` error
([`errors.py`](../../src/fabulexa_forge/errors.py)); the `mixer` verb (`cmd_mixer`,
[`cli.py`](../../src/fabulexa_forge/cli.py)). Tests:
[`tests/exporters/streaming/mixer/`](../../tests/exporters/streaming/mixer/),
[`tests/test_cli_mixer.py`](../../tests/test_cli_mixer.py).

## Boundary

- **Input.** An emit directory, an existing `StreamConfig` (the same file
  `fabulexa-forge stream` reads), and operator HTTP requests. There is no mixer config
  envelope and no educator-facing YAML: a mixer run layers *runtime* state over the
  `StreamConfig`, and `StreamConfig.clock` is ignored (the transport replaces the
  pacer). Run-specific knobs — launch transport (`--speed` / `--play` / `--paused`),
  tick quantum (`--tick`), server bind (`--host` / `--port`), value format (`--fmt`),
  and Kafka bootstrap — are CLI flags, not config.
- **Output.** One Kafka message per released event: key `encode_pinned({"record_id":
  …})`, value the rendered `jsonl` / `debezium` bytes (byte-identical to the stream
  file-sink line minus its trailing newline), record timestamp
  `rebased_epoch_ms(event.event_sim_time, anchor)` (the `CreateTime` the meters
  predict). Plus the control-API JSON — operator state, meters, and the post-update
  echoes. Kafka is the sole sink.
- **Reads only through the engine.** The plane reaches the emit solely through
  `seed_mixer_run` → `iter_stream_events` / `build_topic_set`, which read through the
  one base reader; it opens no `run.duckdb` and parses no `base.json`. The emit is
  **closed after seeding** — the buffers hold every drained `StreamEvent`, the render
  closure has captured its anchor and Debezium schemas, and the sink needs only the
  producer, so the serving phase holds no DuckDB connection.
- **Optional extras, lazily imported.** The `[mixer]` extra (FastAPI + an ASGI server)
  composes the `[kafka]` extra (`confluent-kafka`); both are imported lazily, so
  importing `fabulexa_forge` or running any other verb requires neither. A missing
  `[mixer]` extra is `MixerExtraUnavailable`; a missing client is
  `KafkaClientUnavailable`.
- **The HTTP surface mirrors a shared wire contract.** The endpoints, request /
  response shapes, bounds, base path (`/api`), error codes, and polling cadence are
  owned by the cross-language control-API contract
  ([`pending/fabulmixer-control-api.md`](pending/fabulmixer-control-api.md)) that both
  this backend and the FabulMixer frontend bind to. This plane implements that contract;
  it does not define the wire shapes.

## Semantics

### Launch lifecycle (ordered)

`cmd_mixer` runs a synchronous setup phase, then an async serving phase. Setup failures
exit before any server binds; serving runs until the operator interrupts or the release
task fails. Steps 1–6 run inside the `(ReaderError, ExporterError)` funnel; steps 7–10
run under `asyncio.run(serve_mixer(...))`.

| Step | Phase | Action | On failure |
|------|-------|--------|------------|
| 1 | sync | Flag-level usage checks (see Validation Rules). | exit 1, before the funnel |
| 2 | sync | `load_stream_config(config_path)`. | `ConfigError` → exit 1 |
| 3 | sync | Resolve bootstrap-servers (`--bootstrap-servers` → `kafka` block → `FABEXPORT_KAFKA_BOOTSTRAP`). | `KafkaBootstrapUnresolvable` → exit 1 |
| 4 | sync | `open_emit`; resolve the effective anchor; enforce `KafkaRequiresAnchor` (anchor non-None). | `ReaderError` / `RebaseError` / `ExportError` → exit 1 |
| 5 | sync | Build the per-event `render_value` closure (Debezium rules run here). | `ExportError` → exit 1 |
| 6 | sync | Resolve `record_roles` from the sidecar, build the topic set, `seed_mixer_run(…)` — drain the engine into per-topic buffers, build `ControlState` + `FrontierState`. Close the emit. | `ExportError` → exit 1 |
| 7 | async | Probe the `[mixer]` extra — lazily import FastAPI + the ASGI server. | `MixerExtraUnavailable` → exit 1 (before the sink opens) |
| 8 | async | `KafkaSink.open(…)` — client check, topic pre-creation, create producer. | `KafkaClientUnavailable` / `KafkaDeliveryError` → exit 1 |
| 9 | async | Build the FastAPI app over the run state; bind the server to `host:port`. | `OSError` (e.g. port in use) → exit 1 |
| 10 | async | If launched playing, stamp the play origin; start the `schedule_releases` task with `sink = KafkaSink.deliver`; serve. | see Shutdown and error mapping |

A `--consumer` launch ([`mixer-consumer.md`](mixer-consumer.md)) adds steps to the same
phases, gated on the flag:

| Step | Phase | Action (when `--consumer`) | On failure |
|------|-------|----------------------------|------------|
| 6+ | sync | After `seed_mixer_run`: compute the non-empty topic set from the producer buffers, parse `--window` / `--join` into specs, `seed_consumer_run(…)` → `MixerRunState.consumer`, build the `ConsumerLaunch`. | `ExportError` → exit 1 |
| 8+ | async | After the sink opens: `KafkaSource.open(…)` — client check, subscribe. | `KafkaClientUnavailable` / `KafkaConsumeError` → exit 1 |
| 10+ | async | Start a second task `run_consumer(…)` alongside `schedule_releases`, under the same done-callback wiring. | see Shutdown and error mapping |
| shutdown | async | Cancel both tasks; `aclose()` both source and sink. | — |

### Concurrency and consistency

One asyncio event loop owns the run. `MixerRunState` is mutated by request handlers and
read by `advance`; both touch it only at **synchronous** points (a handler mutates the
dataclass with no intervening `await`; `advance` reads the whole snapshot with no `await`
mid-tick). The only suspension points in the release loop are between events (`await
sink`) and between ticks (`await sleep`). No lock is required, by construction. A
`--consumer` run adds a *second* control + derived-state pair (`MixerRunState.consumer`)
and a second async task to the same loop; the same synchronous-point discipline carries
to it ([`mixer-consumer.md`](mixer-consumer.md) § Driver and consistency).

| Condition | Result |
|-----------|--------|
| A `PUT` lands while the release task awaits `sink` between events | Visible to the **next** `advance` tick, never mid-tick; the current tick already read its `control` snapshot. |
| A `PUT` lands between ticks (during `sleep`) | Visible to the next tick. |
| `GET /state` or `GET /meters` runs concurrently with the release task | A consistent snapshot — the handler builds its response synchronously; no torn read. |
| Two `PUT`s race | Serialized by the single loop; last writer wins, the response echoes the post-update object. |

### Transport and dial mutations

| Endpoint | Effect | Response |
|----------|--------|----------|
| `PUT /api/transport` `{playing, speed}` | Set `transport.playing` and `.speed`. On a `False → True` transition with the play origin unset, stamp `play_origin_monotonic = monotonic()`. | `200` full `Transport`. |
| `PUT /api/topics/{topic}` `{rate, lag_ms, mute}` | Find the `TopicDials` whose `topic` equals the path segment; set `rate`, `lag_ms`, `mute`. Body `topic` / `content` are ignored — the path carries identity. | `200` full `TopicDials`. |
| `PUT /api/topics/{unknown}` | No dial matches. | `404`. |
| Any `PUT` with an out-of-bounds value | Rejected by the request model. | `422`; no mutation. |

The scheduler interprets the mutated dials on the next tick (the lag / rate / mute
recurrence in [`streaming-mixer.md`](streaming-mixer.md)). The control plane adds no
interpretation — it only writes the dials.

`build_app` always registers `GET /api/capabilities` (reporting `consumer_enabled =
state.consumer is not None`) so a client can discover the consumer surface. When the run
was launched with `--consumer`, it additionally registers the gated consumer routes —
`GET /api/consumer/state`, `GET /api/consumer/meters`, and `PUT
/api/consumer/topics/{topic}` (mutate the matching dial's `ingest_rate`; `404` unknown
topic, `422` out of bounds). Their shapes and meter semantics belong to the consumer
instrument ([`mixer-consumer.md`](mixer-consumer.md)); the producer endpoints and wire
shapes are unaffected.

### Meters derivation

Tier-1, producer-side, computed from raw scheduler state on every `GET /api/meters` (the
board polls at 5 Hz). No broker read; no consumer watermark. The anchor is resolved on
every mixer run (`KafkaRequiresAnchor`), so the two rendered timestamps are always
offset-bearing ISO-8601 strings.

| Field | Source | Null / zero cases |
|-------|--------|-------------------|
| `frontier_sim_time` | the frontier event-time, anchor-rendered | `null` before the first play tick (`frontier_sim_time is None`). |
| `wall_elapsed_ms` | `0` if no play origin, else `max(0, round((monotonic() − play_origin) × 1000))` | `0` until the first play transition; thereafter monotonic real time since play started, across later pauses. |
| `TopicMeter.backlog` | `len(buffers[topic])` | `0` for an empty topic and after drain. |
| `TopicMeter.delivery_lag_ms` | `(frontier − delivery_edge) // 1_000_000` (ns gap floored to ms) | `null` when the frontier is `None`, the topic's delivery edge is `None`, or the topic is declared-but-empty. Always `≥ 0` (Invariant 2). |
| `TopicMeter.delivery_edge_sim_time` | the topic's delivery edge, anchor-rendered | `null` before that topic's first delivery. |

`Meters.topics` covers the **same set in the same order** as `ControlState.topics`
(`build_topic_set` order), declared-but-empty topics included. The operator-visible
signal: lagging a topic makes its `delivery_lag_ms` climb (its delivery edge trails the
frontier by the lag) while an un-lagged topic stays near zero.

### Shutdown and error mapping

| Trigger | Behavior | Exit |
|---------|----------|------|
| `SIGINT` / Ctrl-C | The server begins shutdown; the lifespan hook cancels the `schedule_releases` task (cancellation lands on its per-tick `await sleep`), then `KafkaSink.aclose()` flushes and closes the producer. | 0 |
| Buffers fully drain | `schedule_releases` returns on its own; the server keeps serving (meters show `backlog 0`, frozen delivery edges) until the operator interrupts. | 0 (on later interrupt) |
| `KafkaDeliveryError` inside the sink | The release task fails; its done-callback flips the server's `should_exit` to begin shutdown; `aclose()` flushes / closes; after `server.serve()` returns, `serve_mixer` re-raises the stored exception, which the funnel maps to exit 1. | 1 |
| Never drains (permanent mute / launched paused, never played) | Runs until interrupted — the intended live-performance steady state. | 0 (on interrupt) |

### Edge cases

| Condition | Result |
|-----------|--------|
| Zero-event emit (every buffer empty at seed) | `schedule_releases` returns immediately (the scheduler's termination check before `advance`); the server still serves — every topic reports `backlog 0`, `delivery_lag_ms null`, `delivery_edge_sim_time null`, `frontier_sim_time null`. |
| Launched paused (`--paused`, the default) | Nothing releases; `frontier_sim_time` and all delivery edges stay `null`; the operator plays via `PUT /api/transport`, which stamps the play origin. |
| Declared-but-empty topic | A strip in `/state` and `/meters` (neutral dial, `None` edge), always `backlog 0`, pre-created in Kafka. |
| `--fmt debezium` without a `debezium` config block | `DebeziumRequiresConfig` at setup (step 5); exit 1 before any server binds. |
| `[mixer]` extra absent | `MixerExtraUnavailable` at the start of serving, before the sink opens. Because `[mixer]` composes `[kafka]`, this precedes `KafkaClientUnavailable` when both extras are absent, so the operator sees the one install hint that resolves both. |

## Invariants

1. **Lock-free consistency.** One event loop owns `MixerRunState`; `advance` and every
   request handler touch it only at synchronous points, so a dial edit lands atomically
   between ticks and no concurrent read tears.
2. **No meter ever shows a negative lag.** `delivery_lag_ms ≥ 0` whenever it is non-null,
   because no stream leads the frontier (the scheduler's ceiling invariant,
   [`streaming-mixer.md`](streaming-mixer.md) Invariant 1).
3. **Topic-set parity.** `Meters.topics` and `ControlState.topics` cover the same topics
   in the same `build_topic_set` order, declared-but-empty topics included.
4. **Emit closed after seeding.** The serving phase holds no reader resources; every
   released event comes from the in-memory buffers seeded before the server bound.

## Validation Rules

**Parse-time (the request models, [`wire.py`](../../src/fabulexa_forge/exporters/streaming/mixer/wire.py)).**
`TransportUpdate.speed ∈ [0.1, 1000]`; `TopicDialsUpdate.rate ∈ [0.0, 4.0]`,
`lag_ms ∈ [0, 300000]`. An out-of-bounds body is `422` with no mutation. The wire bounds
live **only** here — the scheduler assumes them and `advance` neither re-validates nor
clamps. The request models set `extra="ignore"` (not the config layer's strict base), so
a client may echo the read-only `topic` / `content` it last `GET`d; the path carries
identity.

**Flag-level usage checks (CLI, exit 1 before the funnel).** `--fmt ∈ {jsonl,
debezium}`; `--speed ∈ [0.1, 1000]`; `--tick > 0`; `--port ∈ [1, 65535]`.

**Business rules (setup, surfaced via the funnel as `ExportError`).**
`KafkaRequiresAnchor` (a mixer run requires a resolved anchor for the `CreateTime` and
meter rendering); `DebeziumRequiresConfig` (`--fmt debezium` requires a `debezium`
block); and the engine's eager validation at the `seed_mixer_run` drain (single-branch
guard, the per-stream resolvability rules, the election gates — see
[`streaming.md`](streaming.md) § Validation Rules).

**HTTP-level rules (serving).** `PUT /api/topics/{topic}` with no matching dial → `404`;
an out-of-bounds body → `422`; an unknown route / method → the framework default `404` /
`405`.

## Rationale

- **One event loop, no lock.** Because `advance` and every handler mutate / read
  `MixerRunState` only at synchronous points, a single loop serializes all access for
  free; a lock would add nothing. This is what carries the scheduler's lock-free design
  through to a live, mutated run.
- **The mixer owns its produce/poll loop.** `write_kafka_stream` drains its iterable and
  flushes once — correct for a finished stream, wrong for a pausable feed that releases
  in operator-driven bursts and runs until the operator quits. `KafkaSink` reuses the
  streaming sink's keying, record timestamp, topic pre-creation, delivery callback, and
  idempotent fully-acked producer config, but drives delivery one released event at a
  time behind the scheduler's injected `sink`, flushing only at shutdown.
- **Render closure shared, not duplicated.** The Debezium business rules and the
  value-render branch are factored into one builder, `build_kafka_render_value`, so the
  stream Kafka path and the mixer are two callers of one rule set, not a fork. The
  extraction is behavior-preserving: the stream path keeps its own `KafkaRequiresAnchor`
  check before calling the builder, so its error precedence and byte-for-byte output are
  identical.
- **Producer-side meters (tier-1).** The scheduler already exposes exactly the raw state
  the demo needs — buffer depth (backlog), per-topic delivery edge, the frontier — so the
  meters are a thin derivation, not a broker round-trip. `delivery_lag_ms` *predicts* a
  downstream watermark stall without reading `__consumer_offsets`; the real consumer
  watermark is off-broker, measured by the optional consumer instrument
  ([`mixer-consumer.md`](mixer-consumer.md)).
- **Kafka-only verb.** The verb targets a real broker because the out-of-order arrival it
  showcases is only observable to a downstream consumer, and the `CreateTime` ground truth
  the meters predict is a Kafka artifact. The `sink` injection seam keeps stdout / file
  reachable, but wiring an unused sink would be future scaffolding (Principle #8).
- **CLI launch defaults are rest positions.** `--speed 1.0`, `--paused`, `--tick 0.05`
  are the neutral, safest launch state — the same category as the scheduler's neutral
  dial seed, not Principle-#7 scenario values (which a `StreamConfig` carries). The
  `Transport` constructor and `schedule_releases` still take them as required arguments
  with no function-level default.
- **Emit closed after seeding.** `seed_mixer_run` drains the whole engine into in-memory
  buffers, so the long-running server holds no live DuckDB connection.

## Boundaries

The control plane owns the run lifecycle, the HTTP surface, the Kafka sink, and the
meters; it deliberately does not own:

- **The release semantics.** The per-tick lag / rate / mute / frontier arithmetic lives
  in the scheduler ([`streaming-mixer.md`](streaming-mixer.md)). The plane only writes
  dials and reads derived state.
- **Determinism.** The plane makes no determinism guarantee — it is wall-clock- and
  operator-driven by construction, which is its declared purpose and the reason it is a
  separate verb from the byte-identical `fabulexa-forge stream` and `pace_events`.
- **Non-Kafka sinks.** The verb is Kafka-only; the `sink` injection seam keeps other
  sinks reachable, but none other is wired.
- **A consumer / broker watermark.** The control-plane meters are producer-side tier-1
  only; the true off-broker consumer watermark is the optional consumer instrument's
  ([`mixer-consumer.md`](mixer-consumer.md)), a separate control + derived-state surface,
  not part of the producer meters.
- **The wire contract shapes.** Endpoints, bounds, base path, error codes, and polling
  cadence are owned by the shared control-API contract
  ([`pending/fabulmixer-control-api.md`](pending/fabulmixer-control-api.md)); the plane
  is the backend mirror.
- **Multi-content runs and richer control.** A run carries the one content axis of its
  `StreamConfig`. Solo, `seek`, scenes / automation / record, per-event warts, bounded
  buffering, auth, and server-push transports (SSE / WebSocket) are not part of the
  control surface.

## Related

| Doc | Why |
|-----|-----|
| [`streaming-mixer.md`](streaming-mixer.md) | The headless scheduler this plane drives — `seed_mixer_run` / `advance` / `schedule_releases` and the four runtime dataclasses, consumed as-is. |
| [`mixer-consumer.md`](mixer-consumer.md) | The optional consumer-side instrument this plane hosts under `--consumer` — the second control + derived-state pair, the `KafkaSource`, and the consumer meters / routes. |
| [`streaming.md`](streaming.md) | The streaming exporter whose Kafka sink building blocks and shared render-closure builder (`build_kafka_render_value`) the mixer reuses. |
| [`pending/fabulmixer-control-api.md`](pending/fabulmixer-control-api.md) | The shared cross-language wire contract this backend mirrors — endpoints, request / response shapes, bounds, base path, polling cadence. |
| [`anchor.md`](anchor.md) | The effective-anchor resolution the mixer requires for the Kafka `CreateTime` and meter timestamp rendering. |
