# Key-Election Sprint — Implementation-Planning Contracts

Companion to `docs/architecture/pending/key-election.md`. That doc owns the config
models, `Election` / `resolve_election` / `check_identity_election` /
`check_edge_union_safety`, the presentation-key derivation, and the error taxonomy —
nothing here redesigns them. This file contracts what the doc deliberately left to
implementation planning: the uniqueness guard, the dim source population set helper,
the FK identity-relation restriction, and the threading of `Election` through the
existing plan/spec/render surfaces.

Module placement decided here:

| Module | Contents |
|---|---|
| `src/fabulexa_forge/exporters/election.py` (new) | Everything the doc calls "shared exporter layer": `Election`, `ElectedPopulation`, `resolve_election`, `check_identity_election`, `check_edge_union_safety` (doc-contracted), **plus** `check_elected_key_unique` and `build_population_spine_sql` (contracted below). Mode-neutral: imports the reader (`Emit`, `Sidecar`, `reader.relations`, `KeySpace`/`union_safe`), `config.models` (`KeySurface`), `errors`, and `derivations.presentation_key` / `derivations.record_index` (for the horizon-dispatch helpers `_record_index_sql`/`_presentation_key_sql`); never `exporters.dimensional.*` / `exporters.source.*` / `exporters.base.*` — every mode engine imports it, mirroring `exporters/query_spec.py`'s layering. |
| `src/fabulexa_forge/exporters/dimensional/populations.py` (new) | `DimSourcePopulations`, `resolve_dim_source_populations`, `resolve_fk_surface` (contracted below). All four consumers — FK inheritance resolution, the dimensional edge gates, the FK relation restriction, the guard's dim-side leg — are dimensional (`validation.py`, `fk.py`, `engine.py`); `SourceDecl`'s filter grammar is dimensional config knowledge, and the shared election module must stay importable by source/base without dragging the dimensional grammar in. Importable by `fk.py`, `validation.py`, and `engine.py` with no cycle (it imports only `config.models`, the reader, `exporters.election`, and `errors`). |
| `src/fabulexa_forge/derivations/presentation_key.py` (new) | `build_presentation_key_at_sql` / `build_presentation_key_at_end_sql` + `PRESENTATION_KEY_COLUMNS` — doc-contracted; placed as the exact sibling of `derivations/record_index.py`. Listed for completeness only. |
| `fabulexa_forge/errors.py` | The doc's eight `Election*` error classes join the existing `ExportError` family (the `SourceAnchorRequired` pattern). |

---

## 1. The elected-key uniqueness guard

One function, in `exporters/election.py`. It is the design's single data-touching
check, so it lives where the open `Emit` lives — the engine layer calls it; plan
and validation stay sidecar-only (doc invariant: "gates precede data").

```python
def check_elected_key_unique(
    emit: "Emit",
    relation_sql: str,
    surface: Literal["record_index", "presentation_id"],
    population_spine_sql: str | None,
    context_label: str,
) -> None:
    """Assert one composed identity relation is a bijection on its consumed set.

    The render-time uniqueness guard (doc § The elected-key uniqueness guard;
    business rule ElectedKeyUnique). Executes exactly one aggregate query over
    the emit:

        SELECT COUNT(*),
               COUNT(DISTINCT "record_id"),
               COUNT(DISTINCT "<surface>"),
               COUNT(*) FILTER (WHERE "<surface>" IS NULL)
        FROM (<relation_sql>) AS "_rel"
        [WHERE "_rel"."record_id" IN (<population_spine_sql>)]

    and passes iff the first three counts are equal and the fourth is zero.
    The check ranges over the join relation, never output rows. Deterministic:
    no sampling, no thresholds; a pure function of (emit, arguments).

    The elected column name is not a separate parameter: both identity
    relations project exactly (record_id, <surface>) under the surface's
    contract column name (RECORD_INDEX_COLUMNS / PRESENTATION_KEY_COLUMNS), so
    `surface` names the counted column AND the surface reported in the error —
    one value, no drift. `record_id` needs no guard call (verbatim structural
    identity; the doc scopes the guard to non-record_id elections), hence the
    two-member Literal, not KeySurface.

    Args:
        emit: The open emit (the engine's own handle; the guard reads through
            `emit.query` — one row of four counts, no Arrow surface needed).
        relation_sql: The composed identity relation, verbatim — the exact
            SELECT the consuming render joins (the record-index or
            presentation-key derivation entry point at the table's horizon,
            or its end-of-tape entry point for horizonless tables). Callers
            pass the same string they embed in the render SQL; the guard
            never re-derives a relation.
        surface: The elected surface — names the counted column on the
            relation and appears in the error.
        population_spine_sql: A complete SELECT producing a single
            `record_id` column (from `build_population_spine_sql`) that
            enumerates the consuming population set, composed as a semi-join
            restriction; None when the consumer draws from the kind's full
            domain (the doc: "the full domain needs none"). Never an
            interpolated predicate fragment — a whole relation or nothing.
        context_label: The table or edge identity for the error, rendered by
            the caller (e.g. "orders.id", "fact_ride.driver_id",
            "dim_driver (dim-side leg)", suffixed with the window label under
            an incremental invocation). Free text; the guard never parses it.

    Returns:
        None.

    Raises:
        ElectedKeyDuplicate: The three-way equality fails or an elected value
            is NULL inside the consumed set; the message names
            `context_label`, `surface`, and the four counts (rows, distinct
            record_id, distinct elected value, NULL count) so a corrupted
            emit is diagnosable without re-running.
        RunDatabaseError: The aggregate fails to execute (propagated from
            `emit.query`).
    """
```

