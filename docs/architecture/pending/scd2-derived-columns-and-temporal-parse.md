---
status: draft
---

# SCD-2 Derived Columns and the Temporal Parse Family

Two extensions to the temporal-election surface: (1) the dimensional mode's
`scd: type2` tables gain the per-record derived column modes (`timestamp`,
`date_parse`, `value_map`); (2) the declared parse generalizes from date-only
to the instant-string family — a declared format now denotes `DATE`, `TIME`,
or naive `TIMESTAMP`, in every mode that carries a `date_parse` attach point.

---

## Problem

Two gaps leave obviously-temporal values stuck in wrong types:

**1. `scd: type2` dims refuse every derived column.** The type2 builder's
representation step projects static (untracked) columns as plain
per-source-type casts off the composed records relation; the general
derived-column compiler is never threaded in. The `Scd2ColumnModeSupported`
gate therefore refuses `derived: timestamp`, `date_parse`, `elapsed`,
`ordinal`, and `value_map` (plus `fk` / `correlation`) on any type2 table's
columns — correctly, since accepting them would render `NULL` on every row.
Concrete hit: a `dim_customer.birth_date` sourced from a clean VARCHAR
`%Y-%m-%d` property — the textbook `date_parse` case — cannot be a `DATE`
because `dim_customer` tracks history:

```
ExportError: column 'birth_date' on table 'dim_customer':
derived: date_parse is not supported on an scd: type2 table
```

The workaround (elect on a source/base export instead, leave the dimensional
column VARCHAR) abandons the dimensional shape's own typing.

**2. The declared parse is date-only, in every mode.** The closed directive
set is `%Y/%y/%m/%b/%B/%d/%%` — no time-of-day directives exist. A VARCHAR
`"2026-08-17 14:30:00"` payload column has no path to `TIMESTAMP`, a
`"14:30"` string no path to `TIME`, in any mode. Same author experience as
`birth_date`, one type over.

## Solution

**Fix 1 — extend the type2 column surface to per-record derived modes.**
Scoping principle: a column mode is legal on a type2 table iff it is a
**pure per-record function of the static projectable surface** — no
cross-row reads, no per-version semantics. That admits `derived: timestamp`
(structural instants join per record), `derived: date_parse`, and
`derived: value_map` (each reads one source column in place), sourced from
untracked columns and structural instants only. The type2 representation
step compiles these through the same column compiler the records grain uses,
bound to the composed records relation it already joins — one compiler, one
more consumer. `fk`, `correlation`, `ordinal`, and `elapsed` stay refused:
each has genuine version-grain semantics questions (history-aware edge
resolution, what an ordinal partitions over on version rows) that are their
own future design, not unwired plumbing.

**Fix 2 — generalize the declared parse to the instant-string family.** The
closed directive set gains time-of-day directives; the parse's output type —
its **denoted type** — is derived from the directives the author declared:
complete date → `DATE`, complete date + time → naive `TIMESTAMP`, time-only
→ `TIME`. The declaration is still the election; no new config key, no `as:`
knob, no zone directives. Everything else about the parse — loud mismatch
error, `NULL` passthrough, VARCHAR-source rule, `slice_only` refusal, one
shared renderer — carries over unchanged. Every existing `date_parse` attach
point (dimensional derived columns, the source mode's state- and
junction-table maps, base per-table render declarations) accepts the wider
formats with no grammar change. The event log stays parse-free: its one
temporal surface is the `render` map on `event_sim_time`.

```yaml
# scd: type2 dim — now legal
- name: birth_date
  derived: { date_parse: { from: prop__birth_date, format: "%Y-%m-%d" } }
# any mode — now legal, denotes TIMESTAMP
last_login: { from: prop__last_login_at, format: "%Y-%m-%d %H:%M:%S" }
```

## Affected Subsystems

