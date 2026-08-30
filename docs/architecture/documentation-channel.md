# The Documentation Channel

**Status:** Implemented. Code is the contract — see
[`exporters/query_spec.py`](../../src/fabulexa_forge/exporters/query_spec.py)
(`ColumnProvenance`, `KindValueEntry`, the `QuerySpec` / `TableReport`
provenance, kind-value, and author-description maps),
[`exporters/init_annotations.py`](../../src/fabulexa_forge/exporters/init_annotations.py),
the companion builders in
[`exporters/companion/`](../../src/fabulexa_forge/exporters/companion/),
the override surfaces in
[`config/models.py`](../../src/fabulexa_forge/config/models.py)
(`ColumnDecl.description`, `TableDecl.description`,
`SourceTableDecl.descriptions` / `.description`,
`RenameEntry.descriptions` / `.description`), and
[`reader/documentation.py`](../../src/fabulexa_forge/reader/documentation.py).
Tests: per-mode
[`test_provenance.py`](../../tests/exporters/dimensional/test_provenance.py)
suites, [`tests/exporters/companion/`](../../tests/exporters/companion/),
[`tests/reader/test_documentation.py`](../../tests/reader/test_documentation.py).

The bundle carries five documentation surfaces: per-column `description` /
`unit`, per-table `description`, `enum_domains` per-value glosses, the run's
`scenario_description`, and the vendored contract's pinned structural-column
strings. The documentation channel forwards them end to end. The reader
resolves all five behind one typed view
([`reader.md`](reader.md) § The documentation view); the file-writing exports
embed the resolved dictionary in their companion README and manifest
([`companion-artifacts.md`](companion-artifacts.md)); the three `init`
proposal engines annotate generated configs with it as YAML comments; and the
corrupter's base-emit writer forwards the attributes so a corrupted emit keeps
its dictionary ([`corrupters.md`](corrupters.md) § The base-emit writer).
The channel has two author inputs, both consumed only by the companion
dictionary: an optional per-column `description` override and an optional
per-table `description` override in the three companion-writing modes'
export configs — where present, the author's prose is the column's or
table's rendered description (§ The author description override, § The
table-description resolution). Every rendered string is sourced from the
emit, the vendored contract, a forge-pinned dictionary constant, or the
author's export config (Principle #3); an undocumented item renders nothing
— no placeholder, no fallback, no inference.

---

## Boundary

- **Inputs.** The reader's `Documentation` view (via the `Sidecar` each
  consumer already holds) and, for exports, each mode's compiled plan — the
  one place the source of every output column is known.
- **Outputs.** Documentation fields in the companion README and manifest;
  YAML comments in `init` proposals; forwarded documentation attributes in
  the corrupter's regenerated sidecar. Streams carry none (§ Boundaries).
- **Config surface.** Two author inputs: per-column description prose
  (§ The author description override) and per-table description prose
  (§ The table-description resolution). Everything else the dictionary and
  the annotations carry is unconditional derived output, like the column
  inventory; `readme_overlay` remains the author's table- and export-level
  prose channel and composes with forwarded documentation (author prose
  renders first).

## Semantics

### The column-inheritance rule

An output column inherits documentation **iff its value is the faithful carry
of exactly one source column** — projection, rename, cast-back, a
temporal/value rendering election, dimensional `lookup`. The inherited
documentation is the *source* column's resolved answer, so a carried
structural column gets the contract string and a carried payload column its
sidecar prose. A column fed by computation or by more than one source
inherits nothing:

| Output column | Documentation |
|---|---|
| Pass-through / renamed payload column | source property's `description` / `unit` |
| Structural column projected as stored (`record_id`, `presentation_id`, an elected key surface carried without re-derivation) | the contract string, placeholders bound to the source instance — verbatim except the export-rewrite set below |
| Dimensional `lookup` column | looked-up property's `description` / `unit` |
| Dimensional `derived: value_map` column | source property's `description`; its declared value list is the **post-map domain** — the source options translated through the stamped map (glosses kept, unmapped options dropped — they render NULL), never the source's raw values, which the column does not contain |
| history_interval's interval-end column (the virtual `lead_sim_time`) | `sim_time`'s unit/origin with a **forge-authored end-of-validity description** — the contract documents only the one `sim_time` axis, and the start column's took-effect prose is false of the end bound |
| Derived measure, elapsed, `seq`, SCD-2 `valid_from` / `valid_to`, event-log `changes` / `event_type`, re-derived identity key (base's `<kind>_key` / `<p>_key`, any horizon-re-derived `record_index` / `ref_index` surface) | none — mode-template prose owns their meaning |
| Kind-name-as-value column (the source event log's `item_type` — README only) | per-value gloss list: each rendered label (post-`kind_labels`) glossed by the source kind's `tables[].description`, when present |
| Closed-domain property column | its declared value list rendered with per-value glosses where present (the list itself is `enum_domains` intent — sourced) |

**Export rewrites of base-pointing contract strings.** Four pinned structural
strings carry prose that points at base-layer structure a shaped export does
not contain — "equality-join against `records__<kind>.record_id`" (history's
`record_id`), "use `record_index` for creation order" (records `record_id`),
"its kind is the table name's `<K>` segment" (membership `record_id`), and
"present only when the sidecar declares it" (`presentation_id`). The contract
makes verbatim embedding a MAY, not an obligation (contract § Structural
column descriptions); the companion dictionary renders these four with the
dangling pointer clause rewritten out, keeping each string's factual core.
The rewrite set is enumerated (`_EXPORT_STRUCTURAL_REWRITES` in
[`dictionary.py`](../../src/fabulexa_forge/exporters/companion/dictionary.py)),
applies only to contract-answered docs, and never touches sidecar prose or
units. The reader's `Documentation` view itself is contract-verbatim —
the rewrite is a companion-rendering concern, not a reader one.

**Re-derived keys are computed, not carried.** A key surface produced by
re-derivation rather than projection — base's `<kind>_key` self key and
`<p>_key` reference keys, a `record_index` or `ref_index` surface
reconstructed for a point-in-time horizon — inherits nothing. The pinned
`ref_index__<name>` string says "resolved at the emitted slice", which a
horizon re-derivation makes false, and a verbatim string that misdescribes
the value it sits beside is invention by another route. A mode stamps
provenance only where the output is the source column's stored values,
faithfully projected.

**Unit inheritance stops where the rendering changes the unit.** A rendering
election that changes the value's unit-bearing form — a temporal rendering of
a sim-time structural column, an `instant` rendering of a payload column —
inherits the description but never the unit: `ns` is not true of a `DATE` /
`TIMESTAMPTZ` value, and the rendered type describes itself. `decimal`,
`json_precision`, and cast-back leave the value's unit-bearing form intact,
so the unit inherits with the description.

### The author description override

Each companion-writing mode's config carries an optional per-column
description override, attached in that mode's existing column-addressing
idiom: the dimensional column entry's `description` field, the source
declared table's `descriptions` map (keyed by source identity — the `rename`
key vocabulary), and the base rename entry's `descriptions` map (keyed by the
entry's `columns` key vocabulary; a descriptions-only entry satisfies the
entry's at-least-one-field rule). Field shapes and parse-time validation are
the models ([`config/models.py`](../../src/fabulexa_forge/config/models.py)).

For one output column, the rendered description resolves author-first — the
first present answer wins, and each later tier is the inheritance rule above:

| Tier | Source | Applies to |
|---|---|---|
| 1 | The author override | Any output column of the table |
| 2 | Forge-pinned dictionary constants (the event-log column set; the interval-end description; the four export rewrites) | The marked event-log table's columns; carried columns the other constants address |
| 3 | The inherited source-column answer (sidecar prose / contract string) | Columns with single-source provenance |
| 4 | Nothing | Everything else |

The override replaces the **description only**. On a carried column, unit
inheritance (including the unit-stops-where-the-rendering-changes rule),
declared enum-value lists, and kind-value gloss lists resolve identically
with or without the override; on
a column with no provenance — a derived measure, an SCD-2 validity column, a
re-derived key — the resolution yields a description-only doc where nothing
renders otherwise. The override does not depend on anything inheriting: it
renders even when the emit's sidecar is undocumented.

A bad `descriptions` key is the same addressing mistake as a bad `rename` /
`columns` key: each mode's existing key gates range over the entry's
`descriptions` keys and raise the same plan-time error identities at the
same gate point, before any write (source — `SourceColumnUnresolved` /
`SourceColumnNotAddressable` / the slice-only refusal; base —
`BaseRenameUnresolved` / `BaseRenameSliceOnly`). The dimensional surface
needs no key gate: the description rides the column entry itself and cannot
address a column that does not exist.

### The table-description resolution

Each companion-writing mode's table-addressing idiom carries an optional
table-level `description`, parallel to the per-column override: a field on
the dimensional table entry, on the source declared table, and on the base
rename entry — where it counts toward the entry's at-least-one-field rule,
so a description-only rename entry is legal and touches no name. The events
declaration carries no such field; strict models make a `description` key on
it a parse error. Field shapes and parse-time validation (non-empty,
non-whitespace — the column-override string rule) are the models
([`config/models.py`](../../src/fabulexa_forge/config/models.py)). No
plan-time key gate exists or is needed: each field rides a declaration whose
table addressing is already gated — dimensional and source by the
declaration itself, base by the rename entry's existing `table` / `sub_type`
resolution errors.

For one output table, the rendered description resolves first-present-wins:

| Tier | Source | Applies to |
|---|---|---|
| 1 | The author table override | Any table of the three batch modes |
| 2 | The forge-pinned event-log table description | The marked event-log table |
| 3 | The single-source sidecar forward (`tables[].description`, when every carried column agrees on one source table) | Tables with single-source provenance |
| 4 | Nothing | Everything else |

Tiers 1 and 2 never compete: the events declaration has no `description`
field, so the marked table can never carry an author entry. The resolved
answer renders in the README's per-table description slot and in the
manifest's per-table `description` field; absence renders nothing / JSON
`null`. An overlay `table:` note composes — the note renders first, then the
resolved description, both, never either-or. Resolution is
`resolve_table_description` in
[`dictionary.py`](../../src/fabulexa_forge/exporters/companion/dictionary.py).

### The pinned event-log documentation

The source event log's table and six columns are forge-constructed, so under
the inheritance rule they inherit nothing, and no config surface addresses
them: their documentation is mode-definitional, owned and versioned with the
mode's contract like the log's fixed column set and first id. The companion
dictionary carries a pinned table description and six pinned column
descriptions (`_EVENT_LOG_TABLE_DESCRIPTION` /
`_EVENT_LOG_COLUMN_DESCRIPTIONS` in
[`dictionary.py`](../../src/fabulexa_forge/exporters/companion/dictionary.py)),
applied only to a table whose report carries the event-log marker. A pinned
column doc resolves with `origin: "forge"`, no unit, and no enum options;
`item_type`'s kind-value gloss list renders beneath its pinned description
under the ordinary gloss rule.

The pinned prose is contract-bound to stay true under every author knob and
both source shapes the log carries; an edit to the strings must honor the
same constraints:

- It speaks of *items*, never tables or rows — a kind may be audited with no
  declared table, and a membership source's item is the owner's collection;
  `(item_type, item_id)` is the log's dereference key, not a table-row
  address.
- It names no id surface — `item_id` renders the elected surface per target;
  every elected surface is creation-constant, so the prose claims only that
  one item keeps one identifier.
- It claims no time unit or rendering — `occurred_at` may render raw
  nanoseconds or any elected temporal form.
- It claims no `item_type` vocabulary — values are kind labels or verbatim
  kinds.
- The `changes` pair encoding it states holds across every event shape:
  creations and membership joins carry `[null, value]`, deletions and leaves
  `[value, null]`, updates only the fields whose values differ — and a
  lifecycle event over an empty audited set renders `{}`, which the prose
  does not contradict.

The `changes`-key vocabulary (bare names, per-source `rename`) needs no
per-column prose: it is the mode template's subject. The pinned prose
depends on nothing inherited, so it renders even against an undocumented
emit.

### Provenance carriage

The inheritance rule is answered once, **at plan compile** — the one point
where each mode knows every output column's source, and equally the one
point it knows both the author's config addressing and the final output
names. Each mode stamps its compiled `QuerySpec` with three per-output-column
maps: one `ColumnProvenance` (source table, source column) per faithfully
carried column, one ordered `KindValueEntry` list per kind-name-as-value
column, and the author-description map — the mode's override surface
translated to output-column keys while compiling. The kind-value list's
order is the plan's own event-source compile order — the deterministic order
the event log unions its sources — so the README's gloss list renders in the
order the column's values are sourced, never an order chosen at render time.

Beside the three maps, each spec carries two table-level facts stamped the
same way: the author table description (`author_table_description` — the
mode's table-level override translated while compiling; dimensional and
source stamp it from the table declaration, base from the matched rename
entry) and the event-log marker (`event_log`), set only by the source
compiler and only on the one event-log spec it compiles — exactly one marked
spec when the plan carries an events declaration, none otherwise. Absence
(`None` / `False`) is the answer.

Both report-assembly sites — the shared full-export write dispatch and the
incremental driver's windowed report assembler — forward all three maps and
both table-level facts verbatim from the spec onto `TableReport`, which is how they reach the
companion builders on the report those builders already receive; no builder
entry-point signature carries a separate documentation parameter. The
builders resolve each entry through the documentation view and never
re-derive provenance or overrides from SQL, config, or the materialized
schema. Map keys are output column names (post-rename — the names the
materialized schema carries), so a report entry joins its provenance by
name. Absence of an entry *is* the answer — "inherits nothing" for the
provenance map, "no override" for the author map — with no fallback and no
empty-string sentinel. Field shapes are the dataclass definitions in
[`query_spec.py`](../../src/fabulexa_forge/exporters/query_spec.py).

### The rendered dictionary

The companion README and manifest are the channel's rendered surfaces; their
placement and byte-form rules are
[`companion-artifacts.md`](companion-artifacts.md)'s contract (§ The README,
§ The manifest). The README renders the scenario narrative, per-table
resolved descriptions, per-column description and unit, and declared-value
gloss lists; the manifest mirrors the same resolution machine-readably, with
JSON `null` encoding absence. The two surfaces render the same author-first
resolution and can never disagree, because resolution lives in the one shared
dictionary. An author-answered description carries `origin: "author"` and a
pinned event-log answer `origin: "forge"`, both stamped only by the companion
dictionary — the reader's documentation view
remains two-authority (contract / sidecar) and never sees export config. The
manifest's embedded config carries the override fields like any other config
content, so the provenance of authored prose is on record. Documentation is
run-level — the contract fixes it at run initialization — so every window of
an incremental export renders identical documentation, covered by the
whole-state artifact rewrite rule; a description edited mid-drip simply
renders from the next emitting window's whole-state rewrite (§ Boundaries —
the fingerprint excludes the description surfaces).

