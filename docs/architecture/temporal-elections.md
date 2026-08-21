# Temporal Elections

The cross-mode surface for author-electable temporal output types — `DATE`,
`TIME`, `TIMESTAMP WITH TIME ZONE`, `INTERVAL`, and a declared VARCHAR→temporal
parse over the instant-string family — on the render sites that already compute
temporal values. One shared election vocabulary attaches at each mode's own
render surfaces: the dimensional, source, and base exporters. No new
information is derived; an election changes only the output type of an
already-derived value.

**Source:** the shared election type and instant renderer live in
[`anchor.py`](../../src/fabulexa_forge/anchor.py)
(`TemporalRender`, `render_anchor_temporal_expr`); the date-parse renderer in
[`_sql.py`](../../src/fabulexa_forge/_sql.py) (`render_date_parse_expr`,
with `validate_date_parse_format` and `date_parse_denoted_type`);
the config grammar in
[`config/models.py`](../../src/fabulexa_forge/config/models.py). Per-mode
wiring lives with each mode's own compile code — see § Per-mode attach
points.

---

## Boundary

- **Inputs.** A `sim_time` (ns) column or an elapsed ns delta the mode has
  already resolved; the resolved `EffectiveAnchor` for instant elections; an
  author-declared election value from the config grammar; a VARCHAR source
  column and author-declared format string for the declared parse.
- **Outputs.** A SQL SELECT-list fragment producing the elected type —
  `TIMESTAMP`, `DATE`, `TIME`, `TIMESTAMP WITH TIME ZONE`, `INTERVAL`, or
  (declared parse) the format's denoted `DATE` / `TIME` / naive `TIMESTAMP`.
- **Not an input.** No sidecar field feeds an election, and no election
  writes back to the sidecar; the base-layer contract and `base.json` are
  read-only (see [`bundle.md`](bundle.md)).
- **Closed surface.** The election set covers instants, durations, and
  declared instant strings only — there is no general "cast this column to
  that type" grammar (§ Rationale).

## Semantics

### The election vocabulary

One shared value set for instant renderings, used verbatim at every attach
point:

| Election | Output type | Value |
|---|---|---|
| `timestamp` (default) | naive `TIMESTAMP` (µs) | The instant's local wall clock in the anchor zone — the mode-definitional default rendering |
| `date` | `DATE` | The calendar date of that same local wall clock |
| `time` | `TIME` (µs) | The time of day of that same local wall clock |
| `timestamptz` | `TIMESTAMP WITH TIME ZONE` | The absolute instant itself (µs), zone-aware |

`date` and `time` are pure projections of the `timestamp` rendering: for any
instant, `date` equals the naive timestamp's date part and `time` its time
part, in the anchor zone. The elections never disagree with each other about
what local wall clock an instant maps to — this family identity is the
testable statement of faithfulness for instant elections.

Two further elections exist outside the instant family, neither drawing on
that value set. Duration election (`derived: elapsed` only) has a closed,
single-valued vocabulary: `interval` renders `INTERVAL` at µs precision from
the same ns delta the numeric rendering already computes, sign-preserving.
The declared parse has no election value at all — the format string *is* the
election, and the output type is derived from the directives it carries:
`DATE`, `TIME`, or naive `TIMESTAMP` (§ The declared parse).

### Anchor requirement

| Site | Election | Anchor resolves | Anchor is `None` |
|---|---|---|---|
| Any instant site | `timestamp` (default, not explicitly elected) | wallclock `TIMESTAMP` | Per-mode behavior: raw `BIGINT` ns (dimensional, base); error (source, whose anchor-required posture is independent of elections) |
| Any instant site | any explicit election (including explicit `as: timestamp`) | the elected rendering | Load-time error — an elected rendering never falls back to raw integers. Without a declared calendar the offset is uninterpretable, and a silent raw-integer column under an elected `date` name would be a fallback the faithful-reshaping principle forbids |
| elapsed | `interval` | `INTERVAL` | `INTERVAL` — durations are physical deltas; no anchor is involved |
| declared parse | — | the format's denoted type | the format's denoted type — the string was anchored upstream when minted; parsing reads no `sim_time` |

The explicit-election error (`TemporalRenderRequiresAnchor`, § Validation
Rules) is a plan-time business rule, not a render-time surprise: it fires
during validation, before any query runs.

### DST and zone semantics

All instant elections inherit the anchor's existing semantics
([`anchor.md`](anchor.md)) — a physical-ns affine shift, DST resolved by the
rendering engine's IANA tz database, no package-local DST policy:

