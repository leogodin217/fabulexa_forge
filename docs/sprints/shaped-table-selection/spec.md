# Sprint: shaped-table-selection

## Purpose

Let a tier-2 shaped-playback caller ask for the tables it needs at the bounds
it needs — `window(T1, T2, tables=S)` / `state(T, tables=S)` — paying only for
the selected tables, with the dimensional key-stability and reserved-table-name
rules promoted to always-on load-time validation (so every table that opens can
be windowed) and the start horizon opened only when an ask delivers a delta.

A downstream consumer (loom's per-strip channels) asks `head.window(last_flush,
frontier - lag, tables={"booking"})` per strip instead of materializing the
whole shape per strip; the answer for `booking` is byte-identical whichever
selection it is asked in.

Design source: [`docs/architecture/pending/shaped-table-selection.md`](../../architecture/pending/shaped-table-selection.md)
(the WHY, the semantics tables, the invariants, the four public contracts).
This spec carries the WHAT: placement decisions the doc leaves open, the
internal contracts, phases, and test cases.

## Scope

**Capabilities touched:**
- playback seam, tier 2: the `tables` selection on `window()` and `state()`
  (three `PlaybackError` gates), projection invariance, atomic asks, horizon
  economy; no first-`window()`-ask static refusal remains
- dimensional exporter — validation: `KeyColumnsStable` and the reserved
  table-name rule join the always-on per-table rule set; `WindowKeyMutable`
  and `IncrementalReservedName` retire as windowed rules
- dimensional exporter — engine: `build_query_specs` takes a table selection;
  per-table compile, `WindowKeyDuplicate`, the fk-edge guard, the dim-side leg
  guard, and plan notices scope to it; the windowed form opens the start
  horizon only for a delta-bearing selection
- source exporter — windowed compile: `build_windowed_source_query_specs`
  takes the same selection; whole-config plan per opened horizon; the start
  horizon opens only when the event log is selected
- incremental export: passes no selection; inherits the load-time refusals
  and the delta-free one-horizon compile

**Not included:** horizon memoization across asks; per-table query
performance (`audit_log`); key uniqueness at load; selection on tier 1 or the
stream head; any config surface; folding the pending doc into the live
architecture docs (a separate commit after ACCEPT).

## Breaking Changes

Internal (greenfield — callers updated, no shims):

- `build_query_specs` gains a **required** keyword-only `tables` (no default,
  the same posture as its `window` parameter). Three source callers
  (`engine.py`'s own horizon recursion, `incremental/driver.py`,
  `playback/shaped.py`) and ~148 test call sites across 15 files add
  `tables=None` — one uniform transform, run as a codemod (Phase 2).
- `build_windowed_source_query_specs` gains the same required keyword-only
  `tables`. Two source callers and one test call site.
- `windowing.check_window_key_invariant` is replaced by
  `windowing.check_key_columns_stable` (no delivery-class gate; a new
  message). `windowing.check_windowed_reserved_names` is deleted; the check
  becomes `validation.check_reserved_table_name`, run by `validate_table`.
- `ShapedPlayback.window` / `.state` gain an optional `tables` parameter
  (default `None` = the whole shape, byte-identical to today). Additive.

Observable:

- A dimensional config whose `key` includes a horizon-variant column, or a
  table named `_export_meta` / `_export_windows`, is refused **at load** by
  `export`, the incremental driver, and `open_shaped_playback` alike — where
  today the one-shot export accepts it and the first windowed compile refuses
  it (and only for an `upsert` table, in the key case). Every config shipped
  with the repo already satisfies both rules.
- A delta-free windowed compile (dimensional: no `upsert`/`append` table in
  the selection; source: the event log unselected or undeclared) opens the
  end horizon only, so its plan notices are emitted once per ask instead of
  twice. Delivered content is unchanged.

## Success Criteria

- [ ] `window(T1, T2, tables=S)` and `state(T, tables=S)` return the selected
      tables in `tables()` order; each `ShapedTable` equals the same table's
      answer under `tables=None`, row-for-row and byte-for-byte, for both modes
- [ ] `tables=None` is byte-identical to today's whole-shape answer
- [ ] A bare `str`, an empty selection, and an unknown name each raise
      `PlaybackError` with the design's message before any compile; unknown
      names are listed sorted, declared names in `tables()` order
- [ ] A delta-free `window()` ask opens the end horizon only (plan notices
      emitted once); a delta-bearing ask opens both (twice)
- [ ] An unselected table's `WindowKeyDuplicate` never refuses a sibling's
      ask; a selected table's guarded edge still guards the dim it reaches
      whether or not that dim is selected
- [ ] `validate_table` refuses an unstable `key` column on every table
      regardless of delivery class and a reserved table name, one-shot export
      included; no static rule runs in the windowed compile
- [ ] `open_shaped_playback` refuses what `export` refuses and nothing more;
      after open, `window()` raises only data guards
- [ ] The incremental driver's delivered content is unchanged
      (`tests/incremental/test_horizon_acceptance.py` and the drip-equals-
      full-export tests pass untouched)
- [ ] `make test` green; every recipe and example config still loads

## Contracts

The four public contracts — `ShapedPlayback.window` / `.state`,
`build_query_specs`, `build_windowed_source_query_specs` — are in the design
doc § Interface Contracts verbatim and are not repeated here. Below are the
placement decisions and the internal contracts the doc leaves open.

### Placement: the stable-key rule lives in `windowing.py`

`validation.py` imports `fk.py` at module level and `windowing.py` imports
`validation.py` at module level (`check_source_table_exists`), so the rule
cannot be defined in `validation.py` with a module-level import of the
horizon-invariance reading. It is defined in `windowing.py` beside
`_channel_variance` — the module's own stated posture ("the classifier and
the key gate read the same function so the two cannot drift") — and
`validate_table` imports it inside the function body, exactly as it already
imports `fk` and `lookup`. The design doc's signature is unchanged.

```python
# src/fabulexa_forge/exporters/dimensional/windowing.py
# replaces check_window_key_invariant

