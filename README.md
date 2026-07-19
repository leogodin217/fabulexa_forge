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
for configuration. CLI download for datasets is coming in the future.

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

`fabulexa-forge` is the only entry point. It takes one base-layer emit (`run.duckdb` +
`base.json`) and either reshapes it (exporters) or breaks it realistically
(corrupters):

| Verb | What it does |
|---|---|
| `validate` | Run C1–C12 conformance checks against an emit. |
| `export`   | Reshape an emit per an export config (`dimensional` / `source`). |
| `init`     | Propose a candidate dimensional config from the sidecar. |
| `stream`   | Replay the base layer as a CDC event stream. |
| `mixer`    | Replay the base layer as a live, operator-mixable Kafka feed. |
| `corrupt`  | Inject realistic data-quality defects, with a ground-truth manifest. |

Example — validate an emit, then export it to a DuckDB star schema:

```bash
uv run fabulexa-forge validate path/to/emit
uv run fabulexa-forge export path/to/emit config.yaml out/ --fmt duckdb
```

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
