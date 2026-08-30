# Value Rendering Elections

The cross-mode surface for author-elected renderings of payload column
*values* — numeric precision (`decimal`: DOUBLE → exact `DECIMAL(p, s)`),
payload sim-instants (`instant`: a BIGINT declared to carry sim-time ns,
rendered through the temporal-election vocabulary and the anchor), and
in-place JSON leaf rounding (`json_precision`: named top-level numeric
leaves of a JSON payload, rounded byte-preservingly). Together with the
temporal family's structural shorthand and declared parse, the elections
are declared in one property-first `render:` map on the source and base
declaration grammars; dimensional declares the numeric pair through its
derived one-of, and streaming per declared stream (numeric family only).
Elections are renderings composed **above** the faithful read: the reader
and the derivations layer serve unrendered values, conformance and the
corrupters read unrendered values, and one rendering authority per
election kind keeps every mode byte-identical for the same election.

**Source:** the election grammar in
[`config/models.py`](../../src/fabulexa_forge/config/models.py)
(`RenderElection` and its members `DecimalElection` / `InstantElection` /
`JsonPrecisionElection` / `DateParseElection`; `StreamRenderElection`;
the dimensional spellings `DecimalSpec` / `JsonPrecisionSpec`); the
rendering authorities in [`_sql.py`](../../src/fabulexa_forge/_sql.py)
(`render_decimal_expr`, `render_json_precision_expr`, the
`forge_json_precision` scalar, `register_render_functions`). Per-mode
wiring lives with each mode's own compile code. Examples: the per-mode
test suites under [`tests/`](../../tests/).

---

## Boundary

- **Inputs.** A payload column's values as the faithful read serves them —
  a declared-DOUBLE source for `decimal`, a declared-BIGINT sim-offset
  source for `instant`, a declared-VARCHAR JSON payload for
  `json_precision`; the resolved `EffectiveAnchor` for `instant`; the
  author's election declarations from the config grammar.
- **Outputs.** SQL SELECT-list fragments producing the elected
  representation, compiled by the one authority per election kind.
- **Above the read surface.** No read the reader serves changes under an
  election: the faithful read, the sidecar surface, and every query API
  serve unrendered values, and C1–C15 conformance and the corrupters read
  them unrendered. The reader's one contribution is session setup, not
  reading: `register_render_functions` registers the json-precision scalar
  on the emit's connection at open — the same species of connection-scoped
  setup as the session-zone pin ([`reader.md`](reader.md)). No consumer
  below the modes calls the registered function.
- **Closed surface.** The election set covers the three value renderings
  plus the temporal family's spellings only — there is no general "cast
  this column to that type" grammar (§ Rationale).

## Semantics

### The unified render map

On a source declared table and a base render entry, `render:` is one
property-first map answering "how does this column render". Keys are
**source identities** (the `rename` posture: a renamed column stays
addressable by its source name). Map values are:

| Value shape | Meaning |
|---|---|
| bare scalar (`date`, `time`, `timestamp`, `timestamptz`) | Temporal election on a structural instant column ([`temporal-elections.md`](temporal-elections.md)) |
| `{date_parse: "<format>"}` | The declared parse ([`temporal-elections.md`](temporal-elections.md) § The declared parse) |
| `{instant: <temporal election>}` | Payload sim-instant declaration + rendering |
| `{decimal: [p, s]}` | Numeric precision rendering |
| `{json_precision: {<key>: <digits>, …}}` | JSON payload leaf rounding |

