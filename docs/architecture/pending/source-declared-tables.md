---
status: draft
---

# Source Declared Tables

The source exporter redesigned around author-declared output tables: a shared
declared-table grammar (populations → named tables), the OLTP app-database
render vocabulary (`state` thing-tables, `junction` association tables, one
polymorphic event log), the genre trichotomy deleted, and `init --mode source`
as the proposal engine. Key election's source-mode identity gates anchor on
declared tables.

---

## Problem

Source mode's output layout is decided by classification, not by the author.
The genre trichotomy maps sidecar facts (`record_roles` × `temporal_class`) to
table layout: a tracked kind is forced into one wide CDC table, an untracked
object-registry kind is force-split per sub-type, and no lever exists to
override either. Four failures:

1. **The sidecar's vocabulary is simulation machinery, not output
   vocabulary.** `actor` / `entity` / `resource` is state-machine taxonomy
   (decides / doesn't decide / seizable). Defining output shape is this
   package's entire job; classification outsources it to the producer's
   internals. There is no way to say "give me `trips`, `riders`, `zones`,
   `drivers`, and one `versions` log" — the author gets classification's
   verdict, whatever it is.
2. **No author lever over layout.** An author cannot combine two sub-types
   into one table, split a tracked kind, or choose which tables exist. Under
   key election's one-table-one-identity-surface gate this is a hard trap: a
   tracked sub-typed kind with partial `presentation_keys` coverage spans
   populations whose elections differ, and its forced single table can never
   elect `presentation_id` — the only legal elections are `record_index`
   for every population, or none.
3. **The flagship render is in the wrong mode.** A per-kind wide CDC table is
   an *extraction* artifact — what a CDC tool produces *from* an app database.
   That is streaming's charter. A real application's schema contains thing
   tables and an audit log, not per-thing CDC dumps; likewise the
   reference/transaction genre split is a label with no schema difference —
   two genres that exist only because classification needed somewhere to put
   the role bit.
4. **Zero-config is the wrong bar.** Source has no `init`; its implicit
   behavior *is* its config, invisible and uneditable. The right bar is
   `init --mode source` then zero edits.

## Solution

Two coupled moves.

**A declared-table grammar.** A source config declares every output table: its
author-verbatim name, its source populations, its columns. Populations are the
sub-type atoms — `(kind, sub_type)` where the sidecar declares a discriminator
domain, degenerating to `(kind)` otherwise; combination is explicit, named,
and same-kind-only for thing tables. Sidecar facts gate what a declaration may
ask for (does the kind exist, is the sub-type declared, is the surface
electable) — they never decide layout. Bundle vocabulary reaches output
through exactly one door: `init` proposals. A mode is a render vocabulary plus
a feature-admission profile over this grammar plus delivery constraints;
source's admission profile deliberately excludes the warehouse features
(`lookup`, FK pathfind, SCD windows) — a well-architected app is consistently
normalized.

**Source re-chartered as the well-architected app database.** The rule:
*things get tables; events get the log.*

```yaml
mode: source
keys:
  trip: presentation_id
source:
  tables:
    - name: trips
      kind: trip
    - name: customers
      kind: customer
      sub_types: [standard, vip]
    - name: vip_customers          # populations may land in several tables
      kind: customer
      sub_types: [vip]
    - name: trip_drivers
      membership: {kind: trip, property: drivers}
  events:
    name: versions
    sources:
      - kind: trip
        only: [status, fare]
      - kind: customer
      - membership: {kind: trip, property: drivers}
```

| Render | Declared by | Shape |
|---|---|---|
| `state` | a `tables` entry with `kind` | One current row per record: payload at current value, `created_at` / `updated_at`, soft-delete lifecycle (`active`, `deactivated_at`) |
| `junction` | a `tables` entry with `membership` | One association row per membership interval: owner id, member fields, `joined_at` / `left_at` (NULL while open — the soft-delete idiom) |
| event log | the single `events` block | One polymorphic audit table, event grain: `item_type`, `item_id`, `event`, `occurred_at`, `changes` (serialized JSON changeset) |

The render is determined by the declaration's source shape — a records
population has exactly one thing-render (`state`), a membership table exactly
one (`junction`) — so no `render` field exists; one appears only when a
second render for the same source shape ships.

Deleted outright: the genre trichotomy, the per-kind change-log render, the
reference/transaction genre labels, global `change_delivery`, global
`exclude` / `rename` (exclusion is now omission from the declaration; naming
is per-table). Classification logic demotes to `init --mode source`, which
proposes exactly one state table per kind, one junction per membership table,
and an event-log stub covering every kind with tracked history (lifecycle-only
kinds and membership sources appended as comments) — the author edits. A
source config declaring no output — no tables, no events block — is a
load-time error: there is no implicit layout left to fall back to. Either
side stands alone: tables without a log (a Type-1-only app), or the log
without tables (an audit-stream-only extract).

## Affected Subsystems

- **Config** — `SourceConfig` is rebuilt: `tables` (declared-table list) and
  `events` (the log declaration) replace `change_delivery` / `exclude` /
  `rename`. `mode: source` now *requires* its section (the bare
  zero-config dump is gone). The cross-mode `keys`, `rebase`, `incremental`,
  and `declare_keys` blocks compose unchanged.
- **Shared exporter layer** — gains the population-set resolver (config
  population address → resolved sub-type atoms, presence-driven from the
  sidecar). Key election's identity gates (uniformity, union safety) now
  anchor on author-declared tables in source; the election resolution and the
  gates themselves are unchanged — they check tables the author controls
  instead of tables classification invented.
- **Source mode** — plan and renders rebuilt: the plan resolves declared
  tables (not classified genres); the `state` render is the faithful
  records read (discriminator-filtered per declared populations); the
  `junction` render is carried over; the event-log render is new, composing
  the row-state-events and membership-events folds with a per-record
  previous-after-image diff and deterministic JSON serialization in SQL.
  Source stops consuming `record_roles` entirely.
- **CLI / `init`** — `init` gains `--mode source` (a mode selector with
  `dimensional` as the shipped default), emitting a commented candidate
  source config. The source proposal engine consumes kinds, discriminator
  domains, membership tables, and per-column temporal classes — not
  `record_roles`.
- **Incremental driver** — source window membership re-keys on the declared
  render: `state` becomes the horizon-snapshot class (per-window state-at
  reconstruction), the event log the append class, `junction` keeps
  extract-on-change. The driver's shared mechanics are untouched.
- **Streaming** — no behavior change; it becomes the sole owner of the CDC
  extraction archetype. An author wanting CDC-shaped output uses `stream`,
  not `export --mode source`.
- **Key election** — its source-mode semantics re-base onto
  declared tables: the identity gates run per declared table; the
  per-population-tables escape for a mixed-election kind is now expressible;
  the change-log post-fold identity join is consumed by the event log's
  `item_id` instead. The election grammar, resolution, and gate definitions
  are unchanged.

## What Doesn't Change

- **The reader and the derivations layer.** No new derivation resident: the
  event log composes the existing row-state-events and membership-events
  folds; the changeset diff and JSON serialization are source render
  concerns, single-consumer, and live in the mode.
- **The writers.** The `changes` column is plain `VARCHAR` JSON text rendered
  in the SELECT, so writers serialize it as any other column — no writer
  extension, no JSON column type.
- **Dimensional, base, streaming behavior.** Dimensional's config grammar,
  YAML surface, and behavior are untouched by this design. The shared
  population resolver's only consumer is source mode; it lives in the shared
  layer because it resolves the same population atoms key election addresses,
  while election resolution keeps its own gates.
- **The anchor contract.** Source still requires a resolved
  `EffectiveAnchor` (`SourceAnchorRequired`), same precedence and DST rules.
- **The `slice_only` policy.** Omit-with-notice on auto-projected surfaces;
  a declaration naming a non-exempt `slice_only` column is refused.
- **Operational presentation defaults for columns.** Prefix-stripped names,
  `record_id` → `id`, the lifecycle map (`created_at` / `updated_at` /
  `active` / `deactivated_at`), wallclock rendering through the shared
  anchor renderer, native payload types, the reserved
  `last_mutation_sim_time` output name, the collision fail-fast.
- **Determinism, faithful reshaping, corrupt→source composition,** the
  notice channel, the single-branch guard, the trunk-only `fork_path` drop.
- **`declare_keys`** — opt-in, off by default; the per-render resolution
  changes with the render set (below), the capability itself doesn't.

## Semantics

### Populations and declared tables

A `tables` entry addresses populations of exactly one kind (tabular
combination is same-kind-only — column shape forces it):

| Declaration | Population set |
|---|---|
| `kind: K` (flat kind) | `(K)` — the whole kind |
| `kind: K` (sub-typed kind) | Every declared sub-type of `K` — shorthand for the full discriminator domain |
| `kind: K, sub_types: [a, b]` | `(K, a)` and `(K, b)` |
| `kind: K, sub_types: […]` where `K` is flat | Error `SourceSubTypesOnFlatKind` — a flat kind has no populations to address |
| `membership: {kind: K, property: p}` | The one `membership__<K>__<p>` table |

| Condition | Result |
|---|---|
| A declared kind has no `records__<kind>` table in the sidecar | Error `SourceTableKindUnknown` |
| A declared sub-type is not in the kind's discriminator domain | Error `SourceTableSubTypeUnknown` |
| A declared membership reference resolves to no sidecar table | Error `SourceTableMembershipUnknown` |
| A declared population materializes zero rows | Its table (or its share of a combined table) is emitted empty — declared intent, not observed rows, drives table existence |
| A kind (or sub-type) appears in no declaration | It is not exported. Omission is the exclusion mechanism; references *to* an undeclared kind from declared tables remain ordinary reference columns, rendered in the target population's elected surface as anywhere (an undeclared kind may still carry an election — the key-election exclusion posture) — a restricted extract, documented, not an error |
| Two declared tables share a population | Legal — both render it (the dimensional posture for overlapping dims) |
| Two output tables resolve one name, or two columns of one table resolve one name | Error `SourceNameCollision` — never a silent suffix or drop |

Table names are author-verbatim (`name` is required); there are no default
table names to derive. `init` proposes `<kind>` and `<K>_<p>` verbatim from
sidecar identity.

### The `state` render

The faithful records relation — the reader's records builder,
discriminator-filtered to the table's declared populations. One row per
record, current state. The column set is classified through the reader's
records-column taxonomy, never enumerated:

| Base column | Output (default) |
|---|---|
| `record_id` | `id` — or the table's elected surface under key election |
| `presentation_id` | Kept unprefixed, producer-typed — unless it *is* the elected identity, in which case it renders as the identity column and is not duplicated |
| `created_sim_time` | `created_at`, wallclock |
| `last_mutation_sim_time` | `updated_at`, wallclock |
| `active` / `deactivated_at` | Verbatim / wallclock — the soft-delete pair |
| `prop__<p>` | `<p>`, native type; reference properties render the target population's elected surface per row |
| `prop__<K>_type` (discriminator) | Retained as `<K>_type` when the table spans ≥ 2 populations; dropped when the table's population set is a single sub-type (constant — table identity carries it). Explicitly listing it in `columns` retains it either way |
| `fork_path`, `record_index`, `ref_index__<name>` | Dropped — mechanism columns no operational system carries. `fork_path` / `ref_index__*` are never addressable; `record_index` renders only as a table's elected identity, never as payload (its one addressable use is the identity rename key, below) |
| Non-exempt `slice_only` columns | Omitted with a `slice-only-column-omitted` notice |
| A records column with no taxonomy role | Error `SourceUnclassifiedColumn` |

Per-table `columns` (optional) selects *which* source columns project — a
subset of the taxonomy's projectable set plus the discriminator; the taxonomy
still decides *representation*. The identity column is outside `columns`'
reach: it always projects (a thing-table without identity is not a
thing-table), and a `columns` entry naming the table's **elected** surface is
refused (`SourceColumnNotAddressable` — identity is election-governed, not
selection-governed). A *non-elected* surface name resolves by its own rule:
`presentation_id` when not elected is an ordinary selectable column (below);
`record_id` under a non-`record_id` election is a surface the election
leaves unrendered — `SourceColumnUnresolved`, the message naming the
election (the rename posture below); `record_index` when not elected is a
mechanism column — `SourceColumnNotAddressable`. Everything else — `presentation_id` (when not elected),
the lifecycle columns, payload properties, the discriminator — is selectable
and omittable. Per-table `rename` (optional) overrides the default output
name of any projected column, keyed on the source column name (never a
derived output name, so a default-name collision is always resolvable). The
**identity column's** rename key is the elected surface's contract column
name (`record_id` / `record_index` / `presentation_id` — the key-election
posture); a key naming a surface the election leaves unrendered (`record_id`
under a `presentation_id` election) is unsatisfiable and errors
(`SourceColumnUnresolved`). Absent `columns`, the full classified set
projects.

### The `junction` render

Carried over unchanged in shape: one row per membership interval —
`<K>_id` (owner, elected surface), `joined_at` / `left_at` wallclock
(`left_at` NULL while open — faithful, never fabricated), `elem__<f>` →
`<f>` native, `member__<f>__kind` / `__id` → `<f>_kind` / `<f>_id` with the
member id rendering the target population's elected surface per row.
Per-table `columns` / `rename` apply to the membership surface the same way:
the owner column always projects and is addressed by its source name
`record_id` (it always renders, whatever surface it carries); the interval
columns, element fields, and member fields are selectable and omittable.
The member pair's two columns (`member__<f>__kind` / `member__<f>__id`)
select and rename independently by their own source names — keeping
`<f>_id` while omitting `<f>_kind` is a legal restricted extract (per-row
election resolution consults the kind internally regardless); the pair is
atomic only inside the event log's `changes` expansion.

### The event log

One declared polymorphic audit table at event grain — the app's own history
idiom. Fixed columns; the author names the table, not the columns:

| Column | Content |
|---|---|
| `item_type` | The population's contract identity: the kind name for records sources, `<K>.<property>` for membership sources. Sidecar-derived, independent of which thing-tables are declared |
| `item_id` | For a records source: the record's identity in its own population's elected surface (`record_id` verbatim absent an election); on destroy rows the value comes from the identity join relation, not the fold's nulled after-image, so it is never NULL. For a membership source: the **owner** record's identity in the owner kind's election — the junction-owner-column render, per-row resolved for a sub-typed owner. Column type per the junction-member-column rule over the union of every source's resolved surfaces: the common declared type when all agree, else `VARCHAR` with `record_index` digit-rendered |
| `event` | `create` / `update` / `destroy` — deterministic recode of the folds' ops (`c`/`u`/`d`; `join` → `create`, `leave` → `destroy` of a membership in the named collection, recorded against the owner — `item_type` is what separates collection changes from the owner's own lifecycle rows) |
| `occurred_at` | Wallclock `TIMESTAMP` through the anchor renderer |
| `changes` | Serialized JSON text (codec `VARCHAR`): an object mapping audited property bare names → `[old, new]` pairs — a membership reference field expands in place to its `<f>_kind` / `<f>_id` entry pair (the junction render's names, kind then id; `only` / `ignore` still address the bare field name). Keys in sidecar column-declaration order; values are the folds' `CAST(… AS VARCHAR)` after-image strings verbatim or `null` — the row-state-events / membership-events rendering, the same strings streaming's payloads carry, never the conformance codec — reference-valued entries in the target's elected surface (below). The JSON assembly (object construction, string escaping) is new mode-owned SQL, rendered deterministically in the SELECT |

The audited property set per source: every `tracked` and `constant`-class
property of the kind (the temporally honest set — `slice_only` is
policy-omitted), narrowed by `only` or widened-by-subtraction via `ignore`
(mutually exclusive). The folds' selected-property set *is* the audited set,
`history_tracked` or not: a tracked-flagged property reads as-of each event
from its history rows; an untracked `constant`-class property renders the
current spine value in every after-image (the folds' type-1 path) —
temporally honest precisely because `constant` means current equals genesis.
Selecting by `history_tracked` instead of by class would silently drop
untracked constants from `create` / `destroy` changesets. For a membership
source, the element-schema fields.
The discriminator is in the set: a `constant`-class `prop__<K>_type` as any
constant property, and the exempt sub-typed discriminator despite a
`slice_only` class (the export-wide carve-out — exemption from the policy,
not omission from the set); addressable by its bare name `<K>_type` under
`only` / `ignore`, and — creation-constant — it appears in `create` /
`destroy` changesets and never spawns an `update`. Each policy-omitted
`slice_only` property emits one `slice-only-column-omitted` notice per
events source — the mode's omit-with-notice posture on auto-projected
surfaces.

| Event | `changes` content |
|---|---|
| `create` | Every audited property: `[null, value]` from the `c` after-image |
| `update` | Exactly the audited properties whose after-image value differs from the record's previous event's after-image: `[old, new]`. Coincident changes coalesce into one event (the fold's per-`(record, sim_time)` grain) |
| `update` where no *audited* property changed | The event row is suppressed — an audit log records what it tracks, nothing else |
| `destroy` | Every audited property: `[last value, null]` (old values from the preceding after-image) |
| `create` / `destroy` with an empty audited set | Emitted with `changes = {}` — the lifecycle event is itself information |
| membership `create` (join) | Every selected field: `[null, value]` |
| membership `destroy` (leave) | Every selected field: `[value, null]` — the leave carries what left |

Old values derive from the previous after-image per record (a lag over the
fold's own output, audited subset) — a deterministic reshape of fold values,
nothing recomputed from base state.

**Elected rendering inside `changes`.** An audited reference-valued property
renders its old/new values in the target population's elected surface — the
every-referencing-column rule applied to the diff — and a membership
reference field's `<f>_id` entry renders the member population's election
(`<f>_kind` is the qualifier, as in the junction render). Elected surfaces
are creation-constant, so the translation is one fan-out-free identity join
per referenced kind, horizon-free, applied before the lag (lag-then-translate
and translate-then-lag agree); a mixed-election target resolves per row
through the records-spine discriminator; `NULL` stays `NULL`. The edge
union-safety gate applies to every audited reference property exactly as to
a tabular reference column, and the uniqueness guard ranges over these
composed relations as over any the export composes. No type rule is needed:
`changes` values are `VARCHAR` strings, so a digit-rendered `record_index` is
just another string.

**Identity semantics.** `(item_type, item_id)` names the **audited item** —
the polymorphic-reference idiom: the record for a records source, the
owner's collection for a membership source. It is a dereference key, not a
per-row key: an owner's collection logs one `create` per joining member
under the same pair, the association recovered from `changes`. `item_id` is
a kind-targeted edge render — structurally the junction member column, with
`item_type` as its qualifier: no identity-uniformity gate applies (it is not
a thing-table identity column), and no gate of any kind applies **across**
item-types — `item_type` makes cross-type collision structurally irrelevant,
exactly as `<f>_kind` does. **Per item-type** the edge union-safety gate runs
over the resolved surfaces of the **union of every source resolving to that
item-type's addressed populations** (a records item-type: the union of its
kind's sources' populations; a membership item-type: the owner kind's
populations — the junction-owner-column gate). The gate's granularity follows
the dereference key, not the declaration list: two sources addressing
disjoint sub-types of one kind share an item-type, so their elections must be
union-safe *jointly* — two bare-counter siblings both electing
`presentation_id` are refused whether declared in one source or split across
two (`ElectionUnionUnsafe`, naming the item-type). A mixed election within
one item-type is legal exactly when union-safe. The log declares no keys
under `declare_keys`.

**Cross-kind legality.** The log is the one tabular output spanning kinds.
This does not breach same-kind-only tabular combination: that rule governs
*thing*-rows sharing a column shape; the log's columns are event-shaped and
`item_type`-qualified — the same reason a stream may interleave kinds on one
topic. A delivery lane for events, not a population table.

**Sources.** Each `events` source addresses populations exactly as a
`tables` entry does (whole kind, `sub_types` subset, or membership) and
resolves with the same errors. A `sub_types` subset narrows the fold's rows
per record through the records-spine discriminator (per-row population
resolution — the discriminator is creation-constant, so the filter is
temporally honest at every event time), the same device the `state` render's
population filter composes. Sources resolve to **pairwise-disjoint**
population sets (membership sources distinct by `(kind, property)`) — one
audit stream per population, so no event is double-logged and the total
order stays tie-free; an overlap is `SourceEventSourceOverlap` at plan
time. A kind may be audited without having a
declared state table and vice versa — the log is its own declaration.
Absent `events` block: no log, and the emit's history is dropped from the
export — legal, author-declared dropping (a Type-1-only app), never an
error.

### Identity and key election

| Rule | Behavior |
|---|---|
| State-table identity | The identity column renders the elected surface of the table's populations. The uniformity gate requires every population combined into the table to elect one surface; union safety applies under a uniform `presentation_id` election. Both run at plan time over *declared* tables |
| Mixed-election kind | Legal — declare per-population tables; each table's populations elect uniformly. The escape the trichotomy structurally denied |
| Reference / junction-member columns | Render the target population's elected surface per row (kind-targeted mode semantics; the edge union-safety gate unchanged) |
| Event log `item_id` / `changes` | Kind-targeted edge renders: the edge gate runs per item-type over the union of its sources' addressed populations, and per audited reference property over its target — no gate across item-types (§ The event log) |
| `declare_keys` | State tables declare the identity-column primary key; `presentation_id` uniqueness follows `combined_claim` over the table's **resolved population set** — the registry algebra applied to exactly the populations the table combines. The rule's degenerate cases: a flat kind's table reads the `key` entry, a single-population table its sub-type entry (`key_for` presence — the entry's presence is the claim), and a full-domain table's derivation equals the kind's rollup by the registry's consistency clause; a proper-subset table derives its own combination, so a subset that excludes the colliding sub-type keeps its claim. A derived no-claim combination declares nothing (an incoherent block has already raised `PresentationKeysInvalidError` at the strict accessor — there is no quiet missing-rollup state). Junction and event-log tables declare nothing |

### Ordering and determinism

The exporter remains a pure function of `(emit, config, code version)`.
Total orders over raw sim-time and identity, never rendered timestamps:

| Render | Total order |
|---|---|
| `state` | `(created_sim_time, record_id)` |
| `junction` | `(record_id, joined_sim_time, field columns in element-schema declaration order, VARCHAR-compared, NULLS FIRST)` |
| event log | `(event_sim_time, item_type, event_class, record_id, membership fields in element-schema declaration order, VARCHAR-compared, NULLS FIRST)` — the folds' raw keys (`event_class` is the folds' own ordering ordinal) with `item_type` interposed to disambiguate across sources |

### Incremental composition

Window membership re-keys on the declared render; the driver's shared
mechanics (cursor, fingerprint, drained detection, labels, staging,
empty-window emission) are untouched. Half-open `[start_ns, end_ns)` on raw
ns throughout.

| Render | Window key | Behavior per window |
|---|---|---|
| `state` | — (snapshot class) | One full-table snapshot per window, reconstructed at the window horizon through the state-at derivation: rows with `created_sim_time < end_ns`; tracked properties as-of the horizon; `constant` properties current (the declared temporal-honesty exception); lifecycle horizon-rendered (`active` / `deactivated_at`); **no `updated_at`** — `last_mutation_sim_time` at a past horizon is not faithfully reconstructible, so the column is omitted rather than fabricated. `replace` in DuckDB, re-emitted per CSV drop |
| event log | `event_sim_time` | Append event rows with key ∈ window, computed over the full fold — the `changes` lag's previous after-image may predate the window; window membership selects rows, never alters their content (events are immutable and final) |
| `junction` | activity (`joined_sim_time`, `left_sim_time`) | Extract-on-change, `left_at` horizon-masked — carried over unchanged |

A full (non-incremental) export renders `state` as the current records read
*with* `updated_at`; the windowed shape differs by exactly that one omitted
column, a documented consequence of horizon honesty. An explicit `columns` /
`rename` entry naming `last_mutation_sim_time` is therefore unsatisfiable
under a windowed invocation and errors (`SourceColumnUnresolved`, the
message naming the horizon-honesty omission) — never a silent drop. The
refusal is plan-time: windowed-ness is an invocation fact, so the caller
passes it to `build_source_plan` (`windowed`), which validates every
declaration against the shape this invocation actually delivers. The incremental estate
is thereby the real-world archetype whole: nightly full extracts of app
tables plus an appended audit log plus upsert-shaped junction activity —
the no-CDC teaching shape `change_delivery: snapshot` used to approximate,
now the default and only behavior.

### `init --mode source` inference contract

A pure function of `(emit, code version)`; emits a commented candidate
config the author edits. Consumes kinds, discriminator domains, membership
tables, per-column temporal classes, and the `presentation_keys` registry —
**not** `record_roles`. An emit predating per-column `history_tracked`
flags is refused (`SourceHistoryTrackedRequired`) — a candidate config
that cannot export is not proposed. Proposal order follows the sidecar's table
declaration order. Proposed names are verbatim; when two proposals resolve
one name (underscore-bearing identifiers), the later proposal (sidecar
declaration order) is emitted commented-out with a comment naming the
collision — the emitted config always parses and plans clean, the
key-election `init` self-gating posture.

| Emit condition | Proposal |
|---|---|
| Each `records__<kind>` table | One state table: `name: <kind>`, `kind: <kind>`. For a sub-typed kind, one combined table (the STI shape) with a comment enumerating the declared sub-types and showing the per-sub-type split alternative |
| Each `membership__<K>__<p>` table | One junction table: `name: <K>_<p>` |
| ≥ 1 kind with a class-`tracked` property | One `events` stub named `versions`, one active source entry per such kind; membership sources and lifecycle-only kinds (no tracked property — spine `create` / `destroy` only) appended as commented-out source entries |
| No kind carries a tracked property | The `events` stub is emitted fully commented out (name and every per-kind source), under a comment noting the emit's auditable history is lifecycle-only — spine `create` / `destroy` events; uncommenting opts in |
| The registry declares a population | The `keys` proposal per the key-election `init` contract, aligned with the declared tables |
| Non-exempt `slice_only` columns | Never proposed; one `slice-only-column-omitted` notice each |

## Configuration

```yaml
mode: source
keys:                                # cross-mode key election (unchanged grammar)
  trip: presentation_id
source:
  tables:
    - name: trips
      kind: trip
      columns: [prop__status, prop__fare, prop__rider, created_sim_time]
      rename: {prop__fare: fare_usd}
    - name: customers
      kind: customer
      sub_types: [standard, vip]
    - name: trip_drivers
      membership: {kind: trip, property: drivers}
  events:
    name: versions
    sources:
      - kind: trip
        only: [status, fare]
      - membership: {kind: trip, property: drivers}
  declare_keys: true
```

| Field | Type | Required | Description |
|---|---|---|---|
| `source.tables` | list | No (≥ 1 entry when present) | The declared output tables; at least one of `tables` / `events` must be declared |
| `tables[].name` | str | Yes | Author-verbatim output table name |
| `tables[].kind` | str | Exactly one of `kind` / `membership` | Records-population source |
| `tables[].sub_types` | list[str] | No (only with `kind`) | Explicit population subset; absent = every declared sub-type |
| `tables[].membership` | `{kind, property}` | Exactly one of `kind` / `membership` | Membership-table source |
| `tables[].columns` | list[str] | No | Source-column selection; absent = full classified projection |
| `tables[].rename` | map[str, str] | No | Source column name → output name overrides |
| `source.events` | block | No | The event log; absent = no history exported |
| `events.name` | str | Yes (when block present) | The log's output table name |
| `events.sources` | list | Yes (≥ 1) | Audited populations — same addressing as `tables[]` sources; pairwise-disjoint population sets |
| `events.sources[].only` / `.ignore` | list[str] | No (mutually exclusive) | Audited-property subset by bare property name (element-field name for membership) |
| `source.declare_keys` | bool | No (default false) | Unchanged opt-in |

Removed fields: `source.change_delivery`, `source.exclude`, `source.rename`
(the global block; `rename` is now per-table).

## Interface Contracts

### Config Models

```python
class MembershipRef(StrictBaseModel):
    """Addresses one membership table by its contract identity."""
    kind: str
    property: str


class SourceTableDecl(StrictBaseModel):
    """One declared output table: a name, one population source, optional
    column selection and renames. Exactly one of `kind` / `membership` is
    set; `sub_types` only accompanies `kind`."""
    name: str
    kind: str | None = None
    sub_types: tuple[str, ...] | None = None
    membership: MembershipRef | None = None
    columns: tuple[str, ...] | None = None
    rename: dict[str, str] | None = None


class SourceEventSourceDecl(StrictBaseModel):
    """One audited population set for the event log. Same source addressing
    as SourceTableDecl; `only` / `ignore` are mutually exclusive audited-
    property filters (bare property names; element-field names for a
    membership source)."""
    kind: str | None = None
    sub_types: tuple[str, ...] | None = None
    membership: MembershipRef | None = None
    only: tuple[str, ...] | None = None
    ignore: tuple[str, ...] | None = None


class SourceEventsDecl(StrictBaseModel):
    """The single polymorphic event log declaration."""
    name: str
    sources: tuple[SourceEventSourceDecl, ...]


class SourceConfig(StrictBaseModel):
    """mode: source section — the declared app-database shape. `tables`
    defaults empty (a log-only config is legal); at least one of `tables` /
    `events` must declare output (validated)."""
    tables: tuple[SourceTableDecl, ...] = ()
    events: SourceEventsDecl | None = None
    declare_keys: bool = False
```

### Runtime Types (shared exporter layer)

```python
@dataclass(frozen=True)
class Population:
    """One sub-type atom: (kind, sub_type), sub_type None for a flat kind.
    The unit the declared-table grammar resolves to — the same atom key
    election addresses. Election resolution's richer ElectedPopulation
    (the atom plus its resolved surface and key space) is unchanged; it is
    not refactored over this type."""
    kind: str
    sub_type: str | None
```

### Functions (shared exporter layer)

```python
def resolve_populations(
    sidecar: Sidecar,
    owner: str,
    kind: str,
    sub_types: tuple[str, ...] | None,
) -> tuple[Population, ...]:
    """
    Resolve a config population address to its sub-type atoms.

    Presence-driven from the sidecar: a kind with a declared discriminator
    domain refines to per-sub-type atoms; a flat kind resolves to the single
    (kind, None) atom. `sub_types` selects an explicit subset of the
    declared domain, in declaration order.

    The Source-prefixed errors surface only on declaration resolution —
    election resolution keeps its own resolution gates (ElectionKindUnknown
    / ElectionSubTypeUnknown) and is not rerouted through this function's
    error surface.

    Args:
        sidecar: The open emit's typed sidecar.
        owner: The declaring unit's message label, used verbatim as the
            error-message prefix — "table '<name>'" for a tables entry,
            "events source #<n>" (1-based, declaration order) for an
            events source.
        kind: A records kind name.
        sub_types: Explicit sub-type subset, or None for the full set.

    Returns:
        The resolved atoms, discriminator-domain declaration order.

    Raises:
        SourceTableKindUnknown: `kind` has no records table in the sidecar.
        SourceTableSubTypeUnknown: an entry is outside the kind's
            discriminator domain.
        SourceSubTypesOnFlatKind: `sub_types` given for a kind with no
            discriminator domain.
    """
```

### Functions (source mode)

```python
def build_source_plan(
    emit: Emit,
    config: ExportConfig,
    anchor: EffectiveAnchor,
    election: Election,
    windowed: bool,
    notices: NoticeSink,
) -> SourcePlan:
    """
    Resolve the declared tables and event log against the open emit.

    Resolves every declaration to populations, classifies every projected
    column through the records-column taxonomy, resolves column selection /
    renames, runs the identity gates (uniformity, union safety) per declared
    table over the election view and the edge gates per referencing column,
    per event-log item-type, and per audited reference property, resolves
    the audited property set per events source, and runs the collision and
    reserved-name checks over all resolved output names. Validation is
    against the shape the invocation delivers: under `windowed=True` the
    state render omits `updated_at`, so a `columns` / `rename` entry naming
    `last_mutation_sim_time` is unsatisfiable and refused.

    Args:
        emit: The open emit.
        config: The full export config (mode: source).
        anchor: The resolved wallclock anchor (source requires one; the
            caller has already refused a None resolution).
        election: The resolved key-election view.
        windowed: Whether the invocation is windowed (`--next` /
            `--from`/`--to`) — an invocation fact, supplied by the caller,
            selecting which state-render shape the plan validates against.
        notices: Sink for slice_only omissions and other compile notices.

    Returns:
        The resolved plan: one unit per declared table plus the event-log
        unit when declared.

    Raises:
        SourceTableKindUnknown, SourceTableSubTypeUnknown,
        SourceTableMembershipUnknown, SourceSubTypesOnFlatKind:
            declaration does not resolve in the sidecar.
        SourceColumnUnresolved: a `columns` / `rename` / `only` / `ignore`
            entry names no column / property of its source surface.
        SourceColumnNotAddressable: a `columns` / `rename` entry names a
            mechanism column (fork_path, record_index, ref_index__*).
        SourceSliceOnlyRead: a `columns` / `rename` / `only` / `ignore`
            entry names a non-exempt slice_only column.
        SourceEventSourceOverlap: two events sources resolve overlapping
            population sets.
        SourceUnclassifiedColumn: a projected records column resolves no
            taxonomy role.
        SourceNameCollision: duplicate output table or column names.
        ElectionMixedIdentity, ElectionUnionUnsafe: the identity gates
            per declared table; the edge gates per referencing column and
            per event-log item-type.
        SourceHistoryTrackedRequired: the sidecar predates per-column
            history_tracked flags.
        TemporalClassUnavailableError: a consulted flagged column declares
            no in-enum temporal_class (reader-owned, C13).
        ExportError: reserved output-name violations; the single-branch
            guard (require_single_branch).
    """


def build_source_query_specs(
    plan: SourcePlan,
    window: Window | None,
) -> tuple[QuerySpec, ...]:
    """
    Compile the plan to one QuerySpec per output table.

    Full-export compile when `window` is None; the windowed compile applies
    the per-render window membership (state: horizon snapshot without
    updated_at; event log: append by event_sim_time; junction:
    extract-on-change with left_at horizon-masking).

    Args:
        plan: The resolved source plan (built with the matching
            windowed-ness: a non-None `window` pairs with a
            `windowed=True` plan, None with `windowed=False`).
        window: The incremental window, or None for a full export.

    Returns:
        One spec per output table, declared order; the event log last.

    Raises:
        ValueError: `window` presence disagrees with the plan's
            windowed-ness — a caller programming error, never a config
            validation outcome. Otherwise nothing: the plan already
            carries every validated fact, the windowed-shape checks
            included.
    """


def generate_source_init_config(emit: Emit, notices: NoticeSink) -> str:
    """
    Emit a commented candidate source config for the open emit.

    Pure function of (emit, code version): proposes one state table per
    records kind (combined, with a per-sub-type split alternative in
    comments), one junction table per membership table, an events stub
    covering every kind with a class-`tracked` property (membership sources
    and lifecycle-only kinds as commented-out entries; fully commented when
    no kind carries a tracked property), and the keys block per the
    key-election init contract. Consumes no record_roles. A
    name-colliding proposal degrades to a comment (later in sidecar order),
    so the emitted config always parses and plans clean.

    Args:
        emit: The open emit.
        notices: Sink for slice-only-column-omitted notices.

    Returns:
        The YAML text, comments included.

    Raises:
        SourceHistoryTrackedRequired: the sidecar predates per-column
            history_tracked flags — a candidate config that cannot export
            is not emitted (the export gate, shared).
        TemporalClassUnavailableError: a history_tracked column declares no
            usable temporal_class (C13 breach surfaced on the consuming
            path).
        PresentationKeysInvalidError: the keys proposal's strict-accessor
            read finds an incoherent presentation_keys block (the
            key-election init posture — shared refusal behavior).
    """
```

## Validation Rules

### Parse-Time (Pydantic)

```python
@model_validator(mode='after')
def source_section_required(self) -> Self:
    """mode: source requires a source section declaring at least one
    output — >= 1 table, or an events block; tables non-empty when
    present (two-sided with the other modes' sections, as today — but
    the bare-dump allowance is removed)."""

@model_validator(mode='after')
def table_source_exclusive(self) -> Self:
    """Exactly one of kind / membership per table declaration and per
    events-source declaration alike; sub_types only with kind (both
    shapes); name / columns / rename / sub_types / sources / only /
    ignore non-empty when present, their entries distinct; rename values
    distinct; only and ignore mutually exclusive; table names distinct
    across the declaration list; at most one events block (single log)."""
```

### Business Rules

Run at plan time against the open emit, before any write. `{owner}` in a
message is the declaring unit's label: `table '<name>'` for a `tables`
entry, `events source #<n>` (1-based, declaration order) for an `events`
source.

| Rule | Checks | Error |
|---|---|---|
| `SourceTableKindUnknown` | Every declared `kind` has a `records__<kind>` table | `"{owner}: kind '{kind}' not in this emit"` |
| `SourceTableSubTypeUnknown` | Every `sub_types` entry is in the kind's discriminator domain | `"{owner}: sub_type '{sub_type}' not declared for kind '{kind}'"` |
| `SourceSubTypesOnFlatKind` | `sub_types` only on a sub-typed kind | `"{owner}: kind '{kind}' declares no sub-types"` |
| `SourceTableMembershipUnknown` | Every `membership` reference resolves to a sidecar membership table | `"{owner}: no membership table for ({kind}, {property})"` |
| `SourceColumnUnresolved` | Every `columns` / `rename` key resolves on the table's source surface — a state table's identity column by its elected surface's contract name only, the junction owner column by its source name `record_id` whatever surface it carries, and `last_mutation_sim_time` only on a non-windowed invocation (the windowed state render omits it); every `only` / `ignore` entry names a property (element field) of its source | `"{owner}: '{entry}' not a column of its source"` (the unrendered-surface and windowed-`updated_at` cases name the election / omission) |
| `SourceColumnNotAddressable` | No `columns` / `rename` entry names `fork_path` / `ref_index__*`, or `record_index` other than as the table's elected surface; no `columns` entry names the table's elected surface (identity is election-governed) — a non-elected, unrendered surface name (`record_id` under a `presentation_id` election) is `SourceColumnUnresolved` instead | `"table '{name}': '{column}' is not addressable here"`, naming why |
| `SourceEventSourceOverlap` | `events.sources` resolve pairwise-disjoint population sets (membership sources distinct by `(kind, property)`) | `"events: sources overlap on population '{population}'"` |
| `SourceSliceOnlyRead` | No declaration entry names a non-exempt `slice_only` column | Names the entry, the column, and the omission reason |
| `SourceUnclassifiedColumn` | Every projected records column classifies to a taxonomy role | Names the table and column |
| `SourceAnchorRequired` | An `EffectiveAnchor` resolved | Unchanged message |
| `SourceNameCollision` | Output table names (the event log's included) and per-table column names unique after defaults + renames | `"output name collision: {names}; resolve via rename"` |
| Reserved-name check | No output table name collides with bookkeeping names / suffixes; no output column named `last_mutation_sim_time` | Unchanged |
| `ElectionMixedIdentity` / `ElectionUnionUnsafe` | Identity gates per declared table; edge gates per referencing column, per event-log item-type (over the union of its sources' addressed populations; the owner kind's for a membership item-type), and per audited reference property; no gate across item-types (polymorphic identity) | Per the key-election design |
| `SourceHistoryTrackedRequired` | The sidecar carries `history_tracked` flags — unconditional, as today (the events render and the windowed state snapshot consume them) | Unchanged message |
| `TemporalClassUnavailableError` (reader-owned) | Every consulted flagged column declares an in-enum class (audited-set resolution) | Unchanged |
| Single-branch guard | Exactly one branch | Unchanged |

Retired rules: `SourceRecordRolesRequired`, `SourceRoleUnknown`,
`SourceSubtypesUndeclared`, `SourceExcludeUnresolved`,
`SourceRenameUnresolved` (subsumed per-table by `SourceColumnUnresolved`),
`SourceRenameSliceOnly` (subsumed by `SourceSliceOnlyRead`).
