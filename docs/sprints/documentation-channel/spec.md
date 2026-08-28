# Sprint: documentation-channel

## Purpose

Adopt the v9 sidecar's end-to-end documentation channel: a typed documentation
view on the reader, documentation fidelity in the corrupter's base-emit writer,
per-column provenance on the compiled plan feeding a data dictionary in the
companion README + manifest, and documentation annotations on all three `init`
proposal engines.

**Author use case:** an educator exports a dataset and the README/manifest tell
their students what every table and column means (`prop__balance` — "Current
account balance", unit GBP); `init` proposals arrive annotated with the
scenario's own documentation; a corrupted emit keeps its data dictionary.

**Design doc:** `docs/architecture/pending/documentation-channel.md` — the
normative semantics (resolution authorities, inheritance rule, placeholder
substitution, invariants). This spec carries the contracts and phases; it does
not restate the design's rationale. Where a behavior question arises, the
design doc governs.

## Scope

**Capabilities touched:**
- **reader**: `ColumnSpec` + 7 contract attributes, `TableSpec` + `description`,
  the contract-pinned structural-column strings, `Sidecar.documentation()` /
  `Documentation` view (not: any change to the typed values-only `enum_domains`
  routing surface)
- **corrupters**: base-emit writer forwards the new column/table attributes
  through rename/drop/retype (not: new operations, manifest changes)
- **exporters (dimensional / source / base)**: `QuerySpec` / `TableReport` grow
  `provenance` + `kind_values`, stamped at plan compile per mode, forwarded by
  the shared write dispatch and the incremental windowed assembler (not: any
  row/value/ordering change to any export)
- **companion artifacts**: README gains scenario narrative, table descriptions,
  per-column description/unit, declared-value gloss lists; manifest gains the
  machine-readable mirror and bumps `manifest_format_version` to 2
- **init (dimensional / source / streaming)**: YAML-comment annotations
  (scenario, table prose, `sub_types` glosses, per-property description/unit)

**Not included** (explicit deferrals, per the design doc):
- Rendering `min` / `max` / `immutable` / `required` / `extra_data` in any
  artifact (modeled + forwarded only)
- `keys`-block annotations in `init` proposals
- Streaming in-band documentation (streaming's adoption is its `init` engine
  only)
- Modeling `nullable` (design doc § What Doesn't Change)

## Breaking Changes

- **`TableReport` gains two required fields** (`provenance`, `kind_values`) —
  no defaults, so every construction site is loud. Both `src` sites (the shared
  write dispatch, the incremental windowed assembler) are updated in Phase 3;
  the three companion test files that construct `TableReport` directly are
  migrated in the same phase (add empty maps).
- **`build_grain_sql` returns a 5-tuple** (adds the provenance map). Sole
  caller (`dimensional/engine.py`) updated in the same phase.
- **Source/base plan units gain a required `provenance` field**
  (`SourceStateTablePlan`, `SourceJunctionTablePlan`, `SourceEventLogPlan` —
  which also gains `kind_values` — and `BaseTableSpec`). Each is constructed
  only by its own plan builder, updated in the same phase; test helpers that
  construct plan units directly are migrated in the same phase.
- **`manifest_format_version` bumps 1 → 2**; manifest bytes and README text
  change for every export (new fields / sections). Companion tests that pin
  bytes/ordering are rewritten in Phase 5.
- **`init` proposal text changes** (annotation comments). Proposals still parse
  and plan/stream clean (self-gating preserved); init tests asserting exact
  text are updated in Phase 6.
- **Additive with benign internal defaults** (not breaking): `ColumnSpec`'s
  seven new fields and `TableSpec.description` default to `None`; `QuerySpec`'s
  two new maps default to empty. These are absence-detection defaults on
  internal runtime types (the class Principle #7 explicitly permits — not
  author-config fields), and `QuerySpec`'s are additionally forced by the
  existing `keys: TableKeys | None = None` default preceding them. Tests pin
  that every parse/stamp site populates them, so the defaults cannot silently
  stand in for real values.

## Success Criteria

- [ ] `Sidecar.documentation()` answers all five surfaces on a documented
      fixture emit per the design's resolution table (contract strings for
      structural columns with placeholders bound, sidecar verbatim for per-run
      columns, glossed enum options, table prose, scenario narrative)
- [ ] A corrupted emit read back through the reader carries `description` /
      `unit` / `min` / `max` / `immutable` / `required` / `extra_data` and
      `tables[].description` — following a `schema_drift` rename, dropping
      with a drop, surviving a retype
- [ ] All three batch modes stamp `provenance` (source mode also `kind_values`)
      at plan compile; both report-assembly sites forward verbatim onto
      `TableReport`
- [ ] README and manifest render the data dictionary; absence renders as
      absence (README) / JSON `null` (manifest); `manifest_format_version` = 2;
      byte-identical artifacts across repeated runs and across incremental
      windows
- [ ] All three `init` engines annotate proposals; every emitted config still
      parses and plans/streams clean (existing self-gate tests green)
- [ ] `make check` green (lint + typecheck + conformance + tests)

## Contracts

Full runtime-type contracts (`ColumnDoc`, `EnumOption`, `ColumnProvenance`,
`KindValueEntry`, `Documentation`, extended `ColumnSpec` / `TableSpec` /
`QuerySpec` / `TableReport`, `Sidecar.documentation()`) are specified in the
design doc § Interface Contracts and are normative as written there, with the
following implementation bindings decided here:

### Field defaults and placement

```python
# src/fabulexa_forge/reader/sidecar.py
@dataclass(frozen=True)
class ColumnSpec:
    """One column of a base-layer table, as declared in base.json.

    Extended: the seven optional per-column attributes the contract declares
    are carried verbatim (absent -> None), never validated or coerced at
    parse — C1 owns schema conformance. The None defaults are absence
    detection on an internal runtime type, not invented values.
    """

    name: str
    type: str
    references: str | None
    history_tracked: bool | None
    temporal_class: str | None
    description: str | None = None
    unit: str | None = None
    min: float | int | None = None
    max: float | int | None = None
    immutable: bool | None = None
    required: bool | None = None
    extra_data: bool | None = None
```

```python
# src/fabulexa_forge/reader/sidecar.py
@dataclass(frozen=True)
class TableSpec:
    """One table present in run.duckdb, as declared in base.json.

    Extended: `description` carries tables[].description verbatim
    (absent -> None).
    """

    # ... existing fields unchanged ...
    description: str | None = None
```

```python
# src/fabulexa_forge/exporters/query_spec.py
@dataclass(frozen=True)
class ColumnProvenance:
    """The one source column that faithfully fed an output column.

    Stamped at plan compile for columns whose value is the faithful carry
    of exactly one source (table, column). Computed and multi-source
    columns get no entry — absence is the "inherits nothing" answer.
    """

    source_table: str
    source_column: str


@dataclass(frozen=True)
class KindValueEntry:
    """One rendered label of a kind-name-as-value output column.

    label is the post-`kind_labels` rendered value; source_kind names the
    kind whose rows render under it. List order is the plan's event-source
    compile order.
    """

    label: str
    source_kind: str


@dataclass(frozen=True)
class QuerySpec:
    # ... existing fields unchanged, keys: TableKeys | None = None ...
    provenance: Mapping[str, ColumnProvenance] = field(default_factory=dict)
    kind_values: Mapping[str, tuple[KindValueEntry, ...]] = field(
        default_factory=dict
    )
    # Keys are output column names (post-rename). Empty = nothing stamped;
    # every mode engine stamps at plan compile (tests pin per-mode stamping).


@dataclass(frozen=True)
class TableReport:
    # ... existing fields unchanged ...
    provenance: Mapping[str, ColumnProvenance]      # no default — forwarding
    kind_values: Mapping[str, tuple[KindValueEntry, ...]]  # is always explicit
```

### The documentation view (new module)

```python
# src/fabulexa_forge/reader/documentation.py
@dataclass(frozen=True)
class ColumnDoc:
    """Resolved documentation for one declared column.

    origin names the single authority that answered: "contract" for a
    structural column (pinned strings, instance placeholders bound),
    "sidecar" for a per-run column (verbatim carry).
    """

    description: str | None
    unit: str | None
    origin: Literal["contract", "sidecar"]


@dataclass(frozen=True)
class EnumOption:
    """One declared allowed value of a closed-domain property."""

    value: str
    description: str | None


class Documentation:
    """Typed, read-only documentation view over one emit's five surfaces.

    Permissive verbatim carry — nothing validated, nothing inferred,
    absence is silence. Constructed by Sidecar.documentation(); not
    constructed directly. Resolution rule: design doc § The documentation
    view (one authority per column — the vendored contract strings for
    structural columns, keyed by the reader's structural taxonomy; the
    sidecar entry for every other declared column, taxonomy-no-role
    columns included; never both, never a fallback).
    """

    def scenario_description(self) -> str | None:
        """The run's declared narrative, verbatim; None when absent."""

    def table_description(self, table_name: str) -> str | None:
        """One table's tables[].description, verbatim.

        Args:
            table_name: A table the sidecar declares.

        Returns:
            The description, or None when the table carries none (always
            None for the fixed `history` table, per the contract).

        Raises:
            TableNotFoundError: table_name is not declared by the sidecar.
        """

    def column_doc(self, table_name: str, column_name: str) -> ColumnDoc | None:
        """Resolve one declared column's documentation.

        Structural columns (per the reader's structural taxonomy) answer
        from the vendored contract strings with instance-bound placeholders
        substituted (`<name>` from the column name, `<K>`/`<kind>` from the
        table; `history`-family placeholders stay verbatim); every other
        column answers from its sidecar entry.

        Args:
            table_name: A table the sidecar declares.
            column_name: A column that table declares.

        Returns:
            The resolved ColumnDoc; None when a per-run column carries
            neither description nor unit. Structural columns always answer.

        Raises:
            TableNotFoundError: table_name is not declared by the sidecar.
            ColumnNotFoundError: column_name is not declared by that table.
        """

    def enum_options(self, kind: str, prop: str) -> tuple[EnumOption, ...]:
        """The ordered declared value objects of one closed-domain property.

        Membership and order equal the typed values-only enum_domains
        surface — one parse floor shared by both views (a malformed value
        object drops whole from both; a mis-shaped gloss parses as
        gloss-absent). A sub-typed kind's discriminator is `<kind>_type`.

        Args:
            kind: A kind with an enum_domains entry.
            prop: A property in that kind's entry.

        Returns:
            The declared options in sidecar order, glosses verbatim.

        Raises:
            KeyError: (kind, prop) has no enum_domains entry.
        """
```

The vendored contract's § Structural column descriptions block is pinned in
this module as a private literal (the same hardcoding class as the pinned
column lists and the table-category enum; re-synced on contract re-vendor).

```python
# src/fabulexa_forge/reader/sidecar.py
class Sidecar:
    def documentation(self) -> Documentation:
        """The emit's documentation view.

        Lazy: constructed on first call and cached. Never raises on
        construction — documentation has no consistency rules; identifier
        errors surface per query.

        Returns:
            The Documentation view over this sidecar.
        """
```

Parse-floor delta (`_parse_column` / `_parse_table`, internal): each new
attribute parses verbatim when correctly typed, else `None` — a non-string
`description`, a boolean `min` (`isinstance(x, bool)` excluded from the
numeric parse), a non-bool `immutable` all parse as absent. No new refusals.

### Fixture-builder support (test infrastructure)

```python
# tests/_support/sidecar_builder.py
def prop_column(
    name: str,
    type: str,
    *,
    history_tracked: bool,
    temporal_class: "TemporalClass",
    references: str | None = None,
    description: str | None = None,
    unit: str | None = None,
    min: float | int | None = None,
    max: float | int | None = None,
    immutable: Literal[True] | None = None,
    required: Literal[True] | None = None,
    extra_data: Literal[True] | None = None,
) -> dict[str, object]:
    """Extended: the seven optional attributes are emitted into the column
    dict iff not None. The three flags are typed Literal[True] — the schema
    never renders them false, and this constructor builds only conformant
    columns (negative variants mutate the returned dict, as today).

    Existing Args/Returns/Raises unchanged.
    """
```

Table-level `description` needs no builder change — fixture tables are raw
dicts and the key rides through `write_emit`'s schema validation as-is.

### Corrupter forwarding (behavioral delta, no signature change)

`corrupters/base_writer.py` — `_build_table_entry` extends its existing
attribute rule over the seven new `ColumnSpec` attributes (declared verbatim
when the working spec carries one, absent otherwise — never `null`, never
re-looked-up from the source sidecar) and emits the table-level `description`
from `WorkingTable.spec.description` when present. `write_base_emit`'s
docstring names the enlarged attribute set. No other writer change.

### Per-mode provenance stamping

```python
# src/fabulexa_forge/exporters/dimensional/grains.py
def build_grain_sql(
    ...existing params unchanged...
) -> tuple[
    str,
    Literal["create", "append", "replace"],
    str | None,
    str | None,
    Mapping[str, ColumnProvenance],
]:
    """Extended: the fifth element is the per-output-column provenance map
    for this table — one entry per faithfully carried column (projection,
    rename, cast-back, temporal/value rendering election, `lookup` — the
    looked-up property's (table, column)), keyed by output column name.
    Computed columns (derived measures, elapsed, seq, SCD-2 valid_from /
    valid_to, re-derived identity surfaces) get no entry. Existing
    Args/Returns/Raises unchanged otherwise.
    """
```

`dimensional/engine.py` `build_query_specs` stamps each spec's `provenance`
from the fifth element; dimensional has no kind-name-as-value column, so
`kind_values` stays empty (a pinned test fact, not a default left to chance).

Source plan units (`source/plan.py`, `source/events.py`) gain required
fields, stamped by their own builders inside `build_source_plan`:

- `SourceStateTablePlan.provenance: Mapping[str, ColumnProvenance]`
- `SourceJunctionTablePlan.provenance: Mapping[str, ColumnProvenance]`
- `SourceEventLogPlan.provenance: Mapping[str, ColumnProvenance]` and
  `SourceEventLogPlan.kind_values: Mapping[str, tuple[KindValueEntry, ...]]`
  — the `item_type` column's entry, ordered by the plan's event-source
  compile order; per-source labels post-`kind_labels`. Computed log columns
  (`id`, `event_type`, `changes`, the elected `item_id`, the event-time
  column) get no provenance entry.

`source/engine.py` (`_compile_table_spec`, the event-log spec construction)
copies the unit's maps onto the `QuerySpec` verbatim.

`base/plan.py` `BaseTableSpec` gains
`provenance: Mapping[str, ColumnProvenance]`, stamped by `build_base_plan`:
faithfully projected structural/payload columns (rename and cast-back
included) carry their source `(records__<kind>, column)`; the re-derived
`<kind>_key` / `<p>_key` columns and any horizon-re-derived index surface
get no entry (design doc § Re-derived keys are computed, not carried).
`base/engine.py` copies it onto each spec.

### Report forwarding (behavioral delta, no signature change)

`exporters/query_spec.py` `write_query_specs` and the incremental driver's
windowed report assembler (`incremental/driver.py`) forward `spec.provenance`
and `spec.kind_values` verbatim onto every `TableReport` they construct.
Builders never re-derive provenance from SQL, config, or the materialized
schema.

### Companion artifacts (behavioral delta, no signature change)

`companion/readme.py` `render_readme` — ordering delta per design doc
§ README ordering: scenario narrative in the overview position after the
author's overlay `overview`; each table section renders overlay note →
forwarded table description → column inventory now carrying description and
unit per column → declared-value gloss lists for closed-domain and
kind-name-as-value columns → row count. Every rendered string resolves
through `emit.sidecar.documentation()` via the report's provenance entries;
a column with no entry, and any absent attribute, renders nothing.

`companion/manifest.py` `build_manifest_document` — adds top-level
`scenario_description`, per-table `description`, per-column `description`,
`unit`, and `enum_options` (ordered `[{value, description}]` where the
column's single-source property carries a declared domain); absent → JSON
`null` (stable-field-set posture, matching `primary_key`).
`_MANIFEST_FORMAT_VERSION = 2`. Pinned byte form otherwise unchanged.

### `init` annotations (behavioral delta, no signature changes)

The three engines (`dimensional/init.py`, `source/init.py`,
`streaming/init.py`) annotate their emitted YAML with comments per the design
doc's § `init` annotations table: scenario block at the top; source-table
description on table/dim/fact/stream stubs; discriminator glosses on
`sub_types` values; property `description` (unit appended) on proposed
property/column entries — a membership reference field's annotation reads the
`member__<f>__kind` column's entry. Commented-out alternatives carry the same
annotations inside their commented bodies. Comments never alter grammar;
proposals stay pure functions of `(emit, code version)`.

## Phases

### Phase 1: Reader documentation surface

**Delivers:** `ColumnSpec` / `TableSpec` growth + parse floor, the
`Documentation` view with the contract-pinned structural strings,
`Sidecar.documentation()`, fixture-builder support.

**Demo:** builds a documented fixture emit (scenario narrative, table
description, documented + undocumented `prop__` columns, glossed enum domain)
and prints the resolved dictionary: a structural column's contract string with
its placeholder bound, a payload column's sidecar prose, an undocumented
column's silence, the glossed options, the narrative.

**Contracts:** extended `ColumnSpec` / `TableSpec`, `Documentation`,
`ColumnDoc`, `EnumOption`, `Sidecar.documentation()`, `prop_column`.

**Steps:** `source → author (2 files)`

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/reader/sidecar.py` |
| Create | `src/fabulexa_forge/reader/documentation.py` |
| Modify | `src/fabulexa_forge/reader/__init__.py` |
| Modify | `tests/_support/sidecar_builder.py` |
| Create | `tests/reader/test_documentation.py` |
| Modify | `tests/reader/test_sidecar.py` |
| Create | `docs/sprints/documentation-channel/demos/phase_1_documentation_view.py` |

**Tests:**
- Parse floor (`test_sidecar.py`): each of the seven attributes parses
  verbatim when well-typed; a non-string `description`, a boolean `min`, an
  int `immutable` each parse as `None`; absent attributes are `None`;
  `tables[].description` parses verbatim / absent → `None`
- Resolution table (`test_documentation.py`), one case per row: `history`
  column → pinned string + unit verbatim (placeholders NOT substituted);
  records structural column → pinned string (`created_sim_time` carries
  `unit: "ns"`); `ref_index__opened_by` → `<name>` bound to `opened_by`;
  membership structural column → `<K>` bound from the table; `prop__` column
  with description+unit → sidecar verbatim, origin `"sidecar"`; `prop__`
  column with neither → `None`; unit-only column → `ColumnDoc(description=None,
  unit=...)`; a declared no-role column (arbitrary name) → sidecar answer;
  a structural column whose sidecar entry (defectively) carries a description
  → contract answer wins (one authority, never both)
- `table_description`: present verbatim; absent → `None`; `history` → `None`;
  unknown table → `TableNotFoundError`; unknown column →
  `ColumnNotFoundError`
- `enum_options`: order + membership equal the typed `enum_domains` tuple;
  glosses verbatim; gloss-absent → `description=None`; a malformed value
  object absent from both views; mis-shaped gloss → gloss-absent, value kept;
  unknown `(kind, prop)` → `KeyError`; discriminator via `<kind>_type`
- `scenario_description`: verbatim; absent → `None`
- Laziness: two `documentation()` calls return the same object
- `prop_column`: new kwargs emitted iff not None; `write_emit` still
  schema-validates a documented fixture
- Existing reader suite green (additive fields default to `None`)

### Phase 2: Corrupter documentation fidelity

**Delivers:** the base-emit writer forwards the seven column attributes and
the table description under the existing round-trip rule.

**Demo:** corrupts a documented fixture emit with a `schema_drift` rename +
drop + retype, reopens the output, and prints: the renamed column carrying
its original description under its new name, the dropped column absent, the
retyped column's description intact, the table description forwarded.

**Contracts:** `_build_table_entry` / `write_base_emit` behavioral delta.

**Steps:** none (single implementer)

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/corrupters/base_writer.py` |
| Modify | `tests/corrupters/test_base_writer.py` |
| Create | `docs/sprints/documentation-channel/demos/phase_2_corrupter_fidelity.py` |

**Tests:**
- Each of the seven attributes forwards verbatim on an untouched column;
  absent stays absent (key omitted, never `null`)
- A `schema_drift`-renamed column carries its attributes under the new name
- A dropped column's attributes vanish with it
- A retyped column keeps `description` / `unit` (meaning outlives the type)
- `tables[].description` forwards per surviving table; absent stays absent
- Structural columns gain nothing (their sidecar entries carry no
  documentation by contract)
- Output emit still passes structural conformance (existing end-to-end test
  extended with documented fixtures)
- Attributes are never re-looked-up from the source sidecar (rename case
  proves the join is by post-drift name)

### Phase 3: Provenance carriage + dimensional stamping

**Delivers:** `ColumnProvenance` / `KindValueEntry`, the `QuerySpec` /
`TableReport` growth, verbatim forwarding at both report-assembly sites, and
the dimensional mode's provenance stamping.

**Demo:** compiles a dimensional plan (dim with rename + `lookup` + derived
column; fact with elapsed + `seq`) against a fixture emit and prints each
spec's provenance map: carried columns with their `(table, column)` source,
computed columns absent; then runs the full export and shows the
`TableReport` maps equal the spec maps.

**Contracts:** `ColumnProvenance`, `KindValueEntry`, extended `QuerySpec` /
`TableReport`, `build_grain_sql` 5-tuple, forwarding deltas.

**Steps:** `source → migrate (fan-out, 3 files) → author (3 files)`

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/query_spec.py` |
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/grains.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/scd.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/columns.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/engine.py` |
| Modify | `tests/exporters/companion/test_manifest.py` |
| Modify | `tests/exporters/companion/test_readme.py` |
| Modify | `tests/exporters/companion/test_artifacts.py` |
| Create | `tests/exporters/dimensional/test_provenance.py` |
| Modify | `tests/exporters/test_query_spec.py` |
| Modify | `tests/incremental/test_driver.py` |
| Create | `docs/sprints/documentation-channel/demos/phase_3_dimensional_provenance.py` |

**Tests:**
- Dim pass-through column → `(records__<kind>, prop__<x>)`; renamed column
  keyed by output name; `lookup` column → looked-up `(table, column)`;
  temporal-rendered column keeps its provenance entry (unit inheritance is
  the builders' concern, Phase 5); derived / SCD-2 `valid_from` / `valid_to`
  → no entry; dim key column: elected surface projected as stored → entry;
  fact grain columns: carried → entry, elapsed / `seq` → none
- `kind_values` empty on every dimensional spec (pinned fact)
- `write_query_specs` forwards both maps verbatim onto `TableReport`
  (`test_query_spec.py`)
- The windowed report assembler forwards both maps verbatim
  (`test_driver.py`); windowed and full stamping identical for the same table
- Migrated companion tests: `TableReport(..., provenance={}, kind_values={})`
  constructions compile and pass unchanged otherwise
- Determinism: two compiles of the same plan yield equal maps

### Phase 4: Source + base provenance stamping

**Delivers:** provenance on the source plan units (state / junction / event
log, the latter with `kind_values`) and on `BaseTableSpec`, copied onto specs
by both engines.

**Demo:** compiles a source plan (state table with rename, junction, event
log with two sources and a `kind_labels` mapping) and a base plan (kind with
a reference edge) against a fixture emit; prints per-table provenance, the
event log's ordered `kind_values` gloss keys, and base's key columns
(`<kind>_key` / `<p>_key`) absent from the map while their id-space siblings
carry entries.

**Contracts:** plan-unit field additions; engine copy deltas.

**Steps:** `source → author (2 files)`

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/source/events.py` |
| Modify | `src/fabulexa_forge/exporters/source/engine.py` |
| Modify | `src/fabulexa_forge/exporters/base/plan.py` |
| Modify | `src/fabulexa_forge/exporters/base/engine.py` |
| Create | `tests/exporters/source/test_provenance.py` |
| Create | `tests/exporters/base/test_provenance.py` |
| Create | `docs/sprints/documentation-channel/demos/phase_4_source_base_provenance.py` |

**Tests:**
- Source state table: carried column → entry keyed post-rename;
  temporal/value-rendered column keeps its entry; elected identity column
  projected as stored → entry; computed columns → none
- Junction table: carried membership columns → entries against the
  `membership__<K>__<p>` table
- Event log: `item_type` in `kind_values`, entries ordered by event-source
  compile order, labels post-`kind_labels`, `source_kind` the raw kind;
  `id` / `event_type` / `changes` / `item_id` / event-time → no provenance
- Base: projected payload + structural columns → entries (rename and
  cast-back included); `<kind>_key` / `<p>_key` → no entry; under a
  `slice_at` horizon the re-derivation posture is unchanged (key columns
  still absent from the map)
- Plan units constructed only by their builders (existing test helpers
  constructing units directly migrated with explicit maps)
- Determinism: repeated plan builds yield equal maps

### Phase 5: Companion data dictionary

**Delivers:** the README ordering delta and the manifest's machine-readable
documentation mirror, both resolved through the documentation view via the
report's provenance.

**Demo:** full-exports a documented fixture emit (source mode, with an
overlay) and prints: the README overview showing author prose then scenario
narrative, one table section with forwarded description + documented column
inventory + a gloss list, and the manifest's per-column entries with `null`
for undocumented columns and `manifest_format_version: 2`.

**Contracts:** `render_readme` / `build_manifest_document` behavioral deltas.

**Steps:** `source → author (3 files)`

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/companion/readme.py` |
| Modify | `src/fabulexa_forge/exporters/companion/manifest.py` |
| Modify | `tests/exporters/companion/test_readme.py` |
| Modify | `tests/exporters/companion/test_manifest.py` |
| Modify | `tests/exporters/companion/test_artifacts.py` |
| Create | `docs/sprints/documentation-channel/demos/phase_5_data_dictionary.py` |

**Tests:**
- README ordering: overlay `overview` before scenario narrative; either/both
  absent renders nothing; table section order overlay note → table
  description → column inventory → gloss lists → row count
- Column inventory: description + unit per documented column; undocumented
  column renders name/type only — no placeholder prose; carried structural
  column renders its contract string with bound placeholders; unit dropped
  where a temporal/`instant` rendering changed the unit-bearing form,
  description kept; `decimal` / cast-back keep both
- Gloss lists: closed-domain column renders its declared values with glosses
  where present; kind-name-as-value column renders per-label glosses from the
  source kind's table description, label without prose when absent
- Manifest: top-level `scenario_description`; per-table `description`;
  per-column `description` / `unit` / `enum_options`; absence → `null`;
  `manifest_format_version` = 2; byte form pinned (sorted keys, list order
  semantic)
- Determinism: byte-identical README + manifest across repeated runs; a
  windowed incremental run renders identical documentation every window
- Inertness: dataset bytes and table sets identical with and without
  documentation present in the sidecar

### Phase 6: Init documentation annotations

**Delivers:** annotated proposals from all three engines.

**Demo:** runs the dimensional, source, and streaming proposal engines
against a documented fixture emit and prints the emitted YAML: scenario
comment block at top, table-description comments on stubs, `sub_types` gloss
comments, per-property description/unit comments, an annotated commented-out
alternative — then parses each emitted config to prove self-gating.

**Contracts:** engine behavioral deltas (no signature changes).

**Steps:** `source → author (3 files)`

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/dimensional/init.py` |
| Modify | `src/fabulexa_forge/exporters/source/init.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/init.py` |
| Modify | `tests/test_cli_init.py` |
| Modify | `tests/exporters/source/test_init.py` |
| Modify | `tests/exporters/streaming/test_init.py` |
| Create | `docs/sprints/documentation-channel/demos/phase_6_init_annotations.py` |

**Tests:**
- Per engine: scenario comment present when declared, absent when not; table
  stub carries source-table description comment (dim/fact stub: its source
  kind's); `sub_types` values carry discriminator glosses; proposed
  property/column entries carry description (+ unit appended); membership
  reference field annotated from `member__<f>__kind`; undocumented items get
  no comment — no placeholder
- Block-style rendering of an annotated list parses to the identical value as
  the previous flow-style form
- Commented-out alternatives carry annotations inside their commented bodies
- The `keys` block carries no annotations
- Every existing self-gate test still green (emitted configs parse and
  plan/stream clean); proposals remain pure functions of the emit
- Undocumented emit → proposals byte-equal to a comment-free rendering of the
  same proposal content (no annotation machinery residue)

## What Doesn't Change

- **The typed `enum_domains` surface** (`Sidecar.enum_domains`) stays a
  values-only ordered mapping; every routing consumer untouched.
- **Conformance C1–C15** — no new or changed checks
  (`reader/conformance.py` untouched).
- **The overlay grammar** (`companion/overlay.py`) — slots, refusals,
  precedence unchanged; author prose renders first.
- **The incremental fingerprint** (`incremental/fingerprint.py`, cursor) —
  documentation is run-level and part of neither.
- **Streaming delivery** — no stream event, format, or sink change; the
  streaming adoption is `init.py` only.
- **The `keys` proposal** (`exporters/keys_init.py`) — no annotations.
- **README mode templates** (`companion/templates/*.md`) — computed-column
  meaning already lives there; no template edits this sprint.
- **Writers, compare, datasets, playback, mixer** — untouched.
- **Export data** — no exporter's row membership, linkage, ordering, or
  values change; documentation is presentation (design-doc invariant).
- **`nullable`** stays unmodeled (design doc § What Doesn't Change).

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/reader/sidecar.py` | `ColumnSpec` +7 attrs, `TableSpec` +`description`, parse floor, `Sidecar.documentation()` |
| `src/fabulexa_forge/reader/documentation.py` | New: `Documentation` / `ColumnDoc` / `EnumOption` + pinned contract strings |
| `src/fabulexa_forge/reader/__init__.py` | Export the new documentation types |
| `src/fabulexa_forge/corrupters/base_writer.py` | Forward the 7 column attrs + table description (existing round-trip rule) |
| `src/fabulexa_forge/exporters/query_spec.py` | `ColumnProvenance` / `KindValueEntry`; `QuerySpec` / `TableReport` growth; dispatch forwarding |
| `src/fabulexa_forge/incremental/driver.py` | Windowed report assembler forwards the two maps |
| `src/fabulexa_forge/exporters/dimensional/grains.py` | `build_grain_sql` 5-tuple; per-grain provenance |
| `src/fabulexa_forge/exporters/dimensional/scd.py` | SCD-2 column provenance |
| `src/fabulexa_forge/exporters/dimensional/columns.py` | Column-resolution provenance support |
| `src/fabulexa_forge/exporters/dimensional/engine.py` | Stamp specs from the compile |
| `src/fabulexa_forge/exporters/source/plan.py` | State/junction/event-log unit provenance + `kind_values` stamping |
| `src/fabulexa_forge/exporters/source/events.py` | `SourceEventLogPlan` fields |
| `src/fabulexa_forge/exporters/source/engine.py` | Copy unit maps onto specs |
| `src/fabulexa_forge/exporters/base/plan.py` | `BaseTableSpec.provenance` stamping |
| `src/fabulexa_forge/exporters/base/engine.py` | Copy onto specs |
| `src/fabulexa_forge/exporters/companion/readme.py` | Data-dictionary README ordering delta |
| `src/fabulexa_forge/exporters/companion/manifest.py` | Documentation mirror; version 2 |
| `src/fabulexa_forge/exporters/dimensional/init.py` | Annotation comments |
| `src/fabulexa_forge/exporters/source/init.py` | Annotation comments |
| `src/fabulexa_forge/exporters/streaming/init.py` | Annotation comments |
| `tests/_support/sidecar_builder.py` | `prop_column` documentation kwargs |
| `tests/reader/test_documentation.py` | New: resolution-table suite |
| `tests/reader/test_sidecar.py` | Parse-floor cases for the new attrs |
| `tests/corrupters/test_base_writer.py` | Forwarding cases |
| `tests/exporters/dimensional/test_provenance.py` | New: dimensional stamping suite |
| `tests/exporters/source/test_provenance.py` | New: source stamping suite |
| `tests/exporters/base/test_provenance.py` | New: base stamping suite |
| `tests/exporters/test_query_spec.py` | Dispatch forwarding cases |
| `tests/incremental/test_driver.py` | Windowed forwarding cases |
| `tests/exporters/companion/test_manifest.py` | `TableReport` migration (P3); dictionary + version rewrites (P5) |
| `tests/exporters/companion/test_readme.py` | `TableReport` migration (P3); ordering + dictionary rewrites (P5) |
| `tests/exporters/companion/test_artifacts.py` | `TableReport` migration (P3); determinism/inertness cases (P5) |
| `tests/test_cli_init.py` | Dimensional annotation cases |
| `tests/exporters/source/test_init.py` | Source annotation cases |
| `tests/exporters/streaming/test_init.py` | Streaming annotation cases |
| `docs/sprints/documentation-channel/demos/phase_[1-6]_*.py` | One demo per phase |
