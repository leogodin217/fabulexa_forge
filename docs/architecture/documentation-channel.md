# The Documentation Channel

**Status:** Implemented. Code is the contract — see
[`exporters/query_spec.py`](../../src/fabulexa_forge/exporters/query_spec.py)
(`ColumnProvenance`, `KindValueEntry`, the `QuerySpec` / `TableReport`
provenance maps),
[`exporters/init_annotations.py`](../../src/fabulexa_forge/exporters/init_annotations.py),
the companion builders in
[`exporters/companion/`](../../src/fabulexa_forge/exporters/companion/), and
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
Every rendered string is sourced from the emit or the vendored contract
(Principle #3); an undocumented item renders nothing — no placeholder, no
fallback, no inference.

---

## Boundary

- **Inputs.** The reader's `Documentation` view (via the `Sidecar` each
  consumer already holds) and, for exports, each mode's compiled plan — the
  one place the source of every output column is known.
- **Outputs.** Documentation fields in the companion README and manifest;
  YAML comments in `init` proposals; forwarded documentation attributes in
  the corrupter's regenerated sidecar. Streams carry none (§ Boundaries).
- **Config surface.** None. The dictionary and the annotations are
  unconditional derived output, like the column inventory; `readme_overlay`
  remains the author's prose channel and composes with forwarded
  documentation (author prose renders first).

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
units. The reader's `Documentation` view itself stays contract-verbatim —
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

### Provenance carriage

The inheritance rule is answered once, **at plan compile** — the one point
where each mode knows every output column's source. Each mode stamps its
compiled `QuerySpec` with two per-output-column maps: one `ColumnProvenance`
(source table, source column) per faithfully carried column, and one ordered
`KindValueEntry` list per kind-name-as-value column. The kind-value list's
order is the plan's own event-source compile order — the deterministic order
the event log unions its sources — so the README's gloss list renders in the
order the column's values are sourced, never an order chosen at render time.

Both report-assembly sites — the shared full-export write dispatch and the
incremental driver's windowed report assembler — forward both maps verbatim
from the spec onto `TableReport`, which is how they reach the companion
builders on the report those builders already receive; no builder entry-point
signature carries a separate documentation parameter. The builders resolve
each entry through the documentation view and never re-derive provenance from
SQL, config, or the materialized schema. Map keys are output column names
(post-rename — the names the materialized schema carries), so a report entry
joins its provenance by name. Absence of an entry *is* the "inherits nothing"
answer — there is no fallback. Field shapes are the dataclass definitions in
[`query_spec.py`](../../src/fabulexa_forge/exporters/query_spec.py).

### The rendered dictionary

The companion README and manifest are the channel's rendered surfaces; their
placement and byte-form rules are
[`companion-artifacts.md`](companion-artifacts.md)'s contract (§ The README,
§ The manifest). The README renders the scenario narrative, per-table
forwarded descriptions, per-column description and unit, and declared-value
gloss lists; the manifest mirrors the same resolution machine-readably, with
JSON `null` encoding absence. Documentation is run-level — the contract fixes
it at run initialization — so every window of an incremental export renders
identical documentation, covered by the whole-state artifact rewrite rule.

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

1. **One authority per question.** A column's documentation resolves from
   exactly one source — the vendored contract strings for structural columns,
   the sidecar for per-run columns. No fallback across authorities, no
   inference from names, types, or rows.
2. **Absence is absence, end to end.** No rendered surface ever emits
   placeholder prose ("no description"), a TODO, or derived text for an
   undocumented item.
3. **Sourced, never invented.** Every rendered documentation string traces
   to the sidecar, the vendored contract, or a forge-pinned dictionary
   constant (the interval-end description; the four export rewrites of
   base-pointing contract strings). The only transformations are
   instance-placeholder substitution and those two enumerated constant sets
   — nothing is ever derived from data, column names, or types, and a
   declared value list renders sourced values only: the sidecar's options,
   or their author-declared `value_map` images.
4. **Inheritance only under single-source provenance.** An output column
   carries documentation iff exactly one source column faithfully fed it —
   answered once at plan compile, carried on the report, never re-derived
   downstream.
5. **Documentation is presentation.** No export's row membership, linkage,
   ordering, or values depend on documentation; with documentation absent the
   datasets are byte-identical. The channel's one data-plane effect is the
   corrupter's sidecar carrying the attributes forward.
6. **Run-level determinism.** Same emit + same config + same code version →
   byte-identical rendered documentation, identical across every window of an
   incremental export.

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
- **The event log's kind-value gloss is kind-level.** A label's gloss is
  the source kind's `tables[].description` — often absent for author-declared
  kinds, by the contract's own design. Sub-type meaning renders where the
  discriminator column itself renders (the state table's closed-domain gloss
  list, the `init` `sub_types` comments) and is not duplicated onto the
  event log.
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

- **No author-facing config.** The dictionary and annotations cannot be
  switched off or shaped; every value they carry is sourced from the emit or
  the vendored contract. `readme_overlay` is the author's prose channel.
- **Streaming output carries no in-band documentation.** Streams have no
  companion artifacts, and no documentation rides the messages; streaming's
  place in the channel is its `init` engine only. A streaming-side
  documentation surface is a separate design.
- **The `keys` proposal block carries no annotations.** Identity election is
  not domain meaning; its guidance is the election menu's own commented
  alternatives ([`key-election.md`](key-election.md)).
- **Conformance is untouched.** No C1–C15 check ranges over documentation;
  the channel adds no checks and changes none.
- **The incremental fingerprint excludes documentation.** Documentation is
  run-level and is part of neither the fingerprint's inputs nor the cursor;
  it can never make a resumed drip refuse.
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
