# Sprint: source-row-selection

## Purpose

Implement optional row selection on source-mode declared units per
`docs/architecture/pending/source-row-selection.md`: a `where` predicate gated to
`temporal_class: constant` columns on state tables, junction tables, and event-log
sources, plus owner sub-type selection (`sub_types`) on membership units — so an
author can split a kind partitioned on an undeclared-but-constant property (rider
trips vs. driver shifts) and a sub-typed kind's membership estate (per-ward
junctions and join/leave streams) into separate declared tables with separate
audit streams.

The pending design doc is the authoritative semantics (the WHY). This spec carries
contracts, phases, and test cases (the WHAT). Where a rule or message is stated in
the doc, this spec cites it rather than restating.

## Scope

**Capabilities touched:**

- Source exporter: declaration grammar (`where` on both decl models; `sub_types`
  legal with `membership`), state-render predicate composition, junction
  owner-narrowing semi-join, event-log record/owner narrowing, selection-aware
  event-source disjointness, extended `init` membership proposals
- Row-predicate grammar: source's `where` fields become new *surfaces* of the
  shared grammar (`PredicateValue`, `render_predicate_condition`); the grammar
  itself is unchanged
- Validation: four new business rules (`SourceWhereColumnUnresolved` /
  `SourceWhereNotConstant` / `SourceWhereOnDiscriminator` /
  `SourceWhereValueUncastable`), the reused `discriminator-value-unobserved`
  notice, extended `SourceTableSubTypeUnknown` / `SourceSubTypesOnFlatKind` /
  `SourceEventSourceOverlap` / `SourceItemTypeCollision`

**Not included:** base-mode row predicates; owner-attribute projection into
junction rows; any predicate-grammar extension (range / negation / null tests);
`where` proposals in `init`; recipes (own lifecycle step, post-ship); folding the
pending doc (post-sprint, `/fold-pending`).

## Breaking Changes

- **`sub_types` with `membership:` is no longer a parse error.** The
  `sub_types`-requires-`kind` rule retires from both `SourceTableDecl.table_shape`
  (Phase 2) and `SourceEventSourceDecl.source_shape` (Phase 3, deleting the shared
  `_require_sub_types_only_with_kind` helper). Previously-valid configs are
  unaffected (a relaxation); previously-rejected configs become valid with owner
  sub-type semantics, validated at plan time against the **owner** kind's
  discriminator domain (existing `SourceTableSubTypeUnknown` /
  `SourceSubTypesOnFlatKind` messages, `{kind}` = the owner kind).
- Everything else is additive: `where` is optional on both decls; absent, every
  plan and render is byte-identical to today.

## Success Criteria

- [ ] A flat kind carrying a constant partition property splits into per-value
      state tables and per-value event-log sources (the ride-share shape), full
      and incremental, with row membership invariant across windows/invocations
- [ ] A sub-typed owner's membership estate splits per sub-type (junction tables
      and membership event sources), and a constant-owner-property `where` splits
      a junction (the NHS shape)
- [ ] Every key-axis misuse errors per the doc's gate table; the value axis
      notices (`discriminator-value-unobserved`) and never errors, except
      castability (`SourceWhereValueUncastable`)
- [ ] The overlap gate accepts exactly the doc's § Event-source disjointness
      matrix, comparing typed values, never strings
- [ ] `init --mode source` over a sub-typed owner proposes per-sub-type junction
      stubs + commented per-sub-type membership event-source entries; the emitted
      config parses and plans clean; no `where` is proposed
- [ ] Full suite green (`make test`); no behavior change for configs without
      selection

## Contracts

Design authority: `docs/architecture/pending/source-row-selection.md` (cited as
"doc §…"). Error messages and gate rules are specified there verbatim
(§ Validation Rules, § Event-source disjointness).

### Config models — `src/fabulexa_forge/config/models.py`

`_require_sub_types_only_with_kind` is **deleted** (Phase 3; its `table_shape`
call site drops in Phase 2). New fields (doc § Interface Contracts, adopted):

