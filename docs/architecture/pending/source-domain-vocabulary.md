---
status: draft
---

# Source Domain Vocabulary

Author-facing naming for the source mode's *value* surfaces: the event log's
`item_type` and `changes` keys, and the kind-name values carried by membership
member-kind columns. Closes the finding
`source-event-log-item-type-ignores-table-renames`.

---

## Problem

`mode: source` presents itself as the author-declared app database: every table
and column carries an author-chosen domain name, and the engine's ontology is
deliberately hidden. But four surfaces render engine names as **data values**,
where no existing declaration can reach:

1. **`item_type`** — the kind name for a records source, `<K>.<property>` for a
   membership source. In the NHS example the state table is `patient` but
   `audit_log.item_type` says `actor` for all 55,536 of that source's rows.
2. **`changes` JSON keys** — bare base-layer property / element-schema field
   names. A state table renaming `prop__name` → `full_name` still yields
   `{"name": [old, new]}` in the audit log.
3. **`<f>_kind` values** in a membership source's `changes` entries — raw kind
   names as data.
4. **Junction `member__<f>__kind` values** — the column's *name* renames; its
   *values* still name kinds the state tables renamed away.

A consumer joining the log or a polymorphic junction back to the state tables
must know the engine ontology — exactly what the declared grammar exists to
hide. The root cause is structural: the events-source declaration carries no
naming field of any sort, and no declaration anywhere maps a kind name to a
domain label for value rendering.

## Solution

