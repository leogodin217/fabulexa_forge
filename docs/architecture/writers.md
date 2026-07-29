# Writers

**Status:** Implemented. Code is the contract — see
[`writers/`](../../src/fabulexa_forge/writers/)
(`csv.py`, `duckdb.py`) and
[`tests/writers/`](../../tests/writers/). Public API: `write_csv`,
`write_duckdb`.

The output adapters. A writer takes one `SELECT`, materializes it through
`Emit.query_arrow` — never a raw connection — and serializes the typed Arrow result
to one output target. Writers are generic: they carry no mode or schema knowledge
and own only their output. CSV and DuckDB serve the dimensional mode; their
serialization contract is documented with their consumer in
[`dimensional.md`](dimensional.md).

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
still emits the file or table. Writers hold no schema or mode knowledge. The CSV and
DuckDB serialization contracts — column typing, newline handling, and the windowed
DuckDB append path used by incremental export — are documented with their consumer
in [`dimensional.md`](dimensional.md).

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
- **CSV / DuckDB serialization** is documented with their consumer in
  [`dimensional.md`](dimensional.md); the shared materialization boundary lives here.

## Related

| Document | Why |
|---|---|
| [`dimensional.md`](dimensional.md) | The CSV / DuckDB writers' serialization contract and their consumer. |
| [`declared-keys.md`](declared-keys.md) | The `declare_keys` capability that feeds the DuckDB keyed creation path. |
| [`reader.md`](reader.md) | `Emit.query_arrow` — the one materialization path every writer uses. |
| [`README.md`](README.md) | Design index, package layout, staged roadmap. |
| [`../../CLAUDE.md`](../../CLAUDE.md) | Principles, the isolation boundary, vocabulary. |
