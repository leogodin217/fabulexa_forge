# Conformance (C1–C14)

**Status:** Implemented. Code is the contract — see
[`conformance.py`](../../src/fabulexa_forge/reader/conformance.py),
[`_schema.py`](../../src/fabulexa_forge/reader/_schema.py),
[`cli.py`](../../src/fabulexa_forge/cli.py), and
[`tests/reader/`](../../tests/reader/). Public API:
[`reader/__init__.py`](../../src/fabulexa_forge/reader/__init__.py) —
`validate`, `run_check`, `CheckResult`, `ConformanceReport`.

`validate(emit)` assesses whether a base-layer emit conforms to the base-format
contract, running the fourteen checks C1–C14 against an opened [`Emit`](reader.md). It
reimplements the conformance procedure independently of the producer's
reference conformance checker — reading the **vendored** JSON Schema and querying the
already-open DuckDB connection — so that passing the producer's reference checker and
passing this one are independent facts that must agree. `validate` is the one place
in the export package that *distrusts* the emit. The CLI verb
`fabulexa-forge validate <emit_dir>` runs it and exits non-zero on any failing check. It
depends on nothing outside the vendored `contract/`.

```
open_emit(emit_dir) ─► Emit ─► validate(emit) ─► ConformanceReport (C1..C13)
                                                       │
   contract/base-format.schema.json (vendored, C1) ───┤
   run.duckdb catalog + data (Emit.query, C2–C13) ────┘
```

---

## Surface

| Module | Owns |
|---|---|
| [`conformance.py`](../../src/fabulexa_forge/reader/conformance.py) | `validate`, `run_check`, `CheckResult`, `ConformanceReport`, the fourteen checks, the pinned spec (PS) column lists, the `to_csv_text` codec, identifier quoting |
| [`_schema.py`](../../src/fabulexa_forge/reader/_schema.py) | Loads the vendored JSON Schema from package data (the C1 input); reads only the vendored `contract/`, no other tree |
| [`cli.py`](../../src/fabulexa_forge/cli.py) | `fabulexa-forge validate` — the thin verb over `open_emit` → `validate` → report → exit code |

## Boundary

- **Input.** An `Emit` already opened — and therefore version-gated — by `open_emit`.
- **Output.** A `ConformanceReport`: exactly fourteen `CheckResult`s in C1..C14 order.
- **Reads.** The vendored `contract/base-format.schema.json` (package data) for C1;
  the DuckDB catalog (`information_schema`) and row data through `Emit.query` for
  C2–C13; the sidecar alone for C14.
- **Forbidden imports.** The producer's reference conformance checker and codec are
  references read at design time, never dependencies. Matching the producer's checker and codec
  is a conformance requirement, not a code dependency.

## Semantics

### A conformance failure is a `CheckResult`, never an exception

Diagnosing a non-conformant emit is `validate`'s whole job, so a conformance failure
is always a failing `CheckResult` in the report, never a raised error. `validate`
returns exactly fourteen `CheckResult`s in C1..C14 order and never raises a
conformance-attributable error; `run_check` obeys the same rule for the single check
it runs. `RunDatabaseError` is reserved for a `run.duckdb` that cannot be read at all
(which `open_emit` already surfaces) and never arises from sidecar↔catalog
disagreement on an already-open connection.

**C2 owns catalog↔sidecar agreement.** Any sidecar-declared table *or column* absent
from `run.duckdb` — or any catalog object the sidecar omits — is a C2 failure. Every
*other* data-reading check (C6, C7, C8, C9, C10, C11, C12, C13) first confirms via the
DuckDB catalog that each table **and column** it will read is present; an object a check reads
*opportunistically* but finds absent is excluded from that check's comparison,
recorded in its `skips`, and never queried. The one exception is an object a check
*requires*: C9 needs `records__<pinned_kind>` to exist (a pin must resolve), so its
absence is a C9 *failure*, not a skip. So a missing-but-declared table or column never
reaches a `SELECT`, and a sidecar↔catalog disagreement surfaces as a C2 failure plus
skips, never a raised error. `CheckResult.passed` is the authoritative verdict;
`messages` and `skips` are diagnostics and never decide pass/fail.

