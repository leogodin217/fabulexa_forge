# Architecture

Design index for the Fabulexa composite export package. Read `../../CLAUDE.md` first
for principles, the boundary, and vocabulary. For the feature inventory + status (what
each mode does, what's shipped), see [`../CAPABILITIES.md`](../CAPABILITIES.md); this
doc owns the design and build order.

## Per-subsystem design docs

| Doc | Subsystem |
|---|---|
| [`bundle.md`](bundle.md) | The input, understood — consumer-side orientation to the bundle: where emit data comes from, table-genre semantics, inherited guarantees, mechanism vs presentation columns, single-branch facts. Informational companion to the vendored contract |
| [`reader.md`](reader.md) | The base reader — open + version-gate an emit, expose the typed sidecar, the records-column taxonomy (the one classifier every records-column consumer reads through), the structural-temporal surface (the one answer to which structural columns carry a sim-time instant and which may change after creation), the strict `presentation_keys` registry view + union-safety algebra, the row-tuple + columnar query surfaces, the faithful-read SQL builders (the sole faithful namer of base tables) |
| [`conformance.md`](conformance.md) | C1–C14 conformance — `validate` / `fabulexa-forge validate`, the independent codec, comparison sources |
| [`derivations.md`](derivations.md) | The derivations layer — interpretive shared folds between the reader and the modes; the versioned-intervals, reference-resolution, row-state-events (two-scope: a change-scope set governing `u` event membership × an independently-scoped after-image projection), membership-events, state-at (horizoned + end-of-tape), membership-state-at, and record-index (`record_id` → `record_index`, horizoned + end-of-tape) residents plus the truncated-tape surface, the layer contract (purity / anti-weld / traceability / temporal honesty), the fold-vs-join-relation reading of the determinism rule, the single-branch guard |
| [`dimensional.md`](dimensional.md) | The dimensional exporter — `mode: dimensional` star-schema reshape (config grammar, grains, FK pathfind, `lookup` enrichment, SCD-2, writers, `export` / `init`) |
| [`source.md`](source.md) | The source exporter — `mode: source` as the author-declared app database: the declared-table grammar (populations → named tables), the `state` / `junction` renders and the single polymorphic event log, per-table `columns` / `rename`, the two-axis row selection (population `sub_types` × the constant-gated `where`, evaluated on membership units through the owner parent lookup) and the selection-aware event-source disjointness gate, the author-declared domain vocabulary (`kind_labels` / per-source `item_type` / `rename`) resolving kind-name-as-value and `changes`-key surfaces, operational presentation defaults, the mandatory wallclock anchor, corrupt→source composition, the cross-mode incremental driver's per-render window membership (windowed state snapshots via the state-at fold, appended event log, junction extract-on-change), and the `init --mode source` proposal engine |
| [`base.md`](base.md) | The base exporter — `mode: base` flat projection: one row per record per records kind, no declared-table grammar and no event log, every output table the state-at reconstruction of one kind materialized as a table. The three horizons (tape's end · inclusive `slice_at: T` · per-window under incremental) as three entry points into the shipped state-at resident, the record-index key columns (a `<kind>_key` self key and a re-derived `<p>_key` per reference edge, emitted beside the id-space encoding), the `slice_only` omit-with-notice posture, operational presentation defaults with `exclude`/`rename`, cast-back to sidecar types, the optional anchor (raw-ns fallback), and the point-in-time subsumption position |
| [`streaming.md`](streaming.md) | The streaming exporter — the `fabulexa-forge stream` delivery driver: author-declared streams (the `streams` list of named `KindStream` / `MembershipStream` declarations — the stream name is the topic; sub-type-scoped populations, per-stream `properties` / `fields` projections, payload-independent event sets) over two content axes — `state-changes` (`history` replayed as an ordered `c`/`u`/`d` CDC stream) and `membership-events` (the `membership__<K>__<p>` interval tables unpivoted into an ordered `join`/`leave` stream) — and the content × format × sink model (`StreamConfig`, cross-stream merge + global `seq`, Python-side `ts` rendering, `jsonl` + `debezium` formats — per-stream value schemas, the `route_table` leaf + `table_identity` masquerade — to stdout, per-topic files, or a Kafka broker with topic pre-creation and CLI/config/env bootstrap resolution). The fourth key-election consumer (the elected surface is the message key) and home of the `init --mode streaming` proposal engine. Composes the derivations row-state-events (two-scope) and membership-events folds |
| [`streaming-pacing.md`](streaming-pacing.md) | The streaming pacing surface — realtime delivery of the `seq`-stamped event stream so a finished run replays as a live feed: clock resolution (`ClockConfig` × `--speed` / `--idle-cap` / `--fast`, CLI-wins-per-knob), the drift-free release schedule keyed on `event_sim_time`, paced per-line-flush sink delivery, and the timing-only / determinism invariants. A post-merge timing overlay composed by the streaming driver |
| [`streaming-mixer.md`](streaming-mixer.md) | The streaming mixer scheduler — the headless, operator-driven release core that replays a finished emit as a live mixable feed: a mutable `ControlState` (master `Transport` + per-topic `TopicDials`), an evolving `FrontierState`, the pure per-tick `advance` (master frontier × per-topic lag / rate / mute edges), and the async `schedule_releases` driver over per-topic FIFO buffers seeded by `seed_mixer_run`. A deliberately non-deterministic *sibling* of `pace_events` that perturbs delivery timing only |
| [`mixer-control-plane.md`](mixer-control-plane.md) | The mixer control plane — the `fabulexa-forge mixer` driver that turns the headless scheduler into a live, operator-driven performance: a single-event-loop asyncio app that opens an emit, seeds the scheduler, serves the FabulMixer control API (play / pause / re-speed the master transport; lag, rate-limit, or mute each topic mid-run), and delivers to Kafka. The mutable `MixerRunState`, the launch lifecycle (sync setup → async serve), the lock-free single-loop consistency rule, the wire models (the backend mirror of the shared control-API contract), the async `KafkaSink`, and the producer-side tier-1 meters derivation. Kafka-only; behind a `[mixer]` extra composing `[kafka]` |
| [`mixer-consumer.md`](mixer-consumer.md) | The mixer consumer-side instrument — the optional, `--consumer`-gated downstream half of the FabulMixer performance: a pure timing simulator on the same `fabulexa-forge mixer` event loop that subscribes a real `KafkaSource` to the producer's topic set, pulls each topic at an operator-set `ingest_rate`, and reads only record timing metadata (`.topic()` / `.timestamp()` / `.offset()`) to derive a global watermark (`min` across data-bearing topics), tumbling-window firings, and enrichment-join null health. A second control + derived-state pair (`ConsumerControlState` / `ConsumerState`) with the pure `ingest` tick and async `run_consumer` driver, mirroring the producer's `advance` / `schedule_releases` split; its meters and gated `/api/consumer/*` routes. Reads the broker only — never the bundle |
| [`slice-only.md`](slice-only.md) | The export-wide `slice_only` policy — no exporter output value, row membership, linkage, or ordering derives from a `slice_only` column's value: the policy population and read taxonomy, the mechanical sub-typed-discriminator carve-out (`prop__<K>_type` × non-empty `subtype_values`), per-mode enforcement (dimensional refusal + `lookup` constant-regate, source and base omission, streaming refusal), and the column-projection-only invariance |
| [`row-predicates.md`](row-predicates.md) | The config row-predicate grammar shared by the dimensional mode's five predicate surfaces (`source.filter` / `source.where` / `source.value` / `fk.where` / `derived.elapsed.other_where`) and the source mode's two (`tables[].where` / `events.sources[].where`) — a scalar-or-list value compiling to `=` or `IN` under one rendering authority in the shared SQL utilities, sidecar-resolved literal typing with no VARCHAR fallback, the well-formedness rule carried by the `PredicateValue` type, and the equality-and-set-membership operator boundary |
| [`declared-keys.md`](declared-keys.md) | The opt-in `declare_keys` capability on the base and source modes — the sidecar's `presentation_keys` claims (plus the contract's record-identity guarantees) resolved at plan time into per-table `PRIMARY KEY` / `UNIQUE` declarations, materialized as real DuckDB constraints (full and windowed), reported undeliverable under CSV via `keys-not-declarable-csv`; claims read through the reader's strict accessor, never validated against data |
| [`key-election.md`](key-election.md) | The cross-mode key-election surface — the `keys` config block electing, per population, which identity surface (`record_id` / `record_index` / `presentation_id`) presents as a table's exported identity, with every referencing column rendered in its target's elected surface: the election grammar, the static resolution + combination gates over the registry's normative union-safety algebra, the identity join relations and the render-time elected-key uniqueness guard, per-mode rendering (source's declared tables and event-log `item_id`, base's elective id-space value surface, dimensional FK inheritance + dim-key agreement, streaming's elected message key + after-image render sites), mixed-election edge columns, and `init`'s self-gated `keys` proposal |
| [`notices.md`](notices.md) | The notice channel — the package's one informational output surface: the frozen `Notice` record, the required caller-supplied `NoticeSink`, determinism/severity/timing rules, the notice-code registry, and the CLI's stderr rendering |
| [`anchor.md`](anchor.md) | The effective-anchor resolution surface — origin/zone precedence over sidecar `runtime` + `rebase` config + CLI flags, DST/ambiguity rules, the one `EffectiveAnchor` (and `render_anchor_temporal_expr`) every wallclock mode renders through |
| [`temporal-elections.md`](temporal-elections.md) | The cross-mode temporal-rendering election surface — author-electable `date` / `time` / `timestamptz` instant renderings and an `interval` elapsed-duration rendering over the one shared vocabulary, a declared VARCHAR→`DATE` parse, the anchor-required business rule, DST/precision/determinism posture, and the per-mode attach points on the dimensional, source, and base exporters |
| [`writers.md`](writers.md) | The output adapters — the generic relation → file/table serializers (non-temporal CSV / DuckDB serialization is documented with their consumer in `dimensional.md`; the pinned temporal text forms for the four elected types are owned here) |
| [`incremental.md`](incremental.md) | The incremental export driver — `--next` / `--from` / `--to` window-at-a-time export over the dimensional, source, and base modes: calendar/sim-time regimes, per-table-class window membership, the SCD-2 `valid_to` view, the cursor + fingerprint, drained detection |
| [`playback.md`](playback.md) | The playback seam — the caller-driven, pull-only, deterministic library surface driving an emit as a tape: tier 1 (primitive `events` / `snapshot` / `seek` over atom populations, below the modes) and tier 2 (shaped `window` / `state` over a declared target shape, above the modes), one inclusive-T event-time line, the consistency algebra, the canonical order + entry-point-invariant `seq`, the truncated-tape `state` compile, permissive totality |
| [`config-docstrings.md`](config-docstrings.md) | Developer convention for documenting the export-config Pydantic models — the three channels (class / attribute / validator docstring), allowed/disallowed field prose, and the structural enforcement test. A cross-cutting authoring convention, not a subsystem |
| [`corrupters.md`](corrupters.md) | The corrupter family — the `CorruptConfig` grammar shared by twelve operations (family A's `null_cells` and `mutate_cells` — the latter eleven type-preserving wrong-value transforms — family B's row-set operations `duplicate_rows` (exact, near-duplicate `jitter`, or conflicting-duplicate `mutation`), `delete_rows` (row removal and its referential/pin/history wake), and `insert_rows` (phantom-row injection under a fresh, plausible id); `schema_drift`; family D's `dangle_reference` and `mispoint_reference` (a sentinel-pointing and a wrong-but-real donor-pointing referential defect, the latter optionally constrained to a point-in-time dangling reference); family C's `freeze_series` / `drop_events` / `shift_sim_time` over `history`'s temporal dimension; and family E's `distort_intervals` over the membership tables' SCD-2 interval timeline (overlap an adjacent interval pair, shrink an interval into a coverage gap, or invert a closed interval's timing columns)): a five-way table selector (`table` / `tables` / `glob` / `category` / `record_kind`) with exact-or-pattern column entries, the pooled `Amount` distribution, and the optional biased `placement` axis (`entity_scoped` / `clustered_temporal` / `correlated` MNAR weighted draw); the engine (`corrupt_emit`) that threads the operations over a shared working set, the base-emit writer that regenerates a structurally-conformant `run.duckdb` + `base.json`, family C's series/event units and its C6-mirroring impact oracle, family E's member-timeline/interval units, and the defect manifest (`defects.json`) — the deterministic, label-grade ground-truth artifact naming every injected defect and the conformance guarantee it breaks |
| [`compare.md`](compare.md) | The compare surface — `compare_datasets` + the `fabulexa-forge compare` CLI verb: the dataset-equivalence verdict (exact relational equality of an actual dataset — DuckDB or CSV — against an authoritative expected forge render, DuckDB) under the forge-owned canonical form (ten canonical type families, Python-side canonical value encoding byte-identical to the C6 codec where they overlap, the UTC-pinned compare session, the interval day-fold), multiset row comparison over the compared-column set, the deterministic bounded `ComparisonResult` report, text + byte-stable JSON renderers, and the `0/1/2` exit-code contract. A pure two-input surface: no emit, no bundle, no tolerances, no scoring |

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
layer, the streaming, source, and base exporters (Stage 3), the corrupter family
(Stage 4), and the playback seam have shipped; Stage 5's remaining prong
(queue-state export) and later stages are planned.

