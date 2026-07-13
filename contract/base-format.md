# Base Format

The on-disk shape of one **base-layer emit** — a standalone, self-describing dataset (`run.duckdb` + `base.json`) that looks like production data. The base layer is the canonical machine-readable rendering; downstream tools consume a base-layer emit and either reshape it (exporters: dimensional warehouses, DW staging, denormalized views) or inject data-quality problems (corrupters, which produce new base-layer emits with intentionally broken semantic conformance).

**Purpose.** Define the contract precisely enough that any producer or consumer — in any language, in any repo — can implement this format without depending on any package other than DuckDB itself.

**Status.** Reference producer is the Fabulexa publishing step. The conformance procedure (C1–C13) is verified by the producer's own conformance suite and by the standalone `tools/check_published_conformance.py`.

**Audience.** Producers (today: the Fabulexa base writer; tomorrow: any base-layer producer, in any repo or language), consumers (future exporters, corrupters, analyst tooling, third-party tools).

---

## Contract surface

The contract is **two artifacts per emit**, not a Python package:

```
<emit_dir>/
  run.duckdb        ← base-layer tables (described by base.json)
  base.json         ← sidecar manifest enumerating tables and columns
```

| Artifact | Defined by | Consumed by |
|---|---|---|
| `run.duckdb` | DuckDB's own format | DuckDB (any version supporting the schema written) |
| `base.json` | `base-format.schema.json` (sibling of this doc) | Any reader of any kind |

**`BASE_FORMAT_VERSION = 5`** — lives in the sidecar JSON, not in any Python package. No code imports needed to learn the version.

---

## Required tables

Three table categories. Every base-layer DuckDB MUST contain at least the *fixed-category* tables; *records-category* tables exist one per declared record kind in the persisted run's schemas (zero or more); *membership-category* tables exist one per collection-struct property with at least one membership interval at the emit's slice (zero or more).

| Category | Table name(s) | Cardinality | Rows scope |
|---|---|---|---|
| `fixed` | `history` | exactly one | one row per `HistoryEntry` per history-tracked property per record per emitted branch |
| `records` | `records__<kind>` | one per kind that has at least one record at the emit's slice | one row per `(fork_path, record_id)` reflecting end-of-slice state |
| `membership` | `membership__<kind>__<property>` | one per collection-struct property with at least one membership interval at the emit's slice | one row per membership interval per emitted branch |

An emit whose run has no records of kind *K* in the branch's slice MAY omit `records__K` from the DuckDB and from the sidecar. Empty tables (zero rows but present) are also legal.

Producers MAY also omit `records__K` for **internal bookkeeping kinds** — kinds the producer both authors and consumes for its own machinery, which authors do not declare in scenario YAML and downstream consumers cannot use without producer-internal knowledge. The sidecar accurately reflects what was written, so consumers learn any such omission by table absence and need no out-of-band list.

Tables MUST NOT exist beyond these three categories. A future format version may add categories; readers MUST gate on the version constant.

---

## Fixed-category column lists

### `history`

Six required columns, in this order:

| # | Column | Type | Meaning |
|---|---|---|---|
| 1 | `fork_path` | VARCHAR | branch this row is attributed to (canonical) |
| 2 | `kind` | VARCHAR | record kind whose property changed |
| 3 | `record_id` | VARCHAR | id of the record whose property changed (kind is column 2) |
| 4 | `property` | VARCHAR | name of the property that changed |
| 5 | `sim_time` | BIGINT | simulation time the change took effect |
| 6 | `value` | VARCHAR | new value at `sim_time`, text-encoded by the codec |

`history.value` is always VARCHAR. Consumers cast on read. See *Recommended type mapping* for codec rules.

