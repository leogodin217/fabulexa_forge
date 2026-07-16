# Streaming Exporter

**Status:** Implemented. Code is the contract — see
[`exporters/streaming/`](../../src/fabulexa_forge/exporters/streaming/)
(`types.py`, `engine.py`, `jsonl.py`, `driver.py`),
[`config/`](../../src/fabulexa_forge/config/) (`StreamConfig`,
`load_stream_config`), and
[`tests/exporters/streaming/`](../../tests/exporters/streaming/),
[`tests/config/test_stream_config.py`](../../tests/config/test_stream_config.py),
[`tests/test_cli_stream.py`](../../tests/test_cli_stream.py). Public API:
[`exporters/streaming/__init__.py`](../../src/fabulexa_forge/exporters/streaming/__init__.py).

The `fabulexa-forge stream` verb replays the base layer as an ordered, temporally-honest
event stream. It carries two content axes. `state-changes` replays the `history` change
ledger: the bundle's `history` table is a change-event ledger ordered by `sim_time`, and
`records__<kind>` carries each record's `created_sim_time` / `deactivated_at` / `active`
lifecycle spine and its current type-1 property values; together they are a natural CDC
stream — the shape a Debezium connector emits off an OLTP database — and the exporter
reconstructs, per record, the full row at every instant the row changed and emits those
reconstructions as ordered `c`/`u`/`d` change events. `membership-events` replays the
`membership__<K>__<p>` interval tables: each materialized membership interval unpivots into
a `join` event and, when the element left within the slice, a `leave` event, sourced
directly from the interval tables (collection-valued property changes emit no `history`
rows, so a history-sourced fold is blind to them). It is a **delivery driver, not a
shape-mode**: it declares no `mode`, no target schema, no grain, and carries its own
top-level `StreamConfig` envelope, a sibling of `ExportConfig`. It reads through the Stage-1
reader only and composes the derivations layer's row-state-events fold (`state-changes`) or
membership-events fold (`membership-events`) for its event content.

```
emit (run.duckdb + base.json @ v5)
   │  (reader: Emit + Sidecar; trunk-only — sole branch)
   ▼
content fold (per source, derivations layer)
   state-changes:     row-state-events  (c at created_sim_time | u at each later history sim_time | d at deactivated_at)
   membership-events: membership-events (join at joined_sim_time | leave at left_sim_time when non-null)
   one full payload (after-image) per event
   ▼
engine: materialize each source ▸ (sub-type select) ▸ k-way merge by canonical order ▸ stamp global seq
      ▸ route each event to its topic (Layer A attributes ▸ Layer B policy) ▸ render ts
   ▼
format: jsonl {seq, op, ts, kind, key:{record_id}, after}
      | debezium {schema?, payload:{before, after, source, op, ts_ms, transaction}} ▸ sink
   stdout (all topics interleaved, global seq order) | file (one <topic>.jsonl per topic)
```

---

## Surface