### `init` annotations

Every annotation is a YAML **comment**; comments are not grammar, so the
self-gating guarantee (the emitted config parses and plans/streams clean) is
preserved by construction, and proposals are pure functions of
`(emit, code version)`. All three engines (dimensional, source, streaming)
annotate through the shared helpers in
[`init_annotations.py`](../../src/fabulexa_forge/exporters/init_annotations.py):

| Site | Annotation | When absent |
|---|---|---|
| Top of the generated config (all three engines) | comment block carrying `scenario_description` | nothing |
| State / junction / dim / fact / stream stub for kind `K` or membership `(K, p)` | comment carrying the source table's `tables[].description` (a dim or fact stub: its source kind's) | nothing |
| A `sub_types: [<v>]` stub | comment carrying `<v>`'s discriminator gloss | nothing |
| A proposed property / column entry | comment carrying the property's `description` (and `unit`, appended) | nothing |
| The `keys` block | none (§ Boundaries) | — |

A membership reference field's bare `fields` entry has two sidecar columns
(`member__<f>__kind` / `member__<f>__id`); the contract forwards the one
field declaration's attributes onto both, so they agree by construction — the
annotation reads the `member__<f>__kind` column's entry, a tie-break of
convention, not a semantic choice.

A proposed list may render block-style rather than flow-style to carry
per-entry comments — a formatting change with identical parsed value, not a
grammar change. Commented-out alternatives (membership blocks, collision
losers) carry the same annotations inside their commented bodies, so
uncommenting keeps the documentation.

