# Sprint: source-domain-vocabulary

## Purpose

Give source-mode authors a declared vocabulary for the mode's *value* surfaces —
`source.kind_labels` plus per-events-source `item_type` / `rename` — so the audit
log's `item_type`, its `changes` keys and `<f>_kind` values, and junction
member-kind values speak the author's domain names instead of the engine
ontology. An educator building the NHS example declares `kind_labels: {actor:
patient}` and the 55,536-row `audit_log` says `patient`, joinable to the
`patient` state table without knowing the engine ever called it `actor`.

Design authority: `docs/architecture/pending/source-domain-vocabulary.md` (all
semantics — resolution tables, distinctness rules, rendering rules, rationale).
This spec adds contracts' file placement, phases, and test cases; it does not
restate the design doc.

## Scope

**Capabilities touched:**

- source exporter: config grammar (`SourceConfig.kind_labels`,
  `SourceEventSourceDecl.item_type` / `rename`), plan resolution (item-type
  override → label → verbatim; changes-key pairs; three new business rules,
  three extended ones; union-safety gate over resolved item-types), event-log
  render (resolved item-type stamp, renamed `changes` keys, labeled `<f>_kind`
  entry values), junction render (labeled `member__<f>__kind` values)
- recipes: one new author-facing source recipe exercising all three fields

**Not included** (design doc § Boundaries / § Affected Subsystems): streaming /
base / dimensional vocabulary, `init --mode source` label proposals, value
mapping for property or discriminator values, per-table label scoping, reader /
derivations changes. Architecture-doc promotion (`pending/` → live,
`CAPABILITIES.md`, `source.md`) ships separately after sprint archival.

## Breaking Changes

Author-facing: none. All three config fields are optional and default to
today's verbatim engine names; a config without them produces byte-identical
output (test-guarded by the existing suite and recipe corpus staying green).

Internal (plan types, migrated in-sprint):

- `SourceEventSourcePlan.audited_properties` changes type from
  `tuple[str, ...]` to `tuple[tuple[str, str], ...]` (source bare name, changes
  output key), and the dataclass gains a required `kind_labels` field. Every
  hand-constructed instance (`tests/exporters/source/test_events_render.py`)
  migrates in Phase 3.
- `SourceJunctionTablePlan` gains a required `kind_labels` field. The one
  hand-constructed instance (`tests/playback/test_shaped_open.py`) migrates in
  Phase 2.

## Success Criteria

- [ ] `kind_labels: {actor: patient}` renders `patient` as the actor source's
      `item_type`, in every `<f>_kind` entry value naming `actor`, and in every
      junction `member__<f>__kind` cell naming `actor`
- [ ] A membership source's `item_type: consultant_allocation` overrides its
      `<K>.<property>` identity wholesale; a labeled owner kind yields
      `<label(K)>.<property>` absent an override
- [ ] An events-source `rename: {full_name: name}` renames the `changes` JSON
      key; a membership reference field's rename renames its `_kind` / `_id`
      pair in place
- [ ] All seven validation rules fire with the design doc's messages
      (`SourceKindLabelUnknown`, `SourceKindLabelCollision`,
      `SourceItemTypeCollision`, extended `SourceColumnUnresolved` /
      `SourceSliceOnlyRead` / `SourceNameCollision`, re-partitioned
      union-safety gate)
- [ ] A config declaring none of the new fields exports byte-identically to
      today (full suite + recipe corpus green, no migration of expectations
      beyond the two plan-type constructions)
- [ ] A labeled export over a corrupted emit surfaces a mutated kind-name cell
      verbatim (identity fall-through, never masked, never an error)
- [ ] The `source-domain-vocabulary` recipe loads, runs, and asserts through
      the existing corpus gates

## Contracts

Semantics live in the design doc § Interface Contracts; placement and deltas
below. No default parameters beyond the design doc's optional-config `None`
defaults; no implementation code here.

### Config models — `src/fabulexa_forge/config/models.py` (Phase 1)

`SourceEventSourceDecl` gains two fields and extends its existing validator;
`SourceConfig` gains one field and one new validator — exactly the design doc's
§ Config Models block:

```python
class SourceEventSourceDecl(StrictBaseModel):
    # ... existing fields (kind, sub_types, membership, only, ignore) ...

    item_type: str | None = None
    """This source's resolved item-type, verbatim, overriding the
    kind-label / contract-identity default. Optional; non-empty when
    present."""

    rename: dict[str, str] | None = None
    """Audited property (element field) bare name -> `changes` output key.
    Keys are source identities, never output keys, so a default-key
    collision is always resolvable. A membership reference field's entry
    renames its expanded `<f>_kind` / `<f>_id` pair in place."""

    @model_validator(mode="after")
    def source_shape(self) -> Self:
        """The declaration's structural shape.

        Raises:
            ValueError: (existing rules, plus) `item_type` is empty;
                `rename` is present-but-empty, has an empty key or value,
                or two keys share a target value.
        """


class SourceConfig(StrictBaseModel):
    # ... existing fields (tables, events, declare_keys) ...

    kind_labels: dict[str, str] | None = None
    """Engine kind name -> domain label, applied wherever a kind name
    renders as a value: events-source item-type defaults (including the
    owner half of a membership source's `<K>.<property>` identity),
    `<f>_kind` entries inside `changes`, and junction `member__<f>__kind`
    column values. Never applied to identity values, table names, or
    sub-type discriminator values. Absent = verbatim kind names."""

    @model_validator(mode="after")
    def kind_labels_shape(self) -> Self:
        """`kind_labels`, when present: non-empty, non-empty keys and
        values, distinct values.

        Raises:
            ValueError: The map is empty, a key or value is the empty
                string, or two kinds map to one label.
        """
```

### Errors — `src/fabulexa_forge/errors.py` (Phases 2–3)

Three new `ExportError` subclasses, docstring-only bodies in the module's
existing style; messages per the design doc § Business Rules table:

```python
class SourceKindLabelUnknown(ExportError):
    """A `source.kind_labels` key names no records kind in the sidecar
    (no `records__<kind>` table) — the sidecar-facts-gate-declarations
    posture. Message: `"kind_labels: kind '{kind}' not in this emit"`."""


class SourceKindLabelCollision(ExportError):
    """After labeling, kind -> rendered name is not injective over the
    emit's whole kind universe: a label equals another kind's label or an
    unlabeled kind's own name, so two kinds would be indistinguishable in
    a `<f>_kind` column. Message:
    `"kind_labels: label '{label}' collides with kind '{kind}'"`."""


class SourceItemTypeCollision(ExportError):
    """Two events sources resolve one item-type over two audited item
    spaces (different kinds, or a membership source sharing any source's
    item-type), or a resolved item-type equals the rendered name of
    another kind (any kind, for a membership source) — ranged over the
    emit's whole kind universe. Messages per the design doc § Business
    Rules row."""
```

### Labeling authority — `src/fabulexa_forge/exporters/source/columns.py` (Phase 2)

`columns.py` is the shared sibling of `plan.py` / `renders.py` / `events.py`
(its module docstring's charter: one definition, no cross-imports between the
leaves), so the one labeling authority lives there:

```python
def build_kind_label_expr(
    value_expr: str,
    labels: "tuple[tuple[str, str], ...]",
) -> str:
    """The label-rendered SQL expression for one kind-name-valued expression.

    A compile-time CASE over the declared (kind, label) pairs with identity
    fall-through: a value matching no pair — an unlabeled kind, or a
    corrupted emit's mutated cell — renders verbatim, and NULL stays NULL.
    Byte-identical passthrough (`value_expr` unchanged) when `labels` is
    empty, mirroring the no-join composition rule for default elections.

    The one labeling authority for both call sites: the junction render's
    projected `member__<f>__kind` column, and the event log's `<f>_kind`
    entry values (old and new halves) inside the `changes` JSON assembly.

    Args:
        value_expr: A VARCHAR-typed SQL expression carrying a kind name —
            the junction's qualified `member__<f>__kind`, or the fold's
            `<f>_kind` after-image value expression.
        labels: The resolved (kind, label) pairs, declaration order.

    Returns:
        A VARCHAR-typed SQL expression.
    """
```