### Identifiers vs values in introspection SQL

The checks build SQL two ways. A *value* — a table-name string matched against
`information_schema.tables.table_name`, a `fork_path`, a `slice_at` bound — passes
through `Emit.query`'s positional `parameters`, never interpolated. A SQL *identifier*
— the table or column name a data query reads `FROM`/`SELECT`s — cannot be bound (SQL
binds values, not identifiers), so it is interpolated into the statement text as a
DuckDB double-quoted identifier (wrap in `"`, double any internal `"`).

Quoting upholds the never-raises promise: a malformed name is looked up as one literal
identifier, found absent by the catalog probe, and recorded as a skip (or owned by
C2) — it can never reshape the SQL. Quoting is load-bearing, not cosmetic: the schema
constrains *table* names to `^[A-Za-z_][A-Za-z0-9_]*$` (a bare word) but *column*
names only to `minLength: 1`, so a conformant column name may legally carry whitespace
or a quote and must be quoted, never interpolated bare. The read-only connection is the
final backstop: an interpolated statement can only read.

### The codec

`to_csv_text` is the *encode* half of the base-format codec, reimplemented
independently to match the producer's codec byte-for-byte:

| DuckDB column type | `to_csv_text` re-encode |
|---|---|
| `BIGINT` | `str(v)` |
| `DOUBLE` | `repr(v)` |
| `BOOLEAN` | `"true"` / `"false"` (lowercase) |
| `VARCHAR` | `v` (identity) |
| `BLOB` | not text-round-trippable — C6 skips it |

C6 re-encodes the `records__K.prop__p` cell via this codec and compares the resulting
text against the raw `history.value` (always `VARCHAR`). This is a deliberate,
**equivalent** divergence from the contract's C6 pseudocode, which *decodes*
`history.value` and compares Python values: re-encoding the records cell and comparing
text checks the same byte-symmetry property while needing only the encode half of the
codec — a decoder would have no in-package consumer. So C6 always compares raw
`history.value` text against `to_csv_text(records cell)`, never the reverse, and never
decodes either side. The producer codec is never imported; an independent copy that
must *agree* is stronger than a shared import that cannot disagree (see Rationale). The
decode direction is documented as the reader's contract — see [`reader.md`](reader.md)
§ Typed `prop__` columns.

NULL is decoded-value equality's edge case, handled before the codec runs: a
never-supplied tracked property reads NULL in its genesis `history.value` and in its
`records__` cell alike (contract § Cross-table round-trip), so latest-pre-slice NULL
against a NULL records cell passes without invoking `to_csv_text` (there is no NULL
form to encode). NULL against a non-NULL cell, or a non-NULL series value against a
NULL cell, is a round-trip mismatch — the decoded values differ.

### C1 and the unknown-top-level carve-out

C1 validates `Sidecar.raw` against the vendored JSON Schema. The vendored contract
contradicts itself here: the schema sets top-level `additionalProperties: false`,
while the prose (§ Format versioning) says an unknown optional top-level field "MAY
warn but MUST NOT fail". C1 resolves the contradiction in favor of the prose and the
version gate — an *unknown top-level* field is recorded in C1's `skips` (a warning),
not a C1 failure; an unknown key *nested* inside a known object (branch, table,
column, runtime — all `additionalProperties: false`) still fails C1. The carve-out is
narrow because the gate already accepts only `base_format_version ==
SUPPORTED_BASE_FORMAT_VERSION` and the prose declares a new optional top-level field a
version-compatible extension, so a producer that adds one within the version is not
rejected by this reader.

`record_roles` is a *known* top-level property of the vendored schema, so C1
validates it directly and it never reaches the carve-out. The carve-out governs only
genuinely unknown future top-level fields.