```python
class SourceTableDecl(StrictBaseModel):
    """One declared output table: a name, one population source, optional
    column selection, renames, and row selection."""

    sub_types: tuple[str, ...] | None = None
    """Explicit population subset (with `kind`) or owner sub-type subset
    (with `membership` — the junction renders intervals of owners in these
    sub-types, resolved through the parent lookup). Absent = every declared
    sub-type."""

    where: dict[str, PredicateValue] | None = None
    """Row predicate; entries AND-joined. Keys name `constant`-class payload
    properties of the subject kind (gated at plan time): source column names
    (`prop__<p>`) with `kind`, bare owner-property names with `membership`.
    Absent = every row of the selected populations."""
```

```python
class SourceEventSourceDecl(StrictBaseModel):
    where: dict[str, PredicateValue] | None = None
    """Record predicate over the subject kind (the declared kind, or the
    owner kind for a membership source), keyed by bare property name;
    entries AND-joined; keys must name `constant`-class payload properties
    (gated at plan time). Selects which records' (owners') events feed this
    source's audit stream — orthogonal to `only` / `ignore`, which select
    the audited property set."""
```

(`SourceEventSourceDecl.sub_types` docstring updates to the owner-subset wording
as in `SourceTableDecl` above.)

Validators — existing docstrings, minus the `sub_types`-without-`kind` clause,
plus the `where` shape clause:

```python
@model_validator(mode="after")
def table_shape(self) -> Self:
    """The declaration's structural shape (design doc § Config Models).

    Raises:
        ValueError: `name` is empty; not exactly one of `kind` /
            `membership` is set; `sub_types` / `columns` is
            present-but-empty or carries a duplicate entry; `rename` is
            present-but-empty or two keys share a target value; `where` is
            present-but-empty or has an empty key. (Value emptiness /
            duplication is carried by `PredicateValue` per entry.)
    """
```

```python
@model_validator(mode="after")
def source_shape(self) -> Self:
    """The declaration's structural shape (design doc § Config Models).

    Raises:
        ValueError: Not exactly one of `kind` / `membership` is set;
            `sub_types` / `only` / `ignore` is present-but-empty or carries
            a duplicate entry; both `only` and `ignore` are set; `item_type`
            is empty; `rename` is present-but-empty, has an empty key or
            value, or two keys share a target value; `where` is
            present-but-empty or has an empty key.
    """
```

### Errors — `src/fabulexa_forge/errors.py`

With the other `Source*` classes, matching their style:

```python
class SourceWhereColumnUnresolved(ExportError):
    """A `where` key names no payload property of the declaring unit's
    subject kind (the owner kind for a membership unit) — structural
    columns, membership element fields, and unknown columns all land here.
    Message per doc § Business Rules:
    `"{owner}: where key '{key}' not a payload property of kind '{kind}'"`."""


class SourceWhereNotConstant(ExportError):
    """A resolved `where` column's `temporal_class` is not `constant` —
    `tracked` and `slice_only` each carry their own message variant, per
    doc § Business Rules. `where` keys are this rule's to refuse; the
    existing `SourceSliceOnlyRead` population does not extend to them."""


class SourceWhereOnDiscriminator(ExportError):
    """A `where` key names the subject kind's declared discriminator;
    sub-type selection is `sub_types`' axis. Message per doc § Business
    Rules."""


class SourceWhereValueUncastable(ExportError):
    """A `where` element does not cast to its resolved column's
    sidecar-declared DuckDB type — constant-evaluated at plan time, before
    any write; the disjointness gate's typed-value comparison reuses these
    cast results. Message per doc § Business Rules:
    `"{owner}: where value '{element}' for '{key}' does not cast to {type}"`."""
```

### Typed-cast seam — `src/fabulexa_forge/_sql.py`

