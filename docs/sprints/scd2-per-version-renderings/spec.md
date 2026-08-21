# Sprint: scd2-per-version-renderings

## Purpose

Make the pure per-row value renderings (`derived: decimal` / `json_precision` /
`timestamp` / `date_parse` / `value_map`) legal on `scd: type2` dimensional
columns, evaluated per version over tracked sources — so an author can render a
noisy tracked property (e.g. saas `dim_account.engagement_score`) as
`DECIMAL(5,2)` on the dim's version rows exactly as the same property already
renders in source mode.

Design doc: [`docs/architecture/pending/scd2-per-version-value-renderings.md`](../../architecture/pending/scd2-per-version-value-renderings.md)
— pins semantics (§ Mode admissibility, § Per-version evaluation), invariants
(version structure is election-invariant; source-class-blind rendering), and
the validation-rule table. This spec does not restate it.

## Scope

**Capabilities touched:**
- Dimensional exporter: SCD-2 column-mode surface — per-version derived compile
  over tracked sources; `decimal` / `json_precision` admitted (per-record and
  per-version)
- Validation runner: `Scd2ColumnModeSupported` widened; `Scd2DerivedSourceConstant`
  deleted

**Not included:** `history_interval` `value` re-typing; `fk` / `correlation` /
`derived: ordinal` / `derived: elapsed` / `lookup` on type2 (stay refused);
any grammar, reader, derivations, writer, streaming, source, base, incremental,
or compare change; recipe creation and pending-doc folding (post-sprint, per
process).

## Breaking Changes

All internal — no config-visible surface narrows (the change is purely
widening: every previously-valid config still validates and renders
byte-identically).

- The five pure per-row builders in `columns.py` (`build_timestamp_expr`,
  `build_value_map_expr`, `build_date_parse_expr`, `build_decimal_expr`,
  `build_json_precision_expr`) take `source_expr: str` instead of
  `grain_alias`; the latter three also take `table_label: str` instead of
  `table_decl`. All defaults dropped. Callers: `build_column_expr` dispatch,
  `scd.py`, `tests/exporters/dimensional/test_columns.py`, `test_scd.py`.
- `build_scd2_column_expr_flag` swaps `is_tracked: bool` for
  `tracked_props: frozenset[str]` (source-class resolution moves inside).
- `check_scd2_derived_source_constant` is deleted (definition + sole call
  site). No shim, no alias.

## Success Criteria

- [ ] `derived: {decimal: {from: prop__<tracked>, as: [p, s]}}` on an
  `scd: type2` table validates and exports per-version `DECIMAL(p, s)` values
- [ ] All five renderings legal over tracked and constant sources per the
  design doc's admissibility matrix; `fk` / `correlation` / `ordinal` /
  `elapsed` still refused with the updated message
- [ ] Version structure is election-invariant (test-verified: version count,
  `valid_from` / `valid_to` unchanged under any rendering election)
- [ ] Rendered SQL for an untracked source is byte-identical to the records
  grain's modulo alias; rendered output byte-identical across source classes
- [ ] Export-time guards (decimal overflow, strict parse, JSON payload) fire
  on historical version values, not just current state
- [ ] `make check` green

## Contracts

Design decisions (binding):

1. **Cast-then-authority.** On a type2 table, a pure per-row value rendering
   over a **tracked** source compiles its source expression as
   `CAST("_versions"."prop__<p>" AS <sidecar declared type>)` — the same cast
   the tracked `from` path already emits (the versioned-intervals derivation
   serves every `prop__<p>` as codec VARCHAR) — and hands that expression to
   the same rendering builder every other attach site uses. Over a
   **constant / structural / exempt-discriminator** source it hands
   `"_records"."<src>"` — byte-identical to the records grain's expression
   modulo alias. One compiler, two source expressions.
