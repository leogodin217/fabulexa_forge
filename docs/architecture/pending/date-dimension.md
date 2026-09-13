---
status: draft
---

# Date Dimension

An opt-in, author-ranged, forge-generated calendar table for the dimensional
mode, and a foreign-key column mode into it.

---

## Problem

The dimensional mode cannot produce a calendar dimension, and no column mode
renders an instant as a key into one. Every example star schema renders its
fact instants as `derived: timestamp` (nhs 29 columns, saas-billing 12,
ride-sharing 9, saas 7, marketplace 6, retail 3, security-logs 1) with nothing
to join them to for by-month / by-weekday / by-quarter analysis — the first
thing a warehouse consumer reaches for. The only route today is the author
shipping a ~50k-row CSV as a supplement:

```yaml
supplements:
  - name: dim_date
    file: dim_date.csv          # 4.5 MB the author generated elsewhere
    columns: {date_key: INTEGER, date: DATE, year: INTEGER, ...}
tables:
  - name: fact_action
    columns:
      - {name: occurred_at, derived: {timestamp: {source: created_sim_time}}}
      # no way to emit occurred_date_key: no column mode turns an instant
      # into a date key, and a supplement's columns carry no reference
      # forge can declare or guard
```

Two gaps: no generated calendar, and no column mode that renders a structural
instant or a parsed date string as a key into one. The calendar join is
exactly the reference forge *can* vouch for — the key is a pure function of
the row's own instant and the anchor — and today it cannot.

## Solution

A top-level `date_dimension: {from, to}` block on the export config
materializes `dim_date`, a forge-defined table (pinned name, pinned column
set, `INTEGER` `yyyymmdd` key) generated as one SQL relation over the declared
range and compiled to a `QuerySpec` that flows through the slot supplements
already occupy — dimensional append, incremental `snapshot` delivery, shaped
playback, companion artifacts, and dataset packs are reused unchanged. A new
`date_ref` column mode beside `fk` renders the same `yyyymmdd` key from either
a structural instant (its local date in the anchor's zone) or a parsed date
string, so the author decides per column whether the original timestamp
travels beside the key. Every `date_ref` value is guarded against the declared
range before the first write; the manifest records `dim_date` as
forge-generated with its range and declares each `date_ref` column as a
reference to it.

```yaml
mode: dimensional
date_dimension: {from: "1900-01-01", to: "2099-12-31"}   # materializes dim_date

dimensional:
  tables:
    - name: fact_action
      role: fact
      source: {kind: action}
      key: [action_id]
      columns:
        - {name: action_id, from: record_id}
        - {name: occurred_at, derived: {timestamp: {source: created_sim_time}}}  # optional
        - {name: occurred_date_key, date_ref: {source: created_sim_time}}          # → dim_date
    - name: dim_patient
      role: dim
      source: {kind: patient}
      key: [patient_id]
      columns:
        - {name: patient_id, from: presentation_id}
        - {name: birth_date_key, date_ref: {from: prop__birth_date, format: "%Y-%m-%d"}}
```

```
config ──▶ date_dimension {from, to} ──▶ generate_series(from, to, 1 day)
                                            │  pinned 13-column calendar
                                            ▼
                     QuerySpec "dim_date" (calendar provenance) ─┐
                                                                 ├─▶ writers / incremental /
grain ──▶ date_ref {source | from+format} ──▶ DATE ──▶ yyyymmdd ─┘    playback / companions
                          ▲                        │
                     anchor (instant shape)        └─▶ range guard: every key ∈ [from, to]
```

## Affected Subsystems

- **Export config models** — `ExportConfig` gains `date_dimension`, a
  dimensional-only block carrying the inclusive `from` / `to` calendar bounds.
  `ColumnDecl` gains `date_ref`, a seventh mutually-exclusive column mode
  whose spec takes exactly one of two source shapes (a structural instant, or
  a VARCHAR column plus a declared date-complete parse format). Parse-time
  rules: `from <= to`; `date_ref` anywhere requires `date_dimension`; with
  `date_dimension` present no declared table or supplement may be named
  `dim_date`.
- **Dimensional exporter** — compiles `date_ref` columns on every grain
  through the shared temporal renderer (instant shape) or the declared-parse
  expression (parse shape), then through one date-key expression authority;
  compiles the generated `dim_date` relation from the config block alone;
  runs the range guard over every `date_ref`-bearing table before the first
  write; extends the business rules (`TimestampSourceAvailable`,
  `DateParseSourceColumn`, `TemporalRenderRequiresAnchor`,
  `SliceOnlyColumnRefused`, `Scd2ColumnModeSupported`, `KeyColumnsStable`)
  to the new mode. Plan iteration order becomes declared tables, then
  `dim_date`, then supplements.
- **Compiled-table representation (`QuerySpec` / `TableReport`)** — carries a
  calendar provenance marker (the declared range) beside the existing
  supplement marker, and the set of output columns that reference another
  output table (`date_ref` → `dim_date`; `fk` → its resolved dim table).
- **Incremental driver** — `dim_date` is `snapshot` in every emitting window
  (write mode `replace`), horizon-invariant by construction; a `date_ref`
  column's value channel is its source column's under the one per-column
  variance reading the delivery classifier and `KeyColumnsStable` share
  (the reading `derived: timestamp` / `date_parse` already have), so a
  `date_ref` over a source that can change makes its table `upsert`, never
  `append`; the range guard runs per window over the window's relations;
  `date_dimension` participates in the fingerprint through the config dump
  like any data-affecting field.
- **Shaped playback** — `tables()` lists `dim_date` after the declared tables
  and before the supplements, delivery `snapshot`, identical at every `T` and
  every window; the range guard runs per ask over the ask's materialized
  tables; `date_ref` columns compile in `window()` / `state()` as in the
  export path.
- **Companion artifacts** — the manifest gains `tables[].calendar`
  (`{from, to}` for `dim_date`, `null` elsewhere) and `columns[].references`
  (the referenced output table's name for `date_ref` and `fk` columns, `null`
  elsewhere); `manifest_format_version` bumps. The documentation dictionary
  answers `dim_date`'s table and column prose from a forge-pinned set (the
  event-log pattern) and pins a `date_ref` column's description unless the
  author overrides it. The README renders `dim_date` as any table.
