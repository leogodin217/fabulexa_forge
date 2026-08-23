# Sprint: companion-artifacts

## Purpose

Every file-writing export invocation (dimensional / source / base; full, `--next`,
and `--from`/`--to`) deposits two companion artifacts beside its datasets — a
rendered `<prefix>-readme.md` and a deterministic `<prefix>-manifest.json` — so a
consumer handed an export directory can learn what shaped it without reading
Python. An author optionally points `readme_overlay` at a markdown file of domain
prose; the README renders mode template × overlay × derived facts.

Design doc: `docs/architecture/pending/export-companion-artifacts.md` — the
authority on semantics (artifact naming/placement, writing rules, overlay grammar,
README ordering, manifest fields, determinism invariants). This spec carries the
contracts, phases, and test cases; it does not restate the design's rationale.

## Scope

**Capabilities touched:**
- export config: the `readme_overlay` field (nothing else in the config surface)
- overlay surface (new): H2-slot grammar → `ReadmeOverlay`, load + plan-time
  table validation, two new errors
- companion writer (new, mode-neutral): README renderer over three packaged mode
  templates, pinned-byte manifest builder, prefix/placement, unconditional
  overwrite
- export engines + shared write dispatch + writers (breaking): per-table written
  schema surfaced alongside row counts; entry points gain `overlay`, return
  `ExportReport`
- incremental driver (breaking): windowed entry points gain `overlay`, return
  `ExportReport`; whole-state artifact rewrite per emitting window; CSV census
  exclusion; fingerprint exclusion of `readme_overlay`
- CLI: overlay resolution against the config file's directory, load, threading

**Not included:** streaming artifacts, any corrupt-side README, `validate` /
`compare` / `init` changes, a regenerate verb, repair of last-window
artifact-write failure (accepted wart, § Writing rules of the design doc), new
notice codes, recipe docs (post-sprint doc work).

## Breaking Changes

All internal — no config or dataset compatibility is affected. Existing YAML
configs remain valid (`readme_overlay` is optional-absent).

| Surface | Change | Callers migrated |
|---|---|---|
| `export_dimensional` / `export_source` / `export_base` | + trailing `overlay: ReadmeOverlay \| None` param; `dict[str, int]` → `ExportReport` | cli.py + 15 test files |
| `export_window` | + trailing `overlay` param; `dict[str, int]` → `ExportReport` | cli.py, `export_incremental_next`, tests |
| `export_incremental_next` | + trailing `overlay` param; `IncrementalOutcome.row_counts: dict[str, int]` → `report: ExportReport \| None` (`None` iff drained) | cli.py + tests |
| `write_csv` | `int` → `WrittenRelation` | `write_query_specs`, driver `_write_csv_specs`, tests |
| `write_duckdb` / `write_duckdb_window` | `dict[str, int]` → `dict[str, WrittenRelation]` | `write_query_specs`, driver, tests |
| `write_query_specs` | `dict[str, int]` → `ExportReport` | three engines, tests |

Behavioral: the incremental fingerprint's canonical config dump now excludes
`readme_overlay` (changing only the overlay never raises
`IncrementalFingerprintMismatch`); the CSV fresh/lost census excludes companion
artifact filenames from the non-hidden-entry count. Neither changes any existing
fingerprint value or census verdict for targets that contain no artifacts.

## Success Criteria

- [ ] Full export of each mode, both formats, writes `<prefix>-readme.md` +
      `<prefix>-manifest.json` per the design's placement/prefix table; dataset
      bytes, table sets, stdout, and exit codes are unchanged
- [ ] README follows the ordering contract; manifest carries the design's field
      set under the pinned byte form; re-running the same export is byte-identical
- [ ] Overlay errors are loud and early: `ReadmeOverlayInvalid` at load,
      `ReadmeOverlayUnknownTable` post-compile before any write
- [ ] Incremental: whole-state artifact rewrite on every emitting window (empty
      windows included), drained touches nothing, a range writes
      `incremental.next_window_index: null`, census and fingerprint exclusions
      hold
- [ ] All migrated existing tests pass; `make test` green at every phase end

## Contracts