2. The five pure per-row builders in `columns.py` take `source_expr: str`,
   joining the expression-taking posture of the `render_*` authorities in
   `_sql.py` (untouched). `scd.py`'s current direct `render_date_parse_expr`
   call is replaced by `build_date_parse_expr`.
3. Trackedness resolution: new module-private `_column_source_name` in
   `scd.py` maps a ColumnDecl to its single source column name; the
   prop-prefix + tracked-set membership test lives inline in
   `build_scd2_column_expr_flag`.
4. `build_column_expr`'s contract is unchanged — its dispatch composes
   `f'"{grain_alias}"."{<src>}"'` internally and passes `table_decl.name` to
   the label-taking builders.
5. `check_scd2_column_mode_supported`'s contract is the design doc's
   § Interface Contracts, verbatim; its error message is the design doc's
   § Validation Rules row, verbatim. The declared-type gates
   (`check_decimal_source_column`, `check_json_precision_source_column`,
   `check_date_parse_source_column`, `check_timestamp_source_available`) and
   the slice-only surface are contract-unchanged; their reach widens only
   because the mode gate no longer refuses first.
6. Unchanged in signature and contract: `_collect_tracked_props`,
   `build_versioned_intervals_sql`, `render_decimal_expr`,
   `render_json_precision_expr`, `render_date_parse_expr`,
   `render_anchor_temporal_expr`, `resolve_source_column_type`,
   `build_scd2_view_sql`.

### `src/fabulexa_forge/exporters/dimensional/scd.py`

#### `_column_source_name` (new, module-private)

```python
def _column_source_name(col_decl: "ColumnDecl") -> str | None:
    """Resolve the single source column a ColumnDecl reads its value from.

    The mapping across the source-bearing spellings the type2 build admits:
    `from` -> col_decl.from_; `derived: decimal` -> decimal.from_;
    `derived: json_precision` -> json_precision.from_;
    `derived: date_parse` -> date_parse.from_;
    `derived: value_map` -> value_map.from_;
    `derived: timestamp` -> timestamp.source. Modes with no source column
    (`null`, `derived: scd_window`) return None.

    Callers pass only ColumnDecls the type2 mode gate
    (Scd2ColumnModeSupported) admits; other modes are out of contract.

    Args:
        col_decl: The output column declaration.

    Returns:
        The source column name as declared (e.g. "prop__status",
        "sim_time_created", "presentation_id"), or None when the mode reads
        no source column.
    """
```

#### `build_scd2_column_expr_flag` (changed signature: `is_tracked: bool` → `tracked_props: frozenset[str]`)

```python
def build_scd2_column_expr_flag(
    col_decl: "ColumnDecl",
    version_alias: str,
    records_alias: str,
    tracked_props: frozenset[str],
    anchor: "EffectiveAnchor | None",
    sidecar: "Sidecar",
    source_table_name: str,
    table_label: str,
) -> str:
    """Build a SQL expression for one SCD-2 column.

    Resolves the column's source column (_column_source_name) and its class:
    a source named `prop__<p>` with `<p>` in tracked_props is tracked and
    reads per version from version_alias; every other source (constant
    prop__, structural, projection-introduced, exempt discriminator) reads
    per record from records_alias. Structural sources are never tracked —
    they never carry the prop__ prefix.

    Compilation per mode:
    - `derived: scd_window` renders the version bounds
      (version_start / version_end) through render_anchor_temporal_expr.
    - `null` emits a typed NULL.
    - A pure per-row value rendering (`derived: timestamp` / `date_parse` /
      `value_map` / `decimal` / `json_precision`) compiles through the same
      per-column builder every records-grain column uses
      (build_timestamp_expr / build_date_parse_expr / build_value_map_expr /
      build_decimal_expr / build_json_precision_expr), handed a source
      expression per the source class: tracked ->
      CAST("<version_alias>"."prop__<p>" AS <sidecar declared type>) — the
      derivation serves tracked values as codec VARCHAR; the cast is the
      same representation step the tracked `from` path performs — untracked
      -> "<records_alias>"."<src>". The rendered SQL for an untracked
      source is byte-identical to the records grain's modulo alias; for the
      same source value the rendered output is byte-identical across source
      classes (source-class-blind rendering). value_map's WHEN-predicate
      literal typing uses the source's sidecar declared type for both
      classes — matching the tracked cast.
    - `from` projects the tracked cast or the records-relation column.

    No election reads or renumbers version rows: version bounds come from
    version_alias regardless of any value election on the table
    (version structure is election-invariant).

    Args:
        col_decl: The output column declaration (a type2-admitted mode).
        version_alias: Alias of the versioned-intervals derivation subquery.
        records_alias: Alias of the records-relation subquery.
        tracked_props: History-tracked property names (without the prop__
            prefix), from _collect_tracked_props.
        anchor: The resolved EffectiveAnchor, or None.
        sidecar: The emit's typed sidecar, for source-column declared-type
            reads (tracked-path casts, value_map literal typing).
        source_table_name: The dim's source records table, for sidecar
            column reads.
        table_label: The output table name for renderer error messages.

    Returns:
        A SQL expression fragment: `<expr> AS "<col_name>"`.

    Raises:
        ExportError: source_table_name is not found in the sidecar
            (resolve_source_column_type, on paths that read a declared
            type).
    """
```