C1's schema check carries a `category` enum clause that no emit reaches: a table
`category` outside `{fixed, records, membership}` refuses at the reader's structural
floor, so such an emit never opens and `validate` surfaces the sidecar-parse refusal
instead of a `CheckResult` — the same observable behavior as a *missing* `category`
([`reader.md`](reader.md) § The structural-temporal surface). C1 validates against the
vendored schema with that clause intact; the diagnosis of an out-of-set category
simply sits at the structural floor rather than in conformance.

The mechanism, kept as a normative algorithm:

1. Record the unknown top-level keys as warnings —
   `unknown = sorted(set(SC.raw) - set(VS["properties"]))`, each appended to the
   result's `skips`. The top-level schema declares no `patternProperties`, so this
   set-difference is exact.
2. Validate `SC.raw` against a **shallow** copy of the vendored schema with *only* the
   top-level `additionalProperties` relaxed to `true` —
   `{**VS, "additionalProperties": True}`. Relaxing that one key leaves every nested
   object's own `additionalProperties: false` — and every type, enum, `const`,
   pattern, `minItems`, and conditional-required rule — fully enforced. The copy is
   shallow and never mutates the cached vendored schema object.

The carve-out exists only while the vendored schema and prose disagree; the
contradiction is an upstream contract bug tracked for re-vendoring. A re-vendored
schema whose top-level `additionalProperties` matches the prose collapses step 2 to a
direct validation of the unmodified schema and drops step 1.

### The checks

`validate` runs fourteen checks. Their assertions and independent-implementation notes:

| Check | What it asserts | Implementation notes |
|---|---|---|
| C1 | `base.json` validates against the vendored JSON Schema | `jsonschema.validate` of `Sidecar.raw` against the **vendored** schema; unknown *top-level* field → warning in `skips`, not a failure (above) |
| C2 | DuckDB catalog (table set, column order+types, row counts) matches the sidecar | `information_schema` introspection; compare to `TableSpec`/`ColumnSpec` **by ordinal position** (name + normalized-literal type — uppercase then collapse internal whitespace; DuckDB type synonyms are **not** reconciled and nullability is **not** compared, both per the contract) and `count(*)` vs `TableSpec.rows`. Existence is checked before count, so a phantom table/column is a C2 failure, not a raised error. Column comparison is cardinality-strict — unequal length names the surplus/missing column, never zip-truncates |
| C3 | Required tables present; table names well-formed per category | `history` is the only required fixed-category table; `records__<kind>` and `membership__<kind>__<property>` name composition matches `record_kind`/`property`. A required-but-`None` `record_kind`/`property` is a name-composition mismatch → C3 fails |
| C4 | `history` cols 1–6 match the spec exactly (names + types + order) | vs the pinned spec (PS) — the single place spec column lists are restated, solely to check |
| C5 | `records__K`: cols 1–2 are `(fork_path, record_id)`; an optional `presentation_id` at col 3; the 4-col lifecycle prefix `(created_sim_time, active, deactivated_at, last_mutation_sim_time)` at its (possibly shifted) position; `record_index` (`BIGINT`) immediately after; then, per scalar property in declaration order, `prop__<name>` followed immediately by `ref_index__<name>` iff that property's sidecar entry carries `references` (§ C5 — the records layout) | categorized *shape* of the sidecar `ColumnSpec` list, classified through the reader's records-column taxonomy; declaration order is the producer's guarantee carried by the catalog order, not independently re-derived; C2 is the sole carrier of catalog↔sidecar agreement |
| C6 | history-tracked property round-trip, **exhaustively** | driven by `history`: for every `(fork_path, kind, record_id, property)` series, the latest pre-slice `history.value` decodes to the same value as the records cell at the same `(fork_path, record_id)` — text-compared via `to_csv_text(records cell)` when both are non-NULL; NULL against NULL passes (a never-supplied tracked property), NULL against non-NULL (either direction) fails. "Latest pre-slice" = greatest `sim_time` `≤` `BranchEntry.slice_at` (sidecar-sourced, **not** `MAX(sim_time)`). A prop column outside `{BIGINT,DOUBLE,BOOLEAN,VARCHAR}` is not text-round-trippable → recorded in `skips`. Exhaustive, not sampled, so the determinism invariant holds without inventing a sample size |
| C7 | NULL all-or-none on column groups | `records__K.deactivated_at` NULL iff `active`; each membership reference pair `(member__f__kind, member__f__id)` all-NULL or all-non-NULL |
| C8 | Exactly one branch, and the single distinct `fork_path` across all tables equals that branch's | `branches` has exactly one entry; the union of distinct `fork_path` per table equals the single sidecar `fork_path`; `parent` is **not** constrained |
| C9 | If `pinned_ids` present: each `(kind,label,id)` resolves to exactly one row per `(id × fork_path present in that table)` | exhaustive over `pinned_ids`; per-branch quantifier. An absent `records__<kind>` for a pinned kind is a C9 *failure* — a pin must resolve |
| C10 | Membership integrity | `left_sim_time IS NULL OR left_sim_time >= joined_sim_time`; each non-NULL member reference resolves to **some** row in `records__<member_kind>` on the same `fork_path`, by identity (regardless of `active`) |
| C11 | `history_tracked` validity, **bidirectional** (semantic check, classed with C6/C7/C10) | Forward clause: for each distinct `(kind, property)` pair in `history`, the `prop__<property>` column on `records__<kind>` must carry `history_tracked == true` in the sidecar. Converse clause: for each `records__<kind>` with at least one row, each `prop__` column flagged `history_tracked: true` has at least one `history` row for `(kind, property)` — zero rows violates the unconditional creation seed (see [`bundle.md`](bundle.md) § Column temporal classes). The converse consults only flagged columns whose declared type is round-trippable (`{BIGINT, DOUBLE, BOOLEAN, VARCHAR}`, the same gate C6 uses — collection-struct properties emit membership tables, not `history` rows); the forward clause needs no gate. Skips when no records-category `prop__` column carries `history_tracked`; iterates in sorted order for deterministic messages |
| C12 | Record-role registry consistency (semantic check, classed with C6/C7/C9/C10/C11) | every emitted records-category kind appears in `record_roles`; every role value is in `{"dimension","fact"}`; every distinct `prop__actor_type` in `records__actor` data is declared in `record_roles["actor"]`. Coverage, not exactness — the `actor` object MAY declare unused sub-types. Skips when `record_roles` is absent (below) |
| C13 | Temporal-class consistency: the attribute pairing, the enum, the implications, and the genesis row (below) | Structural clauses over every records-category `prop__` column: `history_tracked` present **iff** `temporal_class` present; a present class is one of the three declared values; `tracked` implies flag `true`; `slice_only` implies flag `false`. Semantic clause: every flagged property of every record has its genesis `history` row at that record's own `created_sim_time` — **exhaustive** where the published procedure samples up to ten records. Skips on the same guard as C11 |
| C14 | Sub-type column partition consistency (semantic check, classed with C6/C7/C9–C13) | **Sidecar-only, no data query.** Skips when `sub_type_columns` is absent; a present `sub_type_columns` with `enum_domains` absent is a *failure*, not a skip (the `<kind>_type` domain defines the partitioned-kind set). Asserts: the partition's kinds are exactly the records kinds carrying a `<kind>_type` discriminator in `enum_domains`; per kind, the sub-type keys equal that declared domain; per kind, the union of the per-sub-type lists equals the value columns (those carrying the temporal pair) minus `prop__<kind>_type`, plus `presentation_id` when `records__<kind>` carries that column, plus each reference-typed value column's `ref_index__` sibling; per sub-type, a reference column and its `ref_index__` sibling are listed together or not at all. The discriminator carries the temporal pair yet is excluded by the carve-out; `presentation_id` carries neither and is admitted by column presence alone — the union clause requires only attribution to *some* sub-type, never which one |

