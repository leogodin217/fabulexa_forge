# Sprint: slice-only-policy

## Purpose

Enforce the contract's `slice_only` mandate across every exporter — no output value,
row membership, linkage, or ordering derives from a `slice_only` column's value — and
land the `Notice`/`NoticeSink` channel that makes auto-projection omission honest.
An educator running any export gets honest data: a clear error when their config names
a column whose past is unknowable, and a stderr notice when a mode silently drops one,
instead of a column whose name promises an as-of value and whose content is the
end-of-run slice.

Design rationale, semantics, and per-surface tables:
[`docs/architecture/pending/slice-only-policy.md`](../../architecture/pending/slice-only-policy.md)
(referenced throughout as **the design doc** — this spec does not duplicate its prose).

## Scope

**Capabilities touched:**

- mode-neutral exporter surface: `Notice` + `NoticeSink` + `render_notice_stderr`;
  the shared discriminator-exemption predicate
- dimensional: `SliceOnlyColumnRefused` (all config-referenced value-reads),
  `LookupColumnSafety` re-keyed to `temporal_class: constant`,
  `DiscriminatorValueObserved` migrated off `warnings.warn`, `init` column-proposal
  skip + notices
- source: render narrowing to `tracked` + `constant` + exempt discriminator,
  per-unit×column omission notices, `SourceRenameSliceOnly`
- streaming: `StreamPropertySliceOnly` in the eager pass (refuse-only, no signature
  changes)
- incremental driver + CLI: `notice_sink` threading; CLI supplies the stderr renderer

**Not included:** playback implementation (separate pending doc; this sprint is its
prerequisite); base mode; structured notice fields beyond `code` + `message`; any
change to the reader, derivations layer, window-gated incremental rules' keying,
source genre predicate, corrupters, or config grammar (design doc § What Doesn't
Change).

## Breaking Changes

