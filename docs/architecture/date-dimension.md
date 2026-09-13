# Date Dimension

The **date dimension** is an opt-in, author-ranged, forge-generated calendar table
for the dimensional mode, and the column mode that keys into it. A top-level
`date_dimension: {from, to}` block on `ExportConfig` materializes `dim_date` — a
forge-defined table with a pinned name, a pinned thirteen-column set, and an
`INTEGER` `yyyymmdd` key — as one SQL relation over the declared inclusive range,
compiled to a `QuerySpec` that occupies the slot supplements occupy: dimensional
append, incremental `snapshot` delivery, shaped playback, companion artifacts, and
dataset packs consume it with no parallel machinery. The `date_ref` column mode,
a reference beside `fk`, renders the same `yyyymmdd` key from either a structural
instant (its local date in the anchor's zone) or a parsed date string, so an
author decides per column whether the original timestamp travels beside the key.
The calendar join is the one reference forge can vouch for outright — the key is
a pure function of the row's own instant and the anchor — and the range guard
refuses any key outside the declared range before the first write. The generated
calendar is one of `CLAUDE.md` Principle #3's three named exceptions: its rows
trace to the author-declared range alone, and the manifest records that range. A
`date_ref` value is an ordinary reshape — it traces to the row's own source
column, exactly as `derived: timestamp` does.

**Source:** [`exporters/date_dimension.py`](../../src/fabulexa_forge/exporters/date_dimension.py)
(`DATE_DIMENSION_TABLE_NAME`, `DATE_DIMENSION_COLUMNS`, `CalendarSource`,
`compile_date_dimension_spec`, `check_date_refs_in_range`);
[`_sql.py`](../../src/fabulexa_forge/_sql.py) (`date_key_expr`, `date_parse_expr`);
[`anchor.py`](../../src/fabulexa_forge/anchor.py) (`anchor_temporal_expr`);
[`exporters/dimensional/columns.py`](../../src/fabulexa_forge/exporters/dimensional/columns.py)
(`render_date_ref_expr`); [`DateDimensionConfig`](../../src/fabulexa_forge/config/models.py),
`DateRefSpec`, `ColumnDecl.date_ref`, and `ExportConfig.date_dimension` in the
config models; the `calendar` / `references` fields on `QuerySpec` / `TableReport`
in [`exporters/query_spec.py`](../../src/fabulexa_forge/exporters/query_spec.py);
`DateRefOutOfRange` in [`errors.py`](../../src/fabulexa_forge/errors.py). Tests:
[`tests/config/test_date_dimension.py`](../../tests/config/test_date_dimension.py)
(parse rules),
[`tests/exporters/test_date_dimension.py`](../../tests/exporters/test_date_dimension.py)
(the calendar relation and the range guard),
[`tests/exporters/dimensional/test_date_ref.py`](../../tests/exporters/dimensional/test_date_ref.py)
(the column mode and its business rules),
[`tests/exporters/dimensional/test_export_date_dimension.py`](../../tests/exporters/dimensional/test_export_date_dimension.py)
(full export),
[`tests/incremental/test_date_dimension.py`](../../tests/incremental/test_date_dimension.py),
[`tests/playback/test_shaped_date_dimension.py`](../../tests/playback/test_shaped_date_dimension.py),
[`tests/exporters/companion/test_date_dimension.py`](../../tests/exporters/companion/test_date_dimension.py)
(manifest and dictionary), and the renderer authorities in
[`tests/test_sql.py`](../../tests/test_sql.py) / [`tests/test_anchor.py`](../../tests/test_anchor.py).
Recipe: [`date-dimension`](../../examples/recipes/date-dimension/config.yaml).

## Boundary

- **In.** The validated `ExportConfig`'s `date_dimension` block (the two inclusive
  bounds) and its `date_ref` column declarations; the resolved
  `EffectiveAnchor | None` (consulted by the instant shape only); the open
  `Emit`'s session, which materializes the calendar relation and evaluates the
  range guard's aggregate probes over the compiled relations.
- **Out.** One `QuerySpec` named `dim_date` carrying a `CalendarSource(from, to)`
  (forwarded to `TableReport.calendar` and the manifest); per `date_ref` column, a
  SQL SELECT-list fragment producing an `INTEGER` key and a `references` entry
  naming `dim_date`; and the range guard's refusal, `DateRefOutOfRange`, before
  any write.
