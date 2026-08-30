# Sprint: table-descriptions

## Purpose

Complete the documentation channel's two remaining rendered surfaces: an author
table-level `description` override on the dimensional, source, and base modes
(the table-granularity twin of the shipped per-column override), and
forge-pinned documentation for the source event log's table and six columns.
An author writes `description:` on a table declaration and the companion README
and manifest render that prose in place of the forwarded sidecar description;
every source export's event log renders a complete data dictionary with no
config at all.

Design doc: `docs/architecture/pending/table-and-event-log-descriptions.md`
(semantics, rationale, pinned prose — the WHY). This spec carries contracts,
phases, and test cases (the WHAT).

## Scope

**Capabilities touched:**
- Documentation channel: author table-description tier, forge-pinned event-log
  tier (table + six columns), `forge` origin in the companion dictionary
- Export-config models: optional `description` on `TableDecl`,
  `SourceTableDecl`, `RenameEntry`
- Compiled-plan carriage: `author_table_description` + `event_log` on
  `QuerySpec`/`TableReport`, forwarded by both report-assembly sites
- Incremental driver: fingerprint exclusion of the three new fields

**Not included:** everything the design doc's What Doesn't Change locks —
per-column override surfaces, `readme_overlay` semantics, the reader's
documentation view (beyond the `ColumnDoc.origin` Literal widening), gloss
lists, the event log's data surface, streams/corrupter, `init` annotations,
dataset bytes.

## Breaking Changes

- **`TableReport` gains two required fields** (`author_table_description`,
  `event_log`) with no defaults — matching the three documentation maps, so
  every report-assembly call site states them explicitly. Internal runtime
  type; every constructor site (2 in `query_spec.py`, 1 in
  `incremental/driver.py`, plus the companion test fixtures) is migrated in
  Phase 2. `QuerySpec` gains the same two fields *with* benign defaults
  (`None` / `False`), matching its existing `provenance`/`kind_values` style —
  internal runtime fields, not author config, so no Principle-#7 conflict.
- **`RenameEntry`'s at-least-one-field rule widens** to
  `name` / `columns` / `descriptions` / `description` — purely additive;
  every existing config still parses.
- **`ColumnDoc.origin` Literal gains `"forge"`** — additive; the reader's
  documentation view still produces only `contract` / `sidecar`.

Everything else is additive.

## Success Criteria

- [ ] `description:` on a dimensional table entry, a source declared table, and
      a base rename entry renders in the README table section and the
      manifest's per-table `description`, replacing the sidecar forward
- [ ] A base rename entry carrying only `description` is a legal entry
- [ ] A `description` key on the source events declaration is a parse error
- [ ] A source export's event log renders the pinned table description and all
      six pinned column descriptions (origin `forge`) in README and manifest,
      even against an undocumented emit; `item_type`'s gloss list still
      renders beneath the pinned description
- [ ] Adding/changing/removing any of the three `description` fields never
      changes the incremental fingerprint
- [ ] Companions byte-deterministic; datasets byte-identical with or without
      any new field (existing suite green)

## Contracts

No implementation code — signatures and docstrings only. Full semantics in the
design doc §§ Semantics / Interface Contracts.

### Config models (`src/fabulexa_forge/config/models.py`)

Each of the three models gains one optional field; when present it must be
non-empty and non-whitespace (the column-override string rule), enforced in the
model's existing after-validator:

```python
class TableDecl(StrictBaseModel):
    description: str | None = None
    """Author-supplied rendered description for this output table. Replaces
    the forwarded source-table description in the companion README and
    manifest. Absent -> forwarding as before."""

class SourceTableDecl(StrictBaseModel):
    description: str | None = None
    """Author-supplied rendered description for this output table. Replaces
    the forwarded source-table description in the companion README and
    manifest. Absent -> forwarding as before."""

class RenameEntry(StrictBaseModel):
    description: str | None = None
    """Author-supplied rendered description for the entry's target table.
    Replaces the forwarded source-table description in the companion README
    and manifest. Counts toward the entry's at-least-one-field rule.
    Absent -> forwarding as before."""
```

`RenameEntry.entry_well_formed` widens: at least one of
`name` / `columns` / `descriptions` / `description` is set; `description`,
when present, is non-empty and non-whitespace. `SourceEventsDecl` is
unchanged — strict models already make a `description` key on it a parse
error; a test states that contract.

### Carriage (`src/fabulexa_forge/exporters/query_spec.py`)

