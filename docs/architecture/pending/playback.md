---
status: draft
---

# Playback API

The playback seam: forge's caller-driven, deterministic library surface for
driving an emit as a tape — **two tiers, one event-time line**: primitive
playback (atoms → events / state) and shaped playback (a declared target shape
→ its tables per window / as of T). Build slot #1 of the one-engine build
order. Feature ratification: `note: playback-api`; boundary:
`note: playback-boundary-razor`; segmentation grammar:
`note: sub-type-atom-principle`; two-tier framing ratified 2026-07-11
(architect session, use-case walk).

---

## Problem

Forge already computes all four playback answers, but each is welded inside a
verb-specific engine, unreachable as a library call:

| Ask | Where the machinery lives | Why a caller can't use it |
|---|---|---|
| Iterate events | streaming engine: row-state-events + membership-events folds, k-way merge, global `seq` | entangled with `StreamConfig`, routing, pacing, and sinks — push-shaped, CLI-shaped |
| Window `[T1, T2)` | incremental driver | keyed to cursor files, fingerprints, and writer orchestration; the driver owns the frontier, not the caller |
| Snapshot at T | state-at derivation | reachable only through source-mode `change_delivery: snapshot`; reconstructs record rows only — no membership containment at T |
| Seek | — | does not exist |

Concrete failure: a cadenced consumer (a loom micro-batch channel, a script, a
test rig) that wants *"exactly what changed in `[T1, T2)` for
`actor_doctor + actor_nurse`, as data, with the caller owning the frontier"*
must today assemble an `ExportConfig`, a cursor file, and a writer directory —
and receives files, not an answer. Every feature that lands before this seam
exists accretes more code against engine internals that the seam will later
have to cut through.

The gap is two-dimensional. The product sells the **shape × delivery grid** —
a real company's data estate mixes ingestion modes and shapes, and every
combination has a customer: source-shape × CDC stream (ETL teaching),
dimensional-shape × live cadence (dashboard teaching, BI-tool demos),
base-shape × snapshot-per-T (data-science feature loops), membership ×
everything (ops/IE teaching). The delivery axis will be driven by callers
above forge, and those callers reach forge through the playback API *only*
(the ratified dependency rule). So the seam must answer both kinds of
question: the **primitive** ones (events and state on atom populations) and
the **shaped** ones (window k of a star schema, this source system as of T) —
as data, statelessly. A seam that speaks only events and state makes the
flagship composition — a CDC channel into Kafka and a star-schema drip into a
warehouse off one master clock — unbuildable without a second door or a
reimplementation of forge's reshaping.

## Solution

A new `playback` package exposing **one API in two tiers**, both pull-only,
deterministic, permissive/total, and keyed on **one inclusive-T event-time
line**:

```
tape (run.duckdb + base.json)
   │  reader (version gate, sidecar, single-branch guard)
   ▼
derivations ── row-state-events · membership-events · state-at
               · NEW: membership-state-at
   ▼
TIER 1 · primitive playback  (atoms in; below the modes)
   ├─ events(start, end)   → canonical-order PlaybackEvent iterator, entry-point-invariant seq
   ├─ snapshot(T)          → record state + membership containment at T (pyarrow)
   └─ seek(T)              ≡ snapshot(T) + events(T+1, ∞)
   ▼
modes (dimensional · source · base later) — compile a declared target shape
   ▼
TIER 2 · shaped playback  (a target shape in; above the modes)
   ├─ window(T1, T2)       → the shape's tables, per-table-class membership, stateless
   └─ state(T)             → the shape's tables as if the slice ended at T
   ▲
callers: loom channels · base mode (slot #2) · forge verbs (re-seam later) · scripts
```

**Tier 1** answers event-native questions on atom populations
(presence-driven sub-type selection, membership atoms, record-id instance
filters): **iterate** is unbounded `events`, the primitive **window** is
bounded `events`, **seek** is the guaranteed-consistent composition of
snapshot and iterate. **Tier 2** answers shaped questions through a declared
target shape (an `ExportConfig`): window k of a star schema or a source
system as data with the caller owning the frontier, and the whole shape as of
T — promoting the per-table-class window membership the incremental driver
already computes internally (each mode's pure windowed compile) from driver
internals to seam contract. Every answer in both tiers renders `sim_time` raw
and, when an anchor resolves, wallclock through the one effective-anchor
surface — tier 1 Python-side as offset-bearing ISO-8601 (streaming's rule),
tier 2 as each mode's shipped full-export rendering: two representations,
deliberately different, of the same resolved instant. Nothing at the seam paces, pushes, connects, or validates semantics:
pacing, cadence-boundary sequences, cursors, and sessions live above it (the
boundary razor); semantic validation belongs to `validate`.

## Affected Subsystems

- **`playback` (new)** — the seam package, two tiers with different layer
  heights under one API. **Tier 1 (primitive)**: the atom-selection types, the
  `Playback` head, the three operations, `PlaybackEvent`,
  entry-point-invariant `seq`, snapshot representation (sub-type stamping,
  wallclock siblings). Sits between derivations and the modes: imports the
  reader, the derivations layer, the anchor surface, and `errors` — never
  `exporters.*`, never `config`. **Tier 2 (shaped)**: `ShapedPlayback` —
  stateless shaped `window` / `state` over a declared target shape. Sits above
  the modes: imports `config` (the shape envelope) and the modes' pure compile
  surfaces. The dependency chain is acyclic by construction: tier 2 → modes →
  derivations → reader, with tier 1 a sibling consumer of derivations — no
  mode imports either tier. Loom's "via the playback API only" rule covers
  both tiers.
- **Exporters (dimensional + source)** — contract promotion, no behavior
  change: each mode's pure windowed compile (the cursor-free, writer-free
  function the incremental driver already wraps) and its full/state render
  become the conforming implementations behind tier 2. The modes keep owning
  compilation; the seam owns the ask contract.
- **Incremental driver** — its per-table-class / per-genre window-membership
  rules are promoted to tier-2 contract. The driver's own mechanics — the
  calendar/sim window-boundary sequence, cursor, fingerprint, drained
  detection, labels, staging, writers — remain driver-side, above the seam;
  the driver becomes tier 2's first re-seam customer (deferred per claim C).
- **Derivations layer** — gains one resident: **membership-state-at**
  (interval containment at an exclusive horizon), under the existing six-rule
  layer contract (pure SQL fold, anti-weld signature, canonical raw output,
  traceability, determinism, temporal honesty). No existing resident changes.
- **Streaming exporter** — contract promotion, no behavior change: the
  canonical total order `(event_sim_time, event_class, source_identity,
  record_id[, field tail])` and the global-`seq` definition, today defined by
  the streaming engine for its own stream, become seam-owned guarantees that
  streaming's output conforms to by construction. Re-seaming the `stream` verb
  onto the playback head is explicitly deferred (ratified relationship claim C:
  re-seam when next materially touched, byte-identical output as the bar).
- **Anchor** — a new consumer, contract unchanged: tier 1 renders wallclock
  from the resolved `EffectiveAnchor` in the absolute-frame Python rule
  (offset-bearing ISO-8601), the same computation the streaming engine performs
  for `ts`; tier-2 values keep their mode's shipped rendering (the
  `render_anchor_timestamp_expr` SQL surface) because every tier-2 value is
  its full-export value.