- **Temporal elections / anchor** — no new election. A `date_ref`'s instant
  shape is, by definition, an explicit `date` election over the shared
  vocabulary and consumes the resolved anchor under the existing
  anchor-required rule. The two shared renderers it composes — the anchor
  temporal renderer and the declared-parse renderer — each split into a
  bare-expression authority (no alias) that the existing aliased fragment
  wraps, the posture the decimal authority already has; nothing about
  either renderer's SQL changes.
- **Principles** — the generated calendar is one of Principle #3's three named
  exceptions (beside supplements and corrupters): its rows trace to the
  author-declared range alone, and the manifest records that range. A
  `date_ref` value is an ordinary reshape — it traces to the row's own source
  column, exactly as `derived: timestamp` does.

## What Doesn't Change

- Source, base, and streaming modes: no `date_dimension`, no `date_ref`.
- Supplements: grammar, loader, `VALUES` relation, gates, fingerprint entry,
  and pack membership are untouched. `dim_date` is not a supplement and
  carries no `SupplementSource`.
- The instant election vocabulary (`timestamp` / `date` / `time` /
  `timestamptz`), the anchor resolution precedence, DST posture, and the
  declared-parse directive set.
- `derived: timestamp`, `derived: date_parse`, `derived: scd_window`, and
  every other derived kind — `date_ref` is a column mode beside them, not a
  change to any of them.
- No key declarations on `dim_date`: the dimensional mode has no
  `declare_keys` surface and this design does not add one; `dim_date`'s
  `keys` is `None` like every dimensional table's.
- No `init` proposal for `date_dimension` or `date_ref` — the temporal
  elections' posture (no engine proposes an election).
- No `scd_window` source shape for `date_ref`; no fiscal calendar, holidays,
  or locale — an author adds those as a supplement keyed on `date_key`.
- `fk`'s grammar and resolution: the only change is that its resolved target
  table is now also reported in the manifest's `references` field.
- The reserved bookkeeping table names, the presentation-name reservation, and
  the source-is-output gate — `dim_date` simply joins the output-name list
  every caller already hands the gate, so a supplement sourced from a
  `dim_date.csv` the invocation would write is refused like any other
  source-is-output collision.
- Source, base, and streaming report assembly: every `TableReport` those modes
  build states `calendar=None` and `references={}` — the two fields are stamped
  by the dimensional plan only.

## Semantics

### The generated calendar

`dim_date` is a forge-defined table. Its name, column set, column order,
types, and row order are the published contract of the feature, not author
choices — the calendar is mode-definitional in the same sense the event log's
first id is. Its rows are a pure function of `(from, to)`; nothing about the
emit, the anchor, or the other tables influences it.

| Column | Type | Value |
|---|---|---|
| `date_key` | `INTEGER` | `year * 10000 + month * 100 + day` — the `yyyymmdd` smart key; the table's row identity and sort order |
| `date` | `DATE` | The calendar date |
| `year` | `INTEGER` | Calendar year |
| `quarter` | `INTEGER` | 1–4 |
| `month` | `INTEGER` | 1–12 |
| `day` | `INTEGER` | Day of month, 1–31 |
| `day_of_week` | `INTEGER` | ISO weekday, 1 = Monday … 7 = Sunday |
| `day_of_year` | `INTEGER` | 1–366 |
| `iso_year` | `INTEGER` | ISO-8601 week-numbering year (differs from `year` around the new year) |
| `iso_week` | `INTEGER` | ISO-8601 week number, 1–53, within `iso_year` |
| `month_name` | `VARCHAR` | English month name (`January` … `December`) |
| `day_name` | `VARCHAR` | English weekday name (`Monday` … `Sunday`) |
| `is_weekend` | `BOOLEAN` | `day_of_week >= 6` |

