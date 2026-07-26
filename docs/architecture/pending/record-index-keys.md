---
status: draft
---

# Record-index keys in base output

Prong 4 of the v6 index harvest: `mode: base` presents the index-space encoding of
every record identity and every reference edge, alongside the id-space encoding it
already emits. The index-space resolution is supplied by a new derivations resident
built to serve every mode, not by base.

---

## Problem

A base export carries no integer key. Every output table's identity is the opaque
`record_id` (presented as `id`), and every reference is the opaque target id. A
student joining two base tables has exactly one path available:

```sql
-- the only join base output supports today
SELECT * FROM actor JOIN entity ON actor.group = entity.id
```

The join resolves — it is not a correctness defect — but `id` is an opaque string
minted by one of several producer paths, so a single kind's id column mixes
sequential decimal strings with hex digests. Sorting it is meaningless, reading it
teaches nothing, and it is the wrong shape for the lesson base exists to carry:
incremental ETL and SCD-2 merge, where the merge key is an integer surrogate.

The base layer already carries the integer encoding. Every `records__<kind>` table
has a `record_index` — a dense, 0-based, creation-ordered ordinal, stable across
every emit of a branch — and every reference-annotated `prop__<name>` has a
`ref_index__<name>` sibling carrying the target's `record_index`. The format
sanctions the integer join outright. Base drops both column families.

The drop is deliberate, not an oversight: the phase-1 posture of base-format-v6
adoption held these columns out of *every* exporter output and deferred each use to
a later, per-mode decision. That posture is unrecorded in this repo, which is what
generated the gap report. It was also mis-sited: base does not strip the columns,
because base never sees them. Base emits exactly the state-at derivation's
reconstruction tuple, and that tuple is a reconstruction contract — not "the records
table minus some columns" — so the columns are absent upstream of base's projection.

The naive repair is therefore wrong in an instructive way. Widening the state-at
tuple would change three unrelated outputs, and would land integer keys on exactly
one of the source exporter's four table genres under one non-default delivery mode,
producing a mode that is internally inconsistent about whether it has surrogate keys.

## Solution

Add a **join-relation** derivation — a two-column relation mapping a kind's
`record_id` to its `record_index` at a horizon — and have base resolve both key
families from it. The state-at resident is untouched.

The move that makes this cheap: state-at already projects the horizon-reconstructed
`prop__<p>` for every selected property. Base's render resolves an edge key by
joining the *target kind's* index relation onto that already-emitted value. Nothing
is reconstructed twice, and the two relations cannot drift out of horizon agreement
because the edge is resolved from the very value the value-relation produced.

```
                     ┌─ state-at resident ───────────┐
                     │  record_id, lifecycle,        │
  records__<kind> ──▶│  presentation_id, prop__<p>   │──┐
  history            └───────────────────────────────┘  │
                                                        ├─▶ base render ─▶ flat table
                     ┌─ record-index resident ───────┐  │   (joins on record_id
  records__<target> ▶│  record_id, record_index      │──┘    and on prop__<p>)
                     └───────────────────────────────┘
```

The record's own key uses the same mechanism with target kind = own kind, so one
uniform rule produces the self key and every edge key.

Both encodings are emitted. The pair is not redundant: an index-space key alone
cannot distinguish "no reference" from "dangling reference" — both are NULL — while
the id-space column beside it makes the distinction visible. Emitting only the index
would discard information the base layer carries, which faithful reshaping forbids.

```
actor
  actor_key   BIGINT   NOT NULL   -- record_index
  id          VARCHAR            -- record_id
  created_at  TIMESTAMP
  active      BOOLEAN
  group       VARCHAR            -- prop__group,        the target's record_id
  group_key   BIGINT   nullable  -- ref_index__group,   the target's record_index
```

## Affected Subsystems