def check_key_columns_stable(
    table_decl: "TableDecl",
    config: "DimensionalConfig",
    sidecar: "Sidecar",
) -> None:
    """Enforce KeyColumnsStable: every `key` column is a stable row identity.

    An always-on business rule of the dimensional mode, run by
    `validate_table` on every table regardless of delivery class — the
    one-shot export, the incremental driver, and the shaped head alike —
    and run last among the per-table rules, after every rule the reading
    presumes (declared key columns, resolvable projections, ordinal
    sibling refs, fk targets and paths, membership edges, lookup safety),
    so those refuse under their own identities first. A pure function of
    declaration and sidecar; never reads data. Each key column's value
    channel is classified by `_channel_variance` — conservative: a channel
    of unknown constancy (a producer column outside the structural set, a
    reference fk on an emit without `history_tracked` flags) is refused,
    not admitted. The first varying channel in `key` order refuses.

    Args:
        table_decl: The output table declaration (its key and columns).
        config: The dimensional config (fk resolution).
        sidecar: The open emit's sidecar.

    Raises:
        ExportError: A key column's value can change over the run. Message:
            "table '{name}' key column '{key_col}': its value can change
            over the run ({variance}); a key identifies the row for the
            whole run".
    """
```

`check_windowed_reserved_names` is deleted from `windowing.py`; the module
docstring drops the two static rules and keeps the classifier,
`check_window_key_unique`, and the horizon-invariance reading.

### Dimensional validation

```python
# src/fabulexa_forge/exporters/dimensional/validation.py

def check_reserved_table_name(table_decl: "TableDecl") -> None:
    """Enforce the reserved table-name rule: no author table named for an
    incremental bookkeeping table.

    Always-on, full export included — beside `check_reserved_presentation_name`
    — so a full export and a later `--next` drip on the same target agree by
    construction (the source mode's posture, now both modes'). Reads the
    shared `exporters.reserved_names.is_reserved_table_name` predicate.

    Args:
        table_decl: The output table declaration.

    Raises:
        ExportError: The table is named `_export_meta` / `_export_windows`.
            Message (unchanged from today's windowed rule): "table '{name}':
            name '{name}' is reserved under incremental export".
    """


