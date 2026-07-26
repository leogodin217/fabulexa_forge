# Sprint: record-index-keys

## Purpose

Give `mode: base` output integer surrogate keys — a `<kind>_key` self key on every
table and a `<p>_key` edge key beside each surviving reference property — resolved
through a new record-index resident in the derivations layer, so an educator's
students can join, sort, and SCD-2-merge base tables on the integer keys the merge
lesson is written against instead of opaque id strings.

Design doc: `docs/architecture/pending/record-index-keys.md` (rationale, semantics,
invariants — referenced throughout, not duplicated here).

## Scope

**Capabilities touched:**

- derivations layer: seventh resident — the record-index join relation
  (`build_record_index_at_sql` horizoned, `build_record_index_at_end_sql`
  structural end-of-tape)
- base exporter: plan-time reference-edge resolution (`ReferenceKey`,
  `BaseTableSpec.reference_keys`), rename-domain extension (`record_index`,
  `ref_index__<p>` identities with `<kind>_key` / `<p>_key` defaults), render-stage
  key joins and interleaved emission order
- notice channel: one new code, `reference-key-target-absent`
- base business rules: key-name collision, reserved names, `BaseRenameSliceOnly` /
  `BaseRenameUnresolved` over the key identities

**Not included:**

- Prongs 1–3 of the v6 index harvest (source integer PK/FK, dimensional surrogate
  keys, streaming change-log index carriage) — deliberately deferred, see the
  design doc § What Doesn't Change
- Collapsing the truncated-tape surface onto the new resident (opposite bound
  inclusivity — explicitly out of scope)
- New config fields, conformance checks, corrupter changes, writer adapter changes
- A new author-facing recipe for key rename; recipe corpus changes here are
  migration only (`expect.yaml` column lists)
- Architecture doc promotion (`pending/` → live) and `CAPABILITIES.md` updates —
  post-sprint, separate commit

## Breaking Changes

- **Every base output table changes shape.** `<kind>_key` becomes the first
  column of every table; each surviving reference property gains a `<p>_key`
  column immediately after it. Existing author configs remain valid — no config
  field changes — but consumers of base output see new columns. The four base
  recipe corpora (`examples/recipes/base/*/expect.yaml`) are migrated in-sprint.
- **`BaseTableSpec` gains a required field** `reference_keys` (internal runtime
  type; constructed only in `plan.py` and one test fixture in
  `tests/exporters/base/test_renders.py`). No default — Principle #7 does not
  apply to internal runtime types, but the sprint migrates both construction
  sites atomically (Phase 2 steps pipeline).
- `BaseTableSpec.column_renames` now always carries the
  `record_index -> <kind>_key` and `ref_index__<p> -> <p>_key` defaults alongside
  `record_id -> id`. Assertions on the exact map contents change; single-key
  assertions do not.

## Success Criteria

- [ ] A base export over a reference-carrying emit emits `<kind>_key` (BIGINT,
      never NULL) as every table's first column and `<p>_key` (BIGINT, nullable)
      immediately after each surviving `prop__<p>` id column
- [ ] Edge keys are re-derived from the reconstructed property value against the
      target kind's record-index relation at the same horizon — correct under
      `slice_at` and incremental windows, never read from a physical
      `ref_index__` column
- [ ] NULL semantics per the design doc: absent property → both NULL; dangling or
      at-or-after-horizon reference → id present, key NULL; deactivated target
      still resolves
- [ ] Absent target kind: key column omitted, one `reference-key-target-absent`
      notice per kind × property, id column unaffected
- [ ] `rename` reaches both key identities; unsatisfiable renames fail at load
      (`BaseRenameSliceOnly` / `BaseRenameUnresolved`); collisions and reserved
      names fail as today
- [ ] Key joins preserve base's row count — a duplicated-row corrupted emit does
      not fan the join out (resident DISTINCT)
- [ ] `make test` green; the four base recipe corpora pass with migrated
      `expect.yaml` column lists

## Contracts

Contracts are lifted from the design doc § Interface Contracts; behavioral deltas
to existing functions are described in docstring terms only.

