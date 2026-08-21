---
status: draft
---

# Value Rendering Elections

Author-elected numeric and payload-instant renderings across the export modes,
unified with the existing per-column rendering declarations under one
property-first `render:` map.

---

## Problem

Exported numeric values carry raw float64 precision wherever a scenario
computes a continuous quantity. The realism QA round flagged two, both inside
JSON payloads — retail's `fact_customer_action.context` carries
`"discount_pct": 0.027498429157793724`, saas's `usage_events.context` carries
`"volume": 65.38160578755163` — and every plain DOUBLE property
(`error_rate`, `demand_pressure`, `margin`, `cost_per_bed_day`) has the same
exposure. No real instrumentation records 17 significant digits; a cold read
flags the dataset as synthetic on sight. Forge has no numeric rendering
surface at all: property values pass through verbatim in every mode, and the
only precision-touching lever is a corrupter mutation — defect injection, the
wrong tool for a presentation choice.

A sibling gap has the same shape. A payload BIGINT that holds a sim-instant
(nhs `booking.prop__requested_at` / `prop__opening_at`) renders as wallclock
in dimensional — `derived: {timestamp: {source: prop__requested_at}}` is
legal there — but ships as raw sim-epoch ns in source and base, whose
`render:` maps accept only *structural* instant columns. The same emit
renders wallclock in one mode and raw ns in another, and the author has no
way to close the gap: there is no surface for declaring what a payload
column's value *is*.

Finally, the declaration grammar that would host the fix is itself awkward:
source and base carry one map per election kind (`render:` for structural
instants, `date_parse:` for parses), so the same column can be named in two
maps and a dedicated cross-map disjointness validator exists only to catch
that collision. Adding three more election kinds as three more maps would
multiply that validator quadratically.

## Solution

One cross-mode surface — **value rendering elections** — extending the
election family (temporal elections, key election) with three new
author-declared renderings, and restructuring the source/base declaration
grammar so *one property-first `render:` map* answers "how does this column
render":

```yaml
render:
  created_sim_time: date                       # temporal shorthand (unchanged meaning)
  prop__birth_date: {date_parse: "%Y-%m-%d"}   # the declared parse, absorbed
  prop__requested_at: {instant: timestamp}     # NEW: payload BIGINT declared a sim-instant
  prop__error_rate: {decimal: [4, 3]}          # NEW: exact DECIMAL(4,3) out
  prop__context: {json_precision: {discount_pct: 2}}  # NEW: named JSON leaf -> 2dp
```

- **`decimal`** — a DOUBLE payload column renders as author-declared
  `DECIMAL(p, s)`: exact decimal out, loud error on overflow.
- **`instant`** — a payload BIGINT is declared a sim-time instant and renders
  through the existing instant-election vocabulary (`timestamp` / `date` /
  `time` / `timestamptz`) and the anchor, exactly as structural instants do.
- **`json_precision`** — for a declared JSON payload column, named top-level
  numeric leaves render rounded to N fraction digits **in place**; every
  other byte of the payload is preserved verbatim.

Dimensional keeps its per-column posture: `derived: {decimal: …}` and
`derived: {json_precision: …}` join the derived one-of as siblings of
`derived: {timestamp: …}` (which already covers the payload-instant case
there). Streaming attaches the two *numeric* elections per declared stream;
the temporal family's streaming exclusion stands. In source mode the
elections reach the event log: an audited property's `changes` entries carry
its elected form, inherited from the kind's declared tables under a
per-kind agreement rule scoped to the properties the log renders. A kind
audited with no declared table keeps raw codec text in `changes` — the
log-only declaration surface is deferred scope (§ Boundaries). One
rendering authority per election kind keeps every mode byte-identical for
the same election.

Elections are renderings composed **above** the faithful read. The reader and
the derivations layer are untouched: conformance and the corrupters keep
reading unrendered values. `compare` changes exactly once — a decimal
canonical family, so a decimal-elected render stays comparable
(§ Affected Subsystems).

## Affected Subsystems

