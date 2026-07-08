# fabulexa-forge

Downstream **exporter** + **corrupter** for Fabulexa composite base-layer emits.

Reads a base-layer emit (`run.duckdb` + `base.json`, `base_format_version 4`) and
writes differently-shaped datasets (exporters) or realistically-broken base layers
(corrupters). A downstream consumer of Fabulexa composite base-layer emits — the
vendored bundle contract is its only coupling.

> **Status: standalone.** Its own repo; the vendored `contract/` is the only coupling.
> Current stage: reader + conformance (trunk-only). See `docs/architecture/README.md`.

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
├── docs/architecture/        # design index + staged roadmap
├── src/fabulexa_export/      # package source
├── tests/
└── tools/                    # repo tooling (mdnav, hooks)
```