English names are a pinned constant of the contract, not a locale knob. Every
integer column is cast to `INTEGER` explicitly (DuckDB's date-part functions
return `BIGINT`); the manifest transcribes `INTEGER`.

| Condition | Result |
|---|---|
| `date_dimension` present | One `dim_date` relation: `generate_series(from, to, INTERVAL 1 DAY)`, each element cast to `DATE`, the thirteen columns projected in the order above, ordered by `date_key` ascending. `from` and `to` are both inclusive; `from == to` yields one row |
| `date_dimension` absent | No `dim_date`; the config is exactly as capable as today |
| `date_dimension` present, no `date_ref` anywhere | `dim_date` is still materialized — an author may want the calendar alone |
| `from` and `to` span *n* days | *n* rows; cost is linear and small (200 years ≈ 73k rows, milliseconds) — no cap |
| Same `(from, to)`, any emit, any anchor, any other config | Byte-identical `dim_date` |

The relation is one SQL statement compiled to a `QuerySpec`: `write_mode`
`create` (full export) or `replace` (windowed — the caller's regime, never
inferred), `keys` `None`, empty `provenance` / `kind_values` /
`author_descriptions`, `author_table_description` `None`, `event_log` `False`,
`supplement` `None`, and a `CalendarSource(from, to)`. It reads no emit table,
so every consumer can compile it before any horizon opens.

### The `date_ref` column mode

A `date_ref` column renders an `INTEGER` `yyyymmdd` key from a date derived
per row. It is a column mode beside `fk` — a reference, not a value
rendering — and addresses base-column identities, never sibling output
columns, which is what lets the author include or omit a `derived: timestamp`
over the same source independently.

| Shape | Spec | Date derivation | Availability rule |
|---|---|---|---|
| Instant | `{source: <col>}` | The instant's local wall-clock date in the anchor's zone — exactly the `date` election over the shared vocabulary, so a `date_ref` and a sibling `derived: timestamp {source, as: date}` over the same column never disagree | `source` is available on the grain under the `derived: timestamp` reading (`TimestampSourceAvailable`): an instant-carrying structural column of the grain's category, the grain's virtual interval end, or a `prop__` column on the projectable surface |
| Parse | `{from: <col>, format: "<fmt>"}` | The declared parse of the VARCHAR source, its date part — exactly `derived: date_parse`'s expression cast to `DATE` | `from` resolves off the grain's projectable surface and carries a declared VARCHAR type (`DateParseSourceColumn`); `format` is date-complete (denotes `DATE` or naive `TIMESTAMP`; a time-only format is refused at parse) |

Both shapes then pass through the one date-key expression authority
(`date_key_expr`), which is also the expression `dim_date`'s own `date_key`
is generated with — so a fact's key and the calendar's key agree by
construction, never by two implementations of `yyyymmdd`.

| Condition | Result |
|---|---|
| Instant shape, anchor resolves | Key of the local date in `anchor.timezone` (DST observed, as for every wallclock rendering) |
| Instant shape, anchor is `None` | Refused at validation — `TemporalRenderRequiresAnchor`, naming the column. A `date_ref` is inherently an explicit `date` election; there is no raw-integer fallback |
| Parse shape, anchor resolves or is `None` | Key of the parsed date; the parse reads no anchor (the string was anchored upstream when minted) |
| Parse shape, a non-`NULL` cell does not match `format` | The export fails loudly — the declared-parse contract, unchanged |
| Source value is `NULL` (`deactivated_at` of a still-active record, `lead_sim_time` on an open interval, `left_sim_time` while bound, a `NULL` payload) | `NULL` key. No "unknown member" sentinel row: `NULL` is the faithful value and breaks no reference |
| Source column is `temporal_class: slice_only` (either shape) | Refused — `SliceOnlyColumnRefused`; both shapes join the rule's exhaustive surface list |
| `date_ref` on an `scd: type2` dim | Admitted as a pure per-row value rendering (both shapes), evaluated per record for untracked sources and per version for tracked ones — the posture `derived: timestamp` / `date_parse` already have under `Scd2ColumnModeSupported` |
| `date_ref` column in a table's `key` | Allowed; horizon-invariant iff its source is, under the same reading as `derived: timestamp`'s `source` / `date_parse.from` (`KeyColumnsStable`); under `upsert` delivery the `WindowKeyDuplicate` guard applies as to any rendered key |
| `date_ref` column on a windowed table (key or not) | Its value channel is its source column's, under the one per-column variance reading the delivery classifier and `KeyColumnsStable` share. A table is `append` only when every channel, `date_ref` included, is horizon-invariant; a `date_ref` over `deactivated_at`, `left_sim_time`, `lead_sim_time`, or a tracked property makes its table `upsert` — the classification `derived: timestamp` over the same source already yields |
| `date_ref` column named by `ordinal.partition_by` | Allowed — an ordinal within a calendar day is a natural use |
| `date_ref` column named by `ordinal.order_by` | Orders by its own value, `record_id` tie-broken — outside the ordinal amendment, like `date_parse` (the key is day-grained, not a rendered instant) |
| `date_ref` column carries a `description` | The author's prose replaces the pinned description in the README and manifest, as for any column |
| Provenance of a `date_ref` column | Stamped as a carried column — `ColumnProvenance(source_table=<the grain's resolved source table>, source_column=<source or from>)` — exactly as `derived: timestamp` / `date_parse` are; the value traces to that column. `references[<name>] = "dim_date"` is stamped beside it |
| Any `date_ref` in the config, `date_dimension` absent | Refused at parse — the key would reference a table that does not exist |

