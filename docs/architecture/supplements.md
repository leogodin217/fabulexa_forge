# Supplementary Tables

A **supplement** is an author-supplied table carried verbatim into a dimensional
export — the manually-entered-warehouse-data case. A real warehouse holds
hand-maintained tables no operational system generated (region codes, negotiated
per-account price ladders, product categories), present whole, ahead of and
regardless of the operational rows they name. The `supplements` block on
`ExportConfig`, honoured by `mode: dimensional`, declares such a table by name, a
typed column list, its data (a CSV file beside the config or inline rows), and
prose. Forge casts the cells to the declared types and interprets nothing else: it
asserts no key, no reference, no timing for them. Each supplement compiles to one
`QuerySpec` whose relation reads the declared data with the declared types in the
same session every other relation materializes in, so it flows through the write
dispatch, the writers' pinned text forms, `TableReport`, the manifest, the README,
incremental delivery, and shaped playback with no parallel machinery. This is the
surface `CLAUDE.md` Principle #3 names as an exporter output value's second
admitted source — sourced from the author instead of the emit, recorded in the
manifest — and Principle #4's no-integrity-claim clause; it is the feature those
clauses describe, not an exception to them.

**Source:** [`exporters/supplements.py`](../../src/fabulexa_forge/exporters/supplements.py)
(`ResolvedSupplement`, `SupplementSource`, `load_supplements`,
`compile_supplement_specs`, `check_supplement_sources_not_outputs`);
[`SupplementDecl`](../../src/fabulexa_forge/config/models.py) and
`ExportConfig.supplements` in the config models; the error classes in
[`errors.py`](../../src/fabulexa_forge/errors.py). Tests:
[`tests/config/test_supplements.py`](../../tests/config/test_supplements.py)
(parse rules),
[`tests/exporters/test_supplements_load.py`](../../tests/exporters/test_supplements_load.py)
(the loader),
[`tests/exporters/test_supplements_compile.py`](../../tests/exporters/test_supplements_compile.py)
(the compile and its gates),
[`tests/exporters/dimensional/test_export_supplements.py`](../../tests/exporters/dimensional/test_export_supplements.py)
(full export, manifest, README),
[`tests/incremental/test_supplements.py`](../../tests/incremental/test_supplements.py)
(delivery, fingerprint, the source-is-output gate),
[`tests/playback/test_shaped_supplements.py`](../../tests/playback/test_shaped_supplements.py),
and [`tests/datasets/test_pack_builder.py`](../../tests/datasets/test_pack_builder.py).
Recipe: [`supplement-tables`](../../examples/recipes/supplement-tables/config.yaml).

## Boundary

- **In.** The validated `ExportConfig`'s `supplements` declarations; the directory
  the config file was loaded from (the resolution base for `file`, exactly as for
  `readme_overlay`); the author's CSV bytes, read **once**, by the loader; the
  resolved `EffectiveAnchor | None` (consulted by the `TIMESTAMPTZ` rule only);
  and the open `Emit`'s session, which materializes the relation and probes
  the cells — never reads an emit table.
- **Out.** The loader yields one frozen `ResolvedSupplement` per declaration in
  declaration order — the declaration verbatim, the resolved absolute path and
  the bytes' SHA-256 (both `None` for inline), and the text rows. The compile
  yields one `QuerySpec` per supplement carrying a `SupplementSource` (the
  declared file string and the SHA-256, both `None` for inline) that
  `TableReport` forwards to the manifest.
- **Two steps at two layers.** `load_supplements` is the filesystem step the
  model never takes: whoever loads the config (the CLI, the pack builder) calls
  it beside the overlay resolution, so the config is emit-independent and
  path-free. `compile_supplement_specs` is a pure function of the declaration,
  the resolved rows, and the anchor. Every consumer — `export_dimensional`, the
  incremental driver's windowed entries, `open_shaped_playback`, the pack
  builder — takes the resolved set as a parameter beside the overlay; nothing
  downstream reads the file again.