- **The derivations layer** gains a seventh resident, the **record-index resident**.
  It is a join relation in the same family as the two reference-resolution
  residents: it returns a narrow relation a mode `LEFT JOIN`s, rather than a fold
  whose output shape is an export's shape or a presenter that replaces a base table.
  Like the state-at resident it has two entry points — one horizoned, one structural
  end-of-tape carrying no horizon predicate — and it obeys the layer's six rules,
  reading the determinism rule's ordered-relation clause as the reference-resolution
  residents already do: a join relation is deterministic as a set and is consumed
  through a `LEFT JOIN`, so it declares no `ORDER BY` of its own. It is designed for every mode's use, not for base's: the same
  relation answers dimensional's surrogate-key question and source's integer-PK/FK
  question when those are scheduled.

- **The base exporter** gains two column families and the plan-time resolution
  behind them. Its planning stage picks up a new obligation: resolving each kind's
  surviving reference properties to their target kinds, deciding which edges can
  produce a key in this emit, and extending the rename/collision/reserved-name
  domain to cover the two new column identities. Its render stage picks up the
  joins and the interleaved emission order. Base's engine is no longer state-at
  alone — it is state-at for values and the record-index resident for identity — but
  it still writes no point-in-time path of its own.

- **The notice channel** gains one code, for a reference edge whose target kind has
  no records table in this emit and therefore yields no key column.

- **The writers** carry a nullable `BIGINT` column family for the first time in base
  output. No adapter behavior changes; the type simply has to survive serialization
  in both formats, NULLs included.

## What Doesn't Change

- **The state-at resident.** Its canonical column tuple, both entry points, and all
  three of its consumers are untouched. This is the design's central constraint, not
  an incidental one.
- **The source, dimensional, and streaming exporters.** No
  output column, ordering, or API changes. Prongs 1–3 of the harvest — source's
  integer PK/FK realism, dimensional's surrogate keys, and change-log carriage of
  the index — are **deliberately deferred**, and this is a real scope decision, not
  an oversight. They are separable because each is dominated by a mode-specific
  question this design does not answer: source must decide how keys interact with
  its four-genre trichotomy and its untracked-only sub-type split, where a target
  kind can map to more than one output table; dimensional must decide whether the
  index is the surrogate for a dimension grain or an attribute of it. Neither
  question has a bearing on base's flat one-table-per-kind shape. The record-index
  resident is designed to serve all three without change when they are scheduled,
  which is what makes the deferral safe rather than merely convenient.
- **The playback seam's API — though a base-shape compile's column set follows
  base's.** No seam signature, entry point, or ordering changes. A tier-2 shaped
  playback over a `mode: base` config gains the key columns automatically, because
  it compiles base's table specs: the record-index resident's structural entry
  point composes over truncated base relations exactly as state-at's does
  (§ Horizon binding), bounded by the truncation with no horizon computed. That
  composition is what keeps the bridging equivalence intact — a `slice_at: T`
  export and the base-shape compile over the tape truncated at `T` remain
  column-for-column equal.
- **The truncated-tape surface.** It already re-derives `ref_index__<name>` against
  a truncated target spine, and that implementation stands. Collapsing it onto the
  record-index resident is a legitimate simplification but is not required by this
  feature, and refactoring a shipped, tested surface for symmetry alone is outside
  this design's scope. The consequence must be recorded rather than left implicit:
  two implementations of "resolve an id to a record_index under a temporal bound"
  will coexist, and **they use opposite bound inclusivity** — the truncated-tape
  surface bounds inclusively at `T`, the record-index resident exclusively at a
  horizon. Reading one as a template for the other is an off-by-one boundary defect.
- **The reader.** The records-column taxonomy, the structural-temporal surface, and
  the sidecar already answer everything this design asks of them. `record_index` is
  pinned set-once; a `ref_index__<name>` column's mutability follows its sibling
  `prop__<name>`. This design is the first consumer of that rule, not a change to it.
- **Conformance.** No check is added. Pair agreement and index resolution are
  producer-guaranteed by construction and explicitly outside the conformance
  procedure — the same trust class as id-space referential integrity. Base consumes
  the guarantee; it does not re-verify it.
