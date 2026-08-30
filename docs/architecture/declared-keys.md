# Declared Keys

The sidecar's `presentation_keys` block statically declares the key properties of
every `presentation_id` column — `unique_within` scope, `branch_stable` /
`slice_stable`, a `key_space` identity — so a downstream consumer can know whether
`presentation_id` is safe as a primary key, a merge key, or a join key without
probing data. This doc owns the opt-in **`declare_keys`** capability on the base
and source modes that turns those claims (plus the contract's record-identity
guarantees) into declared key metadata on every compiled output table, which the
DuckDB writer materializes as real `PRIMARY KEY` / `UNIQUE` constraints. Off — the
default — output is byte-identical to a `declare_keys`-less export. Claims flow
exclusively through the reader's strict accessor
([`reader.md`](reader.md) § The presentation-keys registry is strict on read) and
are never validated against data.

**Source:** [`config/models.py`](../../src/fabulexa_forge/config/models.py)
(`BaseConfig.declare_keys`, `SourceConfig.declare_keys`),
[`exporters/base/plan.py`](../../src/fabulexa_forge/exporters/base/plan.py) /
[`exporters/source/plan.py`](../../src/fabulexa_forge/exporters/source/plan.py)
(the per-mode key resolvers),
[`exporters/query_spec.py`](../../src/fabulexa_forge/exporters/query_spec.py)
(`TableKeys`, `QuerySpec.keys`),
[`writers/duckdb.py`](../../src/fabulexa_forge/writers/duckdb.py) (the keyed
creation path). Tests: the per-mode plan and engine suites,
[`tests/writers/test_duckdb.py`](../../tests/writers/test_duckdb.py),
[`tests/exporters/test_query_spec.py`](../../tests/exporters/test_query_spec.py),
[`tests/incremental/test_driver.py`](../../tests/incremental/test_driver.py).

## Boundary

- **In:** the `presentation_keys` claims, read only through
  `Sidecar.presentation_keys()`; the contract's record-identity guarantees
  (`record_id` uniqueness per kind, `record_index` density); the `declare_keys`
  config field. No data read participates in key resolution.
- **Out:** `TableKeys` metadata on each compiled `QuerySpec`; `PRIMARY KEY` /
  `UNIQUE` constraints in DuckDB output DDL; the `keys-not-declarable-csv`
  notice; `PresentationKeysInvalidError` surfacing on the claim-consuming paths.

## Semantics

### Key resolution per output table

Key declaration is resolved at plan time, before any data is written, from the
sidecar alone. The record-identity keys need no claim — `record_id` uniqueness
per kind and `record_index` density are contract guarantees — so they are
declared whenever `declare_keys` is on; `presentation_id` declarations require a
claim. Under the single-branch guard (C8), a `unique_within` of `"branch"` and
`"emit"` are equally table-wide, so both scopes yield a declaration; the
distinction is not surfaced.

| Mode · table | Primary key | Unique | Claim source |
|---|---|---|---|
| base · per-kind flat table | `<kind>_key` (post-`rename` name) | `id`; `presentation_id` iff claimed | Flat kind: `key` entry. Partitioned kind: the rollup's `unique_within` (absent rollup claim → no declaration) |
| source · state table | `id` (the identity column) | `presentation_id` iff claimed | `combined_claim` over the table's **resolved population set** — the registry algebra applied to exactly the populations the table combines. Degenerate cases: a flat kind's table reads the `key` entry; a single-population table its sub-type's `key_for` entry (the entry's presence *is* the claim — declared partitions are total non-NULL); a full-domain table's derivation equals the kind's rollup by the registry's consistency clause. A proper-subset table derives its own combination, so a subset that excludes a colliding sub-type keeps its claim; a derived no-claim combination declares nothing |
| source · junction | none | none | The block speaks only to `presentation_id` on records kinds; membership rows carry no claimed key |
| source · event log | `id` | none | Construction. `(item_type, item_id)` is a polymorphic dereference key spanning kinds, not a per-row key, and the only candidate composite of the rendered columns includes a wallclock `TIMESTAMP` whose microsecond precision can collide distinct nanosecond events — so no honest key exists *among the rendered values*. `id` is the render's own row-number over the log's total order: per-row unique by construction and immune to that truncation ([`source.md`](source.md) § The event log) |

