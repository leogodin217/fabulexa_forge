---
status: draft
---

# Dataset Equivalence — the compare surface

## Problem

Forge's determinism invariant — same emit + same config + same code version →
identical output — is asserted only *inside* forge. No surface can answer the
question external consumers now depend on: **is this dataset exactly the relation
forge renders?**

The concrete consumer is deterministic grading: a learning environment loads a
forge source-mode render into a learner's database, the learner builds the target
shape (a dimensional warehouse) from it, the environment extracts the learner's
tables into neutral files and needs a decidable verdict — *exactly equal to
forge's own dimensional render of the same emit* — plus a structured account of
how it differs. Not a fuzzy score; a boolean and a report.

Nothing provides that verdict today, and naive comparison gives the wrong one.
Two datasets can be the same relation while differing in every incidental
representation:

- **Row order.** Relations are unordered. Forge's renders carry a deterministic
  total `ORDER BY`, but an external producer's extract order is arbitrary.
- **Column order.** A learner's `CREATE TABLE` may declare the same columns in a
  different order.
- **Value rendering.** Float text (`0.1` vs `0.10000000000000001`), boolean case
  (`TRUE` vs `true`), timestamp formatting, CSV's NULL-versus-empty-string
  ambiguity.
- **Type width.** A round-trip through a learner's Postgres turns `BIGINT` into
  `INT4` where values fit; the relation is unchanged.

Forge also needs this answer internally. The agreement checks that already exist —
incremental-export-versus-full-refresh, playback-window-versus-full-export — each
hand-roll their comparison in test code, with no shared canonical form, no
discrepancy reporting, and no contract.

## Solution

A **compare surface**: one library entry point plus a `fabulexa-forge compare`
CLI verb. It takes two materialized datasets — an *expected* side (a forge
render, DuckDB) and an *actual* side (any dataset claiming to be the same
reshape, DuckDB or CSV) — and reports exact equality under a forge-owned
canonical form:

```
expected (.duckdb, typed, authoritative)  ┐
                                          ├─▶  canonicalize ─▶ multiset compare ─▶ ComparisonResult
actual (.duckdb file | CSV directory)     ┘        (tables by name, columns by name,
                                                    rows as multisets of canonically
                                                    encoded tuples)
```

The verdict is `equal: bool` plus a deterministic, bounded discrepancy report
(tables missing/extra, columns missing/extra/incompatible, row-count deltas,
first-N row-level differences). There are no tolerances, no fuzzy matching, and
no scoring — interpretation of the report belongs to the consumer. The surface is
a pure function of its two inputs: no emit, no bundle, no network, no live
database connection.

## Affected Subsystems

