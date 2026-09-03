# Stream Playback

The playback seam's stream-shaped head and per-event render surface. A
`StreamConfig`-bound sibling of the shaped tier replays an emit as the
streaming exporter's ordered `StreamEvent` feed — bounded (`events`),
mid-tape joinable (`seek`: Debezium snapshot-then-stream), topic-enumerable
(`topics`) — and a render resolved once per `(emit, config, fmt, anchor)`
(`StreamRender`) turns one `StreamEvent` into its message-body bytes, key
bytes, Kafka record timestamp, and embedded value schema with no driver and
no sink. It is the seam's first byte-producing contract, and the
`fabulexa-forge stream` delivery driver is its one mode-side consumer — the
seam owns a verb through it. Bounds, seek position, and format are
library-call arguments: `StreamConfig` carries no field for them, and the
CLI verb is whole-tape.

**Source:** [`playback/stream.py`](../../src/fabulexa_forge/playback/stream.py),
[`playback/stream_render.py`](../../src/fabulexa_forge/playback/stream_render.py);
the bounded and snapshot iterators live in the streaming engine
([`exporters/streaming/engine.py`](../../src/fabulexa_forge/exporters/streaming/engine.py)).
Tests: [`tests/playback/test_stream_head.py`](../../tests/playback/test_stream_head.py),
[`test_stream_seek.py`](../../tests/playback/test_stream_seek.py),
[`test_stream_render.py`](../../tests/playback/test_stream_render.py).
Public API: [`playback/__init__.py`](../../src/fabulexa_forge/playback/__init__.py).

## Boundary

- **Inputs.** An open `Emit`, a validated `StreamConfig` (either content
  type), a resolved `EffectiveAnchor | None`, and a required `NoticeSink` —
  the shaped tier's open shape. `resolve_stream_render` additionally takes
  the format (`jsonl` / `debezium`). The head and render are independently
  openable; a caller composing both passes one sink and one anchor to both.
- **Outputs.** Lazy `Iterator[StreamEvent]` answers and the declared topic
  tuple from the head; unframed body bytes, key bytes, epoch-ms integers,
  and Connect value-schema dicts from the render.
- **Layer direction.** A tier-2 sibling of `shaped.py`: imports `config` and
  the streaming exporter's **pure** compile/render surfaces only (`engine`'s
  `resolve_streams` / `iter_resolved_stream_events` /
  `iter_resolved_snapshot_events` / `build_topic_set`, plus `jsonl`,
  `debezium`, `encoding`, `presentation`, `routing`) — never the driver, the
  Kafka sink, or the pacer. In the reverse direction the delivery driver
  (`stream_export`) is the **one sanctioned mode-side consumer** of the head
  and render surface; no mode's compile/render surface imports the seam. The
  graph is acyclic at module granularity.
