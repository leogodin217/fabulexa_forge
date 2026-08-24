---
status: draft
---

# Streaming Authoring Parity

Per-stream output vocabulary (rename + kind labeling), row selection (`where` +
membership owner `sub_types`), and change scope (`only` / `ignore`) for the
streaming exporter — the three author surfaces every batch mode has and the live
feed lacks.

---

## Problem

The streaming exporter is the only output mode where an author cannot rename,
select rows, or scope events — three surfaces the batch modes all carry:

1. **Engine vocabulary ships on the wire.** After-image keys are the fold's
   column names verbatim (`prop__journey_instance` where the batch exports of
   the same emit say `session_id`); the envelope `kind` carries the raw engine
   kind (`tick_decision`); membership `member__<f>__kind` payload values are
   raw kind names. Source has per-source `rename` + `item_type` and a
   config-level `kind_labels` map; base has `rename`; dimensional names every
   output column. Streaming has nothing — per-column renaming is currently a
   declared non-goal.

2. **No row selection.** A `KindStream` scopes by `sub_types` only; a
   `MembershipStream` is whole-table-or-nothing — no owner `sub_types`, no
   `where`. The shared row-predicate grammar has seven surfaces across the
   dimensional and source modes and zero in streaming. An author streaming a
   security feed cannot say "only the EMEA records' events" without shipping
   everything.

3. **No change-scope control.** Streaming invokes the row-state-events fold
   with change scope pinned to the kind's full property set: every tracked
   property's change fires a `u`, whatever the stream projects. Source's event
   log narrows its audited set with `only` / `ignore`; a stream author has no
   knob on `u` volume.

Everything the following config asks for is a parse error today:

```yaml
content: state-changes
streams:
  - name: security_events
    kind: tick_decision
    sub_types: [login, logout, access_denied]
    where: {region: emea}                                   # (2)
    only: [decision_type, context]                          # (3)
    properties: [journey_instance, decision_type, context]
    rename: {journey_instance: session_id, decision_type: event_type}  # (1)
kind_labels: {tick_decision: security_event, entity: user}  # (1)
```

## Solution

Extend the two stream declarations and `StreamConfig` with the batch modes'
existing grammars, reusing their semantics wholesale — no streaming-local
inventions:

- **Rename + vocabulary.** After-image payload keys default to the **bare
  property / field name** (the `prop__` / `elem__` / `member__` prefixes are
  reader plumbing and stop leaking onto the wire — a deliberate breaking wire
  change), overridable per stream by a `rename` map with source's rename
  grammar. A `StreamConfig.kind_labels` map (source's exact semantics —
  injectivity over the whole kind universe, identity fall-through) applies
  wherever a kind name renders as a value: the JSONL envelope `kind` and
  membership member-kind payload values. A per-stream `kind_label` overrides
  the envelope value wholesale — source's `item_type` analog.
- **Row selection.** `where` on both stream shapes and owner `sub_types` on
  `MembershipStream`, compiled through the one shared predicate-rendering
  authority under source's constant-column gate and parent-lookup semantics.
  The constant gate is what keeps a stream's event set well-defined over the
  whole tape: a constant property selects the same rows at every instant.
- **Change scope.** `only` / `ignore` on `KindStream`, passed as the
  row-state-events fold's change-scope set. The fold already takes change
  scope and projection as two independent sets; streaming stops hard-wiring
  change scope to the full property set. The event set stays
  projection-independent; it becomes declaration-dependent — source's
  event-log posture.

All three are presentation- or scope-level: the folds, the canonical order,
the merge key, `seq`, `ts`, and the key-election machinery are untouched.

## Affected Subsystems

- **The streaming exporter** — the center of the change. The two stream
  declarations gain the new fields; the eager validation pass gains the
  vocabulary, predicate, and change-scope gates; the engine resolves per-stream
  output names and the kind vocabulary where it assembles each event's envelope
  and after-image, narrows fold rows by the compiled `where` record set (the
  shipped `sub_types` row-dropping device, extended), and passes the author's
  change scope to the fold. The format renderers stay transparent — they write
  the assembled maps and `StreamEvent.kind` verbatim, unchanged; the Debezium
  value-schema builder reads the same output-name resolution the after-image
  assembly reads, so schema ↔ row agreement continues to hold by construction.
  `init` proposes none of the new fields.
- **The export-config models** — `KindStream`, `MembershipStream`, and
  `StreamConfig` grow the fields above; the shared rename- and where-map
  well-formedness helpers gain the stream models as consumers.