def validate_table(
    table_decl: "TableDecl",
    config: "DimensionalConfig",
    sidecar: "Sidecar",
    notice_sink: "NoticeSink",
    *,
    anchor: "EffectiveAnchor | None" = None,
    election: "Election | None" = None,
) -> str:
    """Unchanged signature. Behavioral change: runs `check_reserved_table_name`
    in the table-level block immediately after `check_reserved_presentation_name`,
    and runs `check_key_columns_stable` (imported inside the function body from
    `windowing`) as the LAST rule, after the per-column loop — so
    KeyColumnsDeclared, ProjectionColumnExists, OrdinalRefsSiblings,
    FkTargetIsDim, ReferencePathResolvable, MembershipEdgeResolvable, and
    LookupColumnSafety refuse under their own identities first. The docstring's
    rule list gains both rules; its "unlike IncrementalReservedName" aside on
    `check_reserved_presentation_name` is deleted.
    """
```

### Dimensional engine (internal)

```python
# src/fabulexa_forge/exporters/dimensional/engine.py

def _build_windowed_query_specs(
    emit: "Emit",
    config: DimensionalConfig,
    anchor: "EffectiveAnchor | None",
    window: "Window",
    notice_sink: "NoticeSink",
    election: "Election",
    tables: "Collection[str] | None",
) -> list[QuerySpec]:
    """The horizon compile (§ `build_query_specs`, window set), over a selection.

    Classifies the compiled tables only (`window_delivery_class` over the
    declarations `tables` selects, declaration order). Opens the end horizon
    (`window.end_ns - 1`) always, through `build_query_specs(..., None, ...,
    tables=tables)` over the truncated tape; opens the start horizon
    (`window.start_ns - 1`) only when some compiled table's class is 'upsert'
    or 'append'. Pairs end (and start) specs with the compiled declarations
    positionally. No static rule runs here: `check_key_columns_stable` and
    `check_reserved_table_name` are `validate_table`'s, run by every horizon's
    full-export compile.

    Args:
        emit: The open physical emit.
        config: The validated dimensional config.
        anchor: The resolved EffectiveAnchor, or None.
        window: The half-open window to compile.
        notice_sink: Receiver for plan notices, once per horizon opened.
        election: The resolved election.
        tables: The declared names to compile, or None for every table
            (already validated by the caller — `build_query_specs` asserts).

    Returns:
        One QuerySpec per compiled table, declaration order; 'snapshot'
        tables carry the end-horizon query with write_mode 'replace', every
        other the `end EXCEPT ALL start` delta with write_mode 'append' /
        'upsert' (+ `upsert_key`), guarded by WindowKeyDuplicate.

    Raises:
        ExportError: WindowKeyDuplicate on a compiled 'upsert' table; any
            refusal the horizon compiles raise.
    """
```

`build_query_specs`' full-export loop iterates `config.tables` and skips any
declaration not in `tables`; `_guard_fk_columns` runs for compiled tables
only, so the `dim_decls` / `dim_surfaces` accumulators — and therefore
`_guard_dim_side_legs` — cover exactly the dims the compiled tables' guarded
edges reach (the existing accumulator shape already gives this; no new
plumbing). At the top of `build_query_specs`, an `assert` checks every name
in a non-None `tables` is declared (a programming error, never `ExportError`).

### The shaped head (internal)

```python
# src/fabulexa_forge/playback/shaped.py

_SELECTION_IS_STR_MSG = (
    "tables must be a collection of table names, not a str: {value!r}"
)
_SELECTION_EMPTY_MSG = "tables must name at least one declared table"
_SELECTION_UNKNOWN_MSG = (
    "tables names no declared output table: {unknown}; declared: {names}"
)


def _resolve_selection(
    tables: "Collection[str] | None",
    table_decls: tuple[ShapedTableDecl, ...],
) -> frozenset[str] | None:
    """Run the three selection gates; return the selected name set.

    Gate order: a bare `str` (refused before any name is read — a str is a
    Collection of its characters), empty, unknown name. Set semantics: a
    repeated name selects its table once.

    Args:
        tables: The caller's selection, or None for the whole shape.
        table_decls: The head's `tables()`.

    Returns:
        The selected names as a frozenset, or None when `tables` is None.

    Raises:
        PlaybackError: `_SELECTION_IS_STR_MSG` with the value; `_SELECTION_EMPTY_MSG`;
            `_SELECTION_UNKNOWN_MSG` with `unknown` the unknown names sorted
            and comma-joined and `names` the declared names in tables()
            order, comma-joined.
    """