### Plan types (Phases 2–3)

Per the design doc § Plan Types, verbatim:

- `SourceEventSourcePlan` (`exporters/source/events.py`, Phase 3):
  `item_type: str` docstring updated to "the RESOLVED item-type" (override →
  label → verbatim; the dereference key, union-safety gate key, and order-key
  component); `audited_properties: tuple[tuple[str, str], ...]` — (source bare
  name, changes output key) pairs, sidecar column-declaration order, output key
  equal to the bare name absent a rename, a membership reference field's pair
  expanding at render to `<key>_kind` / `<key>_id`; new
  `kind_labels: tuple[tuple[str, str], ...]` — the resolved map threaded to the
  render for `<f>_kind` entry values, identity fall-through, empty when no
  labels are declared. Existing fields unchanged.
- `SourceJunctionTablePlan` (`exporters/source/plan.py`, Phase 2): new
  `kind_labels: tuple[tuple[str, str], ...]` — the resolved map for projected
  `member__<f>__kind` column values; identity fall-through; empty when no
  labels are declared. Existing fields unchanged.

### Modified-function behavior (docstring deltas, not diffs)

- `build_source_plan` (Phase 2): resolves `SourceConfig.kind_labels` to the
  ordered pair tuple once; validates `SourceKindLabelUnknown` (every key has a
  `records__<kind>` table) and `SourceKindLabelCollision` (injectivity over
  the emit's whole kind universe — all sidecar records kinds, not just
  declared ones); threads the resolved tuple into every junction unit and (in
  Phase 3) the events plan build.
- `_build_junction_table_plan` (Phase 2): carries the resolved `kind_labels`
  onto the unit.
- `build_junction_render_sql` (Phase 2): a projected `member__<f>__kind`
  output column's value expression renders through `build_kind_label_expr`;
  every other column, the joins, ordering, and windowing are unchanged.
- `_build_event_source_plan` (Phase 3): resolves the item-type first-match
  (declared `item_type` → kind label — owner-half-labeled
  `<label(K)>.<property>` for a membership source — → verbatim, design doc §
  Item-type resolution); resolves `audited_properties` to (source, output-key)
  pairs from `rename` (design doc § `changes` key resolution); validates each
  `rename` key (extended `SourceColumnUnresolved` — including the
  narrowed-away-by-`only`/`ignore` case, extended `SourceSliceOnlyRead` for a
  non-exempt slice_only property) and the resolved key set (extended
  `SourceNameCollision`, membership pair expansion included); threads
  `kind_labels`. Folds keep receiving source bare names.
- `_build_event_log_plan` (Phase 3): after building sources, runs the
  `SourceItemTypeCollision` checks (design doc § Item-type distinctness —
  pairwise across sources, plus the rendered-kind-name clauses over the whole
  kind universe) *before* the union-safety gate; the gate then groups by
  resolved item-type exactly as it groups by item-type today.
- `_build_records_arm_sql` / `_build_membership_arm_sql` /
  `build_event_log_sql` (Phase 3): `changes` entries keyed by each pair's
  output key (membership reference pair as `<key>_kind` / `<key>_id`);
  `<f>_kind` entry old and new value expressions each render through
  `build_kind_label_expr`; the stamped `item_type` literal is the plan's
  resolved value (mechanically unchanged — the plan now carries the resolved
  string). Fold composition, lag/diff, `item_id`, ordering, `id` numbering,
  and windowing unchanged.

## Phases

### Phase 1: Config grammar

**Delivers:** The three optional config fields parse and validate;
`load_export_config` accepts them; nothing consumes them yet (plan resolution
lands in Phases 2–3).
**Demo:** Parses a full vocabulary config (kind_labels + item_type + rename)
and prints the loaded model; shows each parse-time rejection (empty item_type,
empty rename, duplicate rename targets, empty kind_labels, duplicate label
values) with its ValueError message.
**Contracts:** `SourceEventSourceDecl.item_type` / `.rename` + `source_shape`
extension; `SourceConfig.kind_labels` + `kind_labels_shape`.
**Steps:** none (single implementer)

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `tests/config/test_source_decls.py` |
| Modify | `tests/config/test_source_config.py` |
| Create | `docs/sprints/source-domain-vocabulary/demos/phase_1_config_grammar.py` |