- **Temporal elections (config grammar + shared SQL renderer)** — the
  declared-parse contract widens: the format vocabulary gains time
  directives, the parse gains a denoted type (`DATE` / `TIME` /
  `TIMESTAMP`) derived from the format by a single authority function, and
  the shared renderer emits the denoted type. The election vocabulary's
  "date parse — always `DATE`" clause is replaced by the family rule.
- **Dimensional exporter** — the type2 column-mode gate relaxes to admit
  `derived: timestamp` / `date_parse` / `value_map`; the type2
  representation step compiles those specs through the records-grain column
  compiler over the already-joined records relation; a new business rule
  refuses a derived spec whose source is a history-tracked property.
- **Source and base exporters** — consumers only: their `date_parse` maps
  accept the wider format vocabulary and emit the denoted type. No config
  shape or attach-point change.

## What Doesn't Change

- **The closed-surface posture.** Still no general cast grammar. The parse
  family covers date, time-of-day, and naive datetime strings only — no
  zone directives (`%z` / `%Z`), no `timestamptz` denotation, no numeric
  (non-VARCHAR) parse sources.
- **Parse failure semantics.** `NULL` source → `NULL`; a non-matching
  non-`NULL` value fails the export loudly, naming table, column, and
  value; never a silent `NULL`.
- **Parse source rules.** The source must carry a declared VARCHAR type,
  resolved per each mode's addressing convention, and must not be
  `slice_only` (`DateParseSourceColumn` unchanged).
- **The type2 exclusions that remain.** `fk`, `correlation`,
  `derived: ordinal`, `derived: elapsed` are still refused on type2 tables;
  the `lookup` gate is untouched and separate.
- **SCD-2 reconstruction.** Versioned intervals, the flag-authoritative
  tracked/static split, `scd_window` and its object-form election, and the
  `Scd2NeedsHistory` refusal are all unchanged.
- **Ordering doctrine.** Every table's total order stays over raw sim-time
  keys and identity. Parse columns of any denoted type are never ordinal
  amendment columns and never incremental window keys; they order by value,
  `record_id` tie-broken, as today.
- **Anchor resolution and instant elections.** Precedence, DST/ambiguity
  rules, `TemporalRenderRequiresAnchor`, and the instant-election
  vocabulary (`timestamp` / `date` / `time` / `timestamptz`) are untouched.
- **Writers.** The pinned text forms for `DATE`, `TIME`, and `TIMESTAMP`
  already exist for elected columns; parsed values serialize through them
  identically. No writer contract change.
- **Streaming.** Still carries no elections and no parse.
- **`init`.** No proposal engine proposes a parse or a type2 derived
  column; proposed configs keep default renderings only.

## Semantics

### The parse's denoted type

The format's directive vocabulary splits into two classes plus literals:

| Class | Directives | Meaning |
|---|---|---|
| date | `%Y` `%y` (year) · `%m` `%b` `%B` (month) · `%d` (day) | calendar date |
| time | `%H` (24h hour) · `%I` (12h hour) · `%p` (AM/PM) · `%M` (minute) · `%S` (second) · `%f` (µs fraction) · `%g` (ms fraction) | time of day |
| literal | `%%`, arbitrary literal text | matched verbatim |

**Completeness.** A format's date part is complete iff it carries at least
one year directive, one month directive, and `%d` (the existing rule). Its
time part is complete iff it carries an hour directive — `%H`, or `%I`
paired with `%p`.

**Pairing.** `%I` and `%p` each require the other; `%M` requires an hour
directive; `%S` requires `%M`; `%f` / `%g` require `%S`. A lower-order
directive absent from the format parses as zero — `strptime`'s own
semantics, the parse function's definition, not a forge default.

**Uniqueness.** Each temporal field appears at most once: no directive
repeats, and the alternative forms of one field are mutually exclusive
(`%Y`/`%y`, `%m`/`%b`/`%B`, `%H`/`%I`, `%f`/`%g`). A duplicated or
conflicting directive is a load-time error. This is what keeps the value-
preservation round trip (§ Parse behavior) unconditional — a format whose
directives could disagree about one field has no single denoted value.

