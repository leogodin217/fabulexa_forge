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
line**. Repo-local term, defined here: a **tape** is the emit under playback —
its event-time line driven as replayable media. It names the seam's *view*, not
the artifact: the input remains *the emit*, and contract prose never aliases
it. The **truncated tape** — the emit presented as if its slice ended at T —
is the load-bearing instance (§ Shaped state).

```
tape (run.duckdb + base.json)
   │  reader (version gate, sidecar, single-branch guard)
   ▼
derivations ── row-state-events · membership-events · state-at
               · NEW: membership-state-at · NEW: truncated-tape surface
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
   └─ state(T)             → the shape's tables as if the slice ended at T:
                             the mode's full-export compile over the truncated tape
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
internals to seam contract. Shaped `state` is the same compile at a different
tape: the mode's full-export compile over the truncated tape
(§ Shaped state) — as-of-T correctness by construction, not per-class
reassembly. Every answer in both tiers renders `sim_time` raw
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
  the modes: imports `config` (the shape envelope), the modes' pure compile
  surfaces, the notice channel (`NoticeSink` — the head binds a required
  sink at open and threads it to every compile it runs, so each ask's plan
  notices reach the caller), the derivations truncated-tape surface, and
  the reader's `Emit`
  for the truncated emit-view composition (the seam slices the tape; the
  modes stay time-agnostic). The dependency chain is acyclic by
  construction: tier 2 → modes → derivations → reader (tier 2's direct
  derivations edge adds no cycle), with tier 1 a sibling consumer of
  derivations — no mode imports either tier. Loom's "via the playback API only" rule covers
  both tiers.
- **Exporters (dimensional + source)** — two additive, time-agnostic
  extensions, one redefined semantic, and one naming reservation. The
  extensions leave every path that produces output today byte-identical.
  For `window`, contract promotion: each mode's pure windowed compile (the
  cursor-free, writer-free function the incremental driver already wraps)
  becomes the conforming implementation behind tier 2. For `state`, the
  mode's *full-export* compile runs over the truncated tape: the pure
  compile surfaces gain one required, nullable `base_relations` mapping
  parameter (physical
  base-table name → replacing relation; `None` from the full-export and
  windowed callers), realized by name shadowing over
  the compiled plan, and are invoked against the truncated emit view —
  their sidecar input is the truncated sidecar view, so the faithful
  builders enumerate exactly the columns the replacing relations carry —
  the modes never see a horizon (§ Interface Contracts, The compile
  indirection). One shipped semantic is redefined rather than gated:
  horizon-less `change_delivery: snapshot` — today a refusal, because a
  full export names no instant to snapshot at — becomes "reconstruct at
  the tape's end" (§ Shaped state), turning a refused path into a
  meaningful output. The naming reservation is the **presentation-name
  posture**, the companion product decision this change ratifies:
  `last_mutation_sim_time` is a sim-internal bookkeeping column — read it
  freely, deliver it under its own name never. Its value channels are
  untouched, byte for byte: the source mode's `updated_at` presentation
  default, the dimensional records-grain sources (`from:` /
  `correlation:` / `derived: value_map`, and `derived: timestamp`, where
  it remains the sole permitted records-grain timestamp source), window
  membership keying, the contract-endorsed high-water-mark use, and
  records-grain `ordinal.order_by` all keep their shipped semantics. What
  is withdrawn is the raw name on an output column:
  `last_mutation_sim_time` joins the shared reserved output-name check,
  closing the two paths that can deliver it today — a dimensional
  author-named column and a source `rename` target — as load-time errors
  naming the fix. Under `state`, the truncated tape presents the column
  itself as the honest as-of-T value (§ Shaped state, the recorded
  trail), so every shipped value channel works over it unchanged.
  `slice_only` keeps its shipped export-wide refuse posture (no exporter
  projects one, `init` never proposes one — the contract's mandate). The
  modes keep owning compilation; the seam owns the ask contract; the
  derivations layer owns the tape at T.
- **Incremental driver** — its per-table-class / per-genre window-membership
  rules are promoted to tier-2 contract. The driver's own mechanics — the
  calendar/sim window-boundary sequence, cursor, fingerprint, drained
  detection, labels, staging, writers — remain driver-side, above the seam;
  the driver becomes tier 2's first re-seam customer (deferred per claim C).
- **Derivations layer** — gains three additions. **membership-state-at**
  (interval containment at an exclusive horizon), a fold under the existing
  six-rule layer contract (pure SQL fold, anti-weld signature, canonical raw
  output, traceability, determinism, temporal honesty). An **additive
  second entry point on the state-at resident** — `build_state_at_end_sql`,
  the end-of-tape reconstruction: the same canonical relation with no
  horizon parameter and no horizon predicate in its SQL, so "the tape's
  end" is realized structurally by whatever data the composed relations
  hold (§ Shaped state); the existing horizoned builder's signature is
  untouched. And the
  **truncated-tape surface**: three relation builders and the truncated
  sidecar view, presenting the emit as the producer would have emitted it
  sliced at T (§ Shaped state). The builders are pure, anti-weld,
  deterministic, and total like the folds, but relation *presenters* that
  replace base tables inside a compile, so each carries the replaced
  table's column shape (deviations declared in its contract) rather than a
  canonical ORDER BY. The view is a pure `Sidecar` derivation whose
  records column lists mirror the builders' declared deviations, so a
  compile run against it enumerates exactly the columns the truncated
  relations carry. No existing resident changes.
- **Streaming exporter** — contract promotion, no behavior change: the
  canonical total order `(event_sim_time, event_class, family,
  source_identity, record_id[, field tail])` and the global-`seq` definition,
  today realized by the streaming engine per content (a shipped stream never
  merges record and membership events), become seam-owned guarantees that
  streaming's per-content output conforms to by construction — exactly under
  full-set selections. Two scoped subset divergences, one per fold:
  streaming invokes the membership fold over its config-selected `fields`,
  so a field-subset config's intra-instant tail spans the subset (agreement
  up to intra-instant, same-class, same-owner ties); and it invokes the
  record fold over its config-selected `properties`, so a tracked-subset
  config's `u` row set — and therefore its `seq` numbering — spans the
  subset's change points, where the seam's row set is selection-invariant
  by design (§ The atom selection surface, the full-set invocation
  rule). Re-seaming the `stream` verb
  onto the playback head is explicitly deferred (ratified relationship claim C:
  re-seam when next materially touched, byte-identical output as the bar —
  which for a subset config includes reproducing its subset row set and
  tail, an invocation of the folds the head's `events` deliberately does
  not expose).
- **Anchor** — a new consumer, contract unchanged: tier 1 renders wallclock
  from the resolved `EffectiveAnchor` in the absolute-frame Python rule
  (offset-bearing ISO-8601), the same computation the streaming engine performs
  for `ts`; tier-2 values keep their mode's shipped rendering (the
  `render_anchor_timestamp_expr` SQL surface) because every tier-2 value is
  its full-export value.
- **Reader** — a new consumer, contract unchanged: `open_emit`, the sidecar
  accessors (`subtype_values`, `columns`, `pinned_ids`, `temporal_class`),
  the faithful records relation (population restriction), and the columnar
  `query_arrow` surface. Tier-2 `state` additionally composes the public
  `Emit` constructor to present the truncated sidecar view over the
  already-open connection — a composition by a new consumer, not a reader
  change. The view shares the caller's connection and the seam never
  closes it: the caller owns the emit's lifetime, and closing the view
  would close the shared connection.

## What Doesn't Change

- **The reader's contract** — version gate, sidecar accessors, faithful
  builders, both read surfaces. Playback adds no reader capability.
- **The five existing derivations** — signatures, canonical columns, and
  ORDER BY contracts of versioned-intervals, row-state-events,
  membership-events, state-at, and reference-resolution are untouched. The
  seam composes them; it does not modify them.
- **Every shipped verb, byte for byte — with two declared changes.**
  `validate`, `export` (dimensional + source), `stream`, `mixer`, `corrupt`,
  `init`, and the incremental flags produce identical output on every input
  they accept today, with exactly two declared changes. The unlock: a full
  `export` of a `change_delivery: snapshot` shape — refused today, so it
  has no bytes to preserve — becomes legal, reconstructing at the tape's
  end (§ Shaped state). The reservation: the presentation-name posture
  (§ Affected Subsystems) — a config that names an output column
  `last_mutation_sim_time` (a dimensional author-named column, a source
  `rename` target — both accepted today) goes from accepted to refused at
  load; no output a shipped config delivers under any other name changes
  by a byte. No re-seam happens in this change.
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
deactivates it — `active` false, `deactivated_at` the event's
`event_sim_time` (the event's `after` is `None`; the deactivation instant is
the event key itself), a `join` adds a containment row, a `leave` removes one
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
| `RecordAtomSelection(kind, sub_types=())` | the whole kind — no discriminator filter; the only form for a non-sub-typed kind |
| `RecordAtomSelection(kind, sub_types=("doctor", "nurse"))` | the named sub-type populations only |
| `RecordAtomSelection(..., properties=())` | identity + lifecycle only, no `prop__` columns |
| `RecordAtomSelection(..., properties=None)` | every selectable property — the kind's full `tracked` + `constant` set plus the exempt sub-typed discriminator whatever its class, resolved against the sidecar at open (a non-exempt `slice_only` column sits outside the selectable domain, so the full set never includes one) |
| `RecordAtomSelection(..., record_ids=frozenset({...}))` | instance axis: restrict to the named record ids (pins are the canonical source — the caller feeds `sidecar.pinned_ids`) |
| `RecordAtomSelection(..., record_ids=None)` | no instance restriction |
| `MembershipAtomSelection(owner_kind, owner_sub_types, property_name, fields, owner_record_ids)` | one membership table, optionally restricted to owner sub-type populations and owner instances |

Population restriction is one mechanism applied uniformly: the in-scope
record ids for an atom set are the record spine's ids whose discriminator
`prop__<kind>_type` is among the named sub-type values when `sub_types`
names any (composing the faithful records relation with a discriminator
predicate; whole-kind selection applies no discriminator predicate),
intersected with `record_ids` when given. The restriction is applied as an outer row filter
over each fold's canonical relation, re-imposing the fold's declared ORDER BY
— pure row selection, never recomputation, so every surviving value equals its
unrestricted value. The discriminator is read from the record spine's current value as a
classification — the shipped streaming routing surface's convention for its
Layer A `route_table`. This is a declared convention, not an as-of-T
derivation: the contract does not pin the discriminator's `temporal_class`,
and the seam stamps the spine's current value at every T regardless — the
classification carve-out invariant 5 states.

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
emits the data as it is. One drifted-tape case completes the rules:
`schema_drift` can drop the discriminator column while `record_roles`
still declares the sub-type domain — presence is registry-driven, the
read is a column. When the sidecar does not declare `prop__<kind>_type`
on a sub-typed kind, the stamp is `NULL` on every record (the table
genuinely lacks the classification), whole-kind selection — no
predicate — plays everything so stamped, and a selection *naming*
`sub_types` fails resolvability at open, exactly as naming a dropped
property does: the predicate needs a column the tape lacks
(`SubTypesDeclared`). The owner side mirrors it: an undeclared owner
discriminator stamps `owner_sub_type` `NULL` and fails a named
`owner_sub_types` at open.

Property and field selection is **column projection only**: it narrows
after-images and snapshot columns, never the event row set. Each axis has a
full-set form: `properties=None` / `fields=None` means every selectable
column — the kind's `tracked` + `constant` properties, the table's full
element-schema field set — resolved against the sidecar at open into the
head's effective set (every later "selected" means the resolved set); the
empty tuple means identity only; a named tuple is validated per Validation
Rules. The effective set is *ordered* — sidecar declaration order,
whichever form named it (a selection is a set; the answer's column order
is canonical, never an echo of the caller's tuple order). A `u` whose
coincident changes touch only unselected properties still plays (its
after-image then equals its predecessor's on the selected columns), so `seq`
is invariant under `properties` / `fields` — only the population axes (the
atom set, `sub_types`, `record_ids` / `owner_record_ids`) change the in-scope
stream. Normative mechanism, both event folds: the shipped record-event fold
derives its event row set from the *history-tracked* properties it is
invoked with (a constant property rides after-images as a current-value
column and contributes no events), so the seam always invokes it over the
kind's **full** tracked + constant property set — plus the exempt
discriminator whatever its class, which as an untracked column rides as a
current-value classification and contributes no events, leaving the row
set and `seq` untouched — and applies `properties` as
column projection afterwards — invoking the fold over a tracked subset
would silently change the row set and `seq`. The
membership-events fold is invoked the same way for a different reason: its
row set is field-independent, but its declared ORDER BY tail — the canonical
order's field-value tail — spans exactly the fields it is invoked with, so
the seam always invokes it over the table's **full** element-schema field
set and applies `fields` as column projection afterwards, making the tail
(and therefore `seq`) selection-independent by construction. (The two
state folds — state-at and membership-state-at — carry no event order and
their row sets are property- and field-independent; both are invoked over
the selected properties / fields directly.)

Property selection is additionally gated by the contract's point-in-time
dispatch (`temporal_class`, read per column from the sidecar): a `properties`
entry naming a **non-exempt** `slice_only` column fails at open with
`PlaybackError` — the shipped export-wide predicate, applied verbatim
(streaming's own selection rule refuses exactly the same set). The exempt
sub-typed discriminator is selectable whatever its class — the carve-out is
surface-total by the shipped policy invariant — and rides answers as a
classification at its current spine value, the same convention as the
`sub_type` stamp. Every
playback answer presents values at an event time or a horizon, and the
contract forbids presenting a `slice_only` column as an as-of-T value — the
value at T is unknowable; these are simulation-internal mechanism columns
whose history is deliberately not captured, not data-domain values (the
export-wide refuse posture: no exporter projects one, `init` never proposes
one). `constant` and `tracked` properties are both selectable. Membership
`fields` carry no temporal class (interval-constant by contract) and are
ungated.

Two consequences of the atom grammar:

- **Answers are stamped with atom identity.** Every `PlaybackEvent` carries
  its `RecordAtom` or `MembershipAtom` (per-record sub-type read verbatim
  from the spine); snapshot record tables carry a `sub_type` column (`NULL`
  when the kind is not sub-typed, the discriminator cell is `NULL`, or the
  discriminator column is undeclared — a drifted tape) and
  membership tables an `owner_sub_type` column (`NULL` when the owner kind is
  not sub-typed, the owner row is an orphan, the owner's discriminator
  cell is `NULL`, or its column is undeclared). Callers partition by atom without re-deriving anything;
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

> `(event_sim_time ASC, event_class ASC, family ASC, source_identity ASC, record_id ASC[, field-value tail])`

where `event_class` is one shared integer domain across both event families
(`c`=0 < `u`=1 < `d`=2; `join`=0 < `leave`=1), `family` ranks record events
before membership events (`record`=0 < `membership`=1), and `source_identity`
is compared within one family only: the kind for record events, the
`(owner_kind, property)` pair componentwise for membership events. The family
rank is what keeps the comparison total on every contract-legal tape: a kind
name may itself contain `__`, so no string-flattening of the membership
identity could be collision-free against a kind. Two intra-instant
consequences fall out: an owner's `c` precedes its coincident `join` (class
tie, family tie-break), and a `leave` precedes its owner's coincident `d`
(class 1 < 2) — containment drains before the owner deactivates.

This order is **seam-defined**. Within each family it is the order the
streaming engine realizes for its stream — a shipped stream is
single-content and never merges the two families — with two scoped
subset divergences, one per fold. Streaming invokes the membership fold
over its config-selected `fields`, so its intra-instant field tail spans
that subset, not the full element schema: order agreement is up to
intra-instant, same-class, same-owner ties. And it invokes the record
fold over its config-selected `properties`, so a tracked-subset config
emits only the subset's change points as `u` events: the same order over
a smaller row set — and therefore different `seq` numbering — than the
seam's selection-invariant stream. Streaming conforms by construction
exactly when its selection spans the full tracked property set (record
content) / the full element-schema field set (membership content); the
cross-family interleave is new at the seam. The k-way merge
semantics are unchanged: per-source folds arrive pre-sorted,
`(family, source_identity)` makes the inter-stream tie-break deterministic,
and field tails are never compared across folds — each membership fold's
tail spans its table's full element-schema field set, selection-independent
(§ The atom selection surface).

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
at the seam). Outstanding answers are **independently pullable** — a stated
guarantee, not an accident: pulling one lazy answer never invalidates
another on the same open emit, so `seek`'s two halves interleave freely, a
snapshot table may materialize mid-iteration, and two heads over one emit
do not contend (the seam realizes each pull over its own cursor on the
caller's connection, never assuming exclusive use — the consistency
algebra is worthless if exercising one side of it invalidates the other).
Event content is the shipped fold semantics unchanged:

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
resolves, the absolute instant `start_instant(UTC) + event_sim_time`
projected into `anchor.timezone` as an offset-bearing ISO-8601 `str` — with
streaming's shipped precision rule inherited verbatim: the ns offset is
truncated to whole microseconds before projection (Python datetime
precision), so the seam's `ts` is byte-identical to streaming's for the
same instant and anchor. When no anchor resolves, the raw `event_sim_time`
`int`. Never a naive local timestamp, never `now()`. Snapshot `_ts`
siblings render through this same rule — invariant 7 is byte identity, so
the truncation is contract, not implementation detail.

### Snapshot

`snapshot(at_sim_time=T)` returns a lazy `PlaybackSnapshot`; each table
materializes on first access through the columnar surface (`pyarrow.Table`,
typed even at zero rows). Two table families:

| Family | One table per | Contents |
|---|---|---|
| Record state | selected kind | the state-at fold at horizon `T + 1`, restricted to the selection's population and instances: one row per in-scope record with `created_sim_time ≤ T`; `active` / `deactivated_at` horizon-rendered; selected `prop__` columns as-of T (`tracked`) or current-value (`constant` — the contract declares the current value valid at every T); a `slice_only` property is never present (unselectable — § The atom selection surface); plus the fold's own `presentation_id` column when the kind carries one, and a `sub_type` stamp column composed from the spine by the seam (the fold's canonical columns untouched) |
| Membership containment | selected membership table | the new membership-state-at fold at horizon `T + 1`, restricted to in-scope owners: one row per interval containing T; plus an `owner_sub_type` stamp column |

A record created after T is absent — not present-with-nulls. A membership
interval contains T iff `joined_sim_time ≤ T` and (`left_sim_time` is `NULL`
or `left_sim_time > T`); a zero-width interval (`joined = left`) contains no
T, consistent with applying its coincident `join` then `leave` in event-class
order.

**Wallclock siblings.** When the head's anchor resolves, each raw-ns lifecycle
column on a snapshot table — `created_sim_time`, `deactivated_at`,
`joined_sim_time` — gains a sibling `<name>_ts` column: the offset-bearing
ISO-8601 rendering of the same instant through the tier-1 rule (§ The event
stream — microsecond truncation included), `NULL` where the raw value is
`NULL`.
When no anchor resolves, no sibling columns exist. The raw ns columns are
always present; ordering and the consistency algebra always key on raw ns.
One instant renders byte-identically as an event `ts` and as a snapshot
`_ts` value.

**Column order is contract.** Each snapshot table is the composed fold's
canonical relation verbatim — its declared column order, with `prop__` /
field columns in the order the seam invokes the fold with: the effective
set's sidecar declaration order (§ The atom selection surface) — followed
by the seam-appended columns, in this order: the stamp (`sub_type` /
`owner_sub_type`), then the `_ts` siblings in their raw columns' order
(`created_sim_time_ts`, `deactivated_at_ts`; `joined_sim_time_ts`). Event
after-images follow the same rule in dict-insertion order: the canonical
column-order producers' order verbatim.

**Presentation-property columns.** Beyond `presentation_id`, presentation
columns appear in no tier-1 answer: the composed folds' canonical relations
carry `presentation_id` only (their shipped contracts), and `properties`
names `prop__` payload columns alone. (Tier 2 is different: the truncated
records relation presents presentation-property columns per their temporal
class — § Shaped state — because the modes' faithful builders enumerate
them.)

**Identity columns.** The records-column taxonomy's identity posture holds at
the seam: `record_index` and `ref_index__<name>` appear in no playback answer
— `properties` names `prop__` payload columns only, and every composed fold's
canonical relation carries no identity family beyond `record_id` (and
`presentation_id` where declared). Load-bearing for any future ask that
surfaces the index at a horizon: `ref_index__<name>` is a point-in-time key
stamped at the emitted slice, so such an ask must **re-derive** it from the
reconstructed `prop__<name>` via the target kind's `record_index` — carrying
the slice `ref_index__` beside an as-of-T reference value would pair two
different instants.

### The membership-state-at derivation (new resident)

`build_membership_state_at_sql(sidecar, fork_path, owner_kind, property_name,
fields, horizon_ns)` — the point-in-time counterpart to membership-events,
under the six-rule layer contract:

- **Row set.** One row per interval of the one
  `membership__<owner_kind>__<property_name>` table (discovered through the
  sidecar, filtered to `fork_path`) satisfying
  `joined_sim_time < horizon_ns AND (left_sim_time IS NULL OR left_sim_time >= horizon_ns)`.
- **Canonical columns** (`MEMBERSHIP_STATE_AT_COLUMNS`): `record_id` (the
  owner), `joined_sim_time` (raw ns `BIGINT`), then each selected
  element-schema field's column shape in `resolve_membership_columns` order —
  `elem__<f>` for a scalar field, the `member__<f>__kind` /
  `member__<f>__id` pair for a reference field — each cast to
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
  at all — every horizon fails one of the two conjuncts — and answers
  deterministically; overlapping
  duplicate intervals yield one row each. Faithfully wrong, never an error.
- **Errors.** A missing membership table raises `TableNotFoundError`; an
  unresolvable field raises `ExportError` — the layer's cause-based taxonomy.

### Shaped window (tier 2)

`window(start_sim_time, end_sim_time)` returns one relation per output table
the shape declares, each tagged with its **delivery class** so a caller lands
it correctly (`append`: merge rows in; `snapshot`: replace the table). The
classes are static per table class / genre and declared at open through
`tables()`, so a caller provisions sinks before the first ask. The
content contract is the shipped per-table-class / per-genre window membership,
promoted verbatim — stateless, relations out, caller owns the frontier. All
membership tests run on raw sim-time ns, half-open `[start, end)`.

Dimensional shape:

| Table class | Window key (ns) | Per-window content | Delivery |
|---|---|---|---|
| Fact, records grain | `last_mutation_sim_time` | rows whose key ∈ window — final on arrival, never revised | append |
| Fact, history_point grain | `sim_time` | rows whose key ∈ window | append |
| Dim, SCD-2 | the version's `valid_from` change point | version rows born in the window, as the physical projection: declared columns minus `valid_to` slots plus the raw `__valid_from_ns` bookkeeping column — `valid_to` is never materialized; closing versions is the consumer's merge (or a view above the seam) | append |
| Dim, type-1 | — | full current-state table every window (columns gated `temporal_class: constant`; row set is the end-of-run population — the shipped carve-out) | snapshot |
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
`fk` hops, raw-window-key ordinals, temporally constant slice reads and dim
filters)
gate the config so selecting-not-recomputing is temporally honest — they
apply to tier-2 `window` exactly as they apply to the incremental driver
today, validated on the first `window` ask (the shipped driver's
window-gated pass; a shape used only for `state` never runs them). The
windowed-grain rule is among them and is **whole-shape, as shipped**: a
shape declaring a history_interval or membership grain table cannot
`window()` at all — the first ask fails naming the table (the shipped
config-level rejection, passed through), never a silent per-table skip.
`tables()` marks the offender `window_delivery=None` at open, so a caller
learns before asking which table its config must drop to make the shape
windowable.

### Shaped state (tier 2): the truncated tape

`state(at_sim_time=T)` returns the shape's tables **as if the emit's slice
ended at T** — implemented literally, not per class: the mode's full-export
compile runs over the **truncated tape**, the derivations-owned
presentation of the emit as the producer would have emitted it sliced at T.
Delivery is `snapshot` on every table.

The truncated tape — one relation per base table the compile reads, and
the sidecar view that describes them:

| Base table | Truncated relation |
|---|---|
| `history` | rows with `sim_time ≤ T` — a pure filter |
| `membership__<K>__<p>` | intervals with `joined_sim_time ≤ T`; `left_sim_time` masked `NULL` when `> T` — an interval still open at T, exactly as a slice-at-T emit renders it |
| `records__<kind>` | one row per record with `created_sim_time ≤ T`: identity columns and `record_index` verbatim (`record_index` is slice-stable by contract); `active` / `deactivated_at` horizon-rendered; `constant` properties verbatim; `tracked` properties reconstructed as of T and TRY_CAST back to their sidecar-declared types (the codec round-trip; `NULL` where a corrupted history value does not parse as the declared type — a cast never errors, § Permissive playback); presentation-property columns by the same per-class rule — the contract pins them `history_tracked: true` and class `tracked` or `constant`, never `slice_only`, and a `tracked` presentation property's re-mints are appended to `history`, so it reconstructs as of T exactly as a `tracked` `prop__` does (`constant` verbatim; the view never drops one); each `ref_index__<name>` re-derived from the reconstructed `prop__<name>` via the target kind's *truncated* spine (§ One consistent truncated world; the § Snapshot identity-columns rule, applied — an unresolvable value re-derives `NULL`); **`last_mutation_sim_time` presented as the recorded trail** — `greatest(created_sim_time, the record's latest tracked history instant ≤ T, deactivated_at when ≤ T)`, the last *recorded* content change (§ Shaped state, The recorded trail); **`slice_only` columns absent** (a sub-typed kind's `slice_only` discriminator `prop__<kind>_type` excepted — carried verbatim as the classification column the shipped routing / sub-type-split convention reads, under invariant 5's carve-out; a `tracked` or `constant` discriminator needs no exception, following its class's own rule) — the declared deviations from the physical shape, the column-list ones mirrored by the truncated sidecar view |

