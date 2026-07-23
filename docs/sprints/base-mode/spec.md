# Sprint: base-mode

## Purpose

Ship `mode: base` — the flat, records-only projection that hands an author the
*merged result* (one row per record, every tracked property at its reconstituted
value) at the tape's end, at an inclusive `slice_at: T`, or per window under
`incremental`.

An educator teaching incremental ETL / SCD merge points students at a `mode: base`
export as the answer key: source mode hands them the change log to merge, base mode
hands them what the merge should produce. An ML instructor sets `slice_at: 50` and
gets feature-store rows as of sim-time 50.

Design rationale and semantics live in
[`docs/architecture/pending/base.md`](../../architecture/pending/base.md) — this spec
carries contracts, phases, and test cases only, and does not restate it.

## Scope

**Capabilities touched:**
- exporters/modes: `base` (the last unshipped Stage-3 mode — closes the
  dimensional / source / streaming / base set)
- exporters/shared: `slice_only` policy gains an omit-with-notice surface;
  incremental drip-feed gains a third wrapped mode; the effective anchor gains its
  first *optional* consumer
- Stage-5 point-in-time reconstruction: **subsumed** by `base.slice_at` and retired
  as a standalone item

**Not included:**
- Stage-5 membership / queue-state export — a different grain and derivation, and
  explicitly *not* subsumed (design doc § Point-in-time subsumption)
- Multi-branch / fork-aware base export — trunk-only stage holds
- Any change to the state-at derivation itself; base composes it verbatim
- Doc promotion (`pending/base.md` → live) and `CAPABILITIES.md` status flips —
  those ship as a separate commit after sprint archival

## Breaking Changes

Purely additive to existing configs and outputs, with one latent-defect fix:

- `ExportConfig.mode` widens from `Literal["dimensional","source"]` to include
  `"base"`. Every existing config still loads and behaves identically.
- `ExportConfig` gains `base: BaseConfig | None = None`, forbidden under
  `mode: dimensional` / `mode: source`.
- **`cli.py:187` and `incremental/driver.py:183` are two-way dispatches**
  (`if mode == "source": … else: <dimensional>`). Neither uses `assert_never`, so
  widening the literal would silently route a `mode: base` config into the
  dimensional branch and trip `assert config.dimensional is not None`. Phase 4
  converts both to explicit three-way dispatch. Phases 1–3 stay green because
  nothing constructs a `mode: base` execution path until Phase 4.

## Success Criteria

- [ ] `mode: base` with no `base` section exports one flat table per records kind at
      the tape's end, in CSV and DuckDB
- [ ] `base.slice_at: T` reflects every event with `sim_time <= T` and nothing after
      (exclusive state-at horizon `T + 1`)
- [ ] A record created at-or-after the horizon is **absent**; a record deactivated
      *after* the horizon shows `active = true`, `deactivated_at = NULL`
- [ ] `--next` / `--from`/`--to` emit one full-table snapshot per kind per window
      (`write_mode='replace'`)
- [ ] With a resolved anchor, lifecycle timestamps render wallclock; with none, they
      stay raw sim-time `BIGINT` — base never *requires* an anchor
- [ ] `prop__<p>` / `presentation_id` are cast back to their declared sidecar types,
      not delivered as VARCHAR
- [ ] A non-exempt `slice_only` property is omitted with one
      `slice-only-column-omitted` notice per kind × column; the sub-typed
      discriminator carve-out is retained; omission never suppresses a table
- [ ] `base.slice_at` together with `incremental` is a load-time error
- [ ] No `last_mutation_sim_time` / `updated_at` column is ever emitted
- [ ] A base export over a corrupted emit surfaces the declared defects unchanged
      and drops no row
- [ ] The base recipe corpus loads, runs, and matches its expectations

## Contracts

### Config models — `config/models.py`