### The range guard

A key outside `[from, to]` is a dangling reference. Exporters introduce no
dangling references, so the guard refuses rather than emits — and it refuses
*before the first write*, the posture of every data guard that could
otherwise surface mid-dispatch after earlier tables were written.

| Condition | Result |
|---|---|
| Every non-`NULL` `date_ref` value in every table lies in `[from_key, to_key]` | Clean; the write proceeds |
| Some non-`NULL` value lies outside | `DateRefOutOfRange` before any table of the invocation is written — naming the table, the column, the offending key as it appears in the column (the column's minimum when that lies below `from_key`, else its maximum above `to_key`), and the declared range as ISO dates. Columns are probed in output order; the first violating column refuses |
| A table declares no `date_ref` column | Not probed |
| Windowed invocation | The guard runs per window over that window's compiled relations (the delta or the snapshot the window delivers), before the window's write |
| Shaped playback ask | The guard runs over the ask's materialized tables before the ask returns; a violation raises `DateRefOutOfRange` from the ask |
| `NULL` keys | Never a violation |

The probe is one aggregate query per `date_ref`-bearing table, tables in
plan iteration order: `MIN` and `MAX` over each `date_ref` column of the
table's compiled relation, projected beside the two bound keys —
`date_key_expr` over `DATE '<from>'` and `DATE '<to>'` — in the same
statement, so the bounds come from the one key authority and never from a
Python re-implementation of `yyyymmdd`. The same before-any-write,
evaluate-then-refuse posture as the supplement cell probe and
`WindowKeyDuplicate`.

The guard is the **last** pre-write gate at every entry point, so every
declaration-level refusal surfaces before a data probe runs: on a full or
windowed export it runs after the plan compile (and the `WindowKeyDuplicate`
guard the horizon compile carries), the `dim_date` compile, the supplement
gates, the source-is-output gate, and the overlay check; on the shaped head
it runs after the ask's selection gates and `WindowKeyDuplicate`, before the
ask materializes its return. A narrow range makes the case more likely, not
less — an author declaring 2020–2026 with a 1985 birth date, or a `rebase`
past `to`, is exactly the situation the guard exists for — so it is never
downgraded to a notice or documented as a limitation.

### Delivery across the consumers

`dim_date` is one more `QuerySpec` to every consumer, occupying the slot
supplements already occupy; plan iteration order is declared tables, then
`dim_date`, then supplements.

| Consumer | Contract |
|---|---|
| Dimensional full export | `dim_date` compiled after the plan, before any write; appended after the declared tables and before the supplement specs; the overlay's `table:` slots and the source-is-output gate both read the union of plan, `dim_date`, and supplement names; `dim_date.csv` under `fmt: csv`, a table in the `.duckdb` file otherwise |
| Incremental driver | Delivery class `snapshot` in every emitting window (`write_mode='replace'`), delivered whole on an empty window and on an explicit range like any snapshot table; the fingerprint carries `date_dimension` through the config dump (a changed range mid-drip is a mismatch, as any data-affecting config change is) |
| Shaped playback | `tables()` lists `dim_date` after the declared tables and before the supplements, `window_delivery='snapshot'`; it joins the `tables` selection domain; a `dim_date`-only selection materializes the calendar without opening a horizon or a truncated tape (the supplement-only posture); identical at every `T` and every window |
| Companion artifacts | `tables[].calendar` is `{"from": "<ISO date>", "to": "<ISO date>"}` for `dim_date` and `null` for every other table — the key always present. `dim_date`'s `description` and `columns[].description` come from the forge-pinned calendar dictionary; `unit`, `enum_options`, `primary_key`, `unique`, and `supplement` are `null`. `columns[].references` names the referenced output table for a `date_ref` column (`"dim_date"`) and an `fk` column (the resolved dim table's name), `null` elsewhere. The embedded config carries `date_dimension` as declared |
| Dataset packs | No file, so no pack member; the packed config carries the block |