| Module | Owns |
|---|---|
| [`config/models.py`](../../src/fabulexa_forge/config/models.py) | `StreamConfig` (the `content` axis and its content-conditional `kinds` / `memberships` selection lists), `StreamKindSelection` (with `types` sub-type scope), `MembershipSelection` (one membership table's owner kind / property / carried fields), the optional `RoutingConfig` policy, the optional `DebeziumConfig` / `DebeziumSourceIdentity` block, and the optional `KafkaConfig` connection block — the top-level streaming envelope and its parse-time validators |
| [`config/loader.py`](../../src/fabulexa_forge/config/loader.py) | `load_stream_config` — YAML → validated `StreamConfig`, hard-bound (no mode dispatch) |
| [`exporters/streaming/types.py`](../../src/fabulexa_forge/exporters/streaming/types.py) | `StreamEvent` (one format-agnostic change event — `op` admits `c`/`u`/`d` and `join`/`leave`) and `StreamOutcome` (run counts) |
| [`exporters/streaming/engine.py`](../../src/fabulexa_forge/exporters/streaming/engine.py) | `iter_stream_events` — the up-front business-rule pass (including the routing rules), per-source fold materialization (per-kind row-state for `state-changes`, per-table membership for `membership-events`, dispatched on `config.content`), sub-type selection, the cross-source k-way merge, `seq` stamping, per-event topic/`route_table` stamping, and Python-side `ts` rendering |
| [`exporters/streaming/routing.py`](../../src/fabulexa_forge/exporters/streaming/routing.py) | `route_attributes` / `resolve_subtype_index` / `membership_route_attributes` (Layer A) and `resolve_topic` / `enumerate_topics` (Layer B) — the two-layer routing surface. Its contract is owned by [`streaming-routing.md`](streaming-routing.md) |
| [`exporters/streaming/encoding.py`](../../src/fabulexa_forge/exporters/streaming/encoding.py) | `encode_pinned` — the single byte-stable JSON encoder shared by every sink (stdout / file / kafka), so a given `(event, fmt, anchor, schema)` yields byte-identical message bodies across all three |
| [`exporters/streaming/jsonl.py`](../../src/fabulexa_forge/exporters/streaming/jsonl.py) | `render_jsonl_object` (the JSONL object shape) and `write_jsonl_stream` (the shared `encode_pinned` + stdout / per-kind-file sinks, with the `paced` per-line-flush mode — see [`streaming-pacing.md`](streaming-pacing.md)) |
| [`exporters/streaming/debezium.py`](../../src/fabulexa_forge/exporters/streaming/debezium.py) | `render_debezium_message` / `build_debezium_value_schema` / `rebased_epoch_ms` (the Debezium value-message shape, the embedded Connect schema, and the epoch-millisecond timestamp) and `write_debezium_stream` (the same shared `encode_pinned` + stdout / per-kind-file sinks, the same `paced` flush mode) |
| [`exporters/streaming/kafka_sink.py`](../../src/fabulexa_forge/exporters/streaming/kafka_sink.py) | `resolve_bootstrap_servers` (CLI → config block → environment bootstrap precedence) and `write_kafka_stream` (the Kafka producer lifecycle — topic pre-creation, per-event produce keyed by `record_id`, flush-before-return); `confluent-kafka` is imported lazily here only |
| [`exporters/streaming/pacer.py`](../../src/fabulexa_forge/exporters/streaming/pacer.py) | `ResolvedClock` / `resolve_clock` / `pace_events` — the realtime-pacing surface the driver composes. Its contract is owned by [`streaming-pacing.md`](streaming-pacing.md) |
| [`exporters/streaming/driver.py`](../../src/fabulexa_forge/exporters/streaming/driver.py) | `stream_export` — events → (pace when realtime) → format → sink for one run, the Debezium config/anchor business rules and the per-topic schema-ambiguity rule, the per-table-identity value-schema build, and the declared-but-empty-topic backfill (empty files + zero counts) |
| [`derivations/row_state_events.py`](../../src/fabulexa_forge/derivations/row_state_events.py), [`derivations/membership_events.py`](../../src/fabulexa_forge/derivations/membership_events.py) | The composed event-content folds — their semantics are owned by [`derivations.md`](derivations.md) § The row-state-events derivation and § The membership-events derivation |
| [`cli.py`](../../src/fabulexa_forge/cli.py) | `cmd_stream` — the `fabulexa-forge stream` verb, flag-level usage checks (including the `--speed` / `--idle-cap` / `--fast` clock checks and the `--sink stdout\|file\|kafka` / `--out` pairing), clock resolution, the `--bootstrap-servers` flag and `FABEXPORT_KAFKA_BOOTSTRAP` read for the kafka sink, and the `(ReaderError, ExporterError)` funnel |
| [`anchor.py`](../../src/fabulexa_forge/anchor.py) | The `EffectiveAnchor` the engine renders each event's `ts` from — see [`anchor.md`](anchor.md) |

## Boundary

- **Input.** An open `Emit` (trunk-only — sole branch), a validated `StreamConfig`,
  and a resolved `EffectiveAnchor` — `None` is admissible for `jsonl` (raw-ns
  timestamps) but not for `debezium`, whose `ts_ms` must be epoch-milliseconds. The
  driver consumes no target-schema file and no domain knowledge.
- **Output.** Newline-delimited JSON change events — a JSONL object or a Debezium
  value message per line — to **stdout** (all topics interleaved in global `seq` order)
  or to a **directory** (one `<topic>.jsonl` per topic in the run's topic set, including
  declared-but-empty ones); or, on the **kafka** sink, one message per event to a Kafka
  broker (one topic per route, each pre-created with a single partition). The output is
  an event stream, not a relation.
- **Reader-first; authors no base-table SQL.** Every table and column fact flows from
  the `Sidecar`. The driver composes the row-state-events derivation (`state-changes`) or
  the membership-events derivation (`membership-events`) for content and reuses the reader's
  sidecar accessors for the records spine and membership tables; it hard-codes no column
  list and renders each event's `ts` in Python from the anchor.
- **The streaming sinks are not generic writers.** Every streaming sink — stdout,
  file, and kafka — consumes an already-materialized, cross-source-merged, `seq`-stamped
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
| `content` | `state-changes` | The event content: per-record `c`/`u`/`d` full-row reconstruction from `history` + the records spine. Selects the row-state-events fold, read via the `kinds` selection. |
| `content` | `membership-events` | The event content: per-interval `join`/`leave` events from the `membership__<K>__<p>` tables. Selects the membership-events fold, read via the `memberships` selection. |
| format | `jsonl` / `debezium` | How each event is rendered — a flat JSONL object, or a Debezium value message (see § The Debezium format). Both formats render both content types. |
| sink | `stdout` / `file` / `kafka` | Where the rendered stream is delivered — interleaved on stdout, one `<topic>.jsonl` per topic, or one Kafka message per event (see § The Kafka sink). |

Each axis is a closed `Literal`, so a further content type, format, or sink is
additive. `content` selects both the fold the engine materializes and the selection list
it reads — `kinds` for `state-changes`, `memberships` for `membership-events`. The model
composes: `content` selects the fold the engine materializes,
format renders each `StreamEvent` to a serializable object, and the sink delivers it.
*Delivery timing* is an orthogonal fourth knob: an optional `clock` paces the rendered
stream against wall-clock time without touching any of the three axes or the bytes (see
[`streaming-pacing.md`](streaming-pacing.md)). It is unpaced by default.

### Cross-source merge and global `seq`

There is one **canonical total order** over all events of all in-scope sources, where the
*source identity* is the per-stream constant that makes the inter-stream tiebreak
deterministic — the record `kind` for `state-changes`, the `(owner_kind, property)`
membership-table pair for `membership-events`:

> `(event_sim_time ASC, event_class ASC, source_identity ASC, record_id ASC[, field-value tail])`

Each per-source fold already emits its rows sorted by
`(event_sim_time, event_class, record_id, …)` — the canonical order with `source_identity`
held constant (see [`derivations.md`](derivations.md) § The row-state-events derivation and
§ The membership-events derivation). The engine realizes the global order by a **k-way
merge** of the pre-sorted per-source streams (`heapq.merge` under the canonical key); it
does not concatenate-and-re-sort. The merge key is
`(event_sim_time, event_class, source_identity, record_id)`, read from the materialized
fold rows. The `source_identity` component is load-bearing: `heapq.merge` breaks
*inter-stream* ties by stream-argument order, not by the key, unless `source_identity` is in
the key — so the engine injects it per-stream (it is constant within each stream and is not
a fold column) to make the tiebreak deterministic. For membership-events the merge key
deliberately stops *before* the selected-field tail — the after-image field values that
realize the rest of the canonical order. It can, because `source_identity` is unique per
fold: two rows with an equal merge key always come from the same fold, where the fold's
`ORDER BY` has already sorted them by the field tail and `heapq.merge` (being stable)
preserves that order; rows from different folds never tie on the merge key. So field values
— including a SQL `NULL` or a reference `(kind, id)` pair, neither safely comparable in
Python — are never compared across folds. State-changes carries no field tail because its
key is already total.

`seq` is the 1-based position of an event in that order — a monotonic integer spanning
the whole stream, not reset per source — stamped once as the engine merges. For
`state-changes` the key is a **total** order with no ties: within a kind a record has at most
one `c` (at its `created_sim_time`), one `d` (at its `deactivated_at`), and one `u` per
distinct history `sim_time`, so no two events share all four components and `seq` resolves
nothing the key left ambiguous. For `membership-events` the order is total **up to
byte-identical events**: the contract permits byte-identical intervals (same key,
multiplicity ≥ 2), which produce byte-identical events (same time, class, owner, fields, op,
payload) and so tie the canonical key — but whichever the merge places first takes the lower
`seq`, and because the two differ only in `seq` the emitted byte stream is identical
regardless of which physical row sorted first. The merge reads its sort key — including
`event_class` — from the fold rows; the `StreamEvent` is constructed only after the merge and
carries `seq` and `op`, not `event_class` (once `seq` is stamped the order lives in `seq`).

**Coincident change-and-deactivation.** When a record's final history change lands
exactly at its `deactivated_at = D`, the `u` (its event time is the history `sim_time`)
and the `d` (its event time is `deactivated_at`) carry the same `event_sim_time = D`.
They tie on `sim_time`, and the `event_class` tiebreak — `u` is `1`, `d` is `2` —
orders the `u` strictly before the `d`. No final change is lost, and the `d` still
terminates the record.

### Message key

The message key is **always `record_id`** — on every op and for every source. For
`state-changes` it is the changed record's natural id, on every op including the `d`
tombstone; `presentation_id` rides inside the after-image only and is never the key, even
for a kind that carries a surrogate. Keying on the stable natural id (not the surrogate) is
what lets a record's `c`/`u`/`d` events share one key, so a downstream log-compacted topic
collapses them and the `d` keys the tombstone.

For `membership-events` the key is the **owner `record_id`** — the aggregate root of the
collection — so all of one collection's `join`/`leave` events share a key and stay ordered
together on one partition (a queue's arrival/departure order survives). Membership-events are
**not** log-compaction-coherent on this key: one owner holds many concurrent members, so the
key does not identify a single upserted row. That is the append-only-log consequence of the
owner key (an invariant below), not a defect — `membership-events` is a fact log, not an
upsert stream.

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

### Routing and empty streams

Routing partitions the merged, `seq`-stamped stream into named **topics** through a
two-layer surface (Layer A route-attribute derivation, Layer B policy) the author
configures in YAML; the full contract — sub-type selection, topic naming and grouping,
the `table_identity` masquerade, and the routing validation rules — is owned by
[`streaming-routing.md`](streaming-routing.md). The default policy (no `routing` block)
is one topic per leaf logical table, which is the kind itself for a non-sub-typed kind.
Routing is a pure post-merge partition: it stamps each event's `topic` and `route_table`
and never touches `seq`, the canonical order, the key, or the after-image.

| Sink | Layout |
|---|---|
| `stdout` | all topics interleaved, one JSON object per line, global `seq` order |
| `file` | one `<topic>.jsonl` per topic under the output directory, each in `seq` order |
| `kafka` | one topic per route, each pre-created with a single partition; one message per event, per-partition order == `seq` order (see § The Kafka sink) |

The `file` sink emits one `<topic>.jsonl` for every topic in the run's topic set even
when that topic yields zero events — an empty file, mirroring the generic writers'
zero-row-still-emits rule so the file set is exactly the enumerated topic set regardless
of data. The `stdout` sink writes no bytes for a fully empty stream.
`StreamOutcome.events_per_topic` is keyed by the run's topic set, not by what emitted: it
carries one entry per topic — value `0` for a topic that produced nothing — across **all
three** sinks alike. The `kafka` sink's form of the guarantee is a pre-created empty
topic (see § The Kafka sink). This declared-but-empty-topic guarantee (the empty files,
the pre-created empty topics, and the zero counts) is performed by `stream_export` (the
driver), layered over the writers' seen-only counts, so the CLI summary always lists
every enumerated topic. Either case is a successful run (exit 0).

### Membership-events content

`content: membership-events` streams `join`/`leave` events from the membership tables named
by the `memberships` selection — one membership-events fold per selected table, merged into
the one `seq`-ordered stream exactly as per-kind folds merge for `state-changes`. The fold's
unpivot, payload, and ordering contract are owned by [`derivations.md`](derivations.md) § The
membership-events derivation; this section is the content-level reading the engine and
formats give a membership `StreamEvent`.

`StreamEvent.op` admits `join` / `leave` alongside `c` / `u` / `d` — the only structural
difference; every other field carries its usual meaning. Per membership event:

- `op` is `join` or `leave`; both carry a full `after` payload (the membership-events log is
  append-only, so a `leave` is not a key-only tombstone — it carries what left).
- `kind` carries the **owner kind** (the record kind whose collection changed); the
  relation's `property` and the member identity live in the payload and the topic, not in
  `kind`.
- `record_id` is the **owner `record_id`** and the message key.
- `presentation_id` is `None` (memberships carry no surrogate).
- `route_table` is `<owner_kind>__<property>` (Layer A; see
  [`streaming-routing.md`](streaming-routing.md) § Layer A for membership-events).
- `after` is the owner `record_id` plus one entry per selected element-schema field, each
  value codec `VARCHAR` (`str`) or `null` — non-null on both `join` and `leave`.

**Both formats render a membership event.** `render_jsonl_object` writes `op` / `kind` /
`key: {record_id}` / `after` verbatim — the domain op sits at the top level and `after`
carries only the `resolve_membership_columns` columns (the transparent format).
`render_debezium_message` re-wraps the same event as a canonical insert (see § The Debezium
format § Membership-events content): envelope `op` is `c`, `before` is `null`, and `after`
gains a leading `event` discriminator column carrying the `join` / `leave` op. The
element-field portion of the Debezium `after`, minus that `event` column, equals the JSONL
`after` byte-for-byte — the transparent-JSONL / masquerade-Debezium split.

### The JSONL format

`render_jsonl_object` shapes each event as `{seq, op, ts, kind, key: {record_id},
after}`, keys inserted in exactly that serialized order (the encoder does not sort).
The key is always `{record_id}`. `after` is the reconstructed full-row map — every
value codec `VARCHAR` (`str`) or `null` — on `c`/`u`, and `null` on `d`. The
key-plus-after nesting matches the Debezium format's re-wrap of the same after-image
(see § The Debezium format), so both formats render one event stream.

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
(insert-only `c`, see § Membership-events content below). It is pure output re-wrapping: no
new fold, no engine change, no new sink. Each `StreamEvent` becomes the Debezium **value**
message, the shape a Debezium connector emits off an OLTP database, so an author can feed a
CDC pipeline or teach against the message envelope. The mapping is a deterministic recoding of the same after-image, so
the four streaming invariants hold for it. The format is implemented in
[`debezium.py`](../../src/fabulexa_forge/exporters/streaming/debezium.py); the
config block is `DebeziumConfig` / `DebeziumSourceIdentity` in
[`config/models.py`](../../src/fabulexa_forge/config/models.py).

**Op → before / after (`state-changes`).** The Debezium `op` is the `StreamEvent.op`
verbatim. The stream is an **upsert log** — insert on `c`, upsert on `u`, keyed by
`record_id`, with `d` retiring the key:

| `op` | `before` | `after` |
|---|---|---|
| `c` | `null` | full-row after-image, reconstructed at `created_sim_time` |
| `u` | `null` | full-row after-image at the event `sim_time` |
| `d` | `{ "record_id": <id> }` | `null` |

The key-only `before` on `d` is canonical Debezium under `REPLICA IDENTITY DEFAULT`,
where a delete carries only the primary key. `record_id` is the record's own immutable
identity, known at every time ≤ the event, so it is the one before-image producible
without state reconstruction — it keeps the deleted identity visible in the value even
though the value-only stream emits no separate key message. No before-image
reconstruction, no `r` snapshot, no `t`/`m`.

**Membership-events content.** For `content: membership-events` the Debezium stream is an
**append-only event log**, not an upsert log. Every `join` / `leave` event renders as a
canonical insert — envelope `op` is `c`, `before` is `null`, `after` is the membership
payload — with the domain op carried as the leading `event` column of the after-image. There
is no `d` and no key-only tombstone:

| `StreamEvent.op` | envelope `op` | `before` | `after` |
|---|---|---|---|
| `join` | `c` | `null` | `{ event: "join", record_id, <element fields> }` |
| `leave` | `c` | `null` | `{ event: "leave", record_id, <element fields> }` |

Insert-only is the *faithful* Debezium rendering of an owner-keyed event log, not a
simplification: a real Outbox/Event-Router connector over an append-only event table emits
`op: c` for every row, with the event nature carried as a column (§ Rationale covers why the
owner key forces this append-only model rather than an upsert/delete stream). The `event`
value (`"join"` / `"leave"`, codec `VARCHAR`, never null) is a deterministic recoding of the
fold's `event_class` — the same value `StreamEvent.op` carries — known at the event's own
time, never the counterpart boundary time. Its name is fixed and cannot collide: `elem__` /
`member__` columns are prefixed and `record_id` is reserved.

After the leading `event` column the after-image is the membership after-image verbatim — the
owner `record_id`, then one column per selected element-schema field in
`resolve_membership_columns` declaration order (a scalar `f` → `elem__<f>`; a reference `f` →
the `member__<f>__kind` / `member__<f>__id` pair, both null or both non-null). With empty
`fields` it is `{event, record_id}` — owner identity only. The element-field portion equals
the JSONL `after` byte-for-byte; the `event` column is the Debezium home of the op JSONL
carries at its top level. The full after-image order — `(event, record_id, <element
fields>)` — is the single order both the rendered map and the value schema follow.

Everything else derives exactly as for `state-changes`: `source`, `lsn` (=`seq`), `sequence`,
`snapshot`, `txId`, the `table_identity` masquerade (over the membership `route_table`
`<owner_kind>__<property>`, see [`streaming-routing.md`](streaming-routing.md) §
`table_identity` and the Debezium masquerade), `ts_ms` (rebased event time), and
`transaction` (`null`). The value schema is built from columns `("event",) +
resolve_membership_columns(...)`, keyed by the membership `route_table`; `event` goes through
the same optional-string path as every other after-image column — only the rendered payload,
not the schema slot, guarantees it is always present, so it must not be special-cased as a
required schema field — and the always-`null` membership `before` is schema-legal because the
`before` struct is optional.

**The value envelope.** When schemas are disabled the message *is* the `payload`
envelope; when enabled it is `{ "schema": <value schema>, "payload": <envelope> }`. The
envelope carries `before`, `after`, `source`, `op`, `ts_ms`, `transaction`. The
after-image (`record_id`, `presentation_id` when the kind carries one, one `prop__<p>`
per selected property) is codec `VARCHAR` (`str`) or `null`, the same map the JSONL
`after` carries. The `source` block is the author-supplied identity plus the derived
`ts_ms` / `lsn` / `sequence` / `snapshot` / `txId` / `table`: `lsn` is
`StreamEvent.seq`, `sequence` is `"[null,\"<seq>\"]"` (mimicking Postgres
`[last_commit, current]`), `snapshot` is `"false"`, `txId` is `null`, and `table`
follows the routing `table_identity` policy — the event's `route_table` (leaf logical
table) by default, or its resolved `topic` (see [`streaming-routing.md`](streaming-routing.md)
§ `table_identity` and the Debezium masquerade). Two values are declared deviations from
canonical Debezium:
`payload.transaction` is always `null` (the sanitised subset has no transaction grain),
and `payload.ts_ms` equals `payload.source.ts_ms` equals the rebased event time —
the determinism invariant forbids the connector *processing* time (`now()`) canonical
Debezium stamps into envelope `ts_ms`. `ts_us` / `ts_ns` are not emitted. The exact
field set and types are the contract of `render_debezium_message`.

**Schema wrapping (`schemas_enable`).** The toggle is global to the run and defaults to
`true` — each message is then self-describing (`{schema, payload}`, what a learner sees
in every Debezium-JSON tutorial, needing no registry). At `false` each message is the
bare envelope. The value schema is **per table identity** (`route_table` or `topic` per
the routing `table_identity` policy — its `<source.name>.<table>.Value` name), built once
per identity key by `build_debezium_value_schema`. The row shape differs by logical source
table, so a topic that merges more than one source table — more than one kind
(`state-changes`) or more than one membership table (`membership-events`) — has no
unambiguous per-topic schema and is rejected up front (`StreamTopicSchemaUnambiguous`, see
[`streaming-routing.md`](streaming-routing.md)).
It is a Kafka-Connect `struct`
descriptor of the envelope: `before` and `after` are each an **optional** struct of
optional-string columns in after-image order (the unified `resolve_stream_columns`
order — see [`derivations.md`](derivations.md) § The row-state-events derivation),
named `<source.name>.<table>.Value`; `source` is a non-optional struct; `op` is a
non-optional string; `ts_ms` is an optional `int64`; `transaction` is an optional
struct; the envelope is named `<source.name>.<table>.Envelope`. The `before`/`after`
field set equals the after-image column set for that kind, so the declared schema and
the rendered rows never diverge. The `before`/`after` structs are `optional` precisely
so the key-only `d` before-image is legal: on a `d` the other declared fields
(`presentation_id?`, each `prop__<p>`) are **absent from the message, not null-filled**.
The `payload.before` content is identical under both `schemas_enable` settings — exactly
`{ "record_id": <id> }` on a `d`, `null` on `c`/`u`; the schema's `optional` flags
govern only schema legality, not payload content.

**Serialized key order.** The encoder is the JSONL writer's pinned encoder (UTF-8,
compact separators, `sort_keys=False`, one trailing newline), so insertion order is
wire order and is pinned for byte-identity. The pinned orders, normative for any
reimplementation:

- message (schemas enabled): `schema`, `payload`.
- `payload` (the envelope): `before`, `after`, `source`, `op`, `ts_ms`, `transaction`.
- `source`: `version`, `connector`, `name`, `ts_ms`, `snapshot`, `db`, `sequence`,
  `schema`, `table`, `txId`, `lsn`.
- `before` / `after` maps and the schema's `before`/`after` field lists: the after-image
  column order (`resolve_stream_columns`) — `record_id`, `presentation_id?`, then
  `prop__<p>` in sidecar order.

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
| topic | `event.topic` (the Layer-B routing output). Kafka does not preserve cross-topic order; the global order lives in `seq` / `source.lsn`, exactly as for one-file-per-topic. |
| key | the pinned-encoded `{"record_id": <id>}` (UTF-8, no newline), on every op including `d`, for every kind — never `presentation_id`. The key is **never** schema-wrapped, even under `schemas_enable`. |
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
Debezium business rules before delivery — the `debezium` block is required, and under
`table_identity='topic'` with `schemas_enable` no topic may merge multiple logical source
tables (`StreamTopicSchemaUnambiguous`). The value-render closure reuses `render_debezium_message`
and `rebased_epoch_ms`, so the value bytes equal the Debezium file-sink line minus its
newline.

**The client is an optional extra.** `confluent-kafka` is the `[kafka]` install extra,
imported lazily inside the sink only. With the kafka sink selected and the client not
importable, the run fails with `KafkaClientUnavailable` naming the fix.