Two opt-in author declarations, both defaulting to today's verbatim engine
names (no invented mapping — Principle #7):

- **Per events source: `item_type` and `rename`** — the first-class naming
  surface. `item_type` overrides that source's resolved item-type wholesale —
  per-population granularity, which names each split of a kind for the concept
  it actually is (kinds are a simulation convenience; the sub-type concepts a
  kind groups are the first-class shape of the data — see Rationale). `rename`
  maps audited property / element-field bare names to output `changes` keys,
  mirroring the declared-table `rename` grammar.
- **`source.kind_labels`** — the mode-level convenience: one map, engine kind
  → domain label, applied everywhere a kind name renders **as a value**: the
  default `item_type` of every events source (including the `<K>` half of a
  membership source's composite identity), `<f>_kind` entries inside
  `changes`, and junction member-kind column values. It carries the
  one-concept-kind case in a single declaration, and it is the only reach into
  the surfaces where no per-source declaration exists — a junction's
  member-kind values are structurally kind-valued.

```yaml
source:
  kind_labels:
    actor: patient
    resource: consultant
  tables:
    - name: patient
      kind: actor
      rename: {prop__full_name: name}
  events:
    name: audit_log
    sources:
      - kind: actor
        rename: {full_name: name}      # changes keys follow the state table
      - membership: {kind: resource, property: holders}
        item_type: consultant_allocation
```

With `kind_labels` alone, the actor source's `item_type` becomes `patient` and
every `<f>_kind` value that names `actor` renders `patient`; with the
membership source's `item_type` override, `resource.holders` becomes
`consultant_allocation`.

## Affected Subsystems

- **Source-mode config grammar** — `SourceConfig` gains `kind_labels`;
  `SourceEventSourceDecl` gains `item_type` and `rename`. Parse-time shape
  rules extend the existing declared-table conventions (non-empty, distinct,
  rename-map validity).
- **Source-mode plan resolution** — the plan resolves each events source's
  item-type through override → label → verbatim, resolves per-property
  `changes` output keys, validates the label map against the sidecar, and
  gains three new failure modes (unknown label kind, label vocabulary
  collision, item-type collision). The per-item-type union-safety gate now
  ranges over *resolved* item-types, so aliasing re-partitions the gate.
- **Event-log render** — stamps the resolved item-type; assembles `changes`
  objects under resolved keys; renders `<f>_kind` entry values through the
  label map.
- **Junction render** — renders projected `member__<f>__kind` values through
  the label map.
- **`init --mode source`** — no new proposal. `init` proposes engine-verbatim
  table names, under which every label is the identity mapping; there is
  nothing to propose until an author renames, and the author who renames owns
  the labels. (Deliberate non-scope, not an oversight.)

## What Doesn't Change

- **Table and column naming** — the declared-table `name` / `columns` /
  `rename` grammar is untouched; this design adds no column-level surface to
  state or junction tables.
- **The event log's fixed column set** — the author still names the table, not
  the columns. `id`, `item_type`, `item_id`, `event`, `occurred_at`, `changes`
  keep their names and types.
- **`item_id`, reference values, elected surfaces** — key election and every
  identity rendering rule are untouched. Labels never apply to identity
  *values*, only to kind-name values and changes keys.
- **Sub-type discriminator values** — `<K>_type` column values are sub-type
  names (domain vocabulary from the simulation), not kind names; they render
  verbatim as today. (The discriminator *column name* was always renameable.)
- **Streaming, base, dimensional** — no other mode changes. Streaming's
  payload keys and `kind` field speak engine names by design; it has its own
  routing/naming surface, and extending vocabulary mapping to it is a
  separable future design.
- **The derivations folds and the reader** — labels are a render-time
  presentation concern; folds and reader surfaces are untouched.
- **Determinism, ordering shape, incremental composition** — the log's order
  key shape, `id` construction, window membership, and the cursor/fingerprint
  machinery are unchanged (see Semantics for the one ordering consequence of
  relabeling).

## Semantics

### Item-type resolution

Per events source, first match wins:

| Condition | Resolved item-type |
|---|---|
| `item_type` declared on the source | The declared string, verbatim |
| Records source, kind in `kind_labels` | The kind's label |
| Records source, kind not labeled | The kind name (today's behavior) |
| Membership source, owner kind `K` labeled | `<label(K)>.<property>` |
| Membership source, owner kind not labeled | `<K>.<property>` (today's behavior) |

The resolved item-type is the **contract identity** everywhere the current
design says item-type: the stamped column value, the dereference key, the
union-safety gate's granularity, and the order-key component.

### Item-type distinctness

Today records item-types (kind names) and membership item-types
(`<K>.<property>`) are distinct by construction. Aliasing makes collisions
expressible, and the dereference idiom — `(item_type, item_id)` names one
audited item — decides which are legal:

| Condition | Result |
|---|---|
| Two records sources of **one kind** resolve one item-type | Legal (the current shape for a kind split across sources); the per-item-type union-safety gate runs jointly over the union of their populations |
| Two records sources of **different kinds** resolve one item-type | Refused: `SourceItemTypeCollision`. Two identity spaces behind one dereference key |
| A membership source resolves the same item-type as any other source | Refused: `SourceItemTypeCollision`. Item-type is what separates collection changes from the owner's own lifecycle rows; merging them destroys that separation even though the owner's identity space is shared |
| Two records sources of one kind resolve **different** item-types (one aliased, one not, or two aliases) | Legal; the union-safety gate re-partitions and runs per resolved item-type separately |
| A records source's resolved item-type equals the **rendered name of another kind** (that kind's label, or its verbatim name when unlabeled) | Refused: `SourceItemTypeCollision` — one rendered name identifies at most one kind's population space, whether or not that kind is audited |
| A membership source's resolved item-type equals the rendered name of **any** kind (its owner's included) | Refused: `SourceItemTypeCollision` — a membership item-type names the owner's collection, never a kind's record space |

The gate's granularity follows the dereference key, exactly as today — the key
is now the resolved item-type.

The rendered-kind-name clauses range over the emit's **whole kind universe**,
not the declared sources — the same range as the label injectivity check, for
the same reason: an unaudited kind's rendered name still reaches the output
through `<f>_kind` and junction member-kind values. No layer outranks
another — override, label, and verbatim name are one vocabulary that must not
contradict itself.

### `changes` key resolution

| Condition | Result |
|---|---|
| Audited property (or element field) with a `rename` entry | The entry's value is the JSON key |
| Audited property without one | The bare name (today's behavior) |
| Membership reference field renamed `f → g` | The pair renames in place: `g_kind` / `g_id`. `only` / `ignore` / `rename` all still address the bare field name `f` |
| Two audited properties resolve one output key (a rename value colliding with an unrenamed bare name, or with a membership pair's expanded `_kind` / `_id` name — the rename-value vs rename-value case never reaches plan time; parse-time `source_shape` already refuses it) | Refused at plan time — the collision-never-silent posture, owner-labeled `events source #<n>` |
| `rename` key not a property (element field) of its source | Refused: `SourceColumnUnresolved` posture — `"{owner}: '{entry}' not a column of its source"` |
| `rename` key names a property excluded by `only` / `ignore` | Refused: the entry is unsatisfiable (same posture — never a silent ignore) |
| `rename` key names a non-exempt `slice_only` property | Refused: `SourceSliceOnlyRead`, as for every declaration entry naming one |

Key **order** in the `changes` object is unchanged: sidecar
column-declaration order of the *source* properties. Rename relabels; it never
reorders.

### Kind-label rendering

| Condition | Result |
|---|---|
| `<f>_kind` entry value (event log) or `member__<f>__kind` value (junction) equals a labeled kind | Renders the label |
| The value equals an unlabeled kind | Renders verbatim |
| The value is NULL | NULL stays NULL |
| The value names no sidecar kind (a corrupted emit's mutated cell) | Renders **verbatim** — the mapping is total with identity fall-through, so a corrupter's defect surfaces unchanged, never masked and never an error at render time |

Inside `changes`, an `<f>_kind` entry's `[old, new]` halves each render
through the map independently — labeling is a pure value recode, so it
commutes with the old-value lag (lag-then-label and label-then-lag agree).

The mapping is compile-time config, rendered deterministically in the SELECT —
a declared recoding of a name, the same fidelity class as a table rename
(Principle #3: reshape, never fabricate; the value still traces to the
base-layer kind name through a config-declared bijection).

### Label vocabulary integrity

`<f>_kind` disambiguates per row, and a junction may admit several kinds — so
the *rendered* kind vocabulary must stay injective:

| Condition | Result |
|---|---|
| `kind_labels` key is not a sidecar records kind | Refused: `SourceKindLabelUnknown` (the sidecar-facts-gate-declarations posture) |
| Two kinds map to one label | Refused at parse time (distinct-values rule on the map) |
| A label equals the **rendered name of another kind** (another kind's label, or an unlabeled kind's own name) | Refused: `SourceKindLabelCollision` — after labeling, kind → rendered-name must be injective over the emit's whole kind universe, else two kinds become indistinguishable in a `<f>_kind` column |

The injectivity check runs over all sidecar kinds, not just kinds appearing in
declared tables — a member field's admitted kind universe is not bounded by
the declaration list.

### Ordering consequence

`item_type` is a component of the log's order key `(event_sim_time, item_type,
event_class, record_id, membership-field tail)`. The order key uses the
**resolved** item-type, so relabeling can reorder events that share an instant
across item-types, and therefore renumber `id`. This is within the existing
contract — "two configs over one emit number differently" is already the `id`
guarantee (per-export monotonicity, not cross-export identity) — but it is
worth stating: **adding a label is a config change and renumbers the log like
any other config change.** Determinism is unaffected: same emit + same config
+ same code version → identical output.

### Interaction summary

| Existing feature | Interaction |
|---|---|
| Incremental export | None beyond the existing rule that the window fingerprint binds the config: item-type resolution and labels are compile-time, window-invariant |
| `declare_keys` | None — the log still declares `PRIMARY KEY (id)`; labels touch no key column |
| Key election | Gate granularity follows resolved item-types (above); election semantics unchanged |
| `slice_only` policy | Events-source `rename` joins `columns` / `only` / `ignore` in the set of declaration entries refused when naming a non-exempt `slice_only` column |
| Corrupter composition | Identity fall-through (above): defects surface unchanged |
| Invariant "every log column is a function of the order key" | Preserved: resolved item-type is compile-time per source, and `changes` keys are compile-time per property |

## Configuration

```yaml
source:
  kind_labels:            # optional; engine kind -> domain label
    actor: patient
    resource: consultant
  tables:
    - name: patient
      kind: actor
  events:
    name: audit_log
    sources:
      - kind: actor
        rename: {full_name: name}        # optional; changes-key overrides
      - kind: resource
        item_type: clinician             # optional; wholesale override
      - membership: {kind: resource, property: holders}
        item_type: consultant_allocation
```

| Field | Type | Required | Description |
|---|---|---|---|
| `source.kind_labels` | map str → str | No | Engine kind → domain label, applied to every kind-name-as-value surface. Absent = verbatim kind names |
| `sources[].item_type` | str | No | This source's resolved item-type, wholesale. Wins over `kind_labels`. Absent = label-or-verbatim default |
| `sources[].rename` | map str → str | No | Audited property / element-field bare name → `changes` output key. A membership reference field's rename renames its `_kind` / `_id` pair. Absent = bare names |

## Interface Contracts

### Config Models

```python
class SourceEventSourceDecl(StrictBaseModel):
    """One audited population set for the event log."""

    # ... existing fields (kind, sub_types, membership, only, ignore) ...

    item_type: str | None = None
    """This source's resolved item-type, verbatim, overriding the
    kind-label / contract-identity default. Optional; non-empty when
    present."""

    rename: dict[str, str] | None = None
    """Audited property (element field) bare name -> `changes` output key.
    Keys are source identities, never output keys, so a default-key
    collision is always resolvable. A membership reference field's entry
    renames its expanded `<f>_kind` / `<f>_id` pair in place."""

    @model_validator(mode="after")
    def source_shape(self) -> Self:
        """The declaration's structural shape.

        Raises:
            ValueError: (existing rules, plus) `item_type` is empty;
                `rename` is present-but-empty or two keys share a target
                value.
        """


class SourceConfig(StrictBaseModel):
    """mode: source section — the declared app-database shape."""

    # ... existing fields (tables, events, declare_keys) ...

    kind_labels: dict[str, str] | None = None
    """Engine kind name -> domain label, applied wherever a kind name
    renders as a value: events-source item-type defaults (including the
    owner half of a membership source's `<K>.<property>` identity),
    `<f>_kind` entries inside `changes`, and junction `member__<f>__kind`
    column values. Never applied to identity values, table names, or
    sub-type discriminator values. Absent = verbatim kind names."""

    @model_validator(mode="after")
    def kind_labels_shape(self) -> Self:
        """`kind_labels`, when present: non-empty, non-empty keys and
        values, distinct values.

        Raises:
            ValueError: The map is empty, a key or value is the empty
                string, or two kinds map to one label.
        """
```

### Plan Types

```python
@dataclass(frozen=True)
class SourceEventSourcePlan:
    """One resolved audited population set of the event log."""

    item_type: str
    """The RESOLVED item-type: the declaration's `item_type` override, else
    the kind's label (owner-half-labeled `<label(K)>.<property>` for a
    membership source), else the contract identity verbatim. The
    dereference key, the union-safety gate key, and the order-key
    component."""

    audited_properties: "tuple[tuple[str, str], ...]"
    """The audited set as (source bare name, changes output key) pairs,
    sidecar column-declaration order — the declared-table (source ->
    output) projection shape applied to changes keys. Output key equals
    the bare name absent a rename. For a membership reference field the
    pair expands at render to `<key>_kind` / `<key>_id`."""

    kind_labels: "tuple[tuple[str, str], ...]"
    """The resolved (kind, label) map threaded to the render for
    `<f>_kind` entry values; identity fall-through for any value not
    listed. Empty when no labels are declared."""

    # ... existing fields (kind, property, populations, item_surface,
    #     change_edges) unchanged ...


@dataclass(frozen=True)
class SourceJunctionTablePlan:
    """The resolved junction unit."""

    kind_labels: "tuple[tuple[str, str], ...]"
    """The resolved (kind, label) map for projected `member__<f>__kind`
    column values; identity fall-through. Empty when no labels are
    declared."""

    # ... existing fields unchanged ...
```

### Functions

```python
def build_kind_label_expr(
    value_expr: str,
    labels: "tuple[tuple[str, str], ...]",
) -> str:
    """The label-rendered SQL expression for one kind-name-valued expression.

    A compile-time CASE over the declared (kind, label) pairs with identity
    fall-through: a value matching no pair — an unlabeled kind, or a
    corrupted emit's mutated cell — renders verbatim, and NULL stays NULL.
    Byte-identical passthrough (`value_expr` unchanged) when `labels` is
    empty, mirroring the no-join composition rule for default elections.

    The one labeling authority for both call sites: the junction render's
    projected `member__<f>__kind` column, and the event log's `<f>_kind`
    entry values (old and new halves) inside the `changes` JSON assembly.

    Args:
        value_expr: A VARCHAR-typed SQL expression carrying a kind name —
            the junction's qualified `member__<f>__kind`, or the fold's
            `<f>_kind` after-image value expression.
        labels: The resolved (kind, label) pairs, declaration order.

    Returns:
        A VARCHAR-typed SQL expression.
    """
```

## Validation Rules

### Parse-Time (Pydantic)

| Validator | Rejects |
|---|---|
| `source_shape` (`SourceEventSourceDecl`, extended) | Empty `item_type`; `rename` present-but-empty; two `rename` keys sharing a target value |
| `kind_labels_shape` (`SourceConfig`, new) | Empty `kind_labels` map; empty key or value; two kinds mapping to one label |

### Business Rules

Run at plan time against the open emit, before any write; `{owner}` labeling
as today (`events source #<n>`, 1-based declaration order).

| Rule | Checks | Error Message |
|---|---|---|
| `SourceKindLabelUnknown` | Every `kind_labels` key has a `records__<kind>` table in the sidecar | `"kind_labels: kind '{kind}' not in this emit"` |
| `SourceKindLabelCollision` | After labeling, kind → rendered name is injective over the emit's whole kind universe (a label equals no other kind's label and no unlabeled kind's name) | `"kind_labels: label '{label}' collides with kind '{kind}'"` |
| `SourceItemTypeCollision` | Resolved item-types are pairwise distinct across sources, except records sources of one kind may share one; and no resolved item-type equals the rendered name of another kind (of any kind, for a membership source) — ranged over the emit's whole kind universe | `"events: sources #{m} and #{n} resolve one item_type '{item_type}' over two audited item spaces"`; for the rendered-name clause `"events source #{n}: item_type '{item_type}' collides with kind '{kind}'"` |
| `SourceColumnUnresolved` (extended) | Every events-source `rename` key names an audited property (element field) of its source, surviving `only` / `ignore` narrowing | `"{owner}: '{entry}' not a column of its source"` (the narrowed-away case names the `only` / `ignore` entry) |
| `SourceSliceOnlyRead` (extended) | No events-source `rename` key names a non-exempt `slice_only` column | As today — names the entry, the column, and the omission reason |
| `SourceNameCollision` (extended, per source) | Within one source, resolved changes keys are distinct after renames (a membership pair's expanded `_kind` / `_id` names included) — the existing class's contract (names within one resolved output surface, never silently suffixed or dropped) applied to the `changes` key surface | `"{owner}: changes key collision: {keys}; resolve via rename"` |
| `ElectionMixedIdentity` / `ElectionUnionUnsafe` (granularity change only) | The per-item-type edge union-safety gate ranges over **resolved** item-types | Per the existing key-election rules, message naming the resolved item-type |

## Rationale

- **Sub-types are the concepts; kinds are the engine's grouping.** A kind
  exists to let similar functionality share machinery; the `<K>_type`
  sub-types are, were, and remain the first-class domain concepts and the
  expected default shape of the data. That is why the per-source `item_type`
  override is the primary naming surface (each split of a kind names its own
  concept), why `kind_labels` is a convenience for the one-concept kind and
  for the structurally kind-valued surfaces, and why sub-type discriminator
  *values* render verbatim — they are already domain vocabulary.
- **A naming surface, not a derivation.** Resolving `item_type` through "the
  declared state table for the same population" was rejected: a kind split by
  `sub_types` maps to several tables, a kind may be audited without any
  declared table, and coupling the log's vocabulary to table declarations
  makes it change silently when tables do. The current contract's posture —
  item-type is "sidecar-derived, independent of which thing-tables are
  declared" — is kept; what changes is that the author can now *declare* the
  vocabulary, which is the mode's own thesis (declared intent drives output).
  The derive-from-tables instinct lives where inference belongs: an author
  reading their own table names while writing `kind_labels`.
- **Mode-level `kind_labels` plus per-source override, not per-source only.**
  Kind names surface where no per-source declaration exists (junction member
  kinds), and one kind can appear as a value across many sources and tables —
  a single map keeps the kind-level vocabulary consistent by construction.
  The per-source `item_type` stays the first-class naming over it; the
  rendered-name collision clause is what keeps the two layers from
  contradicting each other (no layer outranks another — one rendered name,
  one population space).
- **Identity fall-through, not strictness, at render time.** Plan-time
  validation is against the sidecar; render-time values may be corrupted by
  design (the dirty source dump). A total mapping preserves declared defects
  and keeps the render infallible.
- **Row-predicate addressing stays out** — decided separately (decision note
  `source-mode-narrows-rows-by-sub-types-only-no-row-predicate-surface`): the
  analytical partition is the star's job; source's population grammar tracks
  the structural partition the sidecar declares.

## Boundaries

- No value mapping for property *values*, sub-type discriminator values, or
  any payload cell — kind names and changes keys only. A general value-map
  surface is dimensional's `derived` territory and out of source's fidelity
  posture.
- No streaming-mode vocabulary — separable future design if wanted.
- No `init` proposal for labels — identity under verbatim proposals.
- No per-table (as opposed to per-mode) label scoping: one vocabulary per
  export.
