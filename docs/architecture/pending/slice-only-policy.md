---
status: draft
---

# The `slice_only` export policy and the notice channel

## Problem

The contract classifies every records-category `prop__` column into a three-way
`temporal_class` — `constant` / `tracked` / `slice_only` — and forge reads and
verifies the classification (the reader's narrowing accessor, C13). But the class
drives no policy. A `slice_only` column carries only the branch's `slice_at` value
with no history behind it — the contract's own words: its past is **unknowable**, and
"a consumer MUST NOT present a `slice_only` column as an as-of-T value". Every
exporter today does exactly that:

- **source** projects `slice_only` columns into every genre — including snapshot
  delivery, which stamps the slice value at window horizons the emit cannot speak
  to. The mode's genre predicate asks the class only one question — is any
  property `tracked`? — which a `slice_only` column never flips, but its
  renders carry the column like any other.
- **dimensional** is worse than silent: the `lookup` gate keys on
  `history_tracked: false`, which *admits* `slice_only` (mutable-but-untracked) —
  stamping a slice value onto a past interval anchor is precisely the fabrication
  the gate exists to refuse. The window-gated incremental rules share the blind
  spot: `IncrementalFkMutableHop` and its siblings read the same bit, so a
  `slice_only` hop or slice-read column passes as if it were constant.
- **streaming** carries a `slice_only` property at its current records-table
  value on every `state-changes` after-image whenever the author selects it in
  `kinds[].properties` — nothing refuses the selection.
- An author who writes

  ```yaml
  columns:
    - name: tier_at_admission
      from: prop__loyalty_tier        # temporal_class: slice_only
  ```

  on a `history_interval` grain gets a column whose name promises an as-of value
  and whose content is the end-of-run slice — silently.

Two structural gaps block the fix:

1. **No informational channel.** The exporters are raise-or-silent; the one
   informational emission in the package (`DiscriminatorValueObserved`) goes
   through a bare `warnings.warn`. An omission policy must tell the author *what
   was omitted and why* — off stdout, which data delivery owns (`init` prints
   its candidate YAML there), and deterministically, so it is testable as data.
2. **The playback seam presupposes the posture.** The pending playback design's
   invariants assume no export carries a `slice_only` value and that each mode's
   own validation refuses one before any export runs. This feature is what makes
   that assumption true; it lands before playback implementation.

## Solution

An **export-wide posture** — no exporter output value, row membership, linkage, or
ordering derives from a `slice_only` column's value — enforced
surface-appropriately, plus the structured notice channel that makes silent
omission honest:

- **Author-named → refuse at validation.** Any config-referenced value-read that
  resolves to a `slice_only` column is an error in the always-on business-rule
  pass — every export, not only windowed ones.
- **Auto-projected → omit, with a notice.** source — the one mode that chooses
  its own projections — drops `slice_only` columns from every render and emits
  one typed notice per omission. `init` never proposes one (and notices each
  skip). Streaming has no auto-projection: its after-image carries exactly the
  author's `kinds[].properties`, so streaming is refuse-only.
- **One carve-out: the sub-typed discriminator.** A sub-typed kind's
  `prop__<kind>_type` is exempt on every surface — carried, selectable,
  filterable, and lookup-projectable *as a classification* (current value at
  every T), never as an as-of value.
- **`lookup` regates to `temporal_class: constant`** along its entire path.
- **Notices are data.** A frozen `Notice` record delivered through a caller-supplied
  sink; the CLI renders each to stderr. Deterministic sequence, never affects
  output data or exit code. The existing `warnings.warn` migrates onto it.

```
                      config-referenced read of slice_only?
                            │yes                │no
                            ▼                   ▼
                      ExportError        auto-projection surface?
                    (always-on rule)          │yes         │no
                                              ▼            ▼
                              omit + Notice("slice-only-   project
                                column-omitted") → stderr  normally
                      exempt everywhere: prop__<kind>_type
                      when subtype_values(kind) is non-empty
```

## Affected Subsystems

- **Dimensional exporter** — gains the always-on `SliceOnlyColumnRefused` rule
  over every config-referenced source-column resolution; `LookupColumnSafety`
  re-keys from `history_tracked: false` to `temporal_class: constant` (the
  exempt discriminator excepted);
  `DiscriminatorValueObserved` migrates from `warnings.warn` to the notice
  channel; `init` skips `slice_only` columns from column proposals and notices
  each skip. Compile and export entry points accept a notice sink.
- **Source exporter** — every records-genre render (change-log after-image,
  reference, transaction, snapshot) narrows its payload set to
  `tracked` + `constant` columns plus the exempt discriminator; one notice per
  omitted column per export unit; a `rename` columns key naming an omitted column
  becomes an error. The genre predicate is untouched (it never consulted
  `slice_only`). Plan and export entry points accept a notice sink.
- **Streaming exporter** — refuse-only. The `state-changes` after-image carries
  exactly the author's `kinds[].properties`, so there is no auto-projection to
  narrow; a `kinds[].properties` entry naming a non-exempt `slice_only`
  property becomes a validation error in the eager pass, before any event is
  delivered. Streaming emits no notices and its entry-point signatures are
  unchanged. `membership-events` is untouched (membership columns carry no
  class).
- **The mode-neutral exporter surface** — a new `Notice` record and `NoticeSink`
  contract, sibling of the shared compiled-table shape and the reserved-name
  check; the one channel every informational emission flows through.
- **CLI** — supplies the stderr notice renderer as the sink for `export` and
  `init`; notice lines are rendered as they are emitted, before data delivery
  begins for plan-time notices.
- **Incremental driver** — `export_window` and `export_incremental_next` gain
  the same required `notice_sink` parameter and thread it to the mode compile.
  Every driver invocation compiles exactly once (an explicit `--from`/`--to`
  range is a single range-window; a `--next` drip derives one window), so the
  sink threads through with no forwarding or dedup logic; window-gated rules
  themselves are untouched.

## What Doesn't Change

- **The reader.** `Sidecar.temporal_class`, `Sidecar.subtype_values`, the
  records-column taxonomy, and every other accessor are sufficient as they stand;
  no new reader surface.
- **The derivations layer.** Every fold keeps its signature and row semantics.
  The folds stay class-agnostic: gating is selection-side (which columns a mode
  passes) and mode-side (validation), never inside a fold. Narrowing a fold's
  input by class would change event row sets; this design never does.
- **Source's genre trichotomy.** The tracked-kind predicate already never
  consults a `slice_only` column; classification outcomes are identical before
  and after.
- **The window-gated incremental rules.** No re-keying. With `slice_only` reads
  refused always-on before the gates run, every `history_tracked: false` column
  that survives to a window gate is either `constant` or the exempt
  discriminator — and the discriminator's admission is the carve-out working as
  intended: a classification carried at its current value, which the gates'
  existing bit-keyed predicates already permit. (A `tracked` discriminator in a
  windowed filter or slice column remains refused by the shipped gates'
  `history_tracked` keying — pre-existing behavior this design leaves
  unchanged.)
- **`updated_at` and `last_mutation_sim_time`.** Structural lifecycle columns
  carry no `temporal_class`; they are outside the policy population by
  construction. Source's `updated_at` rendering, the snapshot render's `updated_at`
  omission, and records-grain window keying are byte-unchanged. The previously
  sketched "`updated_at` narrowing for fully-traced kinds" is dropped: the
  playback seam owns `last_mutation_sim_time` presentation (reserved output name,
  recorded-trail substitution), and a mode-side narrowing would duplicate or
  contradict that contract.
- **Presentation-property columns.** The contract guarantees they are never
  `slice_only`; the policy relies on this and never special-cases them.
- **Membership tables, `history`, identity and lifecycle columns.** Classless by
  contract; untouched.
- **Corrupters.** They write base-shaped output; the policy is an exporter
  concern. A corrupted emit flows through exporters under the same policy as a
  clean one.
- **Exit codes, CLI flags, config grammar.** No new flags; no new YAML fields.
  The policy is contract-mandated ("MUST NOT") and deliberately not
  author-configurable — there is no opt-out knob.
- **Kafka sink, pacing, mixer, writers, anchor resolution.**

## Semantics

### The policy population

The sweep predicate is `temporal_class == "slice_only"`, read through the sidecar's
narrowing accessor (verbatim carry, never inferred). The contract pins which
columns can carry it:

| Column class | Carries `temporal_class`? | Can be `slice_only`? |
|---|---|---|
| Records-category `prop__<name>` | yes | **yes — the entire policy population** |
| Presentation-property column | yes | never (contract guarantee) |
| `presentation_id`, identity columns (`record_id`, `fork_path`, `record_index`, `ref_index__*`) | no | no |
| Lifecycle columns (`created_sim_time`, `active`, `deactivated_at`, `last_mutation_sim_time`) | no | no |
| `history` columns, membership-table columns | no | no |

A column without the pair is never consulted by the sweep. A column carrying
`history_tracked` but no usable class raises the reader's
`TemporalClassUnavailableError` (the emit is non-conformant; C13's finding) — the
same refuse-when-unverifiable stance the source predicate already takes.

### The read taxonomy

| Read kind | Definition | Policy |
|---|---|---|
| **Value-read** | An output value, join resolution, row membership, or row order derives from the column's value: projection (`from`, `correlation`, auto-projection, after-image), `lookup` terminal or hop, `fk via: reference` hop, `fk via: membership` `member_path` hop or `as_of` column, records `filter` key, `value_map.from` source, `derived: timestamp` `source`, `derived: elapsed` `correlate_on` / `start_source` / `end_source` / `other_where` key | Refused (author-named) or omitted (auto-projected) |
| **Metadata read** | The engine reads the column's *class* from the sidecar (the sweep itself, the genre predicate, `init`'s skip) | Always permitted — no value is touched |
| **Classification read** | The sub-typed discriminator's current value used to classify rows, on any surface — projection, filter, `lookup` path included (see the carve-out) | Permitted — the one value-read exception |

### The discriminator carve-out

**Predicate:** a column of `records__<K>` is exempt iff its name is
`prop__<K>_type` **and** `Sidecar.subtype_values(K)` is non-empty. Mechanical —
never a judgment about a column's usefulness. `subtype_values` (the
`enum_domains`-sourced oracle the source and streaming splits already read) is the
authority; the registry's object-vs-string shape plays no role in the exemption.