- **Error hierarchy.** `SupplementFileMissing`, `SupplementFileInvalid`, and
  `SupplementHeaderMismatch` are `ConfigError`s raised by the loader before any
  emit is open; the name, type-vocabulary, and shape rules are Pydantic
  validators surfacing as the loader's `ConfigError`. `SupplementValueInvalid`
  and `SupplementSourceIsOutput` are `ExportError`s raised at plan compile /
  before the first write; the anchor rule raises the temporal elections'
  `TemporalRenderRequiresAnchor`. The CLI's `(ReaderError, ExporterError)`
  funnel renders every one as exit 1 with no data written.
- **The reader boundary is untouched.** Reading an author CSV is not opening
  `run.duckdb` or parsing `base.json`; the reader-first rule is about the
  bundle. The session never opens the author's file — it sees text cells as SQL
  literals.

## Semantics

### Declaration

The declaration grammar is [`SupplementDecl`](../../src/fabulexa_forge/config/models.py):
`name`, an ordered `columns` map of name → type text, exactly one of `file` /
`rows`, and the presentation fields `description` / `descriptions`. `columns` is
the table's schema and is **never inferred** — a CSV header must match it exactly,
an inline row must carry exactly its keys. `supplements` is legal only under
`mode: dimensional` and, when present, non-empty.

### Resolution to text rows

Both data sources resolve at load to the same thing: an ordered list of **text
rows** — one `str | None` cell per declared column, in declared order — that the
compile turns into a relation.

| Condition | Result |
|---|---|
| `file` | Resolved against the config file's directory; the bytes read once, hashed (SHA-256), decoded as `utf-8-sig` (a leading byte-order mark — what a spreadsheet's "CSV UTF-8" save writes — is stripped rather than left on the first header name), and parsed as RFC 4180 CSV under Python `csv`'s default dialect with `strict=True` and `newline=''` semantics: a newline inside a quoted cell is part of that cell; an unterminated quote or a stray quote in an unquoted field is a refusal (the non-strict parser would silently read an unterminated quote to end of file). A file that does not decode or parse is `SupplementFileInvalid`; an absent file is `SupplementFileMissing` |
| CSV header | Must equal the declared column names in declared order, exactly — case-sensitive, no extras, no omissions. Otherwise `SupplementHeaderMismatch` naming the first differing position; a file with no header row (zero bytes, or a BOM alone) mismatches at position 1 |
| CSV data row | Carries exactly the header's field count; a ragged row is `SupplementFileInvalid` naming the 1-based data row |
| CSV cell → text cell | An empty field (quoted or not) is `None`; any other field is its text, verbatim. A file supplement therefore cannot carry an empty-string `VARCHAR` — the one asymmetry between the two sources |
| `rows` | Each row's keys equal the declared column set (`supplement_rows_shape`). Each value is a YAML scalar of `str \| int \| float \| None`: `null` → `None`; a string → itself (`''` is the empty string, not `None`); an integer → its decimal text; a float → its shortest round-trip text (`repr`). Rendered at load; no file, no hash |
| YAML-resolved scalars | A `date` / `datetime` / `time` object (an unquoted `2024-01-01`) and a `bool` (an unquoted `true`, or `yes` / `no` / `on` / `off`, which YAML 1.1 resolves to the same `bool` — a `VARCHAR` cell `NO` would otherwise ship as `false` with no way to tell after load) are refused at parse by `supplement_rows_scalars` with "quote temporal values" / "quote boolean values"; a `BOOLEAN` column takes the quoted text `'true'` and casts like any other cell. The remaining YAML 1.1 implicit resolutions (`12:30:00` sexagesimal → `45000`, leading-zero `010` → `8`, `1_000` → `1000`, `~` → null) are numeric and indistinguishable from an intended number, so they are the author's guard alone: **quote any value YAML would not read as a plain string** |
| Row order | Preserved as given; a supplement is written in file / list order |
| Zero data rows | Legal — a header-only CSV or `rows: []` — written as an empty typed table, reported with row count 0 |

### The type vocabulary and the cast