**Long-form SCD-2.** `history` is the *long-form* rendering of SCD-2 property history: one row per change event, ordered by `sim_time` within each `(fork_path, kind, record_id, property)` series. The validity interval is **implicit** — a row's value holds over `[sim_time, next row's sim_time)` for the same series, and the final row of a series holds from its `sim_time` through the slice boundary. There are deliberately **no `valid_from` / `valid_to` columns**: a consumer asking what a value was at time T never needs to materialize an interval — see § Consumer derivations → *Point-in-time value reconstruction* (one record) and → *Bulk as-of join* (set-valued). `sim_time` is strictly increasing within a series, so the implicit interval is well-formed. Consecutive rows in a series always differ in `value`: the producer treats a value-unchanged write as a no-op, so rows are change events, never write echoes.

**Creation-seed guarantee (unconditional).** Every type-2 (`history_tracked: true`) property of every record is seeded with a `history` row at the record's `created_sim_time` carrying its creation value — **including properties absent at creation, which seed with a NULL `value`**. Creation is the genesis change event (undefined → initial value), so the *one row per change event* model covers it with no exception.

The seed is unconditional: there is no carve-out. A type-2 property with **zero** `history` rows for a record that exists is a **contract violation** (flag-without-rows is forbidden), not a signal that the creation value was NULL. Consequently:

- A record's creation after-image (t0) is recoverable from `history` alone — no `records__<kind>` join and no zero-rows inference.
- "State at T" for any type-2 column is a single ordered lookup over `history` (§ Consumer derivations → Point-in-time value reconstruction), or an as-of join for the set-valued form.

NULL is layer-scoped: it is a legal **history-entry** value (a never-supplied tracked property reads NULL in its genesis row *and* in its `records__` cell, so the cross-table round-trip C6 compares NULL to NULL), but it remains an illegal **property** value — producers reject NULL creation fills and NULL writes.

Collection-struct (set-valued) properties are the one exception to the whole scheme: they emit no `history` rows at all — the `membership__<K>__<p>` table is their sole representation — so they stay outside the input sets of C6, C11, and C13.

**Row order.** Ordered by `sim_time` within each `(fork_path, kind, record_id, property)` series, bounded by `sim_time <= slice_at`. No producer-local sort.

---

## Records-category tables

For each kind *K* with at least one record at the emit's slice, one table named `records__K`. Columns, in order:

| Position | Column | Type | Meaning |
|---|---|---|---|
| 1 | `fork_path` | VARCHAR | branch this row is attributed to (canonical) |
| 2 | `record_id` | VARCHAR | id of the record (kind is in the table name) |
| 2a *(optional)* | `presentation_id` | sidecar-declared scalar | presentation surrogate-identity column; present iff a non-`inherit` `presentation_id` strategy minted it. `history_tracked: false`. See § Column SCD class. |
| 3 | `created_sim_time` | BIGINT | `sim_time` at which the record was created. Non-NULL on every row; set once at creation and never changed by a later property write or deactivation |
| 4 | `active` | BOOLEAN | whether the record is active at the slice boundary |
| 5 | `deactivated_at` | BIGINT (nullable) | `sim_time` the record was deactivated; NULL iff `active` |
| 6 | `last_mutation_sim_time` | BIGINT | `sim_time` of the record's most recent content change — any property write or deactivation |
| 7 .. 6+P | `prop__<name>` | per-property type (see below) | current value of property `<name>` at the slice; NULL iff the property is absent on this record |

Where P is the count of *scalar* declared properties for kind *K* — a collection-struct property (one with an element schema) contributes no `prop__` column; it is materialized as a membership table instead (§ Membership-category tables). `prop__<name>` columns appear in the kind's schema declaration order.

**Optional `presentation_id` column.** When a non-`inherit` `presentation_id` strategy minted a surrogate for the kind, `presentation_id` occupies the slot immediately after `record_id`; the lifecycle columns (positions 3–6) and the `prop__` block then shift down by one position. It is the only structural column whose presence is optional and whose type is producer-determined: the sidecar is authoritative for its (scalar) type, and C2's sidecar↔catalog cross-check guarantees agreement, so the spec pins its name and position but not its type. It is permitted in this slot and **nowhere else** — a `presentation_id` at any other position fails C5 (it displaces a pinned lifecycle column, or lands in the `prop__` block as a non-`prop__` column), so widening this one slot does not weaken the positional check. An `inherit`-strategy kind mirrors `record_id` and adds no column.

**Per-property column type.** Determined by the property's declared Python type per the *Recommended type mapping* table below. Producers MAY use a different mapping if they adhere to the round-trip rules in *Conformance*; the sidecar always reflects the actual type written.

**References-annotated properties.** When a property declares a record-to-record `references` annotation, the renderer emits `prop__<name>` as a single `VARCHAR` column carrying the id portion of the referenced record id (the id only, not the kind). The kind component is redundant with the schema annotation and is exposed via the sidecar's `references` field on the column entry. Producers MUST use this id-only form so downstream tools can equality-join against `records__<references>.record_id` without parsing tuple reprs.

**`created_sim_time` is the record's immutable creation time.** Position 3 carries the `sim_time` at which the record was created and is set exactly once. It is unaffected by every later content event — a property write and a deactivation both leave it unchanged — and is non-NULL on every row, including write-once fact records (`history_tracked: false`). Consumers MAY use it to bound a record's lifetime from below.

**`last_mutation_sim_time` bounds every content change to its record.** Position 6 advances on *every* content event for the record — creation, each property write, and deactivation. A deactivation flip is a content change, **not** exempt: a record whose only post-creation event is deactivation carries `last_mutation_sim_time == deactivated_at`. Producers MUST uphold this so consumers MAY treat the column as a high-water mark over the record's whole lifecycle, deactivation included. This is binding at `base_format_version: 5` — an existing guarantee promoted to contract, not a new field or column.

**Row order.** Creation order within kind, lexicographic on kind across kinds — the order in which the producer created each record, preserved by insertion-order iteration. A kind whose records are created through more than one id-minting path (e.g. sequential integer-string ids and hex-digest ids on the same kind) yields rows interleaved by creation time, **not** sorted by `record_id` value. Consumers MUST NOT rely on any sort derived from `record_id` — ids minted by different paths are structurally disjoint, and lexicographic order over the mixed set carries no semantic meaning.

---

## Membership-category tables

A **collection-struct property** is a history-tracked `tuple` property whose value is a tuple of fixed-shape referencing structs — a queue, a holder set, or any time-varying collection of structured memberships. It is declared by an **element schema** on the property (a fixed-shape struct definition for its members). Such a property is **not** rendered as a `prop__<name>` column or `history` rows; it is materialized as one membership table.

For each collection-struct property *p* on kind *K* with at least one membership interval within the emit's slice, one table named `membership__<K>__<p>`. A row is one **membership interval** — a contiguous span during which one struct element was present in the collection. Columns, in order:

| Position | Column | Type | Meaning |
|---|---|---|---|
| 1 | `fork_path` | VARCHAR | branch this row is attributed to (canonical) |
| 2 | `record_id` | VARCHAR | id of the owning record (kind is the `<K>` table-name segment) |
| 3 | `joined_sim_time` | BIGINT | `sim_time` the element joined the collection |
| 4 | `left_sim_time` | BIGINT (nullable) | `sim_time` the element left; NULL = still present at the slice boundary |
| 5 .. | element-field columns | per field | one or two columns per element-schema field, below |

Element-field columns follow the fixed prefix in element-schema declaration order:

- A **non-reference** field *f* → one column `elem__<f>`, typed by the *Recommended type mapping* for the field's Python type (the scalar rows of that table, including `bytes` → `BLOB`). A nullable field maps `None` → SQL `NULL`.
- A **reference** field *f* → two columns `member__<f>__kind` (VARCHAR) and `member__<f>__id` (VARCHAR) — the split of the member's record id (kind and id). The pair is equality-joinable against `records__<member__f__kind>.record_id`.

The owning kind and property are not columns: `<K>` and `<p>` are the table-name segments, and the sidecar entry carries `record_kind` and `property` explicitly — mirroring how `records__K` omits a `kind` column.

**Row order.** Rows are grouped by the producer's deterministic `(branch, owning record)` traversal order — the same traversal that orders `records__K` and `history`. Within one `(branch, record)` group, rows are ordered by the sort key `(joined_sim_time, element-field values in element-schema declaration order)`: a reference field compares by its `(kind, id)` tuple, a scalar field by its natural ordering with NULL ordering before any non-NULL value. Two byte-identical intervals (same key, multiplicity ≥ 2) are indistinguishable, so their relative order is immaterial and the result is deterministic regardless.

**NULL semantics.** `left_sim_time` is NULL exactly when the element is still present at the slice boundary. An `elem__<f>` column is NULL exactly when that struct slot's value was `None`. A reference field's `(member__<f>__kind, member__<f>__id)` pair is all-NULL-together or all-non-NULL-together — a reference is never half-present (C7).

**Member kind is per-row, not fixed.** Because `member__<f>__kind` is a column, one membership table may reference records of more than one kind; the format neither assumes nor requires a single member kind per collection. `<K>` in the table name is the kind that *owns* the property, not the member kind.

---

## Consumer derivations (non-binding)

Recipes that consumers frequently derive from membership tables. These are derivable by ordinary SQL; the base format stores facts, not computed analytics (Principle #3).

### Point-in-time membership lookup

To find the OWNER record that held a given MEMBER at simulation time T:

```sql
-- Who held member <actor_id> at time T?
SELECT h."record_id"   -- the owner's record_id
FROM "membership__<K>__<p>" h
WHERE h."member__<f>__id" = '<actor_id>'
  AND h."member__<f>__kind" = '<member_kind>'
  AND h."joined_sim_time" <= <T>
  AND <T> < COALESCE(h."left_sim_time", 9223372036854775807)