#### `build_scd2_sql` (signature unchanged; docstring updated)

```python
def build_scd2_sql(
    table_decl: "TableDecl",
    source_table_name: str,
    sidecar: "Sidecar",
    anchor: "EffectiveAnchor | None",
    fork_path: str,
) -> str:
    """Build the SELECT SQL for an scd: type2 records grain.

    Composes the versioned-intervals derivation (build_versioned_intervals_sql)
    for version bounds and tracked prop__<p> values, and the reader records
    relation (build_records_relation_sql) for static columns. The format
    authors no base-table SQL.

    Tracked columns read per version from the derivation's pre-computed
    prop__<p> columns; static columns LEFT JOIN the reader records relation
    on record_id. Column expressions — including the pure per-row value
    renderings, evaluated per version over tracked sources and per record
    otherwise — compile through build_scd2_column_expr_flag, which resolves
    each column's source class from the sidecar tracked set.

    Honors table_decl.source.filter: a discriminator-split source restricts
    both the derivation's version rows and the records relation to the
    filtered sub-type's records.

    Args:
        table_decl: The output table declaration (scd: type2, grain: records).
        source_table_name: The resolved records__<kind> DuckDB table name.
        sidecar: The open emit's sidecar.
        anchor: The resolved EffectiveAnchor, or None.
        fork_path: The sole branch fork_path; passed to the derivation and
            records relation builders.

    Returns:
        A complete, deterministic SELECT statement composing the derivation
        and the reader records relation.

    Raises:
        ExportError: A column's declared-type read finds source_table_name
            missing from the sidecar (build_scd2_column_expr_flag).
    """
```

#### `build_scd2_rows_sql` (signature unchanged; docstring updated)