A declared type text is one of the **supplement type vocabulary**, checked at
parse (`supplement_types_known`): `BIGINT` / `INTEGER` / `SMALLINT` / `TINYINT`,
`DOUBLE` / `FLOAT`, `BOOLEAN`, `VARCHAR`, `TIMESTAMP`, `DATE`, `TIME`,
`TIMESTAMPTZ`, and `DECIMAL(p, s)` under the render elections' bounds
(1 ≤ p ≤ 38, 0 ≤ s ≤ p) — matched case-insensitively with surrounding whitespace
ignored, and written into the relation in that canonical spelling
(`canonical_supplement_type`, the one authority the compile and the fingerprint
both read). These are exactly the canonical families `compare` classifies whose
every value the writers serialize under a specified text form — the pinned forms
for `DATE` / `TIME` / `TIMESTAMPTZ` / `DECIMAL` ([`writers.md`](writers.md)
§ Pinned text forms), the writers' default forms for the rest — so no output
column can carry a type the manifest transcription, the pinned forms, or
`compare` was never specified for.

**The cast is DuckDB's.** A text cell becomes a typed value by `CAST(cell AS
<declared type>)`, the session's `VARCHAR` → `<type>` cast **verbatim**; a `None`
cell is `NULL`. Forge specifies neither a stricter reading nor a more lenient one,
so the cast's own leniency is part of the contract: a `DECIMAL(p, s)` cell with
more than `s` fraction digits is rounded to `s` (`12.345` → `DECIMAL(5, 2)` is
`12.35`); an integer column accepts `1.0`, `1e3`, and surrounding whitespace, and
**rounds** a fractional text rather than refusing it (`1.5` → `BIGINT` is `2` —
the one leniency that changes a value an author would read as exact, named in the
field docstring and the `supplement-typed-columns` recipe); a `BOOLEAN` column accepts DuckDB's spellings
(`true` / `false`, `t` / `f`, `yes` / `no`, `1` / `0`, case-insensitively); a
`DOUBLE` column accepts `nan` and `inf`.

**`TIMESTAMPTZ` requires a resolved anchor.** The session is zone-pinned only when
an anchor resolves ([`reader.md`](reader.md) § The session-zone pin); an unpinned
session would render the column in the host's zone, breaking the *Deterministic*
invariant. Refused at plan compile with `TemporalRenderRequiresAnchor` naming the
supplement and the column — the same rule and error the temporal elections apply
([`temporal-elections.md`](temporal-elections.md)). `TIMESTAMP`, `DATE`, and
`TIME` are zone-free and need no anchor.

### The relation and evaluation before any write

The relation is one `VALUES` list over the text rows — every cell a `VARCHAR`
literal or `NULL`, plus a leading row-position column — wrapped in a `SELECT`
that casts each declared column and orders by position, the position projected
away. A supplement with zero rows has no `VALUES` form and compiles instead to
`SELECT CAST(NULL AS <type>) AS <col>, … WHERE false` — the typed, empty relation
the writers already handle for a grain that resolved to no rows. The spec carries
`write_mode='create'` (full export) or `'replace'` (windowed — the caller's
delivery regime, never inferred), no keys, empty `provenance` and `kind_values`,
the author's `descriptions` as `author_descriptions`, the author's `description`
as `author_table_description`, `event_log=False`, and the `SupplementSource`.
Because it is a `QuerySpec`, every downstream contract — `DESCRIBE`
transcription, the pinned text forms, the report, the manifest, the README —
applies with no special case.

The session evaluates SQL lazily, so a bad cell left to the writer would surface
mid-dispatch as an `ExportRuntimeError` after earlier tables were written. The
compile therefore evaluates each supplement fully, in this order, and every
failure is an `ExportError` raised before the first table of the invocation is
written:

1. **Reserved names** — a supplement named for a bookkeeping table
   (`is_reserved_table_name`, the one predicate every mode reads, applied to the
   supplement set with its own message) is an `ExportError`.
2. **Anchor** — a `TIMESTAMPTZ` column with no resolved anchor is
   `TemporalRenderRequiresAnchor`.
3. **Cells** — per column in declared order, over the untyped text relation: the
   first row (by position) whose cell is non-`NULL` and whose `TRY_CAST(cell AS
   <type>)` is `NULL` is `SupplementValueInvalid`, naming the table, the column,
   the 1-based data row (file / list order), and the cell text. A column with no
   hit is clean.

`TRY_CAST` is the same cast the write performs, so a cell the probe passes is a
cell the write casts identically: the probe is the only place a supplement value
is diagnosed, and the writer's later materialization cannot fail on a cast the
probe passed. All three gates are pure functions of the declaration, the resolved
rows, and the anchor — they touch no emit table — so every caller runs them
before its first write and the shaped playback head runs them at open.

**No interpretation.** Forge reads a supplement's cells only to cast them. It does
not check that an `account_id` names an account, that `effective_from` precedes
`effective_to`, or that any column is unique; a ladder's dates are data, as a
declared table's own rows are. What the author typed is what the warehouse holds
— a typo ships, as it would from a hand-maintained spreadsheet.

### Output naming and the source-is-output gate

| Condition | Result |
|---|---|
| `name` | A SQL identifier (`supplement_name_is_sql_identifier`), the same rule as a declared table |
| Two supplements share a name, or a supplement's name equals a declared dimensional table's `name` | Refused at parse (`supplements_names_unique`, naming both). A dimensional output table's name is its author-declared `name`, so both lists are on the config and the collision is decidable before any emit opens |
| `name` is a bookkeeping name (`_export_meta`, `_export_windows`) | Refused at plan compile (gate 1 above) |
| CSV filename under `fmt: csv` | `<name>.csv` in the output directory (or the window drop), like any table; SQL-identifier names cannot collide with the companion artifact filenames |
| The resolved source file is a path the invocation writes, or lies under a directory it removes | `SupplementSourceIsOutput` before any write, naming the supplement and the path |

The last row exists because the natural config — `name: custom_rate_tier`,
`file: custom_rate_tier.csv`, the config's directory as the CSV output directory —
would otherwise overwrite the author's source with the re-serialized render
(different quoting, `NULL` and temporal forms). `check_supplement_sources_not_outputs`
runs two tests over the resolved source path: **equality** with every file the
invocation writes — each `<table>.csv` (under the `--next` window drop for a
windowed invocation; a range's drop is `out` itself), the `.duckdb` file (`out`
itself under `fmt: duckdb`), the two companion artifacts, and under `--next` CSV
the cursor file — and **containment**, at any depth, in every directory the
invocation removes wholesale (under `--next` CSV the window drop `<out>/<label>`
and the staging directory `<out>/.tmp_<label>`; under a CSV range the sibling
staging directory `<out parent>/.tmp_<label>`). A full export and every DuckDB
invocation remove nothing, so their containment set is empty.

Neither set is enumerated by the caller. Each site that lands a file or removes
a directory exposes its planned paths through one pure naming function — the CSV
writer's `csv_output_paths`, the incremental driver's `csv_removed_dirs`, the
companion module's `companion_artifact_paths`, the cursor module's
`csv_cursor_path` — that the write step and this gate both read, so the gate can
never disagree with the writer about where a file lands or what a window removes.
The DuckDB writer needs none: its one output is `out` itself. Naming is not
supplement-aware; it is each site's own convention made callable.

### Delivery across the consumers

A supplement is one more `QuerySpec` to every consumer; each consumer's own doc
carries its side of the contract.

| Consumer | Contract |
|---|---|
| Dimensional full export | Supplement specs are appended after the declared tables in declaration order; the overlay's `table:` slots validate against the union of plan and supplement names — [`dimensional.md`](dimensional.md) § Boundary |
| Incremental driver | Delivery class `snapshot` in every emitting window, horizon-invariant by construction; the fingerprint carries each supplement's ordered column list and SHA-256 and excludes its presentation fields and `file` string — [`incremental.md`](incremental.md) § Horizon windowing, § Drained detection and the cursor |
| Shaped playback | Supplement tables join a dimensional shape's table set and its `tables` selection domain, identical at every `T`; every gate runs at open, which still reads no emit data — [`playback.md`](playback.md) § Shaped window |
| Companion artifacts | `tables[].supplement` records the declared file string and SHA-256 (`null` fields for inline); the README renders a supplement as any table — [`companion-artifacts.md`](companion-artifacts.md) § The manifest |
| Dataset packs | A packed export config's file supplements are pack members at their config-relative path — [`datasets.md`](datasets.md) § Pack builder |

## Invariants

1. **Verbatim carriage.** A supplement's written rows are its declared data cast
   to its declared types under the session's `VARCHAR` cast — the cast's own
   reading of the text (decimal scale, integer rounding, lenient spellings
   included) is the only transformation — in its given order: no derivation,
   merge, projection, filter, or fill. Same bytes + same declaration → identical
   relation.
2. **Sourced or supplied.** Every exporter output value traces to a base-layer
   value or to a declared supplement's author-supplied data, cast to its declared
   types; the manifest names which for every table.
3. **No claim on supplied data.** Forge reads a supplement's cells only to cast
   them; it asserts no key, reference, order, or timing for them, and its own
   reshape introduces no dangling or forward reference on their account.
4. **Presentation is inert.** A well-formed `description` / `descriptions`
   changes README and manifest documentation bytes only — never data, notices,
   exit codes, or the fingerprint. (Well-formedness — non-blank, keys among the
   declared columns — is a parse rule like any field's.)
5. **Whole and immediate.** A supplement is present whole from the first
   delivery — window 0, every `state(T)` — regardless of which rows it may name
   have arrived; no horizon is taken to build it.

Relied on: plan iteration order is deterministic; the writers' Arrow
materialization is the single truth of output schema; the session is
zone-pinned whenever an anchor resolves, and the one zone-bearing declarable type
(`TIMESTAMPTZ`) requires one, so supplement temporal parsing is
machine-independent; the incremental fingerprint refuses a changed data seam.

## Validation Rules

Parse-time rules are validators on `SupplementDecl` / `ExportConfig`
([`models.py`](../../src/fabulexa_forge/config/models.py); cases in
[`tests/config/test_supplements.py`](../../tests/config/test_supplements.py)):

| Validator | Refuses |
|---|---|
| `supplements_require_dimensional` | A present `supplements` list under any mode but `dimensional` |
| `supplements_names_unique` | An empty `supplements` list; two supplements sharing a `name`; a supplement `name` equal to a declared dimensional table's `name` |
| `supplement_name_is_sql_identifier` | A `name` not matching `^[A-Za-z_][A-Za-z0-9_]*$` |
| `supplement_columns_well_formed` | An empty `columns` map; a column name that is not a SQL identifier; a blank type text |
| `supplement_types_known` | A type text outside the supplement type vocabulary; a `DECIMAL(p, s)` outside the bounds |
| `supplement_source_exactly_one` | Both or neither of `file` / `rows`; a blank `file` |
| `supplement_rows_shape` | An inline row whose key set differs from `columns`' |
| `supplement_rows_scalars` (`before` mode, so the author sees the rule rather than a union-failure text; `bool` checked before `int`, of which it is a subclass) | A `date` / `datetime` / `time` instance or a `bool` instance in an inline cell |
| `supplement_documentation_well_formed` | A blank `description`; a `descriptions` key that is not a declared column; a blank `descriptions` value |

Load-time and plan-time rules:

| Rule | When | Error |
|---|---|---|
| The resolved `file` exists and is a regular file | Load | `SupplementFileMissing` |
| The file decodes as UTF-8, parses under the strict dialect, and every data row has the header's field count | Load | `SupplementFileInvalid` |
| The header equals the declared column names in order | Load | `SupplementHeaderMismatch` |
| No supplement name is a bookkeeping table name | Plan compile | `ExportError` (the shared predicate, the supplement message) |
| A `TIMESTAMPTZ` column has a resolved anchor | Plan compile | `TemporalRenderRequiresAnchor` |
| Every non-`NULL` cell `TRY_CAST`s to its declared type | Plan compile, before any write | `SupplementValueInvalid` |
| No file supplement's resolved path is an output file or lies under a removed directory | Before any write | `SupplementSourceIsOutput` |
| Only a supplement's data-affecting fields move the incremental fingerprint | Test-guarded — [`tests/incremental/test_supplements.py`](../../tests/incremental/test_supplements.py) | `IncrementalFingerprintMismatch` on a changed file byte, inline row, column name, column order, or type; never on a changed description or a moved / renamed file with identical bytes |
| Every packed export config's file supplements exist under the dataset directory and do not escape it | Pack build | `PackBuildError` naming the config and the path |

Messages: [`supplements.py`](../../src/fabulexa_forge/exporters/supplements.py)
and the validators.

## Rationale

- **Verbatim, uninterpreted.** A hand-maintained table is the author's data;
  the value forge adds is carrying it *into* the export — the README and manifest
  describe a complete dataset, `compare`'s expected side needs no hand assembly,
  the dataset pack carries it — not vetting it. Any check beyond the cast
  (existence of a referenced id, date ordering, uniqueness) would be forge
  asserting a claim it has no ground for; the no-claim invariant keeps
  Principle #4's guarantees honest by scoping them to forge's own reshape.
- **The cast is DuckDB's, and the probe is the cast.** One reading of a cell,
  owned by the session, means a cell the probe accepts is a cell the write
  accepts; a forge-side stricter parser would be a second reading that could
  disagree with the first. The leniencies are documented rather than removed
  because removing them is exactly that second reading.
- **Evaluate before any write.** The writers materialize lazily; without the
  probe a bad cell fails mid-dispatch with earlier tables already on disk. The
  probe turns the failure into a pre-write `ExportError`, which is what lets the
  CLI promise "exit 1, nothing written" and lets the shaped head promise "a shape
  that opens has no supplement ask that refuses".
- **The loader reads the file, once.** The model never touches the filesystem
  (the config is emit-independent, the `readme_overlay` precedent), and the
  resolved set is built once per invocation and passed as a parameter so the
  pack builder, the playback head, and the windowed driver all consume the same
  rows the full export would — a file changed between two reads within one
  invocation cannot split the output.
- **Text rows, not typed rows, at the seam.** Resolving both sources to
  `str | None` cells makes a CSV field and an inline scalar the same thing by
  the time the compile sees them, so one relation form, one probe, and one
  fingerprint entry serve both. The two irreducible asymmetries — a CSV cannot
  carry an empty-string `VARCHAR`, and YAML resolves unquoted temporals and
  booleans before the model sees them — are stated rather than papered over,
  because papering over the second would mean guessing what `NO` meant.
- **The SHA-256 is the data's identity; the path is where it was found.** The
  fingerprint carries the hash and excludes the `file` string so a moved or
  renamed file with identical bytes continues a drip while a one-byte edit halts
  it. Columns enter the fingerprint as an ordered list of pairs because the
  canonical form's sorted keys would erase a reordering that reorders the output
  table; the config dump still carries `columns` as declared, so a re-spelled
  type (`bigint` → `BIGINT`) trips the fingerprint through the dump —
  deterministic, and not worth a carve-out.
- **Dimensional only.** Base is the emit's own shape — non-emit data there is the
  bundle-augmenter case. Source's app-database seed tables are the same
  mechanism and a separable extension of this declaration. Streaming's
  `StreamConfig` is a separate envelope with no table set to join.
- **Name collision at parse, not plan.** A dimensional output table's name is
  its author-declared `name`, so the supplement list and the table list are both
  on the config; refusing the collision before any emit opens is the earliest
  honest moment. The reserved-name gate runs at plan compile because that is
  where every mode applies the shared predicate.
- **A `VALUES` literal.** Sized for hand-maintained tables: the relation is one
  SQL literal over the text rows, built once per invocation. No row bound is
  enforced; a supplement large enough to strain the SQL parser is outside the
  intended scale, and should that scale be wanted the change is to the relation
  form (a session-registered relation in place of the literal), not to the
  declaration or the gates.
- **Guarding the author's source.** Re-serializing over the source file is the
  natural config's default outcome, so the gate is always-on and reads the same
  naming functions the writers do — an enumerated path list at the call site
  would drift from the writer the first time a placement convention moved.
- **`utf-8-sig` and the strict dialect.** A spreadsheet's BOM would otherwise
  fail the header check with a header that *looks* identical to the declaration;
  the non-strict CSV parser would silently swallow an unterminated quote to end
  of file. Both are the loud-over-quiet default.
- **Three shapes of non-emit data; this is the first.** *Manually entered
  warehouse data* — present whole, immediately, in the warehouse's own shape —
  is this surface. *Manually entered data with its own emit times* (rows that
  appear at a declared instant) is an additive per-row availability column on
  the same declaration. *Data the upstream system cannot reasonably provide* —
  emit-shaped, with real structural columns, run through C1–C15, visible to
  every mode — is a bundle augmenter beside the corrupters, writing into
  `run.duckdb` + `base.json`, and a separate design.

## Boundaries

- **Dimensional only.** `supplements` under `mode: source` or `mode: base` is a
  parse-time refusal; `StreamConfig` has no `supplements`.
- **Pinned type vocabulary.** `INTERVAL` (the pinned CSV form covers only the
  pure-microsecond durations forge itself derives and drops the calendar
  components an author's `1 day` or `2 months` carries) and `BLOB` (no pinned
  CSV form), like nested, enum, and other session types, are not declarable.
  Widening the vocabulary is a change to the writers' pinned forms first.
- **The cast is DuckDB's.** Forge neither tightens nor loosens the session's
  `VARCHAR` → `<type>` cast; a stricter reading of a cell is not owned here.
- **No `references`.** Forge validates nothing about a supplement's values beyond
  their types. An existence check (`account_id` names an account) would be an
  additive field on the same declaration.
- **No per-row timing.** A supplement is present whole from the first delivery;
  rows that appear at their own instant are the second shape of the spectrum
  (§ Rationale), not this surface.
- **No emit-shaped data.** A supplement never enters the bundle, carries no
  structural columns, is not conformance-checked, and is not a kind: no mode
  sees it as one, so key election, the fk pathfind, `lookup`, and the per-column
  value elections (`render:`, `derived:`) do not reach it — its types are
  declared directly, there is nothing to elect. Corrupters act on the bundle,
  which has no supplements.
- **No inheritance.** A supplement column has no source property, so the
  documentation channel's inheritance rule gives it nothing; its documentation
  is the author's `descriptions` alone, under the same "undocumented renders
  nothing" posture as any column.
- **`init` proposes none.** Nothing in a sidecar implies a supplement.
- **The writers, `compare`, and the README are not supplement-aware.** A writer
  serializes whatever relation it is handed; `compare` sees an ordinary table on
  both sides; the README renders a supplement as any table under its ordering
  contract, and a `table:` overlay slot may name one.

## Related

| Document | Why |
|---|---|
| [`dimensional.md`](dimensional.md) | The one mode that honours `supplements` — where the specs are appended and the overlay slots validated |
| [`incremental.md`](incremental.md) | `snapshot` delivery every window, the fingerprint's `supplements` map, and `csv_removed_dirs` |
| [`playback.md`](playback.md) | Supplement tables in a dimensional shape's table set and selection domain; every gate at open |
| [`companion-artifacts.md`](companion-artifacts.md) | The manifest's `tables[].supplement` provenance and the README's rendering |
| [`datasets.md`](datasets.md) | File supplements as pack members; the builder's loader gate |
| [`writers.md`](writers.md) | The pinned text forms the type vocabulary is bounded by; `csv_output_paths` |
| [`temporal-elections.md`](temporal-elections.md) | The `TemporalRenderRequiresAnchor` rule `TIMESTAMPTZ` columns share |
| [`documentation-channel.md`](documentation-channel.md) | The author-description surfaces a supplement's `description` / `descriptions` join |
| [`compare.md`](compare.md) | The canonical families the type vocabulary is drawn from |
| [`../../CLAUDE.md`](../../CLAUDE.md) | Principle #3's two admitted sources and Principle #4's no-claim clause |