- **Required `notice_sink` parameter** (no default — Principle #7) added to:
  `build_query_specs`, `export_dimensional`, `validate_table`, `export_window`,
  `export_incremental_next` (Phase 1); `generate_init_config` (Phase 2);
  `build_source_plan`, `build_source_query_specs`, `export_source` (Phase 3).
  Every in-repo caller migrates in the same phase; the CLI is the only external
  surface and its flags/exit codes are unchanged.
- **`LookupColumnSafety` re-key**: tightens (`slice_only` on any consulted column now
  refused; previously `history_tracked: false` admitted it) and loosens (a `tracked`
  sub-typed discriminator terminal is now allowed — the carve-out).
- **Source renders drop non-exempt `slice_only` columns** from every records-genre
  render. Column-projection-only: row sets, `seq`, window membership invariant.
- **`DiscriminatorValueObserved` is no longer a Python warning** — it is a
  `discriminator-value-unobserved` notice (CLI: one stderr line).
- **Streaming refuses** a `kinds[].properties` entry resolving to a non-exempt
  `slice_only` column (was: carried at its current records-table value).
- **Recipe fixture re-class** (test tree only): untracked columns in
  `tests/recipes/_recipe_fixture.py` become `temporal_class: constant` (the honest
  class for immutable names/FKs) — except `prop__staff_type`, kept `slice_only`
  deliberately so the recipes suite exercises the discriminator carve-out end-to-end.

## Success Criteria

- [ ] Every surface in the design doc's § Per-surface behavior table behaves as
      specified: author-named `slice_only` reads refused always-on; auto-projected
      ones omitted with one notice per column; carve-out honored everywhere.
- [ ] Notices are deterministic data: same emit + config + code → identical sequence;
      stderr only; never affect output data, table sets, or exit code.
- [ ] Omission is column-projection-only: event row sets, `seq`, and window
      membership are byte-identical before/after (design doc § Invariants 3).
- [ ] No `warnings.warn` remains in `src/fabulexa_forge/exporters/`.
- [ ] `make check` green (lint, mypy-strict, full suite) at every phase boundary.

## Contracts

Entry-point contracts (signatures + full docstrings) live in the design doc
§ Interface Contracts: `Notice`, `NoticeSink`, `render_notice_stderr`,
`build_query_specs`, `export_dimensional`, `build_source_plan`,
`build_source_query_specs`, `export_source`, `iter_stream_events` (behavior only —
signature unchanged), `export_window`, `export_incremental_next`,
`generate_init_config`. They are binding as written there.

Internal contracts (architect-designed against the shipped code):

### Shared predicate — new module `src/fabulexa_forge/exporters/slice_only.py`

Mode-neutral sibling of `reserved_names.py` / `query_spec.py`. One implementation,
imported by every policing surface (design doc Invariant 5).

```python
def is_exempt_discriminator(sidecar: Sidecar, kind: str, column_name: str) -> bool:
    """
    The discriminator carve-out, applied identically on every surface.

    Args:
        sidecar: The open emit's sidecar (subtype_values is the oracle).
        kind: The records-category kind owning the column.
        column_name: Column name as declared (prop__ prefix included).

    Returns:
        True iff column_name == f"prop__{kind}_type" and
        sidecar.subtype_values(kind) is non-empty. Mechanical; the column's
        class is never consulted (exempt at any class).

    Raises:
        Nothing (subtype_values is total).
    """
```

```python
def is_non_exempt_slice_only(sidecar: Sidecar, kind: str, column_name: str) -> bool:
    """
    The policy-population predicate every policing surface consults.

    Returns False without a class read when column_name lacks the prop__
    prefix (outside the population: identity/lifecycle/membership/history
    columns) or when is_exempt_discriminator is True — exemption
    short-circuits, so an exempt discriminator never triggers a class read.
    Otherwise reads sidecar.temporal_class(f"records__{kind}", column_name).

    Args:
        sidecar: The open emit's sidecar.
        kind: The records-category kind owning the column.
        column_name: Column name as declared.

    Returns:
        True iff the column's temporal_class is 'slice_only' and it is not
        the exempt discriminator.

    Raises:
        TemporalClassUnavailableError: Propagated from the reader —
            unverifiable is refused, never inferred.
        TableNotFoundError, ColumnNotFoundError: records__<kind> or the
            column is absent (callers establish existence first).
    """
```

### Dimensional — `validation.py`, `fk.py`, `lookup.py`, `init.py`

Matches `validation.py`'s existing per-check granularity (small `check_*` functions
raising plain `ExportError`, composed by `validate_table`).

```python
def check_slice_only_filter_keys(
    source: SourceDecl,
    table_decl: TableDecl,
    source_table_name: str,
    sidecar: Sidecar,
) -> None:
    """
    SliceOnlyColumnRefused over records `filter` keys (row membership
    derives from the value). No-op unless grain is records with a filter.
    The exempt discriminator passes — filter on prop__<kind>_type is the
    classification read (init's pre-fill relies on it).

    Raises:
        ExportError: A filter key resolves to a non-exempt slice_only column.
        TemporalClassUnavailableError: Propagated.
    """
```

```python
def check_slice_only_column_reads(
    col_decl: ColumnDecl,
    table_decl: TableDecl,
    source: SourceDecl,
    source_table_name: str,
    sidecar: Sidecar,
) -> None:
    """
    SliceOnlyColumnRefused over one column's own value-reads: `from`,
    `correlation`, resolved `value_map.from`, `derived: timestamp` source,
    `derived: elapsed` correlate_on/start_source/end_source/other_where
    keys. Only prop__-named references on the kind's records table are in
    the population (the predicate scopes); membership/history grain surface
    columns are classless and pass untouched. lookup is
    check_lookup_temporal_safety's; fk hops are check_fk_slice_only's.
    Always-on — runs in full and windowed exports alike.

    Raises:
        ExportError: A read resolves to a non-exempt slice_only column;
            message names output table.column, base table.column, the
            class, and the slice-only contract fact.
        TemporalClassUnavailableError: Propagated.
    """
```