```python
@dataclass(frozen=True)
class QuerySpec:
    author_table_description: str | None = None
    """The mode's table-level override translated at plan compile; None
    means no override. Forwarded verbatim to TableReport."""
    event_log: bool = False
    """True iff this spec is the source mode's compiled polymorphic event
    log — the one table whose documentation the companion dictionary answers
    from the forge-pinned event-log set. Stamped only by the source plan
    compiler."""

@dataclass(frozen=True)
class TableReport:
    author_table_description: str | None
    event_log: bool
    # Both forwarded verbatim from the compiled QuerySpec — no default, so
    # every report-assembly call site states them explicitly, matching the
    # three documentation maps.
```

`write_query_specs` (both fmt arms) and the incremental driver's windowed
report assembler forward both fields verbatim, exactly as they forward
`provenance` / `kind_values` / `author_descriptions`. No builder entry-point
signature changes.

### Stamping (the three plan compilers)

- **dimensional** (`exporters/dimensional/engine.py`): each table's spec
  stamps `author_table_description=table_decl.description`.
- **source** (`exporters/source/plan.py` + `engine.py`): the state/junction
  plan units carry the declaration's `description` (stamped at plan build,
  beside `author_descriptions`); `_compile_table_spec` copies it verbatim.
  The event-log spec compiles with `event_log=True` — the only construction
  site anywhere that sets the marker; a plan with no events declaration marks
  nothing. The log's `author_table_description` stays `None` (no config
  surface exists).
- **base** (`exporters/base/plan.py` + `engine.py`): the flat-table plan unit
  carries the matched rename entry's `description` (stamped where
  `author_descriptions` is stamped today); the spec copies it verbatim.

### Fingerprint (`src/fabulexa_forge/incremental/fingerprint.py`)

`_FINGERPRINT_EXCLUDE` gains the three table-description fields
(`dimensional.tables[].description`, `source.tables[].description`,
`base.rename[].description`), extending the standing rule: documentation is
run-level presentation and can never make a resumed drip refuse. The
`event_log` marker is not config and never enters the fingerprint question.

### Resolution (`src/fabulexa_forge/exporters/companion/dictionary.py`)

Pinned constants live beside `_LEAD_SIM_TIME_DESCRIPTION` /
`_EXPORT_STRUCTURAL_REWRITES`: one event-log table description and a
six-entry column map (`id`, `item_type`, `item_id`, `event`, `occurred_at`,
`changes`), prose exactly as pinned in the design doc § The pinned event-log
documentation.

```python
def resolve_table_description(doc: "Documentation", table: "TableReport") -> str | None:
    """One table's resolved description, author-first.

    Args:
        doc: The emit's documentation view.
        table: The output table report.

    Returns:
        The report's author table description when present; else the pinned
        event-log table description when the report is marked as the event
        log; else the single source table's `tables[].description` when
        every carried column agrees on one source table; else None.
    """

def resolve_column_doc(
    doc: "Documentation", table: "TableReport", column_name: str, output_type: str
) -> "ColumnDoc | None":
    """Unchanged signature. Resolution order gains one clause: on a report
    marked as the event log, a column named in the pinned event-log set
    resolves to a description-only ColumnDoc with origin "forge" (author
    entries cannot exist there; nothing inherits there today). All other
    resolution is unchanged."""
```

### Origin vocabulary (`src/fabulexa_forge/reader/documentation.py`)

```python
class ColumnDoc:
    origin: Literal["contract", "sidecar", "author", "forge"]
    """"forge" names a companion-dictionary resolution answered by the
    forge-pinned event-log column set. Like "author", it is stamped only
    downstream — the reader's documentation view never produces it."""
```

## Phases

### Phase 1: Config surface — table-level description fields

**Delivers:** the three optional `description` fields, load-validated;
`RenameEntry`'s widened at-least-one rule; the events-decl rejection stated as
a test.
**Demo:** parses one YAML config per mode carrying the field and prints the
parsed values; shows a description-only rename entry parsing and a
`description` key on the events declaration failing validation.
**Contracts:** Config models.
**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `tests/config/test_models.py` |
| Modify | `tests/config/test_source_decls.py` |
| Create | `docs/sprints/table-descriptions/demos/phase_1_config_surface.py` |