**One consistent truncated world.** The truncated relations are mutually
consistent by definition: wherever a truncated relation's own recipe reads a
base table *other than the one it presents* — the records builder's tracked
reconstruction reads `history`; its `ref_index__` re-derivation reads the
target kind's spine — the read carries truncated-world semantics: its
result must equal a read of that table's truncated presentation, never of
the physical table. For `history` the two coincide (the truncation filter
is idempotent); the rule is load-bearing exactly for the `ref_index__`
re-derivation, where the physical spine would mint an index from a record
created after T — future base state, an invariant-5 leak, and a dangling
index inside the delivered dataset besides. A reconstructed reference value
that resolves to no truncated spine row — a dangling or mispointed value,
or one naming a record created after T on a shifted tape — re-derives
`ref_index__<name>` as `NULL` beside the verbatim non-NULL reference: a
deliberate, faithful break of the physical pair-agreement (the pair agrees
on every temporally-intact tape; where it disagrees, the defect manifest is
the answer key). A builder's read of the base table it *presents* is by
definition the physical table — the source being truncated. Realization:
§ The compile indirection, binding rules.

**The truncated sidecar view.** The truncated tape carries its own
sidecar: a pure `Sidecar` derivation identical to the physical one except
that each `records__<kind>` entry's column list drops exactly the columns
its truncated relation lacks. The `state` compile runs against an emit
view — the same open connection presenting this sidecar — so the faithful
builders, which enumerate their column lists from the sidecar they are
handed, name exactly the columns the replacing relations carry, and the
compiled plan binds against the truncated tape by construction.
Column-list agreement between the view and the truncated relations is a
stated invariant of the surface. So is this: every sidecar field the view
does not rewrite stays physical — the branch's slice bound included — so
no compile path under `state` may read a slice bound from the sidecar;
the truncated world's end is defined by its data, never by metadata (the
same rule the snapshot-delivery redefinition below states for its own
reconstruction). Everything else — open-time validation,
`tables()`, `window()`, and both ask gates — reads the physical sidecar;
only the `state` compile sees the view.

