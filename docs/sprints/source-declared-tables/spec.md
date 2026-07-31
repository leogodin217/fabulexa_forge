# Sprint: source-declared-tables

## Purpose

Rebuild the source exporter around author-declared output tables — populations →
named `state` / `junction` tables plus one polymorphic event log — deleting the
genre trichotomy. An educator declares the exact app-database schema they want
(`trips`, `customers`, one `versions` audit log) in YAML, or runs
`init --mode source` and edits the proposal; classification never decides layout
again.

**Design authority:** `docs/architecture/pending/source-declared-tables.md` owns
semantics, rationale, configuration grammar, and the validation-rule tables.
This spec does not restate it — it adds the reconciled contracts, phases, and
test cases. Where the two disagree, the two recorded deviations under
§ Contracts win (they exist precisely because the doc predates code
reconciliation); everything else defers to the design doc.

## Scope

**Capabilities touched:**

- config: `SourceConfig` rebuilt (`tables` + `events` + `declare_keys`); the four
  declaration models; `mode: source` requires its section
- shared exporter layer: population resolver (`Population`,
  `resolve_populations`); per-row identity-translation helper (election.py,
  additive)
- source mode: plan rebuilt over declared tables; `state` render; `junction`
  render (columns/rename-aware); event-log render (new); engine plan+compile
  split; windowed per-render membership
- key election: identity/edge gates re-anchored on declared tables and event-log
  item-types (gate definitions unchanged — new call sites only)
- playback shaped: source shape re-keyed on plan units; caller-side
  base_relations rewrite
- incremental: source window re-key (state → horizon snapshot, log → append,
  junction → carried); driver mechanics untouched
- CLI / init: `--mode {dimensional,source}` selector; source proposal engine
- recipes: source corpus rebuilt to the declared grammar
- docs/examples: the four `source.yaml` presets regenerated

**Not included:** streaming / dimensional / base / corrupter behavior; new
derivation residents (the event log composes the existing folds); Parquet;
multi-branch; architecture-doc promotion (`pending/` → live ships post-ACCEPT,
outside this sprint).

## Breaking Changes

- **`SourceConfig` grammar replaced.** `exclude`, global `rename`, and
  `change_delivery` are deleted; `tables` / `events` / `declare_keys` replace
  them. No existing source YAML survives: bare `mode: source` (the zero-config
  dump) becomes a load-time error (`mode_section_matches` — the dimensional
  posture), as do `source: {}` and a no-output declaration.
- **Output shape.** Per-kind wide CDC tables are no longer produced by any
  config; streaming is the sole owner of the CDC extraction archetype.
  Reference/transaction genre labels and the untracked sub-type force-split die.
- **Windowed state render omits `updated_at`** (horizon honesty); the full
  export keeps it. A `columns` / `rename` entry naming `last_mutation_sim_time`
  under a windowed invocation is refused at plan time.
- **Engine seam.** `build_source_query_specs(emit, config, anchor, window,
  notice_sink, base_relations)` → `build_source_plan(emit, config, anchor,
  election, windowed, notices)` + `build_source_query_specs(plan, window)`. The
  source engine loses `base_relations`; playback applies the rewrite itself.
- **Guard placement.** The elected-key uniqueness guard moves from the engine
  (post-render) to plan time. Observable: under playback `state(T)` the guard
  ranges over the full tape — a collision existing only among post-T records
  conservatively refuses.
- **Errors.** Retired: `SourceRecordRolesRequired`, `SourceRoleUnknown`,
  `SourceSubtypesUndeclared`, `SourceExcludeUnresolved`,
  `SourceRenameUnresolved`, `SourceRenameSliceOnly`. Added:
  `SourceTableKindUnknown`, `SourceTableSubTypeUnknown`,
  `SourceSubTypesOnFlatKind`, `SourceTableMembershipUnknown`,
  `SourceColumnUnresolved`, `SourceColumnNotAddressable`, `SourceSliceOnlyRead`,
  `SourceEventSourceOverlap`.
- **Recipe corpus.** Six folders retire (genre-named and deleted-feature
  recipes), two migrate, four new declared-grammar recipes land.

## Success Criteria

- [ ] A YAML config declaring state/junction tables + an events block exports
      exactly those tables (CSV + DuckDB), every value tracing to a base-layer
      value.
- [ ] Bare `mode: source`, `source: {}`, and a no-output declaration are
      load-time errors.
- [ ] Source mode consumes no `record_roles` anywhere (plan, renders, engine,
      init).
- [ ] A mixed-election kind exports via per-population declared tables — the
      trap the trichotomy structurally denied.
- [ ] The event log delivers `create`/`update`/`destroy` rows with `[old, new]`
      JSON changesets, elected-surface reference rendering, and non-NULL
      `item_id` on destroy rows.
- [ ] Windowed: state = per-window horizon snapshot without `updated_at`; event
      log = append; junction = extract-on-change. Full export state carries
      `updated_at`.
- [ ] `init --mode source` emits a commented candidate that parses and plans
      clean against the same emit; `init` without `--mode` is byte-identical to
      today.
- [ ] Playback shaped serves source shapes with render-keyed deliveries
      (state=snapshot, junction=append, log=append).
- [ ] Determinism holds: same emit + config + code version → identical output.
- [ ] `make test` green; recipe corpus rebuilt and green.

## Contracts

**Fixed by the design doc — do not re-derive:** the four declaration config
models + the `SourceConfig` swap (§ Config Models), `Population` +
`resolve_populations` (§ Runtime Types / § Functions shared),
`build_source_plan` + `build_source_query_specs` +
`generate_source_init_config` (§ Functions source mode), the parse-time
validators (§ Parse-Time), and the business-rule error table (§ Business
Rules).

**Two recorded deviations from the design doc** (carry into the post-sprint doc
promotion):

1. **Guard placement.** The doc's `build_source_query_specs` Raises clause
   ("ValueError … otherwise nothing") is honored by moving the data-dependent
   elected-key uniqueness guard into `build_source_plan`, which holds the open
   Emit. Sound — elected surfaces are creation-constant and truncation/windowing
   only subset rows — but conservatively strict under playback `state(T)`.
2. **Dual-shape playback head.** One shaped open serves `window()` and
   `state()`, but a plan has one windowed-ness: open validates the full-export
   shape; `window()` builds a windowed plan per ask, so a
   `last_mutation_sim_time` declaration surfaces `SourceColumnUnresolved` at the
   first `window()` ask, not at open.

The remainder of this section is the architect-reconciled contract set for
everything the design doc left open.

Module placement decided here:

| Unit | Module |
|---|---|
| `Population`, `resolve_populations` | `src/fabulexa_forge/exporters/populations.py` (new, shared layer) |
| `SourcePlan`, `SourceStateTablePlan`, `SourceJunctionTablePlan`, `resolve_state_table_keys` | `src/fabulexa_forge/exporters/source/plan.py` (rebuilt, Phase 3) |
| `SourceEventLogPlan`, `SourceEventSourcePlan`, `build_event_log_sql`, `build_changes_object_expr` | `src/fabulexa_forge/exporters/source/events.py` (new, Phase 2) |
| `build_state_render_sql`, `build_junction_render_sql` | `src/fabulexa_forge/exporters/source/renders.py` (rebuilt, Phase 3) |
| `build_identity_translation_sql` | `src/fabulexa_forge/exporters/election.py` (additive — no existing election function is modified) |
| `generate_source_init_config` | `src/fabulexa_forge/exporters/source/init.py` (new, Phase 4) |

`SourceEdgeSurface` (today `exporters/source/plan.py`) survives the rebuild
unchanged in shape — `(source_column, target_kinds, per_kind_populations,
rendered_type)` — and is reused by the event log's `changes` edges. Phase 2
imports it from the *current* plan.py; the Phase 3 rebuild keeps it.

---

### 1. SourcePlan and its unit dataclasses