Semantics for every contract below are pinned in the design doc § Semantics and
§ Interface Contracts; docstrings here are abbreviated, the implementer writes
full ones.

### Config (Phase 1)

```python
class ExportConfig(StrictBaseModel):
    # existing model; one new field, documented per config-docstrings.md
    readme_overlay: str | None = None
    """Optional path to the author's README overlay markdown, resolved against
    the config file's directory by whoever loaded the config (the model never
    touches the filesystem). Absent: the README renders from the mode template
    and derived facts alone."""

    @model_validator(mode="after")
    def readme_overlay_nonempty(self) -> "ExportConfig":
        """A present readme_overlay is a non-empty, non-whitespace string
        (house convention: delegate to a module-level _validate_* helper
        raising ValueError)."""
```

### Errors (Phase 1) — in `src/fabulexa_forge/errors.py`

```python
class ReadmeOverlayInvalid(ConfigError):
    """Overlay file unreadable, not UTF-8, or violating the slot grammar."""


class ReadmeOverlayUnknownTable(ExportError):
    """An overlay 'table:' slot names a table the compiled plan does not
    produce. Raised post-compile, pre-write."""
```

### Overlay surface (Phase 1) — `exporters/companion/overlay.py`

```python
@dataclass(frozen=True)
class ReadmeOverlay:
    """Parsed author overlay. table_notes keys are author-facing output-table
    names; values are verbatim markdown bodies. Constructed only by
    load_readme_overlay."""

    overview: str | None
    table_notes: Mapping[str, str]


def load_readme_overlay(path: Path) -> ReadmeOverlay:
    """Parse an overlay markdown file per the design's slot grammar.

    Args:
        path: Absolute path to the overlay file.

    Returns:
        The parsed ReadmeOverlay.

    Raises:
        ReadmeOverlayInvalid: unreadable / not UTF-8; content before the first
            H2; a heading matching neither slot form (exact, case-sensitive);
            a duplicate slot key.
    """


def validate_overlay_tables(
    overlay: ReadmeOverlay,
    output_table_names: Sequence[str],
) -> None:
    """Refuse table notes referencing tables the plan won't produce.

    Args:
        overlay: The parsed overlay.
        output_table_names: Author-facing output-table names of the compiled
            plan, in plan iteration order.

    Raises:
        ReadmeOverlayUnknownTable: names the slot and lists the plan's tables.
    """
```

### Report types (Phase 2) — `exporters/query_spec.py`, beside `TableKeys`

```python
@dataclass(frozen=True)
class TableReport:
    """One output table as written. columns are (name, type-text) pairs in
    output order, transcribed from the materialized relation via the writers'
    DESCRIBE authority. row_count is None on windowed invocations. keys is
    the declared TableKeys, or None when undeclared or CSV-dropped."""

    name: str
    columns: tuple[tuple[str, str], ...]
    row_count: int | None
    keys: TableKeys | None


@dataclass(frozen=True)
class ExportReport:
    """Per-table reports for one invocation, in plan iteration order."""

    tables: tuple[TableReport, ...]
```

### Companion writer (Phase 2) — `exporters/companion/`

```python
@dataclass(frozen=True)
class WindowedArtifactState:  # artifacts.py
    """Windowed facts a companion-artifact rewrite records. next_window_index
    is the cursor's next index after a --next window, None for a --from/--to
    range (stateless: no cursor exists)."""

    regime: Literal["calendar", "sim_time"]
    label: str
    next_window_index: int | None


def write_companion_artifacts(  # artifacts.py
    emit: Emit,
    config: ExportConfig,
    fmt: Literal["csv", "duckdb"],
    anchor: EffectiveAnchor | None,
    report: ExportReport,
    overlay: ReadmeOverlay | None,
    target: Path,
    windowed: WindowedArtifactState | None,
) -> None:
    """Render and write both artifacts for one export invocation.

    Mode-neutral; placement/prefix follow the target (directory → '<mode>-*'
    inside it; .duckdb file → '<db-stem>-<mode>-*' beside it). Overwrites
    unconditionally. Called only after all data of the invocation is
    delivered; never on a drained or failed invocation.

    Raises:
        ExportRuntimeError: an artifact file cannot be written.
    """


def is_companion_artifact_name(name: str) -> bool:  # artifacts.py
    """True iff name is '<mode>-readme.md' / '<mode>-manifest.json' with mode
    in {dimensional, source, base}."""
```