The full algorithms are the check functions in
[`conformance.py`](../../src/fabulexa_forge/reader/conformance.py); the negative- and
positive-fixture tests in [`tests/reader/`](../../tests/reader/) are the worked
examples. `run_check(emit, check_id)` runs a single named check (used by
targeted negative-fixture tests) and is self-contained: a data-reading check run in
isolation performs the same catalog probe `validate` does, recording a `skip` for an
absent-but-probed object rather than raising; the C9 require-exists exception still
applies.

### C5 — the records layout

C5 checks the sidecar `ColumnSpec` list's categorized shape, classifying every
column through the reader's records-column taxonomy
([`reader.md`](reader.md) § The records-column taxonomy). Let *prefix* be
columns 1–2 (`fork_path`, `record_id`), the optional `presentation_id`, then
the four lifecycle columns:

| Condition | Result |
|---|---|
| Column after the prefix is `record_index` of type `BIGINT` | pass (continue into the property block) |
| `record_index` absent, misplaced, or non-`BIGINT` | C5 failure naming the column and position |
| Property block is, in order: `prop__<name>` [+ `ref_index__<name>` iff that `prop__` entry carries `references`] | pass |
| Reference-annotated `prop__<name>` not immediately followed by `ref_index__<name>` | C5 failure |
| `ref_index__<name>` whose preceding column is not a reference-annotated `prop__<name>` | C5 failure |
| `ref_index__<name>` of a type other than `BIGINT` | C5 failure |
| Any no-role column anywhere in the table | C5 failure |
| Any column after `record_index` that is neither `prop__<name>` nor a paired `ref_index__<name>` — regardless of taxonomy role (a duplicated lifecycle or identity column in the block *has* a role, so the no-role clause cannot be its carrier) | C5 failure |
| `presentation_id` anywhere but the slot after `record_id` | C5 failure |

C5 never raises (the `CheckResult` rule); the taxonomy's *no role* outcome maps
to a recorded failure. C2 is the **sole** carrier of catalog↔sidecar agreement
— C5's subject is the sidecar list's shape alone, and it does not re-check the
catalog's property block; C2's element-wise catalog↔sidecar comparison covers
every column, the index families included, with no per-family clause.

Nullability is not compared, per the shared C2/C5 stance. This is a deliberate
narrowing of the contract's C5 pseudocode, which writes `BIGINT NOT NULL`: the
sidecar carries no nullability field and neither C2 nor C5 compares catalog
nullability, so a NULL-bearing `record_index` is the producer's defect to
prevent, not forge's to detect. Forge checks name, type, and position.

### C12 — record-role registry consistency

C12 skips — recording the skip and passing by vacuity — when the sidecar omits
`record_roles` (an emit predating the additive registry). When present it asserts
three things against the sidecar and `records__actor` data:

- **Kind coverage.** Every emitted records-category kind — any kind with a
  `category == "records"` table declared in the sidecar, `actor` included — must
  appear in `record_roles`. An emit that declares a `records__actor` table is not
  actor-less, so `record_roles["actor"]` is mandatory for it; an actor-less scenario
  has *neither* the table *nor* `record_roles["actor"]`.
- **Role validity.** Every non-`actor` value is in `{"dimension", "fact"}`;
  `record_roles["actor"]`, when present, is an object whose every value is in that
  set.
- **Actor sub-type coverage.** Every distinct `prop__actor_type` value present in
  `records__actor` data is declared in `record_roles["actor"]`. This is coverage, not
  exactness: the object MAY declare sub-types absent from a given slice's data.