ORDER BY h."joined_sim_time" DESC, h."record_id" ASC
LIMIT 1
```

**Containment predicate:** `joined_sim_time <= T < COALESCE(left_sim_time, +∞)`. The open-interval sentinel `9223372036854775807` (INT64 max) substitutes for `NULL` `left_sim_time`, meaning the element is still present at the slice boundary.

**Determinism:** When a member can logically hold at most one slot at any instant, the `LIMIT 1` with `ORDER BY joined_sim_time DESC, record_id ASC` produces a deterministic result. If the domain permits true concurrent holds, the result is an arbitrary-but-deterministic pick; consumers that need all concurrent holders omit `LIMIT 1`.

**Result NULL:** If no row satisfies the containment predicate, no owner held the member at time T — the result is `NULL`. This is correct; do not substitute a default.

### Point-in-time value reconstruction

To recover a column's value as of simulation time T, **dispatch on the column's `temporal_class`** (§ Column temporal semantics). Reading the class first is what makes the answer trustworthy — the three classes have three different correct answers, and one of them is "refuse".

| `temporal_class` | How to answer "value at T" |
|---|---|
| `constant` | Read `records__<kind>.prop__<name>` directly. The current value is valid at every T ≥ `created_sim_time`. No history query. |
| `tracked` | Ordered lookup over `history` (below), or an as-of join in bulk. Exact for any T. |
| `slice_only` | **Refuse.** The value at T is unknowable; only the value as of this emit's `slice_at` is known. Do not substitute the slice value for an as-of-T value. |

For a `tracked` column — the last recorded value at or before T:

```sql
-- Value of <kind>.<property> for <record_id> at time T, on one branch
SELECT h."value"
FROM "history" h
WHERE h."fork_path" = '<fork_path>'
  AND h."kind"      = '<kind>'
  AND h."record_id" = '<record_id>'
  AND h."property"  = '<property>'
  AND h."sim_time" <= <T>
ORDER BY h."sim_time" DESC
LIMIT 1
```

**No `records__` join, no zero-rows special case.** The unconditional creation seed (§ `history`) guarantees a genesis row at `created_sim_time`, so for any T ≥ `created_sim_time` this query always returns a row. A NULL `value` in the result means the property was genuinely NULL at T — not "no data".

**Result empty** iff T < `created_sim_time` — the record did not exist yet. That is a correct answer; do not substitute a default.

`history.value` is VARCHAR; cast on read per § Recommended type mapping.

### Bulk as-of join

The recipe above answers *one record at one T*. The common question is set-valued — the value of a property for **every** probe row, each at its **own** T (e.g. the status of each patient at the instant each of its decisions fired). The same `temporal_class` dispatch governs: a `constant` column is an ordinary equi-join to `records__<kind>`, a `slice_only` column is refused, and a `tracked` column uses an as-of join:

```sql
-- Value of <kind>.<property> for every probe row, as of that row's own T
SELECT e."record_id", h."value"
FROM <probe_table> e
ASOF LEFT JOIN (
  SELECT "record_id", "sim_time", "value" FROM "history"
  WHERE "fork_path" = '<fork_path>'
    AND "kind"      = '<kind>'
    AND "property"  = '<property>'
) h
  ON h."record_id" = e."<ref_column>"
 AND e."<T_column>" >= h."sim_time"