| Module | Role | Status |
|---|---|---|
| `reader/` | The foundation. Open an emit, parse + version-gate `base.json`, expose typed tables/branches/runtime/pins/enum_domains/record_roles, the per-column temporal pair (`history_tracked` + the `temporal_class` accessor), and the records-column taxonomy, and run conformance C1–C14. The one path every exporter/corrupter reads through. See [`reader.md`](reader.md) + [`conformance.md`](conformance.md). | Implemented (Stage 1) |
| `derivations/` | Interpretive shared folds between the reader and the modes. Pure SQL, anti-weld signatures (sidecar + plain values), one canonical raw relation each. Six residents — `history` → versioned-intervals, reference-resolution (reference-path · membership-edge), `history` → row-state-events (per-record `c`/`u`/`d`), `membership__<K>__<p>` → membership-events (`join`/`leave`), `history` + `records__<kind>` → state-at (point-in-time row reconstruction, horizoned + end-of-tape entry points), and `membership__<K>__<p>` → membership-state-at (interval containment at a horizon) — plus the truncated-tape surface (base-table presenters + sidecar view rendering the emit sliced at T); owns the single-branch guard. See [`derivations.md`](derivations.md). | Implemented (Stage 3, extended for playback) — six residents + truncated-tape surface |
| `exporters/` | Base → different shape. One sub-package per mode, plus two mode-neutral modules (`query_spec.py` — the shared compiled-table shape and full-export write dispatch; `reserved_names.py` — the shared bookkeeping-name check). `dimensional` (Stage 2), `streaming` (Stage 3), `source` (Stage 3), and `base` (Stage 3) all ship. The `streaming` mode includes the Layer-A leaf derivation (`streaming/routing.py` — the per-event `route_table` the Debezium `source_table` masquerade reports) and the `init --mode streaming` proposal engine (`streaming/init.py`); `base` is the flat one-row-per-record projection composing the derivations layer's state-at fold as its whole engine. See [`dimensional.md`](dimensional.md), [`source.md`](source.md), [`base.md`](base.md), [`streaming.md`](streaming.md). | `dimensional`, `streaming`, `source` + `base` implemented (Stages 2–3) |
| `corrupters/` | Base → broken base. The engine (`corrupt_emit`), the seeded selection surface (five-way table-selector resolution, pattern column matching, uniform + placement-weighted samplers), the `Corrupter` operation registry (`null_cells` / `mutate_cells` / `duplicate_rows` / `delete_rows` / `insert_rows` / `schema_drift` / `dangle_reference` / `mispoint_reference` / `freeze_series` / `drop_events` / `shift_sim_time`), the base-emit writer, and the defect manifest (`build_defect_manifest`, `defects.json`) — breaking C6/C7/C9–C12 while preserving C1–C5/C8 and C13's structural clauses by construction. See [`corrupters.md`](corrupters.md). | Implemented (Stage 4) |
| `config/` | Pydantic config envelopes. `ExportConfig` — the two-tier dimensional grammar plus the cross-mode `rebase` and `incremental` blocks (siblings of `mode`); `mode` is `Literal["dimensional", "source", "base"]`, the discriminator-plus-per-mode-section shape (`mode_section_matches`) validating the named mode's section is present (optional for `base`) and the other modes' sections are absent — additive by construction, so a further shape-mode extends the `Literal` and adds its section. `SourceConfig` (`tables`, `events`, `declare_keys` — the declared-table grammar; its section is required, no bare dump) is `mode: source`'s section — see [`source.md`](source.md); `BaseConfig` (`slice_at`, `exclude`, `rename`, plus the `base_slice_at_excludes_incremental` cross-field rule) is `mode: base`'s optional section — see [`base.md`](base.md). `StreamConfig` — a separate top-level envelope (not a mode), because streaming is a delivery driver, not a shape-mode; it declares `content` × a `streams` list of named declarations (the `KindStream` / `MembershipStream` discriminated union — the stream name is the topic), carries the cross-mode `keys` election block, and reuses `rebase`. `CorruptConfig` — a third top-level envelope, sibling of `ExportConfig` / `StreamConfig`; a master `seed` plus an ordered list of `kind`-discriminated operations sharing one selector (`Target` — five-way table selector, pattern column entries) / distribution (`Amount`, `Distribution`) / placement (`Placement`) grammar. See [`streaming.md`](streaming.md), [`corrupters.md`](corrupters.md). | Implemented (Stage 2 + Stage 3 `StreamConfig` + Stage 4 `CorruptConfig`) |
| `anchor.py` | The effective-anchor resolver. Combines sidecar `runtime`, `rebase` config, and CLI overrides into one `EffectiveAnchor` (or `None`); the single authority for origin/zone precedence and DST/ambiguity validation. Every wallclock mode renders through it. See [`anchor.md`](anchor.md). | Implemented (Stage 2) |
| `writers/` | Output adapters. CSV + DuckDB (Stage 2) ship; Parquet is planned. DuckDB has a windowed path (`write_duckdb_window`) for incremental export. See [`writers.md`](writers.md). | CSV + DuckDB implemented |
| `incremental/` | The cross-mode incremental driver: window math, cursor, fingerprint, drip/range orchestration. Wraps a mode's pure range export (dimensional, source, base). See [`incremental.md`](incremental.md). | Implemented |
| `playback/` | The playback seam. Tier 1 (primitive) — `open_playback` / `Playback` / `PlaybackEvent` / `PlaybackSnapshot` / `PlaybackPosition` over atom selections, below the modes; imports the reader, derivations, anchor, `errors`. Tier 2 (shaped) — `open_shaped_playback` / `ShapedPlayback` / `ShapedTable` over a declared `ExportConfig` shape, above the modes; imports `config`, the modes' pure compile surfaces, the notice channel, and the derivations truncated-tape surface. Pull-only, deterministic, permissive; one inclusive-T event-time line. See [`playback.md`](playback.md). | Implemented |
| `cli.py` | `fabulexa-forge validate \| export \| stream \| mixer \| corrupt \| init`. `export` dispatches on `config.mode` to the dimensional, source, or base engine, carries the `--base-date` / `--timezone` rebase overrides, the `--next` / `--from` / `--to` incremental flags (all three modes), and `--fmt csv\|duckdb`. `stream` replays the base layer as a CDC event stream — `--fmt jsonl\|debezium`, `--sink stdout\|file\|kafka` (`--bootstrap-servers` for the kafka sink), plus the shared `--base-date` / `--timezone` anchor overrides. `mixer` replays the base layer as a live, operator-mixable Kafka feed, serving the FabulMixer control API (`--fmt jsonl\|debezium`, `--bootstrap-servers`, `--host` / `--port`, transport / tick flags). `corrupt <emit_dir> --config <corrupt.yaml> --out <out_dir>` applies a `CorruptConfig`, always writing `run.duckdb` + `base.json` + `defects.json`. `init` proposes a commented candidate config per `--mode` (dimensional / source / streaming). | `validate` (Stage 1), `export` + `init` (Stage 2), `stream` + incremental flags + `mixer` + `init --mode streaming` (Stage 3), `corrupt` (Stage 4) implemented |