### Documentation

| Item | Prose |
|---|---|
| `dim_date` table | Pinned: a forge-generated calendar over the declared range, one row per day, keyed by `yyyymmdd` |
| `dim_date` columns | Pinned per column, the Value column of the table above |
| A `date_ref` column, no author `description` | Pinned: "Calendar key (`yyyymmdd`) into `dim_date`, derived from `<source column>`" — the source column read from the report's provenance entry, the same for both shapes. The prose names neither the shape nor the parse format: the report carries only the source column and the reference, and the dictionary resolves from the report alone; shape and format are on record in the embedded config |
| A `date_ref` column with an author `description` | The author's prose |

Resolution order for a `date_ref` column's description: the author's
`description` → the pinned `date_ref` prose (keyed off `references[column]
== "dim_date"` on the report) → never inheritance. The pinned entry always
answers when no override exists, so the source column's own description is
not carried onto a key column even though the column carries provenance to
it. `unit` is `None` on **both** paths — a key carries no unit — which is a
rule the dictionary applies to any column with `references[column] ==
"dim_date"`, not a consequence of the existing unit-stop: that stop drops a
carried `ns` unit only when the output type has left the integer family,
and a `yyyymmdd` key is `INTEGER`, so without this rule an author-described
`date_ref` over `created_sim_time` would inherit `ns`. The pinned `date_ref`
doc and every `dim_date` column doc resolve with `origin: "forge"`, no
unit, no enum options — the event-log pattern. `dim_date`'s table and
column prose are keyed off `TableReport.calendar`; `author_descriptions`
cannot exist there.

### Invariants

Relied on:

- The shared temporal renderer's `date` election is the local wall-clock date
  in the anchor zone, and it is a pure projection of the `timestamp`
  rendering — so a `date_ref` never disagrees with a sibling `derived:
  timestamp` over the same source.
- The declared-parse expression is value-preserving and fails loudly on a
  non-matching cell.
- A `QuerySpec` that reads no emit table is horizon-invariant and can be
  compiled before any horizon opens.
- Plan-time business rules run before any SQL is emitted; data guards run
  before the first write.

Introduced:

- **One key authority.** `dim_date.date_key`, every `date_ref` value, and the
  range guard's two bound keys are produced by the same `date_key_expr`; there
  is no second `yyyymmdd` implementation anywhere, Python included.
- **Calendar determinism.** `dim_date` is a pure function of `(from, to)`:
  byte-identical across emits, anchors, formats, windows, and asks.
- **No dangling calendar reference.** Every non-`NULL` `date_ref` value in a
  written or returned table names a `dim_date` row, or the invocation refused
  before writing.
- **Family agreement.** For any row, `date_ref {source: c}` equals
  `date_key_expr` over `derived: timestamp {source: c, as: date}`; `date_ref
  {from: c, format: f}` equals `date_key_expr` over `derived: date_parse
  {from: c, format: f}` cast to `DATE`.
- **Provenance is named.** The manifest states, per table, whether it is an
  emit reshape, a supplement, or the calendar, and per column which output
  table it references.

## Configuration

```yaml
mode: dimensional
date_dimension:
  from: "1900-01-01"      # inclusive; ISO YYYY-MM-DD
  to:   "2099-12-31"      # inclusive; >= from

dimensional:
  tables:
    - name: fact_episode_state
      role: fact
      source: {kind: episode, grain: history_interval, property: state}
      key: [episode_id, entered_at]
      columns:
        - {name: episode_id, from: record_id}
        - {name: entered_at, derived: {timestamp: {source: sim_time}}}
        - {name: entered_date_key, date_ref: {source: sim_time}}
        - {name: exited_date_key, date_ref: {source: lead_sim_time}}   # NULL while open
    - name: dim_patient
      role: dim
      source: {kind: patient}
      key: [patient_id]
      columns:
        - {name: patient_id, from: presentation_id}
        - {name: birth_date, derived: {date_parse: {from: prop__birth_date, format: "%Y-%m-%d"}}}
        - {name: birth_date_key, date_ref: {from: prop__birth_date, format: "%Y-%m-%d"},
           description: "Calendar key of the patient's date of birth."}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `date_dimension` | object | No | Present → materialize `dim_date`. Legal only with `mode: dimensional` |
| `date_dimension.from` | ISO date | Yes (when the block is present) | First calendar day, inclusive |
| `date_dimension.to` | ISO date | Yes (when the block is present) | Last calendar day, inclusive; `>= from` |
| `columns[].date_ref` | object | — | A column mode, exclusive with `from` / `fk` / `correlation` / `derived` / `null` / `lookup` |
| `date_ref.source` | string | Exactly one of `source` / `from` | Instant shape: a `derived: timestamp`-legal source column |
| `date_ref.from` | string | Exactly one of `source` / `from` | Parse shape: a VARCHAR source column on the grain's projectable surface |
| `date_ref.format` | string | Iff `from` | The declared parse format; must be date-complete |

`from` / `to` are quoted in YAML so they load as strings and are parsed by the
model — an unquoted YAML date is refused, the supplement inline-row posture
that keeps the author reading one rule rather than a union-failure text.

## Interface Contracts

### Config Models

```python
class DateDimensionConfig(StrictBaseModel):
    """The inclusive calendar range the generated `dim_date` covers."""

    from_: date = Field(alias="from")
    """First calendar day, inclusive. Loaded from an ISO `YYYY-MM-DD` string."""
    to: date
    """Last calendar day, inclusive; not before `from_` (`range_ordered`)."""

    @model_validator(mode="before")
    @classmethod
    def bounds_are_strings(cls, data: object) -> object:
        """Refuse any non-string value for either bound.

        Both bounds must arrive as `str` and be parsed here, so the author
        sees this rule rather than a type-union failure — and so no other
        input Pydantic's `date` would accept (an unquoted YAML date, or an
        integer read as a Unix timestamp) can parse silently to a wrong day.

        Raises:
            ValueError: `from` or `to` is present and not a `str`.
        """

    @model_validator(mode="after")
    def range_ordered(self) -> Self:
        """`to` is not before `from_`.

        Raises:
            ValueError: `to < from_`.
        """


