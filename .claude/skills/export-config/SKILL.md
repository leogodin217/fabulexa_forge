---
name: export-config
description: Author or edit a Fabulexa export config YAML — read the bundle's atlas and sub-type partition to learn what the simulation's kinds actually mean in the domain, pick the envelope (dimensional ExportConfig or StreamConfig), adapt the nearest known-good recipe, look up only the config types you change in the models via cclsp, then run-iterate to a clean export and profile the output before shipping. Use when writing or modifying an export or streaming config.
argument-hint: [what the export should produce]
---

# Export-Config Authoring

Produce or edit an export config YAML that runs clean **and means something**:
`fabulexa-forge export` / `fabulexa-forge stream` exits 0 against an emit, *and* the
dataset it writes is a faithful, domain-named model of what that emit actually
contains.

Exit 0 is the cheap half. Every naming, split, and grain mistake this skill exists to
prevent exits 0 — a dimension of 400k versions over 31k entities, an all-NULL column
that belongs to another sub-type, a product catalogue that is one-third the version
history of a single server. The loader cannot catch any of them. You catch them by
reading the domain first (§ The bundle speaks simulation) and profiling the output
after (§ Workflow step 5).

## The bundle speaks simulation, not your domain

An emit is written in **one common format, optimized for the simulator and for machine
readability**. Its table and kind names are *engine ontology* — the same handful of
nouns describes a hospital, a retailer, and a ride-share marketplace:

| Simulation kind | What it actually is — decided per emit, never by the name |
|---|---|
| `actor` | Whoever/whatever the simulation drives. A patient. A shopper. A driver. Often sub-typed into several unrelated populations. |
| `entity` | The passive catalogue. Wards, theatres and medications; products and a storefront host; pickup zones. Almost always sub-typed. |
| `journey_instance` | One run of a state machine — a care episode, a shopping session, a trip. Its value is usually its *state timeline*, not its record row. |
| `resource` | Something with capacity that gets held and released. A consultant. A vehicle. |
| `queue`, `tick_decision`, `diary` | Engine mechanism (scheduling, per-tick choices, appointment books). Sometimes bookkeeping to drop; sometimes the central fact — decide from the atlas, not the name. |

Some emits add their own kinds (`booking`, `pairing`). Those are simulation-declared
too — closer to the domain, still not automatically warehouse-ready names.

**Consequence: every output name is a domain name.** `entity` filtered to
`entity_type = product` is `dim_product`, not `dim_entity`. `actor` in a hospital emit
is `dim_patient`. `record_id` becomes `patient_id` / `product_id`, not `id`; a fact's
FK column carries the same name as the key column it points at. A config whose output
tables still read `entity` / `actor` / `journey_instance`, or whose every dim keys on
`id`, has not been authored yet — it has been transcribed.

**The domain meaning is shipped inside the bundle.** You never have to guess it, and
guessing is a Principle #7 violation. Three sources, in order:

1. **`<emit_dir>/ATLAS.md`** — prose for every kind *and every sub-type*
   (`actor.customer`, `entity.infrastructure`), plus each journey's states. This is the
   semantic key. Read it before anything else.
2. **`base.json`** — `record_roles` (warehouse role per kind/sub-type),
   `sub_type_columns` (**which columns are real for which sub-type**), and each
   column's `temporal_class` / `history_tracked` / `references`.
3. **`run.duckdb`** — the counts that settle grain and sparsity questions.

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
| `fabulexa-forge export` / `fabulexa-forge stream` | The **mechanical** gate (Pydantic load + the full reshape against an emit). Necessary, not sufficient — it judges grammar, never meaning. | Run until exit 0, then profile the output (§ Workflow step 5). This repo has **no config-only `validate` verb**; running against an emit is the gate. |
| `docs/examples/<domain>/` | **Worked full-domain configs** — a whole bundle (`bundle/`) with its atlas, and curated `dimensional.yaml` / `source.yaml` / `base.yaml` / `stream.yaml` beside it. Where recipes teach one knob against a domain-agnostic fixture, these show a complete star built from real domain reasoning. `nhs/dimensional.yaml` is the exemplar: read its header decision log. | Read as a model of *reasoning*, never copy its claims — see the warning under § Decision rules. |

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

**Source specifics.** Source declares its output shape table by table — *things get
tables, events get the log* (see
[`../../docs/architecture/source.md`](../../docs/architecture/source.md)). The
`source:` section is required and takes `tables` (each entry a `state` table over a
`kind:`, or a `junction` over a `membership:`), the single `events:` log, and
`declare_keys`. A config declaring no output is a load-time error.