| Rendering | DST posture |
|---|---|
| `timestamp`, `date`, `time` | Naive local wall clock: ambiguous across a fall-back fold (two instants, one local string), steps backward at the fold. Faithful to real wall clocks; `date` / `time` inherit the same posture as `timestamp`. Ordering is never affected — every emitted table's total order is over raw sim-time keys and identity, never rendered values |
| `timestamptz` | Carries the absolute instant; immune to fold ambiguity. Its *display* is a serialization concern ([`writers.md`](writers.md)), but its value is exact |

A `date` election can place two physically-ordered events on calendar dates
that read "backward" across a fold only in the same sense a naive timestamp
already can; no new anomaly class exists.

### Precision

Contract precision is ns; every rendered temporal value truncates to µs
(`timestamp`, `time`, `timestamptz`, `interval` alike). `date` truncation is
the day itself. Sub-µs significance is unspecified by the contract; µs is
forge's uniform presentation choice. A declared parse obeys the same rule
from the other direction: its `TIMESTAMP` and `TIME` denotations carry µs,
and a `%g` millisecond fraction widens exactly to µs.

### Determinism

Same emit + same config + same code version → identical output, with one
qualifier stated for the whole family: local-time renderings are
reproducible modulo the consumer's IANA tz database version. Runs do not pin
a tzdata version, so a historical DST-boundary shift between tz database
versions can move a rendered local value — this class already applies to
`timestamp` rendering and is accepted, not engineered around.
`timestamptz` values (instants), `interval` values (physical deltas), and
parsed values (zone-naive by construction, § The declared parse) are exempt:
they carry no local projection, so the tzdata qualifier does not reach them.

Serialization of every elected type is independent of the executing
machine's locale and session zone — the mechanism is the reader's
session-zone pin ([`reader.md`](reader.md)) plus the writers' pinned CSV
text forms ([`writers.md`](writers.md)).

### The declared parse

A declared parse reinterprets a VARCHAR source column as the temporal type
its author-declared `strptime`-style format denotes. It is never sniffed:
the shape of upstream temporal strings is producer implementation detail,
not contract, so both the source column and the format are author-supplied.

**The directive vocabulary** is closed, in two classes plus literals:

| Class | Directives |
|---|---|
| date | `%Y` `%y` (year) · `%m` `%b` `%B` (month) · `%d` (day) |
| time | `%H` (24h hour) · `%I` (12h hour) · `%p` (AM/PM) · `%M` (minute) · `%S` (second) · `%f` (µs fraction) · `%g` (ms fraction) |
| literal | `%%`, arbitrary literal text — matched verbatim |

Three rules govern a well-formed format:

- **Completeness.** A format's date part is complete iff it carries a year
  directive, a month directive, and `%d`. Its time part is complete iff it
  carries an hour directive — `%H`, or `%I` paired with `%p`.
- **Pairing.** `%I` and `%p` each require the other; `%M` requires an hour
  directive; `%S` requires `%M`; `%f` / `%g` require `%S`. A lower-order
  field absent from the format parses as zero — `strptime`'s own semantics,
  the parse function's definition rather than a forge default.
- **Uniqueness.** Each temporal field appears at most once: no repeated
  directive, and no two alternative forms of one field (`%Y`/`%y`,
  `%m`/`%b`/`%B`, `%H`/`%I`, `%f`/`%g`). This is what makes the
  value-preservation round trip unconditional — a format whose directives
  could disagree about one field has no single denoted value.

**Denotation** is a pure function of a format that satisfies those rules:

| Format carries | Denoted type |
|---|---|
| complete date, no time-class directives | `DATE` |
| complete date + complete time | naive `TIMESTAMP` (µs) |
| complete time, no date-class directives | `TIME` (µs) |

Anything else — a partial date, a partial time, an orphaned pairing, a
duplicated or conflicting directive, a directive outside the closed set — is
a load-time config error naming the format and the violated rule.
`date_parse_denoted_type` is the sole derivation authority: the renderer and
every type-reading consumer resolve the denoted type through it, never by
re-inspecting the format string.

Parse behavior is uniform across the three denotations:

| Condition | Result |
|---|---|
| Source value matches the declared format | The denoted-type value it denotes |
| Source value is `NULL` | `NULL` of the denoted type (nothing to reinterpret) |
| Source value does not match the declared format | A loud export-time error naming the table, column, and offending value — never a silent `NULL`, which would fabricate a missingness defect that is not in the data |
| Format violates a vocabulary, pairing, uniqueness, or completeness rule | A load-time config error; nothing runs |