A kind absent from the block (legally — its column never minted, or the block
absent entirely) declares identity keys only. `presentation_id` uniqueness is
declared as a `UNIQUE` constraint, never a primary key: the claims range over
non-NULL cells and SQL `UNIQUE` ignores NULLs — the same semantics — whereas
`PRIMARY KEY` would reject the NULLs a partitioned kind's undeclared sub-types
legitimately carry.

Under a key election ([`key-election.md`](key-election.md)), the declared
primary key follows the **elected identity column** — under base's
`record_index` election that is `<kind>_key`, the id-space column being
dropped — and side `UNIQUE` declarations follow the surviving columns: a
`UNIQUE` whose column the election absorbed or dropped (`record_id` under a
non-`record_id` election, the standalone `presentation_id` once absorbed) is
simply not declared. Election substitutes the column inside the resolution
table above, never widens it — a table that declares no `PRIMARY KEY`
(the event log, junction) still declares none, and populations without an
election resolve exactly per the table. One posture is scoped to the elected
identity column alone: as an *elected identity column*, `presentation_id` is
PK-eligible — its non-NULL table-wide uniqueness is established by the
election's render-time guard, not by a claim — while non-elected declared
claims keep the `UNIQUE`-never-`PRIMARY KEY` posture above.

| Condition | Result |
|---|---|
| `declare_keys` absent or false | No key metadata compiled; output byte-identical to a `declare_keys`-less export |
| `declare_keys: true`, fmt `duckdb` | Constraints in the output DDL |
| `declare_keys: true`, fmt `csv` | Data identical; one `keys-not-declarable-csv` notice per export invocation, before any data is written (a `--next` drip re-emits it each invocation — the compile-notice rule) |
| `declare_keys: true`, block absent | Identity keys declared; no `presentation_id` declarations; no notice (absence is "no claim", not a defect) |
| `declare_keys: true`, block present and incoherent | `PresentationKeysInvalidError` at plan time, before any output |
| `declare_keys: true` over an emit whose data falsifies a declared key (e.g. corrupter-duplicated rows) | The DuckDB load fails loudly naming the table — the author opted into enforcement, and a silent constraint drop would misdescribe the dataset |

`keys-not-declarable-csv` is emitted where `declare_keys` meets a resolved `csv`
format: the mode's full-export entry path, and each incremental driver
invocation. Both already carry the required `notice_sink`; the compiles, the
shared dispatch, and the writers carry none. The compiles are format-agnostic —
which is also why key resolution, and the strict accessor with it, runs whatever
the format: an incoherent block raises at plan time under CSV too.

### The compiled shape and writer materialization

Declared keys travel as [`TableKeys`](../../src/fabulexa_forge/exporters/query_spec.py)
on `QuerySpec.keys` — column names post-`rename` output names; a table with
nothing to declare carries `None`, never an empty `TableKeys`. A compile path
without the capability sets `keys = None`, and a `None`-keyed spec writes exactly
as an unkeyed one, so compatibility holds by construction. The shared dispatch
(`write_query_specs`) flattens `spec.keys` into the DuckDB writer's `keys`
mapping beside the existing name → SQL flattening; its CSV arm ignores keys (the
notice belongs to the export entry path, not the dispatch).

The DuckDB writer materializes a keyed spec as: create the table with explicit
column DDL (names and types transcribed from the materialized Arrow schema — the
writer is schema-ignorant of modes; it transcribes what the relation already
is) plus the declared constraints, then load by insert. A spec without keys keeps
the `CREATE TABLE AS` path byte-for-byte. Row counts, empty-table emission, and
the fresh-output-connection rule are those of the unkeyed path. A constraint
violation during load is a loud `ExportRuntimeError` naming the table. Constraint
names are DuckDB defaults; forge names nothing. The full signature contract is
[`writers/duckdb.py`](../../src/fabulexa_forge/writers/duckdb.py); the shared
materialization boundary is [`writers.md`](writers.md).

### Incremental interplay

Under `declare_keys`, keys are declared at first-window table creation only where
the write regime preserves the constraint across windows: replace-class tables
trivially (each window rewrites the whole table inside one transaction),
append-class tables only where a row lands in exactly one window and is final.
The gating is the windowed compile's — it sets `QuerySpec.keys` per the table
below; the writer consumes, never decides (`write_duckdb_window` reads
`spec.keys` on its create-if-missing path only; constraints created at the first
window persist, and DuckDB enforces them on every later insert).