## Invariants

1. **Deterministic stream.** Same emit + same `StreamConfig` + same code version →
   byte-identical event sequence (content and `seq`), independent of wall-clock timing.
   Byte-identity is the contract of the pinned encoder — shared by the JSONL and
   Debezium writers — not incidental.
2. **Faithful reshaping.** Every emitted value is a base value, a deterministic
   recoding of base values (`op` / `event_class` / `seq` / `ts`), or `null` with one
   declared meaning. Nothing is fabricated.
3. **Temporal honesty.** No value on an event derives from base state later than the
   event's `sim_time`, except selected type-1 properties (carried at current value on
   every event by their current-value-only contract).
4. **Single-branch.** The stream is over the sole branch; more than one branch is
   refused via the single-branch guard (`require_single_branch`).
5. **Schema ↔ row agreement (Debezium).** When schemas are enabled, the Debezium
   `before`/`after` struct field set equals the after-image column set, in the same order, so
   the declared schema and the rendered rows never diverge. For `state-changes` that set is
   the single `resolve_stream_columns` order for the kind; for `membership-events` it is
   `(event, record_id, <element fields>)` — the `("event",) + resolve_membership_columns`
   order.
6. **Epoch-millis honesty (Debezium).** `ts_ms` is epoch-milliseconds whenever it is
   emitted; the format is unavailable when no anchor can produce epoch-milliseconds.
