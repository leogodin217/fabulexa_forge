# Base Exporter

**Status:** Implemented. Code is the contract — see
[`exporters/base/`](../../src/fabulexa_forge/exporters/base/)
(`plan.py`, `renders.py`, `engine.py`),
[`derivations/state_at.py`](../../src/fabulexa_forge/derivations/state_at.py),
[`derivations/record_index.py`](../../src/fabulexa_forge/derivations/record_index.py),
[`config/models.py`](../../src/fabulexa_forge/config/models.py) (`BaseConfig`), and
[`tests/exporters/base/`](../../tests/exporters/base/),
[`tests/config/test_base_config.py`](../../tests/config/test_base_config.py),
[`tests/recipes/test_base_recipes.py`](../../tests/recipes/test_base_recipes.py),
[`tests/integration/test_corrupt_base.py`](../../tests/integration/test_corrupt_base.py).
Public API: [`exporters/base/engine.py`](../../src/fabulexa_forge/exporters/base/engine.py)
(`export_base`, `build_base_query_specs`) and
[`exporters/base/plan.py`](../../src/fabulexa_forge/exporters/base/plan.py)
(`build_base_plan`).

The `mode: base` exporter renders the emit as a flat single-branch projection: one
row per record, reconstituted to current state — or to an as-of-T state — with no
declared-table grammar and no audit log. Every output table is the state-at
reconstruction of one records kind, materialized as a table. Where source hands the
consumer an app-database schema (thing tables plus an audit log) and dimensional
hands over a reconstructed star, base hands over the already-merged answer: the flat
current-truth table an incremental-ETL author is building. It reads the same emit as the other
modes and composes two derivations-layer residents as its whole engine — state-at for
every value, the record-index resident for every identity key — and introduces no
point-in-time reconstruction of its own.

Each table presents both encodings of every identity the base layer carries: the
id-space `record_id` and reference id it has always emitted, and the index-space
integer key beside it. The integer key is the merge key incremental-ETL and SCD-2
lessons are written against; the opaque id alone is the wrong shape to teach them.

```
records__<kind>  ─┐
history          ─┼─▶  state-at resident  ─▶  values  ─┐
                 │      end-of-tape   (no slice_at → current state)
                 │      horizon T+1   (slice_at: T → as-of-T)     ├─▶ flat <kind> table
                 │      window end_ns (incremental → per-window)  │   (one row/record)
                 └─────────────────────────────────────────────   │
records__<kind>  ─┐                                               │
records__<target>─┴─▶  record-index resident ─▶ self + edge keys ─┘
                       (LEFT JOIN, same horizon as the values)
```

---

## Surface

| Module | Owns |
|---|---|
| [`config/models.py`](../../src/fabulexa_forge/config/models.py) | `ExportConfig.mode: Literal["dimensional", "source", "base"]`, `ExportConfig.base: BaseConfig \| None`; `BaseConfig` (`exclude`, `rename`, `slice_at`) and its parse-time validators; the `mode_section_matches` `base` arm and the `base_slice_at_excludes_incremental` cross-field rule |
| [`exporters/base/plan.py`](../../src/fabulexa_forge/exporters/base/plan.py) | `BaseTableSpec`, `BasePlan`, `ReferenceKey`, `NOTICE_REFERENCE_KEY_TARGET_ABSENT`; `build_base_plan` — records-kind enumeration (no classification), `exclude`, operational presentation defaults, `rename` resolution, the `slice_only` omission with its notices, reference-edge resolution to target kinds with its absent-target notice, and the collision and reserved-name checks over the key identities as well as the state-at ones |
| [`exporters/base/renders.py`](../../src/fabulexa_forge/exporters/base/renders.py) | `build_base_render_sql` — composes the state-at derivation at a horizon and the record-index resident at the same horizon, wraps them with base's presentation (lifecycle wallclock-or-raw-ns, cast-back to sidecar types, rename projection, key-column emission order), carrying the state-at resident's total `ORDER BY` |
| [`exporters/base/engine.py`](../../src/fabulexa_forge/exporters/base/engine.py) | `export_base`, `build_base_query_specs` — plan → per-kind render at one resolved horizon → dispatch to the shared writer. `build_base_query_specs` is the pure compile surface the full-export leaf and the incremental driver's `base` branch both call |
| [`derivations/state_at.py`](../../src/fabulexa_forge/derivations/state_at.py) | `build_state_at_sql`, `build_state_at_end_sql`, `STATE_AT_COLUMNS` — the point-in-time reconstruction supplying every base value; owned by [`derivations.md`](derivations.md) § The state-at derivation |
| [`derivations/record_index.py`](../../src/fabulexa_forge/derivations/record_index.py) | `build_record_index_at_sql`, `build_record_index_at_end_sql`, `RECORD_INDEX_COLUMNS` — the `record_id` → `record_index` join relation supplying every base key column; owned by [`derivations.md`](derivations.md) § The record-index derivation |
| [`exporters/slice_only.py`](../../src/fabulexa_forge/exporters/slice_only.py) | `is_non_exempt_slice_only` — the cross-mode omission predicate base's plan scans per column; owned by [`slice-only.md`](slice-only.md) |
| [`exporters/query_spec.py`](../../src/fabulexa_forge/exporters/query_spec.py) · [`exporters/reserved_names.py`](../../src/fabulexa_forge/exporters/reserved_names.py) | The mode-neutral compiled-table shape + full-export write dispatch, and the cross-mode bookkeeping-name check base's reserved-name enforcement calls |
| [`errors.py`](../../src/fabulexa_forge/errors.py) | The `Base*` error hierarchy (`ExportError` subclasses) |
| [`cli.py`](../../src/fabulexa_forge/cli.py) · [`incremental/driver.py`](../../src/fabulexa_forge/incremental/driver.py) | `cmd_export` dispatches `mode: base` to `export_base`; the driver dispatches a windowed `mode: base` compile to `build_base_query_specs` |

