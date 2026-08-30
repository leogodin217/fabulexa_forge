---
status: draft
---

# Stream Playback — the stream re-seam into the playback API

## Problem

The streaming exporter's event machinery is unreachable as a declared library
surface. `iter_stream_events` is already pure, pull-based, pacing-free and
sink-free — but it is exporter internals: the playback seam's shaped tier binds
`ExportConfig` only, so a `StreamConfig` has no seam entry and a caller honoring
the playback-API-only dependency rule cannot obtain a `StreamEvent`. The seam
owns no verb today.

Three concrete gaps, all blocking a downstream player's continuous release
policy (the loom extraction — finding
`loom-streaming-needs-the-stream-re-seam-into-the-playback-api`):

1. **No seam entry in stream shape.** A caller wanting topics, elected message
   keys, per-stream projections, and rendered envelopes must import
   `exporters.streaming.engine` — repeating the mixer's squat-on-internals.
2. **Whole-tape only.** A stream run replays start to end. Time bounds are
   inexpressible, and mid-tape join does not exist in stream shape — even
   though the seam's primitive tier already solves it structurally
   (`seek(T)` = `snapshot(T)` + `events(T + 1, ∞)` is exactly Debezium
   snapshot-then-stream semantics), no composition emits it as `StreamEvent`s.
3. **Format rendering is welded to the driver/sink path.** The pure pieces
   exist (`render_jsonl_object`, `render_debezium_message`,
   `build_debezium_value_schema`, `encode_pinned`) but the per-run wiring — the
   per-stream value-schema builds, the Debezium anchor and source-identity
   business rules, the `table_identity` resolution — lives in `stream_export`.
   A caller cannot turn a `StreamEvent` into message bytes without the driver.

## Solution

A third head on the playback seam — **shaped stream playback** — bound to a
validated `StreamConfig`, opened exactly like the shaped tier (open `Emit` +
config + resolved anchor + `NoticeSink`), running streaming's existing eager
business-rule pass at open, then answering:

- **`events(start, end)`** — lazy `StreamEvent`s in the canonical total order
  under the seam's half-open ns convention; `(None, None)` is today's whole
  tape; `seq` is entry-point-invariant.
- **`seek(T)`** — the snapshot-seeded mid-tape join: an initial-state phase of
  `r` (read) events for every record live at T, per covering stream, followed
  by `events(T + 1, None)` — Debezium snapshot-then-stream, in stream shape.
- **`topics()`** — the run's declared topic set, so a caller provisions sinks
  before the first ask (parity with the shaped tier's `tables()`).

Beside the head, the **per-event format render becomes a pure declared
surface**: a render resolved once per `(emit, config, fmt, anchor)` that maps
one `StreamEvent` to its message body bytes, its key bytes, and its epoch-ms
record timestamp, matching what the shipped sinks emit today — up to two
declared schema-identity fixes (§ The render surface).

The bar: `fabulexa-forge stream` re-seams to consume this surface with output
identical to the shipped verb for every existing
`(emit, config, anchor, fmt, sink)` outside those two fixes. The seam then
owns its first verb.

```
                    ┌─ playback seam ─────────────────────────────┐
StreamConfig ──▶    │  open_stream_playback ─▶ StreamPlayback     │
Emit + anchor ─▶    │    .topics()                                │
NoticeSink ────▶    │    .events(T1, T2)   ─▶ Iterator[StreamEvent]
                    │    .seek(T)          ─▶ Iterator[StreamEvent]
                    │  resolve_stream_render ─▶ StreamRender      │
                    │    .render_bytes(e) / .render_key_bytes(e)  │
                    │    .timestamp_ms(e) / .value_schema_for(e)  │
                    └─────────────────────────────────────────────┘
                          ▲ consumed by                ▲ consumed by
                    stream_export (the verb)     loom's adapters (later)
```

## Affected Subsystems

- **The playback seam** gains a third head: `StreamConfig`-bound stream
  playback, a sibling of the shaped tier at the same layer height (it imports
  the config envelope and the streaming exporter's pure compile/render
  surfaces — never the driver). The seam's shipped "no mode imports either
  tier" boundary is amended, deliberately: the re-seamed delivery driver
  (`stream_export`) becomes the one sanctioned mode-side consumer of the head
  and render surface; no mode's compile/render surface imports the seam. The
  seam's bound conventions (half-open ns,
  inclusive-T position), entry-point-invariant `seq` rule, pull-only posture,
  permissive totality, and error split (seam-contract violations raise
  `PlaybackError`; the mode's own gates pass through under their existing
  identities) all extend to it. The seam also gains the pure render surface —
  the first seam contract that produces bytes.
