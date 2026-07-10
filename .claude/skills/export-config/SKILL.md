---
name: export-config
description: Author or edit a Fabulexa export config YAML — pick the envelope (dimensional ExportConfig or StreamConfig), adapt the nearest known-good recipe, look up only the config types you change in the models via cclsp, then run-iterate to a clean export/stream. Use when writing or modifying an export or streaming config.
argument-hint: [what the export should produce]
---

# Export-Config Authoring

Produce or edit an export config YAML that loads and runs clean — `fabulexa-forge export`
or `fabulexa-forge stream` exits 0 against an emit.

## Source of truth (never hand-author field shapes)

Everything you need is DERIVED from the recipe corpus and the config model tree. There
is **no author-facing field reference** in this repo — by design (field docs are
developer-only; see `docs/architecture/config-docstrings.md`). You learn fields by
copying a recipe and, when you change a type, reading that model class via cclsp.

| Material | What it gives you | How to read it |
|---|---|---|
| `docs/recipes/README.md` | **Capability index — the primary copy-adapt source.** One narrow recipe per capability (SCD-2 dim, fact from history/membership, FK via reference/membership, `lookup`, the four derived columns, `exclude`, `rebase`, table/column rename; streaming: state-changes CDC, identity tombstone, multi-kind global `seq`, stream `rebase`). | `tools/mdnav` the index, pick the recipe whose capability matches, copy its `config.yaml`. |
| `src/fabulexa_forge/config/models.py` | **Every config type: its fields, types, required/optional, and the cross-field rules.** The authoritative field shapes (Code Is Truth). | cclsp ONLY — `find_definition` / `get_hover` on the type name (`FkClause`, `SourceDecl`, `StreamKindSelection`, `RoutingConfig`…). The class docstring + attribute docstrings + `@model_validator` docstrings carry the field meaning and the rules. **Never read the whole 661-line file to find one field.** |
| `fabulexa-forge init <emit_dir> [<out>]` | A commented **candidate `mode: dimensional` config** inferred from the sidecar (roles, SCD, sub-type splits, FK candidates). A starting point to edit, not a finished config. | Use when authoring a dimensional config from scratch against a real emit. Classification stays author-authoritative — edit the candidate. |
| `fabulexa-forge export` / `fabulexa-forge stream` | The authoritative gate (Pydantic load + the full reshape against an emit). | Run until exit 0. This repo has **no config-only `validate` verb**; running against an emit is the gate. |

Pick the smallest starting point that already does what you need, by capability not by
size: SCD-2 dimension → `dim-scd2-from-records`; FK through a membership edge →
`fact-fk-via-membership`; a CDC change stream → `streaming/state-changes`; choose your
own event wallclock → `streaming/rebase-ts`.

## The three shapes — pick first

This package has two top-level config envelopes, and three shapes across them; the
top-level key tells you which:

| Shape | Envelope | Distinguishing key | Section / lists | Run with |
|---|---|---|---|---|
| Dimensional reshape | **`ExportConfig`** | `mode: dimensional` | a `dimensional:` section (`tables`, optional `exclude`); optional `rebase`, `incremental` | `fabulexa-forge export <emit> <config> <out> --fmt <csv\|duckdb>` |
| Source reshape (operational dump) | **`ExportConfig`** | `mode: source` | an optional `source:` section (`change_delivery`, `exclude`, `rename`); optional `rebase`, `incremental` | `fabulexa-forge export <emit> <config> <out> --fmt <csv\|duckdb>` |
| CDC delivery | **`StreamConfig`** | `content: state-changes` | a `kinds:` list; optional `routing`, `rebase`, `debezium` | `fabulexa-forge stream <emit> <config> --fmt <jsonl\|debezium> --sink <stdout\|file> [--out <dir>]` |