`readme.py` renders the README ordering contract from the packaged mode template
(`companion/templates/<mode>.md`, loaded via `importlib.resources` with the
src-tree fallback pattern of `reader/_schema.py`); `manifest.py` builds the
manifest document (field set per the design § The manifest; config embedded via
the same canonical `model_dump(mode="json")` the fingerprint uses, with
`readme_overlay` **included**; emit identity reuses the sidecar-sha surface the
fingerprint reads) and owns the pinned byte serialization. Internal function
signatures are the implementer's; the public surface is the two functions and
three dataclasses above, re-exported from `companion/__init__.py`.

### Writer surface (Phase 3) — `writers/relation.py` (new)

```python
@dataclass(frozen=True)
class WrittenRelation:
    """What one relation write materialized: rows written and the (name,
    type-text) column pairs of the written relation. row_count is the
    invocation's written rows even on windowed paths (None-for-windowed is a
    report-assembly decision, not a writer fact)."""

    row_count: int
    columns: tuple[tuple[str, str], ...]


def describe_arrow_columns(
    conn: duckdb.DuckDBPyConnection,
    registered_name: str,
) -> tuple[tuple[str, str], ...]:
    """DESCRIBE a registered Arrow relation into (column, type-text) pairs —
    the single transcription authority (promoted from the keyed-creation
    path's private helper)."""


def describe_arrow_table(arrow_table: pa.Table) -> tuple[tuple[str, str], ...]:
    """Transcribe an Arrow table via an in-memory DuckDB registration,
    delegating to describe_arrow_columns. Used by the CSV write path; never
    routes through the emit's connection."""
```

### Changed signatures (Phases 3–4)

```python
# Phase 3 — writers + dispatch + engines + CLI
def write_csv(emit, table_name, query, output_dir) -> WrittenRelation: ...
def write_duckdb(emit, queries, output_path, keys) -> dict[str, WrittenRelation]: ...
def write_query_specs(emit, specs, out, fmt) -> ExportReport:
    """Tables built by iterating specs (plan iteration order), name =
    spec.table_name (full-export specs are all create/no-view), keys =
    spec.keys under duckdb, None under csv."""

def export_dimensional(  # export_source / export_base identical
    emit: Emit,
    config: ExportConfig,
    out: Path,
    fmt: Literal["csv", "duckdb"],
    anchor: EffectiveAnchor | None,
    notice_sink: NoticeSink,
    overlay: ReadmeOverlay | None,
) -> ExportReport:
    """Unchanged through data delivery, plus: immediately after plan compile
    — before any write — calls validate_overlay_tables when overlay is
    present; after data delivery invokes write_companion_artifacts and
    returns the report.

    Raises:
        ReadmeOverlayUnknownTable: pre-write.
        (existing raises unchanged; ExportRuntimeError now also covers a
        failed artifact write)
    """

# Phase 4 — incremental driver
def write_duckdb_window(emit, specs, output_path, window, fingerprint) -> dict[str, WrittenRelation]: ...

def export_window(
    emit, config, out, fmt, anchor, window, fingerprint, notice_sink,
    overlay: ReadmeOverlay | None,
) -> ExportReport:
    """Per-window overlay validation post-compile; artifacts rewritten
    whole-state after the window's data (and cursor, for --next) are
    committed; TableReport.name maps to the author-facing output name (view
    name where an SCD-2 view exists), columns the materialized physical
    projection, row_count None."""

def export_incremental_next(
    emit, config, out, fmt, anchor, notice_sink,
    overlay: ReadmeOverlay | None,
) -> IncrementalOutcome: ...

@dataclass(frozen=True)
class IncrementalOutcome:
    status: Literal["emitted", "drained"]
    window: Window | None
    report: ExportReport | None  # replaces row_counts; None iff drained
```

`compute_fingerprint` keeps its signature; its canonical dump drops
`readme_overlay` before hashing. The census's `_list_non_hidden` additionally
excludes `is_companion_artifact_name` entries.

