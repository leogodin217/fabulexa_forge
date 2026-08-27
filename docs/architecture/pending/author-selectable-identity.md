---
status: draft
---

# Author-selectable identity

An author picks which identity surface presents (`record_id` / `record_index` /
`presentation_id`) and controls **which identity surfaces a publishing layer
publishes, and under what names**. Key election already delivers the pick and
the value-level integrity. This design delivers the projection and the naming
on the two layers that publish identity without an author declaring it — the
streaming after-image and the playback seam's tier-1 maps — and makes the pick
itself visible to authors at `init` time in every mode.

---

## Problem

Key election lets an author elect, per population, which identity surface a
table or topic presents, and guarantees that every referencing site renders its
target's elected surface. Streaming reuses that machinery wholesale. What
neither streaming nor the playback seam has is any control over **which
identity surfaces reach a published row, and what they are called there** — and
the consequence is that correct joins become invisible or, worse, look broken.

### The identity output key is pinned to the contract column name

The message-key entry name and the after-image identity entry both take the
elected surface's contract column name, verbatim, and the `rename` grammar
refuses that name as reserved. A stream over a kind electing `record_index`
therefore ships:

```json
{"kind": "patient", "key": {"record_index": "41"}, "after": {"record_index": "41", "status": "admitted"}}
```

while the paired source export presents the identical value as each table's
`id`. Four independent fresh-eyes QA passes over the nhs, retail, ride-sharing,
and ride-sharing-marketplace example configs measured 100% join rates between
the two — and every pass still flagged it, because nothing on the wire says
these are one id space. The author's only recourse is to change the *election*,
which changes the values, to get a name they could have gotten by renaming.

Source has the handle streaming lacks: a state table's identity-column rename
key is the elected surface's contract column name, so `rename: {record_index: id}`
is one line. Dimensional has it for free — every dim column is author-declared.

### A second identity surface publishes unelected and ungated

`presentation_id` is an identity surface. The base format places it in the
structural block immediately after `record_id`, gives it no `temporal_class`
and no `history_tracked`, and holds it outside the value-column population
entirely; key election makes it one of three electable surfaces. Yet the
streaming after-image publishes it whenever the kind carries one, regardless of
the election, and the election gates never inspect it: absorption fires only
under a `presentation_id` election, so under any other election the column
ships unconditionally and unexamined.

| Example | The published surface | What a consumer sees |
|---|---|---|
| nhs | consultant `CON_…` ids | Joins nothing in the app database — the source export dropped the counter |
| ride-sharing-marketplace | `presentation_id` | Roughly half the rows match by coincidence — a join trap, worse than no match |

No value is fabricated — the column is carried faithfully from the base. The
defect is that an identity surface is published without the author electing it
and without the union-safety algebra ever ruling on it. The
ride-sharing-marketplace half-match is precisely what an ungated identity
column looks like: two populations' key spaces overlapping in one column, the
exact condition the algebra exists to refuse.

The playback seam publishes the same surface for a different reason. Its
tier-1 event after-image and its `record_state` snapshot table both carry
`presentation_id` whenever the kind mints one, independent of the caller's
projection — there is no projection. Those are `dict` and Arrow-table surfaces
a learning environment serializes onward, so a consumer who wanted the record
key alone ships a second one and has no way to say otherwise.

The seam's defect is the missing control, not a missing gate. An ungated
surrogate at the seam is faithful pass-through, and on a corrupted tape a
colliding one is the *deliverable* — the overlapping key space is what an
answer key scores against. The seam needs projection; it must not acquire
gating (§ The playback seam).

### The election itself is invisible to authors

`init` proposes exactly one election per population and emits no trace of the
alternatives — `presentation_id` for registry-declared populations,
`record_index` otherwise. An author reading a generated config sees a value,
not a choice, and nothing tells them the other two surfaces exist or that the
decision is theirs. The pick is the feature; the generated config hides it.

---

## Solution

Three changes, all projection and naming. No event set, no ordering, no `seq`,
no key election, and no elected *value* changes anywhere.

**Identity surfaces are projected, never property-selected.** A publishing
layer declares which identity surfaces its rows carry. On a stream that is the
per-stream `identity` list; absent it, the stream publishes its elected surface
alone. On the playback seam it is the tier-1 record selection's `identity`
tuple, following that layer's own absence convention. On a stream every
published surface — elected or not — runs the election's resolution gates, the
union-safety algebra, and the render-time uniqueness guard, so a published
identity column cannot carry a colliding space. The seam gates nothing and
must not: permissive playback obliges it to deliver a corrupted tape's
colliding surrogate rather than refuse it (§ The playback seam).
`properties` and `fields` stay what they are: payload.

**The identity output key becomes author-addressable through the existing
`rename` map**, using source's grammar: a *published* surface's contract column
name is a legal `rename` key. For the elected surface the resolved key applies
at every identity site on that stream at once — the message key entry, the
after-image identity entry, the Debezium key-only `d` before-image, and the
Debezium value schema — so key and payload can never disagree. The
reserved-name gate moves from the contract name to the resolved key.

**`init` proposes the election as a visible menu in every mode**: `record_index`
active, with the surfaces that would resolve for that population emitted beneath
it as commented alternatives. `record_index` is the universal choice — every
emit carries it, no registry declaration is required, and a uniform
`record_index` proposal passes every gate by construction.

```yaml
# A generated streaming config, after this design
keys:
  # Alternatives for 'patient' — swap the active line for one of these:
  #   patient: record_id
  #   patient: presentation_id
  patient: record_index

streams:
  - name: patient
    kind: patient
    identity: [record_index, presentation_id]   # what this topic publishes
    properties: [status, ward]
    rename:
      record_index: id                          # the elected surface, renamed
      presentation_id: nhs_number
```

```json
{"kind": "patient", "key": {"id": "41"}, "after": {"id": "41", "nhs_number": "NHS-0041", "status": "admitted", "ward": "A3"}}
```

The `identity` block above is the opt-in. Written as
`identity: [record_index]`, or omitted entirely, the topic publishes the
elected surface alone and the nhs and ride-sharing-marketplace traps are gone
by default.

---

## Affected Subsystems

