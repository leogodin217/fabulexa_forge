---
status: draft
---

# Source-Mode Row Selection

Optional row selection on source-mode declared units: a `where` predicate —
gated to `temporal_class: constant` columns — on state tables, junction
tables, and event-log sources, plus owner sub-type selection (`sub_types`) on
membership units. A kind whose rows partition on an undeclared-but-constant
property, and a sub-typed kind's membership estate, can each be split into
separate declared tables with separate audit streams.

---

## Problem

Two gaps with one root: source mode's row selectors stop at the records
surface.

**A constant-property partition cannot split.** Source mode's only row
selector is `sub_types`, which resolves against the sidecar's *declared*
discriminator domain. Concrete case (realism QA, aug11 round): the
ride-sharing emit's `journey_instance` interleaves 6,582 rider trips and 1,987
driver shifts in one table. The partitioning column exists —
`prop__journey_type`, values `rider_journey` / `driver_journey`,
`temporal_class: constant`, `history_tracked: false` — but the emit declares
no journey sub-types, so the config can only rename the combined table
(`user_session`), not split it:

```yaml
# Today: the only legal declaration is one combined table.
- name: user_session
  kind: journey_instance        # sub_types: would error — flat kind
```

A rider's trip and a driver's shift are records no real app keeps in one
table — they belong to different subsystems, with different lifecycles and
different consumers. The interleaved table is a shape no real source system
ships, and renaming it cannot repair that: the mode's own charter — output
that looks like a real system's tables — is undeliverable for this emit.

**A membership estate cannot split at all — even where `sub_types` splits its
owner.** Membership units (junction declarations, membership event sources)
take no selection of any kind. Concrete case (NHS example, shipped): the
config splits `resource` into `ward` / `theatre` / `diagnostic` state tables
by `sub_types`, but `membership__resource__holders` can only be declared
whole, so the export carries one `consultant_allocation` junction whose owner
column points into three different tables row by row, and one undivided
`resource.holders` join/leave stream. No real app shapes an association table
that joins against a different table depending on the row.

The `where` blocker is deliberate: under horizon reconstruction a predicate on
a **history-tracked** property is ambiguous — the value as-of-the-horizon and
the current records value select different row sets. Dimensional's records
grain never poses the question (current state by construction); source's
windowed state snapshots do.

## Solution

Two moves sharing one temporal-honesty argument.

**A `where` predicate, constant-gated.** State tables, junction tables, and
event-log sources gain an optional `where` — a mapping of column → predicate
value, inheriting the shared config row-predicate grammar unchanged (scalar
compiles to `=`, non-empty distinct list to `IN`, entries over distinct
columns AND-joined, rendered by the one rendering authority, literal-typed
from the sidecar) — **gated to `temporal_class: constant` payload
properties**.

The gate is the design's core move. It does not *answer* the
as-of-which-horizon question; it makes the question unposable: a
`constant`-class property's value is identical at every horizon — the mode
already renders constant properties current in windowed snapshots as its
declared temporal-honesty exception — so the full export, every incremental
window, and every event time select the same rows. Row membership under a
`where` is horizon-invariant by construction.