- **The corrupter family.** No operation changes, but the composition is now
  load-bearing in two ways, and both hold by construction. Reference-rewriting
  operations co-write coherent pairs — a dangled edge writes a sentinel id beside a
  sentinel index, a mispointed edge writes the donor's id beside the donor's real
  index — so re-derivation resolves exactly the defect the manifest declares: a
  dangled id finds no target and yields a NULL key; a mispointed id finds the
  donor. Row-set operations leave key joins one-to-one: exact duplication copies
  the row whole, so the duplicate carries the *identical* `(record_id,
  record_index)` pair and the resident's DISTINCT relation (§ Interface Contracts)
  collapses it out of the join's right side; deletion and insertion never reuse or
  collide an id or an index, and the gaps they leave surface verbatim (§ Density).
  The one shape that could fan a key join out — two rows of one kind sharing a
  `record_id` with differing `record_index` — is not producible: identity columns
  sit outside every cell operation's eligible population.
- **Horizon semantics.** `slice_at: T` remains inclusive of `T`, `slice_at` and
  `incremental` remain mutually exclusive, and window arithmetic is unchanged.

## Semantics

### The two key families

| Family | Source identity | Present on |
|---|---|---|
| Self key | `record_index` | every emitted table, exactly once |
| Edge key | `ref_index__<p>` | each surviving reference-annotated `prop__<p>` |

A **surviving reference property** is a `prop__<p>` column of the kind that carries
a sidecar `references` target and is not omitted by the `slice_only` policy.

### Edge keys are always re-derived, never carried

An edge key's value is resolved from the horizon-reconstructed `prop__<p>` against
the target kind's record-index relation at the same horizon. The physical
`ref_index__<p>` column is never read.

This is unconditional, and deliberately so. The physical column carries the target's
index *at the emit's own slice*, which is the correct instant only when the horizon
is the tape's end. A rule that reads the physical value for constant properties and
re-derives for tracked ones would be correct at one horizon and silently wrong at
another; one rule that is correct everywhere is worth the redundant work in the
constant case.

| `prop__<p>` temporal class | Physical `ref_index__<p>` at horizon T | Emitted edge key |
|---|---|---|
| `constant` | correct (value is time-invariant) | re-derived — same answer |
| `tracked` | stale: the target at the emit's slice, not at T | re-derived |
| `slice_only`, non-exempt | not applicable | no key column — property is omitted |

The join is `record_id` against the reconstructed property value. Both sides are
`VARCHAR` — the format pins a reference property's `prop__` column to the id-only
`VARCHAR` form, and the state-at relation's codec after-image is `VARCHAR` — so no
cast participates in the join on either side.

### NULL semantics, and why both encodings ship

The edge key is a `LEFT JOIN` projection, so three distinct conditions collapse to
NULL. The id-space column beside it separates them.

| Condition | id column (`<p>`) | key column (`<p>_key`) |
|---|---|---|
| Property absent on the record | NULL | NULL |
| Reference names a record created before the horizon | the id | that record's `record_index` |
| Reference names a record created at-or-after the horizon | the id | NULL |
| Reference names no record at all (e.g. a dangled sentinel) | the id | NULL |
| Target kind has no records table in this emit | the id | column not emitted (§ Absent target kind) |

A target record **deactivated** before the horizon still resolves. The record-index
relation filters on creation time only and never on `active`: a deactivated record
remains a legal reference target, and filtering it out would manufacture a dangling
edge the base layer does not contain.

### Density survives every horizon

At any horizon, a kind's emitted self keys are exactly the integers `0 .. n-1`,
where `n` is that table's row count. `record_index` is the ordinal in creation
order and the horizon filter is on creation time, so the surviving set is always a
creation-order prefix — records sharing a `created_sim_time` are both retained or
both dropped, so no tie can perforate the prefix. Values are projected verbatim;
nothing is ever renumbered.

This is the property that makes the self key a merge key rather than a row number.
A record carries the same integer at every horizon, in every window of an
incremental run, and in every emit of its branch. Renumbering to close a gap would
destroy exactly the cross-emit comparability the column exists to provide.