**Decision — what SourcePlan carries.** The plan carries `sidecar`,
`fork_path`, `anchor`, and `windowed` — and neither the `Emit` nor the
`Election`. Rationale: every render is a pure function of
`(sidecar, fork_path, anchor, unit, window)`, and every election fact is
consumed at plan time and baked into the units as resolved surfaces
(`identity_surface`, `edge_surfaces`, `item_surface`), so compile needs no
election view; the one data-dependent step — the elected-key uniqueness guard
(`check_elected_key_unique`) — moves to plan time, where `build_source_plan`
holds the open `Emit`, making `build_source_query_specs(plan, window)` a
connection-free, pure SQL-composition step (which is exactly what the design
doc's Raises clause — "ValueError … otherwise nothing" — requires).

Guard-move soundness: elected surfaces are creation-constant and truncation /
windowing only subset rows, so uniqueness over the full tape implies
uniqueness over every truncated or windowed view — plan-time guarding against
the physical tape covers every seam. (Consequence flagged in § 2 and § 5:
under `state(T)` the guard is conservatively strict — a collision that exists
only among post-T records still refuses.)

**Decision — `windowed` is a SourcePlan field.** Yes: it is the fact
`build_source_query_specs` checks `window` presence against (the design doc's
ValueError contract), and it records which state-render shape the plan's
column validation ran against. Carrying it makes the disagreement checkable;
recomputing it is impossible (it is an invocation fact, not a config fact).

```python
@dataclass(frozen=True)
class SourceStateTablePlan:
    """One resolved `state` table: a declared thing-table over the
    populations of exactly one kind.

    `columns` is final: the records-column taxonomy applied, `columns` /
    `rename` selection resolved, the identity column rewritten to the
    elected surface's contract name (absorption under a presentation_id
    election applied), non-exempt slice_only columns absent, the
    discriminator retained/dropped per the >= 2 populations rule, and —
    under a windowed plan — `last_mutation_sim_time` absent (horizon
    honesty). Source names are base-table column identities
    (`record_id` / `record_index` / `presentation_id` for the identity
    slot, `created_sim_time`, `active`, `deactivated_at`, `prop__<p>`),
    never fold or output names.
    """

    name: str
    """Author-verbatim output table name."""
    kind: str
    """The records kind; the source table is `records__<kind>`."""
    populations: tuple[Population, ...]
    """The declared populations, discriminator-domain declaration order.
    A single (kind, None) atom for a flat kind. Drives the render's
    discriminator filter and the declare_keys combined-claim derivation."""
    columns: tuple[tuple[str, str], ...]
    """Ordered (source column, output column) pairs — the table's final
    delivered set."""
    identity_surface: KeySurface
    """The table's uniform elected identity surface ('record_id' under no
    election), gated at plan time over exactly `populations`."""
    edge_surfaces: tuple[SourceEdgeSurface, ...]
    """One entry per projected reference-valued `prop__<p>` column
    resolving a target with a declared records table, `columns` order."""
    keys: TableKeys | None
    """The table's declared keys (§ 4), resolved at plan time; None when
    `declare_keys` is off."""


@dataclass(frozen=True)
class SourceJunctionTablePlan:
    """One resolved `junction` table: a declared membership table.

    `columns` is final: the junction naming map applied (`record_id` ->
    `<K>_id`, `joined/left_sim_time` -> `joined_at`/`left_at`,
    `elem__<f>` -> `<f>`, `member__<f>__kind`/`__id` -> `<f>_kind`/`<f>_id`),
    then `columns` / `rename` selection resolved (the owner column always
    present; member pair columns selected independently). Declares no keys
    under declare_keys — the unit carries no keys field.
    """

    name: str
    """Author-verbatim output table name."""
    owner_kind: str
    """The owning kind `<K>`."""
    property: str
    """The membership property `<p>`."""
    source_table: str
    """The sidecar `membership__<K>__<p>` table name (carried verbatim —
    the sidecar owns the name mangling; the plan never re-derives it)."""
    columns: tuple[tuple[str, str], ...]
    """Ordered (source column, output column) pairs — the table's final
    delivered set."""
    edge_surfaces: tuple[SourceEdgeSurface, ...]
    """The owner column's entry first (when the owner kind has a declared
    records table), then one per *selected* member field, sidecar column
    order."""


@dataclass(frozen=True)
class SourceEventSourcePlan:
    """One resolved audited population set of the event log.

    Lives in `exporters/source/events.py` (Phase 2 hand-constructs it in
    tests; the Phase 3 plan builds it).
    """

    item_type: str
    """The contract identity: the kind name for a records source,
    '<K>.<property>' for a membership source."""
    kind: str
    """The audited kind (records source) or the owner kind `<K>`
    (membership source)."""
    property: str | None
    """The membership property, or None for a records source."""
    populations: tuple[Population, ...]
    """Records source: the addressed atoms (drives the fold's per-row
    discriminator narrowing and the overlap check). Membership source: the
    owner kind's full declared domain (drives per-row item_id resolution;
    membership sources are disjoint by (kind, property), not by these)."""
    audited_properties: tuple[str, ...]
    """The audited set, bare names, sidecar column-declaration order:
    every tracked- and constant-class property (discriminator included,
    slice_only policy-omitted) narrowed by only / widened-by-subtraction
    via ignore, for a records source; the selected element-schema field
    names (member reference fields by bare field name — the pair expands
    at render) for a membership source. Feeds the folds' property set
    verbatim (`build_row_state_events_sql` / `build_membership_events_sql`)."""
    item_surface: tuple[tuple[str | None, KeySurface], ...]
    """Per-population elected surface of the item target — the audited
    kind's addressed populations (records source) or the owner kind's
    domain (membership source). Union-safety is gated at plan time per
    item-type over the union across sources sharing the item_type."""
    change_edges: tuple[SourceEdgeSurface, ...]
    """One entry per audited reference-valued property (records source)
    and per audited member reference field (membership source) whose
    target carries a declared records table — the elected rendering
    inside `changes`, gated per audited reference property."""


@dataclass(frozen=True)
class SourceEventLogPlan:
    """The resolved event log: one polymorphic audit table."""

    name: str
    """Author-verbatim output table name."""
    sources: tuple[SourceEventSourcePlan, ...]
    """Declaration order; population sets pairwise-disjoint (validated)."""
    item_id_type: str
    """The junction-member-column type rule's verdict over the union of
    every source's `item_surface`: the common declared type when all
    agree, else 'VARCHAR' (record_index digit-rendered)."""


@dataclass(frozen=True)
class SourcePlan:
    """The resolved source plan: everything `build_source_query_specs(plan,
    window)` composes from, and nothing else.

    Carries `sidecar` / `fork_path` / `anchor` (the renders' pure inputs)
    and `windowed` (the shape the plan validated against); carries no Emit
    and no Election — data-dependent guards ran at plan build, election
    facts are baked into the units as resolved surfaces. Compile is
    therefore a pure function of (plan, window).
    """

    sidecar: Sidecar
    """The sidecar the plan resolved against (the truncated view's sidecar
    under playback state() — the plan never re-reads the emit)."""
    fork_path: str
    """The sole branch, resolved once via require_single_branch."""
    anchor: EffectiveAnchor
    """The resolved wallclock anchor (source requires one)."""
    windowed: bool
    """Which state-render shape the plan validated against; must agree
    with `window` presence at compile (ValueError otherwise)."""
    tables: tuple[SourceStateTablePlan | SourceJunctionTablePlan, ...]
    """One unit per `tables` declaration, declaration order."""
    events: SourceEventLogPlan | None
    """The event-log unit, or None when no `events` block is declared."""
```

Compile ordering (fixed by the design doc): one `QuerySpec` per unit in
`tables` declaration order, the event log last. Write modes:
full export (`window is None`) tags every spec `create`; windowed tags
`state` `replace`, `junction` `append`, event log `append`.

---

### 2. base_relations reconciliation

**Decision: caller-side rewrite.** `build_source_query_specs(plan, window)`
keeps the design doc's two-argument signature; playback's truncated-tape
state compile applies `apply_base_relations` over the returned specs itself.
Rationale: `apply_base_relations` is already a pure post-compile SQL rewrite
(that is its whole contract), so hoisting it to its only non-None caller
(`playback/shaped.py:_compile_state_specs`) loses nothing and keeps the
engine seam exactly as the design doc fixed it.

**Resulting playback-seam contract change.** The source engine loses its
`base_relations` parameter entirely (dimensional's is untouched). The
elected-key uniqueness guard, having moved to plan time (§ 1), executes
against the physical tape through the truncated view's shared connection —
sound because uniqueness of a creation-constant surface is monotone under
row-subsetting, and conservatively strict (documented in
`_compile_state_specs`' docstring).

```python
def _rewrite_specs_base_relations(
    specs: list[QuerySpec],
    base_relations: Mapping[str, str],
) -> list[QuerySpec]:
    """Apply the base-relations rewrite to every compiled spec.

    playback/shaped.py — the state() seam's post-compile step, replacing
    the engine-side rewrite the old `base_relations` parameter performed.
    Rewrites each spec's `sql` (and `view_sql` when present) via
    `apply_base_relations`, rebuilding the frozen QuerySpecs; every other
    field passes through unchanged.

    Args:
        specs: The mode engine's compiled specs, compile order.
        base_relations: Physical base-table name -> replacing relation
            SELECT, one entry per sidecar base table
            (`_truncated_base_relations`).

    Returns:
        The rewritten specs, input order.
    """
```

---

### 3. Render builders (source mode)

The event log warrants its own module — `exporters/source/events.py` — it is
the one new render, composes two folds plus the lag/diff/JSON machinery no
other render shares, and Phase 2 ships it standalone against
hand-constructed `SourceEventLogPlan` values. `renders.py` keeps the two
table renders.

#### 3a. Shared per-row identity-translation helper (new, `exporters/election.py`)

The junction member-column machinery (`_resolve_edge_render` /
`_member_edge_join_and_expr`, private to renders.py) is judged
**insufficient in placement and shape**, not in concept: it produces
column-expression + join-clause fragments keyed on
`(member__<f>__kind, member__<f>__id)` pairs, while the event log needs the
same creation-constant fact — record identity in its population's elected
surface, resolved per row through the records-spine discriminator for a
mixed-election kind — as a standalone joinable *relation*, applied in three
positions: records-source `item_id` (destroy rows join it, never the nulled
after-image), membership-source `item_id` (the owner), and `changes` value
translation (applied around the lag). One new shared helper, placed with the
existing horizon dispatchers `_record_index_sql` / `_presentation_key_sql`
it composes; no existing election function is modified.

```python
def build_identity_translation_sql(
    sidecar: Sidecar,
    fork_path: str,
    kind: str,
    per_population: tuple[tuple[str | None, KeySurface], ...],
) -> str:
    """One kind's record_id -> elected-surface translation relation.

    A two-column relation `(record_id, elected_value)`, one row per record
    of `kind` restricted to the listed populations: per population, the
    elected surface's value — record_id verbatim, record_index
    digit-rendered, presentation_id via the presentation-key derivation —
    resolved per row through the records-spine discriminator when the
    listed populations elect differing surfaces (the per-row
    mixed-election device the design doc's event log requires).
    `elected_value` is always VARCHAR — the union-safe common carrier; a
    caller needing a typed column (a uniform-surface item_id) CASTs the
    joined value to its resolved rendered type. Horizon-free: elected
    surfaces are creation-constant, so no as-of position exists to pass.

    Composes `_record_index_sql` / `_presentation_key_sql` and the
    records-spine read; a `per_population` uniformly electing 'record_id'
    still composes (identity projection) so callers need no special case.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch.
        kind: The target kind (must carry a `records__<kind>` table).
        per_population: (sub_type, surface) pairs — the populations rows
            may resolve to, each with its gated elected surface. A flat
            kind passes the single (None, surface) pair.

    Returns:
        The relation SELECT (composable as a subquery / CTE body).
    """
```

#### 3b. Table renders (`renders.py`, rebuilt — Phase 3)

```python
def build_state_render_sql(
    sidecar: Sidecar,
    fork_path: str,
    table: SourceStateTablePlan,
    anchor: EffectiveAnchor,
    window: Window | None,
) -> str:
    """The `state` render: one current row per record of the table's
    declared populations.

    Full export (`window is None`): the faithful records read
    (`build_records_relation_sql`), `updated_at` included. Windowed: the
    state-at reconstruction at the window horizon
    (`build_state_at_sql(..., horizon_ns=window.end_ns)`), no
    `updated_at` — the plan already validated the column set against this
    shape, so this builder never re-checks. Both shapes: discriminator
    filter to `table.populations` (omitted when the set is the kind's full
    domain — a no-op filter is not composed), the plan's (source ->
    output) projection, wallclock rendering of structural instants through
    the anchor renderer, identity column per `table.identity_surface`
    (self-identity join for a non-default surface, mirroring the current
    elected-identity join pattern), reference columns per
    `table.edge_surfaces` (LEFT JOIN on `build_identity_translation_sql`
    per non-default edge, CAST to the edge's rendered_type), NULL stays
    NULL. Total ORDER BY `(created_sim_time, record_id)` — raw keys,
    never rendered timestamps. A table with every surface at its default
    composes join-free SQL.

    Args:
        sidecar: The plan's sidecar.
        fork_path: The sole branch.
        table: The resolved state-table unit (from a plan whose
            windowed-ness matches `window` presence — the engine enforces
            the pairing; builders trust it).
        anchor: The resolved wallclock anchor.
        window: The incremental window, or None for a full export.

    Returns:
        The render SELECT.
    """


def build_junction_render_sql(
    sidecar: Sidecar,
    fork_path: str,
    table: SourceJunctionTablePlan,
    anchor: EffectiveAnchor,
    window: Window | None,
) -> str:
    """The `junction` render: one row per membership interval.

    Carried over from the current junction render in shape (faithful
    membership relation, wallclock `joined_at` / `left_at` with open
    intervals NULL, member ids in the target population's elected surface
    per row) — now projection-aware: renders exactly `table.columns`
    (the owner column always present; a member pair's two columns project
    independently; per-row election resolution consults the member kind
    internally even when `<f>_kind` is omitted). Windowed:
    extract-on-change over interval activity, `left_at` horizon-masked at
    `window.end_ns`. Total ORDER BY `(record_id, joined_sim_time,
    element fields in element-schema declaration order, VARCHAR-compared,
    NULLS FIRST)`.

    Args:
        sidecar: The plan's sidecar.
        fork_path: The sole branch.
        table: The resolved junction unit.
        anchor: The resolved wallclock anchor.
        window: The incremental window, or None for a full export.

    Returns:
        The render SELECT.
    """
```

The current `build_render_sql` genre dispatcher and
`build_snapshot_render_sql` die with the trichotomy; the engine dispatches
on the unit's type (two table builders + `build_event_log_sql`).

#### 3c. Event-log render (`events.py`, new — Phase 2)

```python
def build_event_log_sql(
    sidecar: Sidecar,
    fork_path: str,
    log: SourceEventLogPlan,
    anchor: EffectiveAnchor,
    window: Window | None,
) -> str:
    """The polymorphic event-log render: one audit table, event grain.

    Per records source: composes `build_row_state_events_sql(sidecar,
    fork_path, kind, frozenset(audited_properties))`, narrowed per row to
    the source's populations through the records-spine discriminator;
    recodes op c/u/d -> create/update/destroy. Per membership source:
    composes `build_membership_events_sql(sidecar, fork_path, owner_kind,
    property, fields)` (join -> create, leave -> destroy). Old values are
    a per-record lag over the fold's own audited after-images; `changes`
    is the design-doc JSON changeset (create: [null, v] for every audited
    property; update: exactly the differing entries, all-equal rows
    suppressed; destroy: [last, null]; empty audited set: '{}'), keys in
    sidecar column-declaration order, values the folds' CAST-AS-VARCHAR
    after-image strings verbatim or null, assembled via
    `build_changes_object_expr`. Reference-valued entries and membership
    member fields translate through `build_identity_translation_sql` per
    `change_edges` (fan-out-free, applied around the lag — order
    irrelevant, both agree); a member field expands in place to its
    `<f>_kind` / `<f>_id` entry pair. `item_id` joins the source's
    `item_surface` translation relation (destroy rows included — never
    the nulled after-image; the owner's identity for a membership
    source), CAST to `log.item_id_type` when non-VARCHAR. `occurred_at`
    renders wallclock through the anchor renderer. Sources UNION ALL in
    declaration order under the total ORDER BY `(event_sim_time,
    item_type, event_class, record_id, membership fields in
    element-schema declaration order, VARCHAR-compared, NULLS FIRST)`.

    Windowed: append rows with `event_sim_time` in [window.start_ns,
    window.end_ns), computed over the full fold — the lag's previous
    after-image may predate the window; membership selects rows, never
    alters content.

    Args:
        sidecar: The plan's sidecar.
        fork_path: The sole branch.
        log: The resolved event-log unit.
        anchor: The resolved wallclock anchor.
        window: The incremental window, or None for a full export.

    Returns:
        The render SELECT.
    """


def build_changes_object_expr(
    entries: tuple[tuple[str, str, str], ...],
) -> str:
    """The deterministic JSON-object SQL expression for one `changes` cell.

    Mode-owned SQL: builds a VARCHAR expression rendering
    `{"<key>": [old, new], ...}` from (key, old_value_expr,
    new_value_expr) triples, in the given order — entry inclusion (the
    update diff, the suppressed no-change row) is the caller's WHERE/CASE
    concern; this owns only object construction: JSON string escaping of
    keys and of the VARCHAR value expressions, `null` for SQL NULL, `{}`
    for an empty tuple. Never the conformance codec.

    Args:
        entries: (JSON key, old-value SQL expr, new-value SQL expr)
            triples, output order. Value exprs are VARCHAR-typed (the
            folds' after-image strings, already elected-translated).

    Returns:
        A VARCHAR-typed SQL expression.
    """
```

---

### 4. `resolve_source_table_keys` replacement

The engine-called `resolve_source_table_keys(sidecar, spec, change_delivery)`
dies with `change_delivery`. Its replacement is plan-internal: called by
`build_source_plan` only when `config.source.declare_keys` is true, its
result stored on each `SourceStateTablePlan.keys` (so the compile step stays
argument-free). Junction and event-log units declare nothing and carry no
keys field.

```python
def resolve_state_table_keys(
    sidecar: Sidecar,
    kind: str,
    populations: tuple[Population, ...],
    identity_surface: KeySurface,
    columns: tuple[tuple[str, str], ...],
) -> TableKeys:
    """One state table's declared keys under declare_keys.

    Primary key: the identity column's output name — `columns`' entry
    whose source name is the elected surface's contract column name.
    Unique on `presentation_id`'s output name iff (a) the registry claims
    uniqueness for exactly this table's resolved population set —
    `combined_claim` over the populations' PartitionKey entries
    (degenerate cases per the design doc: a flat kind reads the
    whole-table `key` claim; a single-population table its sub-type
    entry, presence-is-the-claim; any addressed population without an
    entry, or a derived no-claim combination, declares nothing) — AND
    (b) `identity_surface != 'presentation_id'` (already the primary key
    there, not doubly declared) AND (c) the `presentation_id` column
    survives in `columns` (absorbed under a presentation_id election,
    omittable via column selection).

    Args:
        sidecar: The plan's sidecar (claims via
            `sidecar.presentation_keys()` — strict-on-read).
        kind: The table's records kind.
        populations: The table's resolved population set.
        identity_surface: The table's gated elected surface.
        columns: The table's final (source, output) pairs — output names
            honor renames.

    Returns:
        The declared keys (primary key always; unique iff claimed).

    Raises:
        PresentationKeysInvalidError: The block is present and incoherent
            (strict accessor, propagated — plan-time, before any output).
    """
```

---

### 5. Playback shaped deltas (`playback/shaped.py`)

```python
def _source_window_delivery(
    unit: SourceStateTablePlan | SourceJunctionTablePlan | SourceEventLogPlan,
) -> Literal["append", "snapshot"]:
    """Static window-delivery class for one source plan unit.

    Mirrors the engine's windowed write_mode dispatch (the two surfaces
    must not drift): state -> 'snapshot' (a full horizon reconstruction
    per window, write_mode 'replace'), junction -> 'append'
    (extract-on-change), event log -> 'append'. Never None — no source
    render is rejected by the windowed-grain rule.

    Args:
        unit: The resolved plan unit.

    Returns:
        'append' or 'snapshot'.
    """


def _open_source(
    config: "ExportConfig",
    emit: "Emit",
    anchor: "EffectiveAnchor",
    notice_sink: "NoticeSink",
    election: "Election",
) -> tuple[ShapedTableDecl, ...]:
    """Run the source mode's full config validation and derive tables().

    Calls `build_source_plan(emit, config, anchor, election,
    windowed=False, notice_sink)` exactly once — the mode's complete
    validation surface, plan-time uniqueness guards included, notices
    emitted exactly once — and maps units to ShapedTableDecl(name,
    _source_window_delivery(unit)), `tables` declaration order, event log
    last. Signature deltas from today: takes the open Emit (plan-time
    guards read data), the resolved anchor (build_source_plan requires
    one; open_shaped_playback already refuses a None resolution for
    source), and the election (resolved once by open_shaped_playback via
    `resolve_election(sidecar, config.keys)` — the engine no longer
    resolves it internally).

    Open validates the FULL-export shape. A config whose `columns` /
    `rename` names `last_mutation_sim_time` therefore opens and serves
    state(); its first window() ask raises SourceColumnUnresolved from
    the windowed plan build — the source counterpart of the dimensional
    window_delivery=None diagnostic, surfaced as the plan-time refusal
    the design doc specifies rather than a decl field (the refusal is
    per-column, not per-table-class).

    Args:
        config: The export config (mode='source').
        emit: The open emit.
        anchor: The resolved effective anchor.
        notice_sink: Receiver for plan notices.
        election: The resolved key-election view.

    Returns:
        One ShapedTableDecl per output table.

    Raises:
        ExportError: A source business rule fails (the full plan-time
            surface, § build_source_plan).
        TemporalClassUnavailableError: Propagated.
    """
```

Seam deltas (existing functions, signatures unchanged unless noted):

- `_compile_window_specs` — source arm becomes
  `plan = build_source_plan(emit, config, anchor, election, windowed=True,
  notice_sink)` then `build_source_query_specs(plan, window)`; `election`
  is now required (non-None) for source shapes too. Per-call plan build
  matches the incremental driver's posture (deterministic; guard cost
  per window equals today's engine-side guard cost).
- `_compile_state_specs` — source arm becomes
  `plan = build_source_plan(truncated_emit, config, anchor, election,
  windowed=False, notice_sink)`, `specs =
  build_source_query_specs(plan, None)`, then
  `_rewrite_specs_base_relations(specs, base_relations)` (§ 2). Docstring
  documents the guard's full-tape conservatism (§ 1).
- `ShapedPlayback` — threads `election` for source shapes (field already
  exists; the None-for-source special case dies).
- Incremental driver (`incremental/driver.py:191-196`) — the source compile
  call becomes the same two-step plan+compile with `windowed=True`; the
  driver's shared mechanics are otherwise untouched. Both `change_delivery`
  consumers die with the field.

---

### 6. CLI init contract (`cli.py`, `exporters/source/init.py`)

```python
def cmd_init(
    emit_dir: Path,
    out_path: Path | None,
    mode: Literal["dimensional", "source"],
) -> int:
    """`fabulexa-forge init` — emit a commented candidate config.

    Dispatches on `mode`: 'dimensional' calls
    `exporters.dimensional.init.generate_init_config` (unchanged);
    'source' calls `exporters.source.init.generate_source_init_config`
    (design doc § Interface Contracts — one state table per kind, one
    junction per membership table, the events stub, the keys proposal).
    Both are pure functions of (emit, code version); output goes to
    `out_path` or stdout exactly as today. `mode` has no default here —
    the argparse layer owns the shipped default.

    Args:
        emit_dir: Directory holding run.duckdb + base.json.
        out_path: Where to write the candidate YAML; stdout when None.
        mode: Which mode's proposal engine to run.

    Returns:
        Process exit code (1 on ReaderError / ExporterError, else 0).
    """
```

`_cmd_init` (argparse) gains:

```python
parser.add_argument(
    "--mode", choices=("dimensional", "source"), default="dimensional"
)
```

`default="dimensional"` is the design doc's own shipped default ("a mode
selector with `dimensional` as the shipped default") — a CLI back-compat
fact, not an invented mapping value; `cmd_init` itself takes `mode`
explicitly. New module `src/fabulexa_forge/exporters/source/init.py` holds
`generate_source_init_config` (contract fixed in the design doc; Phase 4).

---

### 7. Errors (`errors.py`)

All confirmed against `errors.py` (existing Source errors at lines 175-233)
and the design doc's Business Rules / Retired rules tables. Add (Phase 1,
additive; ExportError subclasses, message shapes per the design doc's
Business Rules table — `{owner}` is `table '<name>'` / `events source #<n>`):

| Add | Raised when |
|---|---|
| `SourceTableKindUnknown` | declared `kind` has no `records__<kind>` table |
| `SourceTableSubTypeUnknown` | a `sub_types` entry is outside the kind's discriminator domain |
| `SourceSubTypesOnFlatKind` | `sub_types` given for a kind with no discriminator domain |
| `SourceTableMembershipUnknown` | a `membership` ref resolves to no sidecar table |
| `SourceColumnUnresolved` | a `columns`/`rename`/`only`/`ignore` entry names no column/property of its source surface (incl. the unrendered-surface and windowed-`updated_at` cases, message naming the election / omission) |
| `SourceColumnNotAddressable` | an entry names a mechanism column, or a `columns` entry names the table's elected surface |
| `SourceSliceOnlyRead` | an entry names a non-exempt slice_only column |
| `SourceEventSourceOverlap` | two events sources resolve overlapping population sets |

Retire (Phase 3, with the trichotomy): `SourceRecordRolesRequired`,
`SourceRoleUnknown`, `SourceSubtypesUndeclared`, `SourceExcludeUnresolved`,
`SourceRenameUnresolved`, `SourceRenameSliceOnly`. All six exist and are
referenced only from `exporters/source/plan.py` + its tests
(`SourceRecordRolesRequired` reference check confirms; dimensional init's
record_roles error is its own) — retirement is clean.

Keep unchanged: `SourceHistoryTrackedRequired`, `SourceNameCollision`
(message's "resolve via source.rename" updates to "resolve via rename",
Phase 3), `SourceAnchorRequired`, `SourceUnclassifiedColumn`.

**No missing error found**: every rule in the design doc's Business Rules
table maps to an added, kept, or foreign (Election*/Presentation*/reader)
class. The add list is complete.

---

### 8. ExportConfig validator delta (`config/models.py`, Phase 3)

`mode_section_matches` (models.py:802) changes one arm — `mode='source'`
joins the dimensional posture:

```python
@model_validator(mode="after")
def mode_section_matches(self) -> Self:
    """The section named by `mode` is present and the other modes'
    sections are absent.

    `mode='dimensional'` requires `dimensional` (unchanged).
    `mode='source'` now requires `source` — the bare-dump allowance is
    removed; a source config declares its output or is refused at load
    (SourceConfig's own validator additionally requires >= 1 of tables /
    events, so `source: {}` is equally refused). `mode='base'` keeps its
    escape-hatch posture (bare `mode: base` stays legal). Whichever mode
    is selected, the other modes' sections must be absent.

    Raises:
        ValueError: `mode='dimensional'` without `dimensional`;
            `mode='source'` without `source`; any mode with another
            mode's section present.
    """
```

The `SourceConfig` swap itself (tables/events/declare_keys replacing
exclude/rename/change_delivery, plus the `source_section_required` /
`table_source_exclusive` parse-time validators) is fixed by the design doc's
§ Config Models / § Parse-Time and lands atomically in Phase 3; the four
declaration models (`MembershipRef`, `SourceTableDecl`,
`SourceEventSourceDecl`, `SourceEventsDecl`) land unreferenced-by-
`SourceConfig` in Phase 1 (they are exercised by `resolve_populations` and
model tests — not scaffolding).

---


## Phases

### Phase 1: Population resolver + declaration vocabulary + errors

**Delivers:** The shared population-set resolver (`exporters/populations.py`:
`Population`, `resolve_populations`), the four declaration config models
(`MembershipRef`, `SourceTableDecl`, `SourceEventSourceDecl`,
`SourceEventsDecl` — standalone classes with their field validators, exercised
by tests; wired into `SourceConfig` in Phase 3), and the eight new `Source*`
error classes.

**Demo:** Resolves whole-kind, sub-type-subset, flat-kind, and membership
addresses against a fixture emit; shows the three resolution errors firing with
owner-prefixed messages.

**Contracts:** design doc § Config Models (decl models), § Runtime Types
(`Population`), § Functions shared (`resolve_populations`); § 7 error add list.

**Steps:** none (single implementer).

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Create | `src/fabulexa_forge/exporters/populations.py` |
| Create | `tests/exporters/test_populations.py` |
| Create | `tests/config/test_source_decls.py` |
| Create | `docs/sprints/source-declared-tables/demos/phase_1_populations.py` |

**Tests:**

- Flat kind resolves to the single `(kind, None)` atom.
- Sub-typed kind without `sub_types` resolves the full discriminator domain, in
  domain declaration order.
- Explicit `sub_types` given out of domain order resolve in domain declaration
  order, not the given order.
- Unknown kind → `SourceTableKindUnknown`, message prefixed with the verbatim
  `owner` label (`table 'trips'`).
- Sub-type outside the domain → `SourceTableSubTypeUnknown`; the
  `events source #2` owner form appears verbatim.
- `sub_types` on a flat kind → `SourceSubTypesOnFlatKind`.
- Decl model validators: exactly one of `kind` / `membership` (both / neither
  rejected, table and events-source shapes alike); `sub_types` only with
  `kind`; `name` / `columns` / `rename` / `sub_types` / `sources` / `only` /
  `ignore` non-empty when present with distinct entries; `rename` values
  distinct; `only` + `ignore` together rejected; `MembershipRef` requires both
  fields; extra fields forbidden (StrictBaseModel).
- Existing tests that must still pass: the whole suite — this phase is purely
  additive (`make test`).

### Phase 2: Event-log render (standalone)

**Delivers:** `exporters/source/events.py` — `SourceEventSourcePlan`,
`SourceEventLogPlan`, `build_event_log_sql`, `build_changes_object_expr` — plus
the shared `build_identity_translation_sql` in `exporters/election.py`
(additive; no existing election function modified). Plans are hand-constructed
in tests; Phase 3 wires plan.py to produce them and the engine to compile them.

**Demo:** Renders and executes a `versions` log over a fixture emit (one
records source with `only`, one membership source): prints
create/update/destroy rows with their `changes` JSON, shows a non-NULL destroy
`item_id` and the membership join/leave recode.

**Contracts:** § 3a (`build_identity_translation_sql`), § 3c
(`build_event_log_sql`, `build_changes_object_expr`), § 1
(`SourceEventSourcePlan`, `SourceEventLogPlan`).

**Steps:** none (single implementer).

**Files:**

| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/exporters/source/events.py` |
| Modify | `src/fabulexa_forge/exporters/election.py` |
| Modify | `tests/exporters/test_election.py` |
| Create | `tests/exporters/source/test_events_render.py` |
| Create | `docs/sprints/source-declared-tables/demos/phase_2_event_log.py` |

**Tests:**

- Identity translation: uniform `record_id` election renders identity
  projection; `record_index` digit-rendered VARCHAR; `presentation_id` via the
  presentation-key derivation; a mixed `per_population` resolves per row
  through the records-spine discriminator; every record of the kind appears
  exactly once.
- `build_changes_object_expr`: JSON escaping of quotes / backslashes / control
  characters in keys and values; SQL NULL → `null`; empty tuple → `{}`; entry
  order preserved byte-exactly.
- Event log create: every audited property `[null, value]`; destroy:
  `[last value, null]` with old values from the preceding after-image.
- Update: exactly the differing audited entries; an update touching no audited
  property emits no row; coincident same-sim-time changes coalesce into one
  event (fold grain).
- Destroy `item_id` is never NULL (identity join, not the nulled after-image).
- Membership source: join → `create` `[null, value]`, leave → `destroy`
  `[value, null]`; `item_type` is `<K>.<property>`; `item_id` is the owner's
  elected identity; a member reference field expands in place to its
  `<f>_kind` / `<f>_id` entry pair.
- Empty audited set: `create` / `destroy` emitted with `changes = '{}'`.
- Discriminator: appears in create/destroy changesets of a sub-typed source,
  never spawns an update.
- A `sub_types`-narrowed records source emits events only for rows resolving to
  the addressed populations (per-row spine filter).
- Reference-valued audited property renders old/new in the target's elected
  surface (`record_index` target → digit strings); NULL stays NULL.
- `item_id` type rule: all sources agreeing on a declared type → that type;
  disagreement → VARCHAR with `record_index` digit-rendered.
- Total order is tie-free across sources sharing an `event_sim_time`
  (`item_type` interposed); deterministic across two renders.
- Windowed: rows selected by `event_sim_time` ∈ [start, end); an update whose
  previous after-image predates the window keeps its correct `[old, new]`.

### Phase 3: The cutover (atomic)

**Delivers:** `SourceConfig` swapped to the declared grammar
(+ `mode_section_matches` delta), plan.py rebuilt to produce `SourcePlan` over
declared tables (resolution, taxonomy classification, `columns` / `rename`
resolution, identity/edge/item-type gates, overlap + collision + reserved-name
checks, plan-time uniqueness guards, `resolve_state_table_keys`), renders.py
rebuilt (state + junction builders over plan units; genre dispatch and
changelog/snapshot renders deleted), engine.py rebuilt (plan+compile split,
per-render write modes, event-log wiring), playback shaped re-keyed,
incremental driver call-site updated, the six retired errors deleted, and the
whole test estate + recipe corpus migrated. The suite may be red between steps;
the phase gate runs once after the pipeline.

**Demo:** Full export of a declared config over a fixture emit — two state
tables (one a sub-type subset), a junction, and a `versions` log — then a
two-window drip showing state snapshots without `updated_at`, appended log
rows, and junction extract-on-change.

**Contracts:** §§ 1, 2, 3b, 4, 5 (seam deltas), 8; design doc
`build_source_plan` / `build_source_query_specs` / § Business Rules.

**Steps:** `source (config+plan) → source (renders+engine+playback+driver;
creates the demo) → author (fixtures) → author (config+plan suites) → author
(render+engine suites) → author (election suites) → author (playback suites) →
migrate (fan-out, 6 files) → author (recipes)` — mirrors the `state.yaml`
`steps` block.

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/source/renders.py` |
| Modify | `src/fabulexa_forge/exporters/source/engine.py` |
| Modify | `src/fabulexa_forge/playback/shaped.py` |
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Modify | `tests/exporters/source/_source_fixtures.py` |
| Modify | `tests/config/test_source_config.py` |
| Modify | `tests/exporters/source/test_plan.py` |
| Modify | `tests/exporters/source/test_renders.py` |
| Modify | `tests/exporters/source/test_engine.py` |
| Modify | `tests/exporters/source/test_election_plan.py` |
| Modify | `tests/exporters/source/test_election_renders.py` |
| Modify | `tests/playback/_shaped_fixtures.py` |
| Modify | `tests/playback/test_shaped_open.py` |
| Modify | `tests/playback/test_shaped_state.py` |
| Modify | `tests/playback/test_shaped_window.py` |
| Modify | `tests/config/test_loader.py` |
| Modify | `tests/config/test_base_config.py` |
| Modify | `tests/integration/test_corrupt_source.py` |
| Modify | `tests/incremental/test_driver.py` |
| Modify | `tests/exporters/test_base_relations.py` |
| Modify | `tests/test_cli_export.py` |
| Modify | `tests/recipes/test_source_recipes.py` |
| Modify | `examples/recipes/source/source-junction-from-membership/config.yaml` |
| Modify | `examples/recipes/source/source-junction-from-membership/expect.yaml` |
| Modify | `examples/recipes/source/source-subtype-split/config.yaml` |
| Modify | `examples/recipes/source/source-subtype-split/expect.yaml` |
| Delete | `examples/recipes/source/source-changelog-from-history/config.yaml` |
| Delete | `examples/recipes/source/source-changelog-from-history/expect.yaml` |
| Delete | `examples/recipes/source/source-reference-from-dimension/config.yaml` |
| Delete | `examples/recipes/source/source-reference-from-dimension/expect.yaml` |
| Delete | `examples/recipes/source/source-transaction-from-fact/config.yaml` |
| Delete | `examples/recipes/source/source-transaction-from-fact/expect.yaml` |
| Delete | `examples/recipes/source/source-exclude-kind/config.yaml` |
| Delete | `examples/recipes/source/source-exclude-kind/expect.yaml` |
| Delete | `examples/recipes/source/source-rename-table/config.yaml` |
| Delete | `examples/recipes/source/source-rename-table/expect.yaml` |
| Delete | `examples/recipes/source/source-snapshot-delivery/config.yaml` |
| Delete | `examples/recipes/source/source-snapshot-delivery/expect.yaml` |
| Create | `examples/recipes/source/source-state-tables/config.yaml` |
| Create | `examples/recipes/source/source-state-tables/expect.yaml` |
| Create | `examples/recipes/source/source-event-log/config.yaml` |
| Create | `examples/recipes/source/source-event-log/expect.yaml` |
| Create | `examples/recipes/source/source-columns-rename/config.yaml` |
| Create | `examples/recipes/source/source-columns-rename/expect.yaml` |
| Create | `examples/recipes/source/source-log-only/config.yaml` |
| Create | `examples/recipes/source/source-log-only/expect.yaml` |
| Create | `docs/sprints/source-declared-tables/demos/phase_3_declared_export.py` |

**Tests** (rewrites are contract-anchored; each suite's bullets are the floor,
not the ceiling):

- Config (`test_source_config.py`): declared grammar parses (the design doc's
  § Configuration example verbatim); bare `mode: source` / `source: {}` /
  no-output declaration all load-time errors; duplicate table names in the
  declaration list rejected; `declare_keys` composes with `tables` and
  `events`; loader round-trips the new grammar.
- Plan (`test_plan.py`): one unit per declaration in order, event log last; a
  declared population materializing zero rows still yields its (empty) table;
  omission-as-exclusion — an undeclared kind exports nothing but stays a legal
  reference target; two tables sharing a population both render it; name
  collision → `SourceNameCollision` (never a silent suffix); `columns` subset
  projects with taxonomy-decided representation; identity column outside
  `columns`' reach (elected surface named → `SourceColumnNotAddressable`;
  unrendered surface → `SourceColumnUnresolved` naming the election);
  `rename` keyed on source names, identity rename keyed on the elected
  surface's contract name; mechanism columns unaddressable; non-exempt
  `slice_only` named → `SourceSliceOnlyRead`, auto-projection omits with
  notice; discriminator retained at ≥ 2 populations, dropped at one unless
  listed; events sources overlap → `SourceEventSourceOverlap`; audited-set
  resolution (tracked + constant classes, `only` / `ignore`, membership
  element fields); `SourceUnclassifiedColumn` on an unknown records column;
  windowed plan refuses `last_mutation_sim_time` entries; reserved-name check
  holds for output tables including the log; `SourceHistoryTrackedRequired`
  unconditional; single-branch guard.
- Declared keys: PK on the identity column's output name; `presentation_id`
  UNIQUE follows `combined_claim` over the table's resolved population set
  (flat-kind, single-population, full-domain, proper-subset-excluding-collider
  cases); junction and log declare nothing.
- Election (`test_election_plan.py` / `test_election_renders.py`): uniformity
  gate per declared table; the mixed-election kind splits into per-population
  tables and exports (the trichotomy trap resolved — new test); union safety
  under uniform `presentation_id`; edge gates per referencing column; item-type
  gate over the union of sources' populations — two disjoint-sub-type sources
  electing colliding `presentation_id` refused whether declared in one source
  or two (`ElectionUnionUnsafe` naming the item-type); no gate across
  item-types; plan-time uniqueness guard refuses a corrupted elected key
  before any write.
- Renders + engine (`test_renders.py` / `test_engine.py`): state render = one
  current row per record, discriminator-filtered, `updated_at` present on full
  export; lifecycle map and wallclock rendering; reference columns in the
  target's elected surface; junction render carried (open interval `left_at`
  NULL) now honoring `columns` / `rename` (owner always present; member pair
  splits); total orders per design § Ordering; full export write_mode
  `create`; windowed: state `replace` without `updated_at`, log + junction
  `append`, junction `left_at` horizon-masked; zero-row tables still written;
  CSV + DuckDB parity; export determinism byte-stable across two runs.
