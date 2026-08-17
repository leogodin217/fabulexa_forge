# Sprint: dataset-equivalence

## Purpose

Ship the compare surface from `docs/architecture/pending/dataset-equivalence.md`:
`compare_datasets()` plus a `fabulexa-forge compare` CLI verb that decides — as a
boolean verdict with a deterministic, bounded discrepancy report — whether an
external dataset is exactly the relation forge renders. The first consumer is
deterministic grading: a learning environment compares a learner's extracted
warehouse against forge's own dimensional render of the same emit.

The design doc is the semantic authority (canonical families, encoding rules,
matching semantics, rationale). This spec carries the contracts, phases, and test
cases; it does not restate the doc's WHY.

## Scope

**Capabilities touched:**
- Compare subsystem (new): canonical family classification + canonical value
  encoding, input handling (DuckDB expected side; DuckDB-or-CSV actual side, UTC
  session-zone pin, pinned cast authority), table/column/row multiset comparison,
  `ComparisonResult` report types, determinism
- CLI: new `compare` verb beside `validate` / `export` / `init` / `stream` /
  `mixer` / `corrupt`, with text/JSON rendering and the 0/1/2 exit-code contract

**Not included:**
- Rewiring the existing internal agreement tests (incremental-vs-full,
  playback-vs-full) onto the new surface — explicit follow-up work per the doc
- Any scoring / tolerance / feedback layer (permanently out of scope)
- Recipes (compare has no YAML config surface)
- Folding the pending doc into canonical architecture (`/fold-pending`, post-sprint)

## Success Criteria

- [ ] `compare_datasets(expected, actual)` returns a `ComparisonResult` for any
      valid input pair; malformed inputs raise `CompareInputError` — never a crash
      from a bad value (a cast failure is a value discrepancy)
- [ ] Two renders of the same relation compare equal across row-order, column-order,
      lossless-type-width, and value-rendering drift; any value/schema/row
      difference within the universe yields `equal == False` with the discrepancy
      reported
- [ ] Canonical encoding is byte-identical to the C6 codec's encode half
      (`to_csv_text`) for BIGINT / DOUBLE / BOOLEAN / VARCHAR — asserted by test,
      not by import
- [ ] Same two inputs → byte-identical `ComparisonResult` and byte-identical JSON
      rendering (determinism)
- [ ] `fabulexa-forge compare` exits 0 equal · 1 not equal · 2 input error; report
      on stdout, input errors on stderr
- [ ] `make check` green (lint + typecheck + tests)

## Design Notes (implementation-facing, from the doc)

- **Module home:** `src/fabulexa_forge/compare/` — a new top-level subsystem. It
  never opens an emit; reader-first does not apply. It opens its inputs through
  its **own in-memory DuckDB session**, zone-pinned to UTC (`SET TimeZone`)
  **before either input is read**.