## Phases

### Phase 1: Overlay surface + config field

**Delivers:** The parsed overlay grammar with loud errors, the two new error
types, and the `readme_overlay` config field. Purely additive — no existing
behavior changes.
**Demo:** Parses a sample overlay and prints its slots; shows two grammar
rejections (`## Overview`, duplicate key) and an unknown-table refusal, each
naming the offender.
**Contracts:** `ReadmeOverlay`, `load_readme_overlay`, `validate_overlay_tables`,
`ReadmeOverlayInvalid`, `ReadmeOverlayUnknownTable`, `ExportConfig.readme_overlay`.
**Steps:** none (single implementer)

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/exporters/companion/__init__.py` |
| Create | `src/fabulexa_forge/exporters/companion/overlay.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `src/fabulexa_forge/config/models.py` |
| Create | `tests/exporters/companion/__init__.py` |
| Create | `tests/exporters/companion/test_overlay.py` |
| Create | `tests/config/test_readme_overlay.py` |
| Create | `docs/sprints/companion-artifacts/demos/phase_1_overlay.py` |

**Tests:**
- Overview + two `table:` slots parse into the right `ReadmeOverlay`; bodies
  verbatim with leading/trailing blank lines trimmed, interior blank lines kept
- H3+ headings inside a body stay in the body; the next H2 starts a new slot
- Slot with an empty body is legal (parses; renderer later treats as absent)
- Content before the first H2 → `ReadmeOverlayInvalid`
- `## Overview`, `## table:x`, `## table:  x` (two spaces), `## table: ` (empty
  name) → `ReadmeOverlayInvalid` naming the heading — never normalized
- Duplicate slot key (same `table:` name twice; `overview` twice) →
  `ReadmeOverlayInvalid` naming the key
- Missing file and non-UTF-8 bytes → `ReadmeOverlayInvalid`
- `validate_overlay_tables`: slot naming an absent table →
  `ReadmeOverlayUnknownTable` naming the slot and listing the plan's tables;
  all-known slots pass; overlay with no table notes passes against any plan
- Config: absent `readme_overlay` loads as `None`; empty and whitespace-only
  strings rejected at parse time; `tests/config/test_docstring_convention.py`
  still passes (field documented per convention)

### Phase 2: Companion writer — report types, templates, README + manifest

