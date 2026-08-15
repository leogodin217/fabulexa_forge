# Capabilities

**Purpose:** What the export package's exporters and corrupters do — the feature
inventory and its status. The tracking surface so we don't lose features between
stages.
**Audience:** LLM planners, architects, implementers; downstream authors.

---

## Status legend

Every feature bullet carries one marker. Read this as a capability inventory, not a
process tracker — the marker says whether an author can run the feature from the CLI
against a base-layer emit today.

| Symbol | Meaning |
|---|---|
| ✓ | Shipped end-to-end (CLI runs it against a base emit) |
| ◐ | Partial — works for some shape; gaps called out inline |
| ○ | Not shipped |

**Stage** tags reference the roadmap in
[`architecture/README.md`](architecture/README.md) (1 reader · 2 dimensional ·
3 source/base/streaming · 4 corrupter · 5 queue-state). The Stage-1 reader +
conformance (`fabulexa-forge validate`) and the Stage-2 dimensional exporter (`fabulexa-forge
export` + `init`, CSV + DuckDB) have shipped, along with two cross-mode surfaces —
timestamp rebasing and incremental export, the latter wired over the dimensional,
source, and base modes. The derivations layer (versioned-intervals + reference-resolution +
row-state-events + membership-events + state-at residents, the single-branch guard)
has shipped, composed by the dimensional, streaming, source, and base modes. All three
remaining Stage-3 modes have shipped — streaming (`fabulexa-forge stream` —
`state-changes` `c`/`u`/`d` CDC and `membership-events` `join`/`leave` content),
source (`mode: source` — the author-declared app database: `state` / `junction`
tables plus one polymorphic event log), and base (`mode: base` — the flat
one-row-per-record projection with an optional `slice_at` point-in-time horizon) —
as has the Stage-4 corrupter family (`fabulexa-forge corrupt`). Stage 5's
queue-state export remains planned; its point-in-time prong is retired, delivered
by `mode: base`.

---

## Overview

```
base-layer emit (run.duckdb + base.json @ the supported `base_format_version`)
        │
        ▼
   reader  ──▶ exporters  (base → different shape)
           └─▶ corrupters (base → realistically-broken base)
        │
        ▼
   datasets (CSV · DuckDB · Parquet ○ planned)
```

The input contract is the vendored base-layer spec (`contract/`). Every feature reads
through the one reader; the vendored `contract/` is the only coupling (see `../CLAUDE.md`).

---

## Reader and conformance — the foundation *(Stage 1)*

See [`architecture/reader.md`](architecture/reader.md) and
[`architecture/conformance.md`](architecture/conformance.md).

- ✓ **Open an emit** — `open_emit(emit_dir)` over `run.duckdb` + `base.json`.
- ✓ **Sidecar parse + version-gate** — typed view of `base.json`; refuse any
  `base_format_version` the vendored contract does not define (no auto-upgrade).
- ✓ **Typed accessors** — tables, columns, branches, `runtime` anchor, `pinned_ids`,
  `enum_domains`, per-column `references`, and the per-column temporal pair
  (`history_tracked` + the `Sidecar.temporal_class` narrowing accessor — verbatim
  carry, never inferred) — all read from the sidecar, never hard-coded.
- ✓ **Conformance C1–C14** — reimplemented independently of the producer (`fabulexa-forge
  validate <emit_dir>`). The producer's reference conformance checker is a reference to
  read, never a dependency.
- ✓ **Records-column taxonomy** — `records_column_role` classifies every
  records-category column by name family (`identity` / `presentation` / `lifecycle` /
  `payload`, or loudly *no role*); the one classifier conformance C5, the source
  planner, and dimensional `init` read through. `ref_index_sibling` owns the
  `prop__<name>` ↔ `ref_index__<name>` pairing rule.
- ✓ **Record-role registry accessor** — `Sidecar.record_roles()` exposes the optional
  `record_roles` registry as a typed `RecordRoles` view (or `None` when absent),
  resolving each kind's warehouse role (`dimension` / `fact`) — `actor` per sub-type,
  every other kind as a bare role — without callers re-deriving the object-vs-string
  rule.
