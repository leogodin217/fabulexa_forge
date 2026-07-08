# Streaming Pacing

The pacing surface that sits between the streaming exporter's `seq`-stamped event
stream and its sinks, delivering a finished run in real time so it replays as a live
feed instead of a single burst. It is a pure timing overlay over the already-merged,
`seq`-stamped `Iterable[StreamEvent]`, keyed solely on `event_sim_time`: each event is
released at a wall-clock instant scheduled from its sim-time spacing. Pacing governs
*timing*, never bytes — a paced run produces the exact bytes of the equivalent unpaced
run, on either sink — and is content-, format-, and sink-agnostic. Delivery is unpaced
by default; pacing is opt-in through an optional `clock` block on `StreamConfig` and the
mirroring CLI overrides. The streaming exporter itself is documented in
[`streaming.md`](streaming.md); this doc owns the pacing contract it composes.

**Source:**
[`exporters/streaming/pacer.py`](../../src/fabulexa_export/exporters/streaming/pacer.py)
(`ResolvedClock`, `resolve_clock`, `pace_events`),
[`config/models.py`](../../src/fabulexa_export/config/models.py) (`ClockConfig`, the
optional `StreamConfig.clock` block), the clock wiring in
[`driver.py`](../../src/fabulexa_export/exporters/streaming/driver.py) (`stream_export`),
the per-line-flush paths in
[`jsonl.py`](../../src/fabulexa_export/exporters/streaming/jsonl.py) /
[`debezium.py`](../../src/fabulexa_export/exporters/streaming/debezium.py)
(`write_jsonl_stream` / `write_debezium_stream`, the `paced` parameter), and the clock
flags + flag-level usage checks in
[`cli.py`](../../src/fabulexa_export/cli.py) (`cmd_stream`).
Tests:
[`tests/exporters/streaming/test_pacer.py`](../../tests/exporters/streaming/test_pacer.py),
[`tests/config/test_clock_config.py`](../../tests/config/test_clock_config.py).
Recipe: [`examples/recipes/streaming/clock-realtime`](../../examples/recipes/streaming/clock-realtime/config.yaml).

## Boundary

- **Input.** The merged, `seq`-stamped `Iterable[StreamEvent]` in canonical order, plus
  a `ResolvedClock` (a positive `speed` and an optional `idle_cap_seconds`). Real time
  enters only through two injected callables — `sleep` and `monotonic` — so the
  scheduler is fully deterministic under fakes.
- **Output.** The same events, in the same order and with the same content, each yielded
  at its scheduled real-time instant. Pacing emits no bytes and no new events; it never
  reads or rewrites `seq`, `op`, `ts`, the message key, or the after-image.
- **Keys on `event_sim_time` only.** The pacer is anchor-independent — a paced run with
  no resolved anchor (raw-ns `ts`) is valid. It reads no sidecar, no config beyond the
  `ResolvedClock`, and no domain knowledge.
- **Runs after the merge.** Pacing is the last stage before the sink, downstream of the
  cross-kind k-way merge, `seq` stamping, and routing.
- **Forbidden imports.** The pacer imports only `errors` and the streaming `types`; the
  driver injects `time.sleep` / `time.monotonic` and selects paced delivery on the sink.
  No payload value derives from `now()`.

## Semantics

### Clock resolution (config × CLI)

`resolve_clock` combines the config `clock` block with the three CLI overrides
(`--speed`, `--idle-cap`, `--fast`), CLI winning per knob — mirroring
`resolve_effective_anchor`. The resolved value is a `ResolvedClock` (realtime) or `None`
(fast); a `None` clock means the driver delivers without the pacer. `--speed` or
`--idle-cap` escalate an absent-or-fast configuration to realtime; `--fast` forces
unpaced.

| `config.clock` | `--fast` | `--speed` | `--idle-cap` | Resolved |
|---|---|---|---|---|
| absent / `fast` | no | unset | unset | fast (`None`) |
| `realtime(s, c)` | no | unset | unset | `realtime(s, c)` |
| any | yes | unset | unset | fast (`None`) |
| absent / `fast` | no | `S` | unset | `realtime(S, uncapped)` |
| absent / `fast` | no | `S` | `C` | `realtime(S, C)` |
| absent / `fast` | no | unset | `C` | `ClockSpeedUnresolvable` |
| `realtime(s, c)` | no | `S'` | unset | `realtime(S', c)` — speed overridden, cap inherited |
| `realtime(s, c)` | no | unset | `C'` | `realtime(s, C')` — cap overridden, speed inherited |
| any | yes | set | — | usage error (exit 1, before the funnel) |
| any | yes | — | set | usage error (exit 1, before the funnel) |

