# Sprint: base-format-v5-adopt

## Purpose

Adopt base-format v5 as the sole supported version — the version gate moves to 5, the
sidecar models `temporal_class`, conformance grows C11's converse clause and the new
C13, the source-exporter genre predicate keys on the class, and the corrupter's
base-emit writer round-trips every column attribute. An educator who re-vendored the
v5 contract gets a working `fabulexa-forge validate | export | stream | corrupt`
again — today 156 tests fail and no emit validates end-to-end because the gate says 4
while `contract/` vendors 5.

**Design doc:** `docs/architecture/pending/base-format-v5-adopt.md` — rationale,
semantics, and the measured adoption cost live there. This spec carries contracts,
phases, and test cases only.

## Scope

**Capabilities touched:**

- **reader**: `ColumnSpec.temporal_class` (verbatim, uncoerced), version gate → 5,
  `Sidecar.temporal_class` accessor (raises, never infers),
  `TemporalClassUnavailableError` + `ColumnNotFoundError`
- **conformance**: C11 bidirectional (new converse clause), C13 new (structural
  pairing + enum + exhaustive genesis clause), registry and `validate` enumerate
  C1–C13
- **source exporter**: `_is_kind_tracked` keys on `temporal_class == "tracked"`
- **corrupters**: base-emit writer round-trips every sidecar column attribute the
  reader models; `drop_events` impact oracle gains the emptied-series → `[C11]` clause
- **fixtures / test infrastructure** (re-vendor hardening): one
  `SUPPORTED_BASE_FORMAT_VERSION` literal, `prop_column` + `write_emit` as the sole
  sidecar constructors, genuinely v5-shaped fixtures (paired attributes, unconditional
  genesis rows, presentation columns of both classes, a `slice_only` column), negative
  variants for the new checks

**Not included** (each deferred to its own design — design doc § *Deferred,
deliberately*):

- The `slice_only` policy (refusing/omitting per mode, point-in-time fold narrowing)
- Exploiting the genesis guarantee (dropping the `records__` fallback from the folds)
- Naming C13 breaks in the defect manifest (sentinel sharpening + manifest version bump)

## Breaking Changes

- **Version gate 4 → 5.** A v4 emit is refused with `UnsupportedBaseFormatVersionError`.
  No dual-version support, no auto-upgrade (Principle #9 — we adapt to the external
  contract, never shim it).
- **`ColumnSpec` gains a required field.** `temporal_class: str | None` is added with
  no default (the design contract's shape). All construction sites — two in `src`,
  four in tests — are migrated in the same phase.
- **Genre reclassification (source exporter).** A kind whose *only* `tracked` column
  is a presentation value reclassifies from reference/transaction genre to change-log
  genre. Shipped as a documented, test-guarded behavior change. A kind whose
  presentation column is class `constant` does **not** reclassify — that is the point
  of keying on the class.
- **`validate` is stricter.** Two more checks (C11 converse, C13); an emit that passed
  at v4 semantics can now fail.
- **Published recipe ground truth churns.** `hard-deleted-parents` gains `C6` where
  the recipe fixture's doctor kind becomes tracked; any `drop_events` recipe whose
  seeded draw empties a `(kind, property)` series gains `C11`. Both forced by the
  format, both land as `expect.yaml` updates beside the fixture change that forces
  them.
- **Version-gate negative tests change their stand-in.** Tests using `5` as "an
  unsupported version" switch to the never-valid sentinel `99` (three spike failures
  were exactly these tests quietly becoming valid on the bump).

## Success Criteria

- [ ] `make test` green (baseline: 156 failures).
- [ ] `fabulexa-forge validate` on the spanning fixture passes C1–**C13**; each new
      negative fixture fails exactly the check(s) its expectation names.
- [ ] The supported version appears as an integer literal exactly once
      (`src/fabulexa_forge/__init__.py`); every other site imports it.
- [ ] Every fixture sidecar is written through `write_emit`; every value-carrying
      column is constructed through `prop_column`.
- [ ] A corrupted emit's sidecar carries `temporal_class` verbatim (declared → carried,
      absent → absent) and passes C13's structural clauses by construction.
- [ ] `drop_events` emptying a `(kind, property)` series declares `[C11]` (alone) in
      `defects.json`, and `validate` names the same failure.
- [ ] A kind with a `tracked`-class presentation column classifies change-log genre;
      the same kind with a `constant`-class presentation column does not.
- [ ] A flagged column with no `temporal_class` raises `TemporalClassUnavailableError`
      at plan time, directing the caller to `fabulexa-forge validate`.

## Contracts

Contracts are reproduced from the design doc § *Interface Contracts* (authoritative);
behavioral additions this spec makes concrete are marked.

### Runtime types (`src/fabulexa_forge/reader/sidecar.py`)

```python
TemporalClass = Literal["constant", "tracked", "slice_only"]
"""The point-in-time contract for one value-carrying column, read from the sidecar.

Never inferred: a column that declares no class has no class, and a surface that
needs one refuses rather than deriving it from history_tracked.
"""


@dataclass(frozen=True)
class ColumnSpec:
    """One column of a base-layer table, as declared in base.json."""

    name: str
    type: str
    references: str | None
    history_tracked: bool | None
    temporal_class: str | None
```

