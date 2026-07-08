# Fabulexa Composite Export

**Downstream exporter + corrupter for Fabulexa composite base-layer emits.**

Reads a base-layer emit (`run.duckdb` + `base.json`) and writes differently-shaped
datasets (**exporters**) or realistically-broken base layers (**corrupters**). A
downstream consumer of Fabulexa composite base-layer emits; its **only** coupling is
the vendored bundle contract in `contract/`.

```
base-layer emit (run.duckdb + base.json @ base_format_version 4)
        │
        ▼
   base reader  ──▶ exporters  (dimensional / source / streaming / base)
                └─▶ corrupters (data-quality injection)
        │
        ▼
   datasets (CSV, DuckDB, Parquet, JSONL)
```

## Status: standalone

This package is its own standalone repo. Its sole input is the bundle it receives
(`run.duckdb` + `base.json`); its sole coupling is the vendored contract in
`contract/` (below). It carries no dependency on whatever produces the bundle.

**Current stage: trunk-only.** The first implementation assumes a single branch
(`branches` has one entry). Multi-branch / fork-aware export (paired counterfactuals,
per-branch slices) is a deliberately deferred later stage. See
`docs/architecture/README.md` for the staged roadmap.

---

## The boundary (non-negotiable)

The **only** input is two files per emit — `run.duckdb` + `base.json` — at
`base_format_version: 4`, defined by `contract/base-format.md` +
`contract/base-format.schema.json` (vendored copies of the base-format spec).

- **The bundle + `contract/` is the only interface.** This package carries no
  dependency on whatever produces the bundle; the base-layer contract is the only
  coupling. The standalone `.venv` makes this physical — anything outside the contract
  is unresolvable, and mypy-strict and the tests surface any stray import.
- **Read the sidecar, not this spec.** `base.json` is the authoritative, *per-emit*
  table and column list. The spec is the *minimum* — producers add provenance columns
  and future-version fields. Never hard-code column lists from the spec.
- **Version-gate.** Refuse any `base_format_version` the vendored contract does not
  define. No auto-upgrade.
- **Vendored, not linked.** `contract/` holds copies, re-synced when the spec version
  bumps (see `contract/README.md`). The contract lives only at `contract/`; read it
  there — there is no other tree to reach into.

---

## Environment (which venv)

This package has its **own** venv (`.venv` at the repo root) and is a standalone
uv project with its own lock. Only this package's own dependencies are
installed there, so the boundary is physical: anything outside the bundle contract is
simply not importable.

**Work through `uv run` or `make` from the repo root:**

```bash
uv sync            # create / update .venv
uv run pytest      # or: make test / make check  — all resolve this repo's .venv
```

- `uv run` and `make` always resolve this repo's `.venv`. If some other repo's `.venv`
  happens to be the active `VIRTUAL_ENV`, uv ignores the mismatch with a warning —
  that warning is expected and harmless.
- **Run this code only in its own `.venv`** — never under another project's
  interpreter where extra packages happen to be installed. That is the one way to
  defeat the boundary: an import that should be unresolvable would resolve. `uv run`
  and `make` always select the right venv; the discipline is to use them.

---

## Core Principles

Non-negotiable. Every decision must be checkable against these.

1. **Authors succeed without code.** Export and corrupt targets are described in YAML.
   The code is domain-agnostic — no `if nhs:` / `if retail:` branches. All
   domain-specific behavior comes from configuration.
2. **All output is configurable.** No hardcoded target schemas, table names, or
   domains.
3. **Faithful reshaping — reshape, never fabricate.** Every exporter output value
   traces to a base-layer value. Exporters may drop, rename, denormalize, aggregate,
   or reconstruct point-in-time state — never invent. *Corrupters are the sole
   exception:* they intentionally break **semantic** conformance (C6/C7) to inject
   realistic data-quality defects while preserving **structural** conformance (C1–C5).
   Breaking the data is the corrupter's declared purpose; an exporter never does it.