**Denotation** is a pure function of the validated format:

| Format carries | Denoted type |
|---|---|
| complete date, no time-class directives | `DATE` |
| complete date + complete time | naive `TIMESTAMP` (µs) |
| complete time, no date-class directives | `TIME` (µs) |
| anything else — partial date, partial time, orphaned pairing, duplicated or conflicting directive, unsupported directive | load-time config error naming the format and the violated rule |

One function (`date_parse_denoted_type`) is the sole authority for this
derivation; the renderer and every type-reading consumer resolve the
denoted type through it, never by re-inspecting the format string.

### Parse behavior (all denoted types)

| Condition | Result |
|---|---|
| Source value matches the declared format | The denoted-type value it denotes |
| Source value is `NULL` | `NULL` of the denoted type |
| Source value does not match | Loud export-time error naming table, column, and offending value |
| Format invalid (per denotation table) | Load-time config error; nothing runs |

- **No anchor, ever.** A parse never consults the effective anchor — the
  string was anchored upstream when minted. This holds for the `TIMESTAMP`
  denotation exactly as for `DATE`: a parsed naive `TIMESTAMP` is the
  string's own wall clock, not a rendered instant. The two `TIMESTAMP`
  producers (instant election, declared parse) stay distinct by
  declaration: an election reads a sim-time column and requires an anchor;
  a parse reads a VARCHAR column and ignores the anchor.
- **Zone-naive.** Parsed values carry no zone and no DST posture; parsing
  is independent of the tz database version — unlike local-instant
  renderings, a parse is deterministic with no tzdata qualifier.
- **Precision.** `TIMESTAMP` and `TIME` denotations truncate to µs, the
  family-wide presentation rule; `%g` milliseconds widen exactly to µs.
- **Value preservation.** A parsed value round-trips to its source string
  under the declared format (zero-fill of absent lower-order fields
  included) — the testable faithfulness statement, extending the existing
  `DATE` round-trip invariant to the family.

### Derived columns on `scd: type2` tables

The type2 column-mode surface becomes:

| Column mode on a type2 table | Status | Value semantics |
|---|---|---|
| `from` (tracked property) | unchanged | per-version, from the versioned-intervals derivation |
| `from` (static column), `null` | unchanged | per-record, from the composed records relation |
| `derived: scd_window` (bare or object form) | unchanged | version bounds, optionally elected |
| `derived: timestamp`, source a structural instant or untracked time-valued property | **now legal** | per-record; rendered through the shared anchor renderer, identical expression to the records grain |
| `derived: date_parse`, source an untracked VARCHAR property | **now legal** | per-record; denoted-type parse, identical expression to the records grain |
| `derived: value_map`, source an untracked column | **now legal** | per-record; typed-from-map `CASE`, identical expression to the records grain |
| `derived: timestamp` / `date_parse` / `value_map` with a history-tracked source | refused (`Scd2DerivedSourceUntracked`) | a tracked property's value surface is per-version; deriving from it is undesigned |
| `fk`, `correlation`, `derived: ordinal`, `derived: elapsed` | refused (`Scd2ColumnModeSupported`, as today) | version-grain semantics undesigned |

Rules:

- **Per-version constancy.** Every newly-admitted derived column is a pure
  per-record function of static values, so its value is constant across one
  record's version rows. This is an invariant, not an accident: it is what
  makes the modes admissible without designing per-version semantics.
- **One compiler.** A derived column on a type2 table compiles through the
  same column compiler as on the records grain, bound to the type2 build's
  composed records-relation alias. Same SQL expression modulo alias — the
  byte-identity claim that keeps election semantics (anchor requirement,
  DST posture, precision, mismatch errors) uniform across grains. The type2
  surface introduces no new election site; it joins existing ones.