```python
def check_fk_slice_only(
    col_decl: ColumnDecl,
    table_decl: TableDecl,
    source_grain: str,
    anchor_kind: str,
    target_kind: str,
    sidecar: Sidecar,
) -> None:
    """
    SliceOnlyColumnRefused over an fk column's traversed hops. via:
    reference — the resolved hop chain (path hint or unique pathfind, the
    same helpers build_reference_fk_expr uses), each hop's kind advanced
    via ColumnSpec.references. via: membership with as_of — the member_path
    hop chain plus the as_of column on records__<anchor_kind>. Plain
    membership fk consults no classed column (member/element columns are
    classless): no-op. Called from validate_table immediately after
    build_fk_expr, so path-resolution failures keep their existing messages.

    Raises:
        ExportError: A traversed hop or the as_of column is non-exempt
            slice_only.
        TemporalClassUnavailableError: Propagated.
    """
```

```python
def validate_table(
    table_decl: TableDecl,
    config: DimensionalConfig,
    sidecar: Sidecar,
    window: Window | None,
    notice_sink: NoticeSink,
) -> str:
    """
    Unchanged contract plus: runs SliceOnlyColumnRefused always-on
    (check_slice_only_filter_keys table-level; check_slice_only_column_reads
    and check_fk_slice_only per column — Phase 2); threads notice_sink to
    check_discriminator_value_observed (Phase 1). notice_sink is required.

    Returns:
        The resolved DuckDB source table name.

    Raises:
        ExportError: Any business rule.
        TemporalClassUnavailableError: Non-conformant temporal pair.
    """
```

```python
def check_discriminator_value_observed(
    source: SourceDecl,
    sidecar: Sidecar,
    notice_sink: NoticeSink,
) -> None:
    """
    Emit a 'discriminator-value-unobserved' Notice (not warnings.warn) when
    a records filter value is absent from the kind's enum_domains observed
    values. Check logic and message text unchanged — the former warning
    string becomes Notice.message verbatim. Never raises; never affects
    output data or exit code.
    """
```

`check_lookup_temporal_safety` (`lookup.py:156`) — **signature unchanged**; re-keyed
behavior per its new docstring: clauses (0)–(2) unchanged; the emit-wide
`history_tracked` precheck is dropped; each consulted column (terminal property and
every traversed hop) resolves through `Sidecar.temporal_class` and any class other
than `constant` is refused naming the column and its class — the exempt discriminator
passes at any class, per consulted column, no terminal-vs-hop special case
(deliberately admitting a `tracked` discriminator terminal the old bit-keying
refused). `TemporalClassUnavailableError` propagates, never inferred.
`check_scd2_needs_history` keeps its `history_tracked` keying — untouched.

