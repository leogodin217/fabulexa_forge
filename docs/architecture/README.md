# Architecture

Design index for the Fabulexa composite export package. Read `../../CLAUDE.md` first
for principles, the boundary, and vocabulary. For the feature inventory + status (what
each mode does, what's shipped), see [`../CAPABILITIES.md`](../CAPABILITIES.md); this
doc owns the design and build order.

## Per-subsystem design docs

| Doc | Subsystem |
|---|---|
| [`bundle.md`](bundle.md) | The input, understood — consumer-side orientation to the bundle: where emit data comes from, table-genre semantics, inherited guarantees, mechanism vs presentation columns, single-branch facts. Informational companion to the vendored contract |
| [`reader.md`](reader.md) | The base reader — open + version-gate an emit, expose the typed sidecar, the row-tuple + columnar query surfaces, the faithful-read SQL builders (the sole faithful namer of base tables) |
| [`conformance.md`](conformance.md) | C1–C12 conformance — `validate` / `fabulexa-forge validate`, the independent codec, comparison sources |
| [`derivations.md`](derivations.md) | The derivations layer — interpretive shared folds between the reader and the modes; the versioned-intervals, reference-resolution, row-state-events, membership-events, and state-at residents, the layer contract (purity / anti-weld / traceability / temporal honesty), the single-branch guard |
| [`dimensional.md`](dimensional.md) | The dimensional exporter — `mode: dimensional` star-schema reshape (config grammar, grains, FK pathfind, `lookup` enrichment, SCD-2, writers, `export` / `init`) |
| [`source.md`](source.md) | The source exporter — `mode: source` operational-dump reshape: the genre trichotomy classifying every table (change-log / reference / transaction / junction) from `record_roles` × `history_tracked`, the untracked-only sub-type split, operational presentation defaults, the mandatory wallclock anchor, `exclude`/`rename` escape hatches, corrupt→source composition, the cross-mode incremental driver's per-genre window membership, and `change_delivery: snapshot` (periodic full-table snapshots composing the derivations layer's state-at fold) |
| [`streaming.md`](streaming.md) | The streaming exporter — the `fabulexa-forge stream` delivery driver: two content axes — `state-changes` (`history` replayed as an ordered `c`/`u`/`d` CDC stream) and `membership-events` (the `membership__<K>__<p>` interval tables unpivoted into an ordered `join`/`leave` stream) — over the content × format × sink model (`StreamConfig`, cross-source merge + global `seq`, Python-side `ts` rendering, `jsonl` + `debezium` formats to stdout, per-topic files, or a Kafka broker — the `kafka` sink with topic pre-creation, `record_id` keying, and CLI/config/env bootstrap resolution). Composes the derivations row-state-events and membership-events folds and the streaming-routing surface |
| [`streaming-routing.md`](streaming-routing.md) | The streaming routing surface — the two-layer partition of the `seq`-stamped event stream into topics: a per-content Layer A route-attribute derivation (`route_table` from the record spine for `state-changes`, from the `(owner_kind, property)` identity for `membership-events`), the content-agnostic Layer B policy (`RoutingConfig` — `topic_template`, `groups`, `table_identity`), `types` sub-type selection, declared-but-empty topics, the Debezium `table_identity` masquerade, and the routing validation rules |
| [`streaming-pacing.md`](streaming-pacing.md) | The streaming pacing surface — realtime delivery of the `seq`-stamped event stream so a finished run replays as a live feed: clock resolution (`ClockConfig` × `--speed` / `--idle-cap` / `--fast`, CLI-wins-per-knob), the drift-free release schedule keyed on `event_sim_time`, paced per-line-flush sink delivery, and the timing-only / determinism invariants. A post-merge timing overlay composed by the streaming driver |
| [`streaming-mixer.md`](streaming-mixer.md) | The streaming mixer scheduler — the headless, operator-driven release core that replays a finished emit as a live mixable feed: a mutable `ControlState` (master `Transport` + per-topic `TopicDials`), an evolving `FrontierState`, the pure per-tick `advance` (master frontier × per-topic lag / rate / mute edges), and the async `schedule_releases` driver over per-topic FIFO buffers seeded by `seed_mixer_run`. A deliberately non-deterministic *sibling* of `pace_events` that perturbs delivery timing only |
| [`mixer-control-plane.md`](mixer-control-plane.md) | The mixer control plane — the `fabulexa-forge mixer` driver that turns the headless scheduler into a live, operator-driven performance: a single-event-loop asyncio app that opens an emit, seeds the scheduler, serves the FabulMixer control API (play / pause / re-speed the master transport; lag, rate-limit, or mute each topic mid-run), and delivers to Kafka. The mutable `MixerRunState`, the launch lifecycle (sync setup → async serve), the lock-free single-loop consistency rule, the wire models (the backend mirror of the shared control-API contract), the async `KafkaSink`, and the producer-side tier-1 meters derivation. Kafka-only; behind a `[mixer]` extra composing `[kafka]` |
| [`mixer-consumer.md`](mixer-consumer.md) | The mixer consumer-side instrument — the optional, `--consumer`-gated downstream half of the FabulMixer performance: a pure timing simulator on the same `fabulexa-forge mixer` event loop that subscribes a real `KafkaSource` to the producer's topic set, pulls each topic at an operator-set `ingest_rate`, and reads only record timing metadata (`.topic()` / `.timestamp()` / `.offset()`) to derive a global watermark (`min` across data-bearing topics), tumbling-window firings, and enrichment-join null health. A second control + derived-state pair (`ConsumerControlState` / `ConsumerState`) with the pure `ingest` tick and async `run_consumer` driver, mirroring the producer's `advance` / `schedule_releases` split; its meters and gated `/api/consumer/*` routes. Reads the broker only — never the bundle |
| [`anchor.md`](anchor.md) | The effective-anchor resolution surface — origin/zone precedence over sidecar `runtime` + `rebase` config + CLI flags, DST/ambiguity rules, the one `EffectiveAnchor` (and `render_anchor_timestamp_expr`) every wallclock mode renders through |
| [`writers.md`](writers.md) | The output adapters — the generic relation → file/table serializers (CSV / DuckDB serialization is documented with their consumer in `dimensional.md`) |
| [`incremental.md`](incremental.md) | The incremental export driver — `--next` / `--from` / `--to` window-at-a-time export over the dimensional mode: calendar/sim-time regimes, per-table-class window membership, the SCD-2 `valid_to` view, the cursor + fingerprint, drained detection |
| [`config-docstrings.md`](config-docstrings.md) | Developer convention for documenting the export-config Pydantic models — the three channels (class / attribute / validator docstring), allowed/disallowed field prose, and the structural enforcement test. A cross-cutting authoring convention, not a subsystem |
| [`corrupters.md`](corrupters.md) | The corrupter family — the `CorruptConfig` grammar shared by twelve operations (family A's `null_cells` and `mutate_cells` — the latter eleven type-preserving wrong-value transforms — family B's row-set operations `duplicate_rows` (exact, near-duplicate `jitter`, or conflicting-duplicate `mutation`), `delete_rows` (row removal and its referential/pin/history wake), and `insert_rows` (phantom-row injection under a fresh, plausible id); `schema_drift`; family D's `dangle_reference` and `mispoint_reference` (a sentinel-pointing and a wrong-but-real donor-pointing referential defect, the latter optionally constrained to a point-in-time dangling reference); family C's `freeze_series` / `drop_events` / `shift_sim_time` over `history`'s temporal dimension; and family E's `distort_intervals` over the membership tables' SCD-2 interval timeline (overlap an adjacent interval pair, shrink an interval into a coverage gap, or invert a closed interval's timing columns)): a five-way table selector (`table` / `tables` / `glob` / `category` / `record_kind`) with exact-or-pattern column entries, the pooled `Amount` distribution, and the optional biased `placement` axis (`entity_scoped` / `clustered_temporal` / `correlated` MNAR weighted draw); the engine (`corrupt_emit`) that threads the operations over a shared working set, the base-emit writer that regenerates a structurally-conformant `run.duckdb` + `base.json`, family C's series/event units and its C6-mirroring impact oracle, family E's member-timeline/interval units, and the defect manifest (`defects.json`) — the deterministic, label-grade ground-truth artifact naming every injected defect and the conformance guarantee it breaks |