Because the compile is the shipped full-export compile, as-of-T correctness
is by construction — no per-class rules, no recomputation gates: type-1 dims
and reference tables read as-of-T values from the truncated records relation
(equal to current values exactly for `constant` columns); SCD-2's `LEAD` over
truncated `history` yields change points ≤ T with the latest version open and
`valid_to` materialized per the full-export rules; `history_interval`'s
`lead_sim_time` and the junction's `left_at` mask themselves (their `LEAD` /
render runs over truncated rows); records-grain facts and transaction tables
reconstruct as of T — fk hops join as-of-T reference values into as-of-T
targets, lookups read as-of-T attributes; `change_delivery: snapshot`
reconstructs at horizon `T + 1`. The windowed business rules are not
involved: they exist to make select-not-recompute temporally honest, and
`state` recomputes everything.

**One mode semantic, redefined to make this total.** The shipped source
compile refuses horizon-less snapshot delivery (`change_delivery: snapshot`
with no window) because a one-shot full export names no instant to snapshot
at. This change defines it instead: with no window, snapshot delivery
reconstructs at the tape's end — the state-at reconstruction with every
event applied, where "every event" spans everything the fold keys on its
horizon: `history` rows *and* the spine's lifecycle instants
(`created_sim_time`, `deactivated_at`), which need not appear in
`history` — a deactivation is a spine fact, not a history row. The tape's
end is a property of the data, never of metadata — realized
*structurally*, not computed: the snapshot render composes
`build_state_at_end_sql`, the state-at resident's additive second entry
point (§ Interface Contracts, The new derivations), whose SQL carries no
horizon parameter and no horizon predicate — no `created ≤ T` row filter,
`active` / `deactivated_at` from the spine verbatim, each tracked
property at its latest recorded `history` value. The end is whatever the
composed relations hold, so a horizon never exists to be computed, and
the compile must not read a slice bound from the sidecar, which the
truncated tape does not re-present. (The equivalence is the testable
property: the end-of-tape relation equals the horizoned builder at any
`horizon_ns` strictly beyond every event and lifecycle instant — a
horizon cleared against `history` alone is wrong, rendering a
later-deactivated record active.) The redefinition is additive in effect
(the refusal was the
only path that produced nothing, so no shipped byte changes) and
deliberately ungated: a plain full export of such a shape becomes legal,
yielding end-of-run state tables, and over the truncated tape the end *is*
T — the truncated relations bound the same horizon-free SQL — so
`state(T)` yields the reconstruction at horizon `T + 1` with the mode
still never seeing a horizon — one rule, both callers, and the bridging
theorem holds for the class with no carve-out.