| Windowed table class | Write regime | Declared |
|---|---|---|
| base per-kind flat table | replace — full state-at snapshot per window | Same as full export |
| source state table | replace — state-at-horizon snapshot per window | Same as full export |
| source event log | append — event rows are final | `PRIMARY KEY (id)`, declared at first-window table creation. The append-class gate is satisfied: an event row lands in exactly one window (`event_sim_time` falls in exactly one half-open window) and is final, and `id` is tape-anchored, so later windows insert strictly higher, never-colliding values |
| source junction | append — a closed interval re-emits | none (as full export) |
| dimensional (type-1, SCD-2, facts) | — | n/a — dimensional carries no `declare_keys` |

A false claim under incremental surfaces as a rolled-back window: the constraint
violation aborts the window's transaction under the windowed writer's atomicity
rule, leaving the warehouse exactly as before. The cursor and fingerprint need no
special handling; `declare_keys` participates in the config fingerprint exactly
as any other config field does.

### The `init` advisory

Dimensional export declares no keys, but dimensional `init` consults the block:
when it carries a whole-table claim for a proposed kind, the kind's stub carries
one advisory comment naming `presentation_id` as the contract-declared natural
key ([`dimensional.md`](dimensional.md) § `init` inference contract). Where the
stub's population instead elects `presentation_id` in `init`'s proposed `keys`
block, the key column sources `from: presentation_id` directly and no advisory
is emitted — the claim is consumed as a key source
([`key-election.md`](key-election.md) § `init` proposals).

## Invariants

1. **Determinism.** Key resolution is a pure function of (sidecar, config); no
   data participates. Same emit + config + code → identical declarations.
