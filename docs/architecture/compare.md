# The Compare Surface

The package's dataset-equivalence surface: `compare_datasets()` plus the
`fabulexa-forge compare` CLI verb. It decides whether a dataset is **exactly the
relation a forge render describes** — a boolean verdict plus a deterministic,
bounded discrepancy report, computed under a forge-owned canonical form that
absorbs incidental representation drift (row order, column order, value
rendering, lossless type width) without admitting semantic drift. There are no
tolerances, no fuzzy matching, and no scoring; interpretation of the report
belongs to the consumer. The concrete external consumer is deterministic
grading — a learning environment comparing a learner-built warehouse extract
against forge's own render of the same emit; the internal consumers are the
agreement checks (incremental-vs-full-refresh, playback-window-vs-full-export),
which get one shared canonical form and report contract instead of hand-rolled
per-test comparison.

**Source:** [`src/fabulexa_forge/compare/`](../../src/fabulexa_forge/compare/)
(public API in [`__init__.py`](../../src/fabulexa_forge/compare/__init__.py)),
tests in [`tests/compare/`](../../tests/compare/), CLI verb in
[`cli.py`](../../src/fabulexa_forge/cli.py).

## Boundary

- **In:** two materialized datasets — an *expected* side (a forge render,
  DuckDB file, authoritative) and an *actual* side (a DuckDB file or a
  directory of CSV files, claiming to be the same reshape) — plus an optional
  `tables` narrowing and a `max_row_diffs` listing cap.
- **Out:** a frozen [`ComparisonResult`](../../src/fabulexa_forge/compare/report.py)
  (the verdict and report), renderable as deterministic text or byte-stable
  JSON ([`render.py`](../../src/fabulexa_forge/compare/render.py)); malformed
  input raises [`CompareInputError`](../../src/fabulexa_forge/compare/errors.py)
  — compare's own failure domain, coupled to neither `ExporterError` nor
  `ReaderError`.
- **Non-inputs:** no emit, no sidecar, no bundle, no export config, no network,
  no live database connection. The surface reads its two inputs through its own
  in-memory DuckDB session ([`inputs.py`](../../src/fabulexa_forge/compare/inputs.py));
  it never opens `run.duckdb` or `base.json`, so the reader-first rule (which
  governs the bundle) is not in play. The compare inputs are downstream
  artifacts; the bundle + `contract/` remains the only *upstream* interface.
- **No notice channel.** Compare emits no notices and takes no `notice_sink`;
  its entire informational output is the `ComparisonResult`.
- Both inputs are opened read-only; neither is modified.

## Semantics

### Inputs and the schema authority

The **expected** side must be a DuckDB file: it is authoritative for the table
set, the column sets, and the column types, so it must carry a schema. A CSV
expected side is refused at argument-validation time (CSV carries no types;
inferring them would invent the very authority the expected side exists to
provide).

The **actual** side is a DuckDB file or a directory of CSV files
(`<table>.csv`, header row required — the same layout the CSV writer emits).
The directory scan considers only top-level entries matching `*.csv`
(case-sensitive extension); subdirectories and other files are ignored. Each
file's table name is its stem. CSV cells are text; each is typed by casting
toward the expected column's type (§ Canonical value encoding).

A DuckDB input — either side — is read from its `main` schema only. Tables in
any other schema are invisible to the comparison: never part of the universe,
never `table-extra`. A forge render only ever populates `main`, so the rule
costs the expected side nothing and gives the actual side one unambiguous
answer.

Malformed input raises `CompareInputError` (CLI exit 2): an unreadable
expected/actual path, a CSV file without a header row, an unknown or empty
`tables` selection, an expected-side column type outside the canonical
families (within the comparison universe), or a negative `max_row_diffs`. The
refusal conditions and messages live with the input resolution
([`inputs.py`](../../src/fabulexa_forge/compare/inputs.py),
[`tests/compare/test_inputs.py`](../../tests/compare/test_inputs.py)).

### Table matching

The expected side defines the table universe; an explicit `tables` selection
replaces that universe with exactly the selected names. An empty selection is
refused at argument-validation time — a universe of nothing would compare
vacuously equal, and a grader must not be able to manufacture a pass from one
buggy config line; "compare everything" is spelled `None`, not `[]`. The
selection scopes the **whole comparison, both sides**: expected- and
actual-side tables outside the selection are ignored entirely — an unselected
actual-side table is not `table-extra`. This is what makes narrowing the
composition mechanism for tolerating extras (§ Rationale). Matching is by
exact, case-sensitive name. The reported table set spans the **union** of the
expected- and actual-side table names within the universe, sorted by name: a
`table-extra` entry has no expected-side counterpart, a `table-missing` entry
has no actual-side one, and each is still one `TableComparison`, so it still
counts toward the verdict. A zero-row table present on both sides with
matching columns is equal — an empty relation is a relation; writers emit
zero-row tables, so absence ≠ emptiness.