7. **Upsert-log shape (Debezium).** Per `record_id` the message sequence is one `c`,
   zero or more `u`, optionally one terminal `d`; `before` is `null` except the key-only
   image on `d`.
8. **Deterministic produced messages (Kafka).** Same emit + same config + same code →
   the identical produced message sequence: per topic, ordered `(key, value bytes,
   timestamp_ms)` tuples. Wall-clock produce timing (governed by the clock) and
   broker-assigned metadata (offsets, log-append metadata) are excluded.
9. **Single partition per topic (Kafka).** Each topic carries exactly one partition; it
   is a hard precondition of the global-`seq` ordering guarantee, not a tuning choice. A
   pre-existing topic with any other partition count fails the run.
10. **Flush-before-return (Kafka).** The sink returns only after every produced message
    is acknowledged; a partial delivery is an error, never a silent success.
11. **Faithful unpivot (membership-events).** Every membership event traces to one
    materialized interval row; `op` / `event_class` / `seq` / `ts` are deterministic
    recodings. No interval is invented and none dropped, except the faithful
    no-`leave`-for-an-open-interval rule. No event carries the counterpart boundary time —
    a `join`'s payload never reflects `left_sim_time`.
12. **Append-only owner-keyed log (membership-events).** Membership events form an
    append-only fact log keyed on the owner `record_id`; they are not an upsert/compaction
    stream, and no `leave` tombstones a key.