**Owner-keyed selection on membership units.** A membership unit's rows carry
no owner attributes, so its selection reads the **owner**: `sub_types` (legal
on `membership:` declarations, selecting the owner's declared sub-types) and
`where` (keyed on the owner's constant-class payload properties). Both
evaluate through the **parent lookup** — a fan-out-free identity join from the
membership rows to the owner kind's records spine, the same per-row device
records-source narrowing composes (§ The parent lookup). Discriminator splits
spell `sub_types` and constant-property splits spell `where` on every
declaring unit — one rule, no membership-only carve-out.

State tables, junction tables, and events sources ship together because they
are coupled: splitting a state table without splitting its audit stream — or
splitting an owner kind without splitting its junctions and their join/leave
streams — leaves one undivided surface covering both halves, the exact
incoherence row selection exists to remove.

```yaml
source:
  tables:
    - name: trip
      kind: journey_instance
      where: {prop__journey_type: rider_journey}
    - name: driver_shift
      kind: journey_instance
      where: {prop__journey_type: driver_journey}
    - name: trip_passengers                      # owner-keyed junction split
      membership: {kind: journey_instance, property: passengers}
      where: {journey_type: rider_journey}
    - name: ward_allocation                      # owner sub-type junction split
      membership: {kind: resource, property: holders}
      sub_types: [ward]
  events:
    name: audit_log
    sources:
      - kind: journey_instance
        where: {journey_type: rider_journey}
        item_type: trip
      - kind: journey_instance
        where: {journey_type: driver_journey}
        item_type: driver_shift
```

## Affected Subsystems

- **Source exporter — declaration grammar.** `SourceTableDecl` and
  `SourceEventSourceDecl` each gain an optional `where` field, valid with
  either population source (`kind` or `membership`). `sub_types` becomes
  legal alongside `membership` (owner sub-type selection); its
  kind-only parse rule retires. Key forms follow each unit's addressing
  convention: source column names (`prop__<p>`) on records-backed tables;
  bare property names on events sources (the `only` / `ignore` / `rename`
  convention) and on membership units, where the subject is the *owner*
  kind and owner properties are not columns of the unit at all.
- **Source exporter — state render.** The population filter AND-composes the
  predicate condition: a state table renders the records rows that satisfy
  its population set *and* its predicate. Predicates evaluate over source
  (base-layer) values — before rename, before elected-surface rendering.
- **Source exporter — junction render.** Gains the owner-narrowing semi-join:
  a junction with `sub_types` / `where` renders the membership intervals
  whose owner satisfies the selection. Interval columns, element fields,
  member pairs, `columns` / `rename` — unchanged.
- **Source exporter — event log.** A source's selection narrows which
  *records* (or, for a membership source, which *owners'* intervals) feed its
  audit stream, by the per-row records-spine device — temporally honest at
  every event time because discriminators are creation-constant and `where`
  columns are constant-gated. The pairwise-disjointness gate extends to
  selection-aware disjointness (below). The log's `id` numbers the configured
  events as today: a selection-excluded record's events consume no number
  (the `only` / `ignore` posture — a log numbers what it was configured to
  audit).
- **Source exporter — `init` proposal engine.** The per-sub-type state-table
  default extends to a sub-typed owner's membership estate: per-sub-type
  junction stubs and per-sub-type commented membership event-source entries
  (§ `init` proposals). No `where` is ever proposed.
- **Row-predicate grammar.** Source's `where` fields become surfaces of the
  one shared grammar: `PredicateValue` carries well-formedness, the one
  rendering authority compiles every condition, literal typing is
  sidecar-resolved and total. The grammar itself is unchanged — no new
  operators, no new value forms. The standing rule that source carries no row
  predicate retires, replaced by the constant-column gate; **base mode still
  carries none**.

## What Doesn't Change

- **Base mode carries no row predicate.** Its contract is "every records
  kind, one flat table"; row filtering there is a different feature and
  remains undecided.
- **`sub_types` remains the discriminator surface — uniformly.** A `where`
  key naming the declared discriminator is refused with a pointer to
  `sub_types`, on membership units exactly as on records-backed ones — one
  selection per partition axis, no second spelling to drift.
- **Element fields are never predicate-addressable.** Membership-unit
  selection reads the owner, never the element schema: element fields carry
  no `temporal_class`, and a `where` key naming one is unresolved.
- **No owner-attribute projection into junction rows.** The alternative
  rendering of a split kind's memberships — one merged polymorphic junction
  enriched with an owner-type column — is a different feature with no case in
  hand now that splitting is expressible; junction columns stay exactly the
  membership surface.
- **The predicate grammar is unchanged.** Equality and set membership only;
  scalar/list form rule; no range, negation, or null tests.
- **`init` proposes no `where`.** A de facto discriminator is not
  mechanically distinguishable from any other constant enum property;
  proposing splits from value observation would be invention. Author
  judgment, applied by hand. A sub-typed owner's membership splits are
  different — deterministic from the declared discriminator domain — and
  `init` proposes them (§ `init` proposals).
- **Dimensional, streaming, corrupters, writers, playback** — untouched. The
  corrupter's row selector remains its own grammar.
- **Key election, `declare_keys`, anchor resolution, `kind_labels`** —
  unchanged. Two selection-split sources of one kind — or of one
  `(kind, property)` — may share an item-type or declare distinct ones
  (§ Validation Rules); the union-safety gate runs per resolved item-type
  over the union of its sources' **addressed** populations, exactly as
  today — where a membership unit's addressed set now follows its owner
  `sub_types`, and `where` never narrows it (§ The parent lookup).

## Semantics

### The constant-column gate

A `where` key must name a **payload property** of the declaring unit's
**subject kind** — the declared kind on a records-backed unit, the **owner**
kind on a membership unit — whose `temporal_class` is `constant`. Key form:
source column name (`prop__<p>`) on a records-backed table; bare property
name (`<p>`) on events sources and membership units. Resolution is against
the subject kind's payload-property set only: a bare key on a membership
unit that names both an owner property and an element field resolves to the
owner property; the element-field refusal below diagnoses a key that matches
an element field and nothing on the owner.

| `where` key names | Result |
|---|---|
| A `constant`-class payload property of the subject kind | Accepted |
| A `tracked`-class property | Refused (`SourceWhereNotConstant`) — under horizon reconstruction its as-of value and current value select different rows |
| A `slice_only` property | Refused (`SourceWhereNotConstant`) — its past is unknowable, so row selection cannot read it (the slice-only omission posture; § Validation Rules for the message variants) |
| The subject kind's declared discriminator (`prop__<K>_type` / `<K>_type`) | Refused (`SourceWhereOnDiscriminator`), pointing at `sub_types` — including the slice-only-exempt sub-typed discriminator, and on membership units, where `sub_types` selects owner sub-types |
| A structural column (`record_id`, `created_sim_time`, `active`, …) | Refused (`SourceWhereColumnUnresolved`) — structural columns are not payload properties and are not predicate-addressable |
| An element field of a membership unit (and no owner property of that name) | Refused (`SourceWhereColumnUnresolved`) — element fields carry no `temporal_class`; selection reads the owner |
| A column not on the subject kind | Refused (`SourceWhereColumnUnresolved`) — as any `columns` / `rename` key |

The gate reads the column's declared class, never its values. Because
`constant` means current-equals-genesis, the predicate's row selection is
identical at the tape's end, at every incremental window horizon, and at every
event time — the row-membership question that kept source predicate-free never
arises.

The gate governs *keys*, and every key failure above is an error — the key
names the wrong axis or an unusable column, and no sensible export can ship.
Predicate *values* follow dimensional's shipped posture instead: an element
outside a `where` column's declared `enum_domains` entry draws a per-element
`discriminator-value-unobserved` notice, never an error, and a column with no
`enum_domains` entry is unchecked (§ Validation Rules). One line holds in
both modes: **the key axes error; the value axis notices.**

The value axis's tolerance ends at the column's declared type. Every `where`
element is cast to its resolved column's sidecar-declared DuckDB type at plan
time — the same cast the rendering authority compiles into the predicate,
constant-evaluated on every `where`-bearing unit, gated or not — and an
element the type cannot cast is refused (`SourceWhereValueUncastable`, naming
the element), before any write. This is not the domain check hardened: an
out-of-domain value may be observed by another emit of the config's family; a
type-invalid value is valid against none, and the rendered `CAST` would
otherwise raise at query time, mid-export. The disjointness gate's
typed-value comparison (below) reuses exactly these plan-time cast results.

### The parent lookup

Selection on a membership unit evaluates against the **owner**: an identity
join from the membership rows' owner column (`record_id`) to the owner kind's
records spine, where the discriminator and the predicate columns live. The
join is fan-out-free (`record_id` is unique on the spine) and horizon-free
(the discriminator is creation-constant; `where` columns are constant-gated),
so it is exactly the per-row records-spine device the records-source
`sub_types` narrowing already composes, applied from the membership side. It
is a read for *selection only*: no owner attribute is projected into the
unit's columns, and the membership surface (interval columns, element fields,
member pairs) is untouched.

Owner `sub_types` narrows the unit's **addressed owner population set**: a
membership unit declaring `sub_types` addresses exactly those
`(kind, sub_type)` populations, as a records-backed declaration does — the
set the item-type union-safety gate ranges over, and the surface union that
types the junction owner column and the log's `item_id` (the
junction-member-column rule). A mixed-election owner kind is therefore
splittable per sub-type: each narrowed unit resolves its own populations'
elections, and a narrowed junction whose populations agree on one declared
type carries that type rather than falling to `VARCHAR`. `where` never
narrows the addressed population set — it is value-level, not
population-level: a `where`-only membership unit addresses the owner kind's
full declared population set for gates and type resolution, whatever rows the
predicate then selects.

### Row selection

| Condition | Result |
|---|---|
| State table with `where` | Renders the rows of its population set whose predicate columns satisfy the conjunction; all other render semantics (taxonomy, `columns` / `rename`, lifecycle, election) unchanged |
| Junction table with `sub_types` / `where` | Renders the membership intervals whose owner satisfies the selection, via the parent lookup; all other junction semantics unchanged |
| `where` + `sub_types` together (any unit — state table, junction, events source) | AND-composed: the predicate narrows within the selected populations / owner sub-types |
| Selection matches zero rows | The table is emitted empty — declared intent drives existence, as for an empty population |
| A row whose predicated column is NULL | Never selected — `=` / `IN` is never satisfied by NULL, and the grammar has no null test. Once a kind is split by `where`, a NULL-bearing partition column's rows land in no predicated unit; they remain exportable only through an unpredicated declaration (omission-as-exclusion, applied by value) |
| Predicate column not listed in `columns` | Legal — selection and projection are orthogonal; the predicate reads the subject relation, not the projected output |
| Predicate on a reference-valued constant property | Legal, no special case: the comparison is over base-layer values (record ids), literal-typed from the sidecar, regardless of the elected surface the column *renders* |
| Two tables' selections overlap or exhaust nothing | Legal — tables never required disjointness or coverage (two tables may already share a population); rows matching no declared table are simply not exported (omission is the exclusion mechanism, now value-granular) |
| Events records source with `where` | The source's fold input is narrowed to the records satisfying the predicate, via per-row records-spine resolution; every event of an excluded record is excluded, `create` and `destroy` included |
| Events membership source with `sub_types` / `where` | The fold input is narrowed to the intervals of satisfying owners, via the parent lookup; every `join` / `leave` of an excluded owner's collection is excluded |
| `where` vs `only` / `ignore` on one events source | Orthogonal: `where` selects *records* (or owners), `only` / `ignore` select the audited *property set*. A property may be predicated and ignored simultaneously |
| Incremental, state render | Per-window snapshot applies the same predicate at every window; a record's presence across windows varies only by its lifecycle (`created_sim_time`), never by predicate re-evaluation |
| Incremental, junction render | Extract-on-change runs over the narrowed interval set; activity keys and `left_at` horizon-masking unchanged; owner selection is window-invariant (constant-gated), so an interval's membership in the table never varies by window |
| Incremental, event log | Window membership selects among the selection-narrowed event set; `id` is assigned over that whole-tape narrowed set beneath the window predicate, so numbering stays dense, tape-anchored, and invocation-invariant |

### Event-source disjointness

Invariant — no event is double-logged — is preserved by extending the overlap
gate to selection-aware disjointness. Two events sources can collide only
when they audit one item space: two records sources of one kind with
overlapping population sets, or two membership sources of one
`(kind, property)`. Disjointness must be *decidable from the config alone*,
so exactly these shapes count:

| Two sources auditing one item space | Result |
|---|---|
| Both declare a `where` entry on at least one **common column** whose two value sets are disjoint (as **typed values** under the column's sidecar-declared type; a scalar is a one-element set) | Legal — no record can satisfy both, whatever their other entries do |
| Membership sources of one `(kind, property)` with both-declared, disjoint owner `sub_types` sets | Legal — the owner sub-type is the population axis, read per row through the parent lookup |
| Any other shape (no common predicated column; every common column's value sets intersect; only one source carries a selection) | `SourceEventSourceOverlap`, as today |
| Population sets already disjoint (records sources) | Legal — predicates irrelevant to the gate |

Legality is existential — one common column with typed-disjoint value sets
suffices; entries the sources do not share, and shared columns whose sets
intersect, do not defeat a disjointness another common column establishes.
Value-set disjointness compares **typed values**, never written strings: each
element's plan-time cast result (§ The constant-column gate — the uniform
castability check) compares under the common column's sidecar-declared type,
and the sets are disjoint only when the typed values do not
intersect. Two spellings of one value (`'5'` / `'05'` on a `BIGINT`) are one
value, never a disjoint pair — string comparison would silently license
double-logging. An element the declared type cannot cast never reaches the
gate — the uniform castability check has already refused it
(`SourceWhereValueUncastable`). The gate still never consults row data:
the cast reads config literals and the sidecar's type declaration only, and
two predicates that happen to select disjoint rows but share a typed value
are still refused. Disjointness never implies coverage: a record NULL on
every common predicated column satisfies neither source and is audited by
neither (§ Row selection).

### `init` proposals

`init` proposes a split exactly where the sidecar declares the partition —
deterministic from the discriminator domain, no value read — and nowhere
else. The shipped per-sub-type state-table default extends to a sub-typed
owner's membership estate:

| Emit condition | Proposal |
|---|---|
| `membership__<K>__<p>`, `K` flat | One junction table `<K>_<p>` — unchanged |
| `membership__<K>__<p>`, `K` sub-typed | One junction stub per declared sub-type — `name: <K>_<sub_type>_<p>`, `sub_types: [<sub_type>]` — aligned with the owner's per-sub-type state stubs; the last stub carries a commented combine-alternative (one whole junction, `sub_types:` omitted), mirroring the state stubs' posture |
| Membership event-source entries (commented-out, as today), owner sub-typed | One commented entry per declared sub-type (`sub_types: [<sub_type>]`). Uncommenting the full set stays plan-clean: the entries share the default item-type `<K>.<p>` under the extended sharing exception, and their both-declared disjoint `sub_types` sets satisfy the overlap gate |

Name collisions follow the existing rule (later proposal in sidecar order
emitted commented-out, naming the collision), so the emitted config still
always parses and plans clean. No `where` is proposed on any unit.

### Invariants

Relied on:

- `constant`-class means current-equals-genesis (the mode's declared
  temporal-honesty exception in windowed snapshots; the fold's type-1 path).
- The records-spine per-row resolution device used by `sub_types` narrowing is
  temporally honest for creation-constant columns.
- The membership owner column (`record_id`) resolves uniquely on the owner
  kind's records spine — the parent lookup is a fan-out-free identity join.
- `PredicateValue` carries the well-formedness rule (non-empty, no
  duplicates) for any field declared with it.
- One rendering authority compiles every config predicate condition.

Introduced:

1. **Horizon-invariant row membership.** No source output row's membership in
   a declared table, junction, or audit stream depends on the horizon, the
   window, or the invocation. Over a conformant emit this is guaranteed by
   the constant-column gate (and the creation-constant discriminator), not by
   evaluation discipline. Every narrowing path nonetheless evaluates the
   records spine's current values (the state-at type-1 render, the per-row
   spine resolution, the parent lookup), so even over a corrupted emit —
   where a mutated "constant" falsifies current-equals-genesis — selection
   stays consistent across windows and invocations: a corrupter can change
   *which* rows a predicate selects, never make the selection
   horizon-dependent.
2. **Selection is value-blind at plan time.** Every `where` check — the
   class, discriminator, castability, and disjointness gates and the domain
   notice — reads sidecar declarations and config literals only; none
   consults row data. The casts included: they type config literals by the
   sidecar's declaration, reading no rows.
3. **Selection filters, never transforms.** A `where` or owner `sub_types`
   changes which rows render; it never changes any rendered value, ordering
   key, or the log's tape-anchored numbering rule. In particular the parent
   lookup projects nothing.

## Configuration

```yaml
mode: source
source:
  tables:
    - name: trip
      kind: journey_instance
      where: {prop__journey_type: rider_journey}
    - name: driver_shift
      kind: journey_instance
      where: {prop__journey_type: driver_journey}
    - name: premium_rider                 # scalar-or-list: IN over two tiers
      kind: actor
      sub_types: [rider]
      where: {prop__loyalty_band: [silver, gold]}
    - name: trip_passengers               # junction split by owner constant
      membership: {kind: journey_instance, property: passengers}
      where: {journey_type: rider_journey}
    - name: ward_allocation               # junction split by owner sub-type
      membership: {kind: resource, property: holders}
      sub_types: [ward]
  events:
    name: audit_log
    sources:
      - kind: journey_instance
        where: {journey_type: rider_journey}
        item_type: trip
      - kind: journey_instance
        where: {journey_type: driver_journey}
        item_type: driver_shift
      - membership: {kind: resource, property: holders}
        sub_types: [ward]
        item_type: ward_allocation
```

| Field | Type | Required | Description |
|---|---|---|---|
| `tables[].where` | `dict[str, PredicateValue]` | No | Row predicate. With `kind`: keys are source column names (`prop__<p>`) of the kind's `constant`-class payload properties. With `membership`: keys are bare property names of the **owner** kind's `constant`-class payload properties, evaluated through the parent lookup. Scalar → `=`, list → `IN`; entries AND-joined. |
| `tables[].sub_types` | existing field, extended | No | With `membership`: the owner's declared sub-types — the junction renders intervals of owners in those sub-types. (With `kind`: unchanged.) |
| `events.sources[].where` | `dict[str, PredicateValue]` | No | Record predicate, keyed by bare property names of the subject kind (the declared kind, or the owner kind for a membership source); same value grammar. |
| `events.sources[].sub_types` | existing field, extended | No | With `membership`: owner sub-type selection for the join/leave stream. (With `kind`: unchanged.) |

## Interface Contracts

### Config Models

```python
class SourceTableDecl(StrictBaseModel):
    """One declared output table: a name, one population source, optional
    column selection, renames, and row selection."""

    sub_types: tuple[str, ...] | None = None
    """Explicit population subset (with `kind`) or owner sub-type subset
    (with `membership` — the junction renders intervals of owners in these
    sub-types, resolved through the parent lookup). Absent = every declared
    sub-type."""

    where: dict[str, PredicateValue] | None = None
    """Row predicate; entries AND-joined. Keys name `constant`-class payload
    properties of the subject kind (gated at plan time): source column names
    (`prop__<p>`) with `kind`, bare owner-property names with `membership`.
    Absent = every row of the selected populations."""
```

```python
class SourceEventSourceDecl(StrictBaseModel):
    """One audited population set for the event log."""

    sub_types: tuple[str, ...] | None = None
    """Explicit population subset (with `kind`) or owner sub-type subset
    (with `membership` — narrows the join/leave stream to these owners'
    collections). Absent = every declared sub-type."""

    where: dict[str, PredicateValue] | None = None
    """Record predicate over the subject kind (the declared kind, or the
    owner kind for a membership source), keyed by bare property name;
    entries AND-joined; keys must name `constant`-class payload properties
    (gated at plan time). Selects which records' (owners') events feed this
    source's audit stream — orthogonal to `only` / `ignore`, which select
    the audited property set."""
```

`PredicateValue` is reused as-is; its parse-time well-formedness (non-empty
list, no duplicate elements, reported at the offending entry's path) applies
per mapping entry with no new wiring.

## Validation Rules

### Parse-Time (Pydantic)

Extensions to the existing shape validators (`table_shape`, `source_shape`):

```python
@model_validator(mode="after")
def table_shape(self) -> Self:
    """Existing shape rules, minus the sub_types-requires-kind rule
    (`sub_types` is now valid with either population source), plus: a
    present `where` mapping is non-empty with non-empty keys.

    Raises:
        ValueError: `where` present-but-empty; an empty key. (Value
            emptiness / duplication is carried by `PredicateValue` per
            entry.)
    """
```

```python
@model_validator(mode="after")
def source_shape(self) -> Self:
    """Existing shape rules, minus the sub_types-requires-kind rule, plus:
    a present `where` mapping is non-empty with non-empty keys.

    Raises:
        ValueError: `where` present-but-empty; an empty key.
    """
```

### Business Rules

Run at plan time against the open emit, before any write; `{owner}` as in the
existing source rules (`table '<name>'` / `events source #<n>`). The subject
kind of a membership unit is its **owner** kind throughout.

| Rule | Checks | Error Message |
|---|---|---|
| `SourceWhereColumnUnresolved` | Every `where` key resolves to a payload property of the declaring unit's subject kind (source-name form on records-backed tables, bare form on events sources and membership units). Structural columns and membership element fields are not payload properties and fail here | `"{owner}: where key '{key}' not a payload property of kind '{kind}'"` |
| `SourceWhereNotConstant` | Every resolved `where` column's `temporal_class` is `constant` | `tracked`: `"{owner}: where key '{key}' is temporal_class: tracked; under a horizon reconstruction its as-of and current values select different rows — row selection requires a constant column"`. `slice_only`: `"{owner}: where key '{key}' is temporal_class: slice_only; its past is unknowable, so row selection cannot read it"`. (`where` keys are this rule's to refuse; the existing `SourceSliceOnlyRead` population does not extend to them) |
| `SourceWhereOnDiscriminator` | No `where` key names the subject kind's declared discriminator | `"{owner}: '{key}' is the sub-type discriminator; select sub-types via sub_types, not where"` |
| `SourceWhereValueUncastable` | Every `where` element casts to its resolved column's sidecar-declared DuckDB type, constant-evaluated at plan time on every `where`-bearing unit (§ The constant-column gate); the disjointness gate's typed-value comparison reuses these results | `"{owner}: where value '{element}' for '{key}' does not cast to {type}"` |
| `discriminator-value-unobserved` (notice, per element — dimensional's shipped code and posture, reused) | For a `where` column with a declared enum domain (`enum_domains`, keyed by the subject kind), each predicate element outside the domain draws one notice, in config element order — never an error. Message granularity as dimensional's: a scalar, or a list no element of which is in the domain, states the unit will render no rows; a partially-covered list states only that the element contributes none. A column with no `enum_domains` entry is unchecked | Notice through the notice channel, not an error; names `{owner}`, `{key}`, and the element |
| `SourceTableSubTypeUnknown` / `SourceSubTypesOnFlatKind` (extended) | `sub_types` on a `membership:` declaration validates against the **owner** kind's discriminator domain, with the existing messages (`{kind}` = the owner kind) | Existing messages |
| `SourceEventSourceOverlap` (extended) | Sources auditing one item space (records sources of one kind with overlapping populations; membership sources of one `(kind, property)`) are disjoint only via both-declared disjoint owner `sub_types` sets or a common predicated column with typed-value-disjoint value sets (typed values from the uniform plan-time castability check — § Event-source disjointness) | Existing message; the selection case appends `"; selections do not establish disjointness"` |
| `SourceItemTypeCollision` (sharing exception extended) | Membership sources of one `(kind, property)` may share one resolved item-type, as records sources of one kind may; all other collision clauses unchanged | Existing messages |
| `TemporalClassUnavailableError` (reader-owned, existing) | Every consulted `where` column declares an in-enum `temporal_class` | Existing C13 message |

Typing of predicate literals is the rendering authority's existing contract:
each element casts to the column's sidecar-declared DuckDB type; an
unrecognized type is refused, never defaulted to `VARCHAR`.

## Rationale

- **The partition line moves from sidecar-declared to author-declared.** The
  standing decision that kept source predicate-free
  (`source-mode-narrows-rows-by-sub-types-only-no-row-predicate-surface`,
  superseded by this design) drew the line at what the sidecar declares: a
  declared sub-type partition marks structurally different things (separate
  app tables); a plain value domain marks one shape (one table with a type
  column) — so a row predicate could only ever express the analytical
  partition, the star's job. Its premise was that producer declarations track
  the structural partition, and it filed the counter-case as a
  producer-contract gap, not a mode gap. The ride-sharing emit lands that gap
  on source's own charter: rider trips and driver shifts *are* structurally
  different things, behind a discriminator the producer happened not to
  declare, and a mode whose thesis is output-that-looks-like-a-real-system
  cannot call the interleaved table someone else's problem. Realism decides:
  the split must be expressible, and the author — who knows which constant
  properties are de facto discriminators — is the one who draws it.
- **What the superseded decision protected survives.** Its operative fear was
  the mode erasing "sidecar facts gate declarations; they never decide
  layout." That line is untouched: layout stays author-declared; the
  sidecar's contribution is a gate (the constant class), never a decision;
  and `init` proposes no `where`, so the mode never manufactures a
  value-drawn split — its per-sub-type membership proposals (§ `init`
  proposals) read only the declared discriminator domain, the same
  sidecar-declared partition its state-table default already proposes from.
  The line is determinism: propose from declared structure, never from
  observed values. The
  realistic default the decision defended — one event-spine table over a
  genuine enum axis, never table-per-enum-value — is preserved by omission:
  nothing forces a split, so the analytical anti-pattern now requires an
  author to spell it out deliberately, the same trust source already extends
  over table naming and `columns` selection.
- **The value axis notices; only the key axes error.** Whether a `where`
  value is checkable at all is a producer choice: `enum_domains` covers a
  constant property only where the scenario declared it closed-domain, and
  the motivating emit's `journey_type` — constant, de facto discriminator —
  declares none (the same non-declaration that left it without sub-types). A
  hard out-of-domain error would make source's strictness lottery-shaped — a
  typo on a registered column refuses the export while a typo on an
  unregistered one silently empties a table — and would diverge from
  dimensional's shipped posture that a declared-but-unobserved value is a
  legitimate way to write one config against a family of emits. So the value
  check reuses dimensional's per-element `discriminator-value-unobserved`
  notice (the code's name predates this non-discriminator surface; renaming
  a shipped code is out of scope), and the zero-match outcome stays legal —
  declared intent drives existence. Castability is the one value-shaped
  error, and it is not an exception to this posture: out-of-domain is
  unobserved-but-possible; uncastable is impossible under the declared type,
  in this emit and every sibling of its family, and deferring it to the
  rendered `CAST` would crash the export at query time, after "plan time,
  before any write". The residual net for unregistered
  columns is the run-and-profile authoring workflow, not the gate.
