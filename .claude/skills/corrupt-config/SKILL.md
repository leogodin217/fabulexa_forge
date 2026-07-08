---
name: corrupt-config
description: Author or edit a Fabulexa CorruptConfig YAML — pick the nearest recipe by operation kind and selector shape, look up only the operation/grammar types you change in the models via cclsp, then run-iterate against an emit until fabexport corrupt exits 0 AND defects.json declares exactly the impact you intended. Use when writing or modifying a corrupter config (null_cells, duplicate_rows, schema_drift, dangle_reference, freeze_series, drop_events, shift_sim_time).
argument-hint: [what defect the corruption should inject]
---

# Corrupt-Config Authoring

Produce or edit a `CorruptConfig` YAML that loads, runs, and **declares the defect you
meant to inject** — `fabexport corrupt` exiting 0 is necessary but not sufficient; see
§ Run-iterate.

This is the sibling skill to [`export-config`](../export-config/SKILL.md), scoped to
the third top-level envelope. Reach for `export-config` when the goal is a differently
*shaped* output (dimensional/streaming); reach for this skill when the goal is a
realistically-*broken* base emit. The two are never combined in one config — see
§ Hard rules.

## Source of truth (never hand-author field shapes)

There is no author-facing field reference for corrupters either. You learn the grammar
by copying a recipe and, when you change an operation kind, reading that model class
via cclsp.

| Material | What it gives you | How to read it |
|---|---|---|
| [`docs/recipes/README.md`](../../docs/recipes/README.md) § Corrupters | **Capability index — the primary copy-adapt source.** One narrow recipe per operation/selector/placement combination (cell-level, row/schema, and family-C temporal defects). | `tools/mdnav` the index, pick the recipe whose defect matches, copy its `config.yaml`. |
| `src/fabulexa_export/config/models.py` | **Every config type: its fields, types, required/optional, and the cross-field rules.** `Target` (the five-way selector: `table` / `tables` / `glob` / `category` / `record_kind`, exactly one set, plus optional `where` and column entries), `Amount` (`rate` xor `count`), `Distribution`, `Placement` (`entity_scoped` / `clustered_temporal` / `correlated`), and the seven operation models (`NullCells`, `DuplicateRows`, `SchemaDrift`, `DangleReference`, `FreezeSeries`, `DropEvents`, `ShiftSimTime`) plus `ShiftSimTime`'s `kind`-discriminated `ShiftSpec` union (`ShiftOffset` / `ShiftCollide` / `ShiftSwap`). | cclsp ONLY — `find_definition` / `get_hover` on the type name. The class docstring + attribute docstrings + `@model_validator` docstrings carry field meaning and cross-field rules (e.g. `FreezeSeries.cut`'s allowed values, `history`-only targets for family-C operations, `Target`'s exactly-one-selector rule). **Never read the whole file to find one field.** |
| `fabexport corrupt <emit_dir> --config <cfg> --out <out_dir>` | The authoritative gate (Pydantic load + the full corruption run against an emit). | Run until exit 0, then read `defects.json` — see § Run-iterate. There is no config-only validate verb for `CorruptConfig` either. |

Pick the smallest starting point that already does what you need, by defect kind not
by size: a missing-value defect on a named column → `null-and-dangle` or
`category-null-cells` (whole-class variant); a biased MNAR draw →
`mnar-correlated-nulls`; a row-level or schema defect → `drift-and-duplicates`; a
`history`-series defect → `frozen-status-series` / `event-outage-window` /
`clock-skew-and-collisions`.

## The one envelope

`CorruptConfig` is a third top-level envelope, sibling of `ExportConfig` /
`StreamConfig` — never a mode of either. Its top-level key is `seed` (the master seed)
plus `operations`, an ordered list of `kind`-discriminated operation blocks. Every
operation shares one grammar:

| Piece | Role |
|---|---|
| `target` | The selector — which table(s)/rows/columns the operation reaches. Five-way table selection (`table` / `tables` / `glob` / `category` / `record_kind`), an optional `where` row filter, optional exact-or-pattern column entries. |
| `amount` | The seeded quantity (`rate` — a fraction of the pooled population — xor `count` — an absolute number), plus an optional magnitude `Distribution` for operations that perturb a value. |
| `placement` | Optional biased-draw axis: `entity_scoped`, `clustered_temporal`, or `correlated` (MNAR-weighted). Omit for a uniform draw. |

Family C's three operations (`freeze_series`, `drop_events`, `shift_sim_time`) target
`history` only and select over two additional units beyond cell/row: the **event**
(one `history` row) and the **series** (one `(kind, record_id, property)` change
timeline). Load through `load_corrupt_config` (`config/loader.py`) — the corrupter
sibling of `load_export_config` / `load_stream_config`.

Run with `fabexport corrupt <emit_dir> --config <corrupt.yaml> --out <out_dir>`. The
output is always `run.duckdb` + `base.json` + `defects.json` into `out_dir`; the verb
refuses if `out_dir` already holds a `run.duckdb` or `base.json`.

## Workflow

