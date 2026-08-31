# Sprint: stream-playback

## Purpose

Give the playback seam its third head — stream-shaped playback bound to a
`StreamConfig` — plus the pure per-event render surface, and re-seam the
`stream` verb over both. A downstream caller (the loom extraction's player)
obtains `StreamEvent`s, mid-tape joins (`seek`), and message bytes through the
declared seam API instead of importing exporter internals.

Design source: [`docs/architecture/pending/stream-playback.md`](../../architecture/pending/stream-playback.md)
(the WHY and the seam-level contracts). This spec carries the WHAT: the
engine/driver contracts the doc leaves open, phases, and test cases.

## Scope

**Capabilities touched:**
- playback seam: `open_stream_playback` / `StreamPlayback`
  (`topics` / `events` / `seek`), `resolve_stream_render` / `StreamRender` —
  the seam's first byte-producing contract
- streaming exporter: eager-pass promotion (`resolve_streams`), bounded
  resolved iteration, the `r` snapshot-read op end to end (engine + both
  formats), the `(topic, table-identity)` schema key, the `stream_export`
  re-seam

**Not included:** standalone stream-shaped snapshot or bounded seek;
run-level value-schema enumeration; CLI flags (`--from`/`--to`); any
`StreamConfig` field; pacing/mixer/sink-lifecycle changes; the loom
extraction itself.

## Breaking Changes

Internal (greenfield — callers updated, no shims):

- `_validate_streams` is promoted to public `resolve_streams` returning a new
  frozen `StreamResolution` dataclass (was a private 5-tuple). One internal
  call site (`engine.py`).
- `iter_stream_events` **keeps its shipped 4-arg whole-tape signature**
  (deliberate deviation from widening it: ~120 call sites across 9 test files
  plus the mixer stay untouched, and the design doc's "the mixer keeps
  consuming the engine" holds). Internally it delegates to `resolve_streams`
  + `iter_resolved_stream_events(..., None, None)`. Bounds live on the
  resolved iterator only.
- `write_kafka_stream` takes three per-event render callables (value bytes,
  key bytes, epoch-ms timestamp) instead of computing them; its `anchor`
  parameter is deleted. The format-specific line writers collapse to one
  callable-driven `write_line_stream`. Driver's `_build_value_schemas*`
  family and `build_kafka_render_value` are deleted (the render surface owns
  schema state).
- `StreamEvent.op` Literal widens with `"r"` (additive; no existing op moves).

Observable:

- **Two declared schema-identity fixes** (pending doc § The render surface):
  (1) overlapping streams sharing a `route_table` leaf under `source_table`
  identity each embed their own stream's schema (was first-declared-wins);
  (2) a corrupted out-of-domain leaf gets a per-event-built embedded schema
  on every sink (was: kafka omitted it, line sinks failed the run). Bytes
  move only where shipped output was defective.
- The re-seamed verb runs the eager pass twice (head + render, one sink to
  both), so its pass notices emit twice — the doc's declared cost.

## Success Criteria

- [ ] `open_stream_playback` / `StreamPlayback` and `resolve_stream_render` /
  `StreamRender` ship exactly per the pending doc's Interface Contracts
- [ ] `events(None, None)` byte-identical to the shipped whole-tape stream;
  bounded asks select (never recompute) with entry-point-invariant `seq`
- [ ] `seek(T)` = `r` phase + `events(T + 1, None)`; the seek-state
  equivalence test is green over multiple T
- [ ] `render_bytes` equals shipped per-message bytes for every existing op
  outside the two declared fixes; both formats render `r`
- [ ] Re-seamed `stream_export` output identical to shipped for existing
  invocations outside the two fixes
- [ ] `make check` green

## Contracts

The four seam surfaces — `open_stream_playback`, `StreamPlayback`,
`resolve_stream_render`, `StreamRender`, and the `StreamEvent.op` widening —
are specified in the pending doc § Interface Contracts and are implemented
**verbatim**; they are not restated here. Below are the engine/driver
contracts the doc leaves open.

### `exporters/streaming/engine.py`

```python
@dataclass(frozen=True)
class StreamResolution:
    """The eager business-rule pass's resolved outputs for one (emit, config).

    Produced by resolve_streams; consumed by the engine's resolved iterators
    and by the playback seam's head and render. A pure function of
    (emit, config) — two resolutions of one pair are equal.
    """

    fork_path: str
    """The resolved single branch's fork_path."""
    election: "Election"
    """The resolved message-key election."""
    identity_by_stream: "Mapping[str, IdentityProjection]"
    """Every stream's gated identity projection, by stream name."""
    kind_vocabulary: "Mapping[str, str]"
    """The resolved config-level kind -> label map."""
    selection_by_stream: "Mapping[str, frozenset[str] | None]"
    """Every stream's resolved selection set (None = no narrowing device)."""
```

*(Field list mirrors the shipped `_validate_streams` tuple — the implementer
carries the actual shipped types/shapes over; the pass's semantics do not
change.)*

```python
def resolve_streams(
    emit: "Emit",
    config: "StreamConfig",
    notice_sink: "NoticeSink",
) -> StreamResolution:
    """Run the eager business-rule pass over every declared stream.

    The shipped _validate_streams pass verbatim — single-branch guard,
    per-stream gates, selection resolution (its spine read and
    out-of-domain notices included), election, surfaces, identity, naming,
    kind vocabulary — promoted so the seam's head and render invoke it
    without iterating.

    Args:
        emit: The open emit (reader + connection).
        config: The validated streaming configuration.
        notice_sink: Receiver for the pass's out-of-domain `where` notices.

    Returns:
        The StreamResolution the pass resolves.

    Raises:
        ExportError: Any business rule fails — the shipped identities,
            unchanged (election subclasses, Stream* gates, single-branch
            guard).
        TemporalClassUnavailableError: Propagated from the slice_only check.
    """
```

```python
def iter_resolved_stream_events(
    emit: "Emit",
    config: "StreamConfig",
    anchor: "EffectiveAnchor | None",
    resolution: StreamResolution,
    start: int | None,
    end: int | None,
) -> Iterator[StreamEvent]:
    """Yield bounded events under a pre-resolved eager pass.

    The post-pass half of iter_stream_events: materializes the per-stream
    folds, applies sub_types/selection drops, merges, stamps seq, renders
    ts. Pure row selection over the merged in-scope set: every surviving
    event is byte-identical (seq included) to its (None, None) self; the
    first event of a bounded ask carries seq = 1 + N, N = the internal
    deterministic count of in-scope events strictly before start. Bounds
    are total: any int pair is a legal selection (start > end selects
    nothing); the seam's PlaybackError bound check is the head's, not the
    engine's. Emits no notices and re-runs no gates; `resolution` must be
    resolve_streams(emit, config, ...) for this same pair — threading a
    foreign resolution is a caller error the engine does not detect. Lazy:
    nothing computes until pulled.

    Args:
        emit: The open emit.
        config: The validated streaming configuration.
        anchor: The resolved effective anchor, or None (raw-ns ts).
        resolution: The pair's own eager-pass result.
        start: Inclusive lower bound (ns), or None for tape start.
        end: Exclusive upper bound (ns), or None for tape end.

    Returns:
        An iterator of StreamEvent in canonical order.
    """
```

`iter_stream_events(emit, config, anchor, notice_sink)` keeps its shipped
signature and contract (the whole in-scope tape); its body becomes
`resolve_streams` + `iter_resolved_stream_events(..., None, None)`.

```python
def iter_resolved_snapshot_events(
    emit: "Emit",
    config: "StreamConfig",
    anchor: "EffectiveAnchor | None",
    resolution: StreamResolution,
    at_sim_time: int,
) -> Iterator[StreamEvent]:
    """Yield the seek snapshot phase: one 'r' event per record live at T
    per covering stream, ordered (stream_name ASC, record_id ASC).

    state-changes content: a record is live iff created_sim_time <= T and
    not deactivated at any instant <= T (compaction semantics). Each 'r'
    carries op='r', seq = N (the internal count of in-scope events with
    event_sim_time <= T; 0 when the phase precedes every event; shared by
    the whole phase), event_sim_time = T, ts = T rendered under `anchor`,
    and after = the record's full published image at T — the state fold
    over the kind's full tracked + constant set, projected through the
    stream's identity projection, properties, renames, vocabulary, and
    codec exactly as a 'c'/'u' image. Change scope does not narrow the 'r'
    set or image. membership-events content: yields nothing (an
    append-only fact log has no per-key state), so the head's seek is
    content-uniform: chain(this, events(T + 1, None)). Total over any int
    T (T < 0 selects nothing); the PlaybackError check is the head's.
    Lazy; no notices.

    Args:
        emit: The open emit.
        config: The validated streaming configuration.
        anchor: The resolved effective anchor, or None.
        resolution: The pair's own eager-pass result (resolve_streams).
        at_sim_time: The snapshot position T (ns), inclusive.

    Returns:
        An iterator of 'r' StreamEvents, possibly empty.
    """
```

Note: `N` for seek is well-defined without a shared handle — over integer ns,
`count(< T + 1) ≡ count(≤ T)`, so the snapshot phase's internally-computed
`N` and `events(T + 1, None)`'s prefix count agree by determinism.

### Seam-side placement

- `playback/stream.py` — `open_stream_playback`, `StreamPlayback`, the seam
  bound checks raising `PlaybackError` (from `playback.errors`).
- `playback/stream_render.py` — `resolve_stream_render`, `StreamRender`;
  builds the `(topic, table-identity value)` schema map at resolve time from
  `debezium.py`'s pure builders.
- Both are tier-2 siblings of `shaped.py`: they import `config` and
  streaming's pure surfaces only — `engine` (`resolve_streams`,
  `iter_resolved_stream_events`, `iter_resolved_snapshot_events`,
  `build_topic_set`), `jsonl`, `debezium`, `encoding` — never `driver`,
  `kafka_sink`, `pacer`.
- `playback/__init__.py` adds `StreamPlayback`, `StreamRender`,
  `open_stream_playback`, `resolve_stream_render` to `__all__`.
- Import direction: the driver imports playback (the one sanctioned
  mode-side consumer); the mixer keeps importing the engine, unchanged.

### `exporters/streaming/kafka_sink.py`

```python
def write_kafka_stream(
    events: Iterable[StreamEvent],
    render_value: Callable[[StreamEvent], bytes],
    render_key: Callable[[StreamEvent], bytes],
    render_timestamp: Callable[[StreamEvent], int],
    bootstrap_servers: str,
    topic_set: tuple[str, ...],
    paced: bool,
) -> StreamOutcome:
    """Produce the event stream to Kafka, one message per event.

    Fully format- and time-agnostic: value, key, and record-timestamp
    bytes come from the three callables (the driver passes StreamRender's
    render_bytes / render_key_bytes / timestamp_ms). Topic pre-creation,
    producer config, poll/flush discipline, and zero-count seeding are the
    shipped contract, unchanged.

    Args:
        events: The merged event stream, pacer-wrapped when realtime.
        render_value: Per-event message-body bytes (unframed).
        render_key: Per-event key bytes.
        render_timestamp: Per-event epoch-ms record timestamp.
        bootstrap_servers: The resolved non-empty bootstrap-servers string.
        topic_set: The full topic set; each created and zero-seeded.
        paced: True for incremental delivery under a realtime clock.

    Returns:
        The StreamOutcome (total and per-topic counts).

    Raises:
        KafkaClientUnavailable: confluent-kafka is not importable.
        KafkaDeliveryError: As shipped (connect/create/produce/flush/
            partition-count failures).
    """
```

### `exporters/streaming/driver.py`

`stream_export` keeps its public signature and Raises. Internally it opens
the head, resolves the render (its one `notice_sink` passed to both — the
accepted double-pass cost), consumes `head.events(None, None)`, paces, and
dispatches. The format-specific line writers collapse to one
`write_line_stream(events, render_value, sink, out, topic_set, paced) ->
StreamOutcome` — the shipped line-writer shape plus the callable; the line
sink appends the one `\n` to `render_value`'s unframed bytes. Sink
selection, framing, `out` checks, topic pre-creation, `StreamOutcome`, and
pacing composition stay in `driver.py`.

## Phases

### Phase 1: Engine resolution and bounded iteration

**Delivers:** `StreamResolution` + `resolve_streams` (promoted eager pass),
`iter_resolved_stream_events` with `(start, end)` bounds and
entry-point-invariant `seq`; `iter_stream_events` delegates, signature
unchanged.
**Demo:** Bounded iteration over a two-stream emit: the bounded window's
events are byte-identical to the whole-tape run's, first `seq` = 1 + N.
**Contracts:** `StreamResolution`, `resolve_streams`,
`iter_resolved_stream_events`.
**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/streaming/engine.py` |
| Modify | `tests/exporters/streaming/test_engine.py` |
| Create | `docs/sprints/stream-playback/demos/phase_1_bounded_events.py` |

**Tests** (additive in `test_engine.py`; no existing call sites migrate):
- `iter_resolved_stream_events(..., None, None)` equals
  `iter_stream_events(...)` event-for-event (all fields, `seq` included)
- Bounded ask `(T1, T2)`: every surviving event byte-identical to its
  whole-tape self; first event's `seq` = 1 + count of in-scope events
  strictly before T1
- `(T, T)` yields nothing; `start > end` yields nothing (engine-total, no
  raise); a bound past the last event exhausts without error
- Bounds land on both content types (state-changes and membership-events);
  a declared stream with zero in-window events yields nothing while
  `build_topic_set` still lists it
- Membership byte-identical-multiplicity ties: bounded count N is by
  multiplicity (the shipped `seq` argument holds under bounds)
- `resolve_streams` raises the shipped gate identities unchanged; a green
  config resolves to a `StreamResolution` whose parts drive the resolved
  iterator to the same output as the self-validating entry point
- Existing `test_engine.py` tests pass unchanged

### Phase 2: Snapshot phase and the stream head

**Delivers:** `StreamEvent.op` admits `"r"`; `iter_resolved_snapshot_events`;
`playback/stream.py` (`open_stream_playback`, `StreamPlayback` with
`topics` / `events` / `seek`); seam exports.
**Demo:** Open a head, `seek(T)`: print the `r` phase then the live tail;
fold both seek-and-live and full-play as an upsert log and show equal state.
**Contracts:** `iter_resolved_snapshot_events` (above);
`open_stream_playback` / `StreamPlayback` per the pending doc.
**Steps:** `source → author (2 files)`.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/streaming/types.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/engine.py` |
| Modify | `src/fabulexa_forge/playback/__init__.py` |
| Create | `src/fabulexa_forge/playback/stream.py` |
| Create | `tests/playback/test_stream_head.py` |
| Create | `tests/playback/test_stream_seek.py` |
| Create | `docs/sprints/stream-playback/demos/phase_2_seek_head.py` |

**Tests:**

`test_stream_head.py` (open, topics, events, errors):
- `topics()` returns the declared stream names in declaration order,
  data-independent (a declared-but-empty stream listed)
- `events(None, None)` through the head equals the engine's whole tape;
  a bounded head ask equals the engine's bounded ask
- `events` raises `PlaybackError` on `start > end` and on a negative bound;
  `seek` raises `PlaybackError` on a negative position — data conditions
  (bounds past the tape) do not raise
- Open runs the eager pass: a failing gate raises its shipped `ExportError` /
  election identity at `open_stream_playback`, before any pull; open emits
  the pass's notices to the supplied sink; nothing else computes until an
  iterator is pulled (laziness observable via the emit's query counter or a
  stub)
- Two heads over one emit are independently pullable

`test_stream_seek.py` (the `r` phase + equivalence):
- Record created at exactly T with no `d` ≤ T: in the `r` phase; its `c` is
  not replayed
- Record with `c` and `d` both ≤ T: absent entirely (compaction)
- Record created after T: arrives via `c` in the live phase only
- `u` at exactly T: folded into the `r` after-image, not replayed;
  coincident `u` and `d` at T: record absent from the phase
- No record live at T: empty phase, then the live stream
- Rows outside `sub_types` / `where` scope: absent from the phase
- `r` fields: `op='r'`, shared `seq = N` (and `= 0` when T precedes every
  event), `event_sim_time = T`, `ts` rendered under the anchor (raw int
  without one), after-image equal to a same-instant `c`/`u` image
  (identity projection, properties, renames, vocabulary, codec)
- Change scope does not narrow the `r` set or image: a record whose every
  post-creation change is `ignore`d still snapshots with its state at T
- Phase order `(stream_name ASC, record_id ASC)`; overlapping streams
  snapshot a shared record once per covering stream
- membership-events content: `seek(T)` = `events(T + 1, None)`, empty phase
- **Seek-state equivalence:** for several T (before, between, at, after
  events), the upsert-log fold (insert on `c`/`r`, upsert on `u`, retire on
  `d`, keyed by elected key) of `seek(T)` + live equals the fold of a full
  play; byte equality holds from `T + 1` onward
- Existing `tests/playback/` and `tests/exporters/streaming/` suites pass

### Phase 3: Render surface and `r` rendering

**Delivers:** both formats render `r` (jsonl object; Debezium
`snapshot: "true"` envelope with shared-`lsn` semantics);
`playback/stream_render.py` (`resolve_stream_render`, `StreamRender`) with
the `(topic, table-identity)` schema map, per-event `value_schema_for`, the
render-scoped `timestamp_ms` anchor rule, and the debezium resolve-time
gates.
**Demo:** Resolve renders for both formats over one emit; print
`render_bytes` / `render_key_bytes` / `timestamp_ms` for a `c`, `u`, and
seeked `r` event; show two overlapping streams sharing a leaf each embedding
their own schema (fix 1).
**Contracts:** `resolve_stream_render` / `StreamRender` per the pending doc;
seam placement above.
**Steps:** `source → author (3 files)`.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/streaming/jsonl.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/debezium.py` |
| Modify | `src/fabulexa_forge/playback/__init__.py` |
| Create | `src/fabulexa_forge/playback/stream_render.py` |
| Create | `tests/playback/test_stream_render.py` |
| Modify | `tests/exporters/streaming/test_jsonl.py` |
| Modify | `tests/exporters/streaming/test_debezium.py` |
| Create | `docs/sprints/stream-playback/demos/phase_3_render_surface.py` |

**Tests:**

`test_jsonl.py` / `test_debezium.py` (additive `r` cases):
- jsonl `r`: `{seq, op: "r", ts, kind, key, after}` with the full after-image
- Debezium `r`: `op: "r"`, `before: null`, full `after`,
  `source.snapshot: "true"`, `source.lsn` = the shared `N`,
  `sequence` = `"[null,\"<N>\"]"`; every other op still renders
  `snapshot: "false"` byte-identically

`test_stream_render.py`:
- **Byte parity:** for every existing op on both formats, `render_bytes`
  equals the shipped per-line bytes minus the trailing newline (oracle: the
  still-shipped phase-3 driver/format path over the same emit + config)
- `render_key_bytes` equals the shipped Kafka key bytes
  (`encode_pinned({key_column: key_value})`); `timestamp_ms` equals the
  shipped `rebased_epoch_ms` stamp
- `timestamp_ms` on an anchorless render raises `ExportError` (the
  render-scoped anchor rule); jsonl resolves with or without an anchor
- `value_schema_for`: under `table_identity: topic` the `(name, name)`
  degenerate pair; under `source_table` the `route_table` leaf; overlapping
  streams sharing a leaf get per-stream schemas (fix 1); a corrupted
  out-of-domain leaf gets a per-event-built schema, `route_table` verbatim
  (fix 2); `None` for jsonl and for schemas-disabled
- Resolve-time gates under shipped identities: `debezium` with no anchor
  refused; `debezium` with no `debezium` block refused; the eager pass's
  gate identities raise at `resolve_stream_render`; the pass's notices emit
  to the supplied sink (self-vetting, no head open)
- Render purity: two renders of one event are equal; renders resolved twice
  agree

### Phase 4: The verb re-seam

**Delivers:** `stream_export` consumes `head.events(None, None)` + the
render surface; `write_line_stream` collapse; callable-driven
`write_kafka_stream`; `_build_value_schemas*` / `build_kafka_render_value`
deleted. Output identical to shipped outside the two declared fixes.
**Demo:** Run the re-seamed verb (jsonl + debezium, file sink) over a
fixture emit; independently compose head + render by hand and show
per-topic byte-identical files; show fix 1's corrected schemas.
**Contracts:** `write_kafka_stream`, `write_line_stream`, `stream_export`
internals (above).
**Steps:** `source → author (3 files)`.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/streaming/driver.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/kafka_sink.py` |
| Modify | `tests/exporters/streaming/test_driver.py` |
| Modify | `tests/exporters/streaming/test_kafka_sink.py` |
| Modify | `tests/exporters/streaming/test_notice_sink.py` |
| Create | `docs/sprints/stream-playback/demos/phase_4_verb_reseam.py` |

**Tests:**
- Existing `test_driver.py` output expectations pass **unchanged** except
  where the two fixes bite: overlapping-streams-shared-leaf runs now embed
  per-stream schemas; out-of-domain-leaf runs no longer fail on line sinks
  and embed the per-event schema on every sink — those assertions flip to
  the corrected behavior
- `test_kafka_sink.py` migrates to the callable signature; key bytes and
  record timestamps are asserted equal to the render's outputs (the shipped
  values)
