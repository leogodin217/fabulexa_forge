# Playback Seam

The caller-driven, deterministic library surface for driving an emit as a
tape — its event-time line replayed as media. It sits between the derivations
layer and the exporter modes and exposes **one API in two tiers over one
inclusive-T event-time line**: primitive playback (atom populations →
events / point-in-time state) below the modes, and shaped playback (a
declared target shape → its tables per window or as of T) above them. Both
tiers are pull-only, deterministic, permissive, and stateless; the caller
owns the emit's lifetime and the frontier. A **tape** is the emit under
playback — a repo-local name for the seam's view of it; the input remains the
emit, and the **truncated tape** (the emit presented as if its slice ended at
T) is how shaped `state` is realized.

**Source:** [`playback/`](../../src/fabulexa_forge/playback/),
[`tests/playback/`](../../tests/playback/). Public API:
[`playback/__init__.py`](../../src/fabulexa_forge/playback/__init__.py).

## Boundary

Two tiers with different layer heights under one package:

- **Tier 1 (primitive)** — `open_playback` / `Playback` / `PlaybackEvent` /
  `PlaybackSnapshot` / `PlaybackPosition`. Imports the reader, the derivations
  layer, the anchor surface, and `errors`. It never imports `exporters.*` or
  `config`.
- **Tier 2 (shaped)** — `open_shaped_playback` / `ShapedPlayback` /
  `ShapedTable`. Imports `config` (the shape envelope), the modes' pure
  compile surfaces, the notice channel ([`notices.md`](notices.md)), the
  derivations truncated-tape surface, and the reader's `Emit` (to compose the
  truncated emit view). It sits above the modes.

The dependency chain `tier 2 → modes → derivations → reader` is acyclic by
construction; tier 1 is a sibling consumer of derivations; no mode imports
either tier.

**Inputs.** An open `Emit`; an atom `PlaybackSelection` (tier 1) or a
validated `ExportConfig` shape (tier 2); a resolved `EffectiveAnchor | None`;
and, for tier 2, a required `NoticeSink`. **Outputs.** Lazy `PlaybackEvent`
iterators, `pyarrow` snapshot tables, and shaped relation tuples.
**Non-inputs.** Nothing at the seam paces, buffers, pushes, connects to a
sink, opens a session, or reads a clock — those are caller concerns above it
(the boundary razor). Semantic validation belongs to `validate`.

## Semantics

### Two tiers, one API

| Tier | Takes | Answers | Layer height |
|---|---|---|---|
| 1 — primitive | an atom selection | `events` / `snapshot` / `seek` | below the modes |
| 2 — shaped | a target shape (`ExportConfig`) | `window` / `state`, as the shape's tables | above the modes |

Both tiers share the inclusive-T event-time line, the anchor rendering rules,
permissive totality, and determinism. Tier 2's `open` resolves an anchor and
materializes over the head's connection exactly as the export driver does, so
it joins the reader's session-zone pin the same way
([`reader.md`](reader.md) § The session-zone pin); because `state` and
`window` bind an `ExportConfig` and reuse the modes' own compile and
validation surfaces directly, an elected temporal rendering and its business
rules (the anchor requirement included) flow through tier 2 with no
seam-side handling ([`temporal-elections.md`](temporal-elections.md)). Tier 1
renders instants Python-side and carries no elected rendering.
Selection/identity/event types and the
head signatures are the dataclasses in
[`selection.py`](../../src/fabulexa_forge/playback/selection.py),
[`events.py`](../../src/fabulexa_forge/playback/events.py),
[`head.py`](../../src/fabulexa_forge/playback/head.py),
[`snapshot.py`](../../src/fabulexa_forge/playback/snapshot.py), and
[`shaped.py`](../../src/fabulexa_forge/playback/shaped.py) — the code is the
schema.

### One event-time line, inclusive T