Membership tables need no separate clause — every membership kind also has a records
table, so role coverage is transitive through the records loop. C12 follows the
never-raises rule: if `records__actor` is declared but absent from the catalog the
sub-type clause records a skip (C2 owns the catalog↔sidecar disagreement) while
kind-coverage still runs from the sidecar; if `records__actor` is present but holds
zero rows the sub-type clause passes by vacuity — a pass, not a skip.

### C13 — temporal-class consistency

C13 skips — recording the skip and passing by vacuity — when no records-category
`prop__` column carries `history_tracked`, the same published additive-field guard
C11 uses. Past the version gate the guard is unreachable against a producer-written
emit (the contract puts the attribute pair on every value-carrying column), and it is
retained so the checker is a correct standalone implementation of the published
procedure, guard included.

The **structural clauses** run over every records-category `prop__` column:
`history_tracked` is present **iff** `temporal_class` is present; a present class is
one of the three declared values; `tracked` implies `history_tracked: true`;
`slice_only` implies `history_tracked: false`. The vendored schema enum-constrains a
*present* class value — an out-of-enum declaration therefore fails C1 as well — but
does not enforce the pairing; the pairing clauses are C13's alone.

The **semantic clause** asserts the genesis row: for every `prop__` column flagged
`history_tracked: true` (any class) and every record of that kind, `history` carries
a row for `(kind, record_id, property)` at that record's own `created_sim_time`.
`record_id` is part of the match — a rowless record does not pass because a sibling
of the same kind shares its `created_sim_time` (the published pseudocode's
`(kind, property)` shorthand is scoped inside its per-record loop; the full key is
what is matched). Collection-struct properties stay outside the semantic clause's
input set, excluded by the same round-trippable-type gate C6 and C11's converse use
(`{BIGINT, DOUBLE, BOOLEAN, VARCHAR}`); their changes emit membership tables, not
`history` rows. The structural clauses have no `history` side and run ungated.