- ✓ **Sub-type column partition accessor** — `Sidecar.sub_type_columns()` exposes the
  optional `sub_type_columns` partition as a typed `SubTypeColumns` view (or `None`
  when absent, distinct from a present-but-empty per-sub-type list), naming the value
  columns each sub-type of a sub-typed kind declares — the NULL-disambiguation surface
  (structurally-inapplicable vs value-absent). C14 verifies its consistency; `init`
  reads it to prune per-sub-type column proposals.
- ✓ **Presentation-keys accessor** — `Sidecar.presentation_keys()` exposes the optional
  `presentation_keys` block as a typed `PresentationKeys` view (or `None` when absent —
  "no claims"), strict on read: a present-but-incoherent block raises
  `PresentationKeysInvalidError` rather than yielding claims a consumer would key on.
  Beside it, the contract's union-safety algebra (`union_safe`, `combined_claim`) as
  pure, kind-scoped functions. See [`architecture/reader.md`](architecture/reader.md)
  § The presentation-keys registry is strict on read.

---

## Exporters

### Modes

Each mode reads the same emit and writes a different target shape.

- ✓ **base** *(Stage 3)* — flat single-branch projection: one table per records kind,
  one row per record, every tracked property carrying its reconstituted value. No
  declared-table grammar and no classification — every records kind yields exactly one
  table; membership, junction, and fact tables are never emitted. Each table presents
  both encodings of every identity: an integer `<kind>_key` surrogate first (the
  record's `record_index`), the opaque `id` beside it, and after each reference
  property's id column a `<p>_key` carrying the target's index — re-derived at the
  table's own horizon, never read from the emitted `ref_index__` column, and NULL for a
  reference that resolves to nothing. It composes the
  shipped state-at derivation for values and the record-index derivation for keys as its
  *whole* engine (no new point-in-time
  reconstruction path) at one of three horizons: the tape's end by default (current
  state, via the end-of-tape entry point), an inclusive `slice_at: T` point-in-time
  horizon for a full export, or each window's horizon under an incremental
  invocation (a per-window full-table snapshot) — `slice_at` and `incremental` are
  mutually exclusive at load time. A `slice_only` property is omitted with a
  `slice-only-column-omitted` notice (the source-style auto-projection posture; the
  sub-typed-discriminator carve-out honored, a `rename` naming an omitted column
  errors). Operational presentation defaults (prefix-stripped table names,
  `record_id` → `id`, `<kind>_key` / `<p>_key` for the two key families) apply,
  overridable via `exclude`/`rename` keyed on the contract identity; a name collision
  fails fast. Data columns cast back from the state-at codec VARCHAR to their
  declared sidecar types, so the table is typed, not all-string. The wallclock
  anchor is **optional** — absent one, lifecycle timestamps stay raw sim-time ns
  (explicitly unlike source, which requires a resolved anchor) — so raw sim-time
  keys remain a legitimate landing shape. A base export over a corrupted emit
  surfaces the corrupter's declared defects unchanged (test-guarded, never
  special-cased). CSV + DuckDB output. See
  [`architecture/base.md`](architecture/base.md). *Teaches: incremental ETL, SCD
  merge, point-in-time / feature-store reconstruction.*
- ✓ **dimensional** *(Stage 2)* — star schema. `records__<kind>` → `dim_*` (SCD-2 wide
  via `LEAD`, or Type-1 sub-type split); `history` point/interval and membership-binding
  grains → `fact_*`; typed `prop__` columns read directly (no JSON expansion); FK
  labeled-edge pathfind (reference + membership); `lookup` record-attribute
  enrichment (slice-value projection, gated to `temporal_class: constant`).
  Declarative, domain-agnostic config; CSV + DuckDB output. See
  [`architecture/dimensional.md`](architecture/dimensional.md). *Teaches: data
  warehousing, BI, star-schema design.*