- **The streaming exporter** becomes the seam's first consumer. Its engine
  contract extends with time bounds (a pure row filter over the per-stream
  folds — selection, never recomputation) and the snapshot-phase `r` event
  set; `StreamEvent.op` admits `r`; both formats render `r`; the driver
  re-seams `stream_export` over the head + render surface with output
  identical outside the two declared schema-identity fixes (§ The render
  surface). Its parked "no time bounds / no windowed streaming" boundary retires
  in favor of "bounds and seek live at the seam; the CLI verb remains
  whole-tape."

Pacing, the sinks, the Kafka topic lifecycle, the mixer, and the CLI flag
surface are consumers or siblings that do not change (below).

## What Doesn't Change

- **No author-facing config change.** `StreamConfig` gains no fields. Bounds
  and seek are library-call arguments, not YAML. (The retired finding
  `streaming-has-no-t-0-to-t-x-horizon…` resolved exactly this way: time
  bounds arrive via the playback API, not the config.)
- **No CLI change.** `fabulexa-forge stream` keeps its flags and its
  whole-tape behavior; no `--from`/`--to` on the verb.
- **Byte forms.** The JSONL object shape, the Debezium envelope and value
  schema, and the pinned encoder settings are unchanged. Existing messages'
  bytes move only under the two declared schema-identity fixes (§ The render
  surface) — cases where the shipped output is defective; otherwise new bytes
  exist only for the new `r` op, which no existing run emits.
- **Canonical order, `seq` stamping, cross-stream merge, message-key
  election, identity projection, kind vocabulary, output naming/`rename`,
  row selection, change scope** — all inherited wholesale; this design adds
  entry points, not semantics, to them.
- **Pacing and the mixer.** `pace_events` still wraps an
  `Iterator[StreamEvent]`; the mixer keeps consuming the engine until it
  migrates out of the repo (the loom extraction, a separate effort).
- **The membership-events content model.** Append-only fact log, owner-keyed,
  no tombstones — unchanged; see § Seek for why it gets no snapshot phase.

## Semantics

### Bounds

`events(start, end)` follows the seam's one event-time line: half-open over
integer ns, `start ≤ event_sim_time < end`, either bound `None` for unbounded.
`events(None, None)` yields exactly the shipped whole-tape stream.

Bounding is **pure row selection over the merged in-scope event set** — the
seam's established restriction mechanism: every surviving event is
byte-identical (op, ts, after-image, key, topic, `route_table`, *and* `seq`)
to its whole-tape self. Bounds select events; they never recompute them.

| Condition | Result |
|---|---|
| `events(None, None)` | The whole tape — byte-identical to today's run |
| `events(T, T)` | Empty iterator |
| `start > end`, or a negative bound | `PlaybackError` (caller-contract violation, never a data condition) |
| Bound beyond the tape's last event | Exhaustion — total, no range check |
| Declared stream with zero in-window events | Yields nothing; `topics()` still lists it (declared intent drives topic existence, as today) |

### Entry-point-invariant `seq`

`seq` remains the event's 1-based position in the canonical total order over
the **whole in-scope stream** — a pure function of `(tape, config)`, never of
where the head entered. A head opened at a lower bound numbers its first event
`1 + N`, where `N` counts in-scope events strictly before the bound in
canonical order — a deterministic count, not a replay. Bounded and unbounded
heads agree; `seek(T)` then iterate matches a full play byte-for-byte from
`T + 1` onward.

The count is well-defined under the one order-totality caveat the stream
already carries: byte-identical membership events (contract-legal multiplicity
≥ 2) tie the canonical key, but they are counted by multiplicity and differ
only in `seq`, so the emitted byte stream is unaffected by which physical row
sorted first — exactly the shipped argument.

### Seek

`seek(T)` = an initial-state phase at T, then `events(T + 1, None)`. Position
T is inclusive: the phase represents "every event with `event_sim_time ≤ T`
applied," and the live phase resumes at the next instant, so no event is
duplicated or lost across the boundary.