Density is inherited from the emit, never enforced: base asserts no `0 .. n-1`
check. A corrupted emit whose `record_index` set is perforated (a deleted row) or
repeated (a duplicated row) surfaces those values verbatim — the gap or repeat *is*
the defect, and closing or collapsing it would fabricate.

### Horizon binding

The key relation and the value relation are composed at one horizon per table
render — a stated invariant, since a mismatch would silently resolve edges against
the wrong population.

| Export selector | Record-index entry point |
|---|---|
| No `slice_at`, no `incremental` | End-of-tape — no horizon predicate |
| `slice_at: T` (full export) | Horizoned at `T + 1` |
| `incremental` window | Horizoned at the window's end |

The end-of-tape entry point carries no horizon predicate at all, matching the
state-at resident's structural posture: composed over truncated base relations it
is bounded by the truncation, with no horizon ever computed.

### Naming

| Identity | Default output name | Derived from |
|---|---|---|
| `record_index` | `<kind>_key` | the records **kind** |
| `ref_index__<p>` | `<p>_key` | the bare **property** name |

Both defaults are overridable through `rename`, keyed on the contract identity —
the same convention by which `record_id` is renamed to produce the default `id`.

Two derivation choices are load-bearing:

- The self key is named from the **kind**, not the post-rename output table name.
  Deriving it from the table name would make the default depend on whether a
  `rename` entry was applied first, which is a resolution-order dependency with no
  upside.
- An edge key is named from the **property**, not the target kind. Two properties
  on one kind may reference the same target — `referring_doctor` and
  `attending_doctor` both landing on `doctor` — and naming from the target would
  collide them into one name.

Base is an operationally-presented mode, not a minimal one: it already renames
`records__customer` to `customer` and `record_id` to `id`. A warehouse-legible
`<kind>_key` is the same posture applied to the same kind of column, and it is the
name the merge lesson is written against.

### Emission order

- The self key is the table's **first** column, ahead of `id`. Surrogate-first is
  the convention the lesson teaches.
- Each edge key immediately follows **its own** id-space column, mirroring the way
  the base format interleaves each `ref_index__<name>` immediately after its
  `prop__<name>`.

Every other column keeps its position.

### Typing

| Column | Type | Nullability |
|---|---|---|
| Self key | `BIGINT` | never NULL |
| Edge key | `BIGINT` | nullable |

Both are projected verbatim from `record_index`, which the format pins as
`BIGINT NOT NULL`; an edge key's nullability comes from the outer join, not from
the source column.

### The `slice_only` interaction

A non-exempt `slice_only` reference property is omitted from base output, and its
edge key is omitted with it. This is required, not incidental: the export-wide
policy forbids any output value from deriving from a `slice_only` column's value,
and an edge key derived from an omitted property's reconstructed value would be
exactly that.

The omission is covered by the existing per-column omission notice; the key's
disappearance is a consequence of the property's, not a separately-announced event.

The mechanical sub-typed-discriminator carve-out never interacts with this rule — a
discriminator is a closed-domain enum, never a reference-annotated property — but
the rule is stated over the property's `references` annotation rather than over the
carve-out, so the two remain independent.

### Absent target kind

An emit may legally omit `records__<K>` when kind *K* has no records in the slice.
A reference property pointing at such a kind is therefore contract-legal with no
target table present.

| Condition | Result |
|---|---|
| Target kind's records table present in the sidecar | Edge key emitted |
| Target kind's records table absent from the sidecar | Edge key **not** emitted; one notice per kind × property; the id-space column is unaffected |

Omitting the column is the right failure mode rather than raising: the emit is
valid, the export has always succeeded on it, and nothing base emits today is lost.
The resolution is made at plan time, before any data is written, so the notice
precedes output and the table's column set is known before the render runs.

The resident itself is stricter — asked for a kind with no records table it raises,
matching the layer's cause-based error taxonomy. The permissive behavior is base's
policy, applied by not asking.

### Excluded target kind

