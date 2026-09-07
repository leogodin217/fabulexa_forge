# fabulexa-forge

Multi-mode, multi-format exporter for Fabulexa simulated bundles. Do you need data
for teaching, learning or practicing data skills? Fabulexa forge provides the interface
to give you the data how you need it. It is in active development, so expect more
to come.

Core Features
* Four pre-simulated datasets that model a real world with causality. (Black friday produces
more orders, a flu outbreak creates resource contention on hospital beds, etc.)
* Three export modes: streaming CDC (through Kafka), source (looks like OLTP), dimensional (An entire
data warehouse)
* Time rebasing that resets date ranges
* incremental exports with time windows
* Corruption that introduces common data-quality patterns
* Streaming demo with a mixer board and custom consumer. (Proof of concept of mixing streams with sliders
for rates and contention)

**Note**: This repo was carved out of a larger repo, hence the short git history.

## Concepts

Fabulexa: A synthetic data generator that simulates interconnected business processes.
Referential and temporal ingegrity is baked in, as is direct cause and effect modelling.
Provenance, forking with paired counterfactuals are implemented in the engine but the datasets
in this repo do not use those features.

Fabulexa Forge: While Fabulexa produces dataset bundles in a standard format, Forge shapes them into
more user-friendly formats.

corrupter: Fabulexa makes several guarantees about dataset bundles. The corrupter breaks them. If you want
to learn/teach SQL, dbt, etc. it is useful to have bad data. corrupter breaks the data contracts before
you export.

Bundle: Fabulexa-produced dataset with descriptive docs: .json with the schema, .md with a description. This
is the input to Fabulexa Forge.

Contract: ./contract/ Provides a JSON schema and instructions on using a bundle. Useful for development and
creating your own bundles.

## Getting Started

Start in docs/examples. You'll see four datasets minus the actual data. The data is in DuckDB and attached
as artifacts. Copy the duckdb into the same directory as the example configs. /examples shows various recipes
for configuration, or fetch a pack with `fabulexa-forge datasets get <name>` (see § Commands).

### Streaming Demo

make kafka-up
make mixer-demo EXAMPLE=ride-sharing
make board
Open http://localhost:5173

**Now onto the LLM-generated stuff**

Reads a base-layer emit (`run.duckdb` + `base.json`) and
writes differently-shaped datasets (exporters) or realistically-broken base layers
(corrupters). A downstream consumer of Fabulexa composite base-layer emits — the
vendored bundle contract is its only coupling.

* [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) for the feature inventory and
* [`docs/architecture/README.md`](docs/architecture/README.md) for the staged roadmap.

One name throughout: the distribution and the CLI are both `fabulexa-forge`; the
import package is `fabulexa_forge` — the standard hyphen↔underscore mapping, so
`pip install fabulexa-forge` then `import fabulexa_forge`.

## Install and run

