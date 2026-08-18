# Sprint: scd2-derived-temporal-parse

## Purpose

Deliver the two extensions designed in
`docs/architecture/pending/scd2-derived-columns-and-temporal-parse.md`: the
declared parse generalizes from date-only to the instant-string family
(`DATE` / `TIME` / naive `TIMESTAMP`, denoted by the format), and
`scd: type2` tables gain the per-record derived column modes
(`timestamp`, `date_parse`, `value_map`).

An author writes `format: "%Y-%m-%d %H:%M:%S"` in any existing `date_parse`
attach point and gets a `TIMESTAMP` column; an author puts
`derived: { date_parse: ... }` on an `scd: type2` dim column and gets a typed
per-record value instead of a refusal.

The pending doc owns rationale and semantics; this spec owns contracts,
phases, and test cases. Where the two disagree, the doc's Semantics section
governs.

## Scope

**Capabilities touched:**
- Temporal elections: parse-family widening (time directives), the denoted
  type and its single authority function, the shared renderer emitting the
  denoted type
- Dimensional exporter: type2 per-record derived modes
  (`timestamp` / `date_parse` / `value_map`), the `Scd2DerivedSourceUntracked`
  business rule
- Source + base exporters: consumers only — the wider formats flow through the
  existing map forms and the shared renderer with no mode-code change

**Not included:** recipes (post-sprint lifecycle step), `init` proposals for
parses or type2 derived columns, streaming elections, writer changes (pinned
text forms already exist), zone directives / `timestamptz` denotation /
non-VARCHAR parse sources (doc-pinned non-goals), `fk` / `correlation` /
`ordinal` / `elapsed` on type2 (still refused), folding the pending doc
(post-sprint `/fold-pending`).

## Breaking Changes

- **`Scd2ColumnModeSupported` error text changes**: the supported-modes list
  in the message grows to name the three admitted derived modes. Configs that
  were previously refused for `derived: timestamp` / `date_parse` /
  `value_map` on type2 now load and export; configs using the still-refused
  modes get the amended message.
- **`DateParseSpec.format_denotes_a_date` is renamed
  `format_denotes_a_temporal`** and its refusal messages change (the family
  rule replaces "complete calendar date"). Every previously-valid format
  stays valid and keeps its `DATE` denotation; formats carrying time
  directives — previously refused — now load.
- **`build_scd2_column_expr_flag` signature changes** (internal): gains
  `sidecar`, `source_table_name`, `table_label`; drops the defaulted
  `source_col_type` parameter (the function resolves source types from the
  sidecar itself). Callers: `build_scd2_sql` plus tests.
- **`_validate_date_parse_format` moves/widens**: the format-anatomy
  authority (directive classes, well-formedness, denotation) lives in
  `_sql.py`; `config/models.py` delegates to it. Import direction stays
  `models → _sql` (already the case via `is_recognized_sql_type`).

## Success Criteria

- [ ] A VARCHAR `"%Y-%m-%d %H:%M:%S"` payload column exports as naive
      `TIMESTAMP` through all three `date_parse` attach-point shapes
      (dimensional spec form, source map form, base map form)
- [ ] `"%H:%M"` denotes `TIME`; `"%Y-%m-%d"` still denotes `DATE` — no
      behavior change for any previously-valid format
- [ ] Invalid family formats (pairing, uniqueness, completeness violations)
      are refused at config load with errors naming the format and the
      violated rule
- [ ] Parsed values round-trip to their source strings under the declared
      format (zero-fill of absent lower-order fields included)
- [ ] An `scd: type2` dim exports `derived: timestamp` / `date_parse` /
      `value_map` columns from untracked sources, constant across one
      record's version rows, with expressions identical to the records
      grain's modulo alias
- [ ] A type2 derived spec sourcing a history-tracked property is refused
      (`Scd2DerivedSourceUntracked`); `fk` / `correlation` / `ordinal` /
      `elapsed` on type2 stay refused
- [ ] `make check` green; all existing tests pass (migrated where the
      contract changed)

## Contracts

Extracted from the pending doc § Interface Contracts, with module placement
pinned. Signatures and docstrings only; no defaults; no scaffolding.

### `src/fabulexa_forge/_sql.py` — format anatomy + renderer

The closed directive vocabulary, its class constants (date-class,
time-class), well-formedness validation, and denotation all live here — one
module owns the format contract; `config/models.py` imports from it.