```python
class BaseConfig(StrictBaseModel):
    """The base-mode section: presentation escape hatches plus an optional
    point-in-time slice. Omit the section entirely for a bare current-state dump."""

    exclude: ExcludeDecl | None = None
    """Kinds/output tables dropped before export. `kinds` names records kinds;
    `tables` names base output table names."""
    rename: list[RenameEntry] | None = None
    """Per-table (`name`) / per-column (`columns`) overrides, keyed on the sidecar
    `records__<kind>` name. `columns` keys are state-at column identities
    (`record_id`, `presentation_id`, `created_sim_time`, `active`,
    `deactivated_at`, `prop__<p>`). `sub_type` rejected; `table` targets disjoint."""
    slice_at: int | None = None
    """Inclusive point-in-time horizon (sim-time ns). Absent -> tape's end.
    Mutually exclusive with `incremental` (enforced on ExportConfig)."""

    @model_validator(mode="after")
    def at_least_one_field(self) -> Self:
        """A present `base` section sets at least one of exclude/rename/slice_at.

        Raises:
            ValueError: An empty `base: {}` block; omit the section instead.
        """

    @model_validator(mode="after")
    def slice_at_non_negative(self) -> Self:
        """`slice_at`, when set, is a non-negative sim-time ns.

        Raises:
            ValueError: `slice_at` is negative.
        """

    @model_validator(mode="after")
    def rename_no_sub_type(self) -> Self:
        """No rename entry sets `sub_type` — base never splits a kind.

        Raises:
            ValueError: A rename entry sets `sub_type`.
        """

    @model_validator(mode="after")
    def entries_disjoint(self) -> Self:
        """No two rename entries share a `table` target.

        Raises:
            ValueError: Two rename entries target the same table.
        """
```

**`ExportConfig` behavioral changes** (described, not re-listed):
`mode` becomes `Literal["dimensional", "source", "base"]`; a
`base: BaseConfig | None = None` field is added; `mode_section_matches` gains a
`base` arm — `mode='base'` forbids the `dimensional` and `source` sections and
requires no `base` section (a bare `mode: base` is a valid full dump), and the
`base` section is forbidden under the other two modes. One new cross-field
validator:

```python
    @model_validator(mode="after")
    def base_slice_at_excludes_incremental(self) -> Self:
        """Reject `base.slice_at` together with an `incremental` block — a pinned
        instant and a window sequence are contradictory temporal selectors.

        Raises:
            ValueError: Both are set.
        """
```

### Error classes — `errors.py` (all subclass `ExportError`)

```python
class BaseExcludeUnresolved(ExportError):
    """A `base.exclude.kinds`/`base.exclude.tables` entry matches nothing base emits."""

class BaseRenameUnresolved(ExportError):
    """A `base.rename` entry's `table` is not a surviving `records__<kind>`, or a
    `columns` key does not name a state-at column of that kind."""

class BaseRenameSliceOnly(ExportError):
    """A `base.rename` entry's `columns` key names a column omitted by the
    `slice_only` policy — the rename is unsatisfiable rather than silently ignored."""

class BaseNameCollision(ExportError):
    """Two resolved base output tables share a name, or two columns of one output
    table do, after presentation defaults and `base.rename`. Never suffixed."""
```

The design doc's rule table names four *rules*; `BaseRenameResolvable` /
`BaseExcludeResolvable` map onto `BaseRenameUnresolved` / `BaseExcludeUnresolved`,
following the shipped `Source*` spelling. Message templates are the doc's
(§ Business Rules), verbatim.

### Plan models — `exporters/base/plan.py`

```python
@dataclass(frozen=True)
class BaseTableSpec:
    """One surviving records kind's resolved flat-output shape — time-agnostic."""

    kind: str
    """The records kind (the `records__<kind>` suffix)."""
    table_name: str
    """Output table name after presentation defaults and `rename`."""
    properties: frozenset[str]
    """Bare property names to reconstruct, passed straight to the state-at builder;
    `slice_only` omissions removed, an exempt discriminator retained."""
    has_presentation_id: bool
    """Whether the kind carries presentation_id — drives base's own projection and
    rename of that column in the wrapper (the state-at builder decides for itself)."""
    column_renames: "Mapping[str, str]"
    """State-at column identity -> output name; includes the `record_id -> id`
    default unless a `rename` entry overrides it."""


@dataclass(frozen=True)
class BasePlan:
    """One `BaseTableSpec` per surviving kind, in sidecar kind-declaration order.
    Identical for full, sliced, and windowed exports — the horizon is supplied at
    render, never here."""

    tables: tuple[BaseTableSpec, ...]
```

### Functions

