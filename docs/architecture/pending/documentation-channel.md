---
status: draft
---

# Documentation Channel

Adoption of the v9 sidecar's end-to-end documentation channel: a typed
documentation view on the reader, a data dictionary in the companion
artifacts, semantic annotations on every `init` proposal, and documentation
fidelity in the corrupter's base-emit writer.

---

## Problem

The v9 bundle carries five documentation surfaces, and forge parses past all
of them. What exists in the input today:

1. **`columns[].description` / `unit`** — per-property business meaning and
   unit of measure, on records-table payload columns *and* membership
   element-field columns (`elem__<f>`, `member__<f>__kind` / `__id`).
2. **`tables[].description`** — engine-owned structural prose per kind and
   per membership table.
3. **`enum_domains` value-object glosses** — a per-allowed-value
   `description`, including sub-type discriminator values.
4. **`scenario_description`** — the run's declared narrative.
5. **Contract-verbatim structural-column strings** — the fixed meanings of
   `record_id`, `sim_time`, `joined_sim_time`, etc., pinned in the vendored
   contract's § Structural column descriptions, which the contract explicitly
   invites consumers to embed verbatim.

None reaches an output or a typed accessor. The typed `enum_domains` surface
yields bare value strings (glosses survive only in `Sidecar.raw`);
`ColumnSpec` models neither `description` nor `unit`; no accessor answers
"what does this column mean". Consequences:

- An exported dataset's README/manifest names tables and columns but cannot
  say what any of them mean — a classroom consumer profiles the DuckDB to
  guess that `prop__balance` is in GBP.
- An `init`-generated config proposes `sub_types: [day, weekend]` with no
  hint what those values are; the author opens the DuckDB to learn the
  domain the sidecar already documents.
- A corrupted emit **loses** its data dictionary: the base-emit writer
  rebuilds `tables[]` from `ColumnSpec`, which models only
  name/type/references/history_tracked/temporal_class — `description`,
  `unit`, `min`, `max`, `immutable`, `required`, `extra_data`, and the
  table-level `description` are silently dropped. Schema-legal (all are
  optional) but a breach of the forwarded-verbatim posture, and it starves
  every corrupt→export composition of documentation.

## Solution

One reader-first design with four attach points. A single typed
**documentation view** on the sidecar unifies the five surfaces behind one
resolution rule — structural columns answer from the vendored contract's
pinned strings, per-run columns answer from the sidecar, absence is silence
— and every other consumer reads through it. The companion README/manifest
embed the resolved dictionary; the `init` proposal engines annotate their
output with it as YAML comments; the corrupter's column/table specs grow the
attributes so the existing round-trip invariant forwards them.

```
                      Sidecar.documentation()          ← one view, five surfaces
                     /          |            \
        companion README   init proposals   (any future consumer)
        + manifest         (dimensional /
        (data dictionary)   source / streaming)

        ColumnSpec/TableSpec grow the attributes
                     └→ corrupter base-emit writer forwards them
```

