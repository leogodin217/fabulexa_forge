# Base Reader

**Status:** Implemented. Code is the contract — see
[`emit.py`](../../src/fabulexa_forge/reader/emit.py),
[`sidecar.py`](../../src/fabulexa_forge/reader/sidecar.py),
[`errors.py`](../../src/fabulexa_forge/reader/errors.py), and
[`tests/reader/`](../../tests/reader/). Public API:
[`reader/__init__.py`](../../src/fabulexa_forge/reader/__init__.py).

The foundation every exporter and corrupter reads through. `open_emit(emit_dir)`
opens a base-layer emit (`run.duckdb` + `base.json`), version-gates it to the
supported `base_format_version`, parses the sidecar into typed handles, and opens the single
sanctioned read-only query surface over `run.duckdb`. It depends on nothing outside
the vendored [`contract/`](../../contract/base-format.md) — that is the only
coupling. Conformance assessment (C1–C14) is a separate surface that reads through
this one — see [`conformance.md`](conformance.md).

```
open_emit(emit_dir)
   ├─ base.json  ─► JSON parse ─► version gate (== SUPPORTED_BASE_FORMAT_VERSION) ─► structural floor ─► Sidecar
   └─ run.duckdb ─► read-only DuckDB connection
                                                        └─► Emit (Sidecar + Emit.query)
```

---

## Surface

| Module | Owns |
|---|---|
| [`emit.py`](../../src/fabulexa_forge/reader/emit.py) | `open_emit`, the `Emit` handle (`query` row-tuples, `query_arrow` columnar, `close`, context manager), the read-only DuckDB open |
| [`sidecar.py`](../../src/fabulexa_forge/reader/sidecar.py) | `Sidecar` and its frozen descriptors `ColumnSpec` / `TableSpec` / `BranchEntry` / `RuntimeAnchor`; the typed `RecordRoles` registry view + `Sidecar.record_roles()`; the typed `PresentationKeys` registry view (`KeySpace` / `PartitionKey` / `WholeColumnClaim`) + `Sidecar.presentation_keys()` and the union-safety algebra (`union_safe`, `combined_claim`); the per-column temporal pair — the `history_tracked` flag + `history_tracked_available()`, the `TemporalClass` literal, and the `Sidecar.temporal_class()` accessor (the single narrowing point); the version gate + structural floor (`Sidecar.from_raw`) |
| [`records_columns.py`](../../src/fabulexa_forge/reader/records_columns.py) | The records-column taxonomy — `records_column_role`, `ref_index_sibling`, `REF_INDEX_PREFIX`: the one classifier every records-column consumer reads through (§ The records-column taxonomy). The structural-temporal surface — `StructuralInstant`, `structural_instant_columns`, `records_structural_column_is_mutable`: the one answer to which structural columns carry a sim-time instant and which may change after creation (§ The structural-temporal surface) |
| [`relations.py`](../../src/fabulexa_forge/reader/relations.py) | The faithful-read SQL builders (`build_records_relation_sql`, `build_history_relation_sql`, `build_membership_relation_sql`) and the faithful introspection helper `distinct_prop_values` — the reader's compose-time surface, the sole faithful namer of base tables |
| [`errors.py`](../../src/fabulexa_forge/reader/errors.py) | The reader error hierarchy — operational/structural failures only |

## Boundary

- **Input.** A directory holding `run.duckdb` + `base.json` at the supported
  `base_format_version`. Extra entries in the directory are ignored — the gate checks the two required
  artifacts are present, not that they are the directory's only contents (an emit may
  sit inside a bundle alongside sibling files).
- **Output.** An open `Emit`: a typed `Sidecar` plus a read-only DuckDB connection.
  The caller closes it via `Emit.close()` or a `with` block.
- **Forbidden imports.** No dependency on the bundle's producer. The vendored
  `contract/` is the only coupling.
- **Non-mutation.** `run.duckdb` is opened read-only and `base.json` is never
  written. Reading an emit mutates nothing on disk.
- **Session-zone pin.** The one piece of mutable connection state the reader
  owns: an invocation-scoped time-zone pin on the open connection, set by the
  anchor-resolving caller, never by a mode or writer (§ The session-zone
  pin). It is in-memory connection state, not a disk write — it does not
  breach non-mutation.

## Semantics

### Opening is light: gate, then structural floor — not conformance

`open_emit` performs the version gate and a structural parse only; it does **not**
run conformance. The split is load-bearing: a non-conformant emit must still *open*
so that `validate` can report which check it fails. A sidecar can open successfully
and still be non-conformant — an empty `columns` array or a phantom `prop__` column
opens cleanly and is diagnosed by C1/C2/C5. This is the room the negative fixtures
need.

A table's `category` is the one *value* the floor admits against a closed set
rather than deferring to conformance (§ The structural-temporal surface).