```

`ASOF` resolves each probe row against the last `history` row at or before that row's own T. It is the set-valued form of the `ORDER BY sim_time DESC LIMIT 1` above, and exact for the same reason: the unconditional creation seed guarantees a genesis row at `created_sim_time`.

**Inner vs `LEFT`.** `ASOF LEFT JOIN` keeps probe rows that have no `history` row at or before T (T < `created_sim_time`; `value` is NULL). A plain `ASOF JOIN` drops them. The two differ exactly on the *record did not exist yet* case — choose deliberately, and do not substitute a default for either.

**Do not materialize intervals to do this.** Deriving explicit `[valid_from, valid_to)` intervals with a window function and range-joining them yields the same answer at strictly higher cost, and the resulting range join is a planner hazard: on older DuckDB versions it can degrade catastrophically (observed: an unbounded stall on DuckDB 1.1.3 over a join the as-of form completes in under 0.1s). The as-of join needs no interval columns — which is why the format stores none (§ `history`).

### Firing-time recovery for write-once records

For record kinds whose rows are written exactly once (e.g. `tick_decision`), `last_mutation_sim_time` equals the firing time. Use it as the T argument in the point-in-time lookup above. This recovers the owner that was holding the member at the exact moment the decision fired — for example, the consultant a patient was assigned to when a clinical decision event occurred.

---

## The sidecar: `base.json`

Authoritative description of what the producer wrote. Consumers MUST read the sidecar to discover table list and column shape; consumers MUST NOT hard-code column lists from this spec because the spec is the *minimum* — a producer may add future-format-version columns the sidecar describes.

### Schema

The sidecar's JSON Schema is `base-format.schema.json`, beside this doc. Conformance: `base.json` MUST validate against the schema for the corresponding `base_format_version`.

### Shape

```json
{
  "base_format_version": 5,
  "branches": [
    {"fork_path": "trunk", "parent": null, "slice_at": 1728000000000000}
  ],
  "runtime": {
    "timezone": "Europe/London",
    "start_datetime": "2024-01-01T00:00:00+00:00"
  },
  "tables": [
    {
      "name": "history",
      "category": "fixed",
      "columns": [...],
      "rows": 5678
    },
    {
      "name": "records__patient",
      "category": "records",
      "record_kind": "patient",
      "columns": [...],
      "rows": 100
    }
  ]
}
```

### Field semantics

| Field | Type | Required | Meaning |
|---|---|---|---|
| `base_format_version` | integer | yes | Format version. Current value: `5`. |
| `branches` | array | yes | Exactly one entry — a sanitised emit covers a single branch. See § Branch enumeration and runtime anchor. |
| `branches[].fork_path` | string | yes | Canonical `@`-joined fork path of the single branch. |
| `branches[].parent` | string \| null | yes | Parent fork path (the `@`-joined prefix), or `null` for a root branch; the named parent need not be present in the emit. |
| `branches[].slice_at` | integer | yes | The `sim_time` this branch was sliced at in this emit. |
| `runtime` | object | optional | Run-level wallclock anchor. Present only when the scenario declared a `runtime:` calendar block; omitted entirely otherwise. |
| `runtime.timezone` | string | yes (within `runtime`) | IANA timezone string. |
| `runtime.start_datetime` | string | yes (within `runtime`) | Tz-aware ISO-8601 datetime for `sim_time = 0`. |
| `pinned_ids` | object | optional | Pin identity surface, nested `{<kind>: {<label>: <id-string>}}`. Present only when the run had pinned actors; omitted entirely otherwise. Each `<id-string>` is the id portion of the minted record id, equality-joinable against `records__<kind>.record_id`. See § Pin identity surface. |
| `enum_domains` | object | optional | Closed-domain registry, nested `{<kind>: {<property>: [<option>, ...]}}`. Present only when the scenario declared at least one closed-domain string property (`status` / `category` typed property or a synthesized sub-type discriminator); omitted entirely otherwise. Keys at both nesting levels are sorted lexicographically; option lists preserve declaration order. The authoritative list of allowed values for each closed-domain string property. See § Closed-domain registry. |
| `record_roles` | object | optional | Warehouse-role registry, nested by kind. The `actor` entry is an object `{<sub_type>: role}`; every other records-category kind maps to a single role string (`"dimension"` or `"fact"`). Present when the emit carries ≥ 1 records kind; keys sorted lexicographically at every level. See § Record roles. |
| `tables` | array | yes | Tables present in `run.duckdb`, in the same order as DuckDB's catalog. |
| `tables[].name` | string | yes | DuckDB table name. |
| `tables[].category` | enum | yes | `"fixed"`, `"records"`, or `"membership"`. |
| `tables[].record_kind` | string | only when `category` in `{"records", "membership"}` | Kind name. For a `records` table, the suffix of `name` after `records__`. For a `membership` table, the `<kind>` segment of the table name — the kind that *owns* the collection-struct property, not the member kind (the member kind is the per-row `member__<f>__kind` column). |
| `tables[].property` | string | only when `category=="membership"` | The collection-struct property name; equals the table-name segment after the final `__`. |
| `tables[].columns` | array | yes | Columns in DuckDB-catalog order. |
| `tables[].columns[].name` | string | yes | Column name. |
| `tables[].columns[].type` | string | yes | DuckDB type literal (e.g. `"BIGINT"`, `"VARCHAR"`). |
| `tables[].columns[].nullable` | boolean | optional, default `true` | DuckDB column nullability. Omittable when consistent with the spec's nullability rules. |
| `tables[].columns[].references` | string | optional | Record kind this column points at, when the source schema declared a record-to-record reference. Present iff the column is a foreign-key column (one `VARCHAR` carrying the id portion of a record id); equality-joinable against `records__<references>.record_id`. Omitted for all other columns. Backward-compatible under the rule that unknown fields MAY warn but MUST NOT fail. |
| `tables[].columns[].history_tracked` | boolean | optional | SCD class of a value-carrying column: `true` = type-2 (priors recoverable from the `history` table), `false` = type-1 (current value only). Present on records-category `prop__<name>` columns and presentation-property columns; omitted on structural, fixed-table, membership, and `presentation_id` columns. See § Column temporal semantics. Backward-compatible under the rule that unknown fields MAY warn but MUST NOT fail. |
| `tables[].columns[].temporal_class` | enum | optional | Point-in-time semantics of a value-carrying column: `"constant"`, `"tracked"`, or `"slice_only"`. Answers *"can I ask what this column's value was at time T?"*. Carried on **exactly** the columns that carry `history_tracked` — a column carries one iff it carries the other. See § Column temporal semantics. |
| `tables[].rows` | integer | yes | Row count of the table. |

The fields above are the *required* shape at `base_format_version: 5`. Producers MAY add other top-level fields (cross-emit linkage, pin-identity surfaces, producer hints) as optional extensions; a reader encountering unknown fields under a `base_format_version: 5` sidecar MAY warn but MUST NOT fail. See § Format versioning for which additions are version-compatible vs. require a bumped version.

### Branch enumeration and runtime anchor

`branches` and `runtime` make a known emit interpretable from `base.json` + `run.duckdb` alone — no package import and no companion file.

**`branches`** has exactly one entry — a sanitised emit covers a single branch. `fork_path` is that branch's canonical `@`-joined path; `parent` is the `@`-joined prefix (the path with its last `@`-segment removed) or `null` for a root branch, and the named parent need not be present in the emit (a descendant branch selected at export names a parent that was not carried); `slice_at` is the `sim_time` the branch was sliced at. Every table's `fork_path` equals this branch's `fork_path`.

**`runtime`** is the run's wallclock anchor. `sim_time` is an integer nanosecond offset; converting it to a real datetime requires the run's `timezone` and `start_datetime`:

| Condition | Result |
|-----------|--------|
| Scenario declared a `runtime:` calendar block | `runtime` is present; `timezone` is the IANA string, `start_datetime` is the tz-aware ISO-8601 anchor for `sim_time = 0` |
| Scenario declared no `runtime:` block | `runtime` is omitted entirely; a consumer treats `sim_time` as an uninterpretable integer offset |

`runtime` is a single run-level block; `sim_time = 0` anchors the emit. `start_datetime` is localized in `timezone`; the two are mutually consistent by construction.

### Pin identity surface

`pinned_ids` lets a consumer of a known emit resolve a *pin label* — the
name an author wrote in `scenario.yaml` `initial_state` — back to the
`record_id` the producer minted for it, without re-running the producer
against the archived YAML.

The block is a nested object `{<kind>: {<label>: <id-string>}}`:

| Condition | Result |
|-----------|--------|
| Run had no pinned actors | `pinned_ids` is omitted entirely; an empty `pinned_ids: {}` is equivalent but producers omit the key |
| Run had pinned actors | One entry per kind that has ≥ 1 pinned record; each per-kind object maps `<label>` to the id portion of the minted record id |
| `<id-string>` | The kind-scoped id (`record_id[1]`); the kind is implicit in the outer key. Equality-joinable against `records__<kind>.record_id` |

Pin identity is established at run initialization, so the emitted branch
carries each pinned record. Adding `pinned_ids` is a version-compatible
extension: a reader that does not recognize the key ignores it (unknown
top-level fields MAY warn but MUST NOT fail).

`pinned_ids` is the single pin surface — there is no companion `pinned_label`
column on the `records__<kind>` tables. A consumer that wants labels inline
builds a CTE from `pinned_ids` and `LEFT JOIN`s it; the sidecar carries no
analytical column no known consumer needs (Principle #8). The surface is also
static — it reflects the `(kind, label)` pins an author declared in
`initial_state`, fixed at run initialization. Mid-run pin creation or
renaming and emit-time re-pinning are outside this surface.

### Closed-domain registry

`enum_domains` is the canonical map of allowed values for closed-domain string
properties — both author-declared `status` / `category` typed properties and
the synthesized `<kind>_type` discriminator on sub-typed kinds. It is the
contract carrier downstream tools rely on when their behavior
depends on the enumerated value set rather than the values actually observed
in `records__<kind>` tables. A declared option absent from the live row set is
*still* in `enum_domains`; the registry is intent, not observation.

The block is a nested object `{<kind>: {<property>: [<option>, ...]}}`:

| Condition | Result |
|-----------|--------|
| Scenario declared no closed-domain string properties | `enum_domains` is omitted entirely; an empty `enum_domains: {}` is equivalent but producers omit the key |
| Scenario declared closed-domain string properties | One entry per kind that has ≥ 1 closed-domain property; each per-kind object maps `<property>` to the ordered list of allowed string options |
| Sub-typed kind | Carries an `enum_domains[<kind>][<kind>_type]` entry listing the declared sub-type names; the corresponding `records__<kind>` table carries a populated, never-NULL `prop__<kind>_type` `VARCHAR` column whose values are drawn from this list |

Closed domains are fixed at run initialization and persisted with the run, so
every emit derived from the same persisted run carries the
same registry across `slice_at` choices. Adding
`enum_domains` is a version-compatible extension at `base_format_version: 5`:
a reader that does not recognize the key ignores it (unknown top-level fields
MAY warn but MUST NOT fail). Downstream tools that route per-sub-type read
`enum_domains[<kind>][<kind>_type]` as the authoritative declared key set.

### Record roles

`record_roles` tells a consumer, per emitted kind, how to read its
`records__<kind>` table in warehouse terms — whether a kind is a **dimension**
(a point-in-time entity) or a **fact** (an event). Table shape alone does not
reveal this: a persistent party (a retail `customer`) and a lifecycle-bearing
transaction (an `order`, a ride `trip`, a patient spell) are both physically a
point-in-time SCD-2 `actor`, yet one is a dimension and the other a fact. That
split is a business fact the schema cannot derive, so it is author-declared on
each `actor` sub-type and carried here.

`record_roles` is a nested object keyed by kind; every emitted records-category
kind appears exactly once:

| Kind | Surface | Role |
|---|---|---|
| `actor` | object `{<sub_type>: role}`, one entry per declared actor sub-type | per sub-type, `"dimension"` or `"fact"` (author-declared) |
| `entity`, `resource`, `queue`, `journey_instance` | string | `"dimension"` |
| `tick_decision`, `relationship` | string | `"fact"` |

`actor` is the only kind surfaced as an object because it is the only kind whose
role varies by sub-type — a single `records__actor` table may hold both a
dimension sub-type and a fact sub-type, disambiguated by `prop__actor_type`.
Every other kind has one fixed role uniform across its sub-types. A consumer
reads role as:

| `record_roles[K]` | Read as |
|---|---|
| `K == "actor"` → object | `record_roles["actor"][row.prop__actor_type]`, per row of `records__actor` |
| any other kind → string | the string, for every row of `records__K` |

The interval reading model is conveyed by a table's `category: membership`, not
by `record_roles`: a `membership__<K>__<p>` table is an interval-fact regardless
of the owning kind's role.

Presence and ordering:

| Condition | Result |
|---|---|
| Emit carries ≥ 1 records kind | `record_roles` present |
| Scenario declares ≥ 1 actor sub-type | `record_roles["actor"]` present; absent for an actor-less (entity-only) scenario |
| Keys at every level | sorted lexicographically — the inner `actor` object's sub-type keys included |

Roles are fixed at run initialization and persisted with the run, so every emit
derived from the same persisted run carries the same `record_roles` across
`slice_at` choices. The `actor` object lists **all declared**
actor sub-types, never narrowed to those surviving a slice — this is what keeps
the block slice-stable.

Adding `record_roles` is a version-compatible extension at
`base_format_version: 5`: it is an optional top-level field a reader that does
not recognize it ignores (unknown top-level fields MAY warn but MUST NOT fail).
A generic exporter branches on `record_roles` with no hard-coded kind→role map.

### Column temporal semantics (`history_tracked`, `temporal_class`)

Two paired per-column attributes, peers of `references` on a sidecar column
object. They answer different questions, and a consumer usually wants the second:

| Attribute | Question it answers |
|---|---|
| `history_tracked` | *Is this column type-2 (priors in `history`) or type-1 (current value only)?* — the SCD class, drives time-series vs. static-attribute modelling. |
| `temporal_class` | *Can I ask what this column's value was at time T — and will the answer be true?* — the point-in-time contract. |

`history_tracked` alone is **not** sufficient to answer the second question, which
is why `temporal_class` exists. A type-1 column may be one that never changes (its
current value is valid at every T) or one that changes without a recorded trail
(its past is unknowable). Those are opposite point-in-time contracts and
`history_tracked: false` conflates them. `temporal_class` splits them:

| Value | Meaning | Point-in-time answer for T ≥ `created_sim_time` |
|---|---|---|
| `constant` | Value never changes after creation. | The current value. Valid at every T. |
| `tracked` | Value may change; **every** change is captured in `history`. | An ordered lookup over `history`, or an as-of join in bulk. Exact. |
| `slice_only` | Value may change; changes are **not** captured. | **Unknowable.** Only the value as of the emit's `slice_at` is known. A consumer MUST NOT present a `slice_only` column as an as-of-T value. |

`slice_only` is the honest admission the format previously could not make: it
marks columns whose history is genuinely lost, so a downstream tool refuses the
query instead of silently answering with the slice value.

**Derivation is the producer's, never the author's.** `temporal_class` is derived
from the property's declared schema — whether the property can be modified after
creation at all, and whether it is history-tracked. It is not author-declarable,
so a scenario cannot lie into the sidecar. The derivation is conservative in the
honest direction: it may under-claim `constant`, never over-claim it.

**Pairing (structural).** A column carries `history_tracked` **iff** it carries
`temporal_class`. The two attributes have exactly the same column population, and
they constrain each other: `tracked` ⇒ `history_tracked: true`; `slice_only` ⇒
`history_tracked: false`. (`constant` admits either — a constant column that is
also history-tracked holds exactly its genesis row.) C13 checks this.

**Which columns carry the pair.** Only *value-carrying* columns — those rendering
a record property's value or a presentation value:

| Column class | `history_tracked` | `temporal_class` |
|---|---|---|
| Record `prop__<name>` column | the property's declared SCD class | derived (see above) |
| Presentation-property column (incl. each sub-pick `prop__<name>_<key>`) | `true` | `tracked` if the property's bound source column — directly, or transitively through an earlier sibling it reads — is `tracked`; otherwise `constant`. Never `slice_only`. |
| `presentation_id` | omitted | omitted |
| Structural column (`record_id`, `fork_path`, `active`, lifecycle timestamps) | omitted | omitted |
| Fixed-table column (`history`) | omitted | omitted |
| Membership-table column | omitted | omitted |

`presentation_id` is an identity mint, minted once per record and never
re-minted; it carries neither attribute (an identity column has no temporal
semantics). A presentation property bound to a `tracked` source is itself
`tracked` — it is re-minted at each change instant of its source, and those mints
are appended to the `history` table.

**Coverage.** A `base_format_version: 5` emit carries both attributes on every
records-category `prop__<name>` column, and on every presentation-property column.

**All-or-none across an emit's `prop__` columns.** A producer that emits column
temporal information emits it on *every* `prop__<name>` column of *every*
records-category table, never on some and not others. This makes "attributes
absent" a per-emit signal — the producer predates them — rather than a per-column
ambiguity. C11 and C13 both key their skip guard on it.

**Run-level stability.** Both attributes are fixed at schema assembly, so every
emit derived from the same persisted run carries the same pair for a given column
across `slice_at` choices — matching how `enum_domains` and `pinned_ids` are
run-level.

**Reader contract.** A reader gating on `base_format_version: 5` reads
`temporal_class` directly. On a v4 emit the attribute is **absent and the class is
unknown**; the reader falls back to `history_tracked` inference and inherits its
false-negative tail — and cannot distinguish `constant` from `slice_only` at all,
which is precisely the ambiguity the bump resolves.

Read both from the sidecar: neither is recoverable from raw scenario YAML,
because the bits are partly synthesized at schema assembly and recovering them
would require re-running that assembly.

### What the sidecar does *not* carry

- **Schema fingerprint of the producing scenario.** Lives upstream in the producer's run metadata, not the sidecar.
- **Emit discovery.** A separate index is the surface for *discovering* which emits a distribution contains; `base.json` is self-sufficient for *interpreting* a known emit.
- **Any source-of-truth data.** The sidecar describes what the DuckDB contains; it does not duplicate values that live in the DuckDB itself.

---

## Recommended type mapping (non-binding)

For `prop__<name>` columns and any other place a producer maps a Python value into a DuckDB column. Producers SHOULD follow this mapping; producers MAY deviate if they preserve round-trip equality on the value set actually persisted.

| Property Python type | DuckDB type | Encoding rule |
|---|---|---|
| `int` | `BIGINT` | identity (Python int → DuckDB BIGINT) |
| `float` | `DOUBLE` | identity |
| `bool` | `BOOLEAN` | identity |
| `str` | `VARCHAR` | identity |
| `bytes` | `BLOB` | identity |
| `tuple` | `VARCHAR` | `repr(value)` (round-trip via `ast.literal_eval` on read) |
| `tuple` with `references` annotation | `VARCHAR` | `value[1]` only (id portion); kind lives in the sidecar's `references` field |
| `frozenset` | `VARCHAR` | `repr(value)` |
| `NoneType` | `VARCHAR` | only NULL is ever stored |

**Collection-struct `tuple` properties are excluded from this mapping.** A `tuple`-typed property that declares an element schema is not mapped to a column at all — it is materialized as a `membership__<kind>__<property>` table (§ Membership-category tables) and appears in neither `records__K` nor `history`. The `tuple` rows above apply only to plain, non-collection-struct `tuple` properties.

**Why the recommendation is non-binding.** A future producer might prefer DuckDB's native `INTEGER[]` for `tuple[int, ...]`-typed properties, or `STRUCT(...)` for shaped tuples. As long as the sidecar accurately describes the column type and a consumer can round-trip values, the contract is honored. The current Fabulexa base writer follows this mapping verbatim.

**`history.value` is always VARCHAR.** History stores heterogeneously-typed values across kinds and properties; a single VARCHAR column with codec-text encoding is the only practical shape. The codec used here MUST match the producer's scalar-encoding codec to keep `records.prop__<name>` and `history.value` byte-symmetric for the same logical value (see *Cross-table round-trip*).

### Cross-table round-trip

For any record *r* of kind *K* with history-tracked property *p*, the latest pre-slice `history.value` for `(fork_path, K, r.id, p)` MUST round-trip to the same Python value as `records__K.prop__p` for `(fork_path, r.id)`. Producers using a mapping that preserves this invariant are conformant.

---

## Conformance procedure

A base-layer emit is conformant iff all of the following hold. The procedure is implementable in any language; pseudocode is illustrative.

### C1. Sidecar validates against the JSON Schema

```
load base.json
validate against base-format.schema.json (the schema matching base_format_version)
```

### C2. DuckDB catalog matches the sidecar

```
open run.duckdb
for each table T in DuckDB's information_schema.tables:
    require: T.name appears in base.json[tables][*].name
    require: information_schema.columns(T) order and types match base.json[tables][T].columns