- **Streaming exporter — identity projection.** A new per-stream declaration
  resolving, against the stream's gated election, to the ordered set of
  identity surfaces the after-image publishes. Runs the election's own
  resolution and union-safety gates over every published surface, not only the
  elected one.

- **Streaming exporter — output-name resolution.** The single naming authority
  gains the resolved identity projection as an input and stops receiving a
  pre-resolved identity key from its caller. Its output entries widen from
  `(fold column, output key)` pairs to entries that also carry identity
  surfaces rendered through election relations. Its reserved-name set changes
  from `{identity contract column, presentation_id, event}` to `{each published
  surface's resolved output key, event}`. Its absorption branch is removed —
  under a `presentation_id` election the surface is published once, as identity.

- **Streaming exporter — the eager validation pass.** Gains the identity
  projection rules and one rule refusing an identity surface named in
  `properties`. Its existing per-stream message-prefixing behavior is unchanged.

- **Streaming exporter — the render seam.** Composes one identity relation per
  published non-`record_id` surface, at the end-of-tape entry point, exactly as
  it composes the elected surface's today. `record_id` composes none.

- **Streaming exporter — the stream event record.** `key_column` becomes the
  elected surface's resolved output key rather than its contract name. The
  standalone surrogate field is removed: the surrogate now reaches the wire
  through the after-image when published, so nothing reads it.

- **Streaming config models.** `KindStream` and `MembershipStream` gain the
  optional `identity` list; `rename` keys stay unconstrained at parse time,
  because which keys are legal depends on the election and the projection,
  neither of which parse time knows.

- **The Debezium format.** Key and value-schema field names follow the
  resolver, so both changes reach it with no format-local rule. The `d`
  key-only before-image carries the elected surface's resolved key name.

- **The Kafka sink.** Message key bytes change under an identity rename: the
  key is the pinned-encoded one-entry map, so the entry name is part of the
  key. This is the intended effect — one topic still carries exactly one
  identity space.

- **The playback seam — tier 1.** `RecordAtomSelection` gains an `identity`
  projection governing which identity columns the event after-image map and the
  `record_state` snapshot table carry. `PlaybackEvent`'s typed `record_id` and
  `presentation_id` fields are unchanged. The seam gains projection only — no
  gate, no election import, and no change to either fold: the projection is
  applied above the composed relation, never threaded into it.

- **Cross-mode key election — the `init` proposal contract.** The proposal
  changes from one silently-chosen election per population to a uniform
  `record_index` active proposal plus per-population commented alternatives,
  rendered by one shared emitter the three mode `init`s splice. Because a
  uniform `record_index` proposal passes every gate by construction, the
  existing degrade-on-gate-failure mechanism and its explanatory comments
  become unreachable and are removed.

- **Dimensional `init`.** Its dim-key alignment follows the active election, so
  proposed dim key columns source from `record_index`, and the advisory comment
  naming `presentation_id` as the contract-declared natural key is emitted
  again wherever a surrogate is declared — the alignment rule itself is
  unchanged.

- **The config loader.** Gains a duplicate-mapping-key refusal, shared by the
  export, streaming, and corrupt config paths. Without it, an author who
  uncomments a menu alternative without removing the active line gets silent
  last-wins behavior; with it they get a named error.

- **The mixer.** Pass-through and semantically unaffected, but its
  event-preservation invariant no longer names the removed surrogate field.

---

## What Doesn't Change

- **The derivations layer.** The row-state-events fold, its column-order
  producer, and its fold-row column list are untouched, and so is the state-at
  resident that materializes the seam's `record_state` table — it keeps
  emitting `presentation_id` from the sidecar alone. Both folds keep carrying
  `record_id` and the kind's `presentation_id` whenever the kind mints one —
  derivation-layer identity is complete by definition and is not a projection
  question (§ Two identity layers). Every projection in this design is applied
  **above** the composed relation; none is threaded into a resident, which
  would impose one consumer's presentation choice on the others that share it
  (`mode: base` and the incremental driver both read state-at).
- **Key election's semantics.** The grammar, the three surfaces, the
  resolution and combination gates, the union-safety algebra, the identity join
  relations, and the render-time uniqueness guard are untouched — this design
  widens the population they range over, it does not redefine them. Absent a
  `keys` block the election is still `record_id` throughout.
- **Elected values.** Every identity site renders the same value it renders
  today. This design changes names and which surfaces publish, never a value.
- **Event sets, ordering, `seq`, `ts`, merge order, topic assignment, and
  routing.** The canonical order and merge key still read the fold's
  `record_id`.
- **Reference and member-field rendering.** A reference column still renders
  its target's elected surface; a membership member field still renders the
  member row's kind's elected surface with `__kind` as the disambiguator.
- **Source, base, and dimensional export rendering.** Only their `init`
  proposals change; every export path renders as it does today. Source's and
  dimensional's per-column author control stays the mechanism it already is,
  and base keeps its auto-projected standalone surrogate — deliberately, on a
  distinction it alone has (§ Boundaries).
- **Playback tier 2.** Shaped playback compiles a declared target shape through
  the modes and inherits their identity rendering; it gains no grammar here.
- **The typed playback event record.** `PlaybackEvent.record_id` and
  `.presentation_id` remain, always populated. Projection governs the `after`
  map, not the typed fields.
- **Change scope, row selection, and value-rendering elections.** `only` /
  `ignore` / `where` / `render` domains are unchanged — see § Surfaces closed
  to identity surfaces.
- **The membership `event` reservation.** Reserved on membership streams under
  both formats, as today.
- **Payload key bareness.** `prop__` / `elem__` / `member__` prefixes still
  never reach the wire.

---

## Semantics

### Two identity layers

The design turns on one distinction the package has not previously had to
name.

| Layer | What identity means there | Who decides |
|---|---|---|
| Derivation | Complete: the fold carries `record_id` and the kind's `presentation_id`, always, so every consumer can key and re-derive from one honest row | The contract — never a config |
| Publication | Projected: what a published row actually carries, and under what names | The author, per publishing declaration |

The row-state-events fold has three consumers with three audiences (the
streaming engine, the source event log, the playback seam). A projection
applied in the fold would be one consumer's presentation choice imposed on the
other two. So the fold stays complete and each publishing layer projects.