**Tests:**
- `item_type: clinician` on a records source parses; `item_type: ""` refused
- `rename: {full_name: name}` parses; `rename: {}` refused; empty key or
  value refused; two keys sharing one target value refused (parse-time
  rename-value vs rename-value case — the design doc's § `changes` key
  resolution note)
- `item_type` and `rename` legal on both records and membership sources
- `kind_labels: {actor: patient, resource: consultant}` parses;
  `kind_labels: {}` refused; empty key or value refused; two kinds mapping to
  one label refused
- A source config declaring none of the new fields parses exactly as today
  (all three fields None)
- Existing `tests/config/test_source_decls.py` / `test_source_config.py`
  cases still pass unchanged

### Phase 2: Kind labels — validation + junction rendering

**Delivers:** `kind_labels` resolved and validated at plan time
(`SourceKindLabelUnknown` / `SourceKindLabelCollision`), threaded onto junction
units, and rendered into junction `member__<f>__kind` values through the new
`build_kind_label_expr` authority. Intermediate state, documented: labels do
not yet reach the event log (Phase 3).
**Demo:** Builds a minimal emit with a polymorphic junction, exports twice —
no labels, then `kind_labels` — and prints the junction rows showing member
kind values recoded (and the owner column, timestamps, ids untouched); shows
`SourceKindLabelUnknown` and `SourceKindLabelCollision` firing.
**Contracts:** `build_kind_label_expr`; `SourceKindLabelUnknown` /
`SourceKindLabelCollision`; `SourceJunctionTablePlan.kind_labels`;
`build_source_plan` / `_build_junction_table_plan` / `build_junction_render_sql`
deltas.
**Steps:** none (single implementer — the one plan-construction migration in
`tests/playback/test_shaped_open.py` is a single mechanical site)

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `src/fabulexa_forge/exporters/source/columns.py` |
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/source/renders.py` |
| Modify | `tests/playback/test_shaped_open.py` |
| Modify | `tests/exporters/source/test_plan.py` |
| Modify | `tests/exporters/source/test_renders.py` |
| Create | `docs/sprints/source-domain-vocabulary/demos/phase_2_junction_labels.py` |

**Tests:**
- `build_kind_label_expr`: empty labels → byte-identical passthrough
  (`value_expr` returned unchanged); one pair → CASE recoding that value,
  identity fall-through otherwise; NULL stays NULL; a label value containing a
  quote is SQL-escaped
- Plan: `kind_labels` resolves onto every junction unit in declaration order;
  absent → empty tuple
- Plan: a `kind_labels` key naming no records kind →
  `SourceKindLabelUnknown` with the design doc message
- Plan: a label equal to an *unlabeled* kind's name →
  `SourceKindLabelCollision` — including when that kind appears in no declared
  table (whole-kind-universe range)
- Render: a labeled member kind renders the label; an unlabeled kind renders
  verbatim; a NULL member-kind cell stays NULL (open-interval / non-reference
  rows unaffected)
- Render: a member-kind *value* not naming any sidecar kind renders verbatim
  (corrupted-emit fall-through)
- Render: with no `kind_labels`, junction SQL is byte-identical to today
  (no-labels no-op guard)
- Migrated: `tests/playback/test_shaped_open.py` junction-plan construction
  carries `kind_labels=()` and the module is green
- Existing `test_renders.py` / `test_plan.py` junction cases still pass

### Phase 3: Events path — resolved item-types + changes keys

**Delivers:** The event log speaks the declared vocabulary: per-source
`item_type` resolution (override → label → verbatim), `changes` keys resolved
through `rename`, `<f>_kind` entry values labeled, the distinctness rules
(`SourceItemTypeCollision`) and extended rename rules enforced, the
union-safety gate re-partitioned over resolved item-types.
**Demo:** The design doc's NHS shape end-to-end: one emit, one config with
`kind_labels: {actor: patient}`, a records-source `rename`, and a membership
`item_type` override; prints audit-log rows showing `item_type = patient`, the
renamed `changes` key, the labeled `<f>_kind` halves, and the overridden
membership item-type; prints the same export unlabeled for contrast, and notes
the `id` renumbering consequence (design doc § Ordering consequence).
**Contracts:** `SourceItemTypeCollision`; `SourceEventSourcePlan` reshape;
`_build_event_source_plan` / `_build_event_log_plan` / events-render deltas.
**Steps:** `source` → `author` (`test_events_render.py`) → `author`
(`test_plan.py`) — mirrors the `state.yaml` block. The source step leaves the
suite red (plan-type reshape breaks hand-constructed plans); the two author
steps each migrate their file's existing tests to the new shape *and* author
that file's new cases; the phase gate runs once after all three.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/source/events.py` |
| Modify | `tests/exporters/source/test_events_render.py` |
| Modify | `tests/exporters/source/test_plan.py` |
| Create | `docs/sprints/source-domain-vocabulary/demos/phase_3_events_vocabulary.py` |

