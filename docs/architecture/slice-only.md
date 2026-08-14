# The `slice_only` Export Policy

The contract classifies every records-category `prop__` column into a three-way
`temporal_class` — `constant` / `tracked` / `slice_only`. A `slice_only` column
carries only the branch's `slice_at` value with no history behind it: its past is
unknowable, and the contract states a consumer MUST NOT present it as an as-of-T
value. This doc owns the export-wide posture that enforces that clause: **no
exporter output value, row membership, linkage, or ordering derives from a
`slice_only` column's value**, with one mechanical carve-out for the sub-typed
discriminator. Enforcement is surface-appropriate — author-named reads are
refused at validation, auto-projected surfaces omit with a
[notice](notices.md) — and is not author-configurable: the policy is
contract-mandated, so there is no opt-out knob, no new YAML field.

**Source:** [`src/fabulexa_forge/exporters/slice_only.py`](../../src/fabulexa_forge/exporters/slice_only.py)
(the exemption predicates and refusal message), enforced in each mode's
validation (see [Enforcement per surface](#enforcement-per-surface)). Tests:
[`tests/exporters/test_slice_only.py`](../../tests/exporters/test_slice_only.py)
plus per-surface suites in each mode's test tree.

## Boundary

- **In:** the sidecar's `temporal_class`, read through the reader's narrowing
  accessor — verbatim carry, never inferred — and `Sidecar.subtype_values` (the
  `enum_domains`-sourced oracle) for the carve-out predicate.
- **Out:** refusals (`ExportError` subclasses through each mode's existing
  business-rule pass) and `slice-only-column-omitted` notices.
- A column carrying `history_tracked` but no usable class raises the reader's
  `TemporalClassUnavailableError` — unverifiable is refused, never inferred.

## Semantics

### The policy population

The sweep predicate is `temporal_class == "slice_only"`. The contract pins which
columns can carry it:

| Column class | Carries `temporal_class`? | Can be `slice_only`? |
|---|---|---|
| Records-category `prop__<name>` | yes | **yes — the entire policy population** |
| Presentation-property column | yes | never (contract guarantee) |
| `presentation_id`, identity columns (`record_id`, `fork_path`, `record_index`, `ref_index__*`) | no | no |
| Lifecycle columns (`created_sim_time`, `active`, `deactivated_at`, `last_mutation_sim_time`) | no | no |
| `history` columns, membership-table columns | no | no |

A column without the temporal pair is never consulted by the sweep. Membership
element predicates and history-grain scoping reads are outside the population by
construction — membership and `history` columns carry no class.

### The read taxonomy

| Read kind | Definition | Policy |
|---|---|---|
| **Value-read** | An output value, join resolution, row membership, or row order derives from the column's value: projection, `lookup` terminal or hop, fk hop, filter key, value-map source, derived-column source or correlation key, after-image column | Refused (author-named) or omitted (auto-projected) |
| **Metadata read** | The engine reads the column's *class* from the sidecar (the sweep itself, source's audited-set resolution, `init`'s skip) | Always permitted — no value is touched |
| **Classification read** | The sub-typed discriminator's current value used to classify rows, on any surface | Permitted — the one value-read exception (the carve-out) |

### The discriminator carve-out

**Predicate:** a column of `records__<K>` is exempt iff its name is
`prop__<K>_type` **and** `Sidecar.subtype_values(K)` is non-empty
([`is_exempt_discriminator`](../../src/fabulexa_forge/exporters/slice_only.py)).
Mechanical — never a judgment about a column's usefulness; the registry's
object-vs-string shape plays no role.

The exemption is structural, not temporal. A `records__<K>` table is a wide
union of sub-kinds; `prop__<K>_type` is the tag that says what each row actually
*is*. The contract does not pin a discriminator's `temporal_class` — a producer
may mark it `slice_only` — and an unexempted sweep would strip the one
classification key every consumer groups, routes, and splits by (the source
declared-table population filter, streaming routing and `types` selection, BI
grouping). The
exempt discriminator is carried and selectable *as a classification* — the
current value at every T — never presented as an as-of value. The carve-out
spans every policing surface: the sweep, the source omission, the streaming
refusal, `init`'s skip, and the `lookup` regate alike.

| Condition | Result |
|---|---|
| `prop__<K>_type`, `subtype_values(K)` non-empty, any class | Exempt: projected, filterable, renameable, proposable, permitted on a `lookup` path |
| `prop__<K>_type`, `subtype_values(K)` empty, class `slice_only` | Not exempt |
| Any other `prop__` column, class `slice_only` | Not exempt |

### Enforcement per surface

Each mode enforces the posture through its own authoring model; the mode docs
own the rule detail:

| Mode | Enforcement | Rules (owning doc) |
|---|---|---|
| dimensional | Refuse every config-referenced value-read; `lookup` regated to `constant`; `init` skips proposals with a notice | `SliceOnlyColumnRefused`, `LookupColumnSafety` — [`dimensional.md`](dimensional.md) |
| source | Omit from every auto-projected surface (the state render's classified projection, the event log's audited set), one notice per unit × column; a declaration entry (`columns` / `rename` / `only` / `ignore`) naming a non-exempt column errors | `SourceSliceOnlyRead` — [`source.md`](source.md) |
| base | Omit every non-exempt `slice_only` `prop__` column from the flat table, one `slice-only-column-omitted` notice per kind × column; the sub-typed-discriminator carve-out honored; a `rename` naming an omitted column errors | `BaseRenameSliceOnly` — [`base.md`](base.md) |
| streaming | Refuse-only: the after-image is wholly author-named, so a stream's `properties` entry naming a non-exempt `slice_only` property is refused in the eager pass; no notices. Event membership is unaffected by class: `slice_only` implies `history_tracked: false`, so such a column has no change points to fire | `StreamPropertySliceOnly` — [`streaming.md`](streaming.md) |
| incremental | No rules of its own: refusal is always-on before any window gate runs | [`incremental.md`](incremental.md) |

Omission and refusal narrow the modes' declared temporal-honesty exception into
honesty: where untracked columns ride an after-image at their current
records-table value, the surviving riders are exactly `constant` — values the
contract declares valid at every T.

### Column-projection-only invariance

Omission never changes a row set. The row-state-events fold's rows are keyed on
creation, tracked-property change instants, and deactivation; a `slice_only`
column is by definition untracked and contributes no rows. Narrowing the
property set a mode passes to a fold removes after-image *columns* only: event
row sets, global `seq` assignment, and incremental window membership are
identical with or without the policy. The degenerate case follows the same
rule: a unit whose every property is non-exempt `slice_only` still renders —
rows intact, carrying its classless columns and the exempt discriminator when
present. Omission never suppresses an export unit or a proposal target.

## Invariants

1. **The posture.** No exporter output value, row membership, linkage, or
   ordering derives from a `slice_only` column's value; the sole exception is
   the sub-typed discriminator classification read, honored on every surface.
   Future modes inherit this invariant; a new mode decides *how* to enforce it
   (refuse vs omit per its authoring model), never *whether*.
2. **Refusal is always-on.** The `slice_only` rules run in each mode's always-on
   business-rule pass, full export included — never window-gated.
3. **Omission is column-projection-only.** Event row sets, `seq`, and window
   membership are invariant under the policy.
4. **The carve-out is mechanical and surface-total.** Exemption is exactly
   `name == prop__<K>_type ∧ subtype_values(K) ≠ ∅` — no other column, no other
   condition, and no surface applies a narrower predicate.

Relied upon: the contract's coverage guarantee (every records-category `prop__`
column of a supported-version emit carries the temporal pair; presentation
columns are never `slice_only`); the version gate (the policy never runs against
an emit whose contract predates the classification); `subtype_values` is total
and never raises; the row-state-events and state-at folds accept a caller-chosen
property set and key their row sets on tracked-property change instants only.

## Rationale

- **Refuse vs omit follows authorship.** An author-named read is a stated
  intent the engine cannot honor — honoring it silently would fabricate an
  as-of value — so it is an error. An auto-projected column was never asked
  for; dropping it with a notice keeps the export running and the author
  informed. Streaming has no auto-projection, so it is refuse-only.
- **The policy is not configurable** because the contract mandates it ("MUST
  NOT"); an opt-out knob would be a fabrication switch.
- **Gating is selection-side and mode-side, never inside a fold.** Narrowing a
  fold's input by class would change event row sets; keeping folds
  class-agnostic keeps the invariance provable (see
  [`derivations.md`](derivations.md)).

## Boundaries

- The derivations layer is class-agnostic: no fold consults `temporal_class`
  (see [`derivations.md`](derivations.md) § Boundaries).
- Source's declared-table resolution is outside the policy: declaration
  resolution and the render choice never consult a `slice_only` column — the
  policy touches projections and audited sets, never layout.
- Structural lifecycle columns (`updated_at` sourcing, `last_mutation_sim_time`)
  carry no class and are outside the population; their presentation is owned
  elsewhere. `last_mutation_sim_time` is a reserved output column name under the
  presentation-name posture ([`source.md`](source.md) § Presentation-name posture,
  [`dimensional.md`](dimensional.md) § Output naming) — its value channels freely,
  its raw name never — and the playback seam presents it as the recorded trail
  under `state` ([`playback.md`](playback.md)).
- Corrupters are outside the policy: they write base-shaped output, and a
  corrupted emit flows through exporters under the same policy as a clean one.
- Membership tables, `history`, identity and lifecycle columns are classless by
  contract and outside the population.

## Related

| Document | Why |
|---|---|
| [`notices.md`](notices.md) | The channel omission reports flow through |
| [`dimensional.md`](dimensional.md) · [`source.md`](source.md) · [`streaming.md`](streaming.md) | Per-mode enforcement detail |
| [`derivations.md`](derivations.md) | The class-agnostic folds the invariance rests on |
| [`../../contract/base-format.md`](../../contract/base-format.md) | The `temporal_class` contract and the MUST-NOT clause |