```python
def build_base_plan(
    sidecar: "Sidecar",
    config: "BaseConfig | None",
    notice_sink: "NoticeSink",
) -> BasePlan:
    """
    Resolve the time-agnostic plan for a base export: one flat table per surviving
    records kind, its column set, presentation names, and the `slice_only`
    omissions.

    Classifies nothing and reshapes nothing — every non-excluded records kind
    yields exactly one table whose columns are the kind's STATE_AT_COLUMNS with
    `slice_only` properties omitted (one `slice-only-column-omitted` notice each,
    the discriminator carved out) and presentation names applied. Time selection
    (end-of-tape vs a horizon) is supplied at render, not here, so the plan is
    identical for a full, a sliced, and a windowed export.

    Args:
        sidecar: The reader's narrowing view of `base.json`; source of kinds,
            declared property order, `temporal_class`, and `subtype_values`.
        config: The `base` section, or None for a bare current-state dump.
        notice_sink: Required caller-supplied sink for omission notices.

    Returns:
        A `BasePlan`: one `BaseTableSpec` per surviving kind (output name, bare
        property set, presentation_id flag, column-rename map), ready for
        `build_base_render_sql` to render at a caller-chosen horizon. Column
        emission order is fixed (STATE_AT_COLUMNS prefix, presentation_id, then
        `prop__<p>` in sidecar declaration order), so it is derived, not stored.

    Raises:
        BaseRenameSliceOnly: A `rename` entry names an omitted `slice_only` column.
        BaseRenameUnresolved: A `rename` entry's `table` is not a surviving
            `records__<kind>`, or a `columns` key is not a state-at column identity.
        BaseExcludeUnresolved: An `exclude.kinds`/`exclude.tables` entry matches
            nothing base emits.
        BaseNameCollision: Two output tables, or two columns of one output table,
            share a name after presentation defaults and `rename`.
        ExportError: A resolved output name is reserved under incremental export
            (`_export_meta`/`_export_windows`/`*__rows`, `__valid_from_ns`,
            `last_mutation_sim_time`) — checked always-on via
            `exporters.reserved_names`, as source's `_check_reserved_names` does,
            so a full export and a later incremental drip on the same target agree.
        TableNotFoundError: A declared `records__<kind>` table is absent.
    """
```

```python
def build_base_render_sql(
    sidecar: "Sidecar",
    fork_path: str,
    spec: "BaseTableSpec",
    anchor: "EffectiveAnchor | None",
    horizon_ns: int | None,
) -> str:
    """Render one `BaseTableSpec` to a complete, deterministic SELECT at a horizon.

    Base's counterpart to source's `build_snapshot_render_sql`. Composes the shipped
    state-at derivation verbatim — `build_state_at_end_sql(sidecar, fork_path,
    spec.kind, spec.properties)` when `horizon_ns is None` (the structural tape's
    end, current state), `build_state_at_sql(sidecar, fork_path, spec.kind,
    spec.properties, horizon_ns)` otherwise — then wraps the raw relation with base's
    own presentation: the lifecycle timestamps `created_sim_time` and
    `deactivated_at` render through `render_anchor_timestamp_expr`, which already
    yields the raw sim-time column aliased when `anchor` is None (so base needs no
    conditional of its own); `prop__<p>` and `presentation_id` cast back from the
    state-at codec VARCHAR to their sidecar types (as source's snapshot render does);
    every column is projected under `spec.column_renames` (including
    `record_id -> id`). Never uses the compile-indirection (`base_relations`)
    wrapping. Ordered by the state-at resident's `(created_sim_time, record_id)`
    over raw ns.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        spec: The resolved per-kind flat-output shape from `build_base_plan`.
        anchor: The resolved effective anchor, or None to emit raw sim-time ns.
        horizon_ns: The exclusive reconstruction horizon — `T + 1` for `slice_at: T`,
            a window's `end_ns` under incremental — or None for the tape's end.

    Returns:
        A complete SELECT producing the flat table, ordered by
        `(created_sim_time, record_id)`.

    Raises:
        TableNotFoundError: `records__<kind>` is absent (propagated from state-at).
    """
```

