---
status: draft
---

# Export Companion Artifacts

Every file-writing export deposits two **companion artifacts** beside its
datasets: a human-readable README rendered from a forge-authored mode template,
an optional author-supplied overlay, and derived per-export facts; and a
machine-readable, deterministic export manifest. Modes in v1: dimensional,
source, base.

---

## Problem

An export directory is mute. Upstream, a bundle arrives self-describing on two
layers — `base.json` for machines and (informally) an `ATLAS.md` for humans —
but the moment forge reshapes it, all documentation is severed:
`fabulexa-forge export` writes only the datasets.

```
out/
  dim_patient.csv
  dim_ward.csv
  fact_admission.csv
```

A consumer handed this directory cannot learn what mode shaped it, what the
SCD-2 validity columns mean, what grain `fact_admission` is, which identity
surface was elected as each table's key, what timezone the anchor resolved to,
or which emit it came from. The audience — educators, engineers, analysts who
do not read Python — has no artifact to read, and no machine-readable
inventory exists for tooling either. Notices go to stderr and evaporate with
the terminal session.

## Solution

Each export invocation of a file-writing mode writes two mode-prefixed
companion files beside its datasets, mirroring the bundle's own
machine/human split with forge's intent:

```
out/
  dim_patient.csv … fact_admission.csv
  dimensional-readme.md       ← rendered: mode template + author overlay + derived facts
  dimensional-manifest.json   ← generated, deterministic, machine-readable
```

The README is **generated output, never hand-edited**. Its three inputs are:

```
mode template     forge-authored per mode, shipped as package data
                  (mode semantics: how to read the shape)          ┐
author overlay    optional markdown file the export config points  ├─▶ <mode>-readme.md
                  at (domain prose: what the tables mean)          │
derived facts     tables, columns/types, keys, anchor, emit        ┘
                  identity, row counts — from objects already
                  validated at export time
```

