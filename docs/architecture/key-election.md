# Key Election

The emit carries up to three identity surfaces per record — the opaque
`record_id` (always), the dense `record_index` (always), and the
projection-minted `presentation_id` where the `presentation_keys` registry
declares it. This doc owns the cross-mode **key election** surface: a top-level
`keys` config block electing, per population, which surface presents as that
population's exported identity, with every referencing column rendered in its
*target's* elected surface. Four modes consume it — source, base, dimensional,
and streaming (where the elected surface is the message key — § Rendering:
streaming). The flagship shape is the fully-declared kind, where
every identity and edge value is an operational code (`location: ALPHA_007`
joined to a table keyed `ALPHA_007`) rather than a substrate id the contract
forbids consumers to interpret. Forge never mints and never formats — it selects
among surfaces the emit already carries and lines the edges up. Absent the
block, every mode keys and renders record identity by `record_id`.

**Source:**
[`exporters/election.py`](../../src/fabulexa_forge/exporters/election.py)
(`resolve_election`, the `Election` view, the combination gates, the render-time
uniqueness guard),
[`derivations/presentation_key.py`](../../src/fabulexa_forge/derivations/presentation_key.py)
(the presentation-key join relation),
[`config/models.py`](../../src/fabulexa_forge/config/models.py) (`KeySurface`,
`ExportConfig.keys`, `FkClause.target_key`),
[`errors.py`](../../src/fabulexa_forge/errors.py) (the `Election*` /
`ElectedKeyDuplicate` taxonomy). Tests:
[`tests/exporters/test_election.py`](../../tests/exporters/test_election.py),
[`tests/derivations/test_presentation_key.py`](../../tests/derivations/test_presentation_key.py),
plus per-mode suites (`tests/exporters/{source,base}/test_election_{plan,renders}.py`,
[`tests/exporters/dimensional/test_election_fk.py`](../../tests/exporters/dimensional/test_election_fk.py),
[`tests/exporters/streaming/test_election_stream.py`](../../tests/exporters/streaming/test_election_stream.py)).

## Boundary

- **In:** the config `keys` block and dimensional's per-edge `fk.target_key`;
  the sidecar (records kinds, discriminator domains, and
  `Sidecar.presentation_keys()` — the strict accessor and the contract's
  normative union-safety algebra, consumed as the reader ships them); at render
  time, the record-index and presentation-key join relations from the
  derivations layer.
- **Out:** the resolved, gate-checked `Election` view every mode plan consumes
  (none re-derives it); refusals (`ExportError` subclasses); identity and
  referencing columns rendered in elected surfaces. Election emits no notices —
  its failure modes are errors, not degradations.
- Election resolution is a pure function of (sidecar, config); the uniqueness
  guard is its single data-touching check.

## Semantics

### The election grammar

An election addresses a **population**: a flat kind as a whole, or one declared
sub-type of a sub-typed kind (a kind carrying a synthesized `<kind>_type`
discriminator domain — the same shape test the registry's entry grammar uses).
Three surfaces are electable, named by their contract column names:

| Surface | Key-space identity (for the gates) | Type in output |
|---|---|---|
| `record_id` | class `record_id` | `VARCHAR`, verbatim |
| `record_index` | class `record_index`, `prefix ""`, `width 0` | `BIGINT` (digit-rendered in a mixed edge column — below) |
| `presentation_id` | the population's registry entry's `key_space` | The sidecar's declared `presentation_id` type |

The built-in surfaces' key-space identities are **forge-synthesized** instances
of the contract's declared classes, not registry declarations — the registry
declares key spaces for presentation keys only, and its `record_id` /
`record_index` classes are minting strategies for `presentation_id`. The
synthesis is exact by construction: each class's semantics *is* the verbatim
rendering of the corresponding structural column, so the algebra's verdicts
transfer exactly; the collision rule is the contract's, never a local one.

| Config shape | Kind shape | Meaning |
|---|---|---|
| `<kind>: <surface>` | flat | The kind's whole-table election |
| `<kind>: <surface>` | sub-typed | Shorthand: every declared discriminator-domain sub-type elects `<surface>` uniformly |
| `<kind>: {<sub_type>: <surface>, …}` | sub-typed | Per-population election; unlisted sub-types elect `record_id` (the default) |
| `<kind>: {…}` | flat | Load-time error — a flat kind has no populations to address |
| Kind absent from `keys` | any | `record_id` throughout (the default) |

The grammar is per population; whether elected populations may *share one
output table* is the mode plan's identity gate (below — source and base;
dimensional identity columns are author-declared) — one table, one identity
surface.

### Static gates (load/plan time, before any data is read)