- ✓ **source** *(Stage 3)* — the well-architected app database, declared table by
  table: *things get tables; events get the log.* A source config declares every
  output table through the declared-table grammar — author-verbatim name, source
  populations (`kind`, optional `sub_types` subset, or a `membership` reference),
  optional per-table `columns` / `rename`, and optional row selection. A records declaration renders as a
  `state` thing-table (one current row per record, soft-delete lifecycle:
  `created_at` / `updated_at` / `active` / `deactivated_at`); a membership
  declaration as a `junction` association table (`joined_at` / `left_at`, NULL
  while open); the single `events` block as one polymorphic audit log at event
  grain (`id`, `item_type`, `item_id`, `event`, `occurred_at`, `changes` — a
  deterministic JSON changeset of `[old, new]` pairs composing the
  row-state-events and membership-events folds, keyed by a dense tape-anchored
  `id` that publishes the log's total order and is its primary key under
  `declare_keys`), with `only` / `ignore`
  audited-property filters per source. Row selection narrows a declared unit on
  two axes: `sub_types` picks populations, and `where` — a scalar-or-list
  predicate gated to `temporal_class: constant` payload properties, so its row
  set is identical at every horizon — picks rows. Both are legal on state
  tables, junction tables, and event-log sources; on a membership unit they read
  the *owner* through a fan-out-free identity join, so a sub-typed kind's
  junctions and join/leave streams split alongside its state tables, and a kind
  partitioned by an undeclared-but-constant property (a de facto discriminator)
  splits into separate tables with separate audit streams. Predicate literals
  are cast against the sidecar's declared type at plan time, and two event
  sources auditing one item space are legal only where their owner `sub_types`
  or a common predicated column's typed value sets are disjoint. `init` proposes
  per-sub-type junction and membership-event stubs for a sub-typed owner, and
  never proposes a `where`. An author-declared domain vocabulary
  resolves kind names and `changes` keys that would otherwise render engine
  vocabulary as data: `source.kind_labels` (engine kind → domain label,
  applied wherever a kind name renders as a value — item-type defaults,
  `<f>_kind` entries, junction member-kind values, identity fall-through for
  anything unmapped) plus per-events-source `item_type` (wholesale item-type
  override) and `rename` (audited property → `changes` output key). Sidecar facts gate declarations (unknown
  kind / sub-type / membership fails fast); they never decide layout — omission
  is the exclusion mechanism, and a config declaring no output is a load-time
  error. Operational presentation defaults (prefix-stripped names,
  `record_id` → `id`) apply throughout; a name collision fails fast. Source
  *requires* a resolved wallclock anchor rather than falling back to raw
  integers. `--next` / `--from` / `--to` compose the cross-mode incremental
  driver with per-render window membership: per-window state snapshots at the
  window horizon (state-at derivation, `updated_at` omitted — horizon honesty),
  the event log appended by `event_sim_time`, junction rows extract-on-change
  (`left_at` horizon-masked) — the no-CDC nightly-extract archetype whole. A
  source export over a corrupted emit surfaces the corrupter's declared defects
  unchanged (test-guarded, never special-cased). CSV + DuckDB output. See
  [`architecture/source.md`](architecture/source.md). *Teaches: app-database
  schemas, audit logs, source-to-warehouse ETL.*
- ◐ **streaming** *(Stage 3)* — `fabulexa-forge stream` replays the base layer as an ordered
  event stream of author-declared streams: a `streams` list of named declarations, each
  feeding from one kind's populations (one or more sub-types, or the whole kind) or one
  membership table, where the stream `name` *is* the topic — the Kafka topic, the
  `<name>.jsonl` filename. Each stream projects its own `properties` / `fields` after-image
  (per-sub-type column lists from the sidecar's `sub_type_columns` partition; `[]` is an
  identity-only notification feed), while the event set is payload-independent — a fact of
  the stream's populations, row-level CDC. Two content axes: `state-changes` replays
  `history` + the records spine as a `c`/`u`/`d` CDC stream, one event per record state
  change; `membership-events` replays the `membership__<K>__<p>` interval tables,
  unpivoting each interval into a `join` (always) and a `leave` (when the element left
  within the slice), keyed on the owner — an append-only fact log. The cross-mode `keys`
  election block elects the message key per population (`record_id` default /
  `record_index` / `presentation_id`), with after-image references rendered in their
  target's elected surface ([`architecture/key-election.md`](architecture/key-election.md)).
  Serialized to stdout (all topics interleaved in global `seq` order), one
  `<topic>.jsonl` per topic, or one message per event to a Kafka broker (`--sink kafka`:
  topic pre-creation at 1 partition, elected-key keying, and CLI/config/env bootstrap
  resolution). Two formats: `jsonl` (flat `{seq, op, ts, kind, key, after}`) and
  `debezium` (the Debezium value message — `{schema?, payload}` with per-stream value
  schemas, the configurable masquerade `source` identity, the `table_identity` knob
  reporting the per-event `route_table` leaf or the stream name, and epoch-millisecond
  `ts_ms`; requires a resolved anchor; both content types — `membership-events` renders
  insert-only with the domain op as a leading `event` column). Full-row after-images,
  anchored timestamps; composes the row-state-events (two-scope) and membership-events
  derivations. A configurable clock paces emission — `realtime × speed` with an idle
  cap, or as-fast-as-possible (`--speed` / `--idle-cap` / `--fast`). *Gaps:* the Debezium
  value message only (no separate key message or compaction tombstone), and whole-stream
  (not windowed). See
  [`architecture/streaming.md`](architecture/streaming.md).
  *Teaches: streaming ingestion, event-time processing, CDC.*