**`state-changes` content — the `r` phase.** For each declared stream, one `r`
(read) event per record in the stream's scoped row set that is **live at T**:
`created_sim_time ≤ T` and not deactivated at any instant `≤ T`. This is
compacted-topic semantics: a record whose `d` already passed had its key
retired; replaying it would resurrect it, and a mid-tape joiner of a
log-compacted topic never sees dropped keys.

| `r` event field | Value |
|---|---|
| `op` | `r` |
| `seq` | `N` — the count of in-scope events with `event_sim_time ≤ T` (the stream position the snapshot represents; shared by every `r` event of the phase, `0` when the phase precedes every event; the live phase begins at `N + 1`) |
| `event_sim_time` | `T` |
| `ts` | The rendered instant of T under the resolved anchor (ISO-8601 with offset), or the raw int T with no anchor — the same rendering rule as every event |
| `after` | The record's full published after-image reconstructed at T: identity entries per the stream's identity projection, then the declared `properties` — same naming authority, renames, vocabulary, elections, codec (`str`-or-`null`) as a `c`/`u` after-image. Reconstruction invokes the state fold over the kind's full tracked + constant property set and projects afterwards (the seam's normative invocation rule) |
| `record_id` | The record's natural id, as on every event of that record |
| `key_column` / `key_value` | The elected surface, as on every op |
| `topic`, `kind`, `route_table` | As on every event of that record and stream |

Phase ordering is the canonical order restricted to one instant and one class:
`(stream_name ASC, record_id ASC)` — deterministic, merge-compatible, and per
covering stream (an overlapping-streams record snapshots once per covering
stream, the multiplicity rule).

Change scope does **not** govern the `r` set or image: `only`/`ignore` narrow
*change* (`u`) membership; the `r` phase publishes *state*, which is
projection-scoped only. A record whose every post-creation change was ignored
is still live and still snapshots with its state at T.

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
`events(T + 1, None)` with an empty initial phase. Rationale: the content is
an owner-keyed append-only *fact log* — there is no per-key upsert state for a
snapshot to seed, and a real connector snapshot over an outbox table replays
historical rows, which in this shape is just a bounded replay the caller can
already express as `events(None, T + 1)` followed by the live phase.
Containment *state* at T is a different answer shape, owned by the primitive
tier's membership snapshot. The two content types stay behaviorally distinct
at seek exactly as they are at delivery (upsert log vs fact log).

**The fusion is deliberate.** `seek` is the one composed answer — Debezium
snapshot-then-stream — and the `r` phase is not separately addressable, nor
is a bounded live tail (`seek` takes no end bound). A caller wanting free
composition has the primitive tier (`snapshot(T)` + `events`, in atom shape);
a stream-shaped standalone snapshot or bounded seek waits on a demonstrated
consumer need (tracked:
`stream-playback-seek-fuses-the-snapshot-phase-to-an-unbounded-live-tail`).

**The seek-state equivalence (the testable headline).** For any consumer
folding the `state-changes` stream as an upsert log keyed by the elected key
(insert on `c`/`r`, upsert on `u`, retire on `d`): the folded state after
`seek(T)` + the live phase equals the folded state after a full play, for
every T. Byte equality holds from `T + 1` onward; the prefix is
state-equivalent, not byte-equivalent, by design. The equivalence is
**conditional** exactly as the seam's consistency algebra is: on a tape whose
defect manifest declares family-C/E breakage there is no single consistent
world-state, so seek and replay disagree exactly where the manifest says the
data is broken — the manifest is the answer key, not a seam defect
(§ Open-time behavior and errors).

### The render surface

A render is resolved once per `(emit, config, fmt, anchor)` and is thereafter
a pure per-event function:

- `render_bytes(event)` — the message body: the UTF-8 encoding of the pinned
  compact JSON (`encode_pinned` settings) of the format's rendered object.
  **No framing**: line sinks append their one `\n`; the Kafka sink uses the
  bytes verbatim as the message value. Byte-identity contract: for every op
  the shipped formats emit today, `render_bytes` equals the shipped per-line
  bytes minus the newline, across all sinks — outside the two declared
  schema-identity fixes (below).
- `render_key_bytes(event)` — the UTF-8 pinned encoding of the one-entry
  elected key map `{key_column: key_value}` (the Kafka message key today).
