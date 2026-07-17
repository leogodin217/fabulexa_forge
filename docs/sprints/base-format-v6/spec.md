# Sprint: base-format-v6

Design doc: `docs/architecture/pending/base-format-v6.md` (rationale + semantics —
the WHY; this spec carries contracts, phases, and test cases — the WHAT). The
vendored contract (`contract/base-format.md` § Dense record index) is authoritative
wherever restatements differ.

## Purpose

Forge reads, validates, exports, and corrupts `base_format_version: 6` emits — the
forced compatibility phase of the dense-record-index bump, with zero elective use of
the new columns. An educator's existing configs keep working unchanged against v6
emits: exports carry no new columns, `init` proposals get *cleaner* (role-scoped),
and corrupted emits stay fully declared in `defects.json`.

## Scope

**Capabilities touched:**

- reader: supported-version literal 5 → 6; new records-column taxonomy
  (`records_column_role`, `ref_index_sibling`) — the single classifier every
  records-column consumer reads through
- conformance: C5 amended to the v6 layout; C5's redundant catalog `prop__`-block
  re-check dropped (C2 becomes sole catalog↔sidecar carrier)
- source exporter: all renders classify through the taxonomy; identity columns
  dropped; `SourceUnclassifiedColumn` plan-time error
- dimensional `init`: proposals role-scoped to payload + presentation
- corrupters: pair-scoped reference writes; `insert_rows` fresh-index high-water
  mark; jitter explicit reference exclusion; identity round-trip + never-selectable
  stated invariants
- test fixtures/support: `identity_column` constructor; `write_emit` records-shape
  assertion; v6-shaped spanning fixture (adversarial id-shape mix); negative-fixture
  audit; new C5 negatives