### Shared exporter features *(Stage 2 unless noted)*

- ✓ **Type / table exclusion** — `exclude.kinds` / `exclude.tables` drop kinds or
  sidecar tables before export (validated so no declared table sources an excluded one).
- ✓ **Table / column rename** — every output `name` is author-verbatim; `init` proposes
  prefix-stripped names with a structural-column collision check.
- ◐ **Output transforms** — `derived` columns ship (ordinal, value-map, anchored
  timestamp, SCD window); arbitrary per-table transforms beyond these are not.
- ✓ **`init`** — generate a commented candidate config from the sidecar; `--mode`
  selects the target (`dimensional`, the default, `source`, or `streaming`). Dimensional
  reads `record_roles` for warehouse role, kinds, discriminators, membership tables,
  `history_tracked`; when the sidecar carries `sub_type_columns`, each
  per-sub-type stub proposes only that sub-type's declared columns
  (structurally-inapplicable columns pruned); absent the field, it falls back to
  the full union. Each SCD-2 column proposal states its versions-per-record ratio
  from the sidecar's advisory `row_census` — the evidence that a tracked column is
  operational state rather than a slowly-changing attribute — and says so
  explicitly when the emit carries no census. Source proposes one state table per records kind (combined,
  with the per-sub-type split alternative in comments), one junction table per
  membership table, an `events` stub covering every tracked kind
  (lifecycle-only kinds and membership sources commented out), and the aligned
  `keys` block — consuming no `record_roles`; the emitted config always parses
  and plans clean. Streaming proposes one live stream per population — per declared
  sub-type for a sub-typed kind (names verbatim, `properties` from the
  `sub_type_columns` partition), per kind for a flat kind — with the membership-events
  alternative fully commented, name-collision losers and topic-illegal names commented
  out, and the self-gated `keys` block; the emitted config always parses and streams
  clean, and a recordless emit is refused rather than proposed.
- ✓ **List-valued row predicates** *(dimensional, source)* — every predicate value in
  the dimensional grammar (`source.filter`, `source.where`, `source.value`, a membership
  `fk.where`, `derived.elapsed.other_where`) and in source's two selection surfaces
  (`tables[].where`, `events.sources[].where`) is a scalar or a non-empty list of
  alternatives, compiling to `=` or `IN` through one rendering authority. A list is
  what groups several discriminator values into one named table — the domain's own
  shape (an NHS "Emergency Care" dataset spanning several decision types) instead of
  one table per value or one undifferentiated table. Equality and set membership
  only; entries over distinct columns are AND-joined. On a sub-typed dim's
  discriminator the value set also selects the dim's source population set, keeping
  FK output closed over its target. Each mode adds its own gate on which columns are
  addressable — never a second value grammar. See
  [`architecture/row-predicates.md`](architecture/row-predicates.md).
  *Teaches: authoring warehouse subject areas that don't line up 1:1 with source
  event types.*