## Invariants

1. **Author-first, then one authority.** With an override present, the
   author's prose is the column's or table's description; with none,
   documentation resolves from exactly one source — the forge-pinned
   event-log set for the marked table, the vendored contract strings for
   structural columns, the sidecar for per-run columns and single-source
   tables. Never a blend, no
   fallback across authorities, no inference from names, types, or rows.
2. **Absence is absence, end to end.** No rendered surface ever emits
   placeholder prose ("no description"), a TODO, or derived text for an
   undocumented item.
3. **Sourced, never invented.** Every rendered documentation string traces
   to the sidecar, the vendored contract, a forge-pinned dictionary
   constant (the event-log table + column set; the interval-end
   description; the four export rewrites of
   base-pointing contract strings), or the author's export config — the
   same standing `readme_overlay` has on its surface. The only transformations are
   instance-placeholder substitution and those enumerated constant sets
   — nothing is ever derived from data, column names, or types, and a
   declared value list renders sourced values only: the sidecar's options,
   or their author-declared `value_map` images.
4. **Inheritance only under single-source provenance.** An output column
   carries documentation iff exactly one source column faithfully fed it —
   answered once at plan compile, carried on the report, never re-derived
   downstream.
5. **Documentation is presentation.** No export's row membership, linkage,
   ordering, or values depend on documentation; datasets, notices, and exit
   codes are byte-identical with documentation absent, and with or without
   overrides. The channel's one data-plane effect is the corrupter's sidecar
   carrying the attributes forward.