for each table entry E in base.json[tables]:
    require: a corresponding DuckDB table exists
    require: SELECT count(*) FROM E.name == E.rows
```

### C3. Required tables present

```
require: a fixed-category table named "history" exists
require: every records-category table has name == "records__" + record_kind
require: every membership-category table has
         name == "membership__" + record_kind + "__" + property
```

### C4. Required column lists for fixed tables

```
history columns 1..6 match the spec exactly
```

### C5. Required column prefix for records-category tables

```
for each records__K:
    columns 1..2 match (fork_path, record_id) per the spec
    if column 3 is named presentation_id:
        it is the presentation surrogate-identity column (scalar; type per the
            sidecar, not pinned); the lifecycle prefix follows at columns 4..7
    else:
        the lifecycle prefix follows at columns 3..6
    the lifecycle prefix matches
        (created_sim_time, active, deactivated_at, last_mutation_sim_time)
        per the spec, at its (possibly shifted) positions
    the next P columns are prop__<name> for the P scalar (non-collection-struct)
        schema-declared properties of K, in declaration order; a collection-struct
        property is skipped — it has no prop__ column
```

`presentation_id` is permitted only at column 3 (immediately after `record_id`).
Anywhere else it displaces a pinned lifecycle column or violates the `prop__`
block, and C5 fails — preserving the strictness of the positional check while
widening exactly one optional slot.

### C6. Cross-table round-trip on history-tracked properties

```
for a sample of (fork_path, K, record_id, property) entries:
    let v_history = decoded latest pre-slice value from history.value
    let v_record  = records__K.prop__property at the same row
    require: v_history == v_record
