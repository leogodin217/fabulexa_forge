# Selection Spine

The mode-neutral row-selection device the source and streaming exporters both
narrow rows through. A declared unit selects rows two ways — by population
(`sub_types`) and by value (`where`) — and neither axis is readable from the
rows themselves when the unit's grain is a membership interval, whose columns
carry no owner attributes. The spine answers both from one place: a
`record_id`-producing SELECT over a kind's records spine, composed as a
semi-join by the caller. It is the single seam for both directions of that
read — a records-backed unit narrowing its own rows, and a membership unit
reaching its **owner** through the parent lookup.

**Source:** [`exporters/selection_spine.py`](../../src/fabulexa_forge/exporters/selection_spine.py).
Tests: [`tests/exporters/source/test_where_plan.py`](../../tests/exporters/source/test_where_plan.py)
and [`tests/exporters/streaming/test_selection.py`](../../tests/exporters/streaming/test_selection.py).

## Boundary

- **In:** a `Sidecar`, the sole `fork_path`, the subject kind, its addressed
  `Population` tuple, and the caller's resolved predicate entries
  (`WhereEntry` — the key as written, its `prop__<p>` base-table column, the
  column's sidecar-declared DuckDB type, the config value verbatim, and the
  per-element plan-time cast results).
- **Out:** one `SELECT "record_id" …` string, or `None` when neither axis
  restricts. The caller composes it; the spine executes nothing and opens no
  connection.
- **The gates are the caller's.** The spine receives entries already
  gate-passed and already type-cast. Which columns are addressable, how a key
  is spelled, and what each refusal is named belong to the mode — the spine
  neither resolves a key nor raises a config error.
- **Forbidden imports.** It imports the reader, the shared SQL utilities, the
  population atom, and the notice channel — never `exporters.source.*` or
  `exporters.streaming.*`. Both modes import this module; neither reaches the
  other through it.

## Semantics

### The spine relation

`build_selection_spine_sql` composes the kind's records relation and filters
it by the conjunction of two conditions, each composed only when it restricts:

| Axis | Condition | Composed when |
|---|---|---|
| Population | `prop__<K>_type IN (<sub-type literals>)` | The kind is sub-typed **and** the addressed set is a proper subset of its declared domain |
| Value | One `render_predicate_condition` per entry, AND-joined | The caller passed any entry |

A flat kind has no discriminator column and a full-domain address needs no
filter, so neither composes a no-op predicate. When neither axis restricts the
result is `None` — the caller's signal that every row is in scope, and the
reason an unnarrowed export composes no join at all rather than a
tautological one.

The relation is **fan-out-free**: `record_id` is unique on the records spine,
so a semi-join against it can only remove rows, never multiply them. This is
what lets a membership unit narrow by owner without changing its own
cardinality.

The relation is **horizon-free**: it reads current spine values only. That is
sound because of what the callers' gates admit — a discriminator is
creation-constant by contract, and every `where` column is gated to
`temporal_class: constant`, whose value is identical at every horizon. So the
satisfying set is one set for the whole tape: the same rows under a full
export, under every incremental window, and at every event time. The gates
make the as-of-which-instant question unposable rather than answering it; the
spine is where that guarantee is cashed.

### The parent lookup

A membership unit's rows carry no owner attributes — an owner property is not
a column of the membership table at all. Its selection therefore evaluates
against the owner: the caller passes the **owner** kind, and the spine's
`record_id` column is the owner id the membership rows' owner column joins to.
It is the identical relation a records-backed narrowing composes, read from
the membership side; there is no second code path and no membership-specific
shape.

The lookup is for selection only. No owner attribute is projected into the
unit's columns, and the membership surface — interval columns, element fields,
member pairs — is untouched by it.

### The observed-value notice

`check_where_values_observed` emits one `discriminator-value-unobserved`
[notice](notices.md) per predicate element that falls outside its column's
declared `enum_domains` entry; a column with no entry is unchecked, and an
out-of-domain element is never an error. Declaring a value some emit has not
observed is how one config legitimately serves a family of emits, and the
zero-match outcome is legal — declared intent drives existence.