13. **Total order up to byte-identical events (membership-events).** The canonical order
    ties only between byte-identical events (multiplicity ≥ 2); `seq` resolves the tie and
    the emitted byte stream is deterministic regardless of which physical row sorted first.
14. **Insert-only membership Debezium.** Every membership Debezium message has envelope
    `op: c` and `before: null`; there is no `u`, no `d`, and no key-only tombstone. The
    append-only event-table model is the faithful Debezium rendering of an owner-keyed
    membership log.
15. **Faithful op recoding (membership Debezium).** `payload.after.event` ∈ {`join`,
    `leave`} is a deterministic 1-to-1 recoding of the fold's `event_class` (the value
    `StreamEvent.op` carries) — no fabrication.
16. **Element-field format-parity (membership).** The membership Debezium `payload.after`,
    minus its leading `event` column, equals the membership JSONL `after` for the same event,
    byte-for-byte. The `event` column is the Debezium home of the value JSONL carries as its
    top-level `op`.

## Validation Rules

**Parse-time** (Pydantic; `StreamConfig`, `StreamKindSelection`, `MembershipSelection`,
`DebeziumConfig`, `DebeziumSourceIdentity` in
[`config/models.py`](../../src/fabulexa_forge/config/models.py)): `extra='forbid'`;
`content` is the `state-changes` / `membership-events` literal. The `kinds` and
`memberships` selection lists are **content-conditional**: `selection_matches_content`
requires exactly the selected content's list populated and the other empty — non-empty
`kinds` + empty `memberships` for `state-changes`, non-empty `memberships` + empty `kinds`
for `membership-events`. `kinds_unique` rejects a repeated kind; `memberships_unique` rejects
a repeated `(owner_kind, property)` pair. Each `kinds[].properties` entry is a bare name (no
`prop__` prefix) and may be empty. Each `memberships[].fields` entry is a bare element-schema
field name (no `elem__` / `member__` prefix — `fields_are_bare`), distinct within the list
(`fields_unique`), and the list may be empty (owner identity only). The reused `RebaseConfig`
block, when present, sets at least one of `base_date` / `timezone`. The optional `debezium` block (omittable for `jsonl`, the same
optional-block exception `rebase` takes) forbids unknown fields; `schemas_enable`
defaults to `true`; its `source` and every `source.*` field are required, non-empty
strings. The optional `kafka` block (the same optional-block exception, inert unless
`--sink kafka`) forbids unknown fields and requires a non-empty `bootstrap_servers`.

**Business rules** run in `iter_stream_events` as one **eager** pass — at call time,
before the iterator yields and before any fold is materialized (matching the dimensional
engine — the engine surfaces business rules itself; there is no separate config-load
pass). `iter_stream_events` validates, then returns an inner generator for the fold /
merge / yield, so the pass has already run when the Debezium driver builds per-kind
value schemas *between* constructing the iterator and consuming it; the schema build
therefore cannot reach an unresolved kind or property. Each rule raises `ExportError`
(an `ExporterError`), all caught by the CLI's `(ReaderError, ExporterError)` funnel
(exit 1). The pass is authoritative: it renders the fold's own `TableNotFoundError` /
`ExportError` unreachable defensive backstops.