**Delivers:** The mode-neutral companion writer: `TableReport` / `ExportReport`,
`WindowedArtifactState`, three packaged mode templates, the README renderer, the
pinned-byte manifest builder, `write_companion_artifacts`, and
`is_companion_artifact_name`. Nothing calls it yet from the export paths; it is
fully exercisable with a synthetic emit + hand-built report.
**Demo:** Builds a minimal emit (`tests/_support/sidecar_builder.write_emit`),
hand-assembles an `ExportReport` + overlay, writes both artifacts to a temp
directory for a directory target and a `.duckdb` target, prints the README and
manifest, and re-renders to show byte-identity.
**Contracts:** `TableReport`, `ExportReport`, `WindowedArtifactState`,
`write_companion_artifacts`, `is_companion_artifact_name`.
**Steps:** `source → author (3 files)` — the manifest byte form, README ordering,
and three templates are one deep surface read twice (source, then enumerative
tests); split so each reads it in a fresh context.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/query_spec.py` |
| Create | `src/fabulexa_forge/exporters/companion/artifacts.py` |
| Create | `src/fabulexa_forge/exporters/companion/readme.py` |
| Create | `src/fabulexa_forge/exporters/companion/manifest.py` |
| Create | `src/fabulexa_forge/exporters/companion/templates/dimensional.md` |
| Create | `src/fabulexa_forge/exporters/companion/templates/source.md` |
| Create | `src/fabulexa_forge/exporters/companion/templates/base.md` |
| Create | `tests/exporters/companion/test_artifacts.py` |
| Create | `tests/exporters/companion/test_manifest.py` |
| Create | `tests/exporters/companion/test_readme.py` |
| Create | `docs/sprints/companion-artifacts/demos/phase_2_companion_writer.py` |

Template prose is authored from the mode arch docs (`docs/architecture/
dimensional.md` / `source.md` / `base.md` — read, not edited): each template must
cover how to read its shape (star layout + SCD-2 validity columns; state/junction
tables + the event log's `changes`; state-at horizon + record-index key columns).

**Tests:**
- Placement/prefix: directory target → `<mode>-readme.md` + `<mode>-manifest.json`
  inside it; `.duckdb` target → `<db-stem>-<mode>-*` siblings; both overwritten
  unconditionally on a second call
- `is_companion_artifact_name`: true for all six mode×suffix combinations; false
  for `streaming-readme.md`, `dimensional-readme.txt`, `foo.csv`, `.hidden`
- Manifest: full field set on a full export (`manifest_format_version` 1, mode,
  format, `forge_version` = `__version__`, emit identity incl. sidecar sha +
  branch + runtime-or-null, anchor present and null cases, embedded config
  **including** `readme_overlay`, `incremental` null, tables in report order with
  columns / keys-or-null / row_count)
- Manifest windowed: `incremental` block carries regime + label +
  `next_window_index` (int for `--next`, null for range); `row_count` null
- Manifest byte form: UTF-8, `ensure_ascii` off (non-ASCII survives), two-space
  indent, sorted keys, list order preserved for tables/columns, trailing
  newline; two renders of the same inputs are byte-identical
- README ordering: title + generated-marker naming the manifest file → overview
  (when present) → template prose → per-table sections in report order (note
  when present, column inventory with key markings, row count on full exports
  only) → anchor facts (present and absent renderings) → emit identity
- Table without an overlay slot renders derived facts only — no placeholder text
- Each template's rendered README mentions its mode's shape semantics (smoke
  assertion per mode)
- Unwritable target → `ExportRuntimeError`

### Phase 3: Full-export threading

**Delivers:** Every full export writes companion artifacts: writers surface
`WrittenRelation`, `write_query_specs` returns `ExportReport`, the three engines
take `overlay` / validate post-compile / write artifacts / return the report, and
the CLI resolves + loads the overlay and prints row counts from the report
(stdout unchanged). Atomic: the return-shape change and its test migration land
together.
**Demo:** Builds an emit, runs a `mode: base` full export (csv and duckdb) with
an overlay through the library entry point: lists the output directory, prints
the README and manifest, re-runs to show byte-identity, then shows the
unknown-table overlay refusal leaving the target empty.
**Contracts:** `WrittenRelation`, `describe_arrow_columns`,
`describe_arrow_table`, changed `write_csv` / `write_duckdb` /
`write_query_specs`, changed `export_dimensional` / `export_source` /
`export_base`.
**Steps:** `source → migrate (fan-out, 17 files) → author (3 files)`

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/writers/relation.py` |
| Modify | `src/fabulexa_forge/writers/csv.py` |
| Modify | `src/fabulexa_forge/writers/duckdb.py` |
| Modify | `src/fabulexa_forge/exporters/query_spec.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/engine.py` |
| Modify | `src/fabulexa_forge/exporters/source/engine.py` |
| Modify | `src/fabulexa_forge/exporters/base/engine.py` |
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Modify | `src/fabulexa_forge/cli.py` |
| Modify | 17 test files (migrate step — see state.yaml) |
| Create | `tests/exporters/test_companion_integration.py` |
| Create | `tests/test_cli_readme_overlay.py` |
| Create | `tests/writers/test_relation.py` |
| Create | `docs/sprints/companion-artifacts/demos/phase_3_full_export_artifacts.py` |

Phase-3 boundary notes: `write_duckdb_window` and the driver's entry-point
signatures do **not** change here — `incremental/driver.py` is touched only to
unpack `write_csv`'s new return in `_write_csv_specs` (counts derived from
`WrittenRelation`, windowed behavior otherwise untouched). `cli.py` threads the
overlay to the three engines and prints full-export counts from the report; the
windowed print path still reads the dict until Phase 4. `duckdb.py`'s internal
`_apply_spec` / `_create_*` / `_append_*` / `_replace_*` helpers retype to
`WrittenRelation`; the private `_describe_arrow_columns` is promoted into
`writers/relation.py`.

