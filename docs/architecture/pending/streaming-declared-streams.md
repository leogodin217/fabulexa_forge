---
status: draft
---

# Streaming Declared Streams, Key Election, and `init --mode streaming`

---

## Problem

The streaming exporter is broken from the consumer's point of view in four
compounding ways, and it lacks the onboarding engine both other author-facing
modes ship.

1. **Sparse, unrealistic after-images.** One `kinds` entry carries one
   `properties` list shared by every sub-type of the kind. A polymorphic kind —
   `entity` splitting into `product` / `infrastructure` / `monitoring` — streams
   NULLs for columns a row's sub-type does not declare, even though the sidecar's
   `sub_type_columns` partition states exactly which value columns each declared
   sub-type owns (a NULL in a non-owned column is *structurally inapplicable*,
   not unrecorded). In the real world those entities live in different systems
   and arrive on different feeds; none of them shares a stream, and none carries
   another entity's columns.

2. **No author-owned stream naming or combining at the selection.** The
   dimensional and source modes both put the output name on the declaration
   (`name: entity_product`, `sub_types: [product]`). Streaming instead routes
   naming through a two-layer indirection: a `topic_template` renders
   intermediate names, then a `groups` map regroups them. To rename one topic the
   author writes a `groups` entry keyed by a rendered name they must first
   predict; to name the degenerate case (a kind whose only declared sub-type
   value is `default`, which topic-names as `default`) the NHS example carries a
   half-page comment explaining the `{kind}`-template workaround:

   ```yaml
   # today: rename via template + groups indirection
   routing:
     topic_template: "nhs.{kind}"
     groups:
       nhs.patient: [nhs.actor]
   ```

3. **The event set follows the column selection.** A stream fires a `u` only at
   change points of *its selected* properties, so a feed is silent about every
   change to a column it does not carry — and an identity-only stream
   (`properties: []`) carries no `u` events at all. No real CDC feed works this
   way: a row-level connector emits an event whenever the row changes, whatever
   columns the consumer projects. The thin-feed use case — "something changed on
   this identity; dereference it yourself" — is inexpressible.

4. **The message key is always `record_id`** — the internal synthetic id. A real
   connector keys on the table's primary key, which in the masqueraded app
   database is the app-visible identity (the `presentation_id` surface), not a
   simulation-internal id. Every other mode already lets the author elect the
   exported identity per population through the shipped `keys` block
   ([`key-election.md`](../key-election.md)); streaming — the mode whose whole
   Debezium format is a realism masquerade — is the one mode that cannot. A
   customer joining a stream against a key-elected source or base export hits
   mismatched identities.

5. **No `init --mode streaming`.** Dimensional and source ship sidecar-driven
   `init` proposal engines; a streaming author starts from a blank YAML and
   discovers the emit's kinds, discriminator domains, per-sub-type columns, and
   membership tables by trial and error.

## Solution

Four coordinated changes, one grammar:

- **Declared streams** — the source exporter's declared-table grammar transposed
  to an event stream. Each stream is declared by name; the name *is* the topic.
  A stream addresses the populations of exactly one kind (the sub-type atom
  `(kind, sub_type)`, degenerating to `(kind)` for a flat kind) or exactly one
  membership table. Combining sub-types is listing them in one stream; renaming
  is editing `name`. The Layer-B routing policy (`topic_template` / `groups`) is
  retired; the per-event leaf table (Layer A) survives as the Debezium
  `source_table` identity.
- **Events are the facts; columns are a lens.** A stream's event set is the
  population's — every `c`, every `d`, and a `u` at every change point of the
  kind's tracked columns — and the `properties` list controls only the payload
  projection. `properties: []` is a notification feed: the same events, identity
  only. This makes `state-changes` consistent with `membership-events` (whose
  `join`/`leave` events were always payload-independent) and aligns streaming's
  event set with the playback seam's selection-invariant row set.
- **Key election.** `StreamConfig` gains the cross-mode `keys` block; streaming
  becomes the fourth consumer of the shipped key-election surface. The elected
  surface is the message key — the Kafka key, the rendered `key: {…}` map, the
  `d` tombstone's key-only image — and, because referential integrity is
  non-negotiable, every reference the after-image carries renders in its
  *target's* elected surface, exactly as in every other mode.
- **`init --mode streaming`** proposes the realistic default — one stream per
  `<kind>_type` (per population), each stream's `properties` pre-filled from
  that sub-type's `sub_type_columns` partition, plus the aligned `keys` block —
  as a commented candidate config, pure in `(emit, code version)`, self-gated,
  following the two shipped init engines' conventions.

```yaml
content: state-changes
streams:
  - name: product                    # the topic — author-owned, verbatim
    kind: entity
    sub_types: [product]
    properties: [category, price_cents]
  - name: infrastructure
    kind: entity
    sub_types: [infrastructure]
    properties: [status, capacity]
  - name: pathway_episode            # flat kind: one stream, renamed freely
    kind: journey_instance
    properties: [current_state]
keys:
  entity: presentation_id            # the app-visible key is the message key
```

## Affected Subsystems