The exemption is structural, not temporal. A `records__<K>` table is a wide
union of sub-kinds; `prop__<K>_type` is the tag that says what each row
actually *is* — the convenience that spares the producer one table per
sub-kind. Restricting it on any surface would strip the one classification key
every consumer groups, routes, and splits by. The carve-out therefore spans
every policing surface — the sweep, the source omission, the streaming refusal,
`init`'s skip, and the `lookup` regate alike.

The contract does not pin a discriminator's `temporal_class`; a producer may mark
it `slice_only`. An unexempted sweep would then strip the classification key every
surface groups by — the source sub-type split, streaming routing and `types`
selection, BI grouping downstream. The exempt discriminator is carried and
selectable *as a classification* — the current value at every T — never presented
as an as-of value.

| Condition | Result |
|---|---|
| `prop__<K>_type`, `subtype_values(K)` non-empty, any class | Exempt: projected, filterable, renameable, proposable, and permitted on a `lookup` path — existing sub-type-split retain/drop/strip rules apply unchanged |
| `prop__<K>_type`, `subtype_values(K)` empty, class `slice_only` | Not exempt: omitted / refused like any `slice_only` column |
| Any other `prop__` column, class `slice_only` | Not exempt |

### Per-surface behavior

| Surface | `slice_only` column (non-exempt) | Behavior |
|---|---|---|
| Dimensional `from` / `correlation` | author-named | `SliceOnlyColumnRefused` at `build_query_specs`, always-on |
| Dimensional records `filter` key | author-named | `SliceOnlyColumnRefused` (a filter derives row membership from the value) |
| Dimensional `value_map.from` resolved source | author-named | `SliceOnlyColumnRefused` |
| Dimensional `fk via: reference` hop column | author-named (via `to`/`path`) | `SliceOnlyColumnRefused` (linkage derives from the value) |
| Dimensional `fk via: membership` `member_path` hop or `as_of` column | author-named | `SliceOnlyColumnRefused` (member identity and resolution time derive from the values) |
| Dimensional `derived: timestamp` `source` | author-named | `SliceOnlyColumnRefused` (a `prop__<t>` source stamps the slice value into every row's timestamp) |
| Dimensional `derived: elapsed` `correlate_on` / `start_source` / `end_source` / `other_where` key | author-named | `SliceOnlyColumnRefused` (the delta and the counterpart-row linkage derive from the values — `correlate_on` *is* the correlation key that links the counterpart rows) |
| Dimensional `source.where` / fk membership `where` / `member_field`; `source.property` / `value` | membership / history scoping | Outside the population by construction — membership element columns carry no class; a history grain's rows exist only for tracked properties |
| Dimensional `lookup` terminal or hop | author-named | `LookupColumnSafety` (constant-only regate, below; the exempt discriminator passes, any class) |
| Dimensional `ordinal` / `key` / `scd_window` | name sibling *output* columns | No new check — their sources are already checked at projection |
| `init` column proposal | auto | Skipped; one `slice-only-column-omitted` notice per skip |
| `init` fact fan-out / `filter` pre-fill on `prop__<kind>_type` | classification read | Unchanged (exempt) |
| Source change-log after-image (`changelog` delivery) | auto | Omitted from the fold's projected property set; one notice per column |
| Source snapshot render (`change_delivery: snapshot`) | auto | Omitted from the state-at projection; one notice per column |
| Source reference / transaction render | auto | Omitted; one notice per column per unit |
| Source junction render | n/a | Membership columns carry no class; untouched |
| Source `rename` columns key naming an omitted column | author-named | `SourceRenameSliceOnly` — the column is not exported, so the rename is unsatisfiable; error, not a silent ignore |
| Source `exclude` | kind/table-level only | No interplay — `exclude` cannot name a column |
| Streaming `state-changes` after-image | author-named — its columns are exactly `kinds[].properties` | No auto-projection to narrow; a `slice_only` selection never survives the eager pass (next row) |
| Streaming `kinds[].properties` entry | author-named | `StreamPropertySliceOnly` in the eager pass |
| Streaming `types`, routing Layer A, `sub_type` resolution | classification read | Unchanged (exempt) |
| Streaming `membership-events` | n/a | Untouched |

Omission (source) and refusal (streaming) narrow the modes' declared
temporal-honesty exception into honesty: where untracked columns previously
"rode the after-image at their current records-table value", the surviving
riders are exactly `constant` — values the contract declares valid at every T.

### Column-projection-only invariance

Omission never changes a row set. The row-state-events fold's `c`/`u`/`d` rows are
keyed on creation, tracked-property change instants, and deactivation; a
`slice_only` column is by definition untracked and contributes no rows. Narrowing
the property set a mode passes to a fold therefore removes after-image *columns*
only:

| Quantity | Before → after |
|---|---|
| Event row set, per kind | identical |
| Global `seq` assignment | identical |
| Window membership (incremental, per genre) | identical |
| After-image / rendered column set | minus non-exempt `slice_only` columns |

The degenerate case follows the same rule. A unit whose every property is
non-exempt `slice_only` still renders — rows intact, carrying its classless
columns (identity, lifecycle, `presentation_id` when present) and the exempt
discriminator when present — with one notice per omitted column as usual.
Likewise `init`'s skip is column-level: it never removes a kind from proposal,
only its `slice_only` columns from the proposal's column list. Omission never
suppresses an export unit or a proposal target.

### The `lookup` regate

`LookupColumnSafety` re-keys from `history_tracked: false` to
`temporal_class: constant` — terminal property and every traversed hop column,
the exempt discriminator excepted:

| Path class (terminal or any hop) | Result |
|---|---|
| All `constant` | Allowed — the value is contract-valid at every T; projection is exact at any interval |
| Exempt discriminator on the path, any class | Allowed — a classification read (the carve-out): the row's type tag at its current value, never presented as as-of. This deliberately admits a `tracked` discriminator terminal that the shipped `history_tracked` keying refused — the one loosening the regate introduces |
| Any other `tracked` | Refused — a capability boundary: the slice value is the *final* value, and an as-of reconstruction is not a `lookup`; the error names the tracked column |
| Any other `slice_only` | Refused — permanent: the value at the row's interval is unknowable; the error names the column and the class |
| Pair unavailable on a consulted column | Refused via the reader's `TemporalClassUnavailableError` — unverifiable is refused, never inferred |

(A discriminator can appear on a `lookup` path only as the terminal property —
hop columns carry references, and `prop__<K>_type` is an enum classification —
but the exemption predicate is applied per consulted column, mechanically, with
no terminal-vs-hop special case.)

`Scd2NeedsHistory` keeps its `history_tracked` keying: it asks the SCD-class
question ("does this kind have priors in `history`?"), which is exactly what the
bit answers. Only point-in-time safety re-keys to the class.

### Notice semantics

| Property | Rule |
|---|---|
| Determinism | Same emit + config + code version → identical notice sequence (content and order). Notices follow plan iteration order |
| Severity | Informational only: a notice never changes output data, table sets, or the exit code |
| Channel | Delivered synchronously to the caller-supplied `NoticeSink` as discovered; the CLI's sink writes one line per notice to stderr. stdout is never touched — it is data delivery's channel (`init` prints its candidate YAML there) |
| Timing | Plan-time notices (all of this design's codes) are emitted before any data is written or streamed |
| Incremental | Every driver invocation compiles exactly once — an explicit `--from`/`--to` range is a single range-window; a `--next` drip derives one window — so the sink threads through with no forwarding or dedup logic. A `--next` drip re-emits its compile's notices each invocation |
| Errors vs notices | Anything knowable as wrong at validation time is an error, never a notice. Notices report policy outcomes the author did not ask about |
| Shape | `code` + fully rendered `message` only — no structured subject fields. Determinism makes the message itself assertable data: tests key on `code`, and on the verbatim `message` where the subject (table, column, unit) matters. Structured fields wait for a consumer that needs them (Principle #8) |

Notice codes introduced:

| `code` | Emitted by | Meaning |
|---|---|---|
| `slice-only-column-omitted` | source plan (per unit × column), `init` (per kind × column) | A `slice_only` column was dropped from an auto-projected surface |
| `discriminator-value-unobserved` | dimensional validation (migrated `DiscriminatorValueObserved`) | A records `filter` value is not among the kind's observed `enum_domains` values; the table will be empty |

### Invariants

Introduced:

1. **The posture.** No exporter output value, row membership, linkage, or
   ordering derives from a `slice_only` column's value; the sole exception is the
   sub-typed discriminator classification read, honored on every surface — the
   `lookup` regate included. Future modes inherit this
   invariant; a new mode decides *how* to enforce it (refuse vs omit per its
   authoring model), never *whether*.
2. **Refusal is always-on.** The `slice_only` rules run in the always-on
   business-rule pass, full export included — never window-gated.
3. **Omission is column-projection-only.** Event row sets, `seq`, and window
   membership are invariant under the policy.
4. **Notices are deterministic data, off stdout, and non-fatal.**
5. **The carve-out is mechanical and surface-total.** Exemption is exactly
   `name == prop__<K>_type ∧ subtype_values(K) ≠ ∅` — no other column, no other
   condition, and no surface applies a narrower predicate.

Relied upon:

- The contract's coverage guarantee: every records-category `prop__` column of a
  supported-version emit carries the temporal pair; only such columns can be
  `slice_only`; presentation columns never are.
- The version gate: the policy never runs against an emit whose contract predates
  the classification.
- `subtype_values` is total and never raises (empty tuple for a non-sub-typed
  kind).
- The row-state-events and state-at folds accept a caller-chosen property set and
  key their row sets on tracked-property change instants only.

## Configuration

None. The policy is contract-mandated ("a consumer MUST NOT present a
`slice_only` column as an as-of-T value") and deliberately not
author-configurable: no new YAML fields, no opt-out. Author-facing surface
changes are behavioral only — refusals where a config names a `slice_only`
column, stderr notices where a mode omits one.

## Interface Contracts

### Runtime Types

```python
@dataclass(frozen=True)
class Notice:
    """One informational, non-fatal fact about an export plan.

    Deterministic: the same emit, config, and code version produce the same
    notice sequence. A notice never alters output data or the exit code.
    """

    code: str
    """Stable machine-readable identifier (kebab-case, e.g.
    'slice-only-column-omitted'). Test assertions key on it."""

    message: str
    """Fully rendered human-readable text naming the concrete subject
    (table, column, unit) — self-contained, no interpolation left."""
```

```python
NoticeSink = Callable[[Notice], None]
"""Receiver for notices, called synchronously as each notice is discovered.

The CLI passes a stderr renderer; tests pass a list-appender; a library
caller passes whatever it likes. Never None — a caller that wants silence
passes a discarding sink."""
```

### Functions — the notice channel

```python
def render_notice_stderr(notice: Notice) -> None:
    """
    Write one notice line to stderr: ``notice: {message}``.

    The CLI's NoticeSink for every verb that compiles an export plan.

    Args:
        notice: The notice to render.

    Returns:
        None.

    Raises:
        Nothing.
    """
```

### Functions — changed entry points

Every entry point that can emit a notice gains a required ``notice_sink``
parameter (no default — Principle #7's no-silent-fallback posture applied to an
output channel). Streaming emits none — it is refuse-only — so its entry
points keep their signatures; `iter_stream_events` appears below for its
behavior change alone. Signatures below show the changed contract; unchanged
parameters keep their existing semantics.

```python
def build_query_specs(
    emit: Emit,
    config: DimensionalConfig,
    anchor: EffectiveAnchor | None,
    window: Window | None,
    notice_sink: NoticeSink,
) -> list[QuerySpec]:
    """
    Compile the dimensional plan; run the always-on business rules.

    New behavior: SliceOnlyColumnRefused runs always-on over every
    config-referenced source-column resolution; LookupColumnSafety keys on
    temporal_class: constant (exempt discriminator excepted, any class);
    DiscriminatorValueObserved emits a
    'discriminator-value-unobserved' Notice through notice_sink instead of
    warnings.warn.

    Args:
        emit: The open emit.
        config: The dimensional mode section.
        anchor: The resolved effective anchor, or None.
        window: The incremental window, or None for a full export.
        notice_sink: Receiver for plan notices.

    Returns:
        One QuerySpec per declared output table.

    Raises:
        ExportError: A business rule fails — including SliceOnlyColumnRefused
            and the re-keyed LookupColumnSafety.
        TemporalClassUnavailableError: A consulted column's temporal pair is
            absent or out of enum (non-conformant emit).
    """
```

```python
def export_dimensional(
    emit: Emit,
    config: ExportConfig,
    out: Path,
    fmt: Literal["csv", "duckdb"],
    anchor: EffectiveAnchor | None,
    notice_sink: NoticeSink,
) -> dict[str, int]:
    """
    Run a full dimensional export, threading notice_sink to the compile.

    Args:
        emit: The open emit.
        config: The full export config envelope.
        out: Output directory.
        fmt: Delivery format.
        anchor: The resolved effective anchor, or None.
        notice_sink: Receiver for plan notices.

    Returns:
        Output table name → row count.

    Raises:
        ExportError: Business-rule failure at compile.
        TemporalClassUnavailableError: Non-conformant temporal pair.
    """
```

```python
def build_source_plan(
    sidecar: Sidecar,
    config: SourceConfig | None,
    notice_sink: NoticeSink,
) -> tuple[SourceTableSpec, ...]:
    """
    Classify and plan every export unit.

    New behavior: each unit's delivered column set excludes non-exempt
    slice_only columns (one 'slice-only-column-omitted' Notice per omitted
    column per unit, in plan order); the collision check and rename
    resolution run over the narrowed set; a rename columns key naming an
    omitted column raises SourceRenameSliceOnly.

    Args:
        sidecar: The open emit's sidecar.
        config: The source mode section, or None for the bare full dump.
        notice_sink: Receiver for plan notices.

    Returns:
        One SourceTableSpec per export unit.

    Raises:
        ExportError: Any source business rule — including
            SourceRenameSliceOnly.
        TemporalClassUnavailableError: Non-conformant temporal pair.
    """
```

```python
def build_source_query_specs(
    emit: Emit,
    config: ExportConfig,
    anchor: EffectiveAnchor | None,
    window: Window | None,
    notice_sink: NoticeSink,
) -> list[QuerySpec]:
    """
    Compile the source plan to query specs, threading notice_sink to
    build_source_plan.

    Args:
        emit: The open emit.
        config: The full export config envelope.
        anchor: The resolved effective anchor (source requires one; its
            absence is the existing SourceAnchorRequired).
        window: The incremental window, or None.
        notice_sink: Receiver for plan notices.

    Returns:
        One QuerySpec per output table.

    Raises:
        ExportError: Any source business rule.
        TemporalClassUnavailableError: Non-conformant temporal pair.
    """
```

```python
def export_source(
    emit: Emit,
    config: ExportConfig,
    out: Path,
    fmt: Literal["csv", "duckdb"],
    anchor: EffectiveAnchor | None,
    notice_sink: NoticeSink,
) -> dict[str, int]:
    """
    Run a full source export, threading notice_sink to the plan.

    Args:
        emit: The open emit.
        config: The full export config envelope.
        out: Output directory.
        fmt: Delivery format.
        anchor: The resolved effective anchor.
        notice_sink: Receiver for plan notices.

    Returns:
        Output table name → row count.

    Raises:
        ExportError: Any source business rule.
        TemporalClassUnavailableError: Non-conformant temporal pair.
    """
```

```python
def iter_stream_events(
    emit: Emit,
    config: StreamConfig,
    anchor: EffectiveAnchor | None,
) -> Iterator[StreamEvent]:
    """
    Validate eagerly, then yield the merged, seq-stamped event stream.

    Signature unchanged. New behavior: the eager pass runs
    StreamPropertySliceOnly — a kinds[].properties entry resolving to a
    non-exempt slice_only column is refused before the first event is
    yielded. Streaming emits no notices: the after-image is wholly
    author-named, so there is nothing to omit.

    Args:
        emit: The open emit.
        config: The stream config envelope.
        anchor: The resolved effective anchor, or None.

    Returns:
        The event iterator (eager validation already complete).

    Raises:
        ExportError: Any eager-pass rule — including
            StreamPropertySliceOnly.
        TemporalClassUnavailableError: Non-conformant temporal pair.
    """
```

`stream_export` is unchanged — the new eager-pass rule surfaces through the
`iter_stream_events` call it already makes.

```python
def export_window(
    emit: Emit,
    config: ExportConfig,
    out: Path,
    fmt: Literal["csv", "duckdb"],
    anchor: EffectiveAnchor | None,
    window: Window,
    fingerprint: str | None,
    notice_sink: NoticeSink,
) -> dict[str, int]:
    """
    Run one pure windowed export, threading notice_sink to the mode's
    compile (build_query_specs or build_source_query_specs per config.mode).

    One invocation compiles exactly once — an explicit --from/--to range is
    a single range-window — so every plan notice reaches the sink once, with
    no forwarding or dedup logic.

    Args:
        emit: The open emit.
        config: The full export config envelope.
        out: Output target per fmt.
        fmt: Delivery format.
        anchor: The resolved effective anchor, or None.
        window: The half-open window to export.
        fingerprint: The drip fingerprint (--next), or None (explicit
            range — standalone, bookkeeping-free).
        notice_sink: Receiver for plan notices.

    Returns:
        Output table name → rows written this window.

    Raises:
        ExportError: Any windowed or always-on business rule — including
            the slice_only rules.
        TemporalClassUnavailableError: Non-conformant temporal pair.
        IncrementalRangeTargetExists: Unchanged.
        ExportRuntimeError: Unchanged.
    """
```

```python
def export_incremental_next(
    emit: Emit,
    config: ExportConfig,
    out: Path,
    fmt: Literal["csv", "duckdb"],
    anchor: EffectiveAnchor | None,
    notice_sink: NoticeSink,
) -> IncrementalOutcome:
    """
    Emit the next window and advance the cursor, threading notice_sink to
    export_window. Each drip invocation compiles once and re-emits its
    compile's notices.

    Args:
        emit: The open emit.
        config: The full export config envelope; incremental block required.
        out: Warehouse .duckdb file path (duckdb) or drop parent
            directory (csv).
        fmt: Delivery format.
        anchor: The resolved effective anchor, or None.
        notice_sink: Receiver for plan notices.

    Returns:
        IncrementalOutcome (unchanged shape).

    Raises:
        ExportError: Any business rule.
        TemporalClassUnavailableError: Non-conformant temporal pair.
        IncrementalConfigMissing: Unchanged.
        IncrementalFingerprintMismatch: Unchanged.
        IncrementalCursorInvalid: Unchanged.
        ExportRuntimeError: Unchanged.
    """
```

```python
def generate_init_config(
    emit: Emit,
    notice_sink: NoticeSink,
) -> str:
    """
    Propose a commented candidate dimensional config.

    New behavior: slice_only columns are skipped from column proposals
    (joining identity and lifecycle columns as never-proposed), one
    'slice-only-column-omitted' Notice per skip; the exempt discriminator
    remains proposable and drives filter pre-fill unchanged.

    Args:
        emit: The open emit.
        notice_sink: Receiver for proposal notices.

    Returns:
        The candidate config YAML text.

    Raises:
        InitRequiresRecordRoles: The sidecar omits record_roles.
        TemporalClassUnavailableError: Non-conformant temporal pair.
    """
```

## Validation Rules

### Parse-Time (Pydantic)

None. The policy is emit-dependent (the class lives in the sidecar), so every
rule is a business rule; the config grammar is unchanged.

### Business Rules

All run in each mode's existing always-on business-rule pass (dimensional:
`build_query_specs`; source: plan build; streaming: the eager pass). Each
raises an `ExportError` subclass through the CLI's existing error funnel.

| Rule | Mode | Checks | Error message shape |
|---|---|---|---|
| `SliceOnlyColumnRefused` | dimensional | No config-referenced value-read resolves to a non-exempt `temporal_class: slice_only` column. The surface list is exhaustive over the grammar: `from`, `correlation`, records `filter` keys, `value_map.from`, `derived: timestamp` `source`, `derived: elapsed` `correlate_on` / `start_source` / `end_source` / `other_where` keys, `fk via: reference` resolved-path hop columns (author-hinted `path` or pathfound from `to` — the check runs over the hops the resolution actually traverses), `fk via: membership` `member_path` hop columns and `as_of`. (`lookup` reads are `LookupColumnSafety`'s, below. Membership element predicates — `source.where`, fk `where` / `member_field` — and history-grain scoping — `source.property` / `value` — are outside the population by construction: membership and `history` columns carry no class.) Always-on, full export included | Names the output table/column, the base table.column, the class, and states the contract fact: the value is only known at the emit's slice |
| `LookupColumnSafety` (re-keyed) | dimensional | Terminal property and every traversed hop column are `temporal_class: constant` (was `history_tracked: false`), the exempt discriminator excepted (any class — the carve-out); all other existing resolvability clauses unchanged | Existing shape; the class clause names the offending column and its class (`tracked` or `slice_only`) |
| `SourceRenameSliceOnly` | source | No `rename` columns key names a non-exempt `slice_only` source column (the column is policy-omitted; the rename is unsatisfiable) | Names the rename entry, the column, and the omission reason |
| `StreamPropertySliceOnly` | streaming | No `kinds[].properties` entry resolves to a non-exempt `slice_only` column | Names the kind, the property, and the class |
| `DiscriminatorValueObserved` (migrated) | dimensional | Unchanged check; the emission becomes a `discriminator-value-unobserved` Notice through the sink, not `warnings.warn` | Unchanged text, as the notice `message` |

The reader's `TemporalClassUnavailableError` (a consulted column carries
`history_tracked` but no usable class) surfaces through the same funnel in every
mode, exactly as it does for source today.