6. **Run-level determinism.** Same emit + same config + same code version →
   byte-identical rendered documentation, identical across every window of an
   incremental export.
7. **The marked table has no author tier.** No config surface addresses the
   event log's documentation — the events declaration rejects a
   `description` key (strict models) — so the author tier and the
   forge-pinned event-log tier never compete.
8. **At most one marked table per source plan, zero elsewhere.** Only the
   source compiler sets the event-log marker, only on the event-log spec:
   exactly one marked table when the plan carries an events declaration,
   none otherwise, and never in any other mode.

## Rationale

- **Provenance is stamped at plan compile, not recovered at render time.**
  Only the mode's compiler knows whether an output column is a faithful carry
  and of what; re-deriving the answer downstream from SQL text, config, or
  the materialized schema would be inference, and a wrong inheritance is a
  wrong meaning claim on real data. Stamping once also keeps the two
  report-assembly sites mechanical — they forward, never decide.
- **Computed columns inherit nothing because their meaning is
  mode-definitional.** An aggregate, an SCD-2 validity column, or the event
  log's `changes` has no single source whose prose describes it; the mode's
  README template owns that prose, which is forge-authored and stable.
- **The author tier outranks every forge answer because re-voicing is the
  author's call.** Producer-authored sidecar prose is not forge's to rewrite
  — the export-rewrite set covers only forge's own four base-pointing
  contract strings. When a rename moves a column into domain vocabulary,
  only the author knows the prose that describes it there; a computed column
  is the same gap from the other side — its template prose is generic, and
  only the author can say what the derived value means in the table's own
  vocabulary.
- **The override re-voices; it cannot silence.** There is no "render
  nothing" spelling, and an empty or whitespace-only description is a
  load-time error: a suppression surface would make absence ambiguous
  (undocumented vs. silenced), and an author who wants no inherited prose
  beside a column writes better prose.
- **The event log's kind-value gloss is kind-level.** A label's gloss is
  the source kind's `tables[].description` — often absent for author-declared
  kinds, by the contract's own design. Sub-type meaning renders where the
  discriminator column itself renders (the state table's closed-domain gloss
  list, the `init` `sub_types` comments) and is not duplicated onto the
  event log. An author table-level override on a *declared* table does not
  feed glosses: glosses are sidecar-sourced by design — they describe the
  bundle's kinds, not the export's tables.