### New module: `src/fabulexa_forge/derivations/record_index.py`

```python
#: The canonical column list of the record-index relation, in emission order.
RECORD_INDEX_COLUMNS: tuple[str, ...] = ("record_id", "record_index")
```

```python
def build_record_index_at_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    horizon_ns: int,
) -> str:
    """
    Build the record-id-to-record-index relation for one kind at a horizon.

    One row per distinct (record_id, record_index) pair among the kind's
    records created strictly before the exclusive horizon, projecting
    RECORD_INDEX_COLUMNS — on a conformant emit, one row per record. The
    DISTINCT is a no-op under conformance; it exists so a row-duplicated
    corrupted emit, whose duplicate carries the identical pair, cannot fan a
    consumer's key join out. `record_index` is projected verbatim — the
    contract pins it as set once at creation and never renumbered, so it is a
    temporally-constant value read at a creation instant already bounded
    below the horizon. Rows are filtered on creation time and to `fork_path`;
    `active` is never a predicate, so a record deactivated before the horizon
    is present and remains a resolvable reference target. A join relation,
    not a fold: it declares no ORDER BY, because a consumer LEFT JOINs it
    rather than reading it ordered.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind whose index relation to build.
        horizon_ns: The exclusive horizon in sim-time ns; >= 0.

    Returns:
        A complete, deterministic SELECT producing RECORD_INDEX_COLUMNS.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
    """
```

```python
def build_record_index_at_end_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
) -> str:
    """
    Build the record-id-to-record-index relation for one kind at the tape's end.

    The resident's second entry point: the same DISTINCT RECORD_INDEX_COLUMNS
    relation with no horizon — every record of the kind, filtered only to
    `fork_path`. "The tape's end" is structural: the SQL carries no horizon
    predicate, so composing this relation over a truncated base relation
    bounds it at the truncation with no horizon computed. Equivalence
    contract: equal to build_record_index_at_sql at any horizon strictly
    beyond every creation instant of the composed relation.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind whose index relation to build.

    Returns:
        A complete, deterministic SELECT producing RECORD_INDEX_COLUMNS.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
    """
```

Layer-direction invariant matches the sibling residents: imports only the reader,
`fabulexa_forge._sql` / `fabulexa_forge.errors`, and stdlib.

### `src/fabulexa_forge/exporters/base/plan.py`

```python
@dataclass(frozen=True)
class ReferenceKey:
    """One surviving reference property's index-space edge, resolved at plan time.

    Present only for edges that yield a key column in this emit: a property
    omitted by the `slice_only` policy, or one whose target kind has no records
    table, produces no entry.
    """

    property_name: str
    """The bare property name — the edge key's default output name stem."""
    target_kind: str
    """The referenced records kind, from the property's sidecar `references`."""
```

`BaseTableSpec` gains one field:

```python
    reference_keys: tuple[ReferenceKey, ...]
    """Surviving reference edges that yield a key column, in sidecar
    column-declaration order of their `prop__<p>` columns. Empty when the kind
    has no reference property, or none that survives."""
```

```python
#: Emitted when a reference property's target kind has no records table in the
#: emit, so no index-space key column can be produced for that edge. The
#: id-space column is unaffected.
NOTICE_REFERENCE_KEY_TARGET_ABSENT = "reference-key-target-absent"
```

**`build_base_plan` behavioral delta** (signature unchanged):

- Resolves each kind's surviving reference properties (sidecar `references`
  annotation on a `prop__<p>` that survives the `slice_only` policy) to
  `ReferenceKey` entries, in sidecar column-declaration order. A property whose
  target kind has no records table in the sidecar yields no entry and one
  `reference-key-target-absent` notice (message names the kind, the property, and
  the absent target kind), emitted at plan time in sidecar table-declaration then
  column-declaration order — the same iteration the slice-only omission notices
  follow. An `exclude`d target kind is NOT absent — its records table is in the
  sidecar, so the edge key is still emitted (design doc § Excluded target kind).
