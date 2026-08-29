# Sprint: desc-override

## Purpose

Deliver the per-column description override: an author re-voices any output
column's rendered documentation in the three companion-writing modes
(dimensional, source, base) by writing prose in the export config, and the
companion README and manifest render it author-first — replacing engine-voiced
sidecar prose on renamed columns and giving computed columns a description
where today nothing renders.

Design doc (rationale, semantics, precedence table):
`docs/architecture/pending/per-column-description-override.md`. This spec
carries the contracts, phases, and test cases; the design doc carries the WHY.

## Scope

**Capabilities touched:**
- Export-config models: `ColumnDecl.description`, `SourceTableDecl.descriptions`,
  `RenameEntry.descriptions` (+ widened well-formedness validators)
- Compiled-plan carriage: `QuerySpec.author_descriptions` /
  `TableReport.author_descriptions`, stamped by the three plan compilers,
  forwarded by both report-assembly sites
- Companion dictionary: author-first tier in `resolve_column_doc`;
  `ColumnDoc.origin` gains `"author"` (companion-produced only)
- Plan-time gates: source/base `descriptions` keys ride the existing
  rename-key gates — no new error types
- Incremental fingerprint: canonical config dump excludes all three
  description surfaces

**Not included:** streaming documentation surface, corrupter description
surface, unit / enum-gloss overrides, table-prose suppression or override,
event-log declaration surface, `init` description stubs, promotion of the
pending design doc to live (ships separately post-archival).

## Breaking Changes

- **`TableReport` gains a required field.** `author_descriptions:
  Mapping[str, str]` has no default (mirroring `provenance` / `kind_values`),
  so every construction site — `write_query_specs` both arms, the incremental
  driver's windowed assembler, and the companion test fixtures — states it
  explicitly. All sites are updated in Phase 1; `TableReport` is internal, no
  external surface changes.
- **`RenameEntry` well-formedness widens.** The at-least-one rule becomes
  name / columns / **descriptions** — a descriptions-only entry is now legal.
  Purely widening: every currently-valid config remains valid.

Everything else is additive (`QuerySpec.author_descriptions` defaults to
empty; the three config fields are optional).

## Success Criteria

- [ ] All three config surfaces parse; empty/whitespace prose, empty maps, and
      empty keys are load-time `ValidationError`s
- [ ] Each mode's compiled `QuerySpec.author_descriptions` is keyed by
      post-rename output column name, translated from the mode's addressing idiom
- [ ] A bad source/base `descriptions` key raises the same error the same key
      would raise as a `rename` / `columns` key — no new error types
- [ ] README and manifest render the authored prose identically, author-first
      per the design doc's precedence table; resolved origin is `"author"`
- [ ] A computed column with an override yields a description-only resolved doc
      where today nothing renders
- [ ] Datasets, notices, exit codes byte-identical with and without overrides
- [ ] Editing only a description never changes the incremental fingerprint
- [ ] Without any override, all documentation output is byte-identical to today

## Contracts

Lifted from the design doc § Interface Contracts (authoritative there).
Existing fields elided; no implementation code.

### Config models (`src/fabulexa_forge/config/models.py`) — Phase 1

```python
class ColumnDecl(StrictBaseModel):
    """One output column declaration with exactly one source mode."""

    # ... existing fields unchanged ...
    description: str | None = None
    """Author-supplied rendered description for this output column. Replaces
    the inherited (or forge-pinned) description in the companion README and
    manifest; unit and declared-value resolution are unaffected. Absent ->
    inheritance as before. Non-empty when present."""
```

```python
class SourceTableDecl(StrictBaseModel):
    """One declared output table: a name, one population source, optional
    column selection, renames, row selection, and descriptions."""

    # ... existing fields unchanged ...
    descriptions: dict[str, str] | None = None
    """Source column identity -> author-supplied rendered description, keyed
    like `rename` (source identity, never the output name). Replaces the
    inherited description in the companion README and manifest for the
    addressed output column. Keys validated at plan time against the table's
    source columns. Absent -> inheritance as before."""
```

```python
class RenameEntry(StrictBaseModel):
    """One table's output-name and description overrides, keyed by sidecar
    identity."""

    # ... existing fields unchanged ...
    descriptions: dict[str, str] | None = None
    """Source column identity -> author-supplied rendered description, keyed
    like `columns` (state-at column identities). Replaces the inherited
    description in the companion README and manifest. Keys validated at plan
    time against the target table's columns. Counts toward the entry's
    at-least-one-field rule. Absent -> inheritance as before."""
```

