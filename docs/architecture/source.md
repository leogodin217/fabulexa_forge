# Source Exporter

**Status:** Implemented. Code is the contract — see
[`exporters/source/`](../../src/fabulexa_forge/exporters/source/)
(`plan.py`, `renders.py`, `events.py`, `engine.py`, `init.py`, `columns.py`),
[`exporters/populations.py`](../../src/fabulexa_forge/exporters/populations.py),
[`config/models.py`](../../src/fabulexa_forge/config/models.py) (`SourceConfig`,
`SourceTableDecl`, `SourceEventsDecl`, `SourceEventSourceDecl`, `MembershipRef`),
and [`tests/exporters/source/`](../../tests/exporters/source/),
[`tests/integration/test_corrupt_source.py`](../../tests/integration/test_corrupt_source.py).
Public API: [`exporters/source/engine.py`](../../src/fabulexa_forge/exporters/source/engine.py)
(`export_source`, `build_source_query_specs`),
[`exporters/source/plan.py`](../../src/fabulexa_forge/exporters/source/plan.py)
(`build_source_plan`), and
[`exporters/source/init.py`](../../src/fabulexa_forge/exporters/source/init.py)
(`generate_source_init_config`).

The `mode: source` exporter renders the emit as a **well-architected application
database** — the normalized OLTP schema a real system runs on, declared table by
table by the author. The rule of the shape: *things get tables; events get the
log.* A source config declares every output table — its author-verbatim name, its
source populations, its row selection, its columns — through a declared-table
grammar: thing tables
(`state` render), association tables (`junction` render), and one polymorphic
audit log (the event-log render). Sidecar facts gate what a declaration may ask
for (does the kind exist, is the sub-type declared, is the surface electable);
they never decide layout. Defining output shape is this package's job, and in
source mode the author holds that lever directly: bundle vocabulary reaches
output through exactly one door — `init --mode source` proposals, which the
author edits and owns. Where dimensional hands the consumer a reconstructed star
schema and streaming replays a live CDC feed, source hands over the app-database
shape both of those are built *from*.

```
emit (run.duckdb + base.json @ the supported `base_format_version`)
   │  (reader: Emit + Sidecar; trunk-only — sole branch)
   ▼
source config: tables + events (declared; `init --mode source` proposes)
   │  resolve_populations — declaration → sub-type atoms (shared exporter layer)
   ▼
build_source_plan → per-table plans + the event-log plan (all gates run here)
   ▼  build_source_query_specs (full or windowed)
        tables[].kind        ──▶ state render     (one current row per record)
        tables[].membership  ──▶ junction render  (one row per membership interval)
        events               ──▶ event-log render (one polymorphic audit table)
   ▼
writers (CSV | DuckDB — both via Emit.query_arrow)
```

---

## Surface

| Module | Owns |
|---|---|
| [`config/models.py`](../../src/fabulexa_forge/config/models.py) | `ExportConfig.source: SourceConfig \| None`; `SourceConfig` (`tables`, `events`, `declare_keys`), `SourceTableDecl`, `SourceEventsDecl`, `SourceEventSourceDecl`, `MembershipRef`; the `source_section_required`, `table_shape`, `source_shape`, `events_shape`, `kind_labels_shape`, and `table_source_exclusive` parse-time validators |
| [`exporters/populations.py`](../../src/fabulexa_forge/exporters/populations.py) | `Population` (the sub-type atom) and `resolve_populations` — the shared-layer resolver from a config population address to its atoms. Shared because it resolves the same atoms key election addresses; source mode is its consumer. Election resolution keeps its own resolution gates (`ElectionKindUnknown` / `ElectionSubTypeUnknown`) and is not routed through it |
| [`exporters/source/plan.py`](../../src/fabulexa_forge/exporters/source/plan.py) | `SourceStateTablePlan`, `SourceJunctionTablePlan`, `SourcePlan`; `build_source_plan` — declaration resolution, column classification through the records-column taxonomy, `columns` / `rename` resolution, the identity and edge gates, the row-selection gates and their plan-time predicate-literal casts, the audited-set resolution, the collision and reserved-name checks |
| [`exporters/source/columns.py`](../../src/fabulexa_forge/exporters/source/columns.py) | The shared `prop__<p>` scalar-property lookup `plan.py` and the renders both need, so neither duplicates it |
| [`exporters/source/renders.py`](../../src/fabulexa_forge/exporters/source/renders.py) | `build_state_render_sql` / `build_junction_render_sql` — the thing-table renders, each carrying its total `ORDER BY` and wallclock rendering through the shared anchor renderer; `build_selection_spine_sql` — the parent lookup every owner-keyed narrowing composes (§ Row selection) |
| [`exporters/source/events.py`](../../src/fabulexa_forge/exporters/source/events.py) | `SourceEventSourcePlan`, `SourceEventLogPlan`, `build_event_log_sql` — the event-log render: the row-state-events and membership-events folds composed with the previous-after-image diff and the deterministic JSON `changes` assembly, all rendered in SQL |
| [`exporters/source/engine.py`](../../src/fabulexa_forge/exporters/source/engine.py) | `export_source`, `build_source_query_specs` — plan → per-table render → optional windowing → dispatch to the shared writer. The compile is connection-free and pure; it carries no `base_relations` parameter — the playback seam applies its truncated-relation mapping as a post-compile SQL rewrite ([`playback.md`](playback.md) § The compile indirection) |
| [`exporters/source/init.py`](../../src/fabulexa_forge/exporters/source/init.py) | `generate_source_init_config` — the `init --mode source` proposal engine |
| [`exporters/query_spec.py`](../../src/fabulexa_forge/exporters/query_spec.py) | `QuerySpec`, `write_query_specs` — the mode-neutral compiled-table shape and write dispatch every mode shares |
| [`exporters/reserved_names.py`](../../src/fabulexa_forge/exporters/reserved_names.py) | `is_reserved_table_name` / `is_reserved_column_name` — the cross-mode bookkeeping-name check |
| [`errors.py`](../../src/fabulexa_forge/errors.py) | The `Source*` error hierarchy (`ExportError` subclasses) |
| [`cli.py`](../../src/fabulexa_forge/cli.py) | `cmd_export` — dispatches on `config.mode`; `cmd_init` — `--mode` selector (`dimensional`, the default, or `source`) |

## Boundary

- **Input.** An open `Emit` (trunk-only — sole branch), a validated `ExportConfig`
  with `mode: source` and its required `source` section, the resolved
  `EffectiveAnchor` (or `None` — checked as a business rule, not tolerated as a
  silent fallback), the `fmt`, and an optional `Window` for an incremental
  invocation.
- **Output.** Per `fmt`: one `<table>.csv` per declared output table into the
  output directory, or one typed table per declared output table in a single
  `.duckdb` file — both through the shared writer dispatch
  (`exporters/query_spec.py`). A zero-row table is still emitted.
- **Reader-first; no base-table SQL authored directly.** Every base read is an
  embedded reader relation (`build_records_relation_sql`,
  `build_membership_relation_sql`) or a derivations-layer fold (row-state-events,
  membership-events, state-at) — the mode composes, never hand-writes
  `FROM records__…`. Source consumes no `record_roles`: neither the export path
  nor `init --mode source` reads the registry.
- **Forbidden imports.** `exporters.source` never imports `exporters.dimensional`
  or `exporters.streaming`, and neither imports it back — the mode packages are
  independent leaves composing only the reader, the derivations layer, and the
  mode-neutral shared exporter modules. No dependency on the bundle's producer;
  the vendored `contract/` is the only coupling.

## Semantics

### Populations and declared tables

The unit a declaration resolves to is the **population** — the sub-type atom
`(kind, sub_type)` where the sidecar declares a discriminator domain,
degenerating to `(kind)` for a flat kind. Resolution is presence-driven from the
sidecar, through the shared-layer resolver
(`resolve_populations` in
[`exporters/populations.py`](../../src/fabulexa_forge/exporters/populations.py)).
A `tables` entry addresses populations of exactly one kind — tabular combination
is same-kind-only, because column shape forces it:

| Declaration | Population set |
|---|---|
| `kind: K` (flat kind) | `(K)` — the whole kind |
| `kind: K` (sub-typed kind) | Every declared sub-type of `K` — shorthand for the full discriminator domain |
| `kind: K, sub_types: [a, b]` | `(K, a)` and `(K, b)` |
| `kind: K, sub_types: […]` where `K` is flat | Error `SourceSubTypesOnFlatKind` — a flat kind has no populations to address |
| `membership: {kind: K, property: p}` | The one `membership__<K>__<p>` table, over the owner kind's full declared population set |
| `membership: {kind: K, property: p}, sub_types: [a, b]` | The same table, addressing the owner populations `(K, a)` and `(K, b)` — the intervals whose owner falls in those sub-types (§ Row selection) |

A declaration selects on two axes, and they compose. `sub_types` is the
**population** axis: it narrows the set of `(kind, sub_type)` atoms the unit
addresses, and every downstream gate and type resolution ranges over that
narrowed set. `where` is the **value** axis: an optional row predicate over the
subject kind's `constant`-class payload properties, which changes which rows
render without changing the addressed population set at all (§ Row selection).
Both are legal on a records-backed and a membership declaration alike; on a
membership declaration the subject is the **owner** kind.

| Condition | Result |
|---|---|
| A declared kind has no `records__<kind>` table in the sidecar | Error `SourceTableKindUnknown` |
| A declared sub-type is not in the kind's discriminator domain | Error `SourceTableSubTypeUnknown` |
| A declared membership reference resolves to no sidecar table | Error `SourceTableMembershipUnknown` |
| A declared population materializes zero rows | Its table (or its share of a combined table) is emitted empty — declared intent, not observed rows, drives table existence |
| A kind (or sub-type) appears in no declaration | It is not exported. Omission is the exclusion mechanism; references *to* an undeclared kind from declared tables remain ordinary reference columns, rendered in the target population's elected surface as anywhere (an undeclared kind may still carry an election — the key-election exclusion posture) — a restricted extract, documented, not an error |
| Two declared tables share a population | Legal — both render it (the dimensional posture for overlapping dims) |
| Two output tables resolve one name, or two columns of one table resolve one name | Error `SourceNameCollision` — never a silent suffix or drop |

Table names are author-verbatim (`name` is required); there are no default table
names to derive. `init` proposes `<kind>` and `<K>_<p>` verbatim from sidecar
identity. A source config declaring no output — no `tables`, no `events` block —
is a load-time error: no implicit layout exists to fall back to. Either side
stands alone: tables without a log (a Type-1-only app), or the log without
tables (an audit-stream-only extract).

The render is determined by the declaration's source shape — a records
population has exactly one thing-render (`state`), a membership table exactly
one (`junction`) — so no `render` field exists; one appears only when a second
render for the same source shape ships.