Per-class consequences (derived from the one definition above, stated for
testability):

| Table class / genre | `state(T)` |
|---|---|
| Dim, SCD-2 | version rows with change point ≤ T; the latest version open |
| Dim, type-1 / reference genre | one row per record created ≤ T; `constant` columns current (valid at every T), `tracked` columns as of T |
| Fact, records grain / transaction genre | one row per record created ≤ T, values as of T — not end-of-run |
| Fact, history_point grain / change-log genre (`changelog`) | event rows with key ≤ T |
| Fact, history_interval grain | interval rows with `sim_time ≤ T`; `lead_sim_time` `NULL` past the horizon |
| Junction / membership grain | interval rows with `joined_sim_time ≤ T`; `left_at` `NULL` when the leave is after T |
| change-log genre (`snapshot` delivery) | the state-at reconstruction at horizon `T + 1` |

**The recorded trail.** One records column cannot be read back at a past
T: the physical `last_mutation_sim_time` advances on *every* content
event, and the contract binds it in one direction only — a high-water
mark over the record's lifecycle — so a `slice_only` write may advance it
without leaving history. The truncated records relation therefore
presents the column as the **recorded trail**:
`greatest(created_sim_time, the record's latest tracked history instant
≤ T, deactivated_at when ≤ T)` — the last *recorded* content change,
computable at every T from truncated-world reads alone. Membership
activity is deliberately not a component: a membership interval is its
own fact with its own timestamps (`joined_sim_time` / `left_sim_time`),
leading or lagging the owner row exactly as tables do in a real estate of
services — its timeline is delivered by the membership tables, never
folded into the owner's. By the high-water clause the trail never exceeds
the physical value; equality is producer behavior, not contract — a
producer whose every advance is a recorded event never diverges (the
reference producer holds equality on every record), and where one
advanced the column invisibly, the trail is the defensible delivered
value and the divergence is one-sided. Every shipped value channel then
works over the truncated tape unchanged: the source `updated_at` default,
a dimensional records-grain `from:` / `correlation:` / `value_map` /
`derived: timestamp` source, and a records-grain `ordinal.order_by` all
read the presented column — honest at T by construction. (The shipped
`change_delivery: snapshot` render, which composes state-at, carries no
`updated_at` — unchanged.)