- `column_renames` always carries the defaults `record_index -> <kind>_key` and
  one `ref_index__<p> -> <p>_key` per `ReferenceKey`, alongside
  `record_id -> id`; each overridable via `rename.columns` keyed on the contract
  identity.
- The valid rename-identity domain extends to `record_index` and each surviving
  `ref_index__<p>`. A `rename.columns` key naming `ref_index__<p>` where
  `prop__<p>` is `slice_only`-omitted raises `BaseRenameSliceOnly`; naming
  `ref_index__<p>` for a non-reference property or one whose target kind has no
  records table raises `BaseRenameUnresolved` (the identity set checked is what
  the kind actually emits in this emit, so the absent-target case falls out of
  the existing check).
- The collision and reserved-name checks walk the extended output column set (the
  key output names included), so a resolved key name colliding with another
  output column raises `BaseNameCollision`, and a reserved key name raises the
  existing `ExportError`.

### `src/fabulexa_forge/exporters/base/renders.py`

**`build_base_render_sql` behavioral delta** (signature unchanged):

- Composes the record-index resident at the same horizon selection as state-at:
  `build_record_index_at_end_sql` when `horizon_ns is None`,
  `build_record_index_at_sql` otherwise — one horizon per table render
  (invariant 3).
- Self key: `LEFT JOIN`s the kind's own index relation on `record_id` and
  projects `record_index` verbatim (BIGINT, never NULL on a conformant emit) as
  the table's **first** column, ahead of `id`.
- Edge keys: for each `spec.reference_keys` entry, `LEFT JOIN`s the **target
  kind's** index relation on the horizon-reconstructed `prop__<p>` value (both
  sides VARCHAR, no cast) and projects the target's `record_index` (BIGINT,
  nullable via the outer join) immediately after the `prop__<p>` output column.
  The physical `ref_index__<p>` column is never read.
- Every other column keeps its position and rendering; key columns are projected
  under `spec.column_renames` like every other identity. Row set and ORDER BY are
  unchanged — key joins are at most one-to-one per spine row (invariant 7).

### Unchanged signatures

`build_base_query_specs`, `export_base`, `_resolve_horizon_ns` (engine.py) — no
parameter or behavior change beyond what the plan/render deltas carry through.

## Phases

### Phase 1: Record-index resident

**Delivers:** The derivations layer's seventh resident — the record-index join
relation, both entry points, obeying the layer's six rules.

**Demo:** `demos/phase_1_record_index_resident.py` — builds an emit, prints the
relation at a mid-tape horizon and at the tape's end: the horizoned relation is a
dense creation-order prefix (`0..n-1`), a deactivated record is present, and the
end-of-tape SQL carries no horizon predicate.

**Contracts:** `RECORD_INDEX_COLUMNS`, `build_record_index_at_sql`,
`build_record_index_at_end_sql`.

**Steps:** none (single implementer).

**Files:**

| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/derivations/record_index.py` |
| Create | `tests/derivations/test_record_index.py` |
| Create | `docs/sprints/record-index-keys/demos/phase_1_record_index_resident.py` |

**Tests** (fixtures via `tests/_support/sidecar_builder.py`, matching the sibling
derivations test style):

- Horizoned: only records with `created_sim_time < horizon_ns` appear; a record
  created exactly at the horizon is excluded
- Horizoned: a record deactivated before the horizon is present (`active` never a
  predicate)
- Pairs are projected verbatim: `(record_id, record_index)` match the records
  table, nothing renumbered; surviving indexes are exactly the creation-order
  prefix `0..n-1`
- DISTINCT collapse: a duplicated row carrying the identical pair yields one
  relation row (row-duplicated corrupted emit cannot fan a key join out)
- Relation filters to `fork_path`
- SQL declares no `ORDER BY` (string assertion, matching the
  reference-resolution residents' posture)
- End-of-tape: every record of the kind; SQL carries no horizon predicate
  (string assertion); result equals the horizoned builder at a horizon strictly
  beyond every creation instant (equivalence contract)
- Unknown kind raises `TableNotFoundError` from the sidecar lookup
- Existing tests that must still pass: the whole `tests/derivations/` suite
  (purely additive phase)

### Phase 2: Base exporter key columns

**Delivers:** `mode: base` emits both key families: plan-time edge resolution,
rename/collision/reserved-name coverage of the key identities, the
`reference-key-target-absent` notice, and the render-stage joins with interleaved
emission order.

**Demo:** `demos/phase_2_base_keys.py` — runs a base export over a
reference-carrying emit; prints a table showing `<kind>_key` first and `<p>_key`
beside its id column; shows a dangling edge (id present, key NULL) and the
integer join `actor JOIN target ON actor.<p>_key = target.<target>_key`
resolving identically to the id-space join.

**Contracts:** `ReferenceKey`, `BaseTableSpec.reference_keys`,
`NOTICE_REFERENCE_KEY_TARGET_ABSENT`, the `build_base_plan` and
`build_base_render_sql` behavioral deltas.

**Steps:** `source → migrate (fan-out, 5 files) → author (3 files)` — atomic: the
self key lands on every base output table unconditionally, so the source reshape
and the migration must gate together (mirrors the `state.yaml` `steps` block).

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/base/plan.py` |
| Modify | `src/fabulexa_forge/exporters/base/renders.py` |
| Modify | `tests/exporters/base/test_renders.py` |
| Modify | `examples/recipes/base/base-current-state/expect.yaml` |
| Modify | `examples/recipes/base/base-exclude-kind/expect.yaml` |
| Modify | `examples/recipes/base/base-rename-table/expect.yaml` |
| Modify | `examples/recipes/base/base-slice-at/expect.yaml` |
| Modify | `tests/exporters/base/_base_fixtures.py` |
| Modify | `tests/exporters/base/test_plan.py` |
| Create | `tests/exporters/base/test_reference_keys.py` |
| Create | `docs/sprints/record-index-keys/demos/phase_2_base_keys.py` |