One column, one election — YAML key uniqueness makes a conflicting pair
unrepresentable. Every entry re-renders the projected column **in place**:
no column is added, and output naming stays governed by the mode's
defaults and `rename`. A key must name a column the table emits; a key
naming an omitted or non-existent column is refused at plan time (the
modes' posture for declarations naming omitted columns). Each value form
has a fixed key domain: the bare shorthand addresses instant-carrying
structural columns only, and the typed forms address payload columns of
the table's kind only — a typed election naming a structural column
(`created_sim_time: {instant: timestamp}`) is refused at plan time, so no
rendering ever has two spellings.

On a source `junction` table the typed forms key the `elem__<f>` element
columns — the junction's source identities, exactly as `columns` /
`rename` and the parse address them — and the source-type gates read each
`elem__<f>` column's declared type. The member pair columns
(`member__<f>__kind` / `member__<f>__id`) are outside the typed-election
key domain — reference identity is key election's surface
([`key-election.md`](key-election.md)). The structural-instant shorthand
keys (`joined_sim_time` / `left_sim_time`) follow the temporal family's
rules. An elected column's source joins the `slice_only` refusal surface
([`slice-only.md`](slice-only.md)) exactly as a `date_parse` source does.

The events *block*'s own map carries one structural shorthand key,
`event_sim_time` — the typed forms are unrepresentable there, not
representable-but-refused. `events` sources carry no election map of
their own: the log inherits the declared tables' elections (§ Event-log
and after-image reach).

Dimensional keeps its per-column posture: `derived: {decimal: …}` and
`derived: {json_precision: …}` are members of the derived one-of, siblings
of `derived: {timestamp: …}` (which covers the payload-instant case
there). The cross-mode story: dimensional elects on the column
declaration, source and base elect on the column key.

### The `decimal` election

Output type `DECIMAL(p, s)` (`1 ≤ p ≤ 38`, `0 ≤ s ≤ p`), rendered by the
one decimal authority (`render_decimal_expr`).

| Condition | Result |
|---|---|
| Source value is a finite float within `DECIMAL(p, s)` range | The value rounded to `s` fraction digits; ties round away from zero |
| Source value is `NULL` | `NULL` of the output type |
| Source value overflows `p − s` integer digits | Loud export-time error naming the table, column, and offending value — never a silent `NULL` or saturation |
| Source value is `NaN` / `±Infinity` | The same loud export-time error — no decimal representation exists |
| Source column's declared type is not DOUBLE — the contract's one floating-point type | Plan-time error; integers and VARCHARs have no precision to elect |

Rounding is a pure function of the value — no anchor, no zone, no tzdata
qualifier. The tie rule is stated (away from zero) and testable on exact
binary halves; values that are not exact binary halves round by their
actual binary value, which is the honest reading of a float64 source.

### The `instant` election

An author assertion that a payload BIGINT column carries sim-time ns,
plus a rendering from the instant vocabulary
([`temporal-elections.md`](temporal-elections.md) § The election
vocabulary). Everything downstream of the assertion is the temporal
family's contract:

| Condition | Result |
|---|---|
| Election present, anchor resolves | The elected rendering via the shared wallclock renderer — identical semantics to a structural instant (DST posture, µs precision, tzdata qualifier) |
| Election present, anchor is `None` | Load/plan-time error — an elected rendering never falls back to raw integers (the explicit-election posture; source's global anchor requirement subsumes this for source) |
| Source value is `NULL` | `NULL` of the elected type |
| Source column's declared type is not BIGINT | Plan-time error — the assertion is checkable only against an integer sim-offset column |

The assertion itself is author-supplied and unverifiable — the sidecar
declares no instant marker for payload properties, and forge does not
sniff. A wrong assertion renders garbage wallclocks deterministically;
that is the same trust the `date_parse` format receives.

### The `json_precision` election

Declared per JSON payload column as a map of **top-level key → fraction
digits** (`0 ≤ n ≤ 12`, non-empty). The rendering is in-place token
replacement, not re-serialization:

| Condition | Result |
|---|---|
| Payload is `NULL` | `NULL` |
| Payload is a valid JSON object; a declared key is present with a numeric value | That value's token is replaced by its rounded decimal text with exactly the declared fraction digits (ties away from zero); **every other byte of the payload — whitespace, key order, undeclared values — is preserved verbatim** |
| A declared key is absent from a row's payload | That row's payload is unchanged for that key — payload shapes legitimately vary per row |
| A declared key is present with the JSON literal `null` | That row's payload is unchanged for that key — no error, no notice. `null` is the payload-interior spelling of missingness, the same fact SQL `NULL` states at column level and absence states at object level; nothing to round, nothing fabricated |
| A declared key is present with a non-numeric, non-`null` value (string, object, array, boolean) | Loud export-time error naming the table, column, key, and offending value |
| A declared key appears more than once at top level | Loud export-time error — duplicate keys have no single value to round |
| Payload is non-`NULL` and not a valid JSON object | Loud export-time error naming the table, column, and value — electing the column asserts it is a JSON object, and an unparseable payload is a surfaced author error, never a silent pass-through |

Rounding operates on the **decimal number the token denotes** — exact
decimal arithmetic over the token text, never a float64 re-parse, which
would move ties (`0.005` at two digits is `0.01` as a decimal and `0.00`
through the nearest float64). Ties round away from zero at the exact
decimal half. A rounded token carries exactly the declared fraction
digits; zero digits renders the bare integer text with no decimal point.
Rounded text is always plain decimal notation — a source token in
exponent notation (`6.5e1`) rounds to its plain-form text — and a value
that rounds to zero renders unsigned (`-0.001` at two digits is `0.00`,
never `-0.00`), the no-negative-zero posture SQL `DECIMAL` gives the
decimal election.

Byte preservation of undeclared content is the faithfulness statement for
this election: the payload is upstream-minted data, and the election
touches exactly the tokens the author named.

The authority is the `forge_json_precision` scalar — a pure Python
function registered on the emit's connection at open and invoked only by
expressions `render_json_precision_expr` compiles. It is Python-side
because the transformation is not expressible in SQL: RE2-class regex
cannot distinguish a top-level key from the same name nested deeper, and
DuckDB's JSON functions re-serialize, re-spelling undeclared bytes.

### Cross-mode identity and determinism

One rendering authority per election kind: every mode compiles the same
election to the same expression, so the same emit + config renders
byte-identically in every mode that attaches it. `decimal` and
`json_precision` are pure value functions (no tzdata qualifier) — the
registered json-precision scalar is a pure function of its arguments, so
routing through it changes nothing in the determinism statement;
`instant` inherits the temporal family's determinism statement.

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
`decimal` applies to the after-image entry's codec string cast back to
its declared `DOUBLE` type (exact — the codec round-trips float64
values), `json_precision` to the payload string directly (it is already
the payload's own text). Both compile through the shared authorities in
the stream's SQL pipeline — the post-fold SELECT that assembles
after-images — so payload assembly receives already-elected text and no
mode carries a second rounding implementation. The elected text is
therefore identical to the table modes' render of the same value.

The temporal elections (`instant`, `date_parse`, structural shorthand) do
not attach to streaming (§ Boundaries).

### Event-log and after-image reach (source mode)

The source event log renders `changes` values per **kind**, but elections
are declared per **declared table** — the log declares none of its own
and inherits the tables'. A kind may have several declared tables
(sub-type splits), so the reach rule resolves the grain difference
explicitly. Per property of a kind (per element field of a
`(kind, property)` membership — a junction table's `elem__<f>` election
reaches the log's bare field `<f>`, the junction render's own name
strip):

| Declared tables emitting the property | Its `changes` rendering |
|---|---|
| Every one declares the identical election | The elected form |
| None elects it | The raw codec string |
| The declarations differ — two differing elections, or an electing table beside a silent one (silence asserts the default raw rendering) — and the log renders the property | Plan-time refusal (`ElectionKindConflict`) — the log has one rendering per property, and a raw table column beside an elected log is the same mixed rendering |
| The declarations differ, and no log renders the property (no `events` block, no source addressing the kind, or the property outside every addressing source's audited set) | Legal — the tables are independent output surfaces, with no log rendering to disagree about |

The gate is scoped to what the log renders: adding an `events` source
over a kind can make a previously-legal pair of differing — or
elected-beside-silent — table declarations illegal; the moment one log
must render the property, the refusal fires, naming both tables. The
escape hatch is the audited set itself: narrowing the property out of
every source's audited set (`only` / `ignore`) removes it from the gate's
scope, so a deliberately mixed table rendering stays representable — at
the declared price of not auditing the property. A kind audited with no
declared table renders raw codec text — the log carries no declaration
surface of its own (§ Boundaries).

**Every election kind reaches the log.** `decimal` and `json_precision`
entries carry the elected text; an `instant` or `date_parse` election
renders its `changes` entries as the elected temporal value's text.
In-JSON temporal text is pinned to the same per-type forms the writers
pin for CSV ([`writers.md`](writers.md) § Pinned text forms). Naive
`TIMESTAMP` — which the writers deliberately leave at the default
serialization — is pinned here for the in-JSON site:
`YYYY-MM-DD HH:MM:SS.ffffff`, the six-digit µs field omitted entirely
when the instant's microseconds are zero. That *is* the writer's
default-timestamp serialization, stated as a form so the table-column ↔
`changes`-entry identity stays byte-testable. All forms are produced by
explicit formatting in the JSON assembly — never an incidental `VARCHAR`
cast, whose fraction field trims to significant digits and matches no
pinned form.

Under that resolution an elected property renders identically — as text
— in the declaring table's column and in the log's `changes` entries.
The application site is the mode's, not the fold's: the derivations
folds emit codec `VARCHAR` after-images, and the log applies each
authority to the codec string cast back to its declared source type
(exact — the codec round-trips `DOUBLE` and `BIGINT`; a `date_parse` or
`json_precision` source is already `VARCHAR`), applied to the emitted
`[old, new]` values — the site at which key election renders reference
entries; rendering is a pure per-value function, so lag-then-render and
render-then-lag agree over the displayed pair. The diff's *comparison*
inputs are never rendered (below): key election may sit on either side
of the diff because elected surfaces are injective, but rounding is not,
so here the raw-diff rule is load-bearing. The `create` after-image and
every `u` delta carry the elected form, and the export-time guards —
parse mismatch, decimal overflow, the JSON payload guards — fire at the
log site exactly as at a table site: a log can fail loudly on a value no
declared table selects.

**Changeset membership is a raw-value fact.** The audited-property diff —
which properties an `update` row carries, and whether the row is
suppressed outright — compares raw, unrendered after-image values, so the
event set and the log's dense `id` numbering are election-invariant: a
presentation choice can never suppress or renumber a row. Two raw values
that round to one decimal text therefore display as an equal-looking
`[old, new]` pair — the honest rendering, at the elected precision, of a
real underlying change.

## Invariants

1. **Closed election set.** Each election admits one source type, renders
   one declared representation, and refuses out-of-range values loudly
   instead of mangling them. There is no general cast surface.
2. **Default renderings are byte-identical.** A config with no election
   renders exactly the default SQL — DOUBLE payloads verbatim, BIGINT
   payload instants raw ns, JSON payloads untouched.
3. **One rendering authority per election kind.** `decimal` compiles
   through `render_decimal_expr`, `json_precision` through
   `render_json_precision_expr` (the registered scalar its one
   implementation), payload `instant` through the shared
   `render_anchor_temporal_expr` — no mode carries a second
   implementation, and the elected text is identical at every attach
   site: table column, `changes` entry, streaming after-image.
4. **Byte preservation.** Under `json_precision`, output payload bytes
   differ from source bytes only at declared, present, numeric top-level
   leaves.
5. **Election-invariant event sets and ordering.** Changeset membership,
   `update` suppression, and audit `id` numbering compare raw values;
   row ordering is over raw sim-time keys and identity. A rendering
   election can never create, suppress, renumber, or reorder a row.
6. **Determinism.** `decimal` and `json_precision` are pure value
   functions of their inputs; `instant` inherits the temporal family's
   determinism statement.
7. **Above the faithful read.** The reader's read surfaces, the
   derivations layer, conformance, and the corrupters serve and consume
   unrendered values; elections compose in mode compile only.

## Validation Rules

| Rule | Checks | Error Message |
|---|---|---|
| `RenderKeyResolves` | Every render-map key names a column the table emits, in its form's domain: the structural-shorthand form requires an instant-carrying structural column; the typed forms require payload columns of the table's kind (`elem__<f>` element columns on a junction; the member pair columns are outside the domain) — a typed election naming a structural column is refused | Per-form message shapes — see [`errors.py`](../../src/fabulexa_forge/errors.py) |
| `ElectionKindConflict` | Across the declared tables of one kind (one `(kind, property)` membership: junction tables) that emit a source property **the event log renders**, every table declares the identical election — a silent emitting table counts as differing, its column asserting the default raw rendering; the gate runs only for properties inside some `events` source's audited set; tables differing on a property no log renders are legal (§ Event-log and after-image reach) | `"property '{column}' of kind '{kind}': '{a}' and '{b}' declare conflicting render elections"` — or, silent-table shape, `"… '{a}' declares a render election and '{b}' declares none"` — `{a}` / `{b}` naming the two tables |
| `DecimalSourceIsDouble` | A `decimal` election's source column carries a declared DOUBLE type | `"render key '{column}' on '{table}': decimal rendering requires a DOUBLE source (got {type})"` |
| `InstantSourceIsBigint` | An `instant` election's source column carries a declared BIGINT type | `"render key '{column}' on '{table}': instant rendering requires a BIGINT sim-time source (got {type})"` |
| `TemporalRenderRequiresAnchor` | Payload `instant` elections belong to the explicitly-elected instant set the rule covers ([`temporal-elections.md`](temporal-elections.md) § Validation Rules) | The rule's message |
| `JsonPrecisionSourceIsVarchar` | A `json_precision` election's source column carries a declared VARCHAR type | `"render key '{column}' on '{table}': json_precision requires a VARCHAR JSON payload source (got {type})"` |
| Slice-only refusal | An elected source column is not `slice_only` ([`slice-only.md`](slice-only.md)) | The surface's per-mode messages |
| Export-time overflow guard | In-SQL, per the decimal authority: overflow / NaN / infinity | `"column '{column}' on '{table}': value {value} does not fit DECIMAL({p},{s})"` |
| Export-time payload guards | In-SQL, per the json authority: invalid payload, non-numeric non-`null` declared leaf, duplicate top-level key | `"column '{column}' on '{table}': json_precision key '{key}': {reason} (value: {value})"` |

The parse-time (Pydantic) validators — decimal bounds, the
json-precision leaf-map shape — live on the election models in
[`config/models.py`](../../src/fabulexa_forge/config/models.py). The
source-type gates cover every attach point of their election, not the
render-key form alone: the dimensional `derived` spellings check the
same rules against their `from` column through the mode's
grain-projection resolution; streaming runs them per declared stream
against the kind's sidecar types (a stream `render:` key must name a
declared property or membership field of that stream's projection) — one
rule per check, each mode's message naming its own addressing.

## Rationale

**The closed set stays closed.** These are elections, not a cast knob:
each admits one source type, renders one declared representation, and
refuses out-of-range values loudly instead of mangling them. `decimal`
on a DOUBLE is the numeric analog of ns→µs truncation — a declared,
deterministic, presentation-precision choice; `instant` is the same
declaration `date_parse` makes for strings, applied to the contract's
own ns encoding; `json_precision` touches exactly the tokens the author
names.

**Property-first, one map.** A map per election kind lets one column be
claimed twice and needs a cross-map disjointness validator; five kinds
would need ten. Property-first makes the conflict unrepresentable, gives
authors one place that answers "how does this column render", and
matches dimensional's per-column posture — the cross-mode story is:
dimensional elects on the column declaration, source/base elect on the
column key. The declared parse shares the map for the same reason: one
map, no permanent two-map asymmetry.

**In-place replacement, not re-serialization.** A parse-and-reserialize
implementation of `json_precision` would re-spell every number and
reorder or re-whitespace content the author never declared — mangling
upstream bytes under an election that promised to touch one leaf. Token
replacement keeps the faithfulness statement crisp and testable: bytes
differ only at declared, present, numeric leaves. The authority is
Python-side because no in-SQL tool honors both promises at once:
RE2-class regex cannot distinguish a top-level key from the same name
legally nested in undeclared content, and DuckDB's JSON functions
re-serialize. A scalar function registered by the reader at open keeps
the one-authority shape — the compiled expression is still the single
call site — at the cost of one connection-setup step the reader already
has a home for (the session-zone pin).

**Every election kind reaches the log.** A property that renders
wallclock in its table column but raw ns in the same export's `changes`
entries would reproduce, inside one mode, exactly the cross-surface
inconsistency the election surface exists to close — one property, one
rendering, every surface. Pinning the in-JSON temporal text to the
writers' CSV forms keeps that identity byte-testable instead of resting
on an incidental cast.

**Missingness passes; contradiction fails.** The `json_precision` split
mirrors the declared parse's: SQL `NULL`, an absent member, and a JSON
`null` leaf are three spellings of one fact — no value here — and all
three pass through byte-verbatim, fabricating nothing. A present string,
object, array, or boolean *contradicts* the author's numeric assertion
and fails loudly, exactly as a non-matching string fails the declared
parse. Erroring on `null` would treat the two members of the election
family oppositely on missingness, and would leave an author whose emit
carries one minted `null` no recourse but dropping the election
entirely.

**Raw-value changeset diffs.** The audited-property diff compares raw
after-image values because the alternative lets a presentation election
change row sets: two raw values rounding to one decimal text would
suppress the `update` and renumber every later `id`. Event existence and
numbering are facts of the data; an election renders values only — the
same line "ordering never derives from a rendered value" draws.

**One rendering per kind in the log, gated not resolved — and silence
is a side.** The log's `changes` is a per-kind surface fed by per-table
declarations. Conflicting elections are refused (`ElectionKindConflict`)
rather than resolved by precedence, because any precedence would make
the log's rendering depend on declaration order — the same reason the
unified map makes the per-column conflict unrepresentable instead of
picking a winner. A silent emitting table counts as a conflict, not an
abstention: its column ships the default raw rendering, and letting the
lone election win the log would put a raw table column beside an elected
log — inside one export, exactly the mixed rendering the reach rule
exists to close. The loud fix is one repeated line per sibling table;
the deliberate escape is `only` / `ignore` (§ Event-log and after-image
reach). The gate still reaches exactly as far as its reason: tables
differing on a property no log renders are legal, because they are
independent output surfaces and refusing them would constrain table
rendering for a log that does not exist — dimensional's per-column
freedom, kept wherever the log imposes no identity.

**Elections ride table declarations.** The log carries no declaration
surface of its own, so a kind audited with no declared table renders raw
codec `changes` text. An events-source `render:` map would add a second
declarer species — bare-name addressing, its own key-domain rule, a
cross-source conflict branch — to serve a case none of the motivating
payloads exhibit, and what its absence costs is the default rendering,
not an inconsistency. The agreement gate states what such a surface
would have to satisfy.

**Top-level keys only.** The motivating payloads are flat objects;
nested paths would import a path language (and its escaping and
ambiguity rules) for no observed case.

**Ties away from zero.** The rendering engine's native double→DECIMAL
conversion rule, verified on exact binary halves; adopting it keeps the
authority a single cast-shaped expression instead of a hand-rolled
rounding pipeline, and float64 sources make the tie case vanishingly
rare (only exact binary halves tie).

**Streaming gets the numeric family only.** Numeric elections change
value *text*, which is exactly what a codec surface can carry; the
temporal family's streaming exclusion (payloads are string-typed by
codec, `ts` is a separate contract) is the temporal family's own
boundary ([`temporal-elections.md`](temporal-elections.md)
§ Boundaries).

**Not the reader.** Placing elections at the read surface would redefine
"faithful" for every consumer at once — conformance and the corrupters
read through the reader, and each must keep seeing the emit's own
values. Mode-level composition above one shared authority gives the same
cross-mode identity without touching the read surface. The reader's one
involvement — registering the json-precision scalar at open — installs
capability, not transformation: no read the reader serves changes, and
no consumer below the modes calls the registered function.

**No `init` proposals, no sidecar extension.** No proposal engine can
know that a DOUBLE is a percentage or a BIGINT payload is an instant;
elections remain author-added edits. A per-column unit/instant
annotation in the sidecar would let upstream declare what forge asks the
author to assert — a plausible future contract extension, but the
contract is external and vendored, and this surface reads it as-is.

## Boundaries

- **No general cast surface.** Integer sources, VARCHAR-to-number
  parses, and free-form type changes stay outside the election set.
- **No nested JSON paths and no all-leaves wildcard** — named top-level
  keys only.
- **No log-only election surface.** `events` sources declare no
  elections; a kind audited with no declared table renders raw codec
  `changes` text. Elections ride table declarations (§ Rationale); the
  agreement gate states what a log-side declaration surface would have
  to satisfy.
- **No streaming temporal elections** — the temporal family's exclusion
  ([`temporal-elections.md`](temporal-elections.md) § Boundaries).
- **No corrupter change** — `schema_drift.retype_to` remains the only
  type-breaking surface; a corrupted payload that breaks a declared leaf
  fails the export loudly, the `date_parse` composition posture.
- **No election-specific fingerprint rule** — an election is ordinary
  config content under the incremental driver's fingerprint
  ([`incremental.md`](incremental.md)).
- **The reader seam is registration-only.** `register_render_functions`
  is connection setup at open; no read surface, sidecar accessor, or
  query API is election-aware, and no consumer below the modes calls the
  registered function.
- **No conformance change** — C1–C15 read the base layer unrendered.
  `compare`'s decimal canonical family ([`compare.md`](compare.md)) is
  the one accompaniment that keeps a decimal-elected render comparable;
  no other family, tolerance, or verdict semantics belong to this
  surface.

## Related

| Document | Why |
|---|---|
| [`temporal-elections.md`](temporal-elections.md) | The election-family sibling; the instant vocabulary and anchor rules the payload `instant` election reuses; the `date_parse` semantics the unified map hosts. |
| [`key-election.md`](key-election.md) | The sibling cross-mode election surface, including the after-image render sites the source/streaming reach mirrors. |
| [`source.md`](source.md) / [`base.md`](base.md) / [`dimensional.md`](dimensional.md) / [`streaming.md`](streaming.md) | The modes' attach grammars. |
| [`writers.md`](writers.md) | The pinned DECIMAL CSV text form; the per-type temporal text forms the log's `changes` entries pin to. |
| [`reader.md`](reader.md) | The session-setup site (the session-zone pin's species) where the rendering scalar function is registered at open. |
| [`compare.md`](compare.md) | The decimal canonical family that keeps a decimal-elected render comparable. |
| [`slice-only.md`](slice-only.md) | The refusal surface every elected source column joins. |
| [`anchor.md`](anchor.md) | The wallclock renderer the payload `instant` election compiles through. |
