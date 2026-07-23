# Base Exporter

**Status:** Implemented. Code is the contract — see
[`exporters/base/`](../../src/fabulexa_forge/exporters/base/)
(`plan.py`, `renders.py`, `engine.py`),
[`derivations/state_at.py`](../../src/fabulexa_forge/derivations/state_at.py),
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
genre distinction and no change log. Every output table is the state-at
reconstruction of one records kind, materialized as a table. Where source hands the
consumer the change log to merge (`MAX`-per-id, `LEAD`) and dimensional hands over a
reconstructed star, base hands over the already-merged answer: the flat current-truth
table an incremental-ETL author is building. It reads the same emit as the other
modes and composes the shipped state-at derivation as its whole engine — it introduces
no point-in-time reconstruction of its own.

```
records__<kind>  ─┐
history          ─┼─▶  state-at resident  ─▶  flat  <kind>  table (one row/record)
                 │      end-of-tape   (no slice_at → current state)
                 │      horizon T+1   (slice_at: T → as-of-T)
                 │      window end_ns (incremental → per-window snapshot)
                 └────────────────────────────────────────────────────────────
```

---

## Surface

| Module | Owns |
|---|---|
| [`config/models.py`](../../src/fabulexa_forge/config/models.py) | `ExportConfig.mode: Literal["dimensional", "source", "base"]`, `ExportConfig.base: BaseConfig \| None`; `BaseConfig` (`exclude`, `rename`, `slice_at`) and its parse-time validators; the `mode_section_matches` `base` arm and the `base_slice_at_excludes_incremental` cross-field rule |
| [`exporters/base/plan.py`](../../src/fabulexa_forge/exporters/base/plan.py) | `BaseTableSpec`, `BasePlan`; `build_base_plan` — records-kind enumeration (no classification), `exclude`, operational presentation defaults, `rename` resolution, the `slice_only` omission with its notices, and the collision and reserved-name checks |
| [`exporters/base/renders.py`](../../src/fabulexa_forge/exporters/base/renders.py) | `build_base_render_sql` — composes the state-at derivation at a horizon, wraps it with base's presentation (lifecycle wallclock-or-raw-ns, cast-back to sidecar types, rename projection), carrying the state-at resident's total `ORDER BY` |
| [`exporters/base/engine.py`](../../src/fabulexa_forge/exporters/base/engine.py) | `export_base`, `build_base_query_specs` — plan → per-kind render at one resolved horizon → dispatch to the shared writer. `build_base_query_specs` is the pure compile surface the full-export leaf and the incremental driver's `base` branch both call |
| [`derivations/state_at.py`](../../src/fabulexa_forge/derivations/state_at.py) | `build_state_at_sql`, `build_state_at_end_sql`, `STATE_AT_COLUMNS` — the point-in-time reconstruction base composes as its whole engine; owned by [`derivations.md`](derivations.md) § The state-at derivation |
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