**Tests (author step; migrate step preserves existing intent):**
- Each mode × csv full export writes both artifacts; datasets and table sets
  unchanged (migrated engine tests keep their existing dataset assertions)
- Dimensional duckdb full export → `<db-stem>-dimensional-*` siblings
- Manifest `tables` matches the written relations (names, column types from the
  DESCRIBE authority, row counts equal to the old dict values); dimensional
  entries carry null keys; a declared-keys base/source duckdb export carries them
- Overlay note renders into its table's README section; unknown-table overlay →
  `ReadmeOverlayUnknownTable` and an empty target (no datasets, no artifacts)
- Re-running an identical export is byte-identical for both artifacts
- `describe_arrow_table` output matches the keyed-creation path's type text for
  the same relation
- CLI: `readme_overlay` resolves against the config file's parent (config run
  from a different cwd); missing/invalid overlay file → exit 1 via the existing
  `ConfigError` funnel; stdout row-count lines byte-identical to before
- Existing migrated tests green: `tests/writers/test_csv.py`,
  `test_duckdb.py`, `tests/exporters/test_query_spec.py`, the six engine/rebasing/
  election files, `test_notices.py` (engine calls), four recipes files, two
  corrupt-integration files, `tests/incremental/test_driver.py` (its two direct
  engine calls only)

### Phase 4: Incremental threading