## Staged roadmap

The sanitised subset is still rich (long-form `history` SCD-2 change-log, membership,
`record_roles`, `enum_domains`). We climb it in stages, each shippable and testable on
its own.

1. **Reader + conformance, trunk-only.** Open an emit, validate `base.json` against the
   vendored schema, version-gate, expose tables/columns/runtime/pins/enum_domains as
   typed accessors, reimplement C1–C14 independently (the producer's
   reference conformance checker is a *reference to read*, never a dependency). The
   sanitised subset mandates exactly one `branches` entry (C8 asserts it).
   `fabulexa-forge validate`.
2. **Dimensional exporter, trunk-only.** `records__<kind>` → `dim_<kind>`, `history` and
   membership bindings → `fact_` tables. Typed `prop__` columns read directly (no JSON
   expansion); SCD-2 derived from long-form `history` via `LEAD`. Config envelope,
   writers (CSV/DuckDB), `fabulexa-forge export` + `init`.
3. **Source + base + streaming exporters, trunk-only.** The author-declared
   app database (source), flat/point-in-time projection (base),
   `history`-change-event replay (streaming). The streaming exporter has shipped as the `fabulexa-forge stream` verb —
   `history` replayed as an ordered `c`/`u`/`d` CDC event stream, to stdout, per-topic
   files, or a Kafka broker, composing the row-state-events derivation (see
   [`streaming.md`](streaming.md)). The source exporter has shipped as `mode: source`
   — the author-declared app-database shape: thing tables (`state`), association
   tables (`junction`), and one polymorphic event log, declared through the
   declared-table grammar, composing the row-state-events and membership-events
   derivations for the event log and the state-at derivation for the windowed
   state snapshot (see [`source.md`](source.md)). The base exporter has shipped
   as `mode: base` — the flat one-row-per-record projection, one table per
   records kind with no declared-table grammar and no event log, composing that
   same state-at derivation as its
   whole engine: the tape's end by default, an inclusive `slice_at: T`
   point-in-time horizon on request, or a per-window snapshot under incremental,
   with the wallclock anchor optional (raw sim-time ns otherwise, unlike source)
   (see [`base.md`](base.md)). Timestamp rebasing is a
   cross-mode surface (the effective anchor, see [`anchor.md`](anchor.md)) that
   shipped with the dimensional exporter; each new wallclock mode resolves through it
   and adds only its own representation of the resolved instant — source is the first
   mode that requires resolution rather than falling back to raw integers. Incremental
   export is likewise a cross-mode driver — it wraps both the dimensional mode and the
   source mode's own windowed compile and base's per-window snapshot compile (see
   [`incremental.md`](incremental.md)) — and
   each new mode wires into the same window derivation, cursor, fingerprint, and
   writers.