Validators (Phase 1):

```python
@model_validator(mode="after")
def description_nonempty(self) -> Self:
    """ColumnDecl.description, when present, is non-empty and
    non-whitespace."""
```

```python
@model_validator(mode="after")
def table_shape(self) -> Self:
    """SourceTableDecl: `descriptions`, when present, is a non-empty map
    with non-empty, distinct keys and non-empty, non-whitespace values —
    folded into the existing shape validator alongside `rename` / `render`."""
```

```python
@model_validator(mode="after")
def entry_well_formed(self) -> Self:
    """RenameEntry: at least one of name / columns / descriptions is set;
    `descriptions`, when present, is a non-empty map with non-empty keys and
    non-empty, non-whitespace values — extending the existing well-formedness
    validator."""
```

### Carriage (`src/fabulexa_forge/exporters/query_spec.py`) — Phase 1

```python
@dataclass(frozen=True)
class QuerySpec:
    """A compiled output table: name, SELECT, write mode, optional view pair.

    `author_descriptions` is keyed by output column name (post-rename), like
    `provenance` and `kind_values`; stamped at plan compile from the mode's
    config surface. Empty means no overrides.
    """

    # ... existing fields unchanged ...
    author_descriptions: "Mapping[str, str]" = field(default_factory=dict)
```

```python
@dataclass(frozen=True)
class TableReport:
    """One output table as written.

    `author_descriptions` is forwarded verbatim from the compiled `QuerySpec`
    that produced this table — no default, so every report-assembly call site
    states it explicitly, like `provenance` and `kind_values`.
    """

    # ... existing fields unchanged ...
    author_descriptions: "Mapping[str, str]"
```

Both report-assembly sites forward it verbatim: `write_query_specs`
(`src/fabulexa_forge/exporters/query_spec.py:276`, `:294`) and the incremental
driver's `_build_windowed_report` (`src/fabulexa_forge/incremental/driver.py:245`).

### Resolution (`src/fabulexa_forge/reader/documentation.py`,
`src/fabulexa_forge/exporters/companion/dictionary.py`) — Phase 3

```python
@dataclass(frozen=True)
class ColumnDoc:
    """Resolved documentation for one declared column.

    origin names the single authority that answered: "contract" for a
    structural column (pinned strings, instance placeholders bound),
    "sidecar" for a per-run column (verbatim carry), "author" for a
    companion-dictionary resolution answered by the export config's
    per-column description override. The reader's documentation view never
    produces "author" — it is stamped only downstream, by the companion
    dictionary.
    """

    description: str | None
    unit: str | None
    origin: Literal["contract", "sidecar", "author"]
```

```python
def resolve_column_doc(
    doc: "Documentation", table: "TableReport", column_name: str, output_type: str
) -> "ColumnDoc | None":
    """One output column's resolved documentation.

    Args:
        doc: The emit's documentation view.
        table: The output table report.
        column_name: The output column name (post-rename).
        output_type: The column's materialized DuckDB type text.

    Returns:
        With an `author_descriptions` entry for the column: the resolved doc
        with the author's description and origin "author" — on a carried
        column the inherited unit rides along under today's unit rules; on a
        column with no provenance the doc is description-only (unit None).
        Without an entry: exactly today's resolution — the inherited source
        answer (with the interval-end constant, the export structural
        rewrites, and the ns-unit stop applied), or None for a column with no
        carried provenance or whose source carries neither description nor
        unit.
    """
```

### Plan-time gates (Phase 2)

No new errors. Each mode's existing rename-key gates widen their range to the
entry's `descriptions` keys and raise under the same identities:

| Rule | Checks | Error |
|---|---|---|
| Source descriptions key valid | Every `descriptions` key names a source column the declared table selects (same key vocabulary and gate point as `rename`) | `SourceColumnUnresolved` (or `SourceColumnNotAddressable` / the slice-only refusal, exactly as the same key would fail as a `rename` key), naming the table and the offending key |
| Base descriptions key valid | Every `descriptions` key names a state-at column identity of the entry's target table (same key vocabulary and gate point as `columns`) | `BaseRenameUnresolved` (or `BaseRenameSliceOnly`, exactly as the same key would fail as a `columns` key), naming the table and the offending key |