## What this package is

A downstream consumer of the composite **base layer**. Input is one emit
(`run.duckdb` + `base.json`); output is a differently-shaped dataset (exporter) or a
realistically-broken base layer (corrupter). The contract is vendored under
`../../contract/`, the only coupling this package has.

It is the inverse of the producer, which *writes* the base layer. Where the
producer renders a persisted run into base-layer tables, this
package *reads* those tables and reshapes them.

## Planned package layout

Built stage by stage (Principle #8 — modules appear when their stage lands, not
before). The reader (Stage 1), the dimensional exporter (Stage 2), the derivations
layer, and the streaming and source exporters (Stage 3) have shipped; the remaining
Stage-3 mode (`base`) and later stages are planned.

| Module | Role | Status |
|---|---|---|
| `reader/` | The foundation. Open an emit, parse + version-gate `base.json`, expose typed tables/branches/runtime/pins/enum_domains/record_roles, and run conformance C1–C12. The one path every exporter/corrupter reads through. See [`reader.md`](reader.md) + [`conformance.md`](conformance.md). | Implemented (Stage 1) |
| `derivations/` | Interpretive shared folds between the reader and the modes. Pure SQL, anti-weld signatures (sidecar + plain values), one canonical raw relation each. Five residents — `history` → versioned-intervals, reference-resolution (reference-path · membership-edge), `history` → row-state-events (per-record `c`/`u`/`d`), `membership__<K>__<p>` → membership-events (`join`/`leave`), and `history` + `records__<kind>` → state-at (point-in-time row reconstruction); owns the single-branch guard. See [`derivations.md`](derivations.md). | Implemented (Stage 3) — five residents |
| `exporters/` | Base → different shape. One sub-package per mode, plus two mode-neutral modules (`query_spec.py` — the shared compiled-table shape and full-export write dispatch; `reserved_names.py` — the shared bookkeeping-name check). `dimensional` (Stage 2), `streaming` (Stage 3), and `source` (Stage 3) ship; `base` is planned. The `streaming` mode includes the two-layer routing surface (`streaming/routing.py`). See [`dimensional.md`](dimensional.md), [`source.md`](source.md), [`streaming.md`](streaming.md), [`streaming-routing.md`](streaming-routing.md). | `dimensional`, `streaming` + `source` implemented; `base` planned (Stage 3) |
| `corrupters/` | Base → broken base. The engine (`corrupt_emit`), the seeded selection surface (five-way table-selector resolution, pattern column matching, uniform + placement-weighted samplers), the `Corrupter` operation registry (`null_cells` / `mutate_cells` / `duplicate_rows` / `delete_rows` / `insert_rows` / `schema_drift` / `dangle_reference` / `mispoint_reference` / `freeze_series` / `drop_events` / `shift_sim_time`), the base-emit writer, and the defect manifest (`build_defect_manifest`, `defects.json`) — breaking C6/C7/C9–C12 while preserving C1–C5/C8 by construction. See [`corrupters.md`](corrupters.md). | Implemented (Stage 4) |
| `config/` | Pydantic config envelopes. `ExportConfig` — the two-tier dimensional grammar plus the cross-mode `rebase` and `incremental` blocks (siblings of `mode`); `mode` is `Literal["dimensional", "source"]`, the discriminator-plus-per-mode-section shape (`mode_section_matches`) validating the named mode's section is present and the other mode's section is absent — additive by construction, so a further shape-mode extends the `Literal` and adds its section. `SourceConfig` (`change_delivery`, `exclude`, `rename`) is `mode: source`'s section — see [`source.md`](source.md). `StreamConfig` — a separate top-level envelope (not a mode), because streaming is a delivery driver, not a shape-mode; it declares `content` × per-kind selection, an optional `RoutingConfig` topic policy (`topic_template` / `groups` / `table_identity`), and reuses `rebase`. `CorruptConfig` — a third top-level envelope, sibling of `ExportConfig` / `StreamConfig`; a master `seed` plus an ordered list of `kind`-discriminated operations sharing one selector (`Target` — five-way table selector, pattern column entries) / distribution (`Amount`, `Distribution`) / placement (`Placement`) grammar. See [`streaming.md`](streaming.md), [`streaming-routing.md`](streaming-routing.md), [`corrupters.md`](corrupters.md). | Implemented (Stage 2 + Stage 3 `StreamConfig` + Stage 4 `CorruptConfig`) |
| `anchor.py` | The effective-anchor resolver. Combines sidecar `runtime`, `rebase` config, and CLI overrides into one `EffectiveAnchor` (or `None`); the single authority for origin/zone precedence and DST/ambiguity validation. Every wallclock mode renders through it. See [`anchor.md`](anchor.md). | Implemented (Stage 2) |
| `writers/` | Output adapters. CSV + DuckDB (Stage 2) ship; Parquet is planned. DuckDB has a windowed path (`write_duckdb_window`) for incremental export. See [`writers.md`](writers.md). | CSV + DuckDB implemented |
| `incremental/` | The cross-mode incremental driver: window math, cursor, fingerprint, drip/range orchestration. Wraps a mode's pure range export (dimensional, source). See [`incremental.md`](incremental.md). | Implemented |
| `cli.py` | `fabulexa-forge validate \| export \| stream \| mixer \| corrupt \| init`. `export` dispatches on `config.mode` to the dimensional or source engine, carries the `--base-date` / `--timezone` rebase overrides, the `--next` / `--from` / `--to` incremental flags (both modes), and `--fmt csv\|duckdb`. `stream` replays the base layer as a CDC event stream — `--fmt jsonl\|debezium`, `--sink stdout\|file\|kafka` (`--bootstrap-servers` for the kafka sink), plus the shared `--base-date` / `--timezone` anchor overrides. `mixer` replays the base layer as a live, operator-mixable Kafka feed, serving the FabulMixer control API (`--fmt jsonl\|debezium`, `--bootstrap-servers`, `--host` / `--port`, transport / tick flags). `corrupt <emit_dir> --config <corrupt.yaml> --out <out_dir>` applies a `CorruptConfig`, always writing `run.duckdb` + `base.json` + `defects.json`. | `validate` (Stage 1), `export` + `init` (Stage 2), `stream` + incremental flags + `mixer` (Stage 3), `corrupt` (Stage 4) implemented |