def _compile_window_specs(
    emit: "Emit",
    config: "ExportConfig",
    anchor: "EffectiveAnchor | None",
    window: Window,
    notice_sink: "NoticeSink",
    election: "Election",
    tables: frozenset[str] | None,
) -> "list[QuerySpec]":
    """As today, threading `tables` to the mode engine's horizon compile
    (`build_query_specs(..., tables=tables)` /
    `build_windowed_source_query_specs(..., tables=tables)`). Returns the
    selected tables' specs in the mode's compile order.
    """


def _compile_state_specs(
    truncated_emit: "Emit",
    config: "ExportConfig",
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
    base_relations: "Mapping[str, str]",
    election: "Election",
    tables: frozenset[str] | None,
) -> "list[QuerySpec]":
    """As today, over a selection. Dimensional: `build_query_specs(..., None,
    ..., tables=tables)` — the engine compiles the selected tables only.
    Source: the plan builds whole-config (`build_source_plan`), the full
    compile runs (`build_source_query_specs`), and this seam keeps the specs
    whose `table_name` is in `tables` (every spec when None) before the
    base-relations rewrite. Returns the selected specs in compile order.
    """
```

`ShapedPlayback.window` / `.state` gate bounds first, then
`_resolve_selection`, then compile — every gate sidecar-only. Their docstrings
follow the design doc. `open_shaped_playback`'s docstring drops "the windowed
business rules are ask-scoped — validated on the first window() call"; after
this sprint the only ask-time refusals are the data guards.

### Source engine

`build_windowed_source_query_specs(..., *, tables)` per the design doc.
Internally: the end-horizon plan builds and compiles as today; the unit list
is filtered to the selected names (assert every name is an output table of
the plan); the start horizon is opened iff the event log is among the
selected units; the returned specs are the selected units' in declared order,
event log last. `build_source_query_specs` is unchanged.

## Phases

### Phase 1: Always-on key-stability and reserved-table-name rules

**Delivers:** `KeyColumnsStable` and the reserved table-name rule run in
`validate_table` at every load of a dimensional config; the two static
windowed rules leave `windowing.py` and the engine's horizon compile. After
this phase, `tables()` is the complete static windowability surface.

**Demo:** A hand-built dimensional config with (a) a type-1 dim (`snapshot`
class) keyed on a tracked property and (b) a table named `_export_meta`, each
refused by `validate_table` / `export_dimensional` one-shot with the rule's
message; the same config with a stable key + a legal name exports and opens
a shaped head; `head.window()` on it raises nothing static.

**Contracts:** `check_key_columns_stable`, `check_reserved_table_name`,
`validate_table` (modified), `_build_windowed_query_specs` (the two static
calls removed — the selection parameter arrives in Phase 2).

**Steps:** none (single implementer)

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/dimensional/windowing.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/validation.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/engine.py` |
| Modify | `src/fabulexa_forge/exporters/reserved_names.py` |
| Modify | `tests/exporters/dimensional/test_windowing.py` |
| Modify | `tests/exporters/dimensional/test_validation.py` |
| Modify | `tests/playback/test_shaped_open.py` |
| Create | `docs/sprints/shaped-table-selection/demos/phase_1_always_on_key_rules.py` |

**Tests:**
- `test_windowing.py`: the four `check_window_key_invariant` tests re-target
  `check_key_columns_stable`; `test_snapshot_table_key_not_checked` flips to
  `test_snapshot_table_unstable_key_refused` (a mutable-filter `snapshot`
  table keyed on a tracked property is refused — the class no longer gates
  the rule); the two refusal tests match the new "can change over the run"
  message; `test_reserved_table_name_refused` moves to `test_validation.py`
  against `check_reserved_table_name`; the module docstring drops the two
  static rules