The manifest carries only per-export facts, every value sourced from the emit,
the config, the resolved anchor, the compiled plan, or the code version —
nothing invented. Regeneration is always safe because hand-written prose lives
in the *inputs* (template in the repo, overlay beside the author's config),
never in the output.

```yaml
# export config
mode: dimensional
readme_overlay: ./readme-notes.md   # optional; separate hand-authored file
dimensional:
  ...
```

## Affected Subsystems

- **Export config models** — `ExportConfig` gains one optional top-level
  field, `readme_overlay`: a path (resolved against the config file's
  directory by whoever loaded the config) to the author's overlay markdown.
  Absent means no overlay; the README still renders from template + derived
  facts. The config stays emit-independent: overlay existence, readability,
  and slot validity are load-/plan-time checks outside the model.
- **The overlay surface (new)** — a small author-facing markdown grammar
  (§ Semantics) parsed into a typed `ReadmeOverlay`, with loud errors for
  malformed slots and for notes referencing tables the export does not
  produce — the drift alarm that keeps author prose honest.
- **The companion writer (new, mode-neutral)** — one shared surface that
  renders the README (template × overlay × derived facts), builds the
  manifest, and writes both files. Called by every file-writing entry path
  (the three full-export engines and the incremental driver) after data is
  written; it holds no mode-specific branching — the mode contributes its
  template and its report.
- **The three export engines (dimensional / source / base)** — each engine's
  `export_*` entry point gains an `overlay` parameter, returns a structured
  `ExportReport` (per-table columns/types, row counts, declared keys) instead
  of the bare table→row-count mapping, and finishes by invoking the companion
  writer. The engine is also `validate_overlay_tables`' caller — it is the
  only component holding both the compiled plan and the overlay — invoking it
  immediately after plan compile, before any write. The incremental driver's
  windowed entry point changes the same way (overlay in, `ExportReport` out,
  per-window overlay validation post-compile).
  The report's column names and types are transcribed from the materialized
  Arrow schema — the same transcription authority the DuckDB keyed creation
  path already uses — so the manifest documents what was actually written.
- **Writers / shared write dispatch** — the shared query-spec write dispatch
  returns per-table schema alongside the row count so engines can assemble
  their reports without a second materialization. The windowed write paths
  (the windowed DuckDB writer, the per-window CSV staging) surface the same
  per-table schema for the same reason — the incremental driver's report is
  assembled from what its own window materialized, not from a second run of
  the specs. Dataset serialization is untouched.
- **The incremental driver** — three changes. (1) Each emitting invocation
  rewrites both companion artifacts whole-state after the window's data and
  cursor are committed. (2) The CSV fresh/lost census ignores companion
  artifact filenames the way it ignores dot-entries — otherwise the window-0
  artifacts would make every later invocation read as a lost cursor.
  (3) The cursor fingerprint's canonical document excludes `readme_overlay`
  — the fingerprint guards data-seam consistency, and the overlay provably
  never affects data, so improving documentation mid-drip must not halt a
  drip.
- **The CLI** — resolves `readme_overlay` against the config file's parent
  directory, loads the overlay, and passes it to the export entry point.
  Library callers do the same or pass no overlay.

## What Doesn't Change

- **Streaming** — no companion artifacts; its sinks (stdout, Kafka) are not
  all directories. Deliberately deferred, not designed here.
- **The corrupt path** — a corrupted base already ships `defects.json`; any
  future corrupt-side README is a separate design (and must not leak the
  answer key).
- **`validate`, `compare`, `init`** — untouched; `init` writes no datasets
  and gains no artifacts.
- **Dataset bytes, table sets, exit codes, stdout** — identical with or
  without artifacts on every invocation whose artifact writes succeed; the
  companion writer runs after data delivery and touches only its two files.
  The one new outcome is the artifact-write-failure error itself (§ Writing
  rules), which can only occur after data and cursor are already sound.
- **The notice channel** — no new notice codes. Artifact writing emits no
  notices (an "overwrote previous artifacts" notice would depend on
  filesystem state, breaking notice determinism), and notices are not
  embedded in either artifact.
- **Incremental cursor semantics** — regimes, window membership, drained
  detection, atomicity, and the fingerprint-mismatch refusal are unchanged
  except for the two exclusions named above.
- **Reserved output names** — the existing reservations stand; no new table
  or column names are reserved (artifact filenames cannot collide with
  `<table>.csv` files or with tables inside a `.duckdb`).

## Semantics

### Artifact names and placement

The artifact pair is named by a **prefix** derived from the target:

| Target | Placement | Prefix |
|---|---|---|
| `csv` (output directory) | Inside the output directory | `<mode>` |
| `duckdb` (`.duckdb` file path) | Sibling files of the database file | `<db-stem>-<mode>` |

Filenames are `<prefix>-readme.md` and `<prefix>-manifest.json`. The prefix
comes from the mode literal, so co-located exports of different modes never
collide; the db-stem component keeps two same-mode warehouses in one directory
from clobbering each other's docs. Windowed CSV exports place the artifacts at
the output-directory root, never inside window drop directories.

### Writing rules

| Condition | Result |
|---|---|
| Full export completes | Both artifacts written after all data files/tables, from that invocation's report |
| Incremental `--next` emits a window | Window data + cursor committed first, then both artifacts rewritten whole-state |
| Incremental `--next` finds the run drained | Nothing written — data untouched, artifacts untouched, exit 3 as today |
| Explicit `--from`/`--to` range | Treated as a windowed invocation: artifacts written after the range's data (a range target is always fresh; its `next_window_index` is null — no cursor exists) |
| Empty window | Artifacts rewritten like any emitting window — "ran, empty" stays distinguishable |
| Target already holds this prefix's artifacts | Overwritten unconditionally, like the data files themselves — no notice, no prompt |
| Artifact write fails after data is committed | The invocation errors; data and cursor are already sound, and the next *emitting* invocation rewrites both artifacts |
| Export errors before writing data | No artifacts written — companion artifacts document delivered output only |

One accepted wart: if the artifact write fails on the run's **last emittable
window**, every later `--next` is drained and writes nothing, so the stale or
missing artifacts are never repaired — a drained invocation materializes no
tables and has no report to rewrite from. This is deliberate: drained stays
"nothing written", the data and cursor are sound, and the staleness is
detectable (the manifest's `incremental.next_window_index` lags the cursor).
Deleting the two artifact files is always safe; there is no regenerate verb.

### The overlay grammar

The overlay is a UTF-8 markdown file. H2 headings (`## `) delimit **slots**;
everything between one H2 and the next is that slot's body, passed through
verbatim (leading/trailing blank lines trimmed). H3+ headings are legal inside
a body; an H2 always starts a new slot.

| Slot heading | Meaning |
|---|---|
| `## overview` | Export-level prose, rendered near the top of the README |
| `## table: <name>` | Prose for one output table, rendered in that table's section. `<name>` is the author-facing output-table name (post-rename; an SCD-2 dim's view name where a view exists) |

Heading matching is **exact and case-sensitive**: after stripping trailing
whitespace, the text following `## ` must be exactly `overview`, or `table: `
(one space after the colon) followed by a non-empty `<name>` taken verbatim.
`## Overview`, `## table:x`, and `## table:  x` (two spaces) all match neither
slot form and are `ReadmeOverlayInvalid` — never silently normalized.

| Condition | Result |
|---|---|
| Config's `readme_overlay` absent | No overlay; README renders from template + derived facts alone |
| Overlay path unreadable or not UTF-8 | `ReadmeOverlayInvalid` — nothing written |
| Content before the first H2 heading | `ReadmeOverlayInvalid` — no silent free-floating prose |
| Heading matching neither slot form | `ReadmeOverlayInvalid` naming the heading |
| Duplicate slot (same key twice) | `ReadmeOverlayInvalid` naming the key |
| `table:` slot naming a table the compiled plan does not produce | `ReadmeOverlayUnknownTable`, raised at plan time **before any data is written** — the drift alarm |
| Slot with an empty body | Legal; renders as if absent |
| Output table with no overlay slot | Legal; its section carries derived facts only — no placeholder, no TODO stub |

Overlay slot validation runs after plan compile and before data delivery, so
an overlay error never leaves a half-documented, half-written target.

### The README

The README is rendered output. Its ordering contract: a title identifying the
mode and a generated-artifact marker naming the manifest file; the overlay's
`overview` (when present); the mode template's semantics prose; one section
per output table in plan iteration order — the table's overlay note (when
present), then its derived column inventory (names, types, key markings) and
row count (full exports only); then the resolved anchor facts (start instant
and IANA zone, or their absence); then emit identity. Exact prose is authored
in the templates, not specified here — but each mode's template must cover how
to read its shape: the dimensional template the star layout and the SCD-2
validity columns, the source template the state/junction tables and the
polymorphic event log's `changes`, the base template the state-at horizon and
the record-index key columns.

