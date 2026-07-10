# fabulexa-forge

Downstream **exporter** + **corrupter** for Fabulexa composite base-layer emits.

Reads a base-layer emit (`run.duckdb` + `base.json`, `base_format_version 4`) and
writes differently-shaped datasets (exporters) or realistically-broken base layers
(corrupters). A downstream consumer of Fabulexa composite base-layer emits — the
vendored bundle contract is its only coupling.

> **Status: standalone, trunk-only.** Its own repo; the vendored `contract/` is the
> only coupling. The reader + C1–C12 conformance, the dimensional / source / streaming
> exporters, the corrupter family, and a live streaming **mixer** have shipped;
> multi-branch / fork-aware export is deliberately deferred. See
> [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) for the feature inventory and
> [`docs/architecture/README.md`](docs/architecture/README.md) for the staged roadmap.

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
├── src/fabulexa_forge/      # package source — the fabulexa-forge CLI + library
├── tests/
├── examples/recipes/         # minimal, test-guarded author recipes (one per feature)
├── docs/                     # architecture index, capabilities, recipes, roadmap
├── dev/                      # local demo + Kafka rig (not shipped in the wheel)
├── frontend/                 # FabulMixer live-perform UI — a throwaway Vue POC
├── tools/                    # repo tooling (mdnav, hooks)
└── .claude/                  # AI-agent skills/config — tracked as a workflow showcase
```