`temporal_class` is `str | None`, not `TemporalClass | None`, deliberately: the
sidecar's declared value is carried **verbatim**, neither validated nor coerced at
parse — C13's enum clause must be able to *see* an out-of-enum declared value, and
`validate` reads through the reader. The narrowing to `TemporalClass` happens in
exactly one place: the accessor below.

### Reader (`Sidecar` method)

```python
def temporal_class(self, table_name: str, column_name: str) -> TemporalClass:
    """The declared point-in-time class of one value-carrying column.

    The single point where the sidecar's verbatim declared value narrows to a
    TemporalClass; every surface that needs a class (the genre predicate) resolves
    through it.

    Args:
        table_name: DuckDB table name.
        column_name: Column name, including its prop__ prefix.

    Returns:
        The column's declared TemporalClass.

    Raises:
        TableNotFoundError: No table named `table_name` is declared.
        ColumnNotFoundError: The table declares no column named `column_name`.
        TemporalClassUnavailableError: The column has no usable class. Three cases,
            distinguished in the message: the column carries neither temporal
            attribute (a structural, identity, or membership column — conformant;
            it has no temporal semantics to ask about); it declares history_tracked
            but no temporal_class (non-conformant, C13); or it declares a value
            outside the three-class enum (non-conformant — C13's enum clause, and
            C1, since the vendored schema enum-constrains the value). The
            non-conformant messages direct the caller to `fabulexa-forge validate`.
            No class is ever inferred.
    """
```

### Errors (`src/fabulexa_forge/reader/errors.py`)

```python
class TemporalClassUnavailableError(ReaderError):
    """A column whose point-in-time class is required has no usable one.

    Raised for a column carrying neither temporal attribute (a structural or
    identity column — conformant, but it has no temporal semantics to ask about),
    for a declared history_tracked with no paired temporal_class, and for a
    declared class outside the three-value enum (both non-conformant, C13). The
    message distinguishes the cases; the non-conformant ones direct the caller to
    `fabulexa-forge validate`. Raised rather than inferring a class from
    history_tracked: that inference is the fiction base_format_version 5 exists
    to delete.
    """


class ColumnNotFoundError(ReaderError):
    """A named column is not declared on the named table."""
```

Both exported from `fabulexa_forge.reader` alongside the existing errors.

### Source — the genre predicate (`src/fabulexa_forge/exporters/source/plan.py`)

The existing module-private `_is_kind_tracked` keeps its name and parameter names;
its contract becomes:

```python
def _is_kind_tracked(sidecar: "Sidecar", source_table: str) -> bool:
    """Whether any property of `source_table`'s kind genuinely changes over time.

    A kind is tracked iff one of its prop__ columns is temporal_class 'tracked'.
    Keyed on the class, not on history_tracked: at v5 every presentation column is
    history_tracked, but one bound to an immutable source is class 'constant' and
    holds exactly its genesis row — a kind carrying only such a column does not
    change, and rendering it as a change log would render a change log with no
    changes.

    Only a history_tracked column can be class 'tracked' (the contract constrains
    it), so the class is consulted only for the columns carrying the bit — resolved
    through the sidecar's temporal_class accessor, the single narrowing point. A
    kind carrying no history_tracked prop__ column is untracked without consulting
    any class — the same defensive skip signal C11 and C13 key on, unreachable past
    the version gate against a producer-written v5 emit (coverage is total) and
    retained so the predicate is correct standalone.

    Args:
        sidecar: The open emit's sidecar.
        source_table: The kind's records__<kind> table name.

    Returns:
        True iff some prop__ column of the kind is temporal_class 'tracked'.

    Raises:
        TableNotFoundError: `source_table` is not in the sidecar.
        TemporalClassUnavailableError: A prop__ column declares history_tracked but
            no temporal_class, or declares a class outside the enum. The emit is
            non-conformant (C13); no class is inferred.
    """
```