**Exhaustive where the procedure samples.** The published procedure requires a
sample of up to ten records; C13's genesis clause checks every record — the same
strictness choice C6 makes (the contract permits it: "exhaustive checking is the
consumer's choice"), and an exhaustive pass needs no sample-selection rule, keeping
`validate` deterministic.

### Comparison sources

Every check compares a fixed pair of artifacts; naming both sides removes the
ambiguity of "matches the spec". The five sources:

- **VS** — the vendored JSON Schema (`contract/base-format.schema.json`, package data)
- **PS** — the reader's pinned spec: the C4/C5 fixed column lists restated in code,
  the single sanctioned restatement, used only to *check*, never to discover
- **SC** — the typed `Sidecar` (= `base.json`: `TableSpec`/`ColumnSpec`, `branches`,
  `pinned_ids`, `SC.raw`)
- **DB-cat** — the DuckDB catalog (`information_schema`) via `Emit.query`
- **DB-data** — DuckDB row content via `Emit.query`

| Check | Left | Right | Compared |
|---|---|---|---|
| C1 | `SC.raw` | VS | `jsonschema.validate` |
| C2 | DB-cat | SC | table set; per column name + ordinal position + type; `count(*)` vs `rows`. **Not** nullability |
| C3 | SC table names | SC `category`/`record_kind`/`property` | required tables exist; name composition |
| C4 | SC `history` columns | PS | names + types + order |
| C5 | SC records columns | PS (shape) | `(fork_path, record_id)` · optional `presentation_id` · 4-col lifecycle prefix · contiguous `prop__` block |
| C6 | DB-data `history.value` | DB-data records cell, codec re-encoded | SC supplies the prop type (codec key) and the per-`fork_path` `slice_at` bound |
| C7 | DB-data | — | NULL all-or-none per group |
| C8 | DB-data distinct `fork_path` + SC `branches` cardinality | SC `branches[].fork_path` | exactly one branch; `parent` not constrained |
| C9 | SC `pinned_ids` | DB-data records rows | one row per `(id × branch)`; absent table for a pinned kind → fail |
| C10 | DB-data membership | DB-data records existence | reference resolution by identity |
| C11 | SC `ColumnSpec.history_tracked` | DB-data `history` `(kind, property)` pairs | forward: each pair in `history` is sidecar-flagged; converse: each flagged round-trippable column of a non-empty kind has ≥ 1 `history` row; skip when no records `prop__` column carries the flag |
| C12 | SC `record_roles` + SC `category == "records"` kinds | DB-data distinct `records__actor.prop__actor_type` | kind coverage; role values in `{"dimension","fact"}`; actor sub-type coverage; skip when `record_roles` absent |
| C13 | SC `ColumnSpec.{history_tracked, temporal_class}` | the contract's pairing/enum/implication clauses; DB-data `history` genesis rows vs `records__<kind>.created_sim_time` | pairing iff; enum; implications; a genesis row per `(kind, record_id, property)`, exhaustive over records; skip on C11's guard |

**C4/C5 read the sidecar, not the catalog.** They check that `base.json`'s declared
fixed/records columns match the contract (PS); C2 separately checks that the live
catalog matches `base.json` (SC). The two compose — catalog → sidecar → contract — so
catalog conformance to the contract is transitive. This is why a sidecar edited to
match a broken DuckDB (a `history` col 1 retyped to `BIGINT`) passes C2 (catalog ==
sidecar) but fails C4 (sidecar `BIGINT` ≠ PS `VARCHAR`).

**C5 verifies shape, not scenario declaration order.** A contract-only reader has no
scenario or `Schemas` to compare against and must not acquire one (the zero-deps
boundary). The sidecar column list *is* DuckDB-catalog order, and for a records table
the catalog order of `prop__` columns *is* declaration order (the producer's
guarantee). So C5 verifies the categorized shape against the sidecar `ColumnSpec`
list, C2 ties that list to the live catalog, and "declaration order" itself is taken
as the producer's guarantee, not re-derived.

### CLI surface

`fabulexa-forge validate <emit_dir>` is the thin verb: `open_emit` → `validate` → print the
report → exit code.

| Condition | Result |
|---|---|
| All of C1–C14 pass | print per-check PASS summary (with any skips noted); exit 0 |
| Any check fails | print the failing checks + messages (and any skips); exit non-zero |
| `open_emit` raises (missing files, bad JSON, unsupported version, bad structure, unreadable DB) | print the reader error; exit non-zero |

The reader has no educator-facing YAML config — it reads an emit, it does not describe
a scenario. `fabulexa-forge validate` is its only user surface.

## Invariants

1. **Conformance independence.** `validate` reproduces C1–C14 (and the `to_csv_text`
   codec for C6) from the vendored spec alone. Passing the producer's reference checker
   and passing this one are independent facts that must agree.
2. **Single coupling.** No dependency on the bundle's producer; the vendored
   `contract/` is the only coupling. The producer's reference conformance checker and
   codec are references read at design time, never imported.
3. **Total and never-raising.** `validate` returns exactly fourteen `CheckResult`s in
   C1..C14 order. A conformance failure is a failing `CheckResult`, never an
   exception; only an operational failure (an unreadable `run.duckdb`) raises
   `RunDatabaseError`. Data-reading checks probe the catalog before querying, so a
   sidecar↔catalog disagreement is a C2 failure plus skips, never a raised error.
4. **Exhaustive and deterministic.** C6 checks every `history` series and C13's
   genesis clause checks every record rather than a sample, so the reader's
   determinism invariant holds without inventing a sample size or a
   sample-selection rule.

## Rationale

- **C8 asserts the contracted single-branch invariant.** The sanitised subset
  mandates exactly one branch, so C8 verifies cardinality (one `branches` entry) on
  top of the fork-path set-equality. `validate` enforces this rather than ignoring it
  because a multi-branch emit is non-conformant to the subset, not merely unsupported.
  The check is independent of the derivations layer's `require_single_branch` guard,
  which enforces the same invariant at derivation time: the two are complementary
  (validate-time vs. derive-time) and neither replaces the other. `open_emit` itself
  is branch-agnostic — a multi-branch emit opens rather than failing to construct, so
  C8 can diagnose it.
- **Reimplement the codec rather than parse `history.value` loosely.** C6's whole
  value is byte-symmetry between `records__K.prop__p` and `history.value`. Re-encoding
  the typed record value with the producer's exact codec (lowercase booleans,
  `repr` floats, `str` ints, identity strings) is the only way to detect a
  producer/consumer codec drift. An independent copy that must *agree* is stronger
  than a shared import that cannot disagree.

