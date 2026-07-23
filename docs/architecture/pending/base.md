---
status: draft
---

# `base` Exporter Mode

The flat single-branch projection: one row per record, reconstituted to current
state (or to an as-of-T state), with no genre distinction and no change log. The
last unshipped Stage-3 exporter mode. It closes the Stage-3 mode set
(dimensional / source / streaming / **base**) and — by the position this doc
takes — subsumes the Stage-5 point-in-time / feature-store export.

## Problem

The reader emits `records__<kind>` as a spine plus a long-form `history` SCD-2
change log. Every shipped mode either preserves that change-log shape (source's
CDC render, streaming's event replay) or reshapes it into a warehouse
(dimensional's star). None delivers the *merged result* — the flat current-truth
table an incremental-ETL author is trying to build: one row per record, every
tracked property carrying its latest reconstituted value, denormalized and
ready to consume. There is also no way to ask "what did every record look like
at sim-time T?" as a **dataset** — the state-at reconstruction ships as a
derivation and as source's per-window snapshot, but not as a standalone,
whole-emit table output an author points a feature pipeline at.

Concretely, an author who wants "current state of all customers, flat, as of the
end of the run" or "state of all customers as of sim-time 50, for a training
label window" has no mode to select:

```yaml
# No mode: base exists. The closest shipped option is source with
# change_delivery: snapshot — but that keeps the genre trichotomy, requires an
# anchor, emits junction/reference/transaction tables too, and has no way to
# pin a single point-in-time as a full export (snapshot needs a windowed run).
mode: source
source:
  change_delivery: snapshot   # per-window snapshots; no single-T full export
```

## Solution

Add `mode: base` — a records-only flat projection whose every output table is
the **state-at** reconstruction of one records kind, materialized as a table.
With no slice pinned it renders each kind at the tape's end (current state);
with `slice_at: T` it renders each kind as of an inclusive horizon; under an
incremental invocation it renders each kind at each window's horizon. All three
are entry points into the already-shipped state-at resident — base introduces
**no new point-in-time reconstruction**. It is the flattened, already-merged
counterpart to source: source hands the author the change log to merge; base
hands the author the answer.

```yaml
# Current-state full dump of every records kind — no section needed.
mode: base

# ---

# Point-in-time projection as of sim-time 50, with two escape hatches.
mode: base
base:
  slice_at: 50            # inclusive horizon; state as of sim-time 50
  exclude:
    kinds: [audit_log]
  rename:
    - table: records__customer   # matched on the sidecar base-table name
      name: dim_customer_current
rebase:                    # optional wallclock rendering (raw ns otherwise)
  base_date: 2020-01-01
```

```
records__<kind>  ─┐
history          ─┼─▶  state-at resident  ─▶  flat  <kind>  table (one row/record)
                  │      end-of-tape  (no slice_at → current state)
                  │      horizon T+1  (slice_at: T → as-of-T)
                  │      window horizon (incremental → per-window snapshot)
                  └────────────────────────────────────────────────────────────
```

## Affected Subsystems

- **Config models (`ExportConfig`)** — `mode` gains a third literal, `base`; a
  new optional `base` section (`BaseConfig`) joins `dimensional` and `source`.
  The discriminator validator gains a `base` arm (base's section is optional, a
  bare `mode: base` is a valid full dump; the other two modes' sections are
  forbidden). One new cross-field rule: `slice_at` and an `incremental` block
  are mutually exclusive.
- **A new `base` exporter** — a records-only compile that classifies nothing
  (no genre trichotomy) and reshapes nothing (no star): every records kind maps
  to exactly one flat output table whose columns and rows are the state-at
  resident's canonical relation. It composes the shipped state-at builders,
  the effective-anchor renderer, the `slice_only` policy, the notice channel,
  the operational presentation-name posture, and the writers.
- **Derivations layer** — gains a new consumer, not new code. `build_state_at_sql`
  and `build_state_at_end_sql` (and their `STATE_AT_COLUMNS` relation) are
  base's per-table engine. Base is the first mode for which state-at is the
  *whole* output rather than one delivery option.
- **Incremental driver** — gains `base` as a third wrapped mode. Every base
  table is snapshot-delivered (reconstructed at the window horizon), so base
  wires into the existing window sequence, cursor, and fingerprint exactly as
  source's `change_delivery: snapshot` change-log kinds do — no new window
  derivation.
- **`slice_only` export policy** — gains base as an omit-with-notice surface
  (source-style), reusing the `slice-only-column-omitted` notice and the
  discriminator carve-out verbatim; a `rename` naming an omitted column errors.
- **CLI (`fabulexa-forge export`)** — dispatches `mode: base` to the new
  exporter. No new verb.
- **The Stage-5 point-in-time export** — this mode's `slice_at` *is* the
  "point-in-time reconstruction → ML feature-store rows" capability, so base
  delivers it directly rather than as a separate build (see § Point-in-time
  subsumption). The Stage-5 membership / queue-state prong is a different grain
  and is not subsumed.

## What Doesn't Change

- **The state-at derivation.** Base composes `build_state_at_sql` /
  `build_state_at_end_sql` unchanged — same signatures, same
  `STATE_AT_COLUMNS`, same exclusive-horizon arithmetic. No new reconstruction
  path is written.
- **The truncated-tape / playback seam.** Base is a CLI file exporter, not a
  playback consumer; it does not call `state(T)` and does not use the
  compile-indirection (`base_relations`) wrapping. It reaches state-at directly
  by horizon. (Why this is equivalent: § Relationship to the truncated tape.)
- **Membership / queue-state.** Base reads `records__*` + `history` only. It
  emits no `membership__*` / junction / queue tables. The Stage-5 queue-state
  export is a separate derivation on a separate grain and is **not** subsumed.
- **The `slice_only` invariant.** Base decides *how* it enforces the policy
  (omit, source-style), never *whether*. No opt-out knob, no new YAML field.
- **The effective-anchor contract.** Base resolves through the one shared
  anchor and adds only its own lifecycle-timestamp rendering; it introduces no
  second anchor and no new precedence rule.
- **CDC / change-log shapes.** Base never emits an `op` / `changed_at` column or
  a version-per-change row. That shape is source's and streaming's.

## Semantics

### The flat projection

| Condition | Result |
|---|---|
| An emit with records kinds `K1…Kn` | One flat output table per kind; no junction, membership, reference, or fact tables |
| A records kind `K` | Exactly one output table: the state-at column set for `K` — the `STATE_AT_COLUMNS` prefix (`record_id`, `created_sim_time`, `active`, `deactivated_at`), then `presentation_id` (when the kind carries it), then one `prop__<p>` per surviving property in sidecar column-declaration order |
| A tracked property | Its most-recent `history.value` at-or-before the horizon (raw slice value at the tape's end); `NULL` when no history precedes the horizon |
| A constant (untracked) property | Its current records value — the same declared temporal-honesty exception every state-at consumer shares |
| A `slice_only` property | Omitted with a notice (§ The `slice_only` posture) |
| A sub-typed discriminator `prop__<K>_type` (subtype_values non-empty) | Carried as a classification value (the carve-out), never as an as-of value |

### Temporal selector: three mutually consistent horizons

| Selector | Horizon | State-at entry point | Output |
|---|---|---|---|
| No `slice_at`, no `incremental` | Tape's end (structural, no horizon computed) | `build_state_at_end_sql` | One current-state full table per kind |
| `slice_at: T` (full export) | `T + 1` (exclusive; inclusive of events at T) | `build_state_at_sql(horizon_ns=T+1)` | One as-of-T full table per kind |
| `incremental` (`--next` / `--from` / `--to`) | Each window's end `end_ns` | `build_state_at_sql(horizon_ns=end_ns)` | One full-table snapshot per kind per window |
| `slice_at` **and** `incremental` together | — | — | Config error (§ Validation Rules) |

`slice_at: T` is **inclusive of T**: an event at exactly `sim_time == T` is
reflected, matching playback's inclusive-T snapshot; the exclusive state-at
horizon is therefore `T + 1`. A negative `slice_at` is a parse-time error.

### Lifecycle and mutation columns at a horizon

| Column | Rule |
|---|---|
| `active`, `deactivated_at` | Horizon-rendered from the spine: a record deactivated *after* the horizon shows `active = true`, `deactivated_at = NULL`. A deactivation is a spine fact, not a `history` row — the end-of-tape entry point is used for current state precisely so this is never mis-cleared against `history` alone |
| `created_sim_time` | Carried; a record created at-or-after the horizon is **absent** (not present-with-nulls), because state-at filters `created_sim_time < horizon` |
| `last_mutation_sim_time` / any `updated_at` | **Not emitted.** A past-horizon mutation time is not faithfully reconstructible (untracked writes advance it leaving no history), so base omits the column rather than fabricate or understate it — the same deviation source's snapshot delivery makes. `STATE_AT_COLUMNS` already excludes it |

### The `slice_only` posture (reused, not invented)

Base auto-projects a kind's full property set (there are no author-named column
reads in a flat projection), so it enforces the export-wide `slice_only`
invariant by **omission with a notice** — the source-style enforcement, chosen
because the column was never asked for. This reuses the decided policy in full;
base picks the enforcement *shape* its authoring model dictates, and asserts no
new temporal fiction.

| Condition | Result |
|---|---|
| A records `prop__<p>` with `temporal_class: slice_only`, not the exempt discriminator | Omitted from the flat table; one `slice-only-column-omitted` notice per kind × column |
| The exempt discriminator `prop__<K>_type` with `subtype_values(K)` non-empty, any class | Carried and renameable (the mechanical carve-out), classification-only |
| A `rename` entry naming an omitted `slice_only` column | Config error (parallel to source's `SourceRenameSliceOnly`) |
| Every property of a kind is non-exempt `slice_only` | The table still renders — identity + lifecycle columns and any exempt discriminator; omission never suppresses a table (column-projection-only invariance) |

This is the identical posture playback's shaped state and source's snapshot
delivery already face and resolve: at a past horizon a tracked property carries
its as-of value, a constant property carries its current value, and a
`slice_only` property — whose past is unknowable — is never presented as an
as-of-T value. Base is a new surface for one already-decided rule.

### Presentation and ordering

- **Operational presentation-name posture.** Output table names are the
  prefix-stripped kind (`records__customer` → `customer`) and `record_id` →
  `id`, matching source's defaults. Both are overridable via `rename`: a `name`
  overrides the table name, a `columns` entry overrides a column keyed on the
  state-at column identity (`record_id`, `presentation_id`, `created_sim_time`,
  `active`, `deactivated_at`, `prop__<p>` — the pre-default identity, so
  `record_id`, not `id`). A name collision **fails fast** — base emits one table
  per kind and never combines kinds into one table. Author-verbatim `name`s win.
- **Wallclock rendering is optional.** When the effective anchor resolves
  (`rebase` / `--base-date` / sidecar `runtime`), lifecycle timestamps render
  through the shared `render_anchor_timestamp_expr`; absent an anchor, base
  emits raw `sim_time` `BIGINT` columns — the dimensional-style raw-ns fallback.
  Unlike source, **base does not require an anchor.**
- **Column typing.** Data columns (`prop__<p>`, `presentation_id`) are cast back
  to their declared sidecar types — the state-at resident's codec-VARCHAR
  after-image is reconstituted to the column's type, exactly as source's snapshot
  delivery. Base delivers a typed table, not an all-string one. Lifecycle
  timestamps follow the wallclock / raw-ns rule above; `active` is boolean.
- **Ordering** is the state-at resident's declared `(created_sim_time,
  record_id)`, over raw ns keys — never rendered timestamps.

### Point-in-time subsumption (the position this doc takes)

**Base mode subsumes the Stage-5 point-in-time / ML-feature-store export.** A
`mode: base` export with `slice_at: T` is exactly "replay `history` to sim-time
T → one flat row per record": the feature-store row shape. There is no residual
capability the Stage-5 point-in-time bullet named that `slice_at` plus the
existing anchor and incremental surfaces do not already deliver, so it is
retired as a standalone item rather than built separately.

**Base does not subsume the Stage-5 membership / queue-state export.** That
prong reads `membership__*`, derives a different grain (wait time, FIFO /
priority order), and composes the membership-state-at resident — not per-record
reconstitution. It is orthogonal to base's records-only flat projection and
remains a separate future item.

The build-order note's framing ("base likely subsumes Stage-5 point-in-time
export through the playback surface") is honored with one refinement: base
subsumes it through the **state-at derivation** the playback surface itself
composes, reached directly by horizon rather than through the playback API.

### Relationship to the truncated tape (why direct-horizon is equivalent)

The playback seam calls base "a thin renderer over shaped state," where shaped
state runs a mode's full-export compile over the truncated tape at T. For base
that equivalence collapses to an identity: base's full-export compile of a kind
*is* the state-at relation, and the shipped bridging theorem states state-at at
horizon `T + 1` equals the base-shape compile over the tape truncated at T.
Base therefore realizes point-in-time by the simpler of the two equal paths —
passing a horizon to the state-at resident — and never needs the
compile-indirection (`base_relations`) wrapping, which exists to give
*multi-table* shapes (SCD-2 `LEAD`, fk hops, membership grains) one consistent
truncated world. Base has no such structure to keep consistent.

### Corrupter composition and totality

A base export over a corrupted emit surfaces the corrupter's declared defects
unchanged, never manufacturing new ones (Principle #3), and never special-cased.
Base casts each data column back to its sidecar type (§ Presentation, "Column
typing") — exactly as source's snapshot delivery does — so totality rests on the
corrupter family's value transforms being **type-preserving**: a corrupted
`history.value` remains a valid instance of its column's declared type, so the
cast-back succeeds and the defect surfaces *in* the reconstructed value rather than
dropping or erroring a row. No row a semantic defect made weird is dropped.

## Configuration

```yaml
mode: base

base:                      # optional; omit entirely for a bare current-state dump
  slice_at: 50             # optional point-in-time horizon (sim-time ns, inclusive)
  exclude:
    kinds: [audit_log]     # records kinds dropped before export
    # tables: [customer]   # optional; base output table names (non-empty when present)
  rename:
    - table: records__customer   # matched on the sidecar base-table name
      name: dim_customer_current

rebase:                    # optional; raw ns when omitted
  base_date: 2020-01-01
  timezone: America/New_York

# incremental is an ALTERNATIVE to base.slice_at above — the two are mutually
# exclusive (§ Validation Rules), so a real config sets at most one:
# incremental:
#   period: day            # per-window snapshots instead of a single pinned horizon
```

| Field | Type | Required | Description |
|---|---|---|---|
| `mode` | `Literal["dimensional","source","base"]` | Yes | `base` selects this mode |
| `base` | `BaseConfig \| None` | No | Escape hatches + optional slice; omit for a bare current-state full dump |
| `base.slice_at` | `int \| None` | No | Inclusive point-in-time horizon (sim-time ns); `≥ 0`. Absent → tape's end. Forbidden with `incremental` |
| `base.exclude` | `ExcludeDecl \| None` | No | `kinds` (records kinds) / `tables` (base output table names, per the reused model) dropped before export |
| `base.rename` | `list[RenameEntry] \| None` | No | Per-table (`name`) and per-column (`columns`) output overrides (reused model). Each entry is matched on the sidecar `records__<kind>` name (`table`, keyed by sidecar identity); `columns` keys are state-at column identities. `sub_type` is rejected (base never splits a kind); `table` targets must be disjoint |
| `rebase` | `RebaseConfig \| None` | No | Shared effective-anchor knobs; raw ns when absent (not required) |
| `incremental` | `IncrementalConfig \| None` | No | Per-window snapshot cadence; forbidden with `base.slice_at` |

## Interface Contracts

### Config Models

```python
class BaseConfig(StrictBaseModel):
    """The base-mode section: presentation escape hatches plus an optional
    point-in-time slice. Omit the whole section for a bare current-state dump."""

    exclude: ExcludeDecl | None = None
    """Kinds and output tables dropped before export. `kinds` names records kinds;
    `tables` names base's output table names (the reused model's semantics), which
    `BaseExcludeResolvable` checks against the surviving output set."""
    rename: list[RenameEntry] | None = None
    """Per-table (`name`) and per-column (`columns`) output overrides. Each entry is
    matched on the sidecar `records__<kind>` name (`table`, keyed by sidecar
    identity, as source does); `columns` keys are state-at column identities
    (`record_id`, `presentation_id`, `created_sim_time`, `active`, `deactivated_at`,
    `prop__<p>`). `sub_type` is not applicable — base never splits a kind — and is
    rejected. `table` targets must be disjoint."""
    slice_at: int | None = None
    """Inclusive point-in-time horizon in sim-time ns. Absent renders each kind
    at the tape's end (current state). Mutually exclusive with an incremental
    block (enforced on ExportConfig)."""

    @model_validator(mode="after")
    def at_least_one_field(self) -> Self:
        """A present `base` section sets at least one of exclude / rename / slice_at.

        Raises:
            ValueError: No field was explicitly set (an empty `base: {}` block is
                not meaningful; omit the section for a bare current-state dump).
        """

    @model_validator(mode="after")
    def slice_at_non_negative(self) -> Self:
        """`slice_at`, when set, is a non-negative sim-time ns.

        Raises:
            ValueError: `slice_at` is negative.
        """

    @model_validator(mode="after")
    def rename_no_sub_type(self) -> Self:
        """No base rename entry sets `sub_type` — base emits one table per kind and
        never splits, so a split-unit selector is meaningless.

        Raises:
            ValueError: A rename entry sets `sub_type`.
        """

    @model_validator(mode="after")
    def entries_disjoint(self) -> Self:
        """No two rename entries share the same `table` target — base has one output
        table per kind, so `table` alone is the key.

        Raises:
            ValueError: Two rename entries target the same table.
        """
```

`ExportConfig` changes (described, not re-listed): `mode` becomes
`Literal["dimensional", "source", "base"]`; a `base: BaseConfig | None = None`
field is added; the `mode_section_matches` validator gains a `base` arm —
`mode='base'` forbids the `dimensional` and `source` sections and requires no
`base` section (a bare `mode: base` is a valid full dump). A new
`base_slice_at_excludes_incremental` cross-field validator rejects a config that
sets both `base.slice_at` and `incremental`.

### Plan Models

```python
@dataclass(frozen=True)
class BaseTableSpec:
    """One surviving records kind's resolved flat-output shape — time-agnostic.

    Everything the render step needs except the horizon: the source kind, its
    output table name, the bare property names to reconstruct (post `slice_only`
    omission, discriminator carve-out retained), whether the kind carries a
    presentation_id, and the resolved column-rename map."""

    kind: str
    """The records kind (the `records__<kind>` suffix)."""
    table_name: str
    """The output table name after presentation defaults and `rename`."""
    properties: frozenset[str]
    """Bare property names to reconstruct, passed straight to the state-at builder;
    `slice_only` omissions already removed, an exempt discriminator retained."""
    has_presentation_id: bool
    """Whether the kind carries presentation_id. The state-at builder decides this
    itself from the sidecar (it takes no presentation flag); base keeps the bit to
    drive its own presentation-name projection — whether to project and `rename` a
    `presentation_id` column in the wrapper."""
    column_renames: Mapping[str, str]
    """State-at column identity → output name; includes the `record_id → id`
    default unless a `rename` entry overrides it."""


@dataclass(frozen=True)
class BasePlan:
    """The time-agnostic base plan: one `BaseTableSpec` per surviving kind, in
    deterministic sidecar kind-declaration order. Identical for a full, a sliced,
    and a windowed export — the horizon is supplied at render, never here."""

    tables: tuple[BaseTableSpec, ...]
```

### Functions

```python
def build_base_plan(
    sidecar: Sidecar,
    config: BaseConfig | None,
    notice_sink: NoticeSink,
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
        ExportError: A `rename` entry names an omitted `slice_only` column, an
            unresolvable table/column, or a presentation-name collision.
        TableNotFoundError: A declared `records__<kind>` table is absent.
    """
```

```python
def build_base_render_sql(
    sidecar: Sidecar,
    fork_path: str,
    spec: BaseTableSpec,
    anchor: EffectiveAnchor | None,
    horizon_ns: int | None,
) -> str:
    """Render one `BaseTableSpec` to a complete, deterministic SELECT at a horizon.

    Base's counterpart to source's `build_snapshot_render_sql`. Composes the shipped
    state-at derivation verbatim — `build_state_at_end_sql(sidecar, fork_path,
    spec.kind, spec.properties)` when `horizon_ns is None` (the structural tape's
    end, current state), `build_state_at_sql(sidecar, fork_path, spec.kind,
    spec.properties, horizon_ns)` otherwise — then wraps the raw relation with base's
    own presentation: the lifecycle timestamps `created_sim_time` and
    `deactivated_at` render wallclock through `render_anchor_timestamp_expr` when
    `anchor` is set and stay raw sim-time `BIGINT` otherwise; `prop__<p>` and
    `presentation_id` cast back from the state-at codec VARCHAR to their sidecar
    types (as source's snapshot render does); every column is projected under
    `spec.column_renames` (including `record_id → id`). Never uses the
    compile-indirection (`base_relations`) wrapping. Ordered by the state-at
    resident's `(created_sim_time, record_id)` over raw ns.

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


def build_base_query_specs(
    emit: Emit,
    config: ExportConfig,
    anchor: EffectiveAnchor | None,
    window: Window | None,
    notice_sink: NoticeSink,
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

The state-at signatures these compose are unchanged and are not redefined here.

## Validation Rules

### Parse-Time (Pydantic)

```python
@model_validator(mode="after")
def at_least_one_field(self) -> Self:
    """BaseConfig: reject an empty `base: {}` block."""

@model_validator(mode="after")
def slice_at_non_negative(self) -> Self:
    """BaseConfig: `slice_at` ≥ 0 when set."""

@model_validator(mode="after")
def rename_no_sub_type(self) -> Self:
    """BaseConfig: reject a `rename` entry that sets `sub_type` — base never splits
    a kind, so a split-unit selector is meaningless."""

@model_validator(mode="after")
def mode_section_matches(self) -> Self:
    """ExportConfig: `mode='base'` forbids `dimensional`/`source` sections; the
    `base` section is optional."""

@model_validator(mode="after")
def base_slice_at_excludes_incremental(self) -> Self:
    """ExportConfig: reject `base.slice_at` together with an `incremental` block —
    a single pinned instant and a window sequence are contradictory temporal
    selectors."""
```

### Business Rules

| Rule | Checks | Error Message |
|---|---|---|
| `BaseRenameSliceOnly` | A `base.rename` entry names a column omitted by the `slice_only` policy | `"rename targets column {column!r} on table {table!r}, which is omitted by the slice_only policy"` |
| `BaseRenameResolvable` | Every `rename` `table` target is a surviving `records__<kind>` sidecar table (matched before prefix-stripping, disjoint per `entries_disjoint`) | `"rename targets table {table!r}, which is not a records kind base emits"` |
| `BaseNameCollision` | No two output tables (after rename + presentation defaults) share a name | `"output table name {name!r} is produced by two kinds"` |
| `BaseExcludeResolvable` | Every `exclude.kinds` entry names a real records kind; every `exclude.tables` entry names a surviving base output table | `"exclude names {name!r}, which base does not emit"` |

`slice_only` omission itself is not a business-rule error — it is a notice
(`slice-only-column-omitted`), emitted per surviving kind × omitted column,
before any data is written.

## Invariants

1. **Records-only flat grain.** Base emits exactly one flat table per surviving
   records kind and nothing else — no membership, junction, fact, or CDC table.
2. **State-at is the whole engine.** Every base table value is a state-at
   reconstruction at some horizon (tape's end, `T + 1`, or a window end); base
   writes no independent point-in-time path.
3. **One inclusive horizon per full export.** `slice_at: T` reflects every event
   with `sim_time ≤ T` and nothing after; the exclusive state-at horizon is
   `T + 1`. Current-state uses the structural end-of-tape entry point, never a
   horizon cleared against `history` alone.
4. **`slice_only` enforcement is omit-with-notice, carve-out honored.** Base
   inherits the export-wide invariant and chooses omission; the discriminator
   carve-out (`name == prop__<K>_type ∧ subtype_values(K) ≠ ∅`) is honored;
   omission is column-projection-only and never suppresses a table.
5. **Faithful reshaping.** Every value traces to a base-layer value or a
   deterministic recoding (a cast, a horizon mask, a wallclock render); base
   fabricates nothing, and a corrupted emit surfaces its declared defects
   unchanged.
6. **Anchor optional, single, shared.** Base renders wallclock through the one
   resolved effective anchor or emits raw ns; it never resolves a second anchor
   and never requires one.
7. **Determinism.** Same emit + export config + code version → identical output,
   including the notice sequence.
8. **`slice_at` ⊕ `incremental`.** A base config carries at most one temporal
   selector; the two together are a load-time error.

## Rationale

- **Direct-horizon over compile-indirection.** Base's shape *is* state-at, so
  the truncated-tape wrapping the playback seam needs for multi-table shapes
  buys base nothing; the bridging theorem makes the direct horizon provably
  equal and strictly simpler.
- **Omit, not refuse, for `slice_only`.** Base auto-projects — the author names
  no column reads — so an omission-with-notice matches the authoring model
  exactly as it does for source; refusing would demand an author-named read that
  base's flat projection never has.
- **Subsume point-in-time, not queue-state.** The feature-store row *is* the
  as-of-T flat record; folding it into `slice_at` removes a redundant Stage-5
  build. Queue-state is a genuinely different grain and derivation and is
  correctly left separate — collapsing it into base would fabricate a coupling
  that isn't there (Principle #8).
- **No anchor requirement.** Base's teaching target is incremental ETL / SCD
  merge, where raw sim-time keys are a legitimate and common landing shape;
  requiring wallclock (as source does) would foreclose that lesson.

## Related

| Document | Why |
|---|---|
| [`derivations.md`](../derivations.md) | The state-at / end-of-tape residents base composes as its whole engine |
| [`playback.md`](../playback.md) | Shaped state and the bridging theorem that make direct-horizon equivalent |
| [`slice-only.md`](../slice-only.md) · [`notices.md`](../notices.md) | The reused omission policy and the channel its notices flow through |
| [`source.md`](../source.md) | Snapshot delivery (the same state-at composition), presentation-name posture, and `slice_only` omission shape base reuses |
| [`anchor.md`](../anchor.md) · [`incremental.md`](../incremental.md) | The shared wallclock and window surfaces base wires into |
| [`../../contract/base-format.md`](../../contract/base-format.md) | `temporal_class`, the MUST-NOT-present-as-of-T clause, and the records/`history` shapes |