- **A new compare subsystem** — the canonical encoding (a forge-owned superset of
  the conformance codec's encode family), the table/column/row comparison
  semantics, and the `ComparisonResult` report types. It reads its two inputs
  through its own in-memory DuckDB session; it never opens an emit, so the
  reader-first rule (which governs `run.duckdb` + `base.json`) is not in play.
- **CLI** — a new `compare` verb beside `validate` / `export` / `corrupt` /
  `stream`, with text and JSON report rendering and a three-way exit code.
- **Shared SQL utilities** — gain the canonical-encoding rendering the compare
  engine uses; the encode functions overlap the conformance codec's four types
  byte-for-byte but are a distinct, compare-owned contract (see Rationale).

The conformance checker, the exporters, and the writers are context, not
modification targets: their contracts (the C6 codec stance, the render
determinism guarantee, the writers' serialization) are what make the compare
verdict meaningful, and none of them changes.

## What Doesn't Change

- **The bundle boundary.** The compare surface takes no emit and reads no
  sidecar. The bundle + `contract/` remains the only *upstream* interface; the
  compare inputs are downstream artifacts.
- **C1–C14 and the C6 codec.** The conformance checker's independent
  producer-mirroring codec stays untouched and independent; compare does not
  reuse it as a module and does not extend `validate`.
- **Exporter output.** No exporter's rendering, ordering, or serialization
  changes to accommodate comparison.
- **The notice channel.** Compare emits no notices; its entire informational
  output is the `ComparisonResult`. It takes no `notice_sink`.
- **No live connectivity.** Forge does not gain database drivers, connection
  strings, or extraction tooling. Producing the actual-side files is the
  consumer's job.
- **No scoring.** Partial credit, tolerance policies, and feedback are consumer
  interpretation of the report, out of scope permanently.
- **Internal test migration.** The existing agreement tests are the surface's
  first consumers, but rewiring them is implementation work, not part of this
  design.

## Semantics

### Inputs and the schema authority

The **expected** side must be a DuckDB file: it is authoritative for the table
set, the column sets, and the column types, so it must carry a schema. A CSV
expected side is refused at argument-validation time (CSV carries no types;
inferring them would invent the very authority the expected side exists to
provide).

The **actual** side is a DuckDB file or a directory of CSV files
(`<table>.csv`, header row required — the same layout the CSV writer emits).
CSV cells are text; each is typed by casting toward the expected column's type
(below).

| Input condition | Result |
|---|---|
| Expected path is not a readable DuckDB file | error (raise / exit 2) |
| Actual path is neither a DuckDB file nor a directory containing `.csv` files | error |
| Actual CSV file lacks a header row | error naming the file |
| `tables` selection names a table absent from the expected side | error |

### Table matching

The expected side defines the table universe (optionally narrowed by an explicit
`tables` selection). Matching is by exact, case-sensitive name.

| Condition | Result |
|---|---|
| Table in expected, absent from actual | `table-missing` discrepancy; no row comparison |
| Table in actual, absent from expected | `table-extra` discrepancy |
| Table present on both sides | column matching, then row comparison |
| Zero-row table present on both sides with matching columns | equal (an empty relation is a relation; writers emit zero-row tables, so absence ≠ emptiness) |

### Column matching and type compatibility

Columns match by exact, case-sensitive name; **column order is not part of
equality** (relational semantics — forge's own outputs name their columns, and a
correct warehouse extracted in a different declaration order is the same
relation).

Each matched column pair is classified by the expected column's **canonical
family**. The actual column is *compatible* when it belongs to the same family;
family membership is what absorbs lossless representation drift (`INT4` for
`BIGINT`) without admitting semantic drift (`VARCHAR` for `BIGINT`).

| Canonical family | Expected DuckDB type | Compatible actual types |
|---|---|---|
| integer | `BIGINT` (and narrower integer types) | any integer type |
| float | `DOUBLE` | `DOUBLE`, `FLOAT` (compared after cast to `DOUBLE`) |
| boolean | `BOOLEAN` | `BOOLEAN` |
| text | `VARCHAR` | `VARCHAR` |
| timestamp | `TIMESTAMP` (any precision) | any `TIMESTAMP` precision (compared at microsecond precision) |
| date | `DATE` | `DATE` |
| blob | `BLOB` | `BLOB` |

| Condition | Result |
|---|---|
| Column in expected, absent from actual | `column-missing` discrepancy |
| Column in actual, absent from expected | `column-extra` discrepancy |
| Matched column, incompatible family | `column-incompatible` discrepancy; the column is excluded from row comparison |
| Matched column, compatible family, different physical type | no discrepancy; values compare under the family's canonical encoding |
| Expected column of a type outside the family table | error — the expected side is not a forge render shape this surface defines equality for |

Row comparison runs over the **compared-column set**: the columns matched with a
compatible family. Missing/extra/incompatible columns are already reported at
schema level; excluding them from the row pass keeps the row report signal
instead of failing every row for one bad column. `equal` still requires zero
discrepancies of any kind, so the narrowing never manufactures a false pass.

### Canonical value encoding

Every cell is rendered to canonical text before comparison. The encoding is a
forge-owned canonical form: for the four types the conformance codec covers it is
byte-identical to that codec's encode half; the remaining families extend the
same stance to export-side types.

| Family | Canonical encoding |
|---|---|
| NULL (any family) | a reserved NULL marker, distinct from every encoded value including the empty string |
| integer | `str(int)` |
| float | `repr(float)` after cast to `DOUBLE` |
| boolean | `true` / `false` |
| text | identity |
| timestamp | `YYYY-MM-DD HH:MM:SS.ffffff` at microsecond precision, as stored (no zone conversion — forge renders carry no zone) |
| date | `YYYY-MM-DD` |
| blob | lowercase hex of the bytes |

Actual-side CSV typing: a cell is cast from text to the expected column's family
type; a cast failure (non-NULL text that does not parse) is a **value
discrepancy** carrying the raw text, never a crash. An unquoted empty field reads
as NULL; a quoted empty string (`""`) reads as the empty string — the one place
CSV must distinguish what DuckDB storage distinguishes natively.

### Row comparison

Rows are compared as **multisets** of canonically encoded tuples over the
compared-column set. Order-insensitivity is safe universally: wherever order is
semantic in a forge render (the source event log), the order is *carried in the
data* (its dense `id`), so an order-scrambled copy with intact values is still
equal — and a re-sequenced one still fails on values.

| Condition | Result |
|---|---|
| Same multiset of encoded tuples | table rows equal |
| Tuple in expected with multiplicity m, in actual with multiplicity n < m | `rows-missing` discrepancy (m − n occurrences) |
| Tuple in actual with multiplicity n > m | `rows-extra` discrepancy (n − m occurrences) |
| Row counts differ | `row-count` discrepancy (also implied by the above; reported once with both counts) |

Row-level discrepancies are reported in the canonical sort order of the encoded
tuples, truncated to `max_row_diffs` per table per direction, with the total
count always reported untruncated. Truncation bounds the *report*, never the
verdict: equality is computed over full tables.

### The verdict

`equal` is true iff the comparison produced **zero discrepancies of any kind**
across all compared tables. There are no advisory or ignorable discrepancy
kinds; a consumer that wants to tolerate (say) extra tables filters its inputs
before calling, not the report after.

### Determinism

Same two input datasets → byte-identical `ComparisonResult` (and byte-identical
JSON rendering). Discrepancies are ordered by (table name, discrepancy kind,
canonical tuple / column name). No RNG, clock, network, or environment is
consulted. This is the surface's own instance of the package-wide determinism
invariant, and it is what lets a grader treat the report as a stable artifact.

### Invariants

1. **Decidability.** Every input pair yields either an error (malformed input)
   or a verdict; no input yields "approximately".
2. **Exactness.** No tolerance exists anywhere in the surface. Two datasets are
   equal iff their canonical forms are identical.
3. **Verdict/report consistency.** `equal` ⇔ the discrepancy list is empty.
   Truncation affects listed rows only, never counts or the verdict.
4. **Purity.** The verdict is a function of the two datasets alone.
5. **Read-only.** Neither input is modified; both are opened read-only.

## Configuration

No YAML config surface. The compare surface is caller-parameterized (library
arguments / CLI flags); it introduces no author-facing export-config grammar.

## Interface Contracts

### Runtime Types

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
    missing: tuple[tuple[str | None, ...], ...]    # encoded tuples in expected, absent/short in actual
    extra: tuple[tuple[str | None, ...], ...]      # encoded tuples in actual, absent/short in expected
    missing_total: int                             # untruncated occurrence counts
    extra_total: int
```

```python
@dataclass(frozen=True)
class TableComparison:
    """The full comparison outcome for one expected-side table."""

    table: str
    schema: tuple[SchemaDiscrepancy, ...]
    expected_rows: int | None    # None when the table was not compared (missing side)
    actual_rows: int | None
    rows: RowDiscrepancies | None  # None when row comparison did not run
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
        tables: Optional narrowing of the comparison to these expected-side
            tables. None compares every expected-side table. (Presentation
            selection, not schema invention: the universe is still the
            expected side's.)
        max_row_diffs: Per-table, per-direction cap on *listed* row
            discrepancies. Bounds the report only; totals and the verdict are
            computed over full tables.

    Returns:
        A ComparisonResult; deterministic for identical inputs.

    Raises:
        CompareInputError: expected is not a readable DuckDB file; actual is
            neither a DuckDB file nor a CSV directory; a CSV file lacks a
            header; a `tables` entry names no expected-side table; an
            expected-side column's type is outside the canonical families;
            max_row_diffs < 0.
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
stdout; input errors go to stderr.

## Validation Rules

### Parse-Time (Pydantic)

None — the surface has no config models.

### Business Rules

Argument validation inside `compare_datasets` (all raise `CompareInputError`):

| Rule | Checks | Error Message |
|---|---|---|
| expected-is-duckdb | `expected` opens as a DuckDB database file | `"expected side must be a DuckDB file: {path}"` |
| actual-recognized | `actual` is a DuckDB file or a directory containing ≥ 1 `.csv` | `"actual side is neither a DuckDB file nor a CSV directory: {path}"` |
| csv-header | each actual-side CSV has a header row | `"CSV file has no header row: {path}"` |
| tables-known | every `tables` entry exists in the expected catalog | `"tables selection names unknown table(s): {names}"` |
| family-covered | every expected-side column type maps to a canonical family | `"expected column {table}.{column} has unsupported type {type}"` |
| diff-cap-sane | `max_row_diffs >= 0` | `"max_row_diffs must be >= 0"` |

## Rationale

**Why the expected side must be typed (DuckDB-only).** The canonical form is
type-directed — `repr` for floats, family compatibility, CSV cell casting all
key off the expected column's type. A CSV expected side would force type
inference, inventing the authority the expected side exists to provide. The
consumer always controls how the reference is rendered, so requiring the DuckDB
writer costs nothing.

**Why dataset-vs-dataset rather than emit+config-vs-dataset.** Taking two files
keeps the surface a pure function with no dependency on the export pipeline, the
bundle, or a config parse — usable identically by the grading consumer (which
has already run `export`) and by internal agreement tests (which hold both
relations in hand). A convenience that renders the expected side internally
would couple the verdict to everything upstream of it and add nothing the
caller cannot do in one prior command.

**Why order-insensitive.** Forge's renders are deterministically ordered, but
the property being checked is *relational* equality — the actual side's
producer owes the same rows, not the same scan order. Everywhere forge makes
order semantic it also makes it data (the event log's dense `id`, `seq` in
streams), so multiset comparison never loses a real difference.

**Why a compare-owned encoding rather than reusing the C6 codec.** The C6 codec
exists to mirror the *producer's* codec byte-for-byte and must stay an
independent copy whose agreement is itself the check. The compare encoding is a
different contract — forge's own canonical form over export-side types the
producer codec never sees (timestamps, dates). Where the two overlap the
encodings are byte-identical, asserted by test, not by shared import — the same
independence stance conformance takes, for the same reason: a copy that must
agree is stronger than an import that cannot disagree.

**Why no tolerance, ever.** The consumer's whole claim on this surface is
decidability. A tolerance is a judgment about which differences matter, and
judgment belongs to consumers (a grader's rubric, a test's assertion). Nothing
prevents a consumer from ignoring parts of the report; the surface itself never
does.

**Why strict on extra tables and columns.** "Ignore extras" is a policy with as
many right answers as consumers. The surface reports; a consumer that wants
extras tolerated narrows its inputs (`tables`, or extracting only the spec'd
tables) — narrowing inputs is composition, narrowing the verdict would be
policy.