2. **Claims are read, never invented.** No declaration exists without a contract
   guarantee (identity keys), a block claim (`presentation_id`), or construction
   (the source event log's `id`). Absence of a claim degrades to absence of a
   declaration, never to probing. Construction is admitted as a third ground
   because it introduces no probing of data to discover uniqueness — the render
   assigns the values and assigns them distinct — which is the practice this
   invariant exists to forbid. It is the strongest of the three: a contract
   guarantee can be falsified by a corrupted emit and a claim can be falsified by
   the data, so both can surface as a loud load failure, whereas a constructed
   key cannot be falsified at all. `TableKeys` records the columns and nothing
   about which ground a declaration stands on.
3. **Declarations never change data.** Under any `declare_keys` value the rows,
   columns, ordering, and typing of every output are identical; only DDL differs.
   (Corollary: the tier-2 playback bridging equivalence is untouched — playback
   compiles relations, not DDL.)
4. **Strictness is use-scoped.** An incoherent block fails exactly the operations
   that consume claims (`declare_keys`, `init`), and no others.

## Validation Rules

`declare_keys` is an optional boolean on `BaseConfig` and `SourceConfig`; absent
means off. It carries no cross-field rule — it composes with `slice_at` and
`exclude` / `rename` (base), the declared `tables` / `events` grammar (source),
and `incremental` without restriction (key resolution runs after renames, on
output names).

| Rule | Checks | Error / notice |
|---|---|---|
| Strict accessor (reader-owned) | The block's six coherence clauses ([`reader.md`](reader.md) § The presentation-keys registry is strict on read) | `PresentationKeysInvalidError` naming kind, sub-type, clause |
| CSV declaration | `declare_keys` on a CSV delivery cannot be materialized | Notice `keys-not-declarable-csv`, once per invocation, before data; emitted by the export entry path / driver invocation, never the compiles, dispatch, or writers |
| Writer keys subset | The writer's `keys` mapping names only compiled tables | `ValueError` (a caller bug, not an author error) |

## Rationale

- **`UNIQUE`, never `PRIMARY KEY`, for `presentation_id`.** The contract's claims
  range over non-NULL cells, and SQL `UNIQUE` has exactly those semantics; a
  primary key would reject the NULLs a partitioned kind's undeclared sub-types
  legitimately carry.
- **The event log declares nothing.** Event grain is multiple-rows-per-item,
  `(item_type, item_id)` is a polymorphic dereference key rather than a per-row
  key, and the only candidate composite key includes a rendered wallclock
  `TIMESTAMP` whose microsecond precision can collide distinct nanosecond
  events — declaring a key that render collisions can falsify would be
  dishonest, so no key is declared post-render.
- **Enforcement is the constraint the author opted into.** The contract makes
  data validation optional and forge declines it everywhere: no mode probes
  `presentation_id` values to confirm a claim. The declared-constraint path is
  the one place a false claim surfaces — as the DuckDB constraint violation —
  and a silent constraint drop would misdescribe the dataset.
- **The notice lives at the entry paths.** The compiles are format-agnostic and
  the writers are mode-ignorant; the full-export entry path and the incremental
  driver invocation are the only layers holding both `declare_keys` and the
  resolved format, and they already carry the required `notice_sink`.
- **Off-by-default.** Declared keys change output DDL; an opt-in keeps every
  existing export — and the test-guarded corrupt→base / corrupt→source
  compositions — byte-identical unless the author asks.

## Boundaries

- **Streaming and Kafka keying are outside the capability.** Message keying is
  `record_id`; the streaming mode reads none of this. `record_id` keying is a
  correctness choice (always present, always stable), not a gap.
- **Dimensional export grammar is untouched.** Authors declare dimensional keys
  themselves; only `init`'s advisory comments consult the block.
- **Within-table keys only — no `FOREIGN KEY` constraints.** Referential
  declarations are a distinct capability with hazards of their own: the
  incremental replace regime rewrites parent snapshots (state tables, base's
  flat tables) under their children's persisted rows each window, and a
  restricted extract (base `exclude`, source omission) legally drops FK
  targets. Deferred until demand appears (Principle #8).
- **No conformance check covers the block's semantic rules.** Conformance is the
  published procedure, reimplemented verbatim — forge does not invent checks of
  its own. Enforcement lives in the strict accessor instead, at the moment
  claims are about to be used ([`conformance.md`](conformance.md)).
- **Claims are never validated against data.** No mode probes `presentation_id`
  values; the opted-into DuckDB constraint is the sole surfacing point.
- **Corrupter composition is verbatim-carry.** The corrupter's base-emit writer
  carries the block through verbatim, as it does every sidecar registry; a
  corruption that falsifies a claim (a duplicated row under a claimed-unique
  key) is deliberate semantic non-conformance the contract itself anticipates.
  Verbatim carry also means a structural corruption — a `schema_drift` renaming
  or dropping a `presentation_id` column — can leave the carried block incoherent
  against the drifted catalog, the same verbatim-staleness posture drift imposes
  on every copied registry; the strict accessor then refuses such an emit exactly
  on the claim-consuming paths, while exports that ignore claims are untouched.
  `defects.json` carries no impact vocabulary for this — its vocabulary is the
  C-set, and no C-ID covers the block, so the staleness is inherently
  manifest-invisible ([`corrupters.md`](corrupters.md)).
- **CSV carries no constraint surface.** Under `declare_keys` + CSV the data is
  identical and a notice records the undeliverable declaration.
- **The record-index key columns are a separate capability.** Base's `<kind>_key`
  / `<p>_key` columns, their derivation, naming, and horizon binding are owned by
  [`base.md`](base.md) § Record-index key columns; the block adds declarations
  *about* columns, never columns.

## Related

| Document | Why |
|---|---|
| [`reader.md`](reader.md) | The strict `presentation_keys` accessor and the union-safety algebra every claim flows through |
| [`base.md`](base.md) · [`source.md`](source.md) | The two modes carrying `declare_keys`, and the output tables the resolution rules range over |
| [`writers.md`](writers.md) | The DuckDB materialization boundary the keyed creation path extends |
| [`incremental.md`](incremental.md) | The windowed driver whose write regimes gate per-window declaration |
| [`dimensional.md`](dimensional.md) | `init`'s natural-key advisory — the one dimensional consumer of the block |
| [`key-election.md`](key-election.md) | The key-election surface — the declared primary key follows the elected identity column; PK-eligibility of elected `presentation_id` |
| [`notices.md`](notices.md) | The channel `keys-not-declarable-csv` flows through |
| [`corrupters.md`](corrupters.md) | The verbatim-carry composition and the falsified-claim surfacing |
| [`../../contract/base-format.md`](../../contract/base-format.md) | The `presentation_keys` block, its consistency rules, and the normative union-safety algebra |
