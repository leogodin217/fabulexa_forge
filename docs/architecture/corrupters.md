# Corrupters

**Status:** Implemented. Code is the contract — see
[`corrupters/`](../../src/fabulexa_forge/corrupters/),
[`config/`](../../src/fabulexa_forge/config/), and
[`tests/corrupters/`](../../tests/corrupters/). Public API:
[`corrupters/engine.py`](../../src/fabulexa_forge/corrupters/engine.py) (`corrupt_emit`).

The corrupter family reads a conformant base-layer emit and writes a realistically-broken
one: a structurally-conformant (C1–C5, C8) but semantically-broken (C6/C7/C9–C12) base
emit, plus `defects.json` — a deterministic, label-grade ground-truth artifact naming
every defect it injected. A third top-level YAML envelope, `CorruptConfig`, sibling of
`ExportConfig` / `StreamConfig`, declares a master `seed` and an ordered list of
`kind`-discriminated operations. Every operation shares one domain-agnostic grammar: a
**selector** (`target` — a five-way table selector naming one concrete table or a whole
class of tables, an optional row filter, and optional exact-or-pattern column entries)
and, for the sampling operations, a **distribution** (`amount` — the seeded quantity,
plus an optional magnitude `Distribution` when the operation perturbs a value) and an
optional **placement** (`placement` — a biased-draw axis weighting *which* units the
draw lands on: entity-scoped, temporally clustered, or cross-column-correlated
missing-not-at-random). Twelve operations compose
this grammar: two **family-A** cell-value mutations — `null_cells` (missing values) and
`mutate_cells` (eleven type-preserving wrong-value transforms: sentinel-disguised nulls,
identity mutations, truncation/precision/magnitude drift, mojibake/format dirt, an
intra-column resample, and an out-of-domain synthesis) — three **family-B** row-set
operations — `duplicate_rows` (exact, near-duplicate via numeric `jitter`, or
conflicting-duplicate via a `mutation` transform), `delete_rows` (remove sampled rows,
declaring the referential/pin/history wake the removal trips), and `insert_rows` (inject
phantom rows cloned from a donor under a fresh, plausible id) — `schema_drift`
(rename/retype/drop), two **family-D** referential
operations — `dangle_reference` (rewrite a sampled reference id to a guaranteed-absent
sentinel) and `mispoint_reference` (rewrite one to a wrong-but-real donor row, so the
reference stays resolvable but points at the wrong entity) — three **family-C**
operations over the long-form `history` table's temporal dimension — `freeze_series`
(suppress a change series' tail so its value sticks), `drop_events` (remove sampled
events — lost CDC messages), and `shift_sim_time` (skew, collide, or reorder event
timestamps) — and one **family-E** operation over the membership tables' SCD-2 interval
timeline — `distort_intervals` (overlap an adjacent interval pair, shrink an interval into
a coverage gap, or invert a closed interval's `joined_sim_time`/`left_sim_time`). Family C
introduces two selection units
beyond cell and row — the **event** (one `history` row) and the **series** (one `(kind,
record_id, property)` change timeline) — and declares each defect's impact by mirroring
C6's own predicate against the working state, so the label and the check cannot disagree.
`mutate_cells` reuses that same C6 mirror for its `history.value` mutations, extended with
one C12 predicate (an undeclared actor sub-type) — family A's first operation to mutate
`history.value` and the family's first to reach C12. Family E introduces a third selection
unit beyond cell and row — the **interval unit**, an adjacent pair or a single closed
interval within one **member timeline** — and is the only operation, besides
`dangle_reference`, whose defect can be a genuine C10 break rather than subconformance.

The engine and the manifest are one subsystem with a single internal seam: an operation
*declares* the defects it injects as `DefectRecord`s, and the engine *assembles* them into
`DefectManifest`, serialized to `defects.json` beside the corrupted `run.duckdb` and a
regenerated `base.json`. That seam — declare, then assemble — is internal, never a
cross-document contract, which is why both halves live in this one doc (§ Rationale).

```
run.duckdb + base.json (v4, conformant)
        │  open_emit  →  Emit (read-only)
        ▼
   require_single_branch  →  fork_path
        │
        ▼
   conformance.validate   →  refuse a non-conformant source (CorruptValidationError)
        │
        ▼
   CorruptState: {table_name → WorkingTable(spec, arrow)}   ← every source table,
        │                                                     materialized once (verbatim)
        │  for each operation, in order (seeded from (seed, op_index)):
        │    Target → resolve tables (lexicographic) → filter each CURRENT WorkingTable
        │             by where → canonical content order → pooled unit sequence
        │    Amount (± placement weights) → draw_sample / draw_weighted_sample
        │             → chosen unit indices (ascending pooled order)
        │    Corrupter.apply → mutates + replaces the touched WorkingTables;
        │                      returns an OperationOutcome carrying its DefectRecords
        ▼
   write_base_emit        →  run.duckdb + regenerated base.json (v4, structurally
        │                                        conformant, semantically broken)
   build_defect_manifest  →  canonicalised, id-assigned DefectManifest
   write_defect_manifest  →  defects.json (deterministic, label-grade ground truth)
```

```
out/
  run.duckdb      # corrupted emit — a valid base layer; any exporter runs on it
  base.json       # sidecar, C1–C5 conformant
  defects.json    # THE MANIFEST — our artifact, our schema; never part of base.json
```

---

## Surface

| Module | Owns |
|---|---|
| [`config/models.py`](../../src/fabulexa_forge/config/models.py) | `CorruptConfig` and its parts — `Target`, `Amount`, `Distribution`, the twelve operation models (`NullCells`, `DuplicateRows`, `DeleteRows`, `InsertRows`, `SchemaDrift`, `DangleReference`, `MispointReference`, `FreezeSeries`, `DropEvents`, `ShiftSimTime`, `MutateCells`, `DistortIntervals`), `ShiftSimTime`'s `kind`-discriminated `ShiftSpec` union (`ShiftOffset` / `ShiftCollide` / `ShiftSwap`), `MutateCells`'s `kind`-discriminated `MutationSpec` union (eleven members, also `DuplicateRows.mutation`'s vocabulary), and the `kind`-discriminated `CorruptOperation` union |
| [`config/loader.py`](../../src/fabulexa_forge/config/loader.py) | `load_corrupt_config` — the corrupter sibling of `load_export_config` / `load_stream_config`: YAML → validated `CorruptConfig`, hard-bound (corrupting is not a mode) |
| [`corrupters/validate.py`](../../src/fabulexa_forge/corrupters/validate.py) | `validate_corrupt_config` — the emit-dependent business rules, checked against a per-operation evolved-schema simulation |
| [`corrupters/selection.py`](../../src/fabulexa_forge/corrupters/selection.py) | The selection surface: `resolve_target_tables` (five-way table-selector resolution), `match_column_entries` (exact-or-pattern column matching), the canonical-content-order builder every operation and the base-emit writer share, `derive_row_weights` (per-placement row weights), and the two samplers — `draw_sample` (uniform) and `draw_weighted_sample` (placement-weighted) |
| [`corrupters/state.py`](../../src/fabulexa_forge/corrupters/state.py) | `WorkingTable`, `CorruptState`, `OperationOutcome`, `CorruptReport` — the in-flight working set and the per-operation report |
| [`corrupters/operations/__init__.py`](../../src/fabulexa_forge/corrupters/operations/__init__.py) | The `Corrupter` protocol and the `kind → Corrupter` dispatch registry |
| [`corrupters/operations/`](../../src/fabulexa_forge/corrupters/operations/) (`null_cells.py`, `duplicate_rows.py`, `delete_rows.py`, `insert_rows.py`, `schema_drift.py`, `dangle_reference.py`, `mispoint_reference.py`, `freeze_series.py`, `drop_events.py`, `shift_sim_time.py`, `mutate_cells.py`, `distort_intervals.py`) | One handler per operation kind |
| [`corrupters/operations/_impact.py`](../../src/fabulexa_forge/corrupters/operations/_impact.py) | Shared impact-declaration helpers the handlers read the working state through, including the family-C C6-mirror oracle (`resolve_c6_anchor`, `series_round_trip_fails`) `mispoint_reference` also reads for its records-reference impact rule, series enumeration (`enumerate_series_units`), and the C12 actor-sub-type predicate `mutate_cells` reads (`actor_subtype_undeclared`) |
| [`corrupters/engine.py`](../../src/fabulexa_forge/corrupters/engine.py) | `corrupt_emit` — the driver: guard single-branch, verify source conformance, validate the config, materialize the working set, thread the operations, write the output, assemble and write the manifest |
| [`corrupters/base_writer.py`](../../src/fabulexa_forge/corrupters/base_writer.py) | `write_base_emit` — the base-emit writer; the one place in the package that writes base-format knowledge, deliberately kept out of the schema-agnostic `writers/` |
| [`corrupters/manifest.py`](../../src/fabulexa_forge/corrupters/manifest.py) | The manifest value/model types — `ImpactCode`, `RowCategory`, `RowRef`, `Locator` (`ColumnLocator` / `RowLocator` / `CellLocator`), `DefectRecord`, `ManifestDefect`, `DefectSource`, `DefectCounts`, `DefectManifest` |
| [`corrupters/manifest_build.py`](../../src/fabulexa_forge/corrupters/manifest_build.py) | `build_defect_manifest`, `write_defect_manifest`, `derive_defect_id` — canonicalization, id assignment, and byte-deterministic serialization |
| [`corrupters/fingerprint.py`](../../src/fabulexa_forge/corrupters/fingerprint.py) | `fingerprint_config` — the config fingerprint |
| [`corrupters/defect_manifest.schema.json`](../../src/fabulexa_forge/corrupters/defect_manifest.schema.json) | The manifest's published JSON Schema, generated from `DefectManifest.model_json_schema(by_alias=True)`; a drift-guard test regenerates it and asserts byte-equality with the checked-in file |
| [`errors.py`](../../src/fabulexa_forge/errors.py) | `CorruptError(ExporterError)` / `CorruptValidationError(CorruptError)` — a sibling family under the CLI's `(ReaderError, ExporterError)` funnel |
| [`cli.py`](../../src/fabulexa_forge/cli.py) | `cmd_corrupt` — the `fabulexa-forge corrupt <emit_dir> --config <corrupt.yaml> --out <out_dir>` verb |

## Boundary

- **Input.** An open `Emit` (trunk-only — sole branch) and a validated `CorruptConfig`.
- **Output.** `run.duckdb` + a regenerated `base.json` (a structurally-conformant v4 base
  emit — any exporter can run on it downstream) plus `defects.json`, into `out_dir`
  (created if absent). A corrupt run never clobbers an existing emit: it refuses if
  `out_dir` already holds a `run.duckdb` or `base.json`. The manifest is always written;
  there is no flag to suppress it.
- **Reader-first; one faithful read.** The engine's sole read is `Emit.query_arrow`,
  materializing every source table once, verbatim, into `CorruptState`. Every later read
  is over that working set, never the source — the reader's relation builders emit SQL
  over the immutable `run.duckdb` and are blind to in-flight mutations, so they are
  deliberately not used to fetch operation populations. The engine reuses the derivations
  layer's `require_single_branch` guard and reads table category, column type, identity
  columns, and reference targets from the typed `Sidecar`.
- **The Principle #3 exception.** A corrupter is the one place in the package that
  fabricates a value: a `dangle_reference` sentinel id guaranteed absent from its target
  table, a `mutate_cells` `sentinel` mutation's author-specified literal (Principle #7
  keeps that literal author-chosen, never a built-in default), and an `insert_rows`
  phantom `record_id` — deterministically derived from a real donor id by adjacent-
  character transposition and guaranteed absent from the kind's id universe (§
  `insert_rows`). Every other output value traces to a base-layer value — nulled,
  perturbed (`jitter`), transformed in place (`mutate_cells`'s other ten kinds each
  transform, or draw a real value from, the stored data), cloned or intra-column
  resampled (`insert_rows`' non-id payload), or, for `mispoint_reference`, a real donor id
  drawn from the same target table — only the three invented values above break the
  tracing rule, and only on purpose.
- **`defects.json` is this package's own artifact.** Its schema is ours, checked in at
  `corrupters/defect_manifest.schema.json`. It is never a `base.json` top-level field and
  never enters `contract/` — either would redefine the external base-format contract.
  `open_emit` reads only `run.duckdb` + `base.json` by name, so an extra file in the emit
  directory is invisible to it.
- **Forbidden.** No dependency on the bundle's producer; the vendored `contract/` is the
  only coupling. `CorruptConfig` is a sibling envelope of `ExportConfig` / `StreamConfig`,
  not a mode of either — no discriminator is extended. The generic `writers/` (CSV /
  DuckDB) stay schema-agnostic; base-emit serialization is corrupter-owned because it
  carries sidecar knowledge writers otherwise never touch.

## Semantics

### The selector and distribution grammar

Every operation targets a `Target` — a **table selector** (exactly one of five forms; §
Table-selector resolution), an optional `where` equality-row filter, and an optional
`columns` list of exact names or fnmatch patterns (§ Column entries) — and, for the three
sampling operations, an `Amount` (`rate` or `count`) plus an optional `placement` biasing
which units the draw lands on (§ Placement: weights over units). `where` is evaluated by
registering the current working Arrow as an ephemeral DuckDB relation and running the
predicate there, reusing `render_typed_literal` (the same typed-literal coercion oracle
the dimensional exporter uses, currently in `exporters/dimensional/columns.py`): each
`{column: value}` becomes `<column> = <render_typed_literal(value, column_type)>`,
conjoined with `AND`, so DuckDB performs the cast and typed equality exactly as it does
everywhere else — never a second, pyarrow-native equality path that could silently
disagree on `DOUBLE` / `DECIMAL` / `BOOLEAN` / VARCHAR-quoting and shift the matched set.
Absent `where` selects every row on the sole `fork_path`; `where` keys are always exact
column names, never patterns. Literals are rendered per resolved table against that
table's *current* column type — a same-named column may carry different types across a
resolved set, so one `where` value can be typed differently table to table, and a literal
unrepresentable in some table's type fails inside the shared DuckDB cast at apply time
(the same failure domain as `correlated.value`). `schema_drift` reuses the same DuckDB
`CAST` oracle for its retype path.

Two different "units" appear, one per phase, and they are not the same thing:

- The **selection unit** is what `amount` samples — it depends on the operation: one cell
  (`null_cells`, `mutate_cells`), one row (`duplicate_rows`), one reference cell
  (`dangle_reference`), one adjacent interval pair or one closed interval row
  (`distort_intervals`, mode-dependent; § Member timelines and adjacency).
  `schema_drift` names its columns exactly and carries no `amount`.
- The **declared defect** is one atomic act of corruption — one `DefectRecord`. An
  operation that injects three duplicate copies of a row emits three records (same
  location, distinct ids); one that nulls one cell emits one.

Most operations keep a strict 1:1 between a selected unit and its declared defects; family C
is the exception — a `freeze_series` unit (one series) can yield several removed-row
defects, and a `shift_sim_time` swap unit yields two (§ What freeze_series, drop_events, and
shift_sim_time do). `distort_intervals` keeps the strict 1:1 (§ Member timelines and
adjacency).

### Table-selector resolution

Exactly one selector field of `Target` is set (parse time). Resolution
(`resolve_target_tables`) is a pure function of the selector and sidecar table metadata
(`name`, `category`, `record_kind`) — reader-only, no data read. The resolved set is
ordered **lexicographically ascending by table name** — a pure function of the resolved
name set, independent of sidecar array order — and that order is the canonical table
order everywhere below: pooling, unit enumeration, and defect emission. The table set is
static across a run (no operation adds, drops, or renames a *table*), so resolution is
position-independent; column-level matching is not (§ Validation Rules).

| Selector | Resolves to | Zero-match handling |
|---|---|---|
| `table: T` | the one table `T` | `T` absent from the sidecar → validate-time error |
| `tables: [T1, T2, …]` | exactly the listed tables | any listed name absent → validate-time error naming it |
| `glob: G` | every sidecar table whose name matches `G` (`fnmatch.fnmatchcase` — case-sensitive on every platform) | no match → validate-time error |
| `category: C` | every sidecar table with `category == C` (`fixed` / `records` / `membership`) | no table of that category in the emit → validate-time error |
| `record_kind: K` | every sidecar table with `record_kind == K` (its `records__K` and every `membership__K__*`) | no table of that kind → validate-time error |

A selector resolving to zero tables is a misconfiguration, not an empty population: the
config asks to corrupt something the emit does not have. This is deliberately distinct
from the data-dependent zero-*row* population, which is a no-op (§ Rationale).
`schema_drift` takes the concrete `table` form only — its rename/retype/drop maps name
exact columns of one table and do not generalize to a class.

### Column entries: exact names and patterns

Every `target.columns` entry is either an exact column name or an fnmatch pattern — an
entry containing `*`, `?`, or `[` is a pattern; anything else is exact — and both resolve
through one matching rule, `fnmatch.fnmatchcase` (the same case-sensitive form the table
`glob` uses, never the platform-folding `fnmatch.fnmatch`). Per resolved table, an entry
matches against the operation's **eligible** columns of that table's current working
schema — null-eligible value columns for `null_cells`, reference columns for
`dangle_reference`, numeric payload columns for `jitter`, mutation-eligible columns for
`mutate_cells` (the classes `NullableColumns` / `ReferenceColumns` / `JitterColumnsNumeric`
/ `MutableColumns` define; § Validation Rules, § mutate_cells vocabulary and eligibility).
A table's resolved column list is: entries in list order, each entry's matches in
working-schema column order, deduplicated at first match (`match_column_entries`).

| Condition | Result |
|---|---|
| An entry matches ≥ 1 eligible column in ≥ 1 resolved table | valid; each table contributes its own matches |
| An entry matches zero eligible columns in **every** resolved table | validate-time error naming the entry — a dead entry is a misconfiguration |
| An entry matches in some resolved tables, not others | valid; non-matching tables contribute no units for that entry |
| A pattern matches an existing but ineligible column | not a match — patterns see only eligible columns |
| `duplicate_rows` near mode: no entry matches in some resolved table | that table contributes zero row units — a zero-perturbation copy is not a near-duplicate (§ The pooled population) |
| A single concrete `table` with exact entries | the resolved column list is exactly the listed columns |

`schema_drift`'s `rename_to` / `retype_to` / `drop` keys, every `target.where` key, and
every placement `column` (`correlated.column`, `clustered_temporal.column`) are exact
names — no patterns. Only `target.columns` entries take patterns.

### mutate_cells vocabulary and eligibility

- **Mutation** — one type-preserving transform of one stored cell value, named by
  `mutation.kind` (eleven kinds; § What mutate_cells does).
- **Eligible column** — a column the family-wide name class admits *and* the mutation
  kind's type gate admits (the matrices below). Eligibility is evaluated per resolved
  table against the current working schema — evolved by earlier `schema_drift` — exactly
  as `NullableColumns` and `ReferenceColumns` are.
- **Donor pool** (`resample` only) — the distinct non-NULL values of the mutated cell's
  own column, read from the state the operation began with, narrowed to the sole
  `fork_path` but never to `target.where` (the same whole-timeline stance a
  `freeze_series` / `shift_sim_time` swap partner takes), excluding the cell's current
  value, ordered ascending in DuckDB's total order for the column's type. For
  `history.value` the pool narrows to rows of the same `(kind, property)` — the
  per-property value population is the meaningful "column" there; pooling across
  properties would draw a name into a weight series.
- **No-mutation unit** — a selected unit whose transform leaves the stored value
  unchanged, or cannot apply. It emits no defect and is not counted in `units_affected`;
  its RNG draws are still consumed (the shipped unchanged-unit stance; § What each
  operation breaks, and the impact it declares).

The family-wide **name class** (`MutableColumns`), per resolved table:

| Table category | Eligible columns |
|---|---|
| `records` | `prop__*` with `references` unset; `presentation_id` |
| `membership` | `elem__*` |
| `history` (fixed) | `value` only |

Per mutation kind, the **type gate** narrows the name class by the column's current
declared type:

| `mutation.kind` | Eligible types | Additional gate |
|---|---|---|
| `sentinel` | any | the literal must cast into the column's type (apply-time cast oracle) |
| `typo` | `VARCHAR`, `BIGINT` | — |
| `case` | `VARCHAR` | — |
| `whitespace` | `VARCHAR` | — |
| `truncate` | `VARCHAR` | — |
| `precision_drop` | `DOUBLE` | — |
| `scale` | `BIGINT`, `DOUBLE` | — |
| `mojibake` | `VARCHAR` | — |
| `format_dirt` | `VARCHAR` | — |
| `resample` | any | — |
| `out_of_domain` | `VARCHAR` | records `prop__<p>` only, where the sidecar declares `enum_domains[kind][p]`; `history.value`, `presentation_id`, and `elem__*` are ineligible |

A type gate matches the column's declared type in the current working schema by exact
DuckDB type identity — no type families, no coercion. A deviant-typed column is simply
ineligible for the typed kinds, and a config entry addressing it surfaces as the
`MutableColumns` validation error naming the mutation kind, never a silent skip. `any`
means any declared type, `BOOLEAN` and `BLOB` included: `sentinel` leans on the cast
oracle (an uncastable literal fails loudly), and `resample`'s donor-pool ordering is
bytewise for `BLOB` and `NaN`-greatest for `DOUBLE` — DuckDB's own total order, never a
Python-side sort that would invent a different one.

The enum-domain sub-type discriminator (`prop__<kind>_type`) **is** eligible — a value
mutation preserves structure (C5 categorization and the column's type are untouched), and
mutating it is exactly the C12 / out-of-domain teaching case. This is deliberately looser
than `schema_drift`, which excludes the discriminator because catalog-level drift there
would orphan the domain declaration.

### Selection is faithful; sampling is deterministic

The one faithful read happens once, at materialization. From then on the working set *is*
the truth every operation sees: the selector resolves each resolved table's population
over its **current** `WorkingTable` — the evolving Arrow and the evolving schema
(`WorkingTable.spec`) — narrowed to the sole `fork_path` and `target.where`. A
`null_cells` after a `duplicate_rows` selects among the duplicated rows because it reads
the mutated table; a column renamed by an earlier `schema_drift` resolves against the
current schema, not the source sidecar.

The working Arrow carries no inherent order (DuckDB scan order is not byte-stable —
`reader.md` § Determinism), so the selector imposes a **canonical content order** as a
pure function of row content: order by every column ascending, NULLS FIRST. Selection is
therefore deterministic regardless of scan order; byte-identical rows (legal duplicate
`history` ticks, membership multiplicity ≥ 2) tie, and ties are interchangeable —
mutating one such row versus another yields the identical table multiset.

Each operation draws from its own RNG stream seeded from `(seed, operation_index)` via a
stable combiner (fed to `random.Random`, never Python's per-process-salted `hash()`).
Within an operation the stream is consumed in a fixed order: **(1)** placement setup
draws — the `entity_scoped` subset or the `clustered_temporal` centers (one `rng.sample`
over the sorted universe; `correlated` and an absent `placement` draw nothing here);
**(2)** the unit draw — the uniform `rng.sample` without `placement`, or one
`rng.random()` per pooled unit for the weighted keys (§ The weighted draw); **(3)** mode draws — near-duplicate `jitter`
deltas, one per selected row in pooled canonical order per that row's table's resolved
columns in resolved-column order; and, for family C, one draw per selected unit in
ascending selected-unit order: `freeze_series` with `cut: random` draws one uniform
kept-prefix length per selected series (`rng.randrange(1, N)`, the `[1, N−1]` range);
`shift_sim_time`'s `offset` mode draws one delta per selected event (the same delta-draw
primitive jitter uses); `cut: after_first`, `collide`, and `swap` draw nothing at this
step; and `mutate_cells` draws one `rng.random()` per selected unit, in ascending
selected-unit order, for its three seeded kinds — `typo` (edit position), `resample`
(donor index), `out_of_domain` (candidate rotation) — the other eight kinds draw nothing.
`delete_rows` draws nothing at this step (it has no mode); `insert_rows` draws, per
phantom in ascending selected-unit order, one `rng.random()` for the id-derivation
rotation then one per resolved resample column in resolved-column order; `duplicate_rows`'
`mutation` mode draws, per selected row in pooled canonical order per resolved column in
resolved-column order, one `rng.random()` for the three seeded kinds (`typo`, `resample`,
`out_of_domain`) — the other eight kinds draw nothing, the same `mutate_cells`
discipline. `distort_intervals` draws nothing at this step for any of its three
modes — every rewrite target is a pure function of the operation-start working state,
the same knobless discipline `collide` and `swap` follow. Adding or reordering operations
changes later indices and is a different config — the config is the identity.

| `amount` | Units chosen from a pooled population of size N |
|---|---|
| `rate: r` (0 < r ≤ 1) | exactly `floor(r · N)`, drawn without replacement in canonical order |
| `count: k` (k ≥ 1) | exactly `min(k, N)`, drawn without replacement |
| N = 0 | zero units; the operation is a no-op and reports `units_affected: 0` |
| `count: k`, k > N | all N units — not an error; the population is the ceiling |

### The pooled population and unit enumeration

A multi-table operation resolves one population per resolved table (the current working
Arrow, narrowed to the sole `fork_path` and `target.where`, in canonical content order),
then concatenates them in canonical table order into one **pooled** unit sequence:

- **Cell units** (`null_cells`, `dangle_reference`, `mispoint_reference`, `mutate_cells`): for each table in
  canonical table order → each row in canonical content order → each resolved column in
  resolved-column order.
- **Row units** (`duplicate_rows`): for each table in canonical table order → each row in
  canonical content order. In near mode (`jitter` present), a resolved table whose
  `columns` entries match zero of its eligible columns contributes zero rows — its rows
  could only yield zero-perturbation copies, which are not near-duplicates. Exact mode
  carries no `columns`; every resolved table contributes all its rows.
- **Event units** (`drop_events`; every `shift_sim_time` mode): each `history` row
  narrowed to `fork_path` + `where`, in canonical content order — family C's
  `HistoryOnlyTarget` rule (§ Validation Rules) confines these operations to `history`
  alone, so this is a single-table population.
- **Series units** (`freeze_series`): the distinct `(kind, record_id, property)`
  change-timelines among the narrowed rows, subject to a ≥ 2-row timeline filter (§
  Family-C vocabulary and populations) — one of the pool's two units that is not one row.
- **Interval units** (`distort_intervals`): resolved over member timelines within each
  membership table (fork-narrowed; never narrowed by `target.where`, which decides only
  unit *membership* — § Member timelines and adjacency) — an adjacent interval pair,
  keyed on its earlier row, for `overlap`; one closed interval row for `gap` and
  `left_before_join`.

`amount` applies to the pooled population (the table above): defect *volume* is a
property of the whole target, not of any one table — a rate names a fraction of the
class, and how it lands across tables follows from the draw (uniform or placed), never
from a per-table quota. For `freeze_series`, the pool is series, so N is the series
count, not the row count; for `distort_intervals`, N is the mode's interval-unit count,
not the row count.

| Condition | Result |
|---|---|
| a `where` key is absent from some resolved tables | those tables contribute zero units — no row can satisfy the filter; the key must exist in ≥ 1 resolved table (§ Validation Rules) |
| `where` matches zero rows in some resolved tables | those tables contribute zero units; the draw runs over the rest |
| `where` matches zero rows in every resolved table | a pooled population of zero → no-op, `units_affected: 0` |
| one resolved table (any selector form) | the pool is that table's population |

### Family-C vocabulary and populations

The three family-C operations (`freeze_series`, `drop_events`, `shift_sim_time`) target
`history` only (`HistoryOnlyTarget`, § Validation Rules).

- **Series** — the events sharing one `(kind, record_id, property)` on the sole
  `fork_path` (single-branch stage: `fork_path` is constant).
- **Timeline** — all of a series' rows in the current working `history` table, narrowed
  to the sole `fork_path` (never narrowed by `target.where`), ordered by `sim_time`
  ascending, ties by canonical content order; remaining ties are byte-identical rows and
  interchangeable.
- **Tick** — one `sim_time` value within a series. Distinct series may share ticks
  freely; within a series, duplicate ticks are legal data (a shipped `duplicate_rows`
  defect, or a `shift_sim_time` `collide` result).
- **Predecessor tick** of an event — the greatest tick strictly less than the event's
  tick in its series' timeline; rows at a series' minimum tick have none.
- **C6 view** of a series — its timeline rows with `sim_time ≤ slice_at` (the sole
  branch's sidecar `slice_at`).
- **Anchor** of a series — the `(sim_time, value)` pair C6 itself selects: rank 1 under
  `ORDER BY sim_time DESC, value DESC` over the C6 view, or none when the view is empty.
  This mirrors `_check_c6`'s deterministic tie-break exactly. Within a series the other
  four history columns are constant, so the pair is a row's full distinguishing content —
  the anchor is unique even when byte-identical duplicates tie completely, and every row
  carrying the pair is the anchor: anchor identity is content, never position.

| Operation | Selection unit | Population |
|---|---|---|
| `drop_events` | event row | working `history` rows narrowed to `fork_path` + `where`, canonical content order |
| `shift_sim_time`, `shift.kind: offset` | event row | same as `drop_events` |
| `shift_sim_time`, `shift.kind: collide` / `swap` | event row | same, minus rows with no predecessor tick |
| `freeze_series` | series | distinct `(kind, record_id, property)` triples among the narrowed rows whose timeline has ≥ 2 rows, ordered lexicographically ascending (`enumerate_series_units`) |

For `freeze_series`, `where` decides series-universe *membership* (a series qualifies
when at least one of its rows survives the narrowing), but a selected freeze acts on the
whole timeline — `where` keys on series-constant columns (`kind`, `property`,
`record_id`) behave intuitively.

### Member timelines and adjacency

A **member timeline** is the unit of interval adjacency for `distort_intervals`, resolved
per working membership table on the sole `fork_path`:

- **Identity.** Rows sharing `(record_id, every element-field value)` — the element-field
  columns are all columns other than `fork_path`, `record_id`, `joined_sim_time`,
  `left_sim_time` — compared by typed equality on the current working values, NULL
  grouping with NULL (the same NULLS-first stance canonical content order takes). One
  timeline is one member's presence record in one collection.
- **Order.** Within a timeline, rows order by `joined_sim_time` ascending, ties by
  canonical content order; remaining ties are byte-identical rows (legal multiplicity ≥ 2)
  and interchangeable.
- **Adjacency.** An **adjacent pair** is two consecutive rows (A, B) of one timeline — A
  the earlier row, B its immediate successor. A row is the earlier row of at most one
  pair, so no two pair units rewrite the same cell.
- **Whole-timeline resolution.** Timelines and adjacency are resolved over the full
  working table narrowed only to `fork_path` — never by `target.where`. `where` decides
  unit *membership*: a pair qualifies when its earlier row A survives the narrowing; a
  single-row unit qualifies when its own row does — the same whole-timeline stance
  `freeze_series` takes for series (§ Family-C vocabulary and populations).

Because timeline identity reads working values, earlier operations compose by
working-set truth: a `dangle_reference`d or `mutate_cells`-mutated member value places
its row in the timeline that value now defines.

### distort_intervals: modes, populations, and rewrites

`distort_intervals` rewrites sampled membership intervals' timing boundaries in one of
three **knobless** modes — no `Distribution`, no magnitude field; every rewrite target
derives deterministically from the data itself, the same stance `shift_sim_time`'s
`collide` and `swap` take. The mechanism is **boundary perturbation, cardinality-
preserving**: it rewrites `left_sim_time` (and, for `left_before_join`, `joined_sim_time`)
of existing rows; no row is added, removed, or split, so every table's row count is
preserved and structural conformance holds by construction exactly as for every
cell-rewriting operation.

`slice_at` is the sole branch's sidecar `slice_at`. All resolutions — timelines,
adjacency, successor boundaries, population filters — read the state the operation began
with, and all rewrites apply as one simultaneous set (the family-wide simultaneous-
rewrite stance; § Operations apply in order over a shared working set). Per resolved
table the units enumerate in timeline order (timelines by their first row's canonical
content order, units within a timeline by position); tables pool in canonical table order
(§ The pooled population and unit enumeration).

| Mode | Selection unit | Population (per resolved table, after `where`) | Rewrite (operation-start values) |
|---|---|---|---|
| `overlap` | adjacent pair (A, B), keyed on A | pairs where `A.left_sim_time` is non-NULL and `B_end − B.joined_sim_time ≥ 2`, with `B_end = B.left_sim_time` when non-NULL else `slice_at` | `A.left_sim_time ← B.joined_sim_time + floor((B_end − B.joined_sim_time) / 2)` |
| `gap` | one interval row | rows where `left_sim_time` is non-NULL and `left_sim_time − joined_sim_time ≥ 2` | `left_sim_time ← joined_sim_time + floor((left_sim_time − joined_sim_time) / 2)` |
| `left_before_join` | one interval row | rows where `left_sim_time` is non-NULL and `left_sim_time > joined_sim_time` | swap `joined_sim_time` and `left_sim_time` |

Each rewrite holds properties load-bearing for C10:

| Property | Why it holds |
|---|---|
| `overlap` post-state overlaps: `A.left' > B.joined` | the span filter guarantees `floor(span/2) ≥ 1` |
| `overlap` keeps C10 green on A: `A.left' ≥ A.joined` | timeline order gives `B.joined ≥ A.joined`, and `A.left' > B.joined` |
| `overlap` never lands past the slice: `A.left' ≤ slice_at` | `A.left' < B_end ≤ slice_at` (non-NULL boundaries are pre-slice — the inherited guarantee below; the NULL fallback is `slice_at` itself) |
| `gap` strictly shrinks: `joined ≤ left' < left` | duration `d ≥ 2` gives `1 ≤ floor(d/2) ≤ d − 1` |
| `gap` keeps C10 green | `left' ≥ joined` by the same bound |
| `left_before_join` violates C10 strictly: `left' < joined'` | the population requires `left > joined` strictly; the swap inverts it |
| no two units rewrite one cell | pair units key on distinct earlier rows; single-row units are distinct rows |

`B_end ≤ slice_at` is an **inherited producer guarantee**, not a C1–C12 predicate: the
producer constructs membership intervals from a slice-bounded series, so every non-NULL
`joined_sim_time` / `left_sim_time` is ≤ the branch's `slice_at` — for `left_sim_time` the
contract's NULL biconditional (non-NULL *means* the member left before the slice boundary)
entails it outright. No conformance check enforces the bound on disk; the design leans on
it the way exporters lean on the other inherited guarantees (`bundle.md`). It survives any
corruption chain inductively: no other shipped operation writes membership timing values,
and all three `distort_intervals` modes rewrite both timing columns to values ≤ `slice_at`
(the table above).

An open interval (`left_sim_time` NULL) is never a mutated row in any mode — NULL timing
is `null_cells`' defect, not a distortion — though it may serve as an `overlap` pair's
successor B (its boundary read as `slice_at`).

A selected unit whose rewrite changes nothing — possible only for `overlap`, when the
earlier interval's `left_sim_time` already equals the rewrite target — emits no defect
and is not counted in `units_affected`, the family-wide no-mutation rule (the cell-rewrite
stance; § What each operation breaks, and the impact it declares). `gap` and
`left_before_join` cannot produce one, since their population filters guarantee a strict
change. The unit draw is unaffected; RNG consumption stays a fixed function of population
size and selected count.

### Placement: weights over units

`placement` (optional; `null_cells`, `duplicate_rows`, `dangle_reference`,
`mispoint_reference`, `mutate_cells`, `distort_intervals` only — `schema_drift` samples
nothing) derives one
weight per pooled unit. It is a
`kind`-discriminated union of three models, deliberately a distinct family from the
jitter `Distribution` — the two axes are orthogonal: `Distribution` shapes a
perturbation's magnitude, `placement` shapes where the draw lands. Weights are derived at
**row** granularity — every placement kind is a property of the unit's row — and a cell
unit inherits its row's weight. All value comparisons reuse the `render_typed_literal`
typed-equality oracle; all seeded choices draw from the operation's one RNG stream in a
fixed order (§ Selection is faithful; sampling is deterministic).

A placement `column` must exist in **≥ 1 resolved table** — zero everywhere is a dead
config, a validate-time error — and in a table that lacks it, every row takes the kind's
NULL-value weight: the absent column behaves as an all-NULL column. This is the same
lenient-across-the-set, dead-config-backstop stance as `target.columns` entries, so a
class profile stays portable when only some kinds carry the condition or timestamp
column.

Match flags and column values are read back through the same ephemeral-DuckDB-relation
mechanism `where` uses, with one addition for alignment: the canonically-ordered
population Arrow is registered with an explicit 0-based row index, the per-row flag/value
is projected, and the result is ordered by that index — never by DuckDB result order,
which is not stable (`reader.md` § Determinism).

**`entity_scoped`** — concentrate defects on a seeded subset of entities. The entity
universe is the set of distinct `record_id` values across the pooled population's rows,
ordered lexicographically ascending. A subset is drawn from it using the
`entities: Amount` quantity (the same `floor(rate · E)` / `min(count, E)` rules). Entity
identity is the row's `record_id` value — contract-pinned (the record itself for records
and history rows, the owner for membership rows), not configurable.

| Row | Weight |
|---|---|
| `record_id` in the drawn subset | 1 |
| `record_id` not in the subset | 0 |

**`clustered_temporal`** — defects as contiguous sim-time neighborhoods. The center
universe is the set of distinct non-NULL values of `column` (a `BIGINT` sim-time-valued
column, author-named: `sim_time`, `joined_sim_time`, `deactivated_at`, …) across the
pooled population's rows of the tables that carry the column, sorted ascending.
`min(clusters, |universe|)` centers are drawn without replacement. `width` is the window
half-width in the column's own units (ns offsets).

| Row | Weight |
|---|---|
| `column` value within `width` of any drawn center (`\|v − c\| ≤ width`) | 1 |
| `column` value outside every window | 0 |
| `column` value NULL | 0 |
| `column` absent from the row's table | 0 |

**`correlated`** — missing-not-at-random: defect likelihood conditioned on another
column. No seeded setup. `value` is typed by the same oracle as `where` and inherits its
apply-time cast behavior — a literal unrepresentable in the column's type fails inside
the shared DuckDB cast, not in any placement-specific check.

| Row | Weight |
|---|---|
| `column = value` (typed equality, same oracle as `where`) | `weight` |
| `column ≠ value`, or `column` NULL | 1 |
| `column` absent from the row's table | 1 |

`weight` is any positive float — above 1 concentrates defects on matching rows, below 1
repels them. This is the deliberate MNAR shape: a *weighted draw with an exact total*,
not a per-unit Bernoulli — `amount` keeps its exactness invariant, one sampling mechanism
serves all three kinds, and the missingness is still genuinely conditional on the column
(§ Rationale; per-unit independent-probability quantities are excluded, § Boundaries).

A **series unit** (`freeze_series`) takes the weight of its **terminal row** — rank 1
under `ORDER BY sim_time DESC, value DESC` over its full timeline (the same rule as the
anchor, without the pre-slice gate). Placement weight derivation then runs over the set
of terminal rows, one per series in the universe: the `entity_scoped` entity universe is
the distinct `record_id` values among them, and `clustered_temporal` centers draw from
their `sim_time` values. The terminal row is where a freeze visibly bites, so this one
rule keeps every placement kind well-defined for a multi-row unit.

An `overlap` **pair unit** (`distort_intervals`) takes the weight of its **earlier row
A** — the row the rewrite bites — mirroring the series-unit terminal-row rule; a
single-row `gap` / `left_before_join` unit takes its own row's weight. The placement
universes follow the same rule: `entity_scoped`'s entity set and `clustered_temporal`'s
center set derive from each unit's weight row — a pair's A row, a single-row unit's own
row — exactly as the series-unit universes derive from terminal rows. Both rows of a
pair share `record_id` (one timeline, one owner), so `entity_scoped` is unambiguous;
`clustered_temporal` typically centers on `joined_sim_time` (`BIGINT` in every membership
table).

### The weighted draw

With `placement` present, the unit draw is seeded weighted sampling without replacement
over the pooled units (Efraimidis–Spirakis): one uniform draw `u_i = rng.random()` per
unit in pooled order — every unit, zero-weight included, so RNG consumption is a function
of the pooled population size alone — key `u_i^(1/w_i)` for positive-weight units, select
the `k` largest keys, ties broken by lower pooled index. The chosen indices are returned
in ascending pooled-index order. Zero-weight units are excluded from the draw but
**counted in the population size** — `amount` names a fraction/count of the population;
placement decides where it can land.

| Condition | Result |
|---|---|
| `k` ≤ positive-weight unit count | exactly `k` units, weighted |
| `k` > positive-weight unit count | all positive-weight units — the *drawable* population is the ceiling |
| every unit has weight 0 (e.g. an all-NULL-or-absent temporal column, an empty entity subset from `floor`) | zero units → no-op, `units_affected: 0` |
| `placement` absent | the uniform `rng.sample` draw (`draw_sample`) |

### Operations apply in order over a shared working set

`corrupt_emit` threads the operations through the `CorruptState` in list order; each
operation sees the prior operations' output.

| Situation | Result |
|---|---|
| Two operations target the same table | applied in list order; the second sees the first's mutations |
| An operation's selector resolves to zero tables, or a `tables` entry names a table absent from the sidecar | business-rule failure at validate time, before any write |
| An operation names a column absent from the table's schema as of its position (e.g. renamed away by an earlier `schema_drift`) | business-rule failure at validate time — `validate_corrupt_config` simulates the catalog evolution across operations, so the miss is caught before any read or write |
| `target.where` matches zero rows | empty population → no-op for that operation |
| `duplicate_rows` exact on `history` before a family-C operation | duplicate ticks are legal timeline rows; ties resolve by canonical content order (byte-identical → interchangeable) |
| An earlier `shift_sim_time` `collide` created a differing-value tick pair, a later operation makes that tick the anchor | the round-trip evaluation resolves the pair via `value DESC`, exactly as `validate` will |
| An earlier `schema_drift` renamed/retyped/dropped a series' records `prop__` column | the round-trip evaluation reads the current working schema — a skipped series cannot fail, so the family-C defect declares `beyond-c1-c12` |
| An earlier `null_cells` nulled a series' records cell | the series already fails; family-C anchor participants over-declare soundly, non-participants declare `beyond-c1-c12` |
| A family-C operation before `duplicate_rows` / `null_cells` on `history` | later operations select over the mutated timeline (the shipped shared-working-set rule) |
| Two family-C operations on the same series | the second resolves timelines, predecessors, and anchors against the first's output |
| An earlier `schema_drift` renamed/retyped a column `mutate_cells` later targets | eligibility and the type gate evaluate against the evolved schema; a renamed column is addressable only by its new name, a retyped column moves across type gates |
| An earlier `null_cells` nulled a cell `mutate_cells` later selects | the cell is a no-mutation unit — a mutation transforms a present value, NULL stays NULL |
| A later `null_cells` / `dangle_reference` / family-C operation over a `mutate_cells`-mutated cell | sees the mutated value as working-set truth; a mutated `history.value` participates in later canonical orders and anchor resolutions |
| `duplicate_rows` after `mutate_cells` | copies carry the mutated values — composition, not conflict |
| An earlier `null_cells` nulled a `mispoint_reference` target's id or membership `kind` partner | filtered out — the same population-filter stance `dangle_reference` takes |
| An earlier `dangle_reference` dangled a cell `mispoint_reference` later selects | eligible: the sentinel is non-NULL and, being absent from the target table, is trivially excluded from the donor pool — the mis-point heals the dangle |
| A later `dangle_reference` re-dangles a `mispoint_reference`-mispointed cell | declares its own `C10`; the earlier `beyond-c1-c12` remains a sound over-declaration |
| `duplicate_rows` on a `mispoint_reference` target's referencing table | copies join the pooled population like any other operation |
| `duplicate_rows` on a `mispoint_reference` target's donor table | the distinct-id universe and donor creation times are unchanged — copies carry the same `record_id` and `created_sim_time` as their source row |
| A family-C operation rewrote/removed a series' events before a `mispoint_reference` on the same records reference | the write anchor and the C6 mirror oracle both read the mutated working `history` |
| `delete_rows` then any sampling operation on the same table | later populations exclude the removed rows |
| `duplicate_rows` then `delete_rows` on the same table | copies are ordinary population rows; deleting all copies of a pinned id declares `C9` on each such deletion, deleting only some declares nothing for that id (the post-op survival rule, § `delete_rows`) and may heal the duplicate's `C9` — a sound over-declaration |
| `delete_rows` removes a records row an earlier `null_cells` / `mutate_cells` declared `C6` against | the series is unresolved — C6 still fails, via the missing row rather than the value; the delete's own `C6` clause declares it, and the earlier `C6` stands — joint, sound declarations |
| `delete_rows` removes the membership row an earlier `dangle_reference` dangled | C10 no longer fails there — the earlier `C10` stands as a sound over-declaration |
| `delete_rows` removes every records row of a `mispoint_reference` donor's id | the mis-pointed cell now dangles: a membership mis-point is a surviving non-NULL member pair resolving to the deleted id, so the delete's own `C10` clause declares the break; a records-prop mis-point dangles silently (subconformance), and any `C6` was already declared by the mis-point itself, whose round-trip verdict a donor deletion cannot change |
| `delete_rows` empties a table | the table remains in the catalog with zero rows; later operations see an empty population (no-op). Emptying every table (row-set operations composed with family-C erasure) would leave no row carrying the branch's `fork_path` and fail C8 — the total-erasure guard refuses to write that output (§ Validation Rules) |
| `insert_rows` then `delete_rows` | a phantom is an ordinary deletable row; deleting it declares `beyond-c1-c12` (no pin, no series, no inbound reference) |
| `insert_rows` then `dangle_reference` / `mispoint_reference` on the same kind | phantom ids join the id universe and donor pools — a mis-point may land on a phantom donor (it resolves; the shipped rules apply) |
| `insert_rows` after `delete_rows` on the same kind | the id universe (evaluated at the insert's start) contains every deleted id — via surviving history rows and reference cells where any exist, and via the tombstone set in every case — a phantom never resurrects a deleted entity, even one that left no other trace |
| `delete_rows` / `insert_rows` after `schema_drift` on the same table | rows are removed/cloned under the evolved schema; a phantom clones post-drift columns; resample eligibility evaluates against the evolved schema |
| family C after `delete_rows` orphaned a series | family-C operations still select those `history` rows; the C6-mirror oracle fails the round-trip (no records row — an unresolved series), so an anchor participant declares `C6` beside the delete's own `C6` — the joint declaration on an already-failing series; non-participants declare `beyond-c1-c12` |
| `duplicate_rows` `mutation` after `mutate_cells` on the same cells | copies clone the mutated working values, then transform them — composition, not conflict |
| an earlier `null_cells` nulled a row's `left_sim_time` | the row is filtered from every `distort_intervals` mode's population (all three require non-NULL `left_sim_time`); it may still serve as an `overlap` pair's successor B, read at `slice_at` |
| an earlier `null_cells` / `mutate_cells` / `dangle_reference` / `mispoint_reference` changed a membership row's element or member values | `distort_intervals` timeline identity reads the working values — the row groups under its current values, and adjacency follows |
| an earlier `delete_rows` removed membership intervals | `distort_intervals` timelines and adjacency resolve over the survivors |
| an earlier `duplicate_rows` duplicated an interval | the copy joins its timeline; byte-identical copies tie and are interchangeable; a copy may form an `overlap` pair with its twin — the rewrite rule needs no special case |
| an earlier `distort_intervals left_before_join` inverted a row, a later `overlap` selects it as the pair's earlier row A | the rewrite sets `A.left' ≥ A.joined` — the inversion is healed; the earlier `C10` stands as a sound over-declaration |
| an earlier `distort_intervals left_before_join` inverted a row, a later `gap` / `left_before_join` targets it | filtered out — both populations require `left ≥ joined` (+2 / strict >) on working values |
| `distort_intervals` then `null_cells` / `dangle_reference` / `delete_rows` / `duplicate_rows` on the same table, or a second `distort_intervals` on the same table | later populations, canonical orders, and timeline resolutions see the rewritten timing values — working-set truth |
| an earlier `schema_drift` renamed / dropped an `elem__` column | `distort_intervals` timeline identity groups on the current working schema's element columns — the evolved schema, as everywhere |

Selection is **category-uniform**: it filters and orders the working table's own columns,
so `records`, `membership`, and `fixed` (`history`) tables are all valid targets with no
per-category code path. `duplicate_rows` may target `history` (a duplicate tick is a
legitimate defect); `mutate_cells` may target `history.value`, its one eligible
fixed-category column (§ mutate_cells vocabulary and eligibility); `null_cells`,
`dangle_reference`, and `schema_drift` are confined by their column-eligibility business
rules (§ Validation Rules) to records/membership value columns, so only `duplicate_rows`
and `mutate_cells` ever reach `history` — near-duplicate `jitter` is confined to numeric
`prop__` / `elem__` payload columns, and `history` has none (its one numeric column,
`sim_time`, is structural).

### What each operation breaks, and the impact it declares

Every operation **preserves structural conformance (C1–C5, C8) by construction** (§ The
base-emit writer) and breaks only semantic conformance and/or the pin surface, or falls
outside C1–C12 entirely. The manifest requires each operation to declare the **complete,
correct** `impact` set for each defect it injects — the set of semantic codes it
*actually* trips, computed per-defect from the target's metadata, the operation config,
and **the working state the handler reads as of its own operation** (prior mutations
visible, later ones not). When that set is empty, and only then, the impact is the lone
sentinel `beyond-c1-c12`; codes compose by set union, and the sentinel is never unioned
with a real code.

A multi-table operation declares each defect with the impact rules of *its own* table — a
pooled `null_cells` over `category: records` declares `C6` for a tracked round-trippable
cell in one table and `beyond-c1-c12` for an untracked cell in another, exactly as two
single-table operations would. Locators carry each defect's own table, and the canonical
defect order discriminates by table first. Placement is invisible to the manifest — it
never appears in a `DefectRecord` — because it changes selection, not the nature or
location vocabulary of any defect.

| Operation / target | `class` | Locator | Declared `impact` |
|---|---|---|---|
| `null_cells`, records `prop__` with a history series **and** a round-trippable type (`_ROUND_TRIPPABLE_TYPES`) | `missing_value` | cell | `C6` — the null no longer round-trips to the series' latest `history.value` |
| `null_cells`, a C7-group member (`member__<f>__kind`/`id`, or `deactivated_at`) | `missing_value` | cell | `C7` when the null leaves the group partly populated; `beyond-c1-c12` when it completes an all-NULL membership pair (see below) |
| `null_cells`, any other value column (no history series, or a non-round-trippable type) | `missing_value` | cell | `beyond-c1-c12` |
| `duplicate_rows` exact on `records__<kind>`, sampled row's `record_id` is pinned | `duplicate_row` | row | `C9` |
| `duplicate_rows` exact, otherwise | `duplicate_row` | row | `beyond-c1-c12` (C9 counts only `records__<kind>` rows) |
| `duplicate_rows` near (`jitter`) | `near_duplicate_row` | row | recomputed independently of the exact case: `C9` iff the target is `records__<kind>` and the copy's id is pinned, unioned with `C6` iff a perturbed column is a tracked `prop__` whose copy has a history series **and the perturbation actually changed the stored value** (a delta can vanish under rounding/float absorption); `beyond-c1-c12` iff that union is empty |
| `duplicate_rows` `mutation` (conflicting duplicate) | `conflicting_duplicate_row` | row | recomputed independently: `C9` iff the target is `records__<kind>` and the copy's id is pinned; unioned with `C6` iff a mutated column is a `history_tracked` `prop__` whose current type is round-trippable, the copy's record has a history series with a non-empty C6 view, and the transform actually changed the stored value; unioned with `C12` iff the table is `records__actor`, a mutated column is `prop__actor_type`, `record_roles` declares sub-types, and the post-mutation value is undeclared; `beyond-c1-c12` iff that union is empty (§ `duplicate_rows` — the `mutation` mode) |
| `delete_rows`, `records__<kind>` row | `deleted_row` | row (source coordinate) | the wake, evaluated against the post-operation state: `C9` iff the id is pinned and zero copies survive in a non-empty table; unioned with `C6` iff zero copies survive and a working history series' C6 view exists on a round-trippable tracked property; unioned with `C10` iff zero copies survive and a surviving membership row still resolves to the id; `beyond-c1-c12` iff that union is empty (§ `delete_rows`) |
| `delete_rows`, membership row | `deleted_row` | row (source coordinate) | `beyond-c1-c12` always — removing an interval removes the check subject |
| `insert_rows` | `phantom_row` | row (post-corruption coordinate) | `beyond-c1-c12` always — phantom isolation guarantees no series, reference, or pin touches the fresh id (§ `insert_rows`) |
| `schema_drift` rename/drop, ticked column | `column_rename` / `column_drop` | column | `C11` |
| `schema_drift` retype, ticked column, round-trippable `retype_to` that changes the round-trip | `column_retype` | column | `C6` |
| `schema_drift` rename/retype/drop of an un-ticked payload column, a round-tripping retype, or a retype to a non-round-trippable type | `column_*` | column | `beyond-c1-c12` |
| `dangle_reference`, membership `member__<f>__id` | `dangling_reference` | cell | `C10` |
| `dangle_reference`, records `prop__` reference | `dangling_reference` | cell | `C6` iff the column is `history_tracked` and the dangled row's series exists; `beyond-c1-c12` otherwise |
| `mispoint_reference`, membership `member__<f>__id` | `mispointed_reference` / `point_in_time_dangling_reference` (constrained) | cell | `beyond-c1-c12` always — the donor resolves in `records__<kind>` by construction, so C10 and C7 cannot fail |
| `mispoint_reference`, records `prop__` reference | `mispointed_reference` / `point_in_time_dangling_reference` (constrained) | cell | `C6` iff the column is `history_tracked`, the table has a `record_kind`, and `series_round_trip_fails` on the post-write state; `beyond-c1-c12` otherwise. `constraint` changes the `class`, never the `impact` |
| `mutate_cells`, records `prop__<p>` / `history.value` | one of eleven kinds (§ What mutate_cells does) | cell | `C6` and/or `C12` per the anchor-participant / actor-sub-type rules (§ mutate_cells' impact rule: mirroring C6 and C12); `beyond-c1-c12` otherwise |
| `mutate_cells`, `presentation_id` / `elem__*` | one of eleven kinds | cell | `beyond-c1-c12` always |
| `distort_intervals`, `overlap` | `overlapping_interval` | cell (`left_sim_time`) | `beyond-c1-c12` always |
| `distort_intervals`, `gap` | `interval_gap` | cell (`left_sim_time`) | `beyond-c1-c12` always |
| `distort_intervals`, `left_before_join` | `inverted_interval` | row | `C10` always |

**`distort_intervals`'s impact is closed by construction**, the same unconditional
declaration `insert_rows`' phantom isolation makes: `overlap` / `gap` touch no reference,
no pin, no `history`, and no `records__actor` discriminator, so neither can trip anything
but `beyond-c1-c12`; `left_before_join` trips exactly `C10` on its own mutated rows, by
the strict `left > join` population filter the swap inverts. The impact universe is
closed the same way for every other code: C6 — membership tables carry no history
series; C7 — `member__<f>__kind`/`id` and `deactivated_at` are never written; C9 —
`record_id` is untouched; C11/C12 — `history` and `records__<kind>` are untouched.
`overlap` and `gap` locate their defect at a **cell** on the earlier row, column
`left_sim_time`, at the post-corruption coordinate — which equals the source coordinate,
since the `RowRef` prefix member `joined_sim_time` is untouched by either mode.
`left_before_join` locates its defect at **row** granularity, since it exchanges two
prefix-adjacent cells in one atomic act; its `RowRef` prefix carries the rewritten
`joined_sim_time` — the post-corruption coordinate, the same rewritten-identity-column
stance `shift_sim_time` established (§ Locators, `RowRef`, and `impact`). Membership
localization stays coarse for all three modes, as for every membership defect: the
`RowRef` prefix may match a group of co-joining sibling intervals (§ Locators, `RowRef`,
and `impact`).

**C7 groups are structural**, driven by column shape, not by a `ColumnSpec` field: the
membership pair `member__<f>__kind` / `member__<f>__id` (all-NULL or all-non-NULL) and the
records `deactivated_at` gated by `active`. Nulling one half of a both-non-NULL membership
pair trips C7; nulling the second half (same operation or a later one) completes an
all-NULL pair — C7-conformant — so that second null declares `beyond-c1-c12` and **heals**
the first half's C7, leaving a declared-but-no-longer-firing code. That residual is a sound
over-declaration, never an under-declaration (§ Manifest / validate agreement invariant).
For `deactivated_at`, a non-NULL value marks an inactive row, so nulling it always violates
NULL-iff-active and trips C7.

**Retype and the round-trip.** A `schema_drift` retype trips C6 only when the cast changes
a tracked value's codec encoding: `BIGINT → VARCHAR` round-trips, `BIGINT → DOUBLE` does
not, and a retype to a non-round-trippable type (`DATE`, `DECIMAL`, …) makes C6 *skip* the
series — no break at all. The handler gates first on the same `_ROUND_TRIPPABLE_TYPES` set
C6 gates on, and never calls the round-trip codec on a non-round-trippable type (the codec
raises `ValueError` there, which would otherwise escape the CLI funnel). Only when
`retype_to` is round-trippable does it compare the cast-and-encoded value against the
stored `history.value` and declare `C6` iff any affected row diverges — the same codec, on
the same gate, C6 itself evaluates, so the label and the check cannot disagree.

**`dangle_reference`'s population is restricted three ways**, each an instance of one
precondition — an unresolvable reference cannot be dangled: (1) the reference id is
non-NULL; (2) for a membership `member__<f>__id`, the partner `member__<f>__kind` is also
non-NULL (an earlier `null_cells` may have nulled it); (3) the resolved target
`records__<kind>` table is present in the working set (a conformant emit may omit a
referenced kind's table entirely — C10 skips that case rather than failing). The rewrite
uses a fixed sentinel prefix `DANGLING_ID_PREFIX = "__dangling__"` plus the smallest
non-negative integer suffix absent from the target table's id column on the sole
`fork_path` — deterministic and guaranteed-absent, verified against the working set (so it
accounts for prior operations). Only the id column is rewritten; its `kind` partner stays
non-NULL, so the C7 pair stays whole while C10 resolution fails. A dangled records `prop__`
reference can additionally trip C6 when the column is `history_tracked` and the dangled
row's series exists in the working state — the rewritten sentinel then fails the C6
round-trip.

**`mispoint_reference` extends `dangle_reference`'s three population filters with a
fourth — a non-empty donor pool — and rewrites the id to a real row instead of a
sentinel.** A cell survives filtering only when: (1)–(3) are `dangle_reference`'s own,
verbatim (the id is non-NULL; a membership id column's `member__<f>__kind` partner is
non-NULL; the resolved target `records__<kind>` table is present in the working set);
(4) its **donor pool** — the working `records__<kind>` table's distinct `record_id`
values on the sole `fork_path`, excluding the cell's current id, sorted ascending — is
non-empty. Each selected cell draws exactly one donor by pool index
(`rng.randrange(len(pool))`, one draw per selected unit, ascending selected-unit order —
the same fixed-order RNG discipline every sampling operation follows). Only the id cell
is rewritten; its `kind` partner is untouched, so the C7 pair and C10's resolution both
stay intact by construction — `mispointed_reference` is invisible to `validate` and
recoverable only via `defects.json`.

`constraint: created_after_reference` narrows the donor pool to ids whose creation time
(the minimum `created_sim_time` among a donor's rows) is strictly greater than the
cell's **write anchor** — an upper bound on when the reference's current value was
written, read from the same operation-start working state as the pool:

| Referencing cell | Write anchor |
|---|---|
| membership `member__<f>__id` | the row's `joined_sim_time` |
| records `prop__` reference, `history_tracked`, with a resolvable C6 anchor (`resolve_c6_anchor`) | the C6 anchor's `sim_time` — the exact write time |
| records `prop__` reference otherwise (untracked, no `record_kind`, no series, or an empty C6 view) | the row's `last_mutation_sim_time` — no property write postdates it |

Anchoring at `last_mutation_sim_time` rather than an unknowable exact write time keeps
every `point_in_time_dangling_reference` label sound at the cost of a smaller donor
pool — soundness of the ground-truth artifact over defect volume. The narrowed pool
flips the declared `defect_class` from `mispointed_reference` to
`point_in_time_dangling_reference` (never the `impact` — § What each operation breaks,
and the impact it declares), labeling the late-arriving-dimension case: a reference
that resolves *now* but was dangling *at the moment it was written*. An empty pool (no
donor, or none late enough) population-filters the cell rather than erroring — the same
data-vs-config distinction every zero-row population in this family follows.

No `history` co-write hides a mis-pointed tracked reference: the defect declares `C6`
(or `beyond-c1-c12`) and `defects.json` alone carries the truth, the same stance
`dangle_reference` takes. A `dangle_reference` that later mis-points a cell (or a
`mispoint_reference` that later re-dangles one) **heals** the earlier defect's
resolution — the sentinel is trivially excluded from any donor pool, so the mis-point is
always eligible, and the earlier declaration stands as a sound over-declaration (§
Manifest / validate agreement invariant).

**`schema_drift` transforms the catalog, not row identities.** A rename relabels the
column in `WorkingTable.spec` and the Arrow field, preserving the `ColumnSpec`'s other
fields; a retype casts via DuckDB `CAST`, updating `type` while keeping `references` /
`history_tracked`; a drop removes the column from spec and Arrow. `DriftRenamePreservesCategory`
rejects a rename target that would change the column's structural category (a `prop__`
renames only to a `prop__`, an `elem__` only to an `elem__`), so C5 holds by construction.
The `rename_to` / `retype_to` / `drop` maps within one `schema_drift` resolve against the
**pre-operation schema** and apply as a single atomic set-semantics transform, not chained
left-to-right — a drift whose targets would collide fails at apply time. This
order-independence is what makes sorting those keys in the config fingerprint sound (§
Determinism, canonical ordering, and the config fingerprint).

**Near-duplicate `jitter`** perturbs each copied row's `target.columns` cells (numeric
`prop__` / `elem__` payload cells only) by an additive delta drawn from the `Distribution`:
a `DOUBLE` cell stores `value + delta` as-is; a `BIGINT` cell stores
`round(value + delta)` (round-half-to-even) back in the integer type, so the column keeps
its type; a NULL cell stays NULL (no delta applied). Deltas are drawn in canonical row
order, then `target.columns` list order. A delta may vanish in the store (rounding,
float absorption) — the copy is still injected, but its `C6` declaration follows the
actual-divergence rule above, so a no-op perturbation never declares a code it cannot trip.

### `duplicate_rows` — the `mutation` mode

`mutation: MutationSpec` is `duplicate_rows`' third mode, alongside exact and `jitter`,
mutually exclusive with it (`DuplicateRows.perturbation_governs_columns`, § Validation
Rules); `target.columns` is required when either perturbation mode is set and forbidden
when neither is. All eleven `MutationSpec` kinds are admitted.

**Eligibility.** A `columns` entry matches against the **conflict-eligible** columns of
each resolved table: the records/membership members of the `mutate_cells` name class (§
mutate_cells vocabulary and eligibility) — records `prop__*` with `references` unset,
`presentation_id`, membership `elem__*` — narrowed by the mutation kind's type gate (and,
for `out_of_domain`, its `enum_domains` gate). `history.value` is deliberately not
conflict-eligible: a resolved `history` table contributes zero row units (the zero-match
rule, § Column entries: exact names and patterns), and a history-only target fails at
`ConflictMutableColumns`.

**Effect.** Each selected row is copied once; every matched cell of the copy is
transformed by the mutation kind's shipped transform semantics (§ What mutate_cells
does) — the same donor-pool rule for `resample` (excluding the source cell's value,
operation-start state), the same no-mutation conditions, the same NULL-invariance, the
same apply-time sentinel-representability cast. A copy whose matched cells are all
no-mutation degenerates to an exact copy and is still injected — the jitter
vanishing-delta precedent above — with its impact recomputed from actual divergence.

**Impact.** One `DefectRecord` per copy — class `conflicting_duplicate_row`, a
post-corruption row locator. Impact is the union of:

| Code ∈ impact iff |
|---|
| `C9` — the target is `records__<kind>` and the copied `record_id` is pinned (the exact/near rule above) |
| `C6` — a mutated column is a `history_tracked` `prop__` whose current working type is round-trippable, the copy's record has a history series with a non-empty C6 view, **and** the transform actually changed the stored value — the same actual-divergence rule near mode follows, stated explicitly here because `jitter`'s numeric-only eligibility left the round-trippable gate implicit, while `mutation`'s any-type kinds (`sentinel`, `resample`) do not: a `sentinel` on a `DATE`-typed tracked column would otherwise declare a `C6` the check skips |
| `C12` — the table is `records__actor`, a mutated column is `prop__actor_type`, the sidecar's `record_roles` registers `"actor"` with declared sub-types, and the copy's post-mutation value is undeclared (`actor_subtype_undeclared`, § mutate_cells' impact rule: mirroring C6 and C12) — reachable here because `mutation`, unlike numeric `jitter`, can transform the VARCHAR discriminator |
| `beyond-c1-c12` — that union is empty |

### `delete_rows`

The selection unit is one row; the population is the pooled row population of the
resolved tables (§ The pooled population and unit enumeration) — identical to
`duplicate_rows` exact mode. Every resolved table must be records- or
membership-category (`NonHistoryTarget`, § Validation Rules): `history` row removal is
`drop_events`' alone. `target.columns` is forbidden (`DeleteRows.no_columns`) — a row
removal touches no specific column.

Every drawn row is removed from its working table, all removals within one operation
applying as a single simultaneous set (multiset semantics: two byte-identical drawn rows
remove two copies, ties interchangeable). Each removed `records__<K>` row also records
its `record_id` in the working state's kind-K tombstone set
(`CorruptState.deleted_record_ids`) — read only by `insert_rows`' id universe (§
`insert_rows`); a membership removal records nothing, since no entity id is removed.

**The wake.** Impact is evaluated per deleted row against the state *after* the
operation's own removals apply, so same-operation removals compose correctly:

| Deleted row | Code ∈ impact iff |
|---|---|
| `records__<K>` row, `record_id` R | `C9` iff R is pinned for kind K, zero rows carrying R survive in the working `records__<K>`, and that table is non-empty post-operation — C9 quantifies only over the fork_paths present in the table, so an emptied table passes vacuously and its pins trip nothing |
| `records__<K>` row, `record_id` R | `C6` iff zero rows carrying R survive **and** ≥ 1 working `history` series `(K, R, p)` has a non-empty C6 view whose `prop__<p>` exists in the working `records__<K>` schema with a round-trippable type — the C6 oracle's own gates (§ Family-C's impact rule: mirroring C6), mirrored gate-for-gate: an orphaned series is an *unresolved* series, which C6 fails, never skips |
| `records__<K>` row, `record_id` R | `C10` iff zero rows carrying R survive **and** ≥ 1 surviving membership row anywhere in the working set carries a non-NULL member pair resolving to (K, R) |
| `records__<K>` row — dangling records-prop references from other tables, an orphaned series outside the C6 gates, an intact pin via a surviving copy | none of these trip a check — contributes nothing to the union |
| membership row | always `beyond-c1-c12` — removing an interval removes the check subject; no C1–C12 check quantifies over interval existence |

Codes compose by set union; an empty union declares the lone sentinel
`beyond-c1-c12`. The dangling records-prop reference and the removed membership interval
are the subconformance teaching payload — derivable from the deleted row's manifest
coordinate, with `defects.json` as the only ground truth; the orphaned series is the
visible half, since `validate` itself names the C6 failure as an unresolved series and
only the manifest names the delete that caused it.

**Zero-copy survival, not pre-operation row count, drives the pin rule.** A conformant
source carries exactly one row per records `record_id`; only `duplicate_rows` can raise
that count, declaring its own `C9` when it does. Deleting one of two copies of a pinned
row restores the count to one: the delete declares no code (post-op state: C9 passes),
and the earlier duplicate's `C9` stands as a sound over-declaration (§ Manifest /
validate agreement invariant) — the same healing stance every cross-operation
interaction in this family takes.

### `insert_rows`

The selection unit is one donor row; the population is the pooled row population of the
resolved tables — every resolved table must be records-category
(`RecordsCategoryTarget`, § Validation Rules). `amount` sizes the phantom volume against
the donor population: `rate: r` → `floor(r · N)` phantoms, `count: k` → `min(k, N)`; the
draw is without replacement, so one operation injects at most N phantoms (repeat the
operation for more). N = 0 (an empty donor population) is the data-dependent no-op: a
phantom cannot be fabricated from nothing, by design — every payload value must trace to
a stored value. `placement` weights the donor draw.

**Phantom assembly.** Each drawn donor yields exactly one phantom row in the donor's own
table — a verbatim clone (`fork_path`, lifecycle, payload, `presentation_id` included)
except:

- **`record_id`** — a fresh id derived from the donor's id: candidate ids are each
  adjacent-character exchange of the donor id, positions scanned in seeded rotation,
  falling back, when every exchange is taken, to the donor id with its final character
  (or `"0"` when the donor id is empty) appended repeatedly; the first candidate absent
  from the kind's id universe wins (termination: the universe is finite). The id is
  deliberately plausible — a transposed real id, not a sentinel — because a phantom's
  teaching value is looking real.
- **Resampled payload (optional).** When `target.columns` is present, each matched
  eligible cell of the phantom (records `prop__*` with `references` unset, or
  `presentation_id`) is replaced by an intra-column resample — the `mutate_cells`
  `resample` donor-pool contract verbatim (§ mutate_cells vocabulary and eligibility):
  operation-start state, distinct non-NULL values of the column narrowed to the sole
  `fork_path`, excluding the donor cell's current value, DuckDB total order. An empty
  pool leaves the cloned value (the no-mutation stance); a NULL cloned cell stays NULL. A
  resolved table matching zero of its insert-eligible columns still contributes its whole
  donor population, as pure clones with zero resample draws — unlike near mode's
  zero-match exclusion, a phantom needs no payload divergence: the fresh `record_id` is
  the defect on its own.

**The id universe of kind K** — the absence domain a phantom id must avoid — is the
union, over the working state at the operation's start, of: the working `records__<K>`
`record_id` values; `history.record_id` values on rows with `kind = K`; every non-NULL
`member__<f>__id` whose partner kind cell is K, in every working membership table; every
non-NULL cell of every records `prop__` column whose `references` target is K; the
sidecar's pinned ids for K; the working state's kind-K tombstones
(`CorruptState.deleted_record_ids` — every id an earlier `delete_rows` removed from
`records__<K>`; § `delete_rows`); and the ids already assigned to earlier phantoms of the
same operation. This buys **phantom isolation** (§ Invariants): no history series, no
inbound reference, no pin — and, because referencing cells and tombstones are in the
universe, no accidental healing of an earlier `dangle_reference` sentinel and no
resurrection of an id an earlier `delete_rows` removed.

**Defect declaration.** One `DefectRecord` per phantom — class `phantom_row`, a `RowRef`
locator carrying the phantom's post-corruption coordinate (`fork_path`, the fresh
`record_id`). Impact is always the lone `beyond-c1-c12`, guaranteed by phantom isolation
rather than asserted per-defect: no series to break (C6 skips), no pin (C9), no reference
resolution touched (C10/C7), no history change (C11), and clone-or-resample payload
comes from the working state, so a phantom fails no check its donor's values did not
already fail (C12 included — an inherited out-of-domain discriminator is the originating
defect's declaration, not this one's; § Invariants). The detection signature is the
anti-join — a records row with no history trail — the orphan-detection lesson inverted.

### What freeze_series, drop_events, and shift_sim_time do

**`freeze_series`.** For each selected series, a **cut** `c` — the kept-prefix length
over its timeline of length N — is fixed: `cut: after_first` sets `c = 1` (only the
first timeline row is kept); `cut: random` draws `c` uniformly from `[1, N−1]`. Every
timeline row past the first `c` is removed, including rows past `slice_at`. The kept
prefix ends at the frozen value; the records snapshot is untouched, so when the
suppressed tail contained the anchor, the round-trip breaks (§ Family-C's impact rule:
mirroring C6). One defect per removed row: `class: frozen_series_event`, row locator,
the removed row's source coordinate.

**`drop_events`.** Each selected event row is removed. One defect per removed row:
`class: dropped_event`, row locator, source coordinate.

**`shift_sim_time`.** Rewrites `sim_time` cells; all rewrites in one operation resolve
against the state the operation began with and apply as a single simultaneous set (the
same atomic set-semantics stance as `schema_drift`'s maps).

- `shift.kind: offset` — each selected event's delta is drawn from `distribution` (ns
  units) and rounded round-half-to-even to `BIGINT`; the event's `sim_time` becomes
  `sim_time + delta`. A sum outside `BIGINT` range fails loudly at apply time in the
  shared Arrow/DuckDB integer domain — the same failure domain as the shipped `BIGINT`
  jitter store, never a silent wrap. A delta may round to zero — the row is then
  unchanged: no defect is emitted and the unit is not counted (the no-mutation rule,
  below); the delta draw is still consumed, so RNG consumption stays a fixed function of
  the selected-unit count. A shifted tick may land anywhere: past `slice_at`
  (future/skew — the event leaves the C6 view), negative, or coinciding with another
  tick of the same series (an incidental collision — C6 stays deterministic through its
  `value DESC` tie-break). One defect per mutated event: `class: shifted_event_time`, row
  locator, **post-corruption** coordinate.
- `shift.kind: collide` — the selected event's `sim_time` becomes its predecessor tick:
  two changes now share one tick. One defect per selected event: `class: tick_collision`,
  row locator, post-corruption coordinate (which now names the colliding group — the
  shipped coarse-localization stance for identity-coincident rows).
- `shift.kind: swap` — the selected event and its **partner** — the first row in
  canonical content order at the predecessor tick — exchange `sim_time` values,
  scrambling the value timeline while preserving the series' tick set. The partner is
  resolved from the series' timeline, never from the narrowed population, so a swap may
  rewrite a partner row `target.where` excluded (the same whole-timeline stance as a
  freeze). Two defects per performed swap, one per moved row: `class: reordered_event`,
  row locators, post-corruption coordinates; because `RowRef` identity carries `sim_time`
  but not `value`, the two post-swap coordinates coincide with the pre-swap coordinate
  set — the manifest names the reordered tick pair, not which value moved where, a
  deliberate coarse localization (the shipped identity-coincident stance). Two swaps
  change nothing and fall to the no-mutation rule: an **equal-value swap** (the two rows
  differ only in `sim_time`, so exchanging their ticks yields the byte-identical table
  multiset) and a **chained swap** (the selected row or its partner was already rewritten
  by this operation — chained pairs are skipped in ascending selected-unit order, since
  whether two selected units chain is decided by data and the draw, not by the config).

`units_affected` counts **selected units** whose stored rows actually changed — the same
unit `amount` pooled over: `freeze_series` counts selected series (every selected series
removes at least one row, since `c ≤ N−1`, so this equals the drawn series count);
`drop_events` counts removed event rows; `shift_sim_time` counts selected events,
excluding zero-delta offsets, equal-value swaps, and skipped chained swaps — a performed
swap counts its one selected unit though it rewrites two rows.

The family-wide **no-mutation rule**: a selected unit that changes nothing — a
zero-rounded offset delta, an equal-value swap, a skipped chained swap — emits no defect
and is not counted, the shipped unchanged-unit stance (nulling an already-NULL cell, §
What each operation breaks, and the impact it declares). The RNG cost is still paid
(draws precede the gate), so emission-gating costs no determinism.

`units_affected` keeps one meaning across every operation — selected units that actually
changed stored state — but its equality with `len(defects)` is not universal: `drop_events`
and `shift_sim_time`'s `offset` mode keep the equality, while a freeze counts one series
unit while emitting one defect per removed row, and a swap counts one unit while emitting
two records. The unit remains what `amount` pooled over, within the same manifest schema
(§ Location and schema ownership of the manifest).

### Family-C's impact rule: mirroring C6

Every family-C defect declares either `C6` or `beyond-c1-c12` — never another code: no pin
target changes for C9; no membership row or reference changes for C7/C10; `record_roles`
and the records data are untouched for C12; C11 quantifies over the distinct `(kind,
property)` pairs in `history`, and removing rows or rewriting `sim_time` never adds a
pair, so C11 cannot start failing.

**Round-trip evaluation** (`series_round_trip_fails`) for a series, on the working state
after the calling operation, mirrors `_check_c6` gate-for-gate against the current
working schema (`WorkingTable.spec`, so an earlier `schema_drift` on the records side is
honored):

| Condition (evaluated in order) | Series outcome |
|---|---|
| C6 view empty (no timeline row with `sim_time ≤ slice_at`) | not evaluated → cannot fail |
| `records__<kind>` absent from the working set | skipped → cannot fail |
| `prop__<property>` absent from that working table's schema | skipped → cannot fail |
| that column's current type not round-trippable | skipped → cannot fail |
| no records row at `(fork_path, record_id)` | fails |
| records cell NULL | fails |
| anchor's `value` ≠ `to_csv_text(records cell)` (the same codec C6 uses) | fails |
| anchor's `value` == the encoded cell | passes |

**Per-defect declaration — the anchor-participant rule.** A defect declares `C6` iff both
hold; otherwise it declares `beyond-c1-c12`: (1) its series' round-trip fails on the
post-operation state (table above); (2) its row is an **anchor participant** — it was the
series' anchor in the state the operation began with, or it is the series' anchor after
the operation (a removed row can only satisfy the first disjunct; for a `swap`, each of
the two records tests its own row). Anchor participation is decided by content, never by
position: a row is the anchor in a given state iff its `(sim_time, value)` pair equals
that state's anchor pair — with byte-identical duplicates, every copy carrying the pair
participates, since ties are interchangeable and participation must be a function of the
multiset and the selection content, not of which physical copy a draw touched.

| Scenario | Declared impact |
|---|---|
| Freeze whose suppressed tail contains the anchor, exposed older value differs | the suppressed ex-anchor row's record declares `C6`; the rest of the tail declares `beyond-c1-c12` |
| Freeze cut entirely below the anchor (anchor kept) | all `beyond-c1-c12` |
| Drop of a mid-series event | `beyond-c1-c12` (lost CDC message — subconformant) |
| Drop of the anchor, exposed value differs | `C6` |
| Drop of the anchor, exposed value's codec text equal | `beyond-c1-c12` (actual-divergence stance) |
| Drop / shift-out of a series' entire C6 view | all `beyond-c1-c12` — the series leaves C6's iteration; the records cell is an orphaned snapshot value (subconformance) |
| Offset shifting a non-anchor event above the anchor (new anchor, value differs) | `C6` on that event |
| Offset shifting the anchor past `slice_at` (older value exposed, differs) | `C6` on that event |
| Zero-rounded offset delta (the row is unchanged) | no defect emitted; the unit is not counted (the no-mutation rule) |
| Collide of a non-anchor event onto a lower tick | `beyond-c1-c12` (the canonical tick collision) |
| Collide of the anchor onto its predecessor tick | conditional — the collided pair resolves through C6's `value DESC` tie-break; `C6` iff the resolved value diverges |
| Swap not involving the anchor | both records `beyond-c1-c12` |
| Swap involving the anchor, round-trip fails | each moved row that was or becomes the anchor declares `C6`; a moved row that neither was nor becomes it declares `beyond-c1-c12`. Swapping the anchor's own tick hands that tick to the partner, which typically becomes the new anchor — then both records declare `C6`, a joint, sound declaration |
| Equal-value swap (byte-identical multiset) | no defects emitted; the unit is not counted (the no-mutation rule) |
| Series already failing C6 from an earlier operation's defect, this operation touches no anchor | `beyond-c1-c12` here — the earlier record already carries the `C6` |

**Soundness.** If one operation flips a series from passing to failing, some touched row
is an anchor participant: an untouched row cannot outrank an untouched anchor, so either
the old anchor was removed/moved (touched, first disjunct) or a touched row became the
new anchor (second disjunct). Composed with earlier operations, the containment
invariant `validate_failing ⊆ impact_union` is preserved (§ Manifest / validate agreement
invariant): an inherited failure is already declared by the operation that caused it. A
later family-C operation may also heal an earlier `C6` (dropping the very event an
earlier shift promoted to anchor) — the earlier label becomes a sound over-declaration,
exactly the shipped C7-healing stance. An anchor participant in an already-failing series
declares `C6` too — a joint, sound over-declaration.

### What mutate_cells does

All mutations within one operation resolve against the state the operation began with
(stored values, donor pools) and apply as a single simultaneous set — the same atomic
set-semantics stance `schema_drift`'s maps and family-C's rewrites take. Selected units
are distinct cells, so no two mutations read each other's output.

| `mutation.kind` | Transform | No-mutation when |
|---|---|---|
| `sentinel` | replace the stored value with the author's `value`, rendered into the column's current type by the shared DuckDB cast | the cast result equals the stored value |
| `typo` | `VARCHAR`: exchange the two adjacent characters at a seeded position. `BIGINT`: exchange two adjacent decimal digits of the absolute value, sign preserved, stored back as `BIGINT` (a leading zero after the exchange simply parses to a smaller number) | fewer than two characters / digits; the exchange yields the original (equal neighbors); the exchanged digits do not fit the `BIGINT` domain (cannot-apply — never a loud failure; § Rationale) |
| `case` | apply the author's `form` — `upper`, `lower`, `title`, or `swap` — to the whole string | the transform is identity for the stored value |
| `whitespace` | insert exactly one space character at the author's `where` end (`leading` or `trailing`) | never (any present string changes) |
| `truncate` | keep the first `max_length` characters | length ≤ `max_length` |
| `precision_drop` | round to `digits` decimal places (round-half-to-even), stored back as `DOUBLE` | the value is already equal at that precision |
| `scale` | multiply by `factor`; `DOUBLE` stores the product as-is, `BIGINT` stores round-half-to-even of the product (an out-of-range product fails loudly in the shared Arrow/DuckDB integer domain — the jitter-store precedent, never a silent wrap) | the product equals the stored value (e.g. zero × anything) |
| `mojibake` | re-decode the value's UTF-8 bytes as latin-1 (`café` → `cafÃ©`); total, since latin-1 decodes every byte | the value is pure ASCII (identity) |
| `format_dirt` | when the value is an optional-minus all-digit string of at least four digits, insert comma thousands separators (`12345` → `12,345`) | the value does not match that shape |
| `resample` | replace with the value at the seeded index into the donor pool | the donor pool is empty (no distinct other non-NULL value exists) |
| `out_of_domain` | generate candidates — each adjacent-character exchange of the stored value, positions scanned in seeded rotation, then, as a total fallback, the value with its final character appended repeatedly — and take the first candidate that is neither in the declared domain nor equal to the original (the fallback terminates because the domain is finite) | the stored value is empty (nothing to mutate) |

The consumer-realization caveat: `mojibake`, `format_dirt`, and `truncate` inject values at
the base layer; their teaching payoff (encoding awareness, parsing failures, width errors)
materializes when the corrupted emit is exported downstream. The base layer stores them as
ordinary strings.

Each kind names its own `defect_class`, one per kind, an open-vocabulary addition (no
manifest schema or `DEFECT_MANIFEST_VERSION` change): `sentinel_value`, `typo_value`,
`case_drift`, `whitespace_pad`, `truncated_value`, `precision_drop`, `scaled_value`,
`mojibake_value`, `format_dirt`, `resampled_value`, `out_of_domain_value`. Every locator is
a `cell` locator with the shipped `RowRef` composition (§ Locators, `RowRef`, and
`impact`); for `history.value` mutations `RowRef` carries cols 1–5 (`value` excluded), so
the coordinate never embeds the corrupted value itself, and it is unchanged by the
mutation anyway since `sim_time` is not touched. `units_affected == len(defects)` holds
for `mutate_cells` — one mutated cell, one defect, the same strict 1:1 the
pre-family-C operations keep.

### mutate_cells' impact rule: mirroring C6 and C12

Every `mutate_cells` defect declares a subset of `{C6, C12}`, or the sentinel — never
another code: C7 is structurally unreachable (NULL-invariance, § Invariants), C9 compares
pinned `record_id`s and no structural column is ever mutated, C10 resolves reference
columns, which the name class excludes, and C11 quantifies over the distinct `(kind,
property)` pairs in `history` — a `value` rewrite never adds or removes a pair.

Each defect's impact is computed per mutated cell against the working state — prior
operations visible, later ones not — mirroring the shipped oracles so the label and the
check cannot disagree. For `history.value`, anchor participation consults both the
operation-start and the post-operation states; the round-trip verdict is post-operation
only. Codes compose by set union; an empty union declares the lone sentinel
`beyond-c1-c12`.

| Mutated cell | `C6` ∈ impact iff | `C12` ∈ impact iff |
|---|---|---|
| records `prop__<p>` | `series_round_trip_fails` on the **post-operation** state for the cell's `(kind, p, record_id)` series — the oracle's own gates (empty C6 view, absent series, non-round-trippable type) apply | the table is `records__actor`, the column is `prop__actor_type`, the sidecar's `record_roles` registers `"actor"` with declared sub-types, and the post-mutation value is not one of them (`actor_subtype_undeclared`) |
| `history.value` | the mutated row holds the series' anchor in the **operation-start** state (`resolve_c6_anchor` on the state the operation began with) **or** its post-mutation `(sim_time, value)` pair equals the **post-operation** anchor — **and** `series_round_trip_fails` on the post-operation state. A rewrite is a removal plus an insertion: it can break the round-trip by being, becoming, or ceasing to be the anchor (only the `value DESC` tie-break can move ranking — `sim_time` is never touched) | never |
| `presentation_id`, `elem__*` | never | never |

Consequences worth stating:

| Condition | Result |
|---|---|
| Mutating an untracked / non-round-trippable records `prop__` cell | `beyond-c1-c12` — C6 skips or never sees the series |
| Mutating a post-`slice_at` `history.value` row | `beyond-c1-c12` — the row is outside the C6 view, so it cannot be the anchor |
| A resample that draws back the value a prior operation corrupted away | the oracle reports no failure → `beyond-c1-c12`; the prior defect's `C6` becomes a sound over-declaration (the shipped healing stance) |
| A mutation on a tracked `prop__actor_type` cell | can declare `{C6, C12}` — the union case |
| `record_roles` absent, or `"actor"` unregistered / not sub-typed | C12 is false — never an empty-registry over-declaration |
| Mutating a same-tick non-anchor `history.value` row to a value ranking above the anchor | the row becomes the post-state anchor → participation holds → `C6` iff the round-trip now fails |
| Mutating the operation-start anchor to a value ranking below a same-tick sibling | the sibling becomes the post-state anchor — operation-start participation holds → `C6` iff the round-trip now fails (the demotion break) |

Every mutation kind is subconformance (`beyond-c1-c12`) except where it lands on one of
the three surfaces above — the family's teaching point: `validate` is blind to nearly all
of it, and `defects.json` is the only ground truth.

### Locators, `RowRef`, and `impact`

A `Locator` pinpoints *where* a defect was injected, at one of three granularities —
`column`, `row`, or `cell` — no more, tracking exactly what the operations emit. This is
the opposite stance from `ImpactCode`: that vocabulary mirrors the fixed *external*
conformance set (and so keeps `C12` though no operation trips it), while locator
granularities are a repo-owned coordinate scheme with no external anchor.

A locator is a coordinate in the **base namespace** (table / column / row identity), not a
physical-row pointer, and carries no promise that the coordinate resolves to exactly one
live entity in the corrupted emit. It names the entity's coordinate in the **corrupted**
emit wherever the entity still exists there — a nulled cell, a duplicate row, and a
renamed/retyped column all carry their post-corruption name. The sole exception is an
entity the corruption removed: a dropped column, or a removed `history` row (`drop_events`,
a `freeze_series` tail), has no corrupted-emit coordinate, so it is named by its source
coordinate — the dropped-column precedent extended from columns to rows. A
`shift_sim_time`-rewritten row still exists, so it carries its post-corruption (rewritten
`sim_time`) coordinate like any other mutated row.

| Defect nature | `location.kind` | Carries `RowRef`? |
|---|---|---|
| Single cell nulled, mutated, or overwritten | `cell` | yes |
| A reference cell dangled | `cell` | yes |
| One whole row injected (duplicate / near-duplicate) | `row` | yes |
| A column dropped / renamed / retyped | `column` | no |
| A `history` event removed (`drop_events`, a `freeze_series` tail row) | `row` | yes (source coordinate) |
| A `history` event's `sim_time` rewritten (`shift_sim_time`) | `row` | yes (post-corruption coordinate) |

A `RowRef` carries the row's **structural identity prefix** — the contract-pinned,
non-null identity columns — as ordered `(column_name, codec_text)` pairs, tagged by
category, rendered with the same text codec C6 uses (`to_csv_text`). It deliberately
excludes mutable payload/element columns, so a locator never embeds the value a defect
itself may have corrupted.

| Category | `RowRef.keys` columns (in order) | Source |
|---|---|---|
| `records` | `fork_path`, `record_id` | C5 fixed prefix |
| `history` | `fork_path`, `kind`, `record_id`, `property`, `sim_time` | C4 cols 1–5 (`value` excluded) |
| `membership` | `fork_path`, `record_id`, `joined_sim_time` | membership fixed prefix |

**Membership localization is coarser, by design.** Unlike records (`(fork_path,
record_id)` is a primary key) and history (cols 1–5 are unique in clean data), the
membership prefix is not a unique interval identity — one owner may hold several elements
co-joining at one `sim_time`, disambiguated only by element/member values, which are
exactly what a `null_cells` / `dangle_reference` / near-duplicate defect corrupts.
Embedding them would leak a before-value and create a chicken-and-egg with the defect's
own target column, so a membership coordinate localizes to the *group* of intervals
sharing the prefix, which may include clean sibling intervals. Exact duplicates are
localized the same way — a byte-identical `history` tick or membership row is physically
indistinguishable from its copies, so the manifest labels the row-identity coordinate plus
multiplicity, never a physical offset.

Each record names one or more `impact` codes:

| `impact` entry | Meaning | Visible to `validate`? |
|---|---|---|
| `C6`, `C7`, `C9`–`C12` | This defect causes that named *semantic* check to fail on the corrupted emit. | Yes |
| `beyond-c1-c12` | This defect passes `validate` by design — it breaks no C1–C12 check. | No — the manifest is the only record of it. |

A corrupter preserves structural conformance (C1–C5, C8) by construction, so `ImpactCode`
omits the structural codes entirely — Principle #3 is enforced by the type, not by
convention. `impact` is **operator-declared**, not computed by the driver: an operation
must declare the exact set of semantic codes it trips (the table above). `impact` is a
**set**: `normalize_impact` sorts and de-duplicates it and rejects any mix of
`beyond-c1-c12` with a real code, so two declarations naming the same guarantees render
byte-identically, which is what lets `impact` serve as the final canonical-order
discriminator.

### Manifest / validate agreement invariant

Given a **C1–C12-conformant source emit**, the set of check ids `fabulexa-forge validate`
reports as failing on the corrupted emit is **contained in** the union of `impact` entries
across all manifest records, excluding `beyond-c1-c12`
(`validate_failing ⊆ impact_union`). This **soundness** direction is load-bearing: every
check `validate` finds failing is accounted for by a declared defect, so the corrupter
never breaks something it did not record. It rests on two invariants (§ Invariants):
**structural preservation** (no structural code can fail) and **break locality** (the
writer introduces no incidental conformance breakage beyond the declared defects) — both
per-table writer/operation properties that pooling across tables does not alter.

The reverse direction — every declared code actually fires in `validate` — holds absent
cross-operation healing (no later operation nulls the partner half of a C7 pair an earlier
defect already broke). Where a heal does occur, the earlier label is a sound
over-declaration, not a containment break; each operation derives its `impact` exactly
against the working state it sees. Containment is well-defined for every legal config
because the shipped `_check_c6` treats a NULL tracked cell as a clean round-trip failure
rather than letting its codec raise — an invariant this design relies on, not a change it
makes — so nulling a numeric tracked `prop__` cell re-validates cleanly instead of
crashing.

Containment holds **universally**; the corrupter family additionally asserts **set
equality on the curated recipe fixtures** (configs where every declared code fires),
giving graders a strong equality guarantee without resting the whole family on a brittle
bidirectional contract. The precondition — a conformant source — is enforced by the
engine: `corrupt_emit` runs `conformance.validate` on the source emit up front and raises
`CorruptValidationError` naming the failing check ids when any C1–C12 check fails, before
any table is materialized or written.

### Determinism, canonical ordering, and the config fingerprint

The whole run is a pure function of `(source sidecar identity, corrupter config, code
version)`. No wall-clock time appears anywhere in any output artifact. `run.duckdb` is
written in canonical content order (deterministic logical row order; its *binary*
byte-identity inherits the base format's own binary-determinism caveat — `reader.md` §
Determinism). `base.json` and `defects.json` are byte-identical across runs.

`defects.json`'s three provenance fields are the reproducibility key: `source` binds the
input sidecar's SHA-256 (the existing sidecar-fingerprint convention) plus its
`base_format_version`; `config_fingerprint` is the SHA-256 of the canonicalized corrupter
config; `code_version` is the package version string. The input `run.duckdb` is not
hashed — base DuckDB byte-determinism is not contractually guaranteed, so a data hash
would be an unstable binding; data identity is implied by the determinism relation, not
fingerprinted.

`config_fingerprint` binds the **validated `CorruptConfig`**, not the YAML text, via
`fingerprint_config`: `config.model_dump(mode="json")` with all fields included (no
`exclude_*`) → `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True)` →
SHA-256 hex digest. Binding the parsed model rather than the file means reformatting the
YAML — whitespace, comments, quoting, key order — never changes the fingerprint, because
it never changes the output. `seed`, operation order, and every `target.columns` /
`drop` list order are output-significant and bound; unordered maps (`target.where`,
`rename_to`, `retype_to`) are bound by content with keys sorted, since a `schema_drift`'s
maps resolve simultaneously against the pre-operation schema (§ What each operation
breaks) and so carry no order-dependent meaning to lose. An explicit `name` equal to the
runtime `"{kind}#{index}"` fallback fingerprints differently from an absent `name` — a
deliberately conservative binding, finer than output-equivalence, never coarser.

**Canonical order** for `defects`: the total order `(table, locator-kind rank [column <
row < cell], RowRef.keys tuple, column-or-empty, class, rule, impact)`, where `impact`
compares as its normalized code sequence. Including `impact` makes the key discriminating
for every pair of *distinct* records; the only records that can still tie are
byte-identical before id assignment (the legitimate exact-duplicate-atom case), ordered
among themselves by ascending occurrence ordinal. The serializer writes JSON with sorted
object keys, fixed separators, and a trailing newline.

### The occurrence ordinal, `defect_id`, and the `rule` label

Records sharing `(class, rule, locator)` are distinguished by a 0-based **occurrence
ordinal** — their index within that group in canonical order (impact-inclusive, so the
assignment is deterministic even for a group whose members differ only in `impact`).
`defect_id` is a deterministic, content-derived function of `(class, rule,
canonical-locator-serialization, ordinal)`; it omits `impact`, so the ordinal is what
keeps ids distinct across records sharing `(class, rule, locator)`. It is stable across
re-runs and collision-free within one manifest by construction.

`DefectRecord.rule` is the label of the config operation that requested the injection —
its `name`, or, when absent, the engine's stable fallback `"{kind}#{index}"` (the
operation's 0-based position). `rule` is a grouping key, not an id; it need not be unique
across operations.

### The base-emit writer

`write_base_emit` serializes the final `CorruptState` to `out_dir`. `run.duckdb`: every
working table is written from its Arrow table (untouched tables verbatim, corrupted
tables as the operation output) into a **fresh output DuckDB** — the read-only source is
never touched — in source table order, each table's columns in working-schema order
(source order minus any dropped columns), each table's rows in canonical content order.
`base.json`: the `tables` array is rebuilt **from the written catalog** — each table's
`rows` is the written row count and each column's `{name, type}` is read back from what
was written (this holds whether an operation grows a table, as `duplicate_rows` does, or
shrinks one, as `drop_events` and a `freeze_series` tail do — `rows` is simply the count
actually written), while table-level `category` / `record_kind` / `property` and per-column
`references` / `history_tracked` are carried from the (drift-updated) `WorkingTable.spec`,
joined to the written catalog by post-drift name — never re-looked-up from the source
sidecar by name, so a renamed column carries its metadata on its relabeled spec and a
dropped column drops it. Every other top-level sidecar field (`base_format_version`,
`branches`, `runtime`, `pinned_ids`, `enum_domains`, `record_roles`) is copied verbatim
from the source `Sidecar.raw`.

Because the sidecar is regenerated from the catalog the writer just wrote, **C2 holds by
construction**; because fixed/identity/lifecycle columns are never operation targets,
**C3/C4/C5 hold**; because `branches` is copied and never mutated, **C8 holds**. The
writer runs no conformance check itself and makes no semantic promise — the semantic
breaks the operations injected survive into the output, which is the point. A failure
opening or writing the output DuckDB raises `ExportRuntimeError` (the writer failure
domain), never the reader's `RunDatabaseError`.

### Location and schema ownership of the manifest

`defects.json` is written into the corrupt run's output directory alongside the corrupted
emit. It is logically separable — an educator can withhold it and hand students only
`run.duckdb` + `base.json`. Its JSON Schema is generated from the manifest models via
`DefectManifest.model_json_schema(by_alias=True)` — single source of truth, no
hand-vendoring drift — and checked in as package data at
`corrupters/defect_manifest.schema.json`; it lives in the source tree, never in
`contract/`. A drift-guard test regenerates the schema from the models and asserts
byte-equality with the checked-in file.

On-disk key aliases are pinned: the manifest field `defect_class` serializes to the JSON
key `class` (a Python reserved word) via a pydantic field alias (`alias="class"`, not a
serialization-only alias), with `populate_by_name=True` so both spellings round-trip, and
`by_alias=True` on both `model_dump` and JSON-Schema generation — the plain `alias` is
what makes the generated schema, `defects.json`, the round-trip read, and the
`counts.by_class` grouping key all agree on `class`. No other field is aliased.

## Invariants

1. **Structural preservation.** Corrupt output opens under `open_emit` and passes C1–C5
   and C8; it is a valid `base_format_version: 4` emit.
2. **Break locality.** An operation changes only cells/rows/columns of its `target`;
   every other table and column has identical *content* (same rows, same values) as the
   source — content-locality, not byte-locality, since the base format pins no binary row
   order.
3. **Determinism.** Same emit + same config + same code → the same corruption set, a
   byte-identical `base.json`, and a byte-identical `defects.json`. `run.duckdb`'s logical
   row order is deterministic; its binary byte-identity inherits the base format's own
   caveat.
4. **Manifest authority.** Operations declare their defects; the engine never
   reverse-engineers a clean-vs-corrupted diff. A diff cannot recover the class, the
   originating rule, or the intended conformance impact — only the operation can.
5. **Manifest / validate soundness.** `validate_failing ⊆ impact_union` (excluding
   `beyond-c1-c12`) holds for every C1–C12-conformant source, universally (§ Manifest /
   validate agreement invariant).
6. `Emit.query_arrow` is a faithful, verbatim passthrough — the corrupter's one faithful
   read, and the guarantee every later working-set selection rests on.
7. Arrow write-back preserves each untouched column's source DuckDB type — the load-bearing
   step behind C4 and break locality's "same values" for typed columns.
8. A conformant emit has exactly one branch — the single-branch guard's precondition.
9. **Selector canonicity.** Resolved table order is lexicographically ascending by table
   name — a pure function of the resolved name set. Same emit + same selector → the same
   resolved sequence, always.
10. **Placement determinism.** Every placement decision is a pure function of
    `(pooled population content, placement config, operation RNG stream)` — no
    wall-clock, no scan order, no hash salt. RNG consumption is a fixed function of the
    placement kind and the pooled population size; with `placement` absent, the unit draw
    is the uniform `rng.sample` path.
11. **Volume/placement orthogonality.** `amount` computes over the pooled population size
    before weights; placement can cap the drawn count (the positive-weight population is
    the ceiling) but never raise it.
12. **Impact mirror fidelity.** The family-C impact oracle evaluates the same predicate,
    gates, tie-break, and codec as `_check_c6`; a family-C or `mutate_cells` `C6`
    declaration and the check's verdict can disagree only through the declared
    sound-over-declaration cases (healing; joint participants), never by
    under-declaration. `mutate_cells`'s C12 declaration mirrors `_check_c12`'s actor
    sub-type clause the same way.
13. **Timeline locality.** A family-C operation changes only `history` rows of the series
    its drawn units belong to — removed rows and rewritten `sim_time` cells; `value` and
    every other cell of every kept row is byte-identical to the state the operation began
    with.
14. **Simultaneous rewrite.** Within one family-C or `mutate_cells` operation, every
    predecessor/partner/anchor/donor-pool resolution reads the state the operation began
    with, and all mutations apply as one set. Operation composition happens only between
    operations, in list order.
15. **NULL-invariance.** `mutate_cells` changes no cell's NULL-ness in either direction:
    NULL cells are no-mutation units, and no mutation kind produces NULL — C7 is
    structurally unreachable by this operation.
16. **Type preservation.** Every `mutate_cells` mutation stores back into the column's
    current declared type; `WorkingTable.spec` is never touched by it — C2/C4/C5 hold by
    construction for every mutated cell.
17. **Mis-pointed resolution by construction.** Every `mispoint_reference` donor resolves,
    at the operation's apply time, to ≥ 1 row of the correct target kind on the sole
    `fork_path` — C10 and C7 cannot fail because of this operation.
18. **Point-in-time label soundness.** Every `point_in_time_dangling_reference` defect
    names a donor created strictly after an upper bound on the reference's write time —
    the label is genuinely unresolvable at event time, in every case, including
    references whose exact write time is unknowable from the emit.
19. **Row-set locality.** `delete_rows` and `insert_rows` change table content only by
    removing or appending whole rows of resolved tables; no cell of any surviving or
    pre-existing row changes, and no `WorkingTable.spec` changes.
20. **Phantom isolation.** At its operation's apply time, every phantom `record_id` is
    absent from its kind's id universe: a phantom has no history series, no inbound
    reference, no pin, and never reuses an id any working-state row, cell, or tombstone
    carries — a `DANGLING_ID_PREFIX` sentinel sitting in a dangled cell included.
    `insert_rows`' `beyond-c1-c12` declaration holds by construction for the insertion's
    own act, and no earlier defect is healed or resurrected. Isolation covers the
    id-linked surfaces only: payload inherited from an already-corrupted donor (a
    C7-broken lifecycle pair; an out-of-domain discriminator, cloned or drawn from a
    working donor pool) is not re-declared — the originating defect already names the
    code, the same stance `duplicate_rows` exact mode takes.
21. **Delete-wake soundness.** A `delete_rows` defect's impact, evaluated on the
    post-operation state, names every semantic check the operation's removal set
    trips — Invariant 5 (manifest / validate soundness) is preserved through row
    removal.
22. **Interval locality.** `distort_intervals` changes only the `joined_sim_time` /
    `left_sim_time` cells of its counted rows (`overlap` / `gap`: `left_sim_time`
    only) — no element or member cell, no row count, no `WorkingTable.spec` entry
    changes.
23. **Interval C10 split by construction.** After an `overlap` or `gap` operation, every
    row it touched still satisfies `left_sim_time ≥ joined_sim_time`; after a
    `left_before_join` operation, exactly its counted rows violate it. Reference
    resolution is untouched by all three modes — `left_before_join`'s unconditional
    `C10` is the one new way C10 can fail, and Invariant 5 (manifest / validate
    soundness) holds through it the same way it holds through every other operation's
    declared breaks.
24. **Interval knobless determinism.** Every `distort_intervals` rewrite target is a pure
    function of the operation-start working state; the mode step consumes no RNG.

## Validation Rules

Two phases, by recoverability, mirroring the rest of the package. **Parse-time** failures
raise `ConfigError`; **business-rule** failures raise `CorruptValidationError` at
`validate_corrupt_config`, before any table is read or written.

Parse-time validation is the Pydantic model validators in
[`config/models.py`](../../src/fabulexa_forge/config/models.py):
`Target.exactly_one_selector` (exactly one of `table` / `tables` / `glob` / `category` /
`record_kind` is set; `tables`, when present, is non-empty and names no table twice);
`Target.columns` non-empty and unique; `Amount` exactly one of `rate` (0, 1] / `count`
(≥ 1) — the same model serving `entity_scoped.entities`;
`Distribution.params_match_shape` (uniform sets `low ≤ high` and no normal params; normal
sets `stddev > 0` and no uniform params); the placement validators
(`clusters_and_width_positive`: `clusters ≥ 1`, `width > 0`; `weight_positive`:
`weight > 0`); each operation's `requires_columns` / `no_columns` /
`perturbation_governs_columns` / `table_only_target_and_one_action` (the last also
confines `schema_drift` to the concrete `table` selector); and `CorruptConfig`'s
`operations` non-empty. `DeleteRows.no_columns` forbids `target.columns` (a row removal
touches no specific column); `DuplicateRows.perturbation_governs_columns` allows at most
one of `jitter` / `mutation`, requiring `target.columns` with either and forbidding it
with neither. `StrictBaseModel`
(`extra='forbid'`) on every model surfaces unknown fields; the discriminated unions on
`kind` reject an unknown operation or placement kind at parse time.

`MutationSpec`'s per-kind field constraints: `truncate.max_length ≥ 1`;
`precision_drop.digits ≥ 0`; `scale.factor` finite and not in `{0, 1}`; `case.form` /
`whitespace.where` are closed `Literal`s; `sentinel.value` is a required scalar
(`str | int | float | bool`, never null) and, when float, finite (NaN / ±inf rejected at
parse time — NaN never compares equal, so the no-mutation equality would be ill-defined).

`validate_corrupt_config` does **not** check every operation against the *source*
sidecar — it simulates the catalog evolution the run will perform, folding each
`schema_drift`'s rename/retype/drop into the simulated per-table `TableSpec`s in
operation order (the same evolution `WorkingTable.spec` undergoes at apply time,
computable statically because drift is config-only), and checks each operation against
the schema **as of its position**. A column renamed by an earlier operation is addressable
only by its new name; a retype changes the type later rules see; a dropped column is gone.
Selector resolution runs once against the sidecar table set — the table set is static
across a run — while column-pattern matching, `where`-key existence, placement-column
existence, and eligibility evaluate against each resolved table's simulated schema as of
the operation's position: a column renamed away by an earlier `schema_drift` no longer
matches a pattern; a column renamed *into* pattern range matches. Apply-time resolution
against `WorkingTable.spec` agrees with the simulation by construction. Each rule raises
`CorruptValidationError` naming the operation index.

| Rule | Checks |
|---|---|
| `SelectorResolves` | the target's selector resolves to ≥ 1 sidecar table; every explicit `tables` entry exists |
| `ColumnEntriesMatch` | every `target.columns` entry matches ≥ 1 operation-eligible column in ≥ 1 resolved table, per the simulated schema at this position — a dead entry is a misconfiguration |
| `WhereColumnsExist` | every `target.where` key is a column of ≥ 1 resolved table as of this operation; at apply time, a table lacking a key contributes zero units |
| `PlacementColumnExists` | `correlated.column` / `clustered_temporal.column` is a column of ≥ 1 resolved table at this position, and `clustered_temporal.column` is `BIGINT` in every resolved table that has it; a table lacking the column takes the kind's absent-column weights (§ Placement: weights over units) |
| `EntityScopedRecordId` | with `entity_scoped`, `record_id` is a column of every resolved table |
| `ColumnsExist` | every `schema_drift` `rename_to` / `retype_to` / `drop` key is a column of the operation's one table as of this operation — exact names, no patterns |
| `NullableColumns` | `null_cells` targets only value columns (`prop__*`, `elem__*`, `member__*`, `presentation_id`, `deactivated_at`, `left_sim_time`) — never a structural column |
| `ReferenceColumns` | `dangle_reference` / `mispoint_reference` target only reference columns — a membership `member__<f>__id`, or a records `prop__` column whose `ColumnSpec.references` is set |
| `MutableColumns` | `mutate_cells` targets only the family-wide name class (records `prop__*` without `references`, `presentation_id`, membership `elem__*`, the fixed-category `value` column) narrowed by the mutation kind's type gate — and, for `out_of_domain`, an `enum_domains` gate — per the simulated schema at this position (§ mutate_cells vocabulary and eligibility) |
| `DriftColumnsNonStructural` | `schema_drift` renames/retypes/drops only payload columns, positively enumerated: a records `prop__*` without `references` that is not an `enum_domains` discriminator, or a membership `elem__*`. Every structural column, every reference column, and every discriminator column is ineligible — this positive enumeration is what keeps an eligible column's drift capable of tripping only C6/C11 or nothing, never an unrepresentable structural break, and never C10 (reference columns stay `dangle_reference`'s alone) |
| `DriftRenamePreservesCategory` | a rename target keeps the source column's structural category (`prop__` → `prop__`, `elem__` → `elem__`), so a rename never lands a foreign-category column in the records or membership block. Complete given `DriftColumnsNonStructural` already restricts sources to those two prefixes |
| `JitterColumnsNumeric` | when `jitter` is set, every `target.columns` entry is a numeric (`BIGINT`/`DOUBLE`) payload column (`prop__*`/`elem__*`) — never a `*_sim_time` or other structural column, even when numeric. Perturbing `joined_sim_time`/`left_sim_time` would break C10 and shift the membership row-identity prefix |
| `HistoryOnlyTarget` | for the three family-C kinds (`freeze_series`, `drop_events`, `shift_sim_time`), every resolved table is the fixed-category `history` table |
| `NonHistoryTarget` | `delete_rows`: every resolved table is records- or membership-category — never fixed-category (`history` removal is `drop_events`' alone) |
| `RecordsCategoryTarget` | `insert_rows`: every resolved table is records-category |
| `MembershipOnlyTarget` | `distort_intervals`: every resolved table is membership-category — `history` and records-category tables have no membership intervals |
| `PhantomResampleColumns` | `insert_rows` with `target.columns`: every entry matches ≥ 1 insert-eligible column (records `prop__*` with `references` unset, or `presentation_id`) in ≥ 1 resolved table — a dead entry is a misconfiguration |
| `ConflictMutableColumns` | `duplicate_rows` with `mutation`: every `target.columns` entry matches ≥ 1 conflict-eligible column (the records/membership members of the `mutate_cells` name class, narrowed by the mutation kind's type gate and, for `out_of_domain`, the `enum_domains` gate) in ≥ 1 resolved table |

The eligibility classes behind `NullableColumns` / `ReferenceColumns` /
`JitterColumnsNumeric` / `MutableColumns` are also the match domain for `target.columns`
entries (§ Column entries: exact names and patterns). Zero-row populations, all-zero
weight vectors, and `floor`-to-zero quantities are data-dependent no-ops, never errors.

Three checks are data-dependent and run at apply time, before any output is written:
**`schema_drift` retype validity**, decided by the DuckDB `CAST` that performs the retype
(the reader carries no DuckDB-type registry to check a literal against, and a silent
`TRY_CAST`-to-NULL would fabricate data) — an unrecognized type or impossible cast raises
`CorruptValidationError` there; **`mutate_cells` sentinel representability**, the same
stance applied to an author literal — the `sentinel.value` is rendered into each resolved
column's current type by the shared DuckDB cast, once per (table, column), and an
unrepresentable literal raises `CorruptValidationError` naming the operation index, table,
and column; and **the total-erasure guard** — after the last operation, `corrupt_emit`
refuses a working set left with zero rows across every table, since such an output would
carry no row bearing the branch's `fork_path` and fail C8's data/sidecar set-equality, the
one composition (row-set operations plus family-C erasure) where structural preservation
cannot hold by construction. `CorruptValidationError` names the condition; this is the
sole apply-time guard on the run's total output rather than any one operation's draw —
zero-row *populations* remain data-dependent no-ops everywhere else, never errors.
Cross-operation column existence is decided up front by the evolved-schema simulation;
only the cast and the row-count check themselves need the actual data.

The manifest carries its own, driver-level rules, checked on the assembled manifest by
`build_defect_manifest` — build invariants that cannot trip given well-formed
`DefectRecord`s, raising `CorruptError` (⊂ `ExporterError`) so the CLI funnel catches them
rather than surfacing a bare `ValueError`:

| Rule | Checks |
|---|---|
| `CanonicalOrder` | `defects` are in the canonical total order — enforced by reordering during build, not by raising |
| `UniqueDefectId` | every `defect_id` is unique within the manifest |
| `WellFormedTableName` | `location.table` matches `^history$` \| `^records__<seg>$` \| `^membership__<seg>__<seg>$` (`<seg>` the same single-underscore snake_case as `defect_class`) — purely lexical, no sidecar consulted, keeping `build_defect_manifest` a pure function of its arguments |
| `RowRefCategoryMatchesTable` | `RowRef.category` equals the category the table name implies |
| `NoExistenceRequirement` | locators are **not** required to resolve to a live entity — a dropped-column defect intentionally names a removed entity by its source coordinate. Not a check; documented as a non-check |

The parse-time invariants on `DefectRecord` / `RowRef` (non-empty `impact`, well-shaped
`defect_class`, a `RowRef` prefix matching its category) are preconditions here, already
guaranteed by construction, not re-checked. The **manifest / validate agreement
invariant** itself is not enforced at build time — it is a testable property the
corrupter family's tests assert directly against `validate`'s output.

## Rationale

- **One doc, one seam.** The engine and the manifest share exactly one contract — the
  `DefectRecord` shape an operation emits and the engine assembles — and nothing else
  crosses between them. Splitting them into two docs would misrepresent an internal
  seam as a cross-document contract; keeping them together states that boundary
  correctly.
- **Canonical content order, not physical position, for selection and writing.** DuckDB
  scan order is not byte-stable, so a selector or writer keyed on physical row position
  would make the whole corruption set non-reproducible across runs. Ordering by content
  (every column ascending, NULLS FIRST) is a pure function of the data, so selection and
  the written row order are deterministic regardless of scan order; the only residual
  ties are byte-identical rows, which are interchangeable by construction.
- **The fingerprint binds the parsed model, not the YAML text.** A config's *meaning* is
  what determines its output; reformatting whitespace, comments, or (for unordered maps)
  key order does not change the output, so it must not change the fingerprint. Binding
  the fully-defaulted `model_dump` (not `exclude_defaults`) keeps an author-omitted
  optional field and an explicit-but-equal value from ever fingerprinting differently.
- **Soundness (containment), not full bidirectional equality, is the load-bearing
  manifest/validate guarantee.** Cross-operation healing (a later null completing an
  all-NULL C7 pair an earlier null half-broke) is a real, legal sequence the grammar
  allows, and it produces a sound over-declaration, not an error. Demanding exact
  equality universally would either forbid a legal operation sequence or force silent
  under-declaration; asserting equality only on curated recipe fixtures gets graders the
  stronger guarantee where it is achievable without weakening the family's core
  commitment everywhere else.
- **Locators are base coordinates, not physical-row pointers**, because the base format
  itself does not guarantee unique row identity and some defects (a dropped column, an
  exact duplicate) remove or multiply the entity being labeled. A coordinate scheme that
  degrades gracefully — resolving to a group of candidate rows for membership, or to a
  removed entity's source name for a drop — is honest about that; a scheme that
  promised unique resolution would be lying about the data model it labels.
- **`schema_drift`'s rename/retype/drop maps apply as one atomic, order-independent
  transform**, resolved against the pre-operation schema, not chained left-to-right. This
  is what makes sorting their keys in the config fingerprint sound — an order-dependent
  transform would make key-sort semantically lossy, but an order-independent one loses
  nothing by it.
- **DuckDB's `CAST` is the retype-validity authority**, not a hand-rolled type registry.
  The reader carries no such registry, and a silent `TRY_CAST`-to-NULL on an invalid
  target type would fabricate data (forbidden by Principle #3) rather than fail loudly.
  Deferring to the same cast that performs the retype means the validity check and the
  retype itself can never disagree.
- **`render_typed_literal` is reused from the dimensional exporter, not reimplemented.**
  It is the one coercion oracle the exporters already trust for typed equality; a second,
  pyarrow-native equality path could silently disagree with it on `DOUBLE` / `DECIMAL` /
  `BOOLEAN` / VARCHAR-quoting and shift the matched population without either path
  raising an error.
- **A zero-table selector is a validate-time error; a zero-row population is a no-op.** A
  selector resolving to nothing means the config asks to corrupt something the emit does
  not have — a misconfiguration, caught before any write. A `where` that matches no rows
  is a property of the data, not of the config, and stays a silent no-op. The line
  between the two is config-vs-data.
- **Column entries, `where` keys, and placement columns are lenient across the resolved
  set, with a dead-config backstop.** A class profile stays portable across emits when
  only some kinds carry a given column — a table lacking it contributes no units (or
  takes the placement kind's absent-column weight). An entry or column matching nowhere
  in the whole resolved set is dead config, rejected at validate time. Per-table
  strictness would make class profiles unusable; blanket silence would hide typos.
- **`amount` pools across the resolved set.** Defect volume is a property of the whole
  target — a rate names a fraction of the class, and how it lands across tables follows
  from the draw. A per-table quota would re-couple the config to one emit's table list,
  defeating class targeting.
- **MNAR is a weighted draw with an exact total, not a per-unit Bernoulli.**
  `correlated.weight` biases where defects land while `amount` keeps its exactness
  invariant, so one sampling mechanism (Efraimidis–Spirakis) serves all three placement
  kinds and the missingness is still genuinely conditional on the column. A per-unit
  independent-probability quantity would surrender exactness — and with it the
  determinism story `amount` anchors — for no realism gain.
- **Placement weights are derived at row granularity.** Every placement kind is a
  property of the unit's row — its entity, its timestamp, its condition column — so a
  cell unit inherits its row's weight; a per-cell weight vocabulary would add surface no
  placement kind needs.
- **The weighted draw spends one uniform per pooled unit, zero-weight included**, so RNG
  consumption depends only on the pooled population size — reweighting a placement
  config never shifts which underlying uniforms feed which units, keeping draws
  comparable across config variants over the same population.
- **One `shift_sim_time` with modes, not three operations.** Offset, collide, and swap
  are all `sim_time` rewrites differing only in how the new tick derives; they share
  target, amount, placement, and locator shape. Distinct `defect_class` values keep the
  grader vocabulary sharp — the same pattern as `duplicate_rows`' exact/near split.
- **`freeze_series` is its own kind.** It samples a different unit (series), needs the
  `cut` knob, and tells a different teaching story (staleness) than lost messages
  (`drop_events`); folding it into `drop_events` would overload one operation with two
  unit vocabularies.
- **No `duplicate_event` kind.** `duplicate_rows` exact on `history` already injects the
  duplicate tick; a family-C twin would be a second spelling of the same defect (a
  recipe, not an operation).
- **One `mispoint_reference`, not two, for the mis-point and the point-in-time dangle.**
  Unconstrained mis-pointing and point-in-time dangling differ only in one donor-pool
  predicate — same population, same draw, same rewrite, same impact rule — so a
  `constraint` flag distinguishes them rather than a second operation; the distinct
  `defect_class` per mode keeps `defects.json` teaching-grade without the config in hand
  (the same one-kind-many-modes stance `shift_sim_time` takes).
- **`constraint` is a flat `Literal`, not a one-member discriminated union.** No shipped
  constraint carries parameters; a one-member union would be scaffolding for a
  hypothetical future (Principle #8). A parameterized constraint, if one ever ships,
  follows the `ExportConfig.mode` widening precedent.
- **The impact oracle mirrors `_check_c6`'s implementation predicate** — the same
  pre-slice gate, `(sim_time DESC, value DESC)` tie-break, codec, and skip gates —
  extending the shipped retype rule's stance (the same codec, on the same gate, C6
  itself evaluates) from values to sequence. This is what keeps differing-value tick
  collisions at the anchor decidable: C6's tie-break is deterministic, so the label can
  mirror it instead of forbidding the case.
- **Locators are coordinates, not diffs.** Moved rows carry post-corruption coordinates
  (the renamed-column precedent); removed rows carry source coordinates (the
  dropped-column precedent). The pre-shift tick is not recorded anywhere — no manifest
  field records any before-value, and `sim_time` being part of the row identity does not
  change that stance.
- **Simultaneous within an operation.** Resolving every predecessor/partner against the
  operation's starting state and applying one atomic rewrite set (the `schema_drift` map
  precedent) keeps the outcome a function of the selected set alone; sequential
  application would make interacting units' results depend on draw order. The one
  residual order sensitivity — chained swap pairs — is closed by the fixed
  ascending-order skip rule, so the operation stays a deterministic function of the
  selected set and the fingerprint semantics are unchanged.
- **No mutation, no defect, no unit.** A zero-rounded offset delta, an equal-value swap,
  and a skipped chained swap change nothing; the manifest names injected defects, never
  sampled intents, so none emits a record and none counts — one rule for every
  data/RNG-dependent no-op. Swap conflicts skip rather than fail because whether two
  selected units chain is decided by data and the draw, not by the config — failing the
  run would make a valid config's success depend on the seed.
- **Mutating `sim_time` is structurally legal and stays confined to family C.** C4 pins
  the history columns' shape, not their values. The shipped rules (`NullableColumns`,
  `JitterColumnsNumeric`, `DriftColumnsNonStructural`, `MutableColumns`) keep every other
  operation away from structural columns; family C is the deliberate, declared exception
  for `sim_time` values only — `value`, `kind`, `record_id`, `property`, and `fork_path`
  cells are never rewritten by any family-C operation.
- **One `mutate_cells`, eleven mutation kinds — not eleven operations.** The same
  parameterizations-not-features stance `shift_sim_time`'s modes take: one `kind:
  mutate_cells` with a `MutationSpec` union keeps the `CorruptOperation` union readable
  and makes every mutation automatically inherit the selector / amount / placement
  grammar and any future grammar growth.
- **Author-specified sentinels.** Principle #7: the corrupter never invents the value an
  author should choose. `-999` is realistic in one domain, `1900-01-01` in another; a
  built-in sentinel table would be the banned domain knowledge in disguise. One YAML line
  keeps it honest, and recipes carry the culture.
- **NULL-invariance instead of a sentinel-over-NULL mode.** Letting `sentinel` overwrite
  NULLs would blur the boundary with `null_cells` (two operations owning missingness) and
  make C7 reachable from `mutate_cells`. Excluding it keeps each operation's conformance
  reach small and provable, and matches the shipped jitter-skips-NULL stance.
- **Reference columns excluded.** Resampling a reference id is `mispoint_reference`'s
  territory (§ What each operation breaks, and the impact it declares) — its own
  resolution preconditions (a donor-pool filter, an optional write-anchor constraint)
  belong with that operation, not duplicated here.
- **The full C6 oracle, not a simpler heuristic.** `mutate_cells` reuses
  `series_round_trip_fails` because it is exact and extends the impact-mirror-fidelity
  invariant — a mutation that coincidentally restores the anchor's codec text correctly
  declares `beyond-c1-c12` instead of a false `C6`.
- **Anchor participation for `history.value` — operation-start or post-operation.** A
  post-state round-trip check alone would let an unrelated defect inherit a `C6` label
  from a *prior* operation's break. A rewrite is a removal plus an insertion, so
  participation is the union of family C's two shapes: the mutated row held the anchor in
  the operation-start state (the removal side) or holds it in the post-operation state
  (the insertion side). Either alone under-declares: post-only misses the demotion break,
  pre-only misses a non-anchor row mutated above the anchor. The union is causally
  complete — mutating a row that holds the anchor in neither state leaves that anchor,
  row and value, untouched.
- **Overflow: loud for `scale`, cannot-apply for `typo`.** Loud overflow is for
  author-parameterized stores: `scale.factor` is the author's knob — overflow means the
  parameterization is wrong for that column, and the author can fix it (the same family
  as the `sentinel` cast oracle and family C's jitter store). `typo` has no knob: a digit
  exchange that leaves the int64 domain (possible only for 19-digit near-max values) is a
  property of one drawn cell, seed-dependent and unfixable in config — a loud failure
  would crash a run on a condition the author cannot configure away. It joins "fewer than
  two digits" and "equal neighbors" as a third cannot-apply case: nothing is written, no
  defect is claimed, and the RNG cost is already paid.
- **Uniform resample; donor pool from the whole table.** Uniform-over-distinct is the
  smallest deterministic contract. The pool ignores `target.where` for the same reason a
  swap partner ignores it: the realistic donor universe is the column's data, not the
  sampling filter.
- **`format_dirt` is one narrow, total transform.** Locale dirt in general requires
  interpreting content; thousands-grouping of an all-digit string is the one form that is
  mechanical, total, and type-safe in a VARCHAR cell. Wider styles (currency glyphs, date
  reformats) are content-interpretive and deliberately not designed.
- **`out_of_domain`'s escalating fallback.** A typo'd category is the realistic defect,
  but adjacent transposition alone is not total (single-char values, domains closed under
  transposition). Appending the final character repeatedly is guaranteed to leave any
  finite domain, keeps the output visibly derived from the real value, and stays
  deterministic.
- **Eleven defect classes, not one `mutated_value`.** The manifest's intent label is the
  teaching narration; a grader filtering for sentinel defects should not need to parse
  operation configs. The class vocabulary is open, so the cost is zero.
- **`delete_rows`' pin rule reads post-operation survivor count, not pre-operation row
  count.** A conformant source carries exactly one row per records `record_id`; only
  `duplicate_rows` can raise that count, and it declares its own `C9` when it does.
  Evaluating the wake against the state after the operation's own removals means
  deleting one of two copies of a pinned row restores the count to one and declares no
  code — the earlier duplicate's `C9` stands as a sound over-declaration, the same
  healing stance every cross-operation interaction in this family takes, rather than a
  rule keyed to a snapshot the working set has already moved past.
- **A phantom id is a plausible transposition, not a sentinel.** `insert_rows` derives
  each fresh `record_id` from its donor by adjacent-character exchange rather than a
  `DANGLING_ID_PREFIX`-style marker, because a phantom's teaching value is looking real —
  a ghost record an author must *notice*, not one flagged by its own id shape. The
  escalating fallback (repeated final-character append) guarantees termination without
  ever reusing a real id.
- **One `distort_intervals` kind with a `mode` union, not three kinds.** The three
  distortions share everything but the unit filter and rewrite rule: membership-only
  confinement, the timing-column write surface, knobless determinism, the locator
  shape. `shift_sim_time` set the precedent; three kinds would triplicate a business
  rule and a registry entry for no expressive gain.
- **Boundary perturbation, not interval splitting.** Splitting an interval into two rows
  drags in the row-count sidecar co-write and duplicate-row semantics, and no target
  scenario needs it: `overlap` needs a pair (exists or doesn't), `gap` needs lost
  coverage (shrinking delivers it), `left_before_join` needs one row. Cardinality
  preservation keeps the operation in the cell-rewrite class, where structural
  preservation is free.
- **Knobless, data-derived rewrite targets.** A magnitude knob (a `Distribution`, an
  `extent` fraction) would need clamping rules to keep C10 green for `overlap`/`gap` and
  to keep the overlap actually overlapping — complexity with no pedagogical payoff, since
  detection exercises *existence* of the defect, not its size. Midpoints give
  substantial, always-valid distortions as a pure function of the data, the same
  `collide`/`swap` precedent. A magnitude axis could be added later as an optional field
  without breaking the knobless form.
- **`gap` operates on single closed intervals, not contiguous pairs.** Producer
  timelines need not be contiguous (a member may leave and rejoin later), so a
  contiguous-pair population could be near-empty on real emits. Shrinking any closed
  interval fabricates recorded absence during true presence — the gap defect in its
  general form; when a successor interval exists, the result is exactly the
  hole-between-versions teaching case. This also keeps `gap`'s population rich on every
  emit that has closed intervals.
- **`left_before_join` swaps rather than reflecting or offsetting.** The swap *is* the
  real-world defect (timing columns crossed in an ETL), is knobless, guarantees a strict
  C10 violation from the strict `left > join` filter, and needs no range analysis. It
  rewrites `joined_sim_time` — a `RowRef` prefix member — so its defect carries the
  post-corruption coordinate, the stance `shift_sim_time` already established for
  rewritten identity columns.
- **`overlap`/`gap` mutate only `left_sim_time`.** Both distortions have a two-sided
  formulation (move the successor's join instead); mutating only the left boundary keeps
  `joined_sim_time` — the membership `RowRef` prefix member — stable, so their defect
  coordinates are stable across the corruption, and the write surface stays minimal.
- **Pair units key on the earlier row.** Gives at most one pair per mutated row (no write
  conflicts), a well-defined placement weight (the bitten row), and a stable defect
  locator.
- **The dangling member is not re-delivered by `distort_intervals`.** `dangle_reference`
  on a membership `member__<f>__id` already declares `C10` with a guaranteed-absent
  sentinel; a second mechanism would create two rules for one defect.

## Boundaries

What the corrupter family deliberately does not own:

- **Per-unit Bernoulli `probability`.** `Amount` carries `rate` and `count`; a per-unit
  independent-probability draw is not a supported quantity kind. Biased likelihood is
  expressed through `placement`, which preserves `amount`'s exactness (§ Rationale).
- **String edit-distance ("typo") jitter.** Near-duplicate perturbation is numeric-additive
  only, via `Distribution`; no string-mutation perturbation exists.
- **C12 breakage is narrow.** `mutate_cells`'s `out_of_domain` kind is the only operation
  that can trip C12 — mutating a tracked `records__actor.prop__actor_type` cell outside
  its declared sub-types (§ mutate_cells' impact rule: mirroring C6 and C12). No
  `null_cells`, `duplicate_rows`, `schema_drift`, `dangle_reference`, or family-C defect
  reaches it: C12 reads `record_roles` — copied verbatim by the writer — against the
  sub-type discriminator's data, skips when the discriminator column is absent, and
  ignores NULL discriminator values.
- **Reference-column mutation.** `mutate_cells`'s name class excludes every reference
  column — a records `prop__` with `references` set, and every membership `member__*` —
  the same territory `schema_drift` stays out of. Resampling a reference id is
  `mispoint_reference`'s territory, not family A's.
- **Cross-kind mis-pointing.** `mispoint_reference` never rewrites a membership row's
  `kind` partner — mis-pointing is within-kind only. A cross-kind mis-point would need a
  paired two-cell write with different preconditions and is not a shipped capability.
- **`pinned_ids` untouched by `mispoint_reference`.** A donor draw may land on a pinned
  id — a legal wrong-but-real target, C9 unaffected. Breaking pin resolution itself
  requires removing or re-keying pinned rows — cardinality-changing, family-B territory,
  out of reach for a cell-rewrite operation.
- **`created_after_reference` is `mispoint_reference`'s only constraint.** No "created
  before", no windowed constraints, no cross-column predicates — none has a teaching
  scenario demanding it.
- **Cross-column mutation.** No `mutate_cells` kind reads or writes more than one column;
  a field swap (two same-type columns exchanged) is a different mechanism shape — a
  two-column unit — left to a future family-A extension.
- **Biased resample.** `mutate_cells`'s `resample` draw is uniform over the distinct
  donor pool; a biased "far" resample (the statistical-outlier parameterization) is not a
  supported quantity kind.
- **No restore / clean verb.** The manifest is a terminal, label-grade artifact with a
  published schema, consumed by external tooling and human graders. No verb reads a
  manifest back to reconstruct clean data.
- **Trunk-only.** Corrupters run on the single-branch sanitised subset; multi-branch
  export and provenance lineage are parked, as they are for every other mode (see
  [`README.md`](README.md) § Staged roadmap).
- **No dependency on the bundle's producer.** The only inputs are the emit and the
  vendored `contract/`; fabricating a dangling-reference sentinel (the Principle #3
  exception) needs nothing upstream.
- **Reference-column drift.** `schema_drift` never renames or drops a reference column
  (a records `prop__` with `references` set, or a membership `member__<f>__kind`/`id`
  pair) — `DriftColumnsNonStructural` excludes them, leaving referential breakage entirely
  to `dangle_reference` and `mispoint_reference`.
- **`delete_rows` never targets `history`.** Row removal from the fixed-category table is
  `drop_events`' alone (`NonHistoryTarget`); a differing-value duplicate tick is reachable
  only by composition (`duplicate_rows` exact then `mutate_cells` on `history.value`) or
  `shift_sim_time collide` — `duplicate_rows`' `mutation` mode has no eligible `history`
  column (`history.value` is not conflict-eligible; § `duplicate_rows` — the `mutation`
  mode).
- **No interior gap within one interval.** Opening a gap in the middle of a single
  interval requires splitting a row in two — a cardinality-changing mechanism
  `distort_intervals` deliberately excludes (§ Rationale). A future need is a new mode
  with the row-count co-write, not a change to the three shipped ones.
- **No magnitude / extent knob on `distort_intervals`.** Distortion sizes are
  data-derived (midpoints); an author-tunable magnitude is a possible future optional
  field, designed only when a scenario demands it.
- **No membership-row insertion or deletion via `distort_intervals`.** Phantom intervals
  and interval removal remain `insert_rows`' (records-only today) and `delete_rows`'
  territory; `distort_intervals` never changes row counts.
- **No `history` or records-category targeting for `distort_intervals`.** Temporal
  distortion of `history` is family C's alone; `MembershipOnlyTarget` enforces the split.
- **No cross-timeline distortion.** `overlap` is defined within one member timeline;
  overlapping intervals of *different* members are legal data, not a defect.
- **Nulling `left_sim_time` stays `null_cells`' defect** (`missing_value`, with its
  C7-adjacent rules); every `distort_intervals` mode's population excludes NULL-left
  rows.

## Related

| Document | Why |
|---|---|
| [`../../CLAUDE.md`](../../CLAUDE.md) | Principles (#1 domain-agnostic, #3 the corrupter exception, #7 no invented defaults, #8 no scaffolding, #9 the base contract is not ours to extend), the boundary, vocabulary |
| [`../../contract/base-format.md`](../../contract/base-format.md) | The base format the writer regenerates and the operations break; base row identity columns and the duplicate-tick / multiplicity legality the locator scheme accounts for |
| [`reader.md`](reader.md) | `Emit.query_arrow` (the one faithful materialization), the typed `Sidecar` the selector reads metadata from, the `to_csv_text` codec `RowRef` renders through, and the row-order / binary-determinism caveat |
| [`conformance.md`](conformance.md) | The C1–C12 split (structural preserved, semantic broken) this design targets, and the C1–C12-is-narrower-than-QA boundary the `impact` field and `beyond-c1-c12` sentinel build on |
| [`config-docstrings.md`](config-docstrings.md) | The three-channel docstring convention the corrupter config models adopt |
| [`../CAPABILITIES.md`](../CAPABILITIES.md) | Stage 4 corrupter inventory and status |
| [`README.md`](README.md) | Design index, package layout, staged roadmap |