class DateRefSpec(StrictBaseModel):
    """A `yyyymmdd` key into `dim_date`, from an instant or a parsed date string."""

    source: str | None = None
    """Instant shape: the sim_time source column, rendered to its local
    date in the anchor zone — the same availability as `TimestampSpec.source`."""
    from_: str | None = Field(default=None, alias="from")
    """Parse shape: the VARCHAR source column holding date strings."""
    format: str | None = None
    """Parse shape: the declared parse format (closed strptime-directive set,
    see validate_date_parse_format); must be date-complete. Present iff `from_`."""

    @model_validator(mode="after")
    def exactly_one_shape(self) -> Self:
        """Exactly one of `source` / `from_` is set; `format` iff `from_`;
        a set `source` / `from_` is non-empty; `format` denotes a date.

        Raises:
            ValueError: Neither or both shapes set; `format` without `from_`
                or `from_` without `format`; an empty column name; a
                `format` that is not date-complete (time-only) or that
                fails the declared-parse directive rules.
        """
```

```python
class ColumnDecl(StrictBaseModel):
    """One output column declaration with exactly one source mode."""

    date_ref: DateRefSpec | None = None
    """Renders a `yyyymmdd` key into the generated `dim_date`."""

    @model_validator(mode="after")
    def exactly_one_column_mode(self) -> Self:
        """A ColumnDecl sets exactly one of
        from / fk / correlation / derived / null / lookup / date_ref.

        Raises:
            ValueError: zero or more than one mode is set.
        """
```

```python
class ExportConfig(StrictBaseModel):
    """Top-level export configuration block."""

    date_dimension: DateDimensionConfig | None = None
    """Present → the generated calendar `dim_date` is materialized over the
    declared range and `date_ref` columns may reference it. Legal only with
    mode='dimensional' (`date_dimension_requires_dimensional`). Absent → no
    calendar, and any `date_ref` is refused (`date_refs_require_date_dimension`)."""

    @model_validator(mode="after")
    def date_dimension_requires_dimensional(self) -> Self:
        """A present `date_dimension` requires mode='dimensional'.

        Raises:
            ValueError: `date_dimension` present under mode='source' / 'base'.
        """

    @model_validator(mode="after")
    def date_refs_require_date_dimension(self) -> Self:
        """Any `date_ref` column in the dimensional tables requires
        `date_dimension` (message names the table and column).

        Raises:
            ValueError: A `date_ref` column exists and `date_dimension` is None.
        """

    @model_validator(mode="after")
    def dim_date_name_reserved(self) -> Self:
        """With `date_dimension` present, no declared dimensional table and
        no supplement is named `dim_date` — the generated calendar's
        published name. Without the block the name is the author's to use.

        Raises:
            ValueError: A declared table or supplement is named `dim_date`
                while `date_dimension` is present.
        """
```

### Runtime Types

```python
DATE_DIMENSION_TABLE_NAME: Final = "dim_date"
"""The generated calendar's published output-table name — mode-definitional."""

DATE_DIMENSION_COLUMNS: Final[tuple[tuple[str, str], ...]]
"""The pinned (name, type-text) pairs of `dim_date`, in output order — the
one authority the relation, the documentation dictionary, and the tests read."""


@dataclass(frozen=True)
class CalendarSource:
    """Manifest-facing provenance of the generated calendar: the declared
    inclusive range."""

    from_: date
    to: date