- **Reader** — a new consumer, contract unchanged: `open_emit`, the sidecar
  accessors (`subtype_values`, `columns`, `pinned_ids`), the faithful records
  relation (population restriction), and the columnar `query_arrow` surface.

## What Doesn't Change

- **The reader's contract** — version gate, sidecar accessors, faithful
  builders, both read surfaces. Playback adds no reader capability.
- **The five existing derivations** — signatures, canonical columns, and
  ORDER BY contracts of versioned-intervals, row-state-events,
  membership-events, state-at, and reference-resolution are untouched. The
  seam composes them; it does not modify them.
- **Every shipped verb, byte for byte** — `validate`, `export` (dimensional +
  source), `stream`, `mixer`, `corrupt`, `init`, and the incremental flags
  produce identical output. No re-seam happens in this change.
- **The incremental driver's mechanics** — window-boundary sequences
  (calendar and sim regimes), the cursor, the fingerprint, drained detection,
  labels, empty-window emission, staging, and the writers stay driver-side.
  Tier 2 speaks raw-ns bounds only; computing *which* bounds (civil-calendar
  boundary math, a live wallclock cadence) is the caller's job — the
  incremental driver today, loom's timeline later.
- **Config envelopes** — no new YAML and no new fields. Tier 2 *consumes* the
  existing `ExportConfig` as its shape value (reading the mode and its
  section; `rebase` is resolved to an anchor by the caller, `incremental` is
  driver-side cadence); `StreamConfig` and `CorruptConfig` are untouched.
- **The corrupters** — unchanged; corrupted tapes are input, not subject.
  Two asymmetric facts the seam leans on: a record's *kind* is table identity
  — no shipped operation can re-home a row across `records__*` tables, so
  kind-level atom identity is safe by construction; the *sub-type
  discriminator* (`prop__<kind>_type`) is deliberately corruptible
  (`null_cells`; `mutate_cells` resample / out-of-domain / string dirt — a
  shipped teaching case) and plays through verbatim (§ The atom selection
  surface).
- **The contract boundary and the single-branch stage** — the seam composes
  `require_single_branch` and stays trunk-only.
- **Named atom groups** — deliberately deferred, a genuinely separable layer:
  the seam speaks atoms; a named-group vocabulary (shared with loom plans) can
  be added above the selection surface later without changing any seam
  contract, because a group resolves to a set of atoms before the seam sees it.

## Semantics

### Two tiers, one API

| Tier | Takes | Answers | Layer height | Serves |
|---|---|---|---|---|
| 1 — primitive | an atom selection | `events` / `snapshot` / `seek` | below the modes | CDC and event-native callers, feature-store loops, queue/ops material, SQL-over-events teaching |
| 2 — shaped | a declared target shape (`ExportConfig`) | `window` / `state`, as the shape's tables | above the modes | warehouse drips, landing zones, as-of-T databases, `base` mode, BI/dashboard cadences |

Both tiers share the inclusive-T event-time line below, the anchor rendering
rules, permissive totality, and determinism. Everything in §§ One event-time
line through The membership-state-at derivation is tier 1; §§ Shaped window
and Shaped state are tier 2; the remaining sections govern both.

### One event-time line, inclusive T

Every ask is keyed on raw sim-time ns. **Position T is inclusive**: it means
"every event with `event_sim_time ≤ T` has been applied." The seam expresses
all bounds through one half-open convention, exploiting integer ns:

| Ask | Caller writes | Internally |
|---|---|---|
| Iterate whole tape | `events(None, None)` | all in-scope events |
| Window `[T1, T2)` | `events(T1, T2)` | `T1 ≤ event_sim_time < T2` |
| Resume strictly after T | `events(T + 1, None)` | `event_sim_time > T` |
| Advance a caller-owned frontier from T to T′ (inclusive) | `events(T + 1, T′ + 1)` | `T < event_sim_time ≤ T′` |
| Snapshot at T (inclusive) | `snapshot(T)` | state-at exclusive horizon `T + 1` |
| Seek to T | `seek(T)` | `snapshot(T)` + `events(T + 1, None)` |

**The consistency algebra** (the seam's headline guarantee, testable
directly): for any `0 ≤ T1 ≤ T2`, with `snapshot(−1)` denoting the empty
state (a notational basis case, not a valid ask),

> `snapshot(T2 − 1)` = `snapshot(T1 − 1)` ⊕ (every event of `events(T1, T2)`
> applied in `seq` order)

where ⊕ means: a `c` inserts the after-image row, a `u` replaces it, a `d`
deactivates it, a `join` adds a containment row, a `leave` removes one
containment row matching `(record_id, payload)` — unique up to byte-identical
duplicates under intact interval semantics, so ⊕ is well-defined. A snapshot
at T, a window ending at `T + 1`, and a stream advanced through T agree
exactly — cross-paradigm consistency is a seam guarantee, not a caller
feature.

The algebra is a *conditional* guarantee: it holds on every tape whose
temporal semantics are intact. Playback itself is total over any
structurally-conformant tape (§ Permissive playback) — but a tape whose
defect manifest declares temporal or interval breakage (the family-C and
family-E corrupters: shifted / non-monotonic `history`, distorted membership
intervals) has no single consistent world-state for the three answers to
agree about. On such a tape playback stays total, deterministic, and
faithful; replay and snapshot then disagree exactly where the manifest says
the data is broken — the manifest is the answer key, and the disagreement is
the corruption made visible, not a seam defect.

Because snapshot-at-T consumes *every* event stamped exactly T, there is no
mid-instant tie-break at the seam: coincident events at one instant are never
split across a snapshot boundary.

### The atom selection surface

Selection follows the sub-type atom principle. The atomic population is
`(kind, sub_type)`, presence-driven from the sidecar: a kind refines into
sub-types exactly when `Sidecar.subtype_values(kind)` is non-empty, and
degenerates to the bare kind otherwise. The membership atom is
`(owner_kind, owner_sub_type, property)` under the same presence rule, the
owner's sub-type derived from the record spine. No playback code keys on
*which* kinds can sub-type — the sidecar is the only authority.