### Column matching and canonical families

Columns match by exact, case-sensitive name; **column order is not part of
equality** (relational semantics — forge's own outputs name their columns, and
a correct warehouse extracted in a different declaration order is the same
relation). Unmatched columns are `column-missing` / `column-extra`
discrepancies.

Each matched column pair is classified by the expected column's **canonical
family**. The actual column is *compatible* when it belongs to the same family;
family membership is what absorbs lossless representation drift (`INT4` for
`BIGINT`) without admitting semantic drift (`VARCHAR` for `BIGINT`). An
incompatible pair is a `column-incompatible` discrepancy and is excluded from
row comparison.

| Canonical family | Expected DuckDB type | Compatible actual types |
|---|---|---|
| integer | `BIGINT` (and narrower integer types) | any integer type |
| float | `DOUBLE` | `DOUBLE`, `FLOAT` (compared after cast to `DOUBLE`) |
| boolean | `BOOLEAN` | `BOOLEAN` |
| text | `VARCHAR` | `VARCHAR` |
| timestamp | `TIMESTAMP` (any precision) | any `TIMESTAMP` precision (compared at microsecond precision) |
| date | `DATE` | `DATE` |
| time | `TIME` (any precision) | any `TIME` precision (compared at microsecond precision) |
| timestamptz | `TIMESTAMPTZ` (any precision) | any `TIMESTAMPTZ` precision (compared at microsecond precision) |
| interval | `INTERVAL` | `INTERVAL` |
| blob | `BLOB` | `BLOB` |

The timestamp, date, time, timestamptz, and interval families cover every
temporal type a forge render carries by default or by election
([`temporal-elections.md`](temporal-elections.md)). `DECIMAL` belongs to no
family — deliberately: absorbing a fixed-point round-trip into the float or
integer family would require exactly the lossiness judgment this surface
refuses to make. A `DECIMAL` actual column is `column-incompatible`; producers
extract toward the reference types instead.

The family table is a scope boundary, not an exhaustiveness claim about forge
renders. A render *can* carry an out-of-family data column — base mode casts
data columns back to their declared sidecar types, and the contract admits
producer-chosen types (`DECIMAL` among them) — and such a render is outside
the shape this surface defines equality for. The refusal is whole-comparison,
at argument-validation time, and scoped to the comparison universe: an
out-of-family column in a table outside the `tables` selection does not error,
so a caller holding such a render can still compare its in-family tables by
narrowing. Within the universe the refusal is loud and total — silently
skipping the column would render a verdict over a narrower relation than the
caller named, and admitting the type would require the lossiness judgment the
families exist to avoid.

Row comparison runs over the **compared-column set**: the columns matched with
a compatible family. Missing/extra/incompatible columns are already reported
at schema level; excluding them from the row pass keeps the row report signal
instead of failing every row for one bad column. `equal` still requires zero
discrepancies of any kind, so the narrowing never manufactures a false pass.
If the compared-column set is empty, the row pass degenerates to a row-count
check (every row encodes as the empty tuple); the schema discrepancies that
emptied the set already make the verdict unequal.

### Canonical value encoding

Every cell is rendered to canonical text before comparison. The encoding is a
forge-owned canonical form: for the four types the conformance codec covers it
is byte-identical to that codec's encode half; the remaining families extend
the same stance to export-side types. Encoding is applied Python-side to
materialized values, never via SQL casts — byte-identity with the codec's
`repr`/`str` forms is the contract, and DuckDB's VARCHAR casts do not
guarantee it. The encoding authority is
[`canonical.py`](../../src/fabulexa_forge/compare/canonical.py); the
byte-identity with the C6 codec is asserted by test
([`tests/compare/test_canonical.py`](../../tests/compare/test_canonical.py)),
never by shared import (§ Rationale).