- `timestamp_ms(event)` — the Kafka record timestamp: the rebased event
  instant in epoch milliseconds under the render's anchor, per the shipped
  integer-truncation rule — exposed so a producing adapter never re-derives
  the epoch frame from an internal function. Available only on a render
  resolved with an anchor: on an anchorless (`jsonl`) render the call raises
  `ExportError` under a render-scoped anchor-requirement rule this design
  introduces — the shipped anchor requirements are sink-scoped (the kafka
  sink's) and format-scoped (the debezium format's), and neither covers a
  sink-free anchorless render, so the render surface carries its own. An
  epoch instant without a declared calendar would be an invented value.
- `value_schema_for(event)` — the embedded Debezium value schema this
  event's rendered message embeds, resolved from the event's own
  **`(topic, table-identity value)`** pair: the identity value is the stream
  name under `table_identity: topic` (the pair degenerates to `(name, name)`)
  and the `route_table` leaf under `source_table`. The topic component is
  load-bearing: overlapping streams over one kind are legal with distinct
  `properties`, so under `source_table` identity one leaf carries a
  differently-fielded schema per covering stream — the leaf alone is not a
  schema identity, and "the topic's schema" does not exist. On a corrupted
  out-of-domain leaf the schema is built identically from the event itself
  (permissive totality — the message still embeds it, `route_table` the
  verbatim spine value). `None` when `fmt = jsonl` or schemas are disabled.
  Total over the head's events, so no unknown-key ask exists. There is
  deliberately no run-level enumeration accessor: the re-seamed verb
  resolves per event, and a declared-domain pre-enumeration (e.g. for a
  registry-registering adapter) waits on a demonstrated consumer need — the
  same bar the standalone snapshot waits on (tracked:
  `stream-render-value-schema-enumeration-waits-on-a-demonstrated-consumer`).

**Two declared schema-identity fixes (deliberate breaking changes).** The
`(topic, table-identity)` schema key and the per-event resolution correct
shipped behavior, and the re-seamed verb carries the corrections:

1. **Overlapping streams sharing a leaf.** Under `source_table` identity the
   shipped driver keys its per-run schema map by the leaf alone,
   first-declared-stream-wins — every covering stream's messages embed the
   first stream's schema, a schema ↔ row divergence on the others. Each
   message now embeds its own stream's schema.
2. **Corrupted out-of-domain leaf.** The shipped sinks disagree: the kafka
   sink embeds no schema, the line sinks fail the run — a permissive-totality
   violation. Every sink now embeds the per-event-built schema.

Both change bytes only where the shipped output is defective; no other
message's bytes move, and the verb's acceptance bar is output identity
everywhere outside these two cases.

Resolution is **self-vetting**: it runs streaming's eager business-rule pass
exactly as `open_stream_playback` does — the per-stream naming/schema state
it builds presupposes that pass's resolutions — raising the existing gate
identities unchanged and emitting the pass's notices through a required
caller-supplied `NoticeSink` (the notice-channel contract — a surface that
runs a notice-emitting pass takes a sink), so a render resolves with no head
open; a caller composing head and render passes one sink to both and pays
the eager pass twice (its notices, and its selection-resolution spine read,
twice with it).
On top of the pass, resolution enforces the format's business rules at
resolve time, under their existing error identities: `debezium` with no
resolved anchor is refused (the `ts_ms` epoch-milliseconds rule); `debezium`
with no `debezium` block is refused (no invented mapping values — the block
carries the source identity). `jsonl` resolves with or without an anchor.

**One run, one anchor.** The render's `anchor` is the same resolved anchor
the head was opened with — the verb threads one; a caller composing head and
render passes one. The seam does not compare them: a mismatched pair is a
caller error whose only symptom is incoherent timestamps (`ts` renders under
the head's anchor; `ts_ms` and `timestamp_ms` under the render's).

**Rendering `r`.** Both formats extend naturally, and only for the new op:

| Format | `r` rendering |
|---|---|
| `jsonl` | `{seq, op: "r", ts, kind, key, after}` — the standard object with the full after-image |
| `debezium` | Envelope `op: "r"`, `before: null`, `after`: the full after-image; `source.snapshot: "true"` (on every other op it remains `"false"` as shipped). `source.lsn` is the event's `seq` — the shared snapshot position `N` — and `sequence` is `"[null,\"<N>\"]"`, so every `r` of one phase repeats one `lsn`, deliberately: snapshot reads share one source position, the one place `lsn` is not unique per message. Every other `source` field derives as on any op. Canonical Debezium snapshot-read semantics. Declared deviation: no `"last"` marker on the final snapshot record — every `r` renders `snapshot: "true"` (one fewer stateful special case; the phase boundary is observable as the op change) |