```python
def validate_date_parse_format(fmt: str, field_name: str) -> None:
    """A `date_parse` format string denotes a complete temporal value.

    Closed strptime-directive set — date class `%Y`/`%y` (year),
    `%m`/`%b`/`%B` (month), `%d` (day); time class `%H`/`%I` (hour), `%p`
    (AM/PM), `%M` (minute), `%S` (second), `%f` (µs), `%g` (ms); `%%`
    (literal `%`) plus arbitrary literal text. Pairing: `%I` and `%p` each
    require the other; `%M` requires an hour directive; `%S` requires `%M`;
    `%f`/`%g` require `%S`. Uniqueness: each temporal field at most once —
    no repeated directive, no two alternative forms of one field
    (`%Y`/`%y`, `%m`/`%b`/`%B`, `%H`/`%I`, `%f`/`%g`). Completeness: the
    format must be date-complete (a year directive + a month directive +
    `%d`), time-complete (an hour directive — `%H`, or `%I` with `%p`), or
    both.

    Args:
        fmt: The author-declared format string.
        field_name: The field's dotted name, for the error message.

    Raises:
        ValueError: `fmt` is empty, contains a malformed or unsupported `%`
            directive, violates a pairing or uniqueness rule, or is neither
            date-complete nor time-complete. The message names the format
            and the violated rule.
    """
```

```python
def date_parse_denoted_type(fmt: str) -> Literal["DATE", "TIME", "TIMESTAMP"]:
    """The temporal type a validated date_parse format denotes.

    The single derivation authority: complete date only -> DATE; complete
    date + complete time -> TIMESTAMP; complete time only -> TIME. Every
    consumer of a parse's output type (the renderer, any plan-time typing
    read) resolves through this function; none re-inspects the format.

    Args:
        fmt: A format string that has passed validate_date_parse_format.

    Returns:
        The denoted DuckDB type name.
    """
```

```python
def render_date_parse_expr(
    qualified_source: str,
    date_format: str,
    out_name: str,
    table_label: str,
) -> str:
    """Render the SQL SELECT fragment reinterpreting a VARCHAR column as its
    format-denoted temporal type under an author-declared format.

    Lives in the shared SQL utilities — every mode renders a declared parse
    through this one function. The output type is the format's denoted type
    (date_parse_denoted_type). NULL source values yield NULL of that type.
    A non-NULL value not matching the format fails the export loudly at
    query time, naming table_label, the source column, and the offending
    value — never a silent NULL. TIMESTAMP and TIME denotations truncate to
    µs (the family-wide presentation rule; `%g` milliseconds widen exactly
    to µs). The format is assumed validated (validate_date_parse_format).

    Args:
        qualified_source: The fully table-qualified VARCHAR source column SQL.
        date_format: The author-declared strptime-style format.
        out_name: The output column name (the `AS "<out_name>"` alias).
        table_label: The output table name interpolated into the guard's
            error message.

    Returns:
        A SQL SELECT-list expression fragment ending in `AS "<out_name>"`,
        typed as the format's denoted type.
    """
```

### `src/fabulexa_forge/config/models.py` — the widened validator

`_validate_date_parse_format` is deleted; `DateParseSpec` and
`_require_date_parse_map_valid` call `_sql.validate_date_parse_format`. The
map-form helper's docstring updates to the family rule; its shape is
unchanged.

```python
class DateParseSpec(StrictBaseModel):
    """A declared reinterpretation of a VARCHAR source column as its
    format-denoted temporal type (DATE, TIME, or naive TIMESTAMP)."""

    from_: str = Field(alias="from")
    """The VARCHAR source column holding temporal strings (sidecar-validated)."""
    format: str
    """The author-declared parse format (closed strptime-directive set; see
    validate_date_parse_format). Must denote a complete date, a complete
    time, or both; validated at load time, never defaulted. The format is
    the election — the denoted type is derived from it, never declared
    separately."""

    @model_validator(mode="after")
    def format_denotes_a_temporal(self) -> Self:
        """`from_` is non-empty; `format` denotes a complete temporal value.

        Raises:
            ValueError: `from_` is empty, or `format` is empty, uses a
                directive outside the closed set, violates a pairing rule
                (%I⇔%p, %M needs an hour, %S needs %M, %f/%g need %S),
                duplicates a temporal field (a repeated directive, or two
                alternative forms of one field), or is neither
                date-complete nor time-complete.
        """
```