Everything rendered is sourced, never invented (Principle #3): a column with
no declaration renders no prose, no placeholder, no fallback.

## Affected Subsystems

- **Reader** — `ColumnSpec` grows the seven optional per-column attributes
  the contract declares (`description`, `unit`, `min`, `max`, `immutable`,
  `required`, `extra_data`), carried verbatim, absent → `None`; `TableSpec`
  grows `description`. A new lazy accessor `Sidecar.documentation()` exposes
  the typed `Documentation` view (permissive posture, like the sibling
  registries — not the strict `presentation_keys` posture: documentation has
  no consistency rules to enforce). The contract's structural-column strings
  are vendored as a contract-pinned module literal — the same hardcoding
  class as the pinned column lists and the table-category enum.
- **Companion artifacts** — the README's ordering contract gains the
  scenario narrative and per-table/per-column documentation; the manifest
  gains machine-readable documentation fields and bumps
  `manifest_format_version`. All of it derived facts — no config surface, no
  author opt-in, absence renders as absence.
- **Export plan provenance (dimensional / source / base)** — the mode-neutral
  compiled table (`QuerySpec`) grows a per-output-column provenance surface,
  stamped at plan compile where each mode knows the answer: exactly one
  source `(table, column)` for a faithfully carried column (projection,
  rename, cast-back, temporal/value rendering election, `lookup`), or an
  ordered kind-value list for the event log's kind-name-as-value column. Both report-assembly sites — the shared
  full-export write dispatch and the incremental driver's windowed report
  assembler — forward it verbatim onto `TableReport`, which is how it
  reaches the companion builders on the report they already receive.
  Columns with single-source provenance inherit that column's resolved
  documentation; computed columns (aggregates, elapsed, `seq`, SCD-2
  validity columns, the event log's `changes`, re-derived identity keys)
  carry no entry and inherit nothing — their meaning is mode-definitional
  and lives in the mode's README template prose.
- **`init` proposal engines (dimensional, source, streaming)** — proposals
  annotate their output with documentation as YAML comments: scenario
  narrative at the top, table descriptions on table/dim/fact/stream stubs,
  discriminator glosses on `sub_types` values, per-property description/unit
  on proposed column lists. Comments never alter grammar — the self-gating
  posture (the emitted config parses and plans/streams clean) is untouched.
- **Corrupters (base-emit writer)** — no new writer rule. The existing
  invariant — *the writer round-trips every sidecar column attribute the
  reader models, joined to the written catalog by post-drift name, never
  re-looked-up from the source sidecar* — now covers the documentation and
  value-declaration attributes because `ColumnSpec` models them: they follow
  renames, drop with drops, and forward verbatim. `tables[].description`
  forwards per surviving table via `TableSpec`.

## What Doesn't Change

- **The typed `enum_domains` routing surface** stays a values-only ordered
  mapping (`kind → property → tuple[str, ...]`). Every consumer that routes
  on the declared value set is untouched; glosses live in the documentation
  view only.
- **No new config surface.** The dictionary is unconditional derived output,
  like the column inventory. The overlay grammar, its slots, and its
  refusals are unchanged — author prose and forwarded documentation coexist,
  author prose rendering first.
- **Conformance (C1–C15)** — no new checks, no changed checks.
- **`min` / `max` / `immutable` / `required` / `extra_data` are modeled and
  forwarded but not rendered** in the README, manifest, or init comments.
  They ride `ColumnSpec` because the corrupter's fidelity fix needs them;
  putting them in the rendered dictionary is a separable later decision.
- **`nullable` stays unmodeled — deliberately.** The schema's eighth optional
  per-column attribute is not documentation: it restates the written catalog
  (C2's territory), the schema licenses its omission, and a corrupter may
  legitimately change a column's effective nullability. The reader does not
  model it, so the corrupter writer's round-trip invariant — quantified over
  the attributes the reader models — continues to regenerate columns without
  it, correctly describing what was written.
- **Streaming output** — streams carry no companion artifacts and gain no
  in-band documentation; streaming's adoption is its `init` engine only.
- **The `keys` proposal block** gains no annotations — identity election is
  not domain meaning.
- **The event log's kind-value gloss list stays kind-level.** A label's
  gloss is the source kind's `tables[].description` — often absent for
  author-declared kinds, by the contract's own design. Sub-type meaning
  renders where the discriminator column itself renders — the state table's
  closed-domain gloss list, the `init` `sub_types` comments — and is not
  duplicated onto the event log.
- **The incremental fingerprint** — documentation is run-level and already
  part of neither the fingerprint's inputs nor the cursor.
- **`record_roles`, `row_census`, the anchor, playback, compare** —
  untouched.

## Semantics

### The documentation view

One view per `Sidecar`, constructed lazily on first call, permissive:
nothing is validated, nothing inferred, entry order and text are the
sidecar's / the contract's verbatim.

**Column resolution.** One authority per column — never both, never a
fallback from one to the other. A column is contract-answered iff the
vendored pinned block carries a string for its (family, name): the fixed
`history` table's pinned columns, the records-family structural names (the
reader's structural taxonomy's identity / presentation / lifecycle
classes), the membership-family structural names. Every other declared
column answers from its own sidecar entry — the `prop__` / `elem__` /
`member__` families *and* any declared column matching no name family (the
taxonomy's no-role outcome; e.g. a `schema_drift`-renamed payload column
read back through corrupt→export):

| Column | Authority | Answer |
|---|---|---|
| Fixed-table (`history`) column | vendored contract strings | pinned string + unit, verbatim |
| Records-table structural column (`fork_path`, `record_id`, `presentation_id`, `created_sim_time`, `active`, `deactivated_at`, `last_mutation_sim_time`, `record_index`, `ref_index__<name>`) | vendored contract strings | pinned string + unit; instance-bound placeholders substituted (below) |
| Membership-table structural column (`fork_path`, `record_id`, `joined_sim_time`, `left_sim_time`) | vendored contract strings | pinned string + unit; `<K>` substituted from the table name |
| `prop__` payload column | sidecar | `description` / `unit` verbatim; both absent → no documentation |
| `elem__<f>` / `member__<f>__kind` / `member__<f>__id` element field | sidecar | same |
| Declared column matching no name family (taxonomy no-role) | sidecar | `description` / `unit` verbatim; both absent → no documentation |
| Table or column the sidecar does not declare | — | `TableNotFoundError` / `ColumnNotFoundError` (the reader's existing identifiers-are-strict posture) |

Sidecar authority for every non-structural declared column is a total rule,
not a silent fall-through: the view answers a *meaning* question, so the
taxonomy's loud no-role posture — no consumer may pass an unclassified
column's data through silently — is untouched.

**Placeholder substitution.** A placeholder in a pinned string is
substituted exactly when the concrete column instance binds it: the column
name binds `<name>` (`ref_index__opened_by` renders "…the sibling
prop__opened_by column…"), the table name binds `<K>` / `<kind>` for
records- and membership-family strings. The `history` family's placeholders
vary per row, are bound by nothing, and stay verbatim — exactly the
contract's "embed verbatim" license, no more.

**Table prose.** `table_description` answers the sidecar's
`tables[].description` verbatim; absent → `None`. The fixed `history` table
answers `None` by construction (the contract deliberately carries its
meaning as contract prose, not sidecar prose).

**Enum glosses.** `enum_options(kind, property)` answers the ordered value
objects — `(value, gloss-or-None)` pairs — parsed from the raw
`enum_domains` value objects. Membership and order equal the typed
values-only `enum_domains` surface — one parse floor shared by both views,
so the value sequence of `enum_options(k, p)` is exactly the typed tuple;
the two surfaces can never disagree on the declared value set. Unknown
`(kind, property)` raises `KeyError` (mapping semantics). The sub-type
discriminator's glosses are `enum_options(kind, "<kind>_type")` — no
special surface.

**Scenario narrative.** `scenario_description` answers the top-level field
verbatim; absent → `None`. No name-derived fallback (the contract has none;
forge invents none).

### The data dictionary in companion artifacts

**Column inheritance rule.** An output column inherits documentation iff its
value is the faithful carry of exactly one source column — projection,
rename, cast-back, temporal/value rendering election, dimensional `lookup`.
The inherited documentation is the *source*
column's resolved answer (so a carried structural column gets the
contract-verbatim string, a carried payload column its sidecar prose). A
column fed by computation or by more than one source inherits nothing:

| Output column | Documentation |
|---|---|
| Pass-through / renamed payload column | source property's `description` / `unit` |
| Structural column projected as stored (`record_id`, `presentation_id`, an elected key surface carried without re-derivation) | contract-verbatim string, placeholders bound to the source instance |
| Dimensional `lookup` column | looked-up property's `description` / `unit` |
| Derived measure, elapsed, `seq`, SCD-2 `valid_from` / `valid_to`, event-log `changes` / `event_type`, re-derived identity key (base's `<kind>_key` / `<p>_key`, any horizon-re-derived `record_index` / `ref_index` surface) | none — mode-template prose owns their meaning |
| Kind-name-as-value column (the source event log's `item_type` — README only) | per-value gloss list: each rendered label (post-`kind_labels`) glossed by the source kind's `tables[].description`, when present |
| Closed-domain property column | its declared value list rendered with per-value glosses where present (the list itself is `enum_domains` intent — sourced) |

**Re-derived keys are computed, not carried.** A key surface produced by
re-derivation rather than projection — base's `<kind>_key` self key and
`<p>_key` reference keys, a `record_index` or `ref_index` surface
reconstructed for a point-in-time horizon — inherits nothing. The pinned
`ref_index__<name>` string says "resolved at the emitted slice", which a
horizon re-derivation makes false, and a verbatim string that misdescribes
the value it sits beside is invention by another route. The mode stamps
provenance only where the output is the source column's stored values,
faithfully projected.

**Unit inheritance stops where the rendering changes the unit.** A
rendering election that changes the value's unit-bearing form — a temporal
rendering of a sim-time structural column, an `instant` rendering of a
payload column — inherits the description but never the unit: `ns` is no
longer true of a `DATE` / `TIMESTAMPTZ` value, and the rendered type
describes itself. `decimal`, `json_precision`, and cast-back leave the
value's unit-bearing form intact, so the unit inherits with the
description.

**Provenance carriage.** The inheritance rule is answered once, at plan
compile: each mode stamps its compiled `QuerySpec` with the per-output-column
provenance maps — one `ColumnProvenance` (source table, source column) per
faithfully carried column, one ordered `KindValueEntry` list per
kind-name-as-value column, no entry otherwise. The kind-value list's order is
the plan's own event-source compile order — the deterministic order the event
log unions its sources, already fixed by the existing plan — so the README's
gloss list renders in the order the column's values are sourced, not an order
invented at render time. Each report-assembly site —
the shared full-export write dispatch and the incremental driver's windowed
report assembler — forwards both maps verbatim from the spec onto the
`TableReport`; the builders resolve each entry through the documentation view and
never re-derive provenance from SQL, config, or the materialized schema.
Absence of an entry is the "inherits nothing" answer — no fallback. Map keys
are output column names (post-rename, the names the materialized schema
carries), so a report entry joins its provenance by name.

**README ordering** (delta to the existing ordering contract, everything
else in place): the scenario narrative renders in the overview position
*after* the author's overlay `overview` (author prose first; either or both
may be absent, absence renders nothing). Each table section renders: overlay
note → forwarded table description → column inventory now carrying
description and unit per column → declared-value gloss lists for
closed-domain and kind-name-as-value columns → row count.

**Manifest.** Machine-readable mirror of the same resolution: top-level
`scenario_description`; per-table `description`; per-column `description`,
`unit`, and `enum_options` (the ordered `[{value, description}]` list where
the column's source property carries a declared domain). Absent →
JSON `null` (the manifest's stable-field-set posture, matching
`primary_key`); `null` encodes absence faithfully, never a default.
`manifest_format_version` bumps — the field set changed. The pinned byte
form is otherwise unchanged.

**Determinism.** Documentation is run-level (the contract fixes it at run
initialization), so every window of an incremental export renders identical
documentation; the README/manifest whole-state rewrite rule already covers
this. Same emit + same config + same code version → byte-identical
artifacts, as before.

### `init` annotations

Every annotation is a YAML comment; comments are not grammar, so the
self-gating guarantee (the emitted config parses and plans/streams clean) is
preserved by construction. Proposals stay pure functions of
`(emit, code version)`.

| Site | Annotation | When absent |
|---|---|---|
| Top of the generated config (all three engines) | comment block carrying `scenario_description` | nothing |
| State / junction / dim / fact / stream stub for kind `K` or membership `(K, p)` | comment carrying the source table's `tables[].description` (a dim or fact stub: its source kind's) | nothing |
| A `sub_types: [<v>]` stub | comment carrying `<v>`'s discriminator gloss | nothing |
| A proposed property / column entry | comment carrying the property's `description` (and `unit`, appended) | nothing |
| The `keys` block | none — out of scope by decision | — |

A membership reference field's bare `fields` entry has two sidecar columns
(`member__<f>__kind` / `member__<f>__id`); the contract forwards the one
field declaration's attributes onto both, so they agree by construction —
the annotation reads the `member__<f>__kind` column's entry, a tie-break of
convention, not a semantic choice.

A proposed list may render block-style rather than flow-style to carry
per-entry comments — a formatting change with identical parsed value, not a
grammar change. Commented-out alternatives (membership blocks, collision
losers) carry the same annotations inside their commented bodies, so
uncommenting keeps the documentation.

### Corrupter forwarding

| Attribute | Rides on | Behavior |
|---|---|---|
| `description`, `unit`, `min`, `max`, `immutable`, `required`, `extra_data` | `ColumnSpec` (drift-updated working spec) | verbatim; follows a `schema_drift` rename onto the relabeled spec; drops with a dropped column; survives a retype (the meaning claim outlives the type — realistic drift); absent stays absent |
| `tables[].description` | `TableSpec` | forwarded verbatim per surviving working table |

The writer's existing posture is unchanged: attributes are never re-looked-up
from the source sidecar by name, structural/identity columns regenerate as
the sidecar declared them (structural columns carry no sidecar documentation
by contract, so nothing new appears on them), and the writer still makes no
semantic promise.

### Invariants

Relied on (from the contract): documentation is run-level and fixed at run
initialization; divergent per-(kind, property) declarations are rejected
upstream — one meaning per column; absence of an attribute is silence, never
a default; structural-column meaning lives in the contract, never the
sidecar.

Introduced:

- **One authority per question.** A column's documentation resolves from
  exactly one source — the vendored contract strings for structural columns,
  the sidecar for per-run columns. No fallback across authorities, no
  inference from names, types, or rows.
- **Absence is absence, end to end.** No rendered surface ever emits
  placeholder prose ("no description"), a TODO, or derived text for an
  undocumented item.
- **Sourced, never invented.** Every rendered documentation string traces
  verbatim to the sidecar or the vendored contract; the only transformation
  is instance-placeholder substitution, which binds names the contract says
  the instance binds.
- **Inheritance only under single-source provenance.** An output column
  carries documentation iff exactly one source column faithfully fed it —
  answered once at plan compile, carried on the report, never re-derived
  downstream.
- **Documentation is presentation.** No export's row membership, linkage,
  ordering, or values change under this design; the corrupter's sidecar
  output changes only by carrying attributes it previously dropped.

## Configuration

None. The design adds no author-facing config: the dictionary and the
annotations are unconditional derived output, and every value they carry is
sourced from the emit or the vendored contract. (The existing
`readme_overlay` remains the author's prose channel and is unchanged.)

## Interface Contracts

### Runtime Types

```python
@dataclass(frozen=True)
class ColumnSpec:
    """One column of a base-layer table, as declared in base.json.

    Extended: the seven optional per-column attributes the contract declares
    are carried verbatim (absent -> None), never validated or coerced at
    parse — C1 owns schema conformance.
    """

    name: str
    type: str
    references: str | None
    history_tracked: bool | None
    temporal_class: str | None
    description: str | None
    unit: str | None
    min: float | int | None
    max: float | int | None
    immutable: bool | None
    required: bool | None
    extra_data: bool | None
```

```python
@dataclass(frozen=True)
class TableSpec:
    """One table present in run.duckdb, as declared in base.json.

    Extended: `description` carries tables[].description verbatim
    (absent -> None).
    """

    name: str
    category: str
    record_kind: str | None
    property: str | None
    columns: tuple[ColumnSpec, ...]
    rows: int
    description: str | None
```

```python
@dataclass(frozen=True)
class ColumnDoc:
    """Resolved documentation for one declared column.

    origin names the single authority that answered: "contract" for a
    structural column (pinned strings, instance placeholders bound),
    "sidecar" for a per-run column (verbatim carry).
    """

    description: str | None
    unit: str | None
    origin: Literal["contract", "sidecar"]
```

```python
@dataclass(frozen=True)
class EnumOption:
    """One declared allowed value of a closed-domain property."""

    value: str
    description: str | None
```

```python
@dataclass(frozen=True)
class ColumnProvenance:
    """The one source column that faithfully fed an output column.

    Stamped at plan compile for columns whose value is the faithful carry
    of exactly one source (table, column). Computed and multi-source
    columns get no entry — absence is the "inherits nothing" answer.
    """

    source_table: str
    source_column: str
```

```python
@dataclass(frozen=True)
class KindValueEntry:
    """One rendered label of a kind-name-as-value output column.

    label is the post-`kind_labels` rendered value; source_kind names the
    kind whose rows render under it (that kind's `tables[].description`
    is the label's gloss, when present). List order is the plan's
    event-source compile order.
    """

    label: str
    source_kind: str
```

```python
@dataclass(frozen=True)
class QuerySpec:
    """A compiled output table (existing mode-neutral shape).

    Extended: the two per-output-column provenance maps, stamped at plan
    compile. Keys are output column names (post-rename). A column in
    neither map inherits no documentation.
    """

    table_name: str
    sql: str
    write_mode: Literal["create", "append", "replace"]
    view_name: str | None
    view_sql: str | None
    keys: TableKeys | None
    provenance: Mapping[str, ColumnProvenance]
    kind_values: Mapping[str, tuple[KindValueEntry, ...]]
```

```python
@dataclass(frozen=True)
class TableReport:
    """One output table as written (existing report shape).

    Extended: `provenance` and `kind_values` forwarded verbatim from the
    table's QuerySpec by every report-assembly site — the shared
    full-export write dispatch and the windowed report assembler.
    """

    name: str
    columns: tuple[tuple[str, str], ...]
    row_count: int | None
    keys: TableKeys | None
    provenance: Mapping[str, ColumnProvenance]
    kind_values: Mapping[str, tuple[KindValueEntry, ...]]
```

### Functions

```python
class Documentation:
    """Typed, read-only documentation view over one emit's five surfaces.

    Permissive verbatim carry — nothing validated, nothing inferred,
    absence is silence. Constructed by Sidecar.documentation(); not
    constructed directly.
    """

    def scenario_description(self) -> str | None:
        """The run's declared narrative, verbatim; None when absent."""

    def table_description(self, table_name: str) -> str | None:
        """One table's tables[].description, verbatim.

        Args:
            table_name: A table the sidecar declares.

        Returns:
            The description, or None when the table carries none (always
            None for the fixed `history` table, per the contract).

        Raises:
            TableNotFoundError: table_name is not declared by the sidecar.
        """

    def column_doc(self, table_name: str, column_name: str) -> ColumnDoc | None:
        """Resolve one declared column's documentation.

        Structural columns (per the reader's structural taxonomy) answer
        from the vendored contract strings with instance-bound placeholders
        substituted; every other column answers from its sidecar entry.

        Args:
            table_name: A table the sidecar declares.
            column_name: A column that table declares.

        Returns:
            The resolved ColumnDoc; None when a per-run column carries
            neither description nor unit. Structural columns always answer.

        Raises:
            TableNotFoundError: table_name is not declared by the sidecar.
            ColumnNotFoundError: column_name is not declared by that table.
        """

    def enum_options(self, kind: str, prop: str) -> tuple[EnumOption, ...]:
        """The ordered declared value objects of one closed-domain property.

        Args:
            kind: A kind with an enum_domains entry.
            prop: A property in that kind's entry (a sub-typed kind's
                discriminator is `<kind>_type`).

        Returns:
            The declared options in sidecar order, glosses verbatim.

        Raises:
            KeyError: (kind, prop) has no enum_domains entry.
        """
```

```python
class Sidecar:
    def documentation(self) -> Documentation:
        """The emit's documentation view.

        Lazy: constructed on first call and cached. Never raises on
        construction — documentation has no consistency rules; identifier
        errors surface per query.

        Returns:
            The Documentation view over this sidecar.
        """
```

The companion-artifact builders and the three `init` engines take the view
(or the `Sidecar` they already hold) through their existing entry points.
Provenance reaches the builders on the report they already receive:
`QuerySpec` and `TableReport` grow the two per-column maps above — stamped
at plan compile, forwarded by both report-assembly sites — so no builder
entry-point signature changes. The manifest builder's field additions are
owned by its existing document-assembly function.

## Validation Rules

### Parse-Time (Pydantic)

None — the design adds no config surface.

### Parse floor (reader)

The structural floor's posture extends to the new attributes: a mis-typed
optional attribute (a non-string `description`, a boolean `min`) parses as
absent (`None`) — schema conformance is C1's job, and the floor stays a
floor. A mis-shaped `enum_domains` gloss likewise parses as gloss-absent,
while a malformed value object drops whole under the typed surface's
existing floor — one floor, two views, so the documentation view never
answers a value the routing surface dropped. No new refusals.

### Business Rules

None. The one new failure surface is identifier strictness on the view
(`TableNotFoundError` / `ColumnNotFoundError` / `KeyError`), stated in
§ Interface Contracts.