**Tests** (`test_plan.py` — resolution + rules; the design doc's tables are
the authority, one case per row):
- Item-type resolution, all five rows: declared override wins; records label;
  records verbatim; membership `<label(K)>.<property>`; membership verbatim
- Distinctness, all six rows: two same-kind sources sharing one resolved
  item-type legal (joint gate); two different-kind sources sharing one refused;
  a membership source sharing any source's item-type refused (its own owner's
  included); two same-kind sources with different resolved item-types legal
  (gate re-partitions, each gated separately); a records item-type equal to
  another kind's rendered name refused — including an *unaudited, undeclared*
  kind (whole-universe range); a membership item-type equal to any kind's
  rendered name refused
- `rename` rules: a renamed key resolves; an unrenamed property keeps its bare
  name; a membership reference field's rename produces the `g_kind` / `g_id`
  pair while `only` / `ignore` / `rename` still address bare `f`; a rename
  value colliding with an unrenamed bare name refused
  (`SourceNameCollision`, "changes key collision" message); a rename value
  colliding with a membership pair's expanded `_kind` / `_id` name refused; a
  `rename` key not a property of its source refused (`SourceColumnUnresolved`);
  a `rename` key naming a property excluded by `only` / `ignore` refused; a
  `rename` key naming a non-exempt slice_only property refused
  (`SourceSliceOnlyRead`)
- `kind_labels` threads onto every event-source plan; empty tuple when absent

**Tests** (`test_events_render.py` — render behavior):
- Migrated: every hand-constructed `SourceEventSourcePlan` carries
  (bare, bare) identity pairs + `kind_labels=()`; all existing cases green
  with unchanged expectations
- A renamed records property's `changes` entry uses the output key (create,
  update, and destroy rows); key order stays sidecar declaration order
- A membership reference field renamed `f → g` yields `g_kind` / `g_id`
  entries in place
- A labeled `<f>_kind` entry renders the label in both old and new halves; an
  unlabeled kind renders verbatim; a corrupted (unknown) kind value renders
  verbatim
- The stamped `item_type` is the plan's resolved value; the order key uses it
  (two sources aliased to one item-type interleave; an aliased split orders by
  its resolved names)
- With no labels and no renames, the log SQL is byte-identical to today

### Phase 4: Recipe — source-domain-vocabulary

**Delivers:** The author-facing minimal recipe: `config.yaml` declaring
`kind_labels`, one events-source `item_type` override, and one `rename`, with
`expect.yaml` asserting the labeled `item_type` values, renamed `changes` key,
and labeled junction member-kind values. Auto-discovered by the existing
corpus gates in `tests/recipes/test_source_recipes.py` (no test-file change).
**Demo:** Runs the recipe config against the recipe fixture emit via
`export_source` and prints the audit-log head + junction head, annotating
which values the vocabulary declarations produced.
**Contracts:** none new — exercises Phases 1–3.
**Steps:** none (single implementer)

