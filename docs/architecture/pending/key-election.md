---
status: draft
---

# Key Election

Author-elected identity surfaces per population: which of the emit's identity
columns (`record_id` / `record_index` / `presentation_id`) presents as a
population's id in exported output, with every reference edge rendered in its
target population's elected surface.

---

## Problem

The emit carries up to three identity surfaces per record — the opaque
`record_id` (always), the dense `record_index` (always), and the
projection-minted `presentation_id` (where the `presentation_keys` registry
declares it). Forge lets an author *describe* them (`declare_keys` constraints,
`init` advisories, the dimensional `fk.target_key` per-edge switch) but not
*choose* them: which surface is a table's identity, and which surface a
referencing column carries, are fixed per mode.

The unmet demand: the operational identifier a projection mints (`ALPHA_007`)
is the id a real operational system would key and reference by; the substrate
`record_id` is one the base-format contract forbids consumers to interpret.
Today a source export renders:

```
booking                          entity (alpha rows)
  id        location               id        presentation_id
  b-0102    e-0042        ⟶       e-0042    ALPHA_007
```

The minted identifier is an unlabeled payload column; the FK carries the
substrate id. No configuration reaches the shape a real operational system
would show — `location: ALPHA_007` joined to an entity table keyed
`ALPHA_007`:

- **Source** renders `record_id → id` in every genre and passes reference
  `prop__` columns through verbatim. Neither is electable.
- **Base** ships a fixed pair per identity — the `record_index`-derived
  `<kind>_key` / `<p>_key` beside the id-space columns. The pair's *value*
  surface is not electable.
- **Dimensional** elects per FK edge (`target_key`), so a star with eight facts
  targeting the same dim must repeat the choice eight times, nothing checks the
  dim's own `key` agrees, and `record_index` is not offered.
- **Nothing composes the registry.** A population whose `presentation_id` is
  NULL (undeclared sub-type) or collides across sub-types (union-unsafe bare
  counters — two of the five example emits) is distinguishable statically via
  `presentation_keys`, but no mode consults it when shaping identity.

## Solution

One cross-mode **key election** config block: per population — per sub-type
for sub-typed kinds, per kind for flat — the author elects which surface is
that population's exported identity. Forge never mints and never formats; it
selects among surfaces the emit already carries and lines the edges up:

- **Tables** present the elected surface as their identity column — and **one
  output table has one identity surface**: populations combined into a single
  table must elect the same surface. An identity column never mixes; the
  escape for a mixed-election kind is per-population tables where the mode
  offers them.
- **Edges** — every referencing column renders in its *target's* elected
  surface, re-derived at the export horizon from the reconstructed
  `prop__<p>` through an identity join relation (the record-index derivation's
  pattern; `presentation_id` adds a sibling relation). In the kind-targeted
  modes (source, base) an edge targets a *kind* whose populations may land in
  different tables, so the column renders per target row and *may* mix: each
  value agrees with the identity of the table its own target row lands in. In
  dimensional an edge targets a *declared dim table* — one key, one surface —
  so an FK column renders exactly one surface, its identity relation
  restricted to the dim's source population set.
- **Static gates from the registry** — `presentation_id` is electable exactly
  where `presentation_keys` declares the population; a combined table's
  populations must elect one surface (where the mode renders identity from
  the election — source and base; dimensional identity is author-declared); a
  mixed-election kind is a legal
  reference target exactly when the elected surfaces' key spaces are pairwise
  union-safe under the contract's normative algebra.