```python
def build_scd2_rows_sql(
    table_decl: "TableDecl",
    source_table_name: str,
    sidecar: "Sidecar",
    anchor: "EffectiveAnchor | None",
    window_start_ns: int,
    window_end_ns: int,
    fork_path: str,
) -> str:
    """Build the SELECT SQL for a windowed SCD-2 physical rows table.

    Produces all declared columns except scd_window: valid_to slots, in
    declared order, plus a trailing __valid_from_ns column (the version's
    raw sim-time change point). Applies a half-open window predicate on the
    raw change point.

    Composes the versioned-intervals derivation for version bounds and
    tracked prop__<p> values, and the reader records relation for static
    columns. Column expressions — including the pure per-row value
    renderings, evaluated per version over tracked sources and per record
    otherwise — compile through build_scd2_column_expr_flag, which resolves
    each column's source class from the sidecar tracked set. The window
    predicate and __valid_from_ns read raw version bounds, untouched by any
    value election (version structure is election-invariant).

    Honors table_decl.source.filter: a discriminator-split source restricts
    both the derivation's version rows and the records relation to the
    filtered sub-type's records.

    Args:
        table_decl: The output table declaration (scd: type2, grain: records).
        source_table_name: The resolved records__<kind> DuckDB table name.
        sidecar: The open emit's sidecar.
        anchor: The resolved EffectiveAnchor, or None.
        window_start_ns: The window's inclusive start in sim-time ns.
        window_end_ns: The window's exclusive end in sim-time ns.
        fork_path: The sole branch fork_path; passed to the derivation and
            records relation builders.

    Returns:
        A complete SELECT statement for the physical __rows table.

    Raises:
        ExportError: A column's declared-type read finds source_table_name
            missing from the sidecar (build_scd2_column_expr_flag).
    """
```

### `src/fabulexa_forge/exporters/dimensional/columns.py`

All five pure per-row value builders take `source_expr: str` — the SQL
expression producing the source value — instead of a grain alias; defaults
are dropped. Each remains the single compiler for its spelling at every
attach site (records grain via build_column_expr, type2 via
build_scd2_column_expr_flag); callers own qualification and any
representation cast.

#### `build_timestamp_expr` (reshaped)

```python
def build_timestamp_expr(
    col_decl: "ColumnDecl",
    anchor: "EffectiveAnchor | None",
    source_expr: str,
) -> str:
    """Build a SQL expression for a `derived: timestamp` column.

    When an anchor is present, renders the elected wallclock type (absent
    `as` = the mode-definitional default `timestamp` rendering) via
    `render_anchor_temporal_expr`. When absent, returns the raw sim-time
    integer value (the caller enforces `TemporalRenderRequiresAnchor` for
    any explicit election before this runs). A pure per-row value function
    of source_expr: the caller supplies the qualified (and, for type2
    tracked sources, declared-type-cast) BIGINT-producing expression.

    Args:
        col_decl: A ColumnDecl with derived.timestamp set.
        anchor: The resolved EffectiveAnchor, or None when absent.
        source_expr: SQL expression producing the BIGINT sim-instant source
            value.

    Returns:
        A SQL expression fragment ending in `AS "<col_decl.name>"`.
    """
```

#### `build_value_map_expr` (reshaped)

```python
def build_value_map_expr(
    col_decl: "ColumnDecl",
    source_expr: str,
    source_col_type: str,
) -> str:
    """Build a SQL expression for a `derived: value_map` column (CASE).

    Types every branch (including the unmapped NULL) to the inferred DuckDB
    type. The WHEN comparison side uses render_typed_literal so the
    predicate literal matches source_col_type — the source's sidecar
    declared type, which the caller also uses for any representation cast
    inside source_expr, so predicate and value agree. A pure per-row value
    function of source_expr.

    Args:
        col_decl: A ColumnDecl with derived.value_map set.
        source_expr: SQL expression producing the source value.
        source_col_type: DuckDB declared type of the source column, for
            WHEN predicate literal typing.

    Returns:
        A SQL CASE expression fragment ending in `AS "<col_decl.name>"`.
    """
```

#### `build_date_parse_expr` (reshaped)

```python
def build_date_parse_expr(
    col_decl: "ColumnDecl",
    source_expr: str,
    table_label: str,
) -> str:
    """Build a SQL expression for a `derived: date_parse` column.

    Delegates to `render_date_parse_expr` — the one VARCHAR->DATE parse
    renderer every mode shares. Type and existence gates run at plan time
    (DateParseSourceColumn, ProjectionColumnExists); this builder assumes
    both already passed. A pure per-row value function of source_expr.

    Args:
        col_decl: A ColumnDecl with derived.date_parse set.
        source_expr: SQL expression producing the VARCHAR source value.
        table_label: The output table name interpolated into the strict-
            parse guard's error message.

    Returns:
        A SQL expression fragment ending in `AS "<col_decl.name>"`.
    """
```