**Files:**
| Action | File |
|--------|------|
| Create | `examples/recipes/source/source-domain-vocabulary/config.yaml` |
| Create | `examples/recipes/source/source-domain-vocabulary/expect.yaml` |
| Create | `docs/sprints/source-domain-vocabulary/demos/phase_4_recipe.py` |

**Tests:**
- The three existing corpus gates (config-load, run-and-assert, corpus guard)
  now parametrize over the new folder and pass
- `expect.yaml` asserts at least: a labeled `item_type` value, the renamed
  `changes` key, and a labeled junction member-kind value
- The whole recipe corpus stays green (no other recipe's expectations move —
  the no-declaration default is verbatim)

## What Doesn't Change

Explicit boundaries against implementer drift (design doc § What Doesn't
Change is the authority; the code-level consequences):

- `src/fabulexa_forge/exporters/source/engine.py` and `init.py` stay as-is —
  the plans carry everything the renders need, and `init` proposes no labels
  (deliberate non-scope)
- The event log's fixed column set, `item_id` / identity rendering, key
  election semantics, `id` construction, and the order-key *shape* — only the
  resolved item-type value flowing through them changes
- Sub-type discriminator *values* render verbatim; state-table and junction
  column *naming* grammar untouched
- The derivations folds (`row_state_events`, `membership_events`), the reader,
  and every other exporter mode — labels are a render-time presentation
  concern in the source sub-package only
- `_check_events_source_overlap` (population disjointness) — distinctness of
  item-types is a new, separate check
- Incremental machinery (cursor, fingerprint, window membership) — resolution
  is compile-time, window-invariant
- `tests/exporters/source/test_engine.py`, `test_election_plan.py`,
  `test_election_renders.py`, `test_init.py`, and every non-source test module
  must pass unmodified — no vocabulary declared means today's output

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/config/models.py` | `SourceEventSourceDecl.item_type`/`rename` + `source_shape` extension; `SourceConfig.kind_labels` + `kind_labels_shape` |
| `src/fabulexa_forge/errors.py` | `SourceKindLabelUnknown`, `SourceKindLabelCollision` (P2); `SourceItemTypeCollision` (P3) |
| `src/fabulexa_forge/exporters/source/columns.py` | `build_kind_label_expr` — the one labeling authority |
| `src/fabulexa_forge/exporters/source/plan.py` | Label resolution + validation in `build_source_plan`; `SourceJunctionTablePlan.kind_labels` (P2); item-type / changes-key resolution, distinctness + extended rename rules in the events plan build (P3) |
| `src/fabulexa_forge/exporters/source/renders.py` | Junction `member__<f>__kind` values through `build_kind_label_expr` |
| `src/fabulexa_forge/exporters/source/events.py` | `SourceEventSourcePlan` reshape; `changes` output keys, labeled `<f>_kind` halves |
| `tests/config/test_source_decls.py` | Parse cases: `item_type` / `rename` |
| `tests/config/test_source_config.py` | Parse cases: `kind_labels` |
| `tests/exporters/source/test_plan.py` | Label threading + rule tests (P2); resolution / distinctness / rename-rule tests + migrated assertions (P3) |
| `tests/exporters/source/test_renders.py` | `build_kind_label_expr` unit + junction labeling render tests |
| `tests/exporters/source/test_events_render.py` | Migrated plan constructions + changes-key / `<f>_kind` labeling render tests |
| `tests/playback/test_shaped_open.py` | One junction-plan construction migrated (`kind_labels=()`) |
| `examples/recipes/source/source-domain-vocabulary/config.yaml` | New recipe config |
| `examples/recipes/source/source-domain-vocabulary/expect.yaml` | New recipe expectation |
| `docs/sprints/source-domain-vocabulary/demos/phase_1_config_grammar.py` | Phase 1 demo |
| `docs/sprints/source-domain-vocabulary/demos/phase_2_junction_labels.py` | Phase 2 demo |
| `docs/sprints/source-domain-vocabulary/demos/phase_3_events_vocabulary.py` | Phase 3 demo |
| `docs/sprints/source-domain-vocabulary/demos/phase_4_recipe.py` | Phase 4 demo |