The open sequence and what each step rejects is the [Validation Rules](#validation-rules)
table below. Two ordering guarantees are normative:

- **The version gate precedes the structural parse.** A future-version sidecar may
  carry new required fields the structural parse would choke on; gating first
  guarantees the error a human sees is "unsupported version N", never an opaque
  structural complaint about a field that only exists in a version the reader does
  not support.
- **A malformed `base_format_version` is a structure error, not a version error.**
  `UnsupportedBaseFormatVersionError` is reserved for a well-formed integer version
  the reader does not support; an absent or non-integer version is a malformed
  sidecar (`SidecarStructureError`). "Integer" is the strict test
  `isinstance(v, int) and not isinstance(v, bool)` — a JSON float (`5.0`), a boolean,
  a string, or `null` all route to `SidecarStructureError`. `found_version` is
  therefore always a genuine `int`.

The structural parse has a **floor**: the keys without which a frozen descriptor
cannot be constructed must be present and correctly typed — the required,
non-defaulted fields of `TableSpec` (`name`, `category`, `columns`, `rows`),
`ColumnSpec` (`name`, `type`), and `BranchEntry` (`fork_path`, `parent`,
`slice_at`). Present-but-schema-invalid sits *above* the floor and opens (so C1
diagnoses it); below the floor raises before conformance runs. `parent` is the one
floor field whose value may be `null` — a root branch carries the key with value
`null` (→ `None`); an *absent* `parent` key is below the floor and raises. The floor
does not enforce schema patterns, enums, `const`, `minItems`, or conditional-required
rules — those are C1's job. In particular the category↔`record_kind`/`property`
correspondence is conditional-required, so the floor populates those fields by
presence alone (absent → `None`) and leaves the correspondence to C1 (schema) and C3
(name composition).

### The sidecar drives all discovery

No column or table list is hard-coded from `contract/base-format.md`. Every consumer
learns what exists by reading the `Sidecar`:

| Question | Answered by |
|---|---|
| Which tables exist? | `Sidecar.tables()` (DuckDB-catalog order) |
| What columns, in what order, what DuckDB type? | `TableSpec.columns` → `ColumnSpec.{name,type}` |
| Is a column a foreign key, and to which kind? | `ColumnSpec.references` (the FK target kind, or `None`) |
| What record kind does a `records__*` / `membership__*` table carry? | `TableSpec.record_kind` |
| What collection-struct property does a `membership__*` table project? | `TableSpec.property` |
| Which branches, and where sliced? | `Sidecar.branches()` → `BranchEntry.{fork_path,parent,slice_at}` |
| Wallclock anchor for `sim_time`? | `Sidecar.runtime()` (`None` when no `runtime:` block) |
| Author label → minted record id? | `Sidecar.pinned_ids()` (`{kind: {label: id}}`; empty when absent) |
| Allowed values for a closed-domain property? | `Sidecar.enum_domains()` (`{kind: {prop: (opt,…)}}`; empty when absent) |
| Is a kind sub-typed, and into which sub-types? | `Sidecar.subtype_values(kind)` (the `<kind>_type` domain in declaration order; `()` when not sub-typed) |
| What is a column's point-in-time class? | `Sidecar.temporal_class(table, column)` — the single narrowing point; raises rather than infers (§ Per-column temporal semantics) |

Field shapes of the descriptors and accessors are the dataclass and method
definitions in [`sidecar.py`](../../src/fabulexa_forge/reader/sidecar.py) — that is
their authoritative statement, not a restated table here.

### The records-column taxonomy

Every records-category column classifies through one pure, context-free
classifier — `records_column_role` in
[`records_columns.py`](../../src/fabulexa_forge/reader/records_columns.py) —
by name family alone (no sidecar lookup, no table state):

| Column name | Role |
|---|---|
| `fork_path`, `record_id`, `record_index` | `identity` |
| `ref_index__<name>` (prefix match) | `identity` |
| `presentation_id` | `presentation` |
| `created_sim_time`, `active`, `deactivated_at`, `last_mutation_sim_time` | `lifecycle` |
| `prop__<name>` (prefix match) | `payload` |
| anything else | **no role** (`None`) |

*No role* is a first-class outcome every caller treats loudly: conformance
records a C5 failure ([`conformance.md`](conformance.md) § C5 — the records
layout); an exporter raises a named validation error ([`source.md`](source.md)
§ Validation Rules). No caller may skip, drop, or pass through an unclassified
column. This is the closed-world posture: a new contract column family changes
the taxonomy in one place and turns every unprepared consumer red, instead of
falling through some consumers silently.

The classifier applies to **records-category** tables only. Sibling pairing is
a pure name rule — `ref_index_sibling` maps `prop__<name>` to
`ref_index__<name>`; whether a given `prop__` column *has* a sibling on a given
table is determined by its own sidecar `references` field (annotation present
⇒ sibling required — C5 enforces). Identity columns carry no temporal
attributes and no `references` annotation of their own (see
[`bundle.md`](bundle.md) § The dense record index); `ColumnSpec`'s optional
fields already express them by absence. Signatures and the `ValueError`
contract are the definitions in
[`records_columns.py`](../../src/fabulexa_forge/reader/records_columns.py).

### The structural-temporal surface

The sidecar declares every column's name and type, and for `prop__` columns its
temporal pair. It declares nothing about the **structural** columns' temporal
meaning: whether `created_sim_time` carries an instant, whether `deactivated_at`
may change after a record is created. Those facts are pinned by the contract, by
name and position, and are not machine-readable from the emit. The reader owns
them, in the same shape as the records-column taxonomy — pure, name-based, no
sidecar and no DuckDB read, and loud on anything it does not recognise.

The surface answers two questions and no others.

**Which structural columns carry a sim-time instant, and which instant each
names.** The vocabulary is closed and derives from the contract's column
definitions:

| Category | Column | Instant | Nullable |
|---|---|---|---|
| `records` | `created_sim_time` | `created` | no |
| `records` | `deactivated_at` | `closed` | yes |
| `records` | `last_mutation_sim_time` | `last_touched` | no |
| `fixed` | `sim_time` | `changed` | no |
| `membership` | `joined_sim_time` | `joined` | no |
| `membership` | `left_sim_time` | `left` | yes |

A structural column absent from this table carries no instant. A `prop__` column
may hold a time-valued payload, but that is a declared property with a sidecar
type, not a structural instant, and is outside the surface.

**Which records structural columns may change after the record is created** —
the fact the incremental export needs to decide whether a column is safe to read
once and treat as settled. `active`, `deactivated_at`, and
`last_mutation_sim_time` may change; `created_sim_time`, `fork_path`,
`record_id`, `record_index`, and `presentation_id` are set once.

The mutability domain is closed, and covers the structural half only. A
`prop__<name>` column's mutability is a sidecar question answered by its temporal
pair (§ Per-column temporal semantics); a `ref_index__<name>` column tracks its
sibling `prop__<name>` and resolves through the sibling's sidecar answer. "Structural"
is defined against the taxonomy's families: `identity`, `presentation`, and
`lifecycle`, minus the ref-index prefix. Because the taxonomy classifies
`ref_index__<name>` as `identity`, family alone does not isolate it, so a caller
needing both halves dispatches on family *plus* the ref-index prefix rule —
`payload` to the sidecar, an `identity` name matching the prefix to its sibling's
sidecar answer, everything else in `identity` / `presentation` / `lifecycle` to
this surface. Neither half subsumes the other.

The two loud conditions differ in kind, and so do their signals:

- **An unrecognised table category raises.** The category set is closed,
  contract-pinned, and admitted when the sidecar is read, so a value outside it is
  a programming error rather than emit data.
- **A structural name outside the pinned records set raises** when asked for
  mutability. The open name space is the taxonomy's to classify and the caller
  dispatches through it first. A `prop__` or `ref_index__` question belongs to the
  sidecar; a name the contract does not pin has no mutability answer at all, so a
  quiet "immutable" would state a fact the contract does not hold.
- **A structural column carrying no instant is an ordinary answer**, not a raise.
  The column-name space is open — every `prop__<name>` lives in it — so absence is
  a result the caller interprets.

The instant vocabulary is presentation-free: no output name appears in it. Which
real-world name an instant takes in an output is each mode's presentation policy —
source renders operational names, base keeps structural names under its own
minimal default, dimensional is author-verbatim. One set of contract facts carries
three naming policies.

Signatures, the `ValueError` contracts, and the pinned mappings are the
definitions in
[`records_columns.py`](../../src/fabulexa_forge/reader/records_columns.py).

### The record-role registry overlays roles on discovered kinds

`Sidecar.record_roles()` exposes the optional `record_roles` registry as a typed
`RecordRoles` view, or `None` when `base.json` omits it (an emit predating the
registry — absence is "role unknown", not an error). The registry is read-only
warehouse-role metadata a consumer overlays on the records tables it has already
discovered from `tables()`; it never participates in table or column discovery.

The registry is keyed by kind, and the read rule is asymmetric. `actor` is the one
kind whose warehouse role varies by sub-type, so it maps to a `{sub_type: role}`
object; every other kind maps to a bare role string (`"dimension"` or `"fact"`).
Other kinds *have* sub-types (`entity_type`, etc.) but their role does not vary by
them, so the contract collapses them to a single string — the reader resolves what
the producer emitted and synthesizes no object entries it did not. `RecordRoles` owns
this asymmetric read rule so no downstream module re-derives the object-vs-string
branch (reader-first): `is_subtyped(kind)` reports whether a caller must read the
row's `prop__<kind>_type` discriminator first, and `role_of(kind, sub_type)` resolves
the role. An `actor` object MAY declare more sub-types than appear in data; the
resolver answers for any declared sub-type.

Consumers reach the registry through `emit.sidecar.record_roles()` — there is no
`Emit` passthrough — the same way they reach `branches()`, `runtime()`, and
`tables()`. The method signatures and their `KeyError`/`ValueError` contract are the
definitions in [`sidecar.py`](../../src/fabulexa_forge/reader/sidecar.py).

### The discriminator oracle answers sub-typed-ness

`Sidecar.subtype_values(kind)` is the canonical answer to "is this kind sub-typed,
and into which sub-types does it split?" It returns the declared `<kind>_type`
discriminator values from `enum_domains[kind]["<kind>_type"]` in declaration order,
or `()` when the kind carries no such domain. A kind is sub-typed — splits into one
topic per sub-type downstream — iff the tuple is non-empty.

The oracle reads intent, not observation: a declared sub-type is returned even when
a slice materialises zero rows for it, so the sub-type set is stable across
`slice_at`. `enum_domains[kind]["<kind>_type"]` is the contract's authoritative
declared key set for routing per sub-type, and `subtype_values` is the one accessor
that owns the `<kind>_type` naming convention, so no consumer re-derives it.

Sub-typed-ness is independent of `record_roles`. A kind's warehouse role (the role
registry above) and its sub-typed-ness (this oracle) are orthogonal axes: `entity`
carries a bare `"dimension"` role yet is sub-typed when it declares an `entity_type`
domain. The role registry answers "does the warehouse role vary by sub-type?" — true
only for `actor`; the discriminator oracle answers "does the kind split at all?" The
two accessors keep the axes apart so no downstream module conflates them.

