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
3 source/base/streaming · 4 corrupter · 5 queue-state + point-in-time). The Stage-1 reader +
conformance (`fabulexa-forge validate`) and the Stage-2 dimensional exporter (`fabulexa-forge
export` + `init`, CSV + DuckDB) have shipped, along with two cross-mode surfaces —
timestamp rebasing and incremental export, both wired over the dimensional and source
modes. The derivations layer (versioned-intervals + reference-resolution +
row-state-events + membership-events + state-at residents, the single-branch guard)
has shipped, composed by the dimensional, streaming, and source modes. The Stage-3
streaming exporter (`fabulexa-forge stream` — `state-changes` `c`/`u`/`d` CDC and
`membership-events` `join`/`leave` content) and the Stage-3 source exporter
(`mode: source` — the change-log/reference/transaction/junction genre trichotomy,
`change_delivery: snapshot`) have shipped, as has the Stage-4 corrupter family
(`fabulexa-forge corrupt`); the remaining Stage-3 mode (base) and Stage 5
(queue-state + point-in-time export) are planned.

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
- ✓ **Conformance C1–C13** — reimplemented independently of the producer (`fabulexa-forge
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

---

## Exporters

### Modes

Each mode reads the same emit and writes a different target shape.

- ○ **base** *(Stage 3)* — flat single-branch projection. Current-state reconstitution
  from long-form `history`, optional point-in-time slice, timestamp rebasing. *Teaches:
  incremental ETL, SCD merge.* Uses `records__*` + `history`.
- ✓ **dimensional** *(Stage 2)* — star schema. `records__<kind>` → `dim_*` (SCD-2 wide
  via `LEAD`, or Type-1 sub-type split); `history` point/interval and membership-binding
  grains → `fact_*`; typed `prop__` columns read directly (no JSON expansion); FK
  labeled-edge pathfind (reference + membership); type-1 `lookup` record-attribute
  enrichment (slice-value projection, gated to `history_tracked: false`).
  Declarative, domain-agnostic config; CSV + DuckDB output. See
  [`architecture/dimensional.md`](architecture/dimensional.md). *Teaches: data
  warehousing, BI, star-schema design.*
- ✓ **source** *(Stage 3)* — operational OLTP tables: every table classified into a
  change-log, reference, transaction, or junction genre from `record_roles` ×
  `temporal_class`, with no author declaration. A tracked kind (any
  class-`tracked` property) exports as a wide CDC table composing the
  row-state-events fold — the same derivation streaming replays, landed as a table
  instead of a stream; an untracked kind exports as a reference (dimension role) or
  transaction (fact role) table, FKs not joined; a kind whose role varies by
  sub-type splits into one table per declared sub-type. `membership__<K>__<p>`
  tables export as junction (interval) tables. Operational presentation defaults
  (prefix-stripped names, `record_id` → `id`) apply throughout, overridable via
  `exclude`/`rename`; a name collision fails fast. Source is the first mode that
  *requires* a resolved wallclock anchor rather than falling back to raw integers.
  `--next` / `--from` / `--to` compose the cross-mode incremental driver with
  per-genre window membership (junction rows extract-on-change, `left_at`
  horizon-masked); `change_delivery: snapshot` switches every change-log kind to
  periodic full-table snapshots composing a new state-at derivation, for the
  no-CDC source-system archetype. A source export over a corrupted emit surfaces
  the corrupter's declared defects unchanged (test-guarded, never special-cased) —
  the composite `history` table *is* a change log, so the "students build SCD-2
  themselves" pattern lands naturally. CSV + DuckDB output. See
  [`architecture/source.md`](architecture/source.md). *Teaches:
  source-to-warehouse ETL, CDC.*
- ◐ **streaming** *(Stage 3)* — `fabulexa-forge stream` replays the base layer as an ordered
  event stream over two content axes. `state-changes` replays `history` + the records spine
  as a `c`/`u`/`d` CDC stream, one event per record state change; `membership-events` replays
  the `membership__<K>__<p>` interval tables, unpivoting each interval into a `join` (always)
  and a `leave` (when the element left within the slice), keyed on the owner `record_id` —
  an append-only fact log. Serialized to stdout (all topics interleaved in
  global `seq` order), one `<topic>.jsonl` per topic, or one message per event to a Kafka
  broker (`--sink kafka`: topic pre-creation at 1 partition, `record_id` keying, and
  CLI/config/env bootstrap resolution). Two formats: `jsonl` (flat
  `{seq, op, ts, kind, key, after}`) and `debezium` (the Debezium value message —
  `{schema?, payload}` with the configurable masquerade `source` identity and
  epoch-millisecond `ts_ms`; requires a resolved anchor; both content types — `membership-events`
  renders insert-only with the domain op as a leading `event` column). Record-id
  keyed, full-row after-images, anchored timestamps; composes the row-state-events and
  membership-events derivations. A configurable two-layer routing surface partitions the
  stream into topics — a per-content Layer A (`route_table` from the record spine, or from
  the `(owner_kind, property)` membership identity), `types` sub-type selection, a
  `topic_template` + `groups` policy, and a Debezium `table_identity` masquerade (see
  [`architecture/streaming-routing.md`](architecture/streaming-routing.md)). A
  configurable clock paces emission — `realtime × speed` with an idle cap, or
  as-fast-as-possible (`--speed` / `--idle-cap` / `--fast`). *Gaps:* the Debezium
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
- ✓ **`init`** — generate a commented candidate dimensional config by reading the
  sidecar (`record_roles` for warehouse role, kinds, discriminators, membership tables,
  `history_tracked`).
- ✓ **Incremental drip-feed** — window-at-a-time export, wired for both the
  dimensional and source modes: `--next` reads a cursor and emits the next window
  (or `--from`/`--to` runs a stateless range), one calendar period
  (`day`/`week`/`month`, anchor-resolved) or sim-time interval per window.
  Dimensional: append-only facts and SCD-2 version rows (`valid_to` supplied by a
  view, never materialized); full-snapshot type-1 dims. Source: per-genre window
  membership (see the source mode above). Either mode: a growing DuckDB warehouse
  (cursor atomic with data) or one CSV drop directory per window. See
  [`architecture/incremental.md`](architecture/incremental.md). *Teaches: incremental/
  merge ETL, landing zones, building SCD-2 yourself.*
- ✓ **Timestamp rebasing** *(Stage 2)* — map `sim_time` (ns offset) to wallclock
  through the resolved effective anchor: an author-chosen origin (`rebase.base_date` /
  `--base-date`) and zone (`rebase.timezone` / `--timezone`), falling back to the
  sidecar `runtime` anchor. CLI-wins precedence per knob; DST and ambiguous-origin
  rules fail fast. Cross-mode (one anchor per invocation); the dimensional renderer
  consumes it today. See [`architecture/anchor.md`](architecture/anchor.md).
- ○ **Dry-run / combine** — preview without writing; compose multiple emits.

### Queue-state and point-in-time export *(Stage 5)*

Both build on the sanitised subset (one branch, no provenance); neither needs
branch-awareness.

- ○ **Membership / queue export** — `membership__*` → queue-state facts (wait time,
  FIFO/priority order as SQL).
- ○ **Point-in-time reconstruction** — replay `history` to any `sim_time` → ML
  feature-store rows.

### Parked — needs a future contract extension

The sanitised-subset contract mandates exactly one branch and carries no provenance, so
these are out of reach until the contract restores multi-branch / provenance:

- ○ **Branch selection** — export a chosen `fork_path`.
- ○ **Paired-counterfactual export** — aligned datasets across two branches.
- ○ **Provenance lineage columns** — carry `caused_by` / `written_by` into outputs.

---

## Corrupters *(Stage 4)*

Read base, write base. Break **semantic** conformance (C6/C7/C9–C12, and C13's genesis
clause as an unlabeled side effect) while preserving **structural** conformance (C1–C5,
C8, C13's structural clauses); output stays base-shaped so any exporter can run
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
  declares `C6` or `beyond-c1-c12` by mirroring C6's own round-trip predicate against the
  working state.
- ✓ **Interval defects over membership timelines** (family E: `distort_intervals`) —
  overlap an adjacent interval pair, shrink a closed interval into a coverage gap, or
  invert a closed interval's `joined_sim_time`/`left_sim_time`. `overlap`/`gap` declare
  `beyond-c1-c12`; `left_before_join` declares a genuine `C10` break — the only C10 break
  besides `dangle_reference`'s.
- ✓ **Multi-table class targeting** — a five-way table selector (`table` / `tables` /
  `glob` / `category` / `record_kind`) with exact-or-pattern `columns` entries; one
  operation, one pooled `amount`, over a whole class of tables — emit-portable genre
  profiles.
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

- ✓ `fabulexa-forge validate` *(Stage 1)* — run C1–C13 against an emit.
- ✓ `fabulexa-forge export` *(Stage 2)* — run an export config against an emit,
  dispatching on `config.mode` to the dimensional or source engine; `--fmt csv|duckdb`
  selects delivery; `--next` / `--from` / `--to` drive incremental window-at-a-time
  export.
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
- ✓ `fabulexa-forge init` *(Stage 2)* — propose a candidate dimensional config from the sidecar.
- ✓ `fabulexa-forge corrupt` *(Stage 4)* — `fabulexa-forge corrupt <emit_dir> --config <corrupt.yaml>
  --out <out_dir>`: apply a corrupter config, writing the broken `run.duckdb` +
  regenerated `base.json` plus `defects.json` (always written; no suppress flag). See
  [`architecture/corrupters.md`](architecture/corrupters.md).

---

## What this enables

- ○ Teaching datasets — ETL, SCD-2, star schema, dimensional modeling, CDC, streaming.
- ✓ Realistic data-quality corpora — corrupter-injected defects on faithful data, with
  a label-grade defect manifest (`defects.json`) as the answer key.
- ○ ML feature-store training data — point-in-time reconstruction from `history`.
- ○ Entity-resolution / MDM workloads — multi-observer views (needs a multi-branch
  contract + multi-emit; parked).

---

## Related

| Document | Why |
|---|---|
| [`architecture/README.md`](architecture/README.md) | Design index, package layout, staged roadmap |
| [`../contract/base-format.md`](../contract/base-format.md) | The input contract these features consume |
| [`../CLAUDE.md`](../CLAUDE.md) | Principles, boundary, vocabulary |