- **No election = current behavior** (Principle #7; one owned exception —
  dimensional's shipped `target_key: presentation_id`, subsumed in § What
  Doesn't Change). `init` proposes
  `presentation_id` where the registry declares the population, `record_index`
  where it makes no claim — and gates its own proposal (below).

```yaml
keys:
  actor: presentation_id          # flat-entry kind: whole-kind election
  entity: presentation_id         # fully-declared kind: ALPHA_001… / BETA_001…
booking → location renders ALPHA_007 / BETA_002 — never e-0042
```

The flagship shape is the fully-declared kind: every sub-type carries a
registry entry, the whole-kind scalar elects `presentation_id`, and every
identity and edge value is an operational code. Where the emit genuinely has
undeclared sub-types, the fallback is `record_index` for those populations —
per-population elections then require per-population tables (a split source
unit, a discriminator-filtered dim), and a source or base edge into the kind
renders per target row: `""` (the `record_index` rendering) is
prefix-incomparable with `ALPHA_` / `BETA_`, so the mixed *edge* is
statically collision-free.

## Affected Subsystems

- **Config** — `ExportConfig` gains a top-level `keys` block (cross-mode, like
  `rebase` / `incremental`): per kind, a surface or a per-sub-type map of
  surfaces. Surface names are the contract's column names, never aliased.
- **Election resolution (shared exporter layer)** — a new plan-time resolver
  turns the config block + sidecar into a typed `Election` view: per
  population, the elected surface and its key-space identity (`record_id` /
  `record_index` classes for the built-in surfaces, the registry entry for
  `presentation_id`). The resolution gates live here; the combination gates —
  identity uniformity + union safety, edge union safety — are shared check
  functions the mode plans call (table shape is mode knowledge; the identity
  gates are source's and base's — dimensional identity is author-declared —
  while every mode gates its edges over the populations they admit). Every
  mode consumes the resolved view, none re-derives.
- **Derivations layer** — gains a presentation-key join relation (horizoned +
  end-of-tape entry points), the exact sibling of the record-index derivation:
  `(record_id, presentation_id)` per kind, creation-time filtered, verbatim
  projection, `DISTINCT`, `active` never a predicate.
- **Source mode** — the identity column (`id`) renders the elected surface in
  every genre render (change-log, reference, transaction, junction owner,
  snapshot); reference `prop__` columns and junction member columns render the
  target population's elected surface. The change-log render resolves identity
  through a post-fold join, leaving the row-state-events fold untouched.
- **Base mode** — the id-space *value* surface becomes elective beside the
  always-on `<kind>_key` / `<p>_key` index keys: `presentation_id` election
  renders it in place of the id, `record_index` election drops the id-space
  columns (the index keys already carry the election), no election keeps
  today's shape byte-identical.
- **Dimensional mode** — `fk.target_key` gains `record_index` and becomes an
  optional per-edge *override*: absent, the edge inherits the destination
  dim's source population's election, and the dim-key agreement check forces
  the dim's declared `key` to carry the same surface. Every FK identity
  relation is restricted to the destination dim's source population set — an
  out-of-set target renders `NULL`. Inheritance requires the
  dim's source population set to carry **one** distinct election — a
  mixed-election combined dim is not inheritable; edges into it must be
  explicit. `init` proposes the `keys` block and aligns its dim `key` / FK
  proposals with it.
- **Reader (consumption only)** — the shipped `PresentationKeys` accessor and
  union-safety algebra gain their second consumer; no reader change.
- **`declare_keys` interplay** — where a population elects a surface, the
  declared primary key follows the elected identity column (its uniqueness is
  gate-guaranteed); the existing no-election resolution is unchanged.

## What Doesn't Change

- **No election, no change — one owned exception.** An absent `keys` block
  reproduces today's output byte-for-byte in every mode, for every config
  that does not use dimensional's `fk.target_key: presentation_id`. That
  shipped per-edge switch is deliberately subsumed (§ Rendering per mode) —
  its identity relation becomes restricted to the destination dim's source
  population set (an out-of-set target renders `NULL` where today an orphan
  value renders verbatim) and its column-presence check becomes the stronger
  registry-membership gate — with or without a `keys` block. A breaking
  change to an internal config surface, owned under Principle #9; the
  escapes are `target_key: record_id` or a whole-kind dim. Election
  defaulting is `init`'s proposal, never a silent export-time default.
- **Forge never mints.** No id formats, prefixes, templates, or surrogate
  generation. Formatting is the projection layer's surface; forge selects
  among surfaces the emit carries.
- **Streaming.** CDC events, Kafka message keying, and `StreamConfig` read
  none of this. `record_id` keying is a shipped correctness choice (always
  present, always stable); electing stream payload identity is deferred until
  demand appears — separable because election never changes what streaming
  reads.
- **The reader and the C-set.** `Sidecar.presentation_keys()`, `union_safe`,
  `combined_claim`, strict-on-read, and conformance C1–C14 are consumed
  as shipped, not modified.
- **The record-index derivation and base's key-column contract.** Naming,
  horizon binding, density-inherited-never-enforced, edge-keys-re-derived —
  untouched. Election adds a sibling relation and composes both.
- **The row-state-events fold and its streaming consumers.** Elected identity
  is joined onto the fold's output by the source render; the fold's column
  set, ordering contract, and streaming composition are untouched.
- **Dimensional's author-declared grammar.** `TableDecl.key`, `role`, grains,
  column modes stay author-owned; election supplies edge defaults, the
  dim-key agreement check, and `init` proposals. `correlation:` columns are
  untouched — a degenerate correlation key is a raw projection of a
  reference-id column, not a resolved edge, and it stays verbatim
  `record_id`-space under any election (an author wanting the elected surface
  there declares an `fk` instead).
- **`rename`, `exclude`, `slice_only` policy, anchor resolution, notices
  plumbing.** Existing semantics compose; election introduces no new notice
  codes (its failure modes are errors, not degradations).
- **Corrupter configs and the defect manifest.** No new corrupter surface;
  `defects.json` vocabulary unchanged.

## Semantics

### The election grammar

An election addresses a **population**: a flat kind as a whole, or one
declared sub-type of a sub-typed kind (a kind carrying a synthesized
`<kind>_type` discriminator domain — the same shape test the registry's entry
grammar uses). Three surfaces are electable, named by their contract column
names:

| Surface | Key-space identity (for the gates) | Type in output |
|---|---|---|
| `record_id` | class `record_id` | `VARCHAR`, verbatim |
| `record_index` | class `record_index`, `prefix ""`, `width 0` | `BIGINT` (digit-rendered in a mixed edge column — below) |
| `presentation_id` | the population's registry entry's `key_space` | The sidecar's declared `presentation_id` type |

The built-in surfaces' key-space identities are **forge-synthesized**
instances of the contract's declared classes, not registry declarations —
the registry declares key spaces for presentation keys only, and its
`record_id` / `record_index` classes are minting strategies for
`presentation_id`. The synthesis is exact by construction: each class's
semantics *is* the verbatim rendering of the corresponding structural
column, so the algebra's verdicts transfer unchanged; the collision rule
itself stays the contract's.

| Config shape | Kind shape | Meaning |
|---|---|---|
| `<kind>: <surface>` | flat | The kind's whole-table election |
| `<kind>: <surface>` | sub-typed | Shorthand: every declared discriminator-domain sub-type elects `<surface>` uniformly |
| `<kind>: {<sub_type>: <surface>, …}` | sub-typed | Per-population election; unlisted sub-types elect `record_id` (current behavior) |
| `<kind>: {…}` | flat | Load-time error — a flat kind has no populations to address |
| Kind absent from `keys` | any | `record_id` throughout (current behavior) |

The grammar is per population; whether elected populations may *share one
output table* is the mode plan's identity gate (below — source and base;
dimensional identity columns are author-declared) — one table, one
identity surface.

### Static gates (load/plan time, before any data is read)

All gates resolve from the sidecar and config alone — deterministic,
data-free. The `presentation_id` gates consult the strict accessor, so an
incoherent `presentation_keys` block fails any election-bearing export exactly
as it fails `declare_keys` — on the paths that consume claims, never
elsewhere. The gates run at two moments, both before any data is read: the
**resolution gates** at election resolution (they need only sidecar + config),
and the **combination gates** at each mode's plan step (whether an identity
column is shared by several populations, and which edges a table carries, is
mode knowledge — source splits some kinds into per-population units, base
never splits; dimensional's identity columns are author-declared, so the
identity gates are source's and base's and dimensional runs only the edge
gates).