- **Reads no emit table.** `compile_date_dimension_spec` is a pure function of
  the config block — no emit, no anchor, no session — so every consumer compiles
  it before any horizon opens. The range guard is the one emit-reading step, and
  it reads only the invocation's own compiled relations.
- **Error hierarchy.** The bounds, shape, mode, and name rules are Pydantic
  validators surfacing as `ConfigError` at load; the business rules `date_ref`
  joins are the dimensional mode's `ExportError`s at plan compile;
  `DateRefOutOfRange` is an `ExportError` raised before the first write. The
  CLI's `(ReaderError, ExporterError)` funnel renders every one as exit 1 with no
  data written.

## Semantics

### The generated calendar

`dim_date` is a forge-defined table. Its name, column set, column order, types,
and row order are the published contract of the feature, not author choices — the
calendar is mode-definitional in the same sense the source event log's first id
is. Its rows are a pure function of `(from, to)`; nothing about the emit, the
anchor, or the other tables influences it.

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

`DATE_DIMENSION_COLUMNS` is the one authority for this set — the relation, the
documentation dictionary, and the tests read it. English names are a pinned
constant of the contract, not a locale knob. Every integer column is cast to
`INTEGER` explicitly (DuckDB's date-part functions return `BIGINT`); the manifest
transcribes `INTEGER`.

| Condition | Result |
|---|---|
| `date_dimension` present | One `dim_date` relation: `generate_series(from, to, INTERVAL 1 DAY)`, each element cast to `DATE`, the thirteen columns projected in the order above, ordered by `date_key` ascending. `from` and `to` are both inclusive; `from == to` yields one row |
| `date_dimension` absent | No `dim_date`; the config declares no calendar and no `date_ref` |
| `date_dimension` present, no `date_ref` anywhere | `dim_date` is materialized — the calendar alone is a legitimate ask |
| `from` and `to` span *n* days | *n* rows; cost is linear and small (200 years ≈ 73k rows, milliseconds) — no cap |
| Same `(from, to)`, any emit, any anchor, any other config | Byte-identical `dim_date` |

The relation is one SQL statement compiled to a `QuerySpec`: `write_mode`
`create` (full export) or `replace` (windowed — the caller's regime, never
inferred), `keys` `None`, empty `provenance` / `kind_values` /
`author_descriptions` / `references`, `author_table_description` `None`,
`event_log` `False`, `supplement` `None`, and a `CalendarSource(from, to)`.

### The `date_ref` column mode

A `date_ref` column renders an `INTEGER` `yyyymmdd` key from a date derived per
row. It is a column mode beside `fk` — a reference, not a value rendering — and
addresses base-column identities, never sibling output columns, which is what
lets the author include or omit a `derived: timestamp` over the same source
independently.

| Shape | Spec | Date derivation | Availability rule |
|---|---|---|---|
| Instant | `{source: <col>}` | The instant's local wall-clock date in the anchor's zone — exactly the `date` election over the shared vocabulary, so a `date_ref` and a sibling `derived: timestamp {source, as: date}` over the same column never disagree | `source` is available on the grain under the `derived: timestamp` reading (`TimestampSourceAvailable`): an instant-carrying structural column of the grain's category, the grain's virtual interval end, or a `prop__` column on the projectable surface |
| Parse | `{from: <col>, format: "<fmt>"}` | The declared parse of the VARCHAR source, its date part — exactly `derived: date_parse`'s expression cast to `DATE` | `from` resolves off the grain's projectable surface and carries a declared VARCHAR type (`DateParseSourceColumn`); `format` is date-complete (denotes `DATE` or naive `TIMESTAMP`; a time-only format is refused at parse) |

Both shapes then pass through the one date-key expression authority
(`date_key_expr`), which is also the expression `dim_date`'s own `date_key` is
generated with — so a fact's key and the calendar's key agree by construction,
never by two implementations of `yyyymmdd`. `render_date_ref_expr` composes the
bare (unaliased) forms of the two shared renderers — `anchor_temporal_expr` with
the `date` election for the instant shape, `date_parse_expr` cast to `DATE` for
the parse shape ([`temporal-elections.md`](temporal-elections.md) § Per-mode
attach points) — never a re-spelling of their SQL.

| Condition | Result |
|---|---|
| Instant shape, anchor resolves | Key of the local date in `anchor.timezone` (DST observed, as for every wallclock rendering) |
| Instant shape, anchor is `None` | Refused at validation — `TemporalRenderRequiresAnchor`, naming the column. A `date_ref` is inherently an explicit `date` election; there is no raw-integer fallback |
| Parse shape, anchor resolves or is `None` | Key of the parsed date; the parse reads no anchor (the string was anchored upstream when minted) |
| Parse shape, a non-`NULL` cell does not match `format` | The export fails loudly — the declared-parse contract |
| Source value is `NULL` (`deactivated_at` of a still-active record, `lead_sim_time` on an open interval, `left_sim_time` while bound, a `NULL` payload) | `NULL` key. There is no "unknown member" sentinel row: `NULL` is the faithful value and breaks no reference |
| Source column is `temporal_class: slice_only` (either shape) | Refused — `SliceOnlyColumnRefused`; both shapes are on the rule's exhaustive surface list |
| `date_ref` on an `scd: type2` dim | Admitted as a pure per-row value rendering (both shapes), evaluated per record for untracked sources and per version for tracked ones — the posture `derived: timestamp` / `date_parse` have under `Scd2ColumnModeSupported` |
| `date_ref` column in a table's `key` | Allowed; horizon-invariant iff its source is, under the same reading as `derived: timestamp`'s `source` / `date_parse.from` (`KeyColumnsStable`); under `upsert` delivery the `WindowKeyDuplicate` guard applies as to any rendered key |
| `date_ref` column on a windowed table (key or not) | Its value channel is its source column's, under the one per-column variance reading the delivery classifier and `KeyColumnsStable` share. A table is `append` only when every channel, `date_ref` included, is horizon-invariant; a `date_ref` over `deactivated_at`, `left_sim_time`, `lead_sim_time`, or a tracked property makes its table `upsert` — the classification `derived: timestamp` over the same source yields |
| `date_ref` column named by `ordinal.partition_by` | Allowed — an ordinal within a calendar day is a natural use |
| `date_ref` column named by `ordinal.order_by` | Orders by its own value, `record_id` tie-broken — outside the ordinal amendment, like `date_parse` (the key is day-grained, not a rendered instant) |
| `date_ref` column carries a `description` | The author's prose replaces the pinned description in the README and manifest, as for any column |
| Provenance of a `date_ref` column | Stamped as a carried column — `ColumnProvenance(source_table=<the grain's resolved source table>, source_column=<source or from>)` — exactly as `derived: timestamp` / `date_parse` are; the value traces to that column. `references[<name>] = "dim_date"` is stamped beside it |
| Any `date_ref` in the config, `date_dimension` absent | Refused at parse — the key would reference a table that does not exist |

### The range guard

A key outside `[from, to]` is a dangling reference. Exporters introduce no
dangling references, so the guard refuses rather than emits — and it refuses
*before the first write*, the posture of every data guard that could otherwise
surface mid-dispatch after earlier tables were written.

| Condition | Result |
|---|---|
| Every non-`NULL` `date_ref` value in every table lies in `[from_key, to_key]` | Clean; the write proceeds |
| Some non-`NULL` value lies outside | `DateRefOutOfRange` before any table of the invocation is written — naming the table, the column, the offending key as it appears in the column (the column's minimum when that lies below `from_key`, else its maximum above `to_key`), and the declared range as ISO dates. Columns are probed in output order; the first violating column refuses |
| A table declares no `date_ref` column | Not probed |
| Windowed invocation | The guard runs per window over that window's compiled relations (the delta or the snapshot the window delivers), before the window's write |
| Shaped playback ask | The guard runs over the ask's materialized tables before the ask returns; a violation raises `DateRefOutOfRange` from the ask |
| `NULL` keys | Never a violation |

The probe (`check_date_refs_in_range`) is one aggregate query per
`date_ref`-bearing table, tables in plan iteration order: `MIN` and `MAX` over
each `date_ref` column of the table's compiled relation, projected beside the two
bound keys — `date_key_expr` over `DATE '<from>'` and `DATE '<to>'` — in the same
statement, so the bounds come from the one key authority and never from a Python
re-implementation of `yyyymmdd`. The same before-any-write, evaluate-then-refuse
posture as the supplement cell probe and `WindowKeyDuplicate`.

The guard is the **last** pre-write gate at every entry point, so every
declaration-level refusal surfaces before a data probe runs: on a full or
windowed export it runs after the plan compile (and the `WindowKeyDuplicate`
guard the horizon compile carries), the `dim_date` compile, the supplement
gates, the source-is-output gate, and the overlay check; on the shaped head it
runs after the ask's selection gates and `WindowKeyDuplicate`, before the ask
materializes its return. A narrow range makes the case more likely, not less —
an author declaring 2020–2026 with a 1985 birth date, or a `rebase` past `to`,
is exactly the situation the guard exists for — so it is never downgraded to a
notice or documented as a limitation.

### Delivery across the consumers

`dim_date` is one more `QuerySpec` to every consumer, occupying the slot
supplements occupy; plan iteration order is declared tables, then `dim_date`,
then supplements. Each consumer's own doc carries its side of the contract.

| Consumer | Contract |
|---|---|
| Dimensional full export | `dim_date` compiled after the plan, before any write; appended after the declared tables and before the supplement specs; the overlay's `table:` slots and the source-is-output gate both read the union of plan, `dim_date`, and supplement names; `dim_date.csv` under `fmt: csv`, a table in the `.duckdb` file otherwise — [`dimensional.md`](dimensional.md) § Boundary |
| Incremental driver | Delivery class `snapshot` in every emitting window (`write_mode='replace'`), delivered whole on an empty window and on an explicit range like any snapshot table; the fingerprint carries `date_dimension` through the config dump (a changed range mid-drip is a mismatch, as any data-affecting config change is) — [`incremental.md`](incremental.md) § Horizon windowing |
| Shaped playback | `tables()` lists `dim_date` after the declared tables and before the supplements, `window_delivery='snapshot'`; it joins the `tables` selection domain; a selection naming no declared table materializes the calendar without opening a horizon or a truncated tape; identical at every `T` and every window — [`playback.md`](playback.md) § Shaped window |
| Companion artifacts | `tables[].calendar` is `{"from": "<ISO date>", "to": "<ISO date>"}` for `dim_date` and `null` for every other table; `columns[].references` names the referenced output table for a `date_ref` column (`"dim_date"`) and an `fk` column (the resolved dim table's name), `null` elsewhere; the embedded config carries `date_dimension` as declared — [`companion-artifacts.md`](companion-artifacts.md) § The manifest |
| Documentation | `dim_date`'s table and column prose and a `date_ref` column's default description come from the forge-pinned calendar dictionary, author-overridable per column, `unit` always `None` on a key — [`documentation-channel.md`](documentation-channel.md) § The pinned calendar documentation |
| Dataset packs | No file, so no pack member; the packed config carries the block |

## Invariants

1. **One key authority.** `dim_date.date_key`, every `date_ref` value, and the
   range guard's two bound keys are produced by the same `date_key_expr`; there
   is no second `yyyymmdd` implementation anywhere, Python included.
2. **Calendar determinism.** `dim_date` is a pure function of `(from, to)`:
   byte-identical across emits, anchors, formats, windows, and asks.
3. **No dangling calendar reference.** Every non-`NULL` `date_ref` value in a
   written or returned table names a `dim_date` row, or the invocation refused
   before writing.
4. **Family agreement.** For any row, `date_ref {source: c}` equals
   `date_key_expr` over `derived: timestamp {source: c, as: date}`; `date_ref
   {from: c, format: f}` equals `date_key_expr` over `derived: date_parse
   {from: c, format: f}` cast to `DATE`.
5. **Provenance is named.** The manifest states, per table, whether it is an
   emit reshape, a supplement, or the calendar, and per column which output
   table it references.

Relied on: the shared temporal renderer's `date` election is the local
wall-clock date in the anchor zone and a pure projection of the `timestamp`
rendering; the declared-parse expression is value-preserving and fails loudly on
a non-matching cell; a `QuerySpec` that reads no emit table is horizon-invariant
and can be compiled before any horizon opens; plan-time business rules run
before any SQL is emitted and data guards before the first write.

## Validation Rules

Parse-time rules are validators on `DateDimensionConfig` / `DateRefSpec` /
`ColumnDecl` / `ExportConfig`
([`models.py`](../../src/fabulexa_forge/config/models.py); cases in
[`tests/config/test_date_dimension.py`](../../tests/config/test_date_dimension.py)):

| Validator | Refuses |
|---|---|
| `DateDimensionConfig.bounds_are_strings` (`before` mode, so the author sees this rule rather than a type-union failure, and so no other input Pydantic's `date` would accept — an unquoted YAML date, an integer read as a Unix timestamp — can parse silently to a wrong day) | A non-`str` value for `from` / `to` |
| `DateDimensionConfig.range_ordered` | `to` before `from` |
| `DateRefSpec.exactly_one_shape` | Neither or both of `source` / `from`; `format` without `from` or `from` without `format`; an empty column name; a `format` outside the declared-parse directive rules or not date-complete (time-only) |
| `ColumnDecl.exactly_one_column_mode` | `date_ref` together with any other column mode, or no mode at all |
| `ExportConfig.date_dimension_requires_dimensional` | `date_dimension` under `mode: source` / `base` |
| `ExportConfig.date_refs_require_date_dimension` | A `date_ref` column anywhere in `dimensional.tables` with no `date_dimension` block; names the table and column |
| `ExportConfig.dim_date_name_reserved` | With `date_dimension` present, a declared table or a supplement named `dim_date`. Without the block the name is the author's to use |

Plan-time, the `date_ref` mode joins the dimensional mode's business rules under
their existing identities and messages ([`dimensional.md`](dimensional.md)
§ Validation Rules): `ProjectionColumnExists` and `DateParseSourceColumn` for
the parse shape's `from`, `TimestampSourceAvailable` and
`TemporalRenderRequiresAnchor` for the instant shape's `source`,
`SliceOnlyColumnRefused` for both, `Scd2ColumnModeSupported` (admitted),
`KeyColumnsStable` and the delivery classifier's variance reading. The overlay's
`table:` slots validate against the union of plan, `dim_date`, and supplement
names (`ReadmeOverlayUnknownTable`). The one data guard is `DateRefOutOfRange`
(§ The range guard), run before the first write of an invocation, before each
window's write, and before each shaped ask returns.

## Rationale

- **A generated calendar, not a supplement.** A supplement's columns carry no
  forge integrity claim, and a ~50k-row author-generated CSV is the wrong
  vehicle for a table that is a pure function of a range. The calendar is
  neither emit data nor author data: it derives from the Gregorian calendar over
  a declared range, the same trust base as the anchor's DST and zone rules. That
  is why it is a named exception to "reshape, never fabricate" driven by a
  *range*, not by values — the manifest records the range, and nothing else
  about the emit or the config reaches the rows.
- **One key authority.** Two `yyyymmdd` implementations — one for the calendar,
  one for the facts — would agree by testing, not by construction. Routing the
  calendar's key, every `date_ref`, and the guard's bounds through
  `date_key_expr` makes the family-agreement invariant a property of the SQL,
  not of a test suite.
- **A column mode beside `fk`, not a `derived` kind.** A `date_ref` is a
  reference into an output table, which is what `fk` is and what no `derived`
  kind is; the manifest's `references` field names both the same way. Addressing
  base columns rather than sibling output columns keeps the timestamp and the
  key independent: an author may emit the key alone, the timestamp alone, or
  both.
- **The instant shape is the `date` election, not a new one.** A `date_ref` over
  an instant is by definition the local date in the anchor's zone. Gating it on
  an author-elected `as: date` would reach no fact that renders at the default
  `timestamp` rendering, which is nearly all of them; deriving the date from the
  instant itself, under the same anchor-required rule, reaches every fact
  instant. The parse shape exists because dims carry upstream-minted date
  strings (birth dates decades before any emit event) that the key must reach
  too.
- **Refuse, never a sentinel row.** An "unknown member" row (`date_key = -1`) is a
  fabricated calendar row and a fabricated reference; `NULL` is the faithful
  value for a still-open interval or an absent payload and breaks no reference.
  An out-of-range key is a genuine dangling reference, so the guard refuses
  before any write rather than emitting a notice — a narrow range is the
  author's declaration, and violating it is the case the guard exists for.
- **Bare-expression renderer authorities.** The instant and parse shapes compose
  the shared temporal and declared-parse renderers. Each renderer exposes a bare
  (unaliased) expression the aliased fragment wraps — the posture the decimal
  authority has — so `date_ref` composes the one renderer every wallclock mode
  shares instead of re-spelling its SQL.
- **`INTEGER` `yyyymmdd`, English names, ISO weeks.** The smart key is the
  warehouse convention a downstream consumer expects to join on and read by eye;
  ISO weekday and week numbering are the unambiguous choice; English names are
  pinned because a locale knob would be an author-facing surface with no emit
  ground behind it. An author wanting fiscal periods, holidays, or another
  language adds a supplement keyed on `date_key`.
- **Dimensional only.** The calendar is a star-schema shape. Base is the emit's
  own shape, source's app-database seed tables are the supplement mechanism's
  domain, and streaming has no table set to join.
- **`dim_date` reserved only when declared.** With no `date_dimension` block the
  name is the author's, so a config that ships its own calendar as a supplement
  or a declared table is untouched; with the block present the collision is
  decidable on the config alone and refused at parse.

## Boundaries

- **Dimensional only.** `date_dimension` under `mode: source` or `mode: base` is a
  parse-time refusal; `StreamConfig` has neither `date_dimension` nor
  `date_ref`.
- **Not a supplement.** `dim_date` carries no `SupplementSource`; the supplement
  grammar, loader, `VALUES` relation, gates, fingerprint entry, and pack
  membership do not see it.
- **No fiscal calendar, holidays, or locale.** The thirteen columns and English
  names are the whole contract; such attributes are a supplement keyed on
  `date_key`.
- **No `scd_window` source shape.** A `date_ref` addresses a structural instant
  or a VARCHAR column; a date-grained validity window is `scd_window`'s object
  form with `as: date`.
- **No key declarations.** The dimensional mode has no `declare_keys` surface;
  `dim_date`'s `keys` is `None` like every dimensional table's, and the manifest's
  `primary_key` / `unique` are `null` for it.
- **`init` proposes none.** No engine proposes a calendar or a `date_ref` — the
  temporal elections' posture that no engine proposes an election.
- **`fk` is untouched.** Its grammar and resolution are its own; the one shared
  surface is the manifest's `references` field, which reports an `fk`'s resolved
  target beside a `date_ref`'s `dim_date`.
- **The writers, `compare`, and the README are not calendar-aware.** A writer
  serializes whatever relation it is handed; `compare` sees an ordinary table;
  the README renders `dim_date` as any table, and a `table:` overlay slot may
  name it.

## Related

| Document | Why |
|---|---|
| [`dimensional.md`](dimensional.md) | The one mode that honours `date_dimension` — the column-mode table, where `dim_date` is appended, the business rules `date_ref` joins, the `dim_date` name reservation |
| [`supplements.md`](supplements.md) | The sibling non-emit table surface whose consumer slot the calendar shares; the manifest distinguishes the two |
| [`temporal-elections.md`](temporal-elections.md) | The `date` election the instant shape is, the declared parse the parse shape composes, the anchor-required rule, and the bare-expression renderer authorities |
| [`anchor.md`](anchor.md) | The `EffectiveAnchor` the instant shape renders through |
| [`incremental.md`](incremental.md) | `snapshot` delivery every window, the per-window guard, the `date_ref` value-channel variance reading, the fingerprint |
| [`playback.md`](playback.md) | `dim_date` in a dimensional shape's table set and selection domain; the per-ask guard |
| [`companion-artifacts.md`](companion-artifacts.md) | The manifest's `tables[].calendar` and `columns[].references` |
| [`documentation-channel.md`](documentation-channel.md) | The forge-pinned calendar dictionary and the `date_ref` description resolution |
| [`slice-only.md`](slice-only.md) | The refusal surface both `date_ref` shapes join |
| [`../../CLAUDE.md`](../../CLAUDE.md) | Principle #3's three named exceptions — the generated calendar is the second |