Not yet published to PyPI — clone and run from source with
[uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/leogodin217/fabulexa_forge
cd fabulexa_forge
uv sync                 # resolve this project's own venv
uv run fabulexa-forge --help
```

## Commands

`fabulexa-forge` is the only entry point. Every verb below takes `<emit_dir>` — a
directory holding `run.duckdb` + `base.json` — except `compare` and `datasets`.
`fabulexa-forge <verb> --help` prints each verb's usage.

| Verb | What it does |
|---|---|
| `validate` | Run C1–C15 conformance checks against an emit. |
| `export`   | Reshape an emit per an export config (`mode: dimensional` / `source` / `base`) to CSV or DuckDB. |
| `init`     | Propose a candidate config (dimensional, source, or streaming) from the sidecar. |
| `stream`   | Replay the base layer as a CDC event stream to stdout, files, or Kafka. |
| `mixer`    | Replay the base layer as a live, operator-mixable Kafka feed with a control API. |
| `corrupt`  | Inject realistic data-quality defects, with a ground-truth `defects.json` manifest. |
| `compare`  | Compare two materialized datasets for exact equality. |
| `datasets` | List or download the published example dataset packs. |

Example — validate an emit, then export it to a DuckDB star schema:

```bash
uv run fabulexa-forge validate path/to/emit
uv run fabulexa-forge export path/to/emit config.yaml out.duckdb --fmt duckdb
```

**Exit codes.** `0` success · `1` error (usage, reader, config, or export failure) ·
`3` drained (`export --next` found no more windows). Two verbs carry their own contract:
`compare` returns `0` equal · `1` not equal · `2` input error; `datasets` returns `0`
success · `1` failure · `2` usage error.

**Shared anchor options** (`export`, `stream`, `mixer`). Both are optional; each wins
over the config's `rebase` block, which wins over the sidecar:

| Option | Meaning |
|---|---|
| `--base-date DATETIME` | ISO datetime the run's origin is shifted to (time rebasing). Precedence: flag → `rebase.base_date` → keep the sidecar origin. |
| `--timezone ZONE` | IANA zone wallclock columns render in. Precedence: flag → `rebase.timezone` → sidecar `runtime.timezone`. |

### `validate`

```bash
fabulexa-forge validate <emit_dir>
```

No options. Reports each failing check; exit `1` on any failure.

### `export`

```bash
fabulexa-forge export <emit_dir> <config.yaml> <out> --fmt csv|duckdb \
    [--base-date DATETIME] [--timezone ZONE] \
    [--next | --from START --to END]
```

The export mode comes from the config's `mode:` key. `<out>` is an output directory
for `--fmt csv` or a `.duckdb` file path for `--fmt duckdb`.

| Option | Meaning |
|---|---|
| `--fmt csv\|duckdb` | Required. Output format. |
| `--next` | Incremental export: emit the next window per the config's `incremental` cadence block, advancing the cursor stored in `<out>`. Exit `3` once the tape is drained. |
| `--from START --to END` | One-shot export of the half-open window `[START, END)`, no cursor. When an anchor resolves, bounds are naive civil datetimes in the anchor timezone (a bare date is midnight); with no anchor they are integer sim-time nanoseconds. `<out>` must not already exist. Not combinable with `--next`. |

### `init`

```bash
fabulexa-forge init <emit_dir> [out_path] [--mode dimensional|source|streaming]
```

Writes a candidate config to `out_path`, or to stdout when omitted. `--mode` defaults
to `dimensional`. The proposal is a starting point — every population and key election
is annotated with YAML comments for you to edit.

### `stream`

```bash
fabulexa-forge stream <emit_dir> <stream.yaml> --fmt jsonl|debezium --sink stdout|file|kafka \
    [--out DIR] [--bootstrap-servers HOST:PORT] \
    [--speed S] [--idle-cap SECONDS] [--fast] \
    [--base-date DATETIME] [--timezone ZONE]
```

| Option | Meaning |
|---|---|
| `--fmt jsonl\|debezium` | Required. Message format: plain JSON lines, or Debezium-envelope CDC. |
| `--sink stdout\|file\|kafka` | Required. `file` writes one file per topic and requires `--out DIR`; `stdout` and `kafka` reject `--out`. |
| `--out DIR` | Output directory for `--sink file`. |
| `--bootstrap-servers HOST:PORT` | Kafka brokers for `--sink kafka`; falls back to the `FABEXPORT_KAFKA_BOOTSTRAP` env var. |
| `--speed S` | Pace delivery in real time at `S`× sim-time speed. Overrides the config's `clock.speed`. |
| `--idle-cap SECONDS` | Cap the wallclock wait between consecutive events. Overrides the config's cap; needs a speed from `--speed` or the config. |
| `--fast` | Deliver unpaced (as fast as possible). Not combinable with `--speed` / `--idle-cap`. |

With none of the pacing flags and no `clock` block in the config, delivery is unpaced.

### `mixer`

```bash
fabulexa-forge mixer <emit_dir> <stream.yaml> --fmt jsonl|debezium \
    [--bootstrap-servers HOST:PORT] [--host 127.0.0.1] [--port 8765] \
    [--speed 1.0] [--play | --paused] [--tick 0.05] \
    [--base-date DATETIME] [--timezone ZONE] \
    [--consumer [--window MS ...] [--join FACT:DIM ...] \
                [--consumer-group ID] [--consumer-offset earliest|latest]]
```

Kafka-only (there is no `--sink`); requires the `[mixer]` install extra. Serves the
FabulMixer control API over HTTP so the board can play / pause / re-speed the feed and
lag, rate-limit, or mute each topic mid-run. See the streaming demo above.

| Option | Meaning |
|---|---|
| `--fmt jsonl\|debezium` | Required. Message format. |
| `--bootstrap-servers HOST:PORT` | Kafka brokers; falls back to `FABEXPORT_KAFKA_BOOTSTRAP`. |
| `--host` / `--port` | Control-API bind address. Defaults `127.0.0.1` / `8765`. |
| `--speed S` | Launch transport speed, `0.1`–`1000`. Default `1.0`. |
| `--play` / `--paused` | Launch transport state. Default paused. |
| `--tick SECONDS` | Scheduler tick interval. Default `0.05`. |
| `--consumer` | Also run the consumer-side instrument, which subscribes to the produced topics and reports watermark, window, and join health. The four options below require it. |
| `--window MS` | Tumbling-window size in event-time milliseconds; repeatable. |
| `--join FACT:DIM` | A fact/dimension topic pairing whose enrichment-join null rate is metered; repeatable. |
| `--consumer-group ID` | Kafka consumer group id. |
| `--consumer-offset earliest\|latest` | Initial consumer offset. Default `earliest`. |

### `corrupt`

```bash
fabulexa-forge corrupt <emit_dir> --config <corrupt.yaml> --out <out_dir>
```

| Option | Meaning |
|---|---|
| `--config PATH` | Required. Corrupter config YAML. |
| `--out DIR` | Required. Receives the broken `run.duckdb` + regenerated `base.json` plus `defects.json`, the ground-truth manifest naming every injected defect. Always written. |

The output is itself an emit — point `export` or `stream` at it.

### `compare`

```bash
fabulexa-forge compare <expected> <actual> [--tables NAME ...] [--max-row-diffs N] [--format text|json]
```

`<expected>` is a DuckDB file (an authoritative forge render); `<actual>` is a DuckDB
file or a directory of CSVs.

| Option | Meaning |
|---|---|
| `--tables NAME ...` | Compare only these tables. Default: every table in `expected`. |
| `--max-row-diffs N` | Cap on differing rows reported per table. Default `10`. |
| `--format text\|json` | Report rendering. Default `text`. |

### `datasets`

```bash
fabulexa-forge datasets list [--format text|json]
fabulexa-forge datasets get <name> [--dir DIR] [--force]
```

`list` is offline — it reads the catalog baked into the package. `get` downloads a
pack from GitHub Releases, verifies its checksum and size, extracts it, and prints
ready-to-run example commands.

| Option | Meaning |
|---|---|
| `--format text\|json` | `list` output rendering. Default `text`. |
| `--dir DIR` | `get` extraction directory. Default `./<name>`. |
| `--force` | `get` into a non-empty directory. Without it, an occupied target is refused. |

Export and corrupter targets are described in YAML — no Python. Learn each feature from
a minimal, test-guarded [recipe](docs/recipes/README.md).

## Boundary

The input is the two files per emit, defined by the vendored copies in `contract/`
(`base-format.md` + `base-format.schema.json`). This package has no dependency on
whatever produces the bundle — the standalone `.venv` makes that physical, and
mypy-strict plus the tests surface any stray import.

## Develop

This is a standalone uv project with its own lock and venv. Run everything from the repo root:

```bash
uv sync            # resolve this project's own venv
make check         # lint + typecheck + tests
```

## Layout

```
.
├── CLAUDE.md                 # principles, boundary, vocabulary
├── contract/                 # VENDORED base-layer contract (the only coupling)
├── src/fabulexa_forge/       # package source — the fabulexa-forge CLI + library
├── tests/
├── examples/recipes/         # minimal, test-guarded author recipes (one per feature)
├── docs/                     # architecture index, capabilities, recipes, roadmap
├── dev/                      # local demo + Kafka rig (not shipped in the wheel)
├── frontend/                 # FabulMixer live-perform UI — a throwaway Vue POC
├── tools/                    # repo tooling (mdnav, hooks)
└── .claude/                  # AI-agent skills/config — tracked as a workflow showcase
```

## Use of LLMs

This project is obviously LLM generated. .claude is committed and tracked. For this project,
I act as a product manager and Claude the architect and engineer. The process works really
well for stuff like this that is basically glorified scripts in a CLI.