The accessor is total — it never raises, returning `()` alike for a kind with no
`<kind>_type` entry, an absent `enum_domains`, and an unknown kind. The first two are
genuine not-sub-typed verdicts; the unknown-kind case is a convenience, since a
caller needing unknown-kind diagnostics resolves the kind first (e.g. streaming's
`StreamKindResolvable`, which requires `records__<kind>`) rather than reading a
misleading `()` here. Totality — against the `KeyError`-raising `RecordRoles`
accessors — suits a caller that asks for every selected kind, most of which are
legitimately not sub-typed. The signature is the definition in
[`sidecar.py`](../../src/fabulexa_forge/reader/sidecar.py).

### The presentation-keys registry is strict on read

`Sidecar.presentation_keys()` exposes the optional `presentation_keys` block —
per kind, per minting declaration, the statically-derived key properties of the
kind's `presentation_id` column (`unique_within` scope, `branch_stable` /
`slice_stable`, and a `key_space` identity), plus a kind-level rollup for
partitioned kinds — as a typed `PresentationKeys` view, or `None` when the
sidecar carries no `presentation_keys` key ("no claims", the same absence
posture as the sibling registries). The view is a verbatim carry: nothing is
inferred, nothing re-derived from data, entry order is the sidecar's
(contract-guaranteed lexicographic). A partitioned kind's declared sub-type set
is never narrowed to sub-types with surviving rows (slice-stable, matching
`sub_type_columns`).