The effective speed is `--speed` if set, else the config speed when the config is
realtime, else unresolved. The effective cap is `--idle-cap` if set, else the config cap
when the config is realtime, else uncapped. A run is realtime iff `--fast` is absent and
at least one of `--speed`, `--idle-cap`, or a realtime config is present; a realtime run
with no resolvable speed raises `ClockSpeedUnresolvable`.

### The pacing schedule

The pacer consumes events in arrival (`seq`) order and releases each at a real-time
instant scheduled against a fixed origin captured from `monotonic()` at the first event.
For event *i* with `event_sim_time` `t_i`:

> `release(i) = origin + Σ_{1<k≤i} min((t_k − t_{k−1}) / 1e9 / speed, idle_cap_seconds)`
>
> `sleep_before(i) = max(0, release(i) − monotonic())`

| Condition | Result |
|---|---|
| First event (no predecessor) | released immediately at the captured origin; no sleep |
| Consecutive events, sim gap `Δ` ns | real delay = `min(Δ / 1e9 / speed, idle_cap_seconds)` |
| `idle_cap_seconds` is `None` (uncapped) | real delay = `Δ / 1e9 / speed` (no ceiling) |
| Two consecutive events share an `event_sim_time` (cross-kind coincidence) | `Δ = 0` → zero delay; both released together |
| Consumer slower than the schedule | computed sleep is `< 0` → clamped to `0`; the pacer falls behind, never advances past the schedule |
| `speed = 1.0` | real time — inter-event real delay equals the inter-event `event_sim_time` gap (`Δ / 1e9` seconds), independent of whether an anchor resolved |

The schedule is **drift-free**: releases accrue against the single captured origin, so
per-event serialization and processing time do not accumulate into the timeline. The
schedule is **monotonic non-decreasing** in `seq`, because `speed` and the cap are
positive and `event_sim_time` is non-decreasing in `seq` order.

### Paced delivery

The driver wraps the event iterable through `pace_events` (injecting `time.sleep` /
`time.monotonic`) when the resolved clock is realtime, and passes `paced=True` to the
sink. Under pacing each rendered line is flushed as written: `stdout` flushes after each
line, and the `file` sink appends and flushes each line to its `<topic>.jsonl` as it
arrives rather than buffering a topic's lines until run end — so a file monitor (Kafka
Connect `FileStreamSource`, `tail -f`, an inotify watcher) observes events at the paced
cadence. Each topic's file handle is opened lazily on that topic's first event, kept
open across the run, and closed in a `finally` on completion or abort; per-line flush
already provides durability, so the close is cleanup, not a correctness dependency. A
zero-event topic opens no handle — its empty `<topic>.jsonl` is still materialized by the
declared-but-empty-topic backfill. The `kafka` sink serves delivery incrementally in the
same way — it `poll`s the producer after each event so messages are produced at the paced
cadence rather than buffered to run end, then `flush`es once on completion (see
[`streaming.md`](streaming.md) § The Kafka sink). Byte output — and, for kafka, the
produced message sequence — is identical to an unpaced run across all sinks and both
formats.

## Invariants

1. **Pacing is timing-only.** The emitted byte stream is a pure function of
   `(emit, StreamConfig, CLI overrides)` and is independent of wall-clock time and of the
   clock mode. The streaming determinism invariant (same emit + same config + same code →
   identical output) holds for paced runs as fully as for unpaced ones.
2. **Real time is confined to two injected callables.** `sleep` and `monotonic` are the
   only non-deterministic inputs and enter only through `pace_events` parameters, so the
   scheduling logic is unit-testable with fakes and no real sleeping; no payload value
   ever derives from `now()`.
3. **The schedule is drift-free and monotonic non-decreasing in `seq`.** Releases accrue
   against one captured origin (no per-event drift), and a positive `speed`/cap over a
   non-decreasing `event_sim_time` keeps release times non-decreasing.

These rely on guarantees the streaming exporter and the base layer already hold:
`event_sim_time` is non-decreasing in `seq` order (the canonical merge's primary key is
`sim_time ASC`), and `StreamEvent.event_sim_time` is the nanosecond event-time key
carried on every event independent of whether an anchor resolved.

## Validation Rules

**Parse-time** (Pydantic, on `ClockConfig` in
[`config/models.py`](../../src/fabulexa_export/config/models.py)): `speed` and
`idle_cap_seconds` carry `Field(gt=0)`, so any non-positive value is rejected at parse
time. The `mode_fields_consistent` after-validator enforces per-mode field presence:
`realtime` requires `speed` and allows an optional `idle_cap_seconds` (absent =
uncapped); `fast` forbids both `speed` and `idle_cap_seconds`. The exact field grammar is
the contract of the model.

**Business rules** combine config with CLI state and so run outside the Pydantic model:

| Rule | Checks | Site |
|---|---|---|
| `--fast` exclusivity | `--fast` is not combined with `--speed` or `--idle-cap` | `cmd_stream` flag-level check — exit 1 before the funnel |
| `--speed` / `--idle-cap` positivity | any supplied `--speed` / `--idle-cap` is `> 0` — the CLI counterpart of the config-path `Field(gt=0)`, so a CLI override cannot smuggle a non-positive value past Pydantic | `cmd_stream` flag-level check — exit 1 before the funnel |
| `ClockSpeedUnresolvable` | a realtime run has a resolvable speed (from `--speed` or a realtime config) | raised by `resolve_clock`; funneled to exit 1 by the `(ReaderError, ExporterError)` catch |

The exact messages are the contract of the raising sites; the tests in
[`test_pacer.py`](../../tests/exporters/streaming/test_pacer.py) and
[`test_clock_config.py`](../../tests/config/test_clock_config.py) pin them.

## Rationale

- **A `ResolvedClock | None`, not a fast-mode object.** A fast (unpaced) run is the
  *absence* of a clock, mirroring `EffectiveAnchor`'s `None`. Only realtime needs a
  resolved representation; modeling fast as `None` keeps the driver's branch a single
  null check and avoids a degenerate fast `ResolvedClock` carrying meaningless fields.
- **A pure decorator keyed on `event_sim_time`.** Keying on event time rather than the
  anchor keeps pacing content-, format-, and sink-agnostic — a future content type rides
  it unchanged — and makes paced raw-ns runs (no resolved anchor) valid. The decorator
  passes sequence and payload through verbatim, which is what makes the timing-only
  invariant hold by construction.
- **Real time in two injected callables.** Confining `sleep` / `monotonic` to
  `pace_events` parameters makes the schedule unit-testable with fakes and no real
  sleeping, and keeps determinism over bytes intact.
- **`absent idle_cap_seconds` = uncapped.** The cap is the one optional realtime scalar;
  its absence means no ceiling, so a quiet sim-time gap plays out in full. A sentinel
  default would invent a policy the author did not state (Principle #7).
- **`ClockSpeedUnresolvable` is a direct `ExporterError` child, not an `ExportError`.**
  Clock resolution runs no engine and reads no config, so it is placed exactly as
  `InitRequiresRecordRoles` — a sibling of `ConfigError` / `ExportError` /
  `ExportRuntimeError` / `RebaseError` / `IncrementalError`. A single clock-resolution
  error needs no `ClockError` base class (Principle #8); the existing
  `(ReaderError, ExporterError)` funnel reports it as exit 1, mirroring the `Rebase*`
  resolution errors.
- **CLI wins per knob.** The `--speed` / `--idle-cap` / `--fast` precedence mirrors the
  anchor's `rebase`-plus-flags model, so the streaming overrides share one precedence
  shape rather than inventing a second.

## Boundaries

What pacing deliberately does not own:

- **Event production and ordering.** Pacing is strictly post-merge; the per-kind fold,
  the cross-kind merge, `seq`, the message key, `ts`, and routing are fixed upstream and
  pass through verbatim.
- **Defending against a sim-time decrease.** The pacer relies on `event_sim_time` being
  non-decreasing in `seq` order; a negative gap is an upstream ordering-contract
  violation, not a paced-delivery concern, and the pacer does not guard against it.
- **`ts` rendering.** `ts` is a pure function of `event_sim_time` and the anchor (never
  `now()`); pacing governs when a line is delivered, never the `ts` it carries.
- **New content or format.** Pacing times the existing `state-changes` × {`jsonl`,
  `debezium`} surface across every sink ({`stdout`, `file`, `kafka`}); a new content type
  or format rides the same timing overlay unchanged.
- **Backpressure and catch-up.** A consumer slower than the schedule makes the pacer fall
  behind (the computed sleep clamps to zero); it never skips ahead, batches, or drops
  events to catch up.

## Related

| Document | Why |
|---|---|
| [`streaming.md`](streaming.md) | The streaming exporter this pacing surface composes — the `content × format × sink` model, the cross-kind merge and `seq`, the sinks whose delivery pacing flushes per line |
| [`anchor.md`](anchor.md) | The effective-anchor resolution surface whose CLI-wins-per-knob precedence clock resolution mirrors, and whose `None`-for-absent shape `ResolvedClock` follows |
| [`streaming-routing.md`](streaming-routing.md) | The routing surface that stamps the `topic` each paced `<topic>.jsonl` is flushed to, and the declared-but-empty-topic guarantee paced delivery preserves |
| [`config-docstrings.md`](config-docstrings.md) | The three-channel docstring convention `ClockConfig` follows |
| [`config/models.py`](../../src/fabulexa_export/config/models.py) | The `ClockConfig` grammar these semantics bind |
| [`../CAPABILITIES.md`](../CAPABILITIES.md) | Feature inventory and status |
| [`README.md`](README.md) | Design index, package layout, staged roadmap |
| [`../../CLAUDE.md`](../../CLAUDE.md) | Principles, the isolation boundary, vocabulary |