### Call sites — engine spec-build time, per composed relation, per window

The guard is called from the three `build_*_query_specs` functions (never from
plans, renders, validation, or writers), immediately after the render SQL that
embeds a given relation is composed:

| Engine | Calls |
|---|---|
| `exporters/source/engine.py: build_source_query_specs` | Per output table spec: one call for the identity relation when the table's own population(s) elect non-`record_id` (spine = the split unit's sub_type when the table is a proper-subset population, else None); one call per referencing column's composed relation, per admitted target population subset electing that relation's surface (spine iff that subset ⊊ the target kind's domain) — reference `prop__` columns, the junction owner relation, each junction member kind's relation. |
| `exporters/base/engine.py: build_base_query_specs` | Per table spec: the self relation when the kind elects non-`record_id` (base never splits: spine = None); per `ReferenceKey` edge, per admitted target population subset electing that relation's surface. Includes edges into `exclude`d kinds — the relation is composed for the edge whether or not the target's own table ships. |
| `exporters/dimensional/engine.py: build_query_specs` | (a) per FK column whose resolved surface is non-`record_id`: the FK's composed identity relation, spine = the destination dim's source population set when `proper_subset` (via `resolve_dim_source_populations`), else None; (b) the dim-side leg: per dim that is the destination of ≥ 1 edge whose resolved non-`record_id` surface the dim's declared `key` also projects, one call over that surface's relation for the dim's source kind at the dimensional horizon (slice-state / end-of-tape), spine per the dim's population set. |

Justification against the doc's "per composed relation, per window, never output
rows":

- **Per composed relation** — relations are composed exactly here (`_render_sql_for_spec`
  / `build_base_render_sql` / `build_fk_expr` run inside these functions), so the
  engine is the only layer that can hand the guard the verbatim relation SQL the
  render embeds. Anywhere later (writers) sees only flattened output SQL — checking
  there would range over output rows, which the doc forbids (a change-log
  legitimately repeats identity per event).
- **Per window** — the incremental driver invokes each mode's `build_*_query_specs`
  once per window with that window's `Window`, and the relations are horizon-bound
  inside the same call; a guard call at spec-build time is therefore inherently
  per-window with zero extra plumbing.
- **Before any output exists** — `build_*_query_specs` returns before
  `write_query_specs` / the windowed writer executes anything, so a violation fails
  the export with nothing written for the failing invocation.
- Engines already hold the open `emit` (verified: all three take `emit`), so the
  guard adds no new handle threading.

Determinism note (permitted optimization, not a requirement): within one
invocation an engine MAY skip an exactly-repeated `(relation_sql,
population_spine_sql, surface)` triple — the guard is a pure function of those
strings plus the emit, so deduplication cannot change the pass/fail outcome, only
avoid re-running an identical aggregate.

---

## 2. Population spine (shared) — the restriction relation

In `exporters/election.py`. Shared because three consumers span modes: the guard's
restriction legs in all three engines, and dimensional's FK identity-relation
restriction (§ 4).

```python
def build_population_spine_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    sub_types: Sequence[str],
) -> str:
    """A proper-subset population set's record_id spine, for semi-join use.

    Composes the reader's faithful records relation
    (`build_records_relation_sql` — reader-first, Principle #10; never a raw
    table name) and projects `record_id` filtered to the records-spine
    discriminator:

        SELECT "record_id" FROM (<records relation for kind>) AS "_spine"
        WHERE "_spine"."prop__<kind>_type" IN ('<v1>', ...)

    The discriminator is read from the records spine, never a fold
    after-image (doc § Per-row population resolution): a row's discriminator
    is a per-record constant, so the spine is temporally honest at any
    horizon — one spine serves every horizon a render composes, which is why
    the function takes no horizon parameter. Values render as SQL string
    literals with embedded single quotes doubled; `sub_types` order is
    preserved verbatim (callers pass declaration order, keeping composed SQL
    deterministic).

    Callers pass proper subsets only: the full domain needs no restriction
    (the doc's rule), and an empty set restricts to nothing — both are caller
    logic errors, refused rather than silently composed (Principle #7).

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        kind: A sub-typed records kind (`sidecar.subtype_values(kind)`
            non-empty).
        sub_types: The population set's discriminator values — a non-empty
            proper subset of the kind's declared domain, in declaration
            order.

    Returns:
        A complete SELECT producing a single `record_id` column.

    Raises:
        ExportError: `sub_types` is empty, equals the kind's full declared
            domain, contains a value outside it, or `kind` is not sub-typed
            (`subtype_values` returns `()`).
        TableNotFoundError: `records__<kind>` is absent (propagated from the
            reader relation).
    """
```

---

## 3. Dim source population set helper (dimensional)

In `exporters/dimensional/populations.py`.

```python
@dataclass(frozen=True)
class DimSourcePopulations:
    """The destination dim's source population set, resolved from its
    SourceDecl per doc § Rendering per mode (Dimensional).

    `populations` matches `Election.surface_for`'s sub_type argument shape:
    `(None,)` for a flat kind's whole-table population; the selected
    sub-type singleton when the dim's filter carries a discriminator
    conjunct; the kind's full declared domain otherwise (declaration
    order). `proper_subset` is True iff `populations` is a strict subset of
    the kind's declared domain — the one fact that decides whether a
    restriction spine is composed at all (relation restriction and guard
    legs both key on it), computed once here so no consumer re-derives it.
    """

    kind: str
    populations: tuple[str | None, ...]
    proper_subset: bool
```

```python
def resolve_dim_source_populations(
    sidecar: "Sidecar",
    source_kind: str,
    source_filter: "Mapping[str, object] | None",
) -> DimSourcePopulations:
    """Resolve a dim's source population set from its kind + filter.

    Implements the doc's set rule verbatim: the filter grammar is an
    equality conjunction over records columns, so at most one conjunct can
    address the synthesized discriminator `prop__<source_kind>_type`; when
    present it selects exactly that sub-type's population (further
    conjuncts narrow rows within it, never the set); absent, the set is the
    kind's whole population set — the full declared domain for a sub-typed
    kind, the `(None,)` whole-table population for a flat kind. A
    `prop__<kind>_type` conjunct on a kind whose `subtype_values` is empty
    is an ordinary column conjunct (no declared domain means no
    populations to select among) and yields the flat-kind set.

    Pure function of (sidecar, arguments); consulted by FK inheritance
    resolution (`resolve_fk_surface`), the dimensional edge gates
    (`check_edge_union_safety` callers), the FK identity-relation
    restriction, the guard's dim-side leg, and the dim-key agreement check
    — one resolution, five consumers, zero re-derivation.

    Args:
        sidecar: The open emit's sidecar.
        source_kind: The destination dim's `source.kind`.
        source_filter: The dim's `source.filter` mapping, verbatim from the
            TableDecl; None when the dim declares none.

    Returns:
        The resolved population set.

    Raises:
        ExportError: The discriminator conjunct's value is not a string in
            the kind's declared domain — the dim's scope selects a
            population that cannot exist, which on any election-consuming
            path must fail loudly rather than resolve to an empty set
            (Principle #7). (Reachable only when the kind is sub-typed.)
    """
```

```python
def resolve_fk_surface(
    election: "Election",
    dim_populations: DimSourcePopulations,
    target_key: "KeySurface | None",
    edge_name: str,
) -> "KeySurface":
    """Resolve one FK edge's single rendered surface.

    The doc's inheritance rule as one pure function so `validate_table`
    (gating) and the render path (`build_fk_expr` callers) consume the
    identical answer: an explicit `target_key` wins per edge; absent, the
    edge inherits the population set's one distinct election
    (`election.surface_for` over `dim_populations.populations`); a set
    carrying more than one distinct election has nothing coherent to
    inherit. Resolution-time only — the author's config value is never
    rewritten. Gating of the resolved surface (registry declaration, union
    safety) is NOT here: callers pass the result to
    `check_edge_union_safety(..., surface_override=<result>)` per the doc's
    contract.

    Args:
        election: The resolved election.
        dim_populations: The destination dim's source population set.
        target_key: The edge's explicit override, verbatim from FkClause;
            None to inherit.
        edge_name: The referencing table · column identity, for the error.

    Returns:
        The edge's one resolved surface ('record_id' when the set carries
        no election and no override is given).

    Raises:
        ElectionInheritanceAmbiguous: `target_key` is None and the set's
            populations elect more than one distinct surface; names
            `edge_name` and the differing (population, surface) pairs.
        KeyError: A population outside the emit's declared domain
            (propagated from `Election.surface_for`; unreachable after
            `resolve_dim_source_populations`, which gates the domain).
    """
```

---

## 4. FK identity-relation restriction — SQL shape and ownership

**Decision: the spine is the contracted piece (§ 2); the wrap is private to
`fk.py`.** The restricted relation is a one-line composition:

```sql
SELECT "record_id", "<surface>" FROM (<identity_relation_sql>) AS "_ident"
WHERE "_ident"."record_id" IN (<population_spine_sql>)
```

where `<identity_relation_sql>` is the record-index or presentation-key
derivation entry point (end-of-tape for dimensional — the mode is horizonless;
shipped FK resolution is slice-state) and `<population_spine_sql>` comes from
`build_population_spine_sql`. Composed **only when
`DimSourcePopulations.proper_subset` is True**; the full domain (and the flat
kind) joins the unrestricted relation — matching the doc: "A proper-subset
restriction composes the records-spine discriminator as a semi-join; the full
domain needs none."

No second contracted helper: the wrap has exactly one producer (`fk.py`'s
builders) and its correctness is carried by the two contracted inputs. What
matters for the guard is that the engine passes the **same**
`identity_relation_sql` and the **same** spine to `check_elected_key_unique`
that `fk.py` embedded — the restriction is expressed to the guard as the spine
parameter, not baked into a divergent relation string, so the guard's WHERE
composes the identical semi-join the render composes.

The four FK builders (`build_reference_fk_expr`,
`build_point_in_time_membership_fk_expr`,
`build_membership_fk_expr_on_membership`, `build_membership_fk_expr_on_records`)
each replace their local `target_key == "presentation_id"` arm with one shared
private dispatch on the resolved surface: `record_id` → today's projection;
`record_index` / `presentation_id` → LEFT JOIN the (possibly restricted)
identity relation on the resolved target `record_id` and project the surface
column. The out-of-set → NULL posture falls out of the LEFT JOIN against the
restricted relation — no CASE logic. The shipped `target_key: presentation_id`
column-presence check in `build_fk_expr` (fk.py:659–678) is deleted: subsumed
by the statically-earlier registry-membership check
(`check_edge_union_safety` under the override), per the doc.

---

## 5. Election threading — behavioral-change notes

Resolution point: each mode's `build_*_query_specs` has the full `ExportConfig`
(verified) and calls `resolve_election(emit.sidecar, config.keys)` once per
invocation, before its plan step. Dimensional's compile takes only
`DimensionalConfig`, so its `Election` arrives as a parameter (below). The
resolved `Election` then rides the existing plan/spec types wherever one
exists; only functions with no plan structure between them and the engine gain
a parameter.

| Function | Change | How election arrives |
|---|---|---|
| `build_source_plan` (`source/plan.py:1034`) | Gains `election: "Election"` parameter (no plan dataclass exists upstream of it; the engine passes the resolved view). Runs `check_identity_election` per output unit spanning > 1 population (an unsplit sub-typed reference/transaction unit; a sub-typed change-log kind — never split — over the full domain) and `check_edge_union_safety` per referencing column (each reference-annotated `prop__` column over the target kind's domain; the junction owner column over the owner kind's domain; per junction member kind). Stamps resolution onto the extended `SourceTableSpec` (below). Rename addressing: the id column's rename key becomes the elected surface's contract column name; a rename keyed on an absorbed/dropped column raises `SourceRenameSliceOnly`-posture errors per the doc. Under a `presentation_id` election the standalone `presentation_id` payload column is absorbed from `spec.columns`. New Raises: `ElectionMixedIdentity`, `ElectionUnionUnsafe` (propagated from the checks). | Parameter |
| **`SourceTableSpec`** (`source/plan.py:79`) — the type extended | Two new frozen fields: `identity_surface: KeySurface` (the table's own populations' uniform elected surface; `'record_id'` today) and `edge_surfaces: tuple[SourceEdgeSurface, ...]` — one entry per referencing source column: `(source_column: str, target_kind: str, per_population: tuple[tuple[str \| None, KeySurface], ...], rendered_type: str)`, where `rendered_type` is the mixed-column type rule's verdict (common declared type, else `VARCHAR` with digit-rendered `record_index`; junction member columns computed over the union of member kinds' resolved surfaces). Plan computes; renders read. | — |
| `build_records_render_sql` / `build_changelog_render_sql` / `build_junction_render_sql` / `build_snapshot_render_sql` (`source/renders.py`) | **Signatures unchanged.** Each reads `spec.identity_surface` / `spec.edge_surfaces` and, for a non-`record_id` surface, composes the record-index / presentation-key derivation at the table's horizon (`window.end_ns` when windowed; end-of-tape entry points otherwise — the change-log's post-fold join per the doc, populated on `d` rows) and LEFT JOINs it, mirroring base's `_key_join_clauses` pattern. Mixed edge columns render per row via a discriminator-keyed CASE over the joined relations, typed per `rendered_type`. `spec` with all-default surfaces composes byte-identical SQL to today. | Via `SourceTableSpec` |
| `resolve_source_table_keys` (`source/plan.py`) | **Signature unchanged** (`sidecar, spec, change_delivery`). Reads `spec.identity_surface`: the declared PK follows the elected identity column; a `UNIQUE` whose column the election absorbed or dropped is not declared; an elected `presentation_id` identity column is PK-eligible (guard-established), superseding the always-`UNIQUE` posture for that column alone. No-election resolution table unchanged; genre eligibility unchanged. | Via `SourceTableSpec` |
| `build_base_plan` (`base/plan.py:701`) | Gains `election: "Election"` parameter. Base never splits: runs `check_identity_election` per sub-typed surviving kind over its full domain; `check_edge_union_safety` per `ReferenceKey` edge over the target kind's full domain (including edges whose target kind is excluded from output — exclusion changes emitted tables, not the reference graph; but a target kind with no records table in the emit is skipped and renders verbatim, per the doc's kind-exists consequence). Stamps the extended `BaseTableSpec` / `ReferenceKey` (below); applies the self-column resolution table (drop id-space column under `record_index`, elected value column in the id slot under `presentation_id`, absorption) to `column_renames` keying — the self value column's rename key is the elected surface's contract column name. New Raises: `ElectionMixedIdentity`, `ElectionUnionUnsafe`. `BasePlan` itself is unchanged (a tuple of specs; nothing invocation-scoped belongs on it). | Parameter |
| **`BaseTableSpec` / `ReferenceKey`** (`base/plan.py:82` / `:61`) — the types extended | `BaseTableSpec` gains `identity_surface: KeySurface`. `ReferenceKey` gains `per_population: tuple[tuple[str \| None, KeySurface], ...]`, `value_column_shipped: bool` (False when every admitted population elects `record_index` — the `prop__` value column drops as a `<p>_key` duplicate), and `rendered_type: str` (mixed-edge type rule). | — |
| `build_base_render_sql` (`base/renders.py:209`) | **Signature unchanged.** Adds a `_record_index_sql`-shaped private sibling for the presentation-key relation (same horizon-selection dispatch onto the two derivation entry points) and extends `_key_join_clauses`' pattern with the elected-surface joins; projects the self id-space slot and per-edge `prop__` columns per the spec's resolution fields (the elected edge-value condition table falls out of the LEFT JOIN). Index keys `<kind>_key` / `<p>_key` always ship, unchanged. All-default spec → byte-identical SQL. | Via `BaseTableSpec` |
| `resolve_base_table_keys` (`base/plan.py`) | **Signature unchanged.** Reads `spec.identity_surface` and applies the doc's `declare_keys` interplay row: under `record_index` the PK stays `<kind>_key` (id-space column dropped); under `presentation_id` the PK follows the elected identity column, guard-established; absorbed/dropped columns' side `UNIQUE` claims are simply not declared. | Via `BaseTableSpec` |
| `build_query_specs` (`dimensional/engine.py:37`) | Gains `election: "Election"` parameter (compile takes `DimensionalConfig`, which has no `keys` block; `export_dimensional`, the incremental driver, and tier-2 playback resolve it from `ExportConfig.keys` and pass it). Threads it to `validate_table` and the grain build path; after composing each table's SQL, issues the guard calls of § 1 row 3. | Parameter (resolved by `ExportConfig`-holding callers) |
| `validate_table` (`dimensional/validation.py:1181`) | Gains `election: "Election"` parameter. In the hardcoded-order per-FK block (after `check_fk_target_is_dim`, which is unchanged — callers now also read the returned `TableDecl.source.filter`, closing the kind-only gap): `resolve_dim_source_populations` → `resolve_fk_surface` → `check_edge_union_safety(..., surface_override=<resolved>)` → new hardcoded-order check `check_dim_key_agreement` (raises `ElectionDimKeyDisagrees` when the resolved surface is inherited, non-default, and no declared key column of the destination dim sources `from:` the elected contract column). The probe `build_fk_expr` call passes the resolved surface + population set. New Raises: `ElectionInheritanceAmbiguous`, `ElectionUnionUnsafe`, `ElectionPresentationUndeclared`, `ElectionDimKeyDisagrees`. | Parameter |
| `build_fk_expr` (`dimensional/fk.py:628`) | Gains `resolved_surface: "KeySurface"` and `dim_populations: DimSourcePopulations` parameters (resolution happens once in the caller via § 3; fk.py never touches `Election`). The four builders' local `target_key == "presentation_id"` arms are replaced by the shared surface dispatch of § 4; the fk.py:659–678 column-presence check is deleted (subsumed statically). `build_grain_sql` threads the two values from `validate_table`'s resolution — recomputed identically in the engine path via the same pure functions (both are pure of (sidecar, config), so the two computations cannot disagree). | Parameters (resolved values, not the `Election`) |
| `generate_init_config` (`dimensional/init.py:607`) | **Signature unchanged** (`emit, notice_sink`). Additionally proposes the `keys` block via the doc's natural proposal, self-gated through `resolve_election` + the dimensional plan gates (`check_edge_union_safety` over the emit's reference graph; failures degrade the implicated kinds to uniform `record_index` with a YAML comment naming the gate — degradation, never a raise; termination by construction). Aligns dim proposals: each proposed dim's key column sources `from:` the population's elected surface's contract column, subsuming the shipped `presentation_id` natural-key advisory comment where the election is `presentation_id`; FK candidates remain comments and remain `target_key`-free. No new Raises. | Internal (`resolve_election` over its own proposal) |

Not threaded (unchanged, per the doc's What Doesn't Change): `QuerySpec` /
`TableKeys` / `write_query_specs`, the writers, `build_render_sql`'s dispatch
signature, streaming, the row-state-events fold, the record-index derivation,
`Sidecar` accessors.

---

## Invariants these contracts must preserve

- **Gates precede data**: everything in `plan.py` / `validation.py` /
  `populations.py` is sidecar-only; `check_elected_key_unique` is the sole
  data-touching call and lives only in the three engines.
- **One resolution, no re-derivation**: `resolve_election` runs once per
  invocation per engine; `resolve_dim_source_populations` /
  `resolve_fk_surface` are pure, so gate-path and render-path calls agree by
  construction.
- **Absence composes to identity**: every extended spec field has a default-
  election value under which renders compose byte-identical SQL and
  `resolve_*_table_keys` return today's declarations — the one carve-out is
  dimensional's explicit `target_key: presentation_id` (restriction +
  registry gate apply regardless), per the doc.
- **Determinism**: guard SQL, spine SQL, and all stamped spec fields are pure
  functions of (sidecar, config, code); population and sub_type orders are
  declaration orders throughout.