- **Encoding is Python-side** on materialized values — never SQL `CAST(... AS
  VARCHAR)`. Typing casts of actual-side CSV cells are the one SQL-side step
  (`TRY_CAST` toward the expected family's reference type), with two bespoke
  parses: blob (hex-decode) and interval (writer-form parse first, then
  `TRY_CAST`).
- **Intervals must be materialized so calendar components are observable** (the
  Arrow path's month/day/nanosecond triple, as `writers/csv.py::_format_interval`
  does) — a `datetime.timedelta` fetch would silently destroy the months-carrying
  case the encoding must route to DuckDB text rendering.
- **Errors:** `CompareInputError` is a fresh top-level exception in
  `compare/errors.py` (the `reader/errors.py` / `playback/errors.py`
  one-hierarchy-per-domain pattern). It does not touch the export hierarchy in
  `src/fabulexa_forge/errors.py`.
- **Contract defaults:** `tables=None` ("compare everything" is spelled `None`)
  and `max_row_diffs=10` are pinned by the design doc's published contract —
  surface-definitional, not invented mapping values; the CLI mirrors them.
- **No notices:** compare takes no `notice_sink`; its whole informational output
  is the `ComparisonResult`.

## Contracts

Extracted verbatim from the design doc § Interface Contracts, plus the one
internal seam (the canonical-form module) phases 2–3 build on.

### Canonical-form seam (`compare/canonical.py`)

```python
CanonicalFamily = Literal[
    "integer", "float", "boolean", "text",
    "timestamp", "date", "time", "timestamptz", "interval", "blob",
]


def family_of(duckdb_type: str) -> CanonicalFamily | None:
    """
    Classify a DuckDB type name into its canonical family.

    Implements the doc's family table: any integer type -> integer;
    DOUBLE/FLOAT -> float; BOOLEAN -> boolean; VARCHAR -> text; TIMESTAMP at
    any precision -> timestamp; DATE -> date; TIME at any precision -> time;
    TIMESTAMPTZ at any precision -> timestamptz; INTERVAL -> interval;
    BLOB -> blob.

    Args:
        duckdb_type: A DuckDB type name as the catalog reports it.

    Returns:
        The canonical family, or None for a type outside every family
        (DECIMAL deliberately among them — the caller decides whether that
        is an error, per the comparison-universe scope rule).
    """


def encode_value(value: object, family: CanonicalFamily) -> str | None:
    """
    Encode one materialized value to its canonical text form.

    Implements the doc's encoding table: str(int) / repr(float) /
    "true"/"false" / identity text / microsecond-precision temporal forms
    (timestamptz normalized to UTC `+00:00`; naive timestamp as stored) /
    the interval `[-]H:MM:SS.ffffff` form with the 24h day-fold and the
    DuckDB-text fallback for month-carrying values / lowercase hex for blob.
    Byte-identical to the C6 codec's `to_csv_text` for the four families it
    covers (integer, float, boolean, text) — asserted by test, never imported.

    Args:
        value: The materialized cell value. NULL arrives as None. Interval
            values arrive as the Arrow month/day/nanosecond triple so
            calendar components are observable.
        family: The expected column's canonical family, directing the encoding.

    Returns:
        Canonical text, or None for a NULL input (None is carried through the
        encoded tuple, distinct by construction from every encoded string).
    """
```

### Runtime Types (`compare/report.py`)

```python
@dataclass(frozen=True)
class SchemaDiscrepancy:
    """One table- or column-level difference between the two sides."""

    kind: Literal[
        "table-missing", "table-extra",
        "column-missing", "column-extra", "column-incompatible",
    ]
    table: str
    column: str | None          # None for table-level kinds
    expected_type: str | None   # DuckDB type name; None where inapplicable
    actual_type: str | None
```

```python
@dataclass(frozen=True)
class RowDiscrepancies:
    """The multiset difference for one compared table, canonically ordered."""

    columns: tuple[str, ...]                       # compared-column set, expected-side catalog order
    missing: tuple[tuple[str | None, ...], ...]    # encoded tuples in expected, absent/short in actual;
    extra: tuple[tuple[str | None, ...], ...]      #   one entry per occurrence, truncated to max_row_diffs
    missing_total: int                             # untruncated occurrence counts
    extra_total: int
```

```python
@dataclass(frozen=True)
class TableComparison:
    """The full comparison outcome for one table name, drawn from the union
    of the expected- and actual-side table sets. A table-extra entry has no
    expected-side counterpart; a table-missing entry has no actual-side one."""

    table: str
    schema: tuple[SchemaDiscrepancy, ...]
    expected_rows: int | None    # None when the table is absent from the expected side
    actual_rows: int | None      # None when the table is absent from the actual side
    rows: RowDiscrepancies | None  # None when row comparison did not run (table absent from either side)
```

```python
@dataclass(frozen=True)
class ComparisonResult:
    """The verdict and report for one dataset comparison.

    equal is True iff every TableComparison carries zero schema
    discrepancies and zero row discrepancies (missing_total == extra_total == 0).
    """

    equal: bool
    tables: tuple[TableComparison, ...]   # ordered by table name
```

### Errors (`compare/errors.py`)

```python
class CompareInputError(Exception):
    """Malformed compare input: an unreadable expected/actual path, a CSV file
    without a header row, an unknown or empty `tables` selection, an
    expected-side column type outside the canonical families (within the
    comparison universe), or an invalid `max_row_diffs`.

    A fresh top-level exception — the compare surface is its own failure
    domain, coupled to neither the export pipeline (`ExporterError`) nor the
    reader (`ReaderError`), matching the package's one-hierarchy-per-domain
    convention. The CLI's `compare` command catches it, renders the message
    to stderr, and exits 2.
    """
```

### Functions

```python
def compare_datasets(
    expected: Path,
    actual: Path,
    *,
    tables: Sequence[str] | None = None,
    max_row_diffs: int = 10,
) -> ComparisonResult:
    """
    Compare two materialized datasets for exact equality under the canonical form.

    Args:
        expected: Path to a DuckDB file — the authoritative side (a forge
            render). Defines the table universe, column sets, and types.
        actual: Path to a DuckDB file, or to a directory of <table>.csv files
            with header rows, claiming to be the same reshape.
        tables: Optional narrowing of the comparison universe to exactly
            these tables, on both sides — expected- and actual-side tables
            outside the selection are ignored entirely (an unselected
            actual-side table is not table-extra). None compares every
            expected-side table against the full actual side; an empty
            selection is refused.
        max_row_diffs: Per-table, per-direction cap on *listed* row
            discrepancies. Bounds the report only; totals and the verdict are
            computed over full tables.

    Returns:
        A ComparisonResult; deterministic for identical inputs.

    Raises:
        CompareInputError: expected is not a readable DuckDB file; actual is
            neither a DuckDB file nor a CSV directory; a CSV file lacks a
            header; a `tables` entry names no expected-side table; `tables`
            is empty; an expected-side column's type within the comparison
            universe is outside the canonical families; max_row_diffs < 0.
    """
```

```python
def render_comparison_text(result: ComparisonResult) -> str:
    """
    Render a ComparisonResult as the CLI's human-readable report.

    Args:
        result: The comparison to render.

    Returns:
        A deterministic multi-line report: one verdict line, then one block
        per table carrying discrepancies (equal tables render one line each).
    """
```

```python
def render_comparison_json(result: ComparisonResult) -> str:
    """
    Render a ComparisonResult as deterministic JSON for machine consumers.

    Args:
        result: The comparison to render.

    Returns:
        A JSON document mirroring the ComparisonResult shape byte-stably
        (sorted keys, fixed separators) — the grading consumer's wire format.
    """
```

### CLI

```
fabulexa-forge compare EXPECTED ACTUAL
    [--tables NAME [NAME ...]]
    [--max-row-diffs N]
    [--format text|json]      # default text
```

Exit codes: `0` equal · `1` not equal · `2` input error. The report goes to
stdout; input errors go to stderr. Registered as a `Verb` in `cli.py`'s `VERBS`
tuple (the sole verb registry; help tests parametrize over it).

### Business rules (argument validation inside `compare_datasets`, all `CompareInputError`)

| Rule | Checks | Error Message |
|---|---|---|
| expected-is-duckdb | `expected` opens as a DuckDB database file | `"expected side must be a DuckDB file: {path}"` |
| actual-recognized | `actual` is a DuckDB file or a directory containing ≥ 1 `.csv` | `"actual side is neither a DuckDB file nor a CSV directory: {path}"` |
| csv-header | each actual-side CSV has a header row | `"CSV file has no header row: {path}"` |
| tables-known | every `tables` entry exists in the expected catalog | `"tables selection names unknown table(s): {names}"` |
| tables-nonempty | a provided `tables` selection has ≥ 1 entry | `"tables selection must not be empty"` |
| family-covered | every expected-side column type within the comparison universe maps to a canonical family | `"expected column {table}.{column} has unsupported type {type}"` |
| diff-cap-sane | `max_row_diffs >= 0` | `"max_row_diffs must be >= 0"` |

## Phases

### Phase 1: Canonical form and report types

**Delivers:** The compare package skeleton: `CompareInputError`, the four frozen
report dataclasses, and the canonical-form authority (`family_of` +
`encode_value`) implementing the doc's family and encoding tables — including
byte-identity with the C6 codec's encode half for the four overlapping families.

**Demo:** `phase_1_canonical_form.py` — encodes one representative value (and a
NULL) per family, prints the family → canonical-text table, and asserts
byte-identity between `encode_value` and `reader.conformance.to_csv_text` for
BIGINT / DOUBLE / BOOLEAN / VARCHAR values (including a repr-sensitive float).

**Contracts:** `CanonicalFamily`, `family_of`, `encode_value`,
`SchemaDiscrepancy`, `RowDiscrepancies`, `TableComparison`, `ComparisonResult`,
`CompareInputError`.

**Steps:** none (single implementer)

**Files:**

| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/compare/__init__.py` |
| Create | `src/fabulexa_forge/compare/errors.py` |
| Create | `src/fabulexa_forge/compare/report.py` |
| Create | `src/fabulexa_forge/compare/canonical.py` |
| Create | `tests/compare/__init__.py` |
| Create | `tests/compare/test_canonical.py` |
| Create | `docs/sprints/dataset-equivalence/demos/phase_1_canonical_form.py` |

**Tests (`tests/compare/test_canonical.py`):**

- `family_of` classification: every integer type name DuckDB reports (`BIGINT`,
  `INTEGER`, `SMALLINT`, `TINYINT`, `HUGEINT`, unsigned variants) → `integer`;
  `DOUBLE` and `FLOAT` → `float`; `BOOLEAN` → `boolean`; `VARCHAR` → `text`;
  `TIMESTAMP` at every precision DuckDB names (`TIMESTAMP`, `TIMESTAMP_S`,
  `TIMESTAMP_MS`, `TIMESTAMP_NS`) → `timestamp`; `DATE` → `date`; `TIME` →
  `time`; `TIMESTAMP WITH TIME ZONE` → `timestamptz`; `INTERVAL` → `interval`;
  `BLOB` → `blob`
- `family_of` returns `None` for `DECIMAL(18,3)` and for a never-family type
  (e.g. `UUID`) — no error, classification only
- integer encoding: `str(int)` (positive, negative, zero)
- float encoding: `repr(float)` — `0.1` → `"0.1"`, a repr-sensitive value
  (e.g. `0.30000000000000004`) round-trips exactly
- boolean encoding: lowercase `true` / `false`
- text encoding: identity, including the empty string (distinct from None)
- timestamp encoding: `YYYY-MM-DD HH:MM:SS.ffffff`, fixed six-digit microsecond
  field including `.000000`; naive — no zone attached
- date encoding: `YYYY-MM-DD`
- time encoding: `HH:MM:SS.ffffff` fixed six-digit field
- timestamptz encoding: a nonzero-offset aware datetime normalizes to the UTC
  instant, rendered `YYYY-MM-DD HH:MM:SS.ffffff+00:00`
- interval encoding: pure-µs triple → `H:MM:SS.ffffff` with unbounded hours
  (a 26-hour value renders `26:00:00.000000`); nonzero days fold at exactly
  24 h/day (1 day + 2 h → `26:00:00.000000`); negative delta carries a leading
  `-`; a month-carrying triple encodes as its DuckDB text rendering (never the
  `H:MM:SS` form, never an error)
- blob encoding: lowercase hex; empty bytes → empty string
- NULL: `encode_value(None, family)` returns `None` for every family
- byte-identity: parametrized over representative BIGINT / DOUBLE / BOOLEAN /
  VARCHAR values, `encode_value(v, family)` equals
  `reader.conformance.to_csv_text(v, duckdb_type)` byte-for-byte (test imports
  both; source imports neither)
- report types: frozen dataclasses reject mutation; `ComparisonResult` is
  constructible with nested tuples (smoke — the semantics land in Phase 2)

### Phase 2: The comparison engine

**Delivers:** `compare_datasets` end-to-end: input validation and loading (the
UTC-pinned in-memory DuckDB session, `main`-schema-only catalogs, the CSV
directory scan + per-family typing casts with the blob and interval bespoke
parses), table matching over the universe, column matching + family
compatibility, multiset row comparison with canonical ordering and truncation,
and the deterministic `ComparisonResult` assembly.

**Demo:** `phase_2_compare_datasets.py` — builds a small expected DuckDB file,
then (a) an actual DuckDB copy with scrambled row and column order and a
narrower integer type → prints `equal=True`; (b) a CSV directory export of the
same relation → `equal=True`; (c) a mutated actual (dropped table, extra column,
one changed value, one duplicated row) → prints the discrepancy report showing
each kind.

**Contracts:** `compare_datasets` (public); consumes Phase 1's canonical seam
and report types.

**Steps:** `source → author (1 agent, 2 files)` — the engine and the test suite
each re-read the same deep semantic surface (the doc's matching + encoding +
input rules), so tests are authored in a fresh context.

**Files:**

| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/compare/inputs.py` |
| Create | `src/fabulexa_forge/compare/engine.py` |
| Modify | `src/fabulexa_forge/compare/__init__.py` |
| Create | `tests/compare/test_inputs.py` |
| Create | `tests/compare/test_engine.py` |
| Create | `docs/sprints/dataset-equivalence/demos/phase_2_compare_datasets.py` |

**Tests (`tests/compare/test_inputs.py` — the input surface, exercised through
`compare_datasets`):**

- expected path missing / a plain text file → `CompareInputError`
  (`expected side must be a DuckDB file: ...`)
- actual path neither DuckDB file nor directory-with-`.csv` (missing path,
  empty dir, dir with only `.txt`) → `CompareInputError`
- actual CSV with no header row (zero-byte file) → `CompareInputError` naming
  the file
- `tables` naming an unknown expected-side table → `CompareInputError` listing
  the names; `tables=[]` → `CompareInputError`; `max_row_diffs=-1` →
  `CompareInputError`; `max_row_diffs=0` is accepted (empty listings, totals
  still reported)
- expected-side `DECIMAL` column inside the universe → `CompareInputError`
  (`unsupported type`); the same column in a table excluded by `tables` → no
  error, comparison proceeds
- DuckDB inputs read `main` schema only: a table in another schema on either
  side is invisible (never compared, never `table-extra`)
- actual CSV directory scan: subdirectories, non-`.csv` files, and `.CSV`
  (case-sensitive extension) entries are ignored; table name is the file stem
- CSV typing: integer / float / boolean / temporal cells cast toward the
  expected family's reference type; a failing cast (non-numeric text in an
  integer column) is a **value discrepancy carrying the raw text**, not an error