@dataclass(frozen=True)
class QuerySpec:
    """A compiled output table (existing; two fields added)."""

    calendar: CalendarSource | None = None
    """Set iff this spec is the generated `dim_date`; forwarded to
    `TableReport` by both report-assembly sites."""
    references: Mapping[str, str] = field(default_factory=dict)
    """Output column name -> referenced output table name, for every
    `date_ref` column (-> `dim_date`) and every `fk` column (-> the resolved
    dim table). Empty when the table references nothing. Stamped at plan
    compile; forwarded to `TableReport`."""


@dataclass(frozen=True)
class TableReport:
    """One output table as written (existing; two fields added, forwarded
    verbatim from the compiled QuerySpec — no default, so every
    report-assembly call site states them explicitly)."""

    calendar: CalendarSource | None
    references: Mapping[str, str]
```

### Functions

```python
def date_key_expr(date_sql: str) -> str:
    """The one `yyyymmdd` key expression, over a DATE-typed SQL expression.

    Renders `CAST(year(d) * 10000 + month(d) * 100 + day(d) AS INTEGER)`
    with `d` the given expression, NULL-propagating. Read by the `dim_date`
    relation and by every `date_ref` compile so the calendar's key and every
    reference into it agree by construction.

    Args:
        date_sql: A SQL expression of type DATE.

    Returns:
        A SQL expression of type INTEGER (no alias).
    """


def compile_date_dimension_spec(
    config: DateDimensionConfig,
    write_mode: Literal["create", "replace"],
) -> QuerySpec:
    """Compile the generated calendar into a QuerySpec.

    The SQL is one `generate_series(DATE from, DATE to, INTERVAL 1 DAY)`
    relation, each element cast to DATE, projected to the pinned
    `DATE_DIMENSION_COLUMNS` in order (every integer part cast to INTEGER,
    `date_key` through `date_key_expr`), ordered by `date_key`. Carries
    `table_name=DATE_DIMENSION_TABLE_NAME`, no keys, empty provenance /
    kind_values / author_descriptions / references, no table description,
    `event_log=False`, `supplement=None`, and `CalendarSource(from, to)`.
    A pure function of the config block — no emit, no anchor, no session —
    so every caller compiles it before any horizon opens.

    Args:
        config: The validated `date_dimension` block.
        write_mode: 'create' for a full export, 'replace' for a windowed
            compile — the caller's delivery regime, never inferred.

    Returns:
        The `dim_date` QuerySpec.
    """


def anchor_temporal_expr(
    anchor: EffectiveAnchor,
    qualified_source: str,
    render: TemporalRender,
) -> str:
    """The bare (unaliased) expression of the shared temporal renderer.

    Byte-identical to the expression `render_anchor_temporal_expr` aliases
    today; that function becomes this expression wrapped in `AS
    "<out_name>"` (with its no-anchor pass-through unchanged), so the one
    renderer every wallclock mode shares stays one. The decimal authority's
    posture: bare expression, caller aliases.

    Args:
        anchor: The resolved anchor (the no-anchor path never reaches here).
        qualified_source: The fully table-qualified BIGINT-ns source SQL.
        render: The elected temporal rendering.

    Returns:
        A SQL expression of the elected type (no alias).
    """


def date_parse_expr(
    qualified_source: str,
    date_format: str,
    table_label: str,
) -> str:
    """The bare (unaliased) expression of the declared-parse renderer.

    Byte-identical to the `CASE` expression `render_date_parse_expr` aliases
    today — the loud non-matching-cell guard naming `table_label` included;
    that function becomes this expression wrapped in `AS "<out_name>"`.

    Args:
        qualified_source: The fully table-qualified VARCHAR source SQL.
        date_format: The validated declared parse format.
        table_label: The output table name interpolated into the guard.

    Returns:
        A SQL expression of the format's denoted type (no alias).
    """


def render_date_ref_expr(
    spec: DateRefSpec,
    qualified_source: str,
    out_name: str,
    table_label: str,
    anchor: EffectiveAnchor | None,
) -> str:
    """Render the SQL SELECT fragment for one `date_ref` column.

    Instant shape: `anchor_temporal_expr` with the `date` election over
    `qualified_source` (the caller has already enforced the anchor rule, so
    `anchor` is non-None here), then `date_key_expr`. Parse shape:
    `date_parse_expr` over `qualified_source` (its loud non-matching-cell
    failure naming `table_label`, unchanged) cast to DATE, then
    `date_key_expr`. Either way the fragment ends in `AS "<out_name>"` and
    is NULL-propagating; both shapes compose the bare forms of the shared
    renderers, never a re-spelling of their SQL.

    Args:
        spec: The column's `date_ref` spec.
        qualified_source: The fully table-qualified source column SQL for
            whichever shape the spec carries.
        out_name: The output column name.
        table_label: The output table name, interpolated into the parse
            shape's mismatch error; unused by the instant shape.
        anchor: The resolved anchor; consulted by the instant shape only.

    Returns:
        A SQL SELECT-list expression fragment ending in `AS "<out_name>"`.
    """