**The slice_only precondition.** A `slice_only` column never reaches any
shape's plan — projection or value-read alike. The export-wide policy's
always-on rules (the modes' own validation: the value-read refusal over
every config-referenced surface, the `lookup` constant-regate, the source
rename rule) refuse every such plan, and `open_shaped_playback` runs that
validation at open — so no openable shape reads a column the truncated
tape drops. The seam adds no gate of its own; the posture is inherited as
a precondition, and its owner stays the modes. Stated as an invariant of
the surface: **the truncated sidecar view drops only columns no openable
plan reads** — a consequence of the always-on refusal. A future grammar
surface that adds a records value-read extends the mode's always-on rule
(the posture's owner, per the policy invariant: a mode decides *how* to
enforce, never *whether*), never a seam gate. The carve-outs are
view-construction facts, not checks: the sub-type split's discriminator
read binds because the view *carries* the discriminator (the
classification column, the same current-value convention as the tier-1
stamps — invariant 5's carve-out), and `last_mutation_sim_time` reads
bind because the view presents the recorded trail. That is the domain
fact the surface encodes — `slice_only` is one temporal class in two
roles: sim-minted classification (the discriminator, written at creation
and read everywhere as a filing label — carried) and scenario-author
mechanism state (steering bookkeeping that is part of the domain at no
T — the reason for the export-wide refuse posture), distinguished
mechanically by the discriminator name rule the truncated surface
declares, never by per-column judgment. `window` on the same head is
separately gated by the shipped windowed business rules.

**The bridging theorem.** Truncation at the slice bound is the identity
presentation of the tape, so `state(T_slice)` is value-identical to the
shape's full export for every shape that opens (every openable plan binds
against the truncated tape — the slice_only precondition) — with one
declared condition: an
lmst-sourced value equals its full-export value exactly when the emit's
physical `last_mutation_sim_time` equals its recorded trail, which the
high-water clause makes the only possible direction of divergence and
which a producer whose every advance is a recorded event — the reference
producer — satisfies on every record. This is the
equation that makes `base` mode a thin renderer over shaped state (claim A)
and defines the incremental driver's re-seam bar (claim C). The interior-T
generalization is likewise a stated property: `state(T)` equals the shape's
full export over a *materialized* truncated emit — one whose tables are the
truncated relations written out physically, the trail column included — so
the virtual mechanism always has a dumb-but-obviously-correct oracle.

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
| `null_cells` / `mutate_cells` | values flow through as codec `VARCHAR` / `NULL` — the sub-type discriminator included: a resampled record plays as the sub-type its cell now names, out-of-domain / string dirt stamps verbatim (reachable via whole-kind selection), a nulled discriminator stamps `NULL`. Under tier-2 `state`, a tracked value whose corrupted history text does not parse as its column's declared type reconstructs `NULL` (TRY_CAST — a cast never errors; the manifest is the answer key) |
| `schema_drift` | reads follow the regenerated sidecar; a selection naming a dropped column fails resolvability at open — faithful, because the table genuinely lacks it. A dropped discriminator on a still-sub-typed kind stamps `sub_type` / `owner_sub_type` `NULL` on every record; naming `sub_types` / `owner_sub_types` against it fails at open (the predicate needs the column) |

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
| ask-scoped shape gates (the windowed business rules at `window`) | fail-fast on the first `window` call, config-and-sidecar-only — the windowed rules' existing errors pass through. `state` has no ask-scoped shape gate beyond bounds: every plan that opens binds against the truncated tape (the slice_only precondition) |
| source shape with `anchor=None` | error at `open_shaped_playback` — the source mode's mandatory-anchor rule, surfaced at open |
| `window` / `state` on an empty window / empty population | zero-row typed tables, every declared table present (the declared-but-empty rule) |
| upstream guard/reader errors | pass through unchanged (`ExportError` from the single-branch guard, `TableNotFoundError`, the reader's version-gate errors) |

### Invariants

1. **Pull-only.** No operation performs I/O until an answer is pulled;
   `open_playback` reads the sidecar only. The seam contains no clock reads,
   no sleeps, no sinks, no sessions — timing authority cannot exist here by
   construction. Outstanding lazy answers are independently pullable:
   pulling one never invalidates another on the same emit (§ The event
   stream).
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
   slice bound (the bridging theorem). Across tiers, a shaped change-log
   table over `[T1, T2)` and a tier-1 `events(T1, T2)` pull carry the same
   change set. Cross-paradigm *and* cross-shape consistency is a seam
   guarantee — scoped and stated per class, never implied where a class's
   delivery cannot support it.
5. **Faithful reshaping + temporal honesty, inherited per answer.** Every
   delivered value traces to a base value or a declared recoding; no value on
   an answer derives from base state later than the answer's time key, with
   two stated exceptions: `temporal_class: constant` sources — current values
   the contract declares valid at every T — and the discriminator
   classification reads: the `sub_type` / `owner_sub_type` stamps and the
   sub-type split, which read the spine discriminator's current value at
   every T (the routing surface's shipped convention; the contract does not
   pin the discriminator's temporal class). Dispatch is on `temporal_class`,
   the contract's point-in-time surface. A non-exempt `slice_only` source
   appears in no
   answer of either tier — refused at selection (tier 1) and by the modes'
   own validation before any export runs (tier 2, the contract-mandated
   refusal); the exempt discriminator appears only as a classification,
   whatever its class. `last_mutation_sim_time` appears under its own name in no
   answer of either tier — never selectable at tier 1 (not a `prop__`
   column), a reserved output name at tier 2 (the presentation-name
   posture) — while its value flows freely under presentation names; in a
   `state` answer the presented value is the recorded trail (§ Shaped
   state), the last recorded content change at T, honest by construction.
   A `state` plan never *reads* a `slice_only` column as a value either —
   the modes' always-on value-read refusal, inherited at open (the
   slice_only precondition), the discriminator classification reads
   excepted structurally (the view carries the discriminator); the seam
   never substitutes a slice value.
6. **Permissive totality.** Every operation is total over
   structurally-conformant input; semantic defects flow through unchanged.
7. **Rendered-instant agreement.** One absolute instant renders
   byte-identically wherever it appears — event `ts`, snapshot `_ts` — under
   one resolved anchor. A tier-1 guarantee: tier-2 values keep their mode's
   shipped full-export rendering, a different representation of the same
   resolved instant.
8. **Layer direction.** Tier 1 imports the reader, derivations, the anchor
   surface, and `errors` — never `exporters.*`, never `config`. Tier 2
   imports `config`, the modes' pure compile surfaces, the notice channel,
   the derivations
   truncated-tape surface, and the reader's `Emit` (the truncated emit-view
   composition). The chain tier 2 → modes → derivations → reader
   is acyclic by construction (tier 2's direct derivations edge adds no
   cycle), with tier 1 a sibling consumer of derivations; a mode never
   imports either tier.