The refusal is one-directional: a column declaring a `temporal_class` with no
`history_tracked` is never consulted — the predicate classifies from the flagged
columns alone and stays silent (that broken pairing is C13's to report).

### Conformance (`src/fabulexa_forge/reader/conformance.py`)

```python
def _check_c11(emit: Emit) -> CheckResult:
    """C11 — column SCD class consistency, bidirectional at v5.

    Skips when no records-category prop__ column carries history_tracked (the
    published additive-field guard).

    Forward clause (existing): for each distinct (kind, property) in history, the
    prop__<property> column on records__<kind> is present in the sidecar and flagged
    history_tracked true.

    Converse clause (new): for each records__<kind> with at least one row, each
    prop__<property> column flagged history_tracked true has at least one history row
    for (kind, property). Zero rows is a violation — the unconditional creation seed
    removed the "created NULL, never changed" carve-out that made it legal at v4.

    Collection-struct properties emit membership tables, not history rows, and stay
    outside C11's input set — excluded by the same gate shipped C6 uses: the converse
    clause consults only flagged prop__ columns whose declared type is in the
    round-trippable set (BIGINT, DOUBLE, BOOLEAN, VARCHAR). The forward clause needs
    no gate — a collection property never appears in history.

    Args:
        emit: The open emit.

    Returns:
        A CheckResult for check id "C11".
    """


def _check_c13(emit: Emit) -> CheckResult:
    """C13 — temporal-class consistency.

    Skips when no records-category prop__ column carries history_tracked (the
    published additive-field guard; unreachable against a producer-written v5 emit,
    retained so the checker is a correct standalone implementation of the procedure).

    Structural clauses, over every records-category prop__ column: history_tracked is
    present iff temporal_class is present; a present temporal_class is one of the
    three declared values; 'tracked' implies history_tracked true; 'slice_only'
    implies history_tracked false.

    Semantic clause, over every prop__ column flagged history_tracked true (any
    class), for every record of that kind: history carries a row for
    (kind, record_id, property) at that record's own created_sim_time — the genesis
    row. record_id is part of the match: a rowless record does not pass because a
    sibling of the same kind shares its created_sim_time. (The published
    pseudocode's "(kind, property)" shorthand is scoped inside its per-record loop;
    the tuple here names the full key.)
    The published procedure requires a sample of up to ten records; forge checks
    every record — the same strictness choice its C6 makes (the contract:
    "exhaustive checking is the consumer's choice"), and the exhaustive pass
    needs no sample-selection rule, keeping validate deterministic.

    Collection-struct properties stay outside the semantic clause's input set —
    excluded by the same round-trippable-type gate shipped C6 uses (BIGINT, DOUBLE,
    BOOLEAN, VARCHAR); their changes emit membership tables, not history rows. The
    structural clauses have no history side and run ungated, over every
    records-category prop__ column.

    Args:
        emit: The open emit.

    Returns:
        A CheckResult for check id "C13".
    """
```

The `_CHECKS` registry and `_RECOGNIZED_IDS` enumerate C1–C13; `fabulexa-forge
validate` picks the new check up through the registry (no CLI change).

### Corrupters — behavioral contracts (no new signatures)

**Base-emit writer** (`src/fabulexa_forge/corrupters/base_writer.py`, the column-entry
assembly currently hard-coding `name`/`type`/`references`/`history_tracked`):

> *Invariant (design doc § Re-vendor hardening / § Corrupted emits stay structurally
> conformant):* the writer round-trips **every** sidecar column attribute the reader
> models — a declared attribute is carried verbatim, an absent attribute stays absent.
> `temporal_class` joins the carried set. A corrupted emit is structurally conformant
> by construction (C1–C5, C8, and C13's structural clauses). An absent attribute is
> representable only by omission — the writer never emits `temporal_class: null`.

**`drop_events` impact oracle** (`src/fabulexa_forge/corrupters/operations/drop_events.py`,
composing `_impact.py`):

> *Emptied-series clause (new):* when one apply's drawn removals leave **zero**
> `history` rows for a `(kind, property)` pair whose `records__<kind>` still has rows
> — C11's converse broken — every removed row of that pair's series declares
> `impact: ("C11",)`, and `C11` alone: C11 is inside the manifest's exclusive
> vocabulary, so the sentinel there would be false, and the co-occurring C13 break
> cannot sit beside a real code. The existing anchor-participant rule (`C6` /
> `beyond-c1-c12`) applies only to removals that do not empty their `(kind, property)`
> series. The (kind, property) grain is C11's converse grain — emptying one *record's*
> series while siblings keep rows is not this clause.

### Fixture support (new module `tests/_support/sidecar_builder.py`)

```python
def prop_column(
    name: str,
    type: str,
    *,
    history_tracked: bool,
    temporal_class: TemporalClass,
    references: str | None = None,
) -> dict[str, object]:
    """Build one value-carrying sidecar column.

    The sole constructor for a prop__ column across every fixture builder. Both
    temporal attributes are required and passed together, because the contract pairs
    them: a column carries history_tracked iff it carries temporal_class. A future
    paired attribute changes this one signature, and every call site with it.

    The constructor builds only conformant columns — temporal_class is typed to the
    enum, and the contract's implication clauses are validated: 'tracked' requires
    history_tracked True, 'slice_only' requires history_tracked False. A negative
    variant that breaks the pairing, the enum, or an implication mutates the returned
    dict; a defect is never expressible through the constructor.

    Raises:
        ValueError: temporal_class 'tracked' with history_tracked False, or
            'slice_only' with history_tracked True.
    """


def write_emit(
    dest: Path,
    *,
    tables: list[dict[str, object]],
    branches: list[dict[str, object]] | None = None,
    extra: dict[str, object] | None = None,
    base_format_version: int | None = None,
    schema_valid: bool = True,
) -> None:
    """Write one fixture emit's base.json.

    The sole writer of a fixture sidecar — the wrong-version negative fixture
    included, via the override below; no fixture writes or rewrites a sidecar by
    hand.

    Args:
        dest: The emit directory; base.json is written inside it.
        tables: The sidecar's tables list; value-carrying columns built via
            prop_column.
        branches: The branches list. Defaults to the single-trunk entry.
        extra: Optional top-level sidecar blocks (runtime, pinned_ids, enum_domains,
            record_roles), carried verbatim.
        base_format_version: The version to stamp. None (the default) stamps
            SUPPORTED_BASE_FORMAT_VERSION — the supported version appears as a
            literal nowhere in the test tree. An explicit value exists for the
            version-gate negative fixture alone, which passes the never-valid
            sentinel (design doc § Re-vendor hardening), composed with
            schema_valid=False: the vendored schema pins the version, so any
            override is schema-invalid by construction.
        schema_valid: When True (the default), validate the result against the
            vendored contract/base-format.schema.json before writing, so a fixture
            that has not learned a new required field fails at construction, naming
            the field, rather than surfacing as an unrelated C1 failure at read
            time. False is reserved for negative fixtures whose declared defect is
            schema-level (a wrong version, an out-of-enum class) — they must remain
            writable, and their expectations name the C1 failure.
    """
```

Note on `write_emit`'s `branches`/`extra` defaults: these are test-infrastructure
conveniences on an internal helper, not author-facing config — Principle #7 governs
export config the *author* must specify, not fixture plumbing. The module also exports
`UNSUPPORTED_VERSION_SENTINEL = 99` — the never-valid stand-in every version-gate
negative uses (never a neighbouring real version).

The module lives in a new `tests/_support/` package, imported the same way the
per-directory helper packages already resolve (the tests root is the import root, so
`from _support.sidecar_builder import prop_column, write_emit`). The existing
`tests/reader/_emit_helpers.write_emit` (which writes base.json **and** run.duckdb)
keeps its call surface but routes its base.json through the new writer.

## Phases

### Phase 1: One version authority; gate → 5

**Delivers:** `SUPPORTED_BASE_FORMAT_VERSION = 5` as the single literal; every test
site imports it; version-gate negatives use the never-valid sentinel `99`. The suite
goes from 156 failures to green — the measured "compatibility" step (design doc § What
adopting actually costs).

**Demo:** Builds the spanning fixture, opens it through the reader, runs conformance —
C1 passes against the vendored v5 schema for the first time since the re-vendor; then
shows a sentinel-version sidecar refused with `UnsupportedBaseFormatVersionError`
carrying `found_version=99`.

**Contracts:** none new — one constant changes value.

**Steps:** none (single implementer — one constant flip plus seventeen one-line test
edits; atomic within the phase, the gate runs at phase end).

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/__init__.py` |
| Modify | `tests/reader/test_sidecar.py` |
| Modify | `tests/reader/test_open_emit.py` |
| Modify | `tests/corrupters/_helpers.py` |
| Modify | `tests/corrupters/test_base_writer.py` |
| Modify | `tests/corrupters/test_manifest_models.py` |
| Modify | `tests/corrupters/test_manifest_build.py` |
| Modify | `tests/corrupters/operations/test_mutate_cells.py` |
| Modify | `tests/corrupters/operations/test_duplicate_rows.py` |
| Modify | `tests/derivations/_fixtures.py` |
| Modify | `tests/derivations/test_versioned_intervals.py` |
| Modify | `tests/derivations/test_membership_events.py` |
| Modify | `tests/derivations/test_reference_resolution.py` |
| Modify | `tests/exporters/streaming/test_driver.py` |
| Modify | `tests/exporters/streaming/test_mixer.py` |
| Modify | `tests/exporters/streaming/test_routing.py` |
| Modify | `tests/exporters/streaming/test_routing_engine.py` |
| Modify | `tests/exporters/streaming/test_engine.py` |
| Create | `docs/sprints/base-format-v5-adopt/demos/phase_1_version_gate.py` |

Edit inventory (exhaustive — found by survey, verified by grep):

- `src/fabulexa_forge/__init__.py:13` — `SUPPORTED_BASE_FORMAT_VERSION` 4 → 5.
- Eight module-level `SUPPORTED_VERSION = 4` redefinitions (four `derivations`, four
  `exporters/streaming` files) → import `SUPPORTED_BASE_FORMAT_VERSION`.
- Literal `4` in sidecar dicts / `DefectSource` constructions (`corrupters/*`,
  `exporters/streaming/test_driver.py:106,972`, `reader/test_open_emit.py:106`) →
  the import.
- Version-gate negatives using `5` as "unsupported" (`reader/test_sidecar.py:409,417`,
  `reader/test_open_emit.py:82`) → literal `99` (Phase 3 replaces the literal with
  `UNSUPPORTED_VERSION_SENTINEL` when the support module exists).
  `reader/test_sidecar.py:441`'s `3.0` (a type negative, not a version negative) and
  `_fixtures_build.py:719`'s existing `99` stay as they are.

**Tests:**

- Existing version-gate negatives assert `found_version == 99` (was 5).
- Existing suite green: the 156 C1-mismatch failures clear because every fixture now
  stamps 5 via the import; the 3 spike failures clear via the sentinel.
- No new test files — this phase is migration.

### Phase 2: The reader models the class

**Delivers:** `ColumnSpec.temporal_class` (verbatim), `Sidecar.temporal_class`
accessor (the single narrowing point), `TemporalClassUnavailableError` +
`ColumnNotFoundError`, all exported. Additive — no fixture carries the attribute yet;
the new tests build their own sidecars.

**Demo:** Builds a sidecar declaring all three classes plus an unpaired column, an
out-of-enum column, and a bare structural column; shows the accessor returning each
class and raising each of the three `TemporalClassUnavailableError` cases (distinct
messages) plus `ColumnNotFoundError` / `TableNotFoundError`.

**Contracts:** `TemporalClass`, `ColumnSpec`, `Sidecar.temporal_class`,
`TemporalClassUnavailableError`, `ColumnNotFoundError`.

**Steps:** none (single implementer).

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/reader/sidecar.py` |
| Modify | `src/fabulexa_forge/reader/errors.py` |
| Modify | `src/fabulexa_forge/reader/__init__.py` |
| Modify | `src/fabulexa_forge/reader/conformance.py` |
| Modify | `tests/reader/test_sidecar.py` |
| Modify | `tests/reader/test_conformance_structural.py` |
| Modify | `tests/exporters/dimensional/test_windowed_failfast.py` |
| Modify | `tests/corrupters/_helpers.py` |
| Create | `tests/reader/test_temporal_class.py` |
| Create | `docs/sprints/base-format-v5-adopt/demos/phase_2_temporal_class.py` |

(The four test modifications are `ColumnSpec(...)` construction sites gaining the new
field; `conformance.py`'s is its one in-src construction site. `from_raw` parses the
attribute by presence — absent → `None`, declared → verbatim, no structural-floor
change.)

**Tests (new, `tests/reader/test_temporal_class.py`):**

- Accessor returns `"constant"` / `"tracked"` / `"slice_only"` for columns declaring
  each.
- Column with neither attribute → `TemporalClassUnavailableError`, message names the
  no-temporal-semantics case (and does not mention C13).
- Column with `history_tracked` but no `temporal_class` →
  `TemporalClassUnavailableError`, message cites C13 and directs to
  `fabulexa-forge validate` (matches the design doc's message shape).
- Column declaring `temporal_class: "bogus"` → `TemporalClassUnavailableError`,
  message names the out-of-enum value.
- Unknown table → `TableNotFoundError`; unknown column → `ColumnNotFoundError`.
- `from_raw` carries an out-of-enum declared value verbatim on `ColumnSpec`
  (`temporal_class == "bogus"` — no coercion, no parse error).
- Existing tests still pass (attribute absent everywhere → `None`).

### Phase 3: One sidecar authority; fixtures become v5 emits

**Delivers:** the `tests/_support/sidecar_builder.py` module (`prop_column`,
`write_emit`, `UNSUPPORTED_VERSION_SENTINEL`); every fixture sidecar in the test tree
written through it; every value-carrying column carrying the attribute pair; every
history-tracked property of every record carrying its genesis row (NULL-valued where
the property was absent at creation); the spanning fixture gains a `slice_only`
column; the recipe fixture's doctor kind gains a `tracked` presentation column and
`hard-deleted-parents`' ground truth churns to match. After this phase every fixture
is a genuine v5 emit — the precondition for Phase 5's checks.

**Demo:** Builds the spanning fixture; prints every `prop__` column's
`(history_tracked, temporal_class)` pair (total coverage, all three classes
represented); queries `history` to show a genesis row at each record's
`created_sim_time` including a NULL-valued one; runs `write_emit(schema_valid=True)`
on a table set missing a schema-required field to show construction-time failure
naming the field.

**Contracts:** `prop_column`, `write_emit` (§ Fixture support above).

**Steps:** `source → migrate (fan-out, 8 files)` — mixed work-shapes: the support
module is bounded design work, the migration scales with the builder count. The
source step creates the module, rewires `tests/reader/_emit_helpers.py` through it,
and writes the demo. Each migrate agent converts one builder file and updates the
test expectations that depend on it (same directory), in one pass:

1. Route every base.json write through `write_emit`; every value-carrying column
   through `prop_column` (class assignment: `tracked` for columns with a history
   series, `constant` for values fixed at creation, `slice_only` for
   mutable-untracked ones — the fixture author's declared intent, design doc
   § Fixtures).
2. Add the unconditional genesis rows to the fixture's `history` data, NULL-valued
   where a property had no value at `created_sim_time`. Where the existing history
   already opens at creation, nothing is added.
3. Update dependent expectations that legitimately churn (more `history` rows → more
   versioned intervals / SCD-2 version rows; genesis-coincident rows stay excluded
   from the update stream, so no new `u` events at creation instants). Intent is
   preserved: same scenario, now v5-shaped.

| Step | Kind | Scope |
|------|------|-------|
| 1 | source | `tests/_support/__init__.py` + `sidecar_builder.py`; rewire `tests/reader/_emit_helpers.py`; demo |
| 2 | migrate (fan-out) | the 8 builder files below, one agent each |

Migrate files (disjoint; each agent also owns its directory's dependent expectations):

- `tests/reader/_fixtures_build.py` — 13 base.json sites → `write_emit`; columns →
  `prop_column`; genesis rows; `refs_dangling` gains its genesis row (it must fail
  nothing but the boundary it exercises); the spanning fixture gains a `slice_only`
  column; `build_wrong_version` passes `UNSUPPORTED_VERSION_SENTINEL` with
  `schema_valid=False`.
- `tests/exporters/_emit_fixtures.py`
- `tests/exporters/source/_source_fixtures.py`
- `tests/exporters/streaming/test_driver.py` (two inline sidecars)
- `tests/derivations/_fixtures.py`
- `tests/recipes/_recipe_fixture.py` — plus the doctor `tracked` presentation column
  with its history series, and the forced ground-truth churn in
  `examples/recipes/corrupt/hard-deleted-parents/expect.yaml` (`delete_rows` defects
  gain `C6` where the deletion orphans the new series; `impact_union` follows).
- `tests/corrupters/_helpers.py`
- `tests/corrupters/test_base_writer.py` (inline sidecar)

**Tests:**

- New `tests/_support`-focused cases (may live in `tests/reader/test_fixtures.py` or a
  small `tests/test_support_builder.py`): `prop_column` rejects `tracked` +
  `history_tracked=False` and `slice_only` + `history_tracked=True` (ValueError);
  `write_emit` default stamps the supported version; `schema_valid=True` rejects a
  schema-invalid sidecar naming the failure; `schema_valid=False` writes it.
- Spanning fixture passes C1–C12 (unchanged assertion, now over a v5-shaped emit).
- `refs_dangling` still fails nothing in C1–C12 (its defect is beyond the checks) —
  and carries a genesis row.
- Recipe suite green with the churned `hard-deleted-parents` expectation
  (`impact: [C10]` → `[C6, C10]` on the affected defect, per the design doc's spike).
- Existing derivations / exporter tests green with updated expectations.

### Phase 4: The corrupter writer round-trips the class

**Delivers:** the base-emit writer carries every sidecar column attribute the reader
models — `temporal_class` joins `name`/`type`/`references`/`history_tracked`; declared
→ verbatim, absent → absent (never `temporal_class: null`). A corrupted v5 emit's
sidecar is structurally conformant by construction. Must land before Phase 5: once
C13 exists, a stripped class would fail `validate` on every corrupted emit while the
manifest declared nothing.

**Demo:** Corrupts a v5 emit (built via the Phase-3 builders) with a simple
`null_cells` config; diffs input and output sidecars column-by-column showing the
attribute pair carried verbatim, including a column that legitimately carries neither
attribute staying bare.

**Contracts:** the writer round-trip invariant (§ Corrupters above).

**Steps:** none (single implementer).

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/corrupters/base_writer.py` |
| Modify | `tests/corrupters/test_base_writer.py` |
| Create | `docs/sprints/base-format-v5-adopt/demos/phase_4_writer_roundtrip.py` |

**Tests:**

- A column declaring `temporal_class` round-trips it verbatim through
  `write_base_emit`.
- A column declaring neither temporal attribute stays bare in the output sidecar (no
  `temporal_class: null`, no invented `history_tracked`).
- All three class values round-trip.
- Existing writer tests still pass.

### Phase 5: Conformance judges v5 — C11 converse, C13, and the emptied-series impact

**Delivers:** `_check_c11`'s converse clause; `_check_c13` (structural pairing + enum
+ exhaustive genesis clause); the registry and `validate` enumerating C1–C13;
`drop_events`' emptied-series → `[C11]` impact clause; the four new negative fixtures;
the recipes harness comparison scoped to the manifest's vocabulary (C1–C12) so a
sentinel-labeled C13 break stays *accurate, not false*.

**Demo:** Runs `validate` on the spanning fixture — C1 through C13 all pass; runs each
new negative fixture — each fails exactly the check(s) its expectation names; runs a
`drop_events` corruption that empties a `(kind, property)` series — `defects.json`
declares `[C11]` and `validate` fails C11.

**Contracts:** `_check_c11`, `_check_c13`, the `drop_events` emptied-series clause
(§ Contracts above).

**Steps:** `source → author (1 group)` — the checks and the oracle clause are source
work; the negative fixtures, their tests, the harness scoping, and the ground-truth
churn are new-test authorship over the reshaped surface.

| Step | Kind | Scope |
|------|------|-------|
| 1 | source | `conformance.py` (C11 converse, C13, registry), `drop_events.py` (+ `_impact.py` if a shared helper fits), demo |
| 2 | author | negative fixture builders + conformance tests + harness scoping + `expect.yaml` churn (files below) |

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/reader/conformance.py` |
| Modify | `src/fabulexa_forge/corrupters/operations/drop_events.py` |
| Modify | `src/fabulexa_forge/corrupters/operations/_impact.py` |
| Modify | `tests/reader/_fixtures_build.py` |
| Modify | `tests/reader/test_conformance_structural.py` |
| Modify | `tests/reader/test_conformance_data.py` |
| Modify | `tests/reader/test_fixtures.py` |
| Modify | `tests/recipes/_harness.py` |
| Modify | `tests/recipes/test_corrupt_recipes.py` |
| Modify | `tests/recipes/test_recipe_conformance.py` |
| Modify | `tests/corrupters/operations/test_drop_events.py` |
| Modify | `tests/test_cli.py` |
| Modify | `examples/recipes/corrupt/event-outage-window/expect.yaml` |
| Create | `docs/sprints/base-format-v5-adopt/demos/phase_5_conformance.py` |

(`_impact.py` is listed for the emptied-series helper if the implementer places it
beside the anchor-participant rule; `test_cli.py` for the `validate` check-count
output if it asserts one. `event-outage-window/expect.yaml` churns **only if** its
seeded draw actually empties a series — determined empirically when the clause lands;
if the draw does not empty one, the file is untouched and a dedicated unit test covers
the clause instead.)

New negative fixture builders (in `_fixtures_build.py`, named by defect, per the
design doc § Fixtures):

- broken attribute pairing (`history_tracked` with no `temporal_class`) — fails C13's
  structural clause **alone** (the vendored schema does not enforce the pairing);
  built by mutating `prop_column` output, never through it.
- out-of-enum class — fails C13's enum clause **and necessarily C1** (the schema
  enum-constrains the value); written with `schema_valid=False`; the expectation names
  both.
- missing genesis row with later rows intact — fails C13's semantic clause **alone**
  (C11's converse still sees rows).
- emptied `(kind, property)` series — fails C11's converse **and** C13's genesis
  clause (zero rows implies no genesis row); the expectation names both.

**Tests:**

- Registry order test asserts exactly `["C1", ..., "C13"]` (was C12).
- C11 converse: a flagged `(kind, property)` with zero history rows on a kind with
  records rows → C11 fails; the collection-struct gate: a flagged column outside the
  round-trippable type set is not consulted.
- C11 skip guard: no flagged column anywhere → C11 skips (existing behavior retained).
- C13 structural: each of the four clauses individually violated → C13 fails naming
  the column; all-conformant → passes.
- C13 semantic: a record whose flagged property has rows but none at its own
  `created_sim_time` → fails; a record sharing `created_sim_time` with a sibling that
  has the genesis row does **not** pass vicariously (record_id is part of the match);
  NULL-valued genesis row → passes.
- C13 skip guard: no flagged column anywhere → skips.
- Each negative fixture fails exactly its named check(s) and no other; the spanning
  fixture and recipe fixture pass C1–C13.
- `drop_events`: a draw emptying a `(kind, property)` series → every removed row of
  that series declares `impact: ("C11",)` alone; a draw leaving the series non-empty
  → the existing anchor-participant rule unchanged.
- Recipes harness: `validate` failing-checks are compared within the C1–C12 vocabulary
  (a C13-only failure on a corrupted emit does not fail `impact_union` equality).
- `validate` CLI output includes C13 (via `test_cli.py` if it asserts the roster).

### Phase 6: The genre trichotomy keys on the class

**Delivers:** `_is_kind_tracked` resolves through `Sidecar.temporal_class`; the
plan-time refusal on a non-conformant emit; the new-coverage presentation columns —
one class `tracked` (flips its kind's genre) and one class `constant` (does not) — on
the source-exporter fixtures, making the projected-history superset visible to the
suite.

**Demo:** Plans a source export over a fixture whose kind carries only a `tracked`
presentation column — classified change-log genre; the same kind with a `constant`
presentation column — reference genre; a flagged column stripped of its class →
`TemporalClassUnavailableError` at plan time with the documented message, before any
data read.

**Contracts:** `_is_kind_tracked` (§ Contracts above).

**Steps:** none (single implementer).

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `tests/exporters/source/_source_fixtures.py` |
| Modify | `tests/exporters/source/test_plan.py` |
| Modify | `tests/exporters/source/test_engine.py` |
| Modify | `tests/exporters/source/test_renders.py` |
| Create | `docs/sprints/base-format-v5-adopt/demos/phase_6_genre.py` |

(`test_engine.py` / `test_renders.py` are listed for expectation churn where the new
presentation columns flow through renders; if a fixture variant leaves them untouched,
they are untouched.)

**Tests:**

- A kind whose only `tracked`-class column is a presentation value → change-log genre
  (the documented reclassification).
- The same kind, presentation column class `constant` → reference/transaction genre
  by role (no reclassification; the class, not the bit, decides).
- A kind with a genuinely tracked ordinary property → change-log genre (unchanged).
- A kind with no flagged `prop__` column at all → untracked, no class consulted (the
  standalone skip guard).
- A flagged column with no declared class → `TemporalClassUnavailableError` at plan
  time; message matches the design doc's shape and directs to
  `fabulexa-forge validate`.
- A column declaring a class with no `history_tracked` is never consulted — the
  predicate stays silent (one-directional refusal; C13's to report).
- Existing source-exporter tests green (existing tracked columns are class `tracked`
  after Phase 3, so no classification changes for them).

## What Doesn't Change

Explicit boundaries against implementer drift (design doc § What Doesn't Change):

- **`open_emit` does not gate on class coverage.** The reader reads (verbatim, even
  out-of-enum); conformance judges; the modes refuse. No parse-time rejection of an
  unclassed or mis-classed column.
- **The version gate stays a single integer.** No dual-version support, no inference
  fallback. `Sidecar.history_tracked_available` and its all-or-none parse rule stay
  as-is.
- **The defect manifest's impact vocabulary, schema version, and sentinel
  exclusivity.** No new impact code; `beyond-c1-c12` keeps its exact meaning; the
  manifest keeps rejecting sentinel-plus-real-code mixes. C13 breaks stay
  sentinel-labeled (deferred sharpening).
- **`history`'s shape, the membership tables, every derivation's contract, the anchor
  surface, the streaming routing/pacing/mixer surfaces, the writers, the incremental
  driver's window math.** They see more `history` rows; no contract changes. In
  particular row-state-events **keeps** its `records__` join and its exclusion of
  genesis-coincident rows from the update stream — Phase 3 must not "fix" it into
  emitting a spurious `u` at creation.
- **The point-in-time folds (state-at) keep their `records__` fallback.** Exploiting
  the genesis guarantee is the follow-on design's.
- **C6's input rules.** NULL-against-NULL round-trip is already its regime; no C6 code
  change is expected — Phase 3 merely gives it live NULL-genesis input.
- **The genesis-origin question.** No marker distinguishes an intrinsic birth value
  from a truncated as-of initial condition; forge stays silent.
- **`contract/`** is read-only vendored material — nothing in this sprint edits it.

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/__init__.py` | `SUPPORTED_BASE_FORMAT_VERSION` 4 → 5 (the single literal) |
| `src/fabulexa_forge/reader/sidecar.py` | `TemporalClass`, `ColumnSpec.temporal_class` (verbatim), `Sidecar.temporal_class` accessor |
| `src/fabulexa_forge/reader/errors.py` | `TemporalClassUnavailableError`, `ColumnNotFoundError` |
| `src/fabulexa_forge/reader/__init__.py` | Export the two new errors |
| `src/fabulexa_forge/reader/conformance.py` | C11 converse clause; `_check_c13`; registry C1–C13; its `ColumnSpec` construction gains the field |
| `src/fabulexa_forge/exporters/source/plan.py` | `_is_kind_tracked` keys on the class via the accessor |
| `src/fabulexa_forge/corrupters/base_writer.py` | Column-attribute round-trip invariant (`temporal_class` carried verbatim / absent stays absent) |
| `src/fabulexa_forge/corrupters/operations/drop_events.py` | Emptied-series → `[C11]` impact clause |
| `src/fabulexa_forge/corrupters/operations/_impact.py` | Shared emptied-series helper (if placed here) |
| `tests/_support/__init__.py`, `tests/_support/sidecar_builder.py` | New — `prop_column`, `write_emit`, `UNSUPPORTED_VERSION_SENTINEL` |
| `tests/reader/_emit_helpers.py` | base.json routed through `write_emit` |
| `tests/reader/_fixtures_build.py` | All 13 sidecar writes through the authority; v5 shaping; `slice_only` column; 4 new negative builders |
| `tests/reader/test_temporal_class.py` | New — accessor + error cases |
| `tests/reader/test_sidecar.py`, `test_open_emit.py`, `test_conformance_structural.py`, `test_conformance_data.py`, `test_fixtures.py` | Version-literal migration; `ColumnSpec` sites; C11/C13 cases; registry order C1–C13 |
| `tests/exporters/_emit_fixtures.py`, `tests/exporters/source/_source_fixtures.py`, `tests/derivations/_fixtures.py`, `tests/corrupters/_helpers.py`, `tests/recipes/_recipe_fixture.py`, `tests/exporters/streaming/test_driver.py`, `tests/corrupters/test_base_writer.py` | Builders through the authority; v5 shaping; expectation churn |
| `tests/exporters/dimensional/test_windowed_failfast.py` | `ColumnSpec` site |
| `tests/corrupters/test_manifest_models.py`, `test_manifest_build.py`, `operations/test_mutate_cells.py`, `operations/test_duplicate_rows.py` | Version-literal migration |
| `tests/derivations/test_versioned_intervals.py`, `test_membership_events.py`, `test_reference_resolution.py` | `SUPPORTED_VERSION` redefinition → import |
| `tests/exporters/streaming/test_mixer.py`, `test_routing.py`, `test_routing_engine.py`, `test_engine.py` | `SUPPORTED_VERSION` redefinition → import |
| `tests/recipes/_harness.py`, `test_corrupt_recipes.py`, `test_recipe_conformance.py` | Failing-check comparison scoped to C1–C12; conformance roster C1–C13 |
| `tests/corrupters/operations/test_drop_events.py` | Emptied-series impact cases |
| `tests/exporters/source/test_plan.py`, `test_engine.py`, `test_renders.py` | Genre-predicate rekey coverage; presentation-column churn |
| `tests/test_cli.py` | Sentinel version negative; `validate` roster if asserted |
| `examples/recipes/corrupt/hard-deleted-parents/expect.yaml` | Ground truth `[C10]` → `[C6, C10]` where the deletion orphans the new series |
| `examples/recipes/corrupt/event-outage-window/expect.yaml` | Adds `C11` iff its seeded draw empties a series (empirical) |
| `docs/sprints/base-format-v5-adopt/demos/phase_*.py` | Six demo scripts |
