# Writers

**Status:** Implemented. Code is the contract — see
[`writers/`](../../src/fabulexa_forge/writers/)
(`csv.py`, `duckdb.py`) and
[`tests/writers/`](../../tests/writers/). Public API: `write_csv`,
`write_duckdb`.

The output adapters. A writer takes one `SELECT`, materializes it through
`Emit.query_arrow` — never a raw connection — and serializes the typed Arrow result
to one output target. Writers are generic: they carry no mode or schema knowledge
and own only their output. CSV and DuckDB serve every mode that materializes a
`SELECT` (dimensional, source, base); most of their serialization contract —
column typing, newline handling, the windowed DuckDB append path — is documented
with their first consumer in [`dimensional.md`](dimensional.md). The pinned
per-type text forms (§ Pinned text forms) are a cross-mode writer contract
owned here, since they serve every mode with an elected rendering
([`temporal-elections.md`](temporal-elections.md),
[`value-rendering-elections.md`](value-rendering-elections.md)).

---

## Boundary

- **Inputs.** The open (read-only) `Emit`, the output table / file-stem name, the
  `SELECT` SQL, and the output location (a directory, or a `.duckdb` file path for
  the DuckDB writer).
- **Output.** One `<name>.csv` file or one typed table in a `.duckdb` file. Each
  writer returns the row count written; a zero-row result still emits the file /
  table.
- **Materialization.** Every writer runs its query through `Emit.query_arrow`; none
  opens `run.duckdb` directly.

## Semantics

A writer materializes its `SELECT` through `Emit.query_arrow`, serializes the typed
Arrow result to one target, and returns the row count written; a zero-row relation
still emits the file or table. Writers hold no schema or mode knowledge. The
non-temporal CSV and DuckDB serialization contracts — column typing, newline
handling, and the windowed DuckDB append path used by incremental export — are
documented with their first consumer in [`dimensional.md`](dimensional.md).

### Pinned text forms

DuckDB output stores every elected type as a native value — no text
form arises there. CSV values are serialized Python-side from the
materialized Arrow values; the reader's session-zone pin
([`reader.md`](reader.md) § The session-zone pin) arrives as value-attached
zone metadata, so the writer stays generic (type-driven — no mode, schema,
or anchor knowledge) while its serialization carries explicit, pinned
per-type text forms for the elected types
([`temporal-elections.md`](temporal-elections.md),
[`value-rendering-elections.md`](value-rendering-elections.md)):

| Type | DuckDB output | CSV text form |
|---|---|---|
| `DATE` | native | `YYYY-MM-DD` |
| `TIME` | native | `HH:MM:SS.ffffff` — fixed six-digit µs field |
| `TIMESTAMPTZ` | native — the exact instant; a consumer's own session displays it in its own zone, the type's real-world behavior | `YYYY-MM-DD HH:MM:SS.ffffff±HH:MM` — the local wall clock in the anchor zone, carrying that instant's UTC offset, fixed six-digit µs field |
| `INTERVAL` | native | the signed µs delta as `[-]H:MM:SS.ffffff` — unbounded hours, fixed six-digit µs field, no calendar components |
| `DECIMAL(p, s)` | native | the plain scale-digit decimal string — sign included, exactly `s` fraction digits (`s = 0` renders the bare integer text with no decimal point), no exponent |

The default `TIMESTAMP` and `DOUBLE` forms are the writers' default
serialization — an unelected column carries no pinned form. The elected
types format by this pinned rule, never by an incidental `str()` of the
in-memory value — CSV parity is a commitment of the election surfaces:
every elected type serializes deterministically under both output formats.
The source event log's `changes` entries pin their in-JSON temporal text
to these same forms, plus a naive-`TIMESTAMP` form owned at that site
([`value-rendering-elections.md`](value-rendering-elections.md)
§ Event-log and after-image reach).

### The DuckDB writer's keyed creation path

The DuckDB writer has a keyed creation path: a table whose caller declares keys
(`write_duckdb`'s `keys` mapping of [`TableKeys`](../../src/fabulexa_forge/exporters/query_spec.py);
`QuerySpec.keys` through the shared dispatch) is created with explicit column DDL —
names and types transcribed from the materialized Arrow schema, so the writer stays
schema-ignorant — plus the declared `PRIMARY KEY` / `UNIQUE` constraints, then
loaded by insert. A table without declarations keeps the `CREATE TABLE AS` path
byte-for-byte. A constraint violation during load is a loud `ExportRuntimeError`
naming the table; a `keys` entry naming a table absent from `queries` is a
`ValueError` (a caller bug). The windowed path reads `spec.keys` on its
create-if-missing branch only — constraints created at the first window persist,
and DuckDB enforces them on every later insert. Which tables declare which keys is
the caller's decision, never the writer's ([`declared-keys.md`](declared-keys.md)).

## Invariants

1. **Determinism.** Same relation → byte-identical CSV file; identical DuckDB query
   results.
2. **Generic.** A writer holds no mode or schema knowledge; it serializes whatever
   relation it is handed. The keyed creation path preserves this: the writer
   transcribes column DDL from the relation's own Arrow schema and consumes
   declared keys — it never decides them.

## Boundaries

- **Parquet is not shipped.** Planned; not present.
- **CSV / DuckDB serialization.** The non-elected contract is documented with
  its first consumer in [`dimensional.md`](dimensional.md); the pinned
  text forms are owned here (§ Pinned text forms); the shared materialization
  boundary lives here regardless of consumer.

## Related

| Document | Why |
|---|---|
| [`dimensional.md`](dimensional.md) | The CSV / DuckDB writers' non-temporal serialization contract and their first consumer. |
| [`temporal-elections.md`](temporal-elections.md) | The elected temporal types whose pinned CSV text forms this doc owns. |
| [`value-rendering-elections.md`](value-rendering-elections.md) | The `decimal` election whose `DECIMAL(p, s)` CSV text form this doc owns, and the event-log site that pins its in-JSON temporal text to these forms. |
| [`declared-keys.md`](declared-keys.md) | The `declare_keys` capability that feeds the DuckDB keyed creation path. |
| [`reader.md`](reader.md) | `Emit.query_arrow` — the one materialization path every writer uses — and the session-zone pin that makes temporal serialization machine-independent. |
| [`README.md`](README.md) | Design index, package layout, staged roadmap. |
| [`../../CLAUDE.md`](../../CLAUDE.md) | Principles, the isolation boundary, vocabulary. |