- **The event log's documentation is forge-pinned because its columns are
  forge-constructed.** The log's columns mean exactly the same thing in
  every export and every domain — their semantics are the mode's published
  contract (the fixed column set, the first id, the changeset encoding) —
  which makes author prose the wrong tool and a config knob a surface with
  no legitimate use. Forge constructed these columns; forge describes them.
- **Annotations are comments because grammar must not move.** The `init`
  engines' self-gating posture — the emitted config parses and plans clean —
  is a contract; documentation that altered emitted grammar would put that
  guarantee at the mercy of per-emit prose.
- **`min` / `max` / `immutable` / `required` / `extra_data` are modeled and
  forwarded but not rendered** in the README, manifest, or `init` comments.
  They ride `ColumnSpec` because the corrupter's round-trip fidelity requires
  them; rendering them in the dictionary is a separable decision with its own
  presentation questions.

## Boundaries

- **Author input is description prose only.** The overrides re-voice a
  column's or table's description and nothing else: there is no unit
  override, no enum-gloss override, and no suppression — units and declared
  value lists are facts about the value, not voice. `readme_overlay` is the
  author's *additive* table- and export-level prose channel: its `table:`
  note renders beside the resolved description and replaces nothing. The
  source event log carries no author surface at either granularity — its
  documentation is forge-pinned (§ The pinned event-log documentation).
- **Corrupt configs carry no description surface.** The corrupter's
  base-emit writer forwards producer documentation attributes verbatim;
  re-voicing has no place in a corrupted base emit.
- **No `init` engine proposes description stubs.** Annotations are YAML
  comments; proposals are pure functions of `(emit, code version)`.
- **Streaming output carries no in-band documentation.** Streams have no
  companion artifacts, no documentation rides the messages, and the stream
  grammar carries no description override; streaming's place in the channel
  is its `init` engine only. A streaming-side documentation surface is a
  separate design.
- **The `keys` proposal block carries no annotations.** Identity election is
  not domain meaning; its guidance is the election menu's own commented
  alternatives ([`key-election.md`](key-election.md)).
- **Conformance is untouched.** No C1–C15 check ranges over documentation;
  the channel adds no checks and changes none.
- **The incremental fingerprint excludes documentation.** The canonical
  config dump excludes the description-override surfaces at both
  granularities — the per-column surfaces and the three table-description
  fields — and
  `readme_overlay` alike; documentation is run-level presentation and can
  never make a resumed drip refuse. Editing a description mid-drip renders
  from the next emitting window's whole-state artifact rewrite
  ([`incremental.md`](incremental.md) § Drained detection and the cursor).
- **The typed `enum_domains` routing surface is values-only.** Every consumer
  that routes on the declared value set reads the values-only ordered
  mapping; glosses live in the documentation view alone
  ([`reader.md`](reader.md) § The documentation view).

## Related

| Document | Why |
|---|---|
| [`reader.md`](reader.md) | The documentation view — the one resolution point (contract vs sidecar authority, placeholder substitution, enum glosses, scenario narrative) every consumer of this channel reads through |
| [`companion-artifacts.md`](companion-artifacts.md) | The rendered dictionary's home — README ordering and manifest fields |
| [`corrupters.md`](corrupters.md) | The base-emit writer whose round-trip invariant forwards the documentation attributes onto a corrupted emit |
| [`dimensional.md`](dimensional.md) / [`source.md`](source.md) / [`streaming.md`](streaming.md) | The three `init` proposal engines that annotate their output through the shared helpers |
| [`incremental.md`](incremental.md) | The windowed report assembler — the second provenance-forwarding site — and the fingerprint that excludes documentation |
| [`key-election.md`](key-election.md) | The `keys` proposal block this channel deliberately leaves unannotated |
| [`../../contract/base-format.md`](../../contract/base-format.md) | The vendored contract — the documentation attributes' schema and the pinned structural-column strings |
| [`../../CLAUDE.md`](../../CLAUDE.md) | Principle #3 — sourced, never invented — the channel's governing rule |