1. **Pick the nearest recipe by defect, not by table shape.**
   `tools/mdnav docs/recipes/README.md`, read § Corrupters, pick the recipe whose
   operation/selector/placement combination matches what you need to inject. Edit it —
   change the target, amount, placement, or operation-specific fields — rather than
   authoring from a blank file.

2. **Look up only the types you change.** For any field whose value is a config block
   (`target:`, `amount:`, `placement:`, `shift:`, `rename_to:`...):
   - cclsp `find_definition` / `get_hover` on the corresponding model class.
   - Read its attribute docstrings (field meaning) and `@model_validator` docstrings
     (cross-field rules — e.g. `Target` requires exactly one of its five selector
     fields; `Amount` sets exactly one of `rate`/`count`; family-C operations reject a
     `target` naming anything but `history`).
   - cclsp one symbol at a time.

3. **Run-iterate against an emit until the manifest declares what you meant.** Build or
   reuse an emit (a real one, or the corrupt recipe fixture —
   `reader._fixtures_build.build_history_series` for family-C work; the shared
   `tests.recipes._recipe_fixture.build_recipe_emit` fixture is too thin for family C
   but fine for cell/row operations). Then:
   ```bash
   fabexport corrupt /tmp/some-emit my-corrupt-config.yaml --out /tmp/corrupt-out
   cat /tmp/corrupt-out/defects.json
   ```
   Exit 0 means the config loaded and every operation ran. **That is not the real
   gate.** A `where` filter matching zero rows, or a `slice_at` boundary an operation's
   row never crosses, can exit 0 while injecting fewer defects than intended, or
   defects whose impact never trips a check. Check `defects.json`'s `counts.by_class`
   and each defect's `impact` against your intent, and cross-check independently:
   ```bash
   fabexport validate /tmp/corrupt-out
   ```
   `validate`'s failing checks should union to the same impact codes the manifest
   declares (excluding the `beyond-c1-c12` sentinel, which never trips a check by
   design — see `event-outage-window`). A non-zero exit from `fabexport corrupt` names
   what failed (a `ConfigError` for a bad/unknown/missing field or a violated
   validator; a `CorruptValidationError` for a business rule the emit violates — e.g. a
   family-C operation targeting a non-`history` table). Fix and re-run.

## Hard rules

- **The Principle #3 exception, and its limit.** A corrupter is the one place in this
  package allowed to fabricate a value — but only the value each operation *declares*
  as its purpose: `dangle_reference`'s sentinel id, `schema_drift`'s rename/retype/
  drop, `null_cells`'s NULL, `duplicate_rows`'s copy, family-C's frozen/dropped/shifted
  event. Never invent selection criteria beyond what `target`/`where` state, and never
  reach for a corrupter to reshape data — that is `export-config`'s job.
- **Never invent target values (Principle #7).** `table` / `tables` / `glob` /
  `category` / `record_kind`, column entries, `where` keys/values — these must name
  things that actually exist in the target emit's sidecar. If unknown, ASK the author;
  a loader/validator erroring on an unresolvable target is correct behavior.
- **Structural conformance survives; semantic conformance is the point.** Output is
  still a structurally-conformant v4 base emit (C1–C5, C8 hold) — any exporter can run
  on it downstream. Only C6/C7/C9–C12 break, and only by the operations you declared.
  If a config accidentally breaks C1–C5/C8, that is a bug, not a feature.
- **`defects.json` is generated, never hand-edited.** It is the engine's own
  deterministic ground-truth artifact (same seed + config + code version → identical
  manifest); if it doesn't say what you intended, fix the config, not the manifest.
- **The model is read-scoped, not read-whole.** Same discipline as `export-config`:
  cclsp → one type, never the whole `models.py`.

## Side effect — noticing drift

While authoring, flag (a `note` finding, or fix in passing):

- A defect combination you need has **no recipe**, especially a knob a recent sprint
  shipped.
- A recipe exists under `examples/recipes/corrupt/**` but is missing from
  `docs/recipes/README.md` § Corrupters, or an index entry points at a gone folder.
- A recipe's `config.yaml` comments disagree with the model (the model wins).

## When authoring a recipe (not just a one-off config)

If the output is a new recipe for the corpus:

- **One operation (or one small, deliberately-composed set) per recipe** — a minimal
  diff against the nearest existing recipe.
- It runs against `build_history_series` if it touches `history` (family-C, or any
  recipe wanting a real change series); use the shared `build_recipe_emit` fixture
  otherwise. Every `expect.yaml` assertion must be hand-traceable to that fixture's
  contents (do not invent counts — Principle #7).
- `expect.yaml`'s `defect_counts` and `impact_union` are **exact**, not lower bounds —
  curate the recipe so every declared impact code actually fires on re-validation (no
  heals, no skip-guard cases); this is what makes the corpus's set-equality gate
  possible (`test_corrupt_recipe_agreement_set_equality`).
- The folder holds exactly `{config.yaml, expect.yaml}` under
  `examples/recipes/corrupt/<name>/`.
- Add it to `docs/recipes/README.md` § Corrupters, and confirm the gate is green:
  `uv run pytest tests/recipes/test_corrupt_recipes.py`.