- CSV NULL vs empty string: unquoted empty field reads as NULL; quoted `""`
  reads as the empty string; the two are not equal to each other
- CSV blob: lowercase-hex text decodes to bytes (never DuckDB's literal-bytes
  text→BLOB cast); odd-length or non-hex text is a cast-failure value
  discrepancy
- CSV interval: writer-form text (`26:00:00.000000`, `-0:00:01.000000`) parses
  exactly; another interval vocabulary (`1 day 02:00:00`) goes through
  `TRY_CAST` and compares equal to the 26-hour writer form under the day-fold
- CSV timestamptz: offset-less text reads as a UTC wall clock (the session-zone
  pin — machine-independent); offset-carrying text keeps its own offset and
  compares as the same instant

**Tests (`tests/compare/test_engine.py` — matching, verdict, determinism):**

- identical DuckDB files → `equal=True`, every table zero discrepancies,
  `expected_rows == actual_rows`
- row order scrambled → equal; column declaration order scrambled → equal;
  actual `INTEGER` for expected `BIGINT` with equal values → equal (no
  discrepancy for compatible physical drift); actual `FLOAT` for `DOUBLE` →
  compared after cast to `DOUBLE`
- actual `VARCHAR` for expected `BIGINT` → `column-incompatible`, column
  excluded from the row pass, remaining columns still row-compared,
  `equal=False`
