# Streaming Exporter

**Status:** Implemented. Code is the contract — see
[`exporters/streaming/`](../../src/fabulexa_forge/exporters/streaming/)
(`types.py`, `engine.py`, `jsonl.py`, `driver.py`, `init.py`),
[`config/`](../../src/fabulexa_forge/config/) (`StreamConfig`,
`load_stream_config`), and
[`tests/exporters/streaming/`](../../tests/exporters/streaming/),
[`tests/config/test_stream_config.py`](../../tests/config/test_stream_config.py),
[`tests/test_cli_stream.py`](../../tests/test_cli_stream.py). Public API:
[`exporters/streaming/__init__.py`](../../src/fabulexa_forge/exporters/streaming/__init__.py).

The `fabulexa-forge stream` verb replays the base layer as an ordered, temporally-honest
event stream of author-**declared streams**. Each stream is declared by name, and the name
*is* the topic — the Kafka topic, the `<name>.jsonl` filename, the `events_per_topic` key.
A stream feeds from the populations of exactly one kind (one or more declared sub-types,
or the whole kind) or from exactly one membership table — the source exporter's
declared-table grammar transposed to an event stream, so a polymorphic kind's sub-types
stream as the independent feeds their real-world counterparts would be, each with its own
name and its own column list. The verb carries two content axes. `state-changes` replays
the `history` change ledger: the bundle's `history` table is a change-event ledger ordered
by `sim_time`, and `records__<kind>` carries each record's `created_sim_time` /
`deactivated_at` / `active` lifecycle spine and its current type-1 property values;
together they are a natural CDC stream — the shape a Debezium connector emits off an OLTP
database — and the exporter reconstructs, per record, the full row at every instant the
row changed and emits those reconstructions as ordered `c`/`u`/`d` change events.
`membership-events` replays the `membership__<K>__<p>` interval tables: each materialized
membership interval unpivots into a `join` event and, when the element left within the
slice, a `leave` event, sourced directly from the interval tables (collection-valued
property changes emit no `history` rows, so a history-sourced fold is blind to them). It
is a **delivery driver, not a shape-mode**: it declares no `mode`, no target schema, no
grain, and carries its own top-level `StreamConfig` envelope, a sibling of `ExportConfig`.
It reads through the Stage-1 reader only, composes the derivations layer's
row-state-events fold (`state-changes`) or membership-events fold (`membership-events`)
for its event content, and is the fourth consumer of the cross-mode key-election surface
([`key-election.md`](key-election.md)) — the elected surface is the message key.

Each declared stream carries the three author surfaces the batch modes carry, with their
semantics: an **output vocabulary** (per-stream `rename`, plus `kind_labels` /
`kind_label` wherever a kind name renders as a value), **row selection** (`where` on both
stream shapes, owner `sub_types` on a membership stream), and **change scope** (`only` /
`ignore` on a kind stream). Vocabulary is presentation and never reaches event membership;
selection and change scope decide which events exist and never reach how survivors order.

```
emit (run.duckdb + base.json @ the supported `base_format_version`)
   │  (reader: Emit + Sidecar; trunk-only — sole branch)
   ▼
content fold (one per declared stream, derivations layer)
   state-changes:     row-state-events  (c at created_sim_time | u at each later history sim_time | d at deactivated_at)
   membership-events: membership-events (join at joined_sim_time | leave at left_sim_time when non-null)
   one full payload (after-image, the stream's declared projection) per event
   ▼
engine: materialize each stream ▸ (row scope: sub_types × where) ▸ k-way merge by canonical order ▸ stamp global seq
      ▸ stamp topic = stream name + route_table (leaf) ▸ render elected key ▸ resolve output keys + kind vocabulary ▸ render ts
   ▼
format: jsonl {seq, op, ts, kind, key:{<elected>}, after}
      | debezium {schema?, payload:{before, after, source, op, ts_ms, transaction}} ▸ sink
   stdout (all topics interleaved, global seq order) | file (one <topic>.jsonl per topic)
```

---

## Surface