**Migration notes** (the `migrate` step's API delta):

- `tests/exporters/base/test_renders.py`: the module-level `_COLUMN_ORDER` gains
  `patient_key` at position 0 (the fixture's `patient` kind has no reference
  property, so no edge key appears); every positional row assertion shifts by
  one; `len(row) == 5` becomes 6. Intent preserved — no assertion removed.
- `examples/recipes/base/*/expect.yaml` (4 files): each table's `columns` list
  gains `<kind>_key` first, and each reference property (`prop__doctor_id`,
  `prop__primary_staff_id`, `prop__backup_staff_id` on their kinds) gains its
  `<p>_key` (`doctor_id_key`, …) immediately after it. The harness enforces
  exact column-set equality, so these are pure column-list edits; `row_count`
  and `contains_rows` entries are unchanged.

**Tests** (the `author` step; new fixture builders added to `_base_fixtures.py`
rather than mutating `_PATIENT_COLUMNS`, so the migrated files stay stable):

Plan (`test_plan.py` additions):

- A kind with reference properties resolves `reference_keys` in sidecar
  column-declaration order, each carrying the bare property name and target kind
- A kind with no reference property resolves `reference_keys == ()`
- `column_renames` carries `record_index -> <kind>_key` and
  `ref_index__<p> -> <p>_key` defaults; a `rename.columns` entry overrides each
- Rename `ref_index__<p>` where `prop__<p>` is `slice_only`-omitted →
  `BaseRenameSliceOnly`
- Rename `ref_index__<p>` where `<p>` is not a reference property →
  `BaseRenameUnresolved`
- Rename `ref_index__<p>` where the target kind has no records table →
  `BaseRenameUnresolved`
- Absent target kind: no `ReferenceKey` entry; one `reference-key-target-absent`
  notice naming kind, property, and target kind; notice order follows sidecar
  table then column order alongside the slice-only notices
- Excluded target kind (records table present, kind `exclude`d): `ReferenceKey`
  entry still present
- Renaming another column to the resolved `<kind>_key` name →
  `BaseNameCollision`; renaming `record_index` to a reserved name → `ExportError`

Render (`test_reference_keys.py`, over new reference-edge fixtures):

- Self key is the first column, projected verbatim from `record_index`, never
  NULL; edge key sits immediately after its `prop__<p>` output column
- Resolved edge: key equals the target's `record_index`
- Dangling reference (id names no record): id present, key NULL
- Absent property: id NULL, key NULL
- Target created at-or-after the horizon (`slice_at`): id present, key NULL
- Target deactivated before the horizon: key resolves
- Horizon binding: the same emit rendered at the tape's end vs a mid-tape
  `slice_at` resolves edges against the respective horizon populations
- Two properties on one kind referencing the same target kind yield two key
  columns named per property
- Duplicated target row (identical pair, built via a corrupted-shape fixture):
  output row count equals the spine's — no fan-out
- Renamed key columns (`record_index: actor_sk`, `ref_index__<p>: <x>_sk`)
  appear under their renamed names

Existing tests that must still pass: the whole suite (`make test`) — in
particular `tests/exporters/base/test_engine.py`, `tests/incremental/`,
`tests/integration/test_corrupt_base.py`, and `tests/test_cli_export.py`
unmodified (their assertions are shape-agnostic; their fixtures carry physical
`record_index` columns).

## What Doesn't Change

- **The state-at resident** (`derivations/state_at.py`) — canonical column tuple,
  both entry points, all three consumers. The design's central constraint.
- **`exporters/base/engine.py`** — signatures and behavior; the plan/render
  deltas flow through it untouched.
- **Source, dimensional, and streaming exporters** — no output column, ordering,
  or API changes (prongs 1–3 deferred).
- **The truncated-tape surface** (`derivations/truncated_tape.py`) — stands as
  shipped; it bounds **inclusively** at `T` where the new resident bounds
  **exclusively** at a horizon. Neither is a template for the other.
- **Config models** (`config/models.py`) — no new fields; `rename` reaches the
  key identities through the existing `RenameEntry.columns` mechanism.
- **The reader and conformance** — no new checks; pair agreement and index
  resolution are producer-guaranteed (design doc § What Doesn't Change).
- **Corrupter operations and the notices module** (`Notice` / `NoticeSink` /
  `render_notice_stderr`) — unchanged; only a new code string is introduced.
- **Writers** — no adapter change; nullable BIGINT already serializes in both
  formats.
- **Playback** — no seam change; no playback test compiles a base-shape config
  today, so no playback file is touched.

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/derivations/record_index.py` | New: record-index resident, both entry points + `RECORD_INDEX_COLUMNS` |
| `src/fabulexa_forge/exporters/base/plan.py` | `ReferenceKey`, `BaseTableSpec.reference_keys`, key rename domain/defaults, key collision/reserved coverage, `reference-key-target-absent` notice |
| `src/fabulexa_forge/exporters/base/renders.py` | Self-key + edge-key LEFT JOINs onto the record-index resident; interleaved emission order |
| `tests/derivations/test_record_index.py` | New: resident contract tests |
| `tests/exporters/base/test_renders.py` | Migrate: `_COLUMN_ORDER` and positional assertions gain the self key |
| `examples/recipes/base/base-current-state/expect.yaml` | Migrate: column lists gain key columns |
| `examples/recipes/base/base-exclude-kind/expect.yaml` | Migrate: column lists gain key columns |
| `examples/recipes/base/base-rename-table/expect.yaml` | Migrate: column lists gain key columns |
| `examples/recipes/base/base-slice-at/expect.yaml` | Migrate: column lists gain key columns |
| `tests/exporters/base/_base_fixtures.py` | New reference-edge fixture builders (existing builders untouched) |
| `tests/exporters/base/test_plan.py` | New plan tests: `reference_keys`, rename rules, notices, collisions |
| `tests/exporters/base/test_reference_keys.py` | New: render-level key semantics tests |
| `docs/sprints/record-index-keys/demos/phase_1_record_index_resident.py` | Phase 1 demo |
| `docs/sprints/record-index-keys/demos/phase_2_base_keys.py` | Phase 2 demo |