### `src/fabulexa_forge/exporters/dimensional/scd.py` — the type2 build

```python
def build_scd2_column_expr_flag(
    col_decl: "ColumnDecl",
    version_alias: str,
    records_alias: str,
    is_tracked: bool,
    anchor: "EffectiveAnchor | None",
    sidecar: "Sidecar",
    source_table_name: str,
    table_label: str,
) -> str:
    """Build a SQL expression for one SCD-2 column.

    Tracked `from` columns project from the versioned-intervals derivation
    (cast to the source column's sidecar type); static `from` columns
    project from the records relation; scd_window columns render the
    version bounds through the anchor renderer. A derived timestamp /
    date_parse / value_map spec — legal only with an untracked source
    (Scd2DerivedSourceUntracked) — compiles through the same per-column
    builders the records grain uses (build_timestamp_expr /
    build_date_parse_expr / build_value_map_expr), bound to records_alias,
    so its expression is identical to the records grain's modulo alias.

    Args:
        col_decl: The output column declaration.
        version_alias: Alias of the versioned-intervals derivation subquery.
        records_alias: Alias of the records-relation subquery.
        is_tracked: Whether this column's `from` source is history-tracked.
        anchor: The resolved EffectiveAnchor, or None.
        sidecar: The emit's typed sidecar, for source-column type reads
            (tracked-path casts, value_map literal typing).
        source_table_name: The dim's source records table, for sidecar
            column reads.
        table_label: The output table name for renderer error messages.

    Returns:
        A SQL expression fragment: `<expr> AS "<col_name>"`.
    """
```

`build_scd2_sql` threads the new arguments (it already holds `sidecar`,
`source_table_name`, and `table_decl.name`); its own signature is unchanged.
Its docstring gains the derived-modes sentence.

### `src/fabulexa_forge/exporters/dimensional/validation.py` — the gates

```python
def check_scd2_column_mode_supported(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
) -> None:
    """Enforce Scd2ColumnModeSupported: type2 columns use supported modes.

    The type2 surface admits from, null, derived: scd_window, and the
    per-record derived modes timestamp / date_parse / value_map. It refuses
    fk, correlation, derived: ordinal, and derived: elapsed — cross-row or
    per-version semantics the type2 build does not define. (lookup is gated
    separately by LookupColumnSafety; derived sources are additionally
    gated by Scd2DerivedSourceUntracked.)

    Args:
        col_decl: The column declaration.
        table_decl: The output table declaration (gate applies iff
            scd: type2; also used for error messages).

    Raises:
        ExportError: The column uses an unsupported mode on an scd: type2
            table.
    """
```

```python
def check_scd2_derived_source_untracked(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    sidecar: "Sidecar",
    source_table_name: str,
) -> None:
    """Enforce Scd2DerivedSourceUntracked: type2 derived sources are static.

    A derived timestamp / date_parse / value_map column on an scd: type2
    table must source an untracked column: the spec's source
    (timestamp.source / date_parse.from / value_map.from) must not name a
    prop__ column whose ColumnSpec.history_tracked is True. Structural and
    projection-introduced sources are never tracked and always pass.

    Args:
        col_decl: The column declaration (no-op unless it carries one of
            the three derived specs and the table is scd: type2).
        table_decl: The output table declaration.
        sidecar: The emit's typed sidecar.
        source_table_name: The dim's source records table.

    Raises:
        ExportError: The derived spec sources a history-tracked property.
    """
```

Wiring: `check_scd2_derived_source_untracked` joins the per-column loop in
`validate_table` (beside `check_scd2_column_mode_supported`). The existing
per-column checks (`TimestampSourceAvailable`, `DateParseSourceColumn`,
`TemporalRenderRequiresAnchor`, slice-only reads) already run for type2
tables and apply to the newly-admitted modes unchanged.

Error messages (pending doc § Business Rules):

| Rule | Message |
|---|---|
| `Scd2ColumnModeSupported` (amended) | `"column '{column}' on table '{table}': {mode} is not supported on an scd: type2 table"` (message body lists the supported modes) |
| `Scd2DerivedSourceUntracked` (new) | `"column '{column}' on scd: type2 table '{table}': derived source '{source}' is history-tracked; derived columns on a type2 table read static values only"` |

## Phases