Templates ship inside the package (one per mode, package data), so the
installed CLI is self-sufficient; they are forge-maintained prose whose only
change driver is a mode's own contract changing.

### The manifest

`<prefix>-manifest.json` is a single JSON document. Top-level fields:

| Field | Content |
|---|---|
| `manifest_format_version` | `1` — the artifact's own format version (mode-definitional, like the event log's first id) |
| `mode` | The config's mode literal |
| `format` | `csv` or `duckdb` |
| `forge_version` | The installed package version |
| `emit` | Emit identity: `base_format_version`, SHA-256 of `base.json`'s bytes, the sole branch's `fork_path` and `slice_at`, and the sidecar `runtime` block (or null) |
| `anchor` | The resolved effective anchor — start instant (ISO-8601) + IANA zone — or null when no anchor resolved |
| `config` | The parsed `ExportConfig` under the same canonical model dump the incremental fingerprint uses, with `readme_overlay` **included** — the manifest documents everything that shaped the export; the exclusion of `readme_overlay` is the fingerprint's alone (§ Validation Rules) |
| `incremental` | Null on a full export; on a windowed invocation: the regime, the invocation's window or range label, and the next window index — the cursor's next index after a `--next` window, **null on a `--from`/`--to` range** (a range is stateless: no cursor exists) |
| `tables` | One entry per output table in plan iteration order: name, ordered columns (`name` + type), declared `primary_key` / `unique` (null when undeclared or CSV-dropped), and `row_count` (full exports; null on windowed invocations — a window's counts describe the window, not the accumulated target) |

A table's entry is named by its **author-facing output-table name** — the same
name the overlay's `table:` slots use: an SCD-2 dim windowed with a `valid_to`
column is one entry under its view name, its columns the materialized physical
projection (the declared columns minus the `valid_to` slots, plus the trailing
`__valid_from_ns`) — the manifest states what was written, and the view is not
a written relation, so it gets no entry of its own. Bookkeeping objects
(`_export_meta`, `_export_windows`, the CSV cursor file) are driver state, not
output tables, and never appear in `tables`.

`primary_key` / `unique` carry only what `declare_keys` declared — a surface
the base and source modes have and the dimensional mode does not — so a
dimensional export's entries are always null there, and the README marks no
keys for it. This is deliberate: the report transcribes declarations, it does
not infer keys. The key *election* (which identity surface presents as each
table's id) is still on record in every mode via the embedded `config`'s
`keys` block.

Byte form is pinned: UTF-8, `ensure_ascii` off, two-space indent, sorted
object keys, list order semantic (tables, columns), trailing newline.
Column names and types are transcribed from the materialized Arrow schema —
the single transcription authority shared with the DuckDB keyed creation
path — so the manifest states what was written, not what was planned.

### Determinism and integrity

Invariants this design introduces:

1. **Deterministic artifacts.** Same emit + config + overlay bytes + format +
   code version (+ cursor position for `--next`, range bounds for
   `--from`/`--to`) → byte-identical README and manifest. Neither artifact carries a wallclock generation
   timestamp or any machine-local value.
2. **Sourced values only.** Every manifest value and every README sentence
   traces to the mode template (forge-authored), the overlay
   (author-authored), or a value derived from the emit, the config, the
   resolved anchor, the compiled plan, or the package version. Nothing is
   fabricated.
3. **Artifacts are inert.** Dataset bytes, table sets, notices, and exit
   codes are identical with and without companion artifacts; artifacts are
   written only after data delivery.
4. **Generated output is never an input.** The README and manifest are
   overwritten unconditionally on every invocation; durable hand-written
   prose lives only in the overlay and the templates.

Invariants relied on: plan iteration order is deterministic (the notice
channel already depends on it); the writers' Arrow materialization is the
single truth of output schema; the incremental fingerprint refuses a changed
config or emit mid-drip, so whole-state manifest rewrites can never describe a
target written under a different plan.

### Incremental census exclusion

The CSV fresh/lost census treats companion artifact filenames — any name of
the form `<mode>-readme.md` / `<mode>-manifest.json` for the three
file-writing modes — the way it treats dot-entries: never counted as
non-hidden content. This keeps window-0 artifacts from turning every later
`--next` into a lost-cursor error, and keeps a directory holding only stale
artifacts classifiable as fresh. The DuckDB fresh/lost boundary is
catalog-based and unaffected by sibling files.

## Configuration

```yaml
mode: source
readme_overlay: ./readme-notes.md
source:
  ...
```

```markdown
<!-- readme-notes.md -->
## overview
Nightly extract of the clinic's operational database, reshaped for the
data-engineering course. Timestamps are Europe/London wallclock.

## table: patients
One row per registered patient; `status` is the current value at export time.

## table: ward_events
The polymorphic event log. `changes` holds the per-event column diff as JSON.
```

| Field | Type | Required | Description |
|---|---|---|---|
| `readme_overlay` | `str` | No | Path to the author's overlay markdown, resolved against the config file's directory. Absent: README renders without author prose. |

## Interface Contracts

### Config Models

```python
class ExportConfig(StrictBaseModel):
    """Top-level export configuration block (existing model; new field only)."""

    readme_overlay: str | None = None
    """Optional path to the author's README overlay markdown. Resolved
    against the config file's directory by whoever loaded the config; the
    model itself never touches the filesystem. Absent means the export's
    README renders from the mode template and derived facts alone."""
```

### Runtime Types

```python
@dataclass(frozen=True)
class ReadmeOverlay:
    """The parsed author overlay: export-level prose plus per-table notes.

    table_notes keys are author-facing output-table names; values are
    verbatim markdown bodies. Constructed only by load_readme_overlay.
    """

    overview: str | None
    table_notes: Mapping[str, str]


@dataclass(frozen=True)
class TableReport:
    """What one output table actually looked like after writing.

    columns are (output name, type text) pairs in output order, transcribed
    from the materialized Arrow schema. row_count is None on windowed
    invocations. keys is the table's declared TableKeys, or None when
    nothing was declared or the declaration was CSV-dropped.
    """

    name: str
    columns: tuple[tuple[str, str], ...]
    row_count: int | None
    keys: TableKeys | None


@dataclass(frozen=True)
class ExportReport:
    """Per-table reports for one invocation, in plan iteration order.

    Returned by every file-writing export entry point in place of the bare
    table -> row-count mapping (breaking change; callers read row counts
    from the reports).
    """

    tables: tuple[TableReport, ...]


@dataclass(frozen=True)
class WindowedArtifactState:
    """The windowed facts a companion-artifact rewrite records.

    regime is 'calendar' or 'sim_time'; label is the emitting invocation's
    window or range label; next_window_index is the cursor's next index
    after a --next window, or None for a --from/--to range (stateless: no
    cursor exists to have a next index).
    """

    regime: Literal["calendar", "sim_time"]
    label: str
    next_window_index: int | None
```

### Export entry points (changed)

Every file-writing entry point changes shape the same way — one new trailing
`overlay` parameter, `ExportReport` returned in place of the bare
table→row-count mapping. Dimensional is shown as the representative; source,
base, and the incremental windowed entry point change identically:

```python
def export_dimensional(
    emit: Emit,
    config: ExportConfig,
    out: Path,
    fmt: Literal["csv", "duckdb"],
    anchor: EffectiveAnchor | None,
    notice_sink: NoticeSink,
    overlay: ReadmeOverlay | None,
) -> ExportReport:
    """Run the dimensional exporter, write the star schema and its artifacts.

    Existing behavior unchanged through data delivery, plus: immediately
    after plan compile — before any write — calls validate_overlay_tables
    against the plan's output-table names when overlay is present; after all
    data is delivered, assembles the ExportReport from the write dispatch's
    per-table schema + row counts and invokes write_companion_artifacts.

    Args:
        overlay: The parsed overlay, or None (no overlay configured).
        (remaining args unchanged)

    Returns:
        The invocation's ExportReport, tables in plan iteration order.

    Raises:
        ReadmeOverlayUnknownTable: An overlay slot names a table the plan
            does not produce; nothing has been written.
        (existing raises unchanged; ExportRuntimeError now also covers a
        failed artifact write)
    """
```

### Functions

```python
def load_readme_overlay(path: Path) -> ReadmeOverlay:
    """Parse an overlay markdown file into its slots.

    Applies the overlay grammar: H2-delimited slots, keys 'overview' and
    'table: <name>', bodies verbatim with leading/trailing blank lines
    trimmed. Table existence is NOT checked here (the overlay is loaded
    before any plan exists); see validate_overlay_tables.

    Args:
        path: Absolute path to the overlay file.

    Returns:
        The parsed ReadmeOverlay.

    Raises:
        ReadmeOverlayInvalid: The file is unreadable or not UTF-8; content
            precedes the first slot heading; a heading matches neither slot
            form; a slot key occurs twice.
    """


def validate_overlay_tables(
    overlay: ReadmeOverlay,
    output_table_names: Sequence[str],
) -> None:
    """Refuse overlay table notes that reference tables the plan won't produce.

    Called after plan compile and before any data is written, so an overlay
    error never leaves a partially documented, partially written target.

    Args:
        overlay: The parsed overlay.
        output_table_names: Author-facing output-table names of the compiled
            plan, in plan iteration order.

    Raises:
        ReadmeOverlayUnknownTable: A 'table:' slot names a table absent from
            output_table_names; the message names the slot and lists the
            plan's tables.
    """


def write_companion_artifacts(
    emit: Emit,
    config: ExportConfig,
    fmt: Literal["csv", "duckdb"],
    anchor: EffectiveAnchor | None,
    report: ExportReport,
    overlay: ReadmeOverlay | None,
    target: Path,
    windowed: WindowedArtifactState | None,
) -> None:
    """Render and write the README and manifest for one export invocation.

    Mode-neutral: the mode contributes its packaged template and its report.
    Placement and prefix follow the target: a directory target places
    '<mode>-*' inside it; a .duckdb file target places '<db-stem>-<mode>-*'
    beside it. Both files are overwritten unconditionally. Called after all
    data of the invocation is delivered; never called on a drained or failed
    invocation.

    Args:
        emit: The open emit (sidecar identity, base.json bytes for hashing).
        config: The validated export config.
        fmt: The resolved output format.
        anchor: The resolved effective anchor, or None.
        report: The invocation's per-table report.
        overlay: The parsed overlay, or None.
        target: The output directory (csv) or .duckdb file path (duckdb).
        windowed: Windowed invocation facts, or None for a full export.

    Raises:
        ExportRuntimeError: An artifact file cannot be written.
    """


def is_companion_artifact_name(name: str) -> bool:
    """Whether a directory entry is a companion artifact of any file-writing mode.

    True for '<mode>-readme.md' / '<mode>-manifest.json' with mode in
    {dimensional, source, base}. Used by the incremental CSV fresh/lost
    census to exclude artifacts from the non-hidden-entry count.

    Args:
        name: A directory entry basename.

    Returns:
        True iff the name is a companion artifact filename.
    """
```

### Errors

Both errors live in the shipped `ExporterError` hierarchy, so the CLI's
existing `(ReaderError, ExporterError)` funnel catches them as exit 1 — no
new CLI handling.

```python
class ReadmeOverlayInvalid(ConfigError):
    """The overlay file is unreadable, not UTF-8, or violates the slot
    grammar. A `ConfigError`: the overlay is author-authored input that
    fails at load time, before any emit or plan exists — the loader
    failure domain, exactly like a malformed config file."""


class ReadmeOverlayUnknownTable(ExportError):
    """An overlay 'table:' slot names a table the compiled plan does not
    produce. An `ExportError`: well-formed author input that does not fit
    this export — a plan-time business-rule refusal, raised before any
    data is written."""
```

## Validation Rules

### Parse-Time (Pydantic)

```python
@field_validator("readme_overlay")
def readme_overlay_nonempty(cls, v: str | None) -> str | None:
    """A present readme_overlay is a non-empty, non-whitespace path string."""
```

### Business Rules

| Rule | Checks | Error |
|---|---|---|
| Overlay grammar | UTF-8 readable; no prose before the first H2; every H2 is `overview` or `table: <name>`; no duplicate keys | `ReadmeOverlayInvalid` naming the offending heading or key |
| Overlay↔plan agreement | Every `table:` slot names a compiled output table; enforced post-compile, pre-write | `ReadmeOverlayUnknownTable` naming the slot and listing the plan's tables |
| Fingerprint exclusion | The incremental fingerprint's canonical config dump excludes `readme_overlay`; changing only the overlay pointer or content never raises `IncrementalFingerprintMismatch` | — (behavioral rule, test-guarded) |