Every ask is keyed on raw sim-time ns. **Position T is inclusive**: it means
"every event with `event_sim_time ≤ T` has been applied." All bounds are
expressed through one half-open convention over integer ns: `events(T1, T2)`
is `T1 ≤ event_sim_time < T2`; `snapshot(T)` reconstructs at the state-at
exclusive horizon `T + 1`; `seek(T)` is `snapshot(T)` composed with
`events(T + 1, None)`. `events(None, None)` is the whole tape.

**The consistency algebra** (the headline guarantee, testable directly): for
any `0 ≤ T1 ≤ T2`, with `snapshot(−1)` the empty state,

> `snapshot(T2 − 1)` = `snapshot(T1 − 1)` ⊕ (every event of `events(T1, T2)`
> applied in `seq` order)

where ⊕ applies each op (a `c` inserts the after-image, a `u` replaces it, a
`d` deactivates, a `join` adds a containment row, a `leave` removes the row
matching `(record_id, payload)`). A snapshot at T, a window ending at `T + 1`,
and a stream advanced through T agree exactly. The algebra is **conditional**
on temporal/interval integrity: on a tape whose defect manifest declares
family-C or family-E breakage there is no single consistent world-state, so
replay and snapshot disagree exactly where the manifest says the data is
broken — the manifest is the answer key, not a seam defect. Because
`snapshot(T)` consumes every event stamped exactly T, coincident events at one
instant are never split across a snapshot boundary.

### The atom selection surface

Selection follows the sub-type atom principle. The atomic population is
`(kind, sub_type)`, presence-driven from the sidecar: a kind refines into
sub-types exactly when `Sidecar.subtype_values(kind)` is non-empty, and
degenerates to the bare kind otherwise; the membership atom is
`(owner_kind, owner_sub_type, property)` under the same rule. No playback code
keys on which kinds can sub-type — the sidecar is the only authority.

**Population restriction is one uniform mechanism.** In-scope record ids are
the spine's ids whose discriminator `prop__<kind>_type` is among the named
`sub_types` (whole-kind selection applies no discriminator predicate),
intersected with `record_ids` when given, applied as an outer row filter over
each fold's canonical relation with the fold's ORDER BY re-imposed — pure row
selection, never recomputation, so every surviving value equals its
unrestricted value.

**Sub-type stamping is verbatim; the declared domain is only the selection
vocabulary.** The stamp reads the spine discriminator's current value as a
classification (the streaming routing convention): a corrupted cell stamps
what it holds, an out-of-domain value stamps outside the vocabulary, a nulled
or undeclared discriminator stamps `NULL`. Named `sub_types` are a predicate
over declared values; whole-kind selection plays everything, dirt included.
The membership owner's sub-type composes the spine by **LEFT join**, so an
orphan owner (a `delete_rows` casualty) still plays, stamped `NULL`. A kind is
table identity — no corrupter can re-home a row — so kind-level atom identity
needs no such rule.

**Property/field selection is column projection only** — it narrows
after-images and snapshot columns, never the event row set, so `seq` is
invariant under `properties` / `fields`. This rests on a normative invocation
rule: the seam always invokes the record-event fold over the kind's **full**
tracked + constant property set (plus the exempt discriminator) and the
membership-events fold over the table's **full** element-schema field set, then
applies the caller's projection afterwards. Invoking a fold over a subset would
silently change its row set (record fold) or its ORDER-BY field tail
(membership fold) and therefore `seq`. The effective column set is ordered by
sidecar declaration, whichever selection form named it.