## Boundaries

- **`validate` reimplements the full procedure C1–C14, which is itself narrower than
  the producer's full QA suite.** C14 (sub-type column partition consistency) is
  reimplemented and skips when the optional `sub_type_columns` sidecar field is
  absent, exactly as the contract's own C14 skips. Even so, neither the procedure nor
  `validate` asserts duplicate-tick suppression or records-prop reference integrity;
  conformance resolves references only for *membership* tables (C10), not records
  props. A defect outside the procedure — a duplicate `(…, sim_time)` history row, a
  dangling `records__*.prop__*` reference — *passes* `validate` by design. Those
  deeper guarantees belong to the producer's separate QA tooling, not the bundle
  conformance contract; a checker for them would be a separate checker, not an
  extension of `validate`.
- **Pair agreement and index resolution are producer trust class.** The
  contract places `prop__<name>` ↔ `ref_index__<name>` agreement (NULL
  together; resolving to the same target row) and resolution of `ref_index__`
  values against the target's `record_index` outside C1–C14 —
  producer-guaranteed by construction, the same trust class as `prop__`
  referential integrity (contract § Dense record index) — so `validate`
  mirrors the contract's checks and invents none. C2/C5 check the pair's
  *shape*, never its cell values. The corrupter's pair-atomicity rule exists to
  keep forge's own manifest honest, not to police producers
  ([`corrupters.md`](corrupters.md) § Reference pairs: one edge, two
  encodings).
- **C11 and C13 (the temporal-attribute checks) ARE reimplemented** (semantic checks,
  classed with C6/C7/C10). Both skip when no records-category `prop__` column carries
  `history_tracked` — the published additive-field guard. Consumers (the dimensional
  exporter's tracked-vs-static split, the source exporter's genre predicate) *read*
  the temporal pair through the reader on an emit they trust to be conformant (as
  they trust C1–C14), and C11/C13 are the checks that warrant that trust. The
  producer's deeper QA guarantees remain out of scope.
- **Decode direction of the codec.** Conformance needs the *encode* direction only;
  the per-type decode contract is the reader's — see [`reader.md`](reader.md).
- **The pinned structural column lists are literal here.** Elsewhere a consumer
  needing a structural-column fact reads the reader's surface rather than restating
  one ([`reader.md`](reader.md) § The structural-temporal surface). C5's lists are the
  exception, and must remain so: they *are* the check that the contract's structural
  prefix is present and correctly ordered, so expressing them in terms of a shared
  surface derived from the same contract would make the check test itself.

## Related

| Document | Why |
|---|---|
| [`reader.md`](reader.md) | The `Emit`/`Sidecar` surface every check reads through; the decode-direction codec contract |
| [`../../contract/base-format.md`](../../contract/base-format.md) § Conformance procedure | The vendored conformance procedure this reimplements (C1–C14), and § Format versioning behind the C1 carve-out |
| [`../../contract/base-format.schema.json`](../../contract/base-format.schema.json) | The vendored JSON Schema C1 validates against |
| [`bundle.md`](bundle.md) | Consumer-side orientation — the column temporal classes and the genesis guarantee C11/C13 judge |
| [`../CAPABILITIES.md`](../CAPABILITIES.md) | Feature inventory and status |
| [`../../CLAUDE.md`](../../CLAUDE.md) | Principles, the isolation boundary, vocabulary |