**Tests:**
- `TableDecl` with `description` parses; empty / whitespace-only rejected
- `SourceTableDecl` with `description` parses; empty / whitespace-only rejected
- `RenameEntry` with only `description` is valid (names untouched)
- `RenameEntry` with none of `name`/`columns`/`descriptions`/`description` still raises
- `RenameEntry.description` empty / whitespace-only rejected
- `SourceEventsDecl` with a `description` key raises (extra_forbidden)
- Docstring-convention test still green (three-channel docstrings on the new fields)
- Existing model tests still pass unchanged

### Phase 2: Compiled-plan carriage + fingerprint exclusion

**Delivers:** `QuerySpec`/`TableReport` carrying `author_table_description` +
`event_log`; all three compilers stamping; both report-assembly sites
forwarding verbatim; the fingerprint exclusion. Atomic: `TableReport`'s
no-default fields leave every un-migrated constructor red until all are
migrated, so source + migration land as one steps pipeline.
**Demo:** builds full-export plans against a fixture emit (source config with
a described table + events declaration; base config with a described rename
entry): prints each spec's `author_table_description` / `event_log`, shows
exactly one marked spec (the log, compiled last), runs an export and prints
the `TableReport` fields forwarded verbatim, and computes the fingerprint
with and without the descriptions — identical.
**Contracts:** Carriage, Stamping, Fingerprint.
**Steps:** `source → migrate (fan-out, 4 files) → author (6 files)`.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/query_spec.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/engine.py` |
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/source/engine.py` |
| Modify | `src/fabulexa_forge/exporters/base/plan.py` |
| Modify | `src/fabulexa_forge/exporters/base/engine.py` |
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Modify | `src/fabulexa_forge/incremental/fingerprint.py` |
| Modify | `tests/exporters/companion/_fixtures.py` |
| Modify | `tests/exporters/companion/test_artifacts.py` |
| Modify | `tests/exporters/companion/test_manifest.py` |
| Modify | `tests/exporters/companion/test_readme.py` |
| Modify | `tests/exporters/test_query_spec.py` |
| Modify | `tests/exporters/dimensional/test_provenance.py` |
| Modify | `tests/exporters/source/test_provenance.py` |
| Modify | `tests/exporters/base/test_provenance.py` |
| Modify | `tests/incremental/test_fingerprint.py` |
| Modify | `tests/incremental/test_driver.py` |
| Create | `docs/sprints/table-descriptions/demos/phase_2_carriage.py` |

**Tests:**
- `write_query_specs` forwards `author_table_description` + `event_log`
  verbatim on both fmt arms (extend the existing forwarding tests)
- Dimensional: a described table's spec stamps the declaration's prose; an
  undescribed table stamps `None`; `event_log` is `False` on every spec
- Source: a described declared table's spec stamps the prose; the event-log
  spec is the only spec with `event_log=True` and stamps
  `author_table_description=None`; a plan with no events declaration marks
  nothing
- Base: a described rename entry's target-table spec stamps the prose; a
  description-only entry compiles and stamps; unmatched tables stamp `None`
- Incremental driver: windowed report's two new fields identical to the full
  export's (extend the existing forwarding-identical test)
- Fingerprint unaffected by adding / changing / removing each of the three
  `description` fields (mirror the existing description-override cases);
  still changes on a data-shaping config change
- Existing suite green after migration (companion fixtures/tests state the two
  new `TableReport` fields explicitly)

### Phase 3: Dictionary resolution — author tier, pinned event-log set, forge origin