| Gate | Runs at | Rule | On violation |
|---|---|---|---|
| Kind exists | resolution | Every `keys` key names a kind with a declared `records__<kind>` table | `ElectionKindUnknown` |
| Sub-type exists | resolution | Every map key is in the kind's discriminator domain | `ElectionSubTypeUnknown` |
| `presentation_id` declared | resolution | A population electing `presentation_id` has a registry entry — the flat kind's `key`, or the sub-type's `sub_types` entry (`key_for` presence). The uniform-scalar shorthand on a sub-typed kind requires *every* domain sub-type declared | `ElectionPresentationUndeclared`, naming kind, population, and (when the block is absent entirely) that the emit carries no claims |
| Identity uniformity | mode plan (source, base) | An output table whose rows span several populations of one kind requires every spanned population to elect the **same surface** — one table, one identity surface | `ElectionMixedIdentity`, naming the table and the differing (population, surface) pairs |
| Identity union safety | mode plan (source, base) | Under a uniform `presentation_id` election, the spanned populations' key spaces must additionally be pairwise union-safe (`union_safe` over the table above) — two bare-counter siblings collide even on one surface | `ElectionUnionUnsafe`, naming the table and the unsafe pair |
| Edge union safety | mode plan | Every referencing column — a reference edge, a junction owner column, or a junction member column — requires its **admitted** target populations' key spaces pairwise union-safe, applied per column. The admitted set is the target kind's full declared domain in source and base (the owner kind's domain for a junction owner column; per member kind for a junction member column), the destination dim's source population set in dimensional. The spaces range over the edge's **resolved surfaces**: the populations' own elections in the kind-targeted modes; in dimensional, the FK's one resolved surface (inherited, or the explicit `target_key`) applied to every admitted population | `ElectionUnionUnsafe`, naming the referencing table · column and the unsafe pair |
| Edge `presentation_id` declared | mode plan (dimensional) | An FK resolving `presentation_id` — inherited or explicit `target_key` — requires every population of the destination dim's source set registry-declared | `ElectionPresentationUndeclared`, naming the edge and the uncovered population |

The kind-exists gate has a consequence worth naming: an emit legally omits
`records__<K>` when kind *K* has no records in the slice, so a config electing
*K* applies only to emits that carry it — on any other emit the election fails
loudly rather than silently ignoring an entry (Principle #7). The strictness
is also what makes every *edge* resolvable by construction: a kind absent from
the emit cannot carry an election, so an edge into it renders the default
verbatim `record_id` and needs no join — no separate target-presence gate
exists because none is reachable.

The union-safety checks reuse the contract's normative algebra verbatim —
forge invents no local collision rule; the built-in surfaces enter the
algebra through their synthesized key spaces (§ The election grammar), the
registry-declared spaces as read. Consequences worth naming:

- An identity column never carries a mix — uniformity refuses it before any
  collision question arises, so `union_safe` is consulted for identity
  columns only among a uniform `presentation_id` election's key spaces. On
  *edges* the default matters: a partial map leaves unlisted populations at
  `record_id`, which is union-unsafe with every digit-rendered and `uuid`
  space (opaque strings may collide with rendered digits; only another
  `record_id`-class space — a registry entry minting the record id verbatim
  — is safe beside it, the contract's own verdict) — an edge admitting such
  a population beside a digit-rendered or `uuid` election is refused. The
  remedy is electing `record_index` for the undeclared
  populations — `""` is incomparable with any non-empty non-digit prefix —
  which is exactly `init`'s proposed default.
- Two bare-prefix counter populations (the ride-sharing shape) are unsafe
  with each other *and* with `record_index`: a uniform `presentation_id`
  election over both is refused, and so is any edge admitting the pair. The
  escape is `record_index` for every population of the kind; per-population
  tables may keep their own bare-counter keys only when no edge has to render
  them.
- The single-branch guard makes `unique_within` `"branch"` and `"emit"`
  equally table-wide; the distinction is not surfaced (the shipped
  `declare_keys` posture).

### Identity resolution: the join relations

A non-`record_id` elected value is always **re-derived at the export horizon**
through a join relation keyed on `record_id` — never read from a physical
`ref_index__` column, never trusted from an after-image:

| Surface | Relation | Provenance |
|---|---|---|
| `record_index` | the record-index derivation (shipped) | verbatim, creation-constant |
| `presentation_id` | the presentation-key derivation (new, exact sibling: horizoned + end-of-tape entry points, creation-time filter, verbatim projection, `DISTINCT`, `active` never a predicate) | genesis-minted, never re-minted, never in `history` — creation-constant by the same argument that makes the record-index projection temporally honest |

Both relations are fan-out-free on a conformant emit (`record_id` unique per
kind per branch). The relations compose at the same horizon as the table's
value reconstruction — end-of-tape, `slice_at: T`, or the incremental window's
end — the shipped horizon-binding rule. A table with no value-reconstruction
horizon composes the end-of-tape entry point: the full change-log (its span
*is* the tape) and every dimensional table (the mode is horizonless; shipped
FK resolution is slice-state). For a windowed change-log the window's horizon
and end-of-tape are provably equal — a record's creation precedes its every
event — so this is one rule, not two.

**The elected-key uniqueness guard.** When a population elects a
non-`record_id` surface, the export asserts at render time — over **every
identity relation the export composes**, whether for a table's identity
column or for an edge render (including an edge into an `exclude`d kind) —
that, restricted to the population set the consumer draws from:

```
row count  =  COUNT(DISTINCT record_id)  =  COUNT(DISTINCT elected value)
```

with the elected value non-NULL throughout. The check ranges over the join
relation, never the output rows (a change-log legitimately repeats a record's
identity once per event; a junction repeats its owner's per binding). The
population set is, per composed relation, the populations the consumer
renders *through that relation*: a table's identity column draws from the
table's own population(s); a source or base edge column from the admitted
target populations electing that relation's surface (a junction owner
column, over the owner kind's populations; a junction member column, per
member kind); a dimensional FK — and the dim-side leg below —
from the destination dim's source population set. A proper-subset
restriction composes the
records-spine discriminator (§ Per-row population resolution) as a semi-join;
the full domain needs none. The
three-way equality is deliberate: distinct-values-versus-rows alone would
pass the one corrupted shape `DISTINCT` cannot collapse — a duplicated row
whose elected value was then mutated (`duplicate_rows` + `mutate_cells`
reaches `presentation_id`, unlike `record_index`, whose identity columns sit
outside every cell operation's eligible population) yields two relation rows
for one `record_id` with *distinct* values, which fans the identity join's
spine out. In dimensional the guard runs (a) over every composed relation an
FK render uses and (b) for each dim that is the destination of at least one
edge whose resolved non-`record_id` surface its declared `key` also projects,
over that dim's source population set — the two
sides of the join the agreement check aligned statically. The guard is
deterministic (no sampling), scoped per composed relation's population set,
and per window under an incremental invocation. Violation fails the export
loudly naming the table or edge and the surface. This is the one data check
election performs, and it is deliberate: election makes the surface *the*
join identity of the output, and emitting silently-broken joins would violate
integrity preservation (Principle #4). It complements, not replaces, the
static gates — the registry describes the emit as produced, and a downstream
corruption may falsify it (the contract anticipates exactly this).

### Per-row population resolution

Mixed-election rendering is a **kind-targeted edge-column** affair — an
identity column never mixes (the uniformity gate) and a dimensional FK column
is single-surface by construction. A source or base edge into a
mixed-election kind decides
each row's surface by the target row's population. The deciding value is always the **records-spine
discriminator column** (`prop__<kind>_type` on the target's `records__` table),
never a fold after-image: a change-log `d` row's after-image discriminator is
`NULL`, but its identity join lands on the records spine where the
discriminator is populated. The design relies on an invariant the export
policy already leans on elsewhere: **a row's discriminator value is valid at
every T** (the same fact that licenses the sub-typed-discriminator
`slice_only` carve-out and the registry's per-sub-type NULL partition) —
population membership is a per-record constant, so resolving it from the
spine is temporally honest at any horizon.

### Rendering per mode

**Source.** The elected surface renders as the table's identity column in
every genre, and every referencing column renders its target's election:

| Render site | No election / `record_id` | `record_index` | `presentation_id` |
|---|---|---|---|
| `id` column (reference / transaction / snapshot / split unit) | `record_id` verbatim | `BIGINT` index via the join | declared type via the join; the standalone `presentation_id` payload column is absorbed (it *is* `id` now — emitting both would duplicate a column) |
| Change-log `id` | fold's `record_id` | post-fold join on the fold's `record_id` at the table's horizon (end-of-tape for a full export) — populated on `d` rows too (identity is not an after-image) | same; the fold's after-image `presentation_id` column is absorbed; its `NULL`-on-`d` behavior is superseded by the identity join |
| Reference-valued `prop__<p>` → `<p>` (any genre — reference, transaction, snapshot) | verbatim | target's index at the table's horizon | target's `presentation_id` at the table's horizon |
| Junction owner `<K>_id` | verbatim | owner kind's election, same joins | same |
| Junction member `<f>_id` | verbatim | per the member row's kind's election (the `<f>_kind` column remains the disambiguator; cross-kind columns carry no uniqueness claim, per the contract's consumer rules) | same |

Absorption is the `presentation_id` election's effect alone: under a
`record_id` or `record_index` election the standalone `presentation_id`
payload column is untouched and ships verbatim, as today.

`rename` addressing follows source identity, so the id column's rename key is
the elected surface's contract column name (`record_index`,
`presentation_id`, or `record_id`); a rename keyed on a column the election
absorbed or dropped is unsatisfiable and errors, the `SourceRenameSliceOnly`
posture.

**Base.** The index keys (`<kind>_key`, `<p>_key`) always ship; election
chooses the id-space *value* surface beside them. Self columns follow the
table's **own** population's election; each edge's value column follows its
**target** populations' elections — the two axes are independent:

| Own election | Self identity columns |
|---|---|
| none / `record_id` | `<kind>_key`, `id` (today, byte-identical) |
| `presentation_id` | `<kind>_key`, then the elected value column — default name `id` (it occupies the id-space slot), rename key `presentation_id`; the standalone `presentation_id` payload column is absorbed |
| `record_index` | `<kind>_key` only — the id-space self column is dropped; the index key *is* the election |

The standalone `presentation_id` payload column (present when the kind
carries it) is independent of the identity slot: absorbed under the table's
own `presentation_id` election, untouched and verbatim under `record_id` or
`record_index`.

| Target populations' elections | Per-edge columns |
|---|---|
| all `record_id` (default) | `prop__<p>` verbatim, `<p>_key` |
| `presentation_id` (uniform, or a mix the edge gate admits) | `prop__<p>` renders each target row's elected surface, `<p>_key` unchanged |
| all `record_index` | `prop__<p>` dropped — it would duplicate `<p>_key`, which already carries exactly this surface |
| mix including `record_index` | `prop__<p>` renders per-row (digit-rendered where mixed — below), `<p>_key` unchanged |

A mixed target can arise in base only for an `exclude`d target kind: an
*emitted* kind's populations are uniformity-gated (base never splits), so its
inbound edges render one surface.

`rename` addressing follows the same rule as source: the self value column's
rename key is the elected surface's contract column name, and a rename keyed
on a column the election absorbed or dropped is unsatisfiable and errors.

Dropping is reshaping, not fabrication; what is lost is the id-space NULL
separation (a dangled sentinel and an unresolvable edge both render `NULL`
under a joined surface, where today's verbatim id column separates them). The
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
under `record_id` election remains the maximally-informative shape.

**Dimensional.** `fk.to` names a declared dim table; the edge targets that
*table*, not the kind, so an FK column renders exactly **one** surface and
its identity relation is **restricted to the destination dim's source
population set** — the dim's `source.kind`, narrowed to the sub-type selected
by the `filter`'s conjunct on the synthesized `<kind>_type` discriminator
when the filter carries one (the filter grammar is an equality conjunction,
so any further conjuncts narrow rows *within* that population, never widen
the set), the kind's whole population set when no discriminator conjunct is
present; `via: reference` and `via: membership` edges alike. An `fk` edge without `target_key` inherits that population set's
election. Inheritance requires **one** answer: when the set carries more
than one distinct election, there is nothing coherent to inherit and the
edge is refused at plan time
(`ElectionInheritanceAmbiguous`, naming the edge and the differing elections;
the remedies are a discriminator-filtered dim, a unified election, or an
explicit `target_key`). A combined dim over a mixed-election kind remains
legal *on its own* — its key is author-declared and election renders none of
its columns; every inbound edge must be explicit, and the guard's dim-side
leg covers its key exactly when an inbound edge's resolved surface matches
it. An explicit `target_key` — now `record_id` / `record_index` /
`presentation_id` — overrides per edge. Inheritance is
resolution-time only; the config value the author wrote is never rewritten.
`presentation_id` resolution (inherited or explicit) requires every
population of the dim's source set registry-declared and pairwise union-safe
(`check_edge_union_safety` over the set, the resolved surface passed as
`surface_override`); the shipped
`target_key: presentation_id` behavior is subsumed (its column-presence
check becomes the registry-membership check over the set — strictly
stronger and statically earlier).

Under a non-`record_id` resolved surface, the FK condition table:

| Condition | FK column |
|---|---|
| Reference property absent, or path unresolvable | `NULL` (the shipped posture) |
| Resolved target inside the dim's source population set | the target's elected-surface value |
| Resolved target outside the set | `NULL` — the author's `filter` chose the edge's scope (the `via: membership` kind-mismatch posture, generalized) |
| Dangled sentinel (no such record) | `NULL` |

The `NULL` conflates absent, dangled, and out-of-set — the same trade base
accepts, with the same escapes: no election, an explicit
`target_key: record_id` (a dangling reference stays visible as a verbatim
id, today's shape), or a whole-kind dim.

**Dim-key agreement.** An FK's value is only useful if the destination dim is
keyed on the same surface. When the destination dim's source population
carries a non-default election (and the edge does not override with an
explicit `target_key`), the dim's declared `key` must include a column whose
declaration projects that surface (`from:` the elected contract column);
violation is a load-time `ElectionDimKeyDisagrees` naming the dim, its
declared key sources, and the elected surface. The escape is one line — an
explicit `target_key` on the inbound edges, or re-keying the dim. This is the
same refuse-silently-broken-joins posture as the uniqueness guard, applied
statically: the two sides of every dimensional join are forced to agree
before any data is read.

**Mixed-election edge columns (source and base).** Only a kind-targeted
*referencing* column can mix — an identity column is uniformity-gated and a
dimensional FK column is single-surface by construction. A source or base
referencing column — a reference-valued `prop__` column, a junction owner
column, or a junction member column — into a kind whose
populations elect different surfaces renders per target row's population. Its
type: the common declared type when all admitted surfaces agree, else
`VARCHAR` with `record_index` values digit-rendered — the algebra's own
rendering model, which is precisely what its collision guarantee ranges over.
A single-election column keeps its native type (`BIGINT` for `record_index`,
the declared type for `presentation_id`). A junction member column spans
*kinds*: its type follows the same rule over the **union** of every member
kind's admitted populations' resolved surfaces — the common declared type
when all agree across the member kinds, else `VARCHAR` with `record_index`
values digit-rendered. The union-safety gate stays per member kind
(cross-kind values carry no uniqueness claim; `<f>_kind` disambiguates), so
the cross-kind union feeds the type rule only, never a collision verdict.

### Interplay

| Feature | Interaction |
|---|---|
| Incremental | Elected values are creation-constant, so a record carries the same elected identity in every window of a run — the merge-key property. The `keys` block participates in the config fingerprint as any config field does. Windowed tables resolve their identity joins at each window's horizon (the shipped binding rule); tables with no value horizon compose the end-of-tape entry point (§ Identity resolution). |
| `declare_keys` | On a DuckDB export with both features, the declared primary key follows the elected identity column (under base's `record_index` election that is `<kind>_key` — the id-space column is dropped); its table-wide uniqueness is guard-established. Side `UNIQUE` declarations follow the surviving columns: a `UNIQUE` whose column the election absorbed or dropped (`record_id` under a non-`record_id` election, the standalone `presentation_id` once absorbed) is simply not declared. The no-election resolution table is unchanged, and so is genre eligibility: a table that declares no `PRIMARY KEY` today (source change-log, junction) still declares none — election substitutes the column inside today's resolution table, never widens it. One shipped posture is superseded for the elected identity column alone: `presentation_id` today is always `UNIQUE`, never `PRIMARY KEY` (a claim ranges over non-NULL cells); as an elected identity column it is PK-eligible, its non-NULL table-wide uniqueness guard-established. Non-elected declared claims keep the shipped posture. |
| `slice_only` | Unchanged and prior: an omitted reference property omits its edge in every encoding, election included. |
| Playback (tier 2) | Shaped playback compiles the same relations; a `slice_at: T` export and the base-shape compile over the tape truncated at `T` remain column-for-column equal with election in the config — the end-of-tape entry points are structural, so truncation bounds them with no horizon computed. |
| Corrupt→export composition | Election composes over a corrupted emit like any config. Two sharpenings: a corruption that leaves the carried `presentation_keys` block incoherent fails the strict accessor on election-bearing exports (the shipped claim-consuming-path posture); a corruption that falsifies the elected key itself (e.g. `mutate_cells` on `presentation_id`) fails the uniqueness guard loudly. To surface such defects as data, export without election — the default. |
| `exclude` | An excluded target kind keeps its edge columns in every encoding (the shipped base posture: the author who excluded the kind chose the dangling edge). The election gates still apply to the edge — exclusion changes emitted tables, not the reference graph. A `keys` entry naming an excluded kind is legal and composes the same way: the kind's own tables are not emitted, but edges into it render its elected surface (the records table is still in the emit — exclusion is an output choice, not an input one). The identity gates range over *emitted* tables only, so an excluded kind may legally carry a mixed election — its populations share no output table, and edges into it render per row under the edge gate. |

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
would be a broken proposal — and no per-case analysis is maintained to
prevent it; the gates *are* the spec. `init` runs the natural proposal
through the exact machinery the export would run: `resolve_election` plus the
target mode's plan-time gates (identity uniformity + union safety over the
mode's table shapes, edge union safety over the emit's reference graph).
Every kind implicated in a failure degrades to uniform `record_index`, with a
YAML comment naming the gate that forced it. Termination is by construction:
a `record_index`-uniform kind passes every gate — always present, one shared
space per kind, and `""` collides only with digit-prefixed spaces, which the
degradation just removed.

Mode-awareness falls out of using the mode's own gates: a base proposal
degrades every partially-declared sub-typed kind to the kind-wide
`record_index` scalar (base never splits); a source proposal keeps mixed
per-sub-type maps for split (untracked-only) kinds and degrades unsplit
tracked kinds; a dimensional proposal keeps mixed maps (its shipped `init` already proposes
discriminator-filtered dims) and aligns its dim proposals: each proposed
dim's key column keeps its shipped name and sources `from:` the population's
elected surface's contract column — the dim-key agreement check holds by
construction, subsuming the shipped natural-key advisory comment where the
election is `presentation_id` — while FK candidates remain comments and
remain `target_key`-free (an uncommented candidate inherits, which *is* the
aligned rendering; the self-gate runs over the proposal's grammar, and
comments are not grammar). As with every `init` output, the proposal lands
in the author's file where they see, edit, and own it.

### Invariants

- **Determinism.** Election resolution is a pure function of (sidecar,
  config); rendered values are a pure function of (emit, config, code) —
  identical across runs.
- **Faithful selection.** Every elected value traces verbatim to a base-layer
  cell (`record_id`, `record_index`, `presentation_id`); forge mints, formats,
  and renumbers nothing.
- **One table, one surface.** No output table's identity column mixes
  surfaces: where the mode renders identity from the election (source, base),
  populations combined into one table elect uniformly or the export refuses;
  a dimensional identity column is a single author-declared projection. Only
  kind-targeted edge columns render per row.
- **Edges agree with identities.** Within one export, a referencing column
  and its target table's identity column render the same surface per
  population, at the same horizon — a join between them succeeds or the
  export failed loudly.
- **Gates precede data.** Every election refusal except the uniqueness guard
  is static — load/plan time, sidecar-only, before any output exists. The
  guard is the single data-touching check and it can only fail the export,
  never mend it.
- **Absence composes to identity.** Removing the `keys` block (or any single
  entry) restores current behavior exactly for the affected populations. The
  one carve-out is dimensional's explicit `target_key: presentation_id`,
  whose subsumed gating and population-set restriction apply with or without
  a `keys` block (§ What Doesn't Change).

## Configuration

```yaml
# Source export: operational codes as keys throughout (fully-declared kinds)
mode: source
keys:
  actor: presentation_id            # flat entry: ACTOR_0001…
  entity: presentation_id           # ALPHA_001… / BETA_001…
  booking: record_index             # opaque hex ids swapped for dense integers
source:
  change_delivery: changelog
```

```yaml
# Fallback for a partially-declared kind: per-population elections require
# per-population tables (entity is an untracked, split kind in source)
keys:
  entity:
    alpha: presentation_id          # ALPHA_001…
    beta: presentation_id           # BETA_001…
    gamma: record_index             # no registry entry: dense BIGINT
```

```yaml
# Dimensional: facts inherit each dim's election; one deliberate override
mode: dimensional
keys:
  entity:
    alpha: presentation_id
dimensional:
  tables:
    - name: dim_alpha
      role: dim
      scd: type1
      source: {grain: records, kind: entity, filter: {prop__entity_type: alpha}}
      key: [alpha_id]
      columns:
        - {name: alpha_id, from: presentation_id}
        - {name: alpha_name, from: prop__name}
    - name: fact_transfer
      role: fact
      source: {grain: history_point, kind: booking, property: location}
      key: [booking_id, transferred_at]
      columns:
        - {name: booking_id, from: record_id}
        - {name: alpha_id, fk: {to: dim_alpha, via: reference}}    # inherits → ALPHA_…
        - {name: alpha_ix, fk: {to: dim_alpha, via: reference, target_key: record_index}}
        - {name: transferred_at, derived: {timestamp: {source: sim_time}}}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `keys` | map | No (absent = no election, current behavior) | Per-kind election: a surface name, or a per-sub-type map of surface names for sub-typed kinds |
| `keys.<kind>` | `record_id` \| `record_index` \| `presentation_id`, or map | — | Whole-kind election (scalar) or per-population map |
| `keys.<kind>.<sub_type>` | surface name | — | One population's election; unlisted sub-types keep `record_id` |
| `fk.target_key` (dimensional) | surface name | No (absent = inherit the destination dim's source population set's election) | Per-edge override |

## Interface Contracts

### Config Models

```python
KeySurface = Literal["record_id", "record_index", "presentation_id"]
```

```python
class ExportConfig(StrictBaseModel):
    """(Existing envelope — the one new field.)"""

    keys: dict[str, KeySurface | dict[str, KeySurface]] | None
    """Per-kind key election. A scalar elects the surface for the whole kind
    (every population, for a sub-typed kind); a map elects per sub-type.
    Absent: no election — every mode keys and renders record identity as
    today. Kind and sub-type existence, registry declaration, and union
    safety are export-time gates against the sidecar, not parse-time checks
    (the config is emit-independent; the same config may be applied to many
    emits)."""
```

```python
class FkClause(StrictBaseModel):
    """(Existing model — the changed field.)"""

    target_key: KeySurface | None
    """Which identity surface to write into the FK column. Absent: inherit
    the destination dim's source population set's election (record_id when
    it carries none). Present: per-edge override, gated identically over the
    same population set."""
```

### Runtime Types (shared exporter layer)

```python
@dataclass(frozen=True)
class ElectedPopulation:
    """One population's resolved election.

    `sub_type` is None for a flat kind's whole-table population. `key_space`
    is the surface's key-space identity: the built-in record_id /
    record_index spaces, or the population's registry entry's space for
    presentation_id — the value the combination gates' union-safety
    checks range over.
    """

    kind: str
    sub_type: str | None
    surface: KeySurface
    key_space: KeySpace
```

```python
@dataclass(frozen=True)
class Election:
    """The resolved, gate-checked election for one export invocation.

    Constructed by `resolve_election` only; construction implies the
    resolution gates have passed (kind existence, sub-type existence,
    presentation_id declaration). The combination gates need mode
    knowledge — which tables span several populations, which edges a table
    carries — and run at each mode's plan step through
    `check_identity_election` / `check_edge_union_safety`. Populations
    absent from the config resolve to record_id — the view is total over
    the emit's kinds.
    """

    def surface_for(self, kind: str, sub_type: str | None) -> KeySurface:
        """The elected surface for a population.

        Args:
            kind: A kind with a declared records table in the emit.
            sub_type: The population's discriminator value, or None for a
                flat kind (and for a sub-typed kind under the uniform-scalar
                shorthand, any declared sub_type resolves identically).

        Returns:
            The elected surface; 'record_id' for any population the config
            does not address.

        Raises:
            KeyError: `kind` has no records table in the emit, or `sub_type`
                is not in the kind's discriminator domain.
        """

    def populations_for(self, kind: str) -> tuple[ElectedPopulation, ...]:
        """Every population of a kind with its resolved election.

        One entry for a flat kind; one per declared discriminator-domain
        sub-type for a sub-typed kind, declaration order.

        Args:
            kind: A kind with a declared records table in the emit.

        Returns:
            The kind's populations, resolved.

        Raises:
            KeyError: `kind` has no records table in the emit.
        """

    def is_default(self, kind: str) -> bool:
        """Whether every population of a kind resolves to record_id.

        A kind-local fact — it covers the kind's own identity columns
        only. A table's referencing columns follow their *target*
        populations' elections, so the election-free render fast-path
        test is `is_default` over the kind AND every kind the table's
        referencing columns target (junction owner and member kinds
        included), never this call alone.

        Args:
            kind: A kind with a declared records table in the emit.

        Returns:
            True iff no population of `kind` elects a non-record_id surface.

        Raises:
            KeyError: `kind` has no records table in the emit.
        """
```

### Functions (shared exporter layer)

```python
def resolve_election(
    sidecar: Sidecar,
    keys: dict[str, KeySurface | dict[str, KeySurface]] | None,
) -> Election:
    """Resolve and gate the config's key election against an emit.

    Pure function of (sidecar, config); consults
    `sidecar.presentation_keys()` — and therefore shares its
    strict-on-read refusal — exactly when some population elects
    presentation_id. `keys=None` resolves to the all-default election.

    Args:
        sidecar: The emit's sidecar view.
        keys: The config `keys` block, verbatim.

    Returns:
        The resolved election, total over the emit's kinds.

    Raises:
        ElectionKindUnknown: A config key names no declared records kind.
        ElectionSubTypeUnknown: A map key is outside the kind's
            discriminator domain, or a map addresses a flat kind.
        ElectionPresentationUndeclared: A population elects presentation_id
            without a registry entry (the uniform-scalar shorthand requires
            every domain sub-type declared); the message names the
            population and whether the block is absent entirely.
        PresentationKeysInvalidError: The registry block is present and
            incoherent (propagated from the strict accessor).
    """
```

```python
def check_identity_election(
    election: Election,
    kind: str,
    populations: Sequence[str],
    table_name: str,
) -> None:
    """Gate one output table's identity column against its population mix.

    Called by the source and base plan steps for every output table whose
    rows span more than one population of one kind (an unsplit sub-typed
    source table, a base flat table over a sub-typed kind). A
    single-population table needs no call. Dimensional never calls this
    gate: its identity columns are author-declared (`TableDecl.key` +
    `from:`), never election-rendered — its identity discipline is the
    dim-key agreement check and the guard's dim-side leg. Passes
    when every spanned population elects the same surface (one table, one
    identity surface) and — under a uniform presentation_id election — the
    populations' key spaces are pairwise union-safe.

    Args:
        election: The resolved election.
        kind: The table's records kind.
        populations: The discriminator values whose rows the table carries
            (the kind's full declared domain for an unfiltered table).
        table_name: The output table identity, for the error.

    Returns:
        None.

    Raises:
        ElectionMixedIdentity: The spanned populations elect differing
            surfaces; the message names `table_name`, the (population,
            surface) pairs, and the remedy (per-population tables where the
            mode offers them, unifying the election, or no election).
        ElectionUnionUnsafe: A uniform presentation_id election whose key
            spaces contain a pairwise-unsafe pair (bare-counter siblings);
            the message names `table_name`, the pair, and the remedy
            (electing record_index for every population of the kind).
    """
```

```python
def check_edge_union_safety(
    election: Election,
    target_kind: str,
    populations: Sequence[str],
    edge_name: str,
    surface_override: KeySurface | None = None,
) -> None:
    """Gate one referencing column against its admitted target populations.

    Called by each mode's plan step per referencing column: per reference
    edge, per junction owner column, and per junction member kind.
    `populations` is the target population set the column admits: the
    target kind's full declared domain in source and base (edges are
    kind-targeted; the owner kind's domain for a junction owner column,
    per member kind for a junction member column), the destination dim's
    source population set in dimensional.

    The gated key spaces are the edge's *resolved* surfaces.
    `surface_override=None` (the kind-targeted modes) resolves each
    population through `election`. Dimensional always passes the FK's one
    resolved surface — the inherited election or the explicit
    `target_key` — and every admitted population resolves to it:
    `presentation_id` through the population's registry entry (an
    uncovered population is refused), the built-ins through their
    synthesized spaces. A single-population set, or a mixed set whose
    resolved key spaces are pairwise union-safe, passes.

    Args:
        election: The resolved election.
        target_kind: The referencing column's `references` target kind.
        populations: The admitted target populations' discriminator values
            (the kind's full declared domain for a kind-targeted edge).
        edge_name: The referencing table · column identity, for the error.
        surface_override: The edge's uniformly resolved surface
            (dimensional FKs), or None to resolve each population through
            `election` (kind-targeted edges).

    Returns:
        None.

    Raises:
        ElectionUnionUnsafe: The admitted populations' resolved key spaces
            contain a pairwise-unsafe pair; the message names `edge_name`,
            the pair, and the contract's remedy (per-population targets, or
            a record_index election for the colliding populations).
        ElectionPresentationUndeclared: `surface_override` is
            presentation_id and an admitted population has no registry
            entry; the message names `edge_name` and the population.
        KeyError: `target_kind` has no records table in the emit — a caller
            error: a kind absent from the emit cannot carry an election
            (the kind-exists gate), so callers skip gating such edges and
            render the default verbatim record_id.
    """
```

### Functions (derivations layer)

```python
def build_presentation_key_at_sql(
    sidecar: Sidecar,
    fork_path: str,
    kind: str,
    horizon_ns: int,
) -> str:
    """A kind's record_id → presentation_id join relation at a horizon.

    The record-index derivation's exact sibling: one row per distinct
    (record_id, presentation_id) pair among the kind's records created
    strictly before `horizon_ns`, filtered to `fork_path`. Verbatim
    projection (genesis-minted, never re-minted — temporally constant by
    the same argument as record_index); DISTINCT to keep a consumer's join
    one-to-one over exactly-duplicated corrupted rows; `active` never a
    predicate (a deactivated record remains a legal reference target).
    Declares no ORDER BY — a join relation, not a fold. NULL
    presentation_id rows project verbatim (an undeclared population's
    honest surface value).

    Args:
        sidecar: The emit's sidecar view.
        fork_path: The branch to filter to.
        kind: The records kind; its table must carry a presentation_id
            column.
        horizon_ns: Exclusive creation-time horizon.

    Returns:
        A SQL SELECT producing PRESENTATION_KEY_COLUMNS
        (record_id, presentation_id).

    Raises:
        TableNotFoundError: No records__<kind> table in the emit.
        ExportError: The kind's table declares no presentation_id column —
            a caller gating error (the election gates make it unreachable
            from a gated plan).
    """
```

```python
def build_presentation_key_at_end_sql(
    sidecar: Sidecar,
    fork_path: str,
    kind: str,
) -> str:
    """The presentation-key relation's end-of-tape entry point.

    The same DISTINCT relation with no horizon parameter and no horizon
    predicate — structural in the state-at sense, so composed over a
    truncated base relation it is bounded by the truncation with no horizon
    computed. Equals the horizoned entry point at any horizon strictly
    beyond every creation instant of the composed relation.

    Args:
        sidecar: The emit's sidecar view.
        fork_path: The branch to filter to.
        kind: The records kind; its table must carry a presentation_id
            column.

    Returns:
        A SQL SELECT producing PRESENTATION_KEY_COLUMNS
        (record_id, presentation_id).

    Raises:
        TableNotFoundError: No records__<kind> table in the emit.
        ExportError: The kind's table declares no presentation_id column.
    """
```

### Errors

```python
class ElectionKindUnknown(ExportError):
    """A `keys` entry names a kind with no records table in the emit."""

class ElectionSubTypeUnknown(ExportError):
    """A `keys` map addresses a sub-type outside the kind's discriminator
    domain, or addresses a flat kind with a map."""

class ElectionPresentationUndeclared(ExportError):
    """A population elects presentation_id without a presentation_keys
    entry covering it — or a dimensional edge resolves presentation_id
    (inherited or explicit) over a source population set with an
    uncovered population."""

class ElectionMixedIdentity(ExportError):
    """An output table combines populations electing differing surfaces —
    one table, one identity surface; refused at plan time, naming the
    table and the (population, surface) pairs."""

class ElectionUnionUnsafe(ExportError):
    """Elected key spaces admit a value collision — among a uniform
    presentation_id election's populations on one identity column, or
    across a reference edge's admitted target mix."""

class ElectionInheritanceAmbiguous(ExportError):
    """A dimensional FK without an explicit `target_key` targets a dim
    whose source population set carries more than one distinct election —
    nothing coherent to inherit; names the edge and the differing
    elections."""

class ElectionDimKeyDisagrees(ExportError):
    """A dimensional FK's resolved surface (inherited from the destination
    dim's source population's election, with no explicit target_key
    override) is not among the destination dim's declared key columns'
    sources; names the dim, its key sources, and the elected surface."""

class ElectedKeyDuplicate(ExportError):
    """The render-time uniqueness guard: over a composed identity
    relation, restricted to the consuming population set, row count,
    COUNT(DISTINCT record_id), and COUNT(DISTINCT elected value) are not
    all equal, or an elected value is NULL; names the table or edge and
    the surface."""
```

## Validation Rules

### Parse-Time (Pydantic)

```python
@model_validator(mode="after")
def keys_well_formed(self) -> Self:
    """`keys` (when present) is non-empty; every scalar and map value is a
    KeySurface literal; every per-kind map is non-empty. Emit-dependent
    checks (kind/sub-type existence, registry declaration, union safety)
    are deliberately not here — the config is emit-independent."""
```

```python
@model_validator(mode="after")
def target_key_surface(self) -> Self:
    """`fk.target_key` (when present) is a KeySurface literal."""
```

### Business Rules

| Rule | Runs at | Checks | Error |
|---|---|---|---|
| `ElectionKindExists` | resolution | Every `keys` key names a declared records kind | `ElectionKindUnknown` |
| `ElectionShapeMatchesKind` | resolution | Map ⇒ kind is sub-typed; map keys ⊆ discriminator domain | `ElectionSubTypeUnknown` |
| `ElectionPresentationDeclared` | resolution | `presentation_id` elections covered by the registry (strict accessor; uniform shorthand needs full coverage) | `ElectionPresentationUndeclared` |
| `ElectionIdentityUniform` | mode plan (source, base) | Per output table spanning several populations: all spanned populations elect the same surface (`check_identity_election`) | `ElectionMixedIdentity` |
| `ElectionIdentityUnionSafe` | mode plan (source, base) | Per output table under a uniform `presentation_id` election: spanned key spaces pairwise union-safe (`check_identity_election`) | `ElectionUnionUnsafe` |
| `ElectionEdgeUnionSafe` | mode plan | Per referencing column (reference edge / junction owner column / junction member kind): admitted-population mix pairwise union-safe under the column's resolved surfaces (`check_edge_union_safety` over the admitted set; dimensional passes the FK's resolved surface as `surface_override`) | `ElectionUnionUnsafe` |
| `ElectionEdgePresentationDeclared` | mode plan (dimensional) | Per FK resolving `presentation_id` (inherited or explicit `target_key`): every population of the destination dim's source set registry-declared (`check_edge_union_safety` under the override) | `ElectionPresentationUndeclared` |
| `ElectionInheritanceUnambiguous` | mode plan (dimensional) | Per inheriting FK: the destination dim's source population set carries exactly one distinct election | `ElectionInheritanceAmbiguous` |
| `ElectionDimKeyAgreement` | mode plan (dimensional) | Per inheriting FK: the destination dim's `key` includes a column sourced `from:` the elected surface | `ElectionDimKeyDisagrees` |
| `ElectedKeyUnique` | render-time guard | Per composed identity relation, restricted to the consuming population set: rows = distinct `record_id` = distinct elected value, all non-NULL (per window) | `ElectedKeyDuplicate` |
