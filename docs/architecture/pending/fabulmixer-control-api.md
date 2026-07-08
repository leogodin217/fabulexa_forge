# FabulMixer control API — the frontend/backend seam

**Status:** pending (POC). **Owner of this contract:** shared. This document is the
frozen interface between two parallel work-streams — the FabulMixer **backend
driver** (the frontier scheduler + `ControlState` + FastAPI app) and the **POC web
frontend** (the perform-board). Both sides bind to the shapes below; neither may
change them without updating this doc.

Concept + scope live in the `note` vault: `fabulmixer-live-perform-poc` and
`fabulmixer-streaming-control-board`. This doc is *only* the HTTP surface.

## Why a doc and not "code is truth"

This seam crosses a language boundary (Python ⇄ TypeScript), so neither side's code
is the natural single source. This doc is the truth; each side mirrors it:

| Side | Mirror of this contract |
|---|---|
| Frontend | `frontend/src/api/types.ts` (TypeScript interfaces) |
| Backend | the FastAPI Pydantic request/response models |

When the contract changes, update this doc first, then both mirrors.

## Conventions

- **Base path:** `/api`. All paths below are relative to it.
- **Encoding:** `application/json; charset=utf-8`, request and response.
- **Errors:** FastAPI default shapes — no custom exception handlers, so two shapes
  reach the client and the type of `detail` depends on the error class. `404` (and any
  `HTTPException` — unknown topic on a mutation, consumer off) → `{ "detail":
  "<message>" }`, a **string**. `422` for a value outside its validation bounds is a
  `RequestValidationError` → `{ "detail": [ { "type", "loc", "msg", … }, … ] }`, a
  **list** of error objects. The frontend's error helper must handle both (string
  `detail` as-is; list `detail` → join the inner `msg` fields) — reading `detail` as a
  string unconditionally renders `[object Object]` on any `422`.
- **Polling:** the board GETs `/meters` at **5 Hz** (the money demo needs the
  consequence visible "half a second after the knob turns"). `/state` is fetched on
  load and re-fetched after each mutation; it is not polled.
- **No determinism guarantee.** This API drives the FabulMixer driver, which is
  deliberately wall-clock- and operator-driven and therefore non-deterministic. It is
  a *separate* surface from the deterministic, byte-identical `fabexport stream`.

## Data model

The canonical JSON shapes. Field names are the wire contract.

### `Transport` — the master section

| Field | Type | Bounds | Meaning |
|---|---|---|---|
| `playing` | boolean | — | Is the master frontier advancing through event-time. |
| `speed` | number | `0.1 ≤ speed ≤ 1000` | Event-time advance per unit real time. UI: log-scale slider, detent at `1.0`. |

### `TopicDials` — one channel strip's operator controls