```python
def cast_predicate_element(element: str, sql_type: str) -> object:
    """The plan-time constant evaluation of the CAST `render_typed_literal`
    compiles: one predicate element's typed value under a column's declared
    DuckDB type.

    Returned values are hashable, and `==` / `hash` realize typed-value
    identity under `sql_type` — two spellings of one value ('5' / '05'
    under BIGINT) are one value. Reads no rows.

    Args:
        element: The raw config string element.
        sql_type: The column's DuckDB type from the sidecar.

    Returns:
        The typed value.

    Raises:
        ValueError: `sql_type` cannot cast `element` (the caller wraps this
            into `SourceWhereValueUncastable` with owner context).
        ExportError: `sql_type` is not a recognized DuckDB type — never a
            silent VARCHAR fallback (per `render_typed_literal`).
    """
```

### Plan-time seams — `src/fabulexa_forge/exporters/source/plan.py`

```python
@dataclass(frozen=True)
class SourceWhereEntry:
    """One resolved `where` entry: gate-passed and plan-time-typed."""

    key: str
    """The key as written (source-column or bare form)."""
    source_column: str
    """The base-table column identity (`prop__<p>`) on the subject kind's
    records table."""
    sql_type: str
    """The column's sidecar-declared DuckDB type."""
    value: str | list[str]
    """The config value, verbatim — what the rendering authority compiles."""
    typed_values: tuple[object, ...]
    """Per-element `cast_predicate_element` results, config element order —
    the disjointness gate's comparison set (doc § Event-source disjointness)."""
```