**Delivers:** Windowed invocations write and rewrite companion artifacts:
`write_duckdb_window` surfaces `WrittenRelation`, `export_window` /
`export_incremental_next` take `overlay` and return reports, artifacts are
rewritten whole-state after data + cursor commit, the CSV census ignores
artifact filenames, and the fingerprint excludes `readme_overlay`. Atomic with
its test migration.
**Demo:** Builds an emit, drips a csv export with `--next`-equivalent library
calls: shows window-0 artifacts at the output root, whole-state rewrite after
window 1 (`next_window_index` advancing), an untouched artifact pair on a
drained invocation, a range invocation writing `next_window_index: null`, and a
mid-drip overlay content change that does not trip the fingerprint.
**Contracts:** changed `write_duckdb_window`, `export_window`,
`export_incremental_next`, `IncrementalOutcome.report`; `compute_fingerprint`
exclusion behavior; census exclusion via `is_companion_artifact_name`.
**Steps:** `source → migrate (fan-out, 4 files) → author (3 files)`

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/writers/duckdb.py` |
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Modify | `src/fabulexa_forge/incremental/cursor.py` |
| Modify | `src/fabulexa_forge/incremental/fingerprint.py` |
| Modify | `src/fabulexa_forge/cli.py` |
| Modify | 4 test files (migrate step — see state.yaml) |
| Create | `tests/incremental/test_companion_artifacts.py` |
| Modify | `tests/incremental/test_fingerprint.py` |
| Modify | `tests/incremental/test_cursor.py` |
| Create | `docs/sprints/companion-artifacts/demos/phase_4_incremental_artifacts.py` |

**Tests (author step):**
- `--next` window 0 (csv): artifacts at the output-directory root, never inside
  the window drop directory; manifest `incremental` carries regime, the window
  label, and the cursor's next index; `row_count` null on every table entry
- Second `--next`: both artifacts rewritten whole-state from that window's
  report; `next_window_index` advances
- Empty window: artifacts rewritten like any emitting window
- Drained: exit-path untouched (`status: "drained"`, `report is None`), both
  artifact files byte-identical to before the call
- `--from`/`--to` range: artifacts written after the range's data;
  `next_window_index` null
- Duckdb windowed: artifacts as `<db-stem>-<mode>-*` siblings, rewritten per
  emitting window
- SCD-2 dim under incremental: one manifest entry under the view name, columns
  the physical projection (`__valid_from_ns` included, `valid_to` slots absent)
- Census: a directory holding only companion artifacts classifies fresh; window-0
  artifacts never produce a lost-cursor on the next `--next`; dot-entry handling
  unchanged (`tests/incremental/test_cursor.py` additions)
- Fingerprint: adding/changing/removing `readme_overlay` leaves the fingerprint
  unchanged — no `IncrementalFingerprintMismatch` mid-drip; any other config
  change still mismatches (`tests/incremental/test_fingerprint.py` additions)
- `IncrementalOutcome.report` is `None` iff drained; emitted outcomes carry the
  window's report (migrated `test_driver.py` row-count asserts read the report)

## What Doesn't Change

- **Streaming, corrupt, `validate`, `compare`, `init`, playback** — no artifact
  writing, no signature changes; playback consumes no write-path return values
  (verified: no callers).
- **Dataset bytes, table sets, exit codes, stdout** — identical with and without
  artifacts; the companion writer runs after data delivery and touches only its
  two files. The one new outcome is the artifact-write-failure error, only
  possible after data and cursor are sound.
- **Notice channel** — no new codes; artifact writing emits no notices; notices
  are not embedded in either artifact.
- **Incremental cursor semantics** — regimes, window membership, drained
  detection, atomicity, fingerprint-mismatch refusal all unchanged except the
  two named exclusions (census filenames, fingerprint `readme_overlay`).
- **Reserved output names** — untouched; artifact filenames cannot collide with
  `<table>.csv` or in-db tables.
- **`Emit` / reader surface** — no API additions; the CSV transcription helper
  uses an in-memory DuckDB connection, never the emit's.
- **Dataset serialization** — CSV/DuckDB value encoding untouched; writers
  change return types only.

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/errors.py` | + `ReadmeOverlayInvalid`, `ReadmeOverlayUnknownTable` |
| `src/fabulexa_forge/config/models.py` | + `ExportConfig.readme_overlay` + nonempty validator |
| `src/fabulexa_forge/exporters/companion/__init__.py` | New — public companion surface |
| `src/fabulexa_forge/exporters/companion/overlay.py` | New — `ReadmeOverlay`, `load_readme_overlay`, `validate_overlay_tables` |
| `src/fabulexa_forge/exporters/companion/artifacts.py` | New — `write_companion_artifacts`, `is_companion_artifact_name`, `WindowedArtifactState`, prefix/placement |
| `src/fabulexa_forge/exporters/companion/readme.py` | New — README renderer |
| `src/fabulexa_forge/exporters/companion/manifest.py` | New — manifest builder + pinned byte serialization |
| `src/fabulexa_forge/exporters/companion/templates/{dimensional,source,base}.md` | New — packaged mode templates |
| `src/fabulexa_forge/exporters/query_spec.py` | + `TableReport` / `ExportReport`; `write_query_specs` → `ExportReport` |
| `src/fabulexa_forge/writers/relation.py` | New — `WrittenRelation`, `describe_arrow_columns`, `describe_arrow_table` |
| `src/fabulexa_forge/writers/csv.py` | `write_csv` → `WrittenRelation` |
| `src/fabulexa_forge/writers/duckdb.py` | `write_duckdb` / `write_duckdb_window` → `dict[str, WrittenRelation]`; describe helper promoted out |
| `src/fabulexa_forge/exporters/{dimensional,source,base}/engine.py` | + `overlay` param, post-compile validation, artifact write, `ExportReport` return |
| `src/fabulexa_forge/incremental/driver.py` | Windowed entry points: + `overlay`, `ExportReport` / `IncrementalOutcome.report`, whole-state artifact rewrite |
| `src/fabulexa_forge/incremental/cursor.py` | Census excludes companion artifact filenames |
| `src/fabulexa_forge/incremental/fingerprint.py` | Canonical dump excludes `readme_overlay` |
| `src/fabulexa_forge/cli.py` | Overlay resolution + load + threading; prints from reports |
| 21 existing test files | Migrated to new signatures/return shapes (see state.yaml steps) |
| `tests/exporters/companion/test_{overlay,artifacts,manifest,readme}.py` | New unit suites |
| `tests/exporters/test_companion_integration.py`, `tests/test_cli_readme_overlay.py`, `tests/writers/test_relation.py`, `tests/incremental/test_companion_artifacts.py` | New integration suites |
| `tests/config/test_readme_overlay.py` | New config-field suite |
| `docs/sprints/companion-artifacts/demos/phase_{1..4}_*.py` | Per-phase demos |