## Staged roadmap

The sanitised subset is still rich (long-form `history` SCD-2 change-log, membership,
`record_roles`, `enum_domains`). We climb it in stages, each shippable and testable on
its own.

1. **Reader + conformance, trunk-only.** Open an emit, validate `base.json` against the
   vendored schema, version-gate, expose tables/columns/runtime/pins/enum_domains as
   typed accessors, reimplement C1–C12 independently (the producer's
   reference conformance checker is a *reference to read*, never a dependency). The
   sanitised subset mandates exactly one `branches` entry (C8 asserts it).
   `fabulexa-forge validate`.
2. **Dimensional exporter, trunk-only.** `records__<kind>` → `dim_<kind>`, `history` and
   membership bindings → `fact_` tables. Typed `prop__` columns read directly (no JSON
   expansion); SCD-2 derived from long-form `history` via `LEAD`. Config envelope,
   writers (CSV/DuckDB), `fabulexa-forge export` + `init`.
3. **Source + base + streaming exporters, trunk-only.** OLTP change-log
   (source), flat/point-in-time projection (base), `history`-change-event replay
   (streaming). The streaming exporter has shipped as the `fabulexa-forge stream` verb —
   `history` replayed as an ordered `c`/`u`/`d` CDC event stream, to stdout, per-topic
   files, or a Kafka broker, composing the row-state-events derivation (see
   [`streaming.md`](streaming.md)). The source exporter has shipped as `mode: source`
   — every emitted table classified into a change-log, reference, transaction, or
   junction genre from `record_roles` × `history_tracked`, composing the
   row-state-events derivation for its change-log render and a new state-at
   derivation for its `change_delivery: snapshot` periodic-full-table delivery (see
   [`source.md`](source.md)); `base` remains planned. Timestamp rebasing is a
   cross-mode surface (the effective anchor, see [`anchor.md`](anchor.md)) that
   shipped with the dimensional exporter; each new wallclock mode resolves through it
   and adds only its own representation of the resolved instant — source is the first
   mode that requires resolution rather than falling back to raw integers. Incremental
   export is likewise a cross-mode driver — it wraps both the dimensional mode and the
   source mode's own windowed compile (see [`incremental.md`](incremental.md)) — and
   each new mode wires into the same window derivation, cursor, fingerprint, and
   writers.
