---
status: draft
---

# Supplementary Tables

A pending architecture doc: author-supplied tables carried verbatim into a
dimensional export — the manually-entered-warehouse-data case.

---

## Problem

**An author cannot ship a small hand-maintained table with a warehouse.** The
`saas-billing` example needs negotiated per-account price ladders for a few
named enterprise accounts; any realistic warehouse carries mapping tables
(region codes, product categories) nobody generated — entered by hand,
present in the warehouse regardless of what the operational system has
recorded yet. Today the only route is a file beside the export, outside the
export. The README and manifest then describe an incomplete dataset,
`compare`'s expected side has to be assembled by hand, and the dataset pack
cannot carry it. Principle #3 as written — "every exporter output value traces
to a base-layer value" — forbids forge carrying such a table even verbatim.

```yaml
# Wanted, refused today: an unknown field
supplements:
  - name: custom_rate_tier
    file: custom_rate_tier.csv
```

## Solution

A `supplements` block on `ExportConfig`, honoured by the dimensional mode:
author-supplied tables carried verbatim into the warehouse as ordinary
tables, delivered whole and immediately — as a hand-maintained reference
table is present in a real warehouse ahead of, and regardless of, the
operational rows it names. A supplement is declared with a name, a typed
column list, its data (a CSV file or inline rows), and prose. Forge
interprets none of its values: it asserts no key, no reference, no timing for
them.

```yaml
mode: dimensional
# ...
supplements:
  - name: custom_rate_tier
    file: custom_rate_tier.csv        # resolved against the config file's directory
    columns:
      account_id: VARCHAR
      sku_id: VARCHAR
      ordinal: BIGINT
      from_units: BIGINT
      to_units: BIGINT
      rate: DOUBLE
      effective_from: DATE
      effective_to: DATE
    description: Negotiated per-account usage price ladders; where a row exists for an account and SKU it replaces the SKU's rate_tier bands for that account.
    descriptions:
      to_units: Inclusive upper bound of the band; empty on the open-ended top band.
      effective_to: Last day the band applies; empty while it is current.
  - name: region_code
    rows:
      - {code: EMEA, label: Europe, Middle East and Africa}
      - {code: AMER, label: Americas}
    columns: {code: VARCHAR, label: VARCHAR}
    description: Sales-region codes used by account management.
```

Mechanically, each supplement compiles to one `QuerySpec` whose relation reads
the declared data with the declared types in the same session every other
relation is materialized in. It therefore flows through the existing write
dispatch, the writers' pinned text forms, `TableReport`, the manifest, the
README, incremental delivery, and shaped playback with no parallel machinery.
In the README a supplement is indistinguishable from any other table; in the
manifest its entry records the source file and its hash.

```
config ──▶ resolve supplements (paths, bytes, sha256, text rows)
                 │
   dimensional plan compile ──▶ declared-table specs
                 │                 + supplement specs (appended, snapshot class)
                 ▼
   write dispatch ──▶ writers ──▶ TableReport ──▶ README (as any table)
                                              └──▶ manifest (+ source file, sha256)
```

**The principles this is defined against.** `CLAUDE.md` Principle #3
(*Faithful reshaping*) admits two sources for an exporter output value: a
base-layer value, or a declared supplement's author-supplied data, cast to
its declared types and recorded in the manifest. Principle #4 (*Integrity
preserved*) makes no integrity claim on a supplement's columns: forge declares
no reference for them, delivers the supplement whole regardless of which of
the rows it may name have been delivered yet, and its own reshape still
introduces no dangling or forward reference on a supplement's account. Forge
never invents a value — a supplement is sourced from the author instead of
the emit, and the DO-NOT entry "fabricate data in an exporter" holds. This
design is the feature those clauses describe, not an exception to them.

**The spectrum this design does not climb.** Three shapes of "data the emit
did not carry" exist, and only the first is this design:

1. *Manually entered warehouse data* — present whole, immediately, in the
   warehouse's own shape. **This design.**
2. *Manually entered data with its own emit times* — rows that appear at a
   declared instant, whether or not what they name has arrived. A later,
   additive per-row availability column on the same declaration.
3. *Data the upstream system cannot reasonably provide* — emit-shaped, with
   real structural columns, run through C1–C15, visible to every mode. A
   bundle augmenter beside the corrupters, writing into `run.duckdb` +
   `base.json`; a separate design (and the home of generated exercises).

## Affected Subsystems

- **Config envelope (`ExportConfig`)** — gains an optional `supplements` list
  of `SupplementDecl`, legal only under `mode: dimensional`. Each supplement's
  `description` / `descriptions` join the documentation-presentation surfaces
  (excluded from the incremental fingerprint), and its `file` string is
  excluded too — the file's SHA-256 stands in for it; every other supplement
  field is data-affecting.
- **Config loading (the caller that resolves paths)** — whoever loads the
  config also resolves each supplement's `file` against the config's
  directory, reads and hashes its bytes, and parses it into text cells,
  exactly as it resolves `readme_overlay`; the model never touches the
  filesystem. A new `load_supplements` step produces the resolved set — the
  declaration plus its cells — that the dimensional entry points, the
  incremental driver, the shaped playback head, and the pack builder consume.
  Nothing downstream reads the file again.
- **The dimensional export entry points** — `export_dimensional`, the
  incremental driver's windowed entry, and `open_shaped_playback` each take
  the resolved supplements beside the overlay (the overlay precedent: a
  loader-produced input outside the config). The plan compile appends the
  supplement specs after the declared tables in declaration order and runs
  the reserved-name gate over the supplement names (a supplement colliding
  with a declared table is a parse rule — both name lists are on the config).
- **Companion artifacts** — the manifest's table entry gains a `supplement`
  field and `manifest_format_version` advances. The README renders a
  supplement as any table under its existing ordering contract; nothing else
  about the README changes here.
- **Incremental driver** — supplements are delivered as `snapshot` class in
  every emitting window; the fingerprint document gains each supplement's
  ordered column list and file hash and excludes the new presentation fields.