```python
def build_base_query_specs(
    emit: "Emit",
    config: "ExportConfig",
    anchor: "EffectiveAnchor | None",
    window: "Window | None",
    notice_sink: "NoticeSink",
) -> list[QuerySpec]:
    """Compile the base plan to writer-ready QuerySpecs at one horizon.

    Base's counterpart to `build_source_query_specs`, and the entry point the
    incremental driver's new `mode == 'base'` branch and the full-export CLI path
    both call. Builds the plan once (threading `notice_sink`), then one QuerySpec per
    surviving kind via `build_base_render_sql`. The horizon is `window.end_ns` when
    `window` is set (a per-window snapshot), else `config.base.slice_at + 1` when
    `slice_at` is set, else None (the tape's end). Every base spec is view-less;
    `write_mode` is `'create'` for a full or sliced export and `'replace'` for a
    windowed snapshot — exactly source's snapshot delivery. `base_relations` is not a
    parameter: base never uses the compile-indirection wrapping.

    Args:
        emit: The open emit.
        config: The validated export config (`mode='base'`).
        anchor: The resolved effective anchor, or None to emit raw sim-time ns.
            Not required — unlike source, base falls back to raw ns.
        window: The window to snapshot at, or None for a full or sliced export.
        notice_sink: Receiver for `slice-only-column-omitted` notices.

    Returns:
        One QuerySpec per surviving kind, in deterministic sidecar order.

    Raises:
        ExportError: A base business rule fails (rename resolution or collision).
        TableNotFoundError: A declared `records__<kind>` table is absent.
    """
```

```python
def export_base(
    emit: "Emit",
    config: "ExportConfig",
    out: "Path",
    fmt: Literal["csv", "duckdb"],
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
) -> dict[str, int]:
    """
    Run the base exporter and write the flat projection.

    Builds the full-export base query specs (window=None, so the horizon is
    `config.base.slice_at + 1` when set, else the tape's end), then dispatches to
    the fmt-selected writer via the shared `write_query_specs` — mirroring
    `export_source`, minus the anchor requirement.

    Args:
        emit: The open emit.
        config: The validated export config (mode='base').
        out: Output target — a directory receiving one <table>.csv per output
            table (fmt='csv'), or the .duckdb file path to create (fmt='duckdb').
        fmt: Output format; the CLI constrains the raw string before this point.
        anchor: The resolved effective anchor, or None. Base does NOT require one
            — None renders lifecycle timestamps as raw sim-time ns.
        notice_sink: Receiver for plan notices (slice-only-column-omitted).

    Returns:
        Mapping of every output table name -> row count written (0-row tables are
        still emitted, never dropped).

    Raises:
        ExportError: The single-branch guard or a base business rule fails.
        ExportRuntimeError: A writer fails.
        TableNotFoundError: A declared `records__<kind>` table is absent.
    """
```

## Phases

### Phase 1: Config surface

**Delivers:** `BaseConfig` with its four validators; `ExportConfig` widened to
accept `mode: base`, carrying an optional `base` section, with the
`mode_section_matches` base arm and the `base_slice_at_excludes_incremental`
cross-field rule.
**Demo:** Loads a bare `mode: base`, a sliced one, and an excluding/renaming one;
then shows each of the five rejections with its message.
**Contracts:** `BaseConfig` (+ 4 validators), `ExportConfig` changes,
`base_slice_at_excludes_incremental`
**Steps:** none (single implementer) — additive to `models.py`; every existing
config test stays green.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Create | `tests/config/test_base_config.py` |
| Create | `docs/sprints/base-mode/demos/phase_1_base_config.py` |

**Tests:**
- Bare `mode: base` with no `base` section loads and yields `config.base is None`
- `base: {}` (empty block) is rejected by `at_least_one_field`
- `base: {slice_at: 0}` loads — zero is a valid horizon, not "unset"
- `base: {slice_at: -1}` is rejected by `slice_at_non_negative`
- A `base.rename` entry setting `sub_type` is rejected by `rename_no_sub_type`
- Two `base.rename` entries with the same `table` are rejected by `entries_disjoint`
- `mode: base` with a `source:` section is rejected by `mode_section_matches`
- `mode: base` with a `dimensional:` section is rejected by `mode_section_matches`
- `mode: dimensional` with a `base:` section is rejected by `mode_section_matches`
- `mode: source` with a `base:` section is rejected by `mode_section_matches`
- `base.slice_at` + an `incremental` block is rejected by
  `base_slice_at_excludes_incremental`
- `mode: base` + `incremental` with no `slice_at` loads (the windowed path)
- `mode: base` + `base.exclude`/`base.rename` (no `slice_at`) + `incremental` loads
- Existing tests that must still pass: `tests/config/test_models.py`,
  `tests/config/test_source_config.py`, `tests/config/test_loader.py`

### Phase 2: Plan