**A parse never consults the anchor**, at any denotation. The string was
anchored upstream when it was minted, so a parsed naive `TIMESTAMP` is the
string's own wall clock, not a rendered instant. The two `TIMESTAMP`
producers are distinguished by their declaration: an instant election reads a
`sim_time` column and requires an anchor; a parse reads a VARCHAR column and
ignores it. Parsed values are correspondingly zone-naive — no zone, no DST
posture, no dependence on the tz database version.

Attribution is the renderer's, not the driver's: `render_date_parse_expr`
emits an in-SQL guard that raises the enriched message on a non-matching
non-`NULL` value, so the failure names its site no matter how many parses
one table declares.

The strict-failure rule composes with the corrupter family deliberately: a
`mutate_cells`-corrupted temporal string fails the parse loudly, while a
`null_cells` defect flows through as `NULL`, faithfully. An author exporting
a corrupted emit chooses, per column, whether to declare a parse — the
defect manifest ([`corrupters.md`](corrupters.md)) tells them which columns
carry wrong-value defects.

The parse is a value-read like any other: its source column joins the
`slice_only` refusal surface ([`slice-only.md`](slice-only.md)), and the
source must carry a declared VARCHAR type — parsing a non-VARCHAR column is
a plan-time error, not an implicit cast. Resolution follows each mode's
existing column-resolution rule (§ Per-mode attach points).

A parsed property's reach extends past its table column: under the source
mode's election-reach rule, its event-log `changes` entries carry the
denoted value's pinned text form
([`value-rendering-elections.md`](value-rendering-elections.md)
§ Event-log and after-image reach).

### Per-mode attach points

`render` map keys are **source identities** (e.g. `created_sim_time`,
`joined_sim_time`, `event_sim_time`), never output names — the same posture
as `rename`, and for the same reason: a renamed column stays addressable. A
`render` entry re-renders the projected column **in place** — no column is
added, and the output name stays governed by the mode's existing defaults
and `rename`. On source and base, the temporal spellings — the bare
instant shorthand and the `{date_parse: "<format>"}` entry — live in the
unified property-first `render:` map shared with the value rendering
elections ([`value-rendering-elections.md`](value-rendering-elections.md)
§ The unified render map).

| Mode | Surface | Attach | Detail |
|---|---|---|---|
| dimensional | `derived: timestamp` | `as: <election>` on the spec; default `timestamp` | [`dimensional.md`](dimensional.md) § Timestamp source and the runtime anchor |
| dimensional | `derived: scd_window` | object form `{bound, as}`; the bare-literal shorthand means default rendering | [`dimensional.md`](dimensional.md) § Timestamp source and the runtime anchor |
| dimensional | `derived: elapsed` | exactly one of `unit` (numeric) / `as: interval` | [`dimensional.md`](dimensional.md) § Derived columns |
| dimensional | `derived: date_parse` | `{from, format}` | [`dimensional.md`](dimensional.md) § Derived columns |
| source | declared table (`state` / `junction`) | unified `render:` map — bare shorthand (structural instant) / `{date_parse: …}` entry (payload) | [`source.md`](source.md) § Wallclock timestamps |
| source | event log | `render:` map, keyed on the log's one instant column `event_sim_time` — a constant of the log's published contract, not a reader question | [`source.md`](source.md) § The event log |
| base | per-table render declaration | unified `render:` map keyed on the same pre-default column identities the mode's `rename` uses | [`base.md`](base.md) § Presentation, typing, and ordering |

Which columns are legal `render` keys on a declared table or base entry is
the reader's answer — an instant-carrying structural column of the table's
category, per the structural-temporal surface
([`reader.md`](reader.md) § The structural-temporal surface) — never a
hardcoded per-mode list. A key must also name a column the render **emits**:
a declaration naming a column the table's selection omits is refused at plan
time, the modes' existing posture for declarations naming omitted columns.

Row ordering never derives from an elected rendering — every table's total
order is over raw sim-time keys and identity. The dimensional mode's ordinal
amendment (substituting a rendered-time `order_by` column for its raw-ns
source) and the incremental driver's window-key rule are both
election-aware; each is documented with its owning mechanism in
[`dimensional.md`](dimensional.md) § Derived columns and
[`incremental.md`](incremental.md) § Window membership per table class.