If a reference property's target kind is `exclude`d from the export, the edge key is
still emitted. This matches the id-space column's existing behavior exactly: base
already emits `prop__<p>` pointing at a kind the author excluded. Suppressing one
encoding but not the other would make the pair disagree about what the export
contains, and the author who excluded the kind is the one who chose the dangling
edge.

### Invariants introduced

1. **Both encodings or neither, when resolvable.** A surviving reference property
   whose target kind's records table is present emits its id-space and index-space
   columns together; neither ships without the other. An absent target kind is the
   one stated exception: the key column is omitted with a notice and the id-space
   column stands alone (§ Absent target kind).
2. **Edge keys are re-derived.** No base output value is read from a physical
   `ref_index__` column.
3. **One horizon per table.** The value relation and every key relation composed
   into one output table are composed at the same horizon.
4. **Density under every horizon, inherited — never enforced.** Over a conformant
   emit, a table's self keys are exactly `0 .. n-1` for its row count, at every
   horizon; nothing is renumbered. The property is a consequence of the emit's
   dense `record_index` and the creation-order-prefix filter, not a check base
   performs — a corrupted emit's perforated or repeated indexes surface verbatim.
5. **Creation-time filtering only.** Key resolution filters targets on creation time
   and never on `active`.
6. **No `slice_only` derivation.** No key column derives from a non-exempt
   `slice_only` column's value.
7. **Key resolution preserves row count.** Composing the key relations neither
   adds nor drops output rows — base's row set is the state-at spine's, exactly.
   The key relation is distinct over `(record_id, record_index)`, and a duplicated
   `record_id` always carries an identical `record_index`, so every key join is at
   most one-to-one per spine row.

Invariants the design relies on and does not establish: `record_index` is set once
at creation and never renumbered; it is dense over each `(fork_path, kind)`; a
record keeps it across every emit of its branch; `record_index` is monotone in
`created_sim_time` — creation order agrees with creation time, the base layer's
monotonic-time guarantee, which is what makes a creation-time filter carve a
creation-order prefix (§ Density) rather than a perforated set; pair agreement is
producer-guaranteed rather than conformance-checked; and no emit — corrupted
included — carries two rows of one kind sharing a `record_id` with differing
`record_index`, because identity columns sit outside every corrupter cell
operation's eligible population.

## Configuration

No new config fields. Key columns are always present — they are the capability, and
a toggle for a demand nobody has expressed is scaffolding.

`rename` reaches the new columns through the same mechanism as every other base
column, keyed on the contract identity:

```yaml
mode: base
base:
  rename:
    - table: records__actor
      name: dim_actor
      columns:
        record_index: actor_sk        # the self key
        ref_index__group: group_sk    # one edge key
        record_id: actor_natural_id   # overriding the default `id`
```

| Rename key | Targets | Default output name |
|---|---|---|
| `record_index` | the self key | `<kind>_key` |
| `ref_index__<p>` | property `<p>`'s edge key | `<p>_key` |

Both identities join the domain a `rename.columns` key is validated against, so a
typo fails at load rather than silently doing nothing.

## Interface Contracts

### Derivations — the record-index resident

```python
#: The canonical column list of the record-index relation, in emission order.
RECORD_INDEX_COLUMNS: tuple[str, ...] = ("record_id", "record_index")
```

```python
def build_record_index_at_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    horizon_ns: int,
) -> str:
    """
    Build the record-id-to-record-index relation for one kind at a horizon.

    One row per distinct (record_id, record_index) pair among the kind's
    records created strictly before the exclusive horizon, projecting
    RECORD_INDEX_COLUMNS — on a conformant emit, one row per record. The
    DISTINCT is a no-op under conformance; it exists so a row-duplicated
    corrupted emit, whose duplicate carries the identical pair, cannot fan a
    consumer's key join out. `record_index` is projected
    verbatim — the contract pins it as set once at creation and never
    renumbered, so it is a temporally-constant value read at a creation
    instant already bounded below the horizon. Rows are filtered on creation
    time and to `fork_path`; `active` is never a predicate, so a record
    deactivated before the horizon is present and remains a resolvable
    reference target. A join relation, not a fold: it declares no ORDER BY,
    because a consumer LEFT JOINs it rather than reading it ordered.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind whose index relation to build.
        horizon_ns: The exclusive horizon in sim-time ns; >= 0.

    Returns:
        A complete, deterministic SELECT producing RECORD_INDEX_COLUMNS.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
    """
```