**Identity is projected too.** A record selection's optional `identity` tuple
governs which identity columns the published maps carry — the event `after`
map and the `record_state` snapshot table, coherently, because both are
presentations of the same selected population. `None` resolves to the full
available set — `record_id`, plus `presentation_id` when the kind mints one —
the seam's established absence convention (`None` = the full selectable set),
resolved at open into `ResolvedRecordSelection.identity`
([`selection.py`](../../src/fabulexa_forge/playback/selection.py)).
`record_id` is required in the published set — it is the event key, the
relation spine, and the seam's stated identity — so an empty `identity` is
refused, never read as "none". The admissible surfaces are `record_id` and
`presentation_id`: `record_index` is outside the tier-1 domain, because tier 1
sits below the modes and composes no election relations, so offering a surface
it cannot source would be an empty option — a caller wanting index identity
uses tier 2, which inherits the modes' identity rendering. A selected property
naming an identity surface is refused: identity is projected, never
property-selected. The projection is applied above the composed relation —
never threaded into the fold — so the event row set and `seq` are invariant
under `identity` exactly as under `properties`, and the typed
`PlaybackEvent.record_id` / `.presentation_id` fields are always populated
regardless of it (an unread typed field costs nothing; removing one would
conflate "no surrogate" with "suppressed"). Membership atom selections carry
no projection: their tier-1 payload is the owner's `record_id` only, and no
surrogate reaches them. The seam names the surfaces as string literals rather
than importing the config's `KeySurface` — the layer-direction invariant
(reader + derivations property helpers + stdlib only) is not spent on a shared
type.

A `properties` entry naming a **non-exempt** `slice_only` column is refused at
open: its value at T is unknowable, and the contract forbids presenting a
`slice_only` column as an as-of-T value. The sub-typed discriminator is
exempt — selectable whatever its class — and rides answers only as a
classification at its current spine value ([`slice-only.md`](slice-only.md)).

### Canonical total order and entry-point-invariant `seq`

The seam owns the canonical total order over all in-scope events:

> `(event_sim_time, event_class, family, source_identity, record_id[, field-value tail])`

`event_class` is one shared integer domain across both families
(`c`=0 < `u`=1 < `d`=2; `join`=0 < `leave`=1); `family` ranks record events
(0) before membership events (1); `source_identity` is compared within one
family only (the kind, or the `(owner_kind, property)` pair). The family rank
keeps the comparison total on every legal tape — a kind name may contain
`__`, so no string-flattening of a membership identity could be collision-free
against a kind. Two intra-instant consequences fall out: an owner's `c`
precedes its coincident `join`, and a `leave` precedes its owner's coincident
`d` (containment drains before the owner deactivates).

`seq` is the event's 1-based position in that order over the **whole in-scope
stream** — a pure function of `(tape, selection)`, never of where the head
entered. A head opened at a lower bound numbers its first event `1 + N`, where
`N` counts in-scope events strictly before the bound in canonical order (a
deterministic count, not a replay). Bounded and unbounded heads agree; `seek`
then iterate matches a full play byte-for-byte from `T + 1` onward.

Within each family this is the order the streaming engine realizes for a
single-content stream (see [`streaming.md`](streaming.md) § Cross-source merge
and global `seq`); the cross-family interleave is seam-new. `seq` is
per-selection, exactly as streaming's `seq` is per-config.

### The event stream

