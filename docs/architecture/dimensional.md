# Dimensional Exporter

**Status:** Implemented. Code is the contract — see
[`exporters/dimensional/`](../../src/fabulexa_forge/exporters/dimensional/),
[`config/`](../../src/fabulexa_forge/config/),
[`writers/`](../../src/fabulexa_forge/writers/), and
[`tests/exporters/dimensional/`](../../tests/exporters/dimensional/). Public API:
[`exporters/dimensional/engine.py`](../../src/fabulexa_forge/exporters/dimensional/engine.py).

The `mode: dimensional` exporter reshapes one base-layer emit into a star schema —
`dim_*` + `fact_*` tables, typed columns, SCD-2 — declaratively and
domain-agnostically. The dim-vs-fact role and the output grain are not in the base
tables and no reference-topology rule recovers them (`actor` points nowhere yet is
the central dimension; a terminal event record also points nowhere; a hybrid like
`journey_instance` is tracked, pointed at, and points out), so the role and grain are
author-declared (Principle #7). The sanitised emit declares warehouse role in its
`record_roles` registry, so `init` seeds its candidate's role from the registry and the
author confirms or flips it; the grain has no such declaration and `init` proposes it
from table shape. A single generic export config declares the output schema *and* its
binding to the emit's sources; the engine carries no domain knowledge, consumes no
target-schema file, and reads `record_roles` only in `init` — the export path never
consults it. It reads through the Stage-1 reader only.

```
emit (run.duckdb + base.json @ the supported `base_format_version`)
   │  (reader: Emit + Sidecar; trunk-only — sole branch)
   ├─ fabulexa-forge init   ─▶ commented candidate export config (sidecar-driven)
   └─ fabulexa-forge export ─▶ build_query_specs → one QuerySpec per declared table
          dim  → SCD-2 wide (versioned-intervals) | Type-1 sub-type split
          fact → records grain (filtered) | history point | history interval | membership grain
          FK   → labeled-edge pathfind { reference | membership }
          col  → projection | dim FK | correlation key | derived | NULL-pad
                     │
                     ▼  writers (CSV | DuckDB — both via Emit.query_arrow)
              dim_* + fact_*  (typed columns, SCD-2)
```

---

## Surface

| Module | Owns |
|---|---|
| [`config/models.py`](../../src/fabulexa_forge/config/models.py) | The two-tier config grammar (`ExportConfig`, `DimensionalConfig`, `TableDecl`, `SourceDecl`, `ColumnDecl`, `FkClause`, `DerivedSpec`, …) and its parse-time validators |
| [`config/loader.py`](../../src/fabulexa_forge/config/loader.py) | `load_export_config` — YAML → validated `ExportConfig` |
| [`exporters/dimensional/engine.py`](../../src/fabulexa_forge/exporters/dimensional/engine.py) | `build_query_specs`, `export_dimensional`, `QuerySpec` — the compile + dispatch entry points. `build_query_specs` takes an optional `window` and a required `base_relations: Mapping[str, str] \| None` (the full-export and windowed callers pass `None`; the playback seam's tier-2 `state` passes a truncated-relation mapping — [`playback.md`](playback.md) § The compile indirection), and `QuerySpec` carries `write_mode` / `view_name` / `view_sql`, for the windowed compile path ([`incremental.md`](incremental.md)) |
| [`exporters/dimensional/grains.py`](../../src/fabulexa_forge/exporters/dimensional/grains.py) | `build_grain_sql` and the four per-grain SQL builders — composing the reader faithful-read and versioned-intervals relations |
| [`exporters/dimensional/scd.py`](../../src/fabulexa_forge/exporters/dimensional/scd.py) | `build_scd2_sql` — the SCD-2 wide reconstruction, composing the versioned-intervals derivation and the reader records relation |
| [`exporters/dimensional/fk.py`](../../src/fabulexa_forge/exporters/dimensional/fk.py) | `build_fk_expr` — the labeled-edge pathfind, composing the reference-path and membership-edge derivations |
| [`exporters/dimensional/columns.py`](../../src/fabulexa_forge/exporters/dimensional/columns.py) | `build_column_expr` — the six column source modes |
| [`exporters/dimensional/lookup.py`](../../src/fabulexa_forge/exporters/dimensional/lookup.py) | `build_lookup_expr`, `check_lookup_temporal_safety` — the `lookup` mode (composing the reference-path derivation, terminal `prop__<property>`) and its type-1 temporal-safety business rule |
| [`exporters/dimensional/validation.py`](../../src/fabulexa_forge/exporters/dimensional/validation.py) | The business rules run against the sidecar before any SQL is emitted |
| [`exporters/dimensional/init.py`](../../src/fabulexa_forge/exporters/dimensional/init.py) | `generate_init_config` — the sidecar-driven candidate-config proposer |
| [`writers/duckdb.py`](../../src/fabulexa_forge/writers/duckdb.py), [`writers/csv.py`](../../src/fabulexa_forge/writers/csv.py) | The two output adapters; both read input through `Emit.query_arrow` |
| [`errors.py`](../../src/fabulexa_forge/errors.py) | The export-pipeline error hierarchy (`ExporterError` → `ConfigError` / `ExportError` / `ExportRuntimeError`) |

## Boundary

- **Input.** An open `Emit` (trunk-only — sole branch) and a validated `ExportConfig`
  whose `mode` is `dimensional`. The engine consumes no target-schema file and no
  domain knowledge.
- **Output.** One `dim_*` / `fact_*` table per declared table, written either as a
  directory of `<table>.csv` files (`fmt='csv'`) or a single `.duckdb` file holding
  every table (`fmt='duckdb'`). The two output shapes are why `out` is a directory for
  CSV and a file for DuckDB.
- **Reader-first; authors no base-table SQL.** Every table and column fact flows from
  the `Sidecar`; the engine hard-codes no column list and opens `run.duckdb` only
  through `Emit`. The engine **names no base table in SQL it authors** — it composes
  the reader's faithful-read builders and the derivations layer's interpretive
  relations (§ Composition) and applies a representation step over them. The writers
  materialize each `QuerySpec` through `Emit.query_arrow` — never a raw connection —
  and own only their *output* target. The engine's own introspection (distinct
  discriminator values) runs through the reader's `distinct_prop_values`.
- **Forbidden imports.** No dependency on the bundle's producer; the vendored
  `contract/` is the only coupling.

## Semantics

### Composition: the format authors no base-table SQL

The engine reshapes by **composing relations it does not author**, then representing
and writing them. It speaks in kinds and properties; the reader and the derivations map
those to physical base tables. Every base-table read the format performs is one of:

| Read | Tier | Composed relation |
|---|---|---|
| records snapshot (filtered) | faithful | reader `build_records_relation_sql` |
| `history_point` (filtered) | faithful | reader `build_history_relation_sql` |
| membership grain (filtered) | faithful | reader `build_membership_relation_sql` |
| `history_interval` / SCD-2 wide | interpretive | `build_versioned_intervals_sql` — one tracked property → interval grain, many → SCD-2 |
| FK reference path / `lookup` | interpretive | `build_reference_path_sql` — terminal `record_id` for FK, `prop__<property>` for `lookup` |
| FK membership edge | interpretive | `build_membership_edge_sql` |

The engine embeds each builder's `SELECT` as a subquery, `LEFT JOIN`s the interpretive
resolution relations on `record_id`, and applies its representation step over the
composition: renames, anchor rendering ([`anchor.md`](anchor.md)), per-source-type
`CAST`, `value_map`, ordinal, NULL-pad, and the total `ORDER BY`. "The format names no
base table" is a property of *authorship* — the base-table name appears only inside an
embedded reader or derivation relation, never in SQL the engine writes. The reader builders are faithful
([`reader.md`](reader.md)); the version and resolution relations are interpretive
([`derivations.md`](derivations.md)).

### The two-tier model

A config declares the output star schema and its binding in two tiers. The **table
tier** — per table, a grain source, a `role` (dim/fact), an SCD class (dims), a
`key`, and a column list — is author-declared and irreducible: role and grain cannot
be inferred (Principle #7). The **column tier** is mostly inferable; `init` proposes
it and the author confirms.

A column declaration carries **exactly one** source mode:

| Column mode | Meaning | Resolves to |
|---|---|---|
| `from: <src>` | Projection of a source column | `prop__<p>`, `elem__<f>`, `member__<f>__id`, `record_id`, `active`, … read directly off the grain |
| `fk: {to, via, …}` | Dim foreign key | A labeled-edge pathfind to the target dim's grain `record_id` |
| `correlation: <src>` | Degenerate correlation key | A reference-id column projected + renamed, **no** dim join |
| `derived: <spec>` | Computed column | ordinal / value-map / anchored timestamp / SCD window |
| `null: true` | NULL-pad | A constant **typed** `CAST(NULL AS VARCHAR)` for a target column the emit cannot fill |
| `lookup: {property, to, path}` | Type-1 record-attribute enrichment | A `prop__<property>` value projected from a related (or own) record's `records__<kind>` row, reached by a zero-hop self-join or the reference-edge pathfind (§ Lookup) |

A NULL-pad is `CAST(NULL AS VARCHAR)`, never a bare `NULL`: a typed all-NULL column is
what the DuckDB writer's Arrow path needs — a bare `NULL` is the untyped-object column
that trips the `register` failure the Arrow path exists to avoid. The exporter reads
no target schema, so it does not infer the column's intended type (Principle #7);
every pad is `VARCHAR` and any retype is a downstream concern.

Field shapes are defined by the Pydantic grammar in
[`config/models.py`](../../src/fabulexa_forge/config/models.py); the meaning of each
field is the semantics below.

### Grain sources

The base format is exhaustively four table categories plus sidecar metadata, so the
grain set is fully enumerable. One row of an output table corresponds to one row of
its grain source:

| `grain` | One output row = | Required source fields | Timestamp column(s) available | FK anchor record |
|---|---|---|---|---|
| `records` | one `records__<kind>` row | `kind`; optional `filter` (discriminator predicate) | `last_mutation_sim_time` (always), any `prop__<t>` | the grain record (kind = `kind`) |
| `history_point` | one matching `history` change event | `kind`, `property`; optional `value` | `sim_time` | the changed record (`record_id`, kind = `kind`) |
| `history_interval` | one occupancy interval (LEAD-windowed) | `kind`, `property` | `sim_time` (interval start), `lead_sim_time` (interval end = `LEAD(sim_time)`, `NULL` on a series' last row) | the changed record |
| `membership` | one membership-binding interval | `kind`, `property`; optional `where` (an `elem__` predicate) | `joined_sim_time`, `left_sim_time` | owner record (`record_id`, kind = `kind`) **and** bound member (`member__<f>__id`) |

`filter` on a `records` grain is the discriminator-split mechanism: a predicate
`{prop__decision_type: ed_arrival}` selects one slice of a kind into one named table.
Discriminator values come from `SELECT DISTINCT` or the config — never from
`enum_domains`; the modelling-event discriminator (`decision_type`) is absent from
`enum_domains`. Sub-type discriminators *are* in `enum_domains` and drive Type-1 dim
splits.

Every predicate value in the grammar — records `filter`, membership `where`,
history-point `value`, a membership FK's `where`, and an elapsed correlation's
`other_where` — is a scalar or a non-empty list of alternatives, compiling to `=`
or `IN` under one rule ([`row-predicates.md`](row-predicates.md)). A list is what
projects several discriminator values into one named table:
`{prop__decision_type: [ed_arrival, triage, ed_assessment]}` yields the domain's
own grouping — a clinical-process dataset spanning several decision types — where
a scalar would force one table per value. Entries over distinct columns are
AND-joined, so a list on one column composes freely with a scalar on another.

### Projectable columns per grain

`from:` and `correlation:` read a column **directly off the grain source** — there is
no cross-table `from:`. Reaching a record's other attributes — its own properties
absent from a history/membership grain, or another kind's — is never a bare projection:
a `lookup:` projects a type-1 attribute value (§ Lookup), and an `fk:` resolves to a
dim's `record_id`.

| `grain` | Projectable columns (`from` / `correlation`) |
|---|---|
| `records` | `fork_path`, `record_id`, `presentation_id` (when the emit carries one), `created_sim_time`, `active`, `deactivated_at`, `last_mutation_sim_time`, `record_index`; every `prop__<p>` of the kind, and the `ref_index__<p>` sibling of each reference-typed property |
| `history_point` | `fork_path`, `kind`, `record_id`, `property`, `sim_time`, `value` |
| `history_interval` | the `history_point` surface **plus** the virtual `lead_sim_time` (the engine's `LEAD(sim_time)` interval end; `NULL` on a series' last row) |
| `membership` | `fork_path`, `record_id`, `joined_sim_time`, `left_sim_time`; every `elem__<f>` element-field column; each reference field's `member__<f>__kind` / `member__<f>__id` pair |

`lead_sim_time` is the only virtual (non-sidecar) entry; every other column is one the
sidecar lists for that grain's table. A `from:` naming anything outside its grain's
surface is a `ProjectionColumnExists` failure.

**The surface is sidecar-resolved, not a fixed list.** Each row above spells out the
shape the base-format contract guarantees for that grain, but the check itself is
"does the sidecar declare this column on the grain's table?" — so identity and
lifecycle columns project like any other, and a producer's additional columns (e.g.
opt-in provenance) become projectable without a change here. Read the emit's sidecar,
not this table, for what a *particular* emit offers.

**Projecting a column is not the same as anchoring on it.** A column being projectable
via `from:` does not make it a legal `derived: {timestamp: {source: ...}}` source: the
timestamp-source set is a separate, narrower per-grain set (§ Timestamp source and the
runtime anchor). The two surfaces genuinely differ — an author who can read a column
raw may still be refused when asking to render it as wallclock.

### Role and SCD class

`role` is author-declared (`dim` | `fact`); `scd` is declared only on dims.

| Role + SCD | Output | Reconstruction |
|---|---|---|
| `dim`, `scd: type2` | One row per record-version | SCD-2 wide via `LEAD` over `history` (below) |
| `dim`, `scd: type1` | One row per record (current state) | Direct projection of `records__<kind>` (optionally sub-type filtered) |
| `fact` | One row per grain-source row | Per-grain (above) |

A Type-1 dim split from a sub-type (`dim_consultant` from `entity` filtered to
`entity_type=consultant`) uses a `records` grain with a `filter` whose key is the
synthesized `<kind>_type` discriminator present in `enum_domains`.

### SCD-2 wide reconstruction (the versioned-intervals primitive)

For an `scd: type2` dim, version boundaries are the distinct `sim_time`s at which any
**history-tracked** column of the record changes. The engine composes the
versioned-intervals derivation (`build_versioned_intervals_sql`,
[`derivations.md`](derivations.md)) over the dim's kind and its tracked properties: the
derivation supplies one row per `(record_id, version)` with `version_start` /
`version_end` (raw ns; `version_end` `NULL` on the last version) and each tracked
`prop__<p>` as the most-recent `history.value` at or before `version_start`. This is the
single interval primitive the `history_interval` grain also composes — one
implementation, two consumers.

Tracked and **static** (untracked) columns split **purely from the sidecar**:
`ColumnSpec.history_tracked` is read per column — `True` is tracked, `False` is static,
and a projection-introduced column (no upstream property) is never tracked. The flag is
**authoritative over `history` contents**: a column marked `history_tracked: true` that
never changed has no `history` change points and reconstructs to a single version —
`valid_from` at the record's first appearance, `valid_to` `NULL`, spanning the whole
run. The flag, not the emptiness of `history`, decides type-2 membership.

The split is **flag-only**: a flag-absent emit is refused at validation
(`Scd2NeedsHistory`, § Validation Rules), never reconstructed by `history`-table
inference. The flag is the authoritative SCD-class source; inference has a
false-negative tail — a type-2 property with no post-creation change contributes no
history rows yet is still type-2 — so reading the flag and failing fast without it is
the one policy shared with the `lookup` gate.

The tracked `prop__<p>` values come from the versioned-intervals derivation; each
**static** column and any `lookup` column comes from the composed reader records
relation (`build_records_relation_sql`) `LEFT JOIN`-ed on `record_id` and projected
with its per-source-type `CAST` in the representation step. The `valid_from` /
`valid_to` output columns are `derived: { scd_window: … }`, optionally
electing a temporal rendering via the object form (§ Derived columns); an
elected `date` collapses same-day versions to `valid_from = valid_to` at
date grain (the underlying raw-ns bounds and version ordering are
unaffected), and the open interval's `NULL` `valid_to` stays `NULL` under
every election.

**The type-2 column-mode surface.** A column mode is legal on a `scd: type2`
table iff it is a **pure per-row value function** — a function of one row's
source value, with no cross-row read and no grain-surface semantics. The row
surface that supplies the source value follows the source's class: a
history-tracked `prop__<p>` reads **per version** from the versioned
reconstruction's cast value; every other source (an untracked property, a
structural column, a projection-introduced column, the exempt discriminator)
reads **per record** from the composed records relation — a per-record value
repeats identically across one record's version rows.

| Column mode on a type2 table | Value semantics |
|---|---|
| `from` | The row surface's value per the source class above |
| `null` | A typed `NULL` |
| `derived: scd_window` (bare or object form) | The version bounds, optionally elected |
| `derived: timestamp` / `date_parse` / `value_map` / `decimal` / `json_precision` | The rendering authority's output for the row surface's value — per version for a tracked source, per record otherwise |

`fk`, `correlation`, `derived: ordinal`, and `derived: elapsed` are refused
(`Scd2ColumnModeSupported`, § Validation Rules), as is `lookup`
(`LookupColumnSafety`, § Lookup) — each is a cross-row or grain-surface read,
not a value rendering, and a version-grain answer for it is not defined
(§ Boundaries). A non-exempt `slice_only` source is refused by the
export-wide slice-only surface (`SliceOnlyColumnRefused`), on type2 exactly
as elsewhere; the exempt sub-typed discriminator is untracked, so a
`value_map` or `date_parse` over it renders per record from the current
classification value — carried as a classification, never presented as an
as-of value ([`slice-only.md`](slice-only.md)).

Per-version rendering is the same election applied per row:

- **Source-class-blind rendering** (§ Invariants). The derivation serves
  tracked values as codec `VARCHAR`; the build casts each version's value to
  the sidecar declared type — the same representation step the tracked
  `from` path performs — before handing it to the rendering authority, so a
  tracked value renders byte-identically to the same value at any other
  attach site (a mode table, a `changes` entry, a streaming after-image).
- A version whose value is `NULL` (pre-first-assignment versions, including
  the creation row of a genesis-null property) renders `NULL` of the output
  type — the NULL rule, applied per version.
- The export-time guards range over **every version's value**: a decimal
  overflow, a strict-parse failure, or a JSON payload violation in a
  historical version fails the export loudly, not only one in current state.
- Adjacent versions whose rendered values collide (`4.801` / `4.804` →
  `4.80` under `decimal: [5, 2]`) both emit, values identical — version
  boundaries derive from raw history change points, so a rendering never
  merges, suppresses, or renumbers a version row (election-invariant version
  structure, § Invariants; the posture the `scd_window` date election also
  takes).

**One compiler.** A derived column on a type2 table compiles through the
same per-column builders the records grain uses (`build_timestamp_expr` /
`build_date_parse_expr` / `build_value_map_expr` / `build_decimal_expr` /
`build_json_precision_expr`), handed a source expression per the source
class by `build_scd2_column_expr_flag`
([`scd.py`](../../src/fabulexa_forge/exporters/dimensional/scd.py)). The
type2 surface introduces no election site or rendering authority of its own,
so the anchor requirement, DST posture, precision, tie and overflow rules,
and mismatch errors are one contract across grains and source classes; for
an untracked source the rendered SQL is byte-identical to the records
grain's modulo alias. Examples:
[`test_scd2_renderings.py`](../../tests/exporters/dimensional/test_scd2_renderings.py).

A type2 dim *is* a records-grain table, so a `derived: timestamp`'s `source`
domain is the records-grain domain — the records category's instant-carrying
structural columns plus time-valued properties, tracked ones included, with
`TimestampSourceAvailable` applying as it does anywhere else. An unelected
`derived: timestamp` with no resolved anchor renders the raw ns integer;
any explicit election without an anchor is refused at validation
(`TemporalRenderRequiresAnchor`).

### Foreign keys — the labeled-edge pathfind

An `fk` column names a **destination dim table**; the engine finds the **path** over
the sidecar reference graph from the grain's anchor record to a record of the
destination dim's source kind. The same engine resolves two edge types:

| `via` | Edge | On a `records`/`history` grain | On a `membership` grain |
|---|---|---|---|
| `reference` | a `prop__<x>` column with a `references` annotation | Pathfind from the grain record's kind to the dim's source kind, joining each hop on `prop__<x> = records__<next>.record_id`. Multi-hop allowed (e.g. `decision → journey_instance → actor`). | Pathfind from the **owner** record (`record_id`, kind = source `kind`) outward |
| `membership` | a membership table's `member__<f>__id`, reference field `<f>` named by `member_field` (inferred when the table has one reference field) | Locate `membership__<K>__<p>` (`<K>` = grain kind; `<p>` = `property`, inferred when the kind owns one collection-struct property), join on `record_id = grain.record_id` plus the `where` predicate over `elem__` columns, take `member__<member_field>__id` whose `member__<member_field>__kind` = the dim's source kind | The binding **is** the grain; project `member__<member_field>__id` directly (already narrowed by `source.where`) |

The membership edge carries no contract-fixed field names beyond the four structural
columns (`fork_path`, `record_id`, `joined_sim_time`, `left_sim_time`); every other
column is an `element_schema` field the scenario author named (contract § Membership).
The binding's label column is whatever the author called it, so the author selects
bindings with a `where` predicate over the actual `elem__` columns
(`where: {elem__role_name: surgeon}`), mirroring the records grain's `filter`. The
engine verifies the edge lands on the destination dim's source kind by matching
`member__<member_field>__kind` against that kind — so the author need not restate the
member sub-type, and (because member kind is per-row) rows whose member kind differs
do not join and emit `NULL` rather than a fabricated value. No 1:1 role→kind mapping
is assumed.

The engine authors neither edge's SQL. A `via: reference` FK composes the reference-path
derivation (`build_reference_path_sql`, terminal `record_id`); a `via: membership` FK
composes the membership-edge derivation (`build_membership_edge_sql`,
[`derivations.md`](derivations.md)). Both return a `(record_id, resolved)` relation the
engine `LEFT JOIN`s on the grain's `record_id` under a per-column alias and projects as
`resolved`. reference-path is fan-out-free (every hop keyed on `record_id`); the
membership edge's cardinality is the author's `where`'s responsibility — it fans the
anchor out exactly when the `where` selects more than one binding, as the membership FK
always has.

References resolve along a chain of `references`-annotated `prop__` columns. A `path`
hint is an **ordered list of `prop__<x>` reference columns** — one per hop — not a
list of kinds; naming the column pins both the edge and its target kind, which
disambiguates two paths between the same kinds *and* two columns from one kind to the
same next kind. When exactly one chain exists `path` is optional; when several do it
is required (absence is a validation error — § Validation Rules).

An anchor record with no FK value on some rows — e.g. a `history` grain keyed only on
`record_id` whose path does not start resolvable from that record — emits `NULL` for
the FK. This is a documented limitation, never fabricated: a discharge history row
cannot reach `journey_instance`, so its `spell_id` FK is `NULL`.

**The destination dim's source population set.** An `fk` edge joins the
destination dim's population-restricted identity relation, so the set exists to
keep FK output closed over its target: an owner outside it resolves to `NULL`
rather than to a key the dim does not contain. The set must therefore be exactly
what the dim's `filter` selects — a wider set would let a fact render a key value
its dim excludes, a dangling reference (Principle #4).

For a dim on a sub-typed kind, the discriminator conjunct's **value set** — a
scalar's singleton, or a list's elements in config order — selects exactly those
populations; absent a discriminator conjunct, the set is the kind's whole
population set. Selection is by value set where rendering is by form, so a
one-element list renders `IN` and selects the same populations as the scalar, and
the two reads of the conjunct never disagree on rows. Every element must be a
declared sub-type of the kind's domain — the refusal is per element and names the
offending one, because a population that cannot exist fails loudly rather than
resolving to an empty set (Principle #7). A restriction spine is composed exactly
when the selected set is a *strict* subset of the declared domain, so a list
naming the full domain in any order composes none, identical to omitting the
conjunct. Further conjuncts narrow rows *within* the selected set, never the set
itself. On a kind that is not sub-typed, a discriminator-named conjunct is an
ordinary column conjunct in either form.
[`resolve_dim_source_populations`](../../src/fabulexa_forge/exporters/dimensional/populations.py)
is the resolver; every consumer of the set — foreign-key surface inheritance, the
edge union-safety gate, the identity-relation restriction spine, the uniqueness
guard's dim-side leg, and the dim-key agreement check — ranges over any non-empty
declared subset.

**The FK identity surface.** Which identity surface the resolved FK column
*carries* is the key-election surface's contract
([`key-election.md`](key-election.md) § Rendering: dimensional). In brief:
`fk.target_key` accepts `record_id` / `record_index` / `presentation_id`;
absent, the edge inherits the election of the destination dim's source
population set, refusing at plan time when that set carries more than
one distinct election (`ElectionInheritanceAmbiguous`; a subset spanning differing
elections hits it exactly as a full multi-election domain does, and its remedies —
filter to a single sub-type, unify the election, set an explicit `target_key` —
all apply). A non-`record_id`
surface resolves through the record-index or presentation-key join relation
restricted to that population set — an out-of-set target renders `NULL` — and
`presentation_id` resolution requires every admitted population
registry-declared and pairwise union-safe, with or without a config `keys`
block. The dim-key agreement check (`ElectionDimKeyDisagrees`) forces an
inheriting edge's destination dim to declare a `key` column sourced `from:`
the elected contract column, so both sides of the join agree before any data
is read.

### Lookup — type-1 record-attribute enrichment

A `lookup` column projects a record's scalar `prop__<property>` value onto an output row
whose grain table does not carry it — the `history_point` / `history_interval` and
`membership` grains, and cross-kind pulls on any grain. The record is reached by a
**zero-hop self-join** on `record_id` (the grain's own changed or owner record — the
common case) or, when `to` names a different kind, by the **same reference-edge pathfind
the `fk` mode uses**. Unlike `fk`, `lookup` projects the terminal row's attribute value
rather than its surrogate key, and the terminal kind need not be a declared dimension.

`anchor_kind` is the grain's `source.kind`. The terminal kind is `to` when set, else
`anchor_kind` (zero-hop self). The path is empty for a zero-hop self lookup, else the
ordered reference-column hops from `anchor_kind` to the terminal kind — discovered over
the sidecar reference graph, or taken from the `path` hint (the same ordered
`prop__<x>`-column convention as `fk.path`). Every join is a `LEFT JOIN` keyed on
`record_id`, which is unique within each records table per branch, so every hop matches
at most one row: a lookup is **fan-out-free** and preserves the output row count.
A row whose `record_id` has no matching terminal row — or whose matched `prop__<property>`
is itself `NULL` — projects `NULL`, the same documented behavior as a non-records-grain
`fk`. The engine composes `build_reference_path_sql` (the same reference-path derivation
the `fk` mode uses) with `terminal_projection = prop__<property>` rather than
`record_id`, including the zero-hop self case (empty hops → `prop__<property>` off the
anchor's own records row); `lookup` differs from `fk` only in that terminal projection
and in not requiring a dim target.

**Temporal safety (the crux).** `records__<kind>` holds only the *slice* value — each
record's state at the branch's `slice_at`. That value is exact at a past interval only
if the property is contract-valid at every T, i.e. `temporal_class: constant`. A
`tracked` property's slice value is its *final* value; a `slice_only` property's past
is unknowable outright; stamping either onto a past history-grain interval would
fabricate a value that never (verifiably) held there. So `lookup` is gated to
`temporal_class: constant` **along the entire path** — the terminal property and every
reference hop column — the exempt sub-typed discriminator excepted (a classification
read, any class — see [`slice-only.md`](slice-only.md) § The discriminator carve-out).
The class is read per column through the sidecar's narrowing accessor.

| Path class (terminal or any hop) | Result |
|---|---|
| All `constant` | Allowed — the value is contract-valid at every T; projection is exact at any interval |
| Exempt discriminator on the path, any class | Allowed — a classification read: the row's type tag at its current value, never presented as as-of. This admits a `tracked` discriminator terminal |
| Any other `tracked` | Refused — a capability boundary: the slice value is the *final* value, and an as-of reconstruction is not a `lookup`; the error names the tracked column |
| Any other `slice_only` | Refused — permanent: the value at the row's interval is unknowable; the error names the column and the class |
| Temporal pair unavailable on a consulted column | Refused via the reader's `TemporalClassUnavailableError` — unverifiable is refused, never inferred |

(A discriminator can appear on a `lookup` path only as the terminal property — hop
columns carry references, and `prop__<K>_type` is an enum classification — but the
exemption predicate is applied per consulted column, mechanically, with no
terminal-vs-hop special case.) `Scd2NeedsHistory` keeps its `history_tracked` keying:
it asks the SCD-class question ("does this kind have priors in `history`?"), which is
exactly what the bit answers; only point-in-time safety keys on the class.

Resolution and grain applicability:

| Condition | Result |
|---|---|
| `to` omitted | Zero-hop self: terminal kind = `anchor_kind`, path empty |
| Zero-hop self (`to` omitted or `to == anchor_kind`) on a `records` grain | Refused — the property is already a grain column; use `from:` |
| Zero-hop self on a `history_point` / `history_interval` / `membership` grain | Allowed — the zero-hop reference-path relation projects `prop__<property>` off the anchor's own records row |
| `to` set and differs from `anchor_kind` | Reference pathfind over the sidecar graph (or the `path` hint) to the terminal kind's `prop__<property>` |
| `to` set, multiple reference paths, no `path` hint | Refused — ambiguous; the author supplies `path` |
| `to` set, no reference path | Refused — unresolvable |
| `path` given but `to` omitted | Refused at parse time — a zero-hop self lookup has no hops |
| `scd: type2` table | Refused — the SCD-2 wide builder does not project lookup columns |

On a **membership grain**, `lookup` enriches from the **owner** record (the grain's
`record_id` is the owner's id); the *member* is reached with `fk via: membership`, not
`lookup`.

The engine `LEFT JOIN`s each lookup's reference-path relation on the anchor's
`record_id` under a per-column alias (`_lookup_<col>_rp`), so multiple lookups never
collide, and the JOINs are collected and emitted before the `ORDER BY` exactly as `fk`
JOINs are. A `lookup` and an `fk` anchoring on the same record each compose their own
relation under different aliases — duplicate but harmless, both fan-out-free over the
same rows. Cross-mode relation reuse is not attempted; the per-column-namespaced
composition is the contract.

### Degenerate correlation keys

A `correlation: prop__journey_instance` column projects a reference-id column and
renames it (`attendance_id`, `spell_id`, …). It links facts **to each other**, not to
a dimension — there is no `dim_journey_instance`. It is a distinct column mode
precisely so validation does not require a target dim (unlike `fk`). The source column
may carry a `references` annotation in the sidecar; the correlation key deliberately
does not resolve it.

### Derived columns

| Derived spec | Output | Determinism |
|---|---|---|
| `ordinal: {partition_by, order_by}` | `ROW_NUMBER()` over the partition, ordered by `order_by` | The engine **always** appends `record_id` (the grain's identity column) as the final `ORDER BY` tie-break, so the ordinal is total and reproducible even when `order_by` ties. When `order_by` names a rendered-time column, the `ORDER BY` compiles to that column's **raw ns source** (the ordinal amendment, below) |
| `value_map: {from, map}` | Each source value replaced by its mapped value (typed per the map — below) | Unmapped source values → `NULL` (faithful: never invent a mapping) |
| `timestamp: {source, as}` | The named `sim_time` column rendered as wallclock `TIMESTAMP` via the runtime anchor, or — with `as` electing `date` / `time` / `timestamptz` — that projection of the same instant ([`temporal-elections.md`](temporal-elections.md)) | Pure function of the anchor; `sim_time = 0` → `start_datetime`; `as` absent renders the default `timestamp` byte-identically |
| `scd_window: valid_from \| valid_to \| {bound, as}` | The SCD-2 version-window bound, or — the object form — that bound in an elected temporal type | From the `LEAD` reconstruction; election is a rendering choice over the same raw bounds |
| `elapsed: {…, unit \| as}` | A cross-row time delta, as a `unit`-divided `DOUBLE` or — `as: interval` — a µs-precision `INTERVAL` | Same ns delta either way; exactly one of `unit` / `as` is set |
| `date_parse: {from, format}` | A declared VARCHAR source column reinterpreted as the format's denoted temporal type — `DATE`, `TIME`, or naive `TIMESTAMP` ([`temporal-elections.md`](temporal-elections.md)) | Value-preserving; a non-matching non-`NULL` value fails the export loudly (§ Validation Rules) |
| `decimal: {from, as: [p, s]}` | A declared DOUBLE source column rendered as exact `DECIMAL(p, s)` — ties away from zero, loud overflow/NaN/Infinity error ([`value-rendering-elections.md`](value-rendering-elections.md)) | Pure value function, compiled through the one decimal authority |
| `json_precision: {from, leaves}` | A declared VARCHAR JSON payload column with named top-level numeric leaves rounded in place, every other byte preserved ([`value-rendering-elections.md`](value-rendering-elections.md)) | Pure value function via the registered scalar; payload guards fail loudly |

`partition_by` and `order_by` name **sibling output columns** of the same table (e.g.
`partition_by: patient_id` references the FK-resolved actor id; `order_by: timestamp`
references the anchored timestamp).

**The ordinal amendment.** When `order_by` names a rendered-time column — a
`derived: timestamp` whose `source` is the grain's time column, or an SCD-2 dim's
`scd_window: valid_from` — the `ORDER BY` compiles to that column's **raw ns source**,
then `record_id`, rather than to the rendered value. Rendered timestamps truncate to
microseconds (§ Timestamp source and the runtime anchor), so two rows inside one
microsecond tie at the rendered value and the `record_id` tie-break could order them
against true event order. The rendered value is monotone in its source, so ordering by
the raw ns source changes output only on same-microsecond ties — where raw order *is*
the event order. This is the row-ordering doctrine ("pinned by `sim_time`, never by the
rendered timestamp", § Determinism and ordering) applied to ordinals, and it is what
makes the ordinal sound under windowed export, where a rendered-µs ordering could let a
same-microsecond tie count a row that lands in the next window (see
[`incremental.md`](incremental.md)). The amendment is implemented in
[`columns.py`](../../src/fabulexa_forge/exporters/dimensional/columns.py)
(`_find_raw_ns_source_for_ordinal`).

The amendment is election-aware. `timestamp`, `timestamptz`, and `date`
elections (and the default) all substitute the raw-ns source, since each is
monotone in its source — the substitution changes output only on
rendered-value ties, where raw order is event order. A `time` election is
excluded: time-of-day is **not** monotone in the instant, so raw-ns
substitution would contradict the author's evident intent (ordering by time
of day across days) — a `time`-elected amendment column instead orders by
its own rendered value, `record_id` tie-broken as any ordinary column.
`interval`-rendered `elapsed` columns and `date_parse` columns are never
amendment columns; they order by value, `record_id` tie-broken. An SCD-2
`valid_to` bound stays outside the amendment under every election — it
orders by rendered value like any other column. Under incremental export
the windowed rule (an append-mode table's `ordinal.order_by` must name a
window-key column) is amended the same way — see
[`incremental.md`](incremental.md) § Window membership per table class.

A `value_map` column is **typed from its map's values**, and its generated `CASE`
casts *every* branch — including the unmapped `→ NULL` — to that type, so the column
reaches the writer typed (the same untyped-NULL Arrow hazard the NULL-pad rule
avoids): `BIGINT` when every mapped value is an `int`, `DOUBLE` when they are
`int`/`float`, else `VARCHAR`. The map is author-supplied, so this types the column
without inventing a value (Principle #7). A `value_map`'s `from` reads off the grain's
projectable surface, so an unresolvable `from` fails `ProjectionColumnExists` with a
clean `ExportError`, never a raw SQL failure.

A `date_parse`'s `from` resolves off the grain's projectable surface exactly
as `value_map.from` does, and the resolved column must carry a declared
VARCHAR type — the sidecar type for `prop__` columns, the element-schema
type for a membership grain's `elem__` fields; a column with no declared
type behind it (a structural column, a virtual column, a grain constant) is
refused as non-VARCHAR (`DateParseSourceColumn`, § Validation Rules). On the
`history_interval` grain, `value` participates under the same rule with no
special case: it is an ordinary projectable-surface column whose declared
type is the sidecar `history` table's `value` column type, the same type
authority `value_map`'s literal typing already reads. The full election
vocabulary, the anchor requirement, and the declared-parse contract — its
closed directive vocabulary and the denoted type derived from it — are
[`temporal-elections.md`](temporal-elections.md)'s.

A `decimal`'s and a `json_precision`'s `from` resolve off the grain's
projectable surface the same way, and their source-type gates
(`DecimalSourceIsDouble`, `JsonPrecisionSourceIsVarchar`) check the same
declared-type authority through the mode's grain-projection resolution.
Their semantics, guards, and rendering authorities are
[`value-rendering-elections.md`](value-rendering-elections.md)'s; the
payload-instant case is `derived: timestamp`'s own surface — a payload
BIGINT is a legal `timestamp` source here, so dimensional needs no
`instant` spelling.

### Timestamp source and the runtime anchor

A grain's timestamp sources are the instant-carrying structural columns of the
grain's table category, resolved through the reader's structural-temporal surface
([`reader.md`](reader.md) § The structural-temporal surface), plus the grain's
virtual interval-end column where it defines one. The category mapping and the one
virtual column are dimensional's own — both are dimensional concepts the contract
does not define — but which structural columns carry an instant is never restated
here.

| `timestamp.source` | Availability |
|---|---|
| `created_sim_time` | `records` grain — the record's birth instant |
| `deactivated_at` | `records` grain — the record's close instant; `NULL` for a still-active row, which propagates to a `NULL` timestamp |
| `last_mutation_sim_time` | `records` grain — the record's last-touched instant |
| `prop__<t>` | A time-valued property column |
| `sim_time` | The `history`-grain change time (the interval **start** on `history_interval`) |
| `lead_sim_time` | `history_interval` grain only — the `LEAD(sim_time)` interval end; `NULL` on a series' last interval (open-ended), mirroring SCD-2 `valid_to`. The one virtual (non-contract) source |
| `joined_sim_time` / `left_sim_time` | `membership` grain only — the binding interval's join / leave time (`left_sim_time` `NULL` while still bound) |

All three records instants render through one path: the renderer qualifies whatever
column it is given and hands it to the anchor renderer, so each renders through the
same expression and each falls back to the raw nanosecond integer when no anchor
resolves. A `NULL` `deactivated_at` renders as a `NULL` timestamp rather than an
error — the honest rendering, since the record has not closed, and the same
treatment the membership grain gives a `NULL` `left_sim_time`.

`derived: timestamp` renders through the resolved `EffectiveAnchor` — the one
wallclock anchor `cmd_export` resolves for the invocation (see
[`anchor.md`](anchor.md)). The column is a SQL SELECT fragment, never
per-row Python. When the anchor is `None` and no election is explicitly set
(no sidecar `runtime` and no rebase input), the renderer emits the raw
`sim_time` integer column (no conversion). A plain `from:
last_mutation_sim_time` projection always yields the raw integer; only
`derived: timestamp` applies the anchor. Availability is enforced as
`TimestampSourceAvailable` (§ Validation Rules).

`timestamp`'s `as` field elects the rendering — `date`, `time`, or
`timestamptz` in place of the default `timestamp` — and the `scd_window`
object form carries the same election over an SCD-2 bound; both compile
through `render_anchor_temporal_expr`, the one renderer every wallclock mode
shares ([`anchor.md`](anchor.md), [`temporal-elections.md`](temporal-elections.md)).
Any explicit election — `as: timestamp` included — requires a resolved
anchor: with none, the column is refused at validation
(`TemporalRenderRequiresAnchor`, § Validation Rules) rather than silently
falling back to the raw integer, since a raw column under an elected `date`
name would misrepresent what was rendered.

When an anchor resolves, the `timestamp` election renders a naive local
wall-clock `TIMESTAMP` in `anchor.timezone`:

```sql
timezone('<anchor.timezone>',
         TIMESTAMPTZ '<anchor.start_instant ISO-8601 with offset>'
           + to_microseconds(CAST("<grain>"."<source>" AS BIGINT) // 1000))
```

The `TIMESTAMPTZ` literal fixes the absolute origin; the microsecond interval adds
physical elapsed time; `timezone(zone, …)` projects the resulting instant to the
local wall clock in the effective zone, with DST resolved by DuckDB's bundled tz
database. The two interpolations are pinned: `<anchor.timezone>` is
`str(anchor.timezone)` (the IANA key) and `<anchor.start_instant …>` is
`anchor.start_instant.isoformat()` — a literal carrying the UTC offset at the
origin instant only. DuckDB re-derives each event's local wall clock with full DST
rules, so a single origin offset is sufficient and correct. The renderer uses
exactly these two serializations; the byte content of the SQL and the
value-identity claim below depend on them.

The declared IANA `timezone` is applied, so DST is observed. For a UTC-anchored
emit with no rebase input the materialized column *values* equal a naive
`TIMESTAMP '<start_datetime>' + sim_time` rendering, because a `+00:00` origin's
projected UTC wall clock coincides with the wall-clock reading of the bare literal;
for a DST-observing sidecar zone the projected wall clock reflects the declared
zone. A naive local wall clock is ambiguous across a DST fall-back (two instants
share one local string) and steps backward at the fold; this is faithful to real
wall clocks and accepted — row ordering is pinned by `sim_time`, never by the
rendered timestamp. The dimensional `TIMESTAMP` is microsecond-resolution; a
sub-microsecond `sim_time` tail is truncated (§ Boundaries). The `date` and
`time` elections project this same local wall clock; `timestamptz` renders
the absolute instant instead — the full election vocabulary, DST posture,
and precision rule are [`temporal-elections.md`](temporal-elections.md)'s.

### Membership-grain facts (multi-pick)

When a binding role attaches multiple members per owner decision — e.g. several
medications per decision, an ordinal the scenario carries in an `element_schema` field
projected as `elem__pick_index` — the fact grain must be the **binding**, not the
decision, or rows would be dropped (a fidelity violation). A `membership` grain emits
one row per binding interval: `from: member__<f>__id` resolves the bound member,
`from: elem__<ordinal>` projects a scalar element field as a measure, `from: record_id`
is the owner decision id, `fk: {via: reference}` pathfinds from the owner decision
outward, and `derived: timestamp {source: joined_sim_time}` anchors the binding's join
time. Single-pick roles (one binding per decision) are equally expressible as a
membership-grain fact or as a membership-edge FK on a `records` grain; the author
chooses by whether the binding is the grain or an enrichment.

### History point vs interval facts

| Grain | Row | Use |
|---|---|---|
| `history_point` | One property change matching `(kind, property[, value])` | Terminal-state events that emit no decision row (`discharged`, `deceased`) and outcome mutations like an FFT rating |
| `history_interval` | One occupancy interval per `(kind, record_id, property)` series, `sim_time → LEAD(sim_time)` | State-by-state journey traces |

The interval end is the virtual `lead_sim_time` column (`= LEAD(sim_time)` over the
series). A series' final interval has `lead_sim_time = NULL` (open-ended through the
slice boundary), so a `derived: timestamp {source: lead_sim_time}` column renders
`NULL` there — the same open-ended convention as SCD-2 `valid_to`. `lead_sim_time`
exists only on this grain; the membership grain's interval bounds are
`joined_sim_time` / `left_sim_time`. History grains key on `record_id` only, so
reference-path FKs whose path does not start resolvable from that record emit `NULL`.

**Composition.** The `history_point` grain composes the reader history relation
(`build_history_relation_sql`, filtered to `(kind, property[, value])`). The
`history_interval` grain composes the versioned-intervals derivation over the grain's
**single** tracked property; because that derivation carries only `record_id`,
`version_start`, `version_end`, and the one `prop__<p>`, the format rewrites each author
projection onto those canonical columns in the representation step:

| Author projection (`from:` / `correlation:`) | Composed source |
|---|---|
| `record_id` | the derivation's `record_id` |
| `sim_time` | `version_start` (the interval start *is* the change-point `sim_time`) |
| `lead_sim_time` | `version_end` (`= LEAD(sim_time)`; `NULL` on the last interval) |
| `value` | the sole `prop__<p>` (the boundary is that property's change point, so its as-of value is the row's own `value`) |
| `property`, `kind` | constants the format already holds (the grain's sole tracked property and source kind) |
| `fork_path` | the sole branch from `require_single_branch` |

The per-source-type `CAST`, rename, `value_map`, and ordinal apply on top of the
rewritten source exactly as they do for any projection. The grain projects no provenance
columns — the sanitised subset carries none — so it composes the versioned-intervals
derivation directly, with no secondary join.

### Output naming

Output column and table names are always the author-chosen `name` field; the engine
never strips prefixes or renames automatically. `init` *proposes* clean names
(stripping `prop__`/`elem__`/`member__`) as defaults and runs a collision check
against the five structural record columns (`fork_path`, `record_id`, `active`,
`deactivated_at`, `last_mutation_sim_time`) before suggesting a stripped name; the
author edits freely thereafter.

**`last_mutation_sim_time` is a reserved output name** — the presentation-name
posture. The column is a sim-internal bookkeeping high-water mark; its value is
freely projectable (it is the default records-grain `timestamp.source` and any
`from:` reads it), but naming an output column `last_mutation_sim_time` is refused
at plan build (`check_reserved_presentation_name`, the shared check in
[`exporters/reserved_names.py`](../../src/fabulexa_forge/exporters/reserved_names.py)).
[`source.md`](source.md) carries the same reservation on its `rename` targets, and
the playback seam presents the column as the recorded trail under `state`
([`playback.md`](playback.md) § The recorded trail).

### Determinism and ordering

The exporter is a pure function of `(emit, config, code version)`: same inputs →
identical output rows in identical order. Every emitted table carries a total
`ORDER BY` ending in the grain identity column (`record_id`, or — for membership
grains — `record_id` then `joined_sim_time` and the element-field columns in
`element_schema` declaration order, matching the contract's membership row order), so
row order is reproducible regardless of DuckDB scan order. Ordinals append the same
tie-break. No RNG, clock, or network is consulted. The `history_interval` grain's
identity is `(record_id, version_start)`, and its total `ORDER BY` is on both: a record
with several intervals would otherwise leave their relative order to the engine's scan,
so ordering on the interval start (raw ns) is what makes that grain deterministic.

### `init` inference contract

[`generate_init_config`](../../src/fabulexa_forge/exporters/dimensional/init.py) reads
the emit and emits a **commented candidate config** the author edits — a starting point
(~70–80% on a clean scenario) whose every `role` / `scd` proposal is author-authoritative,
confirmed or flipped, never a decision the engine enforces. The proposal is annotated
with the emit's forwarded documentation as YAML comments — scenario narrative, table
descriptions, per-property description/unit, discriminator glosses — under the shared
annotation contract ([`documentation-channel.md`](documentation-channel.md) § `init`
annotations); comments are not grammar, so the self-gating posture is untouched.

**Role is read from `record_roles`, never inferred from topology.** A bare-string kind
takes its role from `record_roles[<kind>]`; the object-valued kind (today only `actor`)
splits per declared sub-type, each sub-type's role resolved independently from
`record_roles[<kind>][<sub_type>]`. The registry's contract tokens `"dimension"` /
`"fact"` map to the config `role` tokens `dim` / `fact` — the only transformation between
registry value and emitted token.

**Splitting into per-sub-type stubs is a separate question from role, answered by
`Sidecar.subtype_values`.** `record_roles`'s object-vs-string shape tells `init` whether
*role* varies by sub-type (true only for `actor` — the only kind whose warehouse role the
contract allows to differ by sub-type, `base-format.md` § Record roles); it does not tell
`init` whether the kind is *sub-typed* at all. That is `Sidecar.subtype_values(kind)`
(sourced from `enum_domains[<kind>][<kind>_type]`), independent of `record_roles[kind]`'s
shape — a bare-string kind (`entity`, `resource`, `diary` in the nhs/retail example
bundles) can still carry a real `<kind>_type` domain and split into per-sub-type stubs,
each resolving the *same* uniform role via `record_roles.role_of(kind, sub_type)`. One
combined stub over a polymorphic bare-role kind would union unrelated sub-type schemas
into a mostly-NULL table — exactly the shape the nhs/retail example configs' `SPLIT`
header notes hand-correct today.

**Every SCD-2 column proposal carries its versions-per-record evidence.** A tracked
column with many versions per record is operational state wearing an attribute's
name, and proposing it as an SCD-2 column materializes a dimension that many times
its entity count — the nhs example's `resource.allocated` is 838.9 versions per
record over 30 consultants. `init` states the ratio in the column's comment,
sourced from the sidecar's advisory `row_census` ([`reader.md`](reader.md)). It
remains a comment: the proposal is unchanged, and moving the column to its own fact
grain is the author's call, made against evidence instead of against a measurement
they would otherwise take only after exporting and profiling.

When the emit carries no census, or the census enumerates no rows for that series,
the comment says so explicitly. Silence would read as *measured, and fine* on
exactly the emits where nothing was measured.

`init` trusts a C1–C15-conformant emit exactly as the engine does — it does not
re-validate — and relies on these guarantees:

- `record_roles` is present whenever the emit carries ≥ 1 records kind; its absence raises
  [`InitRequiresRecordRoles`](../../src/fabulexa_forge/errors.py) before any proposal is
  built, a fail-fast on malformed input rather than a degraded inference mode.
- C12 guarantees every emitted records kind is covered, so `init` does not re-check coverage.
- `record_roles["actor"]`'s keys are the **declared** sub-type set — a superset of the
  *observed* `prop__actor_type` values a slice may narrow — so a declared-but-unobserved
  sub-type still yields a stub that exports an empty typed table, consistent with the
  every-declared-table-emitted invariant and `DiscriminatorValueObserved`.
- Registry keys are lexicographically sorted at every level, fixing `init`'s output order.

The proposal per kind:

| Emit condition | Proposal |
|---|---|
| `record_roles` absent | Raise `InitRequiresRecordRoles`; emit no output |
| Bare-string kind, role `dimension`, no `history_tracked` column | One `role: dim`, `scd: type1` stub |
| Bare-string kind, role `dimension`, ≥ 1 `history_tracked` column | One `role: dim`, `scd: type2` stub; tracked columns marked, `valid_from` / `valid_to` `scd_window` columns added |
| Bare-string kind, role `fact`, no `prop__<kind>_type` discriminator | One `role: fact` stub; an FK-candidate comment per `references` column |
| Bare-string kind, role `fact`, carries a `prop__<kind>_type` column but no declared `enum_domains[<kind>][<kind>_type]` domain | One `role: fact` stub per `SELECT DISTINCT` observed value (native order), `filter` pre-filled; FK-candidate comments |
| Object-valued kind, sub-type role `dimension` | Per sub-type (from `record_roles[kind]`'s keys): a `role: dim` stub (`scd: type2` with window columns when the kind has a `history_tracked` column, else `scd: type1`), `filter: {prop__<kind>_type: <sub_type>}` |
| Object-valued kind, sub-type role `fact` | Per such sub-type: one `role: fact` stub, `filter` pre-filled; FK-candidate comments |
| Bare-string kind, role `dimension` or `fact`, carries a declared `enum_domains[<kind>][<kind>_type]` domain (`Sidecar.subtype_values(kind)` non-empty) | Per declared sub-type: one stub in the kind's uniform role (`role_of(kind, None)`), same shape as the object-valued row above — `entity`, `resource`, `diary` in the nhs/retail bundles |
| Any proposed kind owning a `membership__<kind>__<property>` table | Membership-FK candidate comments appended |

- **`scd` class is per-kind; role is per-sub-type.** `history_tracked` is a property of the
  shared `records__<kind>` columns, so every dimension sub-type of a kind takes the same SCD
  class; only the role varies.
- **The fact fan-out reads observed values, not the registry.** A modelling discriminator (a
  `prop__<kind>_type` on a bare-string fact kind that is not a sub-type taxonomy) fans out by
  `SELECT DISTINCT`. Facts fan out only on `prop__<kind>_type`, never on an arbitrary
  closed-domain property (`prop__status` / `prop__category` are plain columns).
- **An object-valued fact sub-type yields exactly one stub.** Its `prop__<kind>_type` *is*
  the sub-type taxonomy (consumed by the split) and a sub-type is a value of that column, not
  a kind, so there is no second discriminator to fan out on — an `actor` fact sub-type (e.g.
  `ride`) behaves like a bare-string fact with no modelling discriminator.

**Column proposals are role-scoped.** The proposal loop classifies every
records column through the reader's records-column taxonomy
([`reader.md`](reader.md) § The records-column taxonomy) and proposes **payload
and presentation columns only**. Identity columns (`fork_path`, `record_id`,
`record_index`, `ref_index__<name>`) are never proposed; lifecycle columns are
never proposed either — the SCD-2 stub's `valid_from` / `valid_to` are
`history`-derived, not read from lifecycle columns. Non-exempt `slice_only`
columns join them as never-proposed ([`slice-only.md`](slice-only.md)), each
skip emitting one `slice-only-column-omitted` notice; the skip is column-level —
it never removes a kind from proposal — and the exempt discriminator remains
proposable and drives `filter` pre-fill. Any base column stays
reachable by explicit author projection: identity columns are neither proposed
nor specially forbidden in author config — a base column named explicitly
projects faithfully, as any base value does.

**The natural-key advisory and the `keys` proposal.** When the emit's
`presentation_keys` block carries a whole-table claim for a proposed kind (a
flat `key` entry, or a partitioned rollup with a `unique_within`), the kind's
stub carries one comment naming `presentation_id` as the contract-declared
natural key and its scope. No claim, no comment — and no config grammar change:
the advisory is a comment, never a field. `init` also proposes the cross-mode
`keys` election block and aligns its dim stubs with it: a proposed dim whose
source population elects `presentation_id` sources its key column
`from: presentation_id` directly — the claim consumed as a key source, so the
dim-key agreement check holds by construction and the advisory comment is not
emitted for that stub; the proposal is self-gated through the export's own
election machinery ([`key-election.md`](key-election.md) § `init` proposals).
`init` consults the reader's strict accessor and shares its strict-on-read
behavior — an incoherent present block refuses `init` too
([`reader.md`](reader.md) § The presentation-keys registry is strict on read,
[`declared-keys.md`](declared-keys.md)).

`init` is a pure function of `(emit, code version)`: kind and sub-type order come from the
registry's lexicographic key order, the fact fan-out from the reader's `SELECT DISTINCT`
native-type order; no topology traversal, RNG, or clock participates.
`InitRequiresRecordRoles` is a direct child of `ExporterError` (sibling of `ConfigError`,
`ExportError`, `RebaseError`, `IncrementalError`), deliberately **not** an `ExportError` —
that is the engine's "well-formed config does not fit the emit" failure, whereas `init`
runs no engine and reads no config; the CLI `init` verb reports it (and any `ExporterError`)
as a clear stderr message with a non-zero exit.

## Invariants

1. **Determinism.** The exporter is a pure function of `(emit, config, code version)`:
   identical output rows in identical order. Every table carries a total `ORDER BY`
   ending in the grain identity column; ordinals append `record_id` as a tie-break.
   No RNG, clock, or network.
2. **Faithful reshaping.** Every output value traces to a base-layer value. An
   unmapped `value_map` value, an unresolvable FK on some rows, and a declared-but-
   unfillable column resolve to a **typed** `NULL`, never a fabricated value.
3. **Reader-first.** Every table and column fact flows from the `Sidecar`; the engine
   hard-codes no column list, opens `run.duckdb` only through `Emit`, and the writers
   read input only through `Emit.query_arrow`.
4. **Every declared table is emitted.** A grain that resolves to zero rows yields an
   empty *typed* table (DuckDB) or a header-only file (CSV), never a dropped table —
   so the declared star schema is always present downstream. Both writers obey this
   identically.
5. **Trunk-only.** An emit whose sidecar enumerates more than one branch is refused at
   `build_query_specs`.
6. **Author owns role and grain.** The engine never infers `role` or grain; `init`
   only proposes a candidate.
7. **Lookup enrichment is temporally exact.** An accepted `lookup` column reads only
   `temporal_class: constant` columns along its entire path (the exempt discriminator
   excepted — a classification read), so its projected value is independent of
   `slice_at` and equals the value that held at the output row's interval. A path that
   reads any other `tracked` or `slice_only` column, or a column whose temporal pair
   is unavailable, is refused at `build_query_specs` — never silently approximated.
8. **Authors no base-table SQL.** The engine names no base table in SQL it writes; it
   composes the reader's faithful-read builders and the derivations layer's interpretive
   relations and represents over them. A base-table name appears only inside an embedded
   reader or derivation relation.
9. **`init` requires `record_roles`.** It reads warehouse role from the registry and has
   no inference fallback; an emit whose sidecar omits `record_roles` is refused
   (`InitRequiresRecordRoles`), not guessed.
10. **`init` derives role from the registry, never from topology.** Role for every kind
    and sub-type is read from `record_roles`; reference topology influences no role or
    exclusion decision.
11. **`init` column proposals are role-scoped.** Only payload and presentation
    columns are proposed; identity, lifecycle, and non-exempt `slice_only` columns
    never are. Explicit author projection remains the path to any base column —
    except a non-exempt `slice_only` column, which `SliceOnlyColumnRefused` rejects.
12. **The `slice_only` posture.** No config-referenced value-read resolves to a
    non-exempt `slice_only` column; the rules run always-on, full export included
    ([`slice-only.md`](slice-only.md)).
13. **Version structure is election-invariant.** No rendering election creates,
    merges, suppresses, renumbers, or reorders an SCD-2 version row; `valid_from` /
    `valid_to` are computed from raw bounds regardless of any value election on
    the table.
14. **Source-class-blind rendering.** For the same source value, the rendered
    output is byte-identical whether the value was read per-record or per-version —
    an election has one semantics; the source class only selects which rows supply
    values.

## Validation Rules

Two phases, by recoverability. **Parse-time** failures (malformed config, before any
emit is touched) raise `ConfigError`; **business-rule** failures (a well-formed config
that does not fit *this* emit) raise `ExportError` at `build_query_specs` before any
SQL is emitted.

Parse-time validation is the Pydantic model validators in
[`config/models.py`](../../src/fabulexa_forge/config/models.py): exactly one column
source mode; exactly one `derived` sub-field; `scd` set iff `role == dim`; source
fields matching the grain (`property` required for history/membership, `filter` only
on records, `where` only on membership, `value` only on `history_point`); membership
FK fields (`where`/`member_field`/`property`) only on `via: membership` and `path`
only on `via: reference`; `fk.where` refused on a membership-grain table's plain
membership fk (the grain row already is the binding, so there is nothing left for
the predicate to narrow — `source.where` narrows rows there; the point-in-time
`as_of` form keeps `where`); a `lookup` `path` only with its `to` set; and non-empty collections.
Predicate values carry their own well-formedness rule — non-empty, duplicate-free —
on the value type rather than on the models
([`row-predicates.md`](row-predicates.md) § Validation Rules), and `other_where`,
the grammar's one required predicate mapping, must name at least one entry.

The `SingleBranch` guard is the derivations layer's `require_single_branch`
([`derivations.md`](derivations.md)), invoked from `export_dimensional` before the
sidecar business rules — one implementation shared with every other mode, with one
error message. The remaining business rules run against the sidecar in
[`validation.py`](../../src/fabulexa_forge/exporters/dimensional/validation.py):

| Rule | Checks |
|---|---|
| `SingleBranch` | Exactly one branch — the layer's `require_single_branch`, not in `validation.py` |
| `SourceTableExists` | The `records__<kind>` / `history` / `membership__<kind>__<property>` table for each source is in the sidecar |
| `KeyColumnsDeclared` | Every `key` entry is a declared column of its table |
| `ProjectionColumnExists` | Each `from` / `correlation` source — and each `derived: value_map`'s `from` — exists on the grain's projectable surface; no cross-table reach |
| `FkTargetIsDim` | Each `fk.to` names a declared `role: dim` table |
| `ReferencePathResolvable` | A `via: reference` FK has exactly one `references` `prop__` chain from the anchor kind to the dim's source kind, or a `path` hint naming one (each entry a `references` column whose target is the next hop) |
| `MembershipEdgeResolvable` | A `via: membership` FK resolves to exactly one `membership__<kind>__<property>` table (`property` names it when the kind owns several); every `where` column is a real `elem__` column; `member_field` is a reference field (inferred when unique); some rows' `member__<member_field>__kind` equals the dim's source kind |
| `DiscriminatorValueObserved` | Each element of a records `filter` value is among the kind's observed `enum_domains` values for that property — evaluated per element, one `discriminator-value-unobserved` [`Notice`](notices.md) per unobserved element, in config element order. A notice, never an error: a declared-but-unobserved value is a legitimate way to write a config against a family of emits. The message states the table will be empty for a scalar and for a list no element of which was observed; for a partially-observed list, where the observed elements still contribute rows, it says only that the element contributes no rows. A property absent from `enum_domains` (e.g. a modelling discriminator like `decision_type`) carries no observed-value set, so its filter is not checked |
| Dim-population domain gate | Every element of a sub-typed dim's discriminator conjunct is a declared sub-type of the kind's domain — per element, naming the offending one (§ Foreign keys). Distinct from `DiscriminatorValueObserved`: the domain is the declared sub-type registry rather than the observed-value set, and an out-of-domain population is an error, not a notice |
| `SliceOnlyColumnRefused` | No config-referenced value-read resolves to a non-exempt `temporal_class: slice_only` column ([`slice-only.md`](slice-only.md)). The surface list is exhaustive over the grammar: `from`, `correlation`, records `filter` keys, `value_map.from`, `derived: timestamp` `source`, `derived: elapsed` `correlate_on` / `start_source` / `end_source` / `other_where` keys, `derived: date_parse` `from`, `derived: decimal` `from`, `derived: json_precision` `from`, `fk via: reference` resolved-path hop columns (the check runs over the hops the resolution actually traverses), `fk via: membership` `member_path` hop columns and `as_of`. (`lookup` reads are `LookupColumnSafety`'s. Membership element predicates and history-grain scoping are outside the population — those columns carry no class.) Always-on, full export included |
| `OrdinalRefsSiblings` | `ordinal.partition_by` / `order_by` name sibling output columns of the same table |
| `TimestampSourceAvailable` | Each `derived: timestamp`'s `source` is available on the table's grain: an instant-carrying structural column of the grain's table category (resolved through the reader's structural-temporal surface, not a private list), the grain's virtual interval-end column where the grain defines one, or a `prop__<name>` present on the grain's projectable surface (§ Timestamp source) |
| `TemporalRenderRequiresAnchor` | Every explicitly-elected instant rendering — `derived: timestamp`'s `as`, or the `scd_window` object form — has a resolved effective anchor; naming the column when it does not ([`temporal-elections.md`](temporal-elections.md)) |
| `DateParseSourceColumn` | Each `derived: date_parse`'s `from` resolves off the grain's projectable surface and carries a declared VARCHAR type (§ Derived columns); not `slice_only` |
| `DecimalSourceIsDouble` / `JsonPrecisionSourceIsVarchar` | Each `derived: decimal`'s / `derived: json_precision`'s `from` resolves off the grain's projectable surface and carries a declared DOUBLE / VARCHAR type respectively ([`value-rendering-elections.md`](value-rendering-elections.md) § Validation Rules) |
| `Scd2ColumnModeSupported` | Every column of an `scd: type2` table uses an admitted mode — `from`, `null`, `derived: scd_window`, or a pure per-row value rendering (`derived: timestamp` / `date_parse` / `value_map` / `decimal` / `json_precision`), evaluated per record for untracked sources and per version for tracked ones (§ SCD-2 wide reconstruction). `fk`, `correlation`, `derived: ordinal`, and `derived: elapsed` are refused; the error names the column, the table, and the offending mode. The source-type gates (`DecimalSourceIsDouble`, `JsonPrecisionSourceIsVarchar`, `DateParseSourceColumn`, `TimestampSourceAvailable`) and the export-time guards apply to tracked sources through the same sidecar declared-type authority — the declared type is a sidecar fact independent of source class |
| `Scd2NeedsHistory` | An `scd: type2` table declares a `valid_from` `scd_window` column in `key`, the emit carries the `history_tracked` flag, and the kind has at least one tracked column (flag-authoritative; a tracked-but-unchanged column qualifies). A flag-absent emit is refused — re-emit with `history_tracked` — never reconstructed by `history`-table inference |
| `LookupColumnSafety` | A `lookup` column resolves and reads only temporally exact data: the terminal `records__<kind>` table and its `prop__<property>` exist; a unique reference path resolves from the anchor kind to `to` (or the `path` hint validates hop-by-hop); the terminal property plus every traversed hop column are `temporal_class: constant` (the exempt discriminator excepted, any class — § Lookup); a zero-hop self lookup is not on a `records` grain (redundant with `from`); and the table is not `scd: type2` (the SCD-2 wide builder does not project lookup columns) |
| `ExcludedKindNotSourced` | No declared table sources an `exclude.kinds` kind |
| `ExcludedTableNotSourced` | No declared table's source resolves to an `exclude.tables` sidecar table name |
| `check_reserved_presentation_name` | No author-named output column is `last_mutation_sim_time` — a reserved output name (the presentation-name posture; § Output naming). The shared check in [`exporters/reserved_names.py`](../../src/fabulexa_forge/exporters/reserved_names.py); the value channels freely under any other name |

The engine does not validate author `role` against `record_roles`: role is
author-authoritative (Principle #7), and the registry informs only `init`'s proposal.

`ReferencePathResolvable`, `MembershipEdgeResolvable`, and `LookupColumnSafety` share
the reference-resolution derivations' path-resolution logic
([`derivations.md`](derivations.md)), so validation's "is this resolvable?" and the
executed resolution give one answer; all three read the sidecar (the temporal pair
included), never base tables.

`build_query_specs` and every entry point that can emit a notice take a required
`notice_sink` ([`notices.md`](notices.md)); the CLI supplies the stderr renderer.

Output `fmt` is not a business rule: it is a CLI argument, so `cmd_export` rejects an
unknown `--fmt` as a usage error before the emit opens, and `export_dimensional`
always receives a validated `Literal["csv","duckdb"]`.

## Rationale

- **Discriminator split = per-table `filter` + `init` fan-out, no `split` shorthand.**
  Each output fact is its own declaration with a discriminator `filter`; `init`
  proposes one stub per `DISTINCT` value. This matches the heterogeneous reality of
  per-decision facts (different correlation keys, extra columns, NULL-pads) and avoids
  a second abstraction a homogeneous-only case would need. A `split` block fanning one
  source into N templated tables is not added until a homogeneous case demands it
  (Principle #8).
- **`init` reads role from `record_roles`, not reference topology.** Role is not
  recoverable from topology — `actor` points nowhere yet is the central dimension, a
  terminal event record also points nowhere, `journey_instance` is a tracked, pointed-at,
  outward-pointing hybrid — and a topology heuristic is right only ~70–80% of the time and
  cannot express a `fact`-role `actor` sub-type, which one `records__actor` table can hold
  beside a `dimension` sub-type. The sanitised emit declares the role, so `init` reads the
  declaration directly: it runs no topology role heuristic and proposes no structural-island
  `exclude` entry. The engine never reads `record_roles` — role is author-declared
  (Principle #7) and the registry informs only `init`'s proposal.
- **`init` sources the sub-type split from `enum_domains` via `Sidecar.subtype_values`, role
  from `record_roles`.** These are two different questions the contract answers with two
  different fields, and only `actor` happens to make them look like one: `record_roles`'s
  object-vs-string shape is guaranteed to vary only for `actor` (`base-format.md` § Record
  roles — "every other kind has one fixed role uniform across its sub-types"), but a
  bare-role kind (`entity`, `resource`, `diary`) can still carry a real `<kind>_type`
  domain and needs splitting just as much as `actor` does. An earlier revision sourced the
  split from `record_roles`'s object-vs-string shape on the theory that the two fields
  "agree in a conformant emit" and a divergence "could only degrade `init`'s proposal" —
  that theory is false: `record_roles["entity"]` is a bare string by contract regardless of
  `enum_domains["entity"]["entity_type"]`, so the two fields were never testing the same
  condition, and the bare-role-but-subtyped case is not an edge-case divergence but the
  contract's stated norm. For `actor`, `record_roles[kind]`'s keys remain the authoritative
  sub-type set (they alone carry the per-sub-type role); for every other sub-typed kind,
  `subtype_values` drives the split and `record_roles.role_of(kind, sub_type)` resolves the
  one uniform role per stub.
- **SCD window columns are explicit `derived: scd_window` columns**, not columns
  synthesized implicitly from `scd: type2`. This keeps the column tier uniform — every
  output column names exactly one source mode — and lets the author choose the window
  column names.
- **Unmapped `value_map` values resolve to `NULL`**, faithful to the principle that
  the exporter never invents a mapping. An author who wants strictness lists every
  value.
- **Output naming is author-verbatim; prefix-stripping lives only in `init`'s
  proposal** (with the structural-column collision check there). The engine does no
  implicit renaming.
- **`fact` fidelity for multi-pick is enforced by making the binding the grain**
  (`grain: membership`), not by deduplicating decisions. Single-pick roles may be
  expressed either way.
- **Membership edges are selected by a `where` predicate over `elem__` columns, not a
  bare `role` string.** The contract fixes only four structural membership columns; the
  label, ordinal, and reference fields are all `element_schema` names the scenario
  author chose (`elem__role_name` / `elem__pick_index` / `member__entity__id` are
  domain-specific, not contract columns — hardcoding them is a Principle #1/#2 hazard).
  So the membership FK takes `where` (mirrors the records-grain `filter`),
  `member_field` (which `member__<f>__id` to follow; inferred when unique), and
  `property` (which `membership__<kind>__<p>` table; inferred when the kind owns one).
  A global `membership_label_column` convention is a near-universal assumption the
  contract does not license. This also keeps "role" meaning only dim/fact.
- **A reference `path` hint is an ordered list of `prop__<x>` reference columns, not
  intermediate kinds.** Naming the edge column pins both the hop and its target kind,
  so it disambiguates two paths between the same kinds *and* two columns from one kind
  to the same next kind in one mechanism, rather than kinds plus a separate per-hop
  tiebreak (two constructs for one job).
- **`key` is the declared logical primary key — validated, not materialized as a
  physical constraint.** The engine checks every `key` column is declared
  (`KeyColumnsDeclared`) and treats `key` as the table's documented grain (it drives
  `init`'s proposals and records the warehouse PK) but emits no DuckDB `PRIMARY KEY` /
  `UNIQUE`. Reasons: *writer symmetry* — the CSV writer cannot express a constraint, so
  materializing one only in DuckDB would split the two writers' output semantics;
  *faithful reshaping* — a `PRIMARY KEY` index would fail the export on faithful-but-
  duplicate keys, violating good-enough (Principle #6); and the deterministic
  `ORDER BY` already owns reproducible row order, so `key` is not load-bearing there.
- **NULL-pad columns are typed `VARCHAR` (`CAST(NULL AS VARCHAR)`), not bare `NULL`.**
  A bare all-`NULL` column is exactly the untyped-object column the DuckDB Arrow writer
  is built to avoid; the `CAST` makes the column typed and registrable. The exporter
  consumes no target file, so it does not infer the column's intended type
  (Principle #7); retyping is a downstream concern.
- **Every declared table is emitted, even at zero rows** — an empty typed table
  (DuckDB) or header-only file (CSV), uniform across both writers. "Skip empties" would
  make the two writers' table sets diverge for the same config and would surprise a
  declared-but-unobserved discriminator (`DiscriminatorValueObserved`, a warn); the
  uniform rule falls out for free because a zero-row `Emit.query_arrow` result still
  carries its typed schema.
- **`lookup` projects an attribute *value*; `fk` projects a *key*.** A history- or
  membership-grain fact carries the changed or owner record's `record_id` but none of
  its other scalar properties, and `from:` reads only the grain's own surface. `fk:`
  reaches another record but yields its surrogate key and forces declaring that kind a
  dimension. `lookup` fills the gap — it projects a related (or own) record's
  `prop__<property>` value directly, and its terminal kind need not be a declared dim.
  It reuses the `fk` reference-edge pathfind, differing only in the terminal projection
  (the attribute, not the key) and in not requiring a dim target.
- **`lookup` is gated to type-1 and refuses type-2 rather than approximating it.**
  `records__<kind>` carries each record's slice value (state at `slice_at`). For a
  type-1 property that value held at every past interval, so projecting it onto a
  history-grain row is exact; a type-2 property's slice value is its final value, and
  stamping it onto a past interval would fabricate a value that never held (a
  temporal-integrity violation). Reading a type-2 value as it stood *during* an interval
  needs a correlated as-of join over `history` — a separate, larger feature — so
  `lookup` refuses type-2 targets. Like the SCD-2 reconstruction, the gate is
  **flag-only**: an emit that cannot verify the SCD class is refused rather than inferred
  from `history` contents, because a `lookup` false negative would admit the very
  temporal paradox the gate prevents.

## Boundaries

What the dimensional exporter deliberately does not own:

- **Branch reshaping.** It operates on the emit's sole branch and refuses an emit with
  more than one. Branch selection, paired-counterfactual export, per-branch slices, and
  provenance lineage columns are parked — the sanitised subset mandates one branch and
  carries no provenance.
- **Timestamp resolution.** The rendered `derived: timestamp` is microsecond-resolution
  (the DuckDB `TIMESTAMP` / interval limit); a sub-microsecond `sim_time` tail is
  truncated. Anchor *resolution* (origin/zone precedence, DST, ambiguity) is owned by
  the effective-anchor surface, not here — see [`anchor.md`](anchor.md).
- **`firings`-as-facts.** The sanitised subset carries no `firings` table, and rule-firing
  fact tables are not a grain source; the fact backbone is the four grains above.
- **Provenance columns.** The sanitised subset carries no `created_by_*` / `written_by_*`
  / `deactivated_by_*` provenance columns, and the engine projects none: a config naming
  one fails `ProjectionColumnExists` (the column is absent from the sidecar) like any
  unavailable source.
- **Type-2 (as-of) enrichment.** Reading a history-tracked property's value as it stood
  *during* a row's interval — a correlated as-of join over `history`, with its own
  interval-edge and determinism semantics — is not owned here. `lookup` serves only the
  type-1 case and refuses type-2 targets at validation.
- **Version-grain relational semantics.** A type-2 table admits only pure
  per-row value modes (§ SCD-2 wide reconstruction). `fk`, `correlation`,
  `derived: ordinal`, and `derived: elapsed` each raise a genuine semantic
  question on version rows — which version an edge resolves against, what an
  ordinal partitions over when one record contributes several rows, which
  version bound anchors a cross-row delta — and a version-grain answer is
  not defined here.
- **`lookup` candidate generation in `init`.** `init` proposes no `lookup` columns: an
  unfillable type-1 history- or membership-grain attribute is not auto-surfaced as a
  candidate, and the `init` inference contract carries no `lookup` proposal. The author
  adds `lookup` columns by hand.
- **Queue-state derivation.** Membership read-joins and membership-grain role-binding
  facts are in scope; deriving queue state (wait time, FIFO/priority order) and
  materializing `waiters`/`holders` as queue/bridge tables is not.
- **Presentation-id remap.** The join *mechanism* is `record_id` — every pathfind
  hop keys on it. A `presentation_id` surrogate, when the emit carries one, is a
  faithful passthrough column and an electable FK surface
  ([`key-election.md`](key-election.md)); minting `PAT_`-style ids or synthesizing
  names from scratch is a separate package's job, never the exporter's.
- **Other modes and corrupters.** Only `mode: dimensional` is defined here; `base` /
  `source` / `streaming` modes and corrupters are separate surfaces. CSV and DuckDB are
  the writers; Parquet is not.
- **Conformance re-validation.** Neither the engine nor `init` re-validates the emit: the
  engine consumes `history_tracked` (C11) and `init` reads `record_roles` (C12) on an emit
  both trust to be conformant (as they trust all of C1–C15) — see
  [`conformance.md`](conformance.md) § Boundaries.

## Related

| Document | Why |
|---|---|
| [`anchor.md`](anchor.md) | The `EffectiveAnchor` resolution surface — origin/zone precedence, the `rebase` config + CLI flags, the anchor `derived: timestamp` renders through |
| [`derivations.md`](derivations.md) | The interpretive layer this mode composes — versioned-intervals and reference-resolution; the source of the shared `require_single_branch` guard |
| [`playback.md`](playback.md) | The seam whose tier-2 `state` compiles this mode over a truncated tape via `base_relations`; the presentation-name posture's companion |
| [`reader.md`](reader.md) | The `Emit` / `Sidecar` surface this reads through — `query_arrow`, the `history_tracked` flag, the faithful-read builders, the per-type decode contract |
| [`row-predicates.md`](row-predicates.md) | The scalar-or-list grammar and rendering authority the mode's five predicate surfaces share |
| [`conformance.md`](conformance.md) | The C1–C15 contract the input is trusted to satisfy |
| [`key-election.md`](key-election.md) | The cross-mode key-election surface — FK `target_key` semantics, inheritance, the dim-key agreement check, `init`'s `keys` proposal |
| [`temporal-elections.md`](temporal-elections.md) | The cross-mode election vocabulary `derived: timestamp` / `scd_window` / `elapsed` / `date_parse` render through — the full election set, anchor-requirement rule, and declared date-parse contract |
| [`value-rendering-elections.md`](value-rendering-elections.md) | The value elections `derived: decimal` / `derived: json_precision` spell per column — semantics, guards, and the shared rendering authorities |
| [`config/models.py`](../../src/fabulexa_forge/config/models.py) | The config grammar these semantics bind |
| [`../../contract/base-format.md`](../../contract/base-format.md) | The input contract (table categories, `references`, membership, `history_tracked`) |
| [`../CAPABILITIES.md`](../CAPABILITIES.md) | Feature inventory and status |
| [`README.md`](README.md) | Design index, package layout, staged roadmap |
| [`../../CLAUDE.md`](../../CLAUDE.md) | Principles, the isolation boundary, vocabulary |