| Family | Canonical encoding |
|---|---|
| NULL (any family) | not encoded to text — carried as Python `None` in the encoded tuple (JSON `null`), distinct by construction from every encoded string including the empty string |
| integer | `str(int)` |
| float | `repr(float)` after cast to `DOUBLE` |
| boolean | `true` / `false` |
| text | identity |
| timestamp | `YYYY-MM-DD HH:MM:SS.ffffff` at microsecond precision, as stored (naive — no zone attached, none converted) |
| date | `YYYY-MM-DD` |
| time | `HH:MM:SS.ffffff` at microsecond precision — the writers' pinned CSV text form |
| timestamptz | the absolute instant normalized to UTC, `YYYY-MM-DD HH:MM:SS.ffffff+00:00` at microsecond precision — equality is instant equality, so the representation zone either side stored or displayed is irrelevant |
| interval | the signed microsecond delta as `[-]H:MM:SS.ffffff` — unbounded hours, fixed six-digit µs field, no calendar components (the writers' pinned CSV text form) |
| blob | lowercase hex of the bytes |

The interval family carries a storage rule: DuckDB stores an `INTERVAL` as a
(months, days, microseconds) triple, and only forge's own renders are
guaranteed pure-µs values — an actual side may carry calendar components. The
canonical encoding folds a nonzero days field into the delta at exactly
24 hours per day, the shape Postgres-lineage timestamp subtraction produces
(`1 day 02:00:00` for a 26-hour gap), so a correct duration compares equal
regardless of how the producer's storage justified it. A nonzero months field
has no fixed microsecond value; a month-carrying value encodes as its DuckDB
text rendering instead — which no `[-]H:MM:SS.ffffff` encoding can equal — so
by construction it surfaces as a row discrepancy carrying the real text, never
an error and never a guessed conversion.

**The compare session is zone-pinned to UTC.** The surface's in-memory DuckDB
session sets its time zone to UTC before either input is read — the
compare-side analogue of the reader's session-zone pin
([`reader.md`](reader.md) § The session-zone pin), discharging the same
machine-independence obligation: without it, casting a timestamptz CSV cell
whose text carries no UTC offset would read the host's local zone into the
verdict. Under the pin such text reads as a UTC wall clock, deterministically;
offset-carrying text — including forge's own CSV render form — is unaffected,
since the text's own offset wins. The pin is UTC rather than an anchor zone
because compare has no emit and no anchor to resolve: it is a fixed constant
of the surface, not a resolved value, so Purity holds.

Actual-side CSV typing: a cell is cast from text toward the expected column's
family reference type, and the cast authority is pinned — DuckDB's `TRY_CAST`
in the (zone-pinned) compare session for every family except two with bespoke
parses: blob (a hex-decode) and interval (a parse of the pinned
`[-]H:MM:SS.ffffff` writer form — sign, unbounded hours, fixed six-digit µs
field — tried first, then `TRY_CAST` for other interval vocabularies, whose
result the calendar-component rule above encodes). Typing casts are the one
SQL-side step; canonical *encoding* of the resulting materialized values is
always Python-side. A cast failure (non-NULL text that fails its family's pinned
cast) is a **value discrepancy** carrying the raw text, never a crash. An
unquoted empty field reads as NULL; a quoted empty string (`""`) reads as the
empty string — the one place CSV must distinguish what DuckDB storage
distinguishes natively. A known boundary follows: forge's own CSV writer
serializes NULL and the empty string identically (an unquoted empty field), so
a forge CSV render of a table whose text cells include genuine empty strings
reads back as NULL and will not compare equal to the DuckDB render of the same
config. The DuckDB render is the reference form; the empty-string/NULL
distinction on a CSV actual side is representable only by producers that quote
empty strings. For the blob family, CSV text is the same lowercase-hex form as
the canonical encoding — the cast is a hex-decode, not DuckDB's native
text→`BLOB` cast (which would take the text as literal bytes); text that fails
to hex-decode (odd length, non-hex characters) is a cast failure under the
same rule as any other family.

### Row comparison

Rows are compared as **multisets** of canonically encoded tuples over the
compared-column set. Order-insensitivity is safe universally: wherever order
is semantic in a forge render (the source event log), the order is *carried in
the data* (its dense `id`), so an order-scrambled copy with intact values is
still equal — and a re-sequenced one still fails on values. A tuple with
expected-side multiplicity m and actual-side multiplicity n < m is a
`rows-missing` discrepancy of m − n occurrences; n > m is `rows-extra`. A
row-count difference has no separate discrepancy kind — it is always implied
by the multiset diff, and the counts themselves are carried by
`expected_rows` / `actual_rows`.

Row-level discrepancies are listed **per occurrence** — a tuple with an
occurrence deficit of k appears k times in the listing — in the canonical sort
order of the encoded tuples (elementwise; NULL sorts before every encoded
string), truncated to `max_row_diffs` per table per direction, with the total
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
canonical tuple / column name), with kind ordered as the
`SchemaDiscrepancy.kind` literal declares — table-level before column-level;
within tuples NULL sorts before every encoded string. No RNG, clock, network,
or environment is consulted — the session-zone pin (§ Canonical value
encoding) is what discharges that claim on the timestamptz path. This is the
surface's own instance of the package-wide determinism invariant, and it is
what lets a grader treat the report as a stable artifact.

### The CLI verb