`events(start, end)` yields `PlaybackEvent`s lazily in canonical order;
nothing computes until the iterator is pulled. Outstanding lazy answers are
**independently pullable**: pulling one never invalidates another on the same
open emit (each pull runs over its own cursor on the caller's connection), so
`seek`'s two halves interleave freely and two heads over one emit do not
contend. Event content is exactly the shipped fold semantics — `c`/`u`/`d`
per row-state-events, `join`/`leave` per membership-events
([`derivations.md`](derivations.md)); after-image keys are the column-order
producers' names verbatim, the record `after` map carrying one entry per
published identity surface (the selection's `identity` — § The atom selection
surface) in sidecar column order ahead of the selected `prop__<p>` entries;
every value is codec `VARCHAR` or `None`. `ts`
renders per the anchor exactly as streaming renders it — offset-bearing
ISO-8601 with the same microsecond-truncation rule, or the raw
`event_sim_time` int when no anchor resolves; never a naive local timestamp,
never `now()`.

### Snapshot

`snapshot(T)` returns a lazy `PlaybackSnapshot`; each table materializes on
first access as a `pyarrow.Table`, typed even at zero rows. Record-state tables
are the state-at fold at horizon `T + 1`, membership-containment tables the
membership-state-at fold at the same horizon, each restricted to the
selection's population. A record created after T is absent, not
present-with-nulls; a zero-width membership interval (`joined = left`) contains
no T. **Wallclock siblings**: when the anchor resolves, each raw-ns lifecycle
column gains a `<name>_ts` sibling through the tier-1 rule; the raw ns columns
are always present and ordering always keys on them. **Column order is
contract**: the composed fold's canonical relation verbatim (properties/fields
in sidecar declaration order), then the seam-appended stamp
(`sub_type` / `owner_sub_type`), then the `_ts` siblings in their raw columns'
order. A `record_state` table carries one identity column per published
surface (the selection's `identity` — § The atom selection surface); an
unpublished identity column is absent, the surviving columns keeping the
canonical relation's order. Presentation-property and identity columns beyond
`presentation_id` / `record_id` appear in no tier-1 answer.

### Shaped window (tier 2)

`window(T1, T2)` returns one relation per output table the shape declares, each
tagged with its **delivery class** (`append` or `snapshot`) so a caller lands
it correctly. Classes are static per table class / render and knowable at open
through `tables()`, so a caller provisions sinks before the first ask. The
per-table-class / per-render window-membership contract is the incremental
driver's, promoted verbatim to seam contract — stateless, relations out,
caller owns the frontier (see [`incremental.md`](incremental.md) § Window
membership per table class and [`source.md`](source.md) § Incremental
composition). The window predicate is the outermost filter over the shape's
full-export relation: every emitted value is its full-export value; the window
selects rows, never recomputes them. The shipped windowed business rules gate
the config on the first `window` ask so selecting-not-recomputing is
temporally honest. The windowed-grain rule is whole-shape: a shape declaring a
`history_interval` or `membership` grain table cannot `window()` at all —
`tables()` marks the offender `window_delivery=None` at open so a caller learns
before asking which table it must drop.

### Shaped state (tier 2): the truncated tape

`state(T)` returns the shape's tables **as if the emit's slice ended at T** —
realized literally, not per class: the mode's full-export compile runs over the
**truncated tape**, the derivations-owned presentation of the emit sliced at T
(see [`derivations.md`](derivations.md) § The truncated-tape surface).
Delivery is `snapshot` on every table. Because the compile is the shipped
full-export compile, as-of-T correctness is by construction — no per-class
rules: type-1 dims read as-of-T values, SCD-2's `LEAD` over truncated `history`
yields change points ≤ T, records-grain facts reconstruct as of T, source's
state tables read the truncated records spine (current-at-T), and its event
log's folds range over exactly the events ≤ T.

**The compile indirection.** A `base_relations: Mapping[str, str]` mapping —
physical base-table name to a replacing relation — redirects a compiled
query's base reads. It has two equivalent realizations, one per mode shape:
dimensional's pure compile surface carries it as an additive, time-agnostic
parameter (`base_relations: Mapping[str, str] | None`, required, no default;
`None` compiles byte-identical to a full export, and the full-export and
windowed callers pass `None`); source's compile carries no such parameter —
the seam applies the mapping itself, post-compile, as a pure SQL rewrite over
the engine's plain specs (`apply_base_relations` in
[`exporters/base_relations.py`](../../src/fabulexa_forge/exporters/base_relations.py),
composed by `playback/shaped.py`). Tier-2 `state` builds the mapping with one
entry per base table the sidecar declares (fk-hop target spines and lookup
reads must resolve truncated too) and runs the compile against the truncated
emit view so every faithful builder enumerates exactly the columns the
replacing relations carry; the mode never sees a horizon.

**Realization: name shadowing** — a normative algorithm an independent
reimplementation must honor. The mapping wraps the compiled query in one CTE
per mapped name (`WITH history AS (<replacing SELECT>), ... SELECT * FROM
(<compiled query>)`), a wrap because a compiled query may open with its own
`WITH`. Three binding rules make it correct under DuckDB's binder:

1. **A replacing relation's self-read binds physical.** A bare unqualified
   self-read is a circular CTE reference, so each replacing SELECT
   schema-qualifies its self-read (`main.<table>`) to reach the physical table.
   A test pins this so an engine upgrade cannot silently rebind it.
2. **Cross-reads are binding-insensitive by construction.** A replacing
   SELECT's read of a *different* base table inlines its own truncation
   predicate (`sim_time ≤ T`, `created_sim_time ≤ T`); because every column
   such a read touches is verbatim under truncation, the result is identical
   whether the engine binds the name to a sibling CTE or the physical table —
   so mutually-referencing kinds cannot cycle.
3. **The mode's reads shadow totally.** Base tables are always read as
   unqualified quoted identifiers (`FROM "history"`) — schema-qualification is
   barred — and no internal CTE alias may equal a physical base-table name (the
   underscore-prefixed alias convention, load-bearing here).

**One consistent truncated world.** Wherever a truncated relation's recipe
reads a base table other than the one it presents, the read carries
truncated-world semantics — its result equals a read of that table's truncated
presentation, never the physical table. This is load-bearing for the
`ref_index__<name>` re-derivation: the physical spine would mint an index from
a record created after T (future base state and a dangling index in the
delivered dataset), so a reconstructed reference resolving to no truncated
spine row re-derives `ref_index__` as `NULL` beside the verbatim reference — a
faithful break of the physical pair-agreement where the manifest is the answer
key.

**The recorded trail.** The physical `last_mutation_sim_time` advances on every
content event and the contract binds it only as a high-water mark, so a
`slice_only` write may advance it without leaving history. The truncated records
relation therefore presents it as the **recorded trail** —
`greatest(created_sim_time, the latest tracked history instant ≤ T,
deactivated_at when ≤ T)`, the last *recorded* content change, computable at
every T. Membership activity is deliberately not a component: a membership
interval is its own fact on its own tables. The trail never exceeds the
physical value; equality is producer behavior (the reference producer holds it
on every record). Every shipped value channel that reads
`last_mutation_sim_time` — the source `updated_at` default, a dimensional
records-grain source, a records-grain `ordinal.order_by` — works over the
presented column, honest at T.

**The bridging theorem.** Truncation at the slice bound is the identity
presentation of the tape, so `state(T_slice)` is value-identical to the shape's
full export for every shape that opens — an lmst-sourced value under the
recorded-trail condition (the only possible direction of divergence). This is
what makes `base` mode a thin renderer over shaped state and defines the
incremental driver's re-seam bar.

### Permissive playback

The seam validates nothing beyond what the reader gates plus its own
selection-resolvability checks; semantic conformance (C6/C7/C9–C12) is never
re-checked and defects flow through faithfully — which is what makes corrupted
tapes play identically to intact ones (the learning environment's answer keys
and record/replay stand on it). Totality is the sharpened form: every fold and
every seam operation is a **total** function of structurally-conformant input —
no inner join, filter, or cast may drop or error on a row a semantic defect
made weird. A tier-2 `state` tracked value whose corrupted history text does
not parse reconstructs `NULL` via `TRY_CAST` (a cast never errors). The same
posture governs published identity: the seam runs no publication gate — the
uniqueness guard is a data check permissive totality forbids (a `mutate_cells`
defect on `presentation_id` must play at the layer built to play it, and the
export's escape — export without election — does not exist where there is no
election to drop), and the static union-safety gate would require tier 1 to
reach up into the election, which the layer direction forbids
([`key-election.md`](key-election.md) § Identity publication). A published
surface at the seam is the column verbatim, defects included — on a corrupted
tape the colliding surrogate is the deliverable the answer key scores against,
which is also why the absent-`identity` default is the full available set: a
default that suppressed the surrogate would hide the corrupter's own output
from the environment built to teach from it. The seam shows what the tape
holds; the caller narrows. Behavioral
cases per injected defect are exercised in
[`tests/playback/`](../../tests/playback/).

