# Sprint: list-valued-predicates

## Purpose

Every equality predicate in the dimensional export grammar accepts a scalar or a
non-empty list — a scalar compiles to `=`, a list to `IN` — so an author can
project a records kind whose rows span several domain processes into one table
per process (e.g. an NHS warehouse's Emergency Care dataset grouping four
`prop__decision_type` values) without thirty near-identical table blocks.

Design doc: `docs/architecture/pending/list-valued-predicates.md`. It owns all
semantics and rationale; this spec owns contracts, phases, and test cases. Where
this spec says "§ <name>", it means that doc's section.

## Scope

**Capabilities touched:**
- Dimensional exporter: list-valued `source.filter` / `source.where` /
  `source.value` / `fk.where` / `derived.elapsed.other_where`; per-element
  unobserved-value notice; dim source population subset selection
- Reader: relation builders widened to scalar-or-list predicates
- Derivations: membership-edge + versioned-intervals predicates widened
- Shared SQL utilities: the one predicate-rendering authority; the two private
  typed-literal forks deleted
- Config envelope: `PredicateValue` type + parse-time well-formedness

**Not included** (per the design doc's deferrals): row predicates on base/source
modes, the corrupter selector grammar, new operators (`not_in`, ranges,
null-tests), refusing the inert membership-grain `fk.where`, `init` changes,
streaming/playback/incremental changes (consumers of the widening, not surfaces).

## Breaking Changes

All internal-behavior tightenings (Principle #9); no valid scalar config changes
its output for any column type the contract's recommended mapping produces.

- **`ElapsedSpec.other_where: {}` is rejected at parse time.** Previously parsed
  and rendered a malformed subquery (`WHERE ` with no condition) that failed at
  execution. Now a `ValueError` at config load.
- **Reader-composed predicate on an unrecognized column type (BLOB,
  producer-custom array/struct) raises `ExportError`** instead of silently
  rendering a VARCHAR literal that matches nothing. Deliberate end of the reader
  fork's fallback (§ Consolidating the literal renderers).
- **Parameterized type strings passing the forks' unanchored prefix test but
  failing the shared anchored grammar are refused** on the reader and
  derivations surfaces (they already are on the shared renderer).

## Success Criteria

- [ ] A records `filter` with `prop__decision_type: [a, b, c]` exports one table
      containing all three values' rows (the motivating case)
- [ ] All five predicate surfaces accept a scalar or a non-empty list; scalar SQL
      is byte-identical to today for VARCHAR / BIGINT / DOUBLE / BOOLEAN /
      DECIMAL(p,s) columns
- [ ] Empty list, duplicate-bearing list, and empty `other_where` are rejected at
      parse time with the field's path in the error
- [ ] Exactly one rendering authority: no module outside `_sql.py` renders `=` or
      `IN` over a config predicate value; both `_render_typed_literal` forks are
      gone
- [ ] A list on a sub-typed dim's discriminator selects exactly those populations;
      an out-of-set owner's FK resolves NULL; a full-domain list composes no
      restriction
- [ ] The unobserved-value notice follows the § The unobserved-value notice
      matrix (per-element, weaker wording only for a partially-observed list)

## Contracts

Full signatures + docstrings live in the design doc § Interface Contracts; they
are the contract verbatim. Summary of what each phase implements:

**Phase 1** (§ Functions):
- `render_predicate_condition(column: str, value: str | list[str], sql_type: str, alias: str | None) -> str`
  — new public authority in `src/fabulexa_forge/_sql.py`. Scalar → `= <lit>`;
  list → `IN (<lit>, …)` in element order; discriminates on
  `isinstance(value, str)`; each element typed by `render_typed_literal`
  (raises `ExportError` on unrecognized type — no fallback); column quoted via
  `quote_identifier`; alias `None` → unqualified, else the condition qualifies
  the column as `<alias>."<column>"` with the alias spliced verbatim (engine-
  internal), so existing alias-qualified SQL text stays byte-identical.
- `build_records_relation_sql(…, discriminator_filter: Mapping[str, str | list[str]])`
  — widened; delegates each condition to the authority; gains `ExportError`.
- `build_history_relation_sql(…, value_filter: str | list[str] | None)`
  — widened; routes through the authority with `sql_type="VARCHAR"` (the raw-
  literal behavior is the caller's type choice, not a renderer mode).
- `build_membership_relation_sql(…, where_predicate: Mapping[str, str | list[str]])`
  — widened; delegates; gains `ExportError`.
- `build_versioned_intervals_sql(…, discriminator_filter: Mapping[str, str | list[str]])`
  — widened pass-through; renders nothing itself.
- `build_membership_edge_sql(…, where_predicate: Mapping[str, str | list[str]])`
  — widened; delegates to the authority.
- Both private `_render_typed_literal` forks (`reader/relations.py`,
  `derivations/reference_resolution.py`) deleted. `fk.py`'s point-in-time
  membership FK and `columns.py`'s `build_elapsed_expr` stop rendering their own
  conditions and call the authority (fk with `alias="h"`, elapsed with
  `alias=None`).

**Phase 2** (§ Config Models):
- `_reject_malformed_predicate(value: str | list[str]) -> str | list[str]` and
  `PredicateValue: TypeAlias = Annotated[str | list[str], AfterValidator(...)]`
  in `config/models.py` — non-empty, duplicate-free (message names the repeated
  element); the rule rides the type, so it applies per-entry inside
  `dict[str, PredicateValue]` and reports at the offending field's path.
- `SourceDecl.filter` / `SourceDecl.where` → `dict[str, PredicateValue] | None`;
  `SourceDecl.value` → `PredicateValue | None`;
  `FkClause.where` → `dict[str, PredicateValue] | None`;
  `ElapsedSpec.other_where` → `dict[str, PredicateValue]` + new
  `other_where_non_empty` model validator.
- `check_discriminator_value_observed` becomes per-element per the
  § The unobserved-value notice matrix: unobserved set computed before any
  notice; notices in config element order; `… table will be empty` verbatim for
  a scalar or wholly-unobserved list; `… it contributes no rows` per element for
  a partially-observed list; notice code unchanged.

**Phase 3** (§ Functions):
- `resolve_dim_source_populations` — signature unchanged
  (`Mapping[str, object] | None`); resolution widens: the discriminator
  conjunct's value set (scalar's singleton, or list elements in config order)
  selects exactly those populations; per-element domain gate naming the
  offending element; `proper_subset` is strict set inclusion (full-domain list
  in any order = no restriction). Consumers unchanged.

No default parameters anywhere; no scaffolding; contracts carry no
implementation code.

## Phases

### Phase 1: Rendering authority + renderer consolidation

**Delivers:** The one predicate-rendering authority in `_sql.py`; both private
typed-literal forks deleted; the reader's three builders and the derivations
layer's two builders widened to scalar-or-list and routed through it; the
dimensional point-in-time membership FK and elapsed correlation stop rendering
their own conditions. Render paths accept lists end-to-end; configs cannot yet
produce them (Phase 2).

**Demo:** `demos/phase_1_rendering_authority.py` — pure-function demo, no emit:
prints the rendering matrix (scalar `=` / one-element and multi-element `IN`
across VARCHAR / BIGINT / BOOLEAN / DECIMAL), the alias-qualified form, and the
two refusals (unrecognized type; prefix-passing-but-unanchored parameterized
type), each caught and shown.

**Contracts:** `render_predicate_condition`; widened
`build_records_relation_sql` / `build_history_relation_sql` /
`build_membership_relation_sql` / `build_versioned_intervals_sql` /
`build_membership_edge_sql`.

**Steps:** `source → author (3 files)`. Atomic: deleting the derivations fork
breaks `fk.py`'s import of it, and deleting either fork turns its unit tests in
`tests/derivations/test_reference_resolution.py` red — the source reshape and
the test rewrite must land in one gated phase.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/_sql.py` |
| Modify | `src/fabulexa_forge/reader/relations.py` |
| Modify | `src/fabulexa_forge/derivations/reference_resolution.py` |
| Modify | `src/fabulexa_forge/derivations/versioned_intervals.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/fk.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/columns.py` |
| Create | `tests/test_sql.py` |
| Modify | `tests/derivations/test_reference_resolution.py` |
| Modify | `tests/reader/test_relations.py` |
| Create | `docs/sprints/list-valued-predicates/demos/phase_1_rendering_authority.py` |

**Tests:**
- `tests/test_sql.py` (new): scalar renders `"col" = <lit>` byte-identical to
  `render_typed_literal` composition for each recommended-mapping type; list
  renders `IN` preserving element order; one-element list renders `IN`, not `=`;
  alias qualifies as `h."col"`; VARCHAR renders raw single-quoted literal
  (history surface's behavior); unrecognized type raises `ExportError`; a `str`
  value never takes the list branch (isinstance discrimination)
- `tests/derivations/test_reference_resolution.py`: the six `_render_typed_literal`
  fork unit tests (lines ~121–186) rewritten — behavior now covered by the shared
  renderer's own tests, so keep only what asserts *this module's* composition of
  it; `build_membership_edge_sql` with a list-valued `where_predicate` renders
  `IN`; scalar edge SQL unchanged (existing tests pass as-is); prefix-passing
  parameterized type now refused
- `tests/reader/test_relations.py`: records relation with a list filter renders
  `IN` typed per sidecar column; membership relation likewise; history relation
  with a list `value_filter` renders `IN` over raw literals; scalar SQL
  byte-identical (existing tests pass unmodified); predicate on an unrecognized
  column type raises `ExportError` (was VARCHAR fallback)
- Existing `tests/exporters/dimensional/test_fk.py` and `test_elapsed.py` pass
  unmodified (scalar SQL text preserved, including the `h."col"` qualification)

### Phase 2: Config envelope + dimensional plumbing + per-element notice

**Delivers:** `PredicateValue` on all five config surfaces with the parse-time
well-formedness rule; `other_where` non-empty; lists flow from YAML through the
records / history-point / membership grains, SCD-2 source filter, membership FK
`where`, and elapsed `other_where`; the unobserved-value notice is per-element.
The motivating multi-process fact table exports end-to-end.

**Demo:** `demos/phase_2_list_predicates_export.py` — synthesizes a minimal emit
(DuckDB + stdlib, mirroring `tests/_support/sidecar_builder.write_emit`; may
`sys.path`-inject the repo root to reuse it), embeds a YAML config with a
list-valued records `filter` grouping several discriminator values into one
fact table plus a scalar-filtered dim, runs the dimensional export, prints the
resulting tables; then shows the three parse-time rejections (empty list,
duplicate element, empty `other_where`) with their error messages.

**Contracts:** `PredicateValue`, `_reject_malformed_predicate`, widened
`SourceDecl` / `FkClause` / `ElapsedSpec`, per-element
`check_discriminator_value_observed`.

**Steps:** `source → author (3 files) → author (5 files)`. Not atomic (the
widening is additive) but the new-test surface spans eight files — too much
accumulated context for one window.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/grains.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/scd.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/validation.py` |
| Modify | `tests/config/test_models.py` |
| Modify | `tests/exporters/dimensional/test_validation.py` |
| Modify | `tests/exporters/test_notices.py` |
| Modify | `tests/exporters/dimensional/test_grains.py` |
| Modify | `tests/exporters/dimensional/test_elapsed.py` |
| Modify | `tests/exporters/dimensional/test_fk.py` |
| Modify | `tests/exporters/dimensional/test_scd2_source_filter.py` |
| Modify | `tests/exporters/dimensional/test_export_dimensional.py` |
| Create | `docs/sprints/list-valued-predicates/demos/phase_2_list_predicates_export.py` |

`grains.py` / `scd.py` changes are pass-through typing only (e.g.
`grains.py:176`'s `dict[str, str]` annotation) — the builders they compose were
widened in Phase 1. If mypy surfaces annotation fallout elsewhere from the
widened field types, fixing the annotation is in scope; new behavior is not.

**Tests:**
- `tests/config/test_models.py`: list accepted on all five surfaces; scalar
  still accepted; empty list rejected with the field's path; duplicate element
  rejected naming the element; the rule applies per-entry inside a mapping
  (one bad entry among good ones reports that entry's path); `other_where: {}`
  rejected; `other_where` with one entry accepted; existing grain-gate tests
  unchanged (`filter` records-only etc.)
- `tests/exporters/dimensional/test_validation.py` + `tests/exporters/test_notices.py`:
  the five-row notice matrix from § The unobserved-value notice — scalar
  observed → none; scalar unobserved → one, `table will be empty` verbatim;
  wholly-unobserved list → one per element, `table will be empty`; partially
  observed list → one per unobserved element, `it contributes no rows`, in
  config element order; column absent from `enum_domains` → none for any form
- `tests/exporters/dimensional/test_grains.py`: records grain with list filter
  renders `IN` inside the composed reader relation; membership grain with list
  `where`; history-point grain with list `value`
- `tests/exporters/dimensional/test_elapsed.py`: list-valued `other_where`
  widens the counterpart set; earliest-start-wins unchanged (existing
  duplicate-arrivals test still passes)
- `tests/exporters/dimensional/test_fk.py`: membership FK with list `where`
  narrows to the listed intervals; point-in-time FK with list `where`
- `tests/exporters/dimensional/test_scd2_source_filter.py`: `scd: type2` dim
  with a list filter — the versioned-intervals semi-join restricts to the
  matching records
- `tests/exporters/dimensional/test_export_dimensional.py`: end-to-end — one
  fact table from a three-value list filter contains exactly the three values'
  rows; a scalar-filtered sibling table byte-identical to before

### Phase 3: Dim source population subset

**Delivers:** A list on a sub-typed dim's discriminator conjunct selects exactly
those populations as the dim's source population set — per-element domain gate,
strict-subset restriction rule, selection order = config element order. FK
closure holds over the subset; all five election consumers work over it
unchanged.

**Demo:** `demos/phase_3_dim_population_subset.py` — synthesizes an emit with a
sub-typed kind (e.g. `staff` with consultant/registrar/nurse/porter), exports a
dim filtered to a three-element subset plus a fact with an FK to it: shows the
dim containing only the subset's rows, an out-of-subset owner's FK resolving
NULL (closure, no dangling reference), and a full-domain list composing no
restriction; then shows the per-element domain-gate refusal naming an
out-of-domain element.

**Contracts:** widened resolution rule of `resolve_dim_source_populations`
(signature unchanged).

**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/dimensional/populations.py` |
| Modify | `tests/exporters/dimensional/test_populations.py` |
| Modify | `tests/exporters/dimensional/test_election_fk.py` |
| Modify | `tests/exporters/dimensional/test_fk.py` |
| Create | `docs/sprints/list-valued-predicates/demos/phase_3_dim_population_subset.py` |

**Tests:**
- `tests/exporters/dimensional/test_populations.py`: list selects exactly its
  elements, in config order, `proper_subset=True`; one-element list ≡ scalar's
  population set; full-domain list (any order) → `proper_subset=False`; list
  with an out-of-domain element raises naming that element; scalar out-of-domain
  raises as today; list on a non-sub-typed kind is an ordinary conjunct (flat
  set); existing scalar tests pass unmodified
- `tests/exporters/dimensional/test_election_fk.py`: a subset whose populations
  elect one surface inherits it; a subset electing differing surfaces raises the
  existing `ElectionInheritanceAmbiguous` (unchanged message)
- `tests/exporters/dimensional/test_fk.py`: fact FK to a subset-filtered dim —
  in-set owner resolves; out-of-set owner resolves NULL

## What Doesn't Change

Mirrors the design doc § What Doesn't Change; the implementer must not touch:

- Source mode's `sub_types` / streaming's `types` — separate fields with the
  unknowable-past exemption; their own `IN` rendering stays where it is
- Base and source modes gain no row predicate; the state-at derivations'
  signatures are untouched
- The operator set (equality + `IN` only), AND-join composition, and the grain
  gate (`filter` records-only, `where` membership-only, `value`
  history-point-only)
- `init` — continues proposing scalar predicates only
- The corrupter selector grammar
- The inert membership-grain `fk.where` (list ignored exactly as a scalar is)
- Engine-internal scoping conditions (`fork_path`, `kind`, `property` raw
  literals), sub-type population spines, and semi-join `IN (SELECT …)`
  constructs — not config row predicates, keep their existing rendering
- The unknowable-past gate (`check_slice_only_filter_keys` and the elapsed
  sibling check) — per-column, value form irrelevant; surface list unchanged
- Incremental's one filter-reading rule (gates the discriminator column's
  mutability, never the value); streaming, writers, mixer, playback
- Row order, key columns, table emission; a zero-match grain still emits an
  empty typed table

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/_sql.py` | Add `render_predicate_condition` (the one authority) |
| `src/fabulexa_forge/reader/relations.py` | Delete fork; widen + route 3 builders |
| `src/fabulexa_forge/derivations/reference_resolution.py` | Delete fork; widen + route membership edge |
| `src/fabulexa_forge/derivations/versioned_intervals.py` | Widen pass-through predicate param |
| `src/fabulexa_forge/exporters/dimensional/fk.py` | Point-in-time FK `where` via authority (alias `h`); drop fork import |
| `src/fabulexa_forge/exporters/dimensional/columns.py` | Elapsed `other_where` via authority |
| `src/fabulexa_forge/config/models.py` | `PredicateValue` + 5 field widenings + `other_where` non-empty |
| `src/fabulexa_forge/exporters/dimensional/grains.py` | Pass-through typing for widened predicates |
| `src/fabulexa_forge/exporters/dimensional/scd.py` | Pass-through typing (SCD-2 source filter) |
| `src/fabulexa_forge/exporters/dimensional/validation.py` | Per-element unobserved-value notice |
| `src/fabulexa_forge/exporters/dimensional/populations.py` | Value-set population selection, per-element gate |
| `tests/test_sql.py` | New — authority rendering matrix + refusals |
| `tests/reader/test_relations.py` | List rendering + refusal tests |
| `tests/derivations/test_reference_resolution.py` | Fork tests rewritten; list edge tests |
| `tests/config/test_models.py` | `PredicateValue` matrix, `other_where` non-empty |
| `tests/exporters/dimensional/test_validation.py` | Notice matrix per-element |
| `tests/exporters/test_notices.py` | Notice matrix (sink-level) |
| `tests/exporters/dimensional/test_grains.py` | List through 3 grains |
| `tests/exporters/dimensional/test_elapsed.py` | List `other_where` |
| `tests/exporters/dimensional/test_fk.py` | List FK `where`; subset-dim closure |
| `tests/exporters/dimensional/test_scd2_source_filter.py` | List filter into type2 |
| `tests/exporters/dimensional/test_export_dimensional.py` | Motivating end-to-end |
| `tests/exporters/dimensional/test_populations.py` | Subset selection + domain gate |
| `tests/exporters/dimensional/test_election_fk.py` | Subset inheritance / ambiguity |
| `docs/sprints/list-valued-predicates/demos/phase_1_rendering_authority.py` | Demo 1 |
| `docs/sprints/list-valued-predicates/demos/phase_2_list_predicates_export.py` | Demo 2 |
| `docs/sprints/list-valued-predicates/demos/phase_3_dim_population_subset.py` | Demo 3 |