| Rule | Checks | Message |
|---|---|---|
| `SingleBranch` (reused guard) | the sidecar enumerates exactly one branch | `require_single_branch`'s verbatim message (see [`derivations.md`](derivations.md) § Validation Rules) |
| `StreamKindResolvable` | each `kinds[].kind` has a `records__<kind>` table | `"stream kind '{kind}' has no records__{kind} table"` (caught from the reader's `TableNotFoundError` and re-raised) |
| `StreamPropertyResolvable` | (state-changes) each selected property has a `prop__<property>` column on its kind | `"stream kind '{kind}': property '{property}' has no prop__{property} column"` |
| `MembershipResolvable` | (membership-events) each `memberships[]` pair has a `membership__<owner_kind>__<property>` table | `"stream membership '{owner_kind}.{property}' has no membership__… table"` |
| `MembershipFieldResolvable` | (membership-events) each selected field resolves to an `elem__<f>` column or a `member__<f>__id` / `member__<f>__kind` pair on its table | `"stream membership '{owner_kind}.{property}': field '{field}' has no elem__/member__ column"` |
| `DebeziumRequiresConfig` | format `debezium` carries a `debezium` config block (both content types) | `"format 'debezium' requires a 'debezium' config block with a 'source' identity (connector, name, db, schema, version)"` |
| `DebeziumRequiresAnchor` | format `debezium` has a resolved `EffectiveAnchor` | `"format 'debezium' requires a resolved effective anchor (set rebase.base_date / rebase.timezone, or rely on the sidecar runtime anchor); ts_ms must be epoch-milliseconds"` |
| `KafkaRequiresAnchor` | sink `kafka` has a resolved `EffectiveAnchor` — for **all** formats, `jsonl` included | `ExportError` — `"sink 'kafka' requires a resolved effective anchor …; the Kafka record timestamp must be epoch-milliseconds"` |
| `KafkaBootstrapUnresolvable` | sink `kafka` resolves a non-blank bootstrap string from CLI / config / env | `KafkaBootstrapUnresolvable` — `"sink 'kafka' requires a bootstrap-servers address; set --bootstrap-servers, a kafka.bootstrap_servers config block, or FABEXPORT_KAFKA_BOOTSTRAP"` |
| `KafkaClientUnavailable` | sink `kafka` can import `confluent-kafka` (the `[kafka]` extra) | `KafkaClientUnavailable` — names the fix (install the extra) |
| Pre-existing topic partition mismatch | each topic in the run's topic set has exactly 1 partition | `KafkaDeliveryError` — checked at delivery time |

The two `Debezium*` rules raise `ExportError` from `stream_export` and surface through
the same funnel; they are reachable only because the CLI's flag-level `--fmt` guard
accepts `debezium` (a flag-level rejection would pre-empt them).

The resolvability rules are content-conditional. For `content: membership-events` the
state-changes rules (`StreamKindResolvable`, `StreamPropertyResolvable`, the `StreamTypes*`
sub-type rules) do not apply; `MembershipResolvable` and `MembershipFieldResolvable` replace
them in the same eager pass, needing only `config` + sidecar.
`MembershipResolvable`'s table-existence check is intentionally strict: the contract emits a
`membership__<owner_kind>__<property>` table only for a collection-struct property with at
least one interval in the slice, so a validly-declared property with zero intervals has no
table and fails the rule (the reader-first failure mode — the reader refuses to interpret a
table that was never emitted). This is distinct from a declared-but-empty topic, which covers
a table that *exists* but yields no events on the branch.
The Debezium driver rules are content-agnostic: a `membership-events` Debezium run flows
through the same checks a `state-changes` Debezium run does, in each path's own order. They
run in the driver — not the eager pass — because `iter_stream_events` never receives `fmt`,
so the path where `fmt` is in scope owns them. The two driver paths are **not** identically
ordered:

- **stdout/file (`_stream_export_debezium`):** `DebeziumRequiresConfig` →
  `DebeziumRequiresAnchor` → (when `table_identity='topic'` + `schemas_enable`)
  `StreamTopicSchemaUnambiguous`.
- **kafka (`_stream_export_kafka`):** `KafkaRequiresAnchor` (enforced first, *before* the
  `fmt == "debezium"` block, because the Kafka record timestamp needs the anchor regardless
  of format) → `DebeziumRequiresConfig` → (when `table_identity='topic'` + `schemas_enable`)
  `StreamTopicSchemaUnambiguous`. The kafka path has **no** `DebeziumRequiresAnchor`;
  `KafkaRequiresAnchor` already guarantees a resolved anchor by the time the Debezium block
  runs.

The four `Kafka*` rules fire only for `--sink kafka`. `KafkaRequiresAnchor` reuses
`ExportError` with a message constant (mirroring `DebeziumRequiresAnchor`) and, unlike
the file/stdout sinks — which tolerate `anchor=None` and emit raw-ns `ts` — applies to
`jsonl` too, since a Kafka record timestamp must be epoch-milliseconds.
`KafkaBootstrapUnresolvable` and `KafkaClientUnavailable` are direct `ExporterError`
children resolved before delivery; the partition-count check is a `KafkaDeliveryError`
(an `ExportRuntimeError`, the writer-failure domain) raised by the sink at delivery time.
All land in the CLI's `(ReaderError, ExporterError)` funnel as exit 1. The reused
`StreamTopicSchemaUnambiguous` routing rule constrains a debezium kafka run exactly as a
debezium file run.

The six **routing** business rules (`StreamTypesRequireRegistry`,
`StreamTypesRequireSubtyping`, `StreamTypesDeclared`, `StreamTemplatePlaceholders`,
`StreamGroupMembersResolve`, `StreamTopicSchemaUnambiguous`) run in this same eager pass
(`_validate_routing_rules`, plus the per-topic Debezium check in `stream_export`); their
contract is owned by [`streaming-routing.md`](streaming-routing.md) § Validation Rules.

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
- **Content-conditional selection lists, not a discriminated union.** `kinds` and
  `memberships` each default to `[]` and are required-and-non-empty for their own content,
  forbidden for the other — the populated-set invariant lives in the
  `selection_matches_content` cross-field validator. The `= []` default is not a Principle #7
  silent fallback: an empty list is never usable, the validator rejects it at parse time so a
  missing selection still fails at load time, and an empty list is not an invented mapping
  value. A discriminated union (`StateChangesConfig | MembershipConfig` keyed on `content`)
  would drop the defaults but split `StreamConfig` into two types, forcing `isinstance`
  narrowing at every `config.kinds` / `config.memberships` access and breaking the single
  `StreamConfig` signature the engine and driver dispatch through — so the one type and those
  signatures are kept, and the populated-set check moves to a validator instead.
- **Key on the natural id, not the surrogate.** A record's `c`/`u`/`d` events must
  share one message key for a log-compacted topic to collapse them and for the `d` to
  tombstone the record; the natural `record_id` is stable across the lifecycle where a
  minted surrogate need not be, so `record_id` is the key and `presentation_id` rides
  in the after-image only. A membership event keys on the owner `record_id` for the same
  one-aggregate-one-key reason, though its log is append-only rather than compactible.
- **Membership Debezium is insert-only, forced by the owner key.** A membership stream is
  keyed on the owner `record_id`, and one owner holds many concurrent members — so a `d`
  keyed on the owner would tombstone the whole collection under log compaction, and a `u`
  keyed on the owner would let one member's event overwrite another's. The only shape
  consistent with owner-keying is an append-only event table, whose faithful Debezium
  rendering is insert-only (`op: c`) with the membership op carried as the `event` column.
  Rendering the domain verb into `op` (`op: join`) would emit output no connector produces
  and no standard consumer recognizes, defeating the format's purpose — so insert-only is
  both the canonical choice and the faithful one.
- **Render `ts` in Python, not SQL.** The anchor's SQL projection strips the offset to
  a naive `TIMESTAMP`, which is unrecoverable across a DST fall-back fold; rendering the
  absolute instant in Python and projecting it into the zone keeps the true offset, so
  the timestamp remains faithful to the absolute instant.
- **The JSONL sink is streaming-local.** It consumes a pre-materialized, cross-source
  merged, `seq`-stamped event iterable rather than a single `SELECT`, and it adds stdout
  as a target class — neither fits the generic writer's `SELECT → query_arrow →
  row-count` contract, so JSONL serialization lives with the engine, not in `writers/`.
- **The author declares the Debezium source identity.** The `source.connector` / `name`
  / `db` / `schema` / `version` identity maps to no base value; rather than invent a
  connector identity the exporter requires the author to supply it (Principle #7), so the
  `debezium.source` block is required under `--fmt debezium` with no defaults.
  `schemas_enable` keeps a behavioral default (`true`) because it is a shape toggle, not
  an invented identity.
- **Debezium `ts_ms` requires an anchor.** Debezium `ts_ms` is epoch-milliseconds by
  definition; with no resolved anchor the only honest timestamp is the raw-nanosecond
  `jsonl` fallback, and emitting that under the name `ts_ms` would misrepresent the
  field. The format is refused up front rather than emit a mislabelled timestamp.
- **A single column-order producer.** The Debezium value schema, the engine's
  after-image keying, and the fold's SELECT all read column order from
  `resolve_stream_columns`, so the declared schema and the rendered rows are the same
  list by construction (see [`derivations.md`](derivations.md)).
- **No invented bootstrap endpoint.** A bootstrap address is environment-specific; the
  package supplies no default and resolves CLI → config → env, failing loudly when none
  is given (Principle #7) — the same stance anchor and clock resolution take.
- **Single partition, fixed by the ordering guarantee.** Global `seq` order survives
  end-to-end only on a single partition, so partition count is 1 by the ordering
  invariant, not an author knob; replication factor is 1 for the single-broker
  dev/educational target.
- **The key is never schema-wrapped.** Even under `schemas_enable` the message key is
  the bare `{"record_id": …}`; a Kafka key message schema is a separate key-channel
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
- **Member-kind / per-field routing of memberships.** A membership stream routes on the
  `(owner_kind, property)` relation identity; the per-row member kind is not a route attribute
  (see [`streaming-routing.md`](streaming-routing.md) § Boundaries).
- **The Debezium key message and the null-value tombstone.** Each event becomes a
  single message carrying the **value** only. The Debezium key message and the
  post-delete null-value compaction tombstone are separate key-channel artifacts the
  sink does not emit; a delete is the format's normal delete value (a `null`-after JSONL
  object, or the Debezium `d` value). Keying every op on `record_id` already lets a
  log-compacted topic collapse a record's `c`/`u`/`d`.
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
  JSONL `d` is a `null`-after tombstone; the Debezium `d` carries only the key
  (`{record_id}`) as its before-image — the one image producible without state
  reconstruction — never a full before-row.
- **Branch reshaping.** It streams the emit's sole branch and refuses more than one.
  Branch selection and per-branch streams are parked — the sanitised subset mandates one
  branch.

## Related

| Document | Why |
|---|---|
| [`streaming-routing.md`](streaming-routing.md) | The two-layer routing surface this driver composes — sub-type selection, topic naming and grouping, declared-but-empty topics, the `table_identity` masquerade, and the routing validation rules |
| [`streaming-pacing.md`](streaming-pacing.md) | The realtime-pacing surface this driver composes — clock resolution (config × CLI), the drift-free release schedule, paced per-line-flush delivery, and the clock validation rules |
| [`derivations.md`](derivations.md) | The row-state-events fold (`state-changes`) and the membership-events fold (`membership-events`) this driver composes — `c`/`u`/`d` and `join`/`leave` generation, op classification, after-image reconstruction, and per-source order; the source of the shared `require_single_branch` guard |
| [`anchor.md`](anchor.md) | The `EffectiveAnchor` resolution surface — origin/zone precedence, `rebase` + CLI flags; the absolute instant `ts` renders from |
| [`reader.md`](reader.md) | The `Emit` / `Sidecar` surface this reads through — the records spine, current values, and `history_tracked` flag |
| [`config-docstrings.md`](config-docstrings.md) | The three-channel docstring convention the `StreamConfig` models follow |
| [`config/models.py`](../../src/fabulexa_forge/config/models.py) | The `StreamConfig` grammar these semantics bind |
| [`../CAPABILITIES.md`](../CAPABILITIES.md) | Feature inventory and status |
| [`README.md`](README.md) | Design index, package layout, staged roadmap |
| [`../../CLAUDE.md`](../../CLAUDE.md) | Principles, the isolation boundary, vocabulary |