An elected `date` on `scd_window` renders a date-grained validity window — a
standard warehouse shape. Same-day versions collapse to `valid_from =
valid_to` at date grain (the underlying raw-ns bounds remain distinct, and
version ordering is unaffected); the open interval's `NULL` `valid_to` stays
`NULL` under every election.

## Invariants

1. **Closed election set.** The vocabulary covers instant renderings,
   elapsed-duration rendering, and the declared parse only. There is no
   general cast surface; a free-form cast could silently mangle sidecar
   values, which the faithful-reshaping principle forbids.
2. **Default renderings are byte-identical.** A config with no election
   renders exactly the pre-election SQL: naive µs `TIMESTAMP` instants,
   `DOUBLE` elapsed, VARCHAR payload pass-through. The `timestamp` default
   and the numeric elapsed default are mode-definitional, not invented
   values.
3. **Family identity.** For any instant, `date` and `time` are pure
   projections of the same local wall clock `timestamp` renders; the three
   never disagree about what wall clock an instant maps to.
4. **Faithfulness.** Every elected value traces to base-layer values through
   the same derivations that exist without an election; the election
   changes representation only. The interval's equality to the numeric
   delta at µs, and the parse's value preservation (the denoted value ↔ the
   source string under the declared format, round-trippable at every
   denotation, zero-fill of absent lower-order fields included), are the
   testable statements of this invariant.
5. **One renderer per family.** Every wallclock election renders through
   `render_anchor_temporal_expr`, the one SQL renderer every mode shares
   ([`anchor.md`](anchor.md)); every declared parse renders through
   `render_date_parse_expr`, over the one denoted type
   `date_parse_denoted_type` derives. Neither renderer is duplicated per
   mode, and no consumer re-derives a denoted type.
6. **An elected rendering never falls back to a raw integer.** Absence of an
   anchor under an explicit election is a load-time error, not a silent raw
   ns column (§ Anchor requirement).

## Validation Rules

| Rule | Checks | Error |
|---|---|---|
| `TemporalRenderRequiresAnchor` | Every explicitly-elected instant rendering (dimensional `as`, `scd_window` object form, source/base `render` entries — payload `instant` elections included, [`value-rendering-elections.md`](value-rendering-elections.md)) has a resolved effective anchor. The source mode's global anchor requirement subsumes its entries; the rule still names the offending column | `"column '{column}': temporal rendering '{render}' requires a resolved anchor; this emit declares no runtime calendar and none was supplied"` |
| `DateParseSourceColumn` | Each declared parse source resolves per its mode's addressing convention and carries a declared VARCHAR type, and is not `slice_only` | `"date_parse column '{column}' on '{table}': source must be an existing VARCHAR column (got {type})"` |
| `RenderKeyResolves` | A declared-table or base-entry `render` key resolves in its value form's domain. The bare-shorthand form names an instant-carrying structural column of the table's category (reader-sourced, never hardcoded); the event log's one legal key, `event_sim_time`, is mode-definitional. A key must also name a column the render emits. The typed forms' domains are the value elections' ([`value-rendering-elections.md`](value-rendering-elections.md) § Validation Rules) | `"render key '{column}' on '{table}': not an instant-carrying structural column of this table"` (shorthand form; per-form shapes for the typed forms) |
| Incremental append-mode `order_by` (amended) | Window-key membership is election-aware: a column whose declared source is the window's raw-ns column counts as a window key only if its rendering is also window-monotone. A `time`-elected column over the window's raw-ns source is excluded | The existing rule's message, naming the column and the table's window key ([`incremental.md`](incremental.md)) |

Each rule's exact resolution mechanics (grain-projection resolution on
dimensional vs. direct sidecar-type reads on source/base) are documented
with the mode that implements them — see § Per-mode attach points.

`exactly_one_rendering` (`ElapsedSpec`) and `format_denotes_a_temporal`
(`DateParseSpec`) are parse-time (Pydantic) validators on the config grammar
— see [`config/models.py`](../../src/fabulexa_forge/config/models.py). One
rule serves every attach point: the dimensional spec form and the source and
base map forms all check their format through the shared
`validate_date_parse_format` in
[`_sql.py`](../../src/fabulexa_forge/_sql.py), so the closed-vocabulary,
pairing, uniqueness, and completeness rules are identical wherever a parse is
declared.

## Rationale