4. **Corrupter family.** Reuse the reader; write base-shaped output that breaks
   C6/C7/C9–C12 while preserving C1–C5/C8 by construction. Shipped as the `fabulexa-forge
   corrupt` verb — a `CorruptConfig` envelope over twelve operations (family A's
   `null_cells` and `mutate_cells` — eleven type-preserving wrong-value transforms,
   including the family's first reach into `history.value` and into C12 —
   family B's row-set operations `duplicate_rows` (exact, near-duplicate `jitter`, or
   conflicting-duplicate `mutation`), `delete_rows` (row removal, declaring the
   referential/pin/history wake the removal trips), and `insert_rows` (phantom-row
   injection cloned from a donor under a fresh, plausible id); `schema_drift`; family D's
   `dangle_reference` and `mispoint_reference`
   (referential breakage and mis-pointing, the latter's `constraint` mode declaring a
   point-in-time dangling reference); family C's `freeze_series` /
   `drop_events` / `shift_sim_time` over `history`'s temporal dimension; and family E's
   `distort_intervals` (overlap an adjacent membership interval pair, shrink an interval
   into a coverage gap, or invert a closed interval's `joined_sim_time`/`left_sim_time`))
   sharing one
   selector/distribution/placement grammar (class-level multi-table selection and biased,
   MNAR-capable placement), threaded over a shared working set by the engine
   (`corrupt_emit`), which also
   assembles the operations' declared defects into `defects.json` — a deterministic,
   label-grade ground-truth manifest beside the corrupted emit (see
   [`corrupters.md`](corrupters.md)).
5. **Queue-state + point-in-time export.** `membership__*` → queue-state facts (wait
   time, FIFO/priority as SQL); `history` replay to any `sim_time` → ML feature-store
   rows. Both build on the sanitised subset (one branch, no provenance) — neither needs
   branch-awareness.

Each stage is an `arch-design` doc under `pending/` then a sprint.

**Parked — needs a future contract extension.** Branch-aware export (branch selection,
paired-counterfactual, per-branch slices) and provenance lineage columns are out of
reach: the sanitised-subset contract mandates exactly one branch and carries no
provenance. They return to the roadmap only if the contract restores multi-branch /
provenance.

## Inputs and fixtures

The reader and `validate` are tested without the producer against fixtures
**synthesized programmatically** — DuckDB + stdlib only, no external imports —
by [`tests/reader/_fixtures_build.py`](../../tests/reader/_fixtures_build.py) into a
temporary directory and read only through the reader. No emit is committed to the
repo, and the producer is never invoked.

- A **spanning positive** v4 emit exercises every table category in the sanitised
  subset — `history`, `records__*`, and `membership__*` — plus `pinned_ids`,
  `runtime`, `enum_domains`, a `references` column, and a `record_roles` registry
  covering every emitted kind (including an `actor` object whose sub-types cover every
  `records__actor.prop__actor_type` value). It carries no `firings` table, no
  provenance column group, and exactly one branch, so each C1–C12 check has live
  input.
- Several **deliberately-broken** variants drive the negative suite — a retyped
  `history` column (C4), a dropped `prop__` column the sidecar still declares
  (C2/C5), a half-NULL membership reference pair (C7), a phantom column (C2/C5), a
  `record_roles` registry that omits an emitted kind or an in-data `actor` sub-type
  (C12), and a wrong `base_format_version` (the version gate) — plus defects that
  *pass* C1–C12 by design (duplicate tick, dangling records-prop reference), which
  exercise the boundary that C1–C12 is narrower than the producer's QA suite (see
  [`conformance.md`](conformance.md) § Boundaries).