| Selection element | Meaning |
|---|---|
| `RecordSelection(kind, sub_types=())` | the whole kind — no discriminator filter; the only form for a non-sub-typed kind |
| `RecordSelection(kind, sub_types=("doctor", "nurse"))` | the named sub-type populations only |
| `RecordSelection(..., properties=())` | identity + lifecycle only, no `prop__` columns |
| `RecordSelection(..., record_ids=frozenset({...}))` | instance axis: restrict to the named record ids (pins are the canonical source — the caller feeds `sidecar.pinned_ids`) |
| `RecordSelection(..., record_ids=None)` | no instance restriction |
| `MembershipSelection(owner_kind, owner_sub_types, property_name, fields, owner_record_ids)` | one membership table, optionally restricted to owner sub-type populations and owner instances |

Population restriction is one mechanism applied uniformly: the in-scope
record ids for an atom set are the record spine's ids whose discriminator
`prop__<kind>_type` is among the named sub-type values when `sub_types`
names any (composing the faithful records relation with a discriminator
predicate; whole-kind selection applies no discriminator predicate),
intersected with `record_ids` when given. The restriction is applied as an outer row filter
over each fold's canonical relation, re-imposing the fold's declared ORDER BY
— pure row selection, never recomputation, so every surviving value equals its
unrestricted value. The discriminator is read from the record spine as a
temporally constant classification — the same convention the shipped streaming
routing surface uses for its Layer A `route_table`.

Two verbatim-playback rules keep the selection surface total over corrupted
tapes. First, **the stamp is data; the declared domain is only the selection
vocabulary**. Sub-type stamping reads the spine discriminator verbatim: a
corrupted cell stamps exactly what it holds — a resampled record plays as the
sub-type its cell now names (faithfully wrong; only the defect manifest
knows), an out-of-domain or string-dirt value stamps verbatim as a `sub_type`
outside the declared vocabulary, a nulled cell stamps `NULL`. Selection stays
sidecar-declared: named `sub_types` are a predicate over declared values (a
dirt record matches none of them), and whole-kind selection — no
discriminator filter — plays everything, dirt included; callers partition by
the verbatim stamp. A record's *kind*, by contrast, is table identity — no
corrupter path can re-home a row, so kind-level atom identity needs no such
rule. Second, the membership owner's sub-type composes the spine by
**LEFT join**: a membership row whose owner has no spine row (a `delete_rows`
orphan) still plays — verbatim, never dropped — stamped `owner_sub_type`
`NULL`; it matches no named `owner_sub_types` value (a predicate, not an
error, exactly like unknown `record_ids`), and the empty tuple (no owner
filter) includes it. The seam never decides what corruption intended: it
emits the data as it is.