**Not included** (each per design doc § What Doesn't Change): every elective index
use (source integer PK/FK presentation, dimensional surrogate keys, point-in-time
joins over `ref_index__`, split-brain corrupter ops); no new conformance check;
membership / `history` / fixed-table handling; derivations; streaming, pacing,
routing, mixer, anchor, incremental, writers; config surface and CLI; the
version-gate mechanism. Architecture-doc promotion (`pending/` → live) ships
separately post-ACCEPT.

## Breaking Changes

- **v5 emits are refused.** `SUPPORTED_BASE_FORMAT_VERSION` moves 5 → 6; the gate
  is equality against the single literal (no auto-upgrade, mechanism unchanged).
- **`init` proposals shrink.** `created_sim_time` / `last_mutation_sim_time` were
  proposed only by enumeration accident; role-scoping removes them. Existing
  *generated* configs keep working — explicit author projection of any base column
  still exports faithfully.
- **C5 narrows to sidecar-shape only.** Its catalog `prop__`-block re-check is
  dropped, not extended; C2's element-wise catalog↔sidecar agreement is the sole
  catalog carrier.
- **Test support:** `write_emit` gains a v6 records-shape assertion — fixtures that
  have not learned the v6 columns become construction-time errors (explicit opt-out
  for shape-defect negatives).
- **No author-facing config change.** No new fields in any envelope; no CLI change.

## Success Criteria

- [ ] `fabulexa-forge validate docs/examples/parent-child/published` passes C1–C13
      (the example is a real v6 emit)
- [ ] No exporter output and no `init` proposal contains `record_index` /
      `ref_index__*` (Phase-1 output silence)
- [ ] Every records-column consumer classifies through the one taxonomy; a no-role
      column is a recorded C5 failure / raised `SourceUnclassifiedColumn` — never a
      fall-through
- [ ] Every corrupter reference-write is pair-scoped: no operation leaves a
      `prop__`↔`ref_index__` pair inconsistent without exactly one `DefectRecord`
- [ ] All recipe `expect.yaml` ground truth is stable except the named `init` delta
      (verified by the suite, not re-baselined)
- [ ] Full suite green: `make test`

## Contracts

New interfaces (verbatim from the design doc § Interface Contracts):

### Reader — `src/fabulexa_forge/reader/records_columns.py` (new module)

```python
RecordsColumnRole = Literal["identity", "presentation", "lifecycle", "payload"]

REF_INDEX_PREFIX: Final[str] = "ref_index__"


def records_column_role(name: str) -> RecordsColumnRole | None:
    """
    Classify a records-category column name into its contract role.

    Pure and context-free: classification is by name family alone (design doc
    § Semantics — the records-column taxonomy). `None` means the name matches no
    v6 records-category column family and is a loud condition at every call
    site — conformance records a C5 failure; an exporter raises. Callers MUST
    NOT treat `None` as "skip".

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

    The pairing is a pure name rule; whether the sibling is *required* on a
    given table is determined by the `prop__` column's sidecar `references`
    field, not by this function.

    Args:
        prop_column_name: A `prop__`-prefixed records payload column name.

    Returns:
        The sibling identity column name (`ref_index__` + the property name).

    Raises:
        ValueError: `prop_column_name` is not `prop__`-prefixed.
    """
```

Role table (total, context-free): `fork_path` / `record_id` / `record_index` /
`ref_index__<name>` → `identity`; `presentation_id` → `presentation`;
`created_sim_time` / `active` / `deactivated_at` / `last_mutation_sim_time` →
`lifecycle`; `prop__<name>` → `payload`; anything else → **no role**.

### Test support — `tests/_support/sidecar_builder.py`

```python
def identity_column(name: str, duckdb_type: str) -> dict[str, object]:
    """
    A sidecar column entry for an identity column.

    Sibling of `prop_column`: the sole constructor for identity fixture entries
    (`fork_path` / `record_id` / `record_index` / `ref_index__<name>`) — records
    and membership table entries alike; the check is a pure name rule, so a
    membership table's `fork_path` / `record_id` entries flow through it too.
    Emits a bare ``{"name", "type"}`` entry — a temporal attribute or
    `references` annotation on an identity column is inexpressible through it;
    negative variants mutate the returned dict.

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

### Source exporter — `src/fabulexa_forge/errors.py`

```python
class SourceUnclassifiedColumn(ExportError):
    """
    A records-category column matched no records-column taxonomy role during
    source export planning.

    Raised at plan/validation time, before any output is written. Names the
    table and column. The exporter-side counterpart of C5's recorded failure: a
    contract column family forge does not know is an error, never a silent
    pass-through.
    """
```

(`ExportError` is the existing base every source-mode validation error subclasses
directly; no intermediate class is added.)

### Modified behavior (docstring-level; implementer writes the code)

- `_check_c5_table` (`reader/conformance.py`): validates the v6 sidecar layout per
  design doc § C5 under v6 — `record_index` (`BIGINT`) in the slot immediately
  after the possibly-shifted lifecycle prefix; property block is, in declaration
  order, `prop__<name>` immediately followed by `ref_index__<name>` iff that
  entry carries `references`; `ref_index__` type `BIGINT`; any no-role column
  anywhere fails; any column after `record_index` that is neither `prop__` nor a
  paired `ref_index__` fails; classification flows through
  `records_column_role`. Failures are recorded `CheckResult` messages naming
  table, column, position — never raised. The catalog `prop__`-block re-check is
  removed. Nullability is not compared (existing C2/C5 stance).
- `write_emit` (`tests/_support/sidecar_builder.py`): before writing, asserts every
  records-category table entry classifies totally under the taxonomy (no no-role
  column), `record_index` sits in its slot, and each reference-annotated `prop__`
  entry is immediately followed by its `ref_index__` sibling. Failure is a
  construction-time error naming table + column. Shape-defect negatives opt out
  explicitly via a new flag, a sibling of the existing `schema_valid=False`
  convention — the two nets stay independently addressable.
- Source plan column resolvers (`exporters/source/plan.py` — `_records_columns`,
  `_changelog_columns`, `_snapshot_columns`, `_default_columns`): classify every
  records column through `records_column_role`; identity role → dropped (the
  `fork_path` precedent — not addressable by `rename`); presentation kept;
  lifecycle per existing defaults (`_LIFECYCLE_RENAMES` unchanged); payload per
  existing defaults; no role → raise `SourceUnclassifiedColumn`. All four genres
  agree on the posture.
- `init` proposal loop (`exporters/dimensional/init.py`): the ad-hoc skip list
  (`fork_path` / `record_id` / `active` / `deactivated_at`) is replaced by
  role-scoping — propose payload + presentation columns only; identity and
  lifecycle are never proposed (`valid_from` / `valid_to` stay `history`-derived).
- `null_cells` / `dangle_reference` / `mispoint_reference`
  (`corrupters/operations/`): a write to a records reference `prop__` cell writes
  the sibling `ref_index__` cell in the same act — co-null / co-dangle
  (`-(n + 1)` from the same per-kind sentinel suffix `n`) / co-point (donor's
  `record_index`, read from the same operation-start working state as the donor
  pool). One defect, one `DefectRecord`; locator stays the `prop__` cell; `class`
  and `impact` computed exactly as today. Membership reference writes are
  unchanged (no `ref_index` analog).
- `insert_rows` (`corrupters/operations/insert_rows.py` + `corrupters/state.py`):
  mints a fresh `record_index` per phantom — per-table ordinal high-water mark
  `+ 1 + i` in assignment order (ascending selected-unit order, matching the id
  discipline). The high-water mark is engine state per `records__<K>` working
  table: initialized to the table's maximum `record_index` at working-set load
  (`rows − 1` by input density), advanced past each minted phantom, never
  lowered — deletion gaps, suffix gaps included, are never reused. Donor
  `ref_index__` cells clone verbatim.
- `is_jitter_eligible` (`corrupters/validate.py`): gains an explicit
  reference-exclusion clause — a records reference `prop__` column is
  jitter-ineligible by declared rule, not by the numeric-type coincidence.

## Phases

### Phase 1: Records-column taxonomy + posture ports (green at v5)

**Delivers:** The taxonomy classifier on the reader, the source exporter and
dimensional `init` classifying through it, and the `init` role-scoping behavior
change. At v5 the identity index families do not occur, so exporter output is
byte-identical; only the `init` proposal delta is observable — everything lands
green before the flip, which is what keeps the Phase-2 migration mechanical.

**Demo:** `phase_1_taxonomy_posture.py` — builds a small v5 emit inline, prints the
taxonomy classification for every column family (including a no-role name → the
raised `SourceUnclassifiedColumn`), and shows `init` proposals now carry payload +
presentation only (`created_sim_time` / `last_mutation_sim_time` gone).

**Contracts:** `records_column_role`, `ref_index_sibling`, `REF_INDEX_PREFIX`,
`RecordsColumnRole`, `SourceUnclassifiedColumn`; modified source plan resolvers and
`init` proposal loop.

**Steps:** none (single implementer)

**Files:**

| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/reader/records_columns.py` |
| Modify | `src/fabulexa_forge/reader/__init__.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/init.py` |
| Create | `tests/reader/test_records_columns.py` |
| Modify | `tests/exporters/source/test_plan.py` |
| Modify | `tests/test_cli_init.py` |
| Create | `docs/sprints/base-format-v6/demos/phase_1_taxonomy_posture.py` |

**Tests:**

- `records_column_role`: each family classifies per the role table (`fork_path`,
  `record_id`, `record_index`, `ref_index__x` → identity; `presentation_id` →
  presentation; all four lifecycle names; `prop__x` → payload)
- `records_column_role` returns `None` for `member__x`, `history`-style names,
  `""`, `props__x`, `ref_index_x` (single underscore) — no fuzzy matching
- `ref_index_sibling("prop__group") == "ref_index__group"`; a non-`prop__` name
  raises `ValueError`
- Source plan over an emit whose records table carries an unknown column raises
  `SourceUnclassifiedColumn` naming table + column, before any output
- Source plan output column lists are unchanged for v5-shaped emits (existing
  `test_plan.py` / `test_renders.py` assertions still pass, unmodified)
- `init` proposals contain payload + presentation columns only;
  `created_sim_time` / `last_mutation_sim_time` absent (updated inline
  expectations in `test_cli_init.py` — the deliberate delta, and exactly it)
- Existing source recipes (`examples/recipes/source/*/expect.yaml`) still pass
  untouched

### Phase 2: The v6 flip (atomic)

**Delivers:** `SUPPORTED_BASE_FORMAT_VERSION = 6`, the amended C5, the
`identity_column` constructor, the `write_emit` records-shape assertion, the
v6-shaped spanning fixture with the adversarial id-shape mix, the audited negative
fixtures + five new C5 negatives, and every emit-building test file migrated to the
v6 shape. Atomic: the version literal, the shape assertion, and the fixture
migration cannot land separately — any intermediate state leaves the suite red — so
this is one phase run as a steps pipeline (the suite may be red between steps; the
phase gate runs once after all steps).

**Demo:** `phase_2_v6_flip.py` — runs `validate` on
`docs/examples/parent-child/published` (a real v6 emit; C1–C13 pass including
amended C5), opens the emit and prints `records__actor`'s columns showing
`record_index` and `ref_index__group` in their contract slots, then runs `init`
against it and shows the proposals contain no identity or lifecycle column.

**Contracts:** `identity_column`; modified `_check_c5_table` and `write_emit`.

**Steps:** `source → migrate (fan-out, 41 files) → author (3 files)` (mirrors the
`state.yaml` `steps` block)

**Files** (source step + author step; the migrate fan-out list lives in
`state.yaml` and the Module Changes Summary):

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/__init__.py` |
| Modify | `src/fabulexa_forge/reader/conformance.py` |
| Modify | `tests/_support/sidecar_builder.py` |
| Modify | `tests/reader/_fixtures_build.py` |
| Modify | `tests/reader/_emit_helpers.py` |
| Modify | `tests/reader/test_conformance_structural.py` |
| Modify | `tests/reader/test_fixtures.py` |
| Modify | `tests/_support/test_sidecar_builder.py` |
| Modify | 41 existing test files (migrate fan-out — full list in `state.yaml`) |
| Create | `docs/sprints/base-format-v6/demos/phase_2_v6_flip.py` |

**Tests:**

- Version gate accepts 6; `UNSUPPORTED_VERSION_SENTINEL` negatives still refuse
  (mechanism untouched, literal moved; fixtures pick the new value up through
  their existing import)
- Amended C5 passes on the v6-shaped spanning fixture
- Five new C5 negatives each fail C5 and only C5: missing `record_index`;
  misplaced `record_index`; reference-annotated `prop__` without a following
  `ref_index__`; `ref_index__` with a non-reference predecessor; `ref_index__`
  of a non-`BIGINT` type
- Duplicated lifecycle/identity column in the property block fails C5 (the
  amended block clause, not the no-role clause)
- Every pre-existing negative fixture still fails exactly the check it is named
  for (the audit — the v5 lesson)
- C5's removed catalog re-check: `test_conformance_structural.py` drops those
  assertions; C2 coverage of catalog↔sidecar agreement is untouched
- `identity_column("fork_path"|"record_id"|"record_index"|"ref_index__x",
  "BIGINT"/"VARCHAR")` emits exactly `{"name", "type"}`;
  `identity_column("prop__x", …)` and `identity_column("created_sim_time", …)`
  raise `ValueError`
- `write_emit` construction error names table + column for: a records table
  missing `record_index`; a reference-annotated `prop__` without its sibling; a
  no-role column. The explicit opt-out bypasses the shape net without touching
  `schema_valid`
- Spanning fixture: `record_index` populated `0 … rows−1` per kind;
  `ref_index__` values consistent with the target's ordinals, NULL-together
  with the reference cell; the referenced kind mixes decimal-string and
  hex-digest ids with at least one NULL-together pair (an id/index-conflating
  implementation cannot pass by coincidence)
- Full suite green after migration; all recipe `expect.yaml` stable with zero
  re-baselines

### Phase 3: Corrupter pair-scoped reference writes

**Delivers:** `null_cells`, `dangle_reference`, and `mispoint_reference` write the
edge, not a column: any write to a records reference `prop__` cell writes the
sibling `ref_index__` cell in the same act. Every injected reference defect stays
fully declared in `defects.json`.

**Demo:** `phase_3_pair_writes.py` — corrupts the published v6 example with all
three operations against `records__journey_instance.prop__actor` (embedded
`CorruptConfig` YAML), prints each affected row's pair (`prop__actor`,
`ref_index__actor`) showing NULL/NULL, `__dangling__<n>` / `-(n+1)`, and
donor-id / donor-index respectively, and shows `defects.json` declares exactly one
`DefectRecord` per corrupted cell with unchanged locator shape.

**Contracts:** modified `null_cells` / `dangle_reference` / `mispoint_reference`
behavior (§ Contracts — modified behavior).

**Steps:** none (single implementer)

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/corrupters/operations/null_cells.py` |
| Modify | `src/fabulexa_forge/corrupters/operations/dangle_reference.py` |
| Modify | `src/fabulexa_forge/corrupters/operations/mispoint_reference.py` |
| Modify | `tests/corrupters/operations/test_null_cells.py` |
| Modify | `tests/corrupters/operations/test_dangle_reference.py` |
| Modify | `tests/corrupters/operations/test_mispoint_reference.py` |
| Create | `docs/sprints/base-format-v6/demos/phase_3_pair_writes.py` |

**Tests:**

- `null_cells` on a records reference `prop__` cell: sibling `ref_index__` cell is
  NULL in the same rows; one `DefectRecord` per cell, locator = the `prop__` cell
- `null_cells` on a non-reference `prop__` cell and on a membership `member__id`
  cell: no sibling write (unchanged behavior)
- `dangle_reference`: pair is (`__dangling__<n>`, `-(n + 1)`) with the same `n`
  for every row dangled toward the same target kind; deterministic across runs
  (same seed → same pair values)
- `mispoint_reference`, both `constraint` modes: sibling gets the donor's
  `record_index` read from operation-start working state; the pair resolves to
  the same donor row (fully consistent — invisible to `validate`)
- Declared `class` / `impact` / defect counts unchanged for all three operations
  (corrupt recipe `expect.yaml` — all 27 — pass untouched)

### Phase 4: Index minting + stated invariants

**Delivers:** `insert_rows` mints fresh `record_index` ordinals above a per-table
high-water mark (tombstoned ordinals never resurrected), jitter's reference
exclusion becomes a declared clause, and the two "expected to hold already"
invariants — identity columns never selectable, base-emit writer regenerating
identity columns as bare `{name, type}` entries — become stated and negatively
tested.

**Demo:** `phase_4_index_minting.py` — corrupts the published v6 example with a
`delete_rows` (suffix rows) followed by `insert_rows` on the same records table
(embedded YAML), printing the minted phantom indices to show no tombstoned ordinal
is reused; then a `duplicate_rows` `jitter` pass over a table with a reference
pair, showing the pair travels untouched.

**Contracts:** modified `insert_rows` minting + `is_jitter_eligible` behavior
(§ Contracts — modified behavior).

**Steps:** none (single implementer)

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/corrupters/state.py` |
| Modify | `src/fabulexa_forge/corrupters/operations/insert_rows.py` |
| Modify | `src/fabulexa_forge/corrupters/validate.py` |
| Modify | `tests/corrupters/operations/test_insert_rows.py` |
| Modify | `tests/corrupters/operations/test_duplicate_rows.py` |
| Modify | `tests/corrupters/test_selection.py` |
| Modify | `tests/corrupters/test_base_writer.py` |
| Create | `docs/sprints/base-format-v6/demos/phase_4_index_minting.py` |

**Tests:**

- `insert_rows` on a fresh table of `rows` records: phantoms get
  `rows, rows+1, …` in ascending selected-unit order; each phantom's
  `ref_index__` cells equal the donor's verbatim
- `delete_rows` removing the *suffix* rows, then `insert_rows` on the same table:
  minted indices sit strictly above the pre-delete maximum (a current-max
  implementation would resurrect a tombstoned ordinal — the test distinguishes)
- Two `insert_rows` operations in one config: the second continues above the
  first's phantoms (the mark advances)
- `is_jitter_eligible` is `False` for a records reference `prop__` column
  independent of its DuckDB type (the declared clause, not the numeric gate)
- Never-selectable negatives: `record_index` and `ref_index__x` are excluded by
  the family-A mutability predicate, `schema_drift` eligibility, the nullability
  predicate, jitter eligibility, and `insert_rows` resample eligibility
- Base-emit writer round-trip: a corrupted v6 emit's regenerated sidecar carries
  identity columns as exactly `{"name", "type"}` — no `references`, no
  `history_tracked`, no `temporal_class` keys
- Existing corrupter suite and all corrupt recipes stay green

## What Doesn't Change

- `src/fabulexa_forge/derivations/` — all five residents; none reads identity
  columns; state-at's reconstructed column set stays as-is
- Streaming, pacing, routing, mixer, anchor, incremental, writers — untouched; the
  change-log render's column set was already property-driven
- Membership, `history`, and fixed-table handling — v6 does not touch them; the
  junction render and `member__*` handling are unchanged; no `ref_index` analog on
  membership reference pairs
- Dimensional export grammar — projection stays author-driven; identity columns
  are neither proposed nor forbidden; an explicitly named base column projects
  faithfully
- Config envelopes (`ExportConfig` / `SourceConfig` / `StreamConfig` /
  `CorruptConfig`) and the CLI — zero author-facing surface change
- The version-gate mechanism (`reader/sidecar.py`) — equality against the single
  literal; only the value moves (in Phase 2, not before)
- Conformance C1–C4, C6–C13 — predicates are `prop__`-closed or element-wise;
  identity columns are correctly invisible to them; C2 needs no change to carry
  the new columns' catalog agreement
- `delete_rows`, `duplicate_rows`, `mutate_cells`, `schema_drift`,
  `freeze_series`, `drop_events`, `shift_sim_time`, `distort_intervals` — no
  behavior change (Phase 4 only *states and tests* existing exclusions);
  `delete_rows` wake rules gain no `ref_index__` clause
- `contract/` — vendored, already at v6; never hand-edited
- All recipe `expect.yaml` files — stability is verified, not assumed; any
  unexpected delta is examined, never re-baselined silently

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/reader/records_columns.py` | New: taxonomy (`records_column_role`, `ref_index_sibling`, `REF_INDEX_PREFIX`, `RecordsColumnRole`) |
| `src/fabulexa_forge/reader/__init__.py` | Export the taxonomy surface |
| `src/fabulexa_forge/errors.py` | New `SourceUnclassifiedColumn(ExportError)` |
| `src/fabulexa_forge/exporters/source/plan.py` | Column resolvers classify via taxonomy; identity dropped; no-role raises |
| `src/fabulexa_forge/exporters/dimensional/init.py` | Proposal loop role-scoped: payload + presentation only |
| `src/fabulexa_forge/__init__.py` | `SUPPORTED_BASE_FORMAT_VERSION` 5 → 6 |
| `src/fabulexa_forge/reader/conformance.py` | C5 amended to v6 layout via taxonomy; catalog re-check dropped |
| `src/fabulexa_forge/corrupters/operations/null_cells.py` | Co-null `ref_index__` sibling on records reference writes |
| `src/fabulexa_forge/corrupters/operations/dangle_reference.py` | Co-dangle sibling with `-(n + 1)` sentinel |
| `src/fabulexa_forge/corrupters/operations/mispoint_reference.py` | Co-point sibling with donor's `record_index` |
| `src/fabulexa_forge/corrupters/operations/insert_rows.py` | Fresh `record_index` per phantom via high-water mark |
| `src/fabulexa_forge/corrupters/state.py` | Per-table `record_index` high-water mark state |
| `src/fabulexa_forge/corrupters/validate.py` | Explicit reference-exclusion clause in jitter eligibility |
| `tests/_support/sidecar_builder.py` | `identity_column`; `write_emit` v6 records-shape assertion + opt-out |
| `tests/reader/_fixtures_build.py` | v6 spanning fixture (adversarial id mix); negative audit; 5 new C5 negatives |
| `tests/reader/_emit_helpers.py` | v6-shaped helper emits |
| `tests/reader/test_records_columns.py` | New: taxonomy unit tests |
| `tests/reader/test_conformance_structural.py` | New C5 clause tests; drop removed catalog re-check tests; migrate literals |
| `tests/reader/test_fixtures.py` | v6 fixture-shape assertions |
| `tests/_support/test_sidecar_builder.py` | `identity_column` + shape-assertion tests; migrate literals |
| `tests/exporters/source/test_plan.py` | `SourceUnclassifiedColumn` + posture tests |
| `tests/test_cli_init.py` | Role-scoped proposal expectations (Ph 1); v6 fixture (Ph 2) |
| `tests/corrupters/operations/test_null_cells.py` | Pair co-null tests |
| `tests/corrupters/operations/test_dangle_reference.py` | Pair co-dangle tests |
| `tests/corrupters/operations/test_mispoint_reference.py` | Pair co-point tests |
| `tests/corrupters/operations/test_insert_rows.py` | High-water minting tests |
| `tests/corrupters/operations/test_duplicate_rows.py` | Jitter reference-exclusion negatives |
| `tests/corrupters/test_selection.py` | Identity never-selectable negatives |
| `tests/corrupters/test_base_writer.py` | Bare `{name, type}` identity round-trip invariant |
| 41 test files (Phase 2 migrate fan-out, listed in `state.yaml`) | v6-shape emits; identity literals through `identity_column` |
| `docs/sprints/base-format-v6/demos/phase_[1-4]_*.py` | One demo per phase |