```python
def _resolve_where_selection(
    sidecar: Sidecar,
    where: dict[str, PredicateValue],
    subject_kind: str,
    key_form: Literal["source_column", "bare"],
    label: str,
) -> tuple[SourceWhereEntry, ...]:
    """The constant-column gate (doc § The constant-column gate): resolve
    every `where` key against the subject kind's payload-property set in the
    unit's key form, gate class and discriminator, and constant-evaluate
    every element's cast. Declaration entry order.

    Args:
        sidecar: The open emit's sidecar.
        where: The declaration's `where` mapping (present; callers skip the
            call when the field is absent).
        subject_kind: The declared kind, or the owner kind for a membership
            unit.
        key_form: 'source_column' (`prop__<p>`, records-backed tables) or
            'bare' (events sources and membership units).
        label: The declaring unit's message label (`table '<name>'` /
            `events source #<n>`).

    Returns:
        The resolved entries, `where` declaration order.

    Raises:
        SourceWhereColumnUnresolved: A key resolves to no payload property.
        SourceWhereNotConstant: A resolved column is tracked / slice_only.
        SourceWhereOnDiscriminator: A key names the discriminator.
        SourceWhereValueUncastable: An element fails its column's cast.
        TemporalClassUnavailableError: A consulted column's class is
            unavailable (C13, reader-owned).
        ExportError: A consulted column's declared type is unrecognized.
    """
```

```python
def _check_where_values_observed(
    sidecar: Sidecar,
    entries: tuple[SourceWhereEntry, ...],
    subject_kind: str,
    label: str,
    notice_sink: NoticeSink,
) -> None:
    """Emit dimensional's `discriminator-value-unobserved` notice per
    out-of-domain `where` element — shipped code, message granularity, and
    element order reused (doc § The constant-column gate; dimensional's
    `check_discriminator_value_observed`). A column with no `enum_domains`
    entry is unchecked. Never an error.

    Args:
        sidecar: The open emit's sidecar.
        entries: The unit's resolved `where` entries.
        subject_kind: The `enum_domains` key.
        label: The declaring unit's message label.
        notice_sink: Receiver for the notices.
    """
```

**Plan-unit field additions** (docstrings follow the field style above):
`SourceStateTablePlan.where: tuple[SourceWhereEntry, ...]`;
`SourceJunctionTablePlan.owner_populations: tuple[Population, ...]` (the
addressed owner set — full declared domain when `sub_types` absent; `where`
never narrows it, doc § The parent lookup) and
`.where: tuple[SourceWhereEntry, ...]`;
`SourceEventSourcePlan.where: tuple[SourceWhereEntry, ...]`. Empty tuple = no
predicate (config absence already detected at the decl).

**Modified (behavioral deltas, same signatures):**

- `_build_junction_table_plan` — resolves `decl.sub_types` against the **owner**
  kind's discriminator domain into `owner_populations` (existing
  `SourceTableSubTypeUnknown` / `SourceSubTypesOnFlatKind`, `{kind}` = owner)
  and `decl.where` via
  `_resolve_where_selection(..., subject_kind=owner_kind, key_form="bare", ...)`;
  item-type union-safety and member-column typing range over
  `owner_populations` (doc § The parent lookup). Adds those two errors plus the
  four `SourceWhere*` errors to Raises.
- `_build_event_source_plan` — same delta for membership sources (`sub_types` →
  owner populations; `where` → resolved entries, `key_form="bare"` for both
  source shapes).
- `_check_events_source_overlap` — extends to selection-aware disjointness: two
  sources auditing one item space are legal only via both-declared disjoint
  owner `sub_types` sets (membership) or one common predicated column whose
  `typed_values` sets are disjoint (doc § Event-source disjointness —
  existential over common columns; typed values, never strings; still never
  consults row data). The selection-failure message appends
  `"; selections do not establish disjointness"`.
- `_check_item_type_pairwise_distinctness` — sharing exception extends:
  membership sources of one `(kind, property)` may share one resolved
  item-type.

### Render seams — `src/fabulexa_forge/exporters/source/renders.py`

```python
def build_selection_spine_sql(
    sidecar: Sidecar,
    fork_path: str,
    kind: str,
    populations: tuple[Population, ...],
    where: tuple[SourceWhereEntry, ...],
) -> str | None:
    """The per-row selection spine: a `record_id`-producing SELECT over the
    kind's records spine of the records satisfying the population set AND
    the predicate conjunction (each entry via `render_predicate_condition`
    on its `source_column` / `sql_type`), or None when neither restricts
    (populations cover the declared domain or the kind is flat, and `where`
    is empty). Fan-out-free (`record_id` unique on the spine); evaluates
    current spine values (doc § Invariants #1). One seam for both
    directions: records-source narrowing, and the parent lookup when
    callers pass the owner kind of a membership unit.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch.
        kind: The subject kind (the owner kind for a membership caller).
        populations: The unit's addressed populations.
        where: The unit's resolved predicate entries; empty = none.

    Returns:
        The spine SELECT for an `IN`-semi-join, or None when no restriction
        applies.
    """
```

**Modified:** `build_state_render_sql` — when `table.where` is non-empty,
AND-composes each entry's `render_predicate_condition` (source column, sidecar
type, base-relation alias) into the population filter; the windowed shape
applies the same predicate at the window horizon (constant columns render
current — the mode's declared temporal-honesty exception — so row membership is
window-invariant). Predicates read source values — before rename, before
elected-surface rendering. `build_junction_render_sql` — when
`table.owner_populations` restricts or `table.where` is non-empty, semi-joins
the membership rows' owner `record_id` against
`build_selection_spine_sql(owner_kind, …)`; all other junction semantics
unchanged (doc § Row selection).

### Event-log seams — `src/fabulexa_forge/exporters/source/events.py`

`_records_population_filter_sql` **retires**; its callers compose
`build_selection_spine_sql` directly. **Modified:** `_build_records_arm_sql` —
the fold input narrows to the spine's records (populations AND `source.where`);
every event of an excluded record is excluded, `create` / `destroy` included;
the `id` numbering rule is unchanged — assigned over the narrowed whole-tape
set, beneath the window predicate (doc § Row selection).
`_build_membership_arm_sql` — the fold input narrows to intervals whose owner
`record_id` is in the owner-kind spine (owner populations AND `source.where`) —
the parent lookup applied from the membership side.

### `init` proposals — `src/fabulexa_forge/exporters/source/init.py`

`_JunctionUnit` gains `sub_type: str | None` ("The owner sub-type this stub
addresses, or None for a flat owner / whole junction."). **Modified:**
`_proposed_units` — a sub-typed owner's membership table proposes one junction
unit per declared sub-type, `name=f"{owner}_{sub_type}_{property}"`,
declared-domain order (doc § `init` proposals); flat owners unchanged.
`_membership_sources` — returns `(owner_kind, property, sub_type | None)`
triples, one per declared sub-type of a sub-typed owner. `_write_events_block`
— one commented entry per triple, carrying `sub_types: [<sub_type>]` when
present. **Changed signature:**

```python
def _write_junction_unit(
    w: Callable[[str], None],
    unit: _JunctionUnit,
    domain: tuple[str, ...],
    commented: bool,
) -> None:
    """Write one proposed `junction` table entry; a per-sub-type stub
    carries `sub_types: [<sub_type>]`, and the last stub of a sub-typed
    owner's set carries the commented combine-alternative (one whole
    junction, `sub_types:` omitted), mirroring `_write_state_unit`.
    No `where` is ever proposed.

    Args:
        w: Line-writing callable.
        unit: The proposed unit.
        domain: The owner kind's declared discriminator domain (empty for a
            flat owner) — last-stub detection, as `_write_state_unit`'s.
        commented: True when a same-named proposal was already emitted.
    """
```

## Phases

### Phase 1: Constant-gated `where` on state tables

**Delivers:** `SourceTableDecl.where` end-to-end for records-backed tables: the
parse-time shape clause, the four error classes, the typed-cast seam, the
constant-column gate, the domain notice, and the state render (full + windowed)
AND-composing the predicate.

**Demo:** Splits a flat kind carrying a constant `prop__journey_type` into
`trip` / `driver_shift` state tables (the ride-share shape); shows the refusal
messages for a tracked-column and a discriminator `where`; shows the
out-of-domain notice.

**Contracts:** `SourceTableDecl.where` + `table_shape` `where` clause (the
`sub_types`-with-`membership` relaxation is Phase 2's); the four `SourceWhere*`
errors; `cast_predicate_element`; `SourceWhereEntry`;
`_resolve_where_selection`; `_check_where_values_observed`;
`SourceStateTablePlan.where`; the `build_state_render_sql` delta.

**Steps:** `source → author` — the gate matrix and render tests re-read the
same deep plan/fixture surface the source step reshapes.

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `src/fabulexa_forge/_sql.py` |
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/source/renders.py` |
| Create | `tests/exporters/source/test_where_plan.py` |
| Modify | `tests/config/test_source_decls.py` |
| Modify | `tests/exporters/source/test_renders.py` |
| Modify | `tests/test_sql.py` |
| Create | `docs/sprints/source-row-selection/demos/phase_1_state_where.py` |

**Tests:**

- Parse: `where: {}` and `where` with an empty key rejected by `table_shape`;
  an empty-list / duplicate-element value rejected by `PredicateValue` at the
  offending entry's path
- Gate matrix on a records-backed table (doc § The constant-column gate, all
  seven rows): constant payload property accepted; `tracked` →
  `SourceWhereNotConstant` (tracked message); `slice_only` →
  `SourceWhereNotConstant` (slice_only message); discriminator →
  `SourceWhereOnDiscriminator` naming `sub_types`; structural column
  (`record_id`) → `SourceWhereColumnUnresolved`; unknown column →
  `SourceWhereColumnUnresolved`; bare name (missing `prop__` prefix) on a
  `kind:` table → `SourceWhereColumnUnresolved`
- Castability: a non-numeric element on a `BIGINT` column →
  `SourceWhereValueUncastable` naming the element; `cast_predicate_element`
  equates `'5'` / `'05'` under `BIGINT` (`==` and `hash`); an unrecognized
  type refused, never `VARCHAR`-defaulted
- Domain notice: an element outside a declared `enum_domains` entry draws one
  `discriminator-value-unobserved` notice per element, config element order;
  a fully-out-of-domain scalar states the unit renders no rows; a column with
  no `enum_domains` entry is unchecked; never an error
- Render: scalar compiles `=`, list compiles `IN`, entries AND-joined; only
  satisfying rows render; a NULL-valued predicated column's row never
  selected; `where` + `sub_types` AND-composed; zero-match table emitted
  empty; predicate column omitted from `columns` still selects; a
  reference-valued constant property compares base-layer record ids
- Windowed state: the same predicate applies at every window horizon; a
  record's presence across windows varies only by lifecycle, never predicate
  re-evaluation
- Existing source tests pass unchanged (no `where` → byte-identical plans and
  SQL)

### Phase 2: Membership-unit selection on junction tables

**Delivers:** Owner-keyed selection on `membership:` table declarations:
`sub_types` legal (owner sub-type subset), bare-name `where` through the parent
lookup, the addressed-owner-population narrowing for union-safety and
member-column typing, and the junction render's owner-narrowing semi-join.

**Demo:** Splits a sub-typed owner's junction per sub-type (the NHS
`ward_allocation` shape — owner column typed by the narrowed population's
election, not `VARCHAR`) and a flat owner's junction by a constant owner
property `where`; shows owner-domain validation errors.

**Contracts:** `table_shape` minus the `sub_types`-requires-`kind` clause (the
helper's table call site drops); `SourceJunctionTablePlan.owner_populations` /
`.where`; the `_build_junction_table_plan` delta; `build_selection_spine_sql`;
the `build_junction_render_sql` delta.

**Steps:** none (single implementer)

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/source/renders.py` |
| Modify | `tests/config/test_source_decls.py` |
| Modify | `tests/exporters/source/test_where_plan.py` |
| Modify | `tests/exporters/source/test_renders.py` |
| Modify | `tests/exporters/source/test_election_plan.py` |
| Create | `docs/sprints/source-row-selection/demos/phase_2_junction_split.py` |

**Tests:**

- Parse: `sub_types` with `membership:` now accepted on a table declaration
  (migrate the existing rejection cases to plan-level assertions)
- Plan: owner `sub_types` validated against the owner's discriminator domain
  (`SourceTableSubTypeUnknown` with the owner kind; `SourceSubTypesOnFlatKind`
  for a flat owner); a bare `where` key naming an owner constant accepted; a
  key matching only an element field → `SourceWhereColumnUnresolved`; a key
  matching both an owner property and an element field resolves to the owner
  property; an owner `tracked` property → `SourceWhereNotConstant`; the owner
  discriminator → `SourceWhereOnDiscriminator` pointing at `sub_types`
- Addressed populations: a junction with `sub_types` runs union-safety and
  member-column typing over the narrowed owner set — a mixed-election owner
  splits per sub-type, each narrowed junction's owner column carrying its
  populations' agreed declared type, not `VARCHAR`; a `where`-only junction
  addresses the owner's full declared population set (doc § The parent lookup)
- Render: a `sub_types` junction renders only intervals of owners in those
  sub-types; a `where` junction only intervals of satisfying owners; interval
  columns, element fields, member pairs, `columns` / `rename` unchanged; no
  owner attribute projected; `sub_types` + `where` AND-composed
- Incremental: extract-on-change runs over the narrowed interval set; an
  interval's table membership never varies by window (owner selection is
  constant-gated)
- Existing junction tests pass unchanged (no selection → byte-identical SQL)

### Phase 3: Event-log selection and selection-aware disjointness

**Delivers:** `SourceEventSourceDecl.where` (records and membership sources),
owner `sub_types` on membership sources, the retirement of
`_require_sub_types_only_with_kind`, the event-log arms narrowing through the
selection spine, and the extended overlap + item-type gates.

**Demo:** Splits one kind's audit stream into `trip` / `driver_shift`
item-types by `where` (dense tape-anchored `id` across both, identical `id`s
under a windowed run); shows the overlap refusal for non-disjoint selections
and the `'5'` / `'05'` typed-value case.

**Contracts:** `SourceEventSourceDecl.where` + `source_shape` (helper deleted);
`SourceEventSourcePlan.where`; the `_build_event_source_plan`,
`_check_events_source_overlap`, `_check_item_type_pairwise_distinctness`
deltas; the `events.py` deltas (`_records_population_filter_sql` retires;
`_build_records_arm_sql` / `_build_membership_arm_sql` narrow through
`build_selection_spine_sql`).

**Steps:** `source → author` — the disjointness matrix and events-render tests
are enumerative over the same deep events/fold surface the source step
reshapes.

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/source/events.py` |
| Modify | `tests/config/test_source_config.py` |
| Modify | `tests/exporters/source/test_where_plan.py` |
| Modify | `tests/exporters/source/test_events_render.py` |
| Modify | `tests/incremental/test_driver.py` |
| Create | `docs/sprints/source-row-selection/demos/phase_3_event_log_split.py` |

**Tests:**

- Parse: `where` accepted on an events source; `sub_types` with `membership:`
  accepted on an events source (migrate the existing rejection cases); the
  shared helper is gone (no remaining caller)
- Plan, gates: bare-key resolution on a records events source (the `only` /
  `ignore` addressing convention); a membership source's `where` resolves
  owner constants; the full gate matrix applies with `events source #<n>`
  labels
- Plan, disjointness (doc § Event-source disjointness, every row): two
  same-kind sources with a common predicated column and typed-disjoint value
  sets → legal; `'5'` vs `'05'` on a `BIGINT` column → one typed value,
  refused; no common predicated column → refused with the appended
  `"; selections do not establish disjointness"` clause; only one source
  selective → refused; intersecting value sets on every common column →
  refused; one disjoint common column suffices despite other shared columns
  intersecting; membership sources of one `(kind, property)` with
  both-declared disjoint owner `sub_types` → legal; population-disjoint
  records sources legal regardless of predicates
- Plan, item-type: membership sources of one `(kind, property)` may share one
  resolved item-type; all other collision clauses unchanged
- Events render: every event of an excluded record excluded (`create` /
  `destroy` included); a membership source's stream narrows to satisfying
  owners' intervals (`join` / `leave` both); `where` orthogonal to `only` /
  `ignore` (a property predicated and ignored simultaneously); `id` dense and
  1-based over the narrowed whole-tape set
- Incremental: a windowed export of a selection-narrowed log carries the same
  `id` values as the full export of the same tape (tape-anchored beneath the
  window predicate)
- Existing events tests pass unchanged (no selection → byte-identical SQL)

### Phase 4: `init` membership-estate proposals

**Delivers:** `init --mode source` proposes a sub-typed owner's membership
estate per sub-type: junction stubs (`<K>_<sub_type>_<p>`,
`sub_types: [<sub_type>]`, last stub carrying the commented
combine-alternative) and commented per-sub-type membership event-source
entries; the emitted config still always parses and plans clean.

**Demo:** Runs `generate_source_init_config` over an emit with a sub-typed
owner and a membership table; prints the candidate YAML; parses it and builds
the plan; uncomments the full membership event-source set and shows it plans
clean (shared default item-type + disjoint `sub_types` satisfy the gates).

**Contracts:** `_JunctionUnit.sub_type`; the `_proposed_units`,
`_membership_sources`, `_write_events_block` deltas; `_write_junction_unit`.

**Steps:** none (single implementer)

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/source/init.py` |
| Modify | `tests/exporters/source/test_init.py` |
| Modify | `tests/test_cli_init.py` |
| Create | `docs/sprints/source-row-selection/demos/phase_4_init_membership.py` |

**Tests:**

- A flat owner's membership table proposes one junction `<K>_<p>` — unchanged
- A sub-typed owner's membership table proposes one stub per declared
  sub-type, declared-domain order, named `<K>_<sub_type>_<p>` with
  `sub_types: [<sub_type>]`; the last stub carries the commented
  combine-alternative
- Membership event-source entries: one commented entry per declared sub-type
  carrying `sub_types: [<sub_type>]`; uncommenting the full set parses and
  plans clean (shared default item-type `<K>.<p>` under the extended sharing
  exception; disjoint `sub_types` satisfy the overlap gate)
- Name collisions follow the existing rule (later proposal commented-out,
  naming the collision); the emitted config always parses and plans clean
- No `where` appears anywhere in `init` output
- CLI `init --mode source` end-to-end reflects the new proposals
  (`tests/test_cli_init.py`)

## What Doesn't Change

Per doc § What Doesn't Change:

- **Base mode carries no row predicate** — its "every records kind, one flat
  table" contract is untouched; row filtering there remains undecided
- **`sub_types` remains the discriminator surface** — a `where` key naming the
  discriminator is refused with a pointer to `sub_types`, on membership units
  exactly as on records-backed ones
- **Element fields are never predicate-addressable** — membership selection
  reads the owner, never the element schema
- **No owner-attribute projection into junction rows** — junction columns stay
  exactly the membership surface
- **The predicate grammar is unchanged** — equality and set membership only;
  no range, negation, or null tests; `render_predicate_condition` and
  `PredicateValue` are consumed, not modified (the one addition is the
  plan-time `cast_predicate_element` seam beside the renderer)
- **`init` proposes no `where`** — per-sub-type membership proposals read only
  the declared discriminator domain
- **Dimensional, streaming, corrupters, writers, playback** — untouched; the
  corrupter's row selector remains its own grammar
- **Key election, `declare_keys`, anchor resolution, `kind_labels`** — the
  gates' mechanics are unchanged; only the population sets they range over
  follow the narrowed addressing (doc § The parent lookup)
- **The log's `id` rule** — position among the configured (now
  selection-narrowed) event set, tape-anchored, beneath the window predicate;
  the rule itself does not change

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/config/models.py` | `where` on both source decls; `sub_types` legal with `membership`; `table_shape` / `source_shape` extended; `_require_sub_types_only_with_kind` deleted |
| `src/fabulexa_forge/errors.py` | Four `SourceWhere*` error classes |
| `src/fabulexa_forge/_sql.py` | `cast_predicate_element` plan-time typed-cast seam |
| `src/fabulexa_forge/exporters/source/plan.py` | `SourceWhereEntry`, `_resolve_where_selection`, `_check_where_values_observed`; plan-unit `where` / `owner_populations` fields; junction + event-source builder deltas; selection-aware overlap gate; item-type sharing extension |
| `src/fabulexa_forge/exporters/source/renders.py` | `build_selection_spine_sql`; state render predicate composition; junction owner-narrowing semi-join |
| `src/fabulexa_forge/exporters/source/events.py` | Arms narrow through the selection spine; `_records_population_filter_sql` retires |
| `src/fabulexa_forge/exporters/source/init.py` | Per-sub-type junction stubs + commented membership event-source entries |
| `tests/exporters/source/test_where_plan.py` | New — gate matrix, castability, notice, disjointness matrix |
| `tests/config/test_source_decls.py` | `where` shape cases; `sub_types`-with-`membership` migration (tables) |
| `tests/config/test_source_config.py` | `sub_types`-with-`membership` migration (events sources); `where` shape cases |
| `tests/test_sql.py` | `cast_predicate_element` cases |
| `tests/exporters/source/test_renders.py` | State + junction selection rendering |
| `tests/exporters/source/test_events_render.py` | Event-log narrowing, `id` density |
| `tests/exporters/source/test_election_plan.py` | Narrowed-owner union-safety / member-column typing |
| `tests/incremental/test_driver.py` | Windowed `id` invariance over a narrowed log |
| `tests/exporters/source/test_init.py` | Membership-estate proposal cases |
| `tests/test_cli_init.py` | CLI-level init output |
| `docs/sprints/source-row-selection/demos/phase_1_state_where.py` | Demo — state-table split |
| `docs/sprints/source-row-selection/demos/phase_2_junction_split.py` | Demo — junction split |
| `docs/sprints/source-row-selection/demos/phase_3_event_log_split.py` | Demo — audit-stream split |
| `docs/sprints/source-row-selection/demos/phase_4_init_membership.py` | Demo — init proposals |