- **Shaped playback (tier 2)** — `open_shaped_playback` binds the resolved
  supplements beside the config; supplement tables are part of a dimensional
  shape's table set and of the per-ask `tables` selection domain,
  horizon-invariant, identical at every `T`. Every supplement gate runs at
  open over the resolved rows; open still reads no emit data.
- **Dataset distribution** — the pack builder loads every packed config
  through its loader (the publishability gate for configs) and carries every
  supplement file a packed config references, at the config-relative path;
  it refuses a config that does not load and a missing or escaping file.
- **Reader session** — the site where supplement cells become a relation: one
  `VALUES` list of text cells with one `CAST` per declared column, compiled
  and materialized in the same session as every other relation, so temporal
  and decimal values serialize under the writers' pinned forms like any
  other column. The session never opens the author's file.

## What Doesn't Change

- **Writers.** They serialize whatever relation they are handed; a supplement
  is one more relation. No writer learns what a supplement is. (Every site
  that lands a file or removes a directory exposes its planned paths through
  a pure naming function the source-is-output gate reads — § Interface
  Contracts; the companion module's already exists — but naming is not
  supplement-aware; it is each site's existing convention made callable.
  The DuckDB writer needs none: its one output is `out` itself.)
- **`compare`.** A pure two-input surface; a supplement table is an ordinary
  table on both sides.
- **Corrupters.** They act on the bundle, which has no supplements.
- **Source and base modes.** Neither accepts `supplements` (parse-time
  refusal). Base is the emit's own shape — non-emit data there is the
  bundle-augmenter case, not this one. Source's app-database seed tables are
  the same mechanism and a separable follow-up.
- **Streaming.** `StreamConfig` is a separate envelope; it gains no
  `supplements`.
- **`init`.** No proposal engine proposes supplements — nothing in a sidecar
  implies one.
- **The README's ordering contract and the overlay grammar** (`## overview`,
  `## table: <name>`). A `table:` slot may name a supplement — it is an
  output table of the compiled plan. (The consumer-voice README rewrite is a
  separate companion-artifacts design, tracked as
  `note: consumer-voice-readme-and-required-title-still-need-a-design-of-their-own`.)
- **The manifest's content for existing fields** — emit identity, anchor,
  embedded config, per-table facts.
- **The documentation channel's inheritance rule.** A supplement column has
  no source property, so it inherits nothing; its documentation is the
  author's `descriptions` alone — the same "undocumented renders nothing"
  posture as any other column.
- **Per-column value elections** (`render:`, `derived:`) do not apply to
  supplements. A supplement's types are declared directly; there is nothing
  to elect.
- **Key election, the fk pathfind, and `lookup`** do not reach supplements. A
  supplement declares no keys, takes no constraints, and is neither an `fk`
  target nor a `lookup` source: it is not a kind.
- **The reader's boundary.** Reading an author CSV is not opening
  `run.duckdb` or parsing `base.json`; the reader-first rule is about the
  bundle.

## Semantics

### Supplement declaration and data

Both data sources resolve, at load, to the same thing: an ordered list of
**text rows** — one `str | None` cell per declared column — that the
compile turns into a relation. The file is read exactly once, by the loader;
no later step (compile, window write, playback ask) touches it.