- `test_validation.py`: `validate_table` refuses a `_export_meta` table name
  (message as today's); `validate_table` refuses a type-1 dim keyed on a
  tracked property, naming table, column, and "reads tracked property";
  `validate_table` refuses a key column reading a producer column outside
  the structural set ("of unknown constancy"); rule order — a key column
  that is an fk with an undeclared target refuses under `FkTargetIsDim`'s
  identity, not as a stability refusal; a stable-key table passes
- `test_shaped_open.py`: `open_shaped_playback` refuses a dimensional shape
  with an unstable key on a `snapshot` table (ExportError at open, not on
  the first `window()`); refuses a reserved table name at open
- Existing: `tests/incremental/test_horizon_acceptance.py`,
  `tests/incremental/test_driver.py`, `tests/writers/test_duckdb_window.py`,
  `tests/playback/test_shaped_window.py`, `tests/recipes/`, and every
  example config still pass untouched (the repo's estate already satisfies
  both rules — a red here is a fixture keyed on a mutable column, to be
  re-keyed, never a rule softened)

### Phase 2: Dimensional engine selection and horizon economy

**Delivers:** `build_query_specs(..., tables=)` — per-table compile, the
always-on per-table rules, `WindowKeyDuplicate`, the fk-edge guard, the
dim-side leg guard, and plan notices scoped to the selection; the windowed
form opens the start horizon only for a delta-bearing selection. All callers
state their selection.

**Demo:** Over a small emit with a type-1 dim, an `append` fact reaching it,
and an `upsert` fact: compile `tables={"dim"}` under a window and show one
horizon's notices; compile `tables={"fact_upsert"}` and show two; show the
selected spec's SQL equals the same table's spec under `tables=None`; show a
whole-shape compile over a config with a duplicate-key `upsert` table refuses
while `tables={"dim"}` on the same config succeeds.

**Contracts:** `build_query_specs` (design doc), `_build_windowed_query_specs`.

**Steps:** `source → migrate (codemod, 15 files) → author (1 file)`

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/dimensional/engine.py` |
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Modify | `src/fabulexa_forge/playback/shaped.py` |
| Modify | `tests/exporters/test_base_relations.py` |
| Modify | `tests/exporters/test_notices.py` |
| Modify | `tests/exporters/dimensional/test_election_fk.py` |
| Modify | `tests/exporters/dimensional/test_export_dimensional.py` |
| Modify | `tests/exporters/dimensional/test_fk.py` |
| Modify | `tests/exporters/dimensional/test_grains.py` |
| Modify | `tests/exporters/dimensional/test_lookup.py` |
| Modify | `tests/exporters/dimensional/test_provenance.py` |
| Modify | `tests/exporters/dimensional/test_scd.py` |
| Modify | `tests/exporters/dimensional/test_scd2_renderings.py` |
| Modify | `tests/exporters/dimensional/test_scd2_source_filter.py` |
| Modify | `tests/incremental/test_driver.py` |
| Modify | `tests/playback/test_shaped_state.py` |
| Modify | `tests/playback/test_shaped_window.py` |
| Modify | `tests/writers/test_duckdb_window.py` |
| Create | `tests/exporters/dimensional/test_selection.py` |
| Create | `docs/sprints/shaped-table-selection/demos/phase_2_dimensional_selection.py` |

The `source` step edits the three source files (the driver and the seam pass
`tables=None`; the seam's own selection arrives in Phase 3) and writes the
demo. The `migrate` step is one uniform transform — append `tables=None` to
every `build_query_specs(...)` call that lacks a `tables` keyword — over the
15 listed test files. The `author` step writes `test_selection.py`.

**Tests** (`tests/exporters/dimensional/test_selection.py`):
- `tables=None` returns every declared table in declaration order (full
  export and windowed), spec-for-spec equal to the pre-sprint output shape
- A selection returns the selected tables only, in declaration order
  regardless of the collection's iteration order; a list with a repeated
  name yields one spec
- Projection invariance at spec level: for a three-table config, each
  singleton selection's spec (`sql`, `write_mode`, `upsert_key`, provenance)
  equals the same table's spec under `tables=None`, full export and windowed
- An unselected table's plan notices are not emitted (a table whose
  discriminator value is unobserved emits `discriminator-value-unobserved`
  under `tables=None` and nothing under a sibling-only selection)
- Horizon economy: under a window, a `snapshot`-only selection delivers each
  plan notice once; a selection containing an `upsert` or `append` table
  delivers it twice
- An unselected `upsert` table with a duplicate key does not refuse a
  sibling's selection; selecting it refuses with `WindowKeyDuplicate`
- Dim-side leg locality: an fk edge whose resolved surface the reached dim's
  key projects is guarded when the fact is selected and the dim is not
  (`ElectedKeyDuplicate` labelled "(dim-side leg)" on a corrupted
  presentation_id — reuse the election fixtures of `test_election_fk.py`);
  selecting the dim alone does not run it
- An undeclared name in `tables` fails an `AssertionError`, never
  `ExportError`
- Existing: every migrated file green; `tests/incremental/test_horizon_acceptance.py`
  unchanged and green

### Phase 3: Source engine selection and the seam's `tables` asks

**Delivers:** `build_windowed_source_query_specs(..., tables=)`; the shaped
head's `window(T1, T2, tables=S)` and `state(T, tables=S)` with the three
gates, projection invariance, atomic asks, and the horizon economy on both
modes.

**Demo:** Over the shaped test scaffold's emit shape (a self-contained
rebuild), open a dimensional head and a source head; for each, ask every
singleton and show its `ShapedTable` equals the whole-shape answer's; show
`window(..., tables={"gadget"})` on the source head emits its plan notices
once and `tables={"widget_events"}` twice; show the three gate messages.

**Contracts:** `build_windowed_source_query_specs` and
`ShapedPlayback.window` / `.state` (design doc); `_resolve_selection`,
`_compile_window_specs`, `_compile_state_specs`.

**Steps:** `source → author (3 files)`

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/source/engine.py` |
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Modify | `src/fabulexa_forge/playback/shaped.py` |
| Modify | `tests/playback/test_shaped_window.py` |
| Modify | `tests/playback/test_shaped_state.py` |
| Modify | `tests/playback/_shaped_fixtures.py` |
| Modify | `tests/exporters/source/test_engine.py` |
| Create | `tests/playback/test_shaped_selection.py` |
| Create | `docs/sprints/shaped-table-selection/demos/phase_3_shaped_table_selection.py` |

The `source` step edits the three source files, migrates the one
`build_windowed_source_query_specs` call site in `test_shaped_window.py`
(`tables=None`), and writes the demo. The `author` step writes
`test_shaped_selection.py`, adds the source-engine cases to
`tests/exporters/source/test_engine.py`, and corrects the two stale
docstrings (`test_state_only_shape_never_runs_windowed_business_rules` and
`state_junction_shape_config`) that attribute the membership-grain
`window()` refusal to a "windowed-grain rule" — it is `WindowKeyDuplicate`
(`record_id` is not unique on a membership grain), a data guard, and the
test's behavior is unchanged.

**Tests:**
- `tests/exporters/source/test_engine.py`: `tables=None` returns every unit,
  event log last, as today; a state-table-only selection returns that unit's
  spec (write_mode 'replace') and emits each plan notice once; a selection
  including the event log returns it last with write_mode 'append' and emits
  each plan notice twice; the selected spec equals the same unit's spec under
  `tables=None`; an unknown name fails an `AssertionError`
- `tests/playback/test_shaped_selection.py`:
  - `tables="booking"` (a bare str) raises `PlaybackError` with
    `_SELECTION_IS_STR_MSG`, on both `window` and `state`, both modes
  - `tables=set()` / `tables=[]` raise `PlaybackError` with `_SELECTION_EMPTY_MSG`
  - `tables={"nope", "also_nope", "gadget"}` raises `PlaybackError` naming
    `also_nope, nope` (sorted) and the declared names in `tables()` order;
    nothing compiles (the notice sink stays empty)
  - Gate order: invalid bounds refuse before a bad selection
  - Ask order is irrelevant: `tables=["shipment", "gadget"]` answers in
    `tables()` order; a repeated name answers once
  - Projection invariance, dimensional: for every singleton and one pair,
    `window(T1, T2, tables=S)[t]` equals `window(T1, T2)[t]` (`name`,
    `delivery`, and `table.equals`), on a window with data on both a
    `snapshot` and a delta table; likewise `state`
  - Projection invariance, source: the same over the source shape (state
    tables, the junction, and the event log)
  - `tables=None` on both asks equals the pre-sprint whole-shape answer
    (the existing `test_shaped_window.py` / `test_shaped_state.py` reference
    comparisons keep passing)
  - Horizon economy: a `snapshot`-only `window()` ask on the source shape
    delivers each plan notice once; an ask including the event log twice;
    same on the dimensional shape with a type-1 dim vs an `append` fact
  - Atomicity: on a dimensional shape with a duplicate-key `upsert` table
    and a clean sibling, `window(..., tables={dup, clean})` raises
    `WindowKeyDuplicate` naming `dup` and delivers nothing;
    `tables={clean}` succeeds; `tables={dup}` refuses
  - `state()` delivery is 'snapshot' on every selected table
- Existing: `test_shaped_window.py`, `test_shaped_state.py`,
  `test_shaped_open.py`, `tests/incremental/test_driver.py`'s source-mode
  drip tests, and `test_horizon_acceptance.py` green

## What Doesn't Change

- `ShapedTable` / `ShapedTableDecl`; `tables()` order and membership;
  `window_delivery_class` / `source_window_delivery` and their tests — the
  delivery class is still a pure decl fact and never a refusal
- The answer for any table: values, in-table order, typing, delivery class.
  Selection and the horizon economy alter which tables compile and which
  horizons open, never content
- The two-horizon compile for a delta-bearing ask; `compose_window_delta_sql`;
  `check_window_key_unique` (`WindowKeyDuplicate` stays the one data-level
  windowed refusal, still end-horizon only, still `upsert` tables only)
- `open_truncated_tape`, the compile indirection (`base_relations`), and the
  seam's post-compile rewrite for source `state()`
- The incremental driver's window sequence, cursor, fingerprint, writers, and
  whole-shape compile (it passes `tables=None`); the acceptance suite
  `tests/incremental/test_horizon_acceptance.py` is not edited
- `build_source_query_specs` and `build_source_plan` (whole-config, unchanged
  validation surface); the source mode's own reserved-name check in `plan.py`
- `build_base_query_specs` and the base mode entirely (no shaped head for base)
- Tier 1 (`playback/head.py`, `events`, `snapshot`, `seek`) and the stream head
- The horizon-invariance reading (`_channel_variance`, `_source_variance`,
  `_ordinal_variance`, `_reference_fk_variance`) — the rule reads it, it does
  not change
- `_guard_fk_columns` / `_guard_dim_side_legs` bodies — selection reaches them
  by calling `_guard_fk_columns` for compiled tables only
- `exporters/reserved_names.py` constants and predicates (its module docstring
  updates the dimensional site it names)
- No config field, no `init` change, no CLI change

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/exporters/dimensional/windowing.py` | `check_window_key_invariant` → `check_key_columns_stable` (all tables, new message); delete `check_windowed_reserved_names`; module docstring |
| `src/fabulexa_forge/exporters/dimensional/validation.py` | Add `check_reserved_table_name`; `validate_table` runs it and `check_key_columns_stable` (last); docstrings |
| `src/fabulexa_forge/exporters/dimensional/engine.py` | P1: drop the two static calls from the horizon compile. P2: required `tables` on `build_query_specs`; selection-scoped loop/guards; `_build_windowed_query_specs` classifies selected tables and opens the start horizon only when delta-bearing |
| `src/fabulexa_forge/exporters/source/engine.py` | Required `tables` on `build_windowed_source_query_specs`; selected units returned; start horizon iff the event log is selected |
| `src/fabulexa_forge/incremental/driver.py` | Pass `tables=None` to both windowed compiles |
| `src/fabulexa_forge/playback/shaped.py` | `window` / `state` gain `tables`; `_resolve_selection` + three messages; `_compile_window_specs` / `_compile_state_specs` thread the selection; docstrings drop the ask-scoped static rules |
| `src/fabulexa_forge/exporters/reserved_names.py` | Docstring: the dimensional enforcement site is now `validation.check_reserved_table_name` |
| `tests/exporters/dimensional/test_windowing.py` | Re-target the key-rule tests; flip the snapshot-class case; move the reserved-name test out |
| `tests/exporters/dimensional/test_validation.py` | Reserved table name, stable-key refusals (tracked property, unknown constancy, snapshot class), rule ordering |
| `tests/playback/test_shaped_open.py` | Unstable key / reserved name refused at open |
| `tests/exporters/dimensional/test_selection.py` | New: engine selection, notices, horizon economy, guard locality, assertion |
| 15 test files (Phase 2 codemod) | `tables=None` at every `build_query_specs` call |
| `tests/exporters/source/test_engine.py` | Windowed selection cases |
| `tests/playback/test_shaped_selection.py` | New: gates, order, projection invariance (both modes), horizon economy, atomicity |
| `tests/playback/test_shaped_window.py` | `tables=None` at the source-engine reference call |
| `tests/playback/test_shaped_state.py`, `tests/playback/_shaped_fixtures.py` | Correct the stale "windowed-grain rule" docstrings |
| `docs/sprints/shaped-table-selection/demos/phase_{1,2,3}_*.py` | One demo per phase |