4. **Referential and temporal integrity preserved.** Exporters introduce no dangling
   references, no forward references, and no non-monotonic time. These guarantees come
   in via the base layer; an exporter must not destroy them.
5. **Realistic complexity, faithfully.** Exporters may target clean OLAP shapes or
   messy OLTP shapes (change logs, late-arriving data). Fidelity to the source, not
   messiness, is the invariant.
6. **Good enough beats perfect.** Usefulness over statistical or structural
   perfection.
7. **No invented mapping values.** The exporter never invents the grain, keys, or
   target schema the author must specify. Missing required export config = error at
   load time, not a silent default or fallback.
8. **No future scaffolding.** No stub modules, no-op loops, or `# TODO` placeholders
   for features that don't exist yet. Add code when the feature lands.
9. **Breaking changes are acceptable — internally.** Greenfield package; redesign
   freely, no back-compat shims. **But the base-format contract is external and is NOT
   ours to break.** We adapt to its version bumps; we never redefine it.
10. **Reader-first.** Every exporter and corrupter reads through the **one** base
    reader. No module opens `run.duckdb` or parses `base.json` ad hoc; none hard-codes
    column lists; all schema knowledge flows from the sidecar.

---

## Key Invariants

| Invariant | Meaning |
|---|---|
| Deterministic | Same emit + same export/corrupt config + same code version → identical output. |
| Faithful reshaping | Exporter output traces wholly to base-layer values; no fabrication. |
| Integrity preserved | Exporters emit no dangling/forward references; monotonic time survives the reshape. |
| Version-gated input | Unknown `base_format_version` → refuse to interpret. |
| Single coupling | The bundle + vendored `contract/` is the only interface; no dependency on the bundle's producer. |

---

## Vocabulary (reuse the base-format contract's terms exactly)

These come from the bundle contract (`contract/`) — **never alias them**: emit, base layer, sidecar
(`base.json`), `fork_path`, branch, trunk, slice / `slice_at`, pin / `pinned_ids`,
`runtime` anchor, firing, history (long-form SCD-2), membership interval,
fixed-category / records-category / membership-category table, provenance opt-in.

Repo-local terms: **exporter** (base → different shape), **corrupter** (base → broken
base), **export config**, **target shape**, **fidelity**, **reader**, **recipe**
(minimal single-feature export config, test-guarded — the primary author-facing doc).

## Audience

Downstream authors: educators, data engineers, ML researchers, analysts. They pick a
mode, adjust YAML, and run the CLI. They do not write Python.

---

## DO NOT

- Add any dependency on the bundle's producer — the vendored `contract/` is the only coupling.
- Hard-code table or column lists — read `base.json`.
- Interpret an unrecognized `base_format_version`.
- Invent target grain, keys, or schema the author must specify (Principle #7).
- Write code for features that don't exist yet (Principle #8).
- Look anywhere but the vendored `contract/` for the input spec — it lives only there.
- Fabricate data in an exporter (Principle #3). Only corrupters break conformance, and
  only C6/C7.

---

## Documentation Map

| Location | Contents | When |
|---|---|---|
| `CLAUDE.md` | Principles, invariants, boundary, vocabulary | Always |
| `contract/base-format.md` + `.schema.json` | The input contract (vendored) | Reading or writing the reader |
| `contract/README.md` | How the contract is vendored + re-synced (do not hand-edit) | Before touching `contract/` |
| `docs/CAPABILITIES.md` | Feature inventory + status (exporter modes, corrupters, reader) | Tracking what to build / what's shipped |
| `docs/architecture/README.md` | Design index, package layout, staged roadmap, status | Planning / orientation |
| `src/fabulexa_export/` | Code is the contract; docstrings + tests own behavior | Implementing |

## Context Efficiency

- Read the sidecar (`base.json`) before the DuckDB — it tells you what exists.
- Don't re-read files already read this session.
- Keep explanations proportional to complexity.