Base classifies nothing and reshapes nothing: every records-category kind in the
sidecar maps to exactly one flat output table, in sidecar table-declaration order.
There is no genre trichotomy, no sub-type split, and no membership, junction,
reference, or fact table. A kind's table carries the `STATE_AT_COLUMNS` prefix
(`record_id`, `created_sim_time`, `active`, `deactivated_at`), then `presentation_id`
when the kind carries it, then one `prop__<p>` per surviving property in sidecar
column-declaration order. Each output value is a state-at reconstruction at the
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
table snapshot-delivered at the window horizon, exactly as source's `change_delivery:
snapshot`); a full or sliced spec's is `'create'`.

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
source; both are overridable via `rename` (`name` for the table, a `columns` entry keyed
on the pre-default state-at column identity — `record_id`, `presentation_id`,
`created_sim_time`, `active`, `deactivated_at`, `prop__<p>` — for a column). Data columns
(`prop__<p>`, `presentation_id`) cast back from the state-at resident's codec VARCHAR
after-image to their declared sidecar types, so base delivers a typed table, not an
all-string one; `record_id` and `active` pass through verbatim. Lifecycle timestamps
render wallclock through the shared `render_anchor_timestamp_expr` when an anchor
resolves and stay raw sim-time `BIGINT` when it is `None` — base carries **no anchor
conditional of its own**, since the renderer already handles `anchor=None`. Ordering is
the state-at resident's `(created_sim_time, record_id)` over raw ns keys, never rendered
timestamps.

### Corrupter composition

A base export over a corrupted emit surfaces the corrupter's declared defects unchanged
and manufactures none (Principle #3), by construction — no corrupter-aware branch
exists. Base casts each data column back to its sidecar type, so totality rests on the
corrupter family's value transforms being **type-preserving**: a corrupted `history.value`
remains a valid instance of its column's declared type, the cast-back succeeds, and the
defect surfaces *in* the reconstructed value rather than dropping or erroring a row. The
guarantee is verified by a dedicated integration test, not asserted by inspection.

## Invariants

1. **Records-only flat grain.** Base emits exactly one flat table per surviving records
   kind and nothing else — no membership, junction, fact, or CDC table.
2. **State-at is the whole engine.** Every base table value is a state-at reconstruction
   at some horizon (tape's end, `T + 1`, or a window end); base writes no independent
   point-in-time path.
3. **One inclusive horizon per full export.** `slice_at: T` reflects every event with
   `sim_time ≤ T` and nothing after; the exclusive state-at horizon is `T + 1`.
   Current-state uses the structural end-of-tape entry point, never a horizon cleared
   against `history` alone.
4. **`slice_only` enforcement is omit-with-notice, carve-out honored.** Base inherits the
   export-wide invariant and chooses omission; the discriminator carve-out is honored;
   omission is column-projection-only and never suppresses a table.
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
| `entries_disjoint` (`BaseConfig`) | Two `rename` entries targeting the same `table` — base has one output table per kind, so `table` alone is the key |
| `mode_section_matches` (`ExportConfig`, `base` arm) | A `dimensional` or `source` section present under `mode: base`; the `base` section itself is optional (a bare `mode: base` is a valid full dump) |
| `base_slice_at_excludes_incremental` (`ExportConfig`) | A config setting both `base.slice_at` and an `incremental` block — a pinned instant and a window sequence are contradictory temporal selectors |

**Business rules.** Run at plan build against the open emit's sidecar, before any write;
each raises an `ExportError` subclass surfaced through the CLI's existing error funnel.

| Rule / Error | Checks |
|---|---|
| `BaseExcludeUnresolved` | Every `exclude.kinds` and `exclude.tables` entry resolves — both check the pre-`rename` prefix-stripped kind names (base's only presentation default at this stage), so the two resolve against the same known set |
| `BaseRenameUnresolved` | Every `rename` entry's `table` resolves to a surviving `records__<kind>`, and every `columns` key names a state-at column identity of that kind |
| `BaseRenameSliceOnly` | No `rename` `columns` key names a non-exempt `slice_only` column — the column is policy-omitted, so the rename is unsatisfiable |
| `BaseNameCollision` | All output table names are unique, and within each table all output column names are unique, after presentation defaults and `rename` |
| Reserved-name check (`ExportError`) | No resolved output table name is `_export_meta` / `_export_windows` / `*__rows`, and no output column name is `__valid_from_ns` or `last_mutation_sim_time` — enforced always-on via `exporters/reserved_names.py` |
| Single-branch guard (`derivations/guard.py`, cross-mode) | Exactly one branch |

`slice_only` omission itself is not a business-rule error — it is the
`slice-only-column-omitted` notice, emitted per surviving kind × omitted column before
any data is written.

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

## Boundaries

- **Membership / queue-state is neither emitted nor subsumed.** Base reads `records__*`
  + `history` only and emits no `membership__*` / junction / queue table. The Stage-5
  queue-state export reads `membership__*`, derives a different grain (wait time,
  FIFO / priority order), and composes the membership-state-at resident — orthogonal to
  base's records-only flat projection, and a separate future item.
- **No CDC / change-log shape.** Base never emits an `op` / `changed_at` column or a
  version-per-change row; that shape is source's and streaming's. Base delivers the
  merged result, not the change log.
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
| [`derivations.md`](derivations.md) | The state-at / end-of-tape residents base composes as its whole engine |
| [`source.md`](source.md) | Snapshot delivery (the same state-at composition), the presentation-name posture, and the `slice_only` omission shape base shares |
| [`slice-only.md`](slice-only.md) · [`notices.md`](notices.md) | The reused omission policy and the channel its notices flow through |
| [`playback.md`](playback.md) | Shaped state and the bridging theorem that make direct-horizon equivalent |
| [`anchor.md`](anchor.md) · [`incremental.md`](incremental.md) | The shared wallclock renderer and the window/cursor/fingerprint driver base wires into |
| [`corrupters.md`](corrupters.md) | The corrupt → base composition — a base export over a corrupted emit surfaces declared defects unchanged |
| [`writers.md`](writers.md) | The CSV / DuckDB adapters base shares with every mode |
| [`../../contract/base-format.md`](../../contract/base-format.md) | `temporal_class`, the MUST-NOT-present-as-of-T clause, and the records / `history` shapes |
| [`../CAPABILITIES.md`](../CAPABILITIES.md) | Base-mode feature inventory and status |
| [`README.md`](README.md) | Design index, package layout, staged roadmap |