## Boundary

- **Input.** An open `Emit` (trunk-only — sole branch), a validated `ExportConfig`
  with `mode: base`, the resolved `EffectiveAnchor` **or `None`** (base does not
  require one), the `fmt`, and an optional `Window` for an incremental invocation.
- **Output.** Per `fmt`: one `<table>.csv` per surviving records kind into the output
  directory, or one typed table per kind in a single `.duckdb` file — both through the
  shared writer dispatch (`exporters/query_spec.py`). A zero-row table is still emitted.
- **Reader-first; no base-table SQL authored directly.** Every base read is the
  derivations-layer state-at fold composed over reader relations — the mode wraps that
  relation, never hand-writes `FROM records__…` or `FROM history`.
- **Forbidden imports.** `exporters.base` never imports `exporters.dimensional`,
  `exporters.source`, or `exporters.streaming`, and none imports it back — the mode
  packages are independent leaves composing only the reader, the derivations layer,
  and the mode-neutral `exporters.query_spec` / `exporters.reserved_names` modules. No
  dependency on the bundle's producer; the vendored `contract/` is the only coupling.

## Semantics

### The flat projection

Base declares nothing and reshapes nothing: every records-category kind in the
sidecar maps to exactly one flat output table, in sidecar table-declaration order.
There is no declared-table grammar, no sub-type split, and no membership, junction,
event-log, or fact table. A kind's table opens with its self key, then carries the
`STATE_AT_COLUMNS` prefix
(`record_id`, `created_sim_time`, `active`, `deactivated_at`), then `presentation_id`
when the kind carries it, then one `prop__<p>` per surviving property in sidecar
column-declaration order — each reference property immediately followed by its edge
key (§ Record-index key columns). Each non-key output value is a state-at reconstruction at the
chosen horizon: a tracked property carries its most-recent `history.value` at-or-before
the horizon (`NULL` when no history precedes it), a constant property carries its
current records value (the declared temporal-honesty exception every state-at consumer
shares), and a sub-typed discriminator `prop__<K>_type` is carried as a classification
value, never as an as-of value.

### Three horizons (tape's end · `slice_at: T` · window end)

The plan is time-agnostic; the horizon is supplied at render. `build_base_query_specs`
resolves exactly one horizon per invocation:

| Selector | Horizon | State-at entry point | Output |
|---|---|---|---|
| No `slice_at`, no `incremental` | Tape's end (structural, no horizon computed) | `build_state_at_end_sql` | One current-state full table per kind |
| `slice_at: T` (full export) | `T + 1` (exclusive; inclusive of events at T) | `build_state_at_sql(horizon_ns=T+1)` | One as-of-T full table per kind |
| `incremental` (`--next` / `--from` / `--to`) | Each window's `end_ns` | `build_state_at_sql(horizon_ns=end_ns)` | One full-table snapshot per kind per window |

`slice_at: T` is **inclusive of T** — an event at exactly `sim_time == T` is reflected,
so the exclusive state-at horizon is `T + 1`. `slice_at` and `incremental` are mutually
exclusive (§ Validation Rules). A windowed spec's `write_mode` is `'replace'` (every
table snapshot-delivered at the window horizon, exactly as source's windowed
`state` render); a full or sliced spec's is `'create'`.

### Lifecycle and mutation columns at a horizon