**Delivers:** the `exporters/base/` package skeleton, `BaseTableSpec` / `BasePlan`,
`build_base_plan` (property enumeration, `slice_only` omission + notices,
presentation defaults, `exclude` / `rename` resolution, collision and reserved-name
checks), and the four `Base*` error classes.
**Demo:** Builds a plan over the recipe fixture sidecar and prints each table's
kind, output name, property set, and rename map; then shows a `slice_only` omission
notice and one rejected rename.
**Contracts:** `BaseTableSpec`, `BasePlan`, `build_base_plan`, the four error classes
**Steps:** none (single implementer) — new package plus additive error classes.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/errors.py` |
| Create | `src/fabulexa_forge/exporters/base/__init__.py` |
| Create | `src/fabulexa_forge/exporters/base/plan.py` |
| Create | `tests/exporters/base/__init__.py` |
| Create | `tests/exporters/base/test_plan.py` |
| Create | `docs/sprints/base-mode/demos/phase_2_base_plan.py` |

**Tests:**
- One `BaseTableSpec` per records kind, in sidecar kind-declaration order
- No membership-category or fixed-category table ever produces a `BaseTableSpec`
- `table_name` defaults to the prefix-stripped kind (`records__customer` →
  `customer`)
- `column_renames` carries `record_id → id` by default
- `has_presentation_id` reflects the sidecar per kind
- A non-exempt `slice_only` property is absent from `properties`, with exactly one
  `slice-only-column-omitted` notice per kind × column, in sidecar column order
- The exempt discriminator (`prop__<K>_type` with non-empty `subtype_values`) is
  retained in `properties` even when its class is `slice_only`
- A kind whose every property is non-exempt `slice_only` still yields a table
  (identity + lifecycle only) — omission never suppresses a table
- `rename` with `name` overrides the output table name
- `rename` with `columns` overrides a state-at column identity (keyed on
  `record_id`, not the defaulted `id`)
- `rename` naming an omitted `slice_only` column raises `BaseRenameSliceOnly`
- `rename` whose `table` is not a surviving `records__<kind>` raises
  `BaseRenameUnresolved`
- `rename` whose `columns` key is not a state-at column identity raises
  `BaseRenameUnresolved`
- Two kinds renamed to the same output name raises `BaseNameCollision`
- Two columns of one table renamed to the same name raises `BaseNameCollision`
- `exclude.kinds` drops a kind; `exclude.tables` drops an output table by its
  base output name
- `exclude` naming something base does not emit raises `BaseExcludeUnresolved`
- A `rename` producing a reserved table name (`_export_meta`, `*__rows`) raises
  `ExportError`
- `config=None` yields every kind with defaults and emits only `slice_only` notices

### Phase 3: Render

**Delivers:** `build_base_render_sql` — the state-at composition, horizon selection,
anchor-or-raw-ns lifecycle rendering, cast-back to sidecar types, and rename
projection.
**Demo:** Against the recipe fixture emit, renders `patient` at the tape's end and
at `slice_at: 2*DAY`, printing both result sets side by side so the as-of
difference in `prop__status` is visible.
**Contracts:** `build_base_render_sql`
**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/exporters/base/renders.py` |
| Create | `tests/exporters/base/test_renders.py` |
| Create | `docs/sprints/base-mode/demos/phase_3_base_render.py` |

**Tests:**
- `horizon_ns=None` composes `build_state_at_end_sql` (no horizon predicate in the
  emitted SQL)
- `horizon_ns=T` composes `build_state_at_sql` at exactly `T`
- Executed: at the tape's end, `p001.prop__status` is `discharged` (its latest
  history value)
- Executed: at `horizon_ns = 2*DAY + 1`, `p001.prop__status` is `active` — the
  value at 2×DAY, not the later `discharged`
- Executed: at `horizon_ns = 1*DAY`, a record created at-or-after the horizon is
  absent from the result
- Executed: `a002` (deactivated at 2×DAY) rendered at `horizon_ns = 1*DAY` shows
  `active = true` and `deactivated_at = NULL`
- Executed: the same record at the tape's end shows `active = false` and a
  non-NULL `deactivated_at`
- With an anchor, `created_sim_time` / `deactivated_at` come back as `TIMESTAMP`
- With `anchor=None`, `created_sim_time` / `deactivated_at` come back as `BIGINT`
  raw ns