- **Streaming config grammar** (`StreamConfig` envelope) — the `kinds` and
  `memberships` selection lists and the `RoutingConfig` block are replaced by
  one `streams` list of named declarations, content-conditional in shape
  (kind-shaped for `state-changes`, membership-shaped for `membership-events`),
  modeled as two declaration types so an illegal shape is unrepresentable.
  `table_identity` moves into `DebeziumConfig`, its only consumer. `StreamConfig`
  gains the cross-mode `keys` block.
- **Streaming engine** — materializes one fold per declared stream (not per
  kind/table) with a payload-independent event set, applies the stream's
  `sub_types` scope post-fold, merges per-stream event iterators under a
  canonical key whose source-identity component is the declared stream name,
  and renders keys and after-image references through the election's identity
  join relations. Topic stamping becomes the stream name; the run's topic set
  becomes the declared name set.
- **Derivations layer** (the row-state-events resident) — the fold contract
  splits its one property-set role in two: a **change-scope** set governing
  event membership (a `u` at each change point of the scope's tracked subset)
  and an after-image **projection** set, independently scoped. The fold's
  SELECT emits exactly the projection set, so the single
  column-order-producer rule (fold SELECT, after-image keying, Debezium value
  schema — one `resolve_stream_columns` order) holds verbatim. Shipped
  consumers pass equal sets and keep their behavior exactly: source's event
  log passes the audited set for both scopes (its audited-change-point
  event-membership rule is load-bearing and untouched), the playback seam
  passes the full set for both (its normative full-set invocation rule,
  untouched). Streaming is the one consumer that splits them — change scope
  = the kind's full tracked set, projection = the stream's declared
  `properties`. The membership-events fold is unchanged (its event set was
  always payload-independent).
- **Key-election surface** — gains its fourth consuming mode. The resolution
  gates, union-safety algebra, identity join relations, and the render-time
  uniqueness guard are reused as shipped; streaming contributes its own
  combination gate (one stream, one key surface) and render sites (message key,
  after-image identity, after-image references).
- **Streaming routing surface** — Layer B (template rendering, grouping) is
  retired, and with it three of the five routing business rules; the sub-type
  selection pair is replaced by the declared-stream `sub_types` rules
  (§ Validation Rules). Layer A narrows to the per-event leaf-table attribute
  (`route_table`) consumed solely by the Debezium `source_table` masquerade.
- **Debezium format** — value schemas are built per stream (one declared column
  list per topic), so the per-topic schema-ambiguity constraint holds by
  construction and its rule is retired. The message key stays the
  never-schema-wrapped rendered key map (the shipped rule — the value-only
  stream emits no key message); its one entry follows the stream's elected
  surface. `table_identity` keeps both values with unchanged meaning; only its
  config home moves.
- **Streaming init engine** — new: the `init --mode streaming` proposal
  contract (below), a sibling of the dimensional and source engines, including
  the self-gated `keys` proposal.
- **CLI** — `init --mode` grows a `streaming` arm; the `stream` verb's flag
  surface is unchanged.
- **Mixer control plane / consumer** — both consume the run's topic set, which
  is now the declared stream-name set; the producer's message-key encoding
  follows the elected surface (the same rendered key map the sinks deliver).
  The control-API contract is unchanged.
- **Recipes and example configs** — the streaming recipes and the NHS / retail
  / demo stream presets are rewritten to the declared-stream grammar (they are
  test-guarded config surfaces, not code).

## What Doesn't Change

- The `content` axis (`state-changes` / `membership-events`), single-content
  per run, the `c`/`u`/`d` and `join`/`leave` event shapes, and the
  membership-events fold's faithful-unpivot rules. (What `u` events *exist* for
  a state-changes stream changes — see § Events are the facts.)
- The format axis (`jsonl` / `debezium`) and both formats' rendered shapes:
  the JSONL object layout, the pinned encoder, Debezium's envelope, insert-only
  membership rendering, and the `d` key-only before-image. (What the key map
  *contains* follows the election — see § Message-key election.)
- The sink axis (`stdout` / `file` / `kafka`), single-partition topics,
  flush-before-return, pre-created topics, and the declared-but-empty-topic
  guarantee (empty file / pre-created empty topic / zero count per declared
  stream).
- `ts` rendering (Python-side, absolute-frame, offset-preserving) and anchor
  resolution.
- `seq`: the 1-based position in the merged global order, stamped at the merge.
- The internal canonical *ordering* key: events still order and tiebreak on
  `record_id` regardless of the elected message key — election changes what the
  consumer keys on, never how the stream is sequenced.
- Pacing (`clock` × CLI knobs) and the mixer scheduler — timing overlays over
  the merged stream, untouched.
- The engine's slice-only posture: selecting a non-exempt `slice_only` property
  is refused (refuse-only, no notices) — the notice-emitting omission lives in
  `init`, which never proposes such a column.
- The playback seam's canonical total order and entry-point-invariant `seq`
  remain seam-owned; streaming's conformance statement is restated below (one
  divergence is retired, two are added).
- `properties` / `fields` entries stay bare names (no `prop__` / `elem__` /
  `member__` prefixes). Per-column output renaming inside after-images is not
  part of this design.

## Semantics

### Stream declarations resolve to populations

