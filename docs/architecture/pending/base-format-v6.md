---
status: draft
---

# Base-Format v6 Adoption — Dense Record Index (Compatibility)

Adopt `base_format_version: 6`. This is the **forced** compatibility phase plus the
one structural fix that keeps the next bump cheap, plus the one behavior correction
that fix forces into the open (`init`'s accidental lifecycle proposals); every *use*
of the new columns is deferred and named in § What Doesn't Change.

---

## Problem

The vendored contract bumps to v6, whose structural change is the **dense record
index**: every `records__<kind>` table gains

- `record_index` — `BIGINT NOT NULL`, immediately after the lifecycle prefix
  (shifted by one when the optional `presentation_id` is present): the 0-based
  `(fork_path, kind)` creation-order ordinal;
- `ref_index__<name>` — `BIGINT` nullable, immediately following each
  reference-typed property's `prop__<name>` column, carrying the referenced
  record's `record_index`; NULL iff the reference is NULL.

Both are **identity columns**, like `record_id` and `fork_path`: they carry no
`history_tracked` / `temporal_class` attributes, and `ref_index__<name>` carries no
`references` annotation of its own — the sibling `prop__<name>`'s sidecar entry
stays authoritative for the target kind. The contract's C5 is amended for the new
positional shape. No sidecar field changes beyond the version constant; `history`
and membership tables are untouched. (v6 also stamps the pre-existing
`last_mutation_sim_time` high-water clause as binding — a normative strengthening
riding the same bump; no forge check or mode reads that guarantee, so it is inert
here.)

The contract (§ Dense record index) pins four guarantees this design leans on:

- **Density.** Per `(fork_path, kind)`, `record_index` values exactly cover
  `0 .. tables[].rows - 1` — every integer assigned to exactly one row, so a
  consumer MAY allocate an array of length `rows` and index it directly.
- **Row-order corollary.** `record_index` equals the record's 0-based position
  in the records table's guaranteed row order (creation order within kind) —
  consumer-verifiable directly against the data.
- **Slice stability.** A record keeps the same `record_index` in every emit of
  its branch: an earlier slice drops a creation-order suffix, leaving the
  remaining indices a dense prefix of the same enumeration; deactivation never
  renumbers.
- **Trust class.** Pair agreement (`ref_index__<name>` and its sibling
  `prop__<name>` NULL together and resolving to the same target row) and
  resolution of `ref_index__<name>` values against the target's `record_index`
  are producer-guaranteed **by construction and not verified by C1–C13** — the
  same trust class as `prop__` referential integrity.

The two families differ in temporal character: `record_index` is stable across
slices of a branch, while `ref_index__<name>` is a **point-in-time key** — it
renders the referenced record's `record_index` *at the emitted slice*, so it
legitimately differs across slices of the same branch when the reference itself
was rewritten between them.

Forge cannot read a v6 emit today, and — the sharper problem — parts of it would
misbehave **silently** rather than fail loudly:

- **Conformance.** The version gate refuses v6. C5 asserts every column after the
  lifecycle prefix is `prop__`-prefixed; `record_index` and every interleaved
  `ref_index__` column fail it.
- **Silent flow-through.** The source mode's reference and transaction renders
  enumerate the full records column list, so the new columns would land raw in
  exported tables — while the change-log and snapshot renders (property-driven and
  fixed-list respectively) drop them. One export, inconsistent posture, decided by
  enumeration accident. `init` would likewise propose `record_index` /
  `ref_index__…` as dimension columns.
- **Undeclared corrupter defect.** `dangle_reference` and `mispoint_reference`
  rewrite only the `prop__` side of a records reference; `null_cells` can null it.
  Under v6 the sibling `ref_index__` cell goes stale — the contract's *NULL iff the
  reference is NULL / carries the target's index* pair clause breaks with no
  `defects.json` declaration, violating the manifest's completeness promise.
- **Fixtures.** The single-constructor invariant cannot express an identity column:
  `prop_column` requires the temporal pair. The spanning and negative fixtures are
  not v6-shaped.

The root architectural gap, which the flow-through symptoms share: **records-table
column classification has no single authority and no closed-world posture.** C5,
the source planner, `init`, and the snapshot/change-log column resolvers each
restate "structural vs payload" with private predicates, so a new contract column
family falls through some of them silently instead of failing one shared classifier
loudly.

## Solution

One compatibility design: adopt v6 mechanically (version gate, amended C5,
v6-shaped fixtures), introduce a **records-column taxonomy** on the reader — one
total classifier every records-column consumer reads through, where an
unclassifiable column is a loud condition, never a fall-through — and set the
Phase-1 **identity-column posture: the v6 index columns appear in no exporter
output and no `init` proposal**. Corrupter reference-writes become **pair-scoped**:
any operation that writes a records reference `prop__` cell writes the sibling
`ref_index__` cell in the same act, keeping every injected defect fully declared.

```
records__<kind> column   →  taxonomy role   →  Phase-1 exporter posture
─────────────────────────────────────────────────────────────────────────
fork_path                   identity            dropped (unchanged)
record_id                   identity            kept as `id` (unchanged)
record_index                identity            dropped (new)
ref_index__<name>           identity            dropped (new)
presentation_id             presentation        kept (unchanged)
created_sim_time … etc.     lifecycle           per existing defaults (unchanged)
prop__<name>                payload             per existing defaults (unchanged)
anything else               — no role —         ERROR, never fall-through (new)
```

The posture column reads over exporter *output*; `init` proposals are narrower —
payload and presentation roles only (§ Exporter posture).

Surfacing the index (integer-PK/FK realism in source mode, dimensional surrogate
keys, point-in-time joins) is the elective harvest — deferred, each its own design.

## Affected Subsystems

- **Contract (vendored) — landed.** The v6 contract-sync (upstream `5d566486`)
  is merged: `contract/base-format.md` + `base-format.schema.json` sit at
  `base_format_version: 6`. The vendored files are authoritative — this doc's
  restatement of the v6 shape defers to them wherever they differ; the index
  semantics above cite `contract/base-format.md` § Dense record index.
- **Reader (sidecar surface).** Gains the records-column taxonomy: a pure,
  name-family classifier for records-category columns (`identity` /
  `presentation` / `lifecycle` / `payload`, or *no role*), plus the
  `prop__<name>` ↔ `ref_index__<name>` sibling-name rule. No change to
  `ColumnSpec` (its optional `references` / temporal-pair fields already express
  identity columns), the version gate's mechanism, or the structural floor. The
  supported-version literal moves 5 → 6.
- **Conformance.** C5's positional check adopts the v6 layout: `record_index`
  required (name + type) immediately after the possibly-shifted lifecycle prefix;
  the remaining block is, per scalar property in declaration order, `prop__<name>`
  followed immediately by `ref_index__<name>` iff that property's sidecar entry
  carries `references`. C5 classifies through the taxonomy; a no-role column is a
  C5 failure (recorded, never raised). C1 picks up the re-vendored schema through
  the existing load path. C2 is unchanged because it needs no change — its element-wise
  catalog↔sidecar agreement covers the new columns automatically, and is what
  carries their catalog agreement. C6–C13 are unchanged — their predicates are
  `prop__`-closed, so identity columns are correctly invisible to the
  temporal-pair and round-trip clauses.
- **Source exporter.** The reference, transaction, and snapshot renders classify
  every records column through the taxonomy instead of enumerating the full column
  list; a no-role column fails export validation with a named error. The
  presentation-defaults table gains two rows: `record_index` → dropped,
  `ref_index__<name>` → dropped — following `fork_path`'s precedent (dropped
  identity; not addressable by `rename`, since there is no output column to name).
  All four genres now agree on the posture.