- `prop__<p>` columns come back as their declared sidecar type, not VARCHAR
- `presentation_id` is cast back to its sidecar type when the kind carries it
- `column_renames` are applied to the output (`record_id` emitted as `id`)
- No `last_mutation_sim_time` or `updated_at` column appears in the output
- The emitted SQL orders by `(created_sim_time, record_id)` over raw ns
- A property set that is empty renders identity + lifecycle columns only

### Phase 4: Engine and wiring

**Delivers:** `build_base_query_specs` and `export_base`; the CLI full-export leaf
and the incremental driver both converted to explicit three-way mode dispatch.
**Demo:** Runs a full base export to DuckDB and a two-window incremental drip over
the recipe fixture, printing per-table row counts per window.
**Contracts:** `build_base_query_specs`, `export_base`
**Steps:** none (single implementer) — the CLI and driver edits are a few lines
each; the test files gain new base cases rather than migrating existing ones.

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/exporters/base/engine.py` |
| Modify | `src/fabulexa_forge/cli.py` |
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Create | `tests/exporters/base/test_engine.py` |
| Modify | `tests/test_cli_export.py` |
| Modify | `tests/incremental/test_driver.py` |
| Create | `docs/sprints/base-mode/demos/phase_4_base_export.py` |

**Tests:**
- Full export (`window=None`, no `slice_at`): every spec has `write_mode='create'`,
  `view_name is None`, `view_sql is None`
- `slice_at: T` with `window=None`: the horizon passed to the render is `T + 1`
- `window` set: the horizon is `window.end_ns` and every spec is
  `write_mode='replace'`
- One QuerySpec per surviving kind, in deterministic sidecar order
- `export_base` with `fmt='duckdb'` writes one table per kind and returns row counts
- `export_base` with `fmt='csv'` writes one `<table>.csv` per kind
- `export_base` with `anchor=None` succeeds — no `SourceAnchorRequired` analogue
- A 0-row kind is still emitted, never dropped
- CLI: `fabulexa-forge export` on a `mode: base` config exits 0 and prints per-table
  counts
- CLI: a `mode: base` config with `--from`/`--to` writes a range export
- Driver: `--next` on a `mode: base` config emits per-window full snapshots and
  advances the cursor
- Driver: a `mode: base` config no longer reaches the dimensional branch (the
  regression the three-way dispatch fixes)
- Existing tests that must still pass: all of `tests/test_cli_export.py`,
  `tests/incremental/test_driver.py`, `tests/exporters/source/test_engine.py`

### Phase 5: Recipe corpus and corrupter totality

**Delivers:** the author-facing `examples/recipes/base/` corpus with its three-gate
test (mirroring `test_source_recipes.py`), and the corrupted-emit composition test
the design doc's § Corrupter composition and totality asserts.
**Demo:** Runs every base recipe end to end and prints each one's output tables and
row counts, then runs a base export over a corrupted emit showing the defect
surfaced rather than dropped.
**Contracts:** none new — exercises Phases 1–4 through the author-facing surface
**Steps:** none (single implementer) — the recipe YAMLs are small and formulaic;
the two test files are new authorship.

**Files:**
| Action | File |
|--------|------|
| Create | `examples/recipes/base/base-current-state/config.yaml` |
| Create | `examples/recipes/base/base-current-state/expect.yaml` |
| Create | `examples/recipes/base/base-slice-at/config.yaml` |
| Create | `examples/recipes/base/base-slice-at/expect.yaml` |
| Create | `examples/recipes/base/base-exclude-kind/config.yaml` |
| Create | `examples/recipes/base/base-exclude-kind/expect.yaml` |
| Create | `examples/recipes/base/base-rename-table/config.yaml` |
| Create | `examples/recipes/base/base-rename-table/expect.yaml` |
| Create | `tests/recipes/test_base_recipes.py` |
| Create | `tests/integration/test_corrupt_base.py` |
| Create | `docs/sprints/base-mode/demos/phase_5_base_recipes.py` |

**Tests:**
- Gate 1 (config-load): `load_export_config` succeeds for every base recipe
- Gate 2 (run-and-assert): each recipe exports through `export_base` and matches its
  `expect.yaml` via the shared `assert_recipe_output`
- Gate 3 (corpus guard): the corpus is non-empty and every folder contains exactly
  `{config.yaml, expect.yaml}`
- `base-current-state` shows `p001.status = discharged` (the tape's end)
- `base-slice-at` at 2×DAY shows `p001.status = active` — the recipe that makes the
  point-in-time capability author-visible
- `base-exclude-kind` omits the excluded kind's table entirely
- `base-rename-table` emits the author-verbatim table name
- Corrupter composition: a base export over a corrupted emit surfaces the declared
  defect in the reconstructed value
- Corrupter composition: no row is dropped and no cast error is raised — row counts
  match the uncorrupted export (totality)

## What Doesn't Change

- **`derivations/state_at.py`** — `build_state_at_sql`, `build_state_at_end_sql`,
  and `STATE_AT_COLUMNS` are composed verbatim. Base writes no independent
  point-in-time path (design doc invariant 2). Do not add a horizon parameter, a
  branch parameter, or a new entry point.
- **`exporters/base_relations.py`** — the compile-indirection wrapping exists for
  multi-table shapes that need one consistent truncated world. Base has no such
  structure, so `apply_base_relations` is never called from
  `exporters/base/` and `base_relations` is not a parameter of any base function.
  (The name similarity to the new `exporters/base/` package is coincidental.)
- **`exporters/source/`** — untouched. Base mirrors source's *shape* by writing new
  code, never by refactoring source into a shared helper. No extraction of a common
  plan/render base class.
- **`anchor.py`** — `render_anchor_timestamp_expr` already handles `anchor=None` by
  returning the raw column aliased. Base consumes it as-is; no new anchor
  resolution, no second anchor, no signature change.
- **`exporters/slice_only.py` and `exporters/notices.py`** — base reuses
  `is_non_exempt_slice_only`, the discriminator carve-out, and the
  `slice-only-column-omitted` notice code verbatim. No new notice code, no policy
  opt-out knob.
- **`membership__*` handling** — base reads `records__*` + `history` only. It emits
  no membership, junction, queue, or fact table. The Stage-5 queue-state export
  stays a separate future item.
- **Dimensional and streaming modes** — behavior is unchanged; they are touched only
  by the widened `mode` literal, which they ignore.
- **CDC / change-log shapes** — base never emits `op`, `changed_at`, or a
  version-per-change row, and never a `last_mutation_sim_time` / `updated_at`
  column.

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/config/models.py` | Add `BaseConfig` + 4 validators; widen `ExportConfig.mode`; add `base` field; extend `mode_section_matches`; add `base_slice_at_excludes_incremental` |
| `src/fabulexa_forge/errors.py` | Add `BaseExcludeUnresolved`, `BaseRenameUnresolved`, `BaseRenameSliceOnly`, `BaseNameCollision` |
| `src/fabulexa_forge/exporters/base/__init__.py` | New package docstring + layer-direction invariant |
| `src/fabulexa_forge/exporters/base/plan.py` | New — `BaseTableSpec`, `BasePlan`, `build_base_plan` |
| `src/fabulexa_forge/exporters/base/renders.py` | New — `build_base_render_sql` |
| `src/fabulexa_forge/exporters/base/engine.py` | New — `build_base_query_specs`, `export_base` |
| `src/fabulexa_forge/cli.py` | Three-way `config.mode` dispatch in `_dispatch_export`; route `base` to `export_base` |
| `src/fabulexa_forge/incremental/driver.py` | Three-way `config.mode` dispatch in `export_window`; route `base` to `build_base_query_specs` |
| `tests/config/test_base_config.py` | New — `BaseConfig` + `ExportConfig` validator cases |
| `tests/exporters/base/__init__.py` | New — test package marker |
| `tests/exporters/base/test_plan.py` | New — plan resolution, `slice_only` omission, rename/exclude/collision rules |
| `tests/exporters/base/test_renders.py` | New — state-at composition, horizon semantics, typing, ordering |
| `tests/exporters/base/test_engine.py` | New — QuerySpec compile, horizon selection, write_mode, `export_base` |
| `tests/test_cli_export.py` | Add `mode: base` full-export and range cases |
| `tests/incremental/test_driver.py` | Add `mode: base` windowed-snapshot cases |
| `tests/recipes/test_base_recipes.py` | New — three-gate base recipe corpus test |
| `tests/integration/test_corrupt_base.py` | New — base-over-corrupted-emit totality |
| `examples/recipes/base/*/config.yaml` + `expect.yaml` | New — 4 author-facing recipes |