The two-case structure is fixed here: whether *every* element of an entry was
unobserved is computed once and passed to the caller, because that distinction
changes what the author must be told (the whole unit will be empty, versus
this one element contributes nothing). The **wording** is the caller's, passed
in as a `message` callable — source renders its unit's nouns, streaming
renders its stream's — over the one structure. Notice order follows the
caller's iteration order, which is what keeps the sequence deterministic.

## Invariants

1. **Fan-out-free.** The spine yields at most one row per `record_id`, so a
   semi-join through it never multiplies the composing unit's rows.
2. **Horizon-free.** The relation reads current spine values only, and the
   callers' gates admit only creation-constant columns — so the satisfying set
   is identical at every horizon and in every window.
3. **No no-op composition.** When neither axis restricts, the result is `None`
   and the caller composes no join; an unnarrowed export is byte-identical to
   one that never had the feature.
4. **One rendering authority.** Every value condition is rendered by
   `render_predicate_condition` ([`row-predicates.md`](row-predicates.md));
   the spine renders no `=` or `IN` of its own. The discriminator filter is a
   population filter, not a config predicate, and keeps its own literal
   rendering.
5. **Mode-neutral.** The module names no mode: no `Source*` or `Stream*` error
   is raised here, and no mode's column-addressing convention is encoded.

## Rationale

- **One relation, not one per mode.** Both modes need the identical
  fan-out-free, horizon-free owner lookup resting on the identical gate
  guarantees, so the relation lives in a mode-neutral home both import (the
  `exporters/election.py` precedent) rather than once per mode. A per-mode
  sibling would be a second place for the horizon argument to drift, and the
  argument is the whole reason the lookup is safe.
- **The spine takes entries, not config.** Resolving a key to a column, gating
  its temporal class, and naming the refusal are per-mode concerns — source
  and streaming spell keys differently and name their errors differently. A
  shared resolver parameterized over key form, label, and error class would
  entangle exactly what the two modes must be free to differ on, so the split
  is at the resolved entry: the modes own the gates, the spine owns the
  relation. Each mode's gate walk stays its own, and the duplication that
  remains is the deliberate price of independent error vocabularies.
- **The notice's wording is the caller's, its structure is not.** The
  two-case distinction is a property of the predicate, identical everywhere;
  the nouns naming what will be empty are a property of the mode's output
  unit. Sharing the structure keeps the two modes' notices in step; passing
  the wording keeps each one legible in its own vocabulary.
- **`None` rather than a tautology.** Returning a `WHERE TRUE` spine would
  make every export pay a join to express "no selection", and would make the
  no-selection path indistinguishable from the selected one in the rendered
  SQL. The absent-selection signal is explicit.

## Boundaries

- **It resolves nothing.** Key resolution, the constant-column gate, the
  discriminator refusal, and the plan-time literal casts all run in the
  calling mode before the spine is reached. The spine trusts its entries.
- **It executes nothing.** It returns SQL text; the caller runs it and decides
  what to do with the result — source composes it as a semi-join inside a
  render, streaming materializes the id set and drops fold rows outside it.
- **It carries no horizon parameter.** The horizon-invariance is established
  by the callers' gates, not by an argument. A future predicate surface over a
  non-constant column could not compose this relation as-is; it would owe its
  own answer to the as-of-which-instant question.
- **It is not the predicate grammar.** The scalar-or-list value shape, its
  well-formedness rule, and its literal typing belong to
  [`row-predicates.md`](row-predicates.md). This doc owns the *relation* the
  compiled conditions are evaluated against.

## Related

| Document | Why |
|---|---|
| [`row-predicates.md`](row-predicates.md) | The predicate grammar and the one rendering authority whose conditions the spine composes |
| [`source.md`](source.md) | The first consumer — the constant-column gate, per-unit key forms, and owner `sub_types` as the addressed population set (§ Row selection) |
| [`streaming.md`](streaming.md) | The second consumer — per-stream `where` and membership owner `sub_types` resolved into a satisfying record set (§ Row selection) |
| [`notices.md`](notices.md) | The channel the per-element unobserved-value notice flows through |
| [`reader.md`](reader.md) | The records relation the spine is built over |
| [`slice-only.md`](slice-only.md) | The unknowable-past policy the callers' constant gate subsumes |