4. **Corrupter family.** Reuse the reader; write base-shaped output that breaks
   C6/C7/C9–C12 while preserving C1–C5/C8 and C13's structural clauses by
   construction. Shipped as the `fabulexa-forge
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
5. **Queue-state export.** `membership__*` → queue-state facts (wait time,
   FIFO/priority as SQL). Builds on the sanitised subset (one branch, no
   provenance) — it needs no branch-awareness. **The point-in-time prong of this
   stage is retired:** "`history` replay to any `sim_time` → ML feature-store rows"
   shipped as `mode: base` with `slice_at: T` (see [`base.md`](base.md) §
   Point-in-time subsumption), so it is not built separately. Queue-state is **not**
   subsumed — it reads `membership__*`, derives a different grain, and composes the
   membership-state-at resident rather than per-record reconstitution — and remains
   planned.

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

- A **spanning positive** emit exercises every table category in the sanitised
  subset — `history`, `records__*`, and `membership__*` — plus `pinned_ids`,
  `runtime`, `enum_domains`, a `references` column with its `ref_index__` sibling and
  populated `record_index` ordinals (the dense-index shape — values consistent with the
  target table's ordinals, at least one reference pair NULL-together, and the referenced
  kind mixing decimal-string and hex-digest ids so an implementation that conflates the
  id-space and index-space encodings cannot pass by coincidence), and a `record_roles` registry
  covering every emitted kind (including an `actor` object whose sub-types cover every
  `records__actor.prop__actor_type` value). Every value-carrying `prop__` column
  carries the temporal attribute pair, all three classes are represented (a
  `slice_only` column included), every history-tracked property of every record
  carries its genesis `history` row (NULL-valued rows included, which round-trip
  through C6 as NULL-against-NULL), and presentation columns appear in both classes —
  one bound to a tracked source (class `tracked`, making its kind auditably
  tracked) and one bound to an immutable source (class `constant`, which does not). It carries no
  `firings` table, no provenance column group, and exactly one branch, so each
  C1–C14 check has live input.
- Several **deliberately-broken** variants drive the negative suite — a retyped
  `history` column (C4), a dropped `prop__` column the sidecar still declares
  (C2/C5), a half-NULL membership reference pair (C7), a phantom column (C2/C5), the
  five records-layout defects (each C5 alone — a missing `record_index`, a misplaced
  `record_index`, a reference-annotated `prop__` without its `ref_index__` sibling, a
  `ref_index__` with a non-reference predecessor, a non-`BIGINT` `ref_index__`), a
  `record_roles` registry that omits an emitted kind or an in-data `actor` sub-type
  (C12), a wrong `base_format_version` (the version gate), a broken temporal
  attribute pairing (C13's structural clause alone — the vendored schema does not
  enforce the pairing), an out-of-enum `temporal_class` (C13's enum clause **and**
  C1, the schema enum-constraining the value; the expectation names both), a missing
  genesis row with later rows intact (C13's semantic clause alone), and an emptied
  `(kind, property)` series (C11's converse **and** C13's genesis clause — zero rows
  implies no genesis row; the expectation names both) — plus defects that
  *pass* C1–C14 by design (duplicate tick, dangling records-prop reference), which
  exercise the boundary that C1–C14 is narrower than the producer's QA suite (see
  [`conformance.md`](conformance.md) § Boundaries). A negative fixture must fail the
  check it is named for and no other, except a coupling the contract itself forces —
  which its expectation then names in full.

A later stage that needs multiple branches extends the spanning builder; the
single-branch fixture is enough through Stage 4.

Fixtures are named by what they exercise, never by format version: the spanning
fixture tracks `SUPPORTED_BASE_FORMAT_VERSION` (so a version bump leaves the name
correct), and each negative variant is named by the defect it injects. A version
appears in a name only when version-gating is the assertion under test, and even
then by intent (`wrong_version`), not by the literal number.

**Fixture invariants** (what keeps the next contract re-vendor cheap — semantic
churn is the only cost a version bump should surface, never version-integer or
sidecar-shape churn):

- **The supported version appears as a literal exactly once** —
  [`SUPPORTED_BASE_FORMAT_VERSION`](../../src/fabulexa_forge/__init__.py); every
  other site, the test tree included, imports it. A version-gate negative test uses
  `UNSUPPORTED_VERSION_SENTINEL` — a value the contract has never defined and never
  will — never a neighbouring real version, so a bump cannot quietly turn an
  "unsupported version" test into a supported one.
- **Every well-formed fixture sidecar is written through one function** —
  [`tests/_support/sidecar_builder.py`](../../tests/_support/sidecar_builder.py)'s
  `write_emit`, which stamps the supported version by default and schema-validates
  against the vendored contract before writing, so a fixture that has not learned a
  new required field fails at construction naming the field, not as an unrelated C1
  failure at read time. Because the vendored JSON Schema's generic column shape cannot
  require per-table columns, `write_emit` also asserts the records shape itself before
  writing: every records-category entry classifies totally under the records-column
  taxonomy, `record_index` sits in its slot, and each reference-annotated `prop__`
  entry is immediately followed by its `ref_index__` sibling — failure is a
  construction-time error naming table + column. Negative fixtures whose declared
  defect is schema-level opt out via `schema_valid=False`; those whose declared defect
  *is* a records-shape defect opt out via the sibling `records_shape_valid=False` — the
  two nets stay independently addressable. Deliberately *malformed* specimens
  exercising the reader's rejection paths (invalid JSON, below-floor structure) are the
  only literal writes.
- **Every value-carrying column that declares temporal attributes is constructed
  through one constructor** — `prop_column`, which requires the pair together and
  validates the contract's implication clauses, so a defective pairing is never
  expressible through it (negative variants mutate the returned dict). A new paired
  attribute at the next bump is one signature change, and the type checker names
  every call site.
- **Every identity column entry is constructed through one constructor** —
  `identity_column`, sibling of `prop_column`: the sole constructor for every fixture
  identity entry (`fork_path` / `record_id` / `record_index` / `ref_index__<name>`),
  records and membership table entries alike — the check is a pure name rule, so a
  membership table's `fork_path` / `record_id` entries flow through it too. It emits a
  bare `{name, type}` entry and rejects a non-identity-family name, so a temporal
  attribute or `references` annotation on an identity column is inexpressible through
  it (negative variants mutate the returned dict, mirroring `prop_column`'s
  convention).
- **Prose is version-free.** The supported version integer appears exactly twice
  outside `contract/`: the code literal (`SUPPORTED_BASE_FORMAT_VERSION`) and the
  status-table row in this README. All other prose — docstrings, comments, arch
  docs, diagrams — names the contract or its sections ('the vendored schema', '§
  Dense record index'), never the integer: the version gate admits exactly one
  version, so 'a v6 shape' carries no information 'the contract's shape' doesn't,
  and decays at the next bump. The version-literal hygiene test enforces this
  against an explicit allowlist; a historical-rationale mention (where the version
  *is* the content) is allowlisted with its reason.

## Status

| Area | Status |
|---|---|
| Project skeleton + standalone-venv boundary | Scaffolded |
| Vendored contract (`base_format_version 7`) | Vendored — re-synced on version bump (`contract/README.md`) |
| Reader + conformance | Implemented (Stage 1) — [`reader.md`](reader.md), [`conformance.md`](conformance.md) |
| Reader structural-temporal surface — `StructuralInstant`, `structural_instant_columns`, `records_structural_column_is_mutable`; the closed table-category gate at the sidecar structural floor; dimensional / source / base resolving their instant sets through it | Implemented — [`reader.md`](reader.md) § The structural-temporal surface |
| `fabulexa-forge validate` CLI verb | Implemented (Stage 1) |
| Dimensional exporter + config + CSV/DuckDB writers | Implemented (Stage 2) — [`dimensional.md`](dimensional.md) |
| `fabulexa-forge export` + `init` CLI verbs | Implemented (Stage 2) |
| Effective anchor + timestamp rebasing (`rebase` config, `--base-date` / `--timezone`) | Implemented (Stage 2) — [`anchor.md`](anchor.md) |
| Incremental export (`incremental` config, `--next` / `--from` / `--to`, cursor + SCD-2 view) | Implemented (cross-mode driver over dimensional + source + base) — [`incremental.md`](incremental.md) |
| Derivations layer + versioned-intervals / reference-resolution / row-state-events / membership-events / state-at (horizoned + end-of-tape) / membership-state-at / record-index (horizoned + end-of-tape) residents + the truncated-tape surface + single-branch guard | Implemented (Stage 3, extended for playback and for base's key columns) — [`derivations.md`](derivations.md) |
| Streaming exporter + `StreamConfig` + `fabulexa-forge stream` (author-declared streams — the `streams` list of named `KindStream` / `MembershipStream` declarations, the name as topic, sub-type-scoped populations, payload-independent event sets, per-stream `properties` / `fields` projections, the `keys` message-key election, declared-but-empty topics, the `route_table` leaf + Debezium `table_identity` masquerade; `state-changes`: `history` → ordered `c`/`u`/`d` CDC stream; `membership-events`: `membership__<K>__<p>` → ordered `join`/`leave` stream; `jsonl` + `debezium` formats to stdout / per-topic files / a Kafka broker) + `init --mode streaming` | Implemented (Stage 3) — [`streaming.md`](streaming.md) |
| Streaming pacing — realtime delivery (`ClockConfig`: `mode` / `speed` / `idle_cap_seconds`, `--speed` / `--idle-cap` / `--fast`), drift-free release schedule, paced per-line-flush sinks | Implemented (Stage 3) — [`streaming-pacing.md`](streaming-pacing.md) |
| Streaming Kafka sink — `--sink kafka` (`KafkaConfig`, `--bootstrap-servers` / `FABEXPORT_KAFKA_BOOTSTRAP`); one message per event, topic pre-creation (1 partition / RF 1), elected-key keying, flush-before-return; `confluent-kafka` optional `[kafka]` extra | Implemented (Stage 3) — [`streaming.md`](streaming.md) § The Kafka sink |
| Streaming mixer scheduler — `ControlState` (`Transport` + per-topic `TopicDials`), `FrontierState`, the pure per-tick `advance` (master frontier × per-topic lag / rate / mute), and the async `schedule_releases` driver over `seed_mixer_run` per-topic buffers. The headless correctness core of the FabulMixer live-perform POC | Implemented — [`streaming-mixer.md`](streaming-mixer.md) |
| Streaming mixer control plane — the `fabulexa-forge mixer` driver: sync setup → async serve, the lock-free single-loop `MixerRunState`, the FastAPI control API (`/api/state` · `/api/meters` · `PUT /api/transport` · `PUT /api/topics/{topic}`) mirroring the shared control-API contract, the async `KafkaSink`, and the producer-side tier-1 meters derivation. Kafka-only, behind a `[mixer]` extra composing `[kafka]` | Implemented — [`mixer-control-plane.md`](mixer-control-plane.md) |
| Streaming mixer consumer instrument — the optional `--consumer` downstream simulator: `KafkaSource` read-back, the pure `ingest` tick + async `run_consumer` over a second `ConsumerControlState` / `ConsumerState` pair, per-topic + global watermark (`min` across data-bearing topics), tumbling windows, enrichment-join null health, `derive_consumer_meters`, the gated `/api/consumer/*` + `/api/capabilities` routes, and the `--window` / `--join` / `--consumer-group` / `--consumer-offset` flags. Reads the broker only | Implemented — [`mixer-consumer.md`](mixer-consumer.md) |
| Source exporter — `mode: source` declared-table grammar (author-named `state` / `junction` tables + one polymorphic event log keyed by a dense tape-anchored `id` publishing the log's total order), per-table `columns`/`rename`, operational presentation defaults, mandatory wallclock anchor, corrupt→source composition, cross-mode incremental composition (windowed state snapshots via the state-at derivation, appended event log, junction extract-on-change), `init --mode source` | Implemented (Stage 3) — [`source.md`](source.md) |
| Source row selection — the constant-gated `where` on state tables, junction tables, and event-log sources; owner `sub_types` / `where` on membership units through the parent lookup (a fan-out-free owner semi-join) with owner `sub_types` narrowing the addressed population set; plan-time predicate-literal casts and selection-aware event-source disjointness over typed values; `init`'s per-sub-type membership-estate proposals | Implemented — [`source.md`](source.md) § Row selection |
| Playback seam — tier 1 (`open_playback` / `events` / `snapshot` / `seek` over atom selections; `PlaybackEvent`, entry-point-invariant `seq`, the consistency algebra) and tier 2 (`open_shaped_playback` / `window` / `state` over a declared `ExportConfig` shape; the promoted window-membership contract, the truncated-tape `state` compile via `base_relations`); one inclusive-T event-time line, pull-only, deterministic, permissive; `last_mutation_sim_time` reserved-output-name posture; the `base_relations` indirection (dimensional compile parameter; post-compile rewrite for source) | Implemented — [`playback.md`](playback.md) |
| `slice_only` export policy — always-on refusal (dimensional `SliceOnlyColumnRefused` + `LookupColumnSafety` constant-regate, streaming `StreamPropertySliceOnly`), source auto-projection omission + `SourceSliceOnlyRead`, base per-kind omission + `BaseRenameSliceOnly`, `init` skip, the discriminator carve-out | Implemented — [`slice-only.md`](slice-only.md) |
| Notice channel — `Notice` / `NoticeSink` / `render_notice_stderr`, required `notice_sink` on every emitting entry point, `slice-only-column-omitted` + `discriminator-value-unobserved` + `reference-key-target-absent` + `keys-not-declarable-csv` codes | Implemented — [`notices.md`](notices.md) |
| Base exporter — `mode: base` flat one-row-per-record projection (one table per records kind, no declared-table grammar, no event log), the three horizons (tape's end · inclusive `slice_at: T` · per-window snapshot under incremental) composing the state-at derivation as its whole engine, `slice_only` omit-with-notice, operational presentation defaults + `exclude`/`rename` + reserved-name check, cast-back to sidecar types, optional anchor (raw-ns fallback), corrupt→base composition | Implemented (Stage 3) — [`base.md`](base.md) |
| Base record-index key columns — a `BIGINT` `<kind>_key` self key first on every table and a `<p>_key` edge key after each reference property's id-space column, both resolved through the record-index resident at the table's own horizon, edge keys always re-derived; `record_index` / `ref_index__<p>` joining the `rename`, collision, and reserved-name domains; the `reference-key-target-absent` notice | Implemented — [`base.md`](base.md) § Record-index key columns |
| Declared keys — the strict `Sidecar.presentation_keys()` view + union-safety algebra (`union_safe` / `combined_claim`, `PresentationKeysInvalidError`), opt-in `declare_keys` on base + source (`TableKeys` on `QuerySpec`, DuckDB `PRIMARY KEY` / `UNIQUE` materialization, full + windowed), the source event log's constructed `PRIMARY KEY (id)` as a third declaration ground beside contract guarantee and block claim, the `keys-not-declarable-csv` notice, dimensional `init`'s natural-key advisory | Implemented — [`declared-keys.md`](declared-keys.md) |
| Key election — the cross-mode `keys` block (`ExportConfig.keys` / `StreamConfig.keys`, `fk.target_key` over three surfaces), `resolve_election` + the `Election` view, the identity/edge combination gates over the union-safety algebra, the presentation-key derivation, per-mode elected rendering (source, base, dimensional FK inheritance + dim-key agreement, streaming's message key), the render-time uniqueness guard, `init`'s self-gated `keys` proposal | Implemented — [`key-election.md`](key-election.md) |
| Config row predicates — the scalar-or-list grammar over the dimensional mode's five predicate surfaces and the source mode's two, the `render_predicate_condition` authority in the shared SQL utilities (the two private typed-literal forks consolidated into it), the `PredicateValue` well-formedness rule, per-element unobserved-value notices, and the dim source population set as the discriminator conjunct's selected subset | Implemented — [`row-predicates.md`](row-predicates.md) |
| Temporal rendering elections — `date` / `time` / `timestamptz` instant renderings and `interval` elapsed rendering via `render_anchor_temporal_expr` (the generalized single shared renderer), a declared VARCHAR→temporal parse over the instant-string family via `render_date_parse_expr` (the closed date + time-of-day directive vocabulary validated by `validate_date_parse_format`, the `DATE` / `TIME` / naive `TIMESTAMP` output derived by the sole authority `date_parse_denoted_type`), the reader's session-zone pin, pinned CSV text forms for the elected types, the `TemporalRenderRequiresAnchor` / `DateParseSourceColumn` / `RenderKeyIsInstantColumn` business rules, and per-mode attach points on the dimensional, source, and base exporters (including the election-aware ordinal amendment and incremental window-key rule) | Implemented — [`temporal-elections.md`](temporal-elections.md) |
| SCD-2 per-record derived columns — `derived: timestamp` / `date_parse` / `value_map` on an `scd: type2` dim, compiled through the records-grain column builders bound to the type2 build's records relation, gated by `Scd2ColumnModeSupported` (admitted modes) and `Scd2DerivedSourceConstant` (`temporal_class: constant` sources only) | Implemented — [`dimensional.md`](dimensional.md) § SCD-2 wide reconstruction |
| Parquet writer | Planned (Stage 3) |
| Corrupters — `CorruptConfig` envelope, the selector/distribution/placement grammar (five-way multi-table selection, pattern column entries, `entity_scoped` / `clustered_temporal` / `correlated` biased placement), twelve operations (`null_cells` / `mutate_cells` / `duplicate_rows` / `delete_rows` / `insert_rows` / `schema_drift` / `dangle_reference` / `mispoint_reference` / `freeze_series` / `drop_events` / `shift_sim_time` / `distort_intervals`), the engine (`corrupt_emit`), the base-emit writer, family C's series/event units and C6-mirroring impact oracle, family E's member-timeline/interval units, and the defect manifest (`defects.json`) | Implemented (Stage 4) — [`corrupters.md`](corrupters.md) |
| Compare surface — `compare_datasets` / `fabulexa-forge compare` (dataset-equivalence verdict + deterministic bounded discrepancy report: canonical families, Python-side canonical encoding, UTC-pinned session, multiset row comparison, text/JSON renderers, `0/1/2` exit codes) | Implemented — [`compare.md`](compare.md) |
| Queue-state export (`membership__*` → wait time, FIFO/priority) | Planned (Stage 5) — the point-in-time prong is retired, shipped as `mode: base` + `slice_at` |
| Branch-aware export + provenance lineage | Parked — needs a multi-branch / provenance contract |
| Spanning + negative fixtures | Implemented (programmatic, `tests/reader/_fixtures_build.py`) |