- Playback (`test_shaped_*`): declared source shape opens and enumerates
  tables-then-log; deliveries state=snapshot / junction=append / log=append;
  the `last_mutation_sim_time` declaration opens but refuses at first
  `window()` ask; `state(T)` over the truncated view matches the windowed
  bridging theorem for declared tables; base_relations rewrite applied by the
  seam (`test_base_relations.py`: source event-log/state reads fully
  shadowed).
- CLI + incremental (`test_cli_export.py` / `test_driver.py`): declared config
  exports via CSV and DuckDB paths; `--next` drips to drained with the new
  window classes (state table row count can shrink or grow between snapshots;
  log only appends); config-change fingerprint mismatch still refuses
  mid-drip; anchor-required and anchor-flag tests carried.
- Recipes: corpus per the Files table; every recipe single-feature, loads,
  runs, asserts (`expect.yaml`); the snapshot-delivery harness branch in
  `test_source_recipes.py` deleted.
- Integration: corrupt→source composition surfaces a declared defect through a
  declared-table export unchanged.

### Phase 4: `init --mode source` + examples refresh

**Delivers:** `exporters/source/init.py` (`generate_source_init_config` per the
design doc's inference contract), the CLI `--mode {dimensional,source}`
selector (dimensional the shipped default), and the four `docs/examples/*/
source.yaml` presets regenerated through the new engine.

**Demo:** Runs `init --mode source` against a fixture emit, prints the
commented candidate, loads it back, and builds a plan clean (the self-gating
posture proven end-to-end).

**Contracts:** § 6; design doc § `init --mode source` inference contract +
`generate_source_init_config`.

**Steps:** none (single implementer).

**Files:**

| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/exporters/source/init.py` |
| Modify | `src/fabulexa_forge/cli.py` |
| Create | `tests/exporters/source/test_init.py` |
| Modify | `tests/test_cli_init.py` |
| Modify | `docs/examples/nhs/source.yaml` |
| Modify | `docs/examples/retail/source.yaml` |
| Modify | `docs/examples/ride-sharing/source.yaml` |
| Modify | `docs/examples/ride-sharing-marketplace/source.yaml` |
| Create | `docs/sprints/source-declared-tables/demos/phase_4_init_source.py` |

**Tests:**

- One state table per `records__<kind>`, name verbatim `<kind>`; sub-typed kind
  proposes one combined table with the sub-type enumeration + split
  alternative in comments.
- One junction per membership table, named `<K>_<p>`.
- Events stub `versions` with one active source per tracked-property kind;
  membership sources and lifecycle-only kinds appended commented-out.
- No tracked property anywhere → fully commented events stub with the
  lifecycle-only note.
- Name collision (underscore-bearing identifiers) → later proposal emitted
  commented with a collision comment; the emitted config still parses and
  plans clean.
- Registry-declared population → `keys` proposal aligned with the declared
  tables (self-gated).
- Non-exempt `slice_only` columns never proposed; one notice each.
- Emit predating `history_tracked` → `SourceHistoryTrackedRequired`; incoherent
  `presentation_keys` → `PresentationKeysInvalidError`.
- Round-trip: generated YAML → `load_export_config` → `build_source_plan`
  clean, for both a flat-kind and a sub-typed fixture emit.
- Proposal order follows sidecar table declaration order; output deterministic
  across two runs.
- CLI: `init --mode source` dispatches to the source engine; bare `init` is
  byte-identical to today's dimensional output; `--mode bogus` is an argparse
  error.

## What Doesn't Change

- **The reader and derivations layer** — no new resident; the event log
  composes `row_state_events` / `membership_events`; diff + JSON are mode-owned
  SQL. `exporters/source/columns.py` survives as-is.
- **The writers** — `changes` is plain VARCHAR; `QuerySpec` / `TableKeys`
  shapes untouched.
- **Dimensional, base, streaming behavior** — config grammars, YAML surfaces,
  outputs untouched. Dimensional keeps its `base_relations` engine parameter.
- **Election grammar, resolution, and gate definitions**
  (`resolve_election`, `check_identity_election`, `check_edge_union_safety`,
  `check_elected_key_unique`, `build_population_spine_sql`) — new callers and
  one additive helper only. `ElectedPopulation` is not refactored over
  `Population`.
- **The anchor contract** — `SourceAnchorRequired`, precedence, DST rules.
- **The `slice_only` policy** — omit-with-notice on auto-projected surfaces;
  the sub-typed-discriminator carve-out.
- **Operational presentation defaults** — prefix-stripped names,
  `record_id` → `id`, the lifecycle map, native payload types, the reserved
  `last_mutation_sim_time` output name, collision fail-fast.
- **Incremental driver mechanics** — cursor, fingerprint, drained detection,
  labels, staging, empty-window emission.
- **Corrupters, notices channel, single-branch guard, trunk-only `fork_path`
  drop, determinism and faithful-reshaping invariants.**

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/config/models.py` | P1: four decl models; P3: `SourceConfig` swap + `mode_section_matches` delta |
| `src/fabulexa_forge/errors.py` | P1: eight errors added; P3: six retired |
| `src/fabulexa_forge/exporters/populations.py` | P1: new — `Population`, `resolve_populations` |
| `src/fabulexa_forge/exporters/election.py` | P2: additive `build_identity_translation_sql` |
| `src/fabulexa_forge/exporters/source/events.py` | P2: new — event-log plan types + render |
| `src/fabulexa_forge/exporters/source/plan.py` | P3: rebuilt — declared tables → `SourcePlan` |
| `src/fabulexa_forge/exporters/source/renders.py` | P3: rebuilt — state + junction builders |
| `src/fabulexa_forge/exporters/source/engine.py` | P3: rebuilt — plan+compile split, write modes |
| `src/fabulexa_forge/exporters/source/init.py` | P4: new — source proposal engine |
| `src/fabulexa_forge/playback/shaped.py` | P3: source re-key, election threading, caller-side base_relations |
| `src/fabulexa_forge/incremental/driver.py` | P3: source call-site → plan+compile |
| `src/fabulexa_forge/cli.py` | P4: `init --mode` selector |
| `tests/…` (26 files per phase tables) | P1-P4: new suites + estate migration |
| `examples/recipes/source/…` | P3: corpus rebuilt (6 deleted, 2 migrated, 4 new) |
| `docs/examples/*/source.yaml` | P4: regenerated |