Unlike the sibling registries, the parse is **strict**: construction (lazy, on
first call) verifies the block's normative consistency rules, and a present but
incoherent block raises `PresentationKeysInvalidError` rather than yielding
claims a consumer would key on. Each clause names the kind (and sub-type) that
violates it:

| Clause | Rule |
|---|---|
| Kind membership (both directions) | A kind appears in the block **iff** its declared `records__<kind>` table carries a `presentation_id` column |
| Entry shape | `sub_types` entry iff the kind carries a synthesized `<kind>_type` discriminator domain in `enum_domains`; `key` entry iff not |
| Sub-type domain | `sub_types` keys ⊆ the kind's discriminator domain |
| Scalar–key-space coupling | `unique_within` and the stability pair equal the values `key_space.class` determines (`counter` → `emit`/`false`/`false`; every other class → `branch`/`true`/`true`) |
| Key-space shape | `prefix`/`width` present iff the class is digit-rendered (`counter`/`record_index`) |
| Rollup consistency | A partitioned kind's rollup equals `combined_claim` over its entries (including an omitted `unique_within` exactly when the algebra derives no claim) |

An error is raised only when the block is *present and incoherent*; absence
never raises. Because construction is lazy, a defective block surfaces exactly
on the paths that consult claims — the `declare_keys` capability and dimensional
`init` ([`declared-keys.md`](declared-keys.md)) — never on an export that
ignores them.

Beside the view, `union_safe` and `combined_claim` implement the contract's
normative union-safety algebra verbatim — pairwise safety over `key_space`
identities (identical-`prefix`/`width` `record_index` pairs safe; `uuid`×`uuid`
safe; `record_id`×`record_id` safe; digit-rendered pairs safe iff prefixes
incomparable, where P₁, P₂ are comparable iff one equals the other plus a
possibly-empty digit string; every cross-family pair unsafe) and the
combined-set derivation (all-counter → `emit`/`false`/`false`; all-stable →
`branch`/`true`/`true`; mixed → `branch`/`false`/`false`; any-pair-unsafe → no
uniqueness claim, stability pair `true`/`true` iff every member stable). They
are kind-scoped, as the contract scopes them; no cross-kind call is meaningful
and none is provided. Their one consumer inside the reader is the
rollup-consistency clause; they are public because they state
contract-normative behavior tests must exercise directly. The method signatures
and their `KeyError`/`ValueError` contract are the definitions in
[`sidecar.py`](../../src/fabulexa_forge/reader/sidecar.py).

### The row census is advisory evidence, never a guarantee

`Sidecar.row_census` exposes the optional `row_census` block as a `BranchCensus`,
or `None` when the emit carries none. It answers volume questions — rows per
table, rows and distinct records per `(kind, property)` history series, rows per
sub-type — and nothing else: the block counts emitted rows and record identities,
never aggregates values. Aggregation over values is a consumer's own work.

The block is keyed by `fork_path`, and the accessor resolves the emit's single
branch (a sanitised emit carries exactly one; C8 asserts it), so no caller passes
a branch it cannot vary. A census keyed by some other branch reads `None` rather
than being reinterpreted as this emit's.

Two properties govern how a consumer may use it. It is **optional**, so every
consumer needs a defined path for `None` — one that says so rather than falling
silent, because silence on an unmeasured emit is indistinguishable from a
measurement that came back fine. And it is **advisory**: no conformance check
ranges over its contents, so a census claim is evidence to present to an author,
never a fact to plan against. That is why the parse is tolerant — a malformed
entry drops that one series and leaves the rest readable, since a block carrying
no checking obligation should not be able to refuse an emit.

The record types are the definitions in
[`sidecar.py`](../../src/fabulexa_forge/reader/sidecar.py); its one consumer today
is dimensional `init`'s versions-per-record evidence ([`dimensional.md`](dimensional.md)).

### Typed `prop__` columns and DuckDB read-back

`records__<kind>.prop__<name>` columns are read directly — there is no JSON blob to
expand (the producer writes one typed column per scalar property). `Emit.query`
returns values exactly as the DuckDB Python client yields them, applying no type
transformation. The codec-relevant types read back as:

| DuckDB column type | Python read-back |
|---|---|
| `BIGINT` | `int` |
| `DOUBLE` | `float` |
| `BOOLEAN` | `bool` |
| `VARCHAR` | `str` |
| `BLOB` | `bytes` |

These five are the codec-relevant types — the ones C6's round-trip handles — not an
exhaustive or enforced mapping. The contract lets a producer pick any DuckDB type so
long as C2 holds (`DECIMAL`, `DATE`, `TIMESTAMP`, `HUGEINT`, …); `Emit.query` returns
whatever the DuckDB client yields for such a column, untransformed.

