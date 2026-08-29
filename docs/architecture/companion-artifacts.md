# Companion Artifacts

**Status:** Implemented. Code is the contract — see
[`exporters/companion/`](../../src/fabulexa_forge/exporters/companion/)
(`overlay.py`, `readme.py`, `manifest.py`, `artifacts.py`, `templates/`) and
[`tests/exporters/companion/`](../../tests/exporters/companion/). Public API:
`load_readme_overlay`, `validate_overlay_tables`, `write_companion_artifacts`,
`is_companion_artifact_name`, `ReadmeOverlay`, `WindowedArtifactState`
(plus `ExportReport` / `TableReport` from
[`exporters/query_spec.py`](../../src/fabulexa_forge/exporters/query_spec.py)).

Every file-writing export invocation (dimensional, source, base — full or
windowed) deposits two **companion artifacts** beside its datasets: a
human-readable README and a machine-readable, deterministic manifest. Without
them an export directory is mute — the bundle arrives self-describing
(`base.json` for machines, an atlas for humans), and the reshape would sever
that documentation exactly at the hand-off to the audience least equipped to
recover it (educators, engineers, analysts who do not read Python). The README
is rendered from three inputs — a forge-authored per-mode template (mode
semantics), an optional author-supplied **overlay** (domain prose), and derived
per-export facts — so regeneration is always safe: durable hand-written prose
lives only in the *inputs* (template in the package, overlay beside the
author's config), never in the generated output. The manifest carries only
sourced per-export facts for tooling.

---

## Boundary

- **Inputs.** The open `Emit` (sidecar identity, `base.json` bytes for
  hashing), the validated `ExportConfig`, the resolved format, the resolved
  `EffectiveAnchor` (or None), the invocation's `ExportReport`, the parsed
  `ReadmeOverlay` (or None), the output target, and windowed-invocation facts
  (`WindowedArtifactState`, or None for a full export).
- **Output.** Exactly two files — `<prefix>-readme.md` and
  `<prefix>-manifest.json` — placed by the target (§ Artifact names and
  placement). Nothing else on disk is touched.
- **Entry-point shape.** Every file-writing export entry point
  (`export_dimensional` / `export_source` / `export_base` and the incremental
  driver's windowed entry) takes an `overlay` parameter and returns an
  `ExportReport` — per-table columns/types, row counts, declared keys, and the
  per-column provenance maps
  ([`documentation-channel.md`](documentation-channel.md) § Provenance
  carriage), in plan iteration order — the same object the companion writer
  renders from. The
  engine is `validate_overlay_tables`' caller (it is the only component
  holding both the compiled plan and the overlay), invoking it immediately
  after plan compile, before any write; it finishes by invoking
  `write_companion_artifacts` after all data is delivered.
- **Config surface.** One optional top-level `ExportConfig` field,
  [`readme_overlay`](../../src/fabulexa_forge/config/models.py) — a path
  resolved against the config file's directory by whoever loaded the config
  (the CLI's `_resolve_readme_overlay`; library callers do the same or pass no
  overlay). The model never touches the filesystem, keeping the config
  emit-independent; overlay existence, readability, and slot validity are
  load-/plan-time checks outside the model.
- **Mode-neutral.** The companion writer holds no mode-specific branching; the
  mode contributes its packaged template and its report.

## Semantics

### Artifact names and placement

The artifact pair is named by a **prefix** derived from the target:

| Target | Placement | Prefix |
|---|---|---|
| `csv` (output directory) | Inside the output directory | `<mode>` |
| `duckdb` (`.duckdb` file path) | Sibling files of the database file | `<db-stem>-<mode>` |

The prefix comes from the mode literal, so co-located exports of different
modes never collide; the db-stem component keeps two same-mode warehouses in
one directory from clobbering each other's docs. Windowed CSV exports place
the artifacts at the output-directory root, never inside window drop
directories.

### Writing rules

| Condition | Result |
|---|---|
| Full export completes | Both artifacts written after all data files/tables, from that invocation's report |
| Incremental `--next` emits a window | Window data + cursor committed first, then both artifacts rewritten whole-state |
| Incremental `--next` finds the run drained | Nothing written — data, cursor, and artifacts all untouched |
| Explicit `--from`/`--to` range | A windowed invocation: artifacts written after the range's data; its `next_window_index` is null (a range is stateless — no cursor exists) |
| Empty window | Artifacts rewritten like any emitting window — "ran, empty" stays distinguishable |
| Target already holds this prefix's artifacts | Overwritten unconditionally, like the data files themselves — no notice, no prompt |
| Artifact write fails after data is committed | The invocation errors (`ExportRuntimeError`); data and cursor are already sound, and the next *emitting* invocation rewrites both artifacts |
| Export errors before writing data | No artifacts written — companion artifacts document delivered output only |

There is no regenerate verb; deleting the two artifact files is always safe.

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
slot form and are `ReadmeOverlayInvalid` — never silently normalized. A slot
with an empty body is legal and renders as if absent; an output table with no
overlay slot is legal and carries derived facts only — no placeholder, no TODO
stub. A `table:` slot naming a table the compiled plan does not produce is
`ReadmeOverlayUnknownTable` — the drift alarm that keeps author prose honest —
raised after plan compile and before data delivery, so an overlay error never
leaves a half-documented, half-written target. The full refusal set is in
§ Validation Rules; the grammar's one implementation is
[`overlay.py`](../../src/fabulexa_forge/exporters/companion/overlay.py), its
examples [`tests/exporters/companion/`](../../tests/exporters/companion/).

### The README

The README is rendered output, never hand-edited. Its **ordering contract**: a
title identifying the mode and a generated-artifact marker naming the manifest
file; the overlay's `overview` (when present), then the emit's
`scenario_description` (when present) — author prose first; either or both may
be absent, and absence renders nothing; the mode template's semantics
prose; one section per output table in plan iteration order — the table's
overlay note (when present), then the forwarded `tables[].description` (when
present), then its derived column inventory (names, types, key markings, and
per-column description and unit where the documentation channel's inheritance
rule yields them), then declared-value gloss lists for closed-domain and
kind-name-as-value columns, then row count (full exports only); then the
resolved anchor facts (start instant and IANA zone, or their absence); then
emit identity. Per-column documentation resolves through the reader's
documentation view via the provenance maps on the report
([`documentation-channel.md`](documentation-channel.md) § The
column-inheritance rule, § Provenance carriage); an undocumented item renders
nothing — no placeholder, no TODO.

Exact prose is authored in the templates, not specified here — but each mode's
template must cover how to read its shape: the dimensional template the star
layout and the SCD-2 validity columns, the source template the state/junction
tables and the polymorphic event log's `changes`, the base template the
state-at horizon and the record-index key columns. Templates ship inside the
package (one per mode,
[`companion/templates/`](../../src/fabulexa_forge/exporters/companion/templates/)),
so the installed CLI is self-sufficient; they are forge-maintained prose whose
only change driver is a mode's own contract changing.

### The manifest

`<prefix>-manifest.json` is a single JSON document; its field set is defined
by [`manifest.py`](../../src/fabulexa_forge/exporters/companion/manifest.py)
(`build_manifest_document`) — mode, format, package version, emit identity
(including the SHA-256 of `base.json`'s bytes), the resolved anchor, the
embedded config, windowed facts, and per-table entries. Its own
`manifest_format_version` is mode-definitional, like the event log's first id.
Normative rules the code conforms to:

- **Byte form is pinned.** UTF-8, `ensure_ascii` off, two-space indent, sorted
  object keys, list order semantic (tables, columns), trailing newline.
- **The manifest states what was written, not what was planned.** Column names
  and types are transcribed from the materialized Arrow schema — the single
  transcription authority shared with the DuckDB keyed creation path
  ([`writers.md`](writers.md) § The DuckDB writer's keyed creation path). The
  shared query-spec write dispatch and the windowed write paths surface
  per-table schema alongside the row count, so a report is assembled from what
  the invocation materialized, never from a second run of the specs.
- **Table entries carry the author-facing output-table name** — the same name
  the overlay's `table:` slots use. An SCD-2 dim windowed with a `valid_to`
  column is one entry under its view name, its columns the materialized
  physical projection: the view is not a written relation, so it gets no entry
  of its own. Bookkeeping objects (`_export_meta`, `_export_windows`, the CSV
  cursor file) are driver state, not output tables, and never appear.
- **Documentation fields mirror the resolved dictionary.** Top-level
  `scenario_description`; per-table `description`; per-column `description`,
  `unit`, and `enum_options` (the ordered `[{value, description}]` list where
  the column's source property carries a declared domain) — all resolved
  through the reader's documentation view under the channel's inheritance
  rule ([`documentation-channel.md`](documentation-channel.md)), never
  re-derived from SQL, config, or the materialized schema. Absent → JSON
  `null`, the manifest's stable-field-set posture, matching `primary_key`:
  `null` encodes absence faithfully, never a default.
- **Keys are transcribed declarations, never inferences.** `primary_key` /
  `unique` carry only what `declare_keys` declared — a surface the base and
  source modes have and the dimensional mode does not, so a dimensional
  export's entries are always null there and the README marks no keys for it.
  The key *election* (which identity surface presents as each table's id) is
  on record in every mode via the embedded config's `keys` block.
- **The embedded config includes `readme_overlay`** — the manifest documents
  everything that shaped the export. The exclusion of `readme_overlay` from
  the incremental fingerprint is the fingerprint's alone
  ([`incremental.md`](incremental.md) § Drained detection and the cursor).
- **`row_count` is per-invocation truth.** Full exports carry counts; windowed
  invocations carry null — a window's counts describe the window, not the
  accumulated target. `incremental` is null on a full export; on a windowed
  invocation it carries the regime, the window or range label, and the next
  window index (null on a range).

## Invariants

1. **Deterministic artifacts.** Same emit + config + overlay bytes + format +
   code version (+ cursor position for `--next`, range bounds for
   `--from`/`--to`) → byte-identical README and manifest. Neither artifact
   carries a wallclock generation timestamp or any machine-local value.
2. **Sourced values only.** Every manifest value and every README sentence
   traces to the mode template (forge-authored), the overlay
   (author-authored), or a value derived from the emit, the config, the
   resolved anchor, the compiled plan, or the package version. Nothing is
   fabricated.
3. **Artifacts are inert.** Dataset bytes, table sets, notices, and exit codes
   are identical with and without companion artifacts; artifacts are written
   only after data delivery, and the one outcome they add — the
   artifact-write-failure error — can only occur after data and cursor are
   already sound.
4. **Generated output is never an input.** The README and manifest are
   overwritten unconditionally on every invocation; durable hand-written prose
   lives only in the overlay and the templates.

Invariants relied on: plan iteration order is deterministic (the notice
channel already depends on it); the writers' Arrow materialization is the
single truth of output schema; the incremental fingerprint refuses a changed
config or emit mid-drip, so whole-state manifest rewrites can never describe a
target written under a different plan; documentation is run-level
(contract-fixed at run initialization), so every window of an incremental
export renders identical documentation.

## Validation Rules

**Parse-time (Pydantic).** A present `readme_overlay` is a non-empty,
non-whitespace path string (`readme_overlay_nonempty`).

**Load-time (overlay grammar).** `ReadmeOverlayInvalid` — a `ConfigError`,
raised by `load_readme_overlay` before any emit or plan exists: the file is
unreadable or not UTF-8; content precedes the first H2 slot heading; a heading
matches neither slot form; a slot key occurs twice. Names the offending
heading or key.

**Plan-time (overlay↔plan agreement).** `ReadmeOverlayUnknownTable` — an
`ExportError`, raised by `validate_overlay_tables` post-compile, pre-write: a
`table:` slot names a table absent from the compiled plan. Names the slot and
lists the plan's tables. Both errors live in the `ExporterError` hierarchy, so
the CLI's existing `(ReaderError, ExporterError)` funnel renders them as
exit 1.

**Fingerprint exclusion (behavioral, test-guarded).** The incremental
fingerprint's canonical config dump excludes `readme_overlay`; changing only
the overlay pointer or content never raises `IncrementalFingerprintMismatch`
([`tests/incremental/test_fingerprint.py`](../../tests/incremental/test_fingerprint.py)).

## Rationale

- **The overlay is a separate file, not inline config prose.** Domain prose is
  markdown-shaped and table-scoped; keeping it beside the author's config in
  its own file preserves the config's emit-independence and lets the README be
  regenerated from scratch on every invocation without ever holding
  hand-written content.
- **Exact, case-sensitive slot matching.** A normalized match (`## Overview`,
  variant spacing) would silently accept prose the renderer then drops or
  misfiles; the loud refusal keeps author intent and rendered output in
  agreement.
- **Artifact writing emits no notices.** An "overwrote previous artifacts"
  notice would depend on filesystem state, breaking notice determinism — and
  notices are not embedded in either artifact for the same reason (a notice
  stream is per-invocation stderr, not durable documentation).
- **The last-window staleness wart is accepted.** If the artifact write fails
  on the run's last emittable window, every later `--next` is drained and
  writes nothing, so the stale or missing artifacts are never repaired — a
  drained invocation materializes no tables and has no report to rewrite from.
  This is deliberate: drained stays "nothing written", the data and cursor are
  sound, and the staleness is detectable (the manifest's
  `incremental.next_window_index` lags the cursor). The alternative — a
  drained invocation that writes files — would blur the drained contract for
  an edge case whose repair is "delete two files".
- **Whole-state rewrite, not per-window artifact drops.** One README and one
  manifest describing the accumulated target is what a consumer needs; a trail
  of per-window artifacts would document the drip's history, not the dataset
  in hand, and would land inside window drop directories a consumer may never
  open.

## Boundaries

- **Streaming has no companion artifacts.** Its sinks (stdout, Kafka) are not
  all directories; a streaming-side companion surface is a separate design.
- **The corrupt path has no companion artifacts.** A corrupted base ships
  `defects.json`; a corrupt-side README is a separate design (and must not
  leak the answer key).
- **`validate`, `compare`, and `init` write none.** `init` writes no datasets;
  companion artifacts document delivered output only.
- **No new reserved output names.** Artifact filenames cannot collide with
  `<table>.csv` files or with tables inside a `.duckdb`; the existing
  reservations stand unextended.
- **No notice codes.** The notice registry is untouched by this surface
  (§ Rationale).

## Related

| Document | Why |
|---|---|
| [`documentation-channel.md`](documentation-channel.md) | The inheritance and provenance rules the README's per-column documentation and the manifest's documentation fields render under |
| [`reader.md`](reader.md) | The documentation view (§ The documentation view) the builders resolve every provenance entry through |
| [`incremental.md`](incremental.md) | The windowed caller — whole-state artifact rewrite after data + cursor commit, the CSV census exclusion, the fingerprint's `readme_overlay` exclusion |
| [`writers.md`](writers.md) | The Arrow transcription authority the report's columns/types come from |
| [`declared-keys.md`](declared-keys.md) | The `declare_keys` declarations the manifest transcribes |
| [`key-election.md`](key-election.md) | The `keys` election on record via the embedded config |
| [`anchor.md`](anchor.md) | The resolved `EffectiveAnchor` the manifest and README report |
| [`dimensional.md`](dimensional.md) / [`source.md`](source.md) / [`base.md`](base.md) | The three file-writing engines whose entry points return the `ExportReport` and invoke the companion writer |
| [`notices.md`](notices.md) | The notice channel this surface deliberately does not touch |
| [`../../CLAUDE.md`](../../CLAUDE.md) | Principles — faithful reshaping (Invariant 2 is Principle #3 for documentation), no invented mapping values |