- table in expected only → `table-missing`, `expected_rows` set, `actual_rows`
  and `rows` `None`, no row comparison; table in actual only → `table-extra`,
  mirrored
- zero-row table on both sides with matching columns → equal (absence ≠
  emptiness)
- column missing / extra → schema discrepancies with `expected_type` /
  `actual_type` populated where applicable
- multiset semantics: a tuple with multiplicity 3 vs 1 lists 2 `missing`
  occurrences; an extra duplicate lists as `extra`; totals count occurrences
- `max_row_diffs` truncates `missing` / `extra` listings per table per
  direction; `missing_total` / `extra_total` stay untruncated; `equal` computed
  over full tables (a difference beyond the cap still fails)
- compared-column set empty (every column incompatible) → row pass degenerates
  to the count check; schema discrepancies already make `equal=False`
- `tables` narrowing: an actual-side table outside the selection is not
  `table-extra`; an expected-side table outside the selection is not compared
- NULL vs empty string in a DuckDB actual differ (a NULL cell against an
  empty-string cell is a row difference)
- NULL ordering: in listed tuples, NULL sorts before every encoded string
- `ComparisonResult.tables` is sorted by table name and spans the union of both
  sides' names within the universe
- discrepancy ordering within a table follows the kind's literal declaration
  order (`table-missing` … `column-incompatible`)
