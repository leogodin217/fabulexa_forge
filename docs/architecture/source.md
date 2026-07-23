# Source Exporter

**Status:** Implemented. Code is the contract — see
[`exporters/source/`](../../src/fabulexa_forge/exporters/source/)
(`plan.py`, `renders.py`, `engine.py`, `columns.py`),
[`derivations/state_at.py`](../../src/fabulexa_forge/derivations/state_at.py),
[`config/models.py`](../../src/fabulexa_forge/config/models.py) (`SourceConfig`,
`RenameEntry`), and
[`tests/exporters/source/`](../../tests/exporters/source/),
[`tests/derivations/test_state_at.py`](../../tests/derivations/test_state_at.py),
[`tests/integration/test_corrupt_source.py`](../../tests/integration/test_corrupt_source.py).
Public API: [`exporters/source/engine.py`](../../src/fabulexa_forge/exporters/source/engine.py)
(`export_source`, `build_source_query_specs`) and
[`exporters/source/plan.py`](../../src/fabulexa_forge/exporters/source/plan.py)
(`build_source_plan`).

The `mode: source` exporter renders the entire emit as one faithful operational
dump — the tables a CRM / OMS / inventory system's extract job would land — so the
consumer builds the warehouse themselves. A bare `mode: source` is a complete, valid
config: every table in the emit lands as its operationally correct genre, classified
entirely from the sidecar (`record_roles` × `history_tracked`) with no author
declaration, no domain branching, and no per-emit code. Where the dimensional mode
hands the consumer a reconstructed star schema, source hands over the raw operational
shape a star schema is built *from* — the change-log / reference / transaction /
junction tables an OLTP source system's nightly extract lands, from which the
consumer builds current state (`MAX`-per-id), SCD-2 (`LEAD`), and a star schema
(joins on FK columns) themselves. The two modes read the same emit and sit at
opposite ends of the ETL pipeline it teaches.

```
emit (run.duckdb + base.json @ the supported `base_format_version`)
   │  (reader: Emit + Sidecar; trunk-only — sole branch)
   ▼
records__<kind> ──┬─ any history_tracked column ──▶ change-log table   (wide CDC, op = c/u/d)
                  ├─ untracked + dimension role ──▶ reference table    (current state)
                  └─ untracked + fact role      ──▶ transaction table  (FK columns prominent)
membership__<K>__<p> ─────────────────────────────▶ junction table    (owner, member, joined/left)
   │  presentation (prefix-stripped names, record_id → id) ▸ exclude ▸ rename ▸ collision check
   ▼
build_source_plan → one SourceTableSpec per output table
   ▼  build_source_query_specs (optionally windowed)
        changelog   → row-state-events fold (or state-at snapshot under change_delivery: snapshot)
        reference   → faithful records relation, full snapshot every window
        transaction → faithful records relation, append by last_mutation_sim_time
        junction    → faithful membership relation, extract-on-change
   ▼
writers (CSV | DuckDB — both via Emit.query_arrow)
```

---

## Surface