9. **Bridging (a theorem, not a stipulation).** Truncation at the slice
   bound is the identity presentation of the tape, so `state(T_slice)`
   equals the shape's full export for every shape that opens —
   lmst-sourced values under the recorded-trail condition
   (§ Shaped state, The bridging theorem) —
   the seam is provably sufficient to re-write the shipped verbs on (the CLI
   is the seam's permanent proof of sufficiency).
10. **Inherited.** Version-gated input, sidecar-driven schema discovery,
    single-branch guard, no producer dependency.

## Configuration

None in this change. The seam is a Python library surface; its author-facing
YAML skins are its consumers (`base` mode — build slot #2 — and, per
relationship claim C, the existing envelopes when their verbs re-seam). No new
config model, no new CLI flag.

## Interface Contracts

### Selection and identity types

The selection pair carries the `Atom` infix deliberately: a shipped
config-level `MembershipSelection` (streaming's per-kind selection) already
exists, and one name never means two shapes — the playback pair is named
apart, symmetrically.

```python
@dataclass(frozen=True)
class RecordAtom:
    """One record population: a sub-type of a kind, or a whole non-sub-typed kind.

    sub_type is None when the kind declares no discriminator domain, when
    the record's discriminator cell is NULL (a corrupted tape), or when
    the sidecar does not declare the discriminator column (a drifted
    tape). On a corrupted tape it may hold an undeclared value verbatim —
    the stamp is data; the declared domain is only the selection
    vocabulary.
    """
    kind: str
    sub_type: str | None


@dataclass(frozen=True)
class MembershipAtom:
    """One membership population: an owner population's collection property.

    owner_sub_type is None when the owner kind declares no discriminator
    domain, when the owner has no spine row (a corrupted tape's orphan
    membership row — played verbatim, never dropped), or when the owner's
    discriminator cell is NULL or its column is undeclared (a drifted
    tape). May hold an undeclared value verbatim on a corrupted tape.
    """
    owner_kind: str
    owner_sub_type: str | None
    property_name: str


@dataclass(frozen=True)
class RecordAtomSelection:
    """Select record populations of one kind, with properties and instances.

    sub_types: declared discriminator values to include — a predicate over
        the spine discriminator; the empty tuple means the whole kind (no
        discriminator filter; the bare kind when not sub-typed). Non-empty is
        legal only for a sub-typed kind whose discriminator column the
        sidecar declares (the drifted-tape rule).
    properties: bare property names riding after-images and snapshot rows, of
        temporal class tracked or constant — a non-exempt slice_only
        property fails at open (the shipped export-wide predicate; the
        exempt sub-typed discriminator is selectable, any class); the
        empty tuple means
        identity + lifecycle only; None means the full selectable set —
        every tracked + constant property plus the exempt discriminator,
        resolved at open (never a non-exempt
        slice_only column). Projection only — never changes the event
        row set or seq.
    record_ids: the instance axis — restrict to these record ids; None means
        no instance restriction. Must be non-empty when given. Unknown ids
        select nothing (never an error).
    """
    kind: str
    sub_types: tuple[str, ...]
    properties: tuple[str, ...] | None
    record_ids: frozenset[str] | None


@dataclass(frozen=True)
class MembershipAtomSelection:
    """Select one membership table, with owner populations and instances.

    owner_sub_types: declared owner discriminator values to include — a spine
        predicate (an orphan owner matches no named value); empty tuple = all
        owners, orphans included. Non-empty is legal only for a sub-typed
        owner kind whose discriminator column the sidecar declares (the
        drifted-tape rule).
    fields: bare element-schema field names riding payloads and containment
        rows; empty tuple = owner identity only; None = the full
        element-schema field set, resolved at open. Projection only — never
        changes the event row set or seq.
    owner_record_ids: restrict to these owner ids; None = no restriction.
        Must be non-empty when given. Unknown ids select nothing.
    """
    owner_kind: str
    owner_sub_types: tuple[str, ...]
    property_name: str
    fields: tuple[str, ...] | None
    owner_record_ids: frozenset[str] | None


@dataclass(frozen=True)
class PlaybackSelection:
    """The head's full atom selection.

    At most one RecordAtomSelection per kind and one MembershipAtomSelection per
    (owner_kind, property_name); at least one selection overall.
    """
    records: tuple[RecordAtomSelection, ...]
    memberships: tuple[MembershipAtomSelection, ...]
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
            table / field, a duplicate property / field name, a slice_only
            property, sub_types / owner_sub_types on a non-sub-typed kind
            or against an undeclared discriminator column,
            an empty record_ids / owner_record_ids set).
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

        Columns: STATE_AT_COLUMNS (record_id; created_sim_time; active;
        deactivated_at), the fold's own presentation_id column when the kind
        carries one, a sub_type stamp (the spine
        discriminator verbatim, undeclared values included; NULL when the
        kind is not sub-typed, the cell is NULL, or the discriminator
        column is undeclared), one prop__<p> per
        selected property, and — when the head's anchor resolves — a
        <name>_ts sibling per raw-ns lifecycle column. Typed at zero rows.
        Column order is contract (§ Snapshot): the fold's canonical
        relation verbatim — properties in sidecar declaration order — then
        sub_type, then the _ts siblings in raw-column order.

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
        joined_sim_time; each selected field's column shape — scalar
        elem__<f> or the reference member__<f>__kind / member__<f>__id
        pair), an owner_sub_type
        stamp (verbatim; NULL when the owner kind is not sub-typed, the owner
        row is an orphan, its discriminator cell is NULL, or the
        discriminator column is undeclared), and — when the
        anchor resolves — joined_sim_time_ts. left_sim_time is never present.
        Typed at zero rows. Column order is contract (§ Snapshot): the
        fold's canonical relation verbatim — fields in sidecar
        element-schema order — then owner_sub_type, then joined_sim_time_ts.

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

    name: the shape's output table name, exactly as its full export names it
        (author-declared for a dimensional shape; genre-derived and
        `rename`-mapped for a source shape).
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


@dataclass(frozen=True)
class ShapedTableDecl:
    """One declared output table of the shape — knowable at open, no data read.

    name: the shape's output table name, exactly as its full export names it.
    window_delivery: the table's delivery class under window() — static per
        table class / genre (§ Shaped window) — or None for a table class
        the windowed-grain rule rejects (history_interval / membership
        grain). None is diagnostic, never a skip: the rule is whole-shape,
        so while any declared table carries None, window() refuses the
        whole shape on its first ask, naming the table; the decl tells the
        caller at open which table its config must drop to window this
        shape. state() is unaffected — every table of state() is delivered
        'snapshot' regardless. This is the only delivery fact a caller
        needs before its first ask (sink provisioning, DDL, topic setup).
    """
    name: str
    window_delivery: Literal["append", "snapshot"] | None


def open_shaped_playback(
    emit: Emit,
    config: ExportConfig,
    anchor: EffectiveAnchor | None,
    notice_sink: NoticeSink,
) -> ShapedPlayback:
    """Bind a shaped head to an open emit and a declared target shape.

    Runs the mode's full config validation at open (sidecar-only, no data
    reads) — a shape whose plan projects or value-reads a slice_only column
    is refused here by the mode's own always-on rules (the export-wide
    policy, inherited as a precondition), and an output column named
    last_mutation_sim_time by the mode's reserved output-name check (the
    presentation-name posture). The windowed business rules are ask-scoped —
    validated on the first window() call — so a shape legal for state()
    but not window() still opens. The shape is the config's mode + mode
    section + shared exporter features; the config's rebase block is not
    read (the caller resolves the anchor) and its incremental block is not
    read (cadence-boundary sequences are the caller's job — the seam speaks
    raw-ns bounds only).

    The head binds notice_sink for its lifetime and threads it to every
    mode compile it runs — the open validation and each window() / state()
    compile — so each ask's compile delivers its plan notices to the sink
    as emitted (an ask re-emits its compile's notices, the incremental
    drip rule). Tier 1 runs no mode compile and emits no notices.

    Args:
        emit: An open emit (version-gated by open_emit).
        config: The target shape — a validated ExportConfig (mode:
            dimensional or source; base extends the Literal when it lands).
        anchor: The resolved effective anchor, or None. The source mode's
            mandatory-anchor rule applies at open.
        notice_sink: Receiver for plan notices from every compile the head
            runs (required — the notice-channel contract; a caller that
            wants silence passes a discarding sink).

    Returns:
        A ShapedPlayback head bound to (emit, config, anchor, notice_sink).

    Raises:
        PlaybackError: A seam-level open gate fails (source shape with
            anchor=None).
        ExportError: The mode's own config validation fails or the
            single-branch guard trips (passed through unchanged).
    """


class ShapedPlayback:
    """A shaped tape head: the target shape's tables per window or as of T."""

    def tables(self) -> tuple[ShapedTableDecl, ...]:
        """The shape's declared output tables, in the shape's canonical
        order: config declaration order for a dimensional shape; the source
        mode's deterministic full-export enumeration order for a source
        shape.

        Returns:
            One ShapedTableDecl per table window() and state() will deliver,
            independent of data (the declared-but-empty rule) — name and
            static window delivery class, so a caller can provision sinks
            before any ask.
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
        relations included, in tables() order.

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

        The mode's full-export compile over the truncated tape (§ Shaped
        state); delivery is 'snapshot' on every table. state(T_slice) is
        value-identical to the shape's full export (the bridging theorem).

        Args:
            at_sim_time: The inclusive position T (ns); >= 0.

        Returns:
            One ShapedTable per declared output table, in tables() order.

        Raises:
            PlaybackError: at_sim_time < 0. No slice_only gate exists
                here: a plan projecting or value-reading a slice_only
                column cannot open (the modes' always-on refusal at
                open_shaped_playback — the slice_only precondition), so
                every openable plan binds against the truncated tape.
                last_mutation_sim_time reads bind against the recorded
                trail the view presents, honest at T.
        """
```

### The new derivations

```python
def build_state_at_end_sql(
    sidecar: Sidecar,
    fork_path: str,
    kind: str,
    properties: frozenset[str],
) -> str:
    """Build the canonical end-of-tape state SELECT for one kind.

    The state-at resident's additive second entry point: the same canonical
    relation as build_state_at_sql with no horizon — no created-time row
    filter (every record of the kind), active / deactivated_at from the
    spine verbatim, each selected tracked property at its latest recorded
    history value, constant properties at their current records value.
    Columns and declared ORDER BY are STATE_AT_COLUMNS exactly as the
    horizoned builder emits them. "The tape's end" is structural: the SQL
    carries no horizon predicate, so composing this relation over truncated
    base relations bounds it at the truncation position with no horizon
    ever computed. Equivalence contract: equal to build_state_at_sql at any
    horizon_ns strictly beyond every history and lifecycle instant of the
    composed relations. Total over structurally-conformant input.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind to reconstruct.
        properties: Selected bare property names; may be empty (identity +
            lifecycle only).

    Returns:
        A complete, deterministic SELECT producing STATE_AT_COLUMNS plus
        the selected property columns.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
        ExportError: A selected property resolves to no prop__ column.
    """


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
    ns) — plus each selected element-schema field's column shape (scalar
    elem__<f>, or the reference pair member__<f>__kind / member__<f>__id) in
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

### The truncated-tape surface (new derivations residents)

Three relation builders and one sidecar view presenting the emit as if its
slice ended at `at_sim_time` (inclusive). Each builder returns a complete
SELECT that replaces its base table inside a mode's full-export compile
(§ Shaped state); totality
over structurally-conformant input holds as for the folds. Cross-reads
inside a builder follow the one-consistent-truncated-world rule via inline
truncation predicates; a builder's read of the table it presents names the
physical table (§ The compile indirection, binding rules). No ORDER BY
contract — a replacing relation's order is imposed by the compile that reads
it.

```python
def build_truncated_history_sql(
    fork_path: str,
    at_sim_time: int,
) -> str:
    """The history table truncated at T.

    Rows with sim_time <= at_sim_time, filtered to fork_path; column shape
    verbatim (history is a fixed table).

    Args:
        fork_path: The sole branch, from require_single_branch.
        at_sim_time: The inclusive truncation position T (ns); >= 0.

    Returns:
        A complete SELECT with the history table's column shape.

    Raises:
        Nothing — history is a fixed table; there is no resolvability to
        check.
    """


def build_truncated_membership_sql(
    sidecar: Sidecar,
    fork_path: str,
    owner_kind: str,
    property_name: str,
    at_sim_time: int,
) -> str:
    """membership__<owner_kind>__<property_name> truncated at T.

    Intervals with joined_sim_time <= at_sim_time, filtered to fork_path;
    left_sim_time masked NULL when > at_sim_time (an interval still open at
    T, exactly as a slice-at-T emit renders it); every other column verbatim.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        owner_kind: The membership table's owner kind.
        property_name: The membership table's collection property.
        at_sim_time: The inclusive truncation position T (ns); >= 0.

    Returns:
        A complete SELECT with the membership table's column shape.

    Raises:
        TableNotFoundError: No membership__<owner_kind>__<property_name>
            table is in the sidecar.
    """


def build_truncated_records_sql(
    sidecar: Sidecar,
    fork_path: str,
    kind: str,
    at_sim_time: int,
) -> str:
    """records__<kind> reconstructed as of T.

    One row per record with created_sim_time <= at_sim_time, filtered to
    fork_path. Columns are the physical table's shape with the declared
    deviations. Slice_only columns are absent (the export-wide refuse
    posture; the modes' always-on rules refuse every plan that would
    project or value-read one, so no openable plan misses them — the
    slice_only precondition) — except a sub-typed kind's slice_only discriminator
    prop__<kind>_type, carried verbatim as the classification column the
    routing / sub-type-split convention reads (invariant 5's carve-out; a
    tracked or constant discriminator follows its class's own rule); the
    column-list deviation is mirrored by the truncated sidecar view.
    last_mutation_sim_time is presented as the recorded trail —
    greatest(created_sim_time, the record's latest tracked history
    sim_time <= at_sim_time, deactivated_at when <= at_sim_time): the last
    recorded content change at T, never the physical value (whose
    advances need not leave history — the contract binds it as a
    high-water mark only); membership activity is deliberately not a
    component (its timeline belongs to the membership tables). Otherwise:
    identity columns and record_index verbatim
    (record_index is slice-stable by contract); active / deactivated_at
    horizon-rendered; presentation_id verbatim; each prop__<p> of
    temporal_class constant verbatim, of class tracked reconstructed as of T
    and TRY_CAST back to the column's sidecar-declared type (the codec
    round-trip; NULL where a corrupted history value does not parse as the
    declared type — a cast never errors, the totality invariant); each
    presentation-property column by the same per-class rule (the contract
    pins the pair: history_tracked true, class tracked or constant, never
    slice_only; a tracked presentation property's re-mints are in history,
    so it reconstructs as a tracked prop__ does; constant verbatim); each
    ref_index__<name> re-derived from the reconstructed
    prop__<name> via the target kind's truncated spine (the
    one-consistent-truncated-world rule; the cross-read carries its inline
    truncation predicate — § The compile indirection): NULL beside a NULL
    reference, and NULL beside a verbatim non-NULL reference that resolves
    to no truncated spine row (dangling, mispointed, or naming a record
    created after T).

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind to reconstruct.
        at_sim_time: The inclusive truncation position T (ns); >= 0.

    Returns:
        A complete SELECT with the records table's column shape minus its
        slice_only columns (the discriminator carve-out excepted), the
        last_mutation_sim_time column presenting the recorded trail.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
    """


def build_truncated_sidecar(
    sidecar: Sidecar,
) -> Sidecar:
    """The truncated tape's sidecar view.

    Identical to the physical sidecar except that each records__<kind>
    table entry's column list drops every temporal_class slice_only
    column — a sub-typed kind's slice_only discriminator
    prop__<kind>_type excepted (the classification carve-out) — exactly
    the columns build_truncated_records_sql does not project
    (last_mutation_sim_time stays declared: the truncated relation
    presents it as the recorded trail). Every other table entry and every
    other sidecar field is unchanged — the branch's slice bound included,
    which is why no compile path under state may read a slice bound from
    the sidecar (a stated invariant of the compile indirection). Pure and
    T-independent: the dropped column set is a
    function of the declared schema, not of the truncation position.
    Column-list agreement with the relation builders is a stated invariant
    of the surface.

    Args:
        sidecar: The open emit's physical sidecar.

    Returns:
        A Sidecar describing the truncated tape; tier-2 state presents it
        over the already-open connection through the reader's public Emit
        composition (the truncated emit view).
    """
```

### The compile indirection (tier 2 `state`)

Each mode's pure compile surface gains one additive, time-agnostic
parameter — required, no default (the repo's contract rule; the
notice-sink precedent):

```python
base_relations: Mapping[str, str] | None
```

Physical base-table name (`history`, `records__<kind>`,
`membership__<K>__<p>`) → replacing relation (a complete SELECT). When
given, every base-table read in the compiled plan — the faithful-read
builders and the folds the mode composes — resolves through the mapping,
falling back to the physical name when unmapped. With `None`, compilation
is byte-identical to today; the full-export and windowed callers pass
`None` explicitly.
Tier-2 `state` builds the mapping with **one entry per base table the
sidecar declares** — `history`, every `records__<kind>`, every
`membership__<K>__<p>` — never just the shape's declared sources: fk-hop
target spines and lookup reads must resolve truncated too, so an unmapped
fallback to a physical base table is unreachable under `state` (an
unreferenced CTE costs nothing — the engine prunes it). It invokes the
mode's full-export compile with the mapping and against the truncated emit
view (§ Shaped state, The truncated sidecar view) — the compile's sidecar
input is the view, so every faithful builder enumerates exactly the
columns the replacing relations carry — and the mode never sees a horizon.

**Realization: name shadowing.** The mode applies the mapping by wrapping
its compiled query in one CTE per mapped name — `WITH history AS
(<replacing SELECT>), ... SELECT * FROM (<compiled query>)`; a wrap, not a
textual prefix, because a compiled query may already open with its own
`WITH`. Every read of a mapped name inside fold- and builder-authored SQL
then resolves to the replacing relation, with no signature change anywhere
below the mode (the folds and the reader stay untouched, as declared).
Name binding is contract, never an engine default — three rules:

- **A replacing relation's self-read binds physical.** Each replacing
  SELECT reads the base table it presents; inside its own CTE that read
  binds to the physical table (standard non-recursive `WITH` scoping — a
  CTE's own name is not in scope in its body). The implementation pins this
  binding with a test, so an engine upgrade cannot silently rebind it.
- **A replacing relation's cross-reads are binding-insensitive by
  construction.** A replacing SELECT's read of a *different* base table
  carries truncated-world semantics (§ One consistent truncated world). It
  never relies on sibling-CTE resolution — kinds may reference each other
  mutually, so sibling references could cycle: the builder inlines the
  truncation predicate on every cross-read (`sim_time <= T` on a `history`
  read, `created_sim_time <= T` on a spine read). Because every column such
  a read touches is verbatim under truncation (spine identity columns,
  `history` rows), the inlined predicate makes the read's result identical
  whether the engine binds the name to a sibling CTE or the physical table
  — the semantics hold under either binding.
- **The mode's own reads shadow totally.** Two existing conventions thereby
  become stated invariants of the compiled-SQL surface: base tables are
  always read as unqualified identifiers naming the physical table exactly
  — the shipped SQL quotes them (`FROM "history"`), which still resolves to
  a same-named CTE; schema-qualification would not, and is therefore barred
  — and no internal CTE alias may equal a physical base-table name (the
  underscore-prefixed alias convention, now load-bearing).

### Errors

```python
class PlaybackError(Exception):
    """A playback-seam contract violation: an unresolvable selection, an
    invalid ask argument, or a seam-level shape gate (the source-shape
    anchor requirement). Never raised for a data condition — semantic
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
| `RecordKindResolvable` | each `RecordAtomSelection.kind` has a `records__<kind>` table in the sidecar | `"unknown kind {kind!r}"` |
| `SubTypesDeclared` | each `sub_types` value is in `subtype_values(kind)`; `sub_types` non-empty only when the kind is sub-typed *and* the sidecar declares the discriminator column `prop__<kind>_type` (a drifted tape may lack it — the predicate needs the column); no duplicate values | `"kind {kind!r} declares no sub-type {value!r}"` / `"kind {kind!r} is not sub-typed"` / `"kind {kind!r} lacks its discriminator column"` |
| `PropertiesResolvable` | each `properties` name has a `prop__<name>` column on the kind; no duplicate names | `"kind {kind!r} has no property {name!r}"` / `"duplicate property {name!r}"` |
| `PropertiesNotSliceOnly` | no `properties` name resolves to a **non-exempt** `temporal_class: slice_only` column (`Sidecar.temporal_class`; exempt: `prop__<kind>_type` with non-empty `subtype_values(kind)` — the shipped export-wide carve-out, surface-total) — the contract's as-of-T refusal | `"property {name!r} on kind {kind!r} is slice_only — its value at T is unknowable"` |
| `MembershipResolvable` | each `(owner_kind, property_name)` resolves to a sidecar membership table | `"no membership table for {owner_kind!r}.{property_name!r}"` |
| `OwnerSubTypesDeclared` | each `owner_sub_types` value is in `subtype_values(owner_kind)`; `owner_sub_types` non-empty only when the owner kind is sub-typed *and* the sidecar declares its discriminator column (the drifted-tape rule, as `SubTypesDeclared`); no duplicate values | `"kind {owner_kind!r} declares no sub-type {value!r}"` / `"kind {owner_kind!r} is not sub-typed"` / `"kind {owner_kind!r} lacks its discriminator column"` |
| `MembershipFieldsResolvable` | each `fields` name resolves to exactly one column shape (scalar or reference) on the table; no duplicate names | `"membership {owner_kind!r}.{property_name!r} has no field {name!r}"` / `"duplicate field {name!r}"` |
| `AtomsUnique` | at most one `RecordAtomSelection` per kind; at most one `MembershipAtomSelection` per `(owner_kind, property_name)` | `"duplicate selection for {identity!r}"` |
| `InstanceSetNonEmpty` | `record_ids` / `owner_record_ids` is `None` or a non-empty frozenset | `"empty {record_ids|owner_record_ids} — pass None for no restriction"` (naming the offending field) |
| `AskBoundsValid` | `events`: bounds non-negative, `start <= end` when both given; `snapshot` / `seek`: `at_sim_time >= 0` | `"invalid event-time bound"` |

The naming rules (`PropertiesResolvable`, `PropertiesNotSliceOnly`,
`MembershipFieldsResolvable`) apply to named tuples; `None` — the full-set
form — resolves against the sidecar at open and cannot fail them. Unknown
record *ids* are deliberately not a rule — an id filter is a predicate, and
a corrupted tape may have deleted any id (see § Permissive playback).

Tier-2 rules — the first two applied by `open_shaped_playback`, the rest
ask-scoped (validated once, on the ask's first call):

| Rule | When | Checks | Error |
|---|---|---|---|
| `ShapedModeValid` | at open | the config passes its mode's full existing validation (dimensional plan rules, source plan/collision rules) | the mode's errors, passed through |
| `ShapedAnchorRequired` | at open | a source shape has a non-None anchor | `PlaybackError` |
| `ShapedWindowedRules` | first `window` ask | the shipped windowed business rules hold for the shape (immutable `fk` hops, raw-key ordinals, temporally constant slice reads / dim filters, no history_interval / membership grain under `window`) | the rules' existing errors, passed through |
| `AskBoundsValid` (shared) | every ask | `window`: bounds non-negative, `start <= end`; `state`: `at_sim_time >= 0` | `PlaybackError` |