| Module | Owns |
|---|---|
| [`config/models.py`](../../src/fabulexa_forge/config/models.py) | `StreamConfig` (the `content` axis, the `streams` declaration list, and the cross-mode `keys` election block), the `StreamDeclaration` union — `KindStream` (name / kind / `sub_types` / `identity` / `properties`) and `MembershipStream` (name / `MembershipRef` / `identity` / `fields`), discriminated on which of `kind` / `membership` an entry carries so a shape-mixing declaration is unrepresentable — the optional `DebeziumConfig` (home of `table_identity`) / `DebeziumSourceIdentity` block, and the optional `KafkaConfig` connection block — the top-level streaming envelope and its parse-time validators |
| [`config/loader.py`](../../src/fabulexa_forge/config/loader.py) | `load_stream_config` — YAML → validated `StreamConfig`, hard-bound (no mode dispatch) |
| [`exporters/streaming/types.py`](../../src/fabulexa_forge/exporters/streaming/types.py) | `StreamEvent` (one format-agnostic change event — `op` admits `c`/`u`/`d` and `join`/`leave`; carries `topic`, `route_table`, and the rendered `key_column` / `key_value` pair) and `StreamOutcome` (run counts) |
| [`exporters/streaming/engine.py`](../../src/fabulexa_forge/exporters/streaming/engine.py) | `iter_stream_events` — the up-front business-rule pass (per-stream resolvability, the vocabulary / naming / selection / change-scope gates, the election gates), per-stream fold materialization (kind-shaped: change scope = the stream's declared audited set, projection = the declared `properties`; membership-shaped: the declared table and `fields`), post-fold row scoping (the `sub_types` discriminator index and the resolved satisfying-record set), the cross-stream k-way merge, `seq` stamping, per-event `topic` / `route_table` stamping, identity-projection resolution and gating (`resolve_identity_projection` / `resolve_stream_identities` — the per-stream gated published-surface set), elected-key rendering, output-key assembly, and Python-side `ts` rendering; `build_topic_set` — the run's topic set (the declared names, declaration order) |
| [`exporters/streaming/presentation.py`](../../src/fabulexa_forge/exporters/streaming/presentation.py) | The per-stream naming and vocabulary resolution — `IdentityProjection` / `OutputEntry` (the runtime types), `resolve_identity_output_key` (the single producer of every published identity surface's wire name), `resolve_stream_output_columns` / `resolve_membership_output_columns` (the single naming authority: the ordered `OutputEntry` list — identity entries and payload entries with their resolved output keys — both the after-image assembly and the Debezium value-schema build read), `resolve_stream_kind_vocabulary` (the run's kind vocabulary, validated and returned as the declared value mapping), `resolve_stream_envelope_kind`, and `apply_kind_vocabulary` (the per-value identity-fall-through map). Pure config + sidecar; imports neither the engine nor the drivers |
| [`exporters/streaming/selection.py`](../../src/fabulexa_forge/exporters/streaming/selection.py) | `resolve_stream_selection` — a stream's satisfying record set (owner set, membership-shaped) from its declared `where` / owner `sub_types`: the constant-column gate walk and plan-time literal casts, then the shared parent-lookup relation ([`selection-spine.md`](selection-spine.md)) and the per-element out-of-domain notice |
| [`exporters/streaming/routing.py`](../../src/fabulexa_forge/exporters/streaming/routing.py) | The Layer-A leaf derivation — `route_attributes` / `membership_route_attributes` / `resolve_subtype_index` (the per-event `route_table`, consumed by the Debezium `source_table` masquerade and the discriminator index) — and the election-support sidecar reads (`kind_reference_targets`, `membership_reference_fields`) |
| [`exporters/streaming/encoding.py`](../../src/fabulexa_forge/exporters/streaming/encoding.py) | `encode_pinned` — the single byte-stable JSON encoder shared by every sink (stdout / file / kafka), so a given `(event, fmt, anchor, schema)` yields byte-identical message bodies across all three |
| [`exporters/streaming/jsonl.py`](../../src/fabulexa_forge/exporters/streaming/jsonl.py) | `render_jsonl_object` (the JSONL object shape, keyed by the elected key map) and `write_jsonl_stream` (the shared `encode_pinned` + stdout / per-topic-file sinks, with the `paced` per-line-flush mode — see [`streaming-pacing.md`](streaming-pacing.md)) |
| [`exporters/streaming/debezium.py`](../../src/fabulexa_forge/exporters/streaming/debezium.py) | `render_debezium_message` / `build_debezium_value_schema` / `rebased_epoch_ms` (the Debezium value-message shape, the embedded Connect schema, and the epoch-millisecond timestamp) and `write_debezium_stream` (the same shared `encode_pinned` + stdout / per-topic-file sinks, the same `paced` flush mode) |
| [`exporters/streaming/kafka_sink.py`](../../src/fabulexa_forge/exporters/streaming/kafka_sink.py) | `resolve_bootstrap_servers` (CLI → config block → environment bootstrap precedence) and `write_kafka_stream` (the Kafka producer lifecycle — topic pre-creation, per-event produce keyed by the elected key map, flush-before-return); `confluent-kafka` is imported lazily here only |
| [`exporters/streaming/pacer.py`](../../src/fabulexa_forge/exporters/streaming/pacer.py) | `ResolvedClock` / `resolve_clock` / `pace_events` — the realtime-pacing surface the driver composes. Its contract is owned by [`streaming-pacing.md`](streaming-pacing.md) |
| [`exporters/streaming/driver.py`](../../src/fabulexa_forge/exporters/streaming/driver.py) | `stream_export` — events → (pace when realtime) → format → sink for one run, the Debezium config/anchor business rules, the per-stream value-schema build, and the declared-but-empty-topic backfill (empty files + zero counts) |
| [`exporters/streaming/init.py`](../../src/fabulexa_forge/exporters/streaming/init.py) | `generate_stream_init_config` — the `init --mode streaming` proposal engine (§ `init --mode streaming`); tests in [`tests/exporters/streaming/test_init.py`](../../tests/exporters/streaming/test_init.py) |
| [`derivations/row_state_events.py`](../../src/fabulexa_forge/derivations/row_state_events.py), [`derivations/membership_events.py`](../../src/fabulexa_forge/derivations/membership_events.py) | The composed event-content folds — their semantics are owned by [`derivations.md`](derivations.md) § The row-state-events derivation and § The membership-events derivation |
| [`cli.py`](../../src/fabulexa_forge/cli.py) | `cmd_stream` — the `fabulexa-forge stream` verb, flag-level usage checks (including the `--speed` / `--idle-cap` / `--fast` clock checks and the `--sink stdout\|file\|kafka` / `--out` pairing), clock resolution, the `--bootstrap-servers` flag and `FABEXPORT_KAFKA_BOOTSTRAP` read for the kafka sink, the `init --mode streaming` arm, and the `(ReaderError, ExporterError)` funnel |
| [`anchor.py`](../../src/fabulexa_forge/anchor.py) | The `EffectiveAnchor` the engine renders each event's `ts` from — see [`anchor.md`](anchor.md) |

## Boundary

- **Input.** An open `Emit` (trunk-only — sole branch), a validated `StreamConfig`,
  a resolved `EffectiveAnchor` — `None` is admissible for `jsonl` (raw-ns
  timestamps) but not for `debezium`, whose `ts_ms` must be epoch-milliseconds — and a
  caller-supplied `NoticeSink`, required with no default ([`notices.md`](notices.md)); a
  caller wanting silence passes a discarding sink. The
  driver consumes no target-schema file and no domain knowledge.
- **Output.** Newline-delimited JSON change events — a JSONL object or a Debezium
  value message per line — to **stdout** (all topics interleaved in global `seq` order)
  or to a **directory** (one `<topic>.jsonl` per topic in the run's topic set, including
  declared-but-empty ones); or, on the **kafka** sink, one message per event to a Kafka
  broker (one topic per declared stream, each pre-created with a single partition). The
  output is an event stream, not a relation.
- **Reader-first; authors no base-table SQL.** Every table and column fact flows from
  the `Sidecar`. The driver composes the row-state-events derivation (`state-changes`) or
  the membership-events derivation (`membership-events`) for content and reuses the reader's
  sidecar accessors for the records spine and membership tables; it hard-codes no column
  list and renders each event's `ts` in Python from the anchor.
- **The streaming sinks are not generic writers.** Every streaming sink — stdout,
  file, and kafka — consumes an already-materialized, cross-stream-merged, `seq`-stamped
  `Iterable[StreamEvent]` — not a `SELECT` — and adds non-relation targets (stdout, a
  Kafka broker). They are therefore streaming-local, co-located with the engine, and are
  **not** members of the generic `writers/` family and do not use its `SELECT →
  Emit.query_arrow → row-count` contract.
- **Forbidden imports.** No dependency on the bundle's producer; the vendored
  `contract/` is the only coupling. `confluent-kafka` is an optional install extra
  (`[kafka]`), imported lazily inside the kafka sink only — never at package import time,
  so the boundary is physical for a non-kafka install. Layer direction: the engine
  imports derivations, config, reader, anchor, and errors — never `writers` or `cli`; the
  driver imports the engine and the format/sink modules — never `cli` or `writers`.

## Semantics

### The content × format × sink model

The streaming exporter is organized along three independent axes:

| Axis | Value | Meaning |
|---|---|---|
| `content` | `state-changes` | The event content: per-record `c`/`u`/`d` full-row reconstruction from `history` + the records spine. Selects the row-state-events fold; every declaration must be kind-shaped (`KindStream`). |
| `content` | `membership-events` | The event content: per-interval `join`/`leave` events from the `membership__<K>__<p>` tables. Selects the membership-events fold; every declaration must be membership-shaped (`MembershipStream`). |
| format | `jsonl` / `debezium` | How each event is rendered — a flat JSONL object, or a Debezium value message (see § The Debezium format). Both formats render both content types. |
| sink | `stdout` / `file` / `kafka` | Where the rendered stream is delivered — interleaved on stdout, one `<topic>.jsonl` per topic, or one Kafka message per event (see § The Kafka sink). |

Each axis is a closed `Literal`, so a further content type, format, or sink is
additive. `content` selects the fold family the engine materializes and the required
declaration shape of every `streams` entry. The model composes: `content` selects the
fold, format renders each `StreamEvent` to a serializable object, and the sink delivers
it. *Delivery timing* is an orthogonal fourth knob: an optional `clock` paces the
rendered stream against wall-clock time without touching any of the three axes or the
bytes (see [`streaming-pacing.md`](streaming-pacing.md)). It is unpaced by default.

### Declared streams

A run's output is a list of **declared streams**. The declaration unit mirrors the
source exporter's declared table: a stream addresses the populations of exactly one
kind, or exactly one membership table. Combination is same-kind-only, because column
shape forces it.

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

`name` is author-verbatim and is the topic: the Kafka topic, the `<name>.jsonl`
filename, and the `events_per_topic` key. There are no default stream names to derive
(`init` proposes names verbatim from sidecar identity). Because the sink is a CLI flag —
a config never knows its sink — `name` must be legal for all three up front: it is
validated at parse time against the topic-name rule — `^[A-Za-z0-9._-]+$` and not `.` or
`..` (the Kafka topic-name convention, and a `<name>.jsonl` filename stem safe on every
filesystem — path traversal is unrepresentable). One eager rule covers every sink; no
delivery-time naming verdict exists.

The declaration grammar lives in [`config/models.py`](../../src/fabulexa_forge/config/models.py)
(`KindStream` / `MembershipStream` / `MembershipRef`); the two stream shapes are a
discriminated union on which of `kind` / `membership` the entry carries, so a
declaration mixing the shapes' fields is unrepresentable, not validated away. Recipes:
[`examples/recipes/streaming/`](../../examples/recipes/streaming/), indexed by
[`docs/recipes/README.md`](../recipes/README.md).

### Events are the facts; columns are a lens

A stream's **event set is payload-independent**: it is a function of the stream's
declared **row scope** (populations × `sub_types` × `where`) and its declared **change
scope** (`only` / `ignore`) — never of its column selection or its naming.

| Event | Fires |
|---|---|
| `c` | At each in-scope record's `created_sim_time` |
| `d` | At each in-scope record's `deactivated_at` |
| `u` | At every distinct history `sim_time` carrying a change to a property in the stream's change scope |

The `properties` list controls **only the after-image projection**. Two streams over one
population with equal row and change scope have the *same* event set, whatever they
project or however they name it; a `properties: []` stream is a notification feed — the
same events, identity-only payload ("something changed on this identity; dereference it
yourself"). A stream selecting only constant properties carries the same `u` events with
those constants as payload. This is row-level CDC semantics: a real connector emits an
event whenever the row changes, whatever the consumer projects — and it is exactly how
`membership-events` works (`join`/`leave` events are the intervals; `fields` only projects
the payload). One rule spans both content types.

The two scopes are the author's, and they are the *only* declarations that move the event
set. Row scope decides which records' events exist at all (§ Row selection); change scope
decides which instants of a surviving record fire a `u` (§ Change scope). Both are
declaration-level facts, fixed before any fold runs — which is why narrowing either one
never renumbers what remains out of order: dropped events consume no `seq`, and the
survivors keep the canonical order they would have had.

The slice-only policy is satisfied vacuously at the event level: `slice_only` implies
`history_tracked: false` (the contract's three-way `temporal_class`), so a `slice_only`
column contributes no history rows and has no change points to fire — no event
membership can derive from one by class, not by filter
([`slice-only.md`](slice-only.md)). Selecting a non-exempt `slice_only` column is
refused outright (`StreamPropertySliceOnly`); the exempt discriminator is selectable,
whatever its class.

### Per-stream folds and after-images

Each declared stream materializes its **own** fold over its own declaration — the
stream is an independent feed, exactly as its real-world counterpart would be.

| Condition | Result |
|---|---|
| `state-changes` stream | One row-state-events fold over the kind, `u` rows at the change points of the stream's change scope; rows whose discriminator is outside the stream's `sub_types` scope are dropped before the merge (post-fold, via the `resolve_subtype_index` discriminator index), as are rows outside its resolved satisfying record set (§ Row selection) |
| `membership-events` stream | One membership-events fold over the declared table and `fields`; intervals whose owner is outside the resolved satisfying owner set are dropped before the merge |
| After-image column set | Per stream: the published identity surfaces (§ Identity projection), then the stream's declared `properties` resolved in the kind's column order — the single column-order producer (`resolve_stream_columns`) is consulted per stream. The fold's own column list is complete by definition (it always carries `record_id`, and the kind's `presentation_id` where minted — [`key-election.md`](key-election.md) § Identity publication); the publication projection applies above the fold, never inside it. Each entry's **output key** is resolved beside it by the single naming authority (§ Output names and `rename`), so the Debezium value schema and the rendered rows are the same list, under the same names, by construction |
| After-image identity | One entry per published identity surface (§ Identity projection), in the kind's sidecar column order, ahead of the payload entries. Identity is never read from an after-image — every published non-`record_id` surface rides the fold's `record_id` and renders through its identity join (§ Message key) |

The payload-independence of the event set is realized **in the shared fold, not by
engine-side trimming**. The row-state-events fold takes two independently-scoped property
sets — a **change scope** governing `u` event membership and a **projection** set the
SELECT emits (see [`derivations.md`](derivations.md) § The row-state-events derivation).
Streaming invokes every kind-shaped stream's fold with change scope = the stream's
declared audited set (§ Change scope) and projection = the stream's declared
`properties`, two sets the author moves independently: the SELECT still emits exactly the
declared columns in the single producer's order — no engine-side column trimming, and the
schema ↔ row agreement invariant holds by construction. Source's event log invokes the
same fold with its own two sets; the playback seam invokes it with equal ones.

The after-image column set is a **per-stream fact** — sub-types of one kind need not
share one. A combined stream (several `sub_types`, one `properties` list) has one column
set; rows carry NULL in a selected column their sub-type does not declare — the author
chose the combination, and the NULL is the faithful rendering of structural
inapplicability (the sidecar's `sub_type_columns` partition states which value columns
each sub-type owns). `init` never proposes a combined stream.

**Value elections on after-images.** A declared stream's optional `render:` map
carries `decimal` and `json_precision` entries keyed by the stream's bare
property (or membership field) names; an elected property's after-image entry
(`c` / `u`) carries the elected text form — the decimal string with `s`
fraction digits, or the leaf-rounded payload — in place of the raw codec
string. The `d` tombstone, the Debezium value schema (elected entries remain
string-typed by codec), the message key, merge order, `seq`, and `ts` are all
unaffected. The authorities apply at the codec seam in the post-fold SELECT
that assembles after-images — the fold itself is untouched, and the elected
text is identical to the table modes' render of the same value
([`value-rendering-elections.md`](value-rendering-elections.md) § Streaming
attach). The temporal elections do not attach (§ Boundaries).

### Identity projection

A stream declares which identity surfaces its rows carry: the optional per-stream
`identity` list, on both stream shapes. Identity is **projected, never
property-selected** — `properties` and `fields` are payload, and the three surface
names sit outside the payload namespace entirely (below). Absent `identity`, the
stream publishes its elected surface alone: a stream declares what ships
(`properties` is required with no default; an absent `rename` means bare keys), so
the absent path publishes the minimum a topic cannot do without — its key.

| Shape | Declaration | Admissible surfaces | Absent |
|---|---|---|---|
| Kind-shaped | `identity` | `record_id`, `record_index`, `presentation_id` | The elected surface alone |
| Membership-shaped | `identity` (over the **owner**) | as above, resolved against the owner's election | The owner's elected surface alone |

The declared set resolves against the stream's gated election into an ordered
**published set** (`IdentityProjection`, resolved and gated by
`resolve_identity_projection` — recomputed as the same pure function by the engine's
eager pass and the driver's Debezium schema build, so the two cannot disagree). The
published set must contain the elected surface — a topic must publish its own key
(`StreamIdentityMissingElected`).

**Order is sourced, never invented.** The published set renders in the kind's
`records__<kind>` sidecar column order restricted to the published surfaces —
`record_id`, then `presentation_id`, then `record_index`, the contract's own
positions. A declaration's list order never reaches output; the list is a set.

**Values come through the election relations.** Every published non-`record_id`
surface renders through key election's identity join relation for that surface,
keyed on the fold's `record_id` and composed at the end-of-tape entry point — the
record-index derivation for `record_index`, the presentation-key derivation for
`presentation_id` — exactly as the elected surface renders. A published surface is
never read from the fold's after-image and never from a `ref_index__` column, the
election's standing rule. `record_id` is the fold's own column verbatim and
composes no relation.

**Every published surface runs the election's gates**, not only the elected one —
a published identity column cannot reach a row unexamined. This is a widening of
the gate population, not a new algebra ([`key-election.md`](key-election.md)
§ Identity publication):

| Gate | Ranges over | On violation |
|---|---|---|
| `presentation_id` declared | Each population the stream spans (kind-shaped) or addresses as owner (membership-shaped), when `presentation_id` is published | `ElectionPresentationUndeclared`, naming the stream and the uncovered population |
| Identity union safety | The spanned/addressed populations' key spaces for each published surface, pairwise | `ElectionUnionUnsafe`, naming the stream, the surface, and the unsafe pair |
| Published-key uniqueness | Render time, per composed identity relation: `rows = DISTINCT record_id = DISTINCT value`, value non-NULL | `ElectedKeyDuplicate`, naming the stream and the surface |

Two consequences worth naming. Publishing `presentation_id` requires the registry
to declare every spanned population — the claim-consuming-path posture the
election and `declare_keys` share; the escape is not publishing it, one line. And
`record_index` is publishable on any population — one shared space per kind,
union-safe with itself by the contract's own verdict. `ElectionMixedIdentity`
stays about the *election* alone: a published non-elected surface is one
author-named surface applied to every spanned population, so no mixing is
expressible. The uniqueness guard ranges over surfaces a population did not
elect, so a violation on a published non-elected surface names the **surface**,
not the election.

**Identity surfaces are not properties.** One rule replaces what a payload
reclassification would need as a set of exceptions:

| Condition | Result |
|---|---|
| `record_id`, `record_index`, or `presentation_id` in a stream's `properties` | `StreamPropertyNotAddressable` — identity is projected through `identity`, not selected through `properties` |
| A membership stream's `fields` naming `presentation_id` | `MembershipFieldResolvable` — a membership stream's payload is element fields |
| `presentation_id` published on a kind that mints no surrogate | `StreamIdentityUnavailable` |

The rule claims the three bare names out of the payload namespace deliberately: a
producer payload property that shares one — the contract does not forbid a
property named `record_index`, so `prop__record_index` can exist — is
unaddressable on a stream, full stop. That is source's posture
(`SourceColumnNotAddressable`); on the wire an identity name must mean identity,
and a payload column borrowing one would be a join trap.

Because an identity surface never enters the property namespace, the remaining
author surfaces need no carve-out. None widens; each already resolves its keys
against the payload namespace:

| Surface | Why an identity surface stays outside |
|---|---|
| `properties` / `fields` slice-only refusal | Its keys resolve to `prop__<p>` / `elem__<f>` columns; identity columns carry no `temporal_class` (the contract omits it on all three), so the question is not askable of them — and, with identity out of the property namespace, is never asked |
| `only` / `ignore` change scope | Genesis-minted or creation-constant, never re-minted, never in `history` — not in any kind's audited set. Naming one is `StreamChangeScopeUnresolvable` |
| `where` predicates | Keyed on bare payload-property names of the subject kind |
| `render` value elections | Keyed on the stream's declared `properties` / `fields`; an identity surface fails as a non-member (`RenderKeyResolves`, not a type mismatch) |

A membership stream's after-image carries the published **owner** identity
surfaces (in the owner kind's sidecar column order, after the leading Debezium
`event` column where that format applies), then the selected element fields.

### Output names and `rename`

Every after-image column resolves one **output key** per stream — the key it carries on
the wire. Payload keys are **bare**: the `prop__` / `elem__` / `member__` prefixes are
reader plumbing and do not reach the wire.

| Column | Default output key | `rename` entry |
|---|---|---|
| Kind-stream property `prop__<p>` | `<p>` | `<p>: <target>` → `<target>` |
| Membership scalar element field `elem__<f>` | `<f>` | `<f>: <target>` → `<target>` |
| Membership reference field `member__<f>__kind` / `member__<f>__id` | `<f>_kind` / `<f>_id` — the event log's `changes` pair convention | `<f>: <target>` → `<target>_kind` / `<target>_id`, renamed in place as a pair |
| A published identity surface (§ Identity projection), the membership owner identity entries included | The surface's contract column name | `<surface>: <target>` → `<target>` — the surface's contract column name is a legal `rename` key exactly when the stream publishes it |
| Debezium membership `event` | `event`, fixed | Not addressable |

The grammar is source's `rename` grammar — keys are *source* identities, never
output keys — with source's identity-column handle: a state table renames its
identity column by the elected surface's contract column name, and a stream
renames a published surface the same way.

| Condition | Result |
|---|---|
| No `rename`, or no entry keyed on a published surface | Each published surface ships under its contract column name |
| `rename` key not in the stream's `properties` / `fields` and not a published surface's contract column name | Refused (`StreamRenameUnresolvable`) — when the key is an *unpublished* surface name the message names the stream's published set; a plain property-name typo does not advertise identity surfaces |
| Two output keys collide (two targets; a target against an unrenamed bare default; a renamed pair member against anything) | Refused — never a silent collision |
| An output key equals a reserved name on that stream — each published identity surface's **resolved** output key, or `event` on a membership stream | Refused (`StreamOutputNameCollision`). The reservation tracks what actually publishes, under its resolved name, so a rename moves the reservation with it. `event` is reserved on a membership stream under **both** formats: a config never knows its format, so one eager rule covers both (the topic-name-rule posture) |
| Rename present, after-image order | The single column-order producer's order — rename relabels, never reorders |
| `render:` map keys | Bare source identities — rename does not move election keys |

Two consequences follow from the reservation tracking what publishes.
`rename: {record_index: status}` on a stream that publishes `record_index` and
also selects a `status` property is a collision — the identity moved onto the
payload's name. And `rename: {status: record_index}` is legal on a stream that
does not publish `record_index`, and a collision on one that does: a contract
column name is free for payload use exactly when no identity surface claims it.

The **elected** surface's resolved key applies to **all four** of its sites at
once — the message key map entry, the after-image identity entry, the Debezium
key-only `d` before-image entry, and the Debezium value-schema field — one name,
four sites, one producer (`resolve_identity_output_key`), so key and payload can
never disagree. A published non-elected surface appears in the after-image and
the value schema only; it is never a message key. Membership streams resolve
owner identity keys by the same rule, reading the owner's election and the
stream's owner projection.

Output keys are produced by **one resolver per stream shape**
([`presentation.py`](../../src/fabulexa_forge/exporters/streaming/presentation.py)) — the
single-producer discipline extended from column order to column naming. The engine's
after-image assembly and the Debezium value-schema build read the same ordered
`OutputEntry` list — identity entries (a surface rendered through its election
relation, or `record_id` verbatim) and payload entries (a fold column read
verbatim), each with its resolved output key — so the declared schema and the
rendered rows cannot diverge under any rename. The format renderers are not
consumers: they write the assembled after-image verbatim. The schema builder takes
an ordered column-name list and knows nothing of naming — its caller supplies the
resolver's output keys, after the leading membership `event`.

The resolver takes the stream's **gated identity projection** rather than a
pre-resolved key, because the published set is what decides which rename keys are
legal and which names are reserved. Naming applies where after-image maps are
assembled, never in SQL: the fold's own column names are the base-derived ones
throughout.

### Kind vocabulary

Two declarations govern what a kind name looks like where it renders **as a value**:
the config-level `kind_labels` map and a per-stream `kind_label`. Both default to the
engine's verbatim kind name — the package invents no vocabulary.

The envelope `kind` resolves per stream, first match wins:

| Condition | Envelope `kind` |
|---|---|
| `kind_label` declared on the stream | The declared string, verbatim |
| The stream's kind (owner kind, membership-shaped) is in `kind_labels` | That kind's label |
| Neither | The kind name, verbatim |

The envelope `kind` is per-stream constant — a stream spans populations of one kind.
`kind_labels` additionally applies per value to membership member-kind payload entries
(`<f>_kind` under the bare-name default) with **identity fall-through**: a value matching
no declared pair renders verbatim and `NULL` stays `NULL`. The mapping is total, so a
corrupted emit's mutated kind cell surfaces unchanged — never masked, never a render
error. With no labels declared the passthrough is byte-identical.

**Vocabulary integrity splits by the claim each surface makes.** `kind_labels` is a
*value mapping* — member-kind values render through it — so it stays injective over the
emit's whole kind universe: every key names a sidecar kind, two kinds cannot share a
label, and a label cannot equal a *different* kind's rendered name (its label, or its
verbatim name when unlabeled). Member-kind values are not bounded by the declaration
list, which is why the range is the whole universe rather than the declared streams.
Within the value mapping, one rendered kind name therefore identifies at most one kind.

A per-stream `kind_label` is *feed presentation*, not a kind claim: it names the domain
concept the stream represents — usually sub-type grain, which the kind universe does not
see, since sub-types are the first-class domain concepts and kinds are simulation
machinery. It carries one constraint, the masquerade refusal: it cannot equal a
*different* kind's rendered name, because that string does identify a kind wherever
member-kind values render. Within that bound, sharing is legal declared intent — two
streams, of one kind or of different kinds, may declare the same `kind_label`.

**Reach.** JSONL carries `kind` at the top level; the Debezium envelope has no kind
field, so a per-stream label does not reach Debezium at all, and `kind_labels` reaches it
only through member-kind **values** in the after-image. Labeling is applied where the
engine assembles each event — `StreamEvent.kind` carries the stream's resolved envelope
value, and member-kind entries carry mapped values — at the same assembly site `rename`
applies. The format renderers are byte-transparent to it, which is what makes the
element-field format-parity invariant hold by construction. Event membership, ordering,
`seq`, and topic assignment never read the vocabulary, and the Debezium masquerade is
schema identity rather than a payload value: `kind_labels` does not reach `route_table`,
`source.table`, or the value-schema names.

### Row selection

`where` keys are **bare payload-property names of the subject kind** — the declared kind
on a `KindStream`, the **owner** kind on a `MembershipStream`. Owner properties are not
columns of the membership table at all, so a bare key matching both an owner property and
an element field resolves to the owner property; element fields carry no temporal class
and are not predicate-addressable. Entries are AND-joined, and values are the shared
`PredicateValue` grammar compiling to `=` / `IN` under the one rendering authority
([`row-predicates.md`](row-predicates.md)), literal-typed from the sidecar.

The **constant-column gate** applies as it does in source, and its purpose is sharper
here than anywhere: a stream replays every instant of the tape, so only a property whose
value is identical at every horizon can select rows without making the event set
time-dependent. The gate makes the as-of-which-instant question unposable.

| `where` key names | Result |
|---|---|
| A `constant`-class payload property of the subject kind | Accepted |
| A `tracked`-class property | Refused — its value at event time and its current value select different rows |
| A `slice_only` property | Refused — its past is unknowable ([`slice-only.md`](slice-only.md)) |
| The subject kind's declared discriminator | Refused, pointing at `sub_types` (owner `sub_types` on a membership stream) |
| A structural column, a membership element field, or an unknown column | Refused — unresolvable |

**The key axes error; the value axis notices.** Every element is cast to its resolved
column's sidecar-declared type at validation time, and an uncastable element is refused
before any fold runs. An element outside a column's declared `enum_domains` entry draws a
per-element `discriminator-value-unobserved` [notice](notices.md), never an error — one
config legitimately serves a family of emits. The message leads with `stream '{name}'`
and keeps the shared two-case wording in the stream's nouns: when no element of an entry
was observed it states the topic will be empty; when the entry's other elements were
observed it states only that this element contributes no events. Notice order follows the
eager pass's iteration order — streams in declaration order, a stream's `where` keys in
config key order, a key's elements in declared order — so the sequence is deterministic.

Selection is realized as the engine-side row-scoping device: the satisfying record set
(satisfying **owner** set, membership-shaped) is computed once per stream and fold rows
outside it are dropped before the merge, exactly as out-of-scope `sub_types` rows are.
Dropped rows consume no `seq`. The mechanism splits by stream shape — a kind stream's
`sub_types` stay the discriminator-index device with `where` adding the record-set drop
beside it, while a membership stream's owner `sub_types` and `where` resolve **together**
through the shared parent-lookup spine ([`selection-spine.md`](selection-spine.md)): one
owner-side read producing one satisfying owner set, whether either or both are declared.

| Condition | Result |
|---|---|
| Kind stream with `where` | Every event of a non-satisfying record is excluded — `c` and `d` included. The predicate never enters the fold's SQL; rows are dropped after it |
| Membership stream with owner `sub_types` / `where` | Every `join` / `leave` of a non-satisfying owner's collection is excluded, via the parent lookup |
| `where` and `sub_types` on one stream | AND-composed — the predicate narrows within the scoped populations / owner sub-types |
| Selection matches zero rows | The declared-but-empty topic: the topic exists (empty file, pre-created Kafka topic, `events_per_topic == 0`), exit 0 — declared intent drives existence |
| A row whose predicated column is NULL | Never selected — `=` / `IN` is never satisfied by NULL, and the grammar has no null test |
| Predicated property absent from `properties` | Legal — selection and projection are orthogonal; the predicate reads the subject relation, not the payload |
| Predicate on a reference-valued constant property | Legal — compared over base-layer values (record ids), whatever surface the column renders |
| Overlapping streams with different `where` | Legal — each stream's selection scopes its own feed independently |
| Owner `sub_types` on a membership stream | Narrows the **addressed owner population set** — the set the key-uniformity gate ranges over and per-row owner-election resolution draws from. `where` never narrows the addressed set: it is value-level, not population-level, so gates and type resolution see the full declared scope whatever rows the predicate selects |

Predicates evaluate over source (base-layer) values — before rename, before elected-surface
rendering, before labels.

### Change scope

`only` and `ignore` (mutually exclusive, bare names) narrow the change scope the engine
passes to the row-state-events fold. The **audited scope** is the kind's temporally honest
property set — every `tracked`- and `constant`-class property; `only` narrows to its
entries, `ignore` subtracts its entries. The projection (`properties`) is untouched: the
two scopes are independent, as the fold's contract provides.

| Condition | Result |
|---|---|
| Both fields absent | Change scope is the full audited set |
| A scoped tracked property changes | A `u` fires at that change point |
| Only out-of-scope properties change at an instant | No `u` exists for that instant — the event is never produced and consumes no `seq` |
| In-scope and out-of-scope changes coincide at one instant | One `u` — the fold's per-`(record, sim_time)` grain |
| A property projected but not in change scope | Its changes fire no `u`, but its as-of value still rides every after-image the surviving events carry |
| A property in change scope but not projected | Its change fires a `u` whose after-image does not show it — a notification-shaped feed for that property |
| A `constant`-class (or untracked) name in change scope | Legal and inert for `u` membership — no history rows, no change points (the fold's rule) |
| `ignore` covering every tracked property | A lifecycle-only feed — `c` / `d` only; legal, the no-tracked-property population shape |
| `c` / `d` events | Never affected — lifecycle always fires |
| Membership streams | No change-scope fields — `join` / `leave` are the facts and `fields` is already pure projection; the fields do not exist on the model |

A `slice_only` property is history-untracked and contributes no change points, so its
absence from the audited default costs no event. The change scope is compared over raw
base values: renames, labels, and render elections play no part in event membership — the
source log's election-invariant diff rule.

### Cross-stream merge and global `seq`

There is one **canonical total order** over all events of all declared streams, where
the *source identity* is the per-stream constant that makes the inter-stream tiebreak
deterministic — the **declared stream name**, unique by the name-collision rule:

> `(event_sim_time ASC, event_class ASC, stream_name ASC, record_id ASC[, field-value tail])`

Each per-stream fold already emits its rows sorted by
`(event_sim_time, event_class, record_id, …)` — the canonical order with the stream
name held constant (see [`derivations.md`](derivations.md) § The row-state-events
derivation and § The membership-events derivation). The engine realizes the global
order by a **k-way merge** of the pre-sorted per-stream iterators (`heapq.merge` under
the canonical key); it does not concatenate-and-re-sort. The merge key is
`(event_sim_time, event_class, stream_name, record_id)`, read from the materialized
fold rows. The stream-name component is load-bearing twice over: `heapq.merge` breaks
*inter-stream* ties by stream-argument order, not by the key, unless the identity is in
the key — so the engine injects it per-stream (it is constant within each stream and is
not a fold column) — and, because two streams may cover one population, `record_id`
alone cannot break an inter-stream tie. For membership-events the merge key
deliberately stops *before* the selected-field tail — the after-image field values that
realize the rest of the canonical order. It can, because the stream name is unique per
fold: two rows with an equal merge key always come from the same fold, where the fold's
`ORDER BY` has already sorted them by the field tail and `heapq.merge` (being stable)
preserves that order; rows from different folds never tie on the merge key. So field
values — including a SQL `NULL` or a reference `(kind, id)` pair, neither safely
comparable in Python — are never compared across folds.

`seq` is the 1-based position of an event in that order — a monotonic integer spanning
the whole stream, not reset per stream — stamped once as the engine merges. For
`state-changes` the key is a **total** order with no ties: within a stream a record has
at most one `c` (at its `created_sim_time`), one `d` (at its `deactivated_at`), and one
`u` per distinct history `sim_time`, so no two events share all four components and
`seq` resolves nothing the key left ambiguous. For `membership-events` the order is
total **up to byte-identical events**: the contract permits byte-identical intervals
(same key, multiplicity ≥ 2), which produce byte-identical events (same time, class,
owner, fields, op, payload) and so tie the canonical key — but whichever the merge
places first takes the lower `seq`, and because the two differ only in `seq` the
emitted byte stream is identical regardless of which physical row sorted first. The
merge reads its sort key — including `event_class` — from the fold rows; the
`StreamEvent` is constructed only after the merge and carries `seq` and `op`, not
`event_class` (once `seq` is stamped the order lives in `seq`).

**Overlapping streams.** Under overlapping streams the same base change yields one
event per covering stream — distinct `seq`, distinct topic, identical key, identical
event set, after-image content differing only by each stream's projection. Faithful
reshaping holds: every event still traces to base values; duplication across declared
feeds is declared intent, the streaming analog of two source tables rendering one
population.

**Coincident change-and-deactivation.** When a record's final history change lands
exactly at its `deactivated_at = D`, the `u` (its event time is the history `sim_time`)
and the `d` (its event time is `deactivated_at`) carry the same `event_sim_time = D`.
They tie on `sim_time`, and the `event_class` tiebreak — `u` is `1`, `d` is `2` —
orders the `u` strictly before the `d`. No final change is lost, and the `d` still
terminates the record.

**The canonical order and `seq` are seam-owned guarantees.** The canonical total order
and the global-`seq` definition are owned by the playback seam
([`playback.md`](playback.md) § Canonical total order and entry-point-invariant `seq`);
a streaming stream's per-content output conforms to them by construction, with four
scoped divergences, each following from declaration:

- **Interleave:** where the seam's canonical order tiebreaks on the record `kind` /
  `(owner_kind, property)` identity, a declared-stream run tiebreaks on the author's
  stream names — the interleave of same-instant, same-class events across streams
  follows declaration naming, not bundle identity. Per-topic sequences are unaffected
  (a topic is one stream); only the cross-topic interleave (stdout order, global `seq`
  assignment) is declaration-relative.
- **Multiplicity:** the seam plays each in-scope base event once; overlapping declared
  streams emit one event per covering stream.
- **Membership field-subset tail:** a field-subset declaration's intra-instant
  membership ordering tail spans the subset, so order agreement holds up to
  intra-instant same-class same-owner ties.
- **Row and change scope:** a scoped stream plays the subset of the seam's row set its
  declaration selects (§ Row selection, § Change scope). Within the surviving events the
  canonical order holds — scope removes events, it never reorders them.

The `u` event set is invariant to *projection*, matching the seam's column-selection-invariant
row set by design (§ Events are the facts). A declared stream is single-content and never merges
the two event families, so its per-content order is the canonical order with `family`
held constant; the cross-family interleave lives only at the seam.

### Message key

`StreamConfig` carries the cross-mode `keys` block — the election grammar, surfaces,
and defaults of [`key-election.md`](key-election.md): per population, elect `record_id`
(the default), `record_index`, or `presentation_id`. Streaming reuses that
machinery wholesale — the resolution gates, the union-safety algebra, the identity join
relations, and the render-time uniqueness guard — and adds its own combination
gate (one stream, one key surface — § Validation Rules), the identity projection
that widens the gated population to every published surface (§ Identity
projection), and its render sites
([`key-election.md`](key-election.md) § Rendering: streaming):

| Render site | Rendering |
|---|---|
| Message key (every op, including the `d` tombstone) | The record's elected surface: the Kafka key and the `key: {…}` map (one entry, keyed by the elected surface's **resolved output key** — its contract column name, or its `rename` target — § Output names and `rename`), never schema-wrapped (the value-only stream emits no key message). The Debezium `d` key-only before-image carries the same one entry under the same key. For `membership-events`, the **owner's** elected surface. The key stays single: publishing several identity surfaces widens the after-image, never the key |
| After-image identity (`c`/`u` rows; the membership `after`'s owner entries) | One entry per published surface (§ Identity projection), each via its identity join at the fold's `record_id` (`record_id` itself verbatim), each under its resolved output key, in sidecar column order ahead of the payload entries. A state-changes `d` keeps `after: null` — its identity lives in the message key and the Debezium key-only before-image, both elected-surface-rendered. Under a `presentation_id` election the surface publishes once, as identity; under any other election `presentation_id` ships only when the stream's `identity` declares it |
| Reference-valued `prop__<p>` entries in the after-image | The **target's** elected surface, translated through the target's identity join — referential integrity is non-negotiable: a consumer must be able to join any stream against any other elected output on equal values |
| Membership `member__<f>` reference fields | The member row's kind's elected surface (the `__kind` component remains the disambiguator, and its *value* renders through the kind vocabulary — § Kind vocabulary), the junction-member analog. The pair ships under its resolved output keys, `<f>_kind` / `<f>_id` by default |

Two render facts span the table. **One codec:** elected values keep the streaming
codec at every site — the key map, the after-image identity entry, reference `prop__`
entries, membership `member__<f>` fields — codec `VARCHAR` (`str`) or `null`, exactly
as every after-image value ships. `record_index` renders digit-form, `presentation_id`
the codec rendering of its sidecar-declared value; no site emits a typed JSON number,
so serialization stays total and the byte-determinism invariant needs no extra case.
**The membership owner entries key by election:** in both formats' membership `after`
(JSONL, and Debezium's `{event, …}` payload) the owner-identity entries are the
published owner surfaces under their resolved output keys; the element-field
format-parity invariant is unaffected.

Contract consequences, all inherited from the cross-mode election surface:

- **One stream, one key surface.** A stream is a topic, and a topic's key is one
  identity space. Every population a stream's *keys* draw from must elect the same
  surface: for a kind-shaped stream, the spanned populations (the declared `sub_types`,
  or the full domain under the shorthand); for a membership-shaped stream, the
  **addressed owner population set** — the declared owner `sub_types`, or the owner
  kind's full domain when they are omitted. Violation is `ElectionMixedIdentity`,
  naming the stream. Under a uniform `presentation_id` election the spanned key spaces
  must be pairwise union-safe (`ElectionUnionUnsafe`) — the identity-column posture.
  Because the gate ranges over the addressed set, a mixed-election owner kind is
  splittable per sub-type across streams — the narrowed-unit resolution source has.
- **Edges gate per column.** Each after-image reference column and each membership
  member field runs the edge union-safety gate over its admitted target
  populations' resolved surfaces (`ElectionUnionUnsafe`, naming the stream and column).
  Streaming's admitted set is the kind-targeted modes' (source's and base's): the
  target kind's full declared domain for a reference column, per member kind for a
  membership member field — a stream's `sub_types` scope narrows its *own* rows, never
  which target populations an edge admits. Per-row mixed-election rendering on edges
  resolves the target row's population from the records-spine discriminator, the
  election surface's per-row rule.
- **Compaction stays coherent.** Every electable surface is creation-constant
  (`record_index` by construction; `presentation_id` genesis-minted, never re-minted),
  so a record's `c`/`u`/`d` events keep one key for its whole life and the `d` keys the
  tombstone — guaranteed by the election gates.
- **The uniqueness guard runs — over every published surface.** Every composed
  identity relation, elected or published through `identity`, asserts
  `rows = DISTINCT record_id = DISTINCT value`, value non-NULL, over the population
  set drawn through it (`ElectedKeyDuplicate` on violation, naming the surface).
  Streaming composes every relation at the end-of-tape entry point: a record's
  creation precedes its every event, the same argument that fixes the source event
  log's horizon.
- **Ordering is untouched.** The canonical order and merge key still read the fold's
  `record_id`; the election renders identity, it does not re-sort.

No `keys` block → `record_id` throughout: every identity render site — the key map,
the after-image identity entry, reference `prop__` entries, membership `member__<f>`
fields — renders the natural `record_id` verbatim, and no identity join is composed.
Keying on a creation-constant identity is what lets a record's `c`/`u`/`d` events share
one key, so a downstream log-compacted topic collapses them and the `d` keys the
tombstone. For `membership-events` the key is the **owner's** — the aggregate root of
the collection — so all of one collection's `join`/`leave` events share a key and stay
ordered together on one partition (a queue's arrival/departure order survives).
Membership-events are **not** log-compaction-coherent on this key: one owner holds many
concurrent members, so the key does not identify a single upserted row. That is the
append-only-log consequence of the owner key (an invariant below), not a defect —
`membership-events` is a fact log, not an upsert stream.

### Timestamp rendering

Each event's `ts` is the rebased wallclock instant of its `sim_time`, rendered in
Python directly from the resolved `EffectiveAnchor`:

> `instant = start_instant.astimezone(UTC) + event_sim_time ns`
>
> `ts = instant.astimezone(anchor.timezone).isoformat()`

The elapsed `sim_time` is added in the **absolute (UTC) frame**, not by wall-clock
arithmetic on the zone-aware `start_instant` (which is what Python's `+` on a
`ZoneInfo` datetime does and would mis-add an hour across a DST boundary). This is the
same absolute-instant computation `render_anchor_timestamp_expr` performs in SQL,
minus its final lossy `timezone()` strip — so streaming keeps the true offset:

| Anchor | `ts` on the event |
|---|---|
| resolves | the absolute instant projected into `anchor.timezone`, rendered ISO-8601 **with offset** (e.g. `2026-01-01T08:00:00+00:00`) — a `str` |
| `None` (no `rebase`, no sidecar runtime) | the raw `event_sim_time` nanoseconds — an `int` |

Streaming does **not** route `ts` through the anchor's SQL `TIMESTAMP` projection
(`render_anchor_timestamp_expr`): that projection yields a *naive* local-wallclock
`TIMESTAMP` whose UTC offset is discarded, and a naive timestamp that lands in a DST
fall-back fold cannot be re-localized to its true offset — re-attaching a zone
would misrepresent the absolute instant for fold-region events, a Faithful-reshaping
violation (see [`anchor.md`](anchor.md) § Anchored-timestamp rendering and § Ambiguous
/ nonexistent origin). Because the instant is projected *from* the absolute frame, the
offset is always the true offset for that instant: a fall-back-fold event renders
`…+00:00` vs `…+01:00` unambiguously, never a guessed fold. `ts` is never `now()` — it
is a pure function of `event_sim_time` and the resolved anchor. Anchored `ts` is
microsecond resolution (Python `datetime`); the full-ns event-time key always remains
on `event_sim_time` for any consumer needing nanosecond precision.

### Topics, empty streams, and `route_table`

| Condition | Result |
|---|---|
| Run's topic set | The declared stream names, in declaration order (`build_topic_set`) |
| A declared stream yields zero events | Its topic still exists: empty `<name>.jsonl`, pre-created empty Kafka topic, `events_per_topic[name] == 0` — declared intent, not observed rows, drives topic existence |
| Event's `topic` | The declaring stream's `name` |
| Event's `route_table` | The per-event leaf logical table: the row's `<kind>_type` discriminator value for a sub-typed kind, the bare kind for a flat kind, `<owner_kind>__<property>` for a membership stream |

`route_table` is the *logical source table* an event's record belongs to — the table a
real CDC stream would carry it on. It is derived per event by the Layer-A functions in
[`routing.py`](../../src/fabulexa_forge/exporters/streaming/routing.py)
(`route_attributes` / `membership_route_attributes`): sub-typed-ness is the
`<kind>_type` discriminator domain read through `Sidecar.subtype_values` (a kind's
warehouse role — `record_roles` — plays no part), and the per-event sub-type comes
from one `resolve_subtype_index` map (`record_id → sub_type`) built once per sub-typed
kind before the merge, read from the record spine independent of the selected
`properties`. Because the discriminator is contract-immutable, every event of a record
carries the same `route_table`. Its sole consumer is the Debezium `source_table`
masquerade (§ The Debezium format); topic assignment is the declared stream name and
never derives from `route_table`.

| Sink | Layout |
|---|---|
| `stdout` | all topics interleaved, one JSON object per line, global `seq` order |
| `file` | one `<topic>.jsonl` per topic under the output directory, each in `seq` order |
| `kafka` | one topic per declared stream, each pre-created with a single partition; one message per event, per-partition order == `seq` order (see § The Kafka sink) |

The `file` sink emits one `<topic>.jsonl` for every topic in the run's topic set even
when that topic yields zero events — an empty file, mirroring the generic writers'
zero-row-still-emits rule so the file set is exactly the declared topic set regardless
of data. The `stdout` sink writes no bytes for a fully empty stream.
`StreamOutcome.events_per_topic` is keyed by the run's topic set, not by what emitted:
it carries one entry per topic — value `0` for a topic that produced nothing — across
**all three** sinks alike. The `kafka` sink's form of the guarantee is a pre-created
empty topic (see § The Kafka sink). This declared-but-empty-topic guarantee (the empty
files, the pre-created empty topics, and the zero counts) is performed by
`stream_export` (the driver), layered over the writers' seen-only counts, so the CLI
summary always lists every declared topic. Either case is a successful run (exit 0).

### Membership-events content

`content: membership-events` streams `join`/`leave` events from the membership tables the
membership-shaped streams declare — one membership-events fold per declared stream, merged
into the one `seq`-ordered stream exactly as kind-shaped folds merge for `state-changes`.
The fold's unpivot, payload, and ordering contract are owned by
[`derivations.md`](derivations.md) § The membership-events derivation; this section is the
content-level reading the engine and formats give a membership `StreamEvent`.

`StreamEvent.op` admits `join` / `leave` alongside `c` / `u` / `d` — the only structural
difference; every other field carries its usual meaning. Per membership event:

- `op` is `join` or `leave`; both carry a full `after` payload (the membership-events log is
  append-only, so a `leave` is not a key-only tombstone — it carries what left).
- `kind` carries the stream's resolved envelope value for the **owner kind** (the record
  kind whose collection changed) — the owner kind's name verbatim absent a declared
  vocabulary (§ Kind vocabulary); the relation's `property` and the member identity live
  in the payload and the topic, not in `kind`.
- `record_id` is the **owner `record_id`**; the message key is the owner's elected
  surface (§ Message key), which equals `record_id` under the default.
- `route_table` is `<owner_kind>__<property>` (§ Topics, empty streams, and `route_table`).
- `after` is the published owner identity entries (§ Identity projection — the
  owner's elected surface alone absent an `identity` declaration, each under its
  resolved output key) plus one entry per selected element-schema field under its
  resolved output key (§ Output names and `rename`), each value codec `VARCHAR`
  (`str`) or `null` — non-null on both `join` and `leave`.

**Both formats render a membership event.** `render_jsonl_object` writes `op` / `kind` /
the elected key map / `after` verbatim — the domain op sits at the top level and `after`
carries only the resolver's output keys, in `resolve_membership_columns` order (the
transparent format).
`render_debezium_message` re-wraps the same event as a canonical insert (see § The Debezium
format): envelope `op` is `c`, `before` is `null`, and `after` gains a leading `event`
discriminator column carrying the `join` / `leave` op. The element-field portion of the
Debezium `after`, minus that `event` column, equals the JSONL `after` byte-for-byte — the
transparent-JSONL / masquerade-Debezium split.

### The JSONL format

`render_jsonl_object` shapes each event as `{seq, op, ts, kind, key: {…}, after}`,
keys inserted in exactly that serialized order (the encoder does not sort). `kind` is the
stream's resolved envelope value (§ Kind vocabulary). The key map is the one-entry
elected-surface map `{<elected surface's resolved output key>: <codec value>}` —
`{record_id: …}` under the default. `after` is the reconstructed full-row map, keyed by
the stream's resolved output keys (§ Output names and `rename`) — every value codec
`VARCHAR` (`str`) or `null` — on `c`/`u`, and `null` on `d`.
The key-plus-after nesting matches the Debezium format's re-wrap of the same
after-image (see § The Debezium format), so both formats render one event stream.

`write_jsonl_stream` pins the encoder for the deterministic-stream invariant: UTF-8,
compact `separators=(",", ":")` with no inter-token whitespace, `ensure_ascii=False`
(non-ASCII property text passes through as UTF-8, not `\uXXXX` escapes),
`sort_keys=False` (construction order preserved), exactly one `\n` terminating each
record, and no BOM. Because every after-image value is codec `VARCHAR` (`str`) or
`null` and `ts` is a `str` or an `int`, serialization is total and a given event
sequence always yields byte-identical output.

### The Debezium format

The `debezium` format is a second renderer over the same `StreamEvent` stream — same
fields, same `seq`, same canonical total order. It renders both content types: the
`state-changes` upsert log (`c` / `u` / `d`) and the `membership-events` append-only log
(insert-only `c`, see § Membership-events content above). It is pure output re-wrapping: no
new fold, no engine change, no new sink. Each `StreamEvent` becomes the Debezium **value**
message, the shape a Debezium connector emits off an OLTP database, so an author can feed a
CDC pipeline or teach against the message envelope. The mapping is a deterministic recoding
of the same after-image, so the streaming invariants hold for it. The format is implemented
in [`debezium.py`](../../src/fabulexa_forge/exporters/streaming/debezium.py); the
config block is `DebeziumConfig` / `DebeziumSourceIdentity` in
[`config/models.py`](../../src/fabulexa_forge/config/models.py).

**Op → before / after (`state-changes`).** The Debezium `op` is the `StreamEvent.op`
verbatim. The stream is an **upsert log** — insert on `c`, upsert on `u`, keyed by the
elected message key, with `d` retiring the key:

| `op` | `before` | `after` |
|---|---|---|
| `c` | `null` | full-row after-image, reconstructed at `created_sim_time` |
| `u` | `null` | full-row after-image at the event `sim_time` |
| `d` | the one-entry elected key map (`{ "record_id": <id> }` under the default) | `null` |

The key-only `before` on `d` is canonical Debezium under `REPLICA IDENTITY DEFAULT`,
where a delete carries only the primary key. The elected key is creation-constant,
known at every time ≤ the event, so it is the one before-image producible without state
reconstruction — it keeps the deleted identity visible in the value even though the
value-only stream emits no separate key message. No before-image reconstruction, no `r`
snapshot, no `t`/`m`.

**Membership-events content.** For `content: membership-events` the Debezium stream is an
**append-only event log**, not an upsert log. Every `join` / `leave` event renders as a
canonical insert — envelope `op` is `c`, `before` is `null`, `after` is the membership
payload — with the domain op carried as the leading `event` column of the after-image. There
is no `d` and no key-only tombstone:

| `StreamEvent.op` | envelope `op` | `before` | `after` |
|---|---|---|---|
| `join` | `c` | `null` | `{ event: "join", <owner identity>, <element fields> }` |
| `leave` | `c` | `null` | `{ event: "leave", <owner identity>, <element fields> }` |

Insert-only is the *faithful* Debezium rendering of an owner-keyed event log, not a
simplification: a real Outbox/Event-Router connector over an append-only event table emits
`op: c` for every row, with the event nature carried as a column (§ Rationale covers why the
owner key forces this append-only model rather than an upsert/delete stream). The `event`
value (`"join"` / `"leave"`, codec `VARCHAR`, never null) is a deterministic recoding of the
fold's `event_class` — the same value `StreamEvent.op` carries — known at the event's own
time, never the counterpart boundary time. Its name is fixed, and because payload keys are
bare it is a **reserved output name** on every membership stream: a field or rename target
resolving to `event` is refused at validation time, under both formats (§ Output names and
`rename`).

After the leading `event` column the after-image is the membership after-image verbatim —
the published owner identity entries (each under its resolved output key, in the
owner kind's sidecar column order), then one column per selected element-schema
field in `resolve_membership_columns` declaration order, each under its resolved
output key (a scalar `f` → `<f>`; a reference `f` → the `<f>_kind` / `<f>_id`
pair, both null or both non-null). With empty `fields` it is
`{event, <owner identity>}` — owner identity only. The element-field portion equals the
JSONL `after` byte-for-byte; the `event` column is the Debezium home of the op JSONL
carries at its top level. The full after-image order — `(event, <owner identity>,
<element fields>)` — is the single order both the rendered map and the value schema
follow.

Everything else derives exactly as for `state-changes`: `source`, `lsn` (=`seq`),
`sequence`, `snapshot`, `txId`, the `table_identity` masquerade (over the membership
`route_table` `<owner_kind>__<property>` — below), `ts_ms` (rebased event time), and
`transaction` (`null`). The value schema is built from `event` followed by the naming
authority's output keys for the membership stream; `event` goes through the same optional-string path as
every other after-image column — only the rendered payload, not the schema slot,
guarantees it is always present, so it must not be special-cased as a required schema
field — and the always-`null` membership `before` is schema-legal because the `before`
struct is optional.

**The value envelope.** When schemas are disabled the message *is* the `payload`
envelope; when enabled it is `{ "schema": <value schema>, "payload": <envelope> }`. The
envelope carries `before`, `after`, `source`, `op`, `ts_ms`, `transaction`. The
after-image (one entry per published identity surface under its resolved output
key, then one entry per selected property under its resolved output key) is codec
`VARCHAR` (`str`) or `null`, the same map the JSONL `after` carries. The `source` block
is the author-supplied identity plus the derived `ts_ms` / `lsn` / `sequence` /
`snapshot` / `txId` / `table`: `lsn` is `StreamEvent.seq`, `sequence` is
`"[null,\"<seq>\"]"` (mimicking Postgres `[last_commit, current]`), `snapshot` is
`"false"`, `txId` is `null`, and `table` follows `debezium.table_identity` (below). Two
values are declared deviations from canonical Debezium: `payload.transaction` is always
`null` (the sanitised subset has no transaction grain), and `payload.ts_ms` equals
`payload.source.ts_ms` equals the rebased event time — the determinism invariant
forbids the connector *processing* time (`now()`) canonical Debezium stamps into
envelope `ts_ms`. `ts_us` / `ts_ns` are not emitted. The exact field set and types are
the contract of `render_debezium_message`.

**`table_identity` — the masquerade knob.** `debezium.table_identity` governs what the
Debezium `source.table` (and the value-schema name `<source.name>.<table>.Value`)
reports. It is a *realism* knob — the question is what a real Debezium connector would
emit, not what is faithful to the bundle. It is read only by the `debezium` format;
`jsonl` ignores it — JSONL is the transparent format, Debezium is the masquerade.

| `table_identity` | `source.table` reports |
|---|---|
| `source_table` (default) | the event's `route_table` (leaf logical table) — canonical Debezium: the *origin* table, even inside a combined stream. For a flat kind this is the kind itself. |
| `topic` | the declaring stream's `name` (the routed destination). |

The bundle `kind` is deliberately **not** an option: `actor` is a modeling artifact,
not a table in the masqueraded database; `source_table` reports `actor` only when
`actor` is genuinely flat.

**Schema wrapping (`schemas_enable`).** The toggle is global to the run and defaults to
`true` — each message is then self-describing (`{schema, payload}`, what a learner sees
in every Debezium-JSON tutorial, needing no registry). At `false` each message is the
bare envelope. The value schema is built **per stream** — one declared column list per
topic, for both `table_identity` values — so a per-topic schema is well-defined by
construction (one topic = one stream = one column list, and one key space per topic by
the one-stream-one-key-surface gate); no schema-ambiguity check exists because no
ambiguous case is expressible. It is a Kafka-Connect `struct` descriptor of the
envelope: `before` and `after` are each an **optional** struct of optional-string
columns in after-image order, each field carrying the single naming authority's output key
(the `resolve_stream_columns` order — see
[`derivations.md`](derivations.md) § The row-state-events derivation), the struct itself
named `<source.name>.<table>.Value`; `source` is a non-optional struct; `op` is a
non-optional string; `ts_ms` is an optional `int64`; `transaction` is an optional
struct; the envelope is named `<source.name>.<table>.Envelope`. The `before`/`after`
field set equals the stream's after-image column set, so the declared schema and the
rendered rows never diverge. The `before`/`after` structs are `optional` precisely so
the key-only `d` before-image is legal: on a `d` the other declared fields are **absent
from the message, not null-filled**. The `payload.before` content is identical under
both `schemas_enable` settings — exactly the one-entry key map on a `d`, `null` on
`c`/`u`; the schema's `optional` flags govern only schema legality, not payload
content.

**Serialized key order.** The encoder is the JSONL writer's pinned encoder (UTF-8,
compact separators, `sort_keys=False`, one trailing newline), so insertion order is
wire order and is pinned for byte-identity. The pinned orders, normative for any
reimplementation:

- message (schemas enabled): `schema`, `payload`.
- `payload` (the envelope): `before`, `after`, `source`, `op`, `ts_ms`, `transaction`.
- `source`: `version`, `connector`, `name`, `ts_ms`, `snapshot`, `db`, `sequence`,
  `schema`, `table`, `txId`, `lsn`.
- `before` / `after` maps and the schema's `before`/`after` field lists: the after-image
  column order under the resolved output keys — the published identity surfaces in
  sidecar column order, then the projected properties in sidecar order
  (`resolve_stream_columns`).

The per-field Connect types and optionality follow `_SOURCE_FIELDS` and
`build_debezium_value_schema`; `source.ts_ms` is non-optional (always present) while
envelope `ts_ms` is optional, matching Debezium.

**Timestamps — epoch-milliseconds from the anchor.** `payload.ts_ms` and
`payload.source.ts_ms` are the rebased event instant in epoch-milliseconds, computed by
`rebased_epoch_ms` in the same absolute (UTC) frame the JSONL `ts` uses — the anchor's
resolved `start_instant` plus `event_sim_time` nanoseconds, truncated to milliseconds.
The derivation is **integer-only**: `EffectiveAnchor` carries no epoch field, so the
epoch microseconds are taken from its tz-aware `start_instant` by `timedelta`
floor-division and scaled to nanoseconds — `datetime.timestamp()` (float) is never used,
preserving determinism. Because `start_instant` is microsecond-resolution the
derivation is exact, and the millisecond value agrees with the JSONL path's
microsecond-truncated ISO string. The value is a pure function of `event_sim_time` and
the anchor, never `now()`, and never routes through the SQL/`ts` projection. A resolved
anchor is therefore mandatory: the format is refused up front when none resolves,
because emitting the raw-nanosecond `jsonl` fallback under the name `ts_ms` would
misrepresent an epoch-millisecond field.

**Source identity — masquerade vs derived.** `connector`, `name`, `db`, `schema`, and
`version` are the author-set masquerade block, required with no defaults; the author may
present any identity, including a real connector name, so identity-pattern-matching
consumers work unchanged. `ts_ms`, `lsn`, `sequence`, `snapshot`, `txId`, and `table`
are derived per event and are never configured. The identity maps to no base value;
rather than invent one (Principle #7), the exporter requires the author to declare it,
so omitting `debezium.source` under `--fmt debezium` is a load-time error. The
`schema` wire key maps to the model field `schema_` (a bare `schema` would shadow
`BaseModel.schema`); the renderer writes the literal key `schema`.

### The Kafka sink

The `kafka` sink delivers the merged, `seq`-stamped stream to a Kafka broker, one
message per event, in place of stdout or a file directory. It consumes the same
`Iterable[StreamEvent]` the stdout and file sinks consume and renders each event through
the same format renderer and the shared pinned encoder
([`encoding.py`](../../src/fabulexa_forge/exporters/streaming/encoding.py)), so for a
given `(event, fmt, anchor, schema)` the message value is **byte-identical to the
file-sink line minus its trailing newline**. The sink is format-agnostic: the driver
builds a per-event value-render closure from the selected format — the shared
`build_kafka_render_value` builder — and `write_kafka_stream`
([`kafka_sink.py`](../../src/fabulexa_forge/exporters/streaming/kafka_sink.py)) holds no
`jsonl`/`debezium` knowledge. The live mixer
([`mixer-control-plane.md`](mixer-control-plane.md)) calls the same builder, so the
format branch and the Debezium business rules are single-sourced across the stream Kafka
path and the mixer.

**Message shape.** Each event becomes one Kafka message:

| Field | Value |
|---|---|
| topic | `event.topic` — the declaring stream's name. Kafka does not preserve cross-topic order; the global order lives in `seq` / `source.lsn`, exactly as for one-file-per-topic. |
| key | the pinned-encoded one-entry elected key map (UTF-8, no newline), on every op including `d`. The key is **never** schema-wrapped, even under `schemas_enable`; one key space per topic by the one-stream-one-key-surface gate. The entry name is the elected surface's resolved output key, so the key bytes are part of what an identity `rename` changes — **value**-identical under rename, not byte-identical; one topic still carries exactly one identity space. |
| value | `render_value(event)` — the JSONL object or Debezium value message, pinned-encoded without the trailing newline. |
| record timestamp | `rebased_epoch_ms(event.event_sim_time, anchor)` — the rebased event instant in epoch-milliseconds (equal to the Debezium `source.ts_ms`), never broker append time. |
| partition | each topic has exactly **one** partition, so produce-order equals `seq`-order on the partition. |

**Ordering and delivery.** The producer is idempotent, `acks=all`, in-order under
retries — no silent reordering or drops. The sink `flush`es (blocks until every message
is acknowledged) before returning; an unacked message at flush, or a delivery callback
reporting failure, is a `KafkaDeliveryError` and fails the run. Under a realtime clock
(`paced=True`) the sink serves delivery (`poll`) incrementally as the pacer releases each
event, then flushes once at the end; unpaced (`paced=False`) it produces all events then
flushes once. The produced keys, values, timestamps, topics, and per-partition order are
identical across both pacing modes — only wall-clock produce timing differs.

**Topic pre-creation.** Every topic in the run's topic set is created — 1 partition,
replication factor 1, idempotently — **before** the first produce (broker auto-create is
off). A topic that already exists with 1 partition is used as-is; one that exists with
any other partition count is a `KafkaDeliveryError`, because the global-`seq` guarantee
depends on a single partition and the sink never alters an existing topic. A topic that
receives zero events is still created and still appears in
`StreamOutcome.events_per_topic` with count 0 — the Kafka form of the
declared-but-empty-topic guarantee (an empty topic, not an empty file). Partition count
(1) and replication factor (1) are fixed by the ordering invariant and the single-broker
target, not author knobs.

**Bootstrap resolution.** The one effective bootstrap-servers string resolves by
CLI-wins precedence — `--bootstrap-servers`, then the config `kafka` block, then the
`FABEXPORT_KAFKA_BOOTSTRAP` environment value — mirroring anchor and clock resolution. An
empty or whitespace-only CLI or environment value counts as absent and falls through to
the next source; the package invents no default endpoint (Principle #7), so when no
source contributes a non-blank string the run fails with `KafkaBootstrapUnresolvable`.
`resolve_bootstrap_servers` reads no environment itself — the CLI passes
`os.environ['FABEXPORT_KAFKA_BOOTSTRAP']` in — so it is a pure, testable function.

**Debezium over the kafka sink.** Under `--fmt debezium` the driver enforces the same
Debezium business rules before delivery — the `debezium` block is required. The
value-render closure reuses `render_debezium_message` and `rebased_epoch_ms`, so the
value bytes equal the Debezium file-sink line minus its newline.

**The client is an optional extra.** `confluent-kafka` is the `[kafka]` install extra,
imported lazily inside the sink only. With the kafka sink selected and the client not
importable, the run fails with `KafkaClientUnavailable` naming the fix.

### `init --mode streaming`

`init` proposes; the author edits and owns. The proposal is a commented candidate
config, a pure function of `(emit, code version)`, implemented by
`generate_stream_init_config`
([`init.py`](../../src/fabulexa_forge/exporters/streaming/init.py)) and wired to the
CLI's `init --mode streaming` arm — a sibling of the dimensional and source proposal
engines. It consumes the sidecar's records tables (declaration order),
`subtype_values`, `sub_type_columns`, per-column temporal classes, the records-column
taxonomy, the slice-only policy, the `presentation_keys` registry (for the `keys`
proposal), and the membership tables — **not** `record_roles` (warehouse role plays no
part in a stream). The temporal surface is required as consumed: an emit predating
per-column temporal classes fails with the reader's own refusal
(`TemporalClassUnavailableError`), exactly as the stream engine's slice-only check does
— no dedicated `init` error exists (`SourceHistoryTrackedRequired` is source's posture
because source *export* requires the flags; streaming's does not). It infers no intent:
names are sidecar identity verbatim, and the degenerate sub-type value `default` is
proposed as `name: default` — the author renames if they care. The proposal is
annotated with the emit's forwarded documentation as YAML comments — scenario
narrative, table descriptions on stream stubs, per-property description/unit,
discriminator glosses — under the shared annotation contract
([`documentation-channel.md`](documentation-channel.md) § `init` annotations);
comments are not grammar, so the self-gating posture is untouched.

Every proposed stream is **live**. The commented-out mechanism is reserved for genuine
alternatives (the membership-events block, collision losers, topic-illegal names) —
never for advice — so the emitted config always parses and streams clean by
construction: a collision pair's first entry always stays live, and should no proposal
survive live at all (every sidecar-derived name topic-illegal — a degenerate sidecar),
`init` refuses (`StreamInitNothingToStream`) rather than emit a config that cannot
parse.

| Emit condition | Proposal |
|---|---|
| ≥ 1 records kind | Live `content: state-changes` config; the membership alternative fully commented (below) |
| Flat kind | One live stream: `name: <kind>`, `properties` = the kind's payload-role `prop__` columns, bare, minus non-exempt `slice_only` (`ref_index__*` are identity-role and never proposed; `presentation_id` is presentation-role and not property-selectable) |
| Sub-typed kind | One live stream per declared sub-type, in `<kind>_type` domain order: `name: <sub_type>` verbatim, `sub_types: [<sub_type>]`, `properties` = that sub-type's `sub_type_columns` payload-role `prop__` entries, bare, minus non-exempt `slice_only`. The discriminator is not proposed (constant within the stream — the partition's contract carve-out already excludes it) |
| Sub-typed kind, sidecar omits `sub_type_columns` | Per-sub-type streams still, each proposing the kind's full payload-role `prop__` set minus the discriminator (constant within a single-sub-type stream) and minus non-exempt `slice_only`, with a comment noting the sidecar carried no partition — the init engines' union-fallback convention (dimensional's posture) |
| A population with no tracked property | Its stream proposed **live**, headed by a comment noting the feed is lifecycle-only (`c`/`d`); deleting it opts out |
| Two proposals resolve one name (e.g. two kinds sharing a sub-type value, or a sub-type value equal to a flat kind's name) | The later proposal (sidecar order) emitted commented out with a comment naming the collision — the emitted config always parses and streams clean, the self-gating posture. The rule spans both content blocks: membership auto-names collide too (`<K>_<p>` is underscore-ambiguous — kind `a_b` × property `c` and kind `a` × property `b_c` both derive `a_b_c`), and inside the fully-commented membership alternative the loser is excluded from the uncommentable body and carried as a collision comment, so uncommenting the block wholesale yields a config that parses and streams clean |
| A proposal whose sidecar-derived name fails the topic-name rule (only a sub-type value can — kind names and membership `<K>_<p>` names are table-name segments, identifier-safe by construction) | Emitted commented out with a comment naming the rule and the offending value — the collision-loser posture; the author renames and uncomments. `init` never sanitizes (a rewritten name would be an invented identity) |
| Key election | The `keys` block per the key-election `init` contract ([`key-election.md`](key-election.md) § `init` proposals): the shared election menu, spliced verbatim — a uniform `record_index` active election plus per-population commented alternatives. The uniform proposal is gate-clean by construction (per-stream uniformity is trivially satisfied over the proposed single-population streams; `record_index` edge spaces are self-union-safe), so the emitted config parses and streams clean with no repair pass |
| Each `membership__<K>__<p>` table | One membership stream in the fully-commented `content: membership-events` alternative block: `name: <K>_<p>`, `membership: {kind: <K>, property: <p>}`, `fields` = every element-schema field (bare names) |
| No records kind | Error (`StreamInitNothingToStream`) — a candidate config that cannot stream is not proposed. A membership table cannot exist without its owner's records table (an interval requires an owner record within the slice — the contract's § Membership-category and § Records-category existence rules), so a recordless emit has nothing to stream and no membership-only branch exists |
| Non-exempt `slice_only` columns | Never proposed; one `slice-only-column-omitted` notice each through the caller-supplied `NoticeSink` |
| `rebase` / `debezium` / `clock` / `kafka` blocks | Never proposed — delivery and environment knobs, not emit-derived (no invented identities or endpoints); one trailing comment names them and where they would go |
| `identity` / `rename` / `kind_label` / `kind_labels` / `where` / `only` / `ignore` | Never proposed — an identity projection, a rename target, a label, a predicate, and a change scope are each author intent with no sidecar-derived value, and proposing one would be invention (Principle #7). They join the same trailing comment naming the never-proposed surfaces and where they would go. The consequence for `identity` is the intended default: a generated streaming config publishes each topic's elected surface alone |

`StreamInitNothingToStream` is a direct child of `ExporterError` (the dimensional
`InitRequiresRecordRoles` posture: `init` runs no engine and reads no config, so its
failure is not an `ExportError`); the CLI `init` verb reports it as a clear stderr
message with a non-zero exit.

## Invariants

1. **Deterministic stream.** Same emit + same `StreamConfig` + same code version →
   byte-identical event sequence (content and `seq`), independent of wall-clock timing.
   Byte-identity is the contract of the pinned encoder — shared by the JSONL and
   Debezium writers — not incidental.
2. **Faithful reshaping.** Every emitted value is a base value, a deterministic
   recoding of base values (`op` / `event_class` / `seq` / `ts` / a published identity
   surface), or `null` with one declared meaning. Nothing is fabricated. Duplication
   across overlapping declared streams is declared intent, each copy tracing to the
   same base values. Projection never invents and never hides a value: an identity
   surface's entry ships iff the stream publishes it — an unpublished surface is
   absent, not null-filled; a published one is the election relation's value
   verbatim under the codec.
3. **Temporal honesty.** No value on an event derives from base state later than the
   event's `sim_time`, except selected type-1 properties (carried at current value on
   every event by their current-value-only contract). Elected identity surfaces are
   creation-constant, so the identity join introduces no late knowledge. Row selection
   honors the same rule and needs no exception: `where` columns are constant-gated and
   discriminators are creation-constant, so the satisfying record set is one set for the
   whole tape and no event's *inclusion* depends on state later than the event.
4. **Single-branch.** The stream is over the sole branch; more than one branch is
   refused via the single-branch guard (`require_single_branch`).
5. **Declaration-determined event set.** A stream's event set is a function of its
   declared row scope (populations × `sub_types` × `where`) and its change scope
   (`only` / `ignore`) — never of `properties` / `fields`, `rename`, `kind_label`, or
   `render`. Two streams over one population with equal row and change scope carry
   identical event sets, whatever they project or however they name it.
6. **Presentation invariance.** For a fixed declaration, adding or changing `rename` /
   `kind_labels` / `kind_label` changes output key strings and `kind` / member-kind
   value strings only — payload keys, published identity keys, and therefore the
   Kafka message key bytes, whose one-entry map carries the entry name (the key is
   **value**-identical under rename, not byte-identical). Event count, order, `seq`,
   `ts`, elected values, and topic assignment are byte-identical. Adding or removing
   a published identity surface changes which entries a row carries and nothing
   else. A run declaring no vocabulary renders byte-identically to one where the
   vocabulary surfaces did not exist. Renaming never moves a value: every join that
   holds under contract names holds after a rename.
7. **One naming authority.** Every after-image output key on a stream — identity and
   payload alike — is produced by that stream's one resolver, which the after-image
   assembly and the Debezium value-schema build both read — so no rename can make
   the declared schema and the rendered rows disagree, and no output key silently
   collides. The elected surface carries one resolved key across its four sites
   (message key map, after-image identity entry, Debezium `d` before-image, Debezium
   value schema): a consumer joining a topic's key against its own payload cannot
   mismatch.
8. **One stream, one key surface — and no ungated identity.** Every population a
   stream's keys draw from elects one surface, so a topic carries one key space; a
   record's events keep one key for its whole life (creation-constant surfaces) and
   the `d` keys the tombstone. No identity surface reaches a published row without
   passing resolution, union safety, and the uniqueness guard over the populations
   the stream spans (§ Identity projection). An identity rename is per-stream
   presentation and does not propagate: a reference column in another stream still
   renders its target's elected surface under its own property's output key — a
   topic keyed `id` may be referenced as `patient_id` elsewhere; the values join,
   the names need not match, the shape real CDC feeds have.
9. **Ordering reads `record_id`.** The canonical order and merge key read the fold's
   `record_id` regardless of the elected message key — election changes what the
   consumer keys on, never how the stream is sequenced. Scope removes events; it never
   reorders the survivors, and a dropped event consumes no `seq`.
10. **Schema ↔ row agreement (Debezium).** When schemas are enabled, the Debezium
    `before`/`after` struct field set equals the stream's after-image column set, under
    the same output keys and in the same order, so the declared schema and the rendered
    rows never diverge. For `state-changes` that set is the stream's
    `resolve_stream_columns` order; for `membership-events` it is
    `(event, <owner identity>, <element fields>)`. Per-topic schemas are well-defined
    by construction: one topic = one stream = one column list.
11. **Epoch-millis honesty (Debezium).** `ts_ms` is epoch-milliseconds whenever it is
    emitted; the format is unavailable when no anchor can produce epoch-milliseconds.
12. **Upsert-log shape (Debezium).** Per record the message sequence is one `c`, zero
    or more `u`, optionally one terminal `d`; `before` is `null` except the key-only
    image on `d`.
13. **Deterministic produced messages (Kafka).** Same emit + same config + same code →
    the identical produced message sequence: per topic, ordered `(key, value bytes,
    timestamp_ms)` tuples. Wall-clock produce timing (governed by the clock) and
    broker-assigned metadata (offsets, log-append metadata) are excluded.
14. **Single partition per topic (Kafka).** Each topic carries exactly one partition; it
    is a hard precondition of the global-`seq` ordering guarantee, not a tuning choice. A
    pre-existing topic with any other partition count fails the run.
15. **Flush-before-return (Kafka).** The sink returns only after every produced message
    is acknowledged; a partial delivery is an error, never a silent success.
16. **Faithful unpivot (membership-events).** Every membership event traces to one
    materialized interval row; `op` / `event_class` / `seq` / `ts` are deterministic
    recodings. No interval is invented and none dropped, except the faithful
    no-`leave`-for-an-open-interval rule and the declared owner scope. No event carries
    the counterpart boundary time — a `join`'s payload never reflects `left_sim_time`.
17. **Append-only owner-keyed log (membership-events).** Membership events form an
    append-only fact log keyed on the owner's elected surface; they are not an
    upsert/compaction stream, and no `leave` tombstones a key.
18. **Total order up to byte-identical events (membership-events).** The canonical order
    ties only between byte-identical events (multiplicity ≥ 2); `seq` resolves the tie and
    the emitted byte stream is deterministic regardless of which physical row sorted first.
19. **Insert-only membership Debezium.** Every membership Debezium message has envelope
    `op: c` and `before: null`; there is no `u`, no `d`, and no key-only tombstone. The
    append-only event-table model is the faithful Debezium rendering of an owner-keyed
    membership log.
20. **Faithful op recoding (membership Debezium).** `payload.after.event` ∈ {`join`,
    `leave`} is a deterministic 1-to-1 recoding of the fold's `event_class` (the value
    `StreamEvent.op` carries) — no fabrication.
21. **Element-field format-parity (membership).** The membership Debezium `payload.after`,
    minus its leading `event` column, equals the membership JSONL `after` for the same event,
    byte-for-byte. The `event` column is the Debezium home of the value JSONL carries as its
    top-level `op`.

## Validation Rules

**Parse-time** (Pydantic; `StreamConfig`, `KindStream`, `MembershipStream`,
`MembershipRef`, `DebeziumConfig`, `DebeziumSourceIdentity` in
[`config/models.py`](../../src/fabulexa_forge/config/models.py)): `extra='forbid'`;
`content` is the `state-changes` / `membership-events` literal. The `StreamDeclaration`
union discriminates on which of `kind` / `membership` an entry carries — an entry with
neither or both fails parse with a message naming the two shapes, so the illegal shapes
are unrepresentable rather than validated away. `kind_stream_well_formed` requires the
stream `name` to match the topic-name rule (`^[A-Za-z0-9._-]+$` and not `.` or `..`),
`properties` entries bare (no `prop__` prefix) and duplicate-free, and `sub_types`
non-empty and duplicate-free when present; `membership_stream_well_formed` applies the
same name rule, requires `fields` entries bare (no `elem__` / `member__` prefix) and
duplicate-free, and requires owner `sub_types` non-empty and duplicate-free when present.
On both shapes `identity` is optional with no default — non-empty and duplicate-free
when present, its members the `KeySurface` literal, so an unknown surface name is
unrepresentable rather than validated away; whether the set contains the elected
surface, and whether the kind can source each member, are business rules — parse time
knows neither the election nor the sidecar.
Both apply the shared rename- and where-map helpers to the author surfaces: a present
`rename` is non-empty with non-empty keys and targets and no two keys sharing one target;
a present `where` is non-empty with non-empty keys, its per-entry value shape carried by
`PredicateValue` ([`row-predicates.md`](row-predicates.md)); `only` and `ignore` are
mutually exclusive, each non-empty and distinct when present; `kind_label` is non-empty
when present. `kind_labels_well_formed` requires a present `StreamConfig.kind_labels` map
to be non-empty with non-empty keys and values and no two keys sharing one label — the
injectivity half that needs no emit. Each of these fields is optional with no default, so
absence is genuinely absent (Principle #7): no `rename` means bare keys, no `where` means
every row, no `only` / `ignore` means the full audited set. `properties` and `fields`, by
contrast, are **required with no default**: `[]` must
be written to declare an identity-only feed — omission is an error, never a silent
notification stream. On `StreamConfig`, `streams_match_content` requires a non-empty
`streams` list whose every entry's shape matches `content` (kind-shaped for
`state-changes`, membership-shaped for `membership-events`); `stream_names_unique`
rejects two streams sharing a name (same-kind and same-table repeats are legal —
identity is the name); `keys_well_formed` is `ExportConfig`'s validator verbatim (a
present `keys` map is non-empty, as is every per-kind map). The reused `RebaseConfig`
block, when present, sets at least one of `base_date` / `timezone`. The optional
`debezium` block (omittable for `jsonl`, the same optional-block exception `rebase`
takes) forbids unknown fields; `table_identity` is the `source_table` / `topic` literal
defaulting to `source_table`; `schemas_enable` defaults to `true`; its `source` and
every `source.*` field are required, non-empty strings. The optional `kafka` block (the
same optional-block exception, inert unless `--sink kafka`) forbids unknown fields and
requires a non-empty `bootstrap_servers`.

**Business rules** run in `iter_stream_events` as one **eager** pass — at call time,
before the iterator yields and before any fold is materialized (matching the dimensional
engine — the engine surfaces business rules itself; there is no separate config-load
pass). `iter_stream_events` validates, then returns an inner generator for the fold /
merge / yield, so the pass has already run when the Debezium driver builds per-stream
value schemas *between* constructing the iterator and consuming it; the schema build
therefore cannot reach an unresolved kind or property. Each rule raises `ExportError`
(an `ExporterError`) — or an election error, the election surface's `ExporterError` subclasses —
all caught by the CLI's `(ReaderError, ExporterError)` funnel (exit 1). The pass is
authoritative: it renders the fold's own `TableNotFoundError` / `ExportError`
unreachable defensive backstops. Every per-stream rule's message leads with the stream
name — the author's handle, and (with overlapping streams legal) the only component
that identifies the offending declaration.

The pass is also where streaming's one notice is emitted, through the required
caller-supplied `notice_sink` `iter_stream_events` takes ([`notices.md`](notices.md)) —
before any fold materializes, so an author sees an unobserved predicate value before any
byte is delivered. Every consumer threads the sink: the stream driver paths (the CLI
passes the stderr renderer) and the mixer's `seed_mixer_run`, which takes the same
required parameter and passes it through.

| Rule | Checks | Message |
|---|---|---|
| `SingleBranch` (reused guard) | the sidecar enumerates exactly one branch | `require_single_branch`'s verbatim message (see [`derivations.md`](derivations.md) § Validation Rules) |
| `StreamKindResolvable` | each kind-shaped stream's `kind` has a `records__<kind>` table | `"stream '{name}': kind '{kind}' has no records__{kind} table"` |
| `StreamSubTypesRequireSubtyping` | `sub_types` is present only on a stream whose subject kind has a non-empty `subtype_values` domain — the declared kind on a kind stream, the **owner** kind on a membership stream | `"stream '{name}': kind '{kind}' is not sub-typed; sub_types is not addressable"`, over the owner kind for a membership stream |
| `StreamSubTypesDeclared` | every `sub_types` value is in the subject kind's declared domain | `"stream '{name}': sub_type '{value}' is not declared for kind '{kind}'"` (a `prop__`-prefixed value is simply not a declared discriminator value and fails here), over the owner kind for a membership stream |
| `StreamPropertyResolvable` | each selected property resolves to a `prop__` column on the stream's kind | `"stream '{name}': property '{property}' has no prop__{property} column on kind '{kind}'"` |
| `StreamPropertySliceOnly` | no author-named property resolves to a non-exempt `temporal_class: slice_only` column ([`slice-only.md`](slice-only.md)) — over `properties` and over the `only` / `ignore` change scope alike, the refuse-only posture applied to each new author-named surface. The after-image is wholly author-named — there is no auto-projection to narrow — so streaming refuses rather than omitting, and emits no notices; `membership-events` is outside the population (membership columns carry no class). The reader's `TemporalClassUnavailableError` surfaces through the same funnel | Leads with `stream '{name}'`; names the kind, the property, and the class |
| `MembershipResolvable` | each membership-shaped stream's table exists | `"stream '{name}': membership '{kind}.{property}' has no membership__… table"` |
| `MembershipFieldResolvable` | each selected field resolves to an `elem__<f>` column or a `member__<f>__kind` / `member__<f>__id` pair on its table | `"stream '{name}': field '{field}' has no elem__/member__ column"` |
| `StreamPropertyNotAddressable` | no selected property names an identity surface — identity is projected through `identity`, never selected through `properties` (a distinct authoring mistake from "no such property", warranting its own remedy) | `"stream '{name}': '{property}' is an identity surface — declare it in identity, not properties"` |
| `StreamIdentityMissingElected` | a declared `identity` contains the stream's elected surface — a topic must publish its own key | `"stream '{name}': identity omits the elected surface '{surface}'; a topic must publish its own key"` |
| `StreamIdentityUnavailable` | `presentation_id` is published only on a kind that mints a surrogate | `"stream '{name}': the kind '{kind}' mints no presentation_id"` |
| `StreamRenameUnresolvable` | every `rename` key names a selected property (field) of its stream, or a published identity surface's contract column name | `"stream '{name}': rename key '{key}' names no selected property"` (field-variant for a membership stream) — with `"; this stream publishes {surfaces}"` appended only when the key is an unpublished surface name (a plain property-name typo does not advertise identity surfaces) |
| `StreamOutputNameCollision` | per stream, output keys pairwise distinct and disjoint from the reserved names — each published identity surface's **resolved** output key, and the membership `event` | `"stream '{name}': output name '{key}' collides with '{other}'"` |
| `StreamKindLabelUnknown` | every `kind_labels` key names a sidecar kind | `"kind_labels: '{kind}' is not a kind in this emit"` |
| `StreamKindLabelCollision` | no label, and no per-stream `kind_label`, equals a **different** kind's rendered name over the emit's whole kind universe. Two streams sharing one `kind_label` is legal — the rule is the masquerade refusal, not cross-stream uniqueness | `"stream '{name}': kind_label '{label}' collides with kind '{kind}'"`; the config-level variant carries no stream prefix |
| `StreamWhereNotConstant` | every `where` key names a `constant`-class payload property of the subject kind (tracked and `slice_only` refused) | `"stream '{name}': where key '{key}' is not a constant-class property of kind '{kind}'"` |
| `StreamWhereOnDiscriminator` | no `where` key names the subject kind's declared discriminator | `"stream '{name}': where key '{key}' is the discriminator; use sub_types"` |
| `StreamWhereColumnUnresolved` | every `where` key resolves to a payload property of the subject kind (structural columns, element fields, unknown names refused) | `"stream '{name}': where key '{key}' is not a payload property of kind '{kind}'"` |
| `StreamWhereValueUncastable` | every `where` element casts to its resolved column's sidecar-declared type | `"stream '{name}': where value '{value}' does not cast to {type} for '{key}'"` |
| Out-of-domain `where` value | element outside its column's declared `enum_domains` entry | A per-element `discriminator-value-unobserved` **notice**, never an error; leads with `stream '{name}'`, carries the shared two-case wording (topic-will-be-empty when no element of the entry was observed; element-contributes-no-events otherwise), emitted in eager-pass iteration order (streams → `where` keys → elements) |
| `StreamChangeScopeUnresolvable` | every `only` / `ignore` entry resolves to a `prop__` column of the stream's kind | `"stream '{name}': {field} entry '{property}' has no prop__{property} column on kind '{kind}'"` |
| Election resolution gates | `ElectionKindUnknown` / `ElectionSubTypeUnknown` / `ElectionPresentationUndeclared` — the election surface's gates, reused verbatim ([`key-election.md`](key-election.md) § Static gates) | Those messages |
| Stream render elections | each `render:` key names a declared property (or membership field) of the stream's projection, and its source column carries the election's admitted sidecar type — `DecimalSourceIsDouble` / `JsonPrecisionSourceIsVarchar`, run per declared stream ([`value-rendering-elections.md`](value-rendering-elections.md) § Validation Rules) | Leads with `stream '{name}'`; names the property and the type |
| Stream key uniformity | one stream, one key surface: every population the stream's keys draw from elects the same surface (kind-shaped: the spanned populations; membership-shaped: the addressed owner population set — the declared owner `sub_types`, else the owner kind's full domain); uniform `presentation_id` additionally pairwise union-safe | `ElectionMixedIdentity` / `ElectionUnionUnsafe`, naming the stream and the differing (population, surface) pairs |
| Identity projection gates | every **published** surface (§ Identity projection) runs the election's own gates over the stream's spanned/addressed populations: `presentation_id` published only where the registry declares each population; each published surface's key spaces pairwise union-safe | `ElectionPresentationUndeclared` / `ElectionUnionUnsafe`, naming the stream, the surface, and the population or pair |
| Edge union safety | per after-image reference column and per membership member field, admitted target populations' resolved surfaces pairwise union-safe; the admitted set is the kind-targeted posture — the target kind's full declared domain (per member kind for a member field) | `ElectionUnionUnsafe`, naming the stream, the column, and the unsafe pair |
| Published-key uniqueness | render-time, per composed identity relation (elected or published) at the end-of-tape entry point: `rows = DISTINCT record_id = DISTINCT value`, value non-NULL | `ElectedKeyDuplicate`, naming the stream or edge and the surface — a violation on a published non-elected surface reads as that surface's, never as an election the author did not declare |
| `DebeziumRequiresConfig` | format `debezium` carries a `debezium` config block (both content types) | `"format 'debezium' requires a 'debezium' config block with a 'source' identity (connector, name, db, schema, version)"` |
| `DebeziumRequiresAnchor` | format `debezium` has a resolved `EffectiveAnchor` | `"format 'debezium' requires a resolved effective anchor (set rebase.base_date / rebase.timezone, or rely on the sidecar runtime anchor); ts_ms must be epoch-milliseconds"` |
| `KafkaRequiresAnchor` | sink `kafka` has a resolved `EffectiveAnchor` — for **all** formats, `jsonl` included | `ExportError` — `"sink 'kafka' requires a resolved effective anchor …; the Kafka record timestamp must be epoch-milliseconds"` |
| `KafkaBootstrapUnresolvable` | sink `kafka` resolves a non-blank bootstrap string from CLI / config / env | `KafkaBootstrapUnresolvable` — `"sink 'kafka' requires a bootstrap-servers address; set --bootstrap-servers, a kafka.bootstrap_servers config block, or FABEXPORT_KAFKA_BOOTSTRAP"` |
| `KafkaClientUnavailable` | sink `kafka` can import `confluent-kafka` (the `[kafka]` extra) | `KafkaClientUnavailable` — names the fix (install the extra) |
| Pre-existing topic partition mismatch | each topic in the run's topic set has exactly 1 partition | `KafkaDeliveryError` — checked at delivery time |

**Ordering.** The election's own gates run first: a stream's elected surface is
resolved and the identity-uniformity gate has already refused a mixed-election
stream before any projection resolves, so `resolve_identity_projection` takes a
uniform election as precondition and neither re-checks nor raises
`ElectionMixedIdentity`. A stream that is both mixed-election and carries a
malformed `identity` reports the mixing — the earlier and more basic failure.

The resolvability rules are content-conditional: the declaration union already
guarantees every entry is the right shape, so for `content: membership-events` only the
membership rules apply, in the same eager pass, needing only `config` + sidecar.
`MembershipResolvable`'s table-existence check is intentionally strict: the contract
emits a `membership__<owner_kind>__<property>` table only for a collection-struct
property with at least one interval in the slice, so a validly-declared property with
zero intervals has no table and fails the rule (the reader-first failure mode — the
reader refuses to interpret a table that was never emitted). This is distinct from a
declared-but-empty topic, which covers a table that *exists* but yields no events on
the branch.

The two `Debezium*` rules raise `ExportError` from `stream_export` and surface through
the same funnel; they are reachable only because the CLI's flag-level `--fmt` guard
accepts `debezium` (a flag-level rejection would pre-empt them). They run in the driver
— not the eager pass — because `iter_stream_events` never receives `fmt`, so the path
where `fmt` is in scope owns them. The two driver paths are **not** identically
ordered: the stdout/file path (`_stream_export_debezium`) checks
`DebeziumRequiresConfig` → `DebeziumRequiresAnchor`; the kafka path
(`_stream_export_kafka`) checks `KafkaRequiresAnchor` first, *before* the
`fmt == "debezium"` block (the Kafka record timestamp needs the anchor regardless of
format), then `DebeziumRequiresConfig` — it has **no** `DebeziumRequiresAnchor`, since
`KafkaRequiresAnchor` already guarantees a resolved anchor by the time the Debezium
block runs.

The four `Kafka*` rules fire only for `--sink kafka`. `KafkaRequiresAnchor` reuses
`ExportError` with a message constant (mirroring `DebeziumRequiresAnchor`) and, unlike
the file/stdout sinks — which tolerate `anchor=None` and emit raw-ns `ts` — applies to
`jsonl` too, since a Kafka record timestamp must be epoch-milliseconds.
`KafkaBootstrapUnresolvable` and `KafkaClientUnavailable` are direct `ExporterError`
children resolved before delivery; the partition-count check is a `KafkaDeliveryError`
(an `ExportRuntimeError`, the writer-failure domain) raised by the sink at delivery time.
All land in the CLI's `(ReaderError, ExporterError)` funnel as exit 1.

**CLI usage** (`cmd_stream`, flag-level, before the engine runs): the sink (`--sink` in
`{stdout, file, kafka}`), the sink/out pairing (`--sink file` supplies an output
directory; `--sink stdout` and `--sink kafka` supply none — `--out` is forbidden for
both), and the format (`--fmt` in `{jsonl, debezium}`) are usage constraints the CLI
guards directly — a violation is a usage error to stderr (exit 1), before the funnel. The
engine and the writers also assert the sink/out pairing defensively
(`ExportRuntimeError`), and `stream_export` rejects an unsupported `fmt`.

## Rationale

- **A delivery driver, not a shape-mode.** Streaming produces no target schema — no
  grain, no tables, no `mode` — so `StreamConfig` is a top-level envelope, a sibling of
  `ExportConfig`, not another `mode` value. Folding it into `ExportConfig` would force
  a schema-shaped two-tier grammar onto an event stream that has no schema to declare.
- **The name is on the declaration.** The dimensional and source modes both put the
  output name on the declaration (`name: entity_product`, `sub_types: [product]`);
  streaming follows them: the stream name *is* the topic. Routing naming through a
  template-plus-groups indirection would make the author predict a rendered
  intermediate name to rename one topic, and would name the degenerate single-sub-type
  case (`default`) by workaround rather than an edit. Combining sub-types is listing
  them in one stream; renaming is editing `name`.
- **Per-sub-type streams are the realistic default.** A polymorphic kind's sub-types
  live in different systems in the real world and arrive on different feeds; none
  shares a stream, and none carries another's columns. The sidecar's
  `sub_type_columns` partition states which value columns each sub-type owns — a NULL
  in a non-owned column is structurally inapplicable, not unrecorded — so per-stream
  column lists render each feed with exactly its own columns, and the combined-stream
  NULL is the author's declared choice.
- **Events are population facts — row-level CDC.** A real connector emits an event
  whenever the row changes, whatever columns the consumer projects; a feed silent
  about changes to unselected columns, or an identity-only feed with no `u` events at
  all, matches no real CDC system. Making the event set payload-independent also makes
  `state-changes` consistent with `membership-events` (whose `join`/`leave` events
  were payload-independent from the start) and aligns streaming's event set with the
  playback seam's selection-invariant row set. The split lives in the fold — a change
  scope and a projection scope — so the single column-order producer rule survives
  verbatim rather than being patched around by engine-side trimming.
- **Payload keys are bare because the prefixes are plumbing.** `prop__` / `elem__` /
  `member__` are the reader's disambiguation of a records table's column families; they
  describe how the bundle stores a value, not what the value *is*. A consumer of a CDC
  topic sees a row from an application database, and no such database names a column
  `prop__status`. Shipping the prefixes put engine vocabulary on the wire in the one mode
  that could not rename its way out of it, so the bare name is the default and `rename`
  addresses it.
- **The batch modes' grammars, reused rather than re-invented.** `rename`, `kind_labels`,
  `where`, `sub_types`, and `only` / `ignore` all mean in a stream exactly what they mean
  in source, down to the refusals. An author who has written a source config can read a
  stream config, and the shared helpers, the shared predicate authority, and the shared
  selection spine leave no second implementation to drift. A mode adding a selection
  surface adds a *gate* on which columns are addressable, never a second value grammar.
- **The constant gate is what makes a stream's selection well-defined.** A stream replays
  every instant of the tape, so a predicate on a `tracked` column would have to answer
  *as of when* — its value at each event's own time and its current value select different
  records, and either answer makes the event set a function of the horizon. Gating `where`
  to `constant`-class properties makes the question unposable, and is what lets the
  satisfying record set be computed once, before the folds, and applied uniformly.
- **`kind_label` is feed presentation; `kind_labels` is a kind claim.** The two carry
  different integrity rules because they make different claims. A label in `kind_labels`
  renders as a *value* wherever a member-kind cell ships, so the rendered vocabulary must
  stay injective over the whole kind universe or one rendered name would identify two
  kinds. A per-stream `kind_label` names the domain concept the feed represents — usually
  sub-type grain, which the kind universe cannot express, since sub-types are the
  first-class domain concepts and kinds are simulation machinery. Two feeds may legitimately
  present as the same concept, so sharing is legal; what remains refused is masquerading as
  a *different* kind, because that string does identify a kind elsewhere in the payload.
- **Scope is declaration-dependent; payload is not.** Narrowing a stream's `u` volume is a
  real authoring need — a security feed wants decision changes, not every attribute touch —
  and the fold already separated change scope from projection. Routing the narrowing
  through change scope keeps the event set a property of the *declaration* while leaving it
  independent of what the stream projects, so the two knobs stay orthogonal and a
  notification-shaped feed (in scope, not projected) and a context-carrying feed (projected,
  not in scope) are both expressible.
- **No disjointness gate across streams.** Source's event log is one numbered log, so two
  sources auditing one item space would corrupt a single sequence and are gated. Streams
  have no such shared artifact: each topic is an independent declared feed, and duplication
  across overlapping streams is declared intent. There is nothing for a disjointness rule
  to protect, so selection may overlap freely.
- **Two declaration models, discriminated.** The kind-shaped and membership-shaped
  declarations are two Pydantic models discriminated on which of `kind` / `membership`
  an entry carries, so a shape-mixing declaration is unrepresentable and each shape's
  required fields stay required — no silent identity-only feed from a forgotten
  `properties` key.
- **The message key follows the election.** A real connector keys on the table's
  primary key — in the masqueraded app database, the app-visible identity, not a
  simulation-internal id. Streaming therefore consumes the cross-mode key-election
  surface rather than pinning `record_id`: the elected surface is the message key, and
  every reference the after-image carries renders in its target's elected surface, so
  a consumer can join a stream against a key-elected source or base export on equal
  values. The compaction property the fixed-`record_id` key provided — one key per
  record lifetime, `d` keys the tombstone — is guaranteed instead by the election
  gates: every electable surface is creation-constant, and one stream elects one
  surface. `record_id` remains the default, so an election-free config renders the
  natural id everywhere.
- **Membership Debezium is insert-only, forced by the owner key.** A membership stream is
  keyed on the owner, and one owner holds many concurrent members — so a `d` keyed on
  the owner would tombstone the whole collection under log compaction, and a `u` keyed
  on the owner would let one member's event overwrite another's. The only shape
  consistent with owner-keying is an append-only event table, whose faithful Debezium
  rendering is insert-only (`op: c`) with the membership op carried as the `event`
  column. Rendering the domain verb into `op` (`op: join`) would emit output no
  connector produces and no standard consumer recognizes, defeating the format's
  purpose — so insert-only is both the canonical choice and the faithful one.
- **Render `ts` in Python, not SQL.** The anchor's SQL projection strips the offset to
  a naive `TIMESTAMP`, which is unrecoverable across a DST fall-back fold; rendering the
  absolute instant in Python and projecting it into the zone keeps the true offset, so
  the timestamp remains faithful to the absolute instant.
- **The JSONL sink is streaming-local.** It consumes a pre-materialized, cross-stream
  merged, `seq`-stamped event iterable rather than a single `SELECT`, and it adds stdout
  as a target class — neither fits the generic writer's `SELECT → query_arrow →
  row-count` contract, so JSONL serialization lives with the engine, not in `writers/`.
- **The author declares the Debezium source identity.** The `source.connector` / `name`
  / `db` / `schema` / `version` identity maps to no base value; rather than invent a
  connector identity the exporter requires the author to supply it (Principle #7), so the
  `debezium.source` block is required under `--fmt debezium` with no defaults.
  `schemas_enable` keeps a behavioral default (`true`) because it is a shape toggle, not
  an invented identity.
- **`source_table` is the default `table_identity`, and `kind` is not an option.**
  Canonical Debezium reports the *origin* table even when several sub-types combine
  into one stream, so `source_table` is the faithful default. The bundle `kind`
  (`actor`) is a Fabulexa modeling artifact with no counterpart in the masqueraded
  database, so it is never reported as a table name; `source_table` surfaces `actor`
  only when `actor` is genuinely flat. `table_identity` lives in `DebeziumConfig`
  because the Debezium format is its only reader.
- **Debezium `ts_ms` requires an anchor.** Debezium `ts_ms` is epoch-milliseconds by
  definition; with no resolved anchor the only honest timestamp is the raw-nanosecond
  `jsonl` fallback, and emitting that under the name `ts_ms` would misrepresent the
  field. The format is refused up front rather than emit a mislabelled timestamp.
- **A single column-order producer, extended to a single naming producer.** The Debezium
  value schema, the engine's after-image keying, and the fold's SELECT all read column
  order from `resolve_stream_columns`, consulted per stream (see
  [`derivations.md`](derivations.md)). Renaming could have broken that agreement by giving
  the schema builder and the row assembler each their own view of the output names, so the
  resolver returns ordered `OutputEntry` values — each entry naming its source (an
  identity surface rendered through its election relation, or a fold column read
  verbatim) and its resolved output key — and both consumers read the one list: the
  declared schema and the rendered rows stay the same list under the same names
  by construction, and collisions are caught once, centrally, rather than surfacing as a
  schema/row mismatch at delivery.
- **Identity is projected, not property-selected.** The alternative — admitting
  `presentation_id` into a stream's `properties` — reads as the smaller grammar, but
  it reclassifies a column the base format places in the structural block, gives no
  `temporal_class`, and holds outside the value-column population, and which key
  election names as one of three identity surfaces. The reclassification pays for
  itself in exceptions: a "property" that is not `render`-addressable, not in
  `only` / `ignore`, not `where`-addressable, has no temporal class for the
  slice-only rule to read, and duplicates the identity entry when the population
  elects it. Five carve-outs for one column is the classification being wrong;
  projection needs none of them, because the surface never enters the payload
  namespace.
- **The fold stays complete; publishing layers project.** The row-state-events fold
  has three consumers with three audiences — the streaming engine, the source event
  log, the playback seam. A projection applied in the fold would impose one
  consumer's presentation choice on the other two, so derivation-layer identity is
  complete by definition and each publishing layer projects above the composed
  relation ([`key-election.md`](key-election.md) § Identity publication).
- **Publishing an identity surface earns the gates.** Two populations' key spaces
  overlapping in one published column is exactly the condition the union-safety
  algebra exists to refuse — and suppression alone would let an author re-create it
  by publishing a surrogate deliberately. Running the algebra over every published
  surface means a published identity column either joins cleanly or the export
  refuses; the registry tightening (publishing `presentation_id` requires the claim)
  is the claim-consuming-path posture the election and `declare_keys` already
  share, and the escape — not publishing — costs one line.
- **Elected-only is the stream's absence default.** A stream declares what ships —
  `properties` is required with no default, an absent `rename` means bare keys — so
  an absent `identity` publishes the minimum a topic cannot do without: its key. An
  unelected surrogate riding beside the message key would sit there looking equally
  key-like, the join trap the projection exists to prevent. The playback seam's
  absence default is deliberately the opposite, per its own convention
  ([`playback.md`](playback.md)).
- **No invented bootstrap endpoint.** A bootstrap address is environment-specific; the
  package supplies no default and resolves CLI → config → env, failing loudly when none
  is given (Principle #7) — the same stance anchor and clock resolution take.
- **Single partition, fixed by the ordering guarantee.** Global `seq` order survives
  end-to-end only on a single partition, so partition count is 1 by the ordering
  invariant, not an author knob; replication factor is 1 for the single-broker
  dev/educational target.
- **The key is never schema-wrapped.** Even under `schemas_enable` the message key is
  the bare one-entry key map; a Kafka key message schema is a separate key-channel
  artifact, so the after-image schema never leaks onto the key.
- **The client is an optional, lazily-imported extra.** `confluent-kafka` is needed only
  on the kafka path, so it is an `[kafka]` install extra imported inside the sink — a
  non-kafka install neither carries the dependency nor can defeat the import-time
  boundary.

## Boundaries

What the streaming exporter deliberately does not own:

- **Queue-state projection.** `membership-events` streams the raw `join`/`leave` interval
  boundaries faithfully; it does not derive wait time, FIFO/priority position, or any
  queue-state aggregate. That projection is a separate (queue-state) concern, not a streaming
  content type.
- **Cross-stream topic merging.** Two streams cannot share a name; a combined feed is
  declared as one stream (several `sub_types`, one column list). There is no
  post-declaration regrouping surface.
- **The message key stays single.** A topic's key is the one-entry map of the
  elected surface. Publishing several identity surfaces widens the after-image,
  never the key.
- **The `identity` grammar reaches only layers whose identity columns are not
  author-declared.** The unifying rule is that identity columns are never
  auto-projected — every publishing surface states what it publishes. Source states
  it through each declared table's `columns` list and its identity-column rename
  key; dimensional through author-declared dim columns — both already per-column
  and author-owned, so a second grammar saying the same thing would be a
  regression, not a unification. `identity` exists for the streaming after-image
  and the playback seam's tier-1 maps. Base is the one deliberate exception —
  its standalone surrogate ships auto-projected beside the re-derived key columns
  ([`base.md`](base.md) § Boundaries).
- **The three surface names are outside the payload namespace.** A producer payload
  property named `record_id`, `record_index`, or `presentation_id` is unaddressable
  on a stream (§ Identity projection) — on the wire an identity name means
  identity.
- **Reordering.** `rename` relabels; it never reorders. After-image order is the single
  column-order producer's, and no author surface moves a column within it.
- **Row selection over non-constant state.** `where` reaches `constant`-class payload
  properties only. Selecting on a value that changes over the tape would make the event
  set horizon-dependent, and there is no as-of qualifier to disambiguate it — the gate
  refuses rather than picking an instant.
- **Temporal elections.** A stream `render:` map carries the numeric value
  elections only ([`value-rendering-elections.md`](value-rendering-elections.md));
  the temporal family does not attach — payloads are string-typed by codec and
  `ts` is a separate contract ([`temporal-elections.md`](temporal-elections.md)
  § Boundaries).
- **Member-kind / per-field partitioning of memberships.** A membership stream feeds
  from one `(owner_kind, property)` table; the per-row member kind is a payload column
  (`member__<f>__kind`), not a declarable feed axis — one table may reference several
  member kinds, so there is no single stable per-relation member kind to declare on.
- **The Debezium key message and the null-value tombstone.** Each event becomes a
  single message carrying the **value** only. The Debezium key message and the
  post-delete null-value compaction tombstone are separate key-channel artifacts the
  sink does not emit; a delete is the format's normal delete value (a `null`-after JSONL
  object, or the Debezium `d` value). Keying every op on the record's creation-constant
  elected surface already lets a log-compacted topic collapse a record's `c`/`u`/`d`.
- **Avro, Schema Registry, and a key schema.** Message values are JSON (optionally with
  the embedded Debezium Connect schema); there is no Avro encoding, no Confluent Schema
  Registry integration, and the message key is never schema-wrapped.
- **Multi-partition and multi-broker topics.** Topics are single-partition,
  replication-factor-1 — the global-`seq` ordering guarantee and the single-broker target
  fix both. Partition count and replication are not author knobs, and multi-broker
  replication is out of scope.
- **Pacing.** The pacer is its own post-merge timing surface, not the driver's. The
  driver wraps the event stream through it and selects per-line-flushed delivery only
  when a realtime clock resolves; the scheduling, clock resolution, and paced-sink
  contract live in [`streaming-pacing.md`](streaming-pacing.md). Absent a clock the
  driver delivers unpaced, exactly as the default.
- **Windowed / incremental streaming.** The `--from` / `--to` / `--next` window
  machinery (see [`incremental.md`](incremental.md)) is not wired into `stream`; a run
  emits the whole stream in one invocation.
- **Before-images.** The stream is after-only CDC: a record's terminal state is
  delivered by the preceding `c`/`u`, and no full before-image is reconstructed. The
  JSONL `d` is a `null`-after tombstone; the Debezium `d` carries only the elected key
  map as its before-image — the one image producible without state reconstruction —
  never a full before-row.
- **Branch reshaping.** It streams the emit's sole branch and refuses more than one.
  Branch selection and per-branch streams are parked — the sanitised subset mandates one
  branch.

## Related

| Document | Why |
|---|---|
| [`key-election.md`](key-election.md) | The cross-mode election surface streaming consumes — the `keys` grammar, resolution gates, union-safety algebra, identity join relations, uniqueness guard, the streaming render sites (§ Rendering: streaming), and the derivation/publication identity layer split (§ Identity publication) |
| [`streaming-pacing.md`](streaming-pacing.md) | The realtime-pacing surface this driver composes — clock resolution (config × CLI), the drift-free release schedule, paced per-line-flush delivery, and the clock validation rules |
| [`derivations.md`](derivations.md) | The row-state-events fold (`state-changes` — including the change-scope / projection two-scope contract) and the membership-events fold (`membership-events`) this driver composes — `c`/`u`/`d` and `join`/`leave` generation, op classification, after-image reconstruction, and per-source order; the source of the shared `require_single_branch` guard |
| [`playback.md`](playback.md) | The seam that owns the canonical total order and the global-`seq` definition this stream conforms to; the tier-1 `events` head that re-seams `stream` later |
| [`selection-spine.md`](selection-spine.md) | The mode-neutral parent-lookup relation a stream's `where` and membership owner `sub_types` resolve through |
| [`row-predicates.md`](row-predicates.md) | The scalar-or-list `where` grammar, its literal typing, and the one rendering authority streaming compiles through |
| [`notices.md`](notices.md) | The channel the per-element `discriminator-value-unobserved` notice flows through, and the required-sink posture `iter_stream_events` follows |
| [`slice-only.md`](slice-only.md) | The export-wide `slice_only` policy — streaming's refuse-only posture over every author-named surface, and the class-level event-set vacuity |
| [`value-rendering-elections.md`](value-rendering-elections.md) | The per-stream `render:` map's numeric value elections and their codec-seam application to after-images |
| [`anchor.md`](anchor.md) | The `EffectiveAnchor` resolution surface — origin/zone precedence, `rebase` + CLI flags; the absolute instant `ts` renders from |
| [`reader.md`](reader.md) | The `Emit` / `Sidecar` surface this reads through — the records spine, current values, `subtype_values`, `sub_type_columns`, and the `history_tracked` flag |
| [`config-docstrings.md`](config-docstrings.md) | The three-channel docstring convention the `StreamConfig` models follow |
| [`config/models.py`](../../src/fabulexa_forge/config/models.py) | The `StreamConfig` / `StreamDeclaration` grammar these semantics bind |
| [`../CAPABILITIES.md`](../CAPABILITIES.md) | Feature inventory and status |
| [`README.md`](README.md) | Design index, package layout, staged roadmap |
| [`../../CLAUDE.md`](../../CLAUDE.md) | Principles, the isolation boundary, vocabulary |