- **The config row-predicate grammar** — streaming joins its consumer set (two
  new surfaces: `streams[].where` on both shapes). The grammar, rendering
  authority, and literal-typing rules are unchanged; streaming becomes an
  ordinary consumer of the shared grammar.
- **The selection-spine device** — the fan-out-free, horizon-free owner/record
  parent lookup that source's row selection composes becomes a two-mode
  surface: streaming resolves its `where` record sets (and membership owner
  scoping) through the same device rather than growing a sibling.
- **The key-election surface (membership uniformity granularity)** — a
  membership stream's key-uniformity gate ranges over the **addressed owner
  population set** (the declared owner `sub_types`, or the full domain when
  omitted) instead of unconditionally over the owner kind's full domain — the
  narrowed-unit resolution source already has. A mixed-election owner kind
  becomes splittable per sub-type across streams.
- **The notice channel** — the stream engine emits the shared per-element
  `discriminator-value-unobserved` notice for out-of-domain `where` values, so
  `iter_stream_events` gains a **required** caller-supplied `notice_sink`
  parameter (the notice-channel posture: required, no default; a caller
  wanting silence passes a discarding sink). Every consumer threads it — the
  stream driver paths (the CLI passes the stderr renderer) and the mixer's
  `seed_mixer_run`, which gains the same required parameter and passes it
  through (the mixer CLI passes the stderr renderer). Notices are emitted in
  the eager pass, before any fold materializes.

## What Doesn't Change

- **The content × format × sink model.** No new content type, format, sink,
  flag, or CLI verb. Pacing, the mixer's scheduling and control semantics,
  and the Kafka sink are untouched — the one mixer-side change is mechanical:
  `seed_mixer_run` threads the engine's new required `notice_sink` through to
  `iter_stream_events`.
- **The format renderers.** `render_jsonl_object`, `render_debezium_message`,
  the pinned encoder, and the Kafka render closure keep their contracts:
  rename targets, the envelope label, and member-kind values are resolved
  before any renderer runs, so the renderers stay byte-transparent
  presentation of the assembled event. The Debezium value-schema builder's
  signature is likewise unchanged — only the column-name list its caller
  passes moves from fold names to the resolver's output keys.
- **The derivations layer.** The row-state-events two-scope contract, the
  membership-events fold, and every resident are consumed differently, not
  changed. No new resident.
- **Ordering, `seq`, `ts`.** The canonical order and merge key
  (`event_sim_time`, `event_class`, `stream_name`, `record_id`) read fold
  values and the declared stream name — none of the new fields is a component.
  Renames and labels are presentation, applied at event assembly after the
  merge; selection and change scope change which events exist, never how
  survivors order. `ts` and the anchor rules are
  untouched.
- **The message key and identity entries.** The elected message key, the
  one-entry key map, the after-image identity entry, and the auto-included
  `presentation_id` keep their contract column names at every site. Identity
  is never rename-addressable; `rename` addresses payload properties and
  element fields only.
- **`route_table` and `table_identity`.** The Debezium masquerade is
  schema-identity, not a payload value: `kind_labels` does not reach
  `route_table`, `source.table`, or the value-schema names. The masquerade
  knob remains the only authority over reported table identity.
- **Temporal elections.** Still do not attach to streams; the `render:` map
  still carries `decimal` / `json_precision` only, still keyed by **source**
  identity (bare property / field name), unaffected by `rename`.
- **The slice-only policy.** The policy population and the refuse-only
  streaming posture are unchanged; the new surfaces (`where` keys, change-
  scope entries) fall under the existing read taxonomy and are refused, not
  newly tolerated.
- **Overlapping streams.** Still legal, still no disjointness gate — each
  topic is an independent declared feed, and cross-stream duplication remains
  declared intent. (Source's disjointness gate protects a single numbered
  log; no streaming analog exists to protect.)
- **The base mode.** Still carries no row predicate; the corrupter's row
  selector remains a separate grammar. Windowed / incremental streaming stays
  out of scope.
- **`properties` / `fields`.** Required-no-default, bare, projection-only —
  exactly as shipped.

## Semantics

### Output-name resolution (rename)

Every after-image payload column resolves one **output key**, per stream:

| Column | Default output key | `rename` entry |
|---|---|---|
| Kind-stream property `prop__<p>` | `<p>` (bare) | `<p>: <target>` → `<target>` |
| Membership scalar element field `elem__<f>` | `<f>` (bare) | `<f>: <target>` → `<target>` |
| Membership reference field `member__<f>__kind` / `member__<f>__id` | `<f>_kind` / `<f>_id` (the event log's `changes` pair convention; source's junction render names the halves independently by source name — a shape streaming's bare-field rename grammar deliberately does not carry) | `<f>: <target>` → `<target>_kind` / `<target>_id`, renamed in place as a pair |
| Identity entry (elected surface), auto-included `presentation_id`, membership owner identity entry, Debezium membership `event` | Contract name, verbatim | Not addressable — a `rename` key naming one is unresolvable (it is not a declared property / field) |

Rules, all mirroring source's rename grammar:

| Condition | Result |
|---|---|
| `rename` key not in the stream's `properties` / `fields` | Refused — keys are source identities and must name a selected property / field |
| Two output keys collide (two rename targets; a target vs an unrenamed bare default; a renamed pair member vs anything) | Refused — never a silent collision |
| An output key equals a reserved name on that stream — the identity entry's contract column name, `presentation_id` when the kind carries one (and no election absorbs it), or `event` on a membership stream | Refused. `event` is reserved on membership streams regardless of format — the config never knows its format, so one eager rule covers both (the topic-name-rule posture) |
| Rename present, order of the after-image | Unchanged — the single column-order producer's order; rename relabels, never reorders |
| `render:` map keys | Still bare source identities — rename does not move election keys (source's keys-are-source-identities rule) |

Output names are produced by **one resolver per stream shape** — the
single-producer discipline extended from column order to column naming: the
engine's after-image assembly and the Debezium value-schema build both read
the same resolved `(fold column, output key)` list (the format renderers are
not consumers — they write the assembled after-image verbatim), so the
declared schema and the rendered rows cannot diverge. The schema builder's
own contract is untouched — it already takes an ordered column-name list;
its caller passes the resolver's output keys (after the leading membership
`event`) in place of the fold names. The resolver receives
the stream's **election-resolved identity output key** — the elected surface's
contract column name, absorption already applied by the caller — so the
reserved-name set it enforces is the stream's actual identity surface, never a
guess. The fold's own SQL column names are untouched; naming is applied where
after-image maps are assembled.

### Kind vocabulary (`kind_labels`, per-stream `kind_label`)

The envelope `kind` value resolves per stream, first match wins (source's
item-type resolution, restated for streams):

| Condition | Envelope `kind` |
|---|---|
| `kind_label` declared on the stream | The declared string, verbatim |
| Stream's kind (owner kind, for a membership stream) in `kind_labels` | The kind's label |
| Neither | The kind name, verbatim |

The envelope `kind` is per-stream constant (a stream spans populations of one
kind). `kind_labels` additionally applies per value to membership member-kind
payload entries (`<f>_kind` under the bare-name default) with **identity
fall-through**: a value matching no declared pair renders verbatim, `NULL`
stays `NULL` — the mapping is total, so a corrupted emit's mutated kind cell
surfaces unchanged, never masked and never a render error. Byte-identical
passthrough when no labels are declared.

Vocabulary integrity splits by what claim each surface makes. **`kind_labels`
is a value mapping** — the surface member-kind values render through — and
stays injective over the emit's whole kind universe (source's rule): every
key names a sidecar kind, two kinds cannot share a label, and a label cannot
equal a *different* kind's rendered name (its label, or its verbatim name
when unlabeled; member-kind values are not bounded by the declaration list) —
so within the value mapping, one rendered kind name identifies at most one
kind. **A per-stream `kind_label` is feed presentation**, not a kind claim:
it names the domain concept the stream represents — usually sub-type grain, a
grain the kind universe does not see (sub-types are the first-class domain
concepts; kinds are simulation machinery). It carries one constraint, the
masquerade refusal: it cannot equal a *different* kind's rendered name, since
that string does identify a kind wherever member-kind values render. Within
that bound, sharing is legal declared intent: two streams — of one kind
(source's legal split case) or of different kinds — may declare one
`kind_label`.

Reach: JSONL carries `kind` at the top level; the Debezium envelope has no
kind field, so per-stream labels do not reach Debezium at all — `kind_labels`
reaches Debezium only through membership member-kind **values** in the
after-image. Labeling is applied where the engine assembles each event:
`StreamEvent.kind` carries the stream's resolved envelope value, and
member-kind after-image entries carry the mapped values through the
identity-fall-through device at the same assembly site `rename` applies (the
value-election attach point). The format renderers are byte-transparent to
it — which is what makes the element-field format-parity invariant hold by
construction — and event membership, ordering, `seq`, and topic assignment
never read the vocabulary.

### Row selection (`where`, membership owner `sub_types`)

`where` keys are **bare payload-property names of the subject kind** — the
declared kind on a `KindStream`, the **owner** kind on a `MembershipStream`
(owner properties are not columns of the membership table at all; a bare key
matching both an owner property and an element field resolves to the owner
property — element fields carry no temporal class and are not
predicate-addressable). Entries are AND-joined; values are the shared
`PredicateValue` grammar compiling to `=` / `IN` under the one rendering
authority, literal-typed from the sidecar.

The **constant-column gate** applies verbatim: a key must name a
`constant`-class payload property. Its purpose is sharper here than anywhere —
a stream replays every instant of the tape, so only a property whose value is
identical at every horizon can select rows without making the event set
time-dependent. The gate makes the as-of-which-instant question unposable.

| `where` key names | Result |
|---|---|
| A `constant`-class payload property of the subject kind | Accepted |
| A `tracked`-class property | Refused — its value at event time and its current value select different rows |
| A `slice_only` property | Refused — its past is unknowable (the slice-only posture) |
| The subject kind's declared discriminator | Refused, pointing at `sub_types` (owner `sub_types` on a membership stream) |
| A structural column, a membership element field, or an unknown column | Refused — unresolvable |

Value axis (source's posture verbatim): every element is cast to the resolved
column's sidecar-declared type at validation time — an uncastable element is
refused before any fold runs; an element outside a column's declared
`enum_domains` entry draws a per-element `discriminator-value-unobserved`
notice, never an error — one config legitimately serves a family of emits.
The notice message leads with `stream '{name}'`, as every message in this
design does, and keeps the shipped two-case wording with the stream's nouns:
when no element of an entry was observed it states the topic will be empty
(the declared-but-empty topic); when the entry's other elements were observed
it states only that this element contributes no events. Notice order follows
the eager pass's iteration order — streams in declaration order, a stream's
`where` keys in config key order, a key's elements in declared order — so the
sequence is deterministic, the notice channel's invariant.

Selection is realized as the shipped engine-side row-scoping device: the
satisfying record set (satisfying **owner** set, for a membership stream) is
computed once per stream from the compiled predicate, and fold rows outside it
are dropped before the merge, exactly as out-of-scope `sub_types` rows are
dropped today. Dropped rows consume no `seq`. The scoping mechanism splits by
stream shape: a kind stream's `sub_types` stay the shipped discriminator-index
device, with `where` adding the satisfying-record-set drop beside it; a
membership stream's owner `sub_types` and `where` resolve **together** through
the shared parent-lookup spine — one owner-side read producing one satisfying
owner set, whether either or both are declared.

| Condition | Result |
|---|---|
| Kind stream with `where` | Every event of a non-satisfying record is excluded — `c` and `d` included; the fold and its SQL are unchanged |
| Membership stream with owner `sub_types` / `where` | Every `join` / `leave` of a non-satisfying owner's collection is excluded, via the parent lookup |
| `where` and `sub_types` on one stream | AND-composed — the predicate narrows within the scoped populations / owner sub-types |
| Selection matches zero rows | The declared-but-empty topic: the topic exists (empty file, pre-created Kafka topic, `events_per_topic == 0`), exit 0 — declared intent drives existence |
| A row whose predicated column is NULL | Never selected — `=` / `IN` is never satisfied by NULL, and the grammar has no null test |
| Predicated property absent from `properties` | Legal — selection and projection are orthogonal; the predicate reads the subject relation, not the payload |
| Predicate on a reference-valued constant property | Legal — compared over base-layer values (record ids), whatever surface the column renders |
| Overlapping streams with different `where` | Legal — each stream's selection scopes its own feed independently |
| Owner `sub_types` on a membership stream | Narrows the **addressed owner population set** — the set the key-uniformity gate ranges over and per-row owner-election resolution draws from. `where` never narrows the addressed set (value-level, not population-level — gates and type resolution see the full declared scope, whatever rows the predicate selects) |

Predicates evaluate over source (base-layer) values — before rename, before
elected-surface rendering, before labels.

### Change scope (`only` / `ignore`)

`only` and `ignore` (mutually exclusive, bare names) narrow the change scope
the engine passes to the row-state-events fold. The **default audited scope**
is the kind's temporally honest property set — every `tracked`- and
`constant`-class property; `only` narrows to its entries, `ignore` subtracts
its entries. (The shipped invocation passes the kind's full property set; a
`slice_only` property is history-untracked and contributes no change points,
so the narrowed default's event set is byte-identical — the set the engine
passes changes, the events do not.) The projection (`properties`) is untouched — the two scopes stay
independent, as the fold's contract already provides.

| Condition | Result |
|---|---|
| Both fields absent | Change scope = the full audited set — today's behavior, byte-identical |
| A scoped tracked property changes | A `u` fires at that change point |
| Only out-of-scope properties change at an instant | No `u` exists for that instant — the event is never produced (it consumes no `seq`), the source-log suppression analog |
| In-scope and out-of-scope changes coincide at one instant | One `u` (the fold's per-`(record, sim_time)` grain) |
| A property projected but not in change scope | Its changes fire no `u`, but its as-of value still rides every after-image the surviving events carry |
| A property in change scope but not projected | Its change fires a `u` whose after-image does not show it — a notification-shaped feed for that property |
| A `constant`-class (or untracked) name in change scope | Legal and inert for `u` membership — no history rows, no change points (the fold's rule) |
| `ignore` covering every tracked property | A lifecycle-only feed — `c` / `d` events only; legal, the no-tracked-property population shape |
| `c` / `d` events | Never affected — lifecycle always fires |
| Membership streams | No change-scope fields — `join` / `leave` are the facts and `fields` is already pure projection; the fields do not exist on the model |

The change scope is compared over raw base values — renames, labels, and
render elections play no part in event membership (the source log's
election-invariant diff rule).

### The event-set invariant, restated

The shipped payload-independence invariant becomes: **a stream's event set is
a function of its declared row scope (populations × `sub_types` × `where`) and
its change scope (`only` / `ignore`) — never of `properties` / `fields`,
`rename`, `kind_label`, or `render`.** Two streams over one population with
equal row and change scope carry identical event sets, whatever they project
or however they name it. Presentation invariance is a companion invariant: for
a fixed declaration, adding or changing `rename` / `kind_labels` /
`kind_label` changes payload key strings and `kind` / member-kind value
strings only — event count, order, `seq`, `ts`, message keys, and topic
assignment are byte-identical.

The conformance statement toward the playback seam's canonical order gains one
scoped divergence alongside the shipped three (interleave, multiplicity,
membership field tail): **row and change scope follow declaration** — a scoped
stream plays the subset of the seam's row set its declaration selects, and
within surviving events the canonical order is unchanged.

Selection temporal honesty is an invariant, not an aspiration: because `where`
columns are constant-gated and discriminators are creation-constant, the
satisfying record set is one set for the whole tape — no event's inclusion
depends on state later than the event (the temporal-honesty invariant needs no
new exception).

### `init`

`init --mode streaming` proposes none of the new fields — a rename target, a
label, a predicate, and a change scope are each author intent with no
sidecar-derived value (proposing one would be invention). They join the
trailing comment that names the never-proposed blocks and where they would go.
Proposal output is otherwise unchanged and remains parse-clean by
construction.

## Configuration

```yaml
content: state-changes
streams:
  - name: security_events
    kind: tick_decision
    sub_types: [login, logout, access_denied]
    where: {region: [emea, apac]}          # constant-class property, =/IN
    only: [decision_type, context]         # u events fire on these only
    properties: [journey_instance, decision_type, context]
    rename:
      journey_instance: session_id         # wire key: session_id
      decision_type: event_type            # wire key: event_type
    kind_label: security_event             # envelope kind, this stream
kind_labels:
  entity: user                             # every kind-name-as-value site
```

```yaml
content: membership-events
streams:
  - name: ward_occupancy
    membership: {kind: ward, property: occupants}
    sub_types: [icu, general]              # owner sub-types, parent lookup
    where: {site: north_campus}            # owner constant property
    fields: [bed, admitted_by]
    rename: {admitted_by: clinician}       # reference field: clinician_kind / clinician_id
kind_labels:
  ward: ward                               # member-kind values + envelope kind
  patient: patient
```

| Field | On | Type | Required | Description |
|---|---|---|---|---|
| `rename` | both stream shapes | `dict[str, str]` | No | Selected bare property / field name → after-image output key. A membership reference field's entry renames its expanded `<f>_kind` / `<f>_id` pair in place. |
| `kind_label` | both stream shapes | `str` | No | The stream's envelope `kind` value, verbatim — overrides `kind_labels` and the kind name (source's `item_type` analog). Feed presentation, shareable across streams of any kind; must not equal a different kind's rendered name. |
| `where` | both stream shapes | `dict[str, PredicateValue]` | No | AND-joined row predicate over bare `constant`-class payload properties of the subject kind (the owner kind on a membership stream). |
| `sub_types` | `MembershipStream` (new; shipped on `KindStream`) | `list[str]` | No | Owner sub-type subset — the addressed owner population set, resolved per row through the parent lookup. Absent = every declared owner sub-type. |
| `only` / `ignore` | `KindStream` | `list[str]` | No | Change-scope narrowing by bare property name; mutually exclusive. Absent = the kind's full audited set. |
| `kind_labels` | `StreamConfig` | `dict[str, str]` | No | Engine kind → domain label, applied at every kind-name-as-value site (envelope `kind` default, member-kind payload values). Injective; identity fall-through. |

## Interface Contracts

### Config Models

```python
class KindStream(StrictBaseModel):
    """A kind-shaped declared stream (content: state-changes)."""

    # ... shipped fields: name, kind, sub_types, properties, render ...

    where: dict[str, PredicateValue] | None = None
    """Row predicate over the declared kind, keyed by bare constant-class
    payload-property name; entries AND-joined (gated at validation time).
    Absent = every row of the scoped populations."""
    only: list[str] | None = None
    """Change-scope subset by bare property name; mutually exclusive with
    `ignore`. Governs u-event membership only — projection is `properties`.
    Absent (with `ignore` absent) = the kind's full audited set."""
    ignore: list[str] | None = None
    """Change-scope exclusion by bare property name; mutually exclusive with
    `only`."""
    rename: dict[str, str] | None = None
    """Selected bare property name -> after-image output key. Keys are source
    identities, never output keys. Identity entries are not addressable."""
    kind_label: str | None = None
    """This stream's envelope `kind` value, verbatim, overriding the
    kind_labels / kind-name default. Non-empty when present. Feed
    presentation, not a kind claim — shareable across streams; must not
    equal a different kind's rendered name (business pass)."""


class MembershipStream(StrictBaseModel):
    """A membership-shaped declared stream (content: membership-events)."""

    # ... shipped fields: name, membership, fields, render ...

    sub_types: list[str] | None = None
    """Owner sub-type subset — the stream's addressed owner population set,
    resolved per row through the parent lookup. Non-empty and duplicate-free
    when present; absent = every declared owner sub-type. The owner kind
    must be sub-typed (business pass)."""
    where: dict[str, PredicateValue] | None = None
    """Row predicate over the OWNER kind, keyed by bare constant-class
    owner-property name; entries AND-joined. Element fields are not
    predicate-addressable. Absent = every owner's intervals."""
    rename: dict[str, str] | None = None
    """Selected bare element-field name -> after-image output key. A
    reference field's entry renames its expanded `<f>_kind` / `<f>_id`
    pair in place. The owner identity entry and the Debezium `event`
    column are not addressable."""
    kind_label: str | None = None
    """This stream's envelope `kind` value (the owner-kind slot), verbatim.
    Non-empty when present."""


class StreamConfig(StrictBaseModel):
    """Streaming delivery envelope."""

    # ... shipped fields: content, streams, keys, rebase, debezium, clock, kafka ...

    kind_labels: dict[str, str] | None = None
    """Engine kind -> domain label, applied at every kind-name-as-value
    site: the envelope `kind` default and membership member-kind payload
    values. Non-empty when present; keys and values non-empty; no two keys
    share a label (parse time). Every key must name a sidecar kind and no
    label may collide with another kind's rendered name (validation time).
    Identity fall-through: an unlabeled value renders verbatim."""
```

### Functions

```python
def resolve_stream_output_columns(
    sidecar: Sidecar,
    kind: str,
    properties: Sequence[str],
    rename: Mapping[str, str] | None,
    identity_key: str,
) -> list[tuple[str, str]]:
    """Resolve a kind-shaped stream's after-image (fold column, output key)
    pairs — the single naming authority extending resolve_stream_columns.

    Order is resolve_stream_columns order exactly (identity entry, then
    presentation_id when carried and not absorbed, then projected properties
    in sidecar order); the identity entry's output key is `identity_key`,
    payload columns take their bare name or their rename target.

    Args:
        sidecar: The typed sidecar.
        kind: The stream's records kind, bare.
        properties: The stream's declared projection, bare names.
        rename: The stream's rename map, or None.
        identity_key: The identity entry's output key — the stream's elected
            surface's contract column name (record_id / record_index /
            presentation_id), resolved by the caller from the stream's
            election with absorption applied. Defines the reserved-name set
            together with presentation_id, reserved when the kind carries
            one and identity_key is not presentation_id (the unabsorbed
            case).

    Returns:
        Ordered (fold column name, output key) pairs — the one list the
        after-image keying, the JSONL renderer, and the Debezium value
        schema all consume.

    Raises:
        ExportError: A rename key names no selected property; two output
            keys collide; an output key collides with a reserved identity
            name.
    """


def resolve_membership_output_columns(
    sidecar: Sidecar,
    membership: MembershipRef,
    fields: Sequence[str],
    rename: Mapping[str, str] | None,
    owner_identity_key: str,
) -> list[tuple[str, str]]:
    """The membership analog of resolve_stream_output_columns, extending
    resolve_membership_columns. Order is resolve_membership_columns order
    exactly: owner identity entry, then selected element fields in
    element-schema declaration order (never the config `fields` list's
    order) — a scalar field one pair, a reference field its `<f>_kind` /
    `<f>_id` pair renamed in place.

    Args:
        sidecar: The typed sidecar.
        membership: The stream's membership-table address.
        fields: The stream's declared field projection, bare names.
        rename: The stream's rename map, or None.
        owner_identity_key: The owner identity entry's output key — the
            owner's elected surface's contract column name, resolved by the
            caller. With the membership `event` name, defines the reserved
            set.

    Returns:
        Ordered (fold column name, output key) pairs.

    Raises:
        ExportError: A rename key names no selected field; two output keys
            collide; an output key collides with the owner identity entry
            or the reserved membership `event` name.
    """


def resolve_stream_kind_vocabulary(
    config: StreamConfig,
    sidecar: Sidecar,
) -> Mapping[str, str]:
    """Validate the run's kind vocabulary — the config-level kind_labels
    map plus every per-stream kind_label — and return the declared value
    mapping.

    Args:
        config: The stream config (kind_labels plus every per-stream
            kind_label).
        sidecar: The typed sidecar (the kind universe the integrity rules
            range over).

    Returns:
        The declared config-level (kind, label) pairs; callers render an
        undeclared kind verbatim (identity fall-through is caller-side —
        the total mapping is the pair of this map and that rule). A
        per-stream kind_label is validated here but never enters the
        mapping: the engine applies it on its own stream's envelope only.

    Raises:
        ExportError: A kind_labels key names no sidecar kind; a label or a
            per-stream kind_label equals a different kind's rendered name.
    """


def resolve_stream_selection(
    emit: Emit,
    stream: KindStream | MembershipStream,
    notice_sink: NoticeSink,
) -> frozenset[str] | None:
    """Compute a stream's satisfying record set (owner set, for a
    membership stream) from its declared selection, or None when the
    stream declares no selection this function owns: a kind stream's
    `sub_types` stay the shipped discriminator-index device (None when it
    declares no `where`); a membership stream's owner `sub_types` and
    `where` resolve together here through the parent-lookup spine (None
    only when it declares neither).

    Compiles the predicate through the shared rendering authority against
    the subject kind's records spine (via the shared selection-spine
    parent lookup for a membership stream); the constant-column gate and
    the plan-time value casts run first. Emits the per-element
    `discriminator-value-unobserved` notice for each `where` element
    outside its column's declared `enum_domains` entry.

    Args:
        emit: The open emit.
        stream: The declared stream.
        notice_sink: The caller-supplied sink the out-of-domain notices
            flow through.

    Returns:
        The record_ids whose events the stream carries — codec-encoded
        strings, the type the engine's shipped str-keyed row-scoping
        device compares (base-layer BIGINT ids cross the codec seam here,
        once, at resolution) — or None when the stream declares no
        selection this function owns (all rows in scope).

    Raises:
        ExportError: A `where` key fails the constant-column gate or is
            unresolvable; a value is uncastable under the column's
            declared type.
    """


def iter_stream_events(
    emit: Emit,
    config: StreamConfig,
    anchor: EffectiveAnchor | None,
    notice_sink: NoticeSink,
) -> Iterator[StreamEvent]:
    """The shipped engine entry point, gaining the required caller-supplied
    sink (the notice-channel posture: required, no default — a caller
    wanting silence passes a discarding sink).

    The eager validation pass emits the per-element
    `discriminator-value-unobserved` notices through it, before any fold
    materializes; the pass otherwise raises as shipped. Every consumer
    threads it: the stream driver paths (the CLI passes the stderr
    renderer) and the mixer's seed_mixer_run, which gains the same required
    parameter and passes it through (the mixer CLI passes the stderr
    renderer).

    Args:
        emit: The open emit (as shipped).
        config: The stream config (as shipped).
        anchor: The resolved effective anchor, or None (as shipped).
        notice_sink: The caller-supplied sink plan-time notices flow
            through.

    Returns:
        The seq-stamped event iterator, as shipped.

    Raises:
        ExportError: The shipped eager-pass rules plus this design's
            vocabulary, naming, selection, and change-scope gates.
    """
```

## Validation Rules

### Parse-Time (Pydantic)

Extending the shipped `kind_stream_well_formed` / `membership_stream_well_formed`
and `StreamConfig` validators; the shared rename- / where-map helpers are
reused verbatim:

```python
@model_validator(mode="after")
def kind_stream_well_formed(self) -> Self:
    """Adds: rename map non-empty when present, non-empty keys and
    non-empty targets, no two keys one target; where map non-empty when
    present, non-empty keys (value
    shape carried per entry by PredicateValue); only/ignore mutually
    exclusive, each distinct, non-empty when present; kind_label non-empty
    when present."""

@model_validator(mode="after")
def membership_stream_well_formed(self) -> Self:
    """Adds: sub_types non-empty and duplicate-free when present; rename /
    where / kind_label as kind_stream_well_formed."""

@model_validator(mode="after")
def kind_labels_well_formed(self) -> Self:
    """StreamConfig: kind_labels non-empty when present; keys and values
    non-empty; no two keys share one label."""
```

### Business Rules

All run in the shipped eager pass (before any fold materializes), every
message leading with `stream '{name}'`; the vocabulary and naming rules are
config+sidecar-only, the selection value casts read the sidecar's type
declarations, the out-of-domain value check emits through the pass's
caller-supplied `notice_sink`, and the uniqueness/uniformity rows below are
the shipped election gates at their new granularity:

| Rule | Checks | Error Message |
|---|---|---|
| `StreamRenameUnresolvable` | every `rename` key names a selected property (field) of its stream | `"stream '{name}': rename key '{key}' names no selected property"` (field-variant for membership) |
| `StreamOutputNameCollision` | per stream, output keys pairwise distinct and disjoint from the reserved names — the identity entry's contract column, `presentation_id` when it ships, membership `event` | `"stream '{name}': output name '{key}' collides with '{other}'"` |
| `StreamKindLabelUnknown` | every `kind_labels` key names a sidecar kind | `"kind_labels: '{kind}' is not a kind in this emit"` |
| `StreamKindLabelCollision` | no label / per-stream `kind_label` equals a **different** kind's rendered name, over the emit's whole kind universe (two streams — of any kinds — sharing one `kind_label` is legal; the rule is the masquerade refusal, not cross-stream uniqueness) | `"stream '{name}': kind_label '{label}' collides with kind '{kind}'"` (config-level variant without the stream prefix for `kind_labels`) |
| `StreamWhereNotConstant` | every `where` key names a `constant`-class payload property of the subject kind (tracked and slice_only refused) | `"stream '{name}': where key '{key}' is not a constant-class property of kind '{kind}'"` |
| `StreamWhereOnDiscriminator` | no `where` key names the subject kind's declared discriminator | `"stream '{name}': where key '{key}' is the discriminator; use sub_types"` |
| `StreamWhereColumnUnresolved` | every `where` key resolves to a payload property of the subject kind (structural columns, element fields, unknown names refused) | `"stream '{name}': where key '{key}' is not a payload property of kind '{kind}'"` |
| `StreamWhereValueUncastable` | every `where` element casts to its resolved column's sidecar-declared type | `"stream '{name}': where value '{value}' does not cast to {type} for '{key}'"` |
| out-of-domain `where` value | element outside the column's declared `enum_domains` entry | Per-element `discriminator-value-unobserved` **notice**, never an error; leads with `stream '{name}'`, shipped two-case wording (topic-will-be-empty when no element of the entry was observed / element-contributes-no-events otherwise); emitted in eager-pass iteration order (streams → `where` keys → elements) |
| `StreamChangeScopeUnresolvable` | every `only` / `ignore` entry resolves to a `prop__` column of the stream's kind | `"stream '{name}': {field} entry '{property}' has no prop__{property} column on kind '{kind}'"` |
| `StreamPropertySliceOnly` (extended) | no `only` / `ignore` entry resolves to a non-exempt `slice_only` column — the refuse-only posture over the new surface | The shipped message shape, naming the entry's field |
| `StreamSubTypesRequireSubtyping` / `StreamSubTypesDeclared` (extended) | membership owner `sub_types` only on a sub-typed owner kind; every value in the owner's declared domain | The shipped messages, over the owner kind |
| Stream key uniformity (granularity change) | membership-shaped: every population of the **addressed owner set** (declared `sub_types`, else the full domain) elects one surface | `ElectionMixedIdentity` / `ElectionUnionUnsafe`, as shipped |