### The `state` render

The faithful records relation — the reader's records builder,
discriminator-filtered to the table's declared populations and narrowed by its
`where`, if any (§ Row selection). One row per record,
current state, with soft-delete lifecycle. The column set is classified through
the reader's records-column taxonomy
([`reader.md`](reader.md) § The records-column taxonomy), never enumerated:

| Base column | Output (default) |
|---|---|
| `record_id` | `id` — or the table's elected surface under key election |
| `presentation_id` | Kept unprefixed, producer-typed — unless it *is* the elected identity, in which case it renders as the identity column and is not duplicated |
| `created_sim_time` | `created_at`, wallclock |
| `last_mutation_sim_time` | `updated_at`, wallclock (full export; omitted under a windowed invocation — § Incremental composition) |
| `active` / `deactivated_at` | Verbatim / wallclock — the soft-delete pair |
| `prop__<p>` | `<p>`, native type; reference properties render the target population's elected surface per row |
| `prop__<K>_type` (discriminator) | Retained as `<K>_type` when the table spans ≥ 2 populations; dropped when the table's population set is a single sub-type (constant — table identity carries it). Explicitly listing it in `columns` retains it either way |
| `fork_path`, `record_index`, `ref_index__<name>` | Dropped — mechanism columns no operational system carries. `fork_path` / `ref_index__*` are never addressable; `record_index` renders only as a table's elected identity, never as payload (its one addressable use is the identity rename key, below) |
| Non-exempt `slice_only` columns | Omitted with a `slice-only-column-omitted` notice (§ The `slice_only` posture) |
| A records column with no taxonomy role | Error `SourceUnclassifiedColumn` |

**Column selection and renames.** Per-table `columns` (optional) selects *which*
source columns project — a subset of the taxonomy's projectable set plus the
discriminator; the taxonomy still decides *representation*. Entries are source
column names (`prop__status`, `created_sim_time`), never derived output names.
The identity column is outside `columns`' reach: it always projects (a
thing-table without identity is not a thing-table), and a `columns` entry naming
the table's **elected** surface is refused (`SourceColumnNotAddressable` —
identity is election-governed, not selection-governed). A *non-elected* surface
name resolves by its own rule: `presentation_id` when not elected is an ordinary
selectable column; `record_id` under a non-`record_id` election is a surface the
election leaves unrendered — `SourceColumnUnresolved`, the message naming the
election; `record_index` when not elected is a mechanism column —
`SourceColumnNotAddressable`. Everything else — `presentation_id` (when not
elected), the lifecycle columns, payload properties, the discriminator — is
selectable and omittable. Absent `columns`, the full classified set projects.

Per-table `rename` (optional) overrides the default output name of any projected
column, keyed on the source column name (never a derived output name, so a
default-name collision is always resolvable). The **identity column's** rename
key is the elected surface's contract column name (`record_id` / `record_index`
/ `presentation_id` — the key-election posture); a key naming a surface the
election leaves unrendered (`record_id` under a `presentation_id` election) is
unsatisfiable and errors (`SourceColumnUnresolved`).

**Temporal elections.** A per-table `render` map elects the rendering of a
structural instant column — `created_sim_time` → `date`, say — in place of
the default `created_at` wallclock `TIMESTAMP`; a `date_parse` map declares a
payload VARCHAR column (`prop__<p>`) a temporal string in an author format,
rendered as the type that format denotes — `DATE`, `TIME`, or naive
`TIMESTAMP`. Both are keyed on source identity, re-render the
projected column in place, and require the column to be one the render
already emits ([`temporal-elections.md`](temporal-elections.md) § Per-mode
attach points).

### The `junction` render