| Condition | Result |
|---|---|
| `file` given | The path is resolved against the config file's directory by the loader; the bytes are read once, hashed (SHA-256), decoded, and parsed as RFC 4180 CSV (Python `csv`, default dialect) into text rows. Absent file: `SupplementFileMissing` at load |
| `rows` given | Each row's values are rendered to text cells at load, in list order (below). No file, no hash |
| Both or neither | Refused at parse (validator `supplement_source_exactly_one`) |
| CSV header | Must equal the declared column names, in declared order, exactly (case-sensitive, no extras, no omissions). Otherwise `SupplementHeaderMismatch` naming the first differing position; a file with no header row (zero bytes, or a BOM alone) is a mismatch at position 1 |
| CSV data row | Must carry exactly as many fields as the header. A ragged row is `SupplementFileInvalid` naming the 1-based data row |
| CSV cell → text cell | An empty field (quoted or not) is `None`; any other field is its text, verbatim. A file supplement therefore cannot carry an empty-string VARCHAR — the one asymmetry between the two sources (an inline `''` is the empty string, below) |
| Inline row → text cells | Each row's keys must equal the declared column set exactly (validator `supplement_rows_shape` at parse). Each value is a YAML scalar of `str \| int \| float \| None`: `null` → `None`; a string → itself (`''` included: the empty string, not `None`); an integer → its decimal text; a float → its shortest round-trip text (`repr`). Two scalar classes the YAML loader resolves before the model sees them are refused at parse by validator `supplement_rows_scalars`: a `date` / `datetime` / `time` object (an unquoted `2024-01-01`) with "quote temporal values", and a `bool` (an unquoted `true` — or `yes` / `no` / `on` / `off`, which YAML 1.1 resolves to the same `bool`, so a VARCHAR cell `NO` would otherwise ship as `false` with no way to tell after load) with "quote boolean values"; a BOOLEAN column takes the quoted text `'true'` and casts like any other cell. The remaining YAML 1.1 implicit resolutions (`12:30:00` sexagesimal → `45000`, leading-zero octal `010` → `8`, `1_000` → `1000`, `~` → null) are numeric and cannot be told from an intended number, so they are the author's guard alone: **quote any value YAML would not read as a plain string**, stated in the field's docstring and the recipe |
| Text cell → typed value | `CAST(cell AS <declared type>)`; a `None` cell is `NULL`. The cast is the session's `VARCHAR` → `<type>` cast, **verbatim** — what DuckDB accepts, forge accepts, and the typed value is what DuckDB makes of the text. Forge specifies neither a stricter reading nor a more lenient one, so the cast's own leniency is part of the contract: a `DECIMAL(p, s)` cell with more than `s` fraction digits is rounded to `s` by the cast (`12.345` → `DECIMAL(5, 2)` is `12.35`); an integer column accepts `1.0`, `1e3`, and surrounding whitespace, and **rounds** a fractional text rather than refusing it (`1.5` → `BIGINT` is `2` — the one leniency that changes a value an author would read as exact, so the field docstring and the recipe name it); a `BOOLEAN` column accepts DuckDB's boolean spellings (`true` / `false`, `t` / `f`, `yes` / `no`, `1` / `0`, case-insensitively); a `DOUBLE` column accepts `nan` and `inf`. The compile **probes every cell before any write** (below) with `TRY_CAST` — the same cast, so a cell the probe passes is a cell the write casts identically; a cell that does not cast is `SupplementValueInvalid` naming the table, the column, the 1-based data row (file order / list order), and the cell text |
| Declared type text | One of the **supplement type vocabulary**, checked at parse (validator `supplement_types_known`): `BIGINT` / `INTEGER` / `SMALLINT` / `TINYINT`, `DOUBLE` / `FLOAT`, `BOOLEAN`, `VARCHAR`, `TIMESTAMP`, `DATE`, `TIME`, `TIMESTAMPTZ`, and `DECIMAL(p, s)` under the render elections' bounds (1 ≤ p ≤ 38, 0 ≤ s ≤ p) — matched case-insensitively, surrounding whitespace ignored, written into the relation in that canonical spelling. These are the canonical families `compare` classifies whose every value the writers serialize under a specified text form — the pinned forms for `DATE` / `TIME` / `TIMESTAMPTZ` / `DECIMAL`, the writers' default forms for the rest. `INTERVAL` and `BLOB` are **not** declarable although `compare` classifies both: the CSV writer's pinned `INTERVAL` form covers only the pure-microsecond durations forge itself derives (an author's `1 day` or `2 months` carries calendar components that form drops), and `BLOB` has no pinned CSV form at all. A nested, enum, or other session type is not declarable either, so no output column can carry a type the manifest transcription, the pinned forms, or `compare` was never specified for |
| `TIMESTAMPTZ` and the anchor | A `TIMESTAMPTZ` column requires a resolved anchor: the session is zone-pinned only when one resolves, and an unpinned session would render the column in the host's zone (the *Deterministic* invariant). Refused at plan compile with `TemporalRenderRequiresAnchor` naming the supplement and the column — the same rule and error the temporal elections apply. `TIMESTAMP`, `DATE`, and `TIME` are zone-free and need no anchor |
| Row order | Preserved as given; a supplement is written in file / list order |
| Zero data rows | A legal supplement — header-only CSV or `rows: []` — written as an empty typed table, reported with row count 0 (its relation form is below) |
| Encoding | UTF-8, decoded as `utf-8-sig` so a leading byte-order mark (what a spreadsheet's "CSV UTF-8" save writes) is stripped rather than left on the first header name to fail the header check with a header that looks identical to the declaration. A file that does not decode is `SupplementFileInvalid` |
| CSV dialect | Python `csv`'s default (`excel`) dialect with `strict=True`, fed with `newline=''` semantics: a newline inside a quoted cell is a legal part of that cell's text; an unterminated quote or a stray quote inside an unquoted field is `SupplementFileInvalid` (under the non-strict default the parser would silently read an unterminated quote to end of file) |

**The relation.** One `VALUES` list over the text rows — every cell a
`VARCHAR` literal or `NULL`, plus a leading row-position column — wrapped in a
`SELECT` that casts each declared column and orders by position (the position
column is projected away). A supplement with zero rows has no `VALUES` form
and compiles instead to `SELECT CAST(NULL AS <type>) AS <col>, … WHERE
false` — the same typed, empty relation the writers already handle for a
grain that resolved to no rows. It is built once per invocation as SQL over
the session and compiled into a `QuerySpec` with `write_mode='create'` (full
export) or `'replace'` (windowed), no keys, empty `provenance`, empty
`kind_values`, the author's `descriptions` as `author_descriptions`, the
author's `description` as `author_table_description`, `event_log=False`, and
a `supplement` provenance record. Because it is a `QuerySpec`, every
downstream contract — DESCRIBE transcription, the writers' pinned text forms
for `DATE` / `TIME` / `TIMESTAMPTZ` / `DECIMAL`, the report, the manifest,
the README — applies without special cases.

**Evaluation before any write.** The session evaluates SQL lazily, so a bad
cell left to the writer would surface mid-dispatch as an
`ExportRuntimeError` after earlier tables were written. The compile therefore
evaluates each supplement fully, in this order, and every failure is an
`ExportError` raised before the first table of the invocation is written:

1. Anchor — a `TIMESTAMPTZ` column with no resolved anchor is
   `TemporalRenderRequiresAnchor` (above).
2. Cells — per column in declared order, over the untyped text relation:
   the first row (by position) whose cell is non-`NULL` and whose
   `TRY_CAST(cell AS <type>)` is `NULL`; `SupplementValueInvalid` on the
   first hit. A column with no hit is clean.

The typed relation is then known to materialize; the writer reports its row
count, as for every table. Step 2 is the only place a supplement value is
diagnosed; the writer's later materialization cannot fail on a cast the
probe passed. Both steps are pure functions of the declaration, the resolved
rows, and the anchor — they touch no emit table — so every caller runs them
before its first write and the shaped playback head runs them at open.

**No interpretation.** Forge reads a supplement's cells only to cast them. It
does not check that an `account_id` names an account, that `effective_from`
precedes `effective_to`, or that any column is unique; a ladder's dates are
data, as `rate_tier`'s own repricing rows are. What the author typed is what
the warehouse holds — a typo ships, as it would from a hand-maintained
spreadsheet. A later `references` existence check is additive on this
declaration and out of this design.

### Output naming

| Condition | Result |
|---|---|
| Supplement `name` | A SQL identifier (parse-time, the same rule as a declared table) |
| Two supplements share a name | Refused at parse (validator `supplements_names_unique`) |
| A supplement's name equals a declared dimensional table's name | Refused at parse (validator `supplements_names_unique`, naming both) — a dimensional output table's name is its author-declared `name`, so both lists are on the config and the collision is decidable before any emit opens |
| A supplement's name is a reserved bookkeeping name (`_export_meta`, `_export_windows`) | Refused at plan compile by an `ExportError` naming the supplement — the shared reserved-name predicate (`is_reserved_table_name`, the one set every mode reads) applied to the supplement set with its own message, the way each mode iterates its own declaration shape over the same predicate; the dimensional gate's own message names a declared table and is not borrowed |
| A supplement's CSV filename under `fmt: csv` | `<name>.csv` in the output directory (or the window drop), like any table. SQL-identifier names cannot collide with the companion artifact filenames |
| A supplement's resolved source file is a path this invocation would write or a path under a directory it removes | Refused before any write (`SupplementSourceIsOutput`, an `ExportError` naming the supplement and the path). The natural config — `name: custom_rate_tier`, `file: custom_rate_tier.csv`, the config's directory as the CSV output directory — would otherwise overwrite the author's source with the re-serialized render (different quoting, `NULL` and temporal forms). Two tests, both over the resolved source path: **equality** with every file the invocation writes — each `<table>.csv` (under the `--next` window drop for a windowed invocation; a range's drop is `out` itself), the `.duckdb` file (`out` itself under `fmt: duckdb`), the two companion artifacts, and under `--next` CSV the cursor file; and **containment** in every directory the invocation removes wholesale — under `--next` CSV, the window drop `<out>/<label>` (deleted and re-created from staging on every emitting window) and the staging directory `<out>/.tmp_<label>` (a leftover is discarded); under a CSV range, the sibling staging directory `<out parent>/.tmp_<label>` (a leftover is discarded before staging; the range's drop is `out`, which it refuses when pre-existing, so nothing can lie under the drop itself) — so a source anywhere beneath a removed directory, at any depth, is refused, not just one that happens to share a table's filename. Neither set is enumerated by the caller: each site that lands a file or removes a directory exposes its planned paths through one pure naming function (§ Interface Contracts) that the write step and this gate both read, so the gate can never disagree with the writer about where a file lands or what a window removes. A full export and every DuckDB invocation remove nothing, so their containment set is empty |

### Incremental and playback

| Condition | Result |
|---|---|
| Delivery class | `snapshot`: the whole relation on every emitting window (DuckDB replace; CSV the whole file in each window drop). A supplement is horizon-invariant by construction — no horizon is taken to build it — and is present whole from window 0, regardless of which rows it may name have arrived (§ Solution, the principle amendment) |
| Empty window | The supplement is still rewritten whole, like every snapshot table |
| Explicit range | Delivered whole, like any snapshot table in a range |
| Fingerprint | The canonical config document excludes each supplement's `description` / `descriptions` (presentation, like `readme_overlay`) and its `file` string — the path is where the data was found, not what it is; the `sha256` below is the data identity — and includes every other supplement field (`name`, `columns`, `rows`). The fingerprint document gains `supplements`: a map from supplement name to `{"columns": [[name, type], …], "sha256": <hex or null>}` — the column list as an ordered list of pairs because the canonical form sorts object keys, which would erase a `columns` reordering that reorders the output table; `type` in its canonical spelling (the spelling written into the relation); the SHA-256 `null` for inline. The embedded config dump still carries `columns` as declared, so a re-spelling alone (`bigint` → `BIGINT`) trips the fingerprint through the dump like any other config edit — deterministic, and not worth a carve-out. A changed CSV, a changed inline row, a changed or reordered column, or a changed type mid-drip is `IncrementalFingerprintMismatch`; an edited description, or a moved or renamed file whose bytes are unchanged, is not |
| Shaped playback `state(T)` | Includes every supplement table, identical at every `T` |
| Shaped playback `window(T1, T2)` | Delivers every supplement as `snapshot`; `tables()` reports each with class `snapshot` |
| Shaped playback per-ask `tables` selection | A supplement name is a legal member of `window(tables=…)` / `state(tables=…)` — the shape's table set is the union, and the selection gates (bare `str`, empty, unknown name) range over the union. Projection invariance holds trivially: a supplement's answer never depends on the selection. A selection naming **no** dimensional table opens no horizon and runs no dimensional compile, so it emits no plan notices — the horizon economy taken to its floor; a selection mixing both kinds compiles the dimensional part exactly as today and appends the selected supplements in declaration order |
| Shaped playback open | Static completeness holds for supplements outright ([`playback.md`](../playback.md) § Shaped window): every supplement gate — the reserved-name gate, the anchor rule, and the cell probe — is a pure function of the declaration, the resolved rows, and the anchor, touches no emit table, and runs at open. Open reads no emit data (the cell probe is a session query over the `VALUES` relation). A shape that opens has no reason any supplement table cannot be asked; no supplement ask ever refuses |

### Manifest

| Field | Value |
|---|---|
| `manifest_format_version` | Advances by one (a new per-table field) |
| `config` | The embedded config dump now carries `supplements` (with `file` as declared — the author's string, unresolved — and `rows` verbatim). The pinned byte form sorts object keys, so the embedded `supplements[].columns` map does **not** preserve declared order; the table entry's `columns` list (below) is the order authority, as it is for every table |
| `tables[].supplement` | `null` for every mode table; `{"file": "<as declared>", "sha256": "<hex>"}` for a file supplement; `{"file": null, "sha256": null}` for an inline supplement. The stable-field-set posture: the key is always present |
| `tables[].description`, `columns[].description` | The author's `description` / `descriptions`; `unit` and `enum_options` are `null` (no source property) |
| `tables[].primary_key`, `unique` | `null` |

### Dataset packs

| Condition | Result |
|---|---|
| Every packed config | Loaded by the builder through the loader its top-level shape names — a document with a top-level `mode` key is an export config (`load_export_config`), one with a top-level `streams` key a stream config (`load_stream_config`); a document with neither, or one its loader refuses, is a build refusal rendering the loader's diagnostic. The pack's configs therefore load under the wheel that publishes them by construction |
| A packed export config declares a file supplement | The file is a pack member at its config-relative path (the config sits at the archive root, so `custom_rate_tier.csv` or `data/custom_rate_tier.csv`), resolved through `load_supplements` |
| The file is absent | Build refusal naming the config and the path |
| The path escapes the dataset directory (`..`, absolute) | Build refusal; `get`'s member-safety rule already refuses such members on extraction |
| Determinism | Supplement members join the sorted-path member order under the existing normalization |

### Invariants

Relied on: plan iteration order is deterministic; the writers' Arrow
materialization is the single truth of output schema; the session is
zone-pinned whenever an anchor resolves, and the one zone-bearing declarable
type (`TIMESTAMPTZ`) requires one, so supplement temporal parsing is
machine-independent; the incremental fingerprint refuses a changed data seam.

Introduced:

1. **Verbatim carriage.** A supplement's written rows are its declared data
   cast to its declared types under the session's `VARCHAR` cast
   (§ Supplement declaration and data — the cast's own reading of the text,
   decimal-scale and integer rounding and lenient spellings included, is the
   only transformation),
   in its given order — no derivation, merge, projection, filter, or fill.
   Same bytes + same declaration → identical relation.
2. **Sourced or supplied.** Every output value traces to a base-layer value
   or to a declared supplement's author-supplied data, cast to its declared
   types; the manifest names
   which for every table.
3. **No claim on supplied data.** Forge reads a supplement's cells only to
   cast them; it asserts no key, reference, order, or timing for them, and
   its own reshape introduces no dangling or forward reference on their
   account.
4. **Presentation is inert.** A well-formed `description` / `descriptions`
   changes README and manifest documentation bytes only — never data,
   notices, exit codes, or the fingerprint. (Well-formedness — non-blank,
   keys among the declared columns — is a parse rule like any field's.)

## Configuration

```yaml
mode: dimensional
readme_overlay: README-overlay.md      # unchanged

supplements:                           # optional; dimensional only; omitted → no supplement tables
  - name: custom_rate_tier
    file: custom_rate_tier.csv
    columns:                           # ordered; the CSV header must match exactly
      account_id: VARCHAR
      sku_id: VARCHAR
      ordinal: BIGINT
      from_units: BIGINT
      to_units: BIGINT
      rate: DOUBLE
      effective_from: DATE
      effective_to: DATE
    description: Negotiated per-account usage price ladders; where a row exists for an account and SKU it replaces the SKU's rate_tier bands for that account.
    descriptions:
      to_units: Inclusive upper bound of the band; empty on the open-ended top band.
      effective_to: Last day the band applies; empty while it is current.
  - name: region_code
    rows:
      - {code: EMEA, label: Europe, Middle East and Africa}
      - {code: AMER, label: Americas}
    columns: {code: VARCHAR, label: VARCHAR}
    description: Sales-region codes used by account management.
```

| Field | Type | Required | Description |
|---|---|---|---|
| `supplements` | list | No | Author-supplied tables carried verbatim; `mode: dimensional` only |
| `supplements[].name` | identifier | Yes | Output table name; unique across the export's tables |
| `supplements[].columns` | ordered map name → type text | Yes | The table's columns and their types, from the supplement type vocabulary. Never inferred |
| `supplements[].file` | path | One of `file` / `rows` | UTF-8 CSV with a header row, resolved against the config file's directory |
| `supplements[].rows` | list of maps | One of `file` / `rows` | Inline rows; each map's keys are exactly `columns`' names |
| `supplements[].description` | string | No | Table prose, rendered like any table's |
| `supplements[].descriptions` | map column → string | No | Column prose, keys ⊆ `columns` |

## Interface Contracts

### Config Models

```python
class SupplementDecl(StrictBaseModel):
    """One author-supplied table carried verbatim into a dimensional export.

    Exactly one of `file` / `rows` supplies the data. `columns` is the
    table's ordered schema and is never inferred: a CSV header must match
    it exactly, an inline row must carry exactly its keys. Forge casts the
    cells to the declared types and asserts nothing else about them.
    `description` / `descriptions` are presentation surfaces excluded from
    the incremental fingerprint; `file` is excluded too, its bytes' SHA-256
    standing in for it; every other field is data-affecting.
    """

    name: str
    """Output table name — a SQL identifier, unique across the export."""
    columns: dict[str, str]
    """Ordered column name -> type text from the supplement type vocabulary
    (`supplement_types_known`). Non-empty; names are SQL identifiers. Cells
    are cast by the session's VARCHAR cast, whose leniency is the contract:
    a fractional text rounds into an integer column (`1.5` -> 2), a DECIMAL
    rounds to its scale, boolean spellings are DuckDB's."""
    file: str | None = None
    """CSV path, resolved against the config file's directory by the loader;
    the model never touches the filesystem."""
    rows: list[dict[str, str | int | float | None]] | None = None
    """Inline rows in output order; each row's keys equal `columns`' names.
    Every value is a YAML scalar rendered to text at load and cast to the
    column's declared type. Quote any value YAML would not read as a plain
    string: temporal and boolean scalars are refused (an unquoted date,
    time, `true`, or `yes` is resolved by YAML before this model sees it);
    numeric look-alikes (`010`, `1_000`, `12:30:00`) are not detectable and
    ship as the number YAML made of them."""
    description: str | None = None
    """Table prose; rendered and embedded like a declared table's."""
    descriptions: dict[str, str] | None = None
    """Per-column prose; keys are a subset of `columns`."""


class ExportConfig(StrictBaseModel):
    """Top-level export configuration block."""

    supplements: list[SupplementDecl] | None = None
    """Author-supplied tables carried verbatim; absent means none. Legal
    only with mode='dimensional' (`supplements_require_dimensional`)."""
    # every existing field unchanged
```

### Runtime Types

```python
@dataclass(frozen=True)
class ResolvedSupplement:
    """One supplement with its data resolved by the config's loader.

    `path` is the resolved absolute path for a file supplement (None for
    inline); `sha256` the hex digest of the file bytes (None for inline);
    `rows` the text rows — one tuple per data row in file / list order,
    one `str | None` cell per declared column in declared order (an empty
    CSV field or a YAML `null` is None; every inline scalar is rendered to
    text at load per § Supplement declaration and data). `decl` is the
    declaration verbatim. Built once per invocation; the dimensional entry
    points, the incremental driver, the shaped playback head, and the pack
    builder all consume this — the file is never read again.
    """

    decl: SupplementDecl
    path: Path | None
    sha256: str | None
    rows: tuple[tuple[str | None, ...], ...]


@dataclass(frozen=True)
class SupplementSource:
    """Manifest-facing provenance of one supplement table: the declared
    (config-relative) file string and its SHA-256, both None for inline."""

    file: str | None
    sha256: str | None


@dataclass(frozen=True)
class QuerySpec:
    """(Existing fields unchanged.) Gains:"""

    supplement: SupplementSource | None = None
    """Set iff this spec is a supplement table; forwarded to TableReport."""


@dataclass(frozen=True)
class TableReport:
    """(Existing fields unchanged.) Gains, stated explicitly at every
    report-assembly site like its siblings:"""

    supplement: SupplementSource | None
```

### Functions

```python
def load_supplements(
    config: ExportConfig,
    config_dir: Path,
) -> tuple[ResolvedSupplement, ...]:
    """Resolve every declared supplement's data beside the loaded config.

    The filesystem step the model does not take: resolves each `file`
    against `config_dir`, reads and hashes the bytes, decodes and parses
    them as CSV, checks the header against the declaration, and yields the
    text rows. Inline supplements are rendered to text rows with no file
    facts.

    Args:
        config: The validated export config.
        config_dir: The directory the config file was loaded from.

    Returns:
        One ResolvedSupplement per declaration, in declaration order; empty
        when the config declares none.

    Raises:
        SupplementFileMissing: A `file` does not exist or is not a regular
            file.
        SupplementFileInvalid: The bytes are not UTF-8, do not parse as
            CSV, or a data row's field count differs from the header's
            (names the 1-based data row).
        SupplementHeaderMismatch: The header row differs from the declared
            column names in count, order, or spelling; names the first
            differing position.
    """


def compile_supplement_specs(
    emit: Emit,
    supplements: Sequence[ResolvedSupplement],
    anchor: EffectiveAnchor | None,
    write_mode: Literal["create", "replace"],
) -> list[QuerySpec]:
    """Compile every supplement into a QuerySpec over the session.

    Each spec's SQL is the VALUES-and-cast relation over the resolved text
    rows (§ The relation; the typed empty form for zero rows) and carries
    empty provenance, the author's documentation, and a SupplementSource.
    Runs the reserved-name gate over the supplement names, the anchor rule,
    and the cell probe (§ Evaluation before any write) over every
    supplement, so a returned spec is known to materialize. Every gate is a
    pure function of the declaration, the resolved rows, and the anchor —
    no emit table is read — which is what lets the shaped playback head
    call this at open.

    Args:
        emit: The open emit whose session materializes the relation.
        supplements: The resolved supplements, in declaration order.
        anchor: The resolved effective anchor, or None — consulted by the
            TIMESTAMPTZ rule only.
        write_mode: 'create' for a full export, 'replace' for a windowed
            compile — the caller's delivery regime, never inferred.

    Returns:
        One QuerySpec per supplement, in declaration order.

    Raises:
        ExportError: A supplement name is a bookkeeping name (the shared
            reserved-name predicate, with the supplement message).
        TemporalRenderRequiresAnchor: A TIMESTAMPTZ column with anchor None;
            names the supplement and the column.
        SupplementValueInvalid: A cell does not cast to its declared type;
            names the table, column, 1-based data row, and cell text.
    """


def check_supplement_sources_not_outputs(
    supplements: Sequence[ResolvedSupplement],
    output_paths: Collection[Path],
    removed_dirs: Collection[Path],
) -> None:
    """Refuse a supplement whose resolved source file this invocation would
    overwrite or delete.

    Args:
        supplements: The resolved supplements.
        output_paths: Every file the invocation writes, resolved — the
            union of the naming functions below for the invocation's fmt
            and regime (plus `out` itself under fmt='duckdb'), never
            enumerated by hand at the call site.
        removed_dirs: Every directory the invocation removes wholesale,
            resolved — `csv_removed_dirs` under a CSV `--next` window or
            range; empty for a full export and every DuckDB invocation.

    Raises:
        SupplementSourceIsOutput: A file supplement's `path` equals an
            output path, or lies under a removed directory at any depth;
            names the supplement and the path.
    """


def csv_output_paths(
    out: Path,
    table_names: Sequence[str],
    window_label: str | None,
) -> tuple[Path, ...]:
    """(CSV writer.) Every data file a CSV write of `table_names` lands.

    The writer's existing placement convention made callable; the write step
    and the source-is-output gate both read it.

    Args:
        out: The output directory.
        table_names: The output tables, in plan order.
        window_label: The `--next` window's label, or None for a full export
            or an explicit range (whose drop is `out` itself).

    Returns:
        `<out>/<table>.csv` per table, or `<out>/<window_label>/<table>.csv`
        under `--next`, in `table_names` order.
    """


def csv_removed_dirs(out: Path, window: Window) -> tuple[Path, ...]:
    """(Incremental driver.) The directories a CSV windowed invocation
    removes wholesale — the driver's existing names made callable, read by
    the write step and the source-is-output gate.

    Under `--next` (window.index is not None): the window drop, deleted and
    re-created from staging on every emitting window, and the staging
    directory, whose leftover is discarded at the next staging. Under a
    range (window.index is None): the sibling staging directory alone,
    whose leftover is discarded before staging — the range's drop is `out`,
    refused when pre-existing, so it is never removed.

    Args:
        out: The drop parent directory (`--next`) or the range's drop
            (`--from/--to`).
        window: The window being exported; its `index` distinguishes the
            two regimes, its `label` names the directories.

    Returns:
        `(<out>/<label>, <out>/.tmp_<label>)` under `--next`;
        `(<out parent>/.tmp_<label>,)` under a range.
    """


def companion_artifact_paths(
    target: Path,
    mode: str,
    fmt: Literal["csv", "duckdb"],
) -> tuple[Path, Path]:
    """(Companion module — its existing private naming function, exposed.)
    The README + manifest pair's paths under the placement rule — inside
    the output directory for CSV (the root, never a window drop), siblings
    of the database file for DuckDB — under the existing prefix convention
    (`<mode>` for CSV, `<db-stem>-<mode>` for DuckDB), which is why the mode
    literal is an input.

    Args:
        target: The output directory (csv) or `.duckdb` file path (duckdb).
        mode: The export config's mode literal.
        fmt: Output format.

    Returns:
        `(readme_path, manifest_path)`.
    """


def csv_cursor_path(out: Path) -> Path:
    """(Incremental cursor module.) The `--next` CSV cursor file's path,
    `<out>/.fabulexa-forge-cursor.json` — the cursor writer's own naming,
    read by the gate under `--next` CSV only.

    Args:
        out: The drop parent directory.

    Returns:
        The cursor file path.
    """


def export_dimensional(
    emit: Emit,
    config: ExportConfig,
    out: Path,
    fmt: Literal["csv", "duckdb"],
    anchor: EffectiveAnchor | None,
    notice_sink: NoticeSink,
    overlay: ReadmeOverlay | None,
    supplements: Sequence[ResolvedSupplement],
) -> ExportReport:
    """(Existing contract.) Gains `supplements`, the loader-resolved set —
    empty when the config declares none. After the plan compile and before
    any write: compiles the supplement specs, runs the source-is-output
    gate over the naming functions' union (files; the removed-directory set
    is empty here and is `csv_removed_dirs` under a CSV window or range),
    then validates the overlay's `table:` slots against the union of plan
    and supplement names.
    Supplement specs are appended after the declared tables in declaration
    order and flow through the same write dispatch and companion write.

    The incremental driver's windowed entry (`export_window`) and its
    `--next` entry take the same parameter with the same placement; the
    windowed compile passes write_mode='replace'.

    Raises:
        (Existing.) Plus every compile_supplement_specs error and
        SupplementSourceIsOutput, all before the first write.
    """


def compute_fingerprint(
    config: ExportConfig,
    anchor: EffectiveAnchor | None,
    sidecar_sha256: str,
    fork_path: str,
    fmt: Literal["csv", "duckdb"],
    package_version: str,
    supplements: Sequence[ResolvedSupplement],
) -> str:
    """(Existing contract.) Gains `supplements`, serialized under the key
    `supplements` in the canonical document as a map from supplement name
    to `{"columns": [[name, type], ...], "sha256": hex-or-null}` — the
    column list as an ordered list of pairs, since the canonical form's
    sorted keys would erase a reordering of the declared map; `type` in
    its canonical spelling. The canonical config dump additionally excludes
    every `supplements[].description` / `descriptions` / `file` (the sha256
    in the map above is the file's identity; its path is not) and still
    carries `columns` as declared, so a re-spelled type trips the
    fingerprint through the dump.

    Args:
        (Existing.) Plus supplements: the resolved set, declaration order;
            empty when none are declared.
    """


def open_shaped_playback(
    emit: Emit,
    config: ExportConfig,
    anchor: EffectiveAnchor | None,
    notice_sink: NoticeSink,
    supplements: Sequence[ResolvedSupplement],
) -> ShapedPlayback:
    """(Existing contract.) Gains `supplements`: the resolved supplement set
    bound beside the shape — empty for a source shape (the config cannot
    declare any). Supplement tables join a dimensional shape's table set
    (`tables()` reports each with class `snapshot`; horizon-invariant;
    identical at every `state(T)`) and its per-ask `tables` selection
    domain; a `window()` / `state()` selection naming no dimensional table
    opens no horizon, runs no dimensional compile, and emits no plan
    notices. Open runs every supplement gate — the reserved-name gate, the
    anchor rule, and the cell probe — through compile_supplement_specs; each
    is a pure function of the declaration, the resolved rows, and the
    anchor, so open reads no emit data and no supplement ask ever refuses.

    Raises:
        (Existing.) Plus the reserved-name ExportError,
        TemporalRenderRequiresAnchor, and SupplementValueInvalid, all at
        open.
    """
```

`write_companion_artifacts` and `build_manifest_document` keep their
signatures: each table's supplement provenance arrives on the report, and the
embedded config carries `supplements` through the existing config dump.

### Error hierarchy

`SupplementFileMissing`, `SupplementFileInvalid`, `SupplementHeaderMismatch`
are `ConfigError`s (raised by the loader step before any emit is open); the
name, type-vocabulary, and shape rules are Pydantic validators and surface
as the loader's `ConfigError`. `SupplementValueInvalid` and
`SupplementSourceIsOutput` are `ExportError`s (plan-time / pre-write), and
the anchor rule raises the temporal elections' existing
`TemporalRenderRequiresAnchor`, so the CLI's existing
`(ReaderError, ExporterError)` funnel renders every one as exit 1 with no
data written.

## Validation Rules

### Parse-Time (Pydantic)

```python
@model_validator(mode="after")
def supplements_require_dimensional(self) -> Self:
    """(On ExportConfig.) A present `supplements` list requires
    mode='dimensional'."""


@model_validator(mode="after")
def supplements_names_unique(self) -> Self:
    """(On ExportConfig.) A present `supplements` list is non-empty; no two
    supplements share a `name`; no supplement `name` equals a declared
    dimensional table's `name` (message: "supplement '{name}' collides with
    declared table '{name}'")."""


@model_validator(mode="after")
def supplement_types_known(self) -> Self:
    """Every `columns` type text is one of the supplement type vocabulary
    (§ Supplement declaration and data) — case-insensitive, surrounding
    whitespace ignored, `DECIMAL(p, s)` under the render elections' bounds.
    Message: "supplement '{name}': column '{col}' declares type '{type}';
    supplement types are BIGINT, INTEGER, SMALLINT, TINYINT, DOUBLE, FLOAT,
    BOOLEAN, VARCHAR, TIMESTAMP, DATE, TIME, TIMESTAMPTZ, DECIMAL(p, s)"."""


@model_validator(mode="after")
def supplement_name_is_sql_identifier(self) -> Self:
    """`name` matches ^[A-Za-z_][A-Za-z0-9_]*$."""


@model_validator(mode="after")
def supplement_columns_well_formed(self) -> Self:
    """`columns` is non-empty; every name is a SQL identifier; every type
    text is non-blank."""


@model_validator(mode="after")
def supplement_source_exactly_one(self) -> Self:
    """Exactly one of `file` / `rows` is present; a present `file` is
    non-blank."""


@model_validator(mode="after")
def supplement_rows_shape(self) -> Self:
    """Every inline row's key set equals `columns`' key set."""


@field_validator("rows", mode="before")
def supplement_rows_scalars(cls, value: object) -> object:
    """Every inline cell is a YAML scalar of the declared union. A `date`,
    `datetime`, or `time` instance — what the YAML loader makes of an
    unquoted temporal value — is refused with "supplement rows: quote
    temporal values (row {i}, column '{col}')"; a `bool` instance — an
    unquoted `true` / `false` / `yes` / `no` / `on` / `off` — with
    "supplement rows: quote boolean values (row {i}, column '{col}')".
    Both run before Pydantic's union check so the author sees the rule,
    not a union-failure text. (`bool` is checked before `int`: it is an
    `int` subclass.)"""


@model_validator(mode="after")
def supplement_documentation_well_formed(self) -> Self:
    """A present `description` is non-blank; `descriptions` keys are
    declared columns and values are non-blank."""
```

### Business Rules

| Rule | Checks | Error Message |
|---|---|---|
| `SupplementFileMissing` | The resolved `file` exists and is a regular file | `"supplement '{name}': file not found: {path}"` |
| `SupplementFileInvalid` | The file decodes as UTF-8 (`utf-8-sig`, BOM stripped), parses as CSV under the strict dialect, and every data row has the header's field count | `"supplement '{name}': file is not UTF-8: {path}"` / `"supplement '{name}': file is not valid CSV: {path}: {reason}"` / `"supplement '{name}': data row {n} has {k} fields, header has {h}"` |
| `SupplementHeaderMismatch` | Header equals declared column names in order | `"supplement '{name}': CSV header differs from 'columns' at position {i}: header '{h}', declared '{d}'"` |
| `supplements_names_unique` (validator) | No supplement name equals another supplement's or a declared table's name | `"supplement '{name}' collides with declared table '{name}'"` |
| Reserved-name gate (shared predicate, supplement message) | No supplement name is `_export_meta` / `_export_windows` | `"supplement '{name}' collides with a reserved bookkeeping table name"` (an `ExportError`) |
| `supplement_types_known` (validator) | Every declared type text is in the supplement type vocabulary | `"supplement '{name}': column '{col}' declares type '{type}'; supplement types are …"` |
| `TemporalRenderRequiresAnchor` | A `TIMESTAMPTZ` column has a resolved anchor — at compile, before any write | `"supplement '{name}': column '{col}' is TIMESTAMPTZ and requires a resolved anchor; supply rebase.base_date/timezone or rely on the sidecar runtime anchor"` |
| `SupplementValueInvalid` | Every non-NULL cell `TRY_CAST`s to its declared type — probed at compile, before any write | `"supplement '{name}': column '{col}', data row {n}: value {v!r} is not a {type}"` |
| `SupplementSourceIsOutput` | No file supplement's resolved path is a file the invocation writes or lies under a directory it removes (`--next` CSV's window drop and staging directory; a CSV range's sibling staging directory) | `"supplement '{name}': source file {path} is an output of this export; move the source or change the output target"` |
| `supplement_rows_scalars` (validator) | No inline cell is a `date` / `datetime` / `time` instance, and none is a `bool` | `"supplement rows: quote temporal values (row {i}, column '{col}')"` / `"supplement rows: quote boolean values (row {i}, column '{col}')"` |
| Fingerprint exclusion (test) | Changing only a supplement `description` or `descriptions`, or moving / renaming a file whose bytes are unchanged, never raises `IncrementalFingerprintMismatch`; changing a supplement file's bytes, `rows`, or `columns` — a type, a name, or the order — always does | test-guarded |
| Pack builder — config loads | Every packed config loads through the loader its top-level shape names | the loader's own diagnostic, prefixed `"dataset '{name}': config '{cfg}': "`; a document with neither `mode` nor `streams`: `"dataset '{name}': config '{cfg}' is neither an export nor a stream config"` |
| Pack builder — supplement files | Every packed export config's file supplements exist under the dataset directory and do not escape it | `"dataset '{name}': config '{cfg}' references supplement file '{path}', which is {missing \| outside the dataset directory}"` |

## Boundaries

- **Dimensional only.** `supplements` under `mode: source` or `mode: base` is
  a parse-time refusal. Source's seed tables are the same mechanism, a
  separable follow-up; base is the emit's own shape and takes no non-emit
  data.
- **Pinned type vocabulary.** A supplement column's type is one of the
  canonical families `compare` classifies whose every value the writers
  serialize under a specified form; `INTERVAL` (the pinned CSV form drops
  calendar components an author's value may carry) and `BLOB` (no pinned
  CSV form), like nested, enum, and other session types, are not declarable.
  Widening the vocabulary is a change to the writers' pinned forms first.
- **The cast is DuckDB's.** Forge neither tightens nor loosens the session's
  `VARCHAR` → `<type>` cast; its rounding (decimal scale, fractional text
  into an integer column) and lenient spellings are the contract, and a
  stricter reading of a cell is out of this design.
- **Sized for hand-maintained tables.** The relation is one `VALUES` literal
  over the text rows; no row bound is enforced, and a supplement large enough
  to strain the SQL parser is outside the intended scale. Should that scale
  ever be wanted, the change is to the relation form (a session-registered
  relation in place of the literal), not to the declaration or the gates.
- **No `references`.** Forge validates nothing about a supplement's values
  beyond their types. An existence check (`account_id` names an account) is
  an additive later field on the same declaration.
- **No per-row timing.** A supplement is present whole from the first
  delivery. Rows that appear at their own instant are the second shape on
  the spectrum (§ Solution) — a later, additive per-row availability column.
- **No emit-shaped data.** A supplement never enters the bundle, carries no
  structural columns, and is not conformance-checked; it is not a kind and
  no mode sees it as one. Emit-shaped augmentation is a bundle augmenter
  beside the corrupters — a separate design.
- **The README is untouched.** A supplement renders as any table under the
  existing ordering contract; the consumer-voice rewrite and the required
  `title` are a separate companion-artifacts design.
- **Streaming has no supplements.** `StreamConfig` is untouched.