`active` and `deactivated_at` are horizon-rendered from the spine: a record deactivated
*after* the horizon shows `active = true`, `deactivated_at = NULL`. Deactivation is a
spine fact, not a `history` row — the end-of-tape entry point is used for current state
precisely so it is never mis-cleared against `history` alone. `created_sim_time` is
carried; a record created at-or-after the horizon is **absent** (state-at filters
`created_sim_time < horizon`), never present-with-nulls. `last_mutation_sim_time` (and
any `updated_at`) is **not emitted**: a past-horizon mutation time is not faithfully
reconstructible (an untracked write advances it leaving no history), so base omits the
column rather than fabricate or understate it — the same deviation source's snapshot
delivery makes, and `STATE_AT_COLUMNS` already excludes it.

### Record-index key columns

Every emitted table carries exactly one **self key** — the record's own
`record_index` — and one **edge key** per surviving reference property — the
referenced record's `record_index`. A *surviving reference property* is a
`prop__<p>` column of the kind that carries a sidecar `references` target and is not
omitted by the `slice_only` policy. Both families resolve through one mechanism: a
`LEFT JOIN` onto some kind's record-index relation ([`derivations.md`](derivations.md)
§ The record-index derivation), the kind being the table's own for the self key and
the property's `references` target for an edge key. One uniform rule produces both.

**Edge keys are re-derived, never carried.** An edge key resolves from the
horizon-reconstructed `prop__<p>` against the target kind's record-index relation at
the same horizon; the physical `ref_index__<p>` column is never read. The physical
value carries the target's index *at the emit's own slice* — the correct instant
only when the horizon is the tape's end. Reading it for `constant` properties and
re-deriving for `tracked` ones would be correct at one horizon and silently wrong
under `slice_at` and under every incremental window, so one rule correct everywhere
is worth the redundant work in the constant case. Both join sides are `VARCHAR` —
the format pins a reference property's `prop__` column to the id-only form and the
state-at relation's codec after-image is `VARCHAR` — so no cast participates in the
join.