All gates resolve from the sidecar and config alone — deterministic, data-free.
The `presentation_id` gates consult the strict accessor, so an incoherent
`presentation_keys` block fails any election-bearing export exactly as it fails
`declare_keys` — on the paths that consume claims, never elsewhere. The gates
run at two moments, both before any data is read: the **resolution gates** at
election resolution (they need only sidecar + config), and the **combination
gates** at each mode's plan step (whether an identity column is shared by
several populations, and which edges a table carries, is mode knowledge —
source splits some kinds into per-population units, base never splits;
dimensional's identity columns are author-declared, so the identity gates are
source's and base's and dimensional runs only the edge gates).

| Gate | Runs at | Rule | On violation |
|---|---|---|---|
| Kind exists | resolution | Every `keys` key names a kind with a declared `records__<kind>` table | `ElectionKindUnknown` |
| Sub-type exists | resolution | Every map key is in the kind's discriminator domain | `ElectionSubTypeUnknown` |
| `presentation_id` declared | resolution | A population electing `presentation_id` has a registry entry — the flat kind's `key`, or the sub-type's `sub_types` entry (`key_for` presence). The uniform-scalar shorthand on a sub-typed kind requires *every* domain sub-type declared | `ElectionPresentationUndeclared`, naming kind, population, and (when the block is absent entirely) that the emit carries no claims |
| Identity uniformity | mode plan (source, base, streaming) | An output table — or a declared stream: a topic's key is one identity space — whose rows span several populations of one kind requires every spanned population to elect the **same surface**. One table, one identity surface; one stream, one key surface (kind-shaped: the spanned populations; membership-shaped: the addressed owner population set — the declared owner `sub_types`, else the owner kind's full domain) | `ElectionMixedIdentity`, naming the table or stream and the differing (population, surface) pairs |
| Identity union safety | mode plan (source, base, streaming) | Under a uniform `presentation_id` election, the spanned populations' key spaces must additionally be pairwise union-safe (`union_safe` over the table above) — two bare-counter siblings collide even on one surface | `ElectionUnionUnsafe`, naming the table or stream and the unsafe pair |
| Edge union safety | mode plan | Every referencing column — a reference edge, a junction owner column, a junction member column, a streaming after-image reference column, or a membership member field — requires its **admitted** target populations' key spaces pairwise union-safe, applied per column. The admitted set is the target kind's full declared domain in the kind-targeted modes — source, base, and streaming (the owner kind's domain for a junction owner column; per member kind for a junction/membership member column; a stream's `sub_types` scope narrows its own rows, never which target populations an edge admits) — and the destination dim's source population set in dimensional. The spaces range over the edge's **resolved surfaces**: the populations' own elections in the kind-targeted modes; in dimensional, the FK's one resolved surface (inherited, or the explicit `target_key`) applied to every admitted population | `ElectionUnionUnsafe`, naming the referencing table/stream · column and the unsafe pair |
| Edge `presentation_id` declared | mode plan (dimensional) | An FK resolving `presentation_id` — inherited or explicit `target_key` — requires every population of the destination dim's source set registry-declared | `ElectionPresentationUndeclared`, naming the edge and the uncovered population |