### Phase 1: Parse family — widened formats, denoted type, shared renderer

**Delivers:** The instant-string parse family in every existing attach point:
widened closed directive set with pairing/uniqueness/completeness rules,
`date_parse_denoted_type` as the single denotation authority, and
`render_date_parse_expr` emitting the denoted type. Source and base modes
pick the family up with zero mode-code change (their map forms already
validate through the shared validator and render through the shared
renderer).

**Demo:** `demos/phase_1_parse_family.py` — builds a minimal fixture emit
inline (duckdb + schema-conformant `base.json`, the prior-sprint demo
pattern), then: (1) a base export with `date_parse` entries denoting
`TIMESTAMP` (`"%Y-%m-%d %H:%M:%S"`), `TIME` (`"%H:%M"`), and `DATE`
(`"%Y-%m-%d"`) — prints output column types and values; (2) a dimensional
spec-form parse denoting `TIMESTAMP`; (3) load-time refusals: an orphaned
`%I`, a `%M` with no hour, a duplicated field (`%H` + `%I`/`%p`), a
partial-date-plus-time format; (4) the loud mismatch error naming table,
column, and value.

**Contracts:** `validate_date_parse_format`, `date_parse_denoted_type`,
`render_date_parse_expr`, `DateParseSpec.format_denotes_a_temporal`.

**Steps:** `source → author (unit tests, 2 files) → author (mode
flow-through tests, 5 files)` — the format-rule matrix and the source reshape
read the same directive-anatomy surface; each step gets a fresh context.

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/_sql.py` |
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `tests/config/test_models.py` |
| Modify | `tests/test_sql.py` |
| Modify | `tests/exporters/dimensional/test_columns.py` |
| Modify | `tests/exporters/source/test_renders.py` |
| Modify | `tests/exporters/base/test_renders.py` |
| Modify | `tests/config/test_source_decls.py` |
| Modify | `tests/config/test_base_config.py` |
| Create | `docs/sprints/scd2-derived-temporal-parse/demos/phase_1_parse_family.py` |

**Tests:**

Format validation (`tests/config/test_models.py`):
- `"%Y-%m-%d %H:%M:%S"` parses (denotes TIMESTAMP); `"%H:%M"` parses
  (TIME); `"%I:%M %p"` parses; `"%H:%M:%S.%f"` and `"%H:%M:%S.%g"` parse
- Every previously-valid date-only format still parses (existing
  parametrized cases stay green)
- Pairing refusals, each naming the rule: `"%I:%M"` (orphaned `%I`),
  `"%Y-%m-%d %p"` (orphaned `%p`), `"%Y-%m-%d %M"` (`%M` needs an hour),
  `"%H:%S"` (`%S` needs `%M`), `"%H:%M.%f"` (`%f` needs `%S`)
- Uniqueness refusals: `"%Y-%m-%d %Y"` (repeat), `"%Y %y %m %d"`
  (alternative year forms), `"%H %I %p"` (alternative hour forms),
  `"%H:%M:%S.%f%g"` (alternative fraction forms)
- Completeness refusals: `"%m-%d %H:%M"` (partial date + time),
  `"%Y-%m"` (partial date, unchanged), `"%M:%S"` (time with no hour)
- Locale/zone directives still refused: `"%x"`, `"%A"`, `"%z"`, `"%Z"`
- Existing `%H`/`%M`/`%S` "unsupported directive" cases rewritten: those
  directives are now in the closed set — the refusal (if any) comes from
  pairing/completeness instead
- Map forms (`_require_date_parse_map_valid`): a map entry with a
  TIMESTAMP-denoting format is accepted; an entry violating a family rule
  is refused with the entry-keyed field name

Denotation + renderer (`tests/test_sql.py`):
- `date_parse_denoted_type`: date-only → `"DATE"`, date+time →
  `"TIMESTAMP"`, time-only → `"TIME"` (parametrized across directive
  variants incl. `%I`+`%p` and `%b`/`%B`)
- Renderer emits the denoted type: fragment for a datetime format yields a
  TIMESTAMP-typed column; time format yields TIME; date format yields DATE
  (existing DATE tests unchanged)
- NULL source yields NULL of the denoted type (all three)
- Mismatch error names table, column, and offending value (all three
  denotations)
- Value preservation: parsed value round-trips to the source string under
  the declared format; zero-fill — `"2026-08-17 14:30"` under
  `"%Y-%m-%d %H:%M"` has seconds 0 and round-trips
- `%g` milliseconds widen exactly to µs; `%f` parses at µs

Mode flow-through:
- `tests/exporters/dimensional/test_columns.py`: `build_date_parse_expr`
  with a datetime format produces the TIMESTAMP-denoting fragment
- `tests/exporters/source/test_renders.py`: a declared-table `date_parse`
  map entry with `"%Y-%m-%d %H:%M:%S"` exports a TIMESTAMP column
  end-to-end
- `tests/exporters/base/test_renders.py`: a base render declaration with
  `"%H:%M"` exports a TIME column end-to-end
- `tests/config/test_source_decls.py` / `tests/config/test_base_config.py`:
  map-form configs carrying family formats load; any existing assertions on
  the old "complete calendar date" message text updated
- Existing tests that must still pass: all current `date_parse` tests in
  all eleven files that exercise it (DATE behavior is unchanged)

### Phase 2: SCD-2 per-record derived columns

**Delivers:** `scd: type2` tables accept `derived: timestamp` /
`date_parse` / `value_map` from untracked sources, compiled through the
records-grain column builders bound to the type2 build's records relation;
the `Scd2DerivedSourceUntracked` rule; the amended
`Scd2ColumnModeSupported` gate.

**Demo:** `demos/phase_2_scd2_derived.py` — builds an scd2 fixture emit
inline (a tracked `prop__tier` with history rows, untracked
`prop__birth_date` VARCHAR and `prop__region`), then: (1) exports a type2
dim declaring a tracked `tier`, `derived: date_parse` on `birth_date`, an
elected `derived: timestamp` on `created_sim_time`, and a
`derived: value_map` on `region` — prints rows for a multi-version record
showing per-version `tier` beside version-constant typed derived values;
(2) two refusals: a derived parse sourcing the tracked `prop__tier`
(`Scd2DerivedSourceUntracked`), and `derived: ordinal` on the type2 table
(amended `Scd2ColumnModeSupported` message).

**Contracts:** `build_scd2_column_expr_flag`,
`check_scd2_column_mode_supported`, `check_scd2_derived_source_untracked`.

**Steps:** `source → author (1 group, 3 files)` — the signature change is
atomic across `test_scd.py`'s existing call sites (suite red between steps
is expected); the same files also take intent-changing gate rewrites and the
new-mode tests, so one author step owns all three files.

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/dimensional/scd.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/validation.py` |
| Modify | `tests/exporters/dimensional/test_scd.py` |
| Modify | `tests/exporters/dimensional/test_scd2_source_filter.py` |
| Modify | `tests/exporters/dimensional/test_validation.py` |
| Create | `docs/sprints/scd2-derived-temporal-parse/demos/phase_2_scd2_derived.py` |