- `test_notice_sink.py`: the verb now emits the eager pass's notices twice
  (head + render, one sink) — assertions updated to the declared cost
- Declared-but-empty topics: empty files / zero counts / pre-created topics
  unchanged; `StreamOutcome` counts unchanged; pacing composition unchanged
  (paced path still wraps the head's iterator)
- Full-suite green (`make test`)

## What Doesn't Change

- `StreamConfig` and every config model — no new fields, no validator
  changes (`init` proposal engines untouched)
- The CLI: `fabulexa-forge stream` flags and whole-tape behavior
- `iter_stream_events`'s signature and self-validating contract — the mixer
  (`mixer/scheduler.py`) and `streaming/__init__.py` are untouched
- The pacer (`pace_events` still wraps an `Iterator[StreamEvent]`) and both
  mixer surfaces
- Canonical total order, cross-stream merge, `seq` definition, message-key
  election, identity projection, kind vocabulary, `rename`, row selection,
  change scope — entry points are added to them, not semantics
- Byte forms outside the two declared fixes and the new `r` op: JSONL object
  shape, Debezium envelope/value schema, pinned encoder settings
- Tier 1 (`head.py`, `snapshot.py`, `events.py`, `selection.py`) and tier 2
  (`shaped.py`) of the playback seam
- The membership-events content model (append-only fact log, no tombstones)
- Reader, derivations, conformance, corrupters, the other export modes

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/exporters/streaming/engine.py` | `resolve_streams` + `StreamResolution` promotion; `iter_resolved_stream_events` (bounds, seq offset); `iter_resolved_snapshot_events` (`r` phase); `iter_stream_events` delegates |
| `src/fabulexa_forge/exporters/streaming/types.py` | `StreamEvent.op` admits `"r"` |
| `src/fabulexa_forge/exporters/streaming/jsonl.py` | Render the `r` object |
| `src/fabulexa_forge/exporters/streaming/debezium.py` | Render the `r` envelope (`snapshot: "true"`, shared `lsn`) |
| `src/fabulexa_forge/exporters/streaming/driver.py` | Re-seam over head + render; `write_line_stream` collapse; schema-map family deleted |
| `src/fabulexa_forge/exporters/streaming/kafka_sink.py` | Callable-driven `write_kafka_stream`; own key/timestamp computation deleted |
| `src/fabulexa_forge/playback/stream.py` | New — `open_stream_playback`, `StreamPlayback` |
| `src/fabulexa_forge/playback/stream_render.py` | New — `resolve_stream_render`, `StreamRender` |
| `src/fabulexa_forge/playback/__init__.py` | Export the four new names |
| `tests/exporters/streaming/test_engine.py` | Additive bounds/resolution tests |
| `tests/exporters/streaming/test_jsonl.py` | Additive `r`-rendering tests |
| `tests/exporters/streaming/test_debezium.py` | Additive `r`-envelope tests |
| `tests/exporters/streaming/test_driver.py` | Two-fix assertion updates |
| `tests/exporters/streaming/test_kafka_sink.py` | Callable-signature migration |
| `tests/exporters/streaming/test_notice_sink.py` | Double-pass notice expectations |
| `tests/playback/test_stream_head.py` | New — open/topics/events/errors |
| `tests/playback/test_stream_seek.py` | New — `r` phase + seek-state equivalence |
| `tests/playback/test_stream_render.py` | New — byte parity, schemas, gates |
| `docs/sprints/stream-playback/demos/phase_[1-4]_*.py` | New — per-phase demos |
