# Sprint: key-election

## Purpose

Ship author-elected identity surfaces per population — the `keys` config block from
`docs/architecture/pending/key-election.md` — so an educator's exported dataset keys
and references by the operational identifier a projection minted (`ALPHA_007`), not
the substrate `record_id` the contract forbids consumers to interpret.

## Scope

**Capabilities touched:**

- config: `ExportConfig.keys`, `FkClause.target_key` widened + inheritance default,
  parse-time validators
- shared exporter layer: election resolution + static gates + population spine +
  the render-time uniqueness guard (new `exporters/election.py`)
- derivations: presentation-key join relation (horizoned + end-of-tape)
- base mode: elective id-space value surface, per-edge target rendering, guard
- source mode: elected identity per genre, edge/junction rendering, guard
- dimensional mode: FK inheritance, `target_key: record_index`, population-set
  restriction, dim-key agreement, guard (both legs)
- `init` (dimensional only): self-gated `keys` proposal + aligned dim keys
- `declare_keys` interplay: PK follows the elected identity column

**Not included:** streaming payload identity election (deferred per the design doc),
source/base `init` (no such init capability exists; the doc's mode-aware proposal
prose activates if/when one lands), corrupter surfaces, Parquet, multi-branch.

## Design Doc

`docs/architecture/pending/key-election.md` (the doc) owns semantics, rationale, the
gate table, the rendering condition tables, and the § Interface Contracts set:
`KeySurface`, `ExportConfig.keys`, `FkClause.target_key`, `ElectedPopulation`,
`Election` (+ `surface_for` / `populations_for` / `is_default`), `resolve_election`,
`check_identity_election`, `check_edge_union_safety`,
`build_presentation_key_at_sql` / `build_presentation_key_at_end_sql`, and the eight
election errors. This spec does not restate them — implementers read them there.

`docs/sprints/key-election/contracts.md` (the companion, architect-authored) owns the
sprint-planning contracts the doc left open: `check_elected_key_unique`,
`build_population_spine_sql`, `DimSourcePopulations`,
`resolve_dim_source_populations`, `resolve_fk_surface`, module placement, guard call
sites, and the election-threading table. **One planning amendment to its § 5
threading, binding over it:** `build_source_plan`, `build_base_plan`, dimensional
`build_query_specs`, and `validate_table` gain `election: "Election | None" = None`
(keyword-only; `None` resolves the all-default election internally) instead of a
required parameter, and the extended internal spec dataclass fields
(`SourceTableSpec.identity_surface` etc.) carry default-election values. Rationale:
a required parameter forces an atomic migration of ~150 existing test call sites
across ~14 files for zero semantic gain; these are internal runtime surfaces, not
author config (Principle #7 governs author-facing config — the doc's own
`check_edge_union_safety` contracts `surface_override: KeySurface | None = None`).
`build_fk_expr`'s new `resolved_surface` / `dim_populations` parameters stay
**required**: a defaulted surface there would silently mask an explicit
`target_key`. Callers that hold `ExportConfig.keys` (the three engines, the
incremental driver, tier-2 shaped playback) must resolve and pass the election —
`None` is for election-free callers only.

## Breaking Changes

- **`fk.target_key: presentation_id` (dimensional) is subsumed** (doc § What Doesn't
  Change): its identity relation becomes restricted to the destination dim's source
  population set (out-of-set target renders `NULL` where an orphan value rendered
  verbatim), and its column-presence check (`fk.py:659-678`, deleted) becomes the
  statically-earlier registry-membership gate. Applies with or without a `keys`
  block. Escapes: `target_key: record_id` or a whole-kind dim.
- **`FkClause.target_key` default changes** from `"record_id"` to `None`
  (= inherit the destination dim's source population set's election). Without a
  `keys` block every population elects `record_id`, so inheritance resolves
  `record_id` — existing configs behave identically except the subsumption above.