### Edge and error semantics

`events(T, T)` is an empty iterator; `events(start > end)`, a negative bound,
or a negative `at_sim_time` raises `PlaybackError` (a caller-contract
violation, never a data condition). `at_sim_time` beyond the slice bound yields
final state / exhaustion — total, no range check. An empty population yields
zero events and zero-row typed tables (declared atoms always answer).
Selection-resolvability failure raises `PlaybackError` at open, before any data
read; a source shape with `anchor=None` raises at `open_shaped_playback`; the
windowed business rules raise on the first `window` ask. Upstream guard/reader
errors (`ExportError`, `TableNotFoundError`, the version gate) pass through
untouched.

## Invariants

1. **Pull-only.** No operation performs I/O until an answer is pulled;
   `open_*` reads the sidecar only. No clock, sleep, sink, or session exists at
   the seam. Outstanding lazy answers are independently pullable.
2. **Deterministic.** Same tape + selection + anchor + ask arguments + code
   version → identical events, `seq`, and tables. Corrupted tapes included.
3. **Entry-point-invariant `seq`.** `seq` is a pure function of
   `(tape, selection)`; bounded and unbounded heads agree.
4. **One event-time line, across both tiers.** On a temporally-intact tape the
   tier-1 consistency algebra holds for every `(selection, T1, T2)`; tier-2
   agreement is per table class, exact at the slice bound (the bridging
   theorem) and up to the class's documented consumer merge elsewhere. A shaped
   event-log table over `[T1, T2)` and a tier-1 `events(T1, T2)` pull carry
   the same change set.
5. **Faithful reshaping + temporal honesty, per answer.** Every delivered value
   traces to a base value or a declared recoding; no value derives from base
   state later than the answer's time key, with two stated exceptions —
   `temporal_class: constant` sources and the discriminator classification
   reads (the current spine value at every T). A non-exempt `slice_only` source
   appears in no answer of either tier; `last_mutation_sim_time` appears under
   its own name in no answer of either tier (never selectable at tier 1, a
   reserved output name at tier 2 — see [`source.md`](source.md) /
   [`dimensional.md`](dimensional.md)) while its value flows under presentation
   names, presented as the recorded trail in a `state` answer.
6. **Permissive totality.** Every operation is total over
   structurally-conformant input; semantic defects flow through verbatim.
7. **Rendered-instant agreement.** One absolute instant renders byte-identically
   wherever it appears — event `ts`, snapshot `_ts` — under one resolved anchor
   (a tier-1 guarantee); tier-2 values keep their mode's shipped full-export
   rendering, a different representation of the same instant.
8. **Layer direction.** As § Boundary; the chain is acyclic and no mode imports
   a tier.
9. **Bridging (a theorem, not a stipulation).** `state(T_slice)` equals the
   shape's full export for every shape that opens, so the seam is provably
   sufficient to rewrite the shipped verbs on.
10. **Inherited.** Version-gated input, sidecar-driven schema discovery,
    single-branch guard, no producer dependency.

## Validation Rules

No config models — the seam is a Python library surface. All checks are
open-time or ask-time business rules over plain typed values, each raising
`PlaybackError`; the rule set (selection resolvability, uniqueness,
slice-only refusal, bound validity, and the identity-projection rules — a
given `identity` non-empty and duplicate-free, every member a tier-1 surface
(`record_id` / `presentation_id`), `record_id` a member, `presentation_id`
published only on a kind that mints one, and no selected property naming an
identity surface) is enforced in
[`head.py`](../../src/fabulexa_forge/playback/head.py) and
[`shaped.py`](../../src/fabulexa_forge/playback/shaped.py). Tier-2 additionally
runs the mode's own full config validation at open (passed through as
`ExportError`), the source-shape anchor requirement at open, and the windowed
business rules on the first `window` ask. Unknown record ids are deliberately
not a rule — an id filter is a predicate, and a corrupted tape may have deleted
any id.