The kind-exists gate has a consequence worth naming: an emit legally omits
`records__<K>` when kind *K* has no records in the slice, so a config electing
*K* applies only to emits that carry it — on any other emit the election fails
loudly rather than silently ignoring an entry (Principle #7). The strictness is
also what makes every *edge* resolvable by construction: a kind absent from the
emit cannot carry an election, so an edge into it renders the default verbatim
`record_id` and needs no join — no separate target-presence gate exists because
none is reachable.

The union-safety checks reuse the contract's normative algebra verbatim — forge
invents no local collision rule; the built-in surfaces enter the algebra
through their synthesized key spaces, the registry-declared spaces as read.
Consequences worth naming:

- An identity column never carries a mix — uniformity refuses it before any
  collision question arises, so `union_safe` is consulted for identity columns
  only among a uniform `presentation_id` election's key spaces. On *edges* the
  default matters: a partial map leaves unlisted populations at `record_id`,
  which is union-unsafe with every digit-rendered and `uuid` space (opaque
  strings may collide with rendered digits; only another `record_id`-class
  space — a registry entry minting the record id verbatim — is safe beside it,
  the contract's own verdict) — an edge admitting such a population beside a
  digit-rendered or `uuid` election is refused. The remedy is electing
  `record_index` for the undeclared populations — `""` is incomparable with any
  non-empty non-digit prefix — which is exactly `init`'s proposed default.
- Two bare-prefix counter populations are unsafe with each other *and* with
  `record_index`: a uniform `presentation_id` election over both is refused,
  and so is any edge admitting the pair. The escape is `record_index` for every
  population of the kind; per-population tables may keep their own bare-counter
  keys only when no edge has to render them.
- The single-branch guard makes `unique_within` `"branch"` and `"emit"` equally
  table-wide; the distinction is not surfaced (the `declare_keys` posture).

### The identity join relations

A non-`record_id` elected value is always **re-derived at the export horizon**
through a join relation keyed on `record_id` — never read from a physical
`ref_index__` column, never trusted from an after-image:

| Surface | Relation | Provenance |
|---|---|---|
| `record_index` | the record-index derivation | verbatim, creation-constant |
| `presentation_id` | the presentation-key derivation — the record-index relation's exact sibling: horizoned + end-of-tape entry points, creation-time filter, verbatim projection, `DISTINCT`, `active` never a predicate ([`derivations.md`](derivations.md) § The presentation-key derivation) | genesis-minted, never re-minted, never in `history` — creation-constant by the same argument that makes the record-index projection temporally honest |

Both relations are fan-out-free on a conformant emit (`record_id` unique per
kind per branch). The relations compose at the same horizon as the table's
value reconstruction — end-of-tape, `slice_at: T`, or the incremental window's
end — the modes' horizon-binding rule. A table with no value-reconstruction
horizon composes the end-of-tape entry point: the event log (its span
*is* the tape) and every dimensional table (the mode is horizonless; FK
resolution is slice-state). For a windowed event log the window's horizon and
end-of-tape are provably equal — a record's creation precedes its every event —
so this is one rule, not two.

### The elected-key uniqueness guard

When a population elects a non-`record_id` surface, the export asserts at
render time — over **every identity relation the export composes**, whether
for a table's identity column or for an edge render (including an edge into an
`exclude`d kind) — that, restricted to the population set the consumer draws
from:

```
row count  =  COUNT(DISTINCT record_id)  =  COUNT(DISTINCT elected value)
```

with the elected value non-NULL throughout. The check ranges over the join
relation, never the output rows (the event log legitimately repeats an item's
identity once per event; a junction repeats its owner's per binding). The
population set is, per composed relation, the populations the consumer renders
*through that relation*: a table's identity column draws from the table's own
population(s); a source or base edge column from the admitted target
populations electing that relation's surface (a junction owner column, over
the owner kind's populations; a junction member column, per member kind); a
dimensional FK — and the dim-side leg below — from the destination dim's
source population set. A proper-subset restriction composes the records-spine
discriminator (§ Per-row population resolution) as a semi-join; the full
domain needs none.

The three-way equality is deliberate: distinct-values-versus-rows alone would
pass the one corrupted shape `DISTINCT` cannot collapse — a duplicated row
whose elected value was then mutated (`duplicate_rows` + `mutate_cells`
reaches `presentation_id`, unlike `record_index`, whose identity columns sit
outside every cell operation's eligible population) yields two relation rows
for one `record_id` with *distinct* values, which fans the identity join's
spine out. In dimensional the guard runs (a) over every composed relation an
FK render uses and (b) for each dim that is the destination of at least one
edge whose resolved non-`record_id` surface its declared `key` also projects,
over that dim's source population set — the two sides of the join the
agreement check aligned statically.

The guard is deterministic (no sampling), scoped per composed relation's
population set, and per window under an incremental invocation. Violation
fails the export loudly (`ElectedKeyDuplicate`) naming the table or edge and
the surface. This is the one data check election performs, and it is
deliberate: election makes the surface *the* join identity of the output, and
emitting silently-broken joins would violate integrity preservation
(Principle #4). It complements, not replaces, the static gates — the registry
describes the emit as produced, and a downstream corruption may falsify it
(the contract anticipates exactly this).

### Per-row population resolution

Mixed-election rendering is a **kind-targeted edge-column** affair — an
identity column never mixes (the uniformity gate) and a dimensional FK column
is single-surface by construction. A source or base edge into a mixed-election
kind decides each row's surface by the target row's population. The deciding
value is always the **records-spine discriminator column** (`prop__<kind>_type`
on the target's `records__` table), never a fold after-image: a fold `d` row's
after-image discriminator is `NULL`, but its identity join lands on the
records spine where the discriminator is populated. The design relies on an
invariant the export policy leans on elsewhere: **a row's discriminator value
is valid at every T** (the same fact that licenses the sub-typed-discriminator
`slice_only` carve-out and the registry's per-sub-type NULL partition) —
population membership is a per-record constant, so resolving it from the spine
is temporally honest at any horizon.

### Rendering: source

Source's tables are author-declared ([`source.md`](source.md) § Populations and
declared tables); the identity gates run per declared table over its resolved
population set. The elected surface renders as each state table's identity
column, and every referencing column renders its target's election:

| Render site | No election / `record_id` | `record_index` | `presentation_id` |
|---|---|---|---|
| State-table `id` | `record_id` verbatim | `BIGINT` index via the join | declared type via the join; the standalone `presentation_id` payload column is absorbed (it *is* `id` — emitting both would duplicate a column) |
| Reference-valued `prop__<p>` → `<p>` (state render) | verbatim | target's index at the table's horizon | target's `presentation_id` at the table's horizon |
| Junction owner `<K>_id` | verbatim | owner kind's election, same joins | same |
| Junction member `<f>_id` | verbatim | per the member row's kind's election (the `<f>_kind` column remains the disambiguator; cross-kind columns carry no uniqueness claim, per the contract's consumer rules) | same |
| Event-log `item_id` | fold's `record_id` (owner's, for a membership source) | the audited population's election via the identity join — populated on `destroy` rows too (identity is not an after-image) | same |
| Reference-valued entries inside event-log `changes` | verbatim after-image strings | target's index, translated before the diff's lag | target's `presentation_id`, same |

Absorption is the `presentation_id` election's effect alone: under a
`record_id` or `record_index` election the standalone `presentation_id`
payload column ships verbatim.

The event log's `item_id` is a kind-targeted edge render, not a thing-table
identity column: no identity-uniformity gate applies to it, no gate of any
kind applies across item-types, and the edge union-safety gate runs per
item-type over the union of every source addressing it — the full contract is
[`source.md`](source.md) § The event log.

`rename` addressing follows source identity, so a state table's identity-column
rename key is the elected surface's contract column name (`record_index`,
`presentation_id`, or `record_id`); a rename keyed on a surface the election
absorbs or leaves unrendered is unsatisfiable and errors
(`SourceColumnUnresolved`, the message naming the election).

### Rendering: base

The index keys (`<kind>_key`, `<p>_key`) always ship; election chooses the
id-space *value* surface beside them. Self columns follow the table's **own**
population's election; each edge's value column follows its **target**
populations' elections — the two axes are independent:

| Own election | Self identity columns |
|---|---|
| none / `record_id` | `<kind>_key`, `id` |
| `presentation_id` | `<kind>_key`, then the elected value column — default name `id` (it occupies the id-space slot), rename key `presentation_id`; the standalone `presentation_id` payload column is absorbed |
| `record_index` | `<kind>_key` only — the id-space self column is dropped; the index key *is* the election |

The standalone `presentation_id` payload column (present when the kind carries
it) is independent of the identity slot: absorbed under the table's own
`presentation_id` election, verbatim under `record_id` or `record_index`.

| Target populations' elections | Per-edge columns |
|---|---|
| all `record_id` (default) | `prop__<p>` verbatim, `<p>_key` |
| `presentation_id` (uniform, or a mix the edge gate admits) | `prop__<p>` renders each target row's elected surface, `<p>_key` untouched by the election |
| all `record_index` | `prop__<p>` dropped — it would duplicate `<p>_key`, which already carries exactly this surface |
| mix including `record_index` | `prop__<p>` renders per-row (digit-rendered where mixed — below), `<p>_key` untouched by the election |

A mixed target can arise in base only for an `exclude`d target kind: an
*emitted* kind's populations are uniformity-gated (base never splits), so its
inbound edges render one surface.

`rename` addressing follows the same rule as source: the self value column's
rename key is the elected surface's contract column name, and a rename keyed
on a column the election absorbed or dropped is unsatisfiable and errors.

Dropping is reshaping, not fabrication; what is lost is the id-space NULL
separation (a dangled sentinel and an unresolvable edge both render `NULL`
under a joined surface, where a verbatim id column separates them). The
condition table for an elected edge value column:

| Condition | `prop__<p>` under the target's `presentation_id` election |
|---|---|
| Property absent on the record | `NULL` |
| Target created before the horizon | target's `presentation_id` |
| Target created at-or-after the horizon | `NULL` |
| Dangled sentinel (no such record) | `NULL` |

No row renders `NULL` for *undeclaredness*: a population electing
`presentation_id` is registry-declared (gate), and a declared partition is
total non-NULL (the contract) — a `NULL` inside one is a corruption and fails
the uniqueness guard loudly rather than rendering. An author who needs the
separations keeps the default election; the pair `prop__<p>` + `<p>_key`
under `record_id` election is the maximally-informative shape.

### Rendering: dimensional

`fk.to` names a declared dim table; the edge targets that *table*, not the
kind, so an FK column renders exactly **one** surface and its identity
relation is **restricted to the destination dim's source population set** —
the dim's `source.kind`, narrowed to the sub-types the `filter`'s conjunct on the
synthesized `<kind>_type` discriminator selects when the filter carries one (that
conjunct's value set — a scalar's singleton or a list's elements — is the
selected set; any further conjuncts narrow rows *within* it, never widen it), the
kind's whole population set when no discriminator conjunct is present;
`via: reference` and `via: membership` edges alike. The set's cardinality ranges
over any non-empty declared subset, and every gate below takes it as such
([`dimensional.md`](dimensional.md) § Foreign keys).

An `fk` edge without `target_key` inherits that population set's election.
Inheritance requires **one** answer: when the set carries more than one
distinct election, there is nothing coherent to inherit and the edge is
refused at plan time (`ElectionInheritanceAmbiguous`, naming the edge and the
differing elections; the remedies are a discriminator-filtered dim, a unified
election, or an explicit `target_key`). A combined dim over a mixed-election
kind is legal *on its own* — its key is author-declared and election renders
none of its columns; every inbound edge must be explicit, and the guard's
dim-side leg covers its key exactly when an inbound edge's resolved surface
matches it. An explicit `target_key` — `record_id` / `record_index` /
`presentation_id` — overrides per edge. Inheritance is resolution-time only;
the config value the author wrote is never rewritten. `presentation_id`
resolution (inherited or explicit) requires every population of the dim's
source set registry-declared and pairwise union-safe
(`check_edge_union_safety` over the set, the resolved surface passed as
`surface_override`). The explicit `target_key: presentation_id` is gated and
population-set-restricted whether or not a `keys` block is present — its
column-presence condition is the registry-membership gate (statically earlier
than any data read), and an out-of-set target renders `NULL`.

Under a non-`record_id` resolved surface, the FK condition table:

| Condition | FK column |
|---|---|
| Reference property absent, or path unresolvable | `NULL` (the pathfind posture) |
| Resolved target inside the dim's source population set | the target's elected-surface value |
| Resolved target outside the set | `NULL` — the author's `filter` chose the edge's scope (the `via: membership` kind-mismatch posture, generalized) |
| Dangled sentinel (no such record) | `NULL` |

The `NULL` conflates absent, dangled, and out-of-set — the same trade base
accepts, with the same escapes: no election, an explicit
`target_key: record_id` (a dangling reference stays visible as a verbatim id),
or a whole-kind dim.

**Dim-key agreement.** An FK's value is only useful if the destination dim is
keyed on the same surface. When the destination dim's source population
carries a non-default election (and the edge does not override with an
explicit `target_key`), the dim's declared `key` must include a column whose
declaration projects that surface (`from:` the elected contract column);
violation is a load-time `ElectionDimKeyDisagrees` naming the dim, its
declared key sources, and the elected surface. The escape is one line — an
explicit `target_key` on the inbound edges, or re-keying the dim. This is the
same refuse-silently-broken-joins posture as the uniqueness guard, applied
statically: the two sides of every dimensional join are forced to agree before
any data is read.

### Rendering: streaming

Streaming's declared streams are topics, and the elected surface is the
**message key** ([`streaming.md`](streaming.md) § Message key). The
identity-uniformity gate runs per declared stream (one stream, one key
surface) over the populations that stream's keys draw from: for a kind-shaped
stream the spanned populations, and for a membership-shaped stream the
**addressed owner population set** — the declared owner `sub_types`, or the
owner kind's full declared domain when they are omitted. The granularity is
source's narrowed-unit resolution: a stream that addresses part of an owner
kind is gated over that part, so a mixed-election owner kind is splittable per
sub-type across streams rather than refusing every stream over it. `where`
never narrows the addressed set — it is value-level, not population-level, so
the gate and per-row election resolution see the full declared scope whatever
rows the predicate selects. The render sites are:

| Render site | Rendering |
|---|---|
| Message key (every op, including the `d` tombstone) | The record's — for membership-events, the **owner's** — elected surface, as the one-entry key map `{<surface's contract column>: <codec value>}`: the Kafka key, the JSONL `key` map, and the Debezium `d` key-only before-image |
| After-image identity (`c`/`u`; the membership `after`'s owner entry) | The elected surface via the identity join at the fold's `record_id`, keyed by the surface's contract column name. Under a `presentation_id` election the standalone `presentation_id` payload column is absorbed (source's absorption rule); under `record_id` / `record_index` it ships verbatim when the kind carries one |
| Reference-valued `prop__<p>` after-image entries | The target's elected surface through the target's identity join — the state-render analog |
| Membership `member__<f>` reference fields | The member row's kind's elected surface (`__kind` remains the disambiguator) — the junction-member analog |

Elected values keep the streaming codec at every site — codec `VARCHAR`
(`str`) or `null`, `record_index` digit-form, `presentation_id` its declared
value's codec rendering — so no site emits a typed JSON number and streaming's
byte-determinism needs no extra case. Streaming composes every identity
relation at the **end-of-tape entry point** (a record's creation precedes its
every event — the event-log argument), and the uniqueness guard runs per
composed relation naming the stream or edge. The canonical order and merge key
still read the fold's `record_id`: election renders identity, it never
re-sorts. Every electable surface is creation-constant, so a record's events
keep one key for life and the `d` keys the tombstone — the compaction property
the gates guarantee.

### Mixed-election edge columns (source, base, and streaming)

Only a kind-targeted *referencing* column can mix — an identity column is
uniformity-gated and a dimensional FK column is single-surface by
construction. A source, base, or streaming referencing column — a
reference-valued `prop__` column, a junction owner column, a junction member
column, or their streaming after-image analogs — into a
kind whose populations elect different surfaces renders per target row's
population (streaming's after-image values are codec `VARCHAR` at every site,
so the column-type rule below is source's and base's alone). Its type: the
common declared type when all admitted surfaces
agree, else `VARCHAR` with `record_index` values digit-rendered — the
algebra's own rendering model, which is precisely what its collision guarantee
ranges over. A single-election column keeps its native type (`BIGINT` for
`record_index`, the declared type for `presentation_id`). A junction member
column spans *kinds*: its type follows the same rule over the **union** of
every member kind's admitted populations' resolved surfaces — the common
declared type when all agree across the member kinds, else `VARCHAR` with
`record_index` values digit-rendered. The union-safety gate is per member
kind (cross-kind values carry no uniqueness claim; `<f>_kind` disambiguates),
so the cross-kind union feeds the type rule only, never a collision verdict.

### `init` proposals

`init` proposes a complete `keys` block from the sidecar (consulting the
strict accessor, sharing its refusal behavior). The natural proposal:

| Population state | Proposed election |
|---|---|
| Declared in `presentation_keys` (flat `key`, or the sub-type's entry) | `presentation_id` |
| Undeclared (no entry, or block absent) | `record_index` |

Partitioned kinds propose the per-sub-type map (mirroring the registry's own
shape); flat kinds propose the scalar; a map whose values all agree collapses
to the scalar.

**`init` gates its own proposal.** Proposing a config that fails its own gate
would be a broken proposal — and no per-case analysis is maintained to prevent
it; the gates *are* the spec. `init` runs the natural proposal through the
exact machinery the export runs: `resolve_election` plus the target mode's
plan-time gates (identity uniformity + union safety over the mode's table
shapes, edge union safety over the emit's reference graph). Every kind
implicated in a failure degrades to uniform `record_index`, with a YAML
comment naming the gate that forced it. Termination is by construction: a
`record_index`-uniform kind passes every gate — always present, one shared
space per kind, and `""` collides only with digit-prefixed spaces, which the
degradation just removed.

Mode-awareness falls out of using the mode's own gates: a base proposal
degrades every partially-declared sub-typed kind to the kind-wide
`record_index` scalar (base never splits); a source proposal runs the gates
over its own proposed tables — one combined (full-domain) state table per
kind, so a partially-declared sub-typed kind degrades the same way, with the
per-population-tables escape left to the author's edit; a dimensional
proposal keeps mixed maps (its `init` proposes
discriminator-filtered dims) and aligns its dim proposals: each proposed dim's
key column keeps its default name and sources `from:` the population's elected
surface's contract column — the dim-key agreement check holds by
construction, and where the election is `presentation_id` the natural-key
advisory comment is not emitted (the key sourcing consumes the claim) — while
FK candidates remain comments and remain `target_key`-free (an uncommented
candidate inherits, which *is* the aligned rendering; the self-gate runs over
the proposal's grammar, and comments are not grammar); a streaming proposal
runs the gates over its proposed single-population streams (per-stream
uniformity is trivially satisfied there; edge union safety ranges over the
proposed after-images' reference columns and membership member fields). As
with every `init` output, the proposal lands in the author's file where they
see, edit, and own it.

### Interplay

| Feature | Interaction |
|---|---|
| Incremental | Elected values are creation-constant, so a record carries the same elected identity in every window of a run — the merge-key property. The `keys` block participates in the config fingerprint as any config field does. Windowed tables resolve their identity joins at each window's horizon (the horizon-binding rule); tables with no value horizon compose the end-of-tape entry point (§ The identity join relations). |
| `declare_keys` | On a DuckDB export with both features, the declared primary key follows the elected identity column; its table-wide uniqueness is guard-established ([`declared-keys.md`](declared-keys.md) § Key resolution per output table). |
| `slice_only` | Prior and independent: an omitted reference property omits its edge in every encoding, election included. |
| Playback (tier 2) | Shaped playback compiles the same relations; a `slice_at: T` export and the base-shape compile over the tape truncated at `T` are column-for-column equal with election in the config — the end-of-tape entry points are structural, so truncation bounds them with no horizon computed. |
| Corrupt→export composition | Election composes over a corrupted emit like any config. Two sharpenings: a corruption that leaves the carried `presentation_keys` block incoherent fails the strict accessor on election-bearing exports (the claim-consuming-path posture); a corruption that falsifies the elected key itself (e.g. `mutate_cells` on `presentation_id`) fails the uniqueness guard loudly. To surface such defects as data, export without election — the default. |
| `exclude` | An excluded target kind keeps its edge columns in every encoding (the base posture: the author who excluded the kind chose the dangling edge). The election gates still apply to the edge — exclusion changes emitted tables, not the reference graph. A `keys` entry naming an excluded kind is legal and composes the same way: the kind's own tables are not emitted, but edges into it render its elected surface (the records table is still in the emit — exclusion is an output choice, not an input one). The identity gates range over *emitted* tables only, so an excluded kind may legally carry a mixed election — its populations share no output table, and edges into it render per row under the edge gate. |

## Invariants

1. **Determinism.** Election resolution is a pure function of (sidecar,
   config); rendered values are a pure function of (emit, config, code) —
   identical across runs.
2. **Faithful selection.** Every elected value traces verbatim to a base-layer
   cell (`record_id`, `record_index`, `presentation_id`); forge mints,
   formats, and renumbers nothing.
3. **One table, one surface.** No output table's identity column — and no
   declared stream's message key — mixes surfaces: where the mode renders
   identity from the election (source, base, streaming), populations combined
   into one table or stream elect uniformly or the export refuses; a
   dimensional identity column is a single author-declared projection. Only
   kind-targeted edge columns render per row.
4. **Edges agree with identities.** Within one export, a referencing column
   and its target table's identity column render the same surface per
   population, at the same horizon — a join between them succeeds or the
   export failed loudly.
5. **Gates precede data.** Every election refusal except the uniqueness guard
   is static — load/plan time, sidecar-only, before any output exists. The
   guard is the single data-touching check and it can only fail the export,
   never mend it.
6. **Absence composes to identity.** A config without a `keys` block (or
   without an entry for some kind) resolves the affected populations to the
   default `record_id` rendering. The one carve-out is dimensional's explicit
   `target_key: presentation_id`, whose registry gating and population-set
   restriction apply with or without a `keys` block (§ Rendering:
   dimensional).

## Validation Rules

Parse-time, the config is emit-independent: `keys` (when present) is
non-empty, every scalar and map value is a `KeySurface` literal, every
per-kind map is non-empty
([`config/models.py`](../../src/fabulexa_forge/config/models.py)
`keys_well_formed`); `fk.target_key` is a `KeySurface` literal by type.
Emit-dependent checks are deliberately export-time — the same config may be
applied to many emits.

Export-time, the resolution and combination gates are the table in § Static
gates: the resolution gates run inside `resolve_election`; the identity gates
run in the source and base plan steps through `check_identity_election`; the
edge gates run in every mode's plan step through `check_edge_union_safety`
(dimensional passes the FK's resolved surface as `surface_override`, and
additionally runs the inheritance-ambiguity and dim-key-agreement checks — §
Rendering: dimensional). The render-time uniqueness guard (§ The elected-key
uniqueness guard) is the single data-touching rule, raising
`ElectedKeyDuplicate`.

## Rationale

- **Selection, never minting.** The operational identifier a projection mints
  (`ALPHA_007`) is the id a real operational system keys and references by;
  the substrate `record_id` is one the base-format contract forbids consumers
  to interpret. Rendering the minted identifier as the join identity is a
  reshape of surfaces the emit already carries; formats, prefixes, templates,
  and surrogate generation are the projection layer's job. Election therefore
  *selects* — and every elected value traces verbatim to a base-layer cell
  (Principle #3).
- **One config block, cross-mode.** Identity is a property of the exported
  dataset, not of one mode's grammar; a per-edge-only switch would force an
  author to repeat the choice per fact table with nothing checking the dim's
  own key agrees. The `keys` block states the choice once; dimensional edges
  inherit it, and the dim-key agreement check makes both sides of the join
  agree statically.
- **The registry is composed, not re-derived.** `presentation_id` electability,
  key-space identities, and every collision verdict come from
  `Sidecar.presentation_keys()` and the contract's normative union-safety
  algebra, consumed verbatim. A population whose `presentation_id` is NULL
  (undeclared sub-type) or collides across sub-types (union-unsafe bare
  counters) is distinguishable statically — so the gates refuse statically,
  before any data is read.
- **Re-derive at the horizon, never trust an after-image.** Elected values
  resolve through join relations on the records spine because identity
  surfaces are creation-constant; an after-image (`NULL` discriminator on a
  `d` row, `NULL` `presentation_id` on a deletion) reflects event payload, not
  identity. The join relations make identity rendering temporally honest at
  any horizon.
- **The guard exists because the registry describes the emit as produced.** A
  downstream corruption may falsify a claim the static gates accepted;
  emitting silently-broken joins would violate integrity preservation
  (Principle #4). The guard refuses instead — and an author who wants defects
  *surfaced as data* exports without election, the default.
- **`record_index` is the undeclared-population fallback** because its
  synthesized key space (`prefix ""`, digit-rendered) is union-safe beside any
  non-empty non-digit prefix, where `record_id`'s opaque strings are not — the
  contract's own verdict, and the reason `init` proposes it.
- **The NULL conflation trade is accepted** (absent / dangled / out-of-set all
  render `NULL` under a joined surface) because the escapes are cheap and
  explicit: no election, `target_key: record_id`, or a whole-kind dim — and
  the maximally-informative shape (`prop__<p>` + `<p>_key` under `record_id`)
  is the default.

## Boundaries

- **Forge never mints.** No id formats, prefixes, templates, or surrogate
  generation. Formatting is the projection layer's surface; forge selects
  among surfaces the emit carries.
- **The reader and the C-set are consumed, not extended.**
  `Sidecar.presentation_keys()`, `union_safe`, `combined_claim`,
  strict-on-read, and conformance C1–C14 are composed as the reader ships
  them.
- **Dimensional identity columns are author-declared.** Election renders no
  dimensional identity column; its dimensional surface is edge defaults, the
  dim-key agreement check, and `init` proposals. `correlation:` columns are
  raw projections of reference-id columns, not resolved edges — verbatim
  `record_id`-space under any election; an author wanting the elected surface
  there declares an `fk` instead.
- **The record-index derivation and base's key-column contract** — naming,
  horizon binding, density-inherited-never-enforced, edge-keys-re-derived —
  are composed as owned by [`derivations.md`](derivations.md) and
  [`base.md`](base.md); election adds the sibling presentation-key relation
  and composes both.
- **The row-state-events fold is untouched by election.** Elected identity is
  joined onto the fold's output by the source and streaming renders; the
  fold's column set, ordering contract, and two-scope contract are the fold's
  own.
- **No new notice codes.** Election's failure modes are errors, not
  degradations; `rename`, `exclude`, `slice_only`, anchor resolution, and the
  notice channel compose with their own semantics.
- **Corrupters carry no election surface.** No corrupter reads the `keys`
  block and the `defects.json` vocabulary names no election concept; a
  corrupted emit flows through an election-bearing export under the gates and
  guard above.

## Related

| Document | Why |
|---|---|
| [`reader.md`](reader.md) | The strict `presentation_keys` accessor and the union-safety algebra every gate composes |
| [`derivations.md`](derivations.md) | The record-index and presentation-key join relations elected values resolve through |
| [`source.md`](source.md) · [`base.md`](base.md) | The kind-targeted modes — identity columns rendered from the election, per-declared-table / per-table render surfaces; source's event-log `item_id` edge render |
| [`streaming.md`](streaming.md) | The fourth consuming mode — the elected surface as the message key, the after-image render sites, and streaming's per-stream uniformity gate |
| [`dimensional.md`](dimensional.md) | The FK pathfind the resolved surface rides; author-declared keys, `target_key`, `init` stubs |
| [`declared-keys.md`](declared-keys.md) | The declared primary key follows the elected identity column |
| [`incremental.md`](incremental.md) | The window driver; elected values are creation-constant merge keys |
| [`playback.md`](playback.md) | Tier-2 shaped compile over the same relations; truncated-tape equivalence |
| [`corrupters.md`](corrupters.md) | The compositions the guard defends against — falsified claims, duplicated-and-mutated identity |
| [`../../contract/base-format.md`](../../contract/base-format.md) | `record_id` / `record_index` / `presentation_id`, the key-space classes, the normative union-safety algebra |
| [`config/models.py`](../../src/fabulexa_forge/config/models.py) | The `keys` / `target_key` grammar these semantics bind |
| [`../CAPABILITIES.md`](../CAPABILITIES.md) | Feature inventory and status |
| [`README.md`](README.md) | Design index, package layout, staged roadmap |