Three things about the grammar are easy to get wrong:

- **`name:` is author-verbatim, and it is the naming surface.** Nothing defaults it
  for you, so a config that writes `name: entity` has *chosen* the simulator's noun.
  Every table name here is a domain decision — see § The bundle speaks simulation.
- **`sub_types:` narrows rows, NOT columns.** A table declared over one sub-type still
  projects the whole kind's column set, so the other sub-type's columns come out
  100% NULL. To get a clean table, enumerate `columns:` from that sub-type's
  `sub_type_columns` partition. This is invisible until you profile the output.
- **Structural columns are nameable in `columns:`.** Selecting `columns:` drops the
  lifecycle unless you ask for it: list `created_sim_time`, `active`,
  `deactivated_at`, `last_mutation_sim_time` to keep the `created_at` / `active` /
  `deactivated_at` / `updated_at` soft-delete quartet the source archetype wants.

## Workflow

1. **Read the domain before the grammar.** Non-optional whenever the target is a real
   emit rather than the recipe fixture. You are looking for what each kind and sub-type
   *is*, which columns are real for which sub-type, and where the volume sits.

   ```bash
   ./tools/mdnav <emit_dir>/ATLAS.md      # then read Types (per sub-type) + Journeys
   ```
   ```bash
   uv run python - <<'EOF'
   import json; s = json.load(open("<emit_dir>/base.json"))
   print("record_roles:", json.dumps(s.get("record_roles"), indent=1))
   print("sub_type_columns:", json.dumps(s.get("sub_type_columns"), indent=1))
   for t in s["tables"]:
       if not t["name"].startswith("records__"):
           continue
       print("--", t["name"])
       for c in t["columns"]:
           if c["name"].startswith("prop__"):
               print("   ", c["name"], c.get("type"), c.get("temporal_class"),
                     "-> " + c["references"] if c.get("references") else "")
   EOF
   ```
   ```bash
   uv run python - <emit_dir> <<'EOF'
   import json, sys, duckdb
   b = sys.argv[1]
   s = json.load(open(f"{b}/base.json"))
   c = duckdb.connect(f"{b}/run.duckdb", read_only=True)
   for kind in sorted({t["name"].split("__")[1] for t in s["tables"]
                       if t["name"].startswith("records__")}):
       cols = {col["name"] for t in s["tables"] if t["name"] == f"records__{kind}"
               for col in t["columns"]}
       n = c.execute(f"select count(*) from records__{kind}").fetchone()[0]
       disc = f"prop__{kind}_type"
       split = c.execute(f"select {disc}, count(*) from records__{kind}"
                         f" group by 1 order by 2 desc").fetchall() if disc in cols else ""
       print(f"{kind:20} {n:>7}  {split}")
   print("\nhistory rows per tracked property (vs the kind's record count above):")
   for row in c.execute("select kind, property, count(*) from history"
                        " group by 1, 2 order by 3 desc limit 15").fetchall():
       print("  ", row)
   EOF
   ```

   That last report is the one that decides grain. A kind whose tracked property has
   *many times* its record count is carrying a timeline, not an attribute — retail's
   31,519 shopping sessions against 399,872 `current_state` rows is a fact wearing a
   dimension's name.

   Write down the kind → domain mapping (kind/sub-type, what it is, output name, row
   count) **before** any YAML. Naming, the split/conform call, and the grain call all
   fall out of that table. If the atlas leaves a kind genuinely ambiguous, ask the
   author — do not name it from the engine noun.

2. **Pick the shape and the nearest recipe.**
   - Decide dimensional vs source vs streaming (table above). For a from-scratch
     dimensional config against a real emit, `fabulexa-forge init <emit_dir>` gives a
     candidate to edit (source and streaming have no `init` candidate — author from a
     recipe).
   - `tools/mdnav docs/recipes/README.md`, pick the recipe matching your capability,
     copy its `config.yaml`. Edit it — add/remove tables, columns, kinds, properties,
     routing — rather than authoring from a blank file.

3. **Look up only the types you change.** For any field whose value is a config block
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

4. **Run-iterate against an emit until exit 0.** Build the recipe fixture emit once:
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
   support). Fix and re-run.