The declaration unit mirrors the source exporter's: a stream addresses
populations of exactly one kind, or exactly one membership table. Combination
is same-kind-only, because column shape forces it.

| Declaration | Resolves to |
|---|---|
| `kind: K` (flat kind) | `(K)` — the whole kind |
| `kind: K` (sub-typed kind, `sub_types` omitted) | Every declared sub-type of `K` — shorthand for the full discriminator domain |
| `kind: K, sub_types: [a, b]` | `(K, a)` and `(K, b)` — a deliberate combined stream |
| `kind: K, sub_types: […]` where `K` is flat | Error — a flat kind has no populations to address |
| `membership: {kind: K, property: p}` | The one `membership__<K>__<p>` table |
| A kind / sub-type / membership table appearing in no declaration | Not streamed. Omission is the exclusion mechanism |
| Two streams covering the same population | Legal — both stream it (the source-mode overlapping-declaration posture); each event of the shared population appears once per covering stream, with distinct `seq` |
| Two streams declaring one `name` | Error — never a silent merge. Cross-stream topic merging is not expressible; a combined feed is declared as one stream |

`name` is author-verbatim and is the topic: the Kafka topic, the
`<name>.jsonl` filename, and the `events_per_topic` key. There are no default
stream names to derive (`init` proposes names verbatim from sidecar identity).
Because the sink is a CLI flag — a config never knows its sink — `name` must
be legal for all three up front: it is validated at parse time against the
topic-name rule the retired grammar applied to `groups` targets, carried over
verbatim — `^[A-Za-z0-9._-]+$` and not `.` or `..` (the Kafka topic-name
convention, and a `<name>.jsonl` filename stem safe on every filesystem —
path traversal is unrepresentable). One eager rule covers every sink; no
delivery-time naming verdict remains.

### Events are the facts; columns are a lens

A stream's **event set is payload-independent**: it is a fact about the
stream's populations, never about its column selection.

| Event | Fires |
|---|---|
| `c` | At each in-scope record's `created_sim_time` |
| `d` | At each in-scope record's `deactivated_at` |
| `u` | At every distinct history `sim_time` carrying a change to any of the kind's tracked columns |