### Open-time behavior and errors

Open runs streaming's existing eager pass — the per-stream resolvability,
vocabulary, naming, selection, change-scope, and election gates — before any
event materializes, raising the existing error identities unchanged
(`ExportError` and its election/streaming subclasses, plus the reader-domain
`TemporalClassUnavailableError` the pass's `slice_only` check propagates).
The single-branch guard applies. Seam
contract violations — negative bounds, `start > end`, negative `seek`
argument — raise `PlaybackError`. Opening replays nothing: no answer computes
until an iterator is pulled, and outstanding lazy answers are independently
pullable (two heads over one emit do not contend). Open is deliberately
**not** sidecar-only — a declared, scoped divergence from tier 1's
open-reads-the-sidecar-only rule: the shipped eager pass's selection
resolution reads the records spine (the data-backed case of the selection
out-of-domain notice), and the head runs that pass verbatim, notice timing
included — deferring the read would change the shipped pass's behavior, which
the output-identical verb bar forbids. The spine read is a bounded scope
check, never a replay.

Permissive totality is inherited: semantic defects flow through verbatim; a
corrupted tape plays identically to an intact one, and on a
temporally-corrupted tape (family C/E defects) seek and replay disagree
exactly where the defect manifest says the data is broken — the manifest is
the answer key, not a seam defect.

### The verb re-seam

`stream_export` re-seams to consume `events(None, None)` and the render
surface. Everything driver-owned stays driver-owned: sink selection and
framing, pacing composition, the declared-but-empty-topic guarantee (empty
files, pre-created empty Kafka topics, zero counts), `StreamOutcome`. The
re-seam is observable only as an internal layering change plus the two
declared schema-identity fixes (§ The render surface): **output identical to
the shipped verb for every existing invocation outside those two cases is
the acceptance bar.**

### Invariants

Introduced:

1. **Entry-point-invariant stream `seq`** — every replayed event's `seq` is
   a pure function of `(tape, config)`; bounded, unbounded, and seek heads
   agree on every event they share. An `r` event sits outside the 1-based
   total-order numbering: its `seq` is the snapshot position `N`, a function
   of `(tape, config, T)`.
2. **Bounds select, never recompute** — every bounded answer's events are
   byte-identical to their whole-tape selves.
3. **Seek-state equivalence** — the upsert-log fold of `seek(T)` + live phase
   equals the fold of a full play, every T (state-changes content) —
   conditional on temporal/interval integrity exactly as the seam's
   consistency algebra: on declared family-C/E breakage the two disagree
   where the defect manifest says the data is broken.
4. **Render purity and sink-independence** — `render_bytes` is a pure
   function of `(event, resolved render)` (`render_key_bytes` and
   `timestamp_ms` likewise); one event yields one body byte sequence and one
   record timestamp regardless of sink, and the shipped formats' bytes are
   unchanged for every existing op outside the two declared schema-identity
   fixes.
5. **Output-identical verb** — the re-seamed `stream_export` output is
   identical to the pre-design output for every
   `(emit, config, anchor, fmt, sink)`, up to the two declared
   schema-identity fixes (§ The render surface).

Inherited and extended to the new head: pull-only (amended as § Open-time
behavior and errors declares — open runs the eager pass's selection-resolution
spine read; nothing else computes until pulled), deterministic
(corrupted tapes included), permissive totality, one event-time line,
version-gated input, sidecar-driven schema discovery, single-branch guard, no
producer dependency. Layer direction is inherited **amended**: no mode's
compile/render surface imports the seam, and the re-seamed delivery driver
(`stream_export`) is the one sanctioned mode-side consumer of the head and
render surface. The graph stays acyclic at module granularity — the head and
render import the streaming exporter's pure compile/render surfaces, never
its driver; the driver imports the seam, never the reverse.

## Configuration

No new author-facing configuration. Bounds, seek position, and format are
arguments of the library surface; `StreamConfig` and the `stream` CLI verb are
unchanged.

## Interface Contracts

### Runtime Types

```python
@dataclass(frozen=True)
class StreamEvent:
    """(Existing type — one field's domain widens.)

    op: Literal["c", "d", "u", "join", "leave", "r"]
        'r' is the seek snapshot-read op: the record's published state at the
        seek position, emitted once per covering stream for each record live
        at T. All other fields keep their shipped contracts; on an 'r', seq
        is the shared snapshot position N and event_sim_time is T.
    """
```

### Functions

```python
def open_stream_playback(
    emit: "Emit",
    config: "StreamConfig",
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
) -> StreamPlayback:
    """Bind a stream head to an open emit and a declared stream configuration.

    Runs the streaming exporter's full eager business-rule pass at open,
    verbatim: per-stream resolvability, vocabulary, naming, selection,
    change scope, and the election gates — the pass's selection-resolution
    spine read included (open is not sidecar-only; § Open-time behavior and
    errors). Pull-only thereafter — no answer computes and no event
    materializes until an iterator is pulled.

    Args:
        emit: An open emit (version-gated by open_emit).
        config: The validated streaming configuration (either content type).
        anchor: The resolved effective anchor, or None (events then carry
            raw-ns ts values; a later debezium render resolution will refuse
            the missing anchor at its own gate).
        notice_sink: Receiver for the open pass's notices (required — the
            notice-channel contract; a caller wanting silence passes a
            discarding sink).

    Returns:
        A StreamPlayback head bound to (emit, config, anchor, notice_sink).

    Raises:
        ExportError: A streaming business rule failed (the existing gate
            identities, including the election subclasses), or the
            single-branch guard tripped — passed through unchanged.
        TemporalClassUnavailableError: Propagated from the eager pass's
            slice_only check (a reader-domain identity, passed through).
    """
```

```python
class StreamPlayback:
    """A stream-shaped playback head: bounded events, seek, and the topic set.

    Deterministic and pull-only; outstanding lazy answers are independently
    pullable. All positions and bounds are raw sim-time nanoseconds.
    """

    def topics(self) -> tuple[str, ...]:
        """The run's topic set: the declared stream names, declaration order.

        Returns:
            The declared topic names — declared intent, independent of data,
            so a caller provisions sinks before the first ask.
        """

    def events(
        self,
        start: int | None,
        end: int | None,
    ) -> Iterator[StreamEvent]:
        """Yield the in-scope events with start <= event_sim_time < end.

        Canonical total order, seq stamped entry-point-invariantly (the
        first event of a bounded ask carries 1 + N, N = in-scope events
        strictly before start). (None, None) is the whole tape,
        byte-identical to the shipped whole-tape run. Lazy: nothing computes
        until the iterator is pulled.

        Args:
            start: Inclusive lower bound (ns), or None for tape start.
            end: Exclusive upper bound (ns), or None for tape end.

        Returns:
            An iterator of StreamEvent in canonical order.

        Raises:
            PlaybackError: start > end, or a negative bound.
        """

    def seek(self, at_sim_time: int) -> Iterator[StreamEvent]:
        """Snapshot-then-stream from position T (inclusive).

        state-changes content: first the 'r' phase — one read event per
        record live at T per covering stream, ordered
        (stream_name, record_id), each carrying seq = N and the record's
        published state at T — then every event of events(T + 1, None).
        membership-events content: the initial phase is empty (an
        append-only fact log has no per-key state to seed); the answer is
        events(T + 1, None).

        Args:
            at_sim_time: The seek position T (ns), inclusive.

        Returns:
            An iterator of StreamEvent: the snapshot phase, then the live
            phase, matching a full play byte-for-byte from T + 1 onward.

        Raises:
            PlaybackError: at_sim_time is negative.
        """
```

```python
def resolve_stream_render(
    emit: "Emit",
    config: "StreamConfig",
    fmt: Literal["jsonl", "debezium"],
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
) -> StreamRender:
    """Resolve the pure per-event render for one (emit, config, fmt, anchor).

    Builds the per-stream naming/schema state once (the naming authority's
    output keys; for debezium, the (topic, table-identity)-keyed value
    schemas and the table_identity resolution) and enforces the format's
    business rules. Self-vetting: runs streaming's eager business-rule pass
    exactly as open_stream_playback does (the pass's selection-resolution
    spine read included), so a render resolves with no head open.
    `anchor` is the same resolved anchor the paired head was opened with
    (one run, one anchor — the caller's contract; the seam does not compare).

    Args:
        emit: The open emit (the eager pass's reads only; no replay).
        config: The validated streaming configuration.
        fmt: The output format.
        anchor: The resolved effective anchor, or None.
        notice_sink: Receiver for the eager pass's notices (required — the
            notice-channel contract; a caller composing head and render
            passes one sink to both).

    Returns:
        A StreamRender whose per-event methods are pure functions.

    Raises:
        ExportError: A streaming business rule failed (the eager pass's
            existing gate identities, including the election subclasses, and
            the single-branch guard); or fmt='debezium' with anchor=None
            (the epoch-milliseconds rule), or with no debezium block
            declared (no invented mapping values — the block carries the
            source identity) — the existing error identities, unchanged.
        TemporalClassUnavailableError: Propagated from the eager pass's
            slice_only check (a reader-domain identity, passed through).
    """
```

```python
class StreamRender:
    """The pure per-event format render: StreamEvent -> message body bytes,
    key bytes, and record timestamp.

    One event yields one body byte sequence regardless of sink; the bytes
    equal the shipped sinks' per-message bytes outside the two declared
    schema-identity fixes (line sinks add their one trailing newline; the
    Kafka sink uses them verbatim).
    """

    def render_bytes(self, event: StreamEvent) -> bytes:
        """The message body: UTF-8 pinned-encoder JSON of the format's
        rendered object ({seq, op, ts, kind, key, after} for jsonl; the
        Debezium value message, schema-wrapped when enabled, for debezium).

        Args:
            event: The event to render.

        Returns:
            The message body bytes, unframed.
        """

    def render_key_bytes(self, event: StreamEvent) -> bytes:
        """The message key: UTF-8 pinned-encoder JSON of the one-entry
        elected key map {key_column: key_value}.

        Args:
            event: The event to render.

        Returns:
            The key bytes, unframed.
        """

    def timestamp_ms(self, event: StreamEvent) -> int:
        """The Kafka record timestamp: the rebased event instant in epoch
        milliseconds under the render's anchor — the shipped
        integer-truncation rule (anchor start-instant epoch-ns plus
        event_sim_time, floor-divided to ms), byte-for-byte the timestamp
        the shipped Kafka sink stamps today.

        Args:
            event: The event to stamp.

        Returns:
            Epoch-milliseconds (UTC) of the rebased instant.

        Raises:
            ExportError: The render was resolved with anchor=None — the
                render surface's own anchor-requirement rule (jsonl is the
                only anchorless render; the shipped sink- and format-scoped
                identities do not cover a sink-free render).
        """

    def value_schema_for(self, event: StreamEvent) -> dict[str, object] | None:
        """The value schema this event's rendered message embeds, resolved
        from the event's own (topic, table-identity value) pair — the
        stream name under table_identity='topic', the route_table leaf
        under 'source_table'; the topic component disambiguates
        overlapping streams sharing a leaf with distinct properties. Built
        identically from the event itself on a corrupted out-of-domain
        leaf. None when fmt='jsonl' or schemas are disabled. Total over
        the head's events.

        Args:
            event: The event whose message's schema to return.

        Returns:
            The Connect value-schema descriptor, or None.
        """
```

## Validation Rules

### Parse-Time (Pydantic)

None — no config model changes.

### Business Rules

| Rule | Checks | Error |
|---|---|---|
| Open gates (existing) | The streaming eager pass: resolvability, vocabulary, naming, selection, change scope, elections; single-branch | Existing `ExportError` / election identities (plus the reader-domain `TemporalClassUnavailableError`), unchanged, at `open_stream_playback` |
| Bound validity | `start <= end`, both non-negative when given; `seek` position non-negative | `PlaybackError` (seam contract) |
| Render self-vetting | `resolve_stream_render` runs the streaming eager pass verbatim (its selection-resolution spine read included), head or no head | Existing `ExportError` / election identities (plus the reader-domain `TemporalClassUnavailableError`), unchanged, at `resolve_stream_render` |
| Debezium anchor | `fmt='debezium'` requires a resolved anchor | Existing identity, at `resolve_stream_render` |
| Debezium block | `fmt='debezium'` requires the `debezium` block (the source identity) | Existing identity, at `resolve_stream_render` |
| Timestamp anchor | `timestamp_ms` requires a render resolved with an anchor | `ExportError` — the render surface's own anchor-requirement rule (new; the shipped identities are sink-/format-scoped), at the call |