One association row per membership interval — every interval of the table, or
those whose owner satisfies the declaration's `sub_types` / `where` (§ Row
selection): `record_id` → `<K>_id` (owner,
rendered in the owner kind's elected surface), `joined_sim_time` /
`left_sim_time` → `joined_at` / `left_at` wallclock (`left_at` NULL while the
membership is open — the soft-delete idiom, faithful, never fabricated),
`elem__<f>` → `<f>` native, `member__<f>__kind` / `member__<f>__id` →
`<f>_kind` / `<f>_id` with the member id rendering the target population's
elected surface per row.

Per-table `columns` / `rename` apply to the membership surface the same way: the
owner column always projects and is addressed by its source name `record_id`
(it always renders, whatever surface it carries); the interval columns, element
fields, and member fields are selectable and omittable. The member pair's two
columns (`member__<f>__kind` / `member__<f>__id`) select and rename
independently by their own source names — keeping `<f>_id` while omitting
`<f>_kind` is a legal restricted extract (per-row election resolution consults
the kind internally regardless); the pair is atomic only inside the event log's
`changes` expansion.

The same `render` / `date_parse` maps apply to a junction table: `render`
elects `joined_sim_time` / `left_sim_time`'s rendering; `date_parse` declares
a payload `elem__<f>` VARCHAR field a temporal string.

### Row selection

`sub_types` and `where` narrow which rows a declared unit renders — a state
table, a junction table, or an events source alike. Absent both, a unit renders
every row its population source carries. A kind whose rows partition on an
undeclared-but-constant property, and a sub-typed kind's membership estate, are
each splittable into separate declared tables with separate audit streams.

**The constant-column gate.** A `where` key names a payload property of the
declaring unit's **subject kind** — the declared kind on a records-backed unit,
the **owner** kind on a membership unit — whose `temporal_class` is `constant`.
Key form follows the unit's own addressing convention: the source column name
(`prop__<p>`) on a records-backed table, the bare property name (`<p>`) on an
events source and on a membership unit, where the subject is the owner kind and
owner properties are not columns of the unit at all. Resolution is against the
subject kind's payload-property set only, so a bare key on a membership unit
naming both an owner property and an element field resolves to the owner
property.

| `where` key names | Result |
|---|---|
| A `constant`-class payload property of the subject kind | Accepted |
| A `tracked`-class property | `SourceWhereNotConstant` — under a horizon reconstruction its as-of value and its current value select different rows |
| A `slice_only` property | `SourceWhereNotConstant` — its past is unknowable, so row selection cannot read it (the [`slice_only`](slice-only.md) posture; `where` keys are this rule's to refuse, not `SourceSliceOnlyRead`'s) |
| The subject kind's declared discriminator (`prop__<K>_type` / `<K>_type`) | `SourceWhereOnDiscriminator`, pointing at `sub_types` — including the slice-only-exempt sub-typed discriminator, and on membership units, where `sub_types` selects owner sub-types |
| A structural column (`record_id`, `created_sim_time`, `active`, …) | `SourceWhereColumnUnresolved` — structural columns are not payload properties and are not predicate-addressable |
| An element field of a membership unit, matching no owner property | `SourceWhereColumnUnresolved` — element fields carry no `temporal_class`; selection reads the owner |
| A column not on the subject kind | `SourceWhereColumnUnresolved` — as any `columns` / `rename` key |

The gate makes the as-of-which-horizon question unposable rather than answering
it. A `constant`-class property's value is identical at every horizon — the mode
renders constant properties current in windowed snapshots as its declared
temporal-honesty exception — so the full export, every incremental window, and
every event time select the same rows. The gate reads a column's declared class,
never its values.

**The key axes error; the value axis notices.** Every key failure above is an
error: the key names the wrong axis or an unusable column, and no sensible export
can ship. Predicate *values* follow dimensional's posture instead — an element
outside a `where` column's declared `enum_domains` entry draws a per-element
`discriminator-value-unobserved` [notice](notices.md), never an error, and a
column with no `enum_domains` entry is unchecked. A declared-but-unobserved value
is a legitimate way to write one config against a family of emits, and the
zero-match outcome is legal: declared intent drives existence.

The value axis's tolerance ends at the column's declared type. Every `where`
element is cast to its resolved column's sidecar-declared DuckDB type at plan
time — the same cast the rendering authority compiles into the predicate,
constant-evaluated on every `where`-bearing unit, gated or not — and an element
the type cannot cast is `SourceWhereValueUncastable`, before any write. An
out-of-domain value is unobserved-but-possible; an uncastable one is impossible
under the declared type in every emit of the config's family, and the rendered
`CAST` would otherwise raise at query time, mid-export. The disjointness gate's
typed-value comparison (§ The event log) reuses exactly these plan-time cast
results.

**The parent lookup.** A membership unit's rows carry no owner attributes, so its
selection evaluates against the owner: an identity join from the membership rows'
owner column (`record_id`) to the owner kind's records spine, where the
discriminator and the predicate columns live
([`build_selection_spine_sql`](../../src/fabulexa_forge/exporters/source/renders.py)).
The join is fan-out-free (`record_id` is unique on the spine) and horizon-free
(the discriminator is creation-constant, `where` columns are constant-gated), so
it is exactly the per-row records-spine device a records-source `sub_types`
narrowing composes, applied from the membership side. It is a read for selection
only: no owner attribute is projected into the unit's columns, and the membership
surface — interval columns, element fields, member pairs — is untouched.

Owner `sub_types` narrows the unit's **addressed owner population set**: the
membership unit addresses exactly those `(kind, sub_type)` populations, as a
records-backed declaration does — the set the item-type union-safety gate ranges
over, and the surface union that types the junction owner column and the log's
`item_id` under the junction-member-column rule. A mixed-election owner kind is
therefore splittable per sub-type: each narrowed unit resolves its own
populations' elections, and a narrowed junction whose populations agree on one
declared type carries that type rather than falling to `VARCHAR`. `where` never
narrows the addressed set — it is value-level, not population-level: a
`where`-only membership unit addresses the owner kind's full declared population
set for gates and type resolution, whatever rows the predicate then selects.

**Selection outcomes.**

| Condition | Result |
|---|---|
| State table with `where` | Renders the rows of its population set satisfying the conjunction; the taxonomy, `columns` / `rename`, lifecycle, and election semantics are unaffected by the selection |
| Junction table with `sub_types` / `where` | Renders the membership intervals whose owner satisfies the selection, via the parent lookup; every other junction semantic is unaffected by it |
| `where` and `sub_types` on one unit | AND-composed: the predicate narrows within the selected populations or owner sub-types |
| Selection matches zero rows | The table is emitted empty — declared intent drives existence, as for an empty population |
| A row whose predicated column is NULL | Never selected: `=` / `IN` is never satisfied by NULL and the grammar has no null test. Once a kind is split by `where`, a NULL-bearing partition column's rows land in no predicated unit and remain exportable only through an unpredicated declaration — omission-as-exclusion, applied by value |
| Predicate column absent from `columns` | Legal — selection and projection are orthogonal; the predicate reads the subject relation, not the projected output |
| Predicate on a reference-valued constant property | Legal, no special case: the comparison is over base-layer values (record ids), literal-typed from the sidecar, whatever surface the column *renders* |
| Two declarations' selections overlap, or exhaust nothing | Legal — declared tables require neither disjointness nor coverage (two may already share a population); rows matching no declaration are not exported |
| Events records source with `where` | The fold input narrows to the satisfying records through per-row records-spine resolution; every event of an excluded record is excluded, `create` and `destroy` included |
| Events membership source with `sub_types` / `where` | The fold input narrows to the intervals of satisfying owners via the parent lookup; every `join` / `leave` of an excluded owner's collection is excluded |
| `where` beside `only` / `ignore` on one events source | Orthogonal: `where` selects *records* (or owners), `only` / `ignore` select the audited *property set*. A property may be predicated and ignored at once |

Predicates evaluate over source (base-layer) values — before rename, before
elected-surface rendering — and every condition compiles through the one
rendering authority the whole package shares
([`row-predicates.md`](row-predicates.md)).

### The event log

One declared polymorphic audit table at event grain — the app's own history
idiom. Fixed columns; the author names the table, not the columns:

| Column | Content |
|---|---|
| `id` | `BIGINT`, the audit table's key. The event's 1-based position in the log's total order (below) over the **whole tape** — every row the log emits, across every source, one counter. A row-number, never a value-based rank: two rows tying the order key take consecutive numbers. Assigned above changeset resolution (a suppressed update consumes no number, so `id` is dense) and beneath the window predicate (so an event's number does not depend on which window or invocation exported it). The log's emitted row order is `ORDER BY id` |
| `item_type` | The population's **resolved** item-type: an events source's declared `item_type` override, else its kind's `source.kind_labels` label (the owner kind's label for a membership source, forming `<label>.<property>`), else the kind name verbatim (`<K>.<property>` for membership) — sidecar-derived by default, independent of which thing-tables are declared, unless an author declares it otherwise (§ Domain vocabulary). The contract identity everywhere item-type governs: this stamped column, the `item_id` dereference key, the union-safety gate's granularity, and the order-key component |
| `item_id` | For a records source: the record's identity in its own population's elected surface (`record_id` verbatim absent an election); on destroy rows the value comes from the identity join relation, not the fold's nulled after-image, so it is never NULL. For a membership source: the **owner** record's identity in the owner kind's election — the junction-owner-column render, per-row resolved for a sub-typed owner. Column type per the junction-member-column rule over the union of every source's resolved surfaces: the common declared type when all agree, else `VARCHAR` with `record_index` digit-rendered |
| `event` | `create` / `update` / `destroy` — deterministic recode of the folds' ops (`c`/`u`/`d`; `join` → `create`, `leave` → `destroy` of a membership in the named collection, recorded against the owner — `item_type` is what separates collection changes from the owner's own lifecycle rows) |
| `occurred_at` | Wallclock `TIMESTAMP` through the anchor renderer, or the events block's `render:` election — the log's one legal `render` key is its own instant column `event_sim_time`, a constant of the log's published contract rather than a reader question |
| `changes` | Serialized JSON text (codec `VARCHAR`): an object mapping each audited property's **output key** — its bare name, or its source's declared `rename` target (§ Domain vocabulary) — to `[old, new]` pairs — a membership reference field expands in place to its `<f>_kind` / `<f>_id` entry pair (the junction render's names, kind then id, each renamed in place by a `rename` targeting the bare field name; `only` / `ignore` still address the bare field name). Keys in sidecar column-declaration order of the *source* properties — rename relabels, never reorders; values are the folds' `CAST(… AS VARCHAR)` after-image strings verbatim or `null` — the row-state-events / membership-events rendering, the same strings streaming's payloads carry, never the conformance codec — reference-valued entries in the target's elected surface (below), and `<f>_kind` entry values rendered through `source.kind_labels` (§ Domain vocabulary). The JSON assembly (object construction, string escaping) is mode-owned SQL, rendered deterministically in the SELECT |

**The audited property set** per source: every `tracked` and `constant`-class
property of the kind (the temporally honest set — `slice_only` is
policy-omitted), narrowed by `only` or widened-by-subtraction via `ignore`
(mutually exclusive). The folds' selected-property set *is* the audited set,
`history_tracked` or not: a tracked-flagged property reads as-of each event from
its history rows; an untracked `constant`-class property renders the current
spine value in every after-image (the folds' type-1 path) — temporally honest
precisely because `constant` means current equals genesis. Selecting by
`history_tracked` instead of by class would silently drop untracked constants
from `create` / `destroy` changesets. For a membership source, the
element-schema fields. The discriminator is in the set: a `constant`-class
`prop__<K>_type` as any constant property, and the exempt sub-typed
discriminator despite a `slice_only` class (the export-wide carve-out —
exemption from the policy, not omission from the set); addressable by its bare
name `<K>_type` under `only` / `ignore`, and — creation-constant — it appears in
`create` / `destroy` changesets and never spawns an `update`. Each
policy-omitted `slice_only` property emits one `slice-only-column-omitted`
notice per events source.

| Event | `changes` content |
|---|---|
| `create` | Every audited property: `[null, value]` from the `c` after-image |
| `update` | Exactly the audited properties whose after-image value differs from the record's previous event's after-image: `[old, new]`. Coincident changes coalesce into one event (the fold's per-`(record, sim_time)` grain) |
| `update` where no *audited* property changed | The event row is suppressed — an audit log records what it tracks, nothing else |
| `destroy` | Every audited property: `[last value, null]` (old values from the preceding after-image) |
| `create` / `destroy` with an empty audited set | Emitted with `changes = {}` — the lifecycle event is itself information |
| membership `create` (join) | Every selected field: `[null, value]` |
| membership `destroy` (leave) | Every selected field: `[value, null]` — the leave carries what left |

Old values derive from the previous after-image per record (a lag over the
fold's own output, audited subset) — a deterministic reshape of fold values,
nothing recomputed from base state.

**Elected rendering inside `changes`.** An audited reference-valued property
renders its old/new values in the target population's elected surface — the
every-referencing-column rule applied to the diff — and a membership reference
field's `<f>_id` entry renders the member population's election (`<f>_kind` is
the qualifier, as in the junction render). Elected surfaces are
creation-constant, so the translation is one fan-out-free identity join per
referenced kind, horizon-free, applied before the lag (lag-then-translate and
translate-then-lag agree); a mixed-election target resolves per row through the
records-spine discriminator; `NULL` stays `NULL`. The edge union-safety gate
applies to every audited reference property exactly as to a tabular reference
column, and the uniqueness guard ranges over these composed relations as over
any the export composes. No type rule is needed: `changes` values are `VARCHAR`
strings, so a digit-rendered `record_index` is just another string. The
`changes` column is plain `VARCHAR` JSON text rendered in the SELECT, so writers
serialize it as any other column — no writer extension, no JSON column type.

**Identity semantics.** `(item_type, item_id)` names the **audited item** — the
polymorphic-reference idiom: the record for a records source, the owner's
collection for a membership source. It is a dereference key, not a per-row key:
an owner's collection logs one `create` per joining member under the same pair,
the association recovered from `changes`. `item_id` is a kind-targeted edge
render — structurally the junction member column, with `item_type` as its
qualifier: no identity-uniformity gate applies (it is not a thing-table identity
column), and no gate of any kind applies **across** item-types — `item_type`
makes cross-type collision structurally irrelevant, exactly as `<f>_kind` does.
**Per item-type** the edge union-safety gate runs over the resolved surfaces of
the **union of every source resolving to that item-type's addressed
populations** (a records item-type: the union of its kind's sources'
populations; a membership item-type: the owner kind's populations — the
junction-owner-column gate). The gate's granularity follows the dereference key,
not the declaration list: two sources addressing disjoint sub-types of one kind
share an item-type, so their elections must be union-safe *jointly* — two
bare-counter siblings both electing `presentation_id` are refused whether
declared in one source or split across two (`ElectionUnionUnsafe`, naming the
item-type). A mixed election within one item-type is legal exactly when
union-safe. Under `declare_keys` the log declares `PRIMARY KEY (id)` — `id` is
per-row unique by construction, so the key stands on the render rather than on a
contract guarantee or an author's claim ([`declared-keys.md`](declared-keys.md)
§ Key resolution per output table).

**`id` numbers this log's configured events.** It is the event's position among
the rows *this* log emits, not an absolute address for an event in the emit.
Narrowing the audited set with `only` / `ignore` suppresses update rows, and a
suppressed row consumes no number, so two configs over one emit number
differently. A selection-excluded record's events consume no number for the same
reason — a log numbers what it was configured to audit. The guarantee is
per-export monotonicity, not cross-export
identity — an application's audit-table key numbers what that application chose
to audit and is not comparable to another's.

**`id` is not a `seq`.** Three sequence numbers exist in the package and they
are deliberately distinct. The log's `id` orders by `(event_sim_time,
item_type, event_class, record_id, membership field tail)` over the log's
configured event set; the [stream](streaming.md)'s `seq` and the
[playback seam](playback.md)'s `seq` order by the seam's canonical order over
their own selections. The log and the stream differ only in the relative
priority of `event_class` and the source identity — both are deterministic,
both resolve the update-before-destroy tie identically (within one record the
source identity is constant), and they interleave distinct item types at one
instant differently. The log reports its own emitted row order rather than
adopting the seam's: a table whose key is not monotone in its own rows is not
what an audit table is. Hence `id`, the audit-table key, not `seq`, the seam's
word for a per-selection canonical position.

**Cross-kind legality.** The log is the one tabular output spanning kinds. This
does not breach same-kind-only tabular combination: that rule governs
*thing*-rows sharing a column shape; the log's columns are event-shaped and
`item_type`-qualified — the same reason a stream may interleave kinds on one
topic. A delivery lane for events, not a population table.

**Sources.** Each `events` source addresses populations exactly as a `tables`
entry does (whole kind, `sub_types` subset, or membership) and resolves with the
same errors. A `sub_types` subset narrows the fold's rows per record through the
records-spine discriminator (per-row population resolution — the discriminator
is creation-constant, so the filter is temporally honest at every event time),
the same device the `state` render's population filter composes. A source's
`where` — and, for a membership source, its owner `sub_types` — narrows which
records (or which owners' intervals) feed the stream by the same per-row device,
temporally honest at every event time because discriminators are
creation-constant and `where` columns are constant-gated (§ Row selection).
Sources resolve to **pairwise-disjoint** population sets (membership sources
distinct by `(kind, property)`) — one audit stream per population, so no event is
double-logged and no two *sources* contribute rows colliding on the order key.
Within one source the key still ties exactly where the input permits duplicate
rows (the contract's byte-identical membership intervals; a corrupter's
duplicated records rows), which is why the log's key is `id` and not a composite
of the rendered columns. A kind may be audited without having a
declared state table and vice versa — the log is its own declaration. Absent
`events` block: no log, and the emit's history is dropped from the export —
legal, author-declared dropping (a Type-1-only app), never an error. No new
derivation resident backs the log: it composes the existing row-state-events and
membership-events folds; the changeset diff and JSON serialization are source
render concerns, single-consumer, and live in the mode.

**Selection-aware disjointness.** Two sources can collide only where they audit
one item space — two records sources of one kind with overlapping population
sets, or two membership sources of one `(kind, property)`. Disjointness is
decidable from the config alone, so exactly these shapes establish it:

| Two sources auditing one item space | Result |
|---|---|
| Both declare a `where` entry on at least one **common column** whose two value sets are disjoint as **typed values** (a scalar is a one-element set) | Legal — no record can satisfy both, whatever their other entries do |
| Membership sources of one `(kind, property)` with both-declared, disjoint owner `sub_types` sets | Legal — the owner sub-type is the population axis, read per row through the parent lookup |
| Population sets already disjoint (records sources) | Legal — predicates are irrelevant to the gate |
| Any other shape — no common predicated column, every common column's value sets intersecting, or only one source carrying a selection | `SourceEventSourceOverlap` at plan time |

Legality is existential: one common column with typed-disjoint value sets
suffices, and entries the sources do not share — or shared columns whose sets
intersect — do not defeat a disjointness another common column establishes.
Value-set disjointness compares typed values, never written strings: each
element's plan-time cast result (§ Row selection) compares under the common
column's sidecar-declared type. Two spellings of one value (`'5'` and `'05'` on a
`BIGINT`) are one value, never a disjoint pair — string comparison would silently
license double-logging — and an element the declared type cannot cast never
reaches the gate, having already been refused as `SourceWhereValueUncastable`.
The gate consults no row data: it reads config literals and the sidecar's type
declaration only, so two predicates that happen to select disjoint rows while
sharing a typed value are still refused. Disjointness never implies coverage — a
record NULL on every common predicated column satisfies neither source and is
audited by neither.

### Domain vocabulary

Two opt-in declarations extend the event log's and junction render's
kind-name and `changes`-key surfaces with author-chosen vocabulary — the
reach no table/column declaration touches, because these are kind names and
property names rendered as *values*, not as columns. Both default to
engine-verbatim names (no invented mapping — CLAUDE.md Principle #7):

- **Per events source, `item_type` and `rename`.** `item_type` overrides
  that source's resolved item-type wholesale — per-population granularity,
  naming each split of a kind for the sub-type concept it actually
  represents (kinds are simulation machinery; sub-types are the first-class
  domain concepts — § Rationale). `rename` maps an audited property (or
  membership element field) bare name to its `changes` output key, mirroring
  the declared-table `rename` grammar; a reference field's entry renames its
  expanded `<f>_kind` / `<f>_id` pair in place.
- **`SourceConfig.kind_labels`** — one engine-kind → domain-label map,
  applied everywhere a kind name renders **as a value**: the default
  `item_type` of every events source (including the owner half of a
  membership source's `<label>.<property>` identity), `<f>_kind` entries
  inside `changes`, and junction `member__<f>__kind` column values. It is
  the only reach into surfaces carrying no per-source declaration — a
  junction's member-kind values are structurally kind-valued — and it
  carries the one-concept-kind case in a single declaration.

**Item-type resolution**, per events source, first match wins:

| Condition | Resolved item-type |
|---|---|
| `item_type` declared on the source | The declared string, verbatim |
| Records source, kind in `kind_labels` | The kind's label |
| Records source, kind not labeled | The kind name |
| Membership source, owner kind `K` labeled | `<label(K)>.<property>` |
| Membership source, owner kind not labeled | `<K>.<property>` |

**Item-type distinctness.** Records item-types (kind names) and membership
item-types (`<K>.<property>`) are distinct by construction absent aliasing;
aliasing makes collisions expressible, and the `(item_type, item_id)`
dereference idiom (§ The event log) decides which are legal — refused as
`SourceItemTypeCollision`:

| Condition | Result |
|---|---|
| Two records sources of one kind resolve one item-type | Legal — the union-safety gate (§ Identity and key election) runs jointly over their populations |
| Two records sources of different kinds resolve one item-type | Refused — two identity spaces behind one dereference key |
| A membership source resolves the same item-type as any other source | Refused — item-type is what separates collection changes from the owner's own lifecycle rows |
| Two records sources of one kind resolve different item-types | Legal — the union-safety gate re-partitions and runs per resolved item-type |
| A source's resolved item-type equals the **rendered name of another kind** (that kind's label, or its verbatim name when unlabeled) | Refused — one rendered name identifies at most one kind's population space, audited or not |

The rendered-kind-name clause ranges over the emit's whole kind universe,
the same range as label injectivity (below) — an unaudited kind's rendered
name still reaches the output through `<f>_kind` and junction member-kind
values. Override, label, and verbatim name are one vocabulary that must not
contradict itself; no layer outranks another.

**`changes` key resolution.** An audited property's output key is its
`rename` entry when declared, else its bare name; key order stays sidecar
column-declaration order of the *source* properties — rename relabels, it
never reorders. A membership reference field's rename renames its expanded
`<f>_kind` / `<f>_id` pair in place; `only` / `ignore` still address the
bare field name. Two properties resolving one output key, a `rename` key
naming a non-property or a narrowed-away property, or a `rename` key naming
a non-exempt `slice_only` property are each refused at plan time (never a
silent collision or drop) — the collision case joins the output-table /
output-column collision `SourceNameCollision` already covers (§ Validation
Rules).

**Kind-label rendering.** `<f>_kind` entries (event log) and
`member__<f>__kind` values (junction) render through
`build_kind_label_expr` ([`exporters/source/columns.py`](../../src/fabulexa_forge/exporters/source/columns.py))
— a compile-time `CASE` over the declared `(kind, label)` pairs with
**identity fall-through**: a value matching no pair (an unlabeled kind, or a
corrupted emit's mutated cell) renders verbatim, and `NULL` stays `NULL` —
the mapping is total, so a corrupter's defect surfaces unchanged, never
masked and never a render-time error. Inside `changes`, an `<f>_kind`
entry's `[old, new]` halves each render through the map independently — a
pure value recode commutes with the old-value lag. Byte-identical
passthrough when no labels are declared, mirroring the no-join composition
rule for default elections (§ Identity and key election). The mapping is
the same fidelity class as a table rename (CLAUDE.md Principle #3): the
value still traces to the base-layer kind name through a config-declared
bijection.

**Label vocabulary integrity.** `<f>_kind` disambiguates per row and a
junction may admit several kinds, so the *rendered* kind vocabulary must
stay injective over the emit's whole kind universe — not just kinds in
declared tables, since a member field's admitted kind universe is not
bounded by the declaration list: every `kind_labels` key names a sidecar
kind (`SourceKindLabelUnknown`), two kinds cannot map to one label
(parse-time), and a label cannot equal the rendered name of another kind
(`SourceKindLabelCollision`).

**Ordering consequence.** `item_type` is a component of the log's order key
(§ Ordering and determinism); the key uses the *resolved* item-type, so
declaring `item_type` or `kind_labels` can reorder events that share an
instant across item-types and therefore renumber `id` — within the log's
existing per-export-monotonicity contract ("two configs over one emit
number differently", above), not a new guarantee.

Vocabulary resolution is compile-time and window-invariant — no interaction
with incremental export beyond the existing rule that the window
fingerprint binds the config; touches no key column under `declare_keys`;
and is a render-time presentation concern the derivations folds and the
reader never see.

### Identity and key election

Which identity surface each column carries is the cross-mode key-election
surface's contract ([`key-election.md`](key-election.md) § Rendering: source).
The source-mode summary:

| Rule | Behavior |
|---|---|
| State-table identity | The identity column renders the elected surface of the table's populations. The uniformity gate requires every population combined into the table to elect one surface; union safety applies under a uniform `presentation_id` election. Both run at plan time over *declared* tables |
| Mixed-election kind | Legal — declare per-population tables; each table's populations elect uniformly |
| Reference / junction-member columns | Render the target population's elected surface per row (kind-targeted mode semantics; the edge union-safety gate unchanged) |
| Event log `item_id` / `changes` | Kind-targeted edge renders: the edge gate runs per item-type over the union of its sources' addressed populations, and per audited reference property over its target — no gate across item-types (§ The event log) |
| `declare_keys` | State tables declare the identity-column primary key; `presentation_id` uniqueness follows `combined_claim` over the table's **resolved population set** — the registry algebra applied to exactly the populations the table combines ([`declared-keys.md`](declared-keys.md) § Key resolution per output table). The event log declares `PRIMARY KEY (id)`; junction tables declare nothing |

### The `slice_only` posture

Auto-projected surfaces — the `state` render's classified projection and the
event log's audited set — narrow to `tracked` + `constant` columns plus the
exempt sub-typed discriminator, per the export-wide policy
([`slice-only.md`](slice-only.md)): a non-exempt `slice_only` column is
**omitted** with one `slice-only-column-omitted` [notice](notices.md) per column
per unit (per events source for the log), in plan order. A *declaration* entry —
`columns`, `rename`, `only`, `ignore` — naming a non-exempt `slice_only` column
is refused (`SourceSliceOnlyRead`): the entry is unsatisfiable, an error rather
than a silent ignore. A `where` key is refused by the stricter constant-class
gate instead (`SourceWhereNotConstant`, § Row selection), which admits only
`constant` and so covers `slice_only` and `tracked` under one message.

Omission is column-projection-only: row sets, ordering, and incremental window
membership are identical with or without it. The junction render is untouched
(membership columns carry no class).

### Operational presentation defaults

Source output looks like a real system's tables. Every column default in the
render sections above is **derived** from sidecar identity — never invented —
and overridable via per-table `rename`. Payload columns keep their sidecar
DuckDB types untouched: the mode cannot know a `BIGINT` property is a duration
or a count, so it renders only the *structural* sim-time columns as wallclock.
Which structural columns those are is the reader's answer, not source's — the
renders read the instant-carrying columns of their table category off the
structural-temporal surface ([`reader.md`](reader.md) § The structural-temporal
surface); the *names* those instants take in output (`created_at` /
`updated_at` / `deactivated_at` / `joined_at` / `left_at` / `occurred_at`) are
source's presentation policy.

**Collision policy.** After defaults and renames resolve: two output tables with
one name (the event log's included), or two columns of one table with one name,
is `SourceNameCollision` — an error at plan time, never a silent suffix or drop.
Every `rename` key is source identity — the source column name — never a derived
output name, precisely so a default-name collision is always resolvable.

### Presentation-name posture

`last_mutation_sim_time` is a sim-internal bookkeeping column — a high-water
mark over a record's content lifecycle. Its **value** channels freely: it is the
`updated_at` presentation default. Its **raw name** never reaches output: the
name is a reserved output column name (the shared check in
[`exporters/reserved_names.py`](../../src/fabulexa_forge/exporters/reserved_names.py)),
so a `rename` targeting that output name is refused at plan build. This is the
companion of the playback seam's posture ([`playback.md`](playback.md) § The
recorded trail); [`dimensional.md`](dimensional.md) carries the same
reserved-output-name check on its author-named columns.

### Wallclock timestamps: the anchor is required

Every structural sim-time column renders through the effective anchor via the
shared renderer (`render_anchor_temporal_expr`) — byte-identical rendering
semantics to every other wallclock mode, same precedence (CLI → config `rebase`
→ sidecar `runtime`), same DST and ambiguity failure rules
(see [`anchor.md`](anchor.md)). Source **requires** a resolved anchor — a
requirement independent of, and stricter than, the general anchor-required-
for-an-explicit-election rule ([`temporal-elections.md`](temporal-elections.md)
§ Anchor requirement): source has no default-rendering fallback path at all:

| Anchor resolution outcome | Result |
|---|---|
| `EffectiveAnchor` resolves (sidecar runtime, possibly overridden) | Export proceeds; all structural sim-time columns are wallclock `TIMESTAMP` |
| No anchor resolves (`None`) | Error `SourceAnchorRequired` — an operational dump never shows ns offsets; silently emitting raw integers would be a fallback |

A `render` key on a declared table (`state` / `junction`) must name an
instant-carrying structural column of the table's category, resolved
through the reader's structural-temporal surface
([`reader.md`](reader.md) § The structural-temporal surface) — never a
hardcoded list — and must name a column the render actually emits;
`last_mutation_sim_time` is outside the key domain under a windowed
invocation, where the render omits `updated_at`
(`RenderKeyIsInstantColumn`, [`temporal-elections.md`](temporal-elections.md)
§ Validation Rules).

### Ordering and determinism

The exporter is a pure function of `(emit, config, code version)`. Every emitted
table carries a total `ORDER BY` over raw sim-time keys and identity — never
over rendered timestamps (microsecond truncation would make ties
nondeterministic):

| Render | Total order |
|---|---|
| `state` | `(created_sim_time, record_id)` |
| `junction` | `(record_id, joined_sim_time, field columns in element-schema declaration order, VARCHAR-compared, NULLS FIRST)` |
| event log | `id`, which is the row-number of `(event_sim_time, item_type, event_class, record_id, membership fields in element-schema declaration order, VARCHAR-compared, NULLS FIRST)` — the folds' raw keys (`event_class` is the folds' own ordering ordinal) with `item_type` (the *resolved* item-type — § Domain vocabulary) interposed to disambiguate across sources. Sorting by the ordinal rather than restating the key is what keeps emitted row order monotone in `id` where the key ties (§ The event log) |

### Delivery

`--fmt csv` writes one `<table>.csv` per output table into the output directory;
`--fmt duckdb` writes one typed table per output table into a single `.duckdb`
file. Both via the shared writer dispatch (`exporters/query_spec.py`),
materializing through `Emit.query_arrow`; see [`writers.md`](writers.md). A
zero-row table is still emitted (header-only CSV / empty typed table). The
return contract matches every other mode: a mapping of every output table name
to its row count.

### Corrupter composition (the dirty source dump)

`corrupt → source` is a pipeline, not a feature: corrupter output is
structurally-conformant base shape (C1–C5, C8 preserved), and the source mode
reads only sidecar-declared structure, so a source export over a corrupted emit
yields a dirty operational dump with `defects.json` as the label-grade answer
key — the data-cleaning teaching corpus. Injected defects flow through
faithfully: schema-drifted columns export under their drifted names (the sidecar
is regenerated), duplicated/deleted/phantom rows land in the dump, mutated and
nulled values ride the state tables and the log's after-images, dangling
references survive as unjoinable ids, distorted intervals land in the junction
tables. No corrupter-aware branch exists in the source mode; the guarantee holds
by construction, verified by
[`tests/integration/test_corrupt_source.py`](../../tests/integration/test_corrupt_source.py).

The event log numbers whatever emit it is given. Removed history rows and
dropped events renumber densely; added rows — a duplicated record, a phantom
insert — take numbers of their own. Two consequences follow. The log's `id` is
dense, monotone, and per-row unique whatever the emit, because the render
constructs it, so the declared primary key survives a corrupted emit where a
contract-guarantee key would not: a duplicated `record_id` lands as two
distinctly-numbered rows rather than a load failure. But determinism under the
order key's ties is scoped to a conformant emit. A `duplicate_rows` copy in
`mutation` mode shares its original's `(record_id, created_sim_time)` with
different property values, so the two `create` events tie the order key
completely and render *different* `changes`; which takes the lower `id` is
observable. The arbitrariness belongs to the order key, not to the ordinal —
the two rows tie the key whether or not it is published as a column — and
growing the key to cover a corrupted emit's non-unique `record_id` would be the
mode branching on damage.
Over a corrupted emit the export is as deterministic as the emit it was given,
and `defects.json` names the injected duplicate.

### Incremental composition

`--next` / `--from` / `--to` work over source exports through the cross-mode
driver (see [`incremental.md`](incremental.md)) — window math, cursor,
fingerprint, drained detection, labels, empty-window emission, and staging are
its shared mechanics. The source mode contributes its windowed compile
(`build_source_query_specs`) and the per-render window membership below. Window
membership tests run on raw sim-time ns, half-open `[start_ns, end_ns)`.

| Render | Window key | Behavior per window |
|---|---|---|
| `state` | — (snapshot class) | One full-table snapshot per window, reconstructed at the window horizon through the state-at derivation: rows with `created_sim_time < end_ns`; tracked properties as-of the horizon; `constant` properties current (the declared temporal-honesty exception); lifecycle horizon-rendered (`active` / `deactivated_at`); **no `updated_at`** — `last_mutation_sim_time` at a past horizon is not faithfully reconstructible, so the column is omitted rather than fabricated. `replace` in DuckDB, re-emitted per CSV drop |
| event log | `event_sim_time` | Append event rows with key ∈ window, computed over the full fold — the `changes` lag's previous after-image may predate the window; window membership selects rows, never alters their content (events are immutable and final). `id` is assigned over the whole tape beneath the window predicate, so a window's rows carry the `id` values a full export of the same tape gives them; because the order is time-major, those values form a contiguous ascending block (§ `id` under incremental below) |
| `junction` | activity (`joined_sim_time`, `left_sim_time`) | Extract-on-change, `left_at` horizon-masked (below) |

Row selection is window-invariant across all three renders, because the columns
it reads are constant-gated (§ Row selection). The per-window state snapshot
applies the same predicate at every window, so a record's presence across windows
varies only by its lifecycle (`created_sim_time`), never by predicate
re-evaluation. Junction extract-on-change runs over the narrowed interval set,
with activity keys and `left_at` horizon-masking unaffected by it, so an interval's
membership in the table never varies by window. The event log's window membership
selects among the selection-narrowed event set, with `id` assigned over that
whole-tape narrowed set beneath the window predicate — numbering stays dense,
tape-anchored, and invocation-invariant.

A full (non-incremental) export renders `state` as the current records read
*with* `updated_at`; the windowed shape differs by exactly that one omitted
column, a documented consequence of horizon honesty. An explicit `columns` /
`rename` entry naming `last_mutation_sim_time` is therefore unsatisfiable under
a windowed invocation and errors (`SourceColumnUnresolved`, the message naming
the horizon-honesty omission) — never a silent drop. The refusal is plan-time:
windowed-ness is an invocation fact, so the caller passes it to
`build_source_plan` (`windowed`), which validates every declaration against the
shape this invocation actually delivers. The incremental estate is the
real-world archetype whole: nightly full extracts of app tables plus an appended
audit log plus upsert-shaped junction activity — the no-CDC teaching shape.

**`id` under incremental.** The log's numbering is anchored at the tape's start,
not the window's. The number is assigned over the log's whole-tape row set and
the window predicate applied afterward, so an event carries the same `id`
whichever invocation exports it, and a re-run reproduces it. Successive windows
from the tape's first event concatenate into a dense prefix `1 .. N` with no
renumbering and no overlap; an empty window contributes no rows and consumes no
number. A range export starting mid-tape (`--from` after the first event) begins
above 1, and the front gap is the honest report that earlier events exist and
were not exported — tape-anchoring is what makes the number stable, and
window-local numbering would trade that away. The rendered SQL therefore places
the numbering at its own query level *beneath* the window predicate: SQL
evaluates `WHERE` before window functions, so a row-number computed beside the
predicate would silently yield window-local numbers, a failure invisible on a
full export where the two forms agree.

**Junction extract-on-change.** A membership interval emits a row in each window
containing membership *activity* — its join, its leave, or both:

| Condition | Emission |
|---|---|
| `joined_sim_time` ∈ window | The interval row, with `left_at` **horizon-masked**: rendered only if `left_sim_time < end_ns`, else `NULL` (the leave is future state at this horizon) |
| `left_sim_time` ∈ window and `joined_sim_time` in an earlier window | The interval row re-emitted, `left_at` set |
| Both in one window | One row, `left_at` set |
| Neither in the window | No row |

A closed interval therefore appears at most twice — once open, once closed — and
the later row supersedes the earlier under the natural merge key
`(owner id, member fields, joined_at)`. This is the upsert-extract shape real
source systems deliver; merging it is the teaching exercise. In the DuckDB
warehouse both rows accumulate (append-only); in CSV each window's drop carries
its own activity. Horizon-masking is the one place a source value is
window-dependent, and it is masking (withholding future state), never
recomputation. A full export carries unmasked values: `left_at` is the base
value, one row per interval.

Bookkeeping reserved names (the DuckDB `_export_meta` / `_export_windows`
tables, the `__rows` suffix, `__valid_from_ns`) are reserved for source output
table names under the existing cross-mode rule
(`exporters/reserved_names.py`), enforced at plan build so a full export and a
later incremental drip on the same target agree. The SCD-2 `valid_to` view
machinery is dimensional-only; no source render uses views.

### `init --mode source` inference contract

`init` proposes; the author edits and owns. `generate_source_init_config` is a
pure function of `(emit, code version)` emitting a commented candidate config.
It consumes kinds, discriminator domains, membership tables, per-column temporal
classes, and the `presentation_keys` registry — **not** `record_roles`. An emit
predating per-column `history_tracked` flags is refused
(`SourceHistoryTrackedRequired`) — a candidate config that cannot export is not
proposed. Proposal order follows the sidecar's table declaration order. Proposed
names are verbatim; when two proposals resolve one name (underscore-bearing
identifiers), the later proposal (sidecar declaration order) is emitted
commented-out with a comment naming the collision — the emitted config always
parses and plans clean, the key-election `init` self-gating posture. Splits are
proposed exactly where the sidecar declares the partition — deterministic from
the discriminator domain, with no value read — and nowhere else: no `where` is
proposed on any unit.

| Emit condition | Proposal |
|---|---|
| Each `records__<kind>` table, flat (no declared `<kind>_type` domain) | One state table: `name: <kind>`, `kind: <kind>` |
| Each `records__<kind>` table, sub-typed (`Sidecar.subtype_values(kind)` non-empty) | One state table per declared sub-type — `name: <kind>_<sub_type>`, `sub_types: [<sub_type>]` — `init`'s default split, matching dimensional's per-sub-type stubs. The first sub-type's stub carries a header comment naming the full domain; the last carries a commented combine-alternative (one shared table across every sub-type, `sub_types:` omitted) for a kind whose sub-types share an identical column set |
| Each `membership__<K>__<p>` table, `K` flat | One junction table: `name: <K>_<p>` |
| Each `membership__<K>__<p>` table, `K` sub-typed | One junction stub per declared sub-type — `name: <K>_<sub_type>_<p>`, `sub_types: [<sub_type>]` — aligned with the owner's per-sub-type state stubs; the last stub carries a commented combine-alternative (one whole junction, `sub_types:` omitted), mirroring the state stubs' posture |
| ≥ 1 kind with a class-`tracked` property | One `events` stub named `versions`, one active source entry per such kind; membership sources and lifecycle-only kinds (no tracked property — spine `create` / `destroy` only) appended as commented-out source entries |
| Membership event-source entries (commented-out), owner sub-typed | One commented entry per declared sub-type (`sub_types: [<sub_type>]`). Uncommenting the full set is plan-clean: the entries share the default item-type `<K>.<p>` under the item-type sharing exception, and their both-declared disjoint `sub_types` sets satisfy the overlap gate |
| No kind carries a tracked property | The `events` stub is emitted fully commented out (name and every per-kind source), under a comment noting the emit's auditable history is lifecycle-only; uncommenting opts in |
| The registry declares a population | The `keys` proposal per the key-election `init` contract ([`key-election.md`](key-election.md) § `init` proposals), aligned with the declared tables |
| Non-exempt `slice_only` columns | Never proposed; one `slice-only-column-omitted` notice each |

## Invariants

1. **Declared intent drives output.** Every output table exists because a
   declaration names it; a declared table is emitted even when empty. No table
   layout is inferred from sidecar classification — sidecar facts gate
   declarations, never decide layout.
2. **Every projected column classifies.** Within a state table every records
   column resolves to a records-column taxonomy role; a no-role column is
   `SourceUnclassifiedColumn` at plan time — never a silent pass-through or a
   raw leak into output.
3. **Faithful reshaping.** Every output value traces to a base-layer value or a
   deterministic recoding of one (a cast, a wallclock render, a horizon mask, a
   lag over fold output, a row-number over an order those same values
   determine); the mode fabricates nothing (CLAUDE.md Principle #3). The
   row-number is the one recoding that is a function of a row's *position*
   among the others rather than of its own cells, and it is inside the
   principle because the line the principle draws is invention, not arity: the
   lag also reaches outside the row, and a row-number reaches no further than
   the `ORDER BY` the render computes and emits under. The position is
   published, not manufactured.
   A source export over a corrupted emit surfaces the corrupter's declared
   defects unchanged, never manufacturing new ones.
4. **Wallclock rendering requires a resolved anchor.** Unlike other modes'
   raw-integer fallback, source refuses (`SourceAnchorRequired`) rather than
   emit ns offsets.
5. **Total order over raw sim-time, never rendered timestamps.** Every emitted
   table carries a deterministic `ORDER BY` over raw ns keys and identity, so
   microsecond truncation in wallclock rendering cannot introduce ties. The
   event log sorts by `id` rather than restating its key, which does not weaken
   this: `id` is the row-number *of* that raw-ns order, computed over the raw
   keys and never over the rendered `occurred_at`.
6. **No event is double-logged.** Event-log sources resolve pairwise-disjoint
   population sets, so each population feeds exactly one audit stream and no two
   sources contribute rows colliding on the log's order key. The key is total
   *up to permitted duplicates*, not unconditionally: within one source it ties
   exactly where the input permits duplicate rows — the contract's
   byte-identical membership intervals, a corrupter's duplicated records rows.
7. **The log's `id` is total, dense, and tape-anchored.** It is 1-based and
   gapless over a full export; monotone in emitted row order in every export and
   every window, which the outermost `ORDER BY id` mechanizes where the order
   key itself ties; invariant across which window or invocation exported the
   event; and per-row unique by construction, including across the ties in
   invariant 6. Constructed values cannot be falsified by the data, which is
   what makes the declared primary key honest over a corrupted emit.
8. **Over a conformant emit every log column is a function of the order key.**
   Two rows tying the key therefore render identically, so which takes the lower
   `id` cannot change the emitted bytes. This binds any column added to the log
   later: either it is a function of the key, or the key grows to cover it. It
   is scoped to conformant emits because a corrupter's conflicting duplicate
   falsifies it by construction (§ Corrupter composition).
9. **Windowed `state` is horizon-honest.** The per-window snapshot reconstructs
   at the window horizon through the state-at derivation; `updated_at` is
   omitted rather than fabricated, and a declaration naming it under a windowed
   invocation is refused, never silently dropped.
10. **Determinism.** Same emit + export config + code version → identical output
    (CLAUDE.md § Key Invariants). `init` output is likewise a pure function of
    `(emit, code version)` and always parses and plans clean. Where the log's
    order key ties, determinism rests on invariant 8.
11. **`slice_only` omission is column-projection-only.** Row sets, ordering, and
    window membership are invariant under the policy; omission never suppresses
    an output table ([`slice-only.md`](slice-only.md)).
12. **Row membership is horizon-invariant.** No output row's membership in a
    declared table, junction, or audit stream depends on the horizon, the
    window, or the invocation. Over a conformant emit the constant-column gate
    and the creation-constant discriminator guarantee it, not evaluation
    discipline. Every narrowing path nonetheless evaluates the records spine's
    current values — the state-at type-1 render, the per-row spine resolution,
    the parent lookup — so even over a corrupted emit, where a mutated
    "constant" falsifies current-equals-genesis, selection stays consistent
    across windows and invocations: a corrupter can change *which* rows a
    predicate selects, never make the selection horizon-dependent.
13. **Selection is value-blind at plan time.** Every `where` check — the class,
    discriminator, castability, and disjointness gates, and the domain notice —
    reads sidecar declarations and config literals only. The casts included:
    they type config literals by the sidecar's declaration and read no rows.
14. **Selection filters, never transforms.** A `where` or an owner `sub_types`
    changes which rows render; it never changes a rendered value, an ordering
    key, or the log's tape-anchored numbering rule. The parent lookup in
    particular projects nothing.

## Validation Rules

Field shapes are defined by the Pydantic grammar in
[`config/models.py`](../../src/fabulexa_forge/config/models.py); business-rule
message text is owned by
[`exporters/source/plan.py`](../../src/fabulexa_forge/exporters/source/plan.py).
The rules below state *what* is rejected and *when*.

**Parse-time (Pydantic).**

| Validator | Rejects |
|---|---|
| `source_section_required` (`SourceConfig`) | A `mode: source` config declaring no output — no `tables` entry and no `events` block (two-sided with the other modes' sections; there is no bare zero-config dump) |
| `table_shape` (`SourceTableDecl`) · `source_shape` (`SourceEventSourceDecl`) | Anything but exactly one of `kind` / `membership` per declaration; empty `name` / `columns` / `rename` / `sub_types` / `only` / `ignore`; non-distinct entries; non-distinct `rename` values; `only` and `ignore` together; an events source's empty `item_type`; a `rename` or `where` mapping present-but-empty or carrying an empty key (and, for `rename`, an empty value). `sub_types` is valid with either population source; per-entry `where` value emptiness and duplication ride `PredicateValue` ([`row-predicates.md`](row-predicates.md)) and are reported at the offending entry's path |
| `events_shape` (`SourceEventsDecl`) | An empty log `name`; an empty `sources` list. The single log is structural — `events` is one optional field, never a list |
| `table_source_exclusive` (`SourceConfig`) | Two `tables` entries sharing one `name` — the cross-declaration check the per-declaration validators cannot see |
| `kind_labels_shape` (`SourceConfig`) | An empty `kind_labels` map; an empty key or value; two kinds mapping to one label |

**Business rules.** Run at plan time against the open emit, before any write;
each raises an `ExportError` subclass surfaced through the CLI's existing error
funnel. `{owner}` in a message is the declaring unit's label: `table '<name>'`
for a `tables` entry, `events source #<n>` (1-based, declaration order) for an
`events` source.

| Rule | Checks | Error |
|---|---|---|
| `SourceTableKindUnknown` | Every declared `kind` has a `records__<kind>` table | `"{owner}: kind '{kind}' not in this emit"` |
| `SourceTableSubTypeUnknown` | Every `sub_types` entry is in the kind's discriminator domain — the **owner** kind's for a `membership:` declaration | `"{owner}: sub_type '{sub_type}' not declared for kind '{kind}'"` |
| `SourceSubTypesOnFlatKind` | `sub_types` only on a sub-typed kind — the owner kind for a `membership:` declaration | `"{owner}: kind '{kind}' declares no sub-types"` |
| `SourceWhereColumnUnresolved` | Every `where` key resolves to a payload property of the declaring unit's subject kind (source-name form on records-backed tables, bare form on events sources and membership units). Structural columns and membership element fields are not payload properties and fail here | `"{owner}: where key '{key}' not a payload property of kind '{kind}'"` |
| `SourceWhereNotConstant` | Every resolved `where` column's `temporal_class` is `constant` | `tracked`: `"{owner}: where key '{key}' is temporal_class: tracked; under a horizon reconstruction its as-of and current values select different rows — row selection requires a constant column"`. `slice_only`: `"{owner}: where key '{key}' is temporal_class: slice_only; its past is unknowable, so row selection cannot read it"` |
| `SourceWhereOnDiscriminator` | No `where` key names the subject kind's declared discriminator | `"{owner}: '{key}' is the sub-type discriminator; select sub-types via sub_types, not where"` |
| `SourceWhereValueUncastable` | Every `where` element casts to its resolved column's sidecar-declared DuckDB type, constant-evaluated on every `where`-bearing unit (§ Row selection); the disjointness gate reuses these typed results | `"{owner}: where value '{element}' for '{key}' does not cast to {type}"` |
| `discriminator-value-unobserved` (notice, per element) | For a `where` column with a declared `enum_domains` entry, each predicate element outside the domain draws one notice in config element order — never an error; a column with no entry is unchecked. Message granularity as dimensional's: a scalar, or a list no element of which is in the domain, states the unit will render no rows; a partially-covered list states only that the element contributes none | Through the [notice channel](notices.md), naming `{owner}`, `{key}`, and the element |
| `SourceTableMembershipUnknown` | Every `membership` reference resolves to a sidecar membership table | `"{owner}: no membership table for ({kind}, {property})"` |
| `SourceColumnUnresolved` | Every `columns` / `rename` key resolves on the table's source surface — a state table's identity column by its elected surface's contract name only, the junction owner column by its source name `record_id` whatever surface it carries, and `last_mutation_sim_time` only on a non-windowed invocation (the windowed state render omits it); every `only` / `ignore` entry names a property (element field) of its source; an events source's `rename` key names an audited property (element field) of its source, surviving `only` / `ignore` narrowing | `"{owner}: '{entry}' not a column of its source"` (the unrendered-surface, windowed-`updated_at`, and narrowed-away-rename-key cases name the election / omission / `only`-or-`ignore` entry) |
| `SourceColumnNotAddressable` | No `columns` / `rename` entry names `fork_path` / `ref_index__*`, or `record_index` other than as the table's elected surface; no `columns` entry names the table's elected surface (identity is election-governed) — a non-elected, unrendered surface name (`record_id` under a `presentation_id` election) is `SourceColumnUnresolved` instead | `"table '{name}': '{column}' is not addressable here"`, naming why |
| `SourceEventSourceOverlap` | `events.sources` resolve pairwise-disjoint population sets (membership sources distinct by `(kind, property)`); two sources auditing one item space are disjoint only via both-declared disjoint owner `sub_types` sets or a common predicated column with typed-value-disjoint value sets (§ The event log — selection-aware disjointness) | `"events: sources overlap on population '{population}'"`; the selection case appends `"; selections do not establish disjointness"` |
| `SourceKindLabelUnknown` | Every `kind_labels` key has a `records__<kind>` table in the sidecar | `"kind_labels: kind '{kind}' not in this emit"` |
| `SourceKindLabelCollision` | After labeling, kind → rendered name is injective over the emit's whole kind universe (a label equals no other kind's label and no unlabeled kind's own name) | `"kind_labels: label '{label}' collides with kind '{kind}'"` |
| `SourceItemTypeCollision` | Resolved item-types are pairwise distinct across sources, except that sources auditing one item space may share one — records sources of one kind, and membership sources of one `(kind, property)`; and no resolved item-type equals the rendered name of another kind (of any kind, for a membership source) — ranged over the emit's whole kind universe (§ Domain vocabulary) | `"events: sources #{m} and #{n} resolve one item_type '{item_type}' over two audited item spaces"`; the rendered-name clause: `"events source #{n}: item_type '{item_type}' collides with kind '{kind}'"` |
| `SourceSliceOnlyRead` | No `columns` / `rename` / `only` / `ignore` entry names a non-exempt `slice_only` column. `where` keys are outside this rule's population — `SourceWhereNotConstant` refuses them, with the message the selection surface needs | Names the entry, the column, and the omission reason |
| `SourceUnclassifiedColumn` | Every projected records column classifies to a taxonomy role ([`reader.md`](reader.md) § The records-column taxonomy) | Names the table and column |
| `SourceAnchorRequired` | An `EffectiveAnchor` resolved | `"source export renders wallclock timestamps and requires a resolved anchor: the emit declares no runtime block; supply rebase.base_date/timezone or --base-date/--timezone"` |
| `RenderKeyIsInstantColumn` | A declared-table `render` key names an instant-carrying structural column of the table's category (reader-sourced); the event log's key domain is mode-definitional (`event_sim_time` only). A key must also name a column the render emits ([`temporal-elections.md`](temporal-elections.md)) | `"render key '{column}' on '{table}': not an instant-carrying structural column of this table"` |
| `DateParseSourceColumn` | A declared `date_parse` source is a declared VARCHAR payload column, read from the sidecar type directly, and not `slice_only` ([`temporal-elections.md`](temporal-elections.md)) | `"date_parse column '{column}' on '{table}': source must be an existing VARCHAR column (got {type})"` |
| `SourceNameCollision` | Output table names (the event log's included) and per-table column names unique after defaults + renames; within one events source, resolved `changes` keys are distinct after renames (a membership pair's expanded `_kind` / `_id` names included) | `"output name collision: {names}; resolve via rename"`; the `changes`-key case: `"{owner}: changes key collision: {keys}; resolve via rename"` |
| Reserved-name check (`exporters/reserved_names.py`, raised as `ExportError`) | No output table name collides with bookkeeping names / suffixes; no output column named `last_mutation_sim_time` (§ Presentation-name posture) — checked at plan build over all output names, so a full export and a later `--next` on the same target agree | — |
| `ElectionMixedIdentity` / `ElectionUnionUnsafe` | Identity gates per declared table; edge gates per referencing column, per event-log **resolved** item-type (over the union of its sources' addressed populations; the owner kind's for a membership item-type), and per audited reference property; no gate across item-types (polymorphic identity) | Per [`key-election.md`](key-election.md) |
| `SourceHistoryTrackedRequired` | The sidecar carries `history_tracked` flags (the events render and the windowed state snapshot consume them) | `"source export requires per-column history_tracked flags; this emit predates them"` |
| `TemporalClassUnavailableError` (reader-owned; see [`reader.md`](reader.md)) | Every consulted flagged column declares an in-enum `temporal_class` — audited-set resolution and the row-selection gate alike — a C13 breach surfaced on the consuming path | `"… declares history_tracked but no temporal_class; the emit is non-conformant (C13). Run \`fabulexa-forge validate\`."` |
| Single-branch guard (`derivations/guard.py`, cross-mode) | Exactly one branch | — |

`declare_keys` (`SourceConfig`, optional boolean, default false) is the opt-in
key-declaration capability: state tables declare the identity-column primary key
plus `presentation_id` uniqueness per `combined_claim` over the table's resolved
population set; the event log declares `PRIMARY KEY (id)`; junction tables
declare nothing. Resolution rules,
writer semantics, CSV posture, and incremental gating are owned by
[`declared-keys.md`](declared-keys.md).

## Rationale

- **The author declares layout; the sidecar gates it.** The sidecar's
  `record_roles` vocabulary (`actor` / `entity` / `resource`) is state-machine
  taxonomy — simulation machinery, not output vocabulary. Classifying output
  layout from it outsources this package's entire job (defining output shape) to
  the producer's internals, and leaves the author no lever: no way to combine
  two sub-types into one table, split a kind, or choose which tables exist.
  The declared-table grammar puts the lever in the config; sidecar facts answer
  only "may this declaration resolve?".
- **The partition line is author-declared, not sidecar-declared.** A producer's
  declared sub-type domain marks structurally different things, and where it
  exists `sub_types` expresses the split. But a kind can carry a de facto
  discriminator the producer never declared — an interleaved table whose rows
  belong to different subsystems with different lifecycles and different
  consumers — and a mode whose thesis is output-that-looks-like-a-real-system
  cannot call that shape someone else's problem. Realism decides: the split must
  be expressible, and the author, who knows which constant properties are de
  facto discriminators, is the one who draws it. What that costs is bounded by
  what the sidecar keeps: layout is author-declared, the sidecar's
  contribution is a gate (the constant class) and never a decision, and `init`
  proposes no `where`, so the mode never manufactures a value-drawn split. The
  line is determinism — propose from declared structure, never from observed
  values. Nothing forces a split either, so the analytical anti-pattern
  (table-per-enum-value over a genuine enum axis) requires an author to spell it
  out deliberately, the same trust the mode already extends over table naming
  and `columns` selection.
- **The gate is on the column's class, not on the horizon.** A predicate on a
  `tracked` property is ambiguous under horizon reconstruction — the
  as-of-the-horizon value and the current records value select different row
  sets — and the mode's windowed state snapshots pose that question where
  dimensional's records grain never does. Restricting keys to `constant`-class
  properties makes the question unposable rather than picking an answer, and it
  is what buys horizon-invariant row membership (invariant 12) by construction
  instead of by evaluation discipline.
- **Selection splits the estate, not just the table.** State tables, junctions,
  and events sources all take selection because splitting one without the others
  leaves an undivided surface covering both halves — an association table whose
  owner column points into a different table row by row, or one audit stream
  spanning two concepts — the exact incoherence row selection exists to remove.
  Membership units read the owner because their rows carry no owner attributes;
  discriminator splits spell `sub_types` and constant-property splits spell
  `where` on every declaring unit, one rule with no membership-only carve-out.
- **The key axes error; the value axis notices.** Whether a `where` value is
  checkable at all is a producer choice: `enum_domains` covers a constant
  property only where the scenario declared it closed-domain, and a de facto
  discriminator typically declares none — the same non-declaration that left it
  without sub-types. A hard out-of-domain error would make the mode's strictness
  lottery-shaped (a typo on a registered column refuses the export while a typo
  on an unregistered one silently empties a table) and would diverge from
  dimensional's posture that a declared-but-unobserved value is a legitimate way
  to write one config against a family of emits. Castability is the one
  value-shaped error and is not an exception to that: out-of-domain is
  unobserved-but-possible, uncastable is impossible under the declared type in
  every emit of the family, and deferring it to the rendered `CAST` would crash
  the export at query time, after "plan time, before any write". The residual
  net for unregistered columns is the run-and-profile authoring workflow, not
  the gate.
- **The log publishes its order in a column.** A relation has no inherent row
  order, so an order expressed only as physical row order is lost the moment a
  consumer loads the export and re-`SELECT`s it. None of the rendered values
  recovers it: `occurred_at` is a wallclock `TIMESTAMP` whose microsecond
  precision collides distinct nanosecond events, and `event` is a word, not an
  ordinal — sorting by `(occurred_at, event)` puts `destroy` before `update`
  alphabetically, the exact inverse of the computed order. The before-images do
  not supply it either: recovering the order from them means deducing, per tied
  pair, that a destroy's old value equals the preceding update's new value, and
  membership events carry no chained before-image to deduce from at all. A
  consumer replaying the log to reconstruct state would get wrong answers with
  nothing in the data to signal it. `id` is the ActiveRecord audit-log idiom:
  `paper_trail`, `audited`, and Django's `auditlog` all key their polymorphic
  versions table by an auto-incrementing `id`, and this log matches that table
  column-for-column — `id` is the key every one of them orders by.
- **The outermost sort is `ORDER BY id`, not a restatement of the key.** The two
  agree wherever the key is injective, and the key is not injective everywhere
  (invariant 6). For a tied pair, a sort restating the key is free to emit `n+1`
  ahead of `n` — falsifying monotonicity, and costing byte-stability even over a
  conformant emit, since two rows identical in all five non-ordinal columns
  still emit as two distinct byte sequences depending on which the sort put
  first. Sorting by the ordinal removes the freedom, because the ordinal is
  total where the key is not.
- **Things get tables; events get the log.** A real application's schema
  contains thing tables and an audit log — not per-kind wide CDC dumps. A
  per-kind CDC table is an *extraction* artifact: what a CDC tool produces
  *from* an app database. That archetype is streaming's charter; an author
  wanting CDC-shaped output uses `stream`, not `export --mode source`.
- **`init`-then-zero-edits is the bar, not zero-config.** An implicit layout
  *is* config — invisible and uneditable. A proposal engine makes the layout
  visible in the author's file where they see, edit, and own it, and a config
  declaring no output is an error precisely because no implicit layout remains
  to fall back to.
- **Same-kind-only tabular combination.** Column shape forces it: thing-rows of
  different kinds share no column set. The event log spans kinds legally because
  its columns are event-shaped and `item_type`-qualified — a delivery lane, not
  a population table.
- **The audited set keys on the class, not the `history_tracked` bit.**
  `constant` means current equals genesis, so an untracked constant's current
  value is temporally honest in every after-image; selecting by the bit would
  silently drop untracked constants from `create` / `destroy` changesets.
- **The per-population-tables escape.** A sub-typed kind whose populations elect
  different surfaces can be declared as per-population tables, each electing
  uniformly — a legal layout a forced single table per kind would structurally
  deny (its only legal elections would be `record_index` everywhere, or none).
- **The event-log edge gate follows the dereference key.** `(item_type,
  item_id)` is what a consumer joins on, so union safety must hold per
  item-type over every source addressing it — jointly, however the declaration
  list splits them. Gating per declaration would admit collisions the consumer
  actually hits; gating across item-types would refuse collisions `item_type`
  already disambiguates.
- **Sub-types are the concepts; kinds are the engine's grouping.** A kind
  exists to let similar functionality share machinery; the `<K>_type`
  sub-types are the first-class domain concepts and the expected default
  shape of the data. That is why the per-source `item_type` override is the
  primary naming surface (each split of a kind names its own concept), why
  `kind_labels` is a convenience for the one-concept kind and for the
  structurally kind-valued surfaces, and why sub-type discriminator
  *values* render verbatim — they are already domain vocabulary.
- **A naming surface, not a derivation.** Resolving `item_type` from the
  declared state table for the same population is not viable: a kind split
  by `sub_types` maps to several tables, a kind may be audited without any
  declared table, and coupling the log's vocabulary to table declarations
  would make it change silently when tables do. Item-type stays
  sidecar-derived, independent of which thing-tables are declared, by
  default; what an author gains is the ability to *declare* the vocabulary
  explicitly, matching the mode's own thesis (declared intent drives
  output). Deriving a label from table names remains the author's own read
  while writing `kind_labels`.
- **Mode-level `kind_labels` plus per-source override, not per-source
  only.** Kind names surface where no per-source declaration exists
  (junction member kinds), and one kind can appear as a value across many
  sources and tables — a single map keeps the kind-level vocabulary
  consistent by construction. The per-source `item_type` stays the
  first-class naming over it; the rendered-name collision clause is what
  keeps the two layers from contradicting each other — no layer outranks
  another, one rendered name identifies one population space.
- **Identity fall-through, not strictness, at render time.** Plan-time
  validation runs against the sidecar; render-time values may be corrupted
  by design (the dirty source dump, § Corrupter composition). A total
  mapping preserves declared defects and keeps the render infallible.
- **Suppressing empty updates.** An audit log records what it tracks: an
  `update` event none of whose audited properties changed carries no
  information and would leak the existence of unaudited changes; lifecycle
  events (`create` / `destroy`) are information in themselves and emit even
  with an empty changeset.
- **The anchor is required, not defaulted.** An operational dump has no natural
  "no timestamp" representation; silently emitting raw ns integers would be a
  fallback masking a missing anchor as valid output.
- **Horizon honesty over completeness.** The windowed state snapshot omits
  `updated_at` because `last_mutation_sim_time` at a past horizon is not
  faithfully reconstructible — untracked property writes advance it but leave
  no history. Omission is visible and documented; fabrication or understatement
  would be silent infidelity. The same posture makes horizon-masking (junction
  `left_at`) masking only, never recomputation.
- **The corrupter-composition guarantee is by construction, never
  special-cased.** No corrupter-aware branch exists in the source mode; the
  guarantee that a dirty emit yields a dirty dump follows from the mode reading
  only sidecar-declared structure, and is verified by a dedicated integration
  test rather than asserted by inspection.

## Boundaries

- **No warehouse features.** Source's feature-admission profile excludes
  `lookup`, FK pathfind, and SCD windows — a well-architected app is
  consistently normalized; denormalizing enrichment is dimensional's charter.
- **No CDC render.** Per-kind wide CDC output is streaming's charter — the same
  row-state-events fold, replayed as a live event feed. Source owns the
  app-database shape a CDC tool extracts *from*.
- **Omission is the exclusion mechanism.** There is no `exclude` block:
  a kind not named in any declaration is not exported, and references to it
  remain ordinary columns — a restricted extract by declaration, not
  configuration.
- **Normalized-export posture over denormalized payload is the author's
  omission.** A producer may retain a parent value on a child kind by
  necessity; which payload columns are "really" denormalized is not forge's to
  decide (Principle #7) — dropping one is an author `columns` selection, never
  a mode default.
- **No EAV / long-form history passthrough.** The emit's `history` table
  already is that shape; a passthrough toggle would reproduce the input
  verbatim rather than teach a reshape. The event log is the app-idiom
  presentation of history, not a passthrough.
- **No point-in-time slice export.** A `slice_at`-style horizon is base mode's
  contract ([`base.md`](base.md)); source's only horizons are incremental
  window horizons.
- **Single-branch, like every mode.** Source uses the derivations layer's
  single-branch guard; branch-aware export is parked pending a contract
  extension (see [`README.md`](README.md) § Staged roadmap).
- **CSV + DuckDB only.** No Parquet — the cross-mode writer boundary
  (see [`writers.md`](writers.md)).
- **No value mapping for property values, sub-type discriminator values, or
  any payload cell.** Domain vocabulary (§ Domain vocabulary) covers kind
  names and `changes` keys only; a general value-map surface is
  dimensional's `derived` territory, outside source's fidelity posture.
- **No streaming-mode vocabulary.** Streaming's payload keys and `kind`
  field speak engine names by design — its naming surface is the declared
  stream name ([`streaming.md`](streaming.md) § Declared streams); extending
  vocabulary mapping there is a separable future design.
- **No `init` proposal for labels.** `init` proposes engine-verbatim table
  names, under which every label is the identity mapping — there is nothing
  to propose until an author renames, and the author who renames owns the
  labels.
- **No per-table label scoping.** `kind_labels` is one vocabulary per
  export, not per declared table.
- **Row selection reads constant columns only.** A `where` key on a `tracked`
  or `slice_only` property is refused, not resolved to a horizon (§ Row
  selection); the grammar itself stops at equality and set membership
  ([`row-predicates.md`](row-predicates.md)).
- **`sub_types` is the discriminator surface, uniformly.** A `where` key naming
  the declared discriminator is refused with a pointer to `sub_types`, on
  membership units exactly as on records-backed ones — one selection per
  partition axis, no second spelling to drift.
- **Element fields are never predicate-addressable.** Membership-unit selection
  reads the owner, never the element schema: element fields carry no
  `temporal_class`, so a `where` key naming one is unresolved.
- **No owner-attribute projection into junction rows.** The parent lookup is a
  selection read; junction columns are exactly the membership surface. Merging
  a split kind's memberships into one polymorphic junction enriched with an
  owner-type column is a different shape, and splitting is expressible instead.

## Related

| Document | Why |
|---|---|
| [`reader.md`](reader.md) | The records-column taxonomy the state render classifies through; the `temporal_class` accessor the audited set resolves through |
| [`bundle.md`](bundle.md) | The column temporal classes and the genesis guarantee behind the audited set's temporal honesty |
| [`derivations.md`](derivations.md) | The row-state-events and membership-events folds the event log composes, and the state-at derivation the windowed state snapshot composes |
| [`dimensional.md`](dimensional.md) | The contrasting mode — reconstructed star schema vs. source's app-database shape; both compile to the mode-neutral `QuerySpec` |
| [`streaming.md`](streaming.md) | The owner of the CDC extraction archetype — the same folds, replayed as a live event feed instead of landed as an app schema |
| [`incremental.md`](incremental.md) | The cross-mode window/cursor/fingerprint driver source's windowed compile plugs into |
| [`playback.md`](playback.md) | The seam whose tier-2 `state` compiles this mode over a truncated tape via the post-compile relation rewrite; the presentation-name posture's companion |
| [`anchor.md`](anchor.md) | The effective-anchor resolution source requires — its first mandatory consumer |
| [`corrupters.md`](corrupters.md) | The corrupt → source composition — a source export over a corrupted emit surfaces declared defects unchanged |
| [`writers.md`](writers.md) | The CSV / DuckDB adapters source shares with every mode |
| [`declared-keys.md`](declared-keys.md) | The opt-in `declare_keys` capability — per-render declared primary-key / uniqueness constraints |
| [`key-election.md`](key-election.md) | The cross-mode key-election surface — elected identity and edge rendering, the identity and edge gates source's plan runs |
| [`temporal-elections.md`](temporal-elections.md) | The cross-mode election vocabulary declared-table and event-log `render` / `date_parse` maps render through |
| [`slice-only.md`](slice-only.md) | The export-wide `slice_only` policy source's omit-with-notice, `SourceSliceOnlyRead` refusal, and row-selection class gate instantiate |
| [`row-predicates.md`](row-predicates.md) | The scalar-or-list grammar, `PredicateValue` well-formedness rule, and rendering authority the mode's `where` surfaces share with dimensional's five |
| [`config-docstrings.md`](config-docstrings.md) | The docstring convention the `SourceConfig` family follows |
| [`../CAPABILITIES.md`](../CAPABILITIES.md) | Source-mode feature inventory and status |
| [`README.md`](README.md) | Design index, package layout, staged roadmap |