The `properties` list controls **only the after-image projection**. Two streams
over one population have the *same* event set; a `properties: []` stream is a
notification feed — the same events, identity-only payload ("something changed
on this identity; dereference it yourself"). A stream selecting only constant
properties carries the same `u` events with those constants as payload. This is
row-level CDC semantics: a real connector emits an event whenever the row
changes, whatever the consumer projects — and it is exactly how
`membership-events` has always worked (`join`/`leave` events are the intervals;
`fields` only projects the payload). One rule now spans both content types.

The slice-only policy is satisfied vacuously at the event level: `slice_only`
implies `history_tracked: false` (the contract's three-way `temporal_class`),
so a `slice_only` column contributes no history rows and has no change points
to fire — no event membership can derive from one by class, not by filter
([`slice-only.md`](../slice-only.md)). Selecting a non-exempt `slice_only`
column stays refused outright (`StreamPropertySliceOnly`, unchanged); the
exempt discriminator stays selectable, whatever its class.

### Per-stream folds and after-images

Each declared stream materializes its **own** fold over its own declaration —
the stream is an independent feed, exactly as its real-world counterpart would
be.

| Condition | Result |
|---|---|
| `state-changes` stream | One row-state-events fold over the kind, `u` rows at the tracked columns' change points; rows whose discriminator is outside the stream's `sub_types` scope are dropped before the merge (post-fold, via the discriminator index — the shipped mechanism) |
| `membership-events` stream | One membership-events fold over the declared table and `fields`, unchanged |
| After-image column set | Per stream: the stream's declared `properties` resolved in the kind's column order — the single column-order producer is consulted per stream (it auto-includes `presentation_id` when the kind carries a surrogate, as shipped; § Message-key election for its absorption under a `presentation_id` election), so the Debezium value schema and the rendered rows remain the same list by construction |
| After-image identity | The record's exported identity, rendered in its population's elected surface (§ Message-key election); identity is never read from an after-image — it rides the fold's `record_id` and renders through the identity join |

The payload-independent event set is realized **in the shared fold, not by
engine-side trimming**. The shipped row-state-events derivation derives its
`u` rows from the *selected* properties' tracked subset — one property set
serving both event membership and the after-image. That contract splits (§
Affected Subsystems): the fold takes a change-scope set (event membership)
and a projection set (the after-image) independently. Streaming invokes every
kind-shaped stream's fold with change scope = the kind's full tracked set and
projection = the stream's declared `properties`, so the event set is a fact
of the population while the SELECT still emits exactly the declared columns
in the single producer's order — no engine-side column trimming, and the
schema ↔ row agreement invariant holds by the same construction as today.
Source and playback invoke the same fold with equal sets and are unaffected.

The former invariant "sub-types of one kind share an after-image column set" is
deliberately retired: the after-image column set is now a per-stream fact. A
combined stream (several `sub_types`, one `properties` list) has one column
set; rows carry NULL in a selected column their sub-type does not declare —
the author chose the combination, and the NULL is the faithful rendering of
structural inapplicability. `init` never proposes a combined stream.

### Message-key election

`StreamConfig` gains the cross-mode `keys` block — the same grammar, surfaces,
and defaults as [`key-election.md`](../key-election.md) § The election grammar:
per population, elect `record_id` (the default), `record_index`, or
`presentation_id`. Streaming reuses the shipped machinery wholesale — the
resolution gates, the union-safety algebra, the identity join relations, and
the render-time uniqueness guard — and adds only its own combination gate and
render sites.

| Render site | Rendering |
|---|---|
| Message key (every op, including the `d` tombstone) | The record's elected surface: the Kafka key and the `key: {…}` map (one entry, keyed by the surface's contract column name — `record_id` / `record_index` / `presentation_id`), never schema-wrapped (the shipped rule — the value-only stream emits no key message). The Debezium `d` key-only before-image carries the same one entry. For `membership-events`, the **owner's** elected surface |
| After-image identity (`c`/`u` rows; the membership `after`'s owner entry) | The elected surface, via the identity join at the fold's `record_id`, keyed by the surface's contract column name. A state-changes `d` keeps `after: null` (the shipped layout) — its identity lives in the message key and the Debezium key-only before-image, both elected-surface-rendered. Under a `presentation_id` election the standalone `presentation_id` payload column is absorbed (it *is* the identity — emitting both would duplicate a column); under `record_id` / `record_index` it ships verbatim whenever the kind carries one (auto-included by the column-order producer, as shipped — it is never property-selectable) |
| Reference-valued `prop__<p>` entries in the after-image | The **target's** elected surface, translated through the target's identity join — referential integrity is non-negotiable: a consumer must be able to join any stream against any other elected output on equal values |
| Membership `member__<f>` reference fields | The member row's kind's elected surface (the `__kind` component remains the disambiguator), the junction-member analog |

Two render facts span the table. **One codec:** elected values keep the
streaming codec at every site — the key map, the after-image identity entry,
reference `prop__` entries, membership `member__<f>` fields — codec `VARCHAR`
(`str`) or `null`, exactly as every after-image value ships today.
`record_index` renders digit-form, `presentation_id` the codec rendering of
its sidecar-declared value; no site emits a typed JSON number, so
serialization stays total and the byte-determinism invariant needs no new
case. **The membership owner entry re-keys:** in both formats' membership
`after` (JSONL, and Debezium's `{event, …}` payload) the owner-identity entry
is keyed by the owner's elected surface's contract column name; the
element-field format-parity invariant carries over unchanged.

Contract consequences, all inherited from the shipped surface:

- **One stream, one key surface.** A stream is a topic, and a topic's key is
  one identity space. Every population a stream's *keys* draw from must elect
  the same surface: for a kind-shaped stream, the spanned populations (the
  declared `sub_types`, or the full domain under the shorthand); for a
  membership-shaped stream, the owner kind's full domain (its owners span it).
  Violation is `ElectionMixedIdentity`, naming the stream. Under a uniform
  `presentation_id` election the spanned key spaces must be pairwise union-safe
  (`ElectionUnionUnsafe`) — the identity-column posture, verbatim.
- **Edges gate per column.** Each after-image reference column and each
  membership member field runs the shipped edge union-safety gate over its
  admitted target populations' resolved surfaces (`ElectionUnionUnsafe`,
  naming the stream and column). Streaming's admitted set is the
  kind-targeted modes' (source's and base's): the target kind's full declared
  domain for a reference column, per member kind for a membership member
  field — a stream's `sub_types` scope narrows its *own* rows, never which
  target populations an edge admits. Per-row mixed-election rendering on edges
  resolves the target row's population from the records-spine discriminator,
  the shipped per-row rule.
- **Compaction stays coherent.** Every electable surface is creation-constant
  (`record_index` by construction; `presentation_id` genesis-minted, never
  re-minted), so a record's `c`/`u`/`d` events keep one key for its whole life
  and the `d` keys the tombstone — the property the always-`record_id` design
  existed to protect, now guaranteed by the election gates instead.
- **The uniqueness guard runs.** Every composed identity relation asserts
  `rows = DISTINCT record_id = DISTINCT elected value`, elected value non-NULL,
  over the population set drawn through it (`ElectedKeyDuplicate` on
  violation). Streaming composes every relation at the end-of-tape entry point:
  a record's creation precedes its every event, the same argument that fixes
  the event log's horizon.
- **Ordering is untouched.** The canonical order and merge key still read the
  fold's `record_id`; the election renders identity, it does not re-sort.

No `keys` block → `record_id` throughout: every identity render site — the key
map, the after-image identity entry, reference `prop__` entries, membership
`member__<f>` fields — renders byte-identically to today. (Identity rendering
only: the event set and topic names move with the grammar whether or not `keys`
is written.)

### Merge order, `seq`, and the seam divergences

The global order over all events of all declared streams is

> `(event_sim_time ASC, event_class ASC, stream_name ASC, record_id ASC[, field-value tail])`

— the shipped canonical order with the source-identity component now the
**declared stream name** (the per-stream constant; unique by the name-collision
rule, so the inter-stream tiebreak stays deterministic and no field values are
ever compared across folds). Within one stream the order is unchanged. `seq` is
stamped at the merge exactly as today.

Under overlapping streams the same base change yields one event per covering
stream — distinct `seq`, distinct topic, identical key, identical event set,
after-image content differing only by each stream's projection. Faithful
reshaping holds: every event still traces to base values; duplication across
declared feeds is declared intent, the streaming analog of two source tables
rendering one population.

The playback-seam conformance statement is restated:

- **Retired divergence:** the tracked-subset `u`-row-set divergence. The event
  set is now selection-invariant, matching the seam's row set by design.
- **Interleave divergence (new):** where the seam's canonical order tiebreaks
  on the record `kind` / `(owner_kind, property)` identity, a declared-stream
  run tiebreaks on the author's stream names — the interleave of same-instant,
  same-class events across streams follows declaration naming, not bundle
  identity. Per-topic sequences are unaffected (a topic is one stream); only
  the cross-topic interleave (stdout order, global `seq` assignment) moves.
- **Multiplicity divergence (new):** the seam plays each in-scope base event
  once; overlapping declared streams emit one event per covering stream. Like
  the interleave, multiplicity follows declaration.
- The membership field-subset intra-instant ordering tail spans the subset, as
  shipped — unchanged.

Determinism is unaffected: same emit + same config + same code →
byte-identical stream.

### Topics, empty streams, and the Debezium identity

| Condition | Result |
|---|---|
| Run's topic set | The declared stream names, in declaration order |
| A declared stream yields zero events | Its topic still exists: empty `<name>.jsonl`, pre-created empty Kafka topic, `events_per_topic[name] == 0` — declared intent, not observed rows, drives topic existence |
| Event's `topic` | The declaring stream's `name` |
| Event's `route_table` | The per-event leaf logical table: the row's `<kind>_type` discriminator value for a sub-typed kind, the bare kind for a flat kind, `<owner_kind>__<property>` for a membership stream |
| `debezium.table_identity: source_table` (default) | `source.table` reports the event's `route_table` — canonical Debezium: the origin table, even inside a combined stream |
| `debezium.table_identity: topic` | `source.table` reports the stream name |
| Per-topic Debezium value schema | Built from the stream's declared column set — well-defined by construction (one topic = one stream = one column list), for both `table_identity` values; the former per-topic ambiguity rule is retired, not relaxed |
| Message key encoding | The pinned-encoded one-entry map `{<elected surface's contract column>: <codec value>}` on every sink — never schema-wrapped, even under `schemas_enable` (the shipped rule, unchanged); one key space per topic by the one-stream-one-key-surface gate |

### `init --mode streaming` inference contract

`init` proposes; the author edits and owns. The proposal is a commented
candidate config, a pure function of `(emit, code version)`. It consumes the
sidecar's records tables (declaration order), `subtype_values`,
`sub_type_columns`, per-column temporal classes, the records-column taxonomy,
the slice-only policy, the `presentation_keys` registry (for the `keys`
proposal), and the membership tables — **not** `record_roles` (warehouse role
plays no part in a stream). The temporal surface is required as consumed: an
emit predating per-column temporal classes fails with the reader's own refusal
(`TemporalClassUnavailableError`), exactly as the stream engine's slice-only
check does — no dedicated `init` error exists (`SourceHistoryTrackedRequired`
is source's posture because source *export* requires the flags; streaming's
does not). It infers no intent: names are sidecar identity verbatim, and the
degenerate sub-type value `default` is proposed as `name: default` — the
author renames if they care.

Every proposed stream is **live**. The commented-out mechanism is reserved for
genuine alternatives (the membership-events block, collision losers,
topic-illegal names) — never for advice — so the emitted config always parses
and streams clean by construction: a collision pair's first entry always stays
live, and should no proposal survive live at all (every sidecar-derived name
topic-illegal — a degenerate sidecar), `init` refuses rather than emit a
config that cannot parse.

| Emit condition | Proposal |
|---|---|
| ≥ 1 records kind | Live `content: state-changes` config; the membership alternative fully commented (below) |
| Flat kind | One live stream: `name: <kind>`, `properties` = the kind's payload-role `prop__` columns, bare, minus non-exempt `slice_only` (`ref_index__*` are identity-role and never proposed; `presentation_id` is presentation-role and not property-selectable) |
| Sub-typed kind | One live stream per declared sub-type, in `<kind>_type` domain order: `name: <sub_type>` verbatim, `sub_types: [<sub_type>]`, `properties` = that sub-type's `sub_type_columns` payload-role `prop__` entries, bare, minus non-exempt `slice_only`. The discriminator is not proposed (constant within the stream — the partition's contract carve-out already excludes it) |
| Sub-typed kind, sidecar omits `sub_type_columns` | Per-sub-type streams still, each proposing the kind's full payload-role `prop__` set minus the discriminator (constant within a single-sub-type stream) and minus non-exempt `slice_only`, with a comment noting the sidecar carried no partition — the init engines' union-fallback convention (dimensional's posture) |
| A population with no tracked property | Its stream proposed **live**, headed by a comment noting the feed is lifecycle-only (`c`/`d`); deleting it opts out |
| Two proposals resolve one name (e.g. two kinds sharing a sub-type value, or a sub-type value equal to a flat kind's name) | The later proposal (sidecar order) emitted commented out with a comment naming the collision — the emitted config always parses and streams clean, the shipped self-gating posture. The rule spans both content blocks: membership auto-names collide too (`<K>_<p>` is underscore-ambiguous — kind `a_b` × property `c` and kind `a` × property `b_c` both derive `a_b_c`), and inside the fully-commented membership alternative the loser is excluded from the uncommentable body and carried as a collision comment, so uncommenting the block wholesale yields a config that parses and streams clean |
| A proposal whose sidecar-derived name fails the topic-name rule (only a sub-type value can — kind names and membership `<K>_<p>` names are table-name segments, identifier-safe by construction) | Emitted commented out with a comment naming the rule and the offending value — the collision-loser posture; the author renames and uncomments. `init` never sanitizes (a rewritten name would be an invented identity) |
| Key election | The `keys` block per the key-election `init` contract ([`key-election.md`](../key-election.md) § `init` proposals): `presentation_id` for registry-declared populations, `record_index` otherwise, run through streaming's own gates (per-stream uniformity over the proposed single-population streams, edge union safety over the proposed after-images' reference columns and membership member fields); every kind implicated in a failure degrades to uniform `record_index` with a comment naming the gate |
| Each `membership__<K>__<p>` table | One membership stream in the fully-commented `content: membership-events` alternative block: `name: <K>_<p>`, `membership: {kind: <K>, property: <p>}`, `fields` = every element-schema field (bare names) |
| No records kind | Error — a candidate config that cannot stream is not proposed. A membership table cannot exist without its owner's records table (an interval requires an owner record within the slice — the contract's § Membership-category and § Records-category existence rules), so a recordless emit has nothing to stream and no membership-only branch exists |
| Non-exempt `slice_only` columns | Never proposed; one `slice-only-column-omitted` notice each through the caller-supplied `NoticeSink` |
| `rebase` / `debezium` / `clock` / `kafka` blocks | Never proposed — delivery and environment knobs, not emit-derived (no invented identities or endpoints); one trailing comment names them and where they would go |

## Configuration

```yaml
# state-changes — one declared stream per <kind>_type (init's default shape)
content: state-changes

streams:
  - name: default                    # actor's declared sub-type value, verbatim
    kind: actor
    sub_types: [default]
    properties: [status, admission_count]
  - name: product
    kind: entity
    sub_types: [product]
    properties: [category, price_cents]
  - name: infrastructure
    kind: entity
    sub_types: [infrastructure]
    properties: []                   # notification feed: full event set, identity-only payload
  - name: pathway_episode            # renamed flat kind
    kind: journey_instance
    properties: [current_state]
  # combining sub-types — one stream, one name, one column list:
  # - name: catalog
  #   kind: entity
  #   sub_types: [product, infrastructure]
  #   properties: [category, status]

keys:                                # the cross-mode election grammar, verbatim
  entity: presentation_id
  actor: record_index

debezium:                            # unchanged block, plus table_identity's new home
  table_identity: source_table
  schemas_enable: true
  source: {connector: postgresql, name: retail, db: shop, schema: public, version: "2.5"}
```

```yaml
# membership-events — same envelope, membership-shaped streams
content: membership-events

streams:
  - name: queue_waiters
    membership: {kind: queue, property: waiters}
    fields: [priority, patient]
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `content` | `state-changes` \| `membership-events` | Yes | Unchanged: selects the fold family and the required declaration shape |
| `streams` | list | Yes, non-empty | The declared streams. Replaces `kinds`, `memberships`, and `routing` |
| `streams[].name` | str | Yes | The topic. Author-verbatim; matches the topic-name rule (`^[A-Za-z0-9._-]+$`, not `.` or `..`); unique across the config |
| `streams[].kind` | str | Kind-shaped streams | The records kind this stream feeds from |
| `streams[].sub_types` | list[str] | No (kind-shaped only) | Population scope; omitted on a sub-typed kind = full domain; forbidden on a flat kind |
| `streams[].properties` | list[str] | Yes on a kind-shaped stream; may be empty | Bare property names projected into the after-image; empty = identity-only payload. The key must be written — omission is an error, `[]` is the deliberate notification feed |
| `streams[].membership` | `{kind, property}` | Membership-shaped streams | The membership table this stream feeds from |
| `streams[].fields` | list[str] | Yes on a membership-shaped stream; may be empty | Bare element-schema field names; empty = owner identity only. Same explicit-empty posture as `properties` |
| `keys` | map | No | The cross-mode key-election block, grammar unchanged ([`key-election.md`](../key-election.md)). Absent = `record_id` throughout |
| `debezium.table_identity` | `source_table` \| `topic` | No (default `source_table`) | Moved from the retired `routing` block; meaning unchanged |
| `routing` | — | — | **Retired.** `topic_template`, `groups` no longer exist |

## Interface Contracts

### Config Models

The two stream shapes are two models — a discriminated union on which of
`kind` / `membership` the entry carries — so a declaration mixing the shapes'
fields is unrepresentable, not validated away, and each shape's required
fields stay required (no silent identity-only feed from a forgotten key).

```python
class MembershipRef(StrictBaseModel):
    """The membership table a membership-shaped stream feeds from."""

    kind: str
    """The kind owning the collection-struct property; with `property`, resolves
    to membership__<kind>__<property>."""

    property: str
    """The collection-struct property naming the membership table."""


class KindStream(StrictBaseModel):
    """A kind-shaped declared stream: an author-named topic fed by one kind's
    populations (content: state-changes)."""

    name: str
    """The topic — author-verbatim, matching the topic-name rule
    (^[A-Za-z0-9._-]+$ and not '.' or '..'). The Kafka topic, the
    <name>.jsonl filename, and the events_per_topic key."""

    kind: str
    """The records kind; resolves to records__<kind>."""

    sub_types: list[str] | None = None
    """Population scope for a sub-typed kind: declared `<kind>_type` values,
    non-empty and duplicate-free when present. Omitted on a sub-typed kind =
    the full discriminator domain. A flat kind refuses it."""

    properties: list[str]
    """Bare property names projected into the after-image — required, no
    default: `[]` must be written to declare a notification feed (identity-only
    payload; the event set is payload-independent). Never `prop__`-prefixed;
    duplicate-free."""


class MembershipStream(StrictBaseModel):
    """A membership-shaped declared stream: an author-named topic fed by one
    membership table (content: membership-events)."""

    name: str
    """The topic — same contract as KindStream.name."""

    membership: MembershipRef
    """The membership table."""

    fields: list[str]
    """Bare element-schema field names — required, no default: `[]` must be
    written to declare an owner-identity-only feed. Never `elem__`/`member__`-
    prefixed; duplicate-free."""


StreamDeclaration = KindStream | MembershipStream
# Discriminated by which of `kind` / `membership` the entry carries. An entry
# with neither or both fails parse with a message naming the two shapes.


class StreamConfig(StrictBaseModel):
    """Streaming delivery envelope: content, the declared streams, and the
    key election."""

    content: Literal["state-changes", "membership-events"]
    """Unchanged: the event content axis."""

    streams: list[StreamDeclaration]
    """The declared streams — required, non-empty, names unique. Every entry's
    shape must match `content` (KindStream for state-changes, MembershipStream
    for membership-events)."""

    keys: dict[str, KeySurface | dict[str, KeySurface]] | None = None
    """Per-kind key election — the ExportConfig.keys grammar and
    keys_well_formed validator, verbatim. Absent: record_id throughout."""

    rebase: RebaseConfig | None = None
    debezium: DebeziumConfig | None = None   # gains table_identity
    clock: ClockConfig | None = None
    kafka: KafkaConfig | None = None
    # optional blocks unchanged in meaning; RoutingConfig is deleted


class DebeziumConfig(StrictBaseModel):
    """Debezium options; read for 'debezium', ignored for 'jsonl'."""

    table_identity: Literal["source_table", "topic"] = "source_table"
    """What source.table (and the value-schema name) reports: the event's
    route_table leaf (canonical Debezium) or the stream name. Moved here from
    the retired RoutingConfig; meaning unchanged."""

    schemas_enable: bool = True
    source: DebeziumSourceIdentity
```

### Functions

```python
def generate_stream_init_config(
    emit: Emit,
    notice_sink: NoticeSink,
) -> str:
    """Propose a commented candidate StreamConfig from an open emit.

    A pure function of (emit, code version): one live declared stream per
    population — per declared sub-type for a sub-typed kind (properties from
    the sub_type_columns partition's payload-role entries; union-set fallback
    with a comment when the sidecar omits the partition), per kind for a flat
    kind — a lifecycle-only population's stream live under an advisory comment,
    name-collision losers and topic-illegal names emitted commented out, the
    keys block proposed and self-gated per the key-election init contract, and
    the membership-events alternative fully commented. Names are sidecar
    identity verbatim; no intent is inferred. Non-exempt slice_only columns
    are never proposed, one notice each. The emitted text always parses into
    a valid StreamConfig and streams clean against this emit (self-gated;
    provable — every proposal is live except collision losers and
    topic-illegal names, a collision pair's first entry stays live, and no
    live proposal at all is a refusal, not an emitted config).

    Args:
        emit: An open, version-gated emit (trusted as conformant; not
            re-validated).
        notice_sink: Required notice channel; receives one
            `slice-only-column-omitted` notice per skipped column.

    Returns:
        The candidate config YAML, commented, ending in a trailing comment
        naming the never-proposed delivery blocks (rebase / debezium / clock /
        kafka).

    Raises:
        StreamInitNothingToStream: The emit carries no records kind (and
            therefore no membership table — an interval requires an owner
            record within the slice), or no proposal survives live (every
            sidecar-derived name topic-illegal) — a candidate config that
            cannot stream is not proposed.
        ReaderError: Sidecar access failures surface unchanged — including
            TemporalClassUnavailableError on an emit predating per-column
            temporal classes (the proposal consumes trackedness for the
            lifecycle-only advisory and the slice-only omission).
    """
```

`StreamInitNothingToStream` is a direct child of `ExporterError` (the
dimensional `InitRequiresRecordRoles` posture: `init` runs no engine and reads
no config, so its failure is not an `ExportError`); the CLI `init` verb reports
it as a clear stderr message with a non-zero exit.

## Validation Rules

### Parse-Time (Pydantic)

```python
# StreamDeclaration discrimination (the union itself):
#   an entry carries exactly one of kind / membership; neither or both fails
#   parse with a message naming the two shapes. The retired exactly_one_source
#   validator has no successor — the illegal shapes are unrepresentable.

@model_validator(mode="after")
def kind_stream_well_formed(self) -> Self:
    """KindStream: name matches the topic-name rule — ^[A-Za-z0-9._-]+$ and
    not '.' or '..' (the retired groups-target rule, carried over verbatim);
    properties never prop__-prefixed and duplicate-free; sub_types non-empty
    and duplicate-free when present."""

@model_validator(mode="after")
def membership_stream_well_formed(self) -> Self:
    """MembershipStream: name matches the topic-name rule (as KindStream);
    fields never elem__/member__-prefixed and duplicate-free (carried over
    verbatim from the retired selection models)."""

@model_validator(mode="after")
def streams_match_content(self) -> Self:
    """StreamConfig: streams is non-empty; every entry is a KindStream for
    content='state-changes' and a MembershipStream for
    content='membership-events' (replaces selection_matches_content)."""

@model_validator(mode="after")
def stream_names_unique(self) -> Self:
    """StreamConfig: no two streams share a name (replaces kinds_unique /
    memberships_unique — same-kind and same-table repeats are now legal;
    identity is the name)."""

@model_validator(mode="after")
def keys_well_formed(self) -> Self:
    """StreamConfig: ExportConfig.keys_well_formed, verbatim — keys (when
    present) is non-empty; every per-kind map is non-empty."""
```

Retired parse-time surface: `RoutingConfig.groups_well_formed` (its
groups-target topic-name rule survives verbatim as the stream-name rule), the
`kinds` / `memberships` content-conditional lists, and `exactly_one_source`
(never written — the union makes it unnecessary). `StreamKindSelection.types_are_bare`
has no successor and needs none: a `prop__`-prefixed `sub_types` value is
simply not a declared discriminator value and fails `StreamSubTypesDeclared`
at the business pass.

### Business Rules

Run in the engine's eager pass, as today; each raises `ExportError` into the
CLI's `(ReaderError, ExporterError)` funnel. Every per-stream rule's message
now leads with the stream name — the author's handle, and (with overlapping
streams legal) the only component that identifies the offending declaration.

| Rule | Checks | Error Message |
|------|--------|---------------|
| `SingleBranch` | unchanged | unchanged |
| `StreamKindResolvable` | each kind-shaped stream's `kind` has a `records__<kind>` table | `"stream '{name}': kind '{kind}' has no records__{kind} table"` |
| `StreamSubTypesRequireSubtyping` | `sub_types` is present only on a stream whose kind has a non-empty `subtype_values` domain | `"stream '{name}': kind '{kind}' is not sub-typed; sub_types is not addressable"` |
| `StreamSubTypesDeclared` | every `sub_types` value is in the kind's declared domain | `"stream '{name}': sub_type '{value}' is not declared for kind '{kind}'"` |
| `StreamPropertyResolvable` | each selected property resolves to a `prop__` column on the stream's kind | `"stream '{name}': property '{property}' has no prop__{property} column on kind '{kind}'"` |
| `StreamPropertySliceOnly` | no selected property is non-exempt `slice_only` (refuse-only, no notices) | unchanged check; message gains the `stream '{name}'` prefix |
| `MembershipResolvable` | each membership-shaped stream's table exists | `"stream '{name}': membership '{kind}.{property}' has no membership__… table"` |
| `MembershipFieldResolvable` | each selected field resolves on its table | `"stream '{name}': field '{field}' has no elem__/member__ column"` |
| Election resolution gates | `ElectionKindUnknown` / `ElectionSubTypeUnknown` / `ElectionPresentationUndeclared` — reused verbatim from the shipped surface | unchanged (shipped messages) |
| Stream key uniformity | one stream, one key surface: every population the stream's keys draw from elects the same surface (kind-shaped: the spanned populations; membership-shaped: the owner kind's full domain); uniform `presentation_id` additionally pairwise union-safe | `ElectionMixedIdentity` / `ElectionUnionUnsafe`, naming the stream and the differing (population, surface) pairs |
| Edge union safety | per after-image reference column and per membership member field, admitted target populations' resolved surfaces pairwise union-safe; the admitted set is the kind-targeted posture — the target kind's full declared domain (per member kind for a member field) | `ElectionUnionUnsafe`, naming the stream, the column, and the unsafe pair |
| Elected-key uniqueness | render-time, per composed identity relation at the end-of-tape entry point: `rows = DISTINCT record_id = DISTINCT elected value`, elected value non-NULL | `ElectedKeyDuplicate`, naming the stream or edge and the surface |
| Debezium / Kafka / clock rules | unchanged, in their shipped homes and order | unchanged |

Retired business rules: `StreamTypesRequireSubtyping` / `StreamTypesDeclared`
(replaced by the `sub_types` pair above), `StreamTemplatePlaceholders`,
`StreamGroupMembersResolve`, and `StreamTopicSchemaUnambiguous` (its guarantee
now holds by construction: one topic = one stream = one declared column list,
and one key surface by the uniformity gate).
