# security-logs — a 90-day SIEM corpus with an unlabeled intrusion

A segmented corporate network, simulated for 90 days starting 2026-01-05:
117 internal hosts (developers, finance, kiosks, service accounts) run
role-specific session journeys against a 42-server service estate behind a
9-rule firewall. After a 30-day clean baseline, four external hosts begin
approaching the network — probing routes, stuffing credentials, spreading
their destination reach through shared subnets. **No row anywhere says
"attack."** The signal is behavioral: a shift in per-host reach, deny rates,
and failed-auth clustering.

One bundle (`bundle/`), four export configs — each a different data product
over the same 440,107 events.

> **⚠️ The base export is the answer key.** `base.yaml` deliberately exports
> every ground-truth label the other three configs hide — `host.prop__role`
> (`'external'` flags the four attackers), `session.prop__journey_type`
> (`approach_*` flags the intrusion sessions), and the engine's state
> narration. Hand students the dimensional, source, or stream outputs;
> keep the base export for grading and hint-giving.

## The four exports

| Config | Mode | Shape | Role in the story |
|---|---|---|---|
| `dimensional.yaml` | dimensional | Star schema: `fact_security_event` (440,107 rows, unified event stream) over `dim_host` / `dim_server` (SCD-2) / `dim_account` / `dim_firewall_rule` | The analyst's warehouse. Analytical event vocabulary (`connection_attempt`, `auth_failure`, …) |
| `source.yaml` | source | Normalized app database: 4 inventory tables, `sessions` (correlation), `security_events` (immutable log), `event_entities` (polymorphic link) | The SIEM's backing store. Raw operational event vocabulary (`destination_chosen`, …) — the warehouse's `value_map` is the translation |
| `base.yaml` | base | One flat table per kind, every tracked property at its latest value | **The instructor's answer key** (see warning above) |
| `stream.yaml` | streaming | Two CDC topics: `security_events` (the live log) and `server_inventory` (asset drift) | The live feed — jsonl or Debezium envelopes, and the preset for the `fabulexa-forge mixer` live performance |

## Running the exports

From the repo root (output paths are yours to choose; CSV and stream sinks
need the directory to exist first):

```bash
# warehouse / app DB / answer key — DuckDB (single file) or CSV (directory)
fabulexa-forge export docs/examples/security-logs/bundle docs/examples/security-logs/dimensional.yaml out/warehouse --fmt duckdb
fabulexa-forge export docs/examples/security-logs/bundle docs/examples/security-logs/source.yaml     out/appdb     --fmt duckdb
fabulexa-forge export docs/examples/security-logs/bundle docs/examples/security-logs/base.yaml       out/answers   --fmt duckdb

# the live feed — file dry run (jsonl or debezium), or a Kafka performance
mkdir -p out/feed
fabulexa-forge stream docs/examples/security-logs/bundle docs/examples/security-logs/stream.yaml --fmt jsonl --sink file --out out/feed
fabulexa-forge mixer  docs/examples/security-logs/bundle docs/examples/security-logs/stream.yaml --fmt debezium --bootstrap-servers localhost:9092
```

All timestamps render as wallclock through the bundle's own anchor
(2026-01-05T08:00:00 UTC); no `rebase:` needed.

## What to look for (no spoilers)

Starting points that reward investigation — each visible in the dimensional
or source export without touching the answer key:

- **Reach.** Distinct destination servers per source host per day. Most
  hosts are habitual; breadth is a signal.
- **Denies.** Weekly `firewall_decision` outcomes — matched-deny vs.
  default-deny (`rule_id IS NULL`). The baseline is steady; watch for the
  lift and where it comes from.
- **Failed auth.** `auth_failure` events carry no `account_id` — join back
  to the session's `auth_attempt` rows via `session_id` (see the SPARSE
  note in `dimensional.yaml`). Which sessions present *several* accounts?
- **Inventory drift.** `dim_server` is SCD-2 for a reason: three mail
  servers move ports mid-run, and the metrics tier is decommissioned on
  2026-02-14 yet keeps receiving connections. Neither is the intrusion —
  distinguishing operational noise from attack signal is the exercise.

## Pointers

- `bundle/ATLAS.md` — the generator-facing description of every journey,
  type, and influence rule (spoilers throughout; instructor material).
- Each config's header comments — the full curation rationale: what was
  dropped, renamed, or coalesced, and why.
- `docs/recipes/` — minimal single-feature configs for learning the export
  grammar itself.
- `docs/architecture/` — `dimensional.md`, `source.md`, `base.md`,
  `streaming.md` for mode semantics.