#### `build_decimal_expr` (reshaped)

```python
def build_decimal_expr(
    col_decl: "ColumnDecl",
    source_expr: str,
    table_label: str,
) -> str:
    """Build a SQL expression for a `derived: decimal` column.

    Delegates to `render_decimal_expr` — the one decimal rendering
    authority every mode shares — and aliases its bare expression. The
    source-type gate (DecimalSourceIsDouble) runs at plan time; this
    builder assumes it already passed. A pure per-row value function of
    source_expr.

    Args:
        col_decl: A ColumnDecl with derived.decimal set.
        source_expr: SQL expression producing the DOUBLE source value.
        table_label: The output table name interpolated into the overflow
            guard's error message.

    Returns:
        A SQL expression fragment ending in `AS "<col_decl.name>"`.
    """
```

#### `build_json_precision_expr` (reshaped)

```python
def build_json_precision_expr(
    col_decl: "ColumnDecl",
    source_expr: str,
    table_label: str,
) -> str:
    """Build a SQL expression for a `derived: json_precision` column.

    Delegates to `render_json_precision_expr` — the one JSON-leaf rendering
    authority every mode shares — and aliases its bare expression. The
    source-type gate (JsonPrecisionSourceIsVarchar) runs at plan time; this
    builder assumes it already passed. A pure per-row value function of
    source_expr.

    Args:
        col_decl: A ColumnDecl with derived.json_precision set.
        source_expr: SQL expression producing the VARCHAR JSON payload.
        table_label: The output table name interpolated into the payload
            guard's error messages.

    Returns:
        A SQL expression fragment ending in `AS "<col_decl.name>"`.
    """
```

#### `build_column_expr` (contract unchanged)

Signature and docstring stand. Its dispatch body composes each reshaped
builder's `source_expr` as `f'"{grain_alias}"."{<spec source>}"'` and, for
the three label-taking builders, passes `table_decl.name`; both are
internal to the existing contract.

### `src/fabulexa_forge/exporters/dimensional/validation.py`

#### `check_scd2_column_mode_supported` (changed — contract fixed by the design doc § Interface Contracts)

```python
def check_scd2_column_mode_supported(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
) -> None:
    """Enforce Scd2ColumnModeSupported: type2 columns use supported modes.

    The type2 surface admits from, null, derived: scd_window, and the pure
    per-row value renderings derived: timestamp / date_parse / value_map /
    decimal / json_precision — each a pure function of one row's source
    value, evaluated per record for constant sources and per version for
    tracked sources. It refuses fk, correlation, derived: ordinal, and
    derived: elapsed — cross-row or grain-surface semantics the type2 build
    does not define. (lookup is gated separately by LookupColumnSafety;
    slice_only sources by the export-wide slice-only surface.)

    Args:
        col_decl: The column declaration.
        table_decl: The output table declaration (gate applies iff
            scd: type2; also used for error messages).

    Raises:
        ExportError: The column uses an unsupported mode on an scd: type2
            table.
    """
```

Error message: the design doc § Validation Rules `Scd2ColumnModeSupported`
row, verbatim.

#### `check_scd2_derived_source_constant` (deleted)

Delete the function and its sole call site (`validate_table`,
validation.py:1714–1716). No shim, no alias, no removal comment. Tests
asserting its refusals move to asserting the new admissibility matrix
(legal tracked renderings; slice-only refusals still carried by
`check_slice_only_column_reads`).

## Phases

### Phase 1: Per-version value renderings on type2

**Delivers:** The whole design — widened mode gate, deleted constant-source
gate, per-version derived compile, reshaped builders, migrated and new tests.