**Both encodings ship, and the pair is not redundant.** An edge key is a `LEFT JOIN`
projection, so several distinct conditions collapse to NULL; the id-space column
beside it separates them. Emitting only the index would discard information the base
layer carries, which faithful reshaping forbids (Principle #3).

| Condition | id column (`<p>`) | key column (`<p>_key`) |
|---|---|---|
| Property absent on the record | NULL | NULL |
| Reference names a record created before the horizon | the id | that record's `record_index` |
| Reference names a record created at-or-after the horizon | the id | NULL |
| Reference names no record at all (a dangled sentinel) | the id | NULL |
| Target kind has no records table in this emit | the id | column not emitted |

A target record **deactivated** before the horizon still resolves: the record-index
relation filters on creation time only, so a deactivated record remains a legal
reference target and filtering it out would manufacture a dangling edge the base
layer does not contain.

**Density survives every horizon.** At any horizon a kind's emitted self keys are
exactly the integers `0 .. n-1` for that table's row count, because the surviving
set is always a creation-order prefix. Values are projected verbatim; nothing is
renumbered. This is what makes the self key a merge key rather than a row number — a
record carries the same integer at every horizon, in every window of an incremental
run, and in every emit of its branch, so two exports of the same branch are
comparable on it. Renumbering to close a gap would destroy exactly that
comparability. Density is inherited from the emit, never enforced: base asserts no
`0 .. n-1` check, and a corrupted emit whose index set is perforated or repeated
surfaces those values verbatim — the gap or repeat *is* the defect.

**Horizon binding.** The key relations and the value relation composing one output
table are composed at one horizon, matching the horizon the values were
reconstructed at (§ Three horizons). A mismatch would silently resolve edges against
the wrong population.

| Selector | Record-index entry point |
|---|---|
| No `slice_at`, no `incremental` | End-of-tape — no horizon predicate |
| `slice_at: T` (full export) | Horizoned at `T + 1` |
| `incremental` window | Horizoned at the window's `end_ns` |

The end-of-tape entry point carries no horizon predicate at all, matching the
state-at resident's structural posture: composed over truncated base relations it is
bounded by the truncation with no horizon computed. A tier-2 shaped playback over a
`mode: base` config therefore carries the key columns by the same composition that
gives it the value columns, which is what keeps the bridging equivalence intact — a
`slice_at: T` export and the base-shape compile over the tape truncated at `T` are
column-for-column equal ([`playback.md`](playback.md)).

**Naming.** The self key defaults to `<kind>_key` and an edge key to `<p>_key`, both
overridable through `rename` keyed on the contract identity — `record_index` and
`ref_index__<p>` respectively (§ Presentation, typing, and ordering). Two derivation
choices are load-bearing. The self key is named from the records **kind**, not the
post-`rename` output table name: deriving it from the table name would make the
default depend on whether a `rename` entry applied first, a resolution-order
dependency with no upside. An edge key is named from the **property**, not the
target kind: two properties on one kind may reference the same target —
`referring_doctor` and `attending_doctor` both landing on `doctor` — and naming from
the target would collide them into one name.

**Emission order and typing.** The self key is the table's **first** column, ahead of
`id` — surrogate-first is the convention the merge lesson teaches. Each edge key
immediately follows **its own** id-space column, mirroring the way the base format
interleaves each `ref_index__<name>` after its `prop__<name>`. Every other column
keeps its position. Both families are `BIGINT`, projected verbatim from
`record_index`, which the format pins `BIGINT NOT NULL`; a self key is never NULL,
while an edge key is nullable — its nullability comes from the outer join, not from
the source column.

**An omitted property omits its key.** A non-exempt `slice_only` reference property
is dropped from base output (§ The `slice_only` omission) and its edge key goes with
it. This is required, not incidental: the export-wide policy forbids any output value
from deriving from a `slice_only` column's value, and an edge key derived from an
omitted property's reconstructed value would be exactly that. The disappearance is
covered by that property's existing per-column omission notice, not separately
announced. The mechanical sub-typed-discriminator carve-out never interacts with the
rule — a discriminator is a closed-domain enum, never a reference-annotated property
— but the rule is stated over the property's `references` annotation rather than over
the carve-out, so the two are independent.

**An absent target kind omits the key with a notice.** An emit legally omits
`records__<K>` when kind *K* has no records in the slice, so a reference property
pointing at such a kind is contract-legal with no target table present. Base emits no
edge key for it and one `reference-key-target-absent` notice per kind × property
([`notices.md`](notices.md)); the id-space column is unaffected. Omission is the
right failure mode rather than raising: the emit is valid, and nothing base emits
otherwise is lost. The resolution is made at plan time, before any data is written,
so the notice precedes output and the table's column set is known before the render
runs. The record-index resident itself is stricter — asked for a kind with no records
table it raises `TableNotFoundError`, by the derivations layer's cause-based error
taxonomy. The permissive behavior is base's policy, applied by not asking.

**An excluded target kind keeps its key.** If a reference property's target kind is
`exclude`d from the export, the edge key is still emitted, matching the id-space
column's behavior exactly: base emits `prop__<p>` pointing at a kind the author
excluded. Suppressing one encoding but not the other would make the pair disagree
about what the export contains, and the author who excluded the kind is the one who
chose the dangling edge.

### Elected identity surfaces

The index keys above always ship; which id-space *value* surface ships beside
them is the cross-mode key-election surface's contract
([`key-election.md`](key-election.md) § Rendering: base). In brief: under a
config `keys` election, a table's own `presentation_id` election renders the
elected value in the id-space slot (rename key `presentation_id`, the
standalone payload column absorbed), a `record_index` election drops the
id-space self column (`<kind>_key` *is* the election), and each edge's
`prop__<p>` value column follows its *target* populations' elections beside the
always-on `<p>_key` — the two axes independent. Base's plan step runs the
election's identity gates over each kind's full declared domain (base never
splits — one table, one identity surface) and edge gates per reference edge.
Absent the `keys` block, every table carries the `<kind>_key` / `id` /
`prop__<p>` / `<p>_key` shape this doc states.

### The `slice_only` omission

Base auto-projects a kind's full property set — a flat projection has no author-named
column reads — so it enforces the export-wide `slice_only` invariant by **omission with
a notice**, the source-style shape. A non-exempt `temporal_class: slice_only`
`prop__<p>` column is dropped from the flat table, emitting one `slice-only-column-omitted`
notice per surviving kind × column, in kind order then sidecar column order, before any
data is written. The mechanical sub-typed-discriminator carve-out
(`name == prop__<K>_type ∧ subtype_values(K) ≠ ∅`) is honored: the discriminator is
carried and renameable. Omission is column-projection-only — a kind whose every property
is non-exempt `slice_only` still renders its identity, lifecycle, and any exempt
discriminator columns; omission never suppresses a table. A `rename` naming an omitted
column is a config error (`BaseRenameSliceOnly`), the rename being unsatisfiable rather
than silently ignored. Base decides *how* it enforces the policy, never *whether*
([`slice-only.md`](slice-only.md)).

### Presentation, typing, and ordering

Output table names default to the prefix-stripped kind (`records__customer` →
`customer`) and `record_id → id`, the operational presentation posture base shares with
source; the key columns' `<kind>_key` / `<p>_key` defaults are the same posture applied
to the same kind of column, and it is the name the merge lesson is written against.
All are overridable via `rename` (`name` for the table, a `columns` entry keyed
on the pre-default column identity — `record_id`, `presentation_id`,
`created_sim_time`, `active`, `deactivated_at`, `prop__<p>`, `record_index`,
`ref_index__<p>` — for a column). The key identities join the domain a `rename.columns`
key is validated against, so a typo fails at load rather than silently doing nothing.
Data columns
(`prop__<p>`, `presentation_id`) cast back from the state-at resident's codec VARCHAR
after-image to their declared sidecar types, so base delivers a typed table, not an
all-string one; `record_id` and `active` pass through verbatim. Lifecycle timestamps
render wallclock through the shared `render_anchor_temporal_expr` when an anchor
resolves and stay raw sim-time `BIGINT` when it is `None` — base carries **no anchor
conditional of its own**, since the renderer already handles `anchor=None`. Which
columns are lifecycle timestamps is the reader's answer: base reads the
instant-carrying structural columns of the `records` category off the
structural-temporal surface ([`reader.md`](reader.md) § The structural-temporal
surface). The render iterates the state-at relation's columns, and that projection
carries no `last_mutation_sim_time`, so that member of the set has nothing to match
here (§ Lifecycle and mutation columns at a horizon). Ordering is
the state-at resident's `(created_sim_time, record_id)` over raw ns keys, never rendered
timestamps.

**Render elections.** A per-table entry in `base.render` — `{table, render}`,
keyed on `table`, disjoint across entries, the same posture the mode's
`rename` list uses — carries the unified property-first `render` map: a bare
scalar elects a lifecycle timestamp's rendering (`created_sim_time` → `date`,
say); the typed forms address `prop__<p>` payload columns —
`{date_parse: "<format>"}` (a VARCHAR temporal string, rendered as its
format's denoted type), `{instant: <election>}` (a BIGINT sim-instant),
`{decimal: [p, s]}` (DOUBLE → exact `DECIMAL`), `{json_precision: {…}}`
(in-place JSON leaf rounding). The map is keyed on the same pre-default
column identities `rename.columns` uses, and each entry re-renders the
projected column in place. A bare-shorthand key must name an
instant-carrying structural column of the `records` category the render
actually emits — `last_mutation_sim_time` is outside the domain, the same
exclusion `rename` already makes for it (`RenderKeyResolves`,
[`temporal-elections.md`](temporal-elections.md) § Validation Rules); the
typed forms' key domains and source-type gates are the value elections'
([`value-rendering-elections.md`](value-rendering-elections.md) § The
unified render map). A `date_parse` source reads its declared type directly
from the sidecar and must not be `slice_only` (`DateParseSourceColumn`,
[`temporal-elections.md`](temporal-elections.md)).

### Corrupter composition

A base export over a corrupted emit surfaces the corrupter's declared defects unchanged
and manufactures none (Principle #3), by construction — no corrupter-aware branch
exists. Base casts each data column back to its sidecar type, so totality rests on the
corrupter family's value transforms being **type-preserving**: a corrupted `history.value`
remains a valid instance of its column's declared type, the cast-back succeeds, and the
defect surfaces *in* the reconstructed value rather than dropping or erroring a row. The
guarantee is verified by a dedicated integration test, not asserted by inspection.

The key columns rest on the composition holding in two further ways, both by
construction. **Reference-rewriting operations co-write coherent pairs** — a dangled
edge writes a sentinel id beside a sentinel index, a mispointed edge writes the
donor's id beside the donor's real index — so re-derivation resolves exactly the
defect the manifest declares: a dangled id finds no target and yields a NULL key, a
mispointed id finds the donor. **Row-set operations leave key joins one-to-one** —
exact duplication copies the row whole, so the duplicate carries the identical
`(record_id, record_index)` pair and the record-index relation's `DISTINCT` collapses
it out of the join's right side; deletion and insertion never reuse or collide an id
or an index, and the gaps they leave surface verbatim (§ Record-index key columns).
The one shape that could fan a key join out — two rows of one kind sharing a
`record_id` with differing `record_index` — is not producible: identity columns sit
outside every corrupter cell operation's eligible population.

## Invariants

1. **Records-only flat grain.** Base emits exactly one flat table per surviving records
   kind and nothing else — no membership, junction, fact, or CDC table.
2. **Two residents are the whole engine.** Every base table value is a state-at
   reconstruction at some horizon (tape's end, `T + 1`, or a window end), and every key
   column is a record-index projection at that same horizon; base writes no independent
   point-in-time path.
3. **One inclusive horizon per full export.** `slice_at: T` reflects every event with
   `sim_time ≤ T` and nothing after; the exclusive state-at horizon is `T + 1`.
   Current-state uses the structural end-of-tape entry point, never a horizon cleared
   against `history` alone.
4. **`slice_only` enforcement is omit-with-notice, carve-out honored.** Base inherits the
   export-wide invariant and chooses omission; the discriminator carve-out is honored;
   omission is column-projection-only and never suppresses a table. No output column —
   key columns included — derives from a non-exempt `slice_only` column's value.
5. **Faithful reshaping.** Every value traces to a base-layer value or a deterministic
   recoding (a cast, a horizon mask, a wallclock render); base fabricates nothing, and a
   corrupted emit surfaces its declared defects unchanged.
6. **Anchor optional, single, shared.** Base renders wallclock through the one resolved
   effective anchor or emits raw ns; it never resolves a second anchor and never requires
   one.
7. **Determinism.** Same emit + export config + code version → identical output, including
   the notice sequence.
8. **`slice_at` ⊕ `incremental`.** A base config carries at most one temporal selector;
   the two together are a load-time error.
9. **Reserved names are enforced always-on.** No resolved output table name is a
   bookkeeping name or reserved suffix (`_export_meta` / `_export_windows` / `*__rows`),
   and no output column name is `__valid_from_ns` or `last_mutation_sim_time` — checked
   at plan build over every export (full included), so a full export and a later
   `--next` on the same target agree.
10. **Both encodings or neither, when resolvable.** A surviving reference property whose
    target kind's records table is present emits its id-space and index-space columns
    together; neither ships without the other. An absent target kind is the one stated
    exception: the key column is omitted with a notice and the id-space column stands
    alone.
11. **Edge keys are re-derived.** No base output value is read from a physical
    `ref_index__` column.
12. **One horizon per table.** The value relation and every key relation composed into
    one output table are composed at the same horizon.
13. **Density under every horizon, inherited — never enforced.** Over a conformant emit a
    table's self keys are exactly `0 .. n-1` for its row count, at every horizon, and
    nothing is renumbered. The property follows from the emit's dense `record_index` and
    the creation-order-prefix filter; it is not a check base performs, so a corrupted
    emit's perforated or repeated indexes surface verbatim.
14. **Creation-time filtering only.** Key resolution filters targets on creation time and
    never on `active`.
15. **Key resolution preserves row count.** Composing the key relations neither adds nor
    drops output rows — base's row set is the state-at spine's, exactly. The key relation
    is distinct over `(record_id, record_index)` and a duplicated `record_id` always
    carries an identical `record_index`, so every key join is at most one-to-one per
    spine row.

## Validation Rules

Field shapes are defined by the Pydantic grammar in
[`config/models.py`](../../src/fabulexa_forge/config/models.py) (`BaseConfig`,
`ExportConfig`); business-rule message text is owned by
[`exporters/base/plan.py`](../../src/fabulexa_forge/exporters/base/plan.py). The rules
below state *what* is rejected and *when*.

**Parse-time (Pydantic).**

| Validator | Rejects |
|---|---|
| `at_least_one_field` (`BaseConfig`) | A present `base:` block setting no field (`model_fields_set` empty) — a bare `base: {}` is rejected; omit the section for a bare current-state dump |
| `slice_at_non_negative` (`BaseConfig`) | A negative `slice_at` |
| `rename_no_sub_type` (`BaseConfig`) | A `rename` entry setting `sub_type` — base never splits a kind, so a split-unit selector is meaningless |
| `entries_disjoint` (`BaseConfig`) | Two `rename` **or** `render` entries targeting the same `table` — base has one output table per kind, so `table` alone is the key, checked across both lists |
| `mode_section_matches` (`ExportConfig`, `base` arm) | A `dimensional` or `source` section present under `mode: base`; the `base` section itself is optional (a bare `mode: base` is a valid full dump) |
| `base_slice_at_excludes_incremental` (`ExportConfig`) | A config setting both `base.slice_at` and an `incremental` block — a pinned instant and a window sequence are contradictory temporal selectors |
| `entry_well_formed` (`BaseRenderDecl`) | An empty `table`; a present `render` map that is empty or carries an empty key. Each entry's own shape — decimal bounds, the json-precision leaf map, the date-parse format rules — is carried by its `RenderElection` model ([`value-rendering-elections.md`](value-rendering-elections.md)); one map per column makes a conflicting pair unrepresentable |

**Business rules.** Run at plan build against the open emit's sidecar, before any write;
each raises an `ExportError` subclass surfaced through the CLI's existing error funnel.

| Rule / Error | Checks |
|---|---|
| `BaseExcludeUnresolved` | Every `exclude.kinds` and `exclude.tables` entry resolves — both check the pre-`rename` prefix-stripped kind names (base's only presentation default at this stage), so the two resolve against the same known set |
| `BaseRenameUnresolved` | Every `rename` entry's `table` resolves to a surviving `records__<kind>`, and every `columns` key names an identity the kind actually emits in this emit — a state-at column identity, `record_index`, or a `ref_index__<p>` whose edge yields a key column. A `ref_index__<p>` for a non-reference property, or for one whose target kind has no records table here, is not in that set, so the rule falls out of the same check |
| `BaseRenameSliceOnly` | No `rename` `columns` key names a non-exempt `slice_only` column or its `ref_index__` shadow — the column is policy-omitted, so the rename is unsatisfiable |
| `BaseNameCollision` | All output table names are unique, and within each table all output column names are unique, after presentation defaults and `rename` — the key identities participating in the same domain as the state-at ones |
| Reserved-name check (`ExportError`) | No resolved output table name is `_export_meta` / `_export_windows` / `*__rows`, and no output column name — key columns included — is `__valid_from_ns` or `last_mutation_sim_time` — enforced always-on via `exporters/reserved_names.py` |
| Reference target resolvable | Each surviving reference property's target kind has a records table in the sidecar. Present: the edge key is emitted. Absent: the edge key is omitted and one `reference-key-target-absent` notice is emitted — a notice, not an error |
| `RenderKeyResolves` | Every `render`-map key resolves in its value form's domain: a bare-shorthand key names an instant-carrying structural column of the `records` category the render emits (reader-sourced, never a private list; `last_mutation_sim_time` outside the domain, the mode's existing `rename` exclusion); a typed-form key names a `prop__<p>` payload column ([`temporal-elections.md`](temporal-elections.md); [`value-rendering-elections.md`](value-rendering-elections.md)) |
| `DateParseSourceColumn` | Every `{date_parse: …}` key names a declared VARCHAR `prop__<p>` column, read from the sidecar type directly, and not `slice_only` ([`temporal-elections.md`](temporal-elections.md)) |
| `DecimalSourceIsDouble` / `InstantSourceIsBigint` / `JsonPrecisionSourceIsVarchar` | Each typed value election's source column carries its admitted declared type ([`value-rendering-elections.md`](value-rendering-elections.md) § Validation Rules) |
| `TemporalRenderRequiresAnchor` | Every elected instant rendering — bare shorthand and payload `instant` alike — has a resolved effective anchor ([`temporal-elections.md`](temporal-elections.md)) |
| Single-branch guard (`derivations/guard.py`, cross-mode) | Exactly one branch |

Every business rule is evaluated over every export — full, sliced, and windowed alike —
so that a full export and a later incremental run on the same target agree on the output
shape.

`slice_only` omission itself is not a business-rule error — it is the
`slice-only-column-omitted` notice, emitted per surviving kind × omitted column before
any data is written. The key columns add no config fields: they are the capability, and a
toggle for a demand nobody has expressed is scaffolding (Principle #8).

`declare_keys` (`BaseConfig`, optional boolean, no cross-field rule) is the one
key-*declaration* config field: opt-in declared key metadata on every flat table —
the `<kind>_key` primary key, `id` uniqueness, and `presentation_id` uniqueness
where the sidecar's `presentation_keys` block claims it — materialized as DuckDB
constraints. The resolution rules, writer semantics, CSV posture, and incremental
gating are owned by [`declared-keys.md`](declared-keys.md).

## Rationale

- **Direct-horizon over compile-indirection.** Base's shape *is* state-at, so the
  truncated-tape wrapping the playback seam needs for multi-table shapes (SCD-2 `LEAD`,
  fk hops, membership grains) buys base nothing. The bridging theorem states state-at at
  horizon `T + 1` equals the base-shape compile over the tape truncated at T, so base
  realizes point-in-time by the simpler of the two equal paths — passing a horizon to
  the state-at resident — and never touches the `base_relations` compile-indirection.
- **Omit, not refuse, for `slice_only`.** Base auto-projects — the author names no column
  reads — so an omission-with-notice matches the authoring model exactly as it does for
  source; refusing would demand an author-named read a flat projection never has.
- **Subsume point-in-time, not queue-state.** A `mode: base` export with `slice_at: T`
  *is* the "replay `history` to sim-time T → one flat row per record" feature-store row
  shape, so that Stage-5 prong is delivered directly rather than built separately.
  Queue-state is a genuinely different grain and derivation and is correctly left
  separate — collapsing it into base would fabricate a coupling that is not there.
- **No anchor requirement.** Base's teaching target is incremental ETL / SCD merge, where
  raw sim-time keys are a legitimate and common landing shape; requiring wallclock (as
  source does) would foreclose that lesson.
- **A join relation, not a wider state-at tuple.** Key resolution composes a narrow
  `(record_id, record_index)` relation rather than widening the state-at reconstruction
  tuple. State-at is a reconstruction contract with three consumers, not "the records
  table minus some columns" — widening it would change three unrelated outputs to serve
  one. The join is also what keeps the two relations in horizon agreement: an edge is
  resolved from the very `prop__<p>` value the value relation produced, so nothing is
  reconstructed twice and the two cannot drift.
- **Both encodings, never the index alone.** An index-space key alone cannot distinguish
  "no reference" from "dangling reference" — both are NULL. The id-space column beside it
  makes the distinction visible, and dropping it would discard information the base layer
  carries (Principle #3). The pair is the deliverable, not a transition state.

## Boundaries

- **Membership / queue-state is neither emitted nor subsumed.** Base reads `records__*`
  + `history` only and emits no `membership__*` / junction / queue table. The Stage-5
  queue-state export reads `membership__*`, derives a different grain (wait time,
  FIFO / priority order), and composes the membership-state-at resident — orthogonal to
  base's records-only flat projection, and a separate future item.
- **No CDC / change-log shape.** Base never emits an `op` / `changed_at` column or a
  version-per-change row; that shape is source's and streaming's. Base delivers the
  merged result, not the change log.
- **The standalone surrogate is auto-projected — renameable, not droppable, and
  ungated when unelected.** Base is the one publishing layer that auto-projects an
  identity surface ([`key-election.md`](key-election.md) § Identity publication):
  under a `record_id` or `record_index` election the standalone `presentation_id`
  column ships whenever the kind carries one, and the grammar renames it but
  cannot drop it. The distinction that carries this is base's alone: every table
  always ships a complete, election-independent join surface — the `<kind>_key`
  self key and per-edge `<p>_key`, re-derived from `record_index`, one shared
  dense space per kind, union-safe by construction (§ Record-index key columns) —
  so the surrogate sits beside a correct key as payload, where a stream's would
  sit beside the message key looking equally key-like. Base's identity *slot* is
  election-gated already; the known limit is that an author who finds the
  standalone column misleading can rename it and nothing more.
- **Not a playback consumer.** Base is a CLI file exporter; it does not call `state(T)`
  and does not use the compile-indirection (`base_relations`) wrapping. It reaches
  state-at directly by horizon.
- **Single-branch, like every mode.** Base uses the derivations layer's single-branch
  guard; branch-aware export is parked pending a contract extension
  ([`README.md`](README.md) § Staged roadmap).
- **CSV + DuckDB only.** No Parquet — the cross-mode writer boundary
  ([`writers.md`](writers.md)).

## Related

| Document | Why |
|---|---|
| [`derivations.md`](derivations.md) | The state-at and record-index residents base composes as its whole engine — values from the first, key columns from the second |
| [`source.md`](source.md) | The windowed state snapshot (the same state-at composition), the presentation-name posture, and the `slice_only` omission shape base shares |
| [`slice-only.md`](slice-only.md) · [`notices.md`](notices.md) | The reused omission policy and the channel its notices flow through |
| [`declared-keys.md`](declared-keys.md) | The opt-in `declare_keys` capability — declared primary-key / uniqueness constraints on base's flat tables |
| [`key-election.md`](key-election.md) | The cross-mode key-election surface — the elective id-space value surface beside the always-on index keys, and the gates base's plan runs |
| [`temporal-elections.md`](temporal-elections.md) | The cross-mode temporal election vocabulary the `base.render` declaration list's temporal spellings render through |
| [`value-rendering-elections.md`](value-rendering-elections.md) | The unified `render` map's grammar and the typed value elections (`instant` / `decimal` / `json_precision`) the `base.render` entries carry |
| [`playback.md`](playback.md) | Shaped state and the bridging theorem that make direct-horizon equivalent |
| [`anchor.md`](anchor.md) · [`incremental.md`](incremental.md) | The shared wallclock renderer and the window/cursor/fingerprint driver base wires into |
| [`corrupters.md`](corrupters.md) | The corrupt → base composition — a base export over a corrupted emit surfaces declared defects unchanged |
| [`writers.md`](writers.md) | The CSV / DuckDB adapters base shares with every mode |
| [`../../contract/base-format.md`](../../contract/base-format.md) | `temporal_class`, the MUST-NOT-present-as-of-T clause, and the records / `history` shapes |
| [`../CAPABILITIES.md`](../CAPABILITIES.md) | Base-mode feature inventory and status |
| [`README.md`](README.md) | Design index, package layout, staged roadmap |