```
fabulexa-forge compare EXPECTED ACTUAL
    [--tables NAME [NAME ...]]
    [--max-row-diffs N]
    [--format text|json]      # default text
```

Exit codes: `0` equal · `1` not equal · `2` input error — compare's own
exit-code contract, distinct from every other verb's `0/1/3`, because the
verdict itself is the boolean a scripting consumer branches on. The report
goes to stdout; input errors go to stderr. The report shapes are the frozen
dataclasses in [`report.py`](../../src/fabulexa_forge/compare/report.py); the
JSON rendering is a byte-stable mirror of that shape (sorted keys, fixed
separators) — the grading consumer's wire format.

## Invariants

1. **Decidability.** Every input pair yields either an error (malformed input)
   or a verdict; no input yields "approximately".
2. **Exactness.** No tolerance exists anywhere in the surface. Two datasets
   are equal iff their canonical forms are identical.
3. **Verdict/report consistency.** `equal` ⇔ the discrepancy list is empty.
   Truncation affects listed rows only, never counts or the verdict.
4. **Purity.** The verdict is a function of the two datasets alone.
5. **Read-only.** Neither input is modified; both are opened read-only.

## Rationale

- **The expected side must be typed (DuckDB-only)** — the canonical form is
  type-directed: `repr` for floats, family compatibility, CSV cell casting all
  key off the expected column's type. A CSV expected side would force type
  inference, inventing the authority the expected side exists to provide. The
  consumer always controls how the reference is rendered, so requiring the
  DuckDB writer costs nothing.
- **Dataset-vs-dataset, not emit+config-vs-dataset** — taking two files keeps
  the surface a pure function with no dependency on the export pipeline, the
  bundle, or a config parse — usable identically by the grading consumer
  (which has already run `export`) and by internal agreement tests (which hold
  both relations in hand). A convenience that renders the expected side
  internally would couple the verdict to everything upstream of it and add
  nothing the caller cannot do in one prior command.
- **Order-insensitive** — forge's renders are deterministically ordered, but
  the property being checked is *relational* equality: the actual side's
  producer owes the same rows, not the same scan order. Everywhere forge makes
  order semantic it also makes it data (the event log's dense `id`, `seq` in
  streams), so multiset comparison never loses a real difference.
- **A compare-owned encoding, not a reuse of the C6 codec** — the C6 codec
  exists to mirror the *producer's* codec byte-for-byte and is an
  independent copy whose agreement is itself the check
  ([`conformance.md`](conformance.md)). The compare encoding is a different
  contract — forge's own canonical form over export-side types the producer
  codec never sees (timestamps, dates). Where the two overlap the encodings
  are byte-identical, asserted by test, not by shared import — the same
  independence stance conformance takes, for the same reason: a copy that must
  agree is stronger than an import that cannot disagree.
- **No tolerance, ever** — the consumer's whole claim on this surface is
  decidability. A tolerance is a judgment about which differences matter, and
  judgment belongs to consumers (a grader's rubric, a test's assertion).
  Nothing prevents a consumer from ignoring parts of the report; the surface
  itself never does.
- **Strict on extra tables and columns** — "ignore extras" is a policy with as
  many right answers as consumers. The surface reports; a consumer that wants
  extras tolerated narrows its inputs (`tables`, or extracting only the spec'd
  tables) — narrowing inputs is composition, narrowing the verdict would be
  policy.

## Boundaries

- **No YAML config surface.** The compare surface is caller-parameterized
  (library arguments / CLI flags); it introduces no author-facing
  export-config grammar.
- **No scoring.** Partial credit, tolerance policies, and feedback are
  consumer interpretation of the report — permanently outside this surface.
- **No live connectivity.** Forge carries no database drivers, connection
  strings, or extraction tooling. Producing the actual-side files is the
  consumer's job.
- **No exporter accommodation.** No exporter's rendering, ordering, or
  serialization bends to comparison; compare adapts to what the writers emit,
  never the reverse.

## Related

| Document | Why |
|---|---|
| [`conformance.md`](conformance.md) | The C6 codec whose encode half the canonical form mirrors byte-for-byte on the overlapping families — independent by the same stance |
| [`writers.md`](writers.md) | The pinned temporal CSV text forms the time/interval encodings reuse; the DuckDB/CSV serialization the inputs contract mirrors |
| [`reader.md`](reader.md) | The session-zone pin whose machine-independence obligation the compare session's UTC pin discharges on the compare side |
| [`temporal-elections.md`](temporal-elections.md) | The elected temporal renderings the five temporal families cover |
| [`incremental.md`](incremental.md) | An internal agreement consumer: incremental-vs-full-refresh equivalence |
| [`playback.md`](playback.md) | An internal agreement consumer: playback-window-vs-full-export equivalence |