- Internal signatures: `build_source_plan` / `build_base_plan` / dimensional
  `build_query_specs` / `validate_table` gain the optional `election` parameter;
  `build_fk_expr` gains two required parameters. Internal only (Principle #9).

Everything else: an absent `keys` block reproduces today's output byte-for-byte.

## Success Criteria

- [ ] `keys: {entity: presentation_id}` on a fully-declared kind renders `ALPHA_…`
      codes as the identity column and in every referencing column, in all three
      modes.
- [ ] All nine static gates fire per the doc's gate table, at load/plan time,
      data-free, with the doc's error types and named remedies.
- [ ] The uniqueness guard fails an export loudly (three-way equality + non-NULL,
      over join relations, never output rows) on a corrupted elected key.
- [ ] No `keys` block ⇒ byte-identical output in every mode (except the owned
      `target_key: presentation_id` subsumption).
- [ ] Dimensional FK edges inherit the destination dim's election; dim-key
      agreement is enforced statically; `target_key: record_index` works.
- [ ] `init` (dimensional) proposes a self-gated `keys` block with degradation
      comments; proposals never fail their own gates.
- [ ] `declare_keys` + election: PK follows the elected identity column; absorbed/
      dropped columns' side claims are not declared.
- [ ] `make test` green after every phase; pre-commit clean.

## Contracts

Doc-owned contracts: `key-election.md` § Interface Contracts (config models, runtime
types, shared functions, derivations functions, errors) and § Validation Rules.
Sprint-planning contracts: `contracts.md` §§ 1–4 (guard, spine, dim populations,
FK restriction shape) and § 5 threading table as amended above. No contracts are
restated here; the two files are the authority.

## Phases

### Phase 1: Config surface + election errors

**Delivers:** `KeySurface`, `ExportConfig.keys` + `keys_well_formed`,
`FkClause.target_key: KeySurface | None = None`, and the eight `Election*` /
`ElectedKeyDuplicate` error classes under `ExportError`.
**Demo:** Parses election-bearing configs (scalar, per-sub-type map, dimensional
`target_key: record_index`); shows each parse-time refusal (empty `keys`, empty
per-kind map, non-surface value) and that emit-dependent checks are deliberately
absent at parse time.
**Contracts:** doc § Config Models, § Errors, § Validation Rules (Parse-Time).
**Steps:** none (single implementer).

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `tests/config/test_models.py` |
| Modify | `tests/test_errors.py` |
| Create | `docs/sprints/key-election/demos/phase_1_config_surface.py` |

**Tests:**

- `keys` absent → parses, field is `None`; `keys: {}` → `ValueError`.
- Scalar election parses for each of the three surfaces; per-sub-type map parses;
  empty per-kind map (`entity: {}`) → `ValueError`; non-surface scalar/map value
  (`entity: uuid`) → validation error.
- `keys` accepts a kind name Pydantic can't check against any emit — kind/sub-type
  existence is NOT a parse-time error (emit-independence).
- `fk.target_key: record_index` parses; `target_key` absent → `None` (not
  `"record_id"`); invalid literal refused.
- Each new error class subclasses `ExportError` and lands in the hierarchy test;
  `tests/config/test_docstring_convention.py` stays green (new fields docstringed).
- Existing: full `tests/config/` suite unchanged and green.

### Phase 2: Presentation-key derivation

**Delivers:** `derivations/presentation_key.py` — `build_presentation_key_at_sql`,
`build_presentation_key_at_end_sql`, `PRESENTATION_KEY_COLUMNS`; the exact sibling
of `derivations/record_index.py`.
**Demo:** Builds an emit with declared and undeclared populations plus an
exactly-duplicated corrupted row; prints the relation at a mid-tape horizon, at
end-of-tape, and shows DISTINCT collapsing the duplicate and NULL
`presentation_id` projecting verbatim.
**Contracts:** doc § Functions (derivations layer).
**Steps:** none (single implementer).

**Files:**

| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/derivations/presentation_key.py` |
| Create | `tests/derivations/test_presentation_key.py` |
| Create | `docs/sprints/key-election/demos/phase_2_presentation_key_relation.py` |

**Tests** (mirroring `tests/derivations/test_record_index.py`):

- Horizon membership is strict `<`: created-before included, created-at excluded,
  created-after excluded.
- Deactivated record still present (`active` never a predicate).
- Verbatim `(record_id, presentation_id)` projection; NULL `presentation_id` rows
  project verbatim.
- DISTINCT collapses an exactly-duplicated row; two relation rows with distinct
  values for one `record_id` are both kept (the guard's problem, not the relation's).
- `fork_path` filter honored; no `ORDER BY` in either builder's SQL (string
  assertion).
- End-of-tape SQL carries no `created_sim_time` predicate; result equals a
  far-horizon call.
- Unknown kind → `TableNotFoundError` (both builders); records table without a
  `presentation_id` column → `ExportError` (both builders).

### Phase 3: Election resolution, gates, spine, guard

**Delivers:** `exporters/election.py` — `ElectedPopulation`, `Election`,
`resolve_election`, `check_identity_election`, `check_edge_union_safety`,
`build_population_spine_sql`, `check_elected_key_unique`. Mode-neutral; imports per
`contracts.md` module-placement table.
**Demo:** Resolves elections against a declared emit (prints the total per-kind
view, synthesized key spaces); then fires each gate live: unknown kind, map on a
flat kind, undeclared `presentation_id`, mixed identity, bare-counter union
unsafety, and the guard catching a duplicated-then-mutated elected key.
**Contracts:** doc § Runtime Types + § Functions (shared exporter layer);
`contracts.md` §§ 1–2.
**Steps:** none (single implementer).

**Files:**

| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/exporters/election.py` |
| Create | `tests/exporters/test_election.py` |
| Create | `docs/sprints/key-election/demos/phase_3_election_gates.py` |

**Tests:**

- `resolve_election(sidecar, None)` → total all-`record_id` view; `is_default` true
  for every kind.
- Scalar on flat kind; scalar shorthand on sub-typed kind resolves every declared
  sub-type; partial map leaves unlisted sub-types at `record_id`; declaration order
  in `populations_for`.
- Gates: unknown kind → `ElectionKindUnknown`; map key outside domain and map on a
  flat kind → `ElectionSubTypeUnknown`; `presentation_id` without registry entry →
  `ElectionPresentationUndeclared` (message distinguishes absent block); uniform
  shorthand requires every domain sub-type declared; incoherent registry block →
  `PresentationKeysInvalidError` propagates only when some population elects
  `presentation_id`.
- Synthesized key spaces: `record_id` class, `record_index` class with
  `prefix ""` / `width 0`; algebra verdicts transfer (`record_id` unsafe beside
  digit-rendered and `uuid`; `""` prefix-incomparable with `ALPHA_`).
- `check_identity_election`: same-surface passes; mixed surfaces →
  `ElectionMixedIdentity`; uniform `presentation_id` over bare-counter siblings →
  `ElectionUnionUnsafe`; single-population call passes trivially.
- `check_edge_union_safety`: partial-map default `record_id` beside a
  digit-rendered election → `ElectionUnionUnsafe`; `record_index` beside prefixed
  spaces passes; `surface_override=presentation_id` with an uncovered population →
  `ElectionPresentationUndeclared`; absent target kind → `KeyError`.
- `Election.surface_for` / `populations_for` raise `KeyError` per the doc.
- `build_population_spine_sql`: composes the reader records relation (never a raw
  table name), discriminator `IN` list with quote-doubling, order preserved;
  refuses empty set, full domain, out-of-domain value, non-sub-typed kind.
- `check_elected_key_unique`: passes on a conformant relation; fails on NULL inside
  the consumed set; fails the three-way equality on a duplicated-row +
  mutated-value shape (two relation rows, one `record_id`, distinct values); the
  spine restriction excludes an out-of-set violation; error message carries all
  four counts, the context label, and the surface.

### Phase 4: Base mode election

**Delivers:** Base renders the elected id-space value surface beside the always-on
index keys; per-edge `prop__` columns render target elections (drop under all-
`record_index`, per-row mix only for `exclude`d target kinds); absorption; rename
keyed on the elected surface's contract column name; identity + edge gates at plan
time; guard calls in the engine; `declare_keys` follows the elected column.
**Demo:** The flagship shape — a fully-declared two-kind emit exported `mode: base`
with `keys: presentation_id`: `id` carries operational codes, the referencing
`prop__` column renders the target's codes, the standalone `presentation_id` column
is absorbed; then `record_index` election dropping the id-space column; then no
`keys` → byte-identical to a pre-election export; then a corrupted elected key
failing the guard loudly.
**Contracts:** doc § Rendering per mode (Base) + § Interplay (`declare_keys`);
`contracts.md` § 5 rows for `build_base_plan` / `BaseTableSpec` / `ReferenceKey` /
`build_base_render_sql` / `resolve_base_table_keys`, § 1 call-site row for
`build_base_query_specs`.
**Steps:** `source → author` (mixed work-shapes; existing tests stay green —
default election is byte-identical — so no migrate step).

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/base/plan.py` |
| Modify | `src/fabulexa_forge/exporters/base/renders.py` |
| Modify | `src/fabulexa_forge/exporters/base/engine.py` |
| Modify | `tests/exporters/base/_base_fixtures.py` |
| Create | `tests/exporters/base/test_election_plan.py` |
| Create | `tests/exporters/base/test_election_renders.py` |
| Create | `docs/sprints/key-election/demos/phase_4_base_election.py` |

**Tests:**

- Plan: `identity_surface` stamped per kind; sub-typed kind spanning mixed
  elections → `ElectionMixedIdentity` (base never splits); uniform
  `presentation_id` over union-unsafe siblings → `ElectionUnionUnsafe`; edge gate
  runs per `ReferenceKey` over the target kind's full domain; target kind absent
  from the emit skipped (renders verbatim, no gate).
- Self columns per the doc's table: `record_id` → today's pair byte-identical;
  `presentation_id` → elected value in the id slot (default name `id`, rename key
  `presentation_id`), standalone payload column absorbed; `record_index` → id-space
  column dropped, `<kind>_key` only.
- Edge columns per the doc's table: uniform `presentation_id` targets render codes
  with `<p>_key` unchanged; all-`record_index` targets drop `prop__<p>`;
  `exclude`d mixed-election target renders per row, digit-rendered `record_index`
  in a `VARCHAR` column.
- Elected edge value condition table: absent property, pre-horizon target,
  at-or-after-horizon target, dangled sentinel → the doc's four rows.
- Rename keyed on an absorbed or dropped column → unsatisfiable-rename error;
  rename keyed on `presentation_id` renames the elected id column.
- `resolve_base_table_keys`: PK is `<kind>_key` under `record_index`; PK follows
  the elected identity column under `presentation_id` (PK-eligible, superseding
  always-`UNIQUE` for that column alone); absorbed/dropped columns' side `UNIQUE`
  not declared; no-election resolution unchanged.
- Engine: guard invoked per composed relation (self when non-`record_id`; per edge
  per admitted subset electing that surface, spine iff proper subset); corrupted
  elected key fails `build_base_query_specs` before any writer runs; per-window
  guard under an incremental invocation.
- Existing: `tests/exporters/base/` suite green unchanged (no-election paths
  byte-identical).

### Phase 5: Source mode election

**Delivers:** Every source genre renders the elected identity surface as `id`
(change-log via post-fold join, populated on `d` rows); reference `prop__`,
junction owner, and junction member columns render target elections with the
mixed-column type rule; absorption; rename addressing; identity gate for unsplit
multi-population tables; edge gates; guard calls; `declare_keys` follows the
elected column.
**Demo:** A change-log export with `keys: presentation_id` showing `id` carrying
codes on `c`/`u`/`d` rows alike; a reference table's `prop__` column rendering the
target's codes; a junction with per-member-kind elections and the `<f>_kind`
disambiguator; a mixed-election kind's edge column rendering per target row
(`ALPHA_…` beside digit-rendered indices in one `VARCHAR` column); no `keys` →
byte-identical.
**Contracts:** doc § Rendering per mode (Source) + § Per-row population resolution
+ § Mixed-election edge columns; `contracts.md` § 5 rows for `build_source_plan` /
`SourceTableSpec` / the four renders / `resolve_source_table_keys`, § 1 call-site
row for `build_source_query_specs`.
**Steps:** `source → author` (mixed work-shapes; existing tests stay green, no
migrate step).

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/source/renders.py` |
| Modify | `src/fabulexa_forge/exporters/source/engine.py` |
| Modify | `tests/exporters/source/_source_fixtures.py` |
| Create | `tests/exporters/source/test_election_plan.py` |
| Create | `tests/exporters/source/test_election_renders.py` |
| Create | `docs/sprints/key-election/demos/phase_5_source_election.py` |

**Tests:**

- Plan: split (untracked sub-typed) kinds may elect per population — each unit
  stamps its own `identity_surface`; an unsplit tracked sub-typed kind with mixed
  elections → `ElectionMixedIdentity`; edge gates per reference-annotated column,
  junction owner column, and per junction member kind.
- `id` rendering per genre: reference/transaction/snapshot via the identity join at
  the table's horizon; change-log via post-fold join on the fold's `record_id` —
  identity populated on `d` rows (superseding the after-image's NULL-on-`d`);
  `record_index` election renders `BIGINT`, `presentation_id` the declared type;
  absorption of the standalone payload column under `presentation_id` only.
- The fold itself untouched: row-state-events SQL byte-identical (string-level
  assertion against a no-election render).
- Edge rendering: reference `prop__<p>` → `<p>` renders the target's election at
  the table's horizon; junction owner column follows the owner kind's election;
  junction member column per member row's kind, `<f>_kind` disambiguating;
  mixed-column type rule — common declared type kept, else `VARCHAR` with
  digit-rendered `record_index` (junction member columns over the union of member
  kinds' surfaces).
- Per-row population resolution reads the records-spine discriminator, never the
  fold after-image (a `d`-row's target still resolves).
- Rename: id column's rename key is the elected surface's contract column name;
  rename keyed on an absorbed column errors (`SourceRenameSliceOnly` posture).
- `resolve_source_table_keys`: PK follows the elected identity column; genre
  eligibility unchanged (change-log and junction still declare none).
- Engine: guard per composed relation incl. junction owner and per-member-kind
  relations; split-unit identity guard restricted by the unit's sub-type spine.
- Existing: `tests/exporters/source/` suite green unchanged.

### Phase 6: Dimensional election

**Delivers:** `populations.py` (`DimSourcePopulations`,
`resolve_dim_source_populations`, `resolve_fk_surface`); FK edges inherit the
destination dim's source population set's election; explicit `target_key` override
incl. `record_index`; identity relations restricted to the dim's population set
(the `target_key: presentation_id` subsumption); dim-key agreement check; edge
gates under `surface_override`; guard calls (FK relations + dim-side leg);
election threading through `validate_table` / `build_query_specs` / callers.
**Demo:** A star over a declared kind: the FK inherits and renders `ALPHA_…`
against a dim keyed on `presentation_id`; an explicit `target_key: record_index`
override beside it; an out-of-set target rendering `NULL` under a
discriminator-filtered dim; `ElectionDimKeyDisagrees` and
`ElectionInheritanceAmbiguous` firing statically; the pre-election
`target_key: presentation_id` subsumption shown with no `keys` block.
**Contracts:** doc § Rendering per mode (Dimensional) + § Static gates (dimensional
rows); `contracts.md` § 3, § 4, § 5 rows for `build_query_specs` / `validate_table`
/ `build_fk_expr`, § 1 call-site row (a) + (b).
**Steps:** `source → author` (source reshape + intent-changing `test_fk.py`
rewrites for the subsumption; the rewrites are judgment work against the doc, not
mechanical migration).

**Files:**

| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/exporters/dimensional/populations.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/fk.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/validation.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/engine.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/columns.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/grains.py` |
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Modify | `src/fabulexa_forge/playback/shaped.py` |
| Modify | `tests/exporters/dimensional/test_fk.py` |
| Create | `tests/exporters/dimensional/test_populations.py` |
| Create | `tests/exporters/dimensional/test_election_fk.py` |
| Create | `docs/sprints/key-election/demos/phase_6_dimensional_election.py` |

**Tests:**

- `resolve_dim_source_populations`: flat kind → `(None,)`; discriminator conjunct →
  singleton + `proper_subset`; no conjunct on sub-typed kind → full domain;
  discriminator conjunct on a non-sub-typed kind treated as ordinary column
  conjunct; out-of-domain conjunct value → `ExportError`.
- `resolve_fk_surface`: explicit override wins; inheritance over a one-election
  set; mixed set without override → `ElectionInheritanceAmbiguous`; no election +
  no override → `record_id`.
- FK rendering: inherited `presentation_id` renders codes via the restricted
  relation; `target_key: record_index` renders `BIGINT` indices; out-of-set target
  → `NULL` (all four `via` builders); the doc's FK condition table (absent /
  in-set / out-of-set / dangled → the four rows); `correlation:` columns stay
  verbatim `record_id`-space under any election.
- Subsumption (no `keys` block): `target_key: presentation_id` over a
  discriminator-filtered dim restricts to the dim's population set; an undeclared
  population in the set → `ElectionPresentationUndeclared` (the old
  column-presence `ExportError` is gone); existing `test_fk.py` presentation-id
  cases rewritten to the new posture.
- Dim-key agreement: inherited non-default surface with no dim key column sourcing
  the elected contract column → `ElectionDimKeyDisagrees`; explicit `target_key`
  on the edge escapes it; combined mixed-election dim legal on its own, every
  inbound edge explicit.
- Edge gate under override: union-unsafe admitted pair → `ElectionUnionUnsafe`.
- Engine: guard over each FK's composed relation (spine iff `proper_subset`) and
  the dim-side leg exactly when an inbound edge's resolved non-`record_id` surface
  is also projected by the dim's declared key; per-window under incremental.
- Threading: incremental driver and tier-2 shaped playback resolve
  `ExportConfig.keys` and pass the election (a keyed windowed export and a keyed
  `state()` compile render elections; `keys` participates in the config
  fingerprint as an ordinary field).
- Existing: full dimensional suite green except the deliberately rewritten
  `test_fk.py` subsumption cases.

### Phase 7: `init` keys proposal (dimensional)

**Delivers:** `generate_init_config` additionally proposes the `keys` block —
`presentation_id` where the registry declares the population, `record_index`
elsewhere; scalar/map shape mirroring the registry; self-gated through
`resolve_election` + the dimensional plan gates with degradation to uniform
`record_index` and a YAML comment naming the forcing gate; dim key proposals
source `from:` the elected surface's contract column (subsuming the natural-key
advisory where the election is `presentation_id`); FK candidates remain comments,
`target_key`-free.
**Demo:** `init` against a fully-declared emit (clean `presentation_id` proposal,
aligned dim keys), a partially-declared emit (per-sub-type map with
`record_index` fallback), and a bare-counter-siblings emit (degradation comment
naming the union-safety gate).
**Contracts:** doc § `init` proposals; `contracts.md` § 5 `generate_init_config`
row (signature unchanged, self-gate internal).
**Steps:** none (single implementer).

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/dimensional/init.py` |
| Modify | `tests/test_cli_init.py` |
| Create | `docs/sprints/key-election/demos/phase_7_init_keys.py` |

**Tests:**

- Fully-declared kind → scalar `presentation_id`; undeclared populations →
  `record_index`; partitioned kind proposes the map; all-agreeing map collapses to
  the scalar.
- Self-gate: bare-counter siblings degrade the kind to uniform `record_index` with
  a comment naming the gate; a proposal never fails its own gates (run the emitted
  YAML back through `resolve_election` + the dimensional gates in the test).
- Dim key alignment: proposed dim key column sources `from: presentation_id` where
  elected, keeping its shipped name; the natural-key advisory comment is subsumed
  exactly there and retained elsewhere; FK candidates remain comments without
  `target_key`.
- Incoherent registry block → init refuses (strict-accessor sharing).
- Existing: `tests/test_cli_init.py` non-keys cases green unchanged.

## What Doesn't Change

Per the doc's § What Doesn't Change, held as review boundaries:

- **Streaming** — `StreamConfig`, CDC events, Kafka keying, the mixer: read none of
  this. No streaming file is touched.
- **The reader and the C-set** — `Sidecar.presentation_keys()`, `union_safe`,
  `combined_claim`, conformance C1–C14: consumed as shipped. No `reader/` file is
  touched.
- **The record-index derivation** — `derivations/record_index.py` untouched;
  election adds a sibling, never edits it.
- **The row-state-events fold** — `derivations/row_state_events.py` untouched;
  elected identity is joined post-fold.
- **Base's key-column contract** — `<kind>_key` / `<p>_key` naming, horizon
  binding, edge-keys-re-derived: untouched semantics.
- **Dimensional's author-declared grammar** — `TableDecl.key` / `role` / grains /
  column modes stay author-owned; `correlation:` columns verbatim under any
  election.
- **`QuerySpec` / `TableKeys` / `write_query_specs` / the writers** — unchanged;
  the guard runs in the engines, never the writers.
- **Corrupters and `defects.json`** — no corrupter file is touched.
- **Notices** — no new notice codes; election failure modes are errors.

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/config/models.py` | `KeySurface`, `ExportConfig.keys` + validator, `FkClause.target_key: KeySurface \| None = None` |
| `src/fabulexa_forge/errors.py` | Eight election error classes under `ExportError` |
| `src/fabulexa_forge/derivations/presentation_key.py` | New: presentation-key join relation (horizoned + end-of-tape) |
| `src/fabulexa_forge/exporters/election.py` | New: election resolution, static gates, population spine, uniqueness guard |
| `src/fabulexa_forge/exporters/base/plan.py` | Election param, identity/edge gates, spec fields, rename keying, `declare_keys` interplay |
| `src/fabulexa_forge/exporters/base/renders.py` | Presentation-key join sibling, elected self/edge column rendering |
| `src/fabulexa_forge/exporters/base/engine.py` | Election resolution, guard calls per composed relation |
| `src/fabulexa_forge/exporters/source/plan.py` | Election param, gates, spec fields, rename keying, `declare_keys` interplay |
| `src/fabulexa_forge/exporters/source/renders.py` | Elected identity per genre (post-fold join for change-log), edge/junction rendering, mixed type rule |
| `src/fabulexa_forge/exporters/source/engine.py` | Election resolution, guard calls |
| `src/fabulexa_forge/exporters/dimensional/populations.py` | New: dim source population set + FK surface resolution |
| `src/fabulexa_forge/exporters/dimensional/fk.py` | Shared surface dispatch in the four builders, restricted relations, presence check deleted |
| `src/fabulexa_forge/exporters/dimensional/validation.py` | Election threading, edge gates under override, dim-key agreement check |
| `src/fabulexa_forge/exporters/dimensional/engine.py` | Election param, guard calls (FK + dim-side leg) |
| `src/fabulexa_forge/exporters/dimensional/columns.py` | Thread resolved surface + populations into `build_fk_expr` |
| `src/fabulexa_forge/exporters/dimensional/grains.py` | Thread resolved values along the grain build path |
| `src/fabulexa_forge/exporters/dimensional/init.py` | Self-gated `keys` proposal, aligned dim keys |
| `src/fabulexa_forge/incremental/driver.py` | Resolve `ExportConfig.keys`, pass election to dimensional specs |
| `src/fabulexa_forge/playback/shaped.py` | Resolve `ExportConfig.keys`, pass election in tier-2 compiles |
| `tests/config/test_models.py` | Keys/target_key parse cases |
| `tests/test_errors.py` | New error classes in hierarchy |
| `tests/derivations/test_presentation_key.py` | New: derivation suite |
| `tests/exporters/test_election.py` | New: resolution, gates, spine, guard |
| `tests/exporters/base/_base_fixtures.py` | Presentation-keys-bearing fixture emit |
| `tests/exporters/base/test_election_plan.py` | New: base plan gates + keys interplay |
| `tests/exporters/base/test_election_renders.py` | New: base rendering + guard |
| `tests/exporters/source/_source_fixtures.py` | Presentation-keys-bearing fixture emit |
| `tests/exporters/source/test_election_plan.py` | New: source plan gates + keys interplay |
| `tests/exporters/source/test_election_renders.py` | New: source rendering + guard |
| `tests/exporters/dimensional/test_fk.py` | Subsumption rewrites for `target_key: presentation_id` |
| `tests/exporters/dimensional/test_populations.py` | New: population set + surface resolution |
| `tests/exporters/dimensional/test_election_fk.py` | New: inheritance, restriction, agreement, guard |
| `tests/test_cli_init.py` | Keys proposal cases |