The dimensional surface needs no key gate: the description rides the column
entry itself.

### Fingerprint (`src/fabulexa_forge/incremental/fingerprint.py`) — Phase 3

`compute_fingerprint`'s canonical config dump excludes all three description
surfaces (`dimensional.tables[].columns[].description`,
`source.tables[].descriptions`, `base.rename[].descriptions`) alongside the
existing `readme_overlay` exclusion — changing only a description never raises
a fingerprint mismatch. Behavioral contract, test-guarded; the exclusion
mechanism is the implementer's.

## Phases

### Phase 1: Config surfaces + compiled-plan carriage

**Delivers:** The three config override fields with load-time validation, and
the `author_descriptions` carriage — stamped nowhere yet (engines stamp in
Phase 2), but carried by `QuerySpec` (defaulted empty) and required on
`TableReport`, forwarded verbatim by both report-assembly sites.

**Demo:** Parses example configs exercising all three surfaces (and shows a
whitespace-only description refused at load); synthesizes a minimal emit,
builds a `QuerySpec` carrying `author_descriptions`, runs `write_query_specs`,
and prints the `TableReport.author_descriptions` forwarded verbatim.

**Contracts:** `ColumnDecl.description`, `SourceTableDecl.descriptions`,
`RenameEntry.descriptions` + the three validators;
`QuerySpec.author_descriptions`, `TableReport.author_descriptions`.