- determinism: two runs over the same inputs produce equal `ComparisonResult`
  objects (dataclass equality) — and identical listed-row orderings
- timestamp precision drift: `TIMESTAMP_S` actual vs `TIMESTAMP` expected with
  the same instants → equal (µs-precision comparison)

### Phase 3: Renderers and the CLI verb

**Delivers:** `render_comparison_text` + `render_comparison_json`, and the
`compare` verb wired into `cli.py`'s `VERBS` registry with the doc's exit-code
contract.

**Demo:** `phase_3_cli_compare.py` — builds the Phase-2-style fixtures, invokes
`fabulexa_forge.cli.main` for: an equal pair (`exit 0`, text report), an unequal
pair (`exit 1`), `--format json` (byte-stable JSON on stdout), and a bad input
(`exit 2`, message on stderr).

**Contracts:** `render_comparison_text`, `render_comparison_json`, CLI verb.

**Steps:** none (single implementer)

**Files:**

| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/compare/render.py` |
| Modify | `src/fabulexa_forge/compare/__init__.py` |
| Modify | `src/fabulexa_forge/cli.py` |
| Create | `tests/compare/test_render.py` |
| Create | `tests/test_cli_compare.py` |
| Create | `docs/sprints/dataset-equivalence/demos/phase_3_cli_compare.py` |

**Tests (`tests/compare/test_render.py`):**

- text render of an equal result: one verdict line + one line per equal table
- text render of an unequal result: verdict line, then one block per table
  carrying discrepancies (schema kinds and row listings visible; totals shown
  even when listings are truncated)
- text render is deterministic (same result → same string)
- JSON render: parses back with `json.loads`; mirrors the `ComparisonResult`
  shape (nested tables, `null` for `None` fields, tuples as arrays)
- JSON render is byte-stable: sorted keys, fixed separators; two renders of the
  same result are identical strings

**Tests (`tests/test_cli_compare.py`):**

- equal pair → exit 0, text report on stdout, empty stderr
- unequal pair → exit 1, report still on stdout
- input error (missing expected file) → exit 2, message on stderr, no report
- `--tables` narrows the comparison (an extra actual-side table outside the
  selection no longer fails the verdict)
- `--max-row-diffs 0` accepted; listings empty, totals present
- `--format json` → parseable JSON on stdout; `--format text` is the default
- existing tests still pass unchanged: `tests/test_cli_help.py` parametrizes
  over `VERBS`, so the new verb is covered without migration

## What Doesn't Change

- **The bundle boundary and the reader.** Compare opens no emit, reads no
  sidecar; `src/fabulexa_forge/reader/` is untouched.
- **The conformance checker and its codec.** `reader/conformance.py` (including
  `to_csv_text`) is not modified and not imported by any `compare/` source
  module — byte-identity is asserted in tests only, preserving the
  independent-copy stance.
- **Exporters and writers.** No rendering, ordering, or serialization change in
  `exporters/` or `writers/` — their determinism is what makes the verdict
  meaningful.
- **The export error hierarchy.** `src/fabulexa_forge/errors.py` gains nothing;
  `CompareInputError` lives in its own domain module.
- **The notice channel.** `exporters/notices.py` untouched; compare emits no
  notices.
- **Existing CLI verbs.** `_cmd_validate` / `_cmd_export` / `_cmd_init` /
  `_cmd_stream` / `_cmd_mixer` / `_cmd_corrupt` and `dispatch` / `main` are
  unmodified; `cli.py` changes are additive (new `_cmd_compare`, one `VERBS`
  entry, the module docstring's usage block).
- **Existing agreement tests.** The incremental-vs-full and playback-vs-full
  test comparisons stay as they are; rewiring them onto `compare_datasets` is
  deferred follow-up work.

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/compare/__init__.py` | New package; public surface re-exports (grown per phase) |
| `src/fabulexa_forge/compare/errors.py` | New — `CompareInputError` |
| `src/fabulexa_forge/compare/report.py` | New — the four frozen report dataclasses |
| `src/fabulexa_forge/compare/canonical.py` | New — `CanonicalFamily`, `family_of`, `encode_value` |
| `src/fabulexa_forge/compare/inputs.py` | New — input validation + loading (UTC-pinned session, CSV typing) |
| `src/fabulexa_forge/compare/engine.py` | New — `compare_datasets` |
| `src/fabulexa_forge/compare/render.py` | New — text + JSON renderers |
| `src/fabulexa_forge/cli.py` | Add `compare` verb (`_cmd_compare`, `VERBS` entry, usage docstring) |
| `tests/compare/__init__.py` | New test package |
| `tests/compare/test_canonical.py` | New — family classification + encoding + codec byte-identity |
| `tests/compare/test_inputs.py` | New — input validation, CSV typing, session pin |
| `tests/compare/test_engine.py` | New — matching, multiset rows, verdict, determinism |
| `tests/compare/test_render.py` | New — text/JSON renderer determinism |
| `tests/test_cli_compare.py` | New — verb exit codes, flags, output routing |
| `docs/sprints/dataset-equivalence/demos/phase_1_canonical_form.py` | Phase 1 demo |
| `docs/sprints/dataset-equivalence/demos/phase_2_compare_datasets.py` | Phase 2 demo |
| `docs/sprints/dataset-equivalence/demos/phase_3_cli_compare.py` | Phase 3 demo |
