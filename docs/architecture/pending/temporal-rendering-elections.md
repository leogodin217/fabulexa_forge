---
status: draft
---

# Temporal Rendering Elections

Author-electable temporal output types — DATE, TIME, TIMESTAMP WITH TIME ZONE,
INTERVAL, and a declared VARCHAR→DATE parse — on the render surfaces that
already compute temporal values. One cross-mode election vocabulary; no new
information is derived, only the output type of already-derived values changes.

---

## Problem

The only temporal type any exporter can emit is naive microsecond `TIMESTAMP`,
and only for wallclock instants. Every temporal render is hardcoded by
expression shape, with no author election anywhere in the config grammar:

- **Wallclock instants** (the shared anchor renderer, used by every mode)
  always produce a naive local `TIMESTAMP`. The renderer computes the calendar
  date, the time of day, and the zone projection — then discards all three in
  the final cast.
- **Elapsed durations** (dimensional `derived: elapsed`) always produce a bare
  `DOUBLE`. The author already declares the unit; the value leaves untyped.
- **Domain dates** arrive in emits as ISO `YYYY-MM-DD` strings in ordinary
  VARCHAR `prop__` columns (the producer's presentation layer mints them —
  e.g. a date of birth — anchored to the run's declared calendar). They export
  as VARCHAR with no way to surface them as `DATE`.

An author targeting a realistic app database or warehouse cannot produce a
`DATE` admission-date column, a `TIME` column, a zone-carrying `TIMESTAMPTZ`,
or an `INTERVAL` wait time:

```yaml
# Today: no way to make this column a DATE. It is a TIMESTAMP or nothing.
- name: admission_date
  derived:
    timestamp: {source: sim_time}
```

The producer's position (consulted) is that this is deliberately downstream
work: the bundle carries machine-readable mechanism types only — integer-ns
`sim_time` plus the sidecar `runtime` anchor is its complete temporal surface,
calendar renderings belong to export, and no upstream temporal typing is
planned. The fix is forge's to design.

## Solution

A **temporal rendering election**: a small cross-mode config vocabulary that
elects the output type of temporal values forge already computes, attached at
each mode's existing render surfaces. Three election families:

1. **Instant renderings** — at every wallclock render site: elect
   `timestamp` (today's rendering, the default) | `date` | `time` |
   `timestamptz`. All four are projections of the same resolved instant
   through the same effective anchor.
2. **Duration rendering** — on `derived: elapsed`: elect the existing
   unit-divided numeric output (default) or `interval`, typing the delta the
   expression already computes.
3. **Declared date parse** — an explicit author declaration that a named
   VARCHAR source column contains dates in an author-specified format,
   rendered as `DATE`. Never sniffed: the ISO shape of upstream date strings
   is producer implementation detail, not contract, so both the column and
   the format are author-supplied.

All three are faithful reshaping: value-preserving re-renderings of values
that trace wholly to base-layer data. Row ordering stays pinned by raw
`sim_time`; the sidecar is untouched (exports carry no sidecar; only the
corrupter regenerates `base.json`, and it is out of scope here).

```yaml
mode: dimensional
dimensional:
  tables:
    - name: admissions
      # …
      columns:
        - name: admission_date
          derived:
            timestamp: {source: sim_time, as: date}          # instant → DATE
        - name: admitted_at
          derived:
            timestamp: {source: sim_time, as: timestamptz}   # instant → TIMESTAMPTZ
        - name: wait
          derived:
            elapsed: {correlate_on: patient_id, other_where: {prop__step: arrival},
                      start_source: sim_time, end_source: sim_time,
                      as: interval}                          # delta → INTERVAL
        - name: birth_date
          derived:
            date_parse: {from: prop__dob, format: "%Y-%m-%d"}  # VARCHAR → DATE
```

```yaml
mode: source
source:
  tables:
    - name: patients
      kind: patient
      render: {created_sim_time: date}          # structural instant → DATE
      date_parse: {prop__dob: "%Y-%m-%d"}       # payload VARCHAR → DATE
```

## Affected Subsystems

- **Effective anchor (shared renderer)** — the one SQL renderer every
  wallclock mode shares generalizes from "render a TIMESTAMP" to "render the
  resolved instant in an elected temporal type". Its contract grows the
  election parameter; the `timestamp` election reproduces today's expression
  byte-identically. The renderer remains the single authority for wallclock
  rendering semantics — every mode's election renders through it, so all
  modes stay byte-identical for the same election.
- **Config models** — the election vocabulary enters the grammar: an `as`
  election on the dimensional timestamp and elapsed derivations, a
  `date_parse` derivation kind, per-table `render` / `date_parse` maps on the
  source mode's declared tables and event log, and a per-table render
  declaration list on the base mode. One shared election value type, reused
  at every attach point (the same posture as the `keys` block and the
  row-predicate grammar: one vocabulary, per-mode attach points).
- **Dimensional exporter** — `derived: timestamp` gains the instant election;
  `derived: scd_window` gains an object form carrying the same election;
  `derived: elapsed` gains the `interval` election; `derived: date_parse` is
  a new derivation kind. The ordinal amendment (raw-ns `ORDER BY`
  substitution for rendered-time columns) extends to the new renderings.
- **Source exporter** — declared tables (`state` / `junction`) and the event
  log accept a per-table `render` map electing the rendering of their
  structural instant columns, and declared tables accept a `date_parse` map
  over payload VARCHAR columns. The mode's anchor-required posture is
  unchanged and subsumes the elections' anchor requirement.
- **Base exporter** — a per-table render declaration list elects renderings
  for lifecycle instant columns and date parses for payload VARCHAR columns,
  mirroring the mode's existing per-table `rename` structure and keyed on the
  same pre-default column identities.
- **Reader (materialization surface)** — the open emit's query session gains
  an invocation-scoped session-zone pin: when an anchor resolves, the
  anchor-resolving driver pins the session's time zone to the anchor zone
  before any relation materializes (§ Serialization and the session-zone
  pin). The pin is connection-scoped — it covers both of the reader's query
  surfaces (row-tuple and columnar) — and is set through the reader, never
  by a mode or a writer.
- **Writers** — writers stay generic (no mode, schema, or anchor knowledge),
  but the CSV writer's serialization gains pinned, machine-independent
  per-type text forms for the four new types — a byte-level contract this
  design establishes, since none exists today (§ Serialization and the
  session-zone pin). CSV parity is a commitment of this design: every
  elected type serializes deterministically under both output formats.
- **Playback (tier-2 shaped)** — no new surface: shaped playback binds an
  `ExportConfig` and reuses the modes' own compile and validation surfaces,
  so elections and their business rules (the anchor requirement included)
  flow through unchanged. Its open joins the session-zone pin: it resolves
  an anchor and materializes over the head's connection, so it pins exactly
  as the export driver does. Tier-1 playback renders instants Python-side
  and carries no elected rendering.

## What Doesn't Change

- **Default renderings are byte-identical.** A config with no election renders
  exactly today's SQL: naive µs `TIMESTAMP` instants, `DOUBLE` elapsed,
  VARCHAR payload pass-through. The `timestamp` default and the numeric
  elapsed default are mode-definitional (the published contract's existing
  renderings), not invented values.
- **Streaming.** The streaming mode's payloads are string-typed by codec
  (JSONL / Debezium carry no SQL type surface) and its `ts` rendering is a
  separate Python-side contract. No election attaches to streaming.
- **Anchor resolution.** Precedence (CLI → config `rebase` → sidecar
  `runtime`), DST and ambiguity rules, the one-anchor-per-invocation rule —
  all unchanged. Elections consume the resolved anchor; they never influence
  resolution.
- **Ordering doctrine.** Every emitted table's total order remains over raw
  sim-time keys and identity, never over rendered values.
- **No general cast knob.** The election set is closed: temporal renderings
  of instants, durations, and declared date strings. There is no "cast any
  column to any type" surface — a free-form cast could silently mangle
  sidecar values, which the faithful-reshaping principle forbids.
- **The sidecar and the contract.** No sidecar field is added, read
  differently, or proposed here. (A per-column logical-date annotation is a
  plausible future contract extension; it is not part of this design.)
- **Corrupters.** No corrupter change; `schema_drift.retype_to` remains the
  only type-breaking surface and is unrelated.
- **Incremental export.** Window membership, cursor, and fingerprint are
  computed over raw sim-time and config identity as today. An election is
  ordinary config content: changing it between windows is a config change and
  trips the existing fingerprint mismatch, with no new rule.
- **`init` proposals.** No proposal engine proposes an election or a date
  parse: proposed configs carry default renderings only, and elections
  remain author-added edits.
- **`value_map`, `ordinal` partitioning, FK resolution, key election,
  `slice_only` policy** — all untouched except where named below.

## Semantics

### The election vocabulary

One shared value set for instant renderings, used verbatim at every attach
point:

| Election | Output type | Value |
|---|---|---|
| `timestamp` (default) | naive `TIMESTAMP` (µs) | The instant's local wall clock in the anchor zone — today's rendering, byte-identical |
| `date` | `DATE` | The calendar date of that same local wall clock |
| `time` | `TIME` (µs) | The time of day of that same local wall clock |
| `timestamptz` | `TIMESTAMP WITH TIME ZONE` | The absolute instant itself (µs), zone-aware |

`date` and `time` are pure projections of the `timestamp` rendering: for any
instant, `date` equals the naive timestamp's date part and `time` its time
part, in the anchor zone. This is the defining identity of the family — the
elections never disagree with each other about what local wall clock an
instant maps to.

Duration election (elapsed only): `interval` → `INTERVAL` at µs precision.
Declared date parse: always `DATE`; the election is the declaration itself.

### Anchor requirement

| Site | Election | Anchor resolves | Anchor is `None` |
|---|---|---|---|
| Any instant site | `timestamp` (default, not explicitly elected) | wallclock `TIMESTAMP` | today's per-mode behavior: raw `BIGINT` ns (dimensional, base); error (source, whose anchor-required posture is unchanged) |
| Any instant site | any explicit election (incl. explicit `as: timestamp`) | the elected rendering | **error** — an elected rendering never falls back to raw integers. Without a declared calendar the offset is uninterpretable, and a silent raw-integer column under an elected `date` name would be a fallback |
| elapsed | `interval` | `INTERVAL` | `INTERVAL` — durations are physical deltas; no anchor is involved |
| date parse | — | `DATE` | `DATE` — the string was anchored upstream when minted; parsing reads no `sim_time` |

The explicit-election error is a plan-time business rule, not a render-time
surprise: it fires during validation, before any query runs.

### DST and zone semantics

All instant elections inherit the anchor's existing semantics — physical-ns
affine shift, DST resolved by the rendering engine's IANA tz database, no
package-local DST policy:

| Rendering | DST posture |
|---|---|
| `timestamp`, `date`, `time` | Naive local wall clock: ambiguous across a fall-back fold (two instants, one local string), steps backward at the fold. Faithful to real wall clocks; accepted today for `timestamp`, and `date` / `time` inherit it unchanged. Ordering is never affected (raw-ns doctrine) |
| `timestamptz` | Carries the absolute instant; immune to fold ambiguity. Its *display* is a serialization concern (below), but its value is exact |

A `date` election can therefore place two physically-ordered events on
calendar dates that read "backward" across a fold only in the same sense the
naive timestamp already can; no new anomaly class is introduced.

### Precision

Contract precision is ns; every rendered temporal value truncates to µs
(`timestamp` today, and `time`, `timestamptz`, `interval` alike). `date`
truncation is the day itself. Sub-µs significance is unspecified by the
contract; µs is forge's uniform presentation choice, unchanged.

### Determinism

Same emit + same config + same code version → identical output, with the
existing qualifier now stated for the whole family: **local-time renderings
are reproducible modulo the consumer's IANA tz database version.** Runs do
not pin a tzdata version (producer-confirmed), so a historical DST-boundary
shift between tz database versions can move a rendered local value. This
class already applies to today's `timestamp` rendering and is accepted, not
engineered around. `timestamptz` values (instants) and `interval` values
(physical deltas) are exempt — they carry no local projection.

Serialization of every elected type is independent of the executing
machine's locale and session zone; the mechanisms are the session-zone
pin and the pinned CSV text forms (below).

### Serialization and the session-zone pin

The anchor zone, never the session zone, governs every zone-bearing text
form. The owner of that invariant is the **anchor-resolving driver** —
the export driver, and tier-2 shaped playback's open, which reuses the
modes' compiles and joins the pin — and the mechanism is a session-zone
pin on the reader's open connection: when an anchor resolves, the driver
pins the query session's time zone to the anchor zone for the invocation,
before any relation materializes. The pin is connection-scoped, covering
both of the reader's query surfaces (row-tuple and columnar), is a pure
function of the resolved anchor (deterministic), is set through the
reader — no mode or writer touches session state — and is
invocation-scoped. When no anchor resolves, no elected rendering exists
(§ Anchor requirement) and no zone-bearing value arises, so there is
nothing to pin.

The pin reaches each output format along its real path. DuckDB output
stores zone-bearing values as instants — no text form arises there. CSV
values are serialized Python-side by the CSV writer from the
materialized Arrow values; the pinned session zone arrives as
value-attached zone metadata, so the writer stays generic (type-driven —
no mode, schema, or anchor knowledge) while its serialization gains
explicit per-type text forms for the four new types. No byte-level CSV
text-form contract exists today; **this design establishes it** for the
temporal types, pinned by writer tests. The default `TIMESTAMP` and
`DOUBLE` forms keep their existing serialization byte-identically —
incidental form and all. The four new types format by pinned rule, never
by an incidental `str()` of the in-memory value:

| Type | DuckDB output | CSV text form (pinned) |
|---|---|---|
| `DATE` | native | `YYYY-MM-DD` |
| `TIME` | native | `HH:MM:SS.ffffff` — fixed six-digit µs field |
| `TIMESTAMPTZ` | native — the exact instant; a consumer's session displays it in its own zone, which is the type's real-world behavior | `YYYY-MM-DD HH:MM:SS.ffffff±HH:MM` — the local wall clock in the anchor zone, carrying that instant's UTC offset, fixed six-digit µs field |
| `INTERVAL` | native | the signed µs delta as `[-]H:MM:SS.ffffff` — unbounded hours, fixed six-digit µs field, no calendar components (an elapsed delta is a pure physical duration) |

### The declared date parse

| Condition | Result |
|---|---|
| Source value matches the declared format | The `DATE` it denotes |
| Source value is NULL | NULL (nothing to reinterpret) |
| Source value does not match the declared format | **Loud export-time error** naming the table, column, and offending value — never a silent NULL, which would fabricate a missingness defect that is not in the data |
| Format string is incomplete or uses an unsupported directive (e.g. `%H:%M`) | Load-time config error — the declaration must denote a complete calendar date over the closed directive set (§ Validation Rules) |

Attribution is the renderer's, not the driver's: the emitted parse
expression itself carries the table and column context — an in-SQL guard
that raises the enriched message on a non-matching non-NULL value — so
the failure names its site no matter how many parses one table declares,
and no caller re-attributes a query-level error.

The strict-failure rule composes with the corrupter deliberately: a
`mutate_cells`-corrupted date string fails the parse loudly. An author
exporting a corrupted emit chooses, per column, whether to declare a parse —
the defect manifest tells them which columns carry wrong-value defects. A
`null_cells` defect flows through as NULL, faithfully.

The parse is a value-read like any other: its source column joins the
`slice_only` refusal surface (a parse from a `slice_only` column is refused
at plan time), and the source must carry a declared VARCHAR type — parsing a
non-VARCHAR column is a plan-time error, not an implicit cast. Resolution
follows each surface's existing rule. On the dimensional mode, `from`
resolves off the grain's projectable surface exactly as `value_map.from`
does (failing the existing projection-resolution rule otherwise), and the
VARCHAR gate reads the resolved column's declared type: the sidecar type for
`prop__` columns, the element-schema type for a membership grain's `elem__`
fields. A column with no declared type behind it — a structural column, a
virtual column, a grain constant — is refused as non-VARCHAR. On the
`history_interval` grain, `value` is an ordinary projectable-surface column
(the grain itself aliases the sole tracked `prop__<p>` into it), and its
declared type is the sidecar `history` table's `value` column type — the
same type authority the grain's `value_map` literal typing already reads —
so it participates under the same rule with no special case. On the source and base modes, keys are payload columns per each mode's
addressing convention (§ Per-mode attach points) and the gate reads the
sidecar type directly.

### Per-mode attach points

| Mode | Surface | Attach |
|---|---|---|
| dimensional | `derived: timestamp` | `as: <election>` on the spec; default `timestamp` |
| dimensional | `derived: scd_window` | object form `{bound: valid_from\|valid_to, as: <election>}`; the bare-literal shorthand (`scd_window: valid_from`) remains and means default rendering |
| dimensional | `derived: elapsed` | exactly one of `unit` (numeric, today) / `as: interval` |
| dimensional | `derived: date_parse` | new derivation kind `{from, format}` |
| source | declared table (`state` / `junction`) | `render:` map — structural-instant source identity → election; `date_parse:` map — payload VARCHAR source column → format |
| source | event log | `render:` map on the events block (its one instant column, by source identity) |
| base | per-table render declaration | `render:` / `date_parse:` maps keyed on the same pre-default column identities the mode's `rename` uses |

`render` map keys are **source identities** (e.g. `created_sim_time`,
`joined_sim_time`, `event_sim_time`), never output names — the same posture
as `rename`, and for the same reason: a renamed column stays addressable.
Which columns are legal keys is the reader's answer for category tables — an
instant-carrying structural column of the table's category per the
structural-temporal surface — and mode-definitional for the event log: the
log is a fold output, not a category table, so its one legal key is its own
instant column `event_sim_time`, a constant of the log's published contract,
not a reader question. A key must also name a column the render **emits**
(§ Business Rules). A `render` / `date_parse` entry re-renders the projected
column **in place** — no column is added, and the output name stays governed
by the mode's defaults and `rename`; an entry naming a column the table's
`columns` selection omits is refused at plan time, the modes' existing
posture for declarations naming omitted columns. `date_parse` keys name payload columns in
their source spelling (`prop__<p>` where the table's columns are
`prop__`-addressed; bare property names where the mode's grammar is
bare-named), matching each surface's existing column-addressing convention.

### SCD-2 window bounds

An elected `date` on `scd_window` renders a date-grained validity window — a
standard warehouse shape. Two consequences are inherent and accepted:
same-day versions collapse to `valid_from = valid_to` at date grain (the
underlying raw-ns bounds remain distinct, and version ordering is unaffected);
and the open interval's NULL `valid_to` stays NULL under every election. The
incremental driver's `valid_to` view composes unchanged: both it and the
election derive from the raw-ns bounds, the view before rendering, the
election at rendering.

### Ordering and the ordinal amendment

Row ordering is untouched. The dimensional ordinal amendment — `order_by`
naming a rendered-time column compiles to that column's raw-ns source —
keeps its existing column population: `derived: timestamp` columns and
`scd_window` `valid_from` columns, the object form `{bound: valid_from}`
joining the bare literal. A `valid_to` bound is outside the amendment
today and stays outside under every election — it orders by rendered
value like any other column. Within the population, the amendment extends
by monotonicity:

| `order_by` names an amendment column rendered as | Compiles to |
|---|---|
| `timestamp`, `timestamptz`, `date` | Raw-ns source, then `record_id` (the rendering is monotone in its source; the substitution changes output only on rendered-value ties, where raw order is event order) |
| `time` | The rendered `TIME` value, then `record_id` — time-of-day is **not** monotone in the instant, so raw-ns substitution would contradict the author's evident intent (ordering by time of day across days). Deterministic: µs-truncation ties break on `record_id` |

`interval`-rendered elapsed and `date_parse` columns are not amendment
columns; they order by value as any other column, `record_id` tie-broken
by the existing ordinal rule.

Under incremental export the windowed rule — an append-mode table's
`ordinal.order_by` must name a window-key column — becomes
**election-aware**, the one windowed change the elections force.
Window-key membership is declaration-shaped today: a column whose
declared source is the window's raw-ns column counts, blind to rendering.
It now additionally requires a window-monotone rendering. A column
elected `date` or `timestamptz` (or default-rendered) over the window's
raw-ns source remains a window key and satisfies the rule exactly as
`timestamp` does today; a `time`-elected column is excluded from the
window-key set — time-of-day is not monotone in the window — so an
append-mode `order_by` naming it is refused, the windowed-soundness
posture the amendment exists for.

### Faithfulness

Every elected value traces to base-layer values through the same derivations
that exist today; the election changes representation only. The family
identity (date/time/timestamp agreement, above), the interval's equality to
the numeric delta at µs, and the parse's value preservation (`DATE` ↔ the
source string under the declared format, round-trippable) are the testable
statements of Principle #3 for this feature.

## Configuration

```yaml
# Dimensional — elections on derived columns
columns:
  - name: admission_date
    derived:
      timestamp: {source: sim_time, as: date}
  - name: admitted_at
    derived:
      timestamp: {source: sim_time, as: timestamptz}
  - name: valid_from
    derived:
      scd_window: {bound: valid_from, as: date}
  - name: wait
    derived:
      elapsed:
        correlate_on: patient_id
        other_where: {prop__step: arrival}
        start_source: sim_time
        end_source: sim_time
        as: interval
  - name: birth_date
    derived:
      date_parse: {from: prop__dob, format: "%Y-%m-%d"}
```

```yaml
# Source — per-table maps
source:
  tables:
    - name: patients
      kind: patient
      render: {created_sim_time: date, last_mutation_sim_time: timestamptz}
      date_parse: {prop__dob: "%Y-%m-%d"}
  events:
    name: audit_log
    render: {event_sim_time: date}
    sources: [...]
```

```yaml
# Base — per-table declaration list, mirroring rename's structure
base:
  render:
    - table: records__customer
      columns: {created_sim_time: date}
      date_parse: {prop__signup_date: "%Y-%m-%d"}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `as` (timestamp spec) | `timestamp \| date \| time \| timestamptz` | No — absent means the mode-definitional default `timestamp` rendering; any set value is an explicit election | Instant rendering election |
| `scd_window` (object form) | `{bound, as}` | Both — the object form exists to elect; the bare-literal shorthand is the no-election form | Election on an SCD-2 validity bound |
| `as` (elapsed spec) | `interval` | Exactly one of `unit` / `as` | Duration rendering election; replaces the numeric unit |
| `date_parse` (derivation) | `{from, format}` | Both | Declared VARCHAR→DATE reinterpretation |
| `render` (source table / events / base entry) | map: source identity → election | No | Instant elections for structural columns |
| `date_parse` (source table / base entry) | map: source column → format | No | Declared date parses for payload columns |

## Interface Contracts

### Config Models

```python
TemporalRender = Literal["timestamp", "date", "time", "timestamptz"]
"""The instant-rendering election vocabulary, shared by every attach point."""


class TimestampSpec(StrictBaseModel):
    """A sim_time source column rendered as an elected wallclock type via the anchor."""

    source: str
    """The base-layer sim_time column to convert."""
    as_: TemporalRender | None = Field(None, alias="as")
    """The instant rendering election. Absent (`None`) means the
    mode-definitional default `timestamp` rendering (the published
    contract's existing behavior) — absence detection, not an invented
    value. Any set value — `timestamp` included — is an explicit election
    and makes the column anchor-required (business rule). Explicitness is
    structural: the field is set or it is `None`, never inferred from
    field-set state."""


class ScdWindowSpec(StrictBaseModel):
    """An SCD-2 validity bound with an instant-rendering election."""

    bound: Literal["valid_from", "valid_to"]
    """Which validity bound this column carries."""
    as_: TemporalRender = Field(alias="as")
    """The instant rendering election — required. The object form exists
    to elect (a bound-only object would duplicate the bare-literal
    shorthand), so every object form is an explicit election with the
    same anchor semantics as an explicit TimestampSpec election."""


class DateParseSpec(StrictBaseModel):
    """A declared reinterpretation of a VARCHAR source column as DATE."""

    from_: str = Field(alias="from")
    """The VARCHAR source column holding date strings (sidecar-validated)."""
    format: str
    """The author-declared parse format (strptime specifiers, closed set —
    see `format_denotes_a_date`). Must denote a complete calendar date;
    validated at load time. Never defaulted — the upstream ISO shape is
    implementation detail, not contract."""


class ElapsedSpec(StrictBaseModel):
    """A cross-row elapsed time-delta between two correlated events.

    Rendering: exactly one of `unit` (numeric delta, the existing DOUBLE
    rendering) and `as_` (`interval`, a typed INTERVAL at µs precision) is
    set. `unit` therefore becomes optional in the schema while remaining
    required in effect: omitting both is a load-time error, never a default.
    """

    correlate_on: str
    other_where: dict[str, PredicateValue]
    start_source: str
    end_source: str
    unit: Literal["minutes", "seconds", "hours"] | None = None
    """Numeric rendering: the delta divided to this unit (DOUBLE). Exclusive
    with `as_`; exactly one of the two is required."""
    as_: Literal["interval"] | None = Field(None, alias="as")
    """Typed rendering: the delta as an INTERVAL. Exclusive with `unit`."""


class DerivedSpec(StrictBaseModel):
    """A computed column; exactly one of the six derivation kinds is set."""

    ordinal: OrdinalSpec | None = None
    value_map: ValueMapSpec | None = None
    timestamp: TimestampSpec | None = None
    scd_window: Literal["valid_from", "valid_to"] | ScdWindowSpec | None = None
    """Bare literal (shorthand, default rendering) or the object form
    carrying an election."""
    elapsed: ElapsedSpec | None = None
    date_parse: DateParseSpec | None = None
    """Declared VARCHAR→DATE reinterpretation of a source column."""


class SourceTableDecl(StrictBaseModel):
    """One declared output table (existing fields elided)."""

    render: dict[str, TemporalRender] | None = None
    """Structural-instant rendering elections, keyed by source identity
    (e.g. `created_sim_time`). Keys validated against the table category's
    instant-carrying structural columns (business rule). Absent = default
    rendering for every instant column."""
    date_parse: dict[str, str] | None = None
    """Declared date parses: payload source column -> parse format. Keys
    are source column spellings per this mode's addressing convention."""


class SourceEventsDecl(StrictBaseModel):
    """The event log declaration (existing fields elided)."""

    render: dict[str, TemporalRender] | None = None
    """Rendering election for the log's instant column, keyed by source
    identity (`event_sim_time`)."""


class BaseRenderDecl(StrictBaseModel):
    """Per-table temporal elections for the base mode."""

    table: str
    """The sidecar `records__<kind>` table this entry targets (the same
    keying as the mode's rename entries; targets disjoint across entries)."""
    columns: dict[str, TemporalRender] | None = None
    """Lifecycle-instant elections keyed on pre-default column identities
    (e.g. `created_sim_time`, `deactivated_at`'s source identity).
    `last_mutation_sim_time` is outside the key domain — the mode never
    emits it (business rule)."""
    date_parse: dict[str, str] | None = None
    """Declared date parses: `prop__<p>` -> parse format."""


class BaseConfig(StrictBaseModel):
    """The base-mode section (existing fields elided)."""

    render: list[BaseRenderDecl] | None = None
    """Per-table temporal elections; entries' `table` targets disjoint."""
```

### Functions

```python
def render_anchor_temporal_expr(
    anchor: EffectiveAnchor | None,
    qualified_source: str,
    out_name: str,
    render: TemporalRender,
) -> str:
    """Render the SQL SELECT fragment for a wallclock value derived from a
    nanosecond sim_time column through the effective anchor, in the elected
    temporal type.

    Generalizes the single-rendering predecessor; the `timestamp` election
    reproduces its expression byte-identically. `date` and `time` project the
    same local wall clock; `timestamptz` renders the absolute instant. The
    interpolations remain pinned: the zone is the anchor's IANA key, the
    origin literal the anchor instant's ISO form. This function is the one
    renderer every wallclock mode shares; a mode never composes its own
    temporal SQL.

    When `anchor` is None, `render` must be the caller-side default
    `timestamp` and the raw source column is aliased through unchanged (the
    existing no-anchor path). Callers enforce the elected-rendering-requires-
    anchor rule at validation; passing a non-default election with a None
    anchor is a caller bug.

    Args:
        anchor: The resolved EffectiveAnchor, or None for the no-anchor path.
        qualified_source: The fully table-qualified BIGINT-ns source column SQL.
        out_name: The output column name (the AS alias).
        render: The elected temporal rendering.

    Returns:
        A SQL SELECT-list expression fragment ending in `AS "<out_name>"`.
    """


def render_date_parse_expr(
    qualified_source: str,
    date_format: str,
    out_name: str,
    table_label: str,
) -> str:
    """Render the SQL SELECT fragment reinterpreting a VARCHAR column as DATE
    under an author-declared format.

    NULL source values yield NULL. A non-NULL value that does not match the
    format fails the export loudly at query time — never a silent NULL. The
    renderer owns attribution: the fragment embeds an in-SQL guard that
    raises an error naming `table_label`, the source column, and the
    offending value, so the failure names its site with no caller
    re-attribution however many parses one table declares. The format string
    itself is validated at config load (must denote a complete calendar
    date); this renderer assumes a valid format.

    Args:
        qualified_source: The fully table-qualified VARCHAR source column SQL.
        date_format: The author-declared strptime-style format.
        out_name: The output column name (the AS alias).
        table_label: The output table name interpolated into the guard's
            error message.

    Returns:
        A SQL SELECT-list expression fragment ending in `AS "<out_name>"`.
    """
```

```python
def pin_session_timezone(emit: Emit, anchor: EffectiveAnchor) -> None:
    """Pin the materialization session's time zone to the anchor zone for
    this invocation.

    Called once by the anchor-resolving driver (the export driver; tier-2
    shaped playback's open) after anchor resolution, before any relation
    materializes; the pin is connection-scoped, so every materialization
    through either of the reader's query surfaces thereafter serializes
    zone-bearing values in the anchor zone (§ Serialization and the
    session-zone pin). A pure function of the
    resolved anchor: same anchor -> same session state -> byte-identical
    zone-bearing text forms on any machine. Never called by a mode or a
    writer. With no resolved anchor there is no call — no elected rendering
    exists, so no zone-bearing value arises.

    Args:
        emit: The open emit whose materialization session is pinned.
        anchor: The resolved effective anchor supplying the IANA zone.
    """
```

The elapsed builder keeps its signature; its expression grows the `interval`
branch (µs-precision INTERVAL from the same ns delta, sign-preserving).

## Validation Rules

### Parse-Time (Pydantic)

```python
@model_validator(mode="after")
def exactly_one_rendering(self) -> Self:
    """ElapsedSpec: exactly one of `unit` / `as_` is set. Omitting both is
    an error (no default rendering is invented); setting both is an error
    (the elections contradict)."""


@model_validator(mode="after")
def format_denotes_a_date(self) -> Self:
    """DateParseSpec: `format` is non-empty, uses only the closed directive
    set — `%Y`, `%y`, `%m`, `%d`, `%b`, `%B`, `%%`, plus literal text — and
    is complete: at least one year directive (`%Y` / `%y`), one month
    directive (`%m` / `%b` / `%B`), and `%d`. Any other directive (`%H`,
    locale forms like `%x`) is a load-time error. `from_` is non-empty."""


@model_validator(mode="after")
def render_maps_valid(self) -> Self:
    """SourceTableDecl / SourceEventsDecl / BaseRenderDecl: `render` and
    `date_parse` maps, when present, are non-empty with non-empty keys;
    `date_parse` formats denote complete dates; a column appears in at most
    one of the two maps."""
```

The existing `DerivedSpec` exactly-one validator extends to six kinds; the
existing base-mode entries-disjoint rule extends to the render declaration
list.

### Business Rules

| Rule | Checks | Error Message |
|---|---|---|
| `TemporalRenderRequiresAnchor` | Every explicitly-elected instant rendering (dimensional `as`, `scd_window` object form, source/base `render` entries) has a resolved effective anchor. Source mode's global anchor requirement subsumes its entries; the rule still names the offending column for uniform errors | `"column '{column}': temporal rendering '{render}' requires a resolved anchor; this emit declares no runtime calendar and none was supplied"` |
| `DateParseSourceColumn` | Each declared parse source resolves per its surface's resolution rule and carries a declared VARCHAR type (§ The declared date parse), and is not `slice_only` (the source joins the slice-only refusal surface list) | `"date_parse column '{column}' on '{table}': source must be an existing VARCHAR column (got {type})"` |
| `RenderKeyIsInstantColumn` | A declared-table (`state` / `junction`) or base-entry `render` key names an instant-carrying structural column of the table's category, per the reader's structural-temporal surface — never a payload column, never a hardcoded list. The event log's key domain is mode-definitional, not reader-sourced: the log is a fold output no category owns, so its one legal key is its own instant column `event_sim_time`. A key must also name a column the render **emits**: `last_mutation_sim_time` is outside the base key domain (the mode never emits it — the same exclusion its `rename` domain makes), and a source entry keying it under a windowed invocation — where the render omits `updated_at` — is refused by the windowed business-rule pass, the existing posture for declarations naming omitted columns | `"render key '{column}' on '{table}': not an instant-carrying structural column of this table"` |
| Incremental append-mode `order_by` (existing, amended) | Window-key membership becomes election-aware: beyond the existing declared-source match, the column's rendering must be window-monotone (`timestamp` / `date` / `timestamptz` / default). A `time`-elected column over the window's raw-ns source is excluded from the window-key set, so an append-mode `order_by` naming it is refused (§ Ordering and the ordinal amendment) | The existing rule's message, naming the column and the table's window key |
| `LookupColumnSafety` (existing) | Unchanged; `date_parse` is not a lookup surface | — |
| `SliceOnlyColumnRefused` (existing) | Surface list grows the `date_parse` source read | (existing message) |