A later stage that needs multiple branches extends the spanning builder; the
single-branch fixture is enough through Stage 4.

Fixtures are named by what they exercise, never by format version: the spanning
fixture tracks `SUPPORTED_BASE_FORMAT_VERSION` (so a version bump leaves the name
correct), and each negative variant is named by the defect it injects. A version
appears in a name only when version-gating is the assertion under test, and even
then by intent (`wrong_version`), not by the literal number.

## Status

| Area | Status |
|---|---|
| Project skeleton + standalone-venv boundary | Scaffolded |
| Vendored contract (`base_format_version 4`) | Vendored — re-synced on version bump (`contract/README.md`) |
| Reader + conformance | Implemented (Stage 1) — [`reader.md`](reader.md), [`conformance.md`](conformance.md) |
| `fabulexa-forge validate` CLI verb | Implemented (Stage 1) |
| Dimensional exporter + config + CSV/DuckDB writers | Implemented (Stage 2) — [`dimensional.md`](dimensional.md) |
| `fabulexa-forge export` + `init` CLI verbs | Implemented (Stage 2) |
| Effective anchor + timestamp rebasing (`rebase` config, `--base-date` / `--timezone`) | Implemented (Stage 2) — [`anchor.md`](anchor.md) |
| Incremental export (`incremental` config, `--next` / `--from` / `--to`, cursor + SCD-2 view) | Implemented (cross-mode driver over dimensional + source) — [`incremental.md`](incremental.md) |
| Derivations layer + versioned-intervals / reference-resolution / row-state-events / membership-events / state-at residents + single-branch guard | Implemented (Stage 3) — [`derivations.md`](derivations.md) |
| Streaming exporter + `StreamConfig` + `fabulexa-forge stream` (`state-changes`: `history` → ordered `c`/`u`/`d` CDC stream; `membership-events`: `membership__<K>__<p>` → ordered `join`/`leave` stream; `jsonl` + `debezium` formats to stdout / per-topic files / a Kafka broker) | Implemented (Stage 3) — [`streaming.md`](streaming.md) |
| Streaming routing — two-layer topic partition (`RoutingConfig`: `topic_template` / `groups` / `table_identity`), `types` sub-type selection, declared-but-empty topics, routing validation rules | Implemented (Stage 3) — [`streaming-routing.md`](streaming-routing.md) |
| Streaming pacing — realtime delivery (`ClockConfig`: `mode` / `speed` / `idle_cap_seconds`, `--speed` / `--idle-cap` / `--fast`), drift-free release schedule, paced per-line-flush sinks | Implemented (Stage 3) — [`streaming-pacing.md`](streaming-pacing.md) |
| Streaming Kafka sink — `--sink kafka` (`KafkaConfig`, `--bootstrap-servers` / `FABEXPORT_KAFKA_BOOTSTRAP`); one message per event, topic pre-creation (1 partition / RF 1), `record_id` keying, flush-before-return; `confluent-kafka` optional `[kafka]` extra | Implemented (Stage 3) — [`streaming.md`](streaming.md) § The Kafka sink |
| Streaming mixer scheduler — `ControlState` (`Transport` + per-topic `TopicDials`), `FrontierState`, the pure per-tick `advance` (master frontier × per-topic lag / rate / mute), and the async `schedule_releases` driver over `seed_mixer_run` per-topic buffers. The headless correctness core of the FabulMixer live-perform POC | Implemented — [`streaming-mixer.md`](streaming-mixer.md) |
| Streaming mixer control plane — the `fabulexa-forge mixer` driver: sync setup → async serve, the lock-free single-loop `MixerRunState`, the FastAPI control API (`/api/state` · `/api/meters` · `PUT /api/transport` · `PUT /api/topics/{topic}`) mirroring the shared control-API contract, the async `KafkaSink`, and the producer-side tier-1 meters derivation. Kafka-only, behind a `[mixer]` extra composing `[kafka]` | Implemented — [`mixer-control-plane.md`](mixer-control-plane.md) |
| Streaming mixer consumer instrument — the optional `--consumer` downstream simulator: `KafkaSource` read-back, the pure `ingest` tick + async `run_consumer` over a second `ConsumerControlState` / `ConsumerState` pair, per-topic + global watermark (`min` across data-bearing topics), tumbling windows, enrichment-join null health, `derive_consumer_meters`, the gated `/api/consumer/*` + `/api/capabilities` routes, and the `--window` / `--join` / `--consumer-group` / `--consumer-offset` flags. Reads the broker only | Implemented — [`mixer-consumer.md`](mixer-consumer.md) |
| Source exporter — `mode: source` genre trichotomy (change-log / reference / transaction / junction), untracked-only sub-type split, operational presentation defaults, mandatory wallclock anchor, `exclude`/`rename`, corrupt→source composition, cross-mode incremental composition, `change_delivery: snapshot` (state-at derivation) | Implemented (Stage 3) — [`source.md`](source.md) |
| Base exporter · Parquet writer | Planned (Stage 3) |
| Corrupters — `CorruptConfig` envelope, the selector/distribution/placement grammar (five-way multi-table selection, pattern column entries, `entity_scoped` / `clustered_temporal` / `correlated` biased placement), twelve operations (`null_cells` / `mutate_cells` / `duplicate_rows` / `delete_rows` / `insert_rows` / `schema_drift` / `dangle_reference` / `mispoint_reference` / `freeze_series` / `drop_events` / `shift_sim_time` / `distort_intervals`), the engine (`corrupt_emit`), the base-emit writer, family C's series/event units and C6-mirroring impact oracle, family E's member-timeline/interval units, and the defect manifest (`defects.json`) | Implemented (Stage 4) — [`corrupters.md`](corrupters.md) |
| Queue-state + point-in-time export | Planned (Stage 5) |
| Branch-aware export + provenance lineage | Parked — needs a multi-branch / provenance contract |
| Spanning + negative fixtures | Implemented (programmatic, `tests/reader/_fixtures_build.py`) |