```

C6 is sample-based by design; exhaustive checking is the consumer's choice.

C6 iterates `(fork_path, K, record_id, property)` entries drawn from `history`. A collection-struct property emits no `history` rows, so it is absent from C6's input set — a membership table is the sole representation of its property and has no second representation to round-trip against.

### C7. NULL all-or-none on column groups

```
records__K.deactivated_at: NULL iff records__K.active == TRUE
membership__K__p.(member__<f>__kind, member__<f>__id), per reference field f:
    all NULL together or all non-NULL
```

`left_sim_time` and any nullable `elem__<f>` column are individually nullable, not column groups; C7 does not constrain them — their NULL semantics are fixed by § Membership-category tables.

### C8. Branch enumeration matches table data

```
let table_paths = { distinct fork_path across every table in run.duckdb }
let sidecar_paths = { branches[*].fork_path }
require: table_paths == sidecar_paths
```

A sanitised emit carries exactly one branch: `branches` has one entry and every row in every table carries that one `fork_path`. The check reduces to: the sole `fork_path` value present in the data equals the sole `branches[].fork_path`. A branch's `parent` field may name a fork path not present in the emit; C8 constrains `fork_path` values only, not `parent` values.

### C9. Pin surface consistency

```
if base.json[pinned_ids] is present:
    for each (kind, label, id_string) in pinned_ids:
        require: records__<kind> exists in run.duckdb
        for each fork_path present in records__<kind>:
            require: exactly one row has
                     fork_path == <fork_path> AND record_id == <id_string>
