# FabulMixer live-perform demo

Replay a finished emit as a live, operator-mixable Kafka feed, and watch a
downstream consumer's pipeline stall when you perturb delivery. Driven entirely
from the board's sliders and buttons — launch is **paused** at **1× speed**; you
press play and drive everything live.

## Pieces

Three long-running processes, each in its own terminal:

| Terminal | Command | What it is |
|---|---|---|
| 1 | `make kafka-up` | The Kafka broker (background docker). Tear down with `make kafka-down`. |
| 2 | `make mixer-demo` | The mixer driver — replays `dev/demo/emit` through `dev/demo/config.yaml`, serving the control API on `:8765`. Foreground; Ctrl-C to stop. |
| 3 | `make board` | The live-perform board (vite) on http://localhost:5173, proxying `/api → :8765`. Foreground; Ctrl-C to stop. |

`make demo` prints this run order. `make mixer-demo` materializes the emit first
(`make demo-emit` on its own rebuilds it; output is gitignored).

## What you're driving

`make mixer-demo` launches the **consumer instrument** alongside the producer:
12-hour event-time windows and an `admission`(fact) → `patient`(dim) enrichment
join. Two topics → two channel strips on the board: `patient` and `admission`.

On the board:

1. **Press play, push the speed fader up.** At 1× the ~3-day event span replays in
   real time; crank the log-scale speed (up to 1000×) to replay it in minutes.
   Events flow to Kafka; the consumer pulls them; watermarks advance and windows
   fire.
2. **Lag or mute the `patient` strip** (producer side), or drop its ingest rate
   (consumer side). Either way the same per-topic position falls behind: the
   global watermark (`min` across topics) stalls, windows stop firing, and the
   join's null-rate climbs — *one slow stream stalls the whole pipeline*.
3. **Speed it back up / un-mute** to drain the backlog and watch the pipeline
   recover.

The lesson is two-sided causality: a topic falls behind whether the *producer*
stopped sending it or the *consumer* stopped pulling it. See
[`../../docs/architecture/mixer-consumer.md`](../../docs/architecture/mixer-consumer.md)
and [`../../docs/architecture/mixer-control-plane.md`](../../docs/architecture/mixer-control-plane.md).

## Running a bundled example preset

Pass `EXAMPLE=<name>` to `make mixer-demo` to use a bundled preset instead of the
fixture path. This calls `dev/demo/run.sh <name>`, which resolves the triple
(`bundle/`, `stream.yaml`, `demo.yaml`) from `docs/examples/<name>/` and exec-replaces
itself with `uv run fabexport mixer ...`.

Available presets:

| Name | Description |
|---|---|
| `ride-sharing-marketplace` | Ride-sharing marketplace (primary seed) — two-sided market; `driver`/`rider`/`pairing`/`zone_market` strips with live fact→dimension join lag. |
| `ride-sharing` | Ride-sharing — `actor`/`journey_instance` strips; pure read-back (no consumer join). |
| `retail` | Retail domain — `customer`/`entity`/`journey_instance` strips; pure read-back (no consumer join). |
| `nhs` | NHS domain (primarily a non-streaming fixture) — `nhs.actor`/`nhs.diary`/`nhs.journey_instance` strips; pure read-back (no consumer join). |

Run order with a preset (three terminals, same as the fixture path):

```
1) make kafka-up
2) make mixer-demo EXAMPLE=ride-sharing-marketplace
3) make board
```

Set `DRY_RUN=1` to print the resolved command without executing it (no broker needed):

```
DRY_RUN=1 make mixer-demo EXAMPLE=ride-sharing-marketplace
```

## Knobs

Override on the Make command line:

- `BOOTSTRAP=host:port` — Kafka bootstrap (default `localhost:9092`).
- `MIXER_FLAGS="..."` — replace the consumer flags (e.g. `MIXER_FLAGS=` for a
  producer-only run, or add more `--window <ms>` / `--join <fact>:<dim>`).
- `EXAMPLE=<name>` — use a bundled preset; dispatches to `dev/demo/run.sh <name>`
  instead of the fixture path. See preset table above.

## Files

- `build_emit.py` — materializes a real emit (reuses the recipe-test fixture
  builder; DuckDB + stdlib, no producer). Idempotent.
- `config.yaml` — the board-tuned streaming config (two kinds → two topics).
- `emit/` — the materialized emit. Gitignored; rebuilt on demand.