- ✓ **Incremental drip-feed** — window-at-a-time export, wired for the
  dimensional, source, and base modes: `--next` reads a cursor and emits the next window
  (or `--from`/`--to` runs a stateless range), one calendar period
  (`day`/`week`/`month`, anchor-resolved) or sim-time interval per window.
  Dimensional: append-only facts and SCD-2 version rows (`valid_to` supplied by a
  view, never materialized); full-snapshot type-1 dims. Source: per-render window
  membership (see the source mode above). Base: every table reconstructed at the
  window horizon — a full-table snapshot per kind per window. Any mode: a growing DuckDB warehouse
  (cursor atomic with data) or one CSV drop directory per window. See
  [`architecture/incremental.md`](architecture/incremental.md). *Teaches: incremental/
  merge ETL, landing zones, building SCD-2 yourself.*
- ✓ **Timestamp rebasing** *(Stage 2)* — map `sim_time` (ns offset) to wallclock
  through the resolved effective anchor: an author-chosen origin (`rebase.base_date` /
  `--base-date`) and zone (`rebase.timezone` / `--timezone`), falling back to the
  sidecar `runtime` anchor. CLI-wins precedence per knob; DST and ambiguous-origin
  rules fail fast. Cross-mode (one anchor per invocation): the dimensional, source,
  streaming, and base renderers all consume it — source and the `debezium` stream
  format *require* a resolved anchor, dimensional and base fall back to raw
  sim-time ns. See [`architecture/anchor.md`](architecture/anchor.md).
- ✓ **`slice_only` export policy** — export-wide: no output value, row membership,
  linkage, or ordering derives from a `slice_only` column's value. Author-named reads
  refused always-on (dimensional + streaming); auto-projected surfaces omit with a
  notice (source renders, base's flat projection, `init` proposals); `lookup` regated to
  `temporal_class: constant`; one mechanical carve-out for the sub-typed
  discriminator. See [`architecture/slice-only.md`](architecture/slice-only.md).
- ✓ **Declared keys** *(post-Stage 4)* — opt-in `declare_keys` on the base and source
  modes: each output table declares a record-identity primary key plus
  `presentation_id` uniqueness exactly where the sidecar's `presentation_keys` block
  claims it; DuckDB materializes real `PRIMARY KEY` / `UNIQUE` constraints (full and
  windowed export alike), CSV records the undeliverable declaration with a
  `keys-not-declarable-csv` notice, and dimensional `init` annotates its stubs with
  the claimed natural key. Off by default — output byte-identical; claims are never
  validated against data. See
  [`architecture/declared-keys.md`](architecture/declared-keys.md).
- ✓ **Key election** *(post-Stage 4)* — cross-mode `keys` config block electing, per
  population (per sub-type for sub-typed kinds, per kind for flat), which of the emit's
  identity surfaces — `record_id` / `record_index` / `presentation_id` — presents as
  that population's exported identity, with every referencing column rendered in its
  *target's* elected surface (re-derived at the export horizon through the record-index
  / presentation-key join relations). Statically gated against the sidecar's
  `presentation_keys` registry and the contract's union-safety algebra (one table, one
  identity surface; edges pairwise union-safe); one render-time uniqueness guard
  refuses silently-broken joins over corrupted identities. Source renders the elected
  surface per declared table (the event-log `item_id` as a polymorphic edge); base
  makes the id-space value surface elective beside its
  always-on index keys; dimensional FKs inherit the destination dim's election
  (`fk.target_key` per-edge override, dim-key agreement check); streaming renders the
  elected surface as the message key (one stream, one key surface); `init` proposes a
  self-gated `keys` block. Absent the block, output keeps the `record_id` default.
  Forge never mints — election selects among surfaces the emit carries. See
  [`architecture/key-election.md`](architecture/key-election.md).
- ✓ **Notice channel** — deterministic, non-fatal informational records (`Notice`)
  through a required caller-supplied sink; CLI renders one line per notice to stderr,
  off stdout. See [`architecture/notices.md`](architecture/notices.md).
- ○ **Dry-run / combine** — preview without writing; compose multiple emits.

### Queue-state export *(Stage 5)*

Builds on the sanitised subset (one branch, no provenance); it needs no
branch-awareness.

- ○ **Membership / queue export** — `membership__*` → queue-state facts (wait time,
  FIFO/priority order as SQL). Explicitly **not** subsumed by base: a different
  grain (member intervals, not per-record state) over a different derivation
  (membership-state-at, not state-at).