| Module | Owns |
|---|---|
| [`config/models.py`](../../src/fabulexa_forge/config/models.py) | `ExportConfig.mode: Literal["dimensional", "source", "base"]`, `ExportConfig.source: SourceConfig \| None`; `SourceConfig` (`change_delivery`, `exclude`, `rename`) and `RenameEntry`; the two-sided `mode_section_matches` validator and `SourceConfig` / `RenameEntry`'s own parse-time validators |
| [`exporters/source/plan.py`](../../src/fabulexa_forge/exporters/source/plan.py) | `SourceTableSpec`; `build_source_plan` — the genre trichotomy classification, the untracked-only sub-type split, `exclude`, presentation defaults (delivery-dependent for a change-log kind), `rename` resolution, the collision and reserved-name checks |
| [`exporters/source/columns.py`](../../src/fabulexa_forge/exporters/source/columns.py) | The shared `prop__<p>` scalar-property lookup `plan.py` and `renders.py` both need, so neither duplicates it |
| [`exporters/source/renders.py`](../../src/fabulexa_forge/exporters/source/renders.py) | `build_render_sql` / `build_snapshot_render_sql` — the four genre renders (change-log via the row-state-events fold, reference/transaction via the faithful records relation, junction via the faithful membership relation, snapshot via the state-at derivation), each carrying its genre's total `ORDER BY` and wallclock rendering through the shared anchor renderer |
| [`exporters/source/engine.py`](../../src/fabulexa_forge/exporters/source/engine.py) | `export_source`, `build_source_query_specs` — plan → per-genre render → optional windowing (`write_mode` by genre) → dispatch to the shared writer. `build_source_query_specs` is the pure compile surface: it takes a required `base_relations: Mapping[str, str] \| None` (the full-export and windowed callers pass `None`; the playback seam's tier-2 `state` passes a truncated-relation mapping — see [`playback.md`](playback.md) § The compile indirection) |
| [`exporters/query_spec.py`](../../src/fabulexa_forge/exporters/query_spec.py) | `QuerySpec`, `write_query_specs` — the mode-neutral compiled-table shape and full-export write dispatch every mode's `export_*` entry point shares. Relocated out of the dimensional engine so a second mode can compile to the same writer-ready shape without a cross-mode import |
| [`exporters/reserved_names.py`](../../src/fabulexa_forge/exporters/reserved_names.py) | `is_reserved_table_name` / `is_reserved_column_name` — the cross-mode bookkeeping-name check both dimensional's and source's plan-time collision resolution call |
| [`derivations/state_at.py`](../../src/fabulexa_forge/derivations/state_at.py) | `build_state_at_sql`, `STATE_AT_COLUMNS` — the point-in-time row reconstruction snapshot delivery composes; owned by [`derivations.md`](derivations.md) § The state-at derivation |
| [`derivations/row_state_events.py`](../../src/fabulexa_forge/derivations/row_state_events.py) | `build_row_state_events_sql` — the change-log render's composed fold; owned by [`derivations.md`](derivations.md) § The row-state-events derivation |
| [`errors.py`](../../src/fabulexa_forge/errors.py) | The `Source*` error hierarchy (`ExportError` subclasses) |
| [`cli.py`](../../src/fabulexa_forge/cli.py) | `cmd_export` — dispatches on `config.mode` to `export_dimensional` or `export_source` |

## Boundary

- **Input.** An open `Emit` (trunk-only — sole branch), a validated `ExportConfig`
  with `mode: source`, the resolved `EffectiveAnchor` (or `None` — checked as a
  business rule, not tolerated as a silent fallback), the `fmt`, and an optional
  `Window` for an incremental invocation.
- **Output.** Per `fmt`: one `<table>.csv` per output table into the output
  directory, or one typed table per output table in a single `.duckdb` file — both
  through the shared writer dispatch (`exporters/query_spec.py`). A zero-row table
  is still emitted.
- **Reader-first; no base-table SQL authored directly.** Every base read is an
  embedded reader relation (`build_records_relation_sql`,
  `build_membership_relation_sql`) or a derivations-layer fold (row-state-events,
  state-at) — the mode composes, never hand-writes `FROM records__…`.
- **Forbidden imports.** `exporters.source` never imports `exporters.dimensional`
  or `exporters.streaming`, and neither imports it back — the mode packages are
  independent leaves composing only the reader, the derivations layer, and the
  mode-neutral `exporters.query_spec` / `exporters.reserved_names` modules. No
  dependency on the bundle's producer; the vendored `contract/` is the only
  coupling.

## Semantics

### Classification: the genre trichotomy from the sidecar

Every `records__<kind>` table resolves to exactly one genre from two sidecar facts —
the kind's per-column `temporal_class` declarations and its `record_roles` entry.
Tracked-ness dominates: a kind whose values genuinely change over time exports as
its change log, whatever its warehouse role, because classifying it as a snapshot
would silently drop base-layer history rows (a fidelity violation). Role then splits
the untracked kinds.

| Condition (in precedence order) | Genre | One output row = |
|---|---|---|
| Any `prop__` column of the kind is `temporal_class: "tracked"` | **change-log** | one record state-change event (`c`/`u`/`d`) |
| No tracked column; resolved role `dimension` | **reference** | one record, current state |
| No tracked column; resolved role `fact` | **transaction** | one record, current state, FK columns prominent |

**Tracked-ness keys on the class, not on the `history_tracked` bit.** Every
presentation column is `history_tracked: true`, but one bound to an immutable source
is class `constant` and holds exactly its genesis `history` row
([`bundle.md`](bundle.md) § Column temporal classes) — a predicate keyed on the bit
would classify a kind carrying only such a column as change-log genre and render a
change log with no changes. A kind is tracked **iff** any of its `prop__` columns is
`temporal_class: "tracked"`: a kind whose only tracked column is a presentation
value is change-log genre — faithful, since a name that genuinely changes over time
*is* a change log.

| Kind's `prop__` columns | Tracked? | Genre |
|---|---|---|
| No column carries `history_tracked` | no | reference / transaction (by role) |
| Every history-tracked column is class `constant` | no | reference / transaction (by role) |
| Any column is class `tracked` | yes | change-log |
| A history-tracked column declares no class, or one outside the enum | — | `TemporalClassUnavailableError` |

Only a `history_tracked: true` column can be class `tracked` (the contract
constrains it), so the predicate consults the class — always through the sidecar's
`temporal_class` accessor, the single narrowing point
([`reader.md`](reader.md) § Per-column temporal semantics) — only for the columns
carrying the bit, under the `is True` convention (exactly `True` is flagged). The
first row mirrors C11's and C13's skip guard rather than any legal shape:
coverage is total, and the version gate refuses an emit predating the attributes
before the predicate ever runs; the guard is retained so the predicate is a correct
standalone implementation — a kind with nothing flagged needs no class to be
classified. The refusal is one-directional, deliberately so: a column declaring a
`temporal_class` with **no** `history_tracked` is never consulted — the predicate
classifies the kind from the flagged columns alone, the contract-consistent reading
(only a flagged column can be `tracked`), not a guess; the broken pairing is C13's
to report, and `validate` names it. A `slice_only` column is
`history_tracked: false` and is therefore never consulted here (see Boundaries).
The predicate is `_is_kind_tracked` in
[`exporters/source/plan.py`](../../src/fabulexa_forge/exporters/source/plan.py).

Every `membership__<K>__<p>` table resolves to the fourth genre unconditionally:

| Table | Genre | One output row = |
|---|---|---|
| `membership__<K>__<p>` | **junction** | one membership interval (owner, member, joined/left) |

`history` is consumed by the change-log render and never passed through. The three
sidecar table categories are exhaustive at the supported `base_format_version`, so
this classification is total: the whole emit is covered, nothing else exists to
classify.

Classification requires the sidecar to carry the `record_roles` registry and the
per-column temporal attributes, and every **untracked** exported kind — and
every declared sub-type of an untracked object-registry kind — to resolve a role (a
tracked kind classifies as its change log regardless of role); there are no
inference fallbacks (§ Validation Rules).

### The sub-type split

Tracked-ness is resolved first, and it is a **kind-level** fact (any `prop__`
column of class `tracked`): a tracked kind is a single change-log table
whatever its role-registry shape — its units would share one genre, so nothing
forces a split, and if the kind is sub-typed its `<kind>_type` discriminator is
**retained** as a column (single-table inheritance). The role registry's asymmetric
shape drives an export-unit split only for **untracked** kinds, whose role — and
therefore genre — may vary by sub-type:

| Kind | Export units | Rationale |
|---|---|---|
| Tracked (any registry shape) | One unit: the kind, as one change-log table; a sub-typed kind retains its `<kind>_type` discriminator column | Tracked-ness dominates — every unit is a change log regardless of role, so a split serves nothing |
| Untracked, bare role string | One unit: the kind. A sub-typed kind (one carrying a `<kind>_type` enum domain) stays a single table with its discriminator column — single-table inheritance, an operational shape in its own right | The role is uniform; nothing forces a split |
| Untracked, `{sub_type: role}` object | One unit per **declared** sub-type (the `<kind>_type` enum-domain values, declaration order), each filtered by the discriminator predicate | Roles vary by sub-type; one table cannot carry two genres |

Per-unit classification then applies to the untracked split: each unit resolves its
own role (`role_of(kind, sub_type)`), so a split kind's units may land as a mix of
reference and transaction tables. Each unit is discriminator-filtered through the
reader's records builder (which takes a discriminator predicate), never through the
change-log fold — the split is an untracked-only concern precisely because the
per-kind change-log fold carries no discriminator and a delete row's after-image
discriminator is `NULL`.

| Split-unit condition (untracked object-registry kind) | Result |
|---|---|
| A declared sub-type materializes zero rows | Its table is emitted empty (declared intent is stable across slices; matches the every-declared-table-is-emitted rule) |
| A sub-type present in the enum domain but absent from the registry object | Error `SourceRoleUnknown` |
| A kind with an object registry entry but no `<kind>_type` enum domain | Error `SourceSubtypesUndeclared` — units cannot be enumerated from declared intent |
| Split unit's discriminator column | Dropped from the output — constant within the table, fully recoverable from the table identity |

### The change-log render

A change-log kind exports as one wide CDC table composing the row-state-events
fold — the same derivation streaming replays, written to a table instead of a
stream (source is to streaming what a nightly CDC dump is to a live Kafka feed). Wide
is the only shape. A change-log kind is **never split**: a tracked sub-typed kind
exports as one table with its `<kind>_type` discriminator retained (§ The sub-type
split), so the fold is invoked once per kind — carrying no discriminator predicate —
exactly as streaming invokes it, and delete rows (whose after-image is `NULL`) are
never misfiled to a sub-type.

The fold is invoked with the kind's full scalar property set. Its canonical output
is represented as:

| Fold column | Output column (default) | Representation |
|---|---|---|
| `op` | `op` | `c` / `u` / `d`, verbatim |
| `event_sim_time` | `changed_at` | wallclock `TIMESTAMP` through the anchor renderer |
| `record_id` | `id` | verbatim |
| `presentation_id` (when the kind carries it) | `presentation_id` | `CAST` from the fold's codec `VARCHAR` back to the sidecar's `presentation_id` type — producer-typed, the same cast rule as payload columns; typed `NULL` on a `d` row |
| each `prop__<p>` after-image | `<p>` | `CAST` from the fold's codec `VARCHAR` back to the property's sidecar DuckDB type — every payload column is typed, per-source-type, the same cast rule the dimensional representation applies |
| `event_class` | *not projected* | ordering key only; `op` carries the information |

Inherited fold semantics restated as the table's contract: exactly one `c` per
record at `created_sim_time` (creation values folded in); one `u` per distinct later
change `sim_time` of the tracked properties (coincident property changes coalesce
into one after-image row); a `d` at `deactivated_at` when deactivated, with every
payload and `presentation_id` column `NULL` (canonical after-only delete — typed
NULLs after the cast). Untracked columns ride the after-image at their **current**
records-table value on every event — and the `slice_only` omission (below) narrows
the riders to exactly `constant` columns plus the exempt discriminator: values the
contract declares valid at every T, so the fold's temporal-honesty exception is
honest.

Consumers derive current state (`MAX(changed_at)` per `id`), SCD-2
(`LEAD(changed_at)`), and deletion state (`op = 'd'`) themselves — the teaching
contract this render exists for.

### The reference and transaction renders

Both are the faithful records relation (the reader's records builder,
discriminator-filtered for split units), differing only in genre label
— the label carries role semantics for the consumer (what to `JOIN` vs. what to
aggregate), not a schema difference. The column set is **classified, never
enumerated**: every records column resolves through the reader's records-column
taxonomy ([`reader.md`](reader.md) § The records-column taxonomy) — identity
columns other than `record_id` are dropped, presentation / lifecycle / payload
columns render per the presentation defaults below, and a no-role column fails
export validation with `SourceUnclassifiedColumn` (§ Validation Rules) rather
than passing through. All four genre renders agree on this posture: the
change-log render is property-driven and the snapshot render fixed-list, so
identity columns are absent from them by the same rule, not by enumeration
accident. Reference-annotated `prop__` columns are
already id-only `VARCHAR` per the contract, equality-joinable against the target
table's `id` — the FK columns are prominent by construction, no join is performed
(FKs not joined is the genre's definition; the consumer joins).

### The junction render

Each membership table exports as an operational association table — a faithful
read of the interval rows, no derivation:

| Base column | Output column (default) | Representation |
|---|---|---|
| `fork_path` | *dropped* | (see presentation defaults) |
| `record_id` | `<K>_id` (owner kind from the sidecar entry's `record_kind`) | verbatim value |
| `joined_sim_time` | `joined_at` | wallclock `TIMESTAMP` |
| `left_sim_time` | `left_at` | wallclock `TIMESTAMP`; `NULL` while the membership is open at the slice boundary — faithful, never fabricated |
| `elem__<f>` | `<f>` | verbatim, native type |
| `member__<f>__kind` / `member__<f>__id` | `<f>_kind` / `<f>_id` | verbatim |

### The `slice_only` omission

Every records-genre render — change-log after-image, reference, transaction, and
snapshot — narrows its payload set to `tracked` + `constant` columns plus the
exempt sub-typed discriminator, per the export-wide policy
([`slice-only.md`](slice-only.md)): source chooses its own projections, so a
non-exempt `slice_only` column is **omitted** rather than refused, with one
`slice-only-column-omitted` [notice](notices.md) per omitted column per export
unit, in plan order. The collision check and rename resolution run over the
narrowed set; a `rename` columns key naming an omitted column raises
`SourceRenameSliceOnly` (§ Validation Rules) — the rename is unsatisfiable, an
error rather than a silent ignore.

Omission is column-projection-only: row sets, ordering, and incremental window
membership are identical with or without it. The degenerate case follows the same
rule — a unit whose every property is non-exempt `slice_only` still renders, rows
intact, carrying its classless columns and the exempt discriminator when present.
The junction render is untouched (membership columns carry no class), the genre
predicate never consults a `slice_only` column (classification outcomes are
independent of the policy), and `exclude` has no interplay (it cannot name a
column).

### Operational presentation defaults

Source output looks like a real system's tables. Every default below is
**derived** from sidecar identity — never invented — and every one is overridable
via `rename`. A collision anywhere fails fast; the author resolves it via `rename`.

**Table names:**

| Unit | Default output table name |
|---|---|
| Unsplit records kind | `<kind>` |
| Split unit | `<sub_type>` |
| Membership table `membership__<K>__<p>` | `<K>_<p>` |

**Column names and drops.** Each row names or drops one base column; a genre's
render carries a row only when its column set includes that base column — the CDC
change-log render's set is exactly § The change-log render (`op` / `changed_at` /
`id` / `presentation_id` / payload), the junction's is § The junction render. The
`created_at` / `active` / `deactivated_at` / `updated_at` lifecycle columns below
therefore appear in the reference and transaction renders (and, minus `updated_at`,
the snapshot render — § Snapshot delivery), never in the CDC change-log table:

| Base column | Default |
|---|---|
| `fork_path` | **Dropped.** Constant under the trunk-only guard; a mechanism column no operational system carries |
| `record_index` | **Dropped.** Identity ordinal, following `fork_path`'s precedent; not addressable by `rename` — there is no output column to name |
| `ref_index__<name>` | **Dropped.** Index-space reference encoding; the id-space `prop__<name>` renders as `<name>`. Not addressable by `rename` |
| `record_id` | `id` (records genres) / `<K>_id` (junction owner) |
| `presentation_id` | Kept unprefixed and producer-typed (verbatim from records; `CAST` back from the fold's `VARCHAR` in the change-log render) |
| `created_sim_time` | `created_at`, wallclock |
| `active` | `active`, verbatim |
| `deactivated_at` | `deactivated_at`, wallclock (`NULL` iff active) |
| `last_mutation_sim_time` | `updated_at`, wallclock |
| `prop__<p>` / `elem__<f>` | `<p>` / `<f>` — prefix stripped, native type |
| `member__<f>__kind` / `__id` | `<f>_kind` / `<f>_id` |
| Split unit's `prop__<kind>_type` (untracked object-registry only) | Dropped (constant; table identity carries it). A **retained** discriminator — a tracked kind, or a bare-role sub-typed kind — strips to `<kind>_type` like any `prop__` column |

Payload columns keep their sidecar DuckDB types untouched: the mode cannot know a
`BIGINT` property is a duration or a count, so it renders only the *structural*
sim-time columns (`created_sim_time`, `deactivated_at`, `last_mutation_sim_time`,
`joined_sim_time`, `left_sim_time`, `event_sim_time`) as wallclock. A time-valued
property column is the author's to interpret downstream.

**Collision policy.** After defaults and renames resolve: two output tables with one
name, or two columns of one table with one name, is `SourceNameCollision` — an
error at export validation, never a silent suffix or drop. Likely instances: a kind
named like a junction default (`team_members`), a sub-type named like another kind,
a `prop__id` stripping onto `id`. Every `rename` key is **sidecar/source identity**
— the base table + sub-type for a table, the source column name for a column —
never a derived output name, precisely so a default-name collision is always
resolvable: the `prop__id`-onto-`id` case is broken by renaming source column
`prop__id` (or `record_id`), which the shared output name `id` alone could not
address.

### Presentation-name posture

`last_mutation_sim_time` is a sim-internal bookkeeping column — a high-water mark
over a record's content lifecycle. Its **value** channels freely: it is the
`updated_at` presentation default above, and a downstream reference or transaction
render carries that rendered column. Its **raw name** never reaches output. The
name `last_mutation_sim_time` is a reserved output column name (the shared check
in [`exporters/reserved_names.py`](../../src/fabulexa_forge/exporters/reserved_names.py),
with the source-specific guard in
[`exporters/source/plan.py`](../../src/fabulexa_forge/exporters/source/plan.py)),
so a `rename` targeting that output name is refused at plan build — the one path
by which a source config could deliver the raw name. This is the companion of the
playback seam's posture, where the column is never selectable by its own name and
is presented as the recorded trail under `state` ([`playback.md`](playback.md) §
The recorded trail); [`dimensional.md`](dimensional.md) carries the same
reserved-output-name check on its author-named columns.

### Wallclock timestamps: the anchor is required

Every structural sim-time column renders through the effective anchor via the
shared renderer (`render_anchor_timestamp_expr`) — byte-identical rendering
semantics to every other wallclock mode, same precedence (CLI → config `rebase` →
sidecar `runtime`), same DST and ambiguity failure rules (see [`anchor.md`](anchor.md)).

Source is the first mode that **requires** a resolved anchor:

| Anchor resolution outcome | Result |
|---|---|
| `EffectiveAnchor` resolves (sidecar runtime, possibly overridden) | Export proceeds; all structural sim-time columns are wallclock `TIMESTAMP` |
| No anchor resolves (`None`) | Error `SourceAnchorRequired` — an operational dump never shows ns offsets; silently emitting raw integers would be a fallback |

### `exclude` semantics

| Declaration | Effect |
|---|---|
| `exclude.kinds: [K, …]` | Drops every export unit of kind `K` **and every membership table `K` owns** (a junction without its owner is not an operational shape). Reference *to* `K` from other tables (id-valued `prop__` columns, `member__<f>__*` pairs) remain as plain columns — a restricted extract, documented, not an error |
| `exclude.tables: [T, …]` | Drops the named **sidecar** tables. A `membership__*` entry drops that junction alone; a `records__<kind>` entry is equivalent to excluding the kind |
| An entry resolving to nothing in the sidecar | Error `SourceExcludeUnresolved` |
| An empty list | Rejected at parse time (existing `ExcludeDecl` rule) |

### Ordering and determinism

The exporter is a pure function of `(emit, config, code version)`. Every emitted
table carries a total `ORDER BY` over raw sim-time keys and identity — never over
rendered timestamps (microsecond truncation would make ties nondeterministic):

| Genre | Total order |
|---|---|
| change-log | `(event_sim_time, event_class, record_id)` — the fold's own order |
| reference / transaction | `(created_sim_time, record_id)` |
| junction | `(record_id, joined_sim_time, element-field columns in element-schema declaration order — reference fields by their kind/id pair, values compared as VARCHAR with NULLS FIRST)` |
| snapshot (§ Snapshot delivery) | `(created_sim_time, record_id)` |

### Delivery

`--fmt csv` writes one `<table>.csv` per output table into the output directory;
`--fmt duckdb` writes one typed table per output table into a single `.duckdb`
file. Both via the shared writer dispatch (`exporters/query_spec.py`), materializing
through `Emit.query_arrow`; see [`writers.md`](writers.md) for the adapters
themselves. A zero-row table is still emitted (header-only CSV / empty typed
table). The return contract matches every other mode: a mapping of every output
table name to its row count.

### Corrupter composition (the dirty source dump)

`corrupt → source` is a pipeline, not a feature: corrupter output is
structurally-conformant base shape (C1–C5, C8 preserved), and the source mode reads
only sidecar-declared structure, so a source export over a corrupted emit yields a
dirty operational dump with `defects.json` as the label-grade answer key — the
data-cleaning teaching corpus. Injected defects flow through faithfully:
schema-drifted columns export under their drifted names (the sidecar is
regenerated), duplicated/deleted/phantom rows land in the dump, mutated and nulled
values ride the after-images, dangling references survive as unjoinable ids,
distorted intervals land in the junction tables. No corrupter-aware branch exists in
the source mode; the guarantee holds by construction, verified by
[`tests/integration/test_corrupt_source.py`](../../tests/integration/test_corrupt_source.py),
which corrupts a fixture emit, runs a source export over it, and asserts the export
succeeds and a declared defect is observable in the output.

### Incremental composition

`--next` / `--from` / `--to` work over source exports through the cross-mode
driver (see [`incremental.md`](incremental.md)) — window math, cursor,
fingerprint, drained detection, labels, empty-window emission, and staging are its
shared mechanics, common to every mode it wraps. The source mode contributes only
its windowed compile
(`build_source_query_specs`) and the per-genre window membership below. Window
membership tests run on raw sim-time ns, half-open `[start_ns, end_ns)`.

| Genre | Window key | Behavior per window |
|---|---|---|
| change-log (`change_delivery: changelog`) | `event_sim_time` | Append event rows with key ∈ window. Events are immutable; an appended row is final |
| transaction | `last_mutation_sim_time` | Append rows with key ∈ window — the row lands when its content stops changing, which is exactly creation time for write-once fact kinds (the operational norm). Appended rows are final, never revised |
| reference | — (snapshot class) | Full current-state table every window: `replace` in DuckDB, re-emitted in every CSV drop. Untracked kinds carry no history, so no horizon rendering is possible; the full snapshot is the deliberate, documented choice (values are end-of-run; wider than a real nightly extract, never fabricated) |
| junction | `joined_sim_time` and `left_sim_time` — **activity** | Extract-on-change (below) |
| change-log (`change_delivery: snapshot`) | — (snapshot class) | State-at-horizon table every window (§ Snapshot delivery) |

**Junction extract-on-change.** A membership interval emits a row in each window
containing membership *activity* — its join, its leave, or both:

| Condition | Emission |
|---|---|
| `joined_sim_time` ∈ window | The interval row, with `left_at` **horizon-masked**: rendered only if `left_sim_time < end_ns`, else `NULL` (the leave is future state at this horizon) |
| `left_sim_time` ∈ window and `joined_sim_time` in an earlier window | The interval row re-emitted, `left_at` now set |
| Both in one window | One row, `left_at` set |
| Neither in the window | No row |

A closed interval therefore appears at most twice — once open, once closed — and
the later row supersedes the earlier under the natural merge key `(owner id,
member fields, joined_at)`. This is the upsert-extract shape real source systems
deliver; merging it is the teaching exercise. In the DuckDB warehouse both rows
accumulate (append-only; no emitted row is ever updated); in CSV each window's drop
carries its own activity. Horizon-masking is the one place a source value is
window-dependent, and it is masking (withholding future state), never
recomputation — every emitted value is on-or-before the window horizon. A full
(non-incremental) export carries unmasked values: `left_at` is the base value, one
row per interval.

Bookkeeping reserved names (the DuckDB `_export_meta` / `_export_windows` tables,
the `__rows` suffix, `__valid_from_ns`) are reserved for source output table names
under the existing cross-mode rule (`exporters/reserved_names.py`), enforced at
plan build so a full export and a later incremental drip on the same target agree.
The SCD-2 `valid_to` view machinery is dimensional-only; no source genre uses
views.

### Snapshot delivery

`change_delivery: snapshot` switches every change-log-genre kind from a CDC table
to periodic full-table snapshots — the no-CDC source-system archetype: consumers
get nightly fulls and derive deltas by snapshot-diffing. Reference and transaction
genres are already snapshot-shaped and are unaffected; the axis touches only
change-log kinds. The toggle is global (one source system, one archetype), config
not CLI, and participates in the incremental fingerprint like any config change.

| Condition | Result |
|---|---|
| `change_delivery: snapshot` with an incremental invocation (`--next` or `--from`/`--to`) | Each change-log kind emits one full-table snapshot per window: every record with `created_sim_time < end_ns`, reconstructed at the window horizon. `replace` in DuckDB, re-emitted per CSV drop |
| `change_delivery: snapshot` on a plain full export | Each change-log kind emits one end-of-tape snapshot: every record of the kind, reconstructed at the tape's end. `create` in DuckDB, one CSV drop |
| `change_delivery: changelog` (the default) | The CDC render |

**Snapshot row reconstruction** composes the state-at derivation (see
[`derivations.md`](derivations.md) § The state-at derivation). A windowed
invocation reconstructs at the window horizon through `build_state_at_sql`; a
full export reconstructs at the tape's end through the resident's end-of-tape
entry point `build_state_at_end_sql`, which carries no horizon — "the tape's
end" is structural, whatever data the composed relations hold, so the end-of-run
state renders with every history and lifecycle instant applied (a deactivation
is a spine fact, not a `history` row, so a horizon cleared against `history`
alone would wrongly render a later-deactivated record active). The snapshot
table's shape mirrors a reference table with two deviations:

- **No `updated_at`.** `last_mutation_sim_time` at a past horizon is not
  faithfully reconstructible — untracked property writes advance it but leave no
  history — so the column is omitted rather than fabricated or understated.
- **Lifecycle at the horizon.** `active` and `deactivated_at` are
  horizon-rendered: a record deactivated after the horizon shows `active = true`,
  `deactivated_at = NULL` — deterministic recodings of base values, nothing
  invented.

Tracked properties carry their as-of value at the horizon (cast to their sidecar
type, per the same rule as the CDC render); untracked properties carry their
current value — the same declared temporal-honesty exception as everywhere else,
inherited from the base layer having no history for them.

**Plan-time resolution under snapshot delivery.** A change-log-genre kind's
delivered columns depend on `change_delivery`, so `build_source_plan` consults it:
under `snapshot` it resolves the kind's column set to the snapshot shape above —
identity, horizon-rendered `created_at` / `active` / `deactivated_at`, and
payload, with no `op` / `changed_at` / `updated_at` — and runs the collision check
and rename resolution over *that* set (the columns the kind actually delivers).
Rename keys for a snapshot-delivered change-log kind are therefore the
base/state-at source names (`record_id`, `created_sim_time`, `active`,
`deactivated_at`, `presentation_id`, `prop__<p>`), never the fold names: the
fold-named columns (`op`, `event_sim_time`) exist only under `changelog`
delivery. The `genre` label stays `changelog` — it selects the render — while
`change_delivery` selects the columns.

## Invariants

1. **Classification is total and deterministic.** Every `records__<kind>` and
   `membership__<K>__<p>` table in the sidecar resolves to exactly one genre from
   the trichotomy; the three sidecar table categories are exhaustive at the
   supported `base_format_version`, so nothing is left unclassified. Within a records
   table the same posture holds per column: every column resolves to a
   records-column taxonomy role, and a no-role column is
   `SourceUnclassifiedColumn` at plan time — never a silent pass-through or a
   raw leak into output.
2. **A tracked kind is never split.** Tracked-ness is a kind-level fact; a
   change-log table is emitted once per kind regardless of role-registry shape,
   retaining its `<kind>_type` discriminator if sub-typed.
3. **Every declared sub-type table is emitted, even empty.** Declared intent, not
   observed rows, drives table existence — matching the every-declared-table rule.
4. **Faithful reshaping.** Every output value traces to a base-layer value or a
   deterministic recoding of one (a cast, a wallclock render, a horizon mask); the
   mode fabricates nothing (CLAUDE.md Principle #3). A source export over a
   corrupted emit surfaces the corrupter's declared defects unchanged, never
   manufacturing new ones.
5. **Wallclock rendering requires a resolved anchor.** Unlike every other mode's
   raw-integer fallback, source refuses (`SourceAnchorRequired`) rather than emit
   ns offsets.
6. **Total order over raw sim-time, never rendered timestamps.** Every emitted
   table carries a deterministic `ORDER BY` over raw ns keys and identity, so
   microsecond truncation in wallclock rendering cannot introduce ties.
7. **Snapshot delivery reconstructs at a horizon.** `change_delivery: snapshot`
   reconstructs each change-log kind through the state-at resident: at the window
   horizon under an incremental invocation, at the tape's end under a full export
   (the resident's horizon-free end-of-tape entry point). Every snapshot value is
   an as-of-horizon value, never a raw slice read.
8. **Determinism.** Same emit + export config + code version → identical output
   (CLAUDE.md § Key Invariants).
9. **`slice_only` omission is column-projection-only.** Row sets, ordering, and
   window membership are invariant under the policy; omission never suppresses an
   export unit ([`slice-only.md`](slice-only.md)).

## Validation Rules

Field shapes are defined by the Pydantic grammar in
[`config/models.py`](../../src/fabulexa_forge/config/models.py); business-rule
message text is owned by
[`exporters/source/plan.py`](../../src/fabulexa_forge/exporters/source/plan.py) and
[`exporters/source/engine.py`](../../src/fabulexa_forge/exporters/source/engine.py).
The rules below state *what* is rejected and *when*.

**Parse-time (Pydantic).**

| Validator | Rejects |
|---|---|
| `mode_section_matches` (`ExportConfig`) | A missing section for the declared `mode`, or a present section belonging to the *other* mode — two-sided: `mode: dimensional` requires `dimensional` and forbids `source`; `mode: source` permits an absent `source` block (the bare full dump) and forbids `dimensional` |
| `at_least_one_field` (`SourceConfig`) | A present `source:` block that sets no field (`model_fields_set` empty) — a bare `source: {}` is rejected, matching the `rebase` empty-block rule. An explicit `change_delivery: changelog` — the default value, but explicitly provided — counts as set |
| `entry_well_formed` (`RenameEntry`) | An entry with neither `name` nor `columns` set; an empty `columns` map or one with an empty key/value; non-distinct `columns` values (two source columns renamed to one output name) |
| `entries_disjoint` (`SourceConfig`) | Two rename entries targeting the same `(table, sub_type)` pair |

Cross-mode rules that source's config satisfies too: `ExcludeDecl` non-empty
lists; the `RebaseConfig` / `IncrementalConfig` validators.

**Business rules.** Run at export time against the open emit, before any write;
each raises an `ExportError` subclass surfaced through the CLI's existing error
funnel.

| Rule | Checks | Error |
|---|---|---|
| `SourceRecordRolesRequired` | The sidecar carries a `record_roles` registry | `"source export requires the record_roles registry; this emit predates it"` |
| `SourceHistoryTrackedRequired` | The sidecar carries `history_tracked` flags | `"source export requires per-column history_tracked flags; this emit predates them"` |
| `TemporalClassUnavailableError` (reader-owned; see [`reader.md`](reader.md)) | Every `prop__` column the genre predicate inspects (i.e. one flagged `history_tracked`) declares a `temporal_class` within the three-value enum. Resolved at plan time, against the open emit's sidecar, before any data read | `"… declares history_tracked but no temporal_class; the emit is non-conformant (C13). Run \`fabulexa-forge validate\`."` (an out-of-enum declared value raises the same error, its message naming the value) |
| `SourceUnclassifiedColumn` | Every records column of every planned unit classifies to a records-column taxonomy role ([`reader.md`](reader.md) § The records-column taxonomy) — the exporter-side counterpart of C5's recorded failure. Resolved at plan time, before any output is written | Names the table and column; a direct `ExportError` subclass |
| `SourceRoleUnknown` | Every **untracked** exported kind — and every declared sub-type of an untracked object-registry kind — resolves a role (a tracked kind needs none) | `"kind '{kind}'{sub_type_clause}: no role in record_roles"` |
| `SourceSubtypesUndeclared` | An **untracked** object-registry kind declares a `<kind>_type` enum domain | `"kind '{kind}': role varies by sub-type but no {kind}_type enum domain declares the sub-types"` |
| `SourceAnchorRequired` | An `EffectiveAnchor` resolved for the invocation | `"source export renders wallclock timestamps and requires a resolved anchor: the emit declares no runtime block; supply rebase.base_date/timezone or --base-date/--timezone"` |
| `SourceExcludeUnresolved` | Every `exclude.kinds` / `exclude.tables` entry resolves in the sidecar | `"exclude entry '{entry}' matches nothing in this emit"` |
| `SourceRenameUnresolved` | Every rename entry's `table` (+ `sub_type` iff the kind splits, and only then) resolves, and every `columns` key names a source column of that table | `"rename entry '{table}': {detail}"` |
| `SourceRenameSliceOnly` | No `rename` columns key names a non-exempt `slice_only` source column — the column is policy-omitted (§ The `slice_only` omission), so the rename is unsatisfiable | Names the rename entry, the column, and the omission reason |
| `SourceNameCollision` | All output table names are unique; within each table all output column names are unique | `"output name collision: {names}; resolve via source.rename"` |
| Reserved-name check (`exporters/reserved_names.py` + `exporters/source/plan.py`, raised as `ExportError`) | No resolved output **table** name collides with the bookkeeping names or reserved suffixes, and no resolved output **column** name is `last_mutation_sim_time` (the presentation-name posture — § Presentation-name posture) — checked at plan build over all output names, so a full export and a later `--next` on the same target agree | — |
| Single-branch guard (`derivations/guard.py`, cross-mode) | Exactly one branch | — |

## Rationale

- **Tracked-ness dominates classification.** A kind with any class-`tracked`
  column exports as its change log regardless of role, because classifying it as
  a snapshot would silently drop base-layer history rows — a fidelity violation
  the trichotomy's precedence order exists to prevent.
- **The predicate keys on the class, not the bit.** `history_tracked: true` does
  not separate "changes over time" from "constant with a genesis row" — only
  `temporal_class` does. Keying on the bit would render change logs with no
  changes for kinds whose only flagged column is a constant presentation value;
  keying on the class classifies by what genuinely changes.
- **The sub-type split is untracked-only.** The per-kind change-log fold carries
  no discriminator predicate, and a delete row's after-image discriminator is
  `NULL` — splitting a tracked kind would either misfile deletes or require a
  discriminator the fold cannot supply. Role, not tracked-ness, is what varies by
  sub-type, so only untracked kinds ever split.
- **Identity columns drop rather than carry.** The change-log and snapshot
  renders are fold-driven and structurally cannot carry per-record identity
  columns without new derivation work; carrying `record_index` / `ref_index__*`
  only where a full-list enumeration happens to reach them would be incoherent
  within a single export. Surfacing the index *well* — as the integer PK/FK a
  real operational system shows — is adoption-scale design (key presentation,
  join guidance, incremental-window interaction) that arrives as its own
  design, never as an enumeration side effect.
- **The anchor is required, not defaulted.** An operational dump has no natural
  "no timestamp" representation; silently emitting raw ns integers would be a
  fallback masking a missing anchor as valid output. Every other mode's
  raw-integer fallback is a deliberate carve-out this mode does not take.
- **No `init` for source.** A source config is roughly five lines
  (`mode: source` plus, at most, a handful of `exclude`/`rename` entries);
  generating a candidate teaches nothing a bare-mode default doesn't already
  show. `init` is dimensional-only.
- **No EAV / long-form history passthrough.** The emit's `history` table already
  is that shape; a passthrough toggle would reproduce the input verbatim rather
  than teach a reshape.
- **No membership-events (join/leave event log) table shape.** That is
  streaming's presentation of membership; source's operational truth is the
  interval junction table — one row per membership, not one row per edge.
- **Horizon-masking, never recomputation.** Junction extract-on-change withholds
  future state (`left_at` masked to `NULL` until its window) rather than
  recomputing anything; every emitted value stays on-or-before the window
  horizon, preserving the temporal honesty a projected future `left_at` would
  break.
- **A full-export snapshot reconstructs at the tape's end, structurally.** With no
  window the render composes the state-at resident's horizon-free end-of-tape
  entry point (`build_state_at_end_sql`): the end is whatever the composed base
  relations hold, never a slice bound read from metadata. This yields end-of-run
  state tables (a meaningful `base`-shaped output), and it is the same
  reconstruction the playback seam's shaped `state` drives over a truncated tape —
  one horizon-free rule, two callers ([`playback.md`](playback.md)).
- **The corrupter-composition guarantee is by construction, never
  special-cased.** No corrupter-aware branch exists in the source mode; the
  guarantee that a dirty emit yields a dirty dump follows from the mode reading
  only sidecar-declared structure, and is verified by a dedicated integration
  test rather than asserted by inspection.

## Boundaries

- **No `init` support.** A source config is short enough that generating one
  teaches nothing; `init` remains dimensional-only.
- **No EAV / long-form history passthrough.** Excluded by decision — the base
  layer already carries this shape in `history`.
- **No membership-events (join/leave log) table shape.** That is streaming's
  presentation; source's operational truth is the interval junction table.
- **No point-in-time slice export.** A separate, later contract (Stage 5's
  feature-store rows) will share the state-at derivation this mode introduced
  (see [`derivations.md`](derivations.md) § The state-at derivation) rather than
  reuse source's own plan/render surface.
- **Normalized-export posture over denormalized payload is the author's
  `exclude`.** A producer may retain a parent value on a child kind by
  necessity — the published parent-child example's member kind carries
  `prop__group_domain`, the projection input for the member's `email`
  presentation property, so the upstream cannot drop it. Which payload columns
  are "really" denormalized is not forge's to decide (Principle #7): dropping
  one is an author `exclude`, never a mode default.
- **No `slice_only` policy.** The genre predicate never consults a `slice_only`
  column (`history_tracked: false`), and the mode exports it like any other
  column — including into shapes that stamp its slice value at horizons the emit
  cannot speak to. The class makes that infidelity *visible*; a policy that
  refuses or omits such a column (per mode, with a notice channel) is a separate
  contract this mode does not own.
- **Single-branch, like every mode.** Source uses the derivations layer's
  single-branch guard; branch-aware export is parked pending a contract
  extension (see [`README.md`](README.md) § Staged roadmap).
- **CSV + DuckDB only.** No Parquet — the cross-mode writer boundary (see
  [`writers.md`](writers.md)).

## Related

| Document | Why |
|---|---|
| [`reader.md`](reader.md) | The `Sidecar.temporal_class` accessor the genre predicate resolves through |
| [`bundle.md`](bundle.md) | The column temporal classes and the genesis guarantee behind the trichotomy's tracked-ness predicate |
| [`derivations.md`](derivations.md) | The row-state-events fold the change-log render composes, and the state-at derivation snapshot delivery composes |
| [`dimensional.md`](dimensional.md) | The contrasting mode — reconstructed star schema vs. source's raw operational shape; both compile to the mode-neutral `QuerySpec` |
| [`streaming.md`](streaming.md) | The change-log render's sibling delivery — the same row-state-events fold, replayed as a live event feed instead of landed as a table |
| [`incremental.md`](incremental.md) | The cross-mode window/cursor/fingerprint driver source's windowed compile plugs into |
| [`playback.md`](playback.md) | The seam whose tier-2 `state` compiles this mode over a truncated tape via `base_relations`; the presentation-name posture's companion |
| [`anchor.md`](anchor.md) | The effective-anchor resolution source requires — its first mandatory consumer |
| [`corrupters.md`](corrupters.md) | The corrupt → source composition — a source export over a corrupted emit surfaces declared defects unchanged |
| [`writers.md`](writers.md) | The CSV / DuckDB adapters source shares with every mode |
| [`config-docstrings.md`](config-docstrings.md) | The docstring convention `SourceConfig` / `RenameEntry` follow |
| [`../CAPABILITIES.md`](../CAPABILITIES.md) | Source-mode feature inventory and status |
| [`README.md`](README.md) | Design index, package layout, staged roadmap |