- **Timestamp sources.** A type2 dim is a records-grain table; its
  `timestamp.source` domain is the records-grain domain — the records
  category's instant-carrying structural columns (resolved through the
  reader's structural-temporal surface) plus untracked time-valued
  properties. A `NULL` `deactivated_at` renders a `NULL` timestamp, as on
  the records grain. `TimestampSourceAvailable` applies unchanged.
- **Anchor interaction.** Default (unelected) `derived: timestamp` with no
  resolved anchor renders the raw ns integer; any explicit election with no
  anchor is refused at validation (`TemporalRenderRequiresAnchor`). Both
  unchanged postures, now reachable from type2 columns.
- **Tracked-source refusal is per-source.** The rule reads
  `ColumnSpec.history_tracked` for the derived spec's source
  (`timestamp.source`, `date_parse.from`, `value_map.from`); a
  projection-introduced or structural source is never tracked. The error
  names the column, the source, and the fact that type2 derived columns
  read static values only.

### Interaction with existing features

| Surface | Interaction |
|---|---|
| Incremental export | A parse column (any denoted type) never names a window key; window membership and the fingerprint treat parse config as ordinary config content. Unchanged. |
| Ordinal amendment | Parse columns are never amendment columns (their source is not a raw-ns instant); they order by value. Type2 refuses `ordinal` anyway. Unchanged. |
| Corrupter composition | A `mutate_cells`-corrupted datetime string fails the parse loudly; a `null_cells` defect flows through as `NULL`. Unchanged posture, wider family. |
| `slice_only` | A parse source joins the refusal surface regardless of denoted type. Unchanged. |
| Key election | Parse columns and type2 derived columns are value columns, never identity surfaces. Unchanged. |
| `history_interval` grain `value` parse | Participates under the same rules with the wider formats; no special case. |

## Configuration

No new keys and no shape change. The two existing `date_parse` shapes carry
the wider format vocabulary as-is: the dimensional spec form
(`date_parse: {from, format}`) and the source/base map form (source column →
format string); the existing `derived:` specs on type2 columns.

```yaml
# dimensional — scd: type2 dim with the newly-admitted modes
- name: dim_customer
  role: dim
  source: { grain: records, kind: customer }
  scd: type2
  key: [customer_key, valid_from]
  columns:
    - name: customer_key
      from: record_id
    - name: tier                          # tracked — per-version, as today
      from: prop__tier
    - name: birth_date                    # untracked VARCHAR → DATE
      derived: { date_parse: { from: prop__birth_date, format: "%Y-%m-%d" } }
    - name: signed_up_at                  # structural instant, elected
      derived: { timestamp: { source: created_sim_time, as: timestamptz } }
    - name: region_name                   # untracked code → label
      derived: { value_map: { from: prop__region, map: { n: North, s: South } } }
    - name: valid_from
      derived: { scd_window: valid_from }
    - name: valid_to
      derived: { scd_window: valid_to }
```

```yaml
# source mode — declared-table date_parse map, TIMESTAMP denotation
tables:
  - name: customers
    population: customer
    date_parse:
      prop__last_login_at: "%Y-%m-%d %H:%M:%S"   # → TIMESTAMP

# base mode — per-table render declaration, TIME denotation
render:
  - table: clinic
    date_parse:
      prop__opening_time: "%H:%M"                # → TIME
```