5. **Profile the output — the semantic gate.** Exit 0 means the grammar was legal, not
   that the dataset is right. Point this at what you just wrote:

   ```bash
   uv run python - <<'EOF'
   import duckdb; c = duckdb.connect("/tmp/out.duckdb", read_only=True)
   for (t,) in c.execute("select table_name from information_schema.tables"
                         " order by 1").fetchall():
       n = c.execute(f'select count(*) from "{t}"').fetchone()[0]
       print(f"\n{t}: {n} rows")
       for (col,) in c.execute("select column_name from information_schema.columns"
                               " where table_name = ? order by ordinal_position",
                               [t]).fetchall():
           pop = c.execute(f'select count("{col}") from "{t}"').fetchone()[0]
           if n and (n - pop) / n > 0.30:
               print(f"   {100 * (n - pop) // n}% NULL: {col}")
   EOF
   ```

   Read the NULL report for three signatures. An open SCD-2 window makes `valid_to`
   legitimately NULL on current versions — that one is expected; the rest are not:

   - **A 100%-NULL column** is a `prop__` column projected outside its sub-type's
     `sub_type_columns` list — structurally inapplicable, silently empty. Drop it.
   - **Two blocks of columns with complementary NULL rates** (say 37% and 62%) means you
     conformed two sub-types into one table that should have been split: each block is
     one sub-type's columns, NULL for the other's rows. Split on the discriminator.
   - **Rows vs. entities.** For every dim, compare `count(*)` to `count(distinct <key>)`
     and to the source population's row count in the emit. Far more rows than entities
     is a fact that has been mislabelled a dimension (§ Decision rules).

   Then check the joins are writable, which the profile cannot see: every FK resolves,
   and for an SCD-2 target the fact carries a wallclock column comparable to
   `valid_from` / `valid_to`. Without one, the point-in-time join the star advertises
   cannot be written at all.

   Fix and re-run from step 4. The config is done when it exits 0 **and** this profile
   holds no surprises you cannot justify from the atlas.

## Decision rules

The four calls where a legal config goes wrong. All four are settled by step 1's
reconnaissance, never by the shape of the kind's name.

- **Split on sub-type, or conform?** If `sub_type_columns` gives the sub-types disjoint
  column sets, **split** — one dim per sub-type, each projecting only its own columns.
  Conform only when the sub-types genuinely share a column set *and* something needs a
  single destination (a polymorphic reference must land on one dim). "The column →
  sub-type mapping would be guesswork" is never a reason: read `sub_type_columns` and
  the atlas, which name it outright.
- **Never project a column outside its sub-type's declared list.** It exports all-NULL.
  This is what the partition is *for*.
- **A state machine is a fact, not a dimension.** If a kind's only history-tracked
  property is a state-machine position (`current_state` and friends), its value is the
  state timeline: export it at `history_interval` grain, with a thin Type-1 dim for the
  genuinely constant attributes if one is needed. Compare `history` rows to record
  count — versions far exceeding entities settles it. `record_roles: dimension` does
  **not** settle it; the registry describes the simulation's ontology, and overriding it
  on evidence is the author judgment this skill exists to apply.
- **Name in the domain, and make the joins match.** Output tables and columns carry
  domain names; a fact's FK column is named for the key column it points at, so the
  star can be joined by someone who never saw the emit.

**A warning about decision logs.** The curated configs under `docs/examples/` carry a
header comment recording what was dropped, split, renamed, and why. Adopt that
convention — it is the right one. But **every factual claim in it must be re-derived
against this emit's own sidecar and data.** A claim copied from a sibling config
(`"the sidecar carries no sub_type_columns"`, `"that derivation is refused on this
grain"`) becomes a confident, load-bearing falsehood that justifies a bad model and
survives review because it reads authoritative. If you write a claim, you ran the query
that shows it. A stale decision log is worse than none.

## Hard rules

- **Never invent config values (Principle #7).** Grains, keys, kinds, column/table
  names, FK targets, sub-type discriminators, `topic_template` / `groups`, rebase
  origins — these are the AUTHOR's to specify, and must name things that actually exist
  in the target emit's sidecar. If a value is unknown, ASK the author; do not default
  it. A loader/exporter erroring on missing or unknown config is correct behavior, not
  a bug to paper over.
- **Domain meaning is read, never inferred from a name.** The kind is `entity`; what it
  *is* comes from the atlas, `sub_type_columns`, and the data. Naming an output from
  the engine noun, or deciding a split/grain question from `record_roles` alone without
  looking at the rows, is the characteristic failure of this skill — see § The bundle
  speaks simulation.
- **Exit 0 is half the gate.** Ship only after the output profile in step 5. Every
  serious modeling defect this skill guards against exits 0.
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