Dimensional and source are two `mode`s of the *same* envelope (a discriminated
union — `mode_section_matches` enforces the named mode's section is present and the
other's is absent) and load through the one `load_export_config`. `StreamConfig` is a
genuinely separate envelope with its own loader, `load_stream_config` — streaming is a
delivery driver, not a mode of `ExportConfig`.

All three accept the `--base-date` / `--timezone` rebase overrides.

**Source specifics.** Unlike dimensional, source classifies every table
*automatically* from the sidecar (`record_roles` × `history_tracked` — the genre
trichotomy: change-log / reference / transaction / junction; see
[`../../docs/architecture/source.md`](../../docs/architecture/source.md)). There is no
grain/table declaration to author — the `source:` section is only three optional
knobs: `change_delivery` (default per-genre vs. `snapshot`), `exclude`
(kinds/tables), `rename` (collision escape hatch). Table and column names default from
sidecar identity (kind name, sub-type, `<K>_<p>` for a junction) — see source.md §
Operational presentation defaults before assuming a name.

## Workflow

1. **Pick the shape and the nearest recipe.**
   - Decide dimensional vs source vs streaming (table above). For a from-scratch
     dimensional config against a real emit, `fabulexa-forge init <emit_dir>` gives a
     candidate to edit (source and streaming have no `init` candidate — author from a
     recipe).
   - `tools/mdnav docs/recipes/README.md`, pick the recipe matching your capability,
     copy its `config.yaml`. Edit it — add/remove tables, columns, kinds, properties,
     routing — rather than authoring from a blank file.

2. **Look up only the types you change.** For any field whose value is a config block
   (`fk:`, `derived:`, `source:`, `routing:`, a `kinds[]` entry…):
   - Watch for the `source:` name collision — a *dimensional table's* per-table
     `source:` field (its grain binding, type `SourceDecl`) is unrelated to the
     top-level `source:` section of a `mode: source` config (type `SourceConfig`,
     `exclude`/`rename`/`change_delivery`). Same key, two different types depending
     on where it appears — check which envelope/mode you're in before you cclsp.
   - cclsp `find_definition` / `get_hover` on the corresponding model class
     (`FkClause`, `DerivedSpec`, `SourceDecl`, `SourceConfig`, `RoutingConfig`,
     `StreamKindSelection`).
   - Read its attribute docstrings (field meaning) and `@model_validator` docstrings
     (the cross-field rules — e.g. `via='membership'` forbids `path`; a `DerivedSpec`
     sets exactly one of ordinal/value_map/timestamp/scd_window/elapsed; a `ColumnDecl`
     sets exactly one source mode).
   - cclsp one symbol at a time. Do **not** dump `models.py` to find one field.

3. **Run-iterate against an emit until exit 0.** Build the recipe fixture emit once:
   ```bash
   uv run python - <<'EOF'
   from pathlib import Path
   from tests.recipes._recipe_fixture import build_recipe_emit
   build_recipe_emit(Path("/tmp/recipe-emit"))
   EOF
   ```
   Then run your config against it (or against the author's own emit):
   ```bash
   fabulexa-forge export /tmp/recipe-emit my-config.yaml /tmp/out.duckdb --fmt duckdb
   # or
   fabulexa-forge stream /tmp/recipe-emit my-stream.yaml --fmt jsonl --sink file --out /tmp/out
   ```
   Exit 0 → the config loaded and the reshape ran. A non-zero exit names what failed
   (a `ConfigError` for a bad/unknown/missing field or a violated validator; an
   `ExporterError` for a semantic problem — a kind/column/edge that the sidecar doesn't
   support). Fix and re-run. The config is done only when it exits 0.

## Hard rules

- **Never invent config values (Principle #7).** Grains, keys, kinds, column/table
  names, FK targets, sub-type discriminators, `topic_template` / `groups`, rebase
  origins — these are the AUTHOR's to specify, and must name things that actually exist
  in the target emit's sidecar. If a value is unknown, ASK the author; do not default
  it. A loader/exporter erroring on missing or unknown config is correct behavior, not
  a bug to paper over.
- **Faithful reshaping (Principle #3).** Every output column must trace to a base-layer
  value — `from` / `fk` / `lookup` / `derived` over real columns. The config never
  fabricates data. Only a corrupter may break conformance — that's a third, sibling
  envelope (`CorruptConfig`) with its own grammar and its own skill,
  [`corrupt-config`](../corrupt-config/SKILL.md); this skill writes neither corrupters
  nor base output.
- **The model is read-scoped, not read-whole.** cclsp → one type. Loading all of
  `models.py` to find one field is the exact anti-pattern this skill avoids.
- **Code Is Truth.** The models are authoritative; there is no prose field spec to read
  or to drift. If a recipe and the model disagree, the model wins (and the recipe is a
  drift bug — see below).

## Side effect — noticing drift

Reaching for the nearest recipe is also how recipe drift surfaces. While authoring, if
you notice any of these, flag it (a `note` finding, or fix in passing):

- A capability you need has **no recipe** — especially a knob a recent sprint shipped
  (e.g. a new `routing` field, a new derived kind). Propose the recipe to add.
- A recipe exists on disk under `examples/recipes/**` but is **missing from the index**
  in `docs/recipes/README.md`, or an index entry points at a folder that's gone.
- A recipe's `config.yaml` comments **disagree with the model** (the model wins).

This is not the skill's main job — authoring a clean config is — but a missing or
mis-filed recipe is the most common thing you'll trip over, so surface it.

## When authoring a recipe (not just a one-off config)

If the output is a new recipe for the corpus (not a throwaway config):

- **One capability per recipe** — a minimal diff against the nearest existing recipe.
- It runs against the **recipe fixture emit** (`tests/recipes/_recipe_fixture.py`), so
  every `expect.yaml` assertion must be hand-traceable to that fixture's contents (do
  not invent counts or values — Principle #7).
- The folder holds exactly `{config.yaml, expect.yaml}`; place streaming recipes under
  `examples/recipes/streaming/<name>/`, dimensional ones flat under
  `examples/recipes/<name>/`.
- Add it to the `docs/recipes/README.md` index, and confirm the gate is green:
  `uv run pytest tests/recipes/`.