def check_date_refs_in_range(
    emit: Emit,
    specs: Sequence[QuerySpec],
    config: DateDimensionConfig,
) -> None:
    """The range guard: every non-NULL `date_ref` value lies in the calendar.

    For each spec whose `references` maps at least one column to `dim_date`,
    evaluates one aggregate query over the spec's relation — `MIN` and `MAX`
    of each such column, plus `date_key_expr` over `DATE '<from>'` and
    `DATE '<to>'` as the two bound keys — and compares each column's
    minimum against the lower bound and maximum against the upper. Runs
    before the first write of an invocation (full export), before each
    window's write (incremental), and before each ask returns (shaped
    playback). Columns are checked in the spec's output order; the first
    violating column refuses, its minimum reported when that is below the
    lower bound, else its maximum. NULL aggregates (an empty relation, or
    an all-NULL column) never violate.

    Args:
        emit: The open emit whose session evaluates each relation.
        specs: The compiled specs of the invocation / window / ask.
        config: The validated `date_dimension` block.

    Raises:
        DateRefOutOfRange: A non-NULL value lies outside `[from, to]`; names
            the table, the column, the offending key as it appears in the
            column, and the declared range as ISO dates.
    """
```

```python
class DateRefOutOfRange(ExportError):
    """A `date_ref` column carries a non-NULL date outside the declared
    `date_dimension` range — a reference no `dim_date` row answers. Raised
    before any write. Message: `"table '{table}' column '{column}': date key
    {key} lies outside date_dimension {from}..{to}"` — `{key}` the integer
    as it appears in the column, `{from}` / `{to}` ISO dates."""
```

## Validation Rules

### Parse-Time (Pydantic)

| Validator | Refuses |
|---|---|
| `DateDimensionConfig.bounds_are_strings` | A non-`str` value for `from` / `to` (an unquoted YAML date, an integer) |
| `DateDimensionConfig.range_ordered` | `to` before `from` |
| `DateRefSpec.exactly_one_shape` | Neither or both of `source` / `from`; `format` without `from` or `from` without `format`; an empty column name; a `format` outside the declared-parse directive rules or not date-complete (time-only) |
| `ColumnDecl.exactly_one_column_mode` | `date_ref` together with any other column mode, or no mode at all |
| `ExportConfig.date_dimension_requires_dimensional` | `date_dimension` under `mode: source` / `base` |
| `ExportConfig.date_refs_require_date_dimension` | A `date_ref` column anywhere in `dimensional.tables` with no `date_dimension` block; names the table and column |
| `ExportConfig.dim_date_name_reserved` | With `date_dimension` present, a declared table or a supplement named `dim_date` |
| `TableDecl.column_names_unique` (existing) | A `date_ref` column sharing a name with a sibling — no change |

### Business Rules

Run at plan compile against the sidecar, before any SQL is emitted, in the
dimensional validation runner. Existing rules extended to the new mode keep
their identities and messages; two rules are new.

| Rule | Checks | Error Message |
|---|---|---|
| `TimestampSourceAvailable` (extended) | Each `date_ref` instant shape's `source` is available on the grain under the `derived: timestamp` reading | existing message, naming the column and grain |
| `DateParseSourceColumn` (extended) | Each `date_ref` parse shape's `from` resolves off the grain's projectable surface and carries a declared VARCHAR type | existing message |
| `TemporalRenderRequiresAnchor` (extended) | Every `date_ref` instant shape has a resolved effective anchor | existing message, naming the column |
| `SliceOnlyColumnRefused` (extended) | No `date_ref` `source` / `from` resolves to a non-exempt `temporal_class: slice_only` column | existing message |
| `Scd2ColumnModeSupported` (extended) | `date_ref` is an admitted mode on an `scd: type2` table | existing message |
| `KeyColumnsStable` (extended) | A `date_ref` column in `key` is horizon-invariant iff its source column is, under the reading `derived: timestamp` `source` / `date_parse.from` already use | existing message, naming the varying source |
| Delivery classifier (extended, not a refusal) | The same variance reading classifies a `date_ref`-bearing table's delivery class: `append` only when every channel, `date_ref` included, is horizon-invariant, else `upsert` | — (a classification, never an error) |
| `check_reserved_table_name` (existing, unchanged) | `dim_date` is not a bookkeeping name; nothing new to check — the parse-time reservation is the only `dim_date` name rule |
| `DateRefOutOfRange` (new, data guard) | Before the first write of the invocation / window / ask: every non-NULL `date_ref` value lies in `[from, to]` | `"table '{table}' column '{column}': date key {key} lies outside date_dimension {from}..{to}"` |
| Overlay `table:` slots (existing, extended domain) | Validate against the union of plan, `dim_date`, and supplement names | existing `ReadmeOverlayUnknownTable` |

Windowed and shaped-playback entry points run the same rule set at their
compile; the range guard additionally runs per window / per ask because it
is data-dependent.