| Field | Type | Bounds | Meaning |
|---|---|---|---|
| `topic` | string | — | **Read-only identity.** The routing topic; the dial key. |
| `content` | enum | `state-changes \| membership-events` | **Read-only.** Which content axis feeds this topic. |
| `rate` | number | `0.0 ≤ rate ≤ 4.0` | Per-stream release-rate multiplier. UI: vertical fader, detent at `1.0`. `>1` only drains backlog (the backend caps a stream's release edge at the frontier). |
| `lag_ms` | integer | `0 ≤ lag_ms ≤ 300000` | Delivery lag for this stream, in **event-time** milliseconds — subtracted from the master frontier: a stream releases events whose `event_sim_time ≤ frontier − lag`. At `speed = 1` this equals wall-clock ms; above `1` it drains proportionally faster. UI: horizontal slider + numeric box. The money-demo knob; the `300000` (5 min) ceiling covers the "make this stream arrive 5 minutes behind" example — track it to the demo emit's time scale. |
| `mute` | boolean | — | Stop releasing this stream; backlog accumulates and drains on un-mute / speed-up. UI: toggle button. |

`topic` / `content` are identity and presentation — they are returned by
GET but **ignored** in mutation request bodies (the path carries the topic).

There is no `label` field: the routing surface produces topic *names*, not labels, and
a backend-invented label would violate *reshape, never fabricate* — the frontend owns
display naming from `topic`.

### `ControlState` — the full operator state

| Field | Type | Meaning |
|---|---|---|
| `transport` | `Transport` | The master section. |
| `topics` | `TopicDials[]` | One entry per routed topic, in stable display order. |

### `TopicMeter` — one channel strip's read-only meters

| Field | Type | Bounds | Meaning |
|---|---|---|---|
| `topic` | string | — | Matches a `TopicDials.topic`. |
| `backlog` | integer | `≥ 0` | Buffered events not yet released/delivered for this stream. `0` for a topic with no events. |
| `delivery_lag_ms` | integer \| null | `≥ 0` | **Producer-side** delivery lag: the event-time gap (ms) between the master frontier and this stream's delivered edge. It *predicts* a downstream watermark stall; it is **not** a consumer watermark (none exists at the producer). `null` until this stream's first delivery, and for a declared-but-empty topic. |
| `delivery_edge_sim_time` | string \| null | ISO 8601 | Event-time of the last delivered event for this stream — the producer-side **delivery edge** (renamed from `watermark_sim_time`: the producer emits no watermark). `null` before first delivery. |

### `Meters` — the full read-only snapshot (polled)

| Field | Type | Bounds | Meaning |
|---|---|---|---|
| `frontier_sim_time` | string \| null | ISO 8601 | Event-time position of the master frontier; `null` before first play. |
| `wall_elapsed_ms` | integer | `≥ 0` | Real time elapsed since play started. |
| `topics` | `TopicMeter[]` | — | One entry per topic, same set/order as `ControlState.topics`. |

## Consumer side (optional — present only when launched with `--consumer`)

The producer shapes above are always served. The shapes and endpoints below exist
**only** when the run was launched with `--consumer` (the consumer-side instrument; see
the `fabulmixer-consumer-side-instrument` note and
[`pending/mixer-consumer-instrument.md`](mixer-consumer-instrument.md)). A producer-only
run serves none of them — the frontend discovers the mode via `GET /api/capabilities`
and renders the consumer panel conditionally.

### `Capabilities` — feature discovery (always served)

| Field | Type | Meaning |
|---|---|---|
| `consumer_enabled` | boolean | Whether the consumer subsystem is running this session. The frontend gates the consumer panel on this. |

### `ConsumerTopicDials` — one strip's consumer control

| Field | Type | Bounds | Meaning |
|---|---|---|---|
| `topic` | string | — | **Read-only identity.** Matches a producer `TopicDials.topic`. |
| `content` | enum | `state-changes \| membership-events` | **Read-only.** For UI grouping / parity. |
| `ingest_rate` | number | `0.0 ≤ r ≤ 10000.0` | Messages/sec this topic's consumer pulls. `0` pauses ingestion (backlog accumulates). The consumer's *only* control. UI: vertical fader, `0` at the bottom. |

`topic` / `content` are identity and presentation — returned by GET but **ignored** in
the mutation body (the path carries the topic).

### `ConsumerControlState` — the full consumer operator state

| Field | Type | Meaning |
|---|---|---|
| `topics` | `ConsumerTopicDials[]` | One entry per routed topic, same set/order as producer `ControlState.topics`. |

### `ConsumerTopicMeter` — one strip's consumer-side meters

| Field | Type | Bounds | Meaning |
|---|---|---|---|
| `topic` | string | — | Matches a `ConsumerTopicDials.topic`. |
| `watermark_sim_time` | string \| null | ISO 8601 | The **real** consumer watermark: highest event-time ingested for this topic. `null` before first ingest / declared-but-empty. (Unlike the producer's `delivery_edge_sim_time`, this *is* a watermark — measured from honest broker ingestion.) |
| `consumer_lag` | integer | `≥ 0` | Real broker backlog: messages delivered but not yet pulled (`end_offset − position`). The signature of a throttled / paused consumer. |

### `WindowMeter` — one declared window's firing summary

| Field | Type | Bounds | Meaning |
|---|---|---|---|
| `size_ms` | integer | `> 0` | Declared tumbling size (event-time ms), echoed from launch. |
| `fired_count` | integer | `≥ 0` | Windows fired so far — monotonic; stalls when the global watermark stalls. The "results stop emitting" gauge. |
| `latest_window_end_sim_time` | string \| null | ISO 8601 | End event-time of the most recently fired window; `null` before first firing. |

### `JoinMeter` — one declared fact/dimension join's null health

| Field | Type | Bounds | Meaning |
|---|---|---|---|
| `fact_topic` / `dimension_topic` | string | — | The declared pairing. |
| `fact_count` | integer | `≥ 0` | Fact records ingested. |
| `null_count` | integer | `≥ 0` | Facts whose dimension watermark had not caught up (resolve to null). |
| `null_rate` | number \| null | `0.0 ≤ r ≤ 1.0` | `null_count / fact_count`; `null` when `fact_count == 0`. Climbs as the dimension stream lags — the "enrichment returns nulls" beat. |

### `ConsumerMeters` — the full consumer read-only snapshot (polled at 5 Hz)

| Field | Type | Bounds | Meaning |
|---|---|---|---|
| `global_watermark_sim_time` | string \| null | ISO 8601 | The pipeline watermark = `min` across data-bearing topics; `null` while any data-bearing topic has not yet ingested its first record. The headline "pipeline freeze" gauge. |
| `topics` | `ConsumerTopicMeter[]` | — | One entry per topic, same set/order as `ConsumerControlState.topics`. |
| `windows` | `WindowMeter[]` | — | One per declared window, in `--window` declaration order (stable for the run). |
| `joins` | `JoinMeter[]` | — | One per declared join, in `--join` declaration order (stable for the run). |

Window sizes and join pairings are **job shape** declared at launch (`--window` /
`--join`), not performance dials — they are fixed for the run, like the topic set.
The `windows` / `joins` arrays keep that declaration order for the life of the run, so
the frontend keys each gauge by array position — a `WindowMeter` carries no id of its
own, and two windows declared with the same `size_ms` are distinguished only by
position.

## The topic set is dynamic

The number of channel strips is **not fixed** — it equals the number of topics the
routing surface resolves for the loaded emit + `StreamConfig`/`RoutingConfig`. It
varies per emit and per config; neither side hardcodes a count. The backend's
enumeration entry point is `build_topic_set(config, record_roles)`
(`src/fabulexa_export/exporters/streaming/engine.py`) — the same set `fabexport
stream` materializes.

- `GET /state.topics` is the **authoritative, complete** topic set. The frontend
  derives the strip count from it and renders dynamically.
- `GET /meters.topics` covers the **same** set, in the **same order**.
- The backend enumerates topics once, at startup (when it loads the emit and resolves
  routing); the set is then stable for the life of the run.

**Resolved (yes):** *declared-but-empty* topics (a routing feature — topics with zero
events for this emit) **do appear as strips**. This is a UX call, not a backend
constraint: with whole-emit in-memory buffering the backend knows each topic's event
count right after the startup load, and every topic in the set is pre-created in Kafka
regardless, so a strip for an empty topic is faithful to what
exists — and an operator may want to mute/lag a channel ahead of traffic. An empty
topic reports `backlog: 0` and `delivery_lag_ms: null` / `delivery_edge_sim_time: null`
until its first delivery. The per-strip real-estate counterargument does not bite at
POC scale (topic counts stay at **5–8**); revisit a density/scroll layout only if a
later use case pushes the count higher.

## Endpoints

| Method | Path | Request body | Success | Response body | Errors |
|---|---|---|---|---|---|
| `GET` | `/state` | — | `200` | `ControlState` | — |
| `GET` | `/meters` | — | `200` | `Meters` | — |
| `PUT` | `/transport` | `{ playing, speed }` | `200` | `Transport` (full, post-update) | `422` bounds |
| `PUT` | `/topics/{topic}` | `{ rate, lag_ms, mute }` | `200` | `TopicDials` (full, post-update) | `404` unknown topic · `422` bounds |
| `GET` | `/capabilities` | — | `200` | `Capabilities` | — |
| `GET` | `/consumer/state` | — | `200` | `ConsumerControlState` | `404` consumer off |
| `GET` | `/consumer/meters` | — | `200` | `ConsumerMeters` | `404` consumer off |
| `PUT` | `/consumer/topics/{topic}` | `{ ingest_rate }` | `200` | `ConsumerTopicDials` (full, post-update) | `404` consumer off / unknown topic · `422` bounds |

- `/capabilities` is **always** served (both modes); the four `/consumer/*` rows exist
  only when the run was launched with `--consumer` and otherwise return `404`.
  `/capabilities` is fetched **once on app load, before any panel mounts** — it decides
  whether the consumer panel renders at all — and is never polled or re-fetched. The
  consumer board polls `/consumer/meters` at the same 5 Hz as `/meters`;
  `/consumer/state` follows the producer `/state` cadence — fetched on consumer-panel
  mount and re-fetched after each consumer mutation, never polled.

- Mutations are **full-object PUTs** (the client holds authoritative dial state and
  sends the whole section). No partial/PATCH merge in the POC.
- A mutation response echoes the **post-update** object — the authoritative server
  state after the write — so the client can reconcile its optimistic dial state
  without a re-GET. Out-of-range values are rejected with `422` (see Conventions),
  never clamped, so an accepted echo always equals the request.

## Deferred (not in this contract version)

- `POST /reset` (snap all dials to neutral) — nice for the demo, add when needed.
- `solo`; per-event warts (jitter/drop/duplicate); scenes/automation/record.
- SSE/WebSocket push for meters — POC polls; revisit if 5 Hz polling reads poorly.
- Auth — the board is single-operator on a trusted LAN for the POC.

## Frontend binding note

The frontend develops against this contract **with no backend** via an `api` module
with two implementations behind one interface: `mockApi` (an in-memory simulated
frontier so meters actually move when dials turn — lets the money demo be performed
client-side today) and `httpApi` (real `fetch` to this surface). A build flag selects
between them; swapping to `httpApi` when the backend lands changes nothing else.

Both implementations cover the **whole** surface, producer and consumer:

- `httpApi` adds `GET /capabilities` and the four `/consumer/*` rows alongside the
  producer endpoints. It reports `consumer_enabled` from the real `/capabilities`
  response, so the consumer panel renders only against a `--consumer` backend.
- `mockApi` reports `consumer_enabled: true` and simulates the consumer-side beats the
  same way it simulates the producer frontier, porting the rules from
  [`pending/mixer-consumer-instrument.md`](mixer-consumer-instrument.md) § Semantics.
  The headline consumer move — the **pipeline freeze** — must move client-side: as a
  topic's `ingest_rate` drops toward `0` its `consumer_lag` climbs and its
  `watermark_sim_time` stalls, which stalls `global_watermark_sim_time` (the `min`
  across data-bearing topics), which freezes `WindowMeter.fired_count` and drives
  `JoinMeter.null_rate` upward. Reproducing that chain in the mock is what lets the
  consumer money demo, like the producer one, be performed client-side today.