`VARCHAR` carries four logical shapes that are all already text and therefore
byte-symmetric between `records__K.prop__p` and `history.value`: plain strings;
`repr(tuple)` for plain (non-collection-struct) tuple properties; `repr(frozenset)`;
and the id-string of a references-annotated property (`record_id[1]` only — the kind
lives in `ColumnSpec.references`). A consumer that wants a *decoded* value from
`history.value` (always `VARCHAR`) casts per the prop column's DuckDB type —
`int(text)`, `float(text)`, `text == "true"`, identity, or `ast.literal_eval(text)`
for tuple/frozenset. The reader documents this decode contract but exposes raw
`VARCHAR`; materializing decoded values belongs to the dimensional exporter (see
[Boundaries](#boundaries)). The *encode* direction of the codec is a conformance
concern — see [`conformance.md`](conformance.md) § The codec.

### Two read surfaces: row-tuple and columnar

`Emit` exposes two read-only surfaces over the same encapsulated connection.
`Emit.query` returns materialized row tuples — what conformance and row-wise
consumers need. `Emit.query_arrow` returns a `pyarrow.Table`, the sanctioned
**columnar** surface: the column types are DuckDB's own, so a zero-row result still
carries the typed schema and a `CAST(NULL AS T)` column arrives as a typed all-NULL
column rather than an untyped object column. The dimensional writers materialize each
output table through `query_arrow` — registering a typed Arrow table on a fresh output
DB sidesteps the all-null-object-column `register` failure class. Both surfaces keep
the connection encapsulated; neither hands out a raw connection (`pyarrow` is already a
package dependency, imported lazily like `duckdb`).

### Faithful-read SQL builders

The reader is the **sole faithful namer of base tables**: the compose-time builders in
[`relations.py`](../../src/fabulexa_forge/reader/relations.py) return canonical
`SELECT` strings over a single base table, which a consumer embeds as a subquery and
materializes through `Emit.query` / `Emit.query_arrow`. Each is sidecar-driven, takes
only the `Sidecar` (or `Emit`) and plain values — a `fork_path`, a kind, a property, a
predicate mapping — never a mode's config type, and carries **no `ORDER BY`** (the
consumer's representation step imposes order). They reshape nothing: the rows are the
base-table rows that match.

| Builder | Relation | Filtered to |
|---|---|---|
| `build_records_relation_sql` | `records__<kind>`, full sidecar column list | `fork_path` + a discriminator predicate |
| `build_history_relation_sql` | `history` (the six fixed columns) | `fork_path`, `kind`, `property`, and — when given — `value`, compared as a raw `VARCHAR` literal against `history.value`, never type-coerced, since `history.value` is always `VARCHAR` per contract |
| `build_membership_relation_sql` | `membership__<owner_kind>__<property>`, full column list | `fork_path` + a `where` predicate over `elem__` columns |

Each predicate value is a scalar or a non-empty list of alternatives, compiled to
`=` or `IN` by the one rendering authority
([`row-predicates.md`](row-predicates.md)). The builders own the half the
authority cannot: resolving each predicate column's DuckDB type from the sidecar,
so literals are typed against the column they compare to. A column whose declared
type the shared typed-literal renderer does not recognize — a `BLOB`, a
producer-custom array or struct — is refused with an `ExportError` naming the
type on the records and membership builders, rather than compared as a `VARCHAR`
literal that would quietly match nothing.

A missing table raises `TableNotFoundError`. The faithful introspection helper
`distinct_prop_values(emit, kind, property_name)` *executes* (rather than returning
SQL) the `SELECT DISTINCT prop__<property> FROM records__<kind> WHERE … IS NOT NULL
ORDER BY 1` that the dimensional `init` discriminator fan-out needs — non-`NULL` values
in the source column's native-type order, so a numeric discriminator is not reordered
by a Python string sort. Field shapes and the exact SQL are the function definitions in
[`relations.py`](../../src/fabulexa_forge/reader/relations.py).

These builders are faithful by construction — they name a base table and return its
rows narrowed only by branch and author predicates. A read that narrows by a
reference-graph fact, reconstructs versions, or resolves a reference is *interpretive*
and lives in the derivations layer ([`derivations.md`](derivations.md)), not here.

### Long-form `history` with implicit intervals

`history` is the long-form SCD-2 rendering the contract defines: one row per change
event, `value` always `VARCHAR`. There are **no** `valid_from` / `valid_to` columns —
the validity interval is implicit. A row's value holds over
`[sim_time, next row's sim_time)` within its `(fork_path, kind, record_id, property)`
series; the last row holds through the slice boundary. The canonical derivation of an
explicit `valid_to` is:

```sql
LEAD(sim_time) OVER (
  PARTITION BY fork_path, kind, record_id, property
  ORDER BY sim_time
)
```

well-formed because `sim_time` strictly increases within a series. The reader exposes
long-form `history` through `Emit.query` and documents this derivation; interval
materialization belongs to the dimensional exporter, the first consumer that needs it
(see [Boundaries](#boundaries)). Membership intervals are the parallel case:
`membership__*` rows carry explicit `joined_sim_time` / `left_sim_time`, so no `LEAD`
is needed there; the reader exposes them as-is.

### Per-column temporal semantics: `history_tracked` and `temporal_class`

Every value-carrying records column declares a pair of temporal attributes
(see [`bundle.md`](bundle.md) § Column temporal classes for what the classes mean):

- `ColumnSpec.history_tracked` — the SCD-class flag (C11): `True` for a type-2
  column (priors recoverable from `history`), `False` for type-1 (current value
  only), `None` when the column declares no flag.
  `Sidecar.history_tracked_available()` reports presence by inspecting any column.
- `ColumnSpec.temporal_class` — the column's declared point-in-time class. The field
  is `str | None`, **deliberately not** enum-typed: the sidecar's declared value is
  carried **verbatim**, neither validated nor coerced at parse. The reader reads,
  conformance judges — C13's enum clause must be able to *see* an out-of-enum
  declared value, and `validate` reads through the reader, so a parse-time rejection
  (or a coerce-to-`None`) would hide the very defect `validate` exists to report.

The narrowing from the verbatim string to the typed `TemporalClass` literal
(`"constant" | "tracked" | "slice_only"`) happens in exactly one place:
`Sidecar.temporal_class(table_name, column_name)`. Every surface that needs a class
(the source exporter's audited-set resolution — see [`source.md`](source.md)) resolves
through it, so any holder of a sidecar resolves a class without an open connection.
The accessor raises `TemporalClassUnavailableError` for a column with no usable
class — three cases, distinguished in the message: the column carries neither
temporal attribute (a structural, identity, or membership column — conformant, but
with no temporal semantics to ask about); it declares `history_tracked` but no
`temporal_class` (non-conformant, C13); or it declares a value outside the
three-class enum (non-conformant — C13's enum clause, and C1, since the vendored
schema enum-constrains the value). The non-conformant messages direct the caller to
`fabulexa-forge validate`.

**Never inferred.** A column that declares no class has no class. Deriving a class
from `history_tracked` is precisely the fiction the class attribute exists to
delete — a `history_tracked: false` column may be genuinely constant *or* mutable
with an unknowable past, and only the declared class distinguishes them. A surface
that needs a class refuses rather than guessing.

**No class gate at open.** `open_emit` does not refuse an emit whose `prop__`
columns carry no `temporal_class`, nor one whose declared class is outside the enum;
the sidecar model carries the declared value verbatim. Refusing at open would make
`fabulexa-forge validate` unable to *diagnose* the very emit you would reach for it
with, since validate reads through the reader. The reader reads, conformance judges
(C11, C13), and the modes refuse what they cannot answer honestly.

The dimensional exporter reads the flag to split tracked-vs-static columns purely
from the sidecar; a flag-absent emit is refused at validation rather than
reconstructed by `history`-table inference (see [`dimensional.md`](dimensional.md)
§ SCD-2 wide reconstruction). Signatures and the exact error taxonomy are the
definitions in [`sidecar.py`](../../src/fabulexa_forge/reader/sidecar.py) and
[`errors.py`](../../src/fabulexa_forge/reader/errors.py)
(`TemporalClassUnavailableError`, `ColumnNotFoundError`).

### Determinism and row order

The reader is a pure, read-only function of the emit files (Invariant 1). DuckDB scan
order for an unordered `SELECT` is **not** contractually byte-stable, consistent with
the producer's "binary file determinism not guaranteed". The *logical* order is
pinned by the producer on write (append order, creation order); a consumer that needs
a stable read order specifies `ORDER BY`. The reader adds no nondeterminism and
imposes no implicit ordering.

### The session-zone pin

`pin_session_timezone(emit, anchor)` pins the materialization session's time
zone to the resolved anchor's IANA zone for the invocation. The
anchor-resolving driver — the export driver, and tier-2 shaped playback's
`open` ([`playback.md`](playback.md)) — calls it once, after anchor
resolution and before any relation materializes. The pin is
connection-scoped, so it covers both of the reader's query surfaces
(row-tuple and columnar) for the rest of the invocation; it is a pure
function of the resolved anchor (same anchor → same session state →
byte-identical zone-bearing text forms on any machine); and it is set only
through the reader — no mode or writer touches session state. With no
resolved anchor there is no call: no elected temporal rendering exists
without an anchor ([`temporal-elections.md`](temporal-elections.md) §
Anchor requirement), so no zone-bearing value arises to pin against.

The pin is the mechanism that makes zone-bearing serialization — the
`TIMESTAMP WITH TIME ZONE` election's CSV text form
([`writers.md`](writers.md)) — independent of the executing machine's
locale and session zone: the anchor zone, never the session zone, governs
every zone-bearing text form the writers produce.

## Invariants

1. **Determinism.** The reader is a pure, read-only function of the emit files: the
   same emit yields an identical `Sidecar`, identical `Emit.query` results for
   identical SQL, and an identical `ConformanceReport`. It consumes no RNG, no clock,
   no network. Unordered scans are not byte-stable; a stable order is the consumer's
   `ORDER BY`.
2. **Version-gating.** Any `base_format_version` ≠ `SUPPORTED_BASE_FORMAT_VERSION` is
   refused at `open_emit` with `UnsupportedBaseFormatVersionError`. No auto-upgrade,
   no best-effort read, no dual-version support. The supported version appears as a
   literal exactly once —
   [`fabulexa_forge.SUPPORTED_BASE_FORMAT_VERSION`](../../src/fabulexa_forge/__init__.py)
   — and every other site, the test tree included, imports it (see
   [`README.md`](README.md) § Inputs and fixtures for the fixture-side half of this
   invariant).
3. **Integrity-preservation.** The reader opens `run.duckdb` read-only and never
   writes `base.json`; it fabricates nothing and surfaces exactly what the sidecar
   and DuckDB contain. It is the faithful foundation on which the exporter "reshape,
   never fabricate" invariant rests.
4. **Sidecar-authoritative.** Table and column discovery flow exclusively from the
   sidecar. The reader hard-codes no column list from the spec for discovery; the
   only restated spec column lists live in the conformance checks, used solely to
   *validate* (see [`conformance.md`](conformance.md)).
5. **Total records-column classification.** Every records-category column
   classifies through the one taxonomy (`records_column_role`), and *no role*
   is loud everywhere: a recorded failure in conformance, a raised error in
   export planning. No consumer of records columns falls through on an unknown
   name.
6. **Sub-typed-ness reads from the discriminator domain alone.** `subtype_values`
   derives a kind's sub-type split from `enum_domains[kind]["<kind>_type"]` and
   nothing else; a kind's `record_roles` warehouse role never affects whether it is
   sub-typed. The accessor is total — `()` is the not-sub-typed verdict, never an
   exception.
7. **One owner of the structural-temporal facts.** Exactly one module answers
   "does this structural column carry an instant, and which one" and "may this
   records structural column change after creation". A mode that needs either
   answer reads it from the reader; a mode holding a private copy is a defect.
8. **The instant vocabulary is presentation-free.** No output name appears in it;
   naming belongs to the mode that renders the column.
9. **Only the contract's three table categories are admitted.** An unrecognised
   `category` is refused when the sidecar is read, so no consumer ever sees one —
   which is what makes an unrecognised category at the structural-temporal surface
   a caller error rather than emit data.
10. **Presentation-key claims are verbatim and coherent-or-refused.** The
    `PresentationKeys` view carries the block exactly as the sidecar declares it —
    nothing inferred, nothing narrowed to surviving rows — and an incoherent
    present block raises `PresentationKeysInvalidError` at the accessor rather
    than yielding a silently-mended view. Absence is "no claims", never an error.
11. **The session-zone pin is the reader's alone to set.** Only the
    anchor-resolving caller pins the connection's time zone, and only through
    the reader; no mode or writer touches session state. The pin is a pure
    function of the resolved anchor.

## Validation Rules

`open_emit` rejects at open time, in this order:

| Step | Condition | Result |
|---|---|---|
| 1. Locate | `emit_dir`, `run.duckdb`, or `base.json` missing | `EmitNotFoundError` |
| 2. JSON parse | `base.json` is not valid JSON | `SidecarParseError` |
| 3. Version gate | `base_format_version` is a present integer ≠ `SUPPORTED_BASE_FORMAT_VERSION` | `UnsupportedBaseFormatVersionError(found_version=…)` — no auto-upgrade |
| 4. Structural floor | `base_format_version` absent or non-integer; or a required floor field absent/mis-typed (branches a non-empty list; tables a list; each table has `name`/`category`/`columns`/`rows`; each column object has `name`/`type`; each branch has `fork_path`/`parent`/`slice_at`, `parent` present and `str` or `null`); or a table's `category` is outside `{fixed, records, membership}` | `SidecarStructureError` |
| 5. Open DuckDB | `run.duckdb` present but not a readable DuckDB database | `RunDatabaseError` |
| else | all of the above pass | an open `Emit` |

The reader error hierarchy is operational/structural only; the definitions and the
`found_version` payload live in
[`errors.py`](../../src/fabulexa_forge/reader/errors.py). Conformance *failures* are
never reader errors — they are failing `CheckResult`s (see
[`conformance.md`](conformance.md)).

One rule runs after open time: `Sidecar.presentation_keys()` verifies the block's
six coherence clauses lazily, on first call, raising `PresentationKeysInvalidError`
(a `ReaderError`) naming the kind, sub-type, and violated clause (§ The
presentation-keys registry is strict on read). A defective block therefore fails
the claim-consuming call, never the open.

## Rationale

- **Open-light, validate-heavy.** Coupling "open" to "conform" would make a malformed
  emit un-openable, so `validate` could never *report* what is wrong — it could only
  fail to construct. Splitting them lets the negative fixtures open and be diagnosed,
  and lets a caller open a known-good emit without paying for a full conformance pass
  every time.
- **Version-gate before structural parse.** A newer-version sidecar may add required
  fields the current parse rejects. Gating first guarantees the human-facing error is
  "unsupported version N", the actionable message, not an opaque structural complaint
  about a field that exists only in an unsupported version.
- **The class is carried verbatim and narrowed in one place.** Typing
  `ColumnSpec.temporal_class` to the enum — or rejecting an out-of-enum value at
  parse — would make a C13-non-conformant emit unreadable, and `validate` reads
  through the reader. Carrying the declared string verbatim and narrowing it only in
  `Sidecar.temporal_class()` keeps diagnosis possible while giving every consumer a
  single typed resolution point with a uniform refusal
  (`TemporalClassUnavailableError`) instead of scattered `history_tracked`-based
  guesses.
- **One query primitive, not a query builder.** The dimensional and source exporters
  need real SQL — `LEAD` window functions for implicit intervals, equality joins on
  references. A constrained builder would be torn down immediately. `Emit.query`
  keeps the connection encapsulated (the reader is the sole opener) while admitting
  the SQL those exporters require; the reader-first discipline is enforced by
  convention (names derive from the sidecar), not by sandboxing. Identifier quoting on
  interpolated names lives in the conformance checks (see [`conformance.md`](conformance.md)
  § Identifiers vs values).
- **Columnar reads are a reader method, not a writer's raw connection.** The DuckDB
  writer's Arrow path needs typed Arrow tables from the input, and reader-first forbids
  a writer opening `run.duckdb` itself; `Emit` deliberately exposes no raw-connection
  accessor. So columnar input reads are `Emit.query_arrow`, alongside the row-tuple
  `query`, while output writing is writer-side. Keeping Arrow purely writer-side is
  impossible under reader-first — the writer would have no sanctioned way to read the
  input columnar.
- **One owner for facts the sidecar does not carry.** The contract's structural
  temporal facts are real and fixed, but invisible to the emit, so every consumer
  needing one would otherwise encode it at the point of use — and independent
  encodings of "which records structural columns hold an instant" drift apart, each
  narrowing for a local reason no other site knows about. The failure is not
  hypothetical: a records-grain fact could not render its own birth instant, because
  the timestamp allowlist admitted only `last_mutation_sim_time` while the raw
  projection surface admitted all three, so the two surfaces disagreed. For a
  short-lived record — created, then deactivated, with no writes between — that
  reachable instant is the *close* time on every row, leaving the natural event time
  with no expression and no effective-dated join against an SCD-2 dimension. One
  reader-owned answer is what keeps the surfaces from diverging again.
- **The instant is a contract fact; the name is presentation.** Which instant a
  column carries is pinned by the contract and belongs in the reader. Which
  real-world name that instant takes in an output is a mode's policy, and belongs to
  the mode. Splitting them is what lets three different naming policies rest on one
  set of facts instead of three privately-maintained ones.
- **Naming a column list in code is correct for a pinned prefix.** The prohibition
  on hard-coding column lists exists because the *variable tail* of a table is
  producer-extensible; it has no force against the structural prefix, which the
  contract pins by name and position and conformance checks. The structural-temporal
  mappings and the table-category set restate the vendored schema deliberately, and
  are the same hardcoding class as the conformance checks' pinned column lists.
- **Category is the one value narrowed at parse.** The sidecar reader's posture is
  otherwise permissive — parse structurally, diagnose values in `validate` — and this
  one field departs from it. The structural-temporal surface raises on an
  unrecognised category as a *caller* error, and that signal is honest only if no
  emit-supplied category can reach a consumer unvalidated. The narrowing moves the
  diagnosis of an out-of-set category from a C1 `CheckResult` to the structural
  floor; it does not remove it, and it matches the observable behavior of a *missing*
  category.
- **Strict on read where no conformance check owns the diagnosis.** The sibling
  registry views (`record_roles`, `sub_type_columns`) parse leniently because
  C12/C14 own their diagnosis; no check owns the `presentation_keys` block's
  semantic rules — conformance is the published C1–C14, reimplemented verbatim,
  and forge does not invent a C15 — and a silently-mended block would feed wrong
  keys to a consumer building a merge or join key on them. So the accessor
  refuses instead, placing enforcement at the moment claims are about to be
  used: a defective block fails exactly the claim-consuming paths and no others.
- **Exposing `pinned_ids` / `enum_domains` / `runtime` as typed accessors.** These are
  the reader's named deliverables, and `pinned_ids` is a conformance input (C9 reads
  it). Fixing them as typed accessors gives exporters a stable reader surface to build
  against. Rebasing math off `runtime` and per-sub-type *routing* are downstream
  consumers' work; the reader does own the sub-typed-ness *determination* — the
  `subtype_values` oracle that reads the `<kind>_type` domain out of `enum_domains` —
  because the `<kind>_type` naming convention and the intent-not-observation rule are
  reader-first knowledge no consumer should re-derive.

## Boundaries

What the reader deliberately does not own:

- **Branch reshaping.** The reader exposes every branch via `Sidecar.branches()` and
  imposes no branch restriction at open time; it never selects, slices, or pairs
  branches. Choosing which branch's rows an exporter emits is a reshaping concern that
  belongs to the per-branch consumer. The contracted single-branch invariant is
  enforced downstream of the open: `validate`'s C8 asserts exactly one branch (see
  [`conformance.md`](conformance.md) § The checks) and the derivations layer's
  `require_single_branch` guard enforces it at derivation time. `open_emit` is
  branch-agnostic: a multi-branch emit opens rather than failing to construct, so C8
  can diagnose it.
- **Interval materialization and value decode.** The reader exposes long-form
  `history` and documents the `LEAD` derivation and the per-type decode contract; it
  never materializes `valid_to` columns or decoded Python values. Interval
  reconstruction is the versioned-intervals derivation
  ([`derivations.md`](derivations.md)), which the dimensional mode composes; decode
  helpers land with the consumer that needs decoded values.
- **Output writers.** Reading the emit is the reader's: `Emit.query` (row tuples) and
  `Emit.query_arrow` (columnar Arrow) are both reader surfaces, since reader-first
  forbids a writer opening `run.duckdb` itself. *Writing* output — CSV / DuckDB /
  Parquet files — is a writer concern; the reader produces no output artifact.
- **Timestamp rebasing.** The reader exposes `RuntimeAnchor`; mapping `sim_time`
  through the anchor to wallclock is a downstream exporter concern.
- **Conformance assessment.** The reader *opens*; assessing whether an emit conforms
  (C1–C14) is [`conformance.md`](conformance.md)'s surface, which reads through the
  `Emit` this reader produces.
- **Presentation-column detection.** The reader classifies columns by contract
  family, never by origin. The emit carries no marker distinguishing a
  producer-minted column from an author-declared one, and no inference recovers one
  reliably, so the reader offers no such distinction for a consumer to lean on.
- **Record-kind archetypes.** The reader says which instant a structural column
  carries, never which instant a given output *should* mean. Whether a fact's event
  time is its creation or its close is the author's modelling decision (Principle
  #7); classifying kinds into archetypes to guess it would invent a mapping the
  author must specify.
- **Key declaration policy.** The reader exposes the claims and the algebra;
  whether and how an export turns them into declared constraints — the
  `declare_keys` capability, its per-table resolution rules, and the writer
  materialization — is [`declared-keys.md`](declared-keys.md)'s contract. The
  reader validates claims against the sidecar's own registries, never against
  data.
- **Class policy.** The reader *resolves* a column's class; what a consumer does with
  it is that consumer's contract. The audited-set resolution that consults the class
  is the source exporter's ([`source.md`](source.md)); a policy that refuses or omits a
  `slice_only` column from an export, or a point-in-time fold that leans on the
  genesis guarantee, belongs to the mode or derivation that owns the output shape.

## Related

| Document | Why |
|---|---|
| [`conformance.md`](conformance.md) | The C1–C14 conformance contract that reads through this reader |
| [`bundle.md`](bundle.md) | Consumer-side orientation to the format — the column temporal classes and the genesis guarantee the temporal accessors surface |
| [`source.md`](source.md) | The audited-set resolution — the first consumer of `Sidecar.temporal_class` |
| [`derivations.md`](derivations.md) | The interpretive layer that composes the faithful-read builders — the home for reads that reconstruct versions or resolve references |
| [`row-predicates.md`](row-predicates.md) | The scalar-or-list predicate grammar and the rendering authority the builders' predicate conditions compile through |
| [`dimensional.md`](dimensional.md) | The first reshaping consumer — uses `query_arrow`, the `history_tracked` flag, and the faithful-read builders |
| [`corrupters.md`](corrupters.md) | The base-emit-writing consumer — materializes every table via `query_arrow`, reads column metadata and reference targets from the `Sidecar`, and reuses the single-branch guard |
| [`declared-keys.md`](declared-keys.md) | The `declare_keys` capability — the consumer the strict `presentation_keys` accessor and the union-safety algebra exist for |
| [`temporal-elections.md`](temporal-elections.md) | The session-zone pin's consumer — the elected temporal renderings whose zone-bearing serialization the pin makes machine-independent |
| [`../../contract/base-format.md`](../../contract/base-format.md) | The vendored input contract the reader adapts to (sidecar shape, table categories, type mapping) |
| [`../CAPABILITIES.md`](../CAPABILITIES.md) | Feature inventory and status |
| [`README.md`](README.md) | Design index, package layout, staged roadmap |
| [`../../CLAUDE.md`](../../CLAUDE.md) | Principles, the isolation boundary, vocabulary |