Projection control exists where publishing costs something — where a row leaves
as an untyped map for a consumer who did not declare a schema. That is the
streaming wire and the seam's tier-1 maps. It is not the typed
`PlaybackEvent` fields, where an unread field costs nothing and removing one
would only conflate "no surrogate" with "suppressed".

### Identity projection

A publishing declaration names the identity surfaces its rows carry. The
declared set is resolved against the layer's identity knowledge into an ordered
**published set**.

| Layer | Declaration | Admissible surfaces | Absent |
|---|---|---|---|
| Kind-shaped stream | `identity` | `record_id`, `record_index`, `presentation_id` | The elected surface alone |
| Membership-shaped stream | `identity` (over the **owner**) | as above, resolved against the owner's election | The owner's elected surface alone |
| Playback tier-1 record atom | `RecordAtomSelection.identity` | `record_id`, `presentation_id` | The full available set — `record_id`, plus `presentation_id` when the kind mints one |

Each layer keeps its own absence convention, and in each the absent path is
genuinely taken rather than substituted (Principle #7). A stream declares what
ships — its `properties` is required with no default, and an absent `rename`
means bare keys — so an absent `identity` publishes the minimum a topic cannot
do without: its key. The seam's convention is the opposite and established:
an absent (`None`) projection means the full selectable set, and `identity`
follows it. The empty-tuple end of the seam's convention does not carry over:
`record_id` is required in the published set, so an empty `identity` is
refused, never read as "none" (§ Playback rules).

**Order is sourced, never invented.** The published set renders in the kind's
`records__<kind>` sidecar column order restricted to the published surfaces —
`record_id`, then `presentation_id`, then `record_index`, the contract's own
positions. A declaration's list order never reaches output; the list is a set.

**Values come through the election relations.** Every published
non-`record_id` surface renders through key election's identity join relation
for that surface, keyed on the fold's `record_id`, composed at the end-of-tape
entry point — the record-index derivation for `record_index`, the
presentation-key derivation for `presentation_id`. A published surface is never
read from the fold's after-image and never from a `ref_index__` column, the
election's standing rule. `record_id` is the fold's own column verbatim and
composes no relation.

### Publication gates

On a stream, every published surface runs the gates the elected surface runs.
This is the design's answer to an identity column shipping unexamined; it is a
widening of the gate population, not a new algebra. The gates are a streaming
property throughout — the playback seam resolves no election and acquires none
of them (§ The playback seam).

| Gate | Ranges over | On violation |
|---|---|---|
| `presentation_id` declared | Each population the stream spans (kind-shaped) or addresses as owner (membership-shaped), when `presentation_id` is published | `ElectionPresentationUndeclared`, naming the stream and the uncovered population |
| Identity union safety | The spanned/addressed populations' key spaces for each published surface, pairwise | `ElectionUnionUnsafe`, naming the stream, the surface, and the unsafe pair |
| Published-key uniqueness | Render time, per composed identity relation: `rows = DISTINCT record_id = DISTINCT value`, value non-NULL | `ElectedKeyDuplicate`, naming the stream and the surface |

The uniqueness guard now ranges over surfaces a population did not elect, so
its message must name the **surface** and not describe it as the election's:
`ElectedKeyDuplicate` keeps its identity (it is one guard over one relation
shape, and splitting it would give one failure two names), but a violation on
a published non-elected surface reads as that surface's, not as an election
failure the author never declared.

`ElectionMixedIdentity` — one stream, one key surface — remains about the
*election* alone. A published non-elected surface is one author-named surface
applied to every spanned population, so no mixing is expressible.

Two consequences worth naming. Publishing `presentation_id` now requires the
registry to declare it, where today it ships with no claim consulted at all;
that is a deliberate tightening, and the escape is not publishing it. And
`record_index` is publishable on any population — one shared space per kind,
union-safe with itself by the contract's own verdict.

### Identity output-key resolution

A published surface resolves one output key, from the declaration's `rename`
map.

| Condition | Result |
|---|---|
| No `rename`, or no entry keyed on a published surface | Each published surface's contract column name (today's behavior for the elected one) |
| `rename` entry keyed on a published surface | That entry's target |
| `rename` entry keyed on a surface the stream does not publish | `StreamRenameUnresolvable`, naming the stream's published set |
| `rename` entry keyed on a selected property / field name | The ordinary payload rename (today's behavior) |
| `rename` entry keyed on anything else | `StreamRenameUnresolvable` (today's behavior) |

The **elected** surface's resolved key applies to **all four** of its sites at
once — the message key map entry, the after-image identity entry, the Debezium
key-only `d` before-image entry, and the Debezium value-schema field. One name,
four sites, one producer. A published non-elected surface appears in the
after-image and the value schema only; it is never a message key.

Membership streams resolve owner identity keys by the same rule, reading the
owner's election and the stream's owner projection.

### Reserved output names

| Name | Reserved on | Note |
|---|---|---|
| Each published surface's resolved output key | Every stream | Was the elected surface's *contract* name plus a categorical `presentation_id`; now the resolved keys of exactly what publishes, so a rename moves the reservation with it |
| `event` | Membership streams, both formats | Unchanged |
| `presentation_id` | Nothing categorically | Reserved only when published, and then under its resolved key |

A payload output key colliding with a published identity key is
`StreamOutputNameCollision`, as today. Two consequences follow from the
reservation tracking what actually publishes, under its resolved name.
`rename: {record_index: status}` on a stream that publishes `record_index` and
also selects a `status` property is a collision — the identity moved onto the
payload's name. And `rename: {status: record_index}` is legal on a stream that
does not publish `record_index`, and a collision on one that does: a contract
column name is free for payload use exactly when no identity surface is
claiming it.

### Identity surfaces are not properties

| Condition | Result |
|---|---|
| `record_id`, `record_index`, or `presentation_id` in a stream's `properties` | `StreamPropertyNotAddressable` — identity is projected through `identity`, not selected through `properties` |
| A membership stream's `fields` naming `presentation_id` | `MembershipFieldResolvable` (today's rule) — a membership stream's payload is element fields |
| A playback record selection's `properties` naming an identity surface | `PlaybackError` at open, the seam's schema-question posture |

One rule replaces what a payload reclassification would have needed as a set of
exceptions. Because an identity surface never enters the property namespace,
the surfaces below need no carve-out.

The rule claims the three bare names out of the payload namespace
deliberately: a producer payload property that shares one — the contract does
not forbid a property named `record_index`, so `prop__record_index` can exist —
is unaddressable on a stream and at the seam, full stop. That is the posture
source already takes (`SourceColumnNotAddressable` refuses `record_index` in
`columns` / `rename`); on the wire an identity name must mean identity, and a
payload column that borrows one would be exactly the join trap this design
removes.

### Surfaces closed to identity surfaces

Stating these because the projection could otherwise be read as widening them.
None widen; each already resolves its keys against the payload namespace.

| Surface | Why an identity surface stays outside |
|---|---|
| `properties` / `fields` slice-only refusal | Its keys resolve to `prop__<p>` / `elem__<f>` columns. Identity columns carry no `temporal_class` at all (the contract omits it on `presentation_id`, `record_id`, and `record_index` alike), so the question is not askable of them — and, with identity out of the property namespace, is never asked |
| `only` / `ignore` change scope | Genesis-minted or creation-constant, never re-minted, never in `history` — not in any kind's audited set. Naming one is `StreamChangeScopeUnresolvable` |
| `where` predicates | Keyed on bare payload-property names of the subject kind |
| `render` value elections | Keyed on the stream's declared `properties` / `fields`, then type-checked against the `prop__` / `elem__` column. An identity surface fails the first gate as a non-member — the refusal identity is `RenderKeyResolves`, not a type mismatch |

### Membership owner identity

A membership stream's after-image carries the owner identity entry, then the
selected element fields. The owner projection admits the same three surfaces,
resolved against the owner's election, and publishes them in the owner kind's
sidecar column order ahead of the element fields (after the leading Debezium
`event` column). Playback's membership atom selection gains no projection: its
tier-1 payload carries the owner's `record_id` only, and no surrogate reaches
it today.

### The playback seam

The seam's tier-1 projection governs both of its published-map surfaces
coherently, because both are presentations of the same selected population:

| Surface | Effect of the projection |
|---|---|
| Event `after` map | Carries one entry per published surface, in sidecar column order, ahead of the selected `prop__<p>` entries |
| `record_state` snapshot table | Carries one column per published surface; column order is the fold's canonical relation as today, with the unpublished identity columns absent |
| `PlaybackEvent.record_id` / `.presentation_id` | Unchanged — always populated when the kind mints a surrogate |

`record_id` is required in the published set: it is the event key, the relation
spine, and the seam's stated identity. `record_index` is outside the tier-1
domain — tier 1 sits below the modes and composes no election relations, so
offering a surface it cannot source would be an empty option. A caller wanting
index identity uses tier 2, which inherits the modes'.

The seam declares its own surface vocabulary as string literals rather than
importing the config's `KeySurface`: `playback` imports the reader, the
derivations property helpers, and stdlib only, and this design does not spend
that layer-direction invariant. The two layers share the concept and the
surface *names*; they do not share a Python type.

**The seam runs no publication gate.** Both halves of the streaming gate are
closed to it, and permissive playback is why. The uniqueness guard is
election's one data-touching check, and a corrupted tape is exactly what fails
it — a `mutate_cells` defect on `presentation_id` would become unplayable at
the layer built to play it, and the export's escape (export without election)
does not exist where there is no election to drop. The static union-safety
gate is data-free and would not choke, but running it means reaching up into
the election, which the layer direction forbids. So a published surface at the
seam is what it has always been: the column verbatim, defects included.

**The absent-`identity` default is the full available set, and stays there.**
Beyond following the seam's established absence convention, narrowing it would
work against the corrupter: a colliding surrogate on a corrupted tape is the
artifact the answer key scores against, and a default that suppressed it would
hide the corrupter's own output from the environment built to teach from it.
The seam shows what the tape holds; the caller narrows.

### Referential integrity

The design's central claim, stated as invariants:

1. **Renaming never moves a value.** The value at every identity site is the
   value key election resolves, rendered through the streaming codec. A rename
   changes the key under which that value ships and nothing else, so every join
   that holds today holds after a rename.
2. **One stream, one identity name per surface.** A stream's message key,
   after-image identity entry, `d` before-image, and value schema carry the
   same resolved key for the elected surface. A consumer joining a topic's key
   against its own payload cannot mismatch.
3. **An identity rename is per-stream presentation and does not propagate.**
   A reference column in *another* stream still renders its target's elected
   surface under its own property's output key. A topic keyed `id` may be
   referenced as `patient_id` elsewhere — the values join; the names need not
   match. This is the shape real CDC feeds have.
4. **Projection never invents and never hides a value.** A surface's entry
   ships iff the layer publishes it. An unpublished surface is absent, not
   null-filled; a published one is the election relation's value verbatim under
   the codec.
5. **Every published identity surface is gated where an election resolves.** On
   a stream, no identity column reaches a published row without passing
   resolution, union safety, and the uniqueness guard over the populations it
   spans — what the auto-included surrogate never did. The playback seam is
   outside this invariant by its own contract rather than by omission: the
   uniqueness guard is a data check that permissive totality forbids, and the
   static union-safety gate would require tier 1 to reach up into the election,
   which the layer direction forbids. The seam's control is projection alone
   (§ The playback seam).
6. **Compaction coherence survives.** Every electable surface is
   creation-constant and renaming does not change that, so a record's
   `c`/`u`/`d` events keep one key for life and the `d` keys the tombstone.
7. **Presentation invariance, restated.** For a fixed declaration, adding or
   changing `rename` changes output key strings only — payload keys, published
   identity keys, and therefore the Kafka message key bytes, whose one-entry map
   carries the entry name. Event count, order, `seq`, `ts`, elected values, and
   topic assignment are byte-identical. Adding or removing a published surface
   changes which entries a row carries and nothing else. A message key is
   **value**-identical under rename, not byte-identical: its one-entry map
   carries the entry name, and that name is author-controlled.

### The `init` election menu

Applies to every mode's `init` (dimensional, source, streaming), since the
proposal contract is cross-mode.

| Population state | Active proposal | Commented alternatives |
|---|---|---|
| Any population | `record_index` | `record_id`; and `presentation_id` iff the population is registry-declared |
| Partitioned kind, no sub-type registry-declared | The `record_index` scalar | `record_id` for the kind |
| Partitioned kind, ≥ 1 sub-type registry-declared | The per-sub-type map, `record_index` throughout | Per sub-type, as above |

Rules:

- **The active proposal is uniform `record_index` across every population of
  every kind.** It is always available (every emit carries `record_index`),
  needs no registry declaration, and is gate-clean by construction: one shared
  space per kind, and the empty prefix collides only with digit-prefixed
  spaces, which a uniform proposal contains none of.
- **The map/scalar shape follows the alternatives, not the active values.**
  Today a per-sub-type map whose values agree collapses to the scalar; under a
  uniform active election every map would collapse, leaving per-sub-type
  alternatives nowhere to attach. So the map is emitted when at least one
  sub-type carries a **population-specific** alternative — a `presentation_id`
  line, the only alternative whose availability varies by population — and the
  scalar otherwise. `record_id` is offered for every population and is
  therefore never the deciding factor: it is kind-wide, and on a scalar
  proposal it attaches as the kind-wide shorthand, which elects it uniformly
  across the domain. The collapse rule is not suspended — its input becomes the
  proposal *with* its alternatives.
- **Only resolvable alternatives are emitted.** A `presentation_id` line is
  emitted for a population only when the registry declares it. An alternative
  that could not resolve is not offered — offering it would be a trap.
- **Alternatives are resolvability-checked, not gate-checked.** An author who
  activates an alternative may still hit a union-safety or uniformity gate;
  that failure is loud and names its remedy. Gate-checking hypothetical
  variants would require enumerating combinations across populations, which the
  proposal does not do.
- **Alternatives are a swap, not an uncomment.** The comment block states
  this in one line. Activating an alternative without removing the active line
  is a duplicate mapping key and is refused at load.
- **One renderer, three emitters.** The active lines and their comment block
  are produced by one function beside the proposal; the dimensional, source,
  and streaming `init` engines splice its lines. Three emitters rendering one
  menu three ways is exactly the drift the single-producer discipline exists to
  prevent.
- **Degradation retires.** The existing degrade-to-`record_index`-with-a-comment
  mechanism exists to repair a proposal that fails its own gates; a uniform
  `record_index` proposal cannot, so the mechanism and its comments are removed
  rather than left unreachable.
- **`init` proposes no `identity` block.** A projection is author intent with
  no sidecar-derived value, joining `rename` / `kind_label` / `where` / `only`
  on streaming's never-proposed list and its trailing comment. The consequence
  is the intended default: a generated streaming config publishes the elected
  surface alone, and the nhs and ride-sharing-marketplace traps do not survive
  regeneration.
- **Streaming's `init` posture is preserved.** Every proposed stream stays
  live, and the emitted config parses and streams clean by construction — the
  keys block is now gate-clean by construction rather than by degradation.

### Config loading

| Condition | Result |
|---|---|
| A YAML mapping contains the same key twice, at any depth, in any config | `ConfigError` naming the file, the duplicated key, and its line |
| Otherwise | Unchanged |

This is what makes the menu safe: without it, activating an alternative while
the active line remains produces silent last-wins behavior that depends on
line order.

---

## Configuration

One new optional field on both stream shapes, one grammar widening on
`rename`, one new field on the seam's record selection, and one
generated-output change.

### Kind-shaped stream

```yaml
keys:
  patient: record_index

streams:
  - name: patient
    kind: patient
    identity: [record_index, presentation_id]
    properties:
      - status
      - ward
    rename:
      record_index: id       # the elected surface's contract name -> the wire name
      presentation_id: nhs_number
```

| Field | Type | Required | Description |
|---|---|---|---|
| `identity` | `list[KeySurface]` | No | The identity surfaces this topic publishes; must contain the stream's elected surface. Absent: the elected surface alone |
| `properties` | `list[str]` | Yes | Bare payload property names. Identity surfaces are not admitted |
| `rename` | `dict[str, str]` | No | Selected property name → output key, now additionally admitting a published surface's contract column name → that surface's output key |

### Membership-shaped stream

```yaml
keys:
  driver: presentation_id

streams:
  - name: driver_trips
    membership: {kind: driver, property: trips}
    identity: [presentation_id]
    fields: [fare, vehicle]
    rename:
      presentation_id: driver_id   # the owner's published surface -> the wire name
      vehicle: vehicle_ref
```

| Field | Type | Required | Description |
|---|---|---|---|
| `identity` | `list[KeySurface]` | No | The **owner** identity surfaces this topic publishes; must contain the owner's elected surface. Absent: the owner's elected surface alone |
| `rename` | `dict[str, str]` | No | Selected field name → output key, now additionally admitting a published owner surface's contract column name → that surface's output key |

`fields` is unchanged: a membership stream's payload is element fields.

### Playback tier-1 record selection

```python
RecordAtomSelection(
    kind="patient",
    sub_types=(),
    properties=("status",),
    identity=("record_id",),      # suppress the surrogate in published maps
    record_ids=None,
)
```

| Field | Type | Required | Description |
|---|---|---|---|
| `identity` | `tuple[str, ...] \| None` | No | The identity surfaces the event `after` map and the `record_state` table carry; must contain `record_id`. `None` means the full available set — the seam's established convention |

---

## Interface Contracts

### Runtime types

```python
@dataclass(frozen=True)
class IdentityProjection:
    """One stream's resolved, gated identity projection."""

    elected: "KeySurface"
    """The stream's gated uniform elected surface — for a membership stream,
    the owner's. Always a member of `published`."""

    published: "tuple[KeySurface, ...]"
    """Every surface this stream publishes, in the kind's sidecar column
    order (record_id, presentation_id, record_index). Never empty."""


@dataclass(frozen=True)
class OutputEntry:
    """One after-image entry: where its value comes from, and its wire name."""

    source_kind: "Literal['identity', 'payload']"
    """'identity': `source` names a KeySurface rendered through its election
    relation (or, for record_id, the fold's own column). 'payload': `source`
    names a fold output column read verbatim."""

    source: str
    """The surface name or the fold column name, per `source_kind`."""

    output_key: str
    """The wire name — the bare default or the resolved rename target."""


@dataclass(frozen=True)
class KeyElectionProposal:
    """An `init` keys proposal: the active election plus its alternatives."""

    active: "Mapping[str, KeySurface | Mapping[str, KeySurface]]"
    """Per kind, the active election — a scalar for a flat kind, or for a
    partitioned kind no population of which carries a `presentation_id`
    alternative; a per-sub-type map otherwise. `record_id` is offered
    everywhere and never decides the shape. Uniformly `record_index`."""

    alternatives: "Mapping[str, Sequence[KeySurface]]"
    """Population address -> the surfaces offered as commented alternatives,
    in surface order. A population address is the kind for a flat kind and
    '<kind>.<sub_type>' for one population of a partitioned kind."""
```

### Functions — identity projection

```python
def resolve_identity_projection(
    sidecar: "Sidecar",
    stream_name: str,
    kind: str,
    declared: "Sequence[KeySurface] | None",
    election: "Election",
    populations: "frozenset[str | None]",
) -> "IdentityProjection":
    """Resolve and gate one stream's identity projection.

    Runs the election's own resolution and union-safety gates over every
    published surface, not only the elected one — the design's answer to an
    identity column publishing unexamined. The elected surface is resolved
    first (it is the message key), then the declared set is validated to
    contain it.

    Precondition: the identity-uniformity gate has already run, so the
    stream's populations elect one surface and `ElectionMixedIdentity` is not
    raised here.

    Args:
        sidecar: The typed sidecar.
        stream_name: The declaring stream's name, leading every message.
        kind: The stream's records kind — the owner kind for a membership
            stream.
        declared: The stream's `identity` list, or None for the elected
            surface alone.
        election: The resolved cross-mode election view.
        populations: The populations the stream's keys draw from — the
            spanned populations for a kind stream, the addressed owner
            population set for a membership stream. `None` addresses a flat
            kind.

    Returns:
        The gated projection, `published` in sidecar column order.

    Raises:
        StreamIdentityMissingElected: `declared` omits the elected surface.
        StreamIdentityUnavailable: `presentation_id` is published on a kind
            that mints no surrogate.
        ElectionPresentationUndeclared: `presentation_id` is published for a
            population the registry does not declare.
        ElectionUnionUnsafe: A published surface's spanned key spaces are not
            pairwise union-safe.
    """
```

### Functions — streaming output-name resolution

```python
def resolve_identity_output_key(
    rename: "Mapping[str, str] | None",
    surface: "KeySurface",
) -> str:
    """The wire name of one published identity surface.

    The single producer of every identity output key, consulted by the
    after-image resolvers and by the message-key assembly site, so the key
    map entry and the after-image identity entry cannot diverge.

    Args:
        rename: The stream's declared rename map, or None.
        surface: A published surface.

    Returns:
        The rename target keyed on `surface` when the map carries one, else
        `surface` itself (the contract column name).
    """


def resolve_stream_output_columns(
    sidecar: "Sidecar",
    kind: str,
    properties: "Sequence[str]",
    rename: "Mapping[str, str] | None",
    identity: "IdentityProjection",
) -> list["OutputEntry"]:
    """Resolve a kind-shaped stream's after-image entries.

    The single naming authority. Order is the published identity surfaces in
    sidecar column order, then the selected properties in the column-order
    producer's order. Identity entries take `resolve_identity_output_key`'s
    result; every other entry takes its bare name or its rename target.

    Takes the resolved projection rather than a pre-resolved key, because the
    published set is what decides which rename keys are legal and which names
    are reserved.

    Args:
        sidecar: The typed sidecar.
        kind: The stream's records kind, bare.
        properties: The stream's declared payload projection, bare names.
        rename: The stream's rename map, or None.
        identity: The stream's gated identity projection.

    Returns:
        Ordered entries — the one list the after-image assembly, the JSONL
        renderer, and the Debezium value schema all consume.

    Raises:
        StreamRenameUnresolvable: A rename key names neither a selected
            property nor a published surface. The message names the stream's
            published set when the key is an unpublished surface name.
        StreamOutputNameCollision: Two output keys collide, or one collides
            with a published identity key.
    """


def resolve_membership_output_columns(
    sidecar: "Sidecar",
    membership: "MembershipRef",
    fields: "Sequence[str]",
    rename: "Mapping[str, str] | None",
    owner_identity: "IdentityProjection",
) -> list["OutputEntry"]:
    """The membership analog of resolve_stream_output_columns.

    Order is the published owner identity surfaces in the owner kind's
    sidecar column order, then the selected element fields in element-schema
    declaration order — a scalar field one entry, a reference field its
    `<f>_kind` / `<f>_id` pair renamed in place.

    Args:
        sidecar: The typed sidecar.
        membership: The stream's membership-table address.
        fields: The stream's declared field projection, bare names.
        rename: The stream's rename map, or None.
        owner_identity: The owner's gated identity projection.

    Returns:
        Ordered entries.

    Raises:
        StreamRenameUnresolvable: A rename key names neither a selected field
            nor a published owner surface.
        StreamOutputNameCollision: Two output keys collide, or one collides
            with a published owner identity key or with `event`.
    """
```

### Functions — the `init` election menu

```python
def propose_key_election(
    sidecar: "Sidecar",
) -> "KeyElectionProposal":
    """The cross-mode `keys` proposal: a uniform record_index election plus
    per-population alternatives.

    The active election is `record_index` for every population of every kind
    — universally available and gate-clean by construction, so no
    mode-specific degradation pass is required. Alternatives are offered
    per population by resolvability alone: `record_id` always, and
    `presentation_id` only where the presentation-key registry declares the
    population. Consults the strict registry accessor and shares its refusal
    behavior.

    Args:
        sidecar: The open emit's sidecar.

    Returns:
        The proposal: the active per-population election and, per
        population, the ordered alternatives an emitter renders as commented
        lines. A partitioned kind's active election is a map when at least
        one of its populations carries a `presentation_id` alternative, a
        scalar otherwise.

    Raises:
        ExportError: The emit carries an incoherent presentation-key block.
    """


def render_keys_block(proposal: "KeyElectionProposal") -> list[str]:
    """Render the `keys` block, active lines and commented alternatives.

    The single renderer of the election menu, spliced verbatim by the
    dimensional, source, and streaming `init` engines so one menu cannot
    render three ways. Each population's alternatives precede its active
    line as comments, headed by one line stating that an alternative
    replaces the active line rather than joining it.

    Args:
        proposal: The proposal to render.

    Returns:
        YAML lines, `keys:` first, ready to splice into a candidate config.
    """
```

### Functions — config loading

```python
def load_yaml_mapping(raw: str, label: str, path: "Path") -> object:
    """Parse config YAML, refusing duplicate mapping keys.

    The shared parse step for the export, streaming, and corrupt loaders.
    Duplicate keys are refused rather than resolved last-wins, so an author
    who activates a commented `keys` alternative without removing the active
    line gets a named error instead of silent order-dependent behavior.

    Args:
        raw: The file's text.
        label: The config kind, for the message ('export config',
            'stream config', 'corrupt config').
        path: The file's path, named in the message as every other config
            error names it.

    Returns:
        The parsed YAML document.

    Raises:
        ConfigError: The text is not valid YAML, or a mapping carries the
            same key twice. The duplicate-key message names the file, the
            key, and its line.
    """
```

### Playback — the resolved selection

```python
@dataclass(frozen=True)
class ResolvedRecordSelection:
    """One RecordAtomSelection resolved against the sidecar.

    Gains one field; every existing field is unchanged.
    """

    identity: "tuple[str, ...]"
    """The published identity surfaces, in sidecar column order — the
    resolution of the selection's `identity` (None resolving to the full
    available set: 'record_id', plus 'presentation_id' when the kind mints
    one). Governs the event after-image map and the record_state table;
    never the typed PlaybackEvent fields, and never the fold invocation."""
```

`resolve_selection` gains the rules in § Validation Rules below. Nothing else
in the seam's contract moves: `full_properties` still drives the fold, the
event row set and `seq` are still independent of every projection, and
`PlaybackEvent` is unchanged.

---

## Validation Rules

### Parse-Time (Pydantic)

`KindStream.properties` and `MembershipStream.fields` keep their existing
shape rules — bare names, no `prop__` / `elem__` / `member__` prefix,
duplicate-free, required with no default.

`identity` is optional with no default; when present it is non-empty,
duplicate-free, and its members are the `KeySurface` literal, so an unknown
surface name is unrepresentable rather than validated away. Whether the set
contains the elected surface, and whether the kind can source each member, are
business rules — parse time knows neither the election nor the sidecar.

The `rename` map keeps its shape rules — non-empty when present, non-empty
keys and targets, no two keys sharing one target. Its key-resolution rule
stays where it already is, in the business pass; this design widens that
rule's admissible key set, and adds no parse-time rule.

```python
@model_validator(mode="after")
def kind_stream_well_formed(self) -> Self:
    """Existing rules, plus `identity` shape (non-empty, duplicate-free when
    present; membership of the published set in the election is a business
    rule).

    Raises:
        ValueError: Any existing shape violation, or a malformed `identity`.
    """
```

### Business Rules

Run in the streaming engine's eager pass, before any fold materializes. Each
message leads with the stream name.

**Which names are classes.** The table below follows the shipped convention of
naming rules, not exception types, so it mixes two registers. Three rules here
are new `ExportError` subclasses — `StreamIdentityMissingElected`,
`StreamIdentityUnavailable`, and `StreamPropertyNotAddressable` — the third
because "identity is not a property" is a distinct authoring mistake from "no
such property," and collapsing it into `StreamPropertyResolvable` would hand
the author the wrong remedy. The remaining rows name rules over errors that
already exist.

**Ordering.** The election's own gates run first, unchanged: a stream's
elected surface is resolved and the identity-uniformity gate
(`ElectionMixedIdentity`) has already refused a mixed-election stream before
any projection resolves. `IdentityProjection.elected` is therefore uniform by
precondition, which is why `resolve_identity_projection` neither re-checks it
nor raises it. A stream that is both mixed-election and carries a malformed
`identity` reports the mixing — the election is the earlier and more basic
failure.

| Rule | Checks | Error message |
|---|---|---|
| `StreamIdentityMissingElected` | A declared `identity` contains the stream's elected surface | `"stream '{stream}': identity omits the elected surface '{surface}'; a topic must publish its own key"` |
| `StreamIdentityUnavailable` | `presentation_id` is published only on a kind that mints a surrogate | `"stream '{stream}': the kind '{kind}' mints no presentation_id"` |
| `StreamPropertyNotAddressable` | No selected property names an identity surface | `"stream '{stream}': '{property}' is an identity surface — declare it in identity, not properties"` |
| `StreamPropertyResolvable` | Every selected property names a `prop__<p>` column of the kind | `"stream '{stream}': property '{property}' has no prop__{property} column on kind '{kind}'"` (unchanged) |
| `StreamRenameUnresolvable` | Every rename key names a selected property / field, or a published surface | `"stream '{stream}': rename key '{key}' names no selected {noun}"` — with `"; this stream publishes {surfaces}"` appended only when the key is an unpublished surface name (a plain property-name typo does not advertise identity surfaces) |
| `StreamOutputNameCollision` | No two output keys collide, and none collides with a published identity key or the membership `event` | `"stream '{stream}': output name '{name}' collides with '{other}'"` (unchanged) |
| `StreamChangeScopeUnresolvable` | `only` / `ignore` members are audited payload properties | `"stream '{stream}': {field} entry '{property}' has no prop__{property} column on kind '{kind}'"` (unchanged) |

Reused verbatim over the widened published-surface population (§ Publication
gates): `ElectionPresentationUndeclared`, `ElectionUnionUnsafe`, and the
render-time `ElectedKeyDuplicate`. Unchanged and still scoped to the election
alone: `ElectionMixedIdentity`.

### Playback rules

Run in `resolve_selection` at open — sidecar-only, a schema question, the
seam's established posture. Each raises `PlaybackError`.

| Rule | Checks | Error message |
|---|---|---|
| Identity shape | A given `identity` is non-empty and duplicate-free | `"record atom '{kind}': identity must be non-empty and duplicate-free"` |
| Identity domain | Every member is `record_id` or `presentation_id` | `"record atom '{kind}': '{surface}' is not a tier-1 identity surface"` |
| Identity spine | `record_id` is a member | `"record atom '{kind}': identity must contain record_id — it is the event key"` |
| Identity available | `presentation_id` is published only on a kind that mints one | `"record atom '{kind}': the kind mints no presentation_id"` |
| Properties disjoint | No selected property names an identity surface | `"record atom '{kind}': '{property}' is an identity surface — declare it in identity, not properties"` |

### `init` rules

| Rule | Checks | Behavior |
|---|---|---|
| Alternative resolvability | A `presentation_id` alternative is emitted only for a registry-declared population | Omitted otherwise; never emitted as an unusable option |
| Map/scalar shape | A partitioned kind's active election is a map iff at least one population carries a `presentation_id` alternative | Deterministic from the proposal |
| Active-proposal gate-cleanliness | The uniform `record_index` proposal passes the mode's gates | Holds by construction; no degradation pass |
| Streaming liveness | Every proposed stream is live and the emitted config parses and streams clean | Unchanged; the keys block is now gate-clean by construction |

### Config-load rules

| Rule | Checks | Error message |
|---|---|---|
| Duplicate mapping key | No YAML mapping carries the same key twice, at any depth | `"duplicate key '{key}' in {label} {path} at line {line}"` |

---

## Rationale

- **Projection, not property selection.** The alternative considered — and
  rejected — was admitting `presentation_id` into a stream's `properties`. It
  reads as the smaller change, but it reclassifies a column the base format
  places in the structural block, gives no `temporal_class`, and holds outside
  the value-column population, and which key election already names as one of
  three identity surfaces. The reclassification pays for itself in exceptions:
  a "property" that is not `render`-addressable, not in `only` / `ignore`, not
  `where`-addressable, has no temporal class for the slice-only rule to read,
  and is refused when the population elects it. Five carve-outs for one column
  is the classification being wrong. Projection needs none of them, because the
  surface never enters the payload namespace.

- **The fold stays complete.** Applying the projection in the row-state-events
  fold would impose one consumer's presentation choice on the source event log
  and the playback seam, both of which read the same fold. The layer split (§
  Two identity layers) is what lets three consumers with three audiences share
  one honest derivation.

- **Publishing an identity surface on a stream earns the gates.** The
  ride-sharing half-match is not a naming defect — it is a union-unsafe
  identity column that no gate ever ruled on. Suppression alone would let an
  author re-create it by publishing the surrogate deliberately. Running the
  algebra over every published surface means a published identity column either
  joins cleanly or the export refuses. The seam is the deliberate exception,
  and permissive playback is the whole of the reason (§ The playback seam).

- **Elected-only by default on a stream; full set by default at the seam.** The
  asymmetry is each layer's own established absence convention, not an
  inconsistency. A stream declares what ships — `properties` is required with
  no default and an absent `rename` means bare keys — so an absent `identity`
  publishes the minimum a topic cannot do without. The seam's convention is
  `None` = the full selectable set, and `identity` follows it, which also
  leaves the seam's shipped behavior intact for callers who never ask. The
  asymmetry earns itself twice over on a corrupted tape: the stream must refuse
  a colliding surrogate, and the seam must deliver it.

- **The registry tightening is deliberate.** Publishing `presentation_id`
  requires the registry to declare the population, where today the surrogate
  ships with no claim consulted. This is the claim-consuming-path posture the
  election and `declare_keys` already share, and the escape — not publishing —
  costs the author one line.

- **The menu renderer stays single.** The three `init` engines already splice
  one shared `keys` renderer; this design changes what that renderer emits, it
  does not consolidate three copies. Naming it anyway because the menu is the
  first thing the renderer produces that an author is meant to *edit*, and the
  temptation to let one mode word its comment block differently arrives with
  it — the proposal and its rendering stay one producer for the same reason the
  after-image column order is.

---

## Boundaries

- **Source and dimensional keep per-column author control.** The unifying rule
  is that identity columns are never auto-projected — every publishing surface
  states what it publishes. Source states it through each declared table's
  `columns` list and its identity-column rename key; dimensional states it
  through author-declared dim columns. Both are already per-column and already
  author-owned; adding a second grammar saying the same thing would be a
  regression, not a unification. `identity` exists for the two layers whose
  published identity columns are not author-declared today.

- **Base keeps its standalone surrogate, unprojected and ungated.** Base is the
  one publishing layer that auto-projects an identity surface: under a
  `record_id` or `record_index` election the standalone `presentation_id`
  column ships whenever the kind carries one, and base's grammar renames it but
  cannot drop it. That stands, on a distinction the streaming wire does not
  have — base always ships a complete join surface the election never touches.
  The `<kind>_key` self key and the per-edge `<p>_key` are re-derived from
  `record_index`, one shared dense space per kind, union-safe by construction,
  and they are how base output is meant to be joined
  ([`key-election.md`](../key-election.md) § Rendering: base). Base's identity
  *slot* is election-gated already, and the standalone surrogate sits beside a
  correct key as payload, where a stream's would sit beside the message key
  looking equally key-like. The known limit: an author who finds the column
  misleading can rename it and nothing more. Widening base's grammar to
  suppress it, and gating it when it ships, is a separable change this design
  does not make.

- **The message key stays single.** A topic's key remains the one-entry map of
  the elected surface. Publishing several identity surfaces widens the
  after-image, never the key.

- **Playback tier 2 gains nothing.** Shaped playback compiles a declared target
  shape through the modes and inherits their identity rendering; a second
  grammar at that tier would duplicate the modes'.

- **Playback membership atoms gain nothing.** Their tier-1 payload carries the
  owner's `record_id` only; no surrogate reaches them, so there is nothing to
  project.

- **No new surface on the corrupter or the mixer.** The mixer replays whatever
  the engine produced; corrupters operate below presentation entirely.