- **Non-inputs.** Nothing here paces, buffers, pushes, connects to a sink,
  or reads a clock (the seam's boundary razor). Sink selection, framing,
  pacing composition, the Kafka topic lifecycle, and `StreamOutcome`
  accounting are driver concerns ([`streaming.md`](streaming.md)).

## Semantics

### Bounds

`events(start, end)` follows the seam's one event-time line: half-open over
integer ns, `start ≤ event_sim_time < end`, either bound `None` for
unbounded. `events(None, None)` is the whole tape — byte-identical to the
verb's whole-tape run.

Bounding is **pure row selection over the merged in-scope event set** — the
seam's established restriction mechanism: every surviving event is
byte-identical (op, `ts`, after-image, key, topic, `route_table`, *and*
`seq`) to its whole-tape self. Bounds select events; they never recompute
them.

| Condition | Result |
|---|---|
| `events(T, T)` | Empty iterator |
| `start > end`, or a negative bound | `PlaybackError` (caller-contract violation, never a data condition) |
| Bound beyond the tape's last event | Exhaustion — total, no range check |
| Declared stream with zero in-window events | Yields nothing; `topics()` still lists it (declared intent drives topic existence) |

### Entry-point-invariant `seq`

`seq` is the event's 1-based position in the canonical total order over the
**whole in-scope stream** — a pure function of `(tape, config)`, never of
where the head entered. A head opened at a lower bound numbers its first
event `1 + N`, where `N` counts in-scope events strictly before the bound in
canonical order — a deterministic count, not a replay. Bounded and unbounded
heads agree; `seek(T)` then iterate matches a full play byte-for-byte from
`T + 1` onward.

The count is well-defined under the one order-totality caveat the stream
carries ([`streaming.md`](streaming.md) § Cross-stream merge): byte-identical
membership events (contract-legal multiplicity ≥ 2) tie the canonical key,
but they are counted by multiplicity and differ only in `seq`, so the
emitted byte stream is unaffected by which physical row sorts first.

### Seek

`seek(T)` = an initial-state phase at T, then `events(T + 1, None)`.
Position T is inclusive: the phase represents "every event with
`event_sim_time ≤ T` applied," and the live phase resumes at the next
instant, so no event is duplicated or lost across the boundary.

**`state-changes` content — the `r` phase.** For each declared stream, one
`r` (read) event per record in the stream's scoped row set that is **live at
T**: `created_sim_time ≤ T` and not deactivated at any instant `≤ T`. This
is compacted-topic semantics: a record whose `d` already passed had its key
retired, and a mid-tape joiner of a log-compacted topic never sees dropped
keys. Each `r` carries `op = "r"`, `event_sim_time = T`, `ts` rendered as on
every event, the record's identity/key/routing fields as on every event of
that record and stream, and:

- `seq = N` — the count of in-scope events with `event_sim_time ≤ T`: the
  stream position the snapshot represents, shared by every `r` of the phase
  (`0` when the phase precedes every event); the live phase begins at
  `N + 1`. An `r` sits outside the 1-based total-order numbering.
- `after` — the record's full published after-image reconstructed at T:
  identity entries per the stream's identity projection, then the declared
  `properties` — the same naming authority, renames, vocabulary, elections,
  and codec (`str`-or-`null`) as a `c`/`u` after-image. Reconstruction
  invokes the state fold over the kind's full tracked + constant property
  set and projects afterwards (the seam's normative invocation rule).

Phase ordering is the canonical order restricted to one instant and one
class: `(stream_name ASC, record_id ASC)` — deterministic, merge-compatible,
and per covering stream (an overlapping-streams record snapshots once per
covering stream, the multiplicity rule).

Change scope does **not** govern the `r` set or image: `only` / `ignore`
narrow *change* (`u`) membership; the `r` phase publishes *state*, which is
projection-scoped only. A record whose every post-creation change was
ignored is still live and still snapshots with its state at T.

| Condition (state-changes seek) | Result |
|---|---|
| Record created at exactly T, no `d` ≤ T | In the `r` phase; its `c` (time ≤ T) is not replayed |
| Record with `c` and `d` both ≤ T | Absent entirely — key retired (compaction semantics) |
| Record created after T | Arrives via its `c` in the live phase |
| `u` at exactly T | Folded into the `r` after-image; not replayed |
| Coincident `u` and `d` at exactly T | The `d` ≤ T retires the record — absent from the phase |
| No record live at T | Empty phase, then the live stream |
| Row outside the stream's `sub_types` / `where` scope | Absent, as everywhere |

**`membership-events` content — no snapshot phase.** `seek(T)` is
`events(T + 1, None)` with an empty initial phase. The content is an
owner-keyed append-only *fact log* — there is no per-key upsert state for a
snapshot to seed, and a connector snapshot over an outbox table replays
historical rows, which in this shape is a bounded replay the caller can
already express (`events(None, T + 1)`, then the live phase). Containment
*state* at T is a different answer shape, owned by the primitive tier's
membership snapshot ([`playback.md`](playback.md) § Snapshot). The two
content types are behaviorally distinct at seek exactly as they are at
delivery (upsert log vs fact log).

**The fusion is deliberate.** `seek` is the one composed answer — Debezium
snapshot-then-stream — and the `r` phase is not separately addressable, nor
is a bounded live tail (`seek` takes no end bound). A caller wanting free
composition has the primitive tier (`snapshot(T)` + `events`, in atom
shape); a stream-shaped standalone snapshot or bounded seek waits on a
demonstrated consumer need (vault note
`stream-playback-seek-fuses-the-snapshot-phase-to-an-unbounded-live-tail`).

**The seek-state equivalence (the testable headline).** For any consumer
folding the `state-changes` stream as an upsert log keyed by the elected key
(insert on `c`/`r`, upsert on `u`, retire on `d`): the folded state after
`seek(T)` + the live phase equals the folded state after a full play, for
every T. Byte equality holds from `T + 1` onward; the prefix is
state-equivalent, not byte-equivalent, by design. The equivalence is
**conditional** exactly as the seam's consistency algebra is: on a tape
whose defect manifest declares family-C/E breakage there is no single
consistent world-state, so seek and replay disagree exactly where the
manifest says the data is broken — the manifest is the answer key, not a
seam defect.

### The render surface

A render is resolved once per `(emit, config, fmt, anchor)` and is
thereafter a pure per-event function:

- **`render_bytes(event)`** — the message body: the UTF-8 encoding of the
  pinned compact JSON (`encode_pinned` settings) of the format's rendered
  object. **No framing**: line sinks append their one `\n`; the Kafka sink
  uses the bytes verbatim as the message value. One event yields one body
  byte sequence regardless of sink.
- **`render_key_bytes(event)`** — the UTF-8 pinned encoding of the one-entry
  elected key map `{key_column: key_value}` (the Kafka message key).
- **`timestamp_ms(event)`** — the Kafka record timestamp: the rebased event
  instant in epoch milliseconds under the render's anchor, per the
  integer-truncation rule ([`streaming.md`](streaming.md) § The Debezium
  format) — exposed so a producing adapter never re-derives the epoch frame.
  Available only on a render resolved with an anchor: on an anchorless
  (`jsonl`) render the call raises `ExportError` under the render surface's
  **own** anchor-requirement rule — the other anchor requirements are
  sink-scoped (the Kafka sink's) and format-scoped (the Debezium format's),
  and neither covers a sink-free anchorless render. An epoch instant without
  a declared calendar would be an invented value.
- **`value_schema_for(event)`** — the value schema this event's rendered
  message embeds, resolved from the event's own
  **`(topic, table-identity value)`** pair: the stream name under
  `table_identity: topic` (the pair degenerates to `(name, name)`) and the
  `route_table` leaf under `source_table`. The topic component is
  load-bearing: overlapping streams over one kind are legal with distinct
  `properties`, so under `source_table` identity one leaf carries a
  differently-fielded schema per covering stream — the leaf alone is not a
  schema identity, and "the topic's schema" does not exist. Each message
  embeds its own stream's schema. On a corrupted out-of-domain leaf the
  schema is built identically from the event itself (permissive totality —
  the message still embeds it, `route_table` the verbatim spine value), on
  every sink. `None` when `fmt = jsonl` or schemas are disabled. Total over
  the head's events, so no unknown-key ask exists. There is deliberately no
  run-level enumeration accessor (§ Boundaries).

Resolution is **self-vetting**: it runs streaming's eager business-rule pass
exactly as `open_stream_playback` does — the per-stream naming/schema state
it builds presupposes that pass's resolutions — raising the pass's own gate
identities and emitting the pass's notices through the required
`NoticeSink`, so a render resolves with no head open. A caller composing
head and render passes one sink to both and pays the eager pass twice (its
notices, and its selection-resolution spine read, twice with it). On top of
the pass, resolution enforces the format's business rules at resolve time
under their existing error identities: `debezium` with no resolved anchor is
refused (the `ts_ms` epoch-milliseconds rule); `debezium` with no `debezium`
block is refused (no invented mapping values — the block carries the source
identity). `jsonl` resolves with or without an anchor.

**One run, one anchor.** The render's `anchor` is the same resolved anchor
the head was opened with — the verb threads one; a caller composing head and
render passes one. The seam does not compare them: a mismatched pair is a
caller error whose only symptom is incoherent timestamps (`ts` renders under
the head's anchor; `ts_ms` and `timestamp_ms` under the render's).

### Rendering `r`

Both formats render the snapshot-read op, and no whole-tape play emits one —
`r` reaches a renderer only through `seek`:

| Format | `r` rendering |
|---|---|
| `jsonl` | `{seq, op: "r", ts, kind, key, after}` — the standard object with the full after-image |
| `debezium` | Envelope `op: "r"`, `before: null`, `after`: the full after-image; `source.snapshot: "true"` (every other op renders `"false"`). `source.lsn` is the event's `seq` — the shared snapshot position `N` — and `sequence` is `"[null,\"<N>\"]"`, so every `r` of one phase repeats one `lsn`, deliberately: snapshot reads share one source position, the one place `lsn` is not unique per message. Every other `source` field derives as on any op. Canonical Debezium snapshot-read semantics. Declared deviation: no `"last"` marker on the final snapshot record — every `r` renders `snapshot: "true"` (one fewer stateful special case; the phase boundary is observable as the op change) |

### Open-time behavior and errors

Open runs streaming's existing eager pass — the per-stream resolvability,
vocabulary, naming, selection, change-scope, and election gates — before any
event materializes, raising the pass's own error identities
(`ExportError` and its election/streaming subclasses, plus the reader-domain
`TemporalClassUnavailableError` the pass's `slice_only` check propagates).
The single-branch guard applies. Seam contract violations — negative bounds,
`start > end`, a negative `seek` argument — raise `PlaybackError`. Opening
replays nothing: no answer computes until an iterator is pulled, and
outstanding lazy answers are independently pullable (two heads over one emit
do not contend). Open is deliberately **not** sidecar-only — a declared,
scoped divergence from tier 1's open-reads-the-sidecar-only rule: the eager
pass's selection resolution reads the records spine (the data-backed case of
the selection out-of-domain notice), and the head runs that pass verbatim,
notice timing included. The spine read is a bounded scope check, never a
replay.

Permissive totality is inherited: semantic defects flow through verbatim; a
corrupted tape plays identically to an intact one, and on a
temporally-corrupted tape (family C/E defects) seek and replay disagree
exactly where the defect manifest says the data is broken.

### The delivery driver

`stream_export` consumes `events(None, None)` and the render surface —
opening the head and resolving the render over one
`(emit, config, anchor, notice_sink)`, an accepted double run of the eager
pass. The driver owns sink selection and framing, pacing composition, the
declared-but-empty-topic guarantee (empty files, pre-created empty Kafka
topics, zero counts), and `StreamOutcome`. The driver adds no bytes: every
message's body, key, and record timestamp are the render's.

## Invariants

1. **Entry-point-invariant stream `seq`.** Every replayed event's `seq` is a
   pure function of `(tape, config)`; bounded, unbounded, and seek heads
   agree on every event they share. An `r` event sits outside the 1-based
   total-order numbering: its `seq` is the snapshot position `N`, a function
   of `(tape, config, T)`.
2. **Bounds select, never recompute.** Every bounded answer's events are
   byte-identical to their whole-tape selves.
3. **Seek-state equivalence.** The upsert-log fold of `seek(T)` + live phase
   equals the fold of a full play, every T (state-changes content) —
   conditional on temporal/interval integrity exactly as the seam's
   consistency algebra: on declared family-C/E breakage the two disagree
   where the defect manifest says the data is broken.
4. **Render purity and sink-independence.** `render_bytes` is a pure
   function of `(event, resolved render)` (`render_key_bytes` and
   `timestamp_ms` likewise); one event yields one body byte sequence and one
   record timestamp regardless of sink.
5. **The driver adds no bytes.** The verb's per-message bytes, key bytes,
   and record timestamps are exactly the render's; the driver contributes
   sink selection, framing, pacing, and the empty-topic guarantee only.
6. **Inherited.** Pull-only (with the one declared divergence of § Open-time
   behavior and errors — open runs the eager pass's selection-resolution
   spine read; nothing else computes until pulled), deterministic (corrupted
   tapes included),
   permissive totality, one event-time line, version-gated input,
   sidecar-driven schema discovery, single-branch guard, no producer
   dependency. Layer direction as § Boundary.

## Validation Rules

No config models — bounds, seek position, and format are plain typed
arguments. All checks are open-, resolve-, or call-time business rules:

| Rule | Checks | Error |
|---|---|---|
| Open gates | The streaming eager pass: resolvability, vocabulary, naming, selection, change scope, elections; single-branch | The pass's `ExportError` / election identities (plus the reader-domain `TemporalClassUnavailableError`), at `open_stream_playback` |
| Bound validity | `start ≤ end`, both non-negative when given; `seek` position non-negative | `PlaybackError` (seam contract) |
| Render self-vetting | `resolve_stream_render` runs the eager pass verbatim (spine read included), head or no head | The same identities, at `resolve_stream_render` |
| Debezium anchor | `fmt='debezium'` requires a resolved anchor | The format's `ExportError` identity, at `resolve_stream_render` |
| Debezium block | `fmt='debezium'` requires the `debezium` block (the source identity) | The format's `ExportError` identity, at `resolve_stream_render` |
| Timestamp anchor | `timestamp_ms` requires a render resolved with an anchor | `ExportError` — the render surface's own anchor-requirement rule, at the call |

Enforced in [`playback/stream.py`](../../src/fabulexa_forge/playback/stream.py)
and [`playback/stream_render.py`](../../src/fabulexa_forge/playback/stream_render.py).

## Rationale

- **A declared surface, not exporter internals.** The streaming engine's
  event machinery is pure, pull-based, pacing-free, and sink-free, but it is
  mode internals; a caller honoring the playback-API-only dependency rule
  (topics, elected message keys, per-stream projections, rendered envelopes)
  needs a seam entry in stream shape — the alternative is importing
  `exporters.streaming.engine` directly, the squat-on-internals pattern the
  mixer established and the seam exists to end.
- **Seek composes what the primitive tier already proved.** The seam's
  `seek(T)` = `snapshot(T)` + `events(T + 1, ∞)` identity is exactly
  Debezium snapshot-then-stream semantics; the stream head emits that
  composition as `StreamEvent`s rather than inventing a second mid-tape-join
  mechanism.
- **The fusion, and no standalone snapshot.** One composed `seek` keeps the
  head's answer set minimal; free composition already exists in atom shape
  at the primitive tier. A stream-shaped standalone snapshot, a bounded
  seek, and a run-level value-schema enumeration accessor (for
  registry-registering adapters) each wait on a demonstrated consumer need
  (vault notes
  `stream-playback-seek-fuses-the-snapshot-phase-to-an-unbounded-live-tail`,
  `stream-render-value-schema-enumeration-waits-on-a-demonstrated-consumer`).
- **Compaction semantics for the `r` set.** A mid-tape joiner models a
  consumer attaching to a log-compacted topic: dropped keys are invisible,
  so a record whose `d` passed is absent rather than replayed-then-retired.
- **State, not change, in the `r` phase.** `only` / `ignore` scope which
  *changes* publish; a snapshot publishes *state*. Letting change scope
  thin the `r` image would make the snapshot disagree with the record's
  actual published state at T.
- **A render beside the head, not inside it.** Byte production is per-event
  and format-scoped; welding it to the head would force every head consumer
  through a format. Resolving it separately — self-vetting, so it stands
  alone — lets a delivery adapter render without replaying and lets the head
  serve consumers that never render.
- **The `(topic, table-identity)` schema key.** Overlapping streams over one
  kind with distinct `properties` are legal, so under `source_table`
  identity one `route_table` leaf legitimately carries a differently-fielded
  schema per covering stream — the leaf alone under-identifies, and keying
  per event keeps schema ↔ row agreement unconditional (permissive totality
  covers even a corrupted leaf).
- **A render-scoped anchor rule.** The sink-scoped (Kafka) and format-scoped
  (Debezium) anchor requirements cannot cover a sink-free anchorless render
  asked for an epoch timestamp; without its own rule the surface would have
  to invent an epoch frame.
- **One run, one anchor is a caller contract.** Comparing anchors at the
  seam would require an anchor identity the surface otherwise never needs;
  the verb threads one anchor, and a composing caller does the same.

## Boundaries

- **No author-facing bounds.** Bounds and seek are library-call arguments,
  not YAML: `StreamConfig` carries no horizon field, and the `stream` verb
  is whole-tape with no `--from` / `--to` (time bounds arrive via the
  playback API — see [`incremental.md`](incremental.md) for the windowed
  batch machinery, which is a different surface).
- **The `r` phase is not separately addressable.** No standalone
  stream-shaped snapshot, no bounded seek — § Rationale.
- **No run-level value-schema enumeration.** Schemas resolve per event;
  a declared-domain pre-enumeration waits on a demonstrated consumer.
- **Delivery is above the surface.** Pacing, sinks, framing, and the Kafka
  topic lifecycle are the driver's; the mixer is a sibling consumer of the
  engine, not of this surface.

## Related

| Document | Why |
|---|---|
| [`playback.md`](playback.md) | The seam this head belongs to — the tiers, the inclusive-T event-time line, the consistency algebra, `PlaybackError`, and the layer-direction rule whose one sanctioned mode-side consumer this doc's Boundary names |
| [`streaming.md`](streaming.md) | The stream shape being replayed — declared streams, folds and after-images, canonical order and `seq`, message-key election, the JSONL and Debezium formats, the delivery driver |
| [`streaming-pacing.md`](streaming-pacing.md) | The timing overlay the driver composes over the head's events |
| [`derivations.md`](derivations.md) | The row-state-events and membership-events folds the engine materializes; the state fold the `r` after-image reconstruction invokes |
| [`notices.md`](notices.md) | The required-sink posture both `open_stream_playback` and `resolve_stream_render` follow |
| [`anchor.md`](anchor.md) | The `EffectiveAnchor` the head's `ts` and the render's epoch timestamps derive from |
| [`corrupters.md`](corrupters.md) | The defect manifest that conditions the seek-state equivalence on family-C/E breakage |