**Steps:** `source → migrate (fan-out, 4 files) → author (2 files)` — atomic:
the required `TableReport` field leaves every un-migrated construction site
red until all are updated, so source and migration land in one gated phase.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/exporters/query_spec.py` |
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Modify | `tests/exporters/companion/_fixtures.py` |
| Modify | `tests/exporters/companion/test_manifest.py` |
| Modify | `tests/exporters/companion/test_readme.py` |
| Modify | `tests/exporters/companion/test_artifacts.py` |
| Modify | `tests/config/test_models.py` |
| Modify | `tests/exporters/test_query_spec.py` |
| Create | `docs/sprints/desc-override/demos/phase_1_config_carriage.py` |

**Tests:**
- `ColumnDecl` with `description` beside `from` parses; beside `derived` and
  `null: true` parses (any column mode may carry one)
- `ColumnDecl` with empty-string description → `ValidationError`; with
  whitespace-only description → `ValidationError`
- `SourceTableDecl.descriptions`: valid map parses; present-but-empty map,
  empty key, and whitespace-only value each → `ValidationError`
- `RenameEntry` with only `descriptions` set (no `name`, no `columns`) is now
  valid; an entry with none of name/columns/descriptions still refused;
  present-but-empty `descriptions`, empty key, whitespace-only value each →
  `ValidationError`
- `write_query_specs` DuckDB arm forwards a spec's `author_descriptions`
  verbatim onto its `TableReport`; CSV arm likewise; a spec built without the
  field forwards an empty map (widening the existing provenance-forwarding
  tests at `tests/exporters/test_query_spec.py:82/:114/:147`)
- All existing companion tests pass with `author_descriptions={}` threaded
  through the fixture constructions

### Phase 2: Plan-compile stamping + descriptions key gates

**Delivers:** Each batch-mode plan compiler translates its config surface to
the output-name-keyed `author_descriptions` map at plan compile — dimensional
from the column entries (keyed by the entry's own `name`), source and base
from the source-identity-keyed maps through their rename resolution — and the
source/base rename-key gates widen to `descriptions` keys. The source event
log stamps an empty map (no surface).

**Demo:** Synthesizes an emit, compiles plans in all three modes from configs
with overrides, prints each spec's `author_descriptions` showing
source-identity → output-name translation (e.g. `prop__tier` addressed, `loyalty_tier`
keyed); then shows a bad `descriptions` key raising `SourceColumnUnresolved`
and `BaseRenameUnresolved` before anything is written.

**Contracts:** Plan-time gates table above; stamping semantics per the design
doc § Carriage.

**Steps:** `source → author (6 files)` — the author step re-reads the same
three-engine compile surface the source step reshaped, so each runs in a
fresh context.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/dimensional/engine.py` |
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/source/engine.py` |
| Modify | `src/fabulexa_forge/exporters/base/plan.py` |
| Modify | `src/fabulexa_forge/exporters/base/engine.py` |
| Modify | `tests/exporters/dimensional/test_provenance.py` |
| Modify | `tests/exporters/source/test_provenance.py` |
| Modify | `tests/exporters/source/test_plan.py` |
| Modify | `tests/exporters/base/test_provenance.py` |
| Modify | `tests/exporters/base/test_plan.py` |
| Modify | `tests/incremental/test_driver.py` |
| Create | `docs/sprints/desc-override/demos/phase_2_plan_stamping.py` |

**Tests:**
- Dimensional: a table whose entries carry descriptions compiles
  `author_descriptions == {entry name: prose}` for exactly those entries;
  a `derived` and a `null:` column entry's description is stamped; a table
  with no descriptions stamps an empty map
- Source state table: a `descriptions` key addressed by source identity lands
  under its post-`rename` output name; a key on an un-renamed column lands
  under its own name; junction table descriptions stamp likewise
- Source: a `descriptions` key naming a column the table does not select →
  `SourceColumnUnresolved` naming table and key (same error the key would
  raise as a `rename` key); a structurally un-addressable key →
  `SourceColumnNotAddressable`
- Source event log spec stamps an empty `author_descriptions`
- Base: a descriptions-only rename entry compiles (no `name`, no `columns`);
  keys translate through the entry's `columns` renames to output names; a bad
  key → `BaseRenameUnresolved`; a slice-only-unsatisfiable key →
  `BaseRenameSliceOnly`
- Gates fire at plan time, before any write (no partial output on refusal)
- Incremental: for a config with overrides, a windowed export's
  `TableReport.author_descriptions` equals the full export's for the same
  table (forwarding through `_build_windowed_report`)

### Phase 3: Author-first dictionary resolution + fingerprint exclusion

**Delivers:** The companion dictionary consults the author map first —
overrides re-voice carried columns (unit/enum/gloss resolution untouched) and
give computed columns a description-only resolved doc; `ColumnDoc.origin`
gains `"author"` (companion-produced only); the incremental fingerprint
excludes all three description surfaces.

**Demo:** Runs a full export twice — with and without overrides — against a
synthesized emit: shows the README column line and manifest description
switching to the authored prose (identically on both surfaces), asserts the
written datasets are byte-identical across the two runs, and shows
`compute_fingerprint` unchanged when only a description differs.

**Contracts:** `ColumnDoc` (origin widened), `resolve_column_doc`,
fingerprint exclusion.

**Steps:** none (single implementer) — three shallow source files plus
targeted new tests; no deep surface is read twice.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/reader/documentation.py` |
| Modify | `src/fabulexa_forge/exporters/companion/dictionary.py` |
| Modify | `src/fabulexa_forge/incremental/fingerprint.py` |
| Modify | `tests/exporters/companion/_fixtures.py` |
| Modify | `tests/exporters/companion/test_readme.py` |
| Modify | `tests/exporters/companion/test_manifest.py` |
| Modify | `tests/incremental/test_fingerprint.py` |
| Create | `docs/sprints/desc-override/demos/phase_3_authored_dictionary.py` |

**Tests:**
- Override on a carried column: author prose, `origin == "author"`, source
  unit still inherited; the ns-unit stop still applies (override + temporal
  rendering → unit dropped)
- Override on a projected structural column: author prose wins; the contract
  string and its `_EXPORT_STRUCTURAL_REWRITES` rewrite are not consulted
- Override on a column with no provenance (computed): description-only doc —
  `unit is None`, `origin == "author"` — where today `resolve_column_doc`
  returns None
- Override on the history-interval end column: author prose replaces the
  forge-authored end-of-validity constant; unit resolution through `sim_time`
  unchanged
- Override on a `derived: value_map` column: author prose renders; declared
  enum options remain the post-map list (`resolve_column_enum_options`
  untouched by the override)
- Override present while the source column carries no sidecar documentation:
  author prose still renders
- No `author_descriptions` entry: resolution byte-identical to today
  (existing README/manifest tests unchanged and green)
- README per-column line and manifest per-column `description` render the
  same authored prose for the same report
- Fingerprint unaffected by adding / changing / removing a dimensional
  `description`, a source `descriptions` map, and a base `descriptions` map
  (mirroring the `readme_overlay` tests at
  `tests/incremental/test_fingerprint.py:154–168`); still changes on any
  non-description config change

## What Doesn't Change