Property and field selection is **column projection only**: it narrows
after-images and snapshot columns, never the event row set. A `u` whose
coincident changes touch only unselected properties still plays (its
after-image then equals its predecessor's on the selected columns), so `seq`
is invariant under `properties` / `fields` — only the population axes (the
atom set, `sub_types`, `record_ids` / `owner_record_ids`) change the in-scope
stream.

Two consequences of the atom grammar:

- **Answers are stamped with atom identity.** Every `PlaybackEvent` carries
  its `RecordAtom` or `MembershipAtom` (per-record sub-type read verbatim
  from the spine); snapshot record tables carry a `sub_type` column (`NULL`
  when the kind is not sub-typed or the discriminator cell is `NULL`) and
  membership tables an `owner_sub_type` column (`NULL` when the owner kind is
  not sub-typed, the owner row is an orphan, or the owner's discriminator
  cell is `NULL`). Callers partition by atom without re-deriving anything;
  grouping lives above the seam.
- **The event stream interleaves kinds; tables never do.** One head merges
  all selected atoms — record and membership alike — into one canonical-order
  stream (interleave is blessed on streams). Snapshot tables are per kind /
  per membership table (tabular combination is same-kind only; a kind-level
  table over its sub-type atoms is legal because they share one column shape).
- **Unknown record ids select nothing — never an error.** An id filter is a
  predicate, not a reference: a pinned id deleted by a corrupter simply
  matches no rows. Erroring would break corrupted-tape playback.

### The canonical total order and entry-point-invariant `seq`

The seam owns the canonical total order over all in-scope events:

> `(event_sim_time ASC, event_class ASC, source_identity ASC, record_id ASC[, field-value tail])`

where `source_identity` is the kind for record events and the
`(owner_kind, property)` pair for membership events, and `event_class` is the
folds' ordering key (`c`=0 < `u`=1 < `d`=2; `join`=0 < `leave`=1). This is the
order the streaming engine already realizes; promoting it to the seam makes it
a guarantee streaming conforms to rather than defines. The k-way merge
semantics are unchanged: per-source folds arrive pre-sorted, `source_identity`
makes the inter-stream tie-break deterministic, and field tails are never
compared across folds.

`seq` is the event's 1-based position in that order **over the whole in-scope
stream** — a pure function of `(tape, selection)`, never of where the head
entered. Normative rule: a head opened at any lower bound numbers its first
event `1 + N`, where `N` is the count of in-scope events strictly before the
bound in canonical order — a deterministic count over the same folds, not a
replay. Consequences:

| Condition | Result |
|---|---|
| Same `(tape, selection)`, `events(None, None)` vs `events(T+1, None)` | events after T carry identical `seq` in both |
| `seek(T)` then iterate vs full play | identical events, identical `seq`, from T+1 onward |
| Different `selection` | different in-scope stream — `seq` is per-selection, exactly as streaming's `seq` is per-config; each caller (each loom channel) holds its own head |
| Byte-identical duplicate membership intervals (contract-legal) | byte-identical events tie on the canonical key; whichever sorts first takes the lower `seq`, and the delivered values are identical either way |

### The event stream

`events(start, end)` yields `PlaybackEvent`s lazily in canonical order.
Nothing is computed until the iterator is pulled (the pull commitment: pacing,
buffering, and delivery are caller concerns, so timing authority cannot exist
at the seam). Event content is the shipped fold semantics unchanged:

- Record events: `c`/`u`/`d` per the row-state-events derivation — genesis at
  `created_sim_time` carrying creation values, one `u` per distinct history
  instant after creation (coincident property changes coalesce into one
  after-image), `d` at `deactivated_at` with an all-`NULL` after-image (the
  canonical after-only delete → `after` is `None`).
- Membership events: `join`/`leave` per the membership-events derivation —
  every interval yields a `join`; a `leave` only when the interval closed
  within the slice; both carry the full payload (append-only fact log, no
  key-only tombstones).
- After-image keys are the single column-order producers' names verbatim
  (`resolve_stream_columns` / `resolve_membership_columns`); every value is
  codec `VARCHAR` — `str` or `None`.
- `record_id` is the event key: the changed record's natural id, or the
  membership owner's id. `presentation_id` rides in record events when the
  kind carries one; it is never the key.

`ts` renders per the anchor exactly as streaming renders it: when an anchor
resolves, the absolute instant `start_instant(UTC) + event_sim_time ns`
projected into `anchor.timezone` as an offset-bearing ISO-8601 `str`; when no
anchor resolves, the raw `event_sim_time` `int`. Never a naive local
timestamp, never `now()`.

### Snapshot

`snapshot(at_sim_time=T)` returns a lazy `PlaybackSnapshot`; each table
materializes on first access through the columnar surface (`pyarrow.Table`,
typed even at zero rows). Two table families:

| Family | One table per | Contents |
|---|---|---|
| Record state | selected kind | the state-at fold at horizon `T + 1`, restricted to the selection's population and instances: one row per in-scope record with `created_sim_time ≤ T`; `active` / `deactivated_at` horizon-rendered; selected `prop__` columns as-of T (history-tracked) or current-value (untracked — the declared temporally-constant exception); plus a `sub_type` stamp column |
| Membership containment | selected membership table | the new membership-state-at fold at horizon `T + 1`, restricted to in-scope owners: one row per interval containing T; plus an `owner_sub_type` stamp column |

A record created after T is absent — not present-with-nulls. A membership
interval contains T iff `joined_sim_time ≤ T` and (`left_sim_time` is `NULL`
or `left_sim_time > T`); a zero-width interval (`joined = left`) contains no
T, consistent with applying its coincident `join` then `leave` in event-class
order.

**Wallclock siblings.** When the head's anchor resolves, each raw-ns lifecycle
column on a snapshot table — `created_sim_time`, `deactivated_at`,
`joined_sim_time` — gains a sibling `<name>_ts` column: the offset-bearing
ISO-8601 rendering of the same instant, `NULL` where the raw value is `NULL`.
When no anchor resolves, no sibling columns exist. The raw ns columns are
always present; ordering and the consistency algebra always key on raw ns.
One instant renders byte-identically as an event `ts` and as a snapshot
`_ts` value.

### The membership-state-at derivation (new resident)

`build_membership_state_at_sql(sidecar, fork_path, owner_kind, property_name,
fields, horizon_ns)` — the point-in-time counterpart to membership-events,
under the six-rule layer contract:

- **Row set.** One row per interval of the one
  `membership__<owner_kind>__<property_name>` table (discovered through the
  sidecar, filtered to `fork_path`) satisfying
  `joined_sim_time < horizon_ns AND (left_sim_time IS NULL OR left_sim_time >= horizon_ns)`.
- **Canonical columns** (`MEMBERSHIP_STATE_AT_COLUMNS`): `record_id` (the
  owner), `joined_sim_time` (raw ns `BIGINT`), then one column per selected
  element-schema field in `resolve_membership_columns` order, each cast to
  codec `VARCHAR`. `left_sim_time` is **never projected** — for a contained
  interval it is either `NULL` or strictly future state relative to the
  horizon, and carrying it would violate temporal honesty.
- **Declared order.** `(joined_sim_time, record_id, <field tail>)`, the field
  tail compared as `CAST(... AS VARCHAR) NULLS FIRST` — the membership-events
  tail rule.
- **Event-time key.** The constant horizon, as in state-at: every projected
  value derives from base state strictly before it (field values are
  interval-constant by the same upstream guarantee membership-events relies
  on).
- **Totality.** The predicate is total over any structurally-conformant
  table: an inverted interval (`left < joined`) satisfies it for no horizon
  between the swapped bounds and answers deterministically; overlapping
  duplicate intervals yield one row each. Faithfully wrong, never an error.
- **Errors.** A missing membership table raises `TableNotFoundError`; an
  unresolvable field raises `ExportError` — the layer's cause-based taxonomy.

### Shaped window (tier 2)

`window(start_sim_time, end_sim_time)` returns one relation per output table
the shape declares, each tagged with its **delivery class** so a caller lands
it correctly (`append`: merge rows in; `snapshot`: replace the table). The
content contract is the shipped per-table-class / per-genre window membership,
promoted verbatim — stateless, relations out, caller owns the frontier. All
membership tests run on raw sim-time ns, half-open `[start, end)`.

Dimensional shape:

| Table class | Window key (ns) | Per-window content | Delivery |
|---|---|---|---|
| Fact, records grain | `last_mutation_sim_time` | rows whose key ∈ window — final on arrival, never revised | append |
| Fact, history_point grain | `sim_time` | rows whose key ∈ window | append |
| Dim, SCD-2 | the version's `valid_from` change point | version rows born in the window, as the physical projection: declared columns minus `valid_to` slots plus the raw `__valid_from_ns` bookkeeping column — `valid_to` is never materialized; closing versions is the consumer's merge (or a view above the seam) | append |
| Dim, type-1 | — | full current-state table every window (columns gated temporally constant; row set is the end-of-run population — the shipped carve-out) | snapshot |
| history_interval / membership grain | — | rejected (the shipped windowed-grain rule) | — |

Source shape:

| Genre | Window key (ns) | Per-window content | Delivery |
|---|---|---|---|
| change-log (`changelog` delivery) | `event_sim_time` | event rows whose key ∈ window — immutable | append |
| transaction | `last_mutation_sim_time` | rows whose key ∈ window | append |
| reference | — | full current-state table every window | snapshot |
| junction | `joined_sim_time` / `left_sim_time` — activity | extract-on-change: the interval row in each window containing its join, its leave, or both, with `left_at` horizon-masked (rendered only when `left_sim_time < end`, else `NULL` — masking future state, never recomputation) | append |
| change-log (`snapshot` delivery) | — | one full-table state-at reconstruction at horizon `end` per window | snapshot |

The window predicate stays the outermost filter over the shape's full-export
relation: every emitted value is its full-export value; the window selects
rows, never recomputes them. The shipped windowed business rules (immutable
`fk` hops, raw-key ordinals, temporally constant slice reads and dim filters)
gate the config so selecting-not-recomputing is temporally honest — they
apply to tier-2 `window` exactly as they apply to the incremental driver
today, validated on the first `window` ask (the shipped driver's
window-gated pass; a shape used only for `state` never runs them).

### Shaped state (tier 2): truncated-tape semantics

`state(at_sim_time=T)` returns the shape's tables **as if the emit's slice
ended at T** — one definition covering every class, with the delivery tag
`snapshot` on every table:

| Table class / genre | State at T |
|---|---|
| Dim, SCD-2 | version rows with change point ≤ T; `valid_to` materialized per the full-export rules, the latest version open |
| Fact, history_interval grain | interval rows with `sim_time ≤ T`; `lead_sim_time` horizon-masked (`NULL` unless `lead_sim_time ≤ T`) — the junction rule's dimensional twin |
| Dim, type-1 / reference genre | one row per record created ≤ T; values current (untracked properties are the declared temporally-constant exception) |
| Fact, records grain / transaction genre | one row per record created ≤ T, reconstructed as of T (the state-at fold), not end-of-run values |
| Fact, history_point / change-log genre (`changelog`) | event rows with key ≤ T |
| Junction / membership grain | interval rows with `joined_sim_time ≤ T`, `left_at` horizon-masked (`NULL` unless `left_sim_time ≤ T`) |
| change-log genre (`snapshot` delivery) | the state-at reconstruction at horizon `T + 1` |

**The reconstructibility gate.** A value whose base source cannot be
faithfully reconstructed at a past horizon — `last_mutation_sim_time`
projected as a column value (untracked writes advance it but leave no
history; the shipped source-snapshot render already omits `updated_at` for
exactly this reason) — fails the `state` ask with `PlaybackError` when the
shape projects it, checked sidecar-only on the first `state` call; `window`
on the same head stays legal. Fail-fast, never fabricated, never understated:
the source-snapshot precedent, generalized. Window keys are unaffected
(keying on `last_mutation_sim_time` needs no per-horizon reconstruction;
projecting it does).

**The bridging invariant.** `state(T_slice)` — T at the slice bound — is
value-identical to the shape's full export. This is the equation that makes
`base` mode a thin renderer over shaped state (claim A) and defines the
incremental driver's re-seam bar (claim C).

### Permissive playback — totality over structurally-conformant tapes

The seam validates nothing beyond what the reader already gates (the version
gate, the structural floor) plus its own selection-resolvability checks
against the sidecar. Semantic conformance (C6/C7/C9–C12) is never re-checked;
defects flow through faithfully. This is load-bearing: it is what makes
corrupted tapes play identically to intact ones, which the learning
environment's answer keys and loom's record/replay stand on.

Totality is the sharpened form: **every fold and every seam operation must be
a total function of structurally-conformant input.** No inner join, filter,
or cast may silently drop or error on a row a semantic defect made weird.

| Injected defect (corrupter) | Playback behavior |
|---|---|
| `dangle_reference` / `mispoint_reference` | the reference value flows through after-images and snapshots verbatim; nothing resolves or checks it |
| `duplicate_rows` on membership | duplicate `join`/`leave` events; duplicate containment rows |
| `delete_rows` | the record's events/state are simply absent; a `record_ids` filter naming it matches nothing; surviving membership rows whose owner was deleted play as orphans (`owner_sub_type` `NULL`) |
| `shift_sim_time` / non-monotonic `history` | events order by the shifted values under the canonical key; snapshots reconstruct from the shifted values (the consistency algebra's temporal precondition is broken — the manifest declares it) |
| `distort_intervals` (overlap / gap / inverted) | membership events unpivot verbatim; containment answers by the total predicate (likewise outside the algebra's precondition) |
| `null_cells` / `mutate_cells` | values flow through as codec `VARCHAR` / `NULL` — the sub-type discriminator included: a resampled record plays as the sub-type its cell now names, out-of-domain / string dirt stamps verbatim (reachable via whole-kind selection), a nulled discriminator stamps `NULL` |
| `schema_drift` | reads follow the regenerated sidecar; a selection naming a dropped column fails resolvability at open — faithful, because the table genuinely lacks it |

Anything genuinely wrong with a tape is fixed upstream (producer or
corrupter); the seam never defends downstream.

### Edge and error semantics

| Condition | Result |
|---|---|
| `events(T, T)` | empty iterator — a valid, deterministic empty |
| `events(start, end)` with `start > end` | `PlaybackError` (caller contract violation, not a data condition) |
| negative `start` / `end` / `at_sim_time` | `PlaybackError` — sim-time is a non-negative ns offset by contract |
| `snapshot(0)` | records created at 0 present (inclusive T) |
| `at_sim_time` ≥ the slice bound | final state / remaining events then exhaustion — total, no range check |
| empty population (a sub-type with zero rows in the slice) | zero events; zero-row typed tables — declared atoms always answer (the declared-but-empty rule) |
| selection resolvability failure (unknown kind, sub-type, property, membership table, field) | `PlaybackError` at `open_playback` — fail-fast at open, before any data read |
| shaped config invalid (the mode's own full validation) | fail-fast at `open_shaped_playback` — the mode's existing validation errors pass through |
| ask-scoped shape gates (the windowed business rules at `window`; the reconstructibility gate at `state`) | fail-fast on the ask's first call, sidecar-only — the rules' existing errors pass through; the seam-level gate raises `PlaybackError` |
| source shape with `anchor=None` | error at `open_shaped_playback` — the source mode's mandatory-anchor rule, surfaced at open |
| `window` / `state` on an empty window / empty population | zero-row typed tables, every declared table present (the declared-but-empty rule) |
| upstream guard/reader errors | pass through unchanged (`ExportError` from the single-branch guard, `TableNotFoundError`, the reader's version-gate errors) |

### Invariants

1. **Pull-only.** No operation performs I/O until an answer is pulled;
   `open_playback` reads the sidecar only. The seam contains no clock reads,
   no sleeps, no sinks, no sessions — timing authority cannot exist here by
   construction.
2. **Deterministic.** Same tape + same selection + same anchor + same ask
   arguments + same code version → identical events, identical `seq`,
   identical tables. Corrupted tapes included.
3. **Entry-point-invariant `seq`.** `seq` is a pure function of
   `(tape, selection)`; bounded and unbounded heads agree.
4. **One event-time line, across both tiers.** On a temporally-intact tape
   (§ One event-time line) the tier-1 consistency algebra holds for every
   `(selection, T1, T2)`. Tier-2 agreement is per table class: for the
   event-keyed classes (history_point grain; change-log genre under
   `changelog` delivery) accumulating window slices `[0, T+1)` by union
   reproduces `state(T)` exactly; the remaining classes window on a key
   other than the row's event time or deliver the shipped end-of-run
   carve-out (`last_mutation_sim_time` keying, type-1 / reference full
   tables, junction / history_interval extract-on-change, SCD-2's
   never-materialized `valid_to`), so their per-T agreement is up to the
   class's documented consumer merge, with exact equality guaranteed at the
   slice bound (the bridging invariant). Across tiers, a shaped change-log
   table over `[T1, T2)` and a tier-1 `events(T1, T2)` pull carry the same
   change set. Cross-paradigm *and* cross-shape consistency is a seam
   guarantee — scoped and stated per class, never implied where a class's
   delivery cannot support it.
5. **Faithful reshaping + temporal honesty, inherited per answer.** Every
   delivered value traces to a base value or a declared recoding; no value on
   an answer derives from base state later than the answer's time key (the
   declared temporally-constant sources excepted).
6. **Permissive totality.** Every operation is total over
   structurally-conformant input; semantic defects flow through unchanged.
7. **Rendered-instant agreement.** One absolute instant renders
   byte-identically wherever it appears — event `ts`, snapshot `_ts` — under
   one resolved anchor. A tier-1 guarantee: tier-2 values keep their mode's
   shipped full-export rendering, a different representation of the same
   resolved instant.
8. **Layer direction.** Tier 1 imports the reader, derivations, the anchor
   surface, and `errors` — never `exporters.*`, never `config`. Tier 2
   imports `config` and the modes' pure compile surfaces. The chain
   tier 2 → modes → derivations → reader is acyclic by construction, with
   tier 1 a sibling consumer of derivations; a mode never imports either
   tier.
9. **Bridging.** `state(T_slice)` equals the shape's full export — the seam
   is provably sufficient to re-write the shipped verbs on (the CLI is the
   seam's permanent proof of sufficiency).
10. **Inherited.** Version-gated input, sidecar-driven schema discovery,
    single-branch guard, no producer dependency.

## Configuration

None in this change. The seam is a Python library surface; its author-facing
YAML skins are its consumers (`base` mode — build slot #2 — and, per
relationship claim C, the existing envelopes when their verbs re-seam). No new
config model, no new CLI flag.

## Interface Contracts

### Selection and identity types

```python
@dataclass(frozen=True)
class RecordAtom:
    """One record population: a sub-type of a kind, or a whole non-sub-typed kind.

    sub_type is None when the kind declares no discriminator domain, or when
    the record's discriminator cell is NULL (a corrupted tape). On a
    corrupted tape it may hold an undeclared value verbatim — the stamp is
    data; the declared domain is only the selection vocabulary.
    """
    kind: str
    sub_type: str | None


@dataclass(frozen=True)
class MembershipAtom:
    """One membership population: an owner population's collection property.

    owner_sub_type is None when the owner kind declares no discriminator
    domain, when the owner has no spine row (a corrupted tape's orphan
    membership row — played verbatim, never dropped), or when the owner's
    discriminator cell is NULL. May hold an undeclared value verbatim on a
    corrupted tape.
    """
    owner_kind: str
    owner_sub_type: str | None
    property_name: str


@dataclass(frozen=True)
class RecordSelection:
    """Select record populations of one kind, with properties and instances.

    sub_types: declared discriminator values to include — a predicate over
        the spine discriminator; the empty tuple means the whole kind (no
        discriminator filter; the bare kind when not sub-typed). Non-empty is
        legal only for a sub-typed kind.
    properties: bare property names riding after-images and snapshot rows, of
        either SCD class; the empty tuple means identity + lifecycle only.
        Projection only — never changes the event row set or seq.
    record_ids: the instance axis — restrict to these record ids; None means
        no instance restriction. Must be non-empty when given. Unknown ids
        select nothing (never an error).
    """
    kind: str
    sub_types: tuple[str, ...]
    properties: tuple[str, ...]
    record_ids: frozenset[str] | None


@dataclass(frozen=True)
class MembershipSelection:
    """Select one membership table, with owner populations and instances.

    owner_sub_types: declared owner discriminator values to include — a spine
        predicate (an orphan owner matches no named value); empty tuple = all
        owners, orphans included. Non-empty is legal only for a sub-typed
        owner kind.
    fields: bare element-schema field names riding payloads and containment
        rows; empty tuple = owner identity only. Projection only — never
        changes the event row set or seq.
    owner_record_ids: restrict to these owner ids; None = no restriction.
        Must be non-empty when given. Unknown ids select nothing.
    """
    owner_kind: str
    owner_sub_types: tuple[str, ...]
    property_name: str
    fields: tuple[str, ...]
    owner_record_ids: frozenset[str] | None


@dataclass(frozen=True)
class PlaybackSelection:
    """The head's full atom selection.

    At most one RecordSelection per kind and one MembershipSelection per
    (owner_kind, property_name); at least one selection overall.
    """
    records: tuple[RecordSelection, ...]
    memberships: tuple[MembershipSelection, ...]
```

### The event type

```python
@dataclass(frozen=True)
class PlaybackEvent:
    """One ordered change event on the seam's canonical event-time line.

    seq: 1-based position in the canonical total order over the whole in-scope
        stream — a pure function of (tape, selection), entry-point-invariant.
    op: 'c'/'u'/'d' for record events; 'join'/'leave' for membership events.
    atom: the population the event belongs to, sub-type resolved per record.
    record_id: the changed record's natural id, or the membership owner's id;
        the event key.
    presentation_id: the record's surrogate when the kind carries one; always
        None for membership events. Never the key.
    event_sim_time: the raw event-time key (ns).
    ts: offset-bearing ISO-8601 str when the head's anchor resolves, else the
        raw event_sim_time int.
    after: the full after-image / payload keyed by the canonical column names,
        every value codec VARCHAR (str) or None; None on a 'd' event.
    """
    seq: int
    op: Literal["c", "u", "d", "join", "leave"]
    atom: RecordAtom | MembershipAtom
    record_id: str
    presentation_id: str | None
    event_sim_time: int
    ts: str | int
    after: dict[str, str | None] | None
```

### Opening a head

```python
def open_playback(
    emit: Emit,
    selection: PlaybackSelection,
    anchor: EffectiveAnchor | None,
) -> Playback:
    """Bind a playback head to an open emit and a validated atom selection.

    Validates every selection element against the sidecar (fail-fast, before
    any data read) and enforces the trunk-only single-branch guard. Performs
    no table reads. The caller owns emit's lifetime and resolves the anchor
    (resolve_effective_anchor or None for raw sim-time rendering).

    Args:
        emit: An open emit (version-gated by open_emit).
        selection: The atom selection; validated per Validation Rules.
        anchor: The resolved effective anchor, or None to render raw sim-time
            integers everywhere.

    Returns:
        A Playback head bound to (emit, selection, anchor).

    Raises:
        PlaybackError: The selection fails a validation rule (empty selection,
            duplicate atom, unknown kind / sub-type / property / membership
            table / field, sub_types on a non-sub-typed kind, empty
            record_ids set).
        ExportError: The sidecar enumerates zero or more than one branch
            (single-branch guard, passed through).
    """
```

### The head

```python
class Playback:
    """A tape head: pull-only, deterministic answers over one emit + selection."""

    def events(
        self,
        start_sim_time: int | None,
        end_sim_time: int | None,
    ) -> Iterator[PlaybackEvent]:
        """Iterate in-scope events in canonical total order, lazily.

        Half-open bounds on event_sim_time: yields events with
        start_sim_time <= event_sim_time < end_sim_time. None means unbounded
        on that side. seq is entry-point-invariant: numbering continues the
        whole-stream order regardless of start_sim_time.

        Args:
            start_sim_time: Inclusive lower bound (ns), or None for tape start.
            end_sim_time: Exclusive upper bound (ns), or None for tape end.

        Returns:
            A lazy iterator; no work happens until pulled.

        Raises:
            PlaybackError: start_sim_time > end_sim_time, or a negative bound.
        """

    def snapshot(self, at_sim_time: int) -> PlaybackSnapshot:
        """Point-in-time state at inclusive position T.

        Reflects every in-scope event with event_sim_time <= at_sim_time and
        nothing after: record state per selected kind (state-at fold, horizon
        at_sim_time + 1) and membership containment per selected membership
        table (membership-state-at fold, same horizon), each restricted to the
        selection's populations and instances.

        Args:
            at_sim_time: The inclusive position T (ns); >= 0.

        Returns:
            A lazy PlaybackSnapshot; tables materialize on first access.

        Raises:
            PlaybackError: at_sim_time < 0.
        """

    def seek(self, at_sim_time: int) -> PlaybackPosition:
        """Position the head at T: state as of T plus the stream after T.

        Pure composition, contract-guaranteed consistent: the position's
        snapshot is snapshot(at_sim_time) and its events are
        events(at_sim_time + 1, None), so replaying the events over the
        snapshot reproduces any later snapshot (the consistency algebra).

        Args:
            at_sim_time: The inclusive position T (ns); >= 0.

        Returns:
            A PlaybackPosition; both halves are lazy.

        Raises:
            PlaybackError: at_sim_time < 0.
        """
```

### Snapshot and position

```python
class PlaybackSnapshot:
    """Lazy point-in-time state at one inclusive position.

    at_sim_time: the inclusive position T this snapshot reflects.
    """

    at_sim_time: int

    def record_state(self, kind: str) -> pyarrow.Table:
        """The kind's state table at T.

        Columns: STATE_AT_COLUMNS (record_id; presentation_id when the kind
        carries it; created_sim_time; active; deactivated_at), a sub_type
        stamp (the spine discriminator verbatim, undeclared values included;
        NULL when the kind is not sub-typed or the cell is NULL), one
        prop__<p> per selected property, and — when the head's anchor
        resolves — a <name>_ts sibling per raw-ns lifecycle column. Typed at
        zero rows.

        Args:
            kind: A kind named by the head's selection.

        Returns:
            The materialized table; identical on repeated calls.

        Raises:
            PlaybackError: kind is not in the head's selection.
        """

    def membership_state(
        self,
        owner_kind: str,
        property_name: str,
    ) -> pyarrow.Table:
        """The membership table's containment rows at T.

        Columns: MEMBERSHIP_STATE_AT_COLUMNS (record_id — the owner;
        joined_sim_time; one column per selected field), an owner_sub_type
        stamp (verbatim; NULL when the owner kind is not sub-typed, the owner
        row is an orphan, or its discriminator cell is NULL), and — when the
        anchor resolves — joined_sim_time_ts. left_sim_time is never present.
        Typed at zero rows.

        Args:
            owner_kind: The owner kind of a selected membership table.
            property_name: Its collection property.

        Returns:
            The materialized table; identical on repeated calls.

        Raises:
            PlaybackError: (owner_kind, property_name) is not in the head's
                selection.
        """


class PlaybackPosition:
    """A seek result: state as of T plus the stream strictly after T.

    at_sim_time: the inclusive position T.
    """

    at_sim_time: int

    def snapshot(self) -> PlaybackSnapshot:
        """The state as of T; equal to Playback.snapshot(at_sim_time).

        Returns:
            The lazy snapshot.
        """

    def events(self) -> Iterator[PlaybackEvent]:
        """The stream strictly after T; equal to
        Playback.events(at_sim_time + 1, None).

        Returns:
            A lazy iterator with entry-point-invariant seq.
        """
```

### Shaped playback (tier 2)

```python
@dataclass(frozen=True)
class ShapedTable:
    """One output table of a shaped answer.

    name: the shape's declared output table name (author-verbatim).
    delivery: how a caller lands this relation — 'append' (land the rows
        additively; where a class revises a row across windows — junction /
        history_interval extract-on-change — reconciling is the class's
        documented consumer merge) or 'snapshot' (replace the table). Every
        table of state() is 'snapshot'.
    table: the relation, typed at zero rows.
    """
    name: str
    delivery: Literal["append", "snapshot"]
    table: pyarrow.Table


def open_shaped_playback(
    emit: Emit,
    config: ExportConfig,
    anchor: EffectiveAnchor | None,
) -> ShapedPlayback:
    """Bind a shaped head to an open emit and a declared target shape.

    Runs the mode's full config validation at open (sidecar-only, no data
    reads). The windowed business rules and the reconstructibility gate are
    ask-scoped — validated on the first window() / state() call respectively
    — so a shape legal for one ask but not the other still opens. The
    shape is the config's mode + mode section + shared exporter features;
    the config's rebase block is not read (the caller resolves the anchor)
    and its incremental block is not read (cadence-boundary sequences are
    the caller's job — the seam speaks raw-ns bounds only).

    Args:
        emit: An open emit (version-gated by open_emit).
        config: The target shape — a validated ExportConfig (mode:
            dimensional or source; base extends the Literal when it lands).
        anchor: The resolved effective anchor, or None. The source mode's
            mandatory-anchor rule applies at open.

    Returns:
        A ShapedPlayback head bound to (emit, config, anchor).

    Raises:
        PlaybackError: A seam-level open gate fails (source shape with
            anchor=None).
        ExportError: The mode's own config validation fails or the
            single-branch guard trips (passed through unchanged).
    """


class ShapedPlayback:
    """A shaped tape head: the target shape's tables per window or as of T."""

    def tables(self) -> tuple[str, ...]:
        """The shape's declared output table names, in declaration order.

        Returns:
            Every table window() and state() will deliver, independent of
            data (the declared-but-empty rule).
        """

    def window(
        self,
        start_sim_time: int,
        end_sim_time: int,
    ) -> tuple[ShapedTable, ...]:
        """The shape's tables for the half-open window [start, end).

        Stateless: the caller owns the frontier. Content per table class /
        genre is the promoted window-membership contract (§ Shaped window);
        every value is its full-export value — the window selects rows, never
        recomputes them. One ShapedTable per declared table, zero-row typed
        relations included, in declaration order.

        Args:
            start_sim_time: Inclusive lower bound (ns); >= 0.
            end_sim_time: Exclusive upper bound (ns); >= start_sim_time.

        Returns:
            One ShapedTable per declared output table.

        Raises:
            PlaybackError: Negative bounds or start > end.
            ExportError: A windowed business rule fails for the shape
                (first window call; passed through unchanged).
        """

    def state(self, at_sim_time: int) -> tuple[ShapedTable, ...]:
        """The shape's tables as if the emit's slice ended at T (inclusive).

        Truncated-tape semantics per table class (§ Shaped state); delivery
        is 'snapshot' on every table. state(T_slice) is value-identical to
        the shape's full export (the bridging invariant).

        Args:
            at_sim_time: The inclusive position T (ns); >= 0.

        Returns:
            One ShapedTable per declared output table.

        Raises:
            PlaybackError: at_sim_time < 0, or the shape fails the
                reconstructibility gate (first state call).
        """
```

### The new derivation

```python
def build_membership_state_at_sql(
    sidecar: Sidecar,
    fork_path: str,
    owner_kind: str,
    property_name: str,
    fields: tuple[str, ...],
    horizon_ns: int,
) -> str:
    """Build the canonical membership containment SELECT at one horizon.

    One row per membership__<owner_kind>__<property_name> interval containing
    the exclusive horizon: joined_sim_time < horizon_ns AND (left_sim_time IS
    NULL OR left_sim_time >= horizon_ns). Columns are
    MEMBERSHIP_STATE_AT_COLUMNS — record_id (the owner), joined_sim_time (raw
    ns) — plus one column per selected element-schema field in
    resolve_membership_columns order, each cast to codec VARCHAR.
    left_sim_time is never projected (future state relative to the horizon).
    Ordered by (joined_sim_time, record_id, <field tail>), the tail compared
    as CAST(... AS VARCHAR) NULLS FIRST. Total over structurally-conformant
    input: distorted intervals answer deterministically, never error.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        owner_kind: The membership table's owner kind.
        property_name: The membership table's collection property.
        fields: Selected element-schema field names (bare); may be empty
            (owner identity + joined_sim_time only).
        horizon_ns: The exclusive containment horizon in sim-time ns; >= 0.

    Returns:
        A complete, deterministic SELECT producing
        MEMBERSHIP_STATE_AT_COLUMNS plus the selected field columns.

    Raises:
        TableNotFoundError: No membership__<owner_kind>__<property_name>
            table is in the sidecar.
        ExportError: A selected field resolves to no elem__/member__ column
            shape on the table.
    """
```

### Errors

```python
class PlaybackError(Exception):
    """A playback-seam contract violation: an unresolvable selection, an
    invalid ask argument, or a seam-level shape gate (reconstructibility,
    source-shape anchor). Never raised for a data condition — semantic
    defects flow through (permissive playback)."""
```

## Validation Rules

### Parse-Time (Pydantic)

None — this change introduces no config models. All checks are open-time
business rules over plain typed values.

### Business Rules

Applied by `open_playback` (selection rules, sidecar-only) and by the ask
methods (argument rules). Every violation raises `PlaybackError`.

| Rule | Checks | Error message shape |
|---|---|---|
| `SelectionNonEmpty` | `records` + `memberships` name at least one selection | `"playback selection is empty"` |
| `RecordKindResolvable` | each `RecordSelection.kind` has a `records__<kind>` table in the sidecar | `"unknown kind {kind!r}"` |
| `SubTypesDeclared` | each `sub_types` value is in `subtype_values(kind)`; `sub_types` non-empty only when the kind is sub-typed; no duplicate values | `"kind {kind!r} declares no sub-type {value!r}"` / `"kind {kind!r} is not sub-typed"` |
| `PropertiesResolvable` | each `properties` name has a `prop__<name>` column on the kind | `"kind {kind!r} has no property {name!r}"` |
| `MembershipResolvable` | each `(owner_kind, property_name)` resolves to a sidecar membership table | `"no membership table for {owner_kind!r}.{property_name!r}"` |
| `MembershipFieldsResolvable` | each `fields` name resolves to exactly one column shape (scalar or reference) on the table | `"membership {owner_kind!r}.{property_name!r} has no field {name!r}"` |
| `AtomsUnique` | at most one `RecordSelection` per kind; at most one `MembershipSelection` per `(owner_kind, property_name)` | `"duplicate selection for {identity!r}"` |
| `InstanceSetNonEmpty` | `record_ids` / `owner_record_ids` is `None` or a non-empty frozenset | `"empty record_ids — pass None for no restriction"` |
| `AskBoundsValid` | `events`: bounds non-negative, `start <= end` when both given; `snapshot` / `seek`: `at_sim_time >= 0` | `"invalid event-time bound"` |

Unknown record *ids* are deliberately not a rule — an id filter is a
predicate, and a corrupted tape may have deleted any id (see § Permissive
playback).

Tier-2 rules — the first two applied by `open_shaped_playback`, the rest
ask-scoped (validated once, on the ask's first call):

| Rule | When | Checks | Error |
|---|---|---|---|
| `ShapedModeValid` | at open | the config passes its mode's full existing validation (dimensional plan rules, source plan/collision rules) | the mode's errors, passed through |
| `ShapedAnchorRequired` | at open | a source shape has a non-None anchor | `PlaybackError` |
| `ShapedWindowedRules` | first `window` ask | the shipped windowed business rules hold for the shape (immutable `fk` hops, raw-key ordinals, temporally constant slice reads / dim filters, no history_interval / membership grain under `window`) | the rules' existing errors, passed through |
| `ShapedStateReconstructible` | first `state` ask | the shape projects no value source unreconstructible at a past horizon (`last_mutation_sim_time` as a projected value) | `PlaybackError` |
| `AskBoundsValid` (shared) | every ask | `window`: bounds non-negative, `start <= end`; `state`: `at_sim_time >= 0` | `PlaybackError` |