- ✓ **Point-in-time reconstruction** — *retired as a separate item; shipped as
  `mode: base` with `slice_at: T`* — replay `history` to any `sim_time` → one flat
  row per record, the ML feature-store shape. See
  [`architecture/base.md`](architecture/base.md).

### Parked — needs a future contract extension

The sanitised-subset contract mandates exactly one branch and carries no provenance, so
these are out of reach until the contract restores multi-branch / provenance:

- ○ **Branch selection** — export a chosen `fork_path`.
- ○ **Paired-counterfactual export** — aligned datasets across two branches.
- ○ **Provenance lineage columns** — carry `caused_by` / `written_by` into outputs.

---

## Corrupters *(Stage 4)*

Read base, write base. Break **semantic** conformance (C6/C7/C9–C13, including C13's
genesis clause — now a declared impact) while preserving **structural** conformance
(C1–C5, C8, C13's structural clauses, and the sidecar-only C14); output stays base-shaped
so any exporter can run
downstream of a corrupter. A `CorruptConfig` YAML envelope (sibling of `ExportConfig` /
`StreamConfig`) declares a seed and an ordered list of operations over a shared
selector/distribution/placement grammar; every run also writes `defects.json`, a
deterministic, label-grade defect manifest naming every injected defect and the guarantee
it breaks. See [`architecture/corrupters.md`](architecture/corrupters.md).

- ✓ **Missing values** (`null_cells`) — null sampled value cells.
- ✓ **Wrong values** (`mutate_cells`) — eleven type-preserving transforms over records/
  membership payload columns and the `history.value` changelog side of C6: a
  sentinel-disguised null, identity mutations (typo, case, whitespace), truncation,
  precision-drop, magnitude-scale, mojibake/format-dirt, an intra-column resample, and an
  out-of-domain synthesize. Family A's completion, alongside `null_cells` — never nulls a
  value itself, and the only corrupter operation that can trip C12.
- ✓ **Duplicate, near-duplicate & conflicting-duplicate rows** (`duplicate_rows`) — exact
  copies, near-duplicates perturbed by a numeric-additive `jitter` distribution, or
  conflicting duplicates whose copy is transformed through a `mutation` (the same
  eleven-kind vocabulary `mutate_cells` uses) — the split-brain / fuzzy-entity-resolution
  shape (`"Jon"` / `"John"`, a stale price on a second copy).
- ✓ **Row deletion** (`delete_rows`) — remove sampled rows from records/membership
  tables, declaring the referential, pin, and history wake the removal trips: an orphaned
  `history` series (C6), a dangling pin (C9), a dangling membership reference (C10), or
  subconformance the check suite cannot see.
- ✓ **Phantom-row injection** (`insert_rows`) — clone sampled donor rows under a fresh,
  plausible `record_id` guaranteed absent from the kind's id universe; optionally
  resample matched payload columns so the ghost record doesn't mirror its donor.
- ✓ **Schema drift** (`schema_drift`) — rename/retype/drop payload columns.
- ✓ **Referential breakage** (`dangle_reference`) — rewrite sampled reference ids to a
  guaranteed-absent sentinel (deliberate C10 violation).
- ✓ **Referential mis-pointing** (`mispoint_reference`) — rewrite sampled reference ids to
  a wrong-but-real donor row from the same target table: RI stays green (C10 and C7 pass
  by construction), but the reference points at the wrong entity — recoverable only via
  `defects.json`. `constraint: created_after_reference` narrows the donor pool to a
  point-in-time dangling reference: one that resolves now but was dangling when written.
- ✓ **Temporal defects over `history`** (family C: `freeze_series`, `drop_events`,
  `shift_sim_time`) — suppress a change series' tail so its value sticks, drop sampled
  events (lost CDC messages), or skew/collide/reorder event timestamps. Each defect
  declares `C6`, `C11`, `C13`, or `beyond-c1-c12` by mirroring C6's round-trip predicate,
  C11's converse, and C13's genesis clause against the working state (a drop that empties
  a series or removes its genesis tick, or an `offset` that shifts the genesis tick).
- ✓ **Interval defects over membership timelines** (family E: `distort_intervals`) —
  overlap an adjacent interval pair, shrink a closed interval into a coverage gap, or
  invert a closed interval's `joined_sim_time`/`left_sim_time`. `overlap`/`gap` declare
  `beyond-c1-c12`; `left_before_join` declares a genuine `C10` break — the only C10 break
  besides `dangle_reference`'s.
- ✓ **Multi-table class targeting** — a five-way table selector (`table` / `tables` /
  `glob` / `category` / `record_kind`) with exact-or-pattern `columns` entries; one
  operation, one pooled `amount`, over a whole class of tables — emit-portable
  defect profiles.
- ✓ **Biased placement** (`placement`, on the three sampling operations) — weight *which*
  units the draw hits: `entity_scoped` (seeded entity subset), `clustered_temporal`
  (sim-time windows), `correlated` (MNAR cross-column weighting); a seeded weighted draw
  that preserves `amount`'s exactness.

---

## Output formats (writers)

- ✓ **CSV** *(Stage 2)* · ✓ **DuckDB** *(Stage 2)* · ○ **Parquet** *(Stage 3)*.
  Generic relation → file/table serializers; a writer holds no mode or schema
  knowledge. See [`architecture/writers.md`](architecture/writers.md).

## CLI

- ✓ `fabulexa-forge validate` *(Stage 1)* — run C1–C14 against an emit.
- ✓ `fabulexa-forge export` *(Stage 2)* — run an export config against an emit,
  dispatching on `config.mode` to the dimensional, source, or base engine;
  `--fmt csv|duckdb` selects delivery; `--next` / `--from` / `--to` drive
  incremental window-at-a-time export.
- ✓ `fabulexa-forge stream` *(Stage 3)* — replay the base layer as a CDC event stream;
  `--fmt jsonl|debezium`, `--sink stdout|file|kafka` (output directory for `file`;
  `--bootstrap-servers` / `FABEXPORT_KAFKA_BOOTSTRAP` for `kafka`), plus the
  shared `--base-date` / `--timezone` anchor overrides. See
  [`architecture/streaming.md`](architecture/streaming.md).
- ✓ `fabulexa-forge mixer` *(Stage 3)* — replay the base layer as a live, operator-mixable
  Kafka feed: an asyncio app that serves the FabulMixer control API (play / pause /
  re-speed the master transport; lag, rate-limit, or mute each topic mid-run) and reads
  producer-side meters back. Kafka-only; `--fmt jsonl|debezium`, `--bootstrap-servers`,
  `--host` / `--port`, plus launch transport / tick flags; `--consumer` enables the
  consumer instrument (`--window` tumbling window sizes in event-time ms, `--join`
  fact/dimension topic pairings, `--consumer-group` / `--consumer-offset` for the
  Kafka group id and initial offset). Behind a `[mixer]` install
  extra composing `[kafka]`. See
  [`architecture/mixer-control-plane.md`](architecture/mixer-control-plane.md).
- ✓ `fabulexa-forge init` *(Stage 2)* — propose a candidate config from the sidecar;
  `--mode dimensional` (default), `--mode source`, or `--mode streaming`.
- ✓ `fabulexa-forge corrupt` *(Stage 4)* — `fabulexa-forge corrupt <emit_dir> --config <corrupt.yaml>
  --out <out_dir>`: apply a corrupter config, writing the broken `run.duckdb` +
  regenerated `base.json` plus `defects.json` (always written; no suppress flag). See
  [`architecture/corrupters.md`](architecture/corrupters.md).

---

## What this enables

- ○ Teaching datasets — ETL, SCD-2, star schema, dimensional modeling, CDC, streaming.
- ✓ Realistic data-quality corpora — corrupter-injected defects on faithful data, with
  a label-grade defect manifest (`defects.json`) as the answer key.
- ✓ ML feature-store training data — point-in-time reconstruction from `history`
  via `mode: base` + `slice_at: T`, one flat as-of-T row per record.
- ○ Entity-resolution / MDM workloads — multi-observer views (needs a multi-branch
  contract + multi-emit; parked).

---

## Related

| Document | Why |
|---|---|
| [`architecture/README.md`](architecture/README.md) | Design index, package layout, staged roadmap |
| [`../contract/base-format.md`](../contract/base-format.md) | The input contract these features consume |
| [`../CLAUDE.md`](../CLAUDE.md) | Principles, boundary, vocabulary |