- **Streaming** — no companion artifacts, no override surface; streaming
  routing/engine modules untouched
- **Corrupters** — sidecar forwarding verbatim; no corrupt-config surface
- **The reader's documentation view** — `Documentation.column_doc` and its
  contract/sidecar resolution, placeholder substitution, enum glosses are
  untouched; the only reader-module edit is widening `ColumnDoc.origin`'s
  Literal (+ docstring); the reader never emits `"author"`
- **Units and declared-value lists** — `resolve_column_enum_options`,
  `resolve_kind_value_glosses`, `resolve_table_description`, and unit
  inheritance rules stay as-is; no author unit or gloss override
- **`readme_overlay`** — remains the table-/export-level prose channel;
  forwarded `tables[].description` not overridable or suppressible
- **The source event log declaration** — gains no description surface
- **No suppression** — empty/whitespace prose is a load error; there is no
  "render nothing" spelling
- **`init`** — proposes no description stubs
- **Data planes** — writers, SQL compilation, notices, exit codes: datasets
  byte-identical with and without overrides
- **Conformance** — no C1–C15 check touches documentation; none added
- **`build_carried_provenance`, `ColumnProvenance`, `KindValueEntry`** — the
  existing documentation maps and their stamping are untouched

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/config/models.py` | `ColumnDecl.description`, `SourceTableDecl.descriptions`, `RenameEntry.descriptions` + widened validators |
| `src/fabulexa_forge/exporters/query_spec.py` | `QuerySpec.author_descriptions` (defaulted), `TableReport.author_descriptions` (required), forwarded in both `write_query_specs` arms |
| `src/fabulexa_forge/incremental/driver.py` | `_build_windowed_report` forwards `author_descriptions` verbatim |
| `src/fabulexa_forge/exporters/dimensional/engine.py` | Stamp `author_descriptions` from column entries at plan compile |
| `src/fabulexa_forge/exporters/source/plan.py` | Translate `descriptions` through rename resolution; widen rename-key gates to `descriptions` keys |
| `src/fabulexa_forge/exporters/source/engine.py` | Forward the plan unit's map onto `QuerySpec`; event log stamps empty |
| `src/fabulexa_forge/exporters/base/plan.py` | Translate `descriptions` through `_resolve_naming`; widen `_check_column_domain` gates to `descriptions` keys |
| `src/fabulexa_forge/exporters/base/engine.py` | Forward the table spec's map onto `QuerySpec` |
| `src/fabulexa_forge/reader/documentation.py` | `ColumnDoc.origin` Literal gains `"author"` (never reader-produced) |
| `src/fabulexa_forge/exporters/companion/dictionary.py` | `resolve_column_doc` author-first tier |
| `src/fabulexa_forge/incremental/fingerprint.py` | Canonical dump excludes the three description surfaces |
| `tests/config/test_models.py` | New validator tests for the three surfaces |
| `tests/exporters/test_query_spec.py` | Forwarding tests widen to `author_descriptions` |
| `tests/exporters/companion/_fixtures.py` | Fixtures thread `author_descriptions` (Phase 1: empty; Phase 3: authored variants) |
| `tests/exporters/companion/test_manifest.py` | Migration (Phase 1) + author-tier rendering tests (Phase 3) |
| `tests/exporters/companion/test_readme.py` | Migration (Phase 1) + author-tier rendering tests (Phase 3) |
| `tests/exporters/companion/test_artifacts.py` | Migration (Phase 1) |
| `tests/exporters/dimensional/test_provenance.py` | Stamping tests |
| `tests/exporters/source/test_provenance.py` | Stamping tests |
| `tests/exporters/source/test_plan.py` | Descriptions key-gate tests |
| `tests/exporters/base/test_provenance.py` | Stamping tests |
| `tests/exporters/base/test_plan.py` | Descriptions key-gate + descriptions-only-entry tests |
| `tests/incremental/test_driver.py` | Windowed-vs-full `author_descriptions` forwarding test |
| `tests/incremental/test_fingerprint.py` | Description-surface exclusion tests |
| `docs/sprints/desc-override/demos/phase_1_config_carriage.py` | Phase 1 demo |
| `docs/sprints/desc-override/demos/phase_2_plan_stamping.py` | Phase 2 demo |
| `docs/sprints/desc-override/demos/phase_3_authored_dictionary.py` | Phase 3 demo |