**Delivers:** the rendered behavior — author-first table-description
resolution, the pinned event-log table + column documentation, `origin:
"forge"`. `readme.py` / `manifest.py` are untouched: both already render
through the two resolvers.
**Demo:** runs a full `mode: source` export (described table + events
declaration + a `readme_overlay` table note) against a fixture emit and prints
the README's per-table sections and the manifest's `tables` entries: the
described table shows the author prose (overlay note first, both rendered),
the event log shows the pinned table description and six pinned column lines
with `item_type`'s gloss list beneath.
**Contracts:** Resolution, Origin vocabulary.
**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/reader/documentation.py` |
| Modify | `src/fabulexa_forge/exporters/companion/dictionary.py` |
| Modify | `tests/exporters/companion/_fixtures.py` |
| Modify | `tests/exporters/companion/test_readme.py` |
| Modify | `tests/exporters/companion/test_manifest.py` |
| Create | `docs/sprints/table-descriptions/demos/phase_3_rendered_companions.py` |

**Tests:**
- `resolve_table_description`: author override wins over a single-source
  forward; absent override + single-source forwards as today; absent override
  + multi-source resolves `None`; marked event-log report resolves the pinned
  table description; marked report never sees the sidecar forward
- `resolve_column_doc` on a marked report: each of the six pinned columns
  resolves description-only with `origin="forge"`, no unit, no enum options;
  a column outside the pinned set resolves as today; an unmarked report never
  consults the pinned set
- README: described table renders author prose in the description slot;
  overlay `table:` note renders first, then the description — both; event-log
  section renders pinned table prose + six documented column lines;
  `item_type` gloss list renders beneath the pinned description; pinned prose
  renders against an undocumented emit (bare sidecar)
- Manifest: per-table `description` mirrors the README resolution (author /
  pinned / forward / `null`); event-log columns carry the pinned descriptions
- Determinism: two renders byte-identical
- Existing README/manifest tests still pass unchanged

## What Doesn't Change

- `exporters/companion/readme.py` and `manifest.py` — no edits; both render
  through the shared resolvers, which is where the new tiers live
- The per-column author override — surfaces, precedence, key gates, carriage
  untouched; this sprint adds tiers beside it, not under it
- `readme_overlay` — still the additive prose channel; its `table:` note still
  renders before the description line
- The reader's documentation view — still produces only `contract` /
  `sidecar`; the Literal widening is vocabulary, not behavior
- Kind-name-as-value gloss lists — still sidecar-sourced
  (`resolve_kind_value_glosses` untouched); an author table override on a
  declared table never feeds glosses
- The event log's data surface — column set, first id, `item_type` vocabulary,
  `changes` encoding, key election, temporal rendering
- `init` proposal annotations, streaming, playback, writers, corrupters —
  no annotation site, no companion surface
- Dataset bytes — companions are the only artifacts that change

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/config/models.py` | `description` field on `TableDecl` / `SourceTableDecl` / `RenameEntry`; widened at-least-one rule |
| `src/fabulexa_forge/exporters/query_spec.py` | `author_table_description` + `event_log` on `QuerySpec` (defaulted) and `TableReport` (required); `write_query_specs` forwards both |
| `src/fabulexa_forge/exporters/dimensional/engine.py` | Stamp `author_table_description` from the table declaration |
| `src/fabulexa_forge/exporters/source/plan.py` | State/junction plan units carry the declaration's `description` |
| `src/fabulexa_forge/exporters/source/engine.py` | Copy unit description onto specs; mark the event-log spec `event_log=True` |
| `src/fabulexa_forge/exporters/base/plan.py` | Flat-table plan unit carries the matched rename entry's `description` |
| `src/fabulexa_forge/exporters/base/engine.py` | Copy unit description onto specs |
| `src/fabulexa_forge/incremental/driver.py` | Windowed report assembler forwards the two new fields |
| `src/fabulexa_forge/incremental/fingerprint.py` | `_FINGERPRINT_EXCLUDE` gains the three table-description fields |
| `src/fabulexa_forge/reader/documentation.py` | `ColumnDoc.origin` Literal gains `"forge"` |
| `src/fabulexa_forge/exporters/companion/dictionary.py` | Pinned event-log constants; author-first `resolve_table_description`; event-log clause in `resolve_column_doc` |
| `tests/config/test_models.py` | New field validation tests (dimensional, base) |
| `tests/config/test_source_decls.py` | New field validation tests (source, events rejection) |
| `tests/exporters/test_query_spec.py` | Forwarding tests for the two new fields |
| `tests/exporters/companion/_fixtures.py` | Migrate builders to required fields; expose them as knobs |
| `tests/exporters/companion/test_artifacts.py` | Migrate `TableReport` constructions |
| `tests/exporters/companion/test_manifest.py` | Migrate constructions; pinned/author rendering tests |
| `tests/exporters/companion/test_readme.py` | Migrate constructions; pinned/author rendering tests |
| `tests/exporters/dimensional/test_provenance.py` | Stamping tests |
| `tests/exporters/source/test_provenance.py` | Stamping + marker tests |
| `tests/exporters/base/test_provenance.py` | Stamping tests |
| `tests/incremental/test_fingerprint.py` | Exclusion tests for the three fields |
| `tests/incremental/test_driver.py` | Windowed-forwarding test extension |
| `docs/sprints/table-descriptions/demos/phase_1_config_surface.py` | Phase 1 demo |
| `docs/sprints/table-descriptions/demos/phase_2_carriage.py` | Phase 2 demo |
| `docs/sprints/table-descriptions/demos/phase_3_rendered_companions.py` | Phase 3 demo |