```python
def build_record_index_at_end_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
) -> str:
    """
    Build the record-id-to-record-index relation for one kind at the tape's end.

    The resident's second entry point: the same DISTINCT RECORD_INDEX_COLUMNS
    relation with no horizon — every record of the kind, filtered only to
    `fork_path`.
    "The tape's end" is structural: the SQL carries no horizon predicate, so
    composing this relation over a truncated base relation bounds it at the
    truncation with no horizon computed. Equivalence contract: equal to
    build_record_index_at_sql at any horizon strictly beyond every creation
    instant of the composed relation.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind whose index relation to build.

    Returns:
        A complete, deterministic SELECT producing RECORD_INDEX_COLUMNS.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
    """
```

### Base exporter — runtime types

```python
@dataclass(frozen=True)
class ReferenceKey:
    """One surviving reference property's index-space edge, resolved at plan time.

    Present only for edges that yield a key column in this emit: a property
    omitted by the `slice_only` policy, or one whose target kind has no records
    table, produces no entry.
    """

    property_name: str
    """The bare property name — the edge key's default output name stem."""
    target_kind: str
    """The referenced records kind, from the property's sidecar `references`."""
```

`BaseTableSpec` gains one field:

```python
    reference_keys: tuple[ReferenceKey, ...]
    """Surviving reference edges that yield a key column, in sidecar
    column-declaration order of their `prop__<p>` columns. Empty when the kind
    has no reference property, or none that survives."""
```

`BaseTableSpec.column_renames` extends its key domain to include `record_index`
and each surviving `ref_index__<p>`; its type is unchanged. It carries the
`record_index -> <kind>_key` and `ref_index__<p> -> <p>_key` defaults alongside the
existing `record_id -> id`, each overridable.

### Notices

```python
#: Emitted when a reference property's target kind has no records table in the
#: emit, so no index-space key column can be produced for that edge. The
#: id-space column is unaffected.
NOTICE_REFERENCE_KEY_TARGET_ABSENT = "reference-key-target-absent"
```

Message names the kind, the property, and the absent target kind. Emitted at plan
time, one per kind × property, in sidecar table-declaration order then sidecar
column-declaration order — the same iteration the slice-only omission notices
follow — before any data is written.

## Validation Rules

### Parse-Time (Pydantic)

None. The feature adds no config fields, so no model gains a validator.

### Business Rules

Plan-time checks, evaluated over every export — full, sliced, and windowed alike —
so that a full export and a later incremental run on the same target agree on the
output shape.

| Rule | Checks | Outcome |
|---|---|---|
| Key-name collision | No resolved key column name equals another resolved output column name on the same table | `BaseNameCollision`, naming both contributors |
| Reserved output name | No resolved key column name is a reserved bookkeeping or suffix name | The existing reserved-name failure |
| Rename of a `slice_only`-omitted edge key | A `rename.columns` key names `ref_index__<p>` where `prop__<p>` is omitted by the `slice_only` policy | `BaseRenameSliceOnly` — the rename is unsatisfiable, never silently ignored |
| Rename of an unavailable identity | A `rename.columns` key names `ref_index__<p>` for a property that is not a reference, or whose target kind has no records table in this emit | `BaseRenameUnresolved`, the message naming why the identity is unavailable |
| Reference target resolvable | Each surviving reference property's target kind has a records table in the sidecar | Present: edge key emitted. Absent: edge key omitted, one `reference-key-target-absent` notice |

The valid-identity set a `rename.columns` key is checked against is the set of
identities the kind actually emits in this emit. An edge key omitted for an absent
target is therefore not a valid rename target, which is what makes the fourth rule
fall out of the existing check rather than needing a bespoke error.