- **Dimensional `init`.** The proposal loop classifies through the taxonomy and
  proposes only payload (and presentation) columns. This is a deliberate behavior
  change, not a pure port: today's loop skips exactly `fork_path` / `record_id` /
  `active` / `deactivated_at` and proposes everything else — so `created_sim_time`
  and `last_mutation_sim_time` land in proposals by enumeration accident, the same
  fall-through species as the v6 leak. Under the taxonomy the posture is per-role:
  identity dropped, lifecycle never proposed (the SCD-2 stub's `valid_from` /
  `valid_to` are `history`-derived, not read from lifecycle columns),
  payload + presentation proposed. Any base column stays reachable by explicit
  author projection.
- **Corrupters.** Four changes. (1) *Pair-scoped reference writes*: `null_cells`
  co-nulls, `dangle_reference` co-dangles (absent-index sentinel), and
  `mispoint_reference` co-points (donor's `record_index`) the sibling
  `ref_index__` cell whenever they write a records reference `prop__` cell — one
  defect, one `DefectRecord`, locator and `impact` unchanged. (2) `insert_rows`
  mints a fresh `record_index` per phantom alongside the fresh `record_id`.
  (3) The base-emit writer round-trips identity columns as bare `{name, type}`
  sidecar entries (no annotation, no temporal pair) — expected to hold already;
  becomes a stated, test-guarded invariant. Identity columns remain never
  selectable by any operation (already true by predicate construction; becomes a
  stated invariant with negative tests). (4) Jitter's eligibility gains an
  explicit reference-exclusion clause: today a records reference `prop__` cell is
  jitter-ineligible only via the numeric-type gate (reference ids are not
  numeric); pair atomicity must not rest on a type coincidence, so the exclusion
  becomes declared and negatively tested.
- **Test fixtures and support.** A sibling constructor to `prop_column` for
  identity columns — the sole constructor for every fixture identity entry
  (`fork_path` / `record_id` / `record_index` / `ref_index__<name>`), in records
  and membership table entries alike: `fork_path` / `record_id` are the same
  identity name family on membership tables (the index families never occur
  there), and the constructor's check is a pure name rule, so nothing scopes it
  to one category. Every existing hand-written `fork_path` / `record_id` literal
  entry migrates through it, so the single-constructor invariant covers the
  whole family. The spanning fixture becomes genuinely v6-shaped (populated
  `record_index` ordinals; `ref_index__` values consistent with its reference
  column). Every negative fixture is audited to stay v6-shaped so each still fails
  *only* its named check; new C5 negatives cover the v6 layout.
- **Recipe ground truth.** Expected to be **stable** under the drop posture:
  exporter outputs carry no new columns, and the pair co-writes change no defect
  class, locator, count, or `impact` set. The one deliberate exception: `init`
  recipe expectations move — `created_sim_time` / `last_mutation_sim_time`
  disappear from proposals (§ Dimensional `init`) — and exactly that delta.
  Stability is verified, not assumed; any other expectation that moves is the
  system reporting a semantic change and must be examined, not re-baselined
  silently.

## What Doesn't Change

- **Every elective use of the index** — source-mode integer PK/FK presentation,
  dimensional surrogate keys from `record_index`, point-in-time / feature-store
  joins over `ref_index__`, carrying the index through the change-log or streaming
  folds, and any corrupter operation targeting deliberate `prop__`↔`ref_index__`
  split-brain. Each returns as its own design. One rule is already fixed for any
  future reconstruction surface (Stage 5): `history.value` is id-space only, so
  point-in-time reconstruction **re-derives** `ref_index__<name>` from the
  reconstructed `prop__<name>` via the target's `record_index` — it never
  carries the emitted slice's `ref_index__` value, which is a point-in-time key
  valid only at its own slice (§ Problem).
- **No new conformance check.** The contract itself places pair agreement and
  index resolution outside C1–C13 — producer-guaranteed by construction, the
  same trust class as `prop__` referential integrity (contract § Dense record
  index) — so `validate` mirrors the contract's checks and invents none. The
  corrupter's pair rule exists to keep forge's own manifest honest, not to
  police producers.
- **Membership, `history`, and fixed tables** — v6 does not touch them; the
  junction render, family-C/E populations, and `member__*` handling are unchanged.
  There is no `ref_index` analog on membership reference pairs.
- **Derivations.** All five residents are untouched: none reads identity columns,
  and state-at's reconstructed column set stays as it is.
- **Streaming, pacing, routing, mixer, anchor, incremental, writers** — no change.
  The change-log render's column set (already property-driven) is unchanged.
- **Dimensional export grammar.** Projection stays author-driven; identity columns
  are neither proposed nor specially forbidden — a base column named explicitly in
  author config projects faithfully, as any base value does.
- **Config surface.** No new fields in `ExportConfig` / `SourceConfig` /
  `StreamConfig` / `CorruptConfig`; no CLI changes. This change has zero
  author-facing configuration.
- **The version-gate mechanism** — equality against the single supported-version
  literal, `UNSUPPORTED_VERSION_SENTINEL` for gate negatives; only the literal's
  value moves.

## Semantics

### The records-column taxonomy

Classification is by name family alone, total over the v6 contract layout, and
context-free (no sidecar lookup, no table state):

| Column name | Role |
|---|---|
| `fork_path`, `record_id`, `record_index` | `identity` |
| `ref_index__<name>` (prefix match) | `identity` |
| `presentation_id` | `presentation` |
| `created_sim_time`, `active`, `deactivated_at`, `last_mutation_sim_time` | `lifecycle` |
| `prop__<name>` (prefix match) | `payload` |
| anything else | **no role** |

*No role* is a first-class outcome every caller must treat loudly: C5 records a
failure; an exporter raises a named validation error. No caller may skip, drop, or
pass through an unclassified column. This is the closed-world posture: the next
contract column family changes the taxonomy in one place and turns every
unprepared consumer red.

The classifier applies to **records-category** tables only. Sibling pairing is a
pure name rule: `prop__<name>` ↔ `ref_index__<name>` share `<name>`; whether a
given `prop__` column *has* a sibling is determined by its own sidecar
`references` field (annotation present ⇒ sibling required — C5 enforces).

### C5 under v6

C5 checks the sidecar `ColumnSpec` list's categorized shape. C2 carries
catalog↔sidecar agreement — and becomes its **sole** carrier: today's C5 also
independently re-checks the catalog's `prop__` block, a redundancy the amended
C5 drops rather than extends to the v6 layout. Let *prefix* be columns 1–2
(`fork_path`, `record_id`), the optional `presentation_id`, then the four
lifecycle columns:

| Condition | Result |
|---|---|
| Column after the prefix is `record_index` of type `BIGINT` | pass (continue into the property block) |
| `record_index` absent, misplaced, or non-`BIGINT` | C5 failure naming the column and position |
| Property block is, in order: `prop__<name>` [+ `ref_index__<name>` iff that `prop__` entry carries `references`] | pass |
| Reference-annotated `prop__<name>` not immediately followed by `ref_index__<name>` | C5 failure |
| `ref_index__<name>` whose preceding column is not a reference-annotated `prop__<name>` | C5 failure |
| `ref_index__<name>` of a type other than `BIGINT` | C5 failure |
| Any no-role column anywhere in the table | C5 failure |
| Any column after `record_index` that is neither `prop__<name>` nor a paired `ref_index__<name>` — regardless of taxonomy role (a duplicated lifecycle or identity column in the block *has* a role, so the no-role clause cannot be its carrier) | C5 failure (with the no-role clause, subsumes today's "non-`prop__` in the block" clause) |
| `presentation_id` anywhere but the slot after `record_id` | C5 failure (unchanged) |

C5 never raises (the `CheckResult` rule); the taxonomy's *no role* outcome maps to
a recorded failure. Nullability is not compared, per the existing C2/C5 stance —
`record_index`'s `NOT NULL` is the producer's to write; forge checks name, type,
and position. This is a deliberate narrowing of the contract's C5 pseudocode,
which writes `BIGINT NOT NULL`: the sidecar carries no nullability field and
neither today's C2 nor C5 compares catalog nullability, so a NULL-bearing
`record_index` is the producer's defect to prevent, not forge's to detect.

### Exporter posture

| Mode / surface | v6 identity columns (`record_index`, `ref_index__*`) |
|---|---|
| Source — reference & transaction renders | dropped (via taxonomy classification; previously would have leaked raw) |
| Source — change-log render | dropped (already property-driven; now also true by posture, not accident) |
| Source — snapshot render | dropped (fixed column set, unchanged) |
| Source — junction render | n/a (membership tables carry no v6 columns) |
| `init` proposals | never proposed |
| Dimensional export | not proposed, not forbidden — author-declared projection only |
| Streaming (both content types) | absent (property-driven event assembly, unchanged) |

Source-mode classification failure: a records column with no taxonomy role fails
export validation with a named error (see § Validation Rules) — the exporter
sibling of C5's recorded failure. `exclude` and `rename` semantics are unchanged;
a dropped identity column is not a rename target (rename keys are source identity
for columns that produce output; `fork_path`'s existing precedent).

`init` proposals are role-scoped beyond the identity drop: payload and
presentation only. Lifecycle columns are never proposed: `active` /
`deactivated_at` stay skipped as today (the SCD-2 stub's `valid_from` /
`valid_to` are `history`-derived, not read from lifecycle columns), and
`created_sim_time` / `last_mutation_sim_time` — proposed today only because the
ad-hoc skip list never decided them — no longer are.
Explicit author projection remains the path to any base column (§ What Doesn't
Change — dimensional export grammar).

Denormalized payload the producer retains by necessity is likewise the
author's call, not forge's: the published parent-child example's member kind
carries `prop__group_domain` — a parent value projected onto the child because
it is the projection input for the member's `email` presentation property, so
the upstream cannot drop it. A normalized-export posture over such a column is
an author `exclude`, never a forge default (Principle #7 — forge does not
decide which payload columns are "really" denormalized); a recipe should show
the exclude when the recipe set for this area lands.

**Rationale for dropping rather than carrying:** the change-log and snapshot
renders are fold-driven and structurally cannot carry per-record identity columns
without new derivation work; carrying them only where full-list enumeration
happens to leak them is incoherent within a single export. Surfacing the index
*well* — as the integer PK/FK a real operational system would show — is
adoption-scale design (key presentation, join guidance, incremental-window
interaction) that must not half-ship as an enumeration side effect.

### Corrupter reference-pair writes

At v6, a records reference is **one edge with two encodings** — id-space
`prop__<name>` (joining the target's `record_id`) and index-space
`ref_index__<name>` (joining the target's `record_index`). The rule: an
operation that rewrites a reference rewrites the **edge, not a column** — it
writes both encodings of the same row in the same act. Rewriting only the
id-space encoding would leave the index-join still resolving to the old target:
an undeclared recovery path, so `defects.json` would misdescribe the injected
defect. One defect, one `DefectRecord`; the locator stays the `prop__<name>`
cell; declared `class` and `impact` are computed exactly as today (no semantic C-check reads `ref_index__`
cell *values* — C2/C5 check shape, not values — and it carries no history
series, so no impact rule changes).

| Operation | `prop__<name>` write | Sibling `ref_index__<name>` write | Pair state after |
|---|---|---|---|
| `null_cells` | NULL | NULL | consistent (NULL/NULL — a whole missing reference) |
| `dangle_reference` | `__dangling__<n>` id sentinel (existing rule) | `-(n + 1)` — negative, hence guaranteed absent from the 0-based index domain; deterministic from the same suffix `n` | consistent-in-shape (both non-NULL, both unresolvable) |
| `mispoint_reference` (both `constraint` modes) | donor's `record_id` (existing rule) | donor's `record_index`, read from the same operation-start working state as the donor pool | fully consistent — the defect remains invisible to `validate`, recoverable only via `defects.json` |

Boundary rows of the same table (no rule change, stated for closure):

| Operation | v6 behavior |
|---|---|
| `delete_rows` | removes whole rows; inbound `ref_index__` cells elsewhere dangle *alongside* their `prop__` siblings — both encodings of the same now-broken edge, **outside the declared surface** exactly as inbound `prop__` dangles are today (records referential integrity has no C-check; it is the contract's producer trust class). The existing wake rules (pin / history / membership) are unchanged and none gains a `ref_index__` clause. The ordinal gap the removal leaves in the `record_index` domain is part of the same declared row-removal defect, exactly as the removed `record_id` is — density is contract prose, not a C-check |
| `duplicate_rows` (all three modes) | copies whole rows; the pair travels together. The copy's duplicated `record_index` is part of the declared duplicate-row defect, exactly as its duplicated `record_id` is. Jitter cannot perturb the pair (identity columns fail its prefix gate; reference `prop__` columns fall under the now-explicit reference-exclusion clause); `mutation` cannot mutate it (reference columns are not family-A-eligible) |
| `insert_rows` | clones the donor's `ref_index__` cells verbatim (they resolve to the same targets the donor's do — consistent with the cloned `prop__` references); resample eligibility already excludes reference columns, so a resample can never split a pair |
| `mutate_cells`, `schema_drift` | reference columns remain ineligible (existing predicates); therefore no other operation can write one side of a pair |

The sentinel choice `-(n+1)`: negative values are absent from a 0-based ordinal
domain *by construction* — immune to later `insert_rows` phantoms (which mint
upward) and requiring no working-set scan; deriving it from the id sentinel's
suffix `n` keeps the pair visibly one defect and preserves determinism. `n` is
minted once **per target kind** and shared by that kind's dangles (the existing
id-sentinel rule), so every row dangled toward the same kind carries the same
(`__dangling__<n>`, `-(n+1)`) pair — harmless, since pair consistency is a
per-row property and each dangled row has its own `DefectRecord`.

### `insert_rows` — fresh `record_index`

| Aspect | Rule |
|---|---|
| Value | per-table ordinal high-water mark `+ 1 + i`, for the operation's *i*-th phantom in assignment order (ascending selected-unit order, matching the existing id-assignment discipline). The high-water mark is engine state per `records__<K>` working table: initialized to the table's maximum `record_index` when the working set loads (`rows − 1`, by input density), advanced past each minted phantom, never lowered |
| Absence guarantee | strictly above every ordinal that has *ever* appeared in the working table — not merely above the current maximum, which an earlier `delete_rows` may have lowered; earlier phantoms of the same operation occupy the intervening values |
| Interaction with earlier `delete_rows` | gaps left by deletions are *not* reused, suffix gaps included. `max` over the current working table alone would re-mint an ordinal whose row a deletion removed — and a stale inbound `ref_index__` cell dangling toward that ordinal would silently re-resolve to the phantom, an undeclared mispoint-shaped pair state the pair-atomicity invariant forbids. The high-water mark forecloses it: no tombstoned identity is resurrected |
| Declared defect | unchanged — the phantom's one `DefectRecord`; the fresh index is part of the phantom's identity, as the fresh id is |

### Fixtures

| Requirement | Rule |
|---|---|
| Spanning fixture | every records table carries `record_index` populated `0 … rows−1` in a deterministic per-kind order, and `ref_index__<name>` beside its reference `prop__<name>`, values consistent with the target table's ordinals (NULL iff the reference cell is NULL) |
| Adversarial shape | imitate the published example (`docs/examples/parent-child/published/`): the referenced kind mixes id shapes (decimal-string and hex-digest ids interleaved by creation order), and at least one reference pair is NULL-together — so an implementation that conflates the two encodings (e.g. `prop__X = CAST(ref_index__X AS VARCHAR)`, which matches only 16/29 membered rows in the example) cannot pass by id/index coincidence |
| Identity-column construction | through the new constructor only — it emits a bare `{name, type}` entry and rejects a non-identity-family name; a temporal attribute or `references` annotation on an identity column is *inexpressible* through it (negative variants mutate the returned dict, mirroring `prop_column`'s convention). Every existing hand-written `fork_path` / `record_id` literal entry migrates through it — membership-table entries included (§ Affected Subsystems — test fixtures and support) |
| New C5 negatives | one fixture per amended clause: missing `record_index`; misplaced `record_index`; reference-annotated `prop__` without a following `ref_index__`; `ref_index__` with a non-reference predecessor; `ref_index__` of a non-`BIGINT` type. Each fails C5 and only C5 |
| Negative-fixture audit | every existing negative fixture becomes v6-shaped so it still fails exactly the check it is named for (the v5 lesson: an un-updated fixture starts failing the *new* clause instead of its own) |
| `write_emit` records-shape assertion | the vendored JSON Schema cannot carry this net — its column-object shape is generic (`required: ["name", "type"]`), so it cannot require per-table v6 columns. `write_emit` therefore asserts the v6 records shape itself before writing: every records-category table entry classifies totally under the taxonomy (no *no-role* column), `record_index` sits in its slot, and each reference-annotated `prop__` entry is immediately followed by its `ref_index__` sibling. Failure is a construction-time error naming table + column. Negative fixtures whose declared defect *is* one of these shapes opt out explicitly, as a sibling of the existing `schema_valid=False` convention — the two nets stay independently addressable |
| Version literal | fixtures keep importing the single supported-version literal; gate negatives keep `UNSUPPORTED_VERSION_SENTINEL` |

### Invariants

Introduced:

- **Total classification.** Every records-category column classifies through the
  one taxonomy, and *no role* is loud everywhere: a recorded failure in
  conformance, a raised error in export planning. No consumer of records columns
  falls through on an unknown name.
- **Pair atomicity.** No corrupter operation writes one side of a
  `prop__`↔`ref_index__` pair — including by perturbation: jitter's reference
  exclusion is an explicit clause, not a type coincidence. Every pair in a
  corrupted emit is either faithful, or inconsistent *as declared by exactly one
  `DefectRecord`*.
- **Identity columns are never corruption-selectable** — `record_index` and
  `ref_index__*` join `record_id` / `fork_path`: invisible to family A,
  `schema_drift`, jitter, and resample by predicate construction, now stated and
  negatively tested.
- **Identity round-trip.** The corrupter base-emit writer regenerates identity
  columns as bare `{name, type}` sidecar entries — no `references`, no temporal
  pair.
- **Phase-1 output silence.** No exporter output and no `init` proposal contains
  `record_index` or `ref_index__*` unless the author names one explicitly in
  dimensional config.

Relied upon (must survive the change):

- Determinism: same emit + config + code → identical output, `defects.json`
  included.
- Manifest completeness: every injected semantic break is declared; the pair rule
  exists to preserve this under v6.
- The never-raises conformance rule: C5's new clauses record failures, never
  raise.
- The single version-literal and single-fixture-writer invariants: the bump moves
  one constant, and every well-formed fixture sidecar still flows through
  `write_emit` — whose new records-shape assertion (§ Fixtures), not the vendored
  JSON Schema (which cannot express per-table column requirements), is what turns
  a fixture that has not learned the v6 columns into a construction-time error
  naming the gap.

## Interface Contracts

### Reader — records-column taxonomy

```python
RecordsColumnRole = Literal["identity", "presentation", "lifecycle", "payload"]

REF_INDEX_PREFIX: Final[str] = "ref_index__"


def records_column_role(name: str) -> RecordsColumnRole | None:
    """
    Classify a records-category column name into its contract role.

    Pure and context-free: classification is by name family alone (§ Semantics —
    the records-column taxonomy). `None` means the name matches no v6
    records-category column family and is a loud condition at every call site —
    conformance records a C5 failure; an exporter raises. Callers MUST NOT treat
    `None` as "skip".

    Args:
        name: The column name as declared in the sidecar (or observed in the
            catalog) for a records-category table.

    Returns:
        The column's role, or None when the name matches no v6 records-category
        column family.
    """


def ref_index_sibling(prop_column_name: str) -> str:
    """
    The `ref_index__<name>` column name paired with `prop__<name>`.

    The pairing is a pure name rule; whether the sibling is *required* on a given
    table is determined by the `prop__` column's sidecar `references` field, not
    by this function.

    Args:
        prop_column_name: A `prop__`-prefixed records payload column name.

    Returns:
        The sibling identity column name (`ref_index__` + the property name).

    Raises:
        ValueError: `prop_column_name` is not `prop__`-prefixed.
    """
```

### Test support — identity-column constructor

```python
def identity_column(name: str, duckdb_type: str) -> dict[str, object]:
    """
    A sidecar column entry for an identity column.

    Sibling of `prop_column`: the sole constructor for identity fixture entries
    (`fork_path` / `record_id` / `record_index` / `ref_index__<name>`) — records
    and membership table entries alike; the check is a pure name rule, so a
    membership table's `fork_path` / `record_id` entries flow through it too.
    Emits a bare ``{"name", "type"}`` entry —
    a temporal attribute or `references` annotation on an identity column is
    inexpressible through it; negative variants mutate the returned dict.

    Args:
        name: The column name; must classify as `identity` under
            `records_column_role`.
        duckdb_type: The DuckDB type literal (`"BIGINT"` for both v6 families).

    Returns:
        The sidecar column entry dict.

    Raises:
        ValueError: `name` does not classify as `identity`.
    """
```

### Source exporter — classification error

```python
class SourceUnclassifiedColumn(ExportError):
    """
    A records-category column matched no records-column taxonomy role during
    source export planning.

    Raised at plan/validation time, before any output is written. Names the table
    and column. The exporter-side counterpart of C5's recorded failure: a contract
    column family forge does not know is an error, never a silent pass-through.
    """
```

*(`ExportError` is the existing base every source-mode validation error subclasses
directly — there is no source-specific intermediate class, and this design adds
none; the contract is: named error, plan-time, pre-output.)*

## Validation Rules

### Parse-Time (Pydantic)

None. No config model changes; this change has no author-facing configuration.

### Business Rules

| Rule | Checks | Error / failure |
|---|---|---|
| Version gate | `base_format_version == 6` (single supported-version literal) | `UnsupportedBaseFormatVersionError` (mechanism unchanged; value moves) |
| C5 (amended) | v6 records layout per § Semantics — C5 under v6 | recorded `CheckResult` failure naming table, column, position |
| Source plan classification | every records column of every planned unit classifies to a taxonomy role | `SourceUnclassifiedColumn` naming table + column |
| `identity_column` constructor | name classifies as `identity` | `ValueError` at fixture construction |
| `write_emit` records-shape assertion | records-category entries classify totally; v6 layout holds (`record_index` slot, `ref_index__` siblings) | construction-time error naming table + column (explicit opt-out for shape-defect negatives, sibling of `schema_valid`) |
| Corrupter pair rule | reference-writing operations write both pair cells; jitter eligibility excludes reference columns explicitly (engine-level; not author-visible) | test-guarded invariant, no new error surface |
| Base-emit writer round-trip | identity columns regenerate as bare `{name, type}` entries | test-guarded invariant |