**Tests:**

Builder (`tests/exporters/dimensional/test_scd.py`):
- All existing `build_scd2_column_expr_flag` / `build_scd2_sql` tests
  migrated to the new signature and still passing (tracked cast, static
  projection, scd_window forms, null, anchor variants)
- `derived: date_parse` on an untracked VARCHAR prop exports a DATE column
  on a type2 dim, constant across one record's version rows
- `derived: date_parse` with a datetime format exports TIMESTAMP on a
  type2 dim (composes Phase 1)
- `derived: timestamp` on a structural instant (`created_sim_time`) with
  an anchor and an explicit election renders the elected type on every
  version row; identical value across versions
- Default (unelected) `derived: timestamp` with no anchor renders the raw
  ns integer
- `derived: value_map` on an untracked prop exports the typed CASE value,
  constant across versions
- Expression identity: the type2 derived expression equals the
  records-grain builder's output modulo the grain alias (direct string
  comparison against `build_timestamp_expr` / `build_date_parse_expr` /
  `build_value_map_expr` with `grain_alias="_records"`)

Gates (`tests/exporters/dimensional/test_scd2_source_filter.py`,
`tests/exporters/dimensional/test_validation.py`):
- `check_scd2_column_mode_supported` passes for `derived: timestamp` /
  `date_parse` / `value_map`; still refuses `fk`, `correlation`,
  `derived: ordinal`, `derived: elapsed` with the amended message
  (existing gate tests rewritten — refusal cases for the three admitted
  modes become acceptance cases)