| Field | Type | Required | Description |
|---|---|---|---|
| `date_parse.from` | str | yes (dimensional spec form) | The VARCHAR source column (mode-resolved; map forms key on the column instead) |
| `date_parse.format` | str | yes | The declared parse format (the map forms carry it as the entry's value); its directives determine the denoted type (`DATE` / `TIME` / `TIMESTAMP`) |

## Interface Contracts

### Config Models

```python
class DateParseSpec(StrictBaseModel):
    """A declared reinterpretation of a VARCHAR source column as its
    format-denoted temporal type (DATE, TIME, or naive TIMESTAMP)."""

    from_: str = Field(alias="from")
    """The VARCHAR source column holding temporal strings (sidecar-validated)."""
    format: str
    """The author-declared parse format (closed strptime-directive set; see
    format_denotes_a_temporal). Must denote a complete date, a complete
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

### Functions

```python
def date_parse_denoted_type(fmt: str) -> Literal["DATE", "TIME", "TIMESTAMP"]:
    """The temporal type a validated date_parse format denotes.

    The single derivation authority: complete date only -> DATE; complete
    date + complete time -> TIMESTAMP; complete time only -> TIME. Every
    consumer of a parse's output type (the renderer, any plan-time typing
    read) resolves through this function; none re-inspects the format.

    Args:
        fmt: A format string that has passed format_denotes_a_temporal.

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
    value — never a silent NULL. The format is assumed validated
    (format_denotes_a_temporal).

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

    Tracked `from` columns project from the versioned-intervals derivation;
    static `from` columns project from the records relation; scd_window
    columns render the version bounds through the anchor renderer. A
    derived timestamp / date_parse / value_map spec — legal only with an
    untracked source (Scd2DerivedSourceUntracked) — compiles through the
    same per-column compiler the records grain uses, bound to
    records_alias, so its expression is identical to the records grain's
    modulo alias.

    Args:
        col_decl: The output column declaration.
        version_alias: Alias of the versioned-intervals derivation subquery.
        records_alias: Alias of the records-relation subquery.
        is_tracked: Whether this column's `from` source is history-tracked.
        anchor: The resolved EffectiveAnchor, or None.
        sidecar: The emit's typed sidecar, for source-column type reads
            (tracked-path casts, value_map literal typing, date_parse
            VARCHAR verification already done at validation).
        source_table_name: The dim's source records table, for sidecar
            column reads.
        table_label: The output table name for renderer error messages.

    Returns:
        A SQL expression fragment: `<expr> AS "<col_name>"`.
    """
```

### Validation Functions

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

## Validation Rules

### Parse-Time (Pydantic)

```python
@model_validator(mode="after")
def format_denotes_a_temporal(self) -> Self:
    """DateParseSpec: from_ non-empty; format uses only the closed
    directive set, satisfies the pairing and uniqueness rules, and is
    date-complete, time-complete, or both."""
```

Replaces `format_denotes_a_date`. Closed set: `%Y %y %m %b %B %d` (date),
`%H %I %p %M %S %f %g` (time), `%%` + literal text. Pairing: `%I` ⇔ `%p`;
`%M` requires an hour directive; `%S` requires `%M`; `%f`/`%g` require
`%S`. Uniqueness: each temporal field at most once (§ The parse's denoted
type). Completeness: date = year + month + `%d`; time = an hour directive.

One rule, every attach point: the source/base map forms validate each
entry's format through the same shared format validator at load time — the
spec form and the map forms enforce the identical closed-set + pairing +
uniqueness + completeness rule.

### Business Rules

| Rule | Checks | Error Message |
|---|---|---|
| `Scd2ColumnModeSupported` (amended) | A type2 column's mode is one of: `from`, `null`, `derived: scd_window`, `derived: timestamp`, `derived: date_parse`, `derived: value_map` | `"column '{column}' on table '{table}': {mode} is not supported on an scd: type2 table"` |
| `Scd2DerivedSourceUntracked` (new) | A type2 derived column's source is not a history-tracked property | `"column '{column}' on scd: type2 table '{table}': derived source '{source}' is history-tracked; derived columns on a type2 table read static values only"` |
| `DateParseSourceColumn` (unchanged) | Parse source resolves per mode convention, carries a declared VARCHAR type, is not `slice_only` | existing message |
| `TemporalRenderRequiresAnchor` (unchanged) | Explicit instant elections have a resolved anchor — now also reachable from type2 `derived: timestamp` / `scd_window` object-form columns | existing message |
| `TimestampSourceAvailable` (unchanged) | `timestamp.source` is in the grain's source domain — applies to type2 dims as records-grain tables | existing message |