```

A no-op when `pinned_ids` is absent. Pin identity is established at run
initialization, so the single branch carries each pinned record: the check is
exactly one row per `(record_id, fork_path)`. C9 is exhaustive over `pinned_ids`
entries — unlike C6, which samples — because the pin surface is run-level
identity metadata bounded by the author's `initial_state` declarations, small
enough that a full pass costs nothing meaningful at any plausible scale.

### C10. Membership table integrity

```
for each membership__K__p and each row:
    require: left_sim_time IS NULL OR left_sim_time >= joined_sim_time
    for each reference field f whose (member__<f>__kind, member__<f>__id)
        pair is non-NULL:
        require: a row exists in records__<member__f__kind> with matching
                 fork_path and record_id == member__f__id
```

C10 resolves references against record *identity*: **any** row for that `record_id` on that `fork_path` satisfies it, regardless of the row's `active` value. A membership interval may outlive the referenced record's active SCD-2 row, so C10 does not require `active == TRUE`.

### C11. Column SCD class consistency

```
if no records-category prop__ column in the sidecar carries history_tracked:
    skip            # producer predates the attribute (additive field)
for each distinct (kind, property) in history (columns 2, 4):
    let col = the prop__<property> column on records__<kind>
    require: col is present in the sidecar with history_tracked == true
for each records__<kind> with >= 1 row:
    for each prop__<property> column flagged history_tracked == true:
        require: history has >= 1 row for (kind, property)
```

A property appears in `history` only if it is type-2, so every `(kind, property)` observed in the history table maps to a `prop__<property>` column the sidecar flags `history_tracked: true`.

C11 is **bidirectional at v5**. The converse clause is new: because the creation seed is unconditional (§ `history` → Creation-seed guarantee), a flagged column on a kind with at least one record MUST have history rows. Zero rows is a violation, not a "created NULL, never changed" inference — that carve-out is gone. Collection-struct properties emit membership tables, not history rows, and stay outside C11's input set (mirroring C6).

The skip guard keys on the all-or-none invariant (§ Column temporal semantics): a producer that emits column SCD information emits it on *every* `prop__<name>` column, so "no `prop__` column carries `history_tracked`" is exactly the signal that the emit predates the attribute. C11 is classed with the semantic checks (C6, C7, C10).

### C12. Record-role registry consistency

```
if base.json[record_roles] is absent:
    skip                      # producer predates the attribute (additive)