- `check_scd2_derived_source_untracked`: a derived spec sourcing a
  history-tracked `prop__` column raises, message naming column, source,
  and the static-values-only rule; an untracked prop source passes; a
  structural source passes; a non-type2 table is a no-op
- `validate_table` on a type2 dim: an explicit timestamp election with no
  anchor raises `TemporalRenderRequiresAnchor` (now reachable);
  `TimestampSourceAvailable` and `DateParseSourceColumn` fire on type2
  derived columns exactly as on the records grain
- Existing tests that must still pass: the full dimensional suite,
  `check_scd2_needs_history` tests untouched

## What Doesn't Change

- **`fk` / `correlation` / `derived: ordinal` / `derived: elapsed` on
  type2** stay refused — version-grain semantics are undesigned (pending
  doc § Solution). The `lookup` gate (`LookupColumnSafety`) is untouched
  and separate.
- **`check_scd2_needs_history`, versioned intervals, the tracked/static
  split, `scd_window` and its object form** — the SCD-2 reconstruction is
  unchanged.
- **No zone directives (`%z`/`%Z`), no `timestamptz` denotation, no
  non-VARCHAR parse sources** — the closed-surface posture holds.
- **Parse failure semantics and source rules** — loud mismatch error, NULL
  passthrough, VARCHAR-source rule, `slice_only` refusal
  (`DateParseSourceColumn` and `check_slice_only_column_reads` unchanged).
- **A parse never consults the anchor** — `render_date_parse_expr` keeps
  its anchor-free signature; the two TIMESTAMP producers (election, parse)
  stay distinct by declaration.
- **Anchor resolution, instant elections, `render_anchor_temporal_expr`**
  — untouched; type2 derived timestamps render through them as-is.
- **Ordering doctrine** — parse columns are never ordinal amendment
  columns and never incremental window keys; the incremental driver and
  ordinal gates are not modified.
- **Writers** — the pinned temporal text forms already serialize the three
  denoted types; no writer file is touched.
- **Streaming, `init`, the event log** — no elections, no parse, no
  proposal changes.
- **Source and base mode code** — `exporters/source/*` and
  `exporters/base/*` source files are not modified; the family reaches them
  through the shared validator and renderer.

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/_sql.py` | Format-anatomy authority: widened `validate_date_parse_format` (moved in from models), new `date_parse_denoted_type`, `render_date_parse_expr` emits the denoted type |
| `src/fabulexa_forge/config/models.py` | `DateParseSpec` validator renamed `format_denotes_a_temporal`, delegates to `_sql`; `_validate_date_parse_format` deleted; docstrings re-voiced to the family |
| `src/fabulexa_forge/exporters/dimensional/scd.py` | `build_scd2_column_expr_flag` new contract (sidecar-resolved types, derived-mode compilation via records-grain builders); `build_scd2_sql` threads the new args |
| `src/fabulexa_forge/exporters/dimensional/validation.py` | `check_scd2_column_mode_supported` amended; `check_scd2_derived_source_untracked` added and wired into the per-column loop |
| `tests/config/test_models.py` | Format-family matrix; flipped `%H`/`%M`/`%S` cases rewritten |
| `tests/test_sql.py` | Denotation authority, denoted-type rendering, round-trip, µs rules |
| `tests/exporters/dimensional/test_columns.py` | Spec-form flow-through (TIMESTAMP denotation) |
| `tests/exporters/source/test_renders.py` | Map-form flow-through (TIMESTAMP end-to-end) |
| `tests/exporters/base/test_renders.py` | Map-form flow-through (TIME end-to-end) |
| `tests/config/test_source_decls.py` | Family formats load in the source map form; message updates |
| `tests/config/test_base_config.py` | Family formats load in the base map form; message updates |
| `tests/exporters/dimensional/test_scd.py` | Signature migration + new-mode builder tests + expression identity |
| `tests/exporters/dimensional/test_scd2_source_filter.py` | Gate rewrite: admitted modes accepted, refused modes' amended message |
| `tests/exporters/dimensional/test_validation.py` | `Scd2DerivedSourceUntracked` tests; type2 reachability of existing per-column gates |
| `docs/sprints/scd2-derived-temporal-parse/demos/phase_1_parse_family.py` | Phase 1 demo |
| `docs/sprints/scd2-derived-temporal-parse/demos/phase_2_scd2_derived.py` | Phase 2 demo |