- **Export-config models and loader** — the source and base per-table
  declaration grammar restructures: `render:` becomes a property-first map
  whose values are either the bare temporal-election shorthand (a scalar, for
  structural instant columns — today's shape, unchanged meaning) or a typed
  election object (`date_parse` / `instant` / `decimal` / `json_precision`).
  The base entry's `columns` map is renamed `render` — one name for the one
  map across both modes — and the standalone `date_parse:` maps on
  `SourceTableDecl` and the base render declaration are absorbed and removed
  — a breaking grammar change; existing configs and recipes migrate. The render/date-parse disjointness validator
  disappears: one map makes the collision unrepresentable. Dimensional's
  derived one-of gains `decimal` and `json_precision` members. Stream
  declarations (`KindStream` / `MembershipStream`) gain a `render:` map
  restricted to the numeric elections. `events` source declarations gain
  no field.
- **Shared SQL rendering utilities** — two new rendering authorities, each
  the single compiler for its election everywhere it attaches (the
  `render_anchor_temporal_expr` / `render_date_parse_expr` posture): the
  decimal-render expression, and the json-precision renderer — a
  Python-side exact token replacer (RE2-class SQL regex cannot distinguish
  a top-level key from the same name nested deeper; DuckDB's JSON functions
  re-serialize, re-spelling undeclared bytes) registered as a DuckDB scalar
  function and invoked by the compiled expression. The payload `instant`
  election compiles through the existing wallclock renderer; no new
  temporal authority.
- **Reader** — one session-setup step at open: registering the shared
  utilities' rendering scalar function on the emit's connection
  (`register_render_functions`), the same species of connection-scoped
  setup as the session-zone pin. No read-surface change: the faithful read,
  the sidecar surface, and every query API are untouched, and nothing in
  conformance or corrupter SQL calls the registered function.
- **Dimensional exporter** — two new derived kinds; the payload-instant case
  is already covered by `derived: timestamp` and does not change.
- **Source exporter** — the unified `render:` map on declared tables and the
  event log. On a `state` table the typed keys name payload columns of the
  table's kind; on a `junction` table they name the `elem__<f>` element
  columns — the junction's source identities, exactly as `columns` /
  `rename` and today's `date_parse` address them; the member pair columns
  (`member__<f>__kind` / `member__<f>__id`) are outside the typed-election
  domain (reference identity is key election's surface); the event log
  keeps its single structural block-level key, `event_sim_time`. Elected
  properties — every election kind — also render inside the audit log's
  `changes` entries under the per-kind agreement rule (§ Event-log and
  after-image reach); changeset membership and `id` numbering stay
  raw-value facts.
- **Base exporter** — the same unified map on the per-table render
  declaration list.
- **Streaming exporter** — per-stream `render:` maps carrying `decimal` and
  `json_precision` only; an elected property's after-image entry carries the
  elected text form instead of the raw codec string. Value schemas, message
  keys, ordering, and `seq` are untouched.
- **Writers** — the pinned CSV text form for `DECIMAL(p, s)`: the plain
  scale-digit decimal string (sign included, exactly `s` fraction digits —
  `s = 0` renders the bare integer text with no decimal point — no
  exponent), joining the pinned temporal text forms.
- **Validation runner** — new plan-time business rules (source-type gates
  per election kind, the per-kind election agreement gate) and export-time
  attributed failure guards (overflow, bad JSON payloads), detailed under
  Validation Rules.
- **Compare surface** — the canonical family table gains a **decimal**
  family (`DECIMAL` expected ↔ any `DECIMAL` actual, compared as exact
  decimal values; the family's canonical value encoding normalizes scale —
  trailing fractional zeros strip — so equal values at different declared
  scales encode identically), so a decimal-elected render stays inside the shape
  `compare` defines equality for — the same accompaniment the temporal
  elections' four families made. No other family, tolerance, or verdict
  semantics move.

## What Doesn't Change

- **The faithful read and the derivations layer.** The read surface is
  untransformed; elections compose above it. The reader's one change is
  session setup, not reading — it registers the rendering scalar function
  at open (§ Affected Subsystems) — and no query or sidecar surface moves.
  C1–C14 conformance and the corrupters read unrendered values exactly as
  today; `compare`'s only change is the decimal canonical family
  (§ Affected Subsystems).
- **Default renderings are byte-identical.** A config with no election
  renders exactly today's SQL — DOUBLE payloads verbatim, BIGINT payload
  instants raw, JSON payloads untouched.
- **The temporal election vocabulary and its semantics** — anchor rules, DST
  posture, precision, the declared-parse directive rules (vocabulary,
  pairing, uniqueness, completeness, strict failure). The parse moves
  *where it is declared* (into the unified map); the parse itself is
  unchanged. Its one behavior change is reach, not semantics: parsed and
  instant-elected properties now render inside the event log's `changes`
  entries (§ Event-log and after-image reach). Dimensional's `derived:`
  spellings are all unchanged.
- **Streaming's temporal exclusion.** Streaming still carries no temporal
  election and its `ts` rendering contract is untouched.
- **Key election, anchor resolution, row ordering.** Identity surfaces,
  the one-anchor rule, and every table's raw-sim-time total order are
  unaffected; ordering never derives from a rendered value.
- **Event sets, changeset membership, and audit `id` numbering.** Which
  events exist, which audited properties an `update` row carries, whether a
  row is suppressed, and how the log numbers its rows are raw-value facts;
  no election can create, suppress, or renumber a row (§ Event-log and
  after-image reach).
- **The incremental driver.** An election is ordinary config content under
  the existing fingerprint; `decimal` / `json_precision` render
  non-temporal values and are irrelevant to window keys. A payload `instant`
  column is never a structural window key, so the window-membership rule is
  untouched.
- **The corrupter family and the no-general-cast invariant.** The election
  set stays closed; `schema_drift.retype_to` remains the only type-breaking
  surface. `decimal` is not a general cast: it admits `DOUBLE` sources
  only and refuses out-of-range values instead of mangling them.

## Semantics

### The unified render map

`render:` keys are **source identities** (the `rename` posture: a renamed
column stays addressable by its source name). Map values are:

| Value shape | Meaning |
|---|---|
| bare scalar (`date`, `time`, `timestamp`, `timestamptz`) | Temporal election on a structural instant column — today's `render:` entry, unchanged |
| `{date_parse: "<format>"}` | The declared parse — today's `date_parse:` entry, absorbed |
| `{instant: <temporal election>}` | Payload sim-instant declaration + rendering |
| `{decimal: [p, s]}` | Numeric precision rendering |
| `{json_precision: {<key>: <digits>, …}}` | JSON payload leaf rounding |

One column, one election — YAML key uniqueness makes a conflicting pair
unrepresentable. Every entry re-renders the projected column **in place**: no
column is added, and output naming stays governed by the mode's defaults and
`rename`. A key must name a column the table emits; a key naming an omitted
or non-existent column is refused at plan time (the modes' existing posture).
Each value form has a fixed key domain: the bare shorthand addresses
instant-carrying structural columns only, and the typed forms address
payload columns of the table's kind only — a typed election naming a
structural column (`created_sim_time: {instant: timestamp}`) is refused at
plan time, so no rendering ever has two spellings.
On a source `junction` table the typed forms key the `elem__<f>` element
columns — the junction's source identities, exactly as `columns` / `rename`
and the absorbed `date_parse` address them — and the source-type gates read
each `elem__<f>` column's declared type. The member pair columns
(`member__<f>__kind` / `member__<f>__id`) are outside the typed-election
key domain — reference identity is key election's surface. The
structural-instant shorthand keys (`joined_sim_time` / `left_sim_time`) are
unchanged. An
elected column's source joins the `slice_only` refusal surface exactly as
a `date_parse` source does today.

The events *block*'s own map keeps its one structural shorthand key,
`event_sim_time`. `events` sources carry no election map of their own —
the log inherits the declared tables' elections (§ Event-log and
after-image reach).

### The `decimal` election

Output type `DECIMAL(p, s)`, rendered by the one decimal authority.

| Condition | Result |
|---|---|
| Source value is a finite float within `DECIMAL(p, s)` range | The value rounded to `s` fraction digits; ties round away from zero |
| Source value is `NULL` | `NULL` of the output type |
| Source value overflows `p − s` integer digits | Loud export-time error naming the table, column, and offending value — never a silent `NULL` or saturation |
| Source value is `NaN` / `±Infinity` | The same loud export-time error — no decimal representation exists |
| Source column's declared type is not DOUBLE — the contract's one floating-point type | Plan-time error; integers and VARCHARs have no precision to elect |

Rounding is a pure function of the value — no anchor, no zone, no tzdata
qualifier. The tie rule is stated (away from zero) and testable on exact
binary halves; values that are not exact binary halves round by their actual
binary value, which is the honest reading of a float64 source.

### The `instant` election

An author assertion that a payload BIGINT column carries sim-time ns, plus a
rendering from the existing instant vocabulary. Everything downstream of the
assertion is the temporal family's existing contract:

| Condition | Result |
|---|---|
| Election present, anchor resolves | The elected rendering via the shared wallclock renderer — identical semantics to a structural instant (DST posture, µs precision, tzdata qualifier) |
| Election present, anchor is `None` | Load/plan-time error — an elected rendering never falls back to raw integers (the explicit-election posture; source's global anchor requirement subsumes this for source) |
| Source value is `NULL` | `NULL` of the elected type |
| Source column's declared type is not BIGINT | Plan-time error — the assertion is checkable only against an integer sim-offset column |

The assertion itself is author-supplied and unverifiable — the sidecar
declares no instant marker for payload properties, and forge does not sniff.
A wrong assertion renders garbage wallclocks deterministically; that is the
same trust the `date_parse` format already receives.

### The `json_precision` election

Declared per JSON payload column as a map of **top-level key → fraction
digits**. The rendering is in-place token replacement, not re-serialization:

| Condition | Result |
|---|---|
| Payload is `NULL` | `NULL` |
| Payload is a valid JSON object; a declared key is present with a numeric value | That value's token is replaced by its rounded decimal text with exactly the declared fraction digits (ties away from zero); **every other byte of the payload — whitespace, key order, undeclared values — is preserved verbatim** |
| A declared key is absent from a row's payload | That row's payload is unchanged for that key — payload shapes legitimately vary per row (retail's `discount_pct` exists on ~3% of rows) |
| A declared key is present with the JSON literal `null` | That row's payload is unchanged for that key — no error, no notice. `null` is the payload-interior spelling of missingness, the same fact SQL `NULL` states at column level and absence states at object level; nothing to round, nothing fabricated |
| A declared key is present with a non-numeric, non-`null` value (string, object, array, boolean) | Loud export-time error naming the table, column, key, and offending value |
| A declared key appears more than once at top level | Loud export-time error — duplicate keys have no single value to round |
| Payload is non-`NULL` and not a valid JSON object | Loud export-time error naming the table, column, and value — electing the column asserts it is a JSON object, and an unparseable payload is a surfaced author error, never a silent pass-through |

Rounding operates on the **decimal number the token denotes** — exact
decimal arithmetic over the token text, never a float64 re-parse, which
would move ties (`0.005` at two digits is `0.01` as a decimal and `0.00`
through the nearest float64). Ties round away from zero at the exact
decimal half. A rounded token carries exactly the declared fraction digits;
zero digits renders the bare integer text with no decimal point. Rounded
text is always plain decimal notation — a source token in exponent
notation (`6.5e1`) rounds to its plain-form text — and a value that rounds
to zero renders unsigned (`-0.001` at two digits is `0.00`, never
`-0.00`), the no-negative-zero posture SQL `DECIMAL` gives the decimal
election.

Nested paths and wildcard "all numeric leaves" are deliberately outside the
grammar (§ Boundaries). Byte preservation of undeclared content is the
faithfulness statement for this election: the payload is upstream-minted
data, and the election touches exactly the tokens the author named.

### Cross-mode identity and determinism

One rendering authority per election kind: every mode compiles the same
election to the same expression, so the same emit + config renders
byte-identically in every mode that attaches it. `decimal` and
`json_precision` are pure value functions (no tzdata qualifier) — the
registered json-precision scalar is a pure function of its arguments, so
routing through it changes nothing in the determinism statement; `instant`
inherits the temporal family's determinism statement unchanged.

### Streaming attach

A declared stream's `render:` map accepts `decimal` and `json_precision`
entries keyed by the stream's bare property (or membership field) names.

| Site | Rendering |
|---|---|
| After-image entry of an elected property (`c` / `u`) | The elected text form — the decimal string with `s` fraction digits, or the leaf-rounded payload — in place of the raw codec string |
| `d` tombstone | Unaffected — carries no after-image |
| Debezium value schema | Unaffected — elected entries remain string-typed by codec; the election changes value text only |
| Message key, merge order, `seq`, `ts` | Unaffected — identity is key election's, ordering is raw sim-time's |

The authorities apply at the codec seam — the fold itself is untouched:
`decimal` applies to the after-image entry's codec string cast back to its
declared `DOUBLE` type (exact — the codec round-trips float64 values),
`json_precision` to the payload string directly (it is already the
payload's own text). Both compile through the shared authorities in the
stream's SQL pipeline — the post-fold SELECT that assembles after-images —
so payload assembly receives already-elected text and no mode carries a
second rounding implementation. The elected text is therefore identical to
the table modes' render of the same value.

The temporal elections (`instant`, `date_parse`, structural shorthand) do not
attach to streaming (§ Boundaries).

### Event-log and after-image reach (source mode)

The log renders `changes` values per **kind**, but elections are declared
per **declared table** — the log declares none of its own and inherits the
tables'. A kind may have several declared tables (sub-type splits), so the
reach rule resolves the grain difference explicitly. Per property of a
kind (per element field of a `(kind, property)` membership — a junction
table's `elem__<f>` election reaches the log's bare field `<f>`, the
junction render's own name strip):

| Declared tables emitting the property | Its `changes` rendering |
|---|---|
| Every one declares the identical election | The elected form |
| None elects it | The raw codec string, exactly today |
| The declarations differ — two differing elections, or an electing table beside a silent one (silence asserts the default raw rendering) — and the log renders the property | Plan-time refusal (`ElectionKindConflict`) — the log has one rendering per property, and a raw table column beside an elected log is the same mixed rendering |
| The declarations differ, and no log renders the property (no `events` block, no source addressing the kind, or the property outside every addressing source's audited set) | Legal — the tables are independent output surfaces, with no log rendering to disagree about |

The gate is scoped to what the log renders: adding an `events` source over
a kind can make a previously-legal pair of differing — or
elected-beside-silent — table declarations illegal; the moment one log
must render the property, the refusal fires, naming both tables. The
escape hatch is the audited set itself: narrowing the property out of
every source's audited set (`only` / `ignore`) removes it from the gate's
scope, so a deliberately mixed table rendering stays representable — at
the declared price of not auditing the property. A kind audited with no
declared table renders raw codec text — the log-only declaration surface
is deferred (§ Boundaries).

**Every election kind reaches the log.** `decimal` and `json_precision`
entries carry the elected text; an `instant` or `date_parse` election
renders its `changes` entries as the elected temporal value's text — a
deliberate behavior change for the declared parse, whose entries today ride
the raw codec string. In-JSON temporal text is pinned to the same per-type
forms the writers pin for CSV ([`writers.md`](../writers.md) § Temporal
text forms). Naive `TIMESTAMP` — which the writers deliberately leave at
the default serialization — is pinned here for the in-JSON site:
`YYYY-MM-DD HH:MM:SS.ffffff`, the six-digit µs field omitted entirely when
the instant's microseconds are zero. That *is* the writer's
default-timestamp serialization, stated as a form so the table-column ↔
`changes`-entry identity stays byte-testable. All forms are produced by
explicit formatting in the JSON assembly — never an incidental `VARCHAR`
cast, whose fraction field trims to significant digits and matches no
pinned form.

Under that resolution an elected property renders identically — as text —
in the declaring table's column and in the log's `changes` entries. The
application site is the mode's, not the fold's: the derivations folds still
emit codec `VARCHAR` after-images, and the log applies each authority to
the codec string cast back to its declared source type (exact — the codec
round-trips `DOUBLE` and `BIGINT`; a `date_parse` or `json_precision`
source is already `VARCHAR`), applied to the emitted `[old, new]` values —
the site at which key election renders reference entries; rendering is a
pure per-value function, so lag-then-render and render-then-lag agree over
the displayed pair. The diff's *comparison* inputs are never rendered
(below): key election may sit on either side of the diff because elected
surfaces are injective, but rounding is not, so here the raw-diff rule is
load-bearing. The `create` after-image and every `u` delta carry
the elected form, and the export-time guards — parse mismatch, decimal
overflow, the JSON payload guards — fire at the log site exactly as at a
table site: a log can fail loudly on a value no declared table selects.

**Changeset membership is a raw-value fact.** The audited-property diff —
which properties an `update` row carries, and whether the row is suppressed
outright — compares raw, unrendered after-image values, so the event set
and the log's dense `id` numbering are election-invariant: a presentation
choice can never suppress or renumber a row. Two raw values that round to
one decimal text therefore display as an equal-looking `[old, new]` pair —
the honest rendering, at the elected precision, of a real underlying
change.

## Configuration

```yaml
# source — per-table unified render map (property-first)
- name: storefront
  kind: entity
  sub_types: [infrastructure]
  columns: [created_sim_time, active, deactivated_at, last_mutation_sim_time,
            prop__status, prop__error_rate]
  render:
    prop__error_rate: {decimal: [4, 3]}

- name: booking
  kind: booking
  render:
    prop__requested_at: {instant: timestamp}
    prop__opening_at: {instant: timestamp}

- name: customer_action
  kind: tick_decision
  render:
    created_sim_time: timestamp
    prop__context: {json_precision: {discount_pct: 2}}
```

```yaml
# base — the per-table render entry; `columns` renamed `render`, unified
render:
  - table: records__storefront
    render:
      created_sim_time: date
      prop__error_rate: {decimal: [4, 3]}
```

```yaml
# dimensional — the derived one-of gains two members
- {name: error_rate, derived: {decimal: {from: prop__error_rate, as: [4, 3]}}}
- {name: context, derived: {json_precision: {from: prop__context, leaves: {discount_pct: 2}}}}
```

```yaml
# streaming — per-stream numeric render map
streams:
  - name: usage
    kind: journey_instance
    properties: [current_state, volume, context]
    render:
      volume: {decimal: [8, 1]}
      context: {json_precision: {discount_pct: 2}}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `render` (source table / base entry / stream) | map: source identity → election | No | Per-column rendering election; absent = default rendering |
| `decimal` | `[p, s]` — two ints | Yes (within its entry) | Output `DECIMAL(p, s)`; `1 ≤ p ≤ 38`, `0 ≤ s ≤ p` |
| `instant` | temporal election literal | Yes (within its entry) | `timestamp` \| `date` \| `time` \| `timestamptz` |
| `json_precision` | map: top-level key → digits | Yes (within its entry) | Non-empty; digits `0 ≤ n ≤ 12` |
| `date_parse` | format string | Yes (within its entry) | Unchanged parse semantics; relocated spelling |
| dimensional `derived.decimal` | `{from, as: [p, s]}` | — | Same election, dimensional's per-column spelling |
| dimensional `derived.json_precision` | `{from, leaves: {key: digits}}` | — | Same election, dimensional's per-column spelling |

## Interface Contracts

### Config Models

```python
class DecimalElection(StrictBaseModel):
    """Numeric precision rendering: DOUBLE source -> DECIMAL(p, s)."""

    decimal: tuple[int, int]
    """(precision, scale); 1 <= precision <= 38, 0 <= scale <= precision."""


class InstantElection(StrictBaseModel):
    """Payload sim-instant declaration: BIGINT ns source, rendered via the
    anchor through the shared instant-election vocabulary."""

    instant: Literal["timestamp", "date", "time", "timestamptz"]
    """Which instant rendering the declared ns offset receives."""


class JsonPrecisionElection(StrictBaseModel):
    """In-place rounding of named top-level numeric leaves of a JSON payload."""

    json_precision: dict[str, int]
    """Top-level key -> fraction digits (0..12); non-empty."""


class DateParseElection(StrictBaseModel):
    """The declared parse, relocated into the unified render map; format
    semantics unchanged."""

    date_parse: str
    """strptime-style format; validated by the shared format rules."""


#: A render-map value: a bare temporal-election literal (structural instant
#: shorthand) or one typed election object. Source identity -> RenderElection.
RenderElection = (
    Literal["timestamp", "date", "time", "timestamptz"]
    | DateParseElection
    | InstantElection
    | DecimalElection
    | JsonPrecisionElection
)


class DecimalSpec(StrictBaseModel):
    """Dimensional derived spelling of the decimal election."""

    from_: str = Field(alias="from")
    """The grain-surface source column (DOUBLE payload column)."""
    as_: tuple[int, int] = Field(alias="as")
    """(precision, scale), same bounds as DecimalElection."""


class JsonPrecisionSpec(StrictBaseModel):
    """Dimensional derived spelling of the json_precision election."""

    from_: str = Field(alias="from")
    """The grain-surface source column (VARCHAR JSON payload)."""
    leaves: dict[str, int]
    """Top-level key -> fraction digits (0..12); non-empty."""
```

`SourceTableDecl.render` and `BaseRenderDecl.columns` — renamed `render`,
so a base entry is `{table, render}` — change type to
`dict[str, RenderElection] | None`; the standalone `date_parse` fields are
removed. The events block's own render field keeps its narrow
temporal-shorthand type: its one legal key is the structural
`event_sim_time`, so the typed forms are unrepresentable there rather than
representable-but-refused. `events` source declarations gain no field.
`KindStream` / `MembershipStream` gain
`render: dict[str, DecimalElection | JsonPrecisionElection] | None` keyed by
bare property / field name. `DerivedSpec`'s one-of gains `decimal` and
`json_precision` members.

### Functions

```python
def render_decimal_expr(
    source_expr: str,
    precision: int,
    scale: int,
    column_label: str,
    table_label: str,
) -> str:
    """
    Compile the decimal election to its SQL expression — the one decimal
    rendering authority every mode composes. Returns a bare expression
    (no alias); callers alias per their own naming — the
    render_date_parse_expr attribution posture, plain string labels.

    Args:
        source_expr: SQL expression producing the DOUBLE source value.
        precision: Declared DECIMAL precision (1..38).
        scale: Declared DECIMAL scale (0..precision).
        column_label: The column name interpolated into the guard's error
            message.
        table_label: The output table name interpolated likewise.

    Returns:
        A SQL expression yielding DECIMAL(precision, scale); NULL in, NULL
        out; ties away from zero; raises the enriched conversion error in
        SQL on overflow, NaN, or infinity, naming table, column, and the
        offending value.
    """


def render_json_precision_expr(
    source_expr: str,
    leaves: Mapping[str, int],
    column_label: str,
    table_label: str,
) -> str:
    """
    Compile the json_precision election to its SQL expression — a call to
    the registered scalar (forge_json_precision below), the one JSON-leaf
    rendering authority every mode composes. Returns a bare expression
    (no alias). The leaf map and the two attribution labels are spliced as
    constant arguments of the call.

    Args:
        source_expr: SQL expression producing the VARCHAR JSON payload.
        leaves: Top-level key -> fraction digits, non-empty.
        column_label: The column name for guard attribution.
        table_label: The output table name for guard attribution.

    Returns:
        A SQL expression yielding the scalar's result (contract below):
        the payload with declared leaves rounded in place, all other bytes
        preserved; NULL in, NULL out.
    """


def forge_json_precision(
    payload: str | None,
    leaves_json: str,
    column_label: str,
    table_label: str,
) -> str | None:
    """
    The json_precision scalar — exact, byte-preserving token replacement
    over one payload. Registered on the emit's connection at open
    (register_render_functions) and invoked only by expressions
    render_json_precision_expr compiles. Python-side because the
    transformation is not expressible in SQL: RE2-class regex cannot
    distinguish a top-level key from the same name nested deeper, and JSON
    re-serialization would re-spell undeclared bytes. A pure function of
    its arguments.

    Args:
        payload: The JSON payload text, or None (SQL NULL).
        leaves_json: The declared leaf map as a compact JSON literal
            ('{"discount_pct": 2}'), spliced as a constant by the compiler.
        column_label: The column name for guard attribution, spliced as a
            constant by the compiler.
        table_label: The output table name likewise — two labels, so the
            raised message composes the guards' shared column-on-table
            shape without re-splitting a fused string.

    Returns:
        The payload with each present, numeric declared top-level leaf's
        value token replaced in place by its rounded decimal text — exact
        decimal arithmetic on the token, exactly the declared fraction
        digits, ties away from zero, plain decimal notation, unsigned when
        the rounded value is zero — all other bytes preserved; None for
        None; a leaf present as the JSON literal `null` left verbatim
        (missingness, not a contradiction).

    Raises:
        ValueError: invalid payload, non-numeric non-null declared leaf, or
            duplicate top-level key — the enriched message naming the
            table, column, and key; DuckDB surfaces it as the query's
            failure.
    """


def register_render_functions(conn: duckdb.DuckDBPyConnection) -> None:
    """
    Register the shared rendering scalar functions — today exactly one,
    forge_json_precision — on a connection. Called once by the reader at
    open: connection-scoped session setup, the session-zone pin's species.
    Every registered function is a pure function of its arguments, so
    registration adds nothing to the determinism statement.

    Args:
        conn: The emit's DuckDB connection.
    """
```

The payload `instant` election compiles through the existing
`render_anchor_temporal_expr`; no new temporal renderer exists. Streaming
and the event log apply the same authorities at the codec seam (§ Streaming
attach, § Event-log and after-image reach), so the elected text is
identical at every attach site.

## Validation Rules

### Parse-Time (Pydantic)

```python
@model_validator(mode="after")
def decimal_bounds(self) -> Self:
    """1 <= precision <= 38; 0 <= scale <= precision."""

@model_validator(mode="after")
def json_precision_shape(self) -> Self:
    """Leaf map non-empty; keys non-empty; 0 <= digits <= 12."""
```

The render map's shape (source-identity keys, one election per key) is
carried by the model type; the absorbed parse keeps the shared format
validation (closed vocabulary, pairing, uniqueness, completeness) unchanged.

### Business Rules

| Rule | Checks | Error Message |
|---|---|---|
| `RenderKeyIsInstantColumn` (amended; renamed `RenderKeyResolves`) | Every render-map key names a column the table emits, in its form's domain: the structural-shorthand form requires an instant-carrying structural column (the shipped rule's check); the typed forms require payload columns of the table's kind (`elem__<f>` element columns on a junction; the member pair columns are outside the domain) — a typed election naming a structural column is refused | Existing message shapes, per form |
| `ElectionKindConflict` | Across the declared tables of one kind (one `(kind, property)` membership: junction tables) that emit a source property **the event log renders**, every table declares the identical election — a silent emitting table counts as differing, its column asserting the default raw rendering; the gate runs only for properties inside some `events` source's audited set; tables differing on a property no log renders are legal (§ Event-log and after-image reach) | `"property '{column}' of kind '{kind}': '{a}' and '{b}' declare conflicting render elections"` — or, silent-table shape, `"… '{a}' declares a render election and '{b}' declares none"` — `{a}` / `{b}` naming the two tables |
| `DecimalSourceIsDouble` | A `decimal` election's source column carries a declared DOUBLE type | `"render key '{column}' on '{table}': decimal rendering requires a DOUBLE source (got {type})"` |
| `InstantSourceIsBigint` | An `instant` election's source column carries a declared BIGINT type | `"render key '{column}' on '{table}': instant rendering requires a BIGINT sim-time source (got {type})"` |
| `TemporalRenderRequiresAnchor` (extended) | Payload `instant` elections join the explicitly-elected instant set the rule already covers | The rule's existing message |
| `JsonPrecisionSourceIsVarchar` | A `json_precision` election's source column carries a declared VARCHAR type | `"render key '{column}' on '{table}': json_precision requires a VARCHAR JSON payload source (got {type})"` |
| Slice-only refusal (existing surface) | An elected source column is not `slice_only` | The surface's existing per-mode messages |
| Export-time overflow guard | In-SQL, per the decimal authority: overflow / NaN / infinity | `"column '{column}' on '{table}': value {value} does not fit DECIMAL({p},{s})"` |
| Export-time payload guards | In-SQL, per the json authority: invalid payload, non-numeric non-`null` declared leaf, duplicate top-level key | `"column '{column}' on '{table}': json_precision key '{key}': {reason} (value: {value})"` |

The source-type gates cover every attach point of their election, not the
render-key form alone: the dimensional `derived` spellings check the same
rules against their `from` column through the mode's grain-projection
resolution; streaming runs them per declared stream against the kind's
sidecar types (a stream `render:` key must name a declared property or
membership field of that stream's projection) — the
`validate_date_parse_format` posture: one rule per check, each mode's
message naming its own addressing.

## Rationale

**The closed set stays closed.** These are elections, not a cast knob: each
admits one source type, renders one declared representation, and refuses
out-of-range values loudly instead of mangling them. `decimal` on a DOUBLE
is the numeric analog of ns→µs truncation — a declared, deterministic,
presentation-precision choice; `instant` is the same declaration
`date_parse` already makes for strings, applied to the contract's own ns
encoding; `json_precision` touches exactly the tokens the author names.

**Property-first, one map.** Kind-first maps (today's `render:` +
`date_parse:`) let one column be claimed twice and need a disjointness
validator; five kinds would need ten. Property-first makes the conflict
unrepresentable, gives authors one place that answers "how does this column
render", and matches dimensional's per-column posture — the cross-mode story
becomes: dimensional elects on the column declaration, source/base elect on
the column key. Absorbing `date_parse` is a breaking spelling change with no
semantic change, taken now because the grammar is pre-1.0 internal surface
and the alternative is permanent two-map asymmetry.

**In-place replacement, not re-serialization.** A parse-and-reserialize
implementation of `json_precision` would re-spell every number and reorder
or re-whitespace content the author never declared — mangling upstream
bytes under an election that promised to touch one leaf. Token replacement
keeps the faithfulness statement crisp and testable: bytes differ only at
declared, present, numeric leaves. The authority is Python-side because no
in-SQL tool honors both promises at once: RE2-class regex cannot
distinguish a top-level key from the same name legally nested in
undeclared content, and DuckDB's JSON functions re-serialize. A scalar
function registered by the reader at open keeps the one-authority shape —
the compiled expression is still the single call site — at the cost of one
connection-setup step the reader already has a home for (the session-zone
pin).

**Every election kind reaches the log.** A property that renders wallclock
in its table column but raw ns in the same export's `changes` entries
would reproduce, inside one mode, exactly the cross-surface inconsistency
this design exists to close. The declared parse's reach is the one
behavior change taken with it — today its `changes` entries ride the raw
codec string — accepted for the same identity: one property, one
rendering, every surface. Pinning the in-JSON temporal text to the
writers' CSV forms keeps that identity byte-testable instead of resting on
an incidental cast.

**Missingness passes; contradiction fails.** The `json_precision` split
mirrors the declared parse's: SQL `NULL`, an absent member, and a JSON
`null` leaf are three spellings of one fact — no value here — and all
three pass through byte-verbatim, fabricating nothing. A present string,
object, array, or boolean *contradicts* the author's numeric assertion and
fails loudly, exactly as a non-matching string fails the declared parse.
Erroring on `null` would treat the two members of the election family
oppositely on missingness, and would leave an author whose emit carries one
minted `null` no recourse but dropping the election entirely.

**Raw-value changeset diffs.** The audited-property diff compares raw
after-image values because the alternative lets a presentation election
change row sets: two raw values rounding to one decimal text would suppress
the `update` and renumber every later `id`. Event existence and numbering
are facts of the data; an election renders values only — the same line
"ordering never derives from a rendered value" already draws.

**One rendering per kind in the log, gated not resolved — and silence is
a side.** The log's `changes` is a per-kind surface fed by per-table
declarations. Conflicting elections are refused (`ElectionKindConflict`)
rather than resolved by precedence, because any precedence would make the
log's rendering depend on declaration order — the same reason the unified
map makes the per-column conflict unrepresentable instead of picking a
winner. A silent emitting table counts as a conflict, not an abstention:
its column ships the default raw rendering, and letting the lone election
win the log would put a raw table column beside an elected log — inside
one export, exactly the mixed rendering the reach rule exists to close.
The loud fix is one repeated line per sibling table; the deliberate
escape is `only` / `ignore` (above). The gate still reaches exactly as
far as its reason: tables differing on a property no log renders are
legal, because they are independent output surfaces and refusing them
would constrain table rendering for a log that does not exist —
dimensional's per-column freedom, kept wherever the log imposes no
identity.

**Elections ride table declarations; the log-only surface is deferred.** A
kind may be audited with no declared table, and such a kind's `changes`
entries stay raw codec text — this design gives the log no declaration
surface of its own. An events-source `render:` map was considered and cut:
it adds a second declarer species (bare-name addressing, its own
key-domain rule, a cross-source conflict branch) to serve a case none of
the motivating payloads exhibit, and what deferral costs is today's
rendering, not an inconsistency. A later design can add the surface; the
agreement gate already states what it must satisfy.

**Top-level keys only.** The motivating payloads are flat objects; nested
paths would import a path language (and its escaping and ambiguity rules)
for no observed case — scope another design can widen if a real payload
demands it.

**Ties away from zero.** The rendering engine's native double→DECIMAL
conversion rule, verified on exact binary halves; adopting it keeps the
authority a single cast-shaped expression instead of a hand-rolled rounding
pipeline, and float64 sources make the tie case vanishingly rare (only
exact binary halves tie).

**Streaming gets the numeric family only.** Numeric elections change value
*text*, which is exactly what a codec surface can carry; the temporal
family's streaming exclusion (payloads are string-typed by codec, `ts` is a
separate contract) is unchanged by this design and is not re-litigated here.

**Not the reader.** Placing elections at the read surface would redefine
"faithful" for every consumer at once — conformance and the corrupters
read through the reader, and each must keep seeing the emit's own
values. Mode-level composition above one shared authority gives the same
cross-mode identity without touching the read surface. The reader's one
change — registering the json-precision scalar at open — installs
capability, not transformation: no read the reader serves changes, and no
consumer below the modes calls the registered function.

**No `init` proposals, no sidecar extension.** No proposal engine can know
that a DOUBLE is a percentage or a BIGINT payload is an instant; elections
remain author-added edits. A per-column unit/instant annotation in the
sidecar would let upstream declare what forge currently asks the author to
assert — a plausible future contract extension, but the contract is
external and vendored, and this surface reads it as-is.

## Boundaries

- **No general cast surface.** Integer sources, VARCHAR-to-number parses,
  and free-form type changes stay outside the election set.
- **No nested JSON paths and no all-leaves wildcard** — named top-level
  keys only.
- **No log-only election surface.** `events` sources declare no elections;
  a kind audited with no declared table renders raw codec `changes` text.
  Deferred scope — a later design can add the surface under the existing
  agreement gate.
- **No streaming temporal elections** — the existing exclusion stands.
- **No corrupter change** — `schema_drift.retype_to` remains the only
  type-breaking surface; a corrupted payload that breaks a declared leaf
  fails the export loudly, the `date_parse` composition posture.
- **No election-specific fingerprint rule** — ordinary config content under
  the incremental driver's existing fingerprint.
- **The reader seam is registration-only.** `register_render_functions` is
  connection setup at open; no read surface, sidecar accessor, or query
  API changes, and no consumer below the modes calls the registered
  function.
- **No conformance change** — C1–C14 read the base layer as today. Compare
  changes exactly once — the new decimal canonical family (§ Affected
  Subsystems); today `DECIMAL` deliberately belongs to no family and a
  decimal-elected render would refuse whole comparisons, so the family is
  load-bearing, not optional. No other family, tolerance, or verdict
  semantics move.

## Related

| Document | Why |
|---|---|
| `temporal-elections.md` | The election-family template this design extends; the instant vocabulary and anchor rules the payload `instant` election reuses; the `date_parse` semantics absorbed into the unified map. |
| `key-election.md` | The sibling cross-mode election surface, including the after-image render sites the source/streaming reach mirrors. |
| `source.md` / `base.md` / `dimensional.md` / `streaming.md` | The modes whose attach grammars change. |
| `writers.md` | The pinned text form the DECIMAL type joins; the per-type temporal text forms the log's `changes` entries pin to. |
| `reader.md` | The session-setup site (the session-zone pin's species) where the rendering scalar function is registered at open. |
| `compare.md` | The canonical family table the decimal family joins; today's deliberate DECIMAL exclusion this design lifts. |
| `slice-only.md` | The refusal surface every elected source column joins. |
| `anchor.md` | The wallclock renderer the payload `instant` election compiles through. |