let emitted_kinds = { t.record_kind for t in tables if t.category == "records" }
for each K in emitted_kinds:
    require: K in record_roles
for each kind K, value V in record_roles:
    if K == "actor":
        require: V is an object; every value in V is in {"dimension","fact"}
        for each distinct v in records__actor.prop__actor_type:
            require: v in V
    else:
        require: V in {"dimension","fact"}
```

`emitted_kinds` ranges over `category == "records"` tables only; `membership` tables are reached via `category`, not `record_roles`, and need no separate clause — every membership kind also has a records table, so its role coverage is transitive through the records loop. Every emitted kind is covered by `record_roles`: a sanitised emit carries no machinery kinds, so each records table has a business role. The `actor` object MAY list more sub-types than appear in `records__actor.prop__actor_type` (it lists every declared sub-type); C12 requires coverage, not exactness. C12 is classed with the semantic checks (C6, C7, C9, C10, C11) and is skipped only when `record_roles` is absent — the additive-field guard, mirroring C11's skip.

### C13. Temporal-class consistency

```
if no records-category prop__ column carries history_tracked:
    skip                      # producer predates the attribute pair (additive)
for each records-category prop__<name> column:
    require: (history_tracked present) == (temporal_class present)
    if present:
        require: temporal_class in {"constant", "tracked", "slice_only"}
        require: temporal_class == "tracked"     implies history_tracked == true
        require: temporal_class == "slice_only"  implies history_tracked == false
for each prop__<property> column with history_tracked == true (any class):
    for a sample of up to 10 records of that kind:
        require: history has a row for (kind, property) at the record's
                 own created_sim_time            # the genesis row
```

The structural clauses enforce the attribute pairing (§ Column temporal semantics). The semantic clause enforces the unconditional creation seed (§ `history`): a `history_tracked: true` column MUST have a genesis row at each record's `created_sim_time`, whatever its class — a `constant`-class tracked column holds exactly that one row. Sampling mirrors C6's regime.

C13 reads only `records__<kind>`, `history`, and the sidecar, so it applies **in full to a sanitised emit** — nothing in it is narrowed for this subset. Collection-struct properties emit membership tables rather than history rows and stay outside C13's input set (mirroring C6 and C11).

A reference Python conformance check ships in `tools/check_published_conformance.py` implementing C1–C13 against any `(emit_dir,)` argument. It checks the sanitised subset described by this document — the clauses here, not the wider set a mechanism-carrying emit answers to. Implementations in other languages that pass C1–C13 are equally conformant.

---

## Format versioning

| Field | Lives on | Bumps when |
|---|---|---|
| `base_format_version` | `base.json` | Required tables change, fixed-table column lists change, sidecar schema changes |

**Current version = 5.** This document defines v5. A version bump implies one of:
- The required-tables set changed (added/removed/renamed tables)
- A fixed-table required-column list changed
- The sidecar schema gained a new *required* top-level field
- A change to an existing field's semantics
- Any other on-disk-shape change a prior-version reader cannot handle correctly

The `branches` field is the new *required* top-level field that forced the `1 → 2` bump; `runtime` shipped under the same bump as an optional field.

The `2 → 3` bump is forced by the `membership` table category: a new table category, the removal of `prop__<name>` columns for collection-struct properties, the new required `property` sidecar field on membership entries, and the amended conformance procedure (C3/C5/C6/C7 and the new C10) are each changes a v2 reader cannot interpret correctly.

The `3 → 4` bump is forced by the `created_sim_time` lifecycle column inserted at position 3 of the fixed prefix of every `records__<kind>` table: a records-prefix column-list change that shifts `active`, `deactivated_at`, `last_mutation_sim_time`, and the entire `prop__` block down one position, plus the amended C5 (the lifecycle prefix is now four columns) — a v3 reader keying on the prior positions cannot interpret a v4 table correctly.

Adding a *new optional* column group is **not** a version bump as long as prior-version readers continue to read prior-version sidecars correctly — column presence is already self-describing.

The same rule applies to **new optional top-level sidecar fields** (the `record_roles` registry, a pin-identity surface, a future cross-emit linkage block). Their presence is self-describing — a reader gating on `base_format_version` ignores unknown top-level fields per § Field semantics ("MAY warn but MUST NOT fail"). Adding such a field is a version-compatible extension, not a bump. A bump is required only when a prior-version reader could mis-interpret the sidecar.

It applies equally to a **new optional attribute on a column object** (`references`, `history_tracked`): presence is self-describing, the column object's `required` set (`["name", "type"]`) is unaffected, and a prior-version reader ignores the attribute. Adding one is a version-compatible extension, not a bump.

The `4 → 5` bump is therefore **not** forced by the `temporal_class` column attribute — under the rule above that attribute alone would have been version-compatible. It is forced by the **strengthened normative guarantee** that ships with it: the creation seed became unconditional, so a consumer must be able to distinguish

- **v4** — zero `history` rows on a type-2 column *may* mean "created NULL, never changed" (the old carve-out), from
- **v5** — zero `history` rows on a type-2 column of an extant record is a **violation** (C11's new converse clause).

A v4 reader applying v4's inference to a v5 emit is not wrong-but-safe; a v5 reader applying v5's guarantee to a v4 emit will report false violations. The two readings are incompatible, which is what a bump is for.

**Absent-field semantics for v4 emits:** temporal class is *unknown*. A reader falls back to `history_tracked` inference and inherits its documented false-negative tail.

A reader MUST gate on `base_format_version` and refuse to interpret an unknown version. No auto-upgrade.

The format version is independent of any producer or packaging version — it evolves on its own cadence.

---

## Reading an emit

A base-layer emit is a directory holding exactly two files: `run.duckdb` and its `base.json` sidecar. The pair is self-sufficient — a consumer needs nothing else to interpret the emit.

A consumer's read path is:
1. Open `base.json` to learn what tables and columns exist.
2. Open `run.duckdb` and query.

`base.json` is the authority on per-table contents; `run.duckdb` carries the data.

---

## Related

| Document | Why |
|---|---|
| [`base-format.schema.json`](base-format.schema.json) | JSON Schema for `base.json` (machine-readable contract). |