## Rationale

- **A seam, not a second door.** Every playback answer already lived welded
  inside a verb-specific engine (streaming, the incremental driver, source
  snapshot) and was unreachable as a library call. Exposing them once, as data
  and statelessly, is what lets a caller above forge get "exactly what changed
  in `[T1, T2)` for these atoms" without assembling an `ExportConfig`, a cursor
  file, and a writer directory. A seam that spoke only events and state would
  leave the shaped compositions (a star-schema drip, a source system as of T)
  needing a reimplementation of forge's reshaping — hence two tiers.
- **Pull-only, no timing.** Timing authority cannot exist at the seam by
  construction, because pacing, cadence boundaries, cursors, and sessions are
  caller concerns (the boundary razor). This keeps the seam a pure function and
  makes the consistency algebra worth stating.
- **`last_mutation_sim_time` is sim-internal bookkeeping.** It is read freely
  and delivered under its own name never — a high-water mark the contract binds
  in one direction, not a data-domain value. The reserved-output-name posture
  and the recorded-trail presentation follow from that one fact.
- **Truncation over per-class reconstruction.** Realizing `state(T)` as the
  full-export compile over a truncated tape makes as-of-T correctness a
  property of the data, not of per-class reassembly rules that would drift from
  the full-export path.
- **Identity projection reaches the published maps, not the typed fields.**
  Projection control exists where publishing costs something — where a row
  leaves as an untyped map (the event `after` map, the `record_state` table)
  for a consumer who did not declare a schema and would otherwise serialize a
  second identity onward with no way to say otherwise. A typed field costs an
  unread consumer nothing, so `PlaybackEvent.record_id` / `.presentation_id`
  stay always-populated; projection governs the `after` map, never the typed
  fields.

## Boundaries

- **Pacing, cadence, cursors, sinks, and sessions are above the seam.** Tier 2
  speaks raw-ns bounds only; computing *which* bounds (civil-calendar math, a
  live wallclock cadence) is the caller's job — the incremental driver today,
  loom's timeline later.
- **Named atom groups are deferred.** The seam speaks atoms; a named-group
  vocabulary resolves to a set of atoms before the seam sees it, so it can be
  added above the selection surface later without changing any seam contract.
- **Tier 2 carries no identity grammar of its own.** Shaped playback compiles
  a declared target shape through the modes and inherits their identity
  rendering (the `keys` election, per-mode render rules); a second grammar at
  that tier would duplicate the modes'.
- **Re-seaming the shipped verbs is deferred.** `stream` and the incremental
  driver re-seam when next materially touched, with byte-identical output as
  the bar; the seam owns no verb today.
- **Trunk-only.** The seam composes `require_single_branch` and is
  single-branch; multi-branch playback is Stage 5.

## Related

| Document | Why |
|---|---|
| [`derivations.md`](derivations.md) | The folds the seam composes, plus the membership-state-at, end-of-tape, and truncated-tape residents it owns |
| [`source.md`](source.md) · [`dimensional.md`](dimensional.md) | The modes tier 2 compiles; the `base_relations` compile parameter and the `last_mutation_sim_time` reserved-output-name posture |
| [`incremental.md`](incremental.md) | The per-table-class window-membership contract tier-2 `window` promotes |
| [`streaming.md`](streaming.md) | The canonical order and `seq` a single-content stream conforms to |
| [`key-election.md`](key-election.md) | The identity-publication layer split (§ Identity publication) — why the seam projects published identity but never gates it |
| [`slice-only.md`](slice-only.md) | The `slice_only` policy the seam inherits at selection and at open |
| [`anchor.md`](anchor.md) | The `EffectiveAnchor` both tiers render wallclock through |
| [`temporal-elections.md`](temporal-elections.md) | The election vocabulary tier 2 renders by reusing the modes' own compile and validation surfaces directly |