**A closed election set, not a general cast knob.** Authors need realistic
app-database and warehouse column types (`DATE` admission dates, `TIME`
columns, zone-carrying `TIMESTAMPTZ`, `INTERVAL` wait times) for values the
export already computes. A general "cast any column to any type" surface
would let an author silently mangle a sidecar value instead of reshaping it
— the closed set (instants, durations, declared parses) keeps every
election a value-preserving re-representation.

**Never sniffed.** A declared parse requires both the source column
and the format from the author rather than inferring them from the string
shape, because the upstream string format is the producer's
implementation detail, not part of the contract (`bundle.md`); sniffing
would couple forge to an undeclared upstream convention.

**The format is the election.** A parse's output type is derived from the
directives the author wrote, not declared beside them in a second `as:`
knob. An author writing `%Y-%m-%d %H:%M:%S` has already said the column is a
datetime; a separate type declaration could only agree redundantly or
contradict, and a contradiction has no faithful resolution — the format
determines what the string denotes. One statement of intent also makes the
round trip checkable from the format alone.

**No sidecar extension.** A per-column logical-date annotation in the
sidecar is a plausible future contract extension, but adding one is not
this surface's decision to make — the base-format contract is external and
vendored (`CLAUDE.md` § The boundary); this surface reads the sidecar as-is.

**One renderer per family.** Every wallclock election compiles through
`render_anchor_temporal_expr`, so any current or future mode renders
byte-identically for the same election.

## Boundaries

- **Streaming carries no temporal election.** The streaming mode's payloads
  are string-typed by codec (JSONL / Debezium carry no SQL type surface) and
  its `ts` rendering is a separate Python-side contract
  ([`streaming.md`](streaming.md)). The numeric value elections do attach
  per stream — they change value text only
  ([`value-rendering-elections.md`](value-rendering-elections.md)
  § Streaming attach).
- **The parse family covers naive strings only.** There are no zone
  directives (`%z` / `%Z`) and no `timestamptz` denotation: a zone-bearing
  string would need a zone policy for offsets the anchor never saw, and the
  parse deliberately reads no anchor. Non-VARCHAR sources are outside the
  family too — reinterpreting a numeric column is a cast, not a parse.
- **No `init` proposals.** No proposal engine proposes an election or a
  declared parse; proposed configs carry default renderings only, and
  elections remain author-added edits.
- **No corrupter change.** `schema_drift.retype_to` remains the only
  type-breaking surface and is unrelated to elections
  ([`corrupters.md`](corrupters.md)).
- **No election-specific fingerprint rule.** An election is ordinary config
  content: changing it between windows is a config change like any other,
  tripping the existing fingerprint mismatch
  ([`incremental.md`](incremental.md)).
- **Elections do not influence anchor resolution.** Precedence, DST and
  ambiguity rules, and the one-anchor-per-invocation rule are entirely
  the anchor's own ([`anchor.md`](anchor.md)); elections only consume the
  resolved anchor.

## Related

| Document | Why |
|---|---|
| [`anchor.md`](anchor.md) | The shared `EffectiveAnchor` and the one wallclock renderer every election compiles through. |
| [`reader.md`](reader.md) | The structural-temporal surface (legal `render` keys) and the session-zone pin that makes zone-bearing serialization machine-independent. |
| [`writers.md`](writers.md) | The pinned CSV text forms for the elected temporal types, which parsed values serialize through identically. |
| [`dimensional.md`](dimensional.md) | The dimensional mode's attach points, the election-aware ordinal amendment, and the `scd: type2` column-mode surface a `date_parse` or `timestamp` column may attach to. |
| [`source.md`](source.md) | The source mode's unified `render` map on declared tables and the event log. |
| [`value-rendering-elections.md`](value-rendering-elections.md) | The value-election siblings sharing the unified `render` map — the payload `instant` election that reuses this family's vocabulary and anchor rules, and the event-log reach of parsed and elected temporal text. |
| [`base.md`](base.md) | The base mode's per-table render declaration list. |
| [`incremental.md`](incremental.md) | The election-aware append-mode window-key rule. |
| [`playback.md`](playback.md) | Tier-2 shaped playback's reuse of the modes' own compile and validation surfaces, session-zone pin included. |
| [`slice-only.md`](slice-only.md) | The refusal surface a `date_parse` source column joins. |
| [`key-election.md`](key-election.md) | A sibling cross-mode election surface: one vocabulary, per-mode attach points. |
| [`row-predicates.md`](row-predicates.md) | A sibling shared-grammar surface with one rendering authority. |