**Demo:** Builds a small emit with a tracked DOUBLE property carrying
float64-noisy per-version values, exports an `scd: type2` dim with
`derived: decimal` and `derived: value_map` columns, and prints the version
rows — showing per-version rendered values with version count and
`valid_from` / `valid_to` identical to the unrendered export. Also shows
`derived: ordinal` on the same table still refused with the updated message.

**Contracts:** All of § Contracts.

**Steps:** `source → migrate (fan-out, 2 files) → author (2 files) → author
(1 new file)` — atomic: the builder reshape and validator deletion leave
`test_columns.py` / `test_scd.py` / `test_validation.py` red until migrated,
so source and tests must land in one gated phase.

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/dimensional/validation.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/scd.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/columns.py` |
| Modify | `tests/exporters/dimensional/test_columns.py` |
| Modify | `tests/exporters/dimensional/test_scd.py` |
| Modify | `tests/exporters/dimensional/test_validation.py` |
| Modify | `tests/exporters/dimensional/test_scd2_source_filter.py` |
| Create | `tests/exporters/dimensional/test_scd2_renderings.py` |
| Create | `docs/sprints/scd2-per-version-renderings/demos/phase_1_per_version_renderings.py` |

**Tests:**

Migrated mechanically (`test_columns.py`, `test_scd.py`) — intent preserved:

- Every direct call to the five reshaped builders passes a qualified source
  expression (`'"_grain"."<src>"'`-shaped) and, where applicable,
  `table_decl.name` as the label; assertions unchanged.

Rewritten (`test_validation.py`):

- The `Scd2DerivedSourceConstant` block (fixture `_scd2_derived_source_sidecar`
  usage sites + the eight tests around lines 912–1126) and the import are
  deleted; replacement coverage lands in its place:
  - A `derived: date_parse` over a tracked prop on a type2 table passes
    `validate_table` (previously refused).
  - A non-exempt `slice_only` derived source on a type2 table is still
    refused — by the slice-only surface (`check_slice_only_column_reads`),
    asserting its existing message, at the `validate_table` level.
- The "type2 derived columns run the records-grain column gates" section
  gains decimal/json_precision cases: a `derived: decimal` over a non-DOUBLE
  tracked source and a `derived: json_precision` over a non-VARCHAR tracked
  source on type2 fail `DecimalSourceIsDouble` / `JsonPrecisionSourceIsVarchar`
  with their existing messages.

Rewritten (`test_scd2_source_filter.py`):

- `decimal` and `json_precision` move from `_UNSUPPORTED_MODE_COLUMNS` to
  `_SUPPORTED_MODE_COLUMNS` (ids updated); refusal tests keep passing
  against the updated message; docstrings referencing
  `Scd2DerivedSourceConstant` are rewritten.

New (`tests/exporters/dimensional/test_scd2_renderings.py`) — per-version
evaluation per the design doc § Per-version evaluation, end-to-end through
`export` against fixture emits:

- `derived: decimal` over a tracked DOUBLE prop with noisy values: each
  version row carries the rounded `DECIMAL(p, s)` value; version count and
  `valid_from` / `valid_to` are identical to the same table exported with
  `from` instead (version structure is election-invariant).
- Adjacent versions whose rendered values collide (e.g. 4.801 / 4.804 →
  4.80): both version rows emitted with identical rendered values.
- Pre-first-assignment version (genesis-null tracked prop): rendered value
  is `NULL` of the output type.
- Tracked prop that never changed post-creation: one version row, rendered
  once.
- Decimal overflow in a *historical* (non-latest) version's value: loud
  export-time error naming table, column, and offending value.
- `derived: value_map` over a tracked code prop: per-version mapped values;
  an unmapped historical value renders typed `NULL`.
- `derived: date_parse` over a tracked VARCHAR date prop: per-version parsed
  values; a historical value that fails the declared format fails the export
  loudly.
- `derived: timestamp` over a tracked BIGINT sim-instant payload prop:
  per-version anchored rendering; with no resolved anchor the unelected
  shorthand renders raw ns per version.
- `derived: json_precision` over a tracked VARCHAR JSON payload prop: the
  named leaf is rounded per version, every other byte preserved; invalid
  JSON in a historical version fails loudly.
- Source-class-blind: a `derived: decimal` over a *constant* (untracked)
  DOUBLE prop on type2 renders per record, byte-identical to the same
  election on a records-grain fact over the same emit.
- Exempt sub-typed discriminator: a `derived: value_map` over
  `prop__<K>_type` (non-empty `subtype_values`) on a type2 table is legal
  and renders per record from the current classification value.
- Windowed: `build_scd2_rows_sql` path (incremental export of a type2 dim
  with a rendered tracked column) emits per-version rendered values inside
  each window; `__valid_from_ns` and window membership read raw bounds.

Existing tests that must still pass: the rest of the dimensional suite
(`test_scd.py` reconstruction and election tests, `test_lookup.py`'s type2
lookup refusal, `test_windowed*.py`, `test_export_dimensional.py`) and the
full `make test` suite.

## What Doesn't Change

- Config grammar: the `derived` one-of and every spec model
  (`DecimalSpec`, `JsonPrecisionSpec`, timestamp / date_parse / value_map
  spellings) — this sprint changes where specs are legal, not their shape.
- The `render_*` authorities in `_sql.py` and `render_anchor_temporal_expr` —
  signatures, tie rules, loud-error contracts, pinned text forms.
- `build_versioned_intervals_sql` and the interval primitive's contract —
  renderings compose above it in mode compile.
- Non-type2 grains' election surfaces (records, `history_point`,
  `history_interval`, membership) — in particular `history_interval`'s
  `value` column keeps its codec VARCHAR type.
- `fk` / `correlation` / `derived: ordinal` / `derived: elapsed` refusals on
  type2 (updated message text only) and `lookup`'s `LookupColumnSafety` gate.
- The slice-only surface (`check_slice_only_column_reads`) and the
  declared-type gates (`check_decimal_source_column`,
  `check_json_precision_source_column`, `check_date_parse_source_column`,
  `check_timestamp_source_available`) — contract-unchanged; reach widens
  only because the mode gate no longer refuses first.
- `_collect_tracked_props`, `resolve_source_column_type`,
  `build_scd2_view_sql`, `build_column_expr`'s contract.
- Reader, derivations, conformance, corrupters, compare, writers,
  incremental driver, streaming, source, base — no contract moves.

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/exporters/dimensional/validation.py` | Widen `check_scd2_column_mode_supported` (admit decimal/json_precision; new message); delete `check_scd2_derived_source_constant` + its call site |
| `src/fabulexa_forge/exporters/dimensional/scd.py` | Per-version derived compile in `build_scd2_column_expr_flag` (`tracked_props` param, cast-then-authority); new `_column_source_name`; `build_scd2_sql` / `build_scd2_rows_sql` drop inline trackedness resolution |
| `src/fabulexa_forge/exporters/dimensional/columns.py` | Five pure per-row builders take `source_expr` (+ `table_label` for date_parse/decimal/json_precision); `build_column_expr` dispatch composes the expression |
| `tests/exporters/dimensional/test_columns.py` | Mechanical migration to the reshaped builder signatures |
| `tests/exporters/dimensional/test_scd.py` | Mechanical migration to the reshaped builder signatures |
| `tests/exporters/dimensional/test_validation.py` | Delete `Scd2DerivedSourceConstant` block; add tracked-legal / slice-only-still-refused / widened-gate cases |
| `tests/exporters/dimensional/test_scd2_source_filter.py` | Move decimal/json_precision to the supported params; updated message + docstrings |
| `tests/exporters/dimensional/test_scd2_renderings.py` | New per-version rendering suite (created) |
| `docs/sprints/scd2-per-version-renderings/demos/phase_1_per_version_renderings.py` | Phase demo (created) |