`init.py` deltas: `_build_candidate_yaml(emit, notice_sink) -> str` threads the sink;
`_write_dim_scd2_stub(w, kind, name, all_tables, sidecar, notice_sink, filter_line)`
skips non-exempt `slice_only` columns from the payload/presentation proposal loop
(via `is_non_exempt_slice_only`), one `slice-only-column-omitted` notice per skip
naming kind and column in sidecar column order; the exempt discriminator remains
proposable; `filter_line` loses its `=None` default (Principle #7). The type1/fact
stub writers emit no prop proposals — unchanged, no skip, no notice.

### Source — `plan.py` (+ one `renders.py` behavior delta), `errors.py`

```python
def _omitted_slice_only_columns(sidecar: Sidecar, kind: str) -> tuple[str, ...]:
    """
    The unit-invariant omitted set for one records kind: every non-exempt
    temporal_class: slice_only prop__ column of records__<kind>, in sidecar
    column-declaration order (is_non_exempt_slice_only per column). Never
    called for junction units (membership columns carry no class).

    Raises:
        TemporalClassUnavailableError: Propagated.
    """
```

Changed signatures — each gains `omitted: frozenset[str]` and drops those sources
from its returned pairs: `_changelog_columns(sidecar, kind, omitted)` (additionally
narrows the property set passed to the stream-column resolution),
`_snapshot_columns(sidecar, kind, omitted)`,
`_records_columns(sidecar, source_table, drop_discriminator, omitted)`,
`_default_columns(sidecar, unit, change_delivery, omitted)` (passes `frozenset()`
for junction units). `_apply_rename_entry(entry, default_name, default_columns,
omitted)` raises `SourceRenameSliceOnly` when a columns key names an omitted column,
before the existing not-a-source-column check.
`_resolve_specs(sidecar, units, rename, change_delivery, notice_sink)` is the
emission point: per unit, computes the omitted set and emits one
`slice-only-column-omitted` notice per unit × column — unit order, then sidecar
column order — naming the unit (source table + sub-type) and column, before rename
resolution and spec assembly. `_check_collisions` unchanged (runs over the narrowed
set). `build_changelog_render_sql` — signature unchanged; derives the fold property
set from `spec.columns`' prop sources (the pattern the snapshot render already uses)
instead of the sidecar-wide scalar-property scan; row set identical (Invariant 3).

New exception (source is the one mode using per-rule subclasses):
`SourceRenameSliceOnly(ExportError)` in `src/fabulexa_forge/errors.py`, sibling of
`SourceRenameUnresolved` — a `rename` columns key names a policy-omitted
`slice_only` column; the rename is unsatisfiable, never silently ignored.

### Streaming — `engine.py`

```python
def _check_stream_properties_slice_only(
    sidecar: Sidecar,
    kind: str,
    properties: Sequence[str],
) -> None:
    """
    StreamPropertySliceOnly: no kinds[].properties entry resolves to a
    non-exempt slice_only prop__<p> column of records__<kind>. Hooked in
    _validate_kinds' per-kind loop immediately after the existing
    property-resolvability check (column existence already established).
    Refuse-only; emits nothing.

    Raises:
        ExportError: Message names the kind, the property, and the class.
        TemporalClassUnavailableError: Propagated.
    """
```

### Error-message shapes (message-only rules; dimensional + streaming raise plain `ExportError`)

- `SliceOnlyColumnRefused`: `table '<t>': column '<c>' reads
  '<records__k>.<prop__p>' which is temporal_class: slice_only; its value is known
  only at the emit's slice and cannot be presented as an as-of value` (filter / fk /
  as_of variants name the surface: "filter key", "fk hop column", "as_of column").
- `LookupColumnSafety` class clause: `lookup column '<t>.<c>': <terminal
  property|traversed hop column> '<prop__p>' on kind '<k>' is temporal_class:
  <tracked|slice_only>; only constant columns are allowed`.
- `StreamPropertySliceOnly`: `stream kind '<k>': property '<p>' is temporal_class:
  slice_only; it cannot ride the state-changes after-image`.

### Test support — new module `tests/_support/notices.py`

```python
def discard_notice_sink(notice: Notice) -> None:
    """Swallow a notice. The migration sink for tests indifferent to notices."""
```

```python
class RecordingNoticeSink:
    """Callable NoticeSink that appends every received Notice to `self.notices`
    (a list, in delivery order) for sequence and content assertions."""
```

## Phases

### Phase 1: Notice channel + sink threading (dimensional / incremental / CLI)

**Delivers:** `exporters/notices.py` (`Notice`, `NoticeSink`,
`render_notice_stderr`); required `notice_sink` on `build_query_specs`,
`export_dimensional`, `validate_table`, `export_window`, `export_incremental_next`;
`check_discriminator_value_observed` migrated onto the channel (the phase's live
emission — no dead parameter); CLI `export` verb supplies `render_notice_stderr`;
`tests/_support/notices.py`; recipe-fixture re-class.
**Demo:** CLI export whose records `filter` names an unobserved discriminator value
→ one `notice: …` line on stderr, data on stdout/disk unchanged, exit 0; run twice
→ byte-identical notice sequence; a `--next` drip re-emits its compile's notices
each invocation.
**Contracts:** `Notice`, `NoticeSink`, `render_notice_stderr`, `build_query_specs`,
`export_dimensional`, `export_window`, `export_incremental_next` (design doc);
`validate_table`, `check_discriminator_value_observed`, test-support sinks (above).
**Steps:** `source → migrate (codemod, 12 files) → author (3 files)` — atomic: a
required parameter leaves every un-migrated call site red until all are migrated.

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/exporters/notices.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/engine.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/validation.py` |
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Modify | `src/fabulexa_forge/cli.py` |
| Create | `tests/_support/notices.py` |
| Create | `tests/exporters/test_notices.py` |
| Modify | `tests/exporters/dimensional/test_fk.py` |
| Modify | `tests/exporters/dimensional/test_grains.py` |
| Modify | `tests/exporters/dimensional/test_lookup.py` |
| Modify | `tests/exporters/dimensional/test_scd.py` |
| Modify | `tests/exporters/dimensional/test_scd2_source_filter.py` |
| Modify | `tests/exporters/dimensional/test_windowed.py` |
| Modify | `tests/exporters/dimensional/test_windowed_failfast.py` |
| Modify | `tests/exporters/dimensional/test_export_dimensional.py` |
| Modify | `tests/exporters/dimensional/test_rebasing.py` |
| Modify | `tests/exporters/dimensional/test_validation.py` |
| Modify | `tests/writers/test_duckdb_window.py` |
| Modify | `tests/incremental/test_driver.py` |
| Modify | `tests/recipes/test_recipes.py` |
| Modify | `tests/recipes/_recipe_fixture.py` |
| Create | `docs/sprints/slice-only-policy/demos/phase_1_notice_channel.py` |

**Tests:**
- `Notice` is frozen (mutation raises) and value-equal by fields.
- `render_notice_stderr` writes exactly `notice: {message}\n` to stderr; stdout
  untouched; returns None.
- Unobserved records `filter` value → exactly one notice, `code ==
  "discriminator-value-unobserved"`, `message` verbatim equal to the former warning
  text; **no** Python warning raised (passes under `warnings.simplefilter("error")`).
- Observed filter value → zero notices.
- Determinism: two identical `build_query_specs` runs against the same emit + config
  → identical notice sequences (content and order) via `RecordingNoticeSink`.
- `export_dimensional` threads the sink to compile; output tables byte-identical
  whether the sink records or discards.
- `export_window` and `export_incremental_next` thread the sink to the dimensional
  compile; two consecutive `--next`-style drips each re-emit the compile's notices.
- CLI `export`: notice rendered to stderr before data delivery; exit code 0;
  stdout carries only the existing output.
- Recipe suite green after fixture re-class; the `staff_type` discriminator remains
  `slice_only` (asserted in the fixture's own guard test if one exists, else by the
  recipes passing unchanged — the carve-out is not enforced until Phase 2, so this
  phase only requires green).
- All migrated tests pass with `discard_notice_sink`.

### Phase 2: Dimensional refusal, lookup regate, init skip

**Delivers:** `exporters/slice_only.py` (shared predicate pair);
`check_slice_only_filter_keys` + `check_slice_only_column_reads` (validation.py) +
`check_fk_slice_only` (fk.py) wired into `validate_table`, always-on;
`check_lookup_temporal_safety` re-keyed to `temporal_class: constant` with the
carve-out; `init` proposal skip + notices (`generate_init_config` gains
`notice_sink`; CLI `init` verb supplies the renderer).
**Demo:** configs naming a `slice_only` column via `from`, a records `filter` key,
an fk reference hop, and a `lookup` terminal → each refused with the rule's message;
the same reads through the exempt discriminator pass; a `tracked` discriminator
`lookup` terminal now passes (the loosening); `init` over an emit with a
`slice_only` column skips it with a stderr notice while still proposing the kind and
its discriminator.
**Contracts:** `is_exempt_discriminator`, `is_non_exempt_slice_only`,
`check_slice_only_filter_keys`, `check_slice_only_column_reads`,
`check_fk_slice_only`, re-keyed `check_lookup_temporal_safety`, init deltas (above);
`generate_init_config` (design doc).
**Steps:** `source → author (3 files: predicate + validation + fk tests) → author
(1 file: lookup regate) → author (1 file: CLI init)` — mixed shapes: source reshape
plus intent-changing rewrites of the shipped lookup-gate tests.

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/exporters/slice_only.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/validation.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/fk.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/lookup.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/init.py` |
| Modify | `src/fabulexa_forge/cli.py` |
| Create | `tests/exporters/test_slice_only.py` |
| Modify | `tests/exporters/dimensional/test_validation.py` |
| Modify | `tests/exporters/dimensional/test_fk.py` |
| Modify | `tests/exporters/dimensional/test_lookup.py` |
| Modify | `tests/test_cli_init.py` |
| Create | `docs/sprints/slice-only-policy/demos/phase_2_dimensional_refusal.py` |

**Tests:**
- Predicate unit tests (`tests/exporters/test_slice_only.py`, matching the module's
  directory): exempt iff `prop__<K>_type` ∧ `subtype_values(K)` non-empty; empty
  subtype_values → not exempt; non-discriminator name → not exempt; non-`prop__`
  name → False with no class read; `slice_only` non-exempt → True; `constant` /
  `tracked` → False; missing pair → `TemporalClassUnavailableError`.
- Refusal per surface, each its own case: `from`, `correlation`, records `filter`
  key, resolved `value_map.from`, `derived: timestamp` `source`, `derived: elapsed`
  `correlate_on` / `start_source` / `end_source` / `other_where` key, `fk via:
  reference` hop (author-hinted `path` and pathfound `to`), `fk via: membership`
  `member_path` hop and `as_of` column. Message names output table.column, base
  table.column, class, and the slice-fact clause.
- Exempt discriminator: projectable via `from`, filterable, renameable — any class,
  including `slice_only`.
- Non-sub-typed kind's `prop__<K>_type` marked `slice_only` → refused like any
  column.
- Membership `source.where` / fk `where` / `member_field` and history-grain
  `source.property` / `value` scoping validate untouched against an emit whose
  records columns are `slice_only` (outside the population).
- Refusal fires on a **full** export compile (no window) — always-on.
- Lookup regate: all-`constant` path allowed; `slice_only` terminal refused naming
  the class; `tracked` terminal refused naming the class; non-constant traversed hop
  refused; `tracked` discriminator terminal **allowed** (the deliberate loosening);
  missing pair on a consulted column → `TemporalClassUnavailableError`.
- `check_scd2_needs_history` still keys on `history_tracked` (existing tests
  unchanged and green).
- `init`: `slice_only` column absent from the SCD-2 stub's column list; one
  `slice-only-column-omitted` notice per skip; the kind itself still proposed
  (skip is column-level); discriminator proposed and `filter` pre-fill unchanged;
  CLI `init` writes candidate YAML to stdout and notices to stderr.

### Phase 3: Source omission + rename rule

**Delivers:** `notice_sink` threaded through `build_source_plan`,
`build_source_query_specs`, `export_source`, the incremental driver's source branch,
and the CLI source dispatch; every records-genre render narrowed to `tracked` +
`constant` + exempt discriminator; one `slice-only-column-omitted` notice per
unit × column in plan order; `SourceRenameSliceOnly`.
**Demo:** source export over an emit with a `slice_only` column: the column is
absent from the change-log and snapshot renders, one stderr notice per unit × column,
row counts identical to the un-narrowed baseline; a unit whose every property is
`slice_only` still renders rows (identity + lifecycle + discriminator); a `rename`
naming the omitted column → `SourceRenameSliceOnly`.
**Contracts:** `build_source_plan`, `build_source_query_specs`, `export_source`
(design doc); `_omitted_slice_only_columns`, the `omitted`-threaded column builders,
`_apply_rename_entry`, `_resolve_specs`, `SourceRenameSliceOnly`, changelog-render
delta (above).
**Steps:** `source → migrate (codemod, 3 files) → author (3 files)` — atomic:
required parameter on three source entry points; `test_plan.py`, `test_renders.py`,
and `_source_fixtures.py` take both the uniform arg-add and the new policy tests, so
they sit in the author step (disjoint from the codemod slice).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/source/renders.py` |
| Modify | `src/fabulexa_forge/exporters/source/engine.py` |
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Modify | `src/fabulexa_forge/cli.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `tests/exporters/source/test_engine.py` |
| Modify | `tests/recipes/test_source_recipes.py` |
| Modify | `tests/integration/test_corrupt_source.py` |
| Modify | `tests/exporters/source/test_plan.py` |
| Modify | `tests/exporters/source/test_renders.py` |
| Modify | `tests/exporters/source/_source_fixtures.py` |
| Create | `docs/sprints/slice-only-policy/demos/phase_3_source_omission.py` |

**Tests:**
- Change-log unit: `slice_only` column absent from the after-image column set; the
  `c`/`u`/`d` row set and `seq` assignment identical to the same emit exported
  before narrowing (column-projection-only invariance).
- Snapshot render: `slice_only` column absent from the state-at projection; snapshot
  row set unchanged.
- Reference and transaction units: column omitted; one notice per unit × column;
  notices in plan order then sidecar column order, deterministic across runs.
- Degenerate unit (every property non-exempt `slice_only`): still renders — rows
  intact, identity/lifecycle/`presentation_id` columns carried, exempt discriminator
  carried, one notice per omitted column; the unit is never suppressed.
- Junction units: untouched, no class read, no notices.
- Sub-type split: exempt `slice_only` discriminator carried; existing
  retain/drop/strip rules unchanged.
- `rename` columns key naming an omitted column → `SourceRenameSliceOnly` naming the
  entry, the column, and the omission reason; renaming a delivered column still
  works; collision check runs over the narrowed set.
- `export_window` / `export_incremental_next` in source mode thread the sink; a
  windowed source compile emits the same notices as the full compile.
- CLI source export: notices to stderr before data delivery; exit 0; row counts
  unchanged by sink choice.
- Existing source tests green with `discard_notice_sink` (fixtures carry no
  `slice_only` columns; new policy tests add them via `prop_column`).

### Phase 4: Streaming refusal

**Delivers:** `StreamPropertySliceOnly` in the eager pass — a `kinds[].properties`
entry resolving to a non-exempt `slice_only` column refused before any event is
yielded. No signature changes; streaming emits no notices.
**Demo:** stream config selecting a `slice_only` property → `ExportError` naming
kind, property, class, raised by `iter_stream_events` before the first event; the
same config selecting the exempt discriminator (and using `types` selection) streams
normally.
**Contracts:** `_check_stream_properties_slice_only` (above); `iter_stream_events`
behavior delta (design doc).
**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/streaming/engine.py` |
| Modify | `tests/exporters/streaming/test_engine.py` |
| Create | `docs/sprints/slice-only-policy/demos/phase_4_streaming_refusal.py` |

**Tests:**
- `kinds[].properties` naming a non-exempt `slice_only` property → `ExportError`
  from the eager pass (raised by the `iter_stream_events` call itself, before the
  first `next()`); message names kind, property, class.
- Discriminator entry in `properties` allowed at any class (carve-out); `types`
  sub-type selection and routing unchanged.
- `constant` and `tracked` property selections unaffected.
- `membership-events` content untouched (no class read).
- Missing temporal pair on a selected property → `TemporalClassUnavailableError`.

## What Doesn't Change

The design doc's § What Doesn't Change is binding. In code terms:

- `reader/` — no new surface; `Sidecar.temporal_class`, `subtype_values`, the
  taxonomy stand as shipped.
- `derivations/` — every fold keeps its signature and row semantics; gating is
  selection-side only. No fold learns about classes.
- Source genre trichotomy (`plan.py:146` keying) — classification outcomes
  identical.
- Window-gated incremental rules (`check_incremental_*`) — no re-keying; their
  `history_tracked` predicates stand.
- `check_scd2_needs_history` — keeps `history_tracked` keying (SCD-class question).
- `updated_at` / `last_mutation_sim_time` rendering — byte-unchanged (classless).
- Corrupters, config grammar (no new YAML fields, no opt-out), exit codes, CLI
  flags, Kafka sink, pacing, mixer, writers, anchor resolution.
- `iter_stream_events` / `stream_export` signatures.

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/exporters/notices.py` | New: `Notice`, `NoticeSink`, `render_notice_stderr` |
| `src/fabulexa_forge/exporters/slice_only.py` | New: shared exemption + population predicates |
| `src/fabulexa_forge/exporters/dimensional/engine.py` | `notice_sink` on `build_query_specs` / `export_dimensional` |
| `src/fabulexa_forge/exporters/dimensional/validation.py` | Sink threading; discriminator check → notice; slice-only refusal checks |
| `src/fabulexa_forge/exporters/dimensional/fk.py` | `check_fk_slice_only` over traversed hops |
| `src/fabulexa_forge/exporters/dimensional/lookup.py` | `check_lookup_temporal_safety` re-keyed to `constant` + carve-out |
| `src/fabulexa_forge/exporters/dimensional/init.py` | Proposal skip + notices; sink threading |
| `src/fabulexa_forge/exporters/source/plan.py` | Omitted-set computation, narrowed column builders, notices, `SourceRenameSliceOnly` |
| `src/fabulexa_forge/exporters/source/renders.py` | Changelog fold property set derived from spec columns |
| `src/fabulexa_forge/exporters/source/engine.py` | `notice_sink` on `build_source_query_specs` / `export_source` |
| `src/fabulexa_forge/exporters/streaming/engine.py` | `StreamPropertySliceOnly` in the eager pass |
| `src/fabulexa_forge/incremental/driver.py` | `notice_sink` on `export_window` / `export_incremental_next`, threaded to mode compiles |
| `src/fabulexa_forge/cli.py` | `render_notice_stderr` supplied for `export` and `init` |
| `src/fabulexa_forge/errors.py` | `SourceRenameSliceOnly` |
| `tests/_support/notices.py` | New: discard + recording sinks |
| `tests/exporters/test_notices.py` | New: notice channel unit tests |
| `tests/exporters/test_slice_only.py` | New: predicate unit tests |
| `tests/exporters/dimensional/test_*.py` (10 files) | Sink migration; discriminator, refusal, regate tests |
| `tests/exporters/source/test_*.py` (3 files) + `_source_fixtures.py` | Sink migration; omission, rename, notice tests |
| `tests/exporters/streaming/test_engine.py` | Refusal tests |
| `tests/writers/test_duckdb_window.py` | Sink migration |
| `tests/incremental/test_driver.py` | Sink migration; drip notice re-emission |
| `tests/recipes/test_recipes.py`, `test_source_recipes.py`, `_recipe_fixture.py` | Sink migration; fixture re-class (`constant`; discriminator kept `slice_only`) |
| `tests/integration/test_corrupt_source.py` | Sink migration |
| `tests/test_cli_init.py` | Init skip + notice tests |
| `docs/sprints/slice-only-policy/demos/phase_*.py` (4 files) | Phase demos |
